"""Releases (contract sections 3 and 4): DRAFT -> EVALUATING (Dev deployment if needed, then the dataset run) -> BLOCKED |
PENDING_APPROVAL -> APPROVED | REJECTED -> PROMOTING -> PROMOTED | FAILED.

A release run is one execution of the CodePipeline release pipeline (`platform_api.services.release_pipeline`): Source (the
release snapshot, pinned by S3 object version) -> Validate (DeployDev then Evaluate) -> Approve (manual) -> Promote (manual, then the Prod
deployment). Creating a release starts its run (`autoRun`, the default; `autoRun: false` leaves a DRAFT that
`POST /releases/{id}/run` starts). The release is EVALUATING meanwhile and carries the run in `pipeline` (the job the UI follows
in `jobId`, plus `pipelineName`, `executionId`, `sourceVersionId` and the authoritative `executionStatus`); a failed run puts it
back to DRAFT with `error` set. Evaluations are only recorded by the pipeline's Evaluate action: `POST /releases/{id}/evaluation`
is refused (NATIVE_GATE_REQUIRED), so no externally computed summary can pass the gate.

Approve, reject and promote are the approver's clicks: the application writes its record first (conditional on the status and
on the execution), then answers the execution's manual approval with `PutApprovalResult`. The Prod deployment action refuses to
run without that record, so an approval answered elsewhere (the console) cannot promote. Whether a run or a promotion may be
started again follows the execution's CodePipeline status (Failed, Stopped, Superseded, Cancelled), never the age of a job.

The verdict of an evaluation is always `platform_sdk.evaluation.apply_thresholds` with the thresholds of the release's blueprint
version; the backend never re-implements the comparison.
"""

from __future__ import annotations

import logging
import uuid

from boto3.dynamodb.conditions import Attr

from platform_sdk import evaluation, names

from .. import db, errors, jobs, validation
from ..services import clients, evaluation_run
from ..services import release_pipeline as pipeline_service
from .blueprints import load_blueprint, parse_version
from .projects import load_project

logger = logging.getLogger(__name__)
EVALUATION_STATUSES = ("DRAFT", "EVALUATING", "BLOCKED", "PENDING_APPROVAL")
RUN_STATUSES = ("DRAFT", "BLOCKED")  # a run starts from these; an EVALUATING release only when its execution has ended
PROMOTE_STATUSES = ("APPROVED", "FAILED")  # FAILED: retry the Prod deployment of the same execution
RUN_STAGES = ("DEPLOYING", "EVALUATING", "DONE", "FAILED")
MAX_RESULTS = 200
INTEGER_SCORE_FIELDS = ("count", "errors", "failed")


def load_release(release_id: str) -> dict:
    """The release item (with table keys) by id alone: releases live under their project, GSI1 lists them all."""
    for item in db.query_gsi1("RELEASE", descending=True):
        if item.get("releaseId") == release_id:
            # the index is eventually consistent: the item itself is read strongly consistently by its key
            fresh = db.get({"PK": item["PK"], "SK": item["SK"]}, consistent=True)
            if fresh is not None:
                return fresh
            break
    raise errors.not_found(f"release {release_id!r} does not exist")


def save(
    release: dict, status: str | None = None, expected_status: str | None = None, condition=None, **fields
) -> dict:
    """Write the release; with `expected_status` the put is conditional on the status that was checked, so two overlapping
    transitions (approve + reject, two promote clicks) cannot both succeed (the loser gets 409 CONFLICT). `condition` adds a
    further check (the execution or the start token the caller read)."""
    check = Attr("status").eq(expected_status) if expected_status else None
    if condition is not None:
        check = condition if check is None else check & condition
    if status:
        release["status"] = status
    release.update(fields)
    release["updatedAt"] = db.now_iso()
    db.put(release, condition=check)
    return db.strip_keys(release)


def owned_by(execution_id: str):
    """The condition "the release still follows this execution"."""
    return Attr("pipeline.executionId").eq(execution_id)


def _token_condition(token: str | None):
    return Attr("pipeline.startToken").eq(token) if token else Attr("pipeline.startToken").not_exists()


def _execution(release: dict) -> tuple[str | None, str]:
    """(executionId, its CodePipeline status) of the release's run; the status is UNKNOWN when it cannot be read."""
    execution_id = (release.get("pipeline") or {}).get("executionId")
    if not execution_id:
        return None, pipeline_service.UNKNOWN
    try:
        return execution_id, pipeline_service.execution_status(pipeline_service.settings(), execution_id)
    except errors.ApiError:
        raise
    except Exception as exc:  # noqa: BLE001 - an unreadable status is UNKNOWN: never a reason to start again
        logger.warning("status of execution %s unavailable: %s", execution_id, errors.message_of(exc))
        return execution_id, pipeline_service.UNKNOWN


def _start_interrupted(release: dict) -> bool:
    """EVALUATING with a start token but no execution recorded: the request that started the run died in between."""
    pipeline = release.get("pipeline") or {}
    return (
        release.get("status") == "EVALUATING" and bool(pipeline.get("startToken")) and not pipeline.get("executionId")
    )


def active_run(project_id: str) -> dict | None:
    """The project's EVALUATING release, whose pipeline run owns the Dev harness until its verdict."""
    for item in db.query(db.project_pk(project_id), "RELEASE#"):
        if item.get("status") == "EVALUATING":
            return item
    return None


def refuse_dev_change(project_id: str, env: str) -> None:
    """409 EXECUTION_ACTIVE for a direct Dev deployment while a release run evaluates that harness."""
    if env != "dev":
        return
    running = active_run(project_id)
    if running is not None:
        pipeline = running.get("pipeline") or {}
        raise errors.conflict(
            f"release {running['releaseId']} is being evaluated against the Dev harness (execution "
            f"{pipeline.get('executionId') or 'starting'} of {pipeline.get('pipelineName')}); deploy once it has its verdict",
            code="EXECUTION_ACTIVE",
        )


def run_stuck(release: dict) -> bool:
    """An EVALUATING release can be run again when its execution ended without moving it (Failed, Stopped, Superseded,
    Cancelled) or its start was interrupted; an execution that is in progress or queued, or whose status is unknown, owns it."""
    if release.get("status") != "EVALUATING":
        return False
    if _start_interrupted(release):
        return True
    _, status = _execution(release)
    return status in pipeline_service.RETRYABLE_STATUSES


def promotion_stuck(release: dict) -> bool:
    """A PROMOTING release can be promoted again when its execution ended (the Prod deployment failed or was stopped)."""
    if release.get("status") != "PROMOTING":
        return False
    _, status = _execution(release)
    return status in pipeline_service.RETRYABLE_STATUSES


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


def _new_pipeline(release: dict, by: str, job_id: str, cfg: pipeline_service.Settings) -> dict:
    project_id, version = release["projectId"], int(release["blueprintVersion"])
    harness = evaluation_run.dev_harness(project_id)
    deploy_skipped = evaluation_run.harness_runs_version(harness, version)
    attributes = (harness or {}).get("attributes") or {}
    return {
        "stage": "EVALUATING" if deploy_skipped else "DEPLOYING",
        "jobId": job_id,
        "releaseJobId": job_id,
        "deployJobId": None if deploy_skipped else job_id,
        "evaluationJobId": None,
        "deploySkipped": deploy_skipped,
        "startedAt": db.now_iso(),
        "by": by,
        "finishedAt": None,
        "harnessVersion": attributes.get("version") if deploy_skipped else None,
        "harnessArn": harness.get("arn") if deploy_skipped and harness else None,
        "logGroup": attributes.get("logGroup") if deploy_skipped else None,
        "runId": None,
        "seconds": None,
        "error": None,
        "pipelineName": cfg.name,
        "executionId": None,
        "executionStatus": pipeline_service.STARTING,
        "startToken": uuid.uuid4().hex,
        "sourceBucket": cfg.bucket,
        "sourceKey": cfg.key,
        "sourceVersionId": None,
        "consoleUrl": None,
    }


def start_run(release: dict, by: str) -> dict:
    """Start the release's pipeline execution and move the release to EVALUATING with its `pipeline`; returns the `release` job
    the UI follows (the DeployDev action runs it).

    Order: the conditional put to EVALUATING (one of two racing requests wins), the snapshot upload (its version id is saved before
    the start), StartPipelineExecution with the start token (idempotent), the execution id. A release whose start was interrupted
    resumes with the same token and snapshot version, so CodePipeline returns the execution it may already have started. When the
    pipeline cannot be started the release goes back to its previous status with `error` PIPELINE_UNAVAILABLE (502)."""
    cfg = pipeline_service.settings()
    if _start_interrupted(release) and (release.get("pipeline") or {}).get("sourceVersionId"):
        return _resume_start(release, cfg)
    previous = release.get("status")
    old_token = (release.get("pipeline") or {}).get("startToken")
    project_id, version = release["projectId"], int(release["blueprintVersion"])
    item = load_blueprint(project_id, version)
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
        ttl_seconds=jobs.PIPELINE_JOB_TTL_SECONDS,
    )
    pipeline = _new_pipeline(release, by, job["jobId"], cfg)
    token = pipeline["startToken"]
    try:
        save(
            release,
            "EVALUATING",
            expected_status=previous,
            condition=_token_condition(old_token),
            pipeline=pipeline,
            error=None,
        )
    except errors.ApiError as exc:  # another request moved the release first: this job never runs
        jobs.set_status(job["jobId"], "FAILED", error={"code": "CONFLICT", "message": exc.message})
        raise
    try:
        pipeline["sourceVersionId"] = pipeline_service.upload_snapshot(cfg, release, item, token)
        save(release, expected_status="EVALUATING", condition=_token_condition(token), pipeline=pipeline)
        execution_id = pipeline_service.start(cfg, release, pipeline["sourceVersionId"], token)
    except Exception as exc:
        _start_failed(release, previous, pipeline, job["jobId"], exc)
        raise errors.upstream(
            f"the release pipeline could not be started: {errors.message_of(exc)}", code="PIPELINE_UNAVAILABLE"
        ) from exc
    _record_execution(release, pipeline, execution_id, cfg)
    return job


def _resume_start(release: dict, cfg: pipeline_service.Settings) -> dict:
    pipeline = dict(release["pipeline"])
    try:
        execution_id = pipeline_service.start(cfg, release, pipeline["sourceVersionId"], pipeline["startToken"])
    except Exception as exc:
        raise errors.upstream(
            f"the release pipeline could not be started: {errors.message_of(exc)}", code="PIPELINE_UNAVAILABLE"
        ) from exc
    _record_execution(release, pipeline, execution_id, cfg)
    return jobs.load(pipeline["jobId"])


def _record_execution(release: dict, pipeline: dict, execution_id: str, cfg: pipeline_service.Settings) -> None:
    pipeline.update(
        executionId=execution_id,
        executionStatus="InProgress",
        consoleUrl=pipeline_service.console_url(clients.cfg().region, cfg, execution_id),
    )
    try:
        save(
            release, expected_status="EVALUATING", condition=_token_condition(pipeline["startToken"]), pipeline=pipeline
        )
    except errors.ApiError:  # superseded while starting: nobody follows this execution
        pipeline_service.stop(cfg, execution_id, "superseded while starting")
        raise


def _start_failed(release: dict, previous: str, pipeline: dict, job_id: str, exc: BaseException) -> None:
    api_error = errors.from_exception(exc)
    jobs.set_status(job_id, "FAILED", error=api_error.as_job_error())
    error = {
        "code": "PIPELINE_UNAVAILABLE",
        "message": f"the release pipeline could not be started: {api_error.message}",
        "jobId": job_id,
        "stage": pipeline["stage"],
    }
    failed = {**pipeline, "stage": "FAILED", "finishedAt": db.now_iso(), "error": error}
    try:
        save(
            release,
            previous,
            expected_status="EVALUATING",
            condition=_token_condition(pipeline["startToken"]),
            pipeline=failed,
            error=error,
        )
    except errors.ApiError:
        logger.warning("release %s moved while its start failed: %s", release["releaseId"], api_error.message)


def create(request):
    """POST /projects/{id}/releases {blueprintVersion, notes?, autoRun?: true} -> 201 the release.

    With `autoRun` (the default) the release is returned EVALUATING with its `pipeline`; a pipeline that cannot be started leaves
    it DRAFT with `error` set (the release exists, so this is not a 5xx). `autoRun: false` creates the DRAFT only: the `workshop`
    command starts it with POST /releases/{id}/run once it has checked the gateway mode. Without a configured pipeline an
    `autoRun` release is refused before it is stored (503 PIPELINE_NOT_CONFIGURED)."""
    project_id = request.params["projectId"]
    load_project(project_id)
    body = request.require_body()
    version = parse_version(body.get("blueprintVersion"))
    load_blueprint(project_id, version)
    notes = validation.string(body, "notes", required=False, default="", max_length=4000) or ""
    auto_run = _boolean(body, "autoRun", default=True)
    if auto_run:
        pipeline_service.settings()  # no release is stored for a run that cannot start
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
            if exc.code != "PIPELINE_UNAVAILABLE":
                raise
            logger.warning("release %s was created but its run could not start: %s", release_id, exc.message)
    return 201, db.strip_keys(item)


def run(request):
    """POST /releases/{id}/run -> 202 {jobId, stage, executionId, pipelineName}: start the pipeline run of a DRAFT or BLOCKED
    release (again), or of an EVALUATING one whose execution has ended or whose start was interrupted."""
    release = load_release(request.params["releaseId"])
    status = release.get("status")
    if status not in RUN_STATUSES and not run_stuck(release):
        pipeline = release.get("pipeline") or {}
        raise errors.conflict(
            f"release {release['releaseId']} is {status}; only DRAFT or BLOCKED releases can be run"
            + (
                f" (execution {pipeline.get('executionId')} of {pipeline.get('pipelineName')} still owns it: an EVALUATING "
                "release can be run again once its execution has failed, stopped or been superseded)"
                if status == "EVALUATING"
                else ""
            ),
            code="EXECUTION_ACTIVE" if status == "EVALUATING" else "CONFLICT",
        )
    load_project(release["projectId"])
    job = start_run(release, request.principal.username)
    pipeline = release["pipeline"]
    return 202, {
        "jobId": job["jobId"],
        "status": "EVALUATING",
        "type": "release",
        "releaseId": release["releaseId"],
        "projectId": release["projectId"],
        "blueprintVersion": int(release["blueprintVersion"]),
        "stage": pipeline["stage"],
        "pipelineName": pipeline.get("pipelineName"),
        "executionId": pipeline.get("executionId"),
        "executionStatus": pipeline.get("executionStatus"),
    }


def get(request) -> dict:
    """GET /releases/{id}: the release with the execution's current CodePipeline status (`pipeline.executionStatus`,
    `retryable`, `retryAction`); an execution that ended without moving the release is reconciled here."""
    return refresh(load_release(request.params["releaseId"]))


def refresh(release: dict) -> dict:
    """The release with its execution's live status; an ended execution moves a release that still waits on it:
    EVALUATING -> DRAFT, PENDING_APPROVAL / APPROVED -> DRAFT (APPROVAL_EXPIRED or the end status), PROMOTING -> FAILED."""
    execution_id = (release.get("pipeline") or {}).get("executionId")
    if execution_id:
        _, status = _execution(release)
        release["pipeline"] = {**release["pipeline"], "executionStatus": status, "statusCheckedAt": db.now_iso()}
        if status in pipeline_service.RETRYABLE_STATUSES:
            _reconcile(release, status)
        release["pipeline"].update(_approval_readiness(release, status))
    view = db.strip_keys(release)
    view["pipeline"] = _with_retry(view) if view.get("pipeline") else view.get("pipeline")
    return view


APPROVAL_GATES = {
    "PENDING_APPROVAL": ("release", names.RELEASE_APPROVE_STAGE, names.RELEASE_APPROVE_ACTION),
    "APPROVED": ("promotion", names.RELEASE_PROMOTE_STAGE, names.RELEASE_PROMOTE_GATE_ACTION),
}
APPROVAL_RETRY_SECONDS = 5  # CodePipeline moves an execution to the next stage within seconds (longer when queued)


def _approval_readiness(release: dict, execution_status: str) -> dict:
    """`approvalGate` (release | promotion | None) and `approvalReady`: whether the execution waits at that manual approval
    right now, so a client enables Approve / Promote only then (the verdict is stored before CodePipeline reaches the stage).
    The token itself is never returned."""
    gate = APPROVAL_GATES.get(release.get("status"))
    if gate is None or execution_status != "InProgress":
        return {"approvalGate": gate[0] if gate else None, "approvalReady": False}
    try:
        ready = bool(
            pipeline_service.approval_token(
                pipeline_service.settings(), release["pipeline"]["executionId"], gate[1], gate[2]
            )
        )
    except Exception as exc:  # noqa: BLE001 - readiness is advisory; the action itself re-checks
        logger.warning(
            "approval readiness of release %s unavailable: %s", release.get("releaseId"), errors.message_of(exc)
        )
        ready = False
    return {"approvalGate": gate[0], "approvalReady": ready}


def not_waiting(message: str, execution_status: str) -> errors.ApiError:
    """409 APPROVAL_NOT_WAITING with the hint to retry shortly (the execution has not reached the manual approval yet)."""
    return errors.ApiError(
        409,
        "APPROVAL_NOT_WAITING",
        message,
        details={"retryAfterSeconds": APPROVAL_RETRY_SECONDS, "executionStatus": execution_status},
    )


def _ended_error(status: str, execution_id: str, waiting: str | None = None) -> dict:
    if waiting and status == "Failed":
        return {
            "code": "APPROVAL_EXPIRED",
            "message": f"execution {execution_id} ended Failed while the release waited for its {waiting} (the approval "
            "expired, or was answered outside the platform); run the release again",
        }
    return {
        "code": f"PIPELINE_{status.upper()}",
        "message": f"execution {execution_id} ended {status} before the release moved on; run the release again",
    }


def _finish_waiting_jobs(release: dict, execution_id: str, status: str) -> None:
    """The release's jobs that never ran because the execution ended are finished as FAILED (which starts their 24-hour TTL)."""
    pipeline, promotion = release.get("pipeline") or {}, release.get("promotion") or {}
    for job_id in {pipeline.get("releaseJobId"), pipeline.get("evaluationJobId"), promotion.get("jobId")} - {None}:
        job = jobs.get(job_id)
        if job and job.get("status") == "QUEUED":
            jobs.set_status(
                job_id,
                "FAILED",
                error={
                    "code": "PIPELINE_ENDED",
                    "message": f"execution {execution_id} ended {status} before this job ran",
                },
            )


def _reconcile(release: dict, status: str) -> None:
    pipeline = release["pipeline"]
    execution_id = pipeline["executionId"]
    current = release.get("status")
    _finish_waiting_jobs(release, execution_id, status)
    try:
        if current == "EVALUATING":
            fail_run(
                release,
                _ended_error(status, execution_id),
                pipeline.get("jobId"),
                pipeline.get("stage") or "EVALUATING",
            )
        elif current in ("PENDING_APPROVAL", "APPROVED"):
            waiting = "release approval" if current == "PENDING_APPROVAL" else "promotion approval"
            error = _ended_error(status, execution_id, waiting)
            save(release, "DRAFT", expected_status=current, condition=owned_by(execution_id), error=error)
        elif current == "PROMOTING":
            error = {**_ended_error(status, execution_id), "jobId": (release.get("promotion") or {}).get("jobId")}
            save(release, "FAILED", expected_status="PROMOTING", condition=owned_by(execution_id), error=error)
    except errors.ApiError as exc:  # another request moved it first; the next read shows that
        logger.info("release %s not reconciled: %s", release.get("releaseId"), exc.message)


def _with_retry(view: dict) -> dict:
    """`retryable` / `retryAction` for the UI from the release status and the execution's CodePipeline status alone."""
    pipeline = dict(view["pipeline"])
    status, execution_status = view.get("status"), pipeline.get("executionStatus")
    ended = not pipeline.get("executionId") or execution_status in pipeline_service.RETRYABLE_STATUSES
    if status in RUN_STATUSES and (ended or execution_status == "Succeeded"):
        action = "run"
    elif status == "EVALUATING" and (
        execution_status in pipeline_service.RETRYABLE_STATUSES or _start_interrupted(view)
    ):
        action = "run"
    elif status in ("FAILED", "PROMOTING") and execution_status in ("Failed", "Stopped"):
        action = "promote"
    else:
        action = None
    pipeline.update(retryable=action is not None, retryAction=action)
    return pipeline


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
    execution_id = (release.get("pipeline") or {}).get("executionId")
    condition = owned_by(execution_id) if execution_id else None
    return save(
        release, verdict.status, expected_status=release["status"], condition=condition, evaluation=record, **fields
    )


def fail_run(release: dict, error: dict, job_id: str, stage: str) -> dict:
    """The run failed in `stage`: the release goes back to DRAFT with the error on the release and on its pipeline. Nothing is
    written when the release has moved on (another run superseded this job): the conditional put refuses and the error is logged."""
    record = {**error, "jobId": job_id, "stage": stage}
    pipeline = {**(release.get("pipeline") or {}), "stage": "FAILED", "finishedAt": db.now_iso(), "error": record}
    try:
        execution_id = (release.get("pipeline") or {}).get("executionId")
        condition = owned_by(execution_id) if execution_id else None
        return save(
            release, "DRAFT", expected_status="EVALUATING", condition=condition, pipeline=pipeline, error=record
        )
    except errors.ApiError as exc:
        logger.warning(
            "release %s moved during job %s; its failure is not recorded: %s", release.get("releaseId"), job_id, exc
        )
        return db.strip_keys(release)


def submit_evaluation(request) -> dict:
    """POST /releases/{id}/evaluation: refused. The verdict is recorded only by the release pipeline's Evaluate action, which runs
    the dataset against the Dev harness itself; a summary computed elsewhere cannot pass the gate."""
    release = load_release(request.params["releaseId"])
    raise errors.conflict(
        f"release {release['releaseId']}: evaluations are recorded by the release pipeline only; start the run with "
        f"POST /releases/{release['releaseId']}/run",
        code="NATIVE_GATE_REQUIRED",
    )


def _gate_token(release: dict, cfg: pipeline_service.Settings, stage: str, action: str, waiting: str) -> str:
    """The token of the manual approval waiting for the release's execution; 409 APPROVAL_NOT_WAITING while the execution has
    not reached it, APPROVAL_EXPIRED (and the release reconciled) when the execution has ended."""
    execution_id = (release.get("pipeline") or {}).get("executionId")
    token = pipeline_service.approval_token(cfg, execution_id, stage, action)
    if token:
        return token
    _, status = _execution(release)
    if status in pipeline_service.RETRYABLE_STATUSES:
        _reconcile(release, status)
        raise errors.conflict(_ended_error(status, execution_id, waiting)["message"], code="APPROVAL_EXPIRED")
    raise not_waiting(
        f"execution {execution_id} is not waiting for the {waiting} ({status}); try again in a moment", status
    )


def _require_execution(release: dict) -> str:
    execution_id = (release.get("pipeline") or {}).get("executionId")
    if not execution_id:
        raise errors.conflict(
            f"release {release['releaseId']} has no release pipeline execution; run it with POST /releases/{release['releaseId']}/run",
            code="NATIVE_GATE_REQUIRED",
        )
    return execution_id


def _pipeline_refusal(exc: BaseException, what: str) -> errors.ApiError:
    code = pipeline_service.error_code(exc)
    if code in pipeline_service.APPROVAL_GONE_CODES:
        return errors.conflict(
            f"{what}: the pipeline no longer waits for it ({code}); reload the release", code="APPROVAL_NOT_WAITING"
        )
    if code in pipeline_service.RETRY_REFUSED_CODES:
        return errors.conflict(
            f"{what}: CodePipeline refused ({code}); run the release again", code="PROMOTION_NOT_RETRYABLE"
        )
    if isinstance(exc, errors.ApiError):
        return exc
    return errors.upstream(f"{what}: {errors.message_of(exc)}", code="PIPELINE_UNAVAILABLE")


def _decide(request, decision: str) -> dict:
    release = load_release(request.params["releaseId"])
    if release.get("status") != "PENDING_APPROVAL":
        raise errors.conflict(
            f"release {release['releaseId']} is {release.get('status')}; only PENDING_APPROVAL releases can be {decision.lower()}"
        )
    execution_id = _require_execution(release)
    cfg = pipeline_service.settings()
    token = _gate_token(release, cfg, names.RELEASE_APPROVE_STAGE, names.RELEASE_APPROVE_ACTION, "release approval")
    body = request.body if isinstance(request.body, dict) else {}
    comment = validation.string(body, "comment", required=False, default="", max_length=4000) or ""
    by = request.principal.username
    approval = {"by": by, "at": db.now_iso(), "comment": comment, "decision": decision, "executionId": execution_id}
    # the application's record first: the pipeline never moves on without it
    saved = save(
        release, decision, expected_status="PENDING_APPROVAL", condition=owned_by(execution_id), approval=approval
    )
    summary = f"{decision.lower()} in the platform by {by}" + (f": {comment}" if comment else "")
    try:
        pipeline_service.put_approval(
            cfg, names.RELEASE_APPROVE_STAGE, names.RELEASE_APPROVE_ACTION, token, decision == "APPROVED", summary
        )
    except Exception as exc:
        try:
            save(release, "PENDING_APPROVAL", expected_status=decision, condition=owned_by(execution_id), approval=None)
        except errors.ApiError:
            logger.warning("release %s: the %s could not be reverted", release["releaseId"], decision)
        raise _pipeline_refusal(exc, f"the {decision.lower()} of release {release['releaseId']}") from exc
    return saved


def approve(request) -> dict:
    return _decide(request, "APPROVED")


def reject(request) -> dict:
    return _decide(request, "REJECTED")


def promote(request):
    """POST /releases/{id}/promote -> 202 {jobId}: answer the execution's promotion approval (or, after a failed Prod deployment,
    retry that action); the Prod deployment action runs the `promote` job (DEFAULT follows the new version, online evaluation)."""
    release = load_release(request.params["releaseId"])
    previous_status = release.get("status")
    if previous_status not in PROMOTE_STATUSES and not promotion_stuck(release):
        raise errors.conflict(
            f"release {release['releaseId']} is {previous_status}; only APPROVED releases can be promoted"
            + (" (a FAILED promotion can be retried)" if previous_status != "FAILED" else "")
            + (
                " (a PROMOTING release can be promoted again once its execution has failed or stopped)"
                if previous_status == "PROMOTING"
                else ""
            )
        )
    execution_id = _require_execution(release)
    approval = release.get("approval") or {}
    if approval.get("decision") != "APPROVED" or approval.get("executionId") != execution_id:
        raise errors.conflict(
            f"release {release['releaseId']} has no platform approval for execution {execution_id}",
            code="APPROVAL_REQUIRED",
        )
    project = load_project(release["projectId"])
    load_blueprint(project["projectId"], release["blueprintVersion"])
    cfg = pipeline_service.settings()
    token = pipeline_service.approval_token(
        cfg, execution_id, names.RELEASE_PROMOTE_STAGE, names.RELEASE_PROMOTE_GATE_ACTION
    )
    if token is None:
        _, status = _execution(release)
        if status not in ("Failed", "Stopped"):
            if status not in pipeline_service.TERMINAL_STATUSES:
                raise not_waiting(
                    f"execution {execution_id} is not waiting for the promotion ({status}); try again in a moment",
                    status,
                )
            raise errors.conflict(
                f"execution {execution_id} is {status}; run the release again", code="PROMOTION_NOT_RETRYABLE"
            )
    by = request.principal.username
    job = jobs.create(
        "promote",
        {
            "releaseId": release["releaseId"],
            "projectId": project["projectId"],
            "blueprintVersion": int(release["blueprintVersion"]),
            "env": "prod",
        },
        by,
        ttl_seconds=jobs.PIPELINE_JOB_TTL_SECONDS,
    )
    previous_promotion = release.get("promotion")
    promotion = {
        "jobId": job["jobId"],
        "startedAt": db.now_iso(),
        "by": by,
        "executionId": execution_id,
        "mode": "approve" if token else "retry",
        "attempt": int((previous_promotion or {}).get("attempt") or 0) + 1,
    }
    try:
        save(
            release,
            "PROMOTING",
            expected_status=previous_status,
            condition=owned_by(execution_id),
            promotion=promotion,
            error=None,
        )
    except errors.ApiError as exc:  # another request moved the release first (two promote clicks): this job never runs
        jobs.set_status(job["jobId"], "FAILED", error={"code": "CONFLICT", "message": exc.message})
        raise
    try:
        if token:
            pipeline_service.put_approval(
                cfg,
                names.RELEASE_PROMOTE_STAGE,
                names.RELEASE_PROMOTE_GATE_ACTION,
                token,
                True,
                f"promotion requested in the platform by {by}",
            )
        else:
            pipeline_service.retry_promotion(cfg, execution_id)
    except Exception as exc:
        refusal = _pipeline_refusal(exc, f"the promotion of release {release['releaseId']}")
        jobs.set_status(job["jobId"], "FAILED", error=refusal.as_job_error())
        try:
            save(
                release,
                previous_status if previous_status != "PROMOTING" else "FAILED",
                expected_status="PROMOTING",
                condition=owned_by(execution_id),
                promotion=previous_promotion,
                error={**refusal.as_job_error(), "jobId": job["jobId"]},
            )
        except errors.ApiError:
            logger.warning("release %s moved while its promotion failed to start", release["releaseId"])
        raise refusal from exc
    return 202, {
        "jobId": job["jobId"],
        "status": "PROMOTING",
        "type": "promote",
        "releaseId": release["releaseId"],
        "projectId": project["projectId"],
        "blueprintVersion": int(release["blueprintVersion"]),
        "pipelineName": cfg.name,
        "executionId": execution_id,
    }
