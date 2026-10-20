"""Releases (contract sections 3 and 4): DRAFT -> EVALUATING (Dev deployment if needed, then the dataset run) -> BLOCKED |
PENDING_APPROVAL -> APPROVED | REJECTED -> PROMOTING -> PROMOTED | FAILED.

Creating a release starts its run (`autoRun`, the default): the `release` worker job deploys the blueprint version to Dev unless
the Dev harness already runs it, then queues the `evaluate` job, which runs the evaluation dataset against the harness and records
the verdict (`platform_api.worker_jobs`, `platform_api.services.evaluation_run`). The release is EVALUATING meanwhile and carries
the run's progress in `pipeline`; a failed run puts it back to DRAFT with `error` set, and `POST /releases/{id}/run` starts it
again. `POST /releases/{id}/evaluation` still accepts a summary computed elsewhere (`workshop apply 6` runs the dataset from the
learner's machine and creates its releases with `autoRun: false`).

The verdict of an evaluation is always `platform_sdk.evaluation.apply_thresholds` with the thresholds of the release's blueprint
version; the backend never re-implements the comparison. Promotion is a worker job (`platform_api.worker_jobs.run_promote`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from boto3.dynamodb.conditions import Attr

from platform_sdk import evaluation

from .. import db, errors, jobs, validation
from ..services import evaluation_run
from .blueprints import load_blueprint, parse_version
from .projects import load_project

logger = logging.getLogger(__name__)
EVALUATION_STATUSES = ("DRAFT", "EVALUATING", "BLOCKED", "PENDING_APPROVAL")
RUN_STATUSES = ("DRAFT", "BLOCKED")  # a run starts from these; an EVALUATING release only when its previous run stalled
PROMOTE_STATUSES = ("APPROVED", "FAILED")  # FAILED: retry after a failed promotion (documented leniency)
STUCK_JOB_SECONDS = 15 * 60  # the worker Lambda's timeout: a job not updated for this long died hard (OOM, timeout)
STUCK_PROMOTION_SECONDS = STUCK_JOB_SECONDS
RUN_STAGES = ("DEPLOYING", "EVALUATING", "DONE", "FAILED")
MAX_RESULTS = 200
INTEGER_SCORE_FIELDS = ("count", "errors", "failed")


def load_release(release_id: str) -> dict:
    """The release item (with table keys) by id alone: releases live under their project, GSI1 lists them all."""
    for item in db.query_gsi1("RELEASE", descending=True):
        if item.get("releaseId") == release_id:
            return item
    raise errors.not_found(f"release {release_id!r} does not exist")


def save(release: dict, status: str | None = None, expected_status: str | None = None, **fields) -> dict:
    """Write the release; with `expected_status` the put is conditional on the status that was checked, so two overlapping
    transitions (approve + reject, two promote clicks) cannot both succeed (the loser gets 409 CONFLICT)."""
    if status:
        release["status"] = status
    release.update(fields)
    release["updatedAt"] = db.now_iso()
    db.put(release, condition=Attr("status").eq(expected_status) if expected_status else None)
    return db.strip_keys(release)


def _parse_iso(value) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


def _job_stuck(job_id: str | None) -> bool:
    """The job is gone, finished without moving the release, or has not been updated for longer than the worker timeout."""
    job = jobs.get(job_id) if job_id else None
    if job is None or job.get("status") in ("FAILED", "SUCCEEDED"):
        return True
    updated = _parse_iso(job.get("updatedAt") or job.get("createdAt"))
    return updated is None or (datetime.now(UTC) - updated).total_seconds() > STUCK_JOB_SECONDS


def promotion_stuck(release: dict) -> bool:
    """A PROMOTING release can be promoted again when its job is gone, finished without moving the release, or has not been
    updated for longer than the worker timeout (the worker died hard: OOM, timeout)."""
    if release.get("status") != "PROMOTING":
        return False
    return _job_stuck((release.get("promotion") or {}).get("jobId"))


def run_stuck(release: dict) -> bool:
    """An EVALUATING release can be run again under the same rule, applied to the job its pipeline follows."""
    if release.get("status") != "EVALUATING":
        return False
    return _job_stuck((release.get("pipeline") or {}).get("jobId"))


def check_scores(scores: dict) -> dict:
    """`scores` as sent by RunSummary.to_api_body(): evaluator -> {mean, min, passRate, count, errors, failed}; the numbers must
    be numbers (a stringified summary would otherwise crash apply_thresholds with a TypeError -> 500)."""
    if not scores or not all(isinstance(v, dict) for v in scores.values()):
        raise errors.validation("scores must map evaluator names to {mean, min, passRate, count, errors, failed}")
    for name, entry in scores.items():
        for field in ("mean", "min", "max", "passRate"):
            value = entry.get(field)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise errors.validation(f"scores.{name}.{field} must be a number (got {value!r})")
        for field in INTEGER_SCORE_FIELDS:
            value = entry.get(field)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float)) or not float(value).is_integer()
            ):
                raise errors.validation(f"scores.{name}.{field} must be an integer (got {value!r})")
    return scores


def _boolean(body: dict, key: str, default: bool) -> bool:
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise errors.validation(f"{key} must be true or false")
    return value


def list_releases(request) -> list:
    project_id = request.params["projectId"]
    load_project(project_id)
    return [db.strip_keys(item) for item in db.query(db.project_pk(project_id), "RELEASE#")]


def list_all(request) -> list:
    """GET /releases: every release of every project, newest first (the Releases screen)."""
    return [db.strip_keys(item) for item in db.query_gsi1("RELEASE", descending=True)]


def start_run(release: dict, by: str) -> dict:
    """Queue the `release` job and move the release to EVALUATING with its `pipeline`; returns the job.

    The deploy stage is decided here from the registered Dev harness (the worker checks again): when the harness already runs the
    blueprint version the pipeline starts at EVALUATING with `deploySkipped`. When the worker cannot be started the release goes
    back to its previous status with `error` WORKER_UNAVAILABLE and the error is raised (502)."""
    previous = release.get("status")
    project_id, version = release["projectId"], int(release["blueprintVersion"])
    item = load_blueprint(project_id, version)
    harness = evaluation_run.dev_harness(project_id)
    deploy_skipped = evaluation_run.harness_runs_version(harness, version)
    attributes = (harness or {}).get("attributes") or {}
    job = jobs.create(
        "release",
        {
            "releaseId": release["releaseId"],
            "projectId": project_id,
            "blueprintVersion": version,
            "env": "dev",
            "model": item.get("model"),
        },
        by,
    )
    now = db.now_iso()
    pipeline = {
        "stage": "EVALUATING" if deploy_skipped else "DEPLOYING",
        "jobId": job["jobId"],
        "deployJobId": None if deploy_skipped else job["jobId"],
        "evaluationJobId": None,
        "deploySkipped": deploy_skipped,
        "startedAt": now,
        "by": by,
        "finishedAt": None,
        "harnessVersion": attributes.get("version") if deploy_skipped else None,
        "harnessArn": harness.get("arn") if deploy_skipped and harness else None,
        "logGroup": attributes.get("logGroup") if deploy_skipped else None,
        "runId": None,
        "seconds": None,
        "error": None,
    }
    try:
        save(release, "EVALUATING", expected_status=previous, pipeline=pipeline, error=None)
    except errors.ApiError as exc:  # another request moved the release first: this job never runs
        jobs.set_status(job["jobId"], "FAILED", error={"code": "CONFLICT", "message": exc.message})
        raise
    try:
        jobs.enqueue(job)
    except Exception as exc:
        api_error = errors.from_exception(exc)
        jobs.set_status(job["jobId"], "FAILED", error=api_error.as_job_error())
        error = {
            "code": "WORKER_UNAVAILABLE",
            "message": f"the worker could not be started: {api_error.message}",
            "jobId": job["jobId"],
            "stage": pipeline["stage"],
        }
        save(
            release,
            previous,
            pipeline={**pipeline, "stage": "FAILED", "finishedAt": db.now_iso(), "error": error},
            error=error,
        )
        raise errors.upstream(error["message"], code="WORKER_UNAVAILABLE") from exc
    return job


def create(request):
    """POST /projects/{id}/releases {blueprintVersion, notes?, autoRun?: true} -> 201 the release.

    With `autoRun` (the default) the release is returned EVALUATING with its `pipeline`; a worker that cannot be started leaves it
    DRAFT with `error` set (the release exists, so this is not a 5xx). `autoRun: false` is for clients that run the evaluation
    themselves and post it with POST /releases/{id}/evaluation (the workshop command)."""
    project_id = request.params["projectId"]
    load_project(project_id)
    body = request.require_body()
    version = parse_version(body.get("blueprintVersion"))
    load_blueprint(project_id, version)
    notes = validation.string(body, "notes", required=False, default="", max_length=4000) or ""
    auto_run = _boolean(body, "autoRun", default=True)
    release_id, now = db.new_id(), db.now_iso()
    item = {
        **db.release_key(project_id, release_id),
        **db.release_gsi(now, release_id),
        "releaseId": release_id,
        "projectId": project_id,
        "blueprintVersion": version,
        "status": "DRAFT",
        "notes": notes,
        "evaluation": None,
        "approval": None,
        "promotion": None,
        "pipeline": None,
        "error": None,
        "createdAt": now,
        "createdBy": request.principal.username,
        "updatedAt": now,
    }
    db.put(item)
    if auto_run:
        try:
            start_run(item, request.principal.username)
        except errors.ApiError as exc:
            if exc.code != "WORKER_UNAVAILABLE":
                raise
            logger.warning("release %s was created but its run could not start: %s", release_id, exc.message)
    return 201, db.strip_keys(item)


def run(request):
    """POST /releases/{id}/run -> 202 {jobId, stage}: start the deploy-and-evaluate run of a DRAFT or BLOCKED release (again)."""
    release = load_release(request.params["releaseId"])
    status = release.get("status")
    if status not in RUN_STATUSES and not run_stuck(release):
        raise errors.conflict(
            f"release {release['releaseId']} is {status}; only DRAFT or BLOCKED releases can be run"
            + (
                " (an EVALUATING release can be run again once its job has failed, disappeared or stalled for 15 minutes)"
                if status == "EVALUATING"
                else ""
            )
        )
    load_project(release["projectId"])
    job = start_run(release, request.principal.username)
    return 202, {
        "jobId": job["jobId"],
        "status": "EVALUATING",
        "type": "release",
        "releaseId": release["releaseId"],
        "projectId": release["projectId"],
        "blueprintVersion": int(release["blueprintVersion"]),
        "stage": release["pipeline"]["stage"],
    }


def get(request) -> dict:
    return db.strip_keys(load_release(request.params["releaseId"]))


def _compact_results(results) -> list:
    if not isinstance(results, list):
        raise errors.validation("results must be a list of {scenarioId, status, scores{}, error?}")
    rows = []
    for row in results[:MAX_RESULTS]:
        if not isinstance(row, dict):
            raise errors.validation("results entries must be objects")
        compact = {
            "scenarioId": row.get("scenarioId"),
            "status": row.get("status", "COMPLETED"),
            "scores": row.get("scores") or {},
        }
        for key in ("sessionId", "labels", "error", "errors"):
            if row.get(key):
                compact[key] = row[key] if not isinstance(row[key], str) else row[key][:2000]
        rows.append(compact)
    return rows


def record_evaluation(release: dict, body: dict, by: str, source: str = "api", **fields) -> dict:
    """Apply the blueprint thresholds to a run summary (`RunSummary.to_api_body()` shape: runId?, datasetName?, scenarioCount?,
    scores, results?, failedScenarios?) and move the release to BLOCKED or PENDING_APPROVAL; `fields` are saved with it (the
    evaluate job's pipeline). The put is conditional on the status the caller read."""
    scores = check_scores(validation.mapping(body, "scores", required=True))
    results = _compact_results(body.get("results") or [])
    failed_scenarios = validation.string_list(body, "failedScenarios", required=False)
    project = load_project(release["projectId"])
    blueprint_item = load_blueprint(project["projectId"], release["blueprintVersion"])
    thresholds = ((blueprint_item.get("spec") or {}).get("evaluation") or {}).get("thresholds") or {}
    if not thresholds:
        raise errors.conflict(
            f"blueprint version {release['blueprintVersion']} has no spec.evaluation.thresholds; add them and create a new version",
            code="THRESHOLDS_NOT_CONFIGURED",
        )
    verdict = evaluation.apply_thresholds(scores, thresholds, failed_scenarios=failed_scenarios)
    record = {
        "runId": validation.string(body, "runId", required=False, max_length=200),
        "datasetName": validation.string(body, "datasetName", required=False, max_length=200),
        "scenarioCount": validation.integer(body, "scenarioCount", required=False, minimum=0),
        "scores": scores,
        "thresholds": thresholds,
        "verdict": "PASSED" if verdict.passed else "BLOCKED",
        "passed": verdict.passed,
        "failures": verdict.failures,
        "details": verdict.details,
        "results": results,
        "failedScenarios": failed_scenarios if failed_scenarios is not None else [],
        "source": source,
        "submittedBy": by,
        "completedAt": db.now_iso(),
    }
    fields.setdefault("error", None)  # a verdict supersedes the error of a failed run
    return save(release, verdict.status, expected_status=release["status"], evaluation=record, **fields)


def fail_run(release: dict, error: dict, job_id: str, stage: str) -> dict:
    """The run failed in `stage`: the release goes back to DRAFT with the error on the release and on its pipeline. Nothing is
    written when the release has moved on (another run superseded this job): the conditional put refuses and the error is logged."""
    record = {**error, "jobId": job_id, "stage": stage}
    pipeline = {**(release.get("pipeline") or {}), "stage": "FAILED", "finishedAt": db.now_iso(), "error": record}
    try:
        return save(release, "DRAFT", expected_status="EVALUATING", pipeline=pipeline, error=record)
    except errors.ApiError as exc:
        logger.warning(
            "release %s moved during job %s; its failure is not recorded: %s", release.get("releaseId"), job_id, exc
        )
        return db.strip_keys(release)


def submit_evaluation(request) -> dict:
    """POST /releases/{id}/evaluation with RunSummary.to_api_body(): thresholds from the blueprint -> BLOCKED | PENDING_APPROVAL.

    Refused (409) while the platform's own run is live: the worker owns an EVALUATING release until its job has finished, failed,
    disappeared or stalled for 15 minutes. An accepted summary supersedes whatever run came before it (a failed one, a stalled
    one): the release's `pipeline` and `error` are cleared with it, so the Releases screen shows the submitted verdict alone."""
    release = load_release(request.params["releaseId"])
    status = release.get("status")
    if status == "EVALUATING" and not run_stuck(release):
        raise errors.conflict(
            f"release {release['releaseId']} is EVALUATING: the platform is running it (job "
            f"{(release.get('pipeline') or {}).get('jobId')}); wait for its verdict, or post one once the job has failed, "
            "disappeared or stalled for 15 minutes"
        )
    if status not in EVALUATION_STATUSES:
        raise errors.conflict(
            f"release {release['releaseId']} is {status}; evaluations are accepted while a release is "
            f"{' / '.join(EVALUATION_STATUSES)} - create a new release for another attempt"
        )
    body = request.require_body()
    return record_evaluation(release, body, request.principal.username, source="api", pipeline=None, error=None)


def _decide(request, decision: str) -> dict:
    release = load_release(request.params["releaseId"])
    if release.get("status") != "PENDING_APPROVAL":
        raise errors.conflict(
            f"release {release['releaseId']} is {release.get('status')}; only PENDING_APPROVAL releases can be {decision.lower()}"
        )
    body = request.body if isinstance(request.body, dict) else {}
    comment = validation.string(body, "comment", required=False, default="", max_length=4000) or ""
    approval = {"by": request.principal.username, "at": db.now_iso(), "comment": comment, "decision": decision}
    return save(release, decision, expected_status="PENDING_APPROVAL", approval=approval)


def approve(request) -> dict:
    return _decide(request, "APPROVED")


def reject(request) -> dict:
    return _decide(request, "REJECTED")


def promote(request):
    """POST /releases/{id}/promote -> 202 {jobId}; the worker deploys Prod, repoints `live` and ensures the online evaluation."""
    release = load_release(request.params["releaseId"])
    previous_status = release.get("status")
    if previous_status not in PROMOTE_STATUSES and not promotion_stuck(release):
        raise errors.conflict(
            f"release {release['releaseId']} is {previous_status}; only APPROVED releases can be promoted"
            + (" (a FAILED promotion can be retried)" if previous_status != "FAILED" else "")
            + (
                " (a PROMOTING release can be promoted again once its job has failed, disappeared or stalled for 15 minutes)"
                if previous_status == "PROMOTING"
                else ""
            )
        )
    project = load_project(release["projectId"])
    load_blueprint(project["projectId"], release["blueprintVersion"])
    job = jobs.create(
        "promote",
        {
            "releaseId": release["releaseId"],
            "projectId": project["projectId"],
            "blueprintVersion": int(release["blueprintVersion"]),
            "env": "prod",
        },
        request.principal.username,
    )
    try:
        save(
            release,
            "PROMOTING",
            expected_status=previous_status,
            promotion={"jobId": job["jobId"], "startedAt": db.now_iso(), "by": request.principal.username},
            error=None,
        )
    except errors.ApiError as exc:  # another request moved the release first (two promote clicks): this job never runs
        jobs.set_status(job["jobId"], "FAILED", error={"code": "CONFLICT", "message": exc.message})
        raise
    try:
        jobs.enqueue(job)
    except Exception as exc:
        api_error = errors.from_exception(exc)
        jobs.set_status(job["jobId"], "FAILED", error=api_error.as_job_error())
        save(release, "FAILED", error={**api_error.as_job_error(), "jobId": job["jobId"]})
        raise errors.upstream(
            f"the worker could not be started: {api_error.message}", code="WORKER_UNAVAILABLE"
        ) from exc
    return 202, {
        "jobId": job["jobId"],
        "status": "PROMOTING",
        "type": "promote",
        "releaseId": release["releaseId"],
        "projectId": project["projectId"],
        "blueprintVersion": int(release["blueprintVersion"]),
    }
