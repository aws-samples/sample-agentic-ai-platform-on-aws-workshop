"""The release pipeline's Lambda actions, run by the worker: DeployDev (`release` job), Evaluate (`evaluate` job) and PromoteProd
(`promote` job). The existing job handlers do the work (`platform_api.worker_jobs`); this module only decides whether the
CodePipeline job may run them and reports the outcome with PutJobSuccessResult / PutJobFailureResult.

Every action names its release, project and execution in UserParameters (`#{variables.releaseId}`, `#{variables.projectId}`,
`#{codepipeline.PipelineExecutionId}`). An action runs only for the execution the release follows (`pipeline.executionId`) and
for the snapshot version that execution was started with; an execution the platform did not start, or one a newer run
superseded, fails its action without touching the release. While the API is still recording a just-started execution the action
waits with a continuation token. A CodePipeline job is claimed with a conditional write, so a duplicate delivery never runs a
deployment twice. The Prod deployment needs the platform's approval and the approver's promote click for this execution: an
approval answered in the console alone fails the action.

Nothing here logs the CodePipeline event: it carries temporary artifact credentials. Logs name the job, action and release only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass

from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from . import db, errors, jobs
from .services import clients

logger = logging.getLogger(__name__)
CLAIM_TTL_SECONDS = 7 * 24 * 3600
WAIT_ROUNDS = (
    20  # continuation rounds while the API records the execution id (CodePipeline re-invokes after a short pause)
)
FAILURE_MESSAGE_MAX = 5000  # FailureDetails.message
SUMMARY_MAX = 2048  # ExecutionDetails.summary
PARAMS = ("action", "releaseId", "projectId", "executionId")
RESULT_ALREADY_FINAL = ("InvalidJobStateException",)
CLAIM_STALE_SECONDS = 16 * 60  # past the worker's 15-minute limit: an unreported claim belongs to a dead invocation


@dataclass(frozen=True)
class Outcome:
    kind: str  # "success" | "failure" | "wait"
    message: str
    continuation: str | None = None


def success(message: str) -> Outcome:
    return Outcome("success", message)


def failure(message: str) -> Outcome:
    return Outcome("failure", message)


class Reporter:
    """Reports one result per CodePipeline job (the watchdog may report a timeout before the action returns)."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self.reported: Outcome | None = None
        self._lock = threading.Lock()

    def report(self, outcome: Outcome) -> Outcome:
        """Send the result. InvalidJobStateException (CodePipeline has a result already, or timed the job out) is harmless;
        any other refusal (AccessDenied, throttling) is raised, so the invocation fails visibly instead of looking reported."""
        with self._lock:
            if self.reported is not None:
                return self.reported
            self.reported = outcome
        client = clients.codepipeline()
        try:
            if outcome.kind == "failure":
                client.put_job_failure_result(
                    jobId=self.job_id,
                    failureDetails={"type": "JobFailed", "message": outcome.message[:FAILURE_MESSAGE_MAX]},
                )
            else:
                kwargs = {"executionDetails": {"summary": outcome.message[:SUMMARY_MAX]}}
                if outcome.continuation:
                    kwargs["continuationToken"] = outcome.continuation
                client.put_job_success_result(jobId=self.job_id, **kwargs)
        except ClientError as exc:
            code = (exc.response.get("Error") or {}).get("Code")
            if code not in RESULT_ALREADY_FINAL:
                with self._lock:
                    self.reported = None  # not delivered: a later report (or the raise below) must not look accepted
                raise
            logger.warning("pipeline job %s: result not accepted: %s", self.job_id, code)
        return outcome


def handle(event: dict, deadline_seconds: float | None = None) -> dict:
    """Worker entry for {"CodePipeline.job": ...} events."""
    job = event.get("CodePipeline.job") if isinstance(event.get("CodePipeline.job"), dict) else {}
    job_id = job.get("id") if isinstance(job.get("id"), str) else None
    data = job.get("data") if isinstance(job.get("data"), dict) else {}
    params, problem = _params(data)
    action = params.get("action")
    logger.info(
        "pipeline job %s: action %s, release %s, execution %s",
        job_id,
        action,
        params.get("releaseId"),
        params.get("executionId"),
    )
    if not job_id:
        return {"status": "IGNORED", "message": "no CodePipeline job id"}
    continuation = data.get("continuationToken") if isinstance(data.get("continuationToken"), str) else None
    if not _claim(job_id, continuation, deadline_seconds):
        logger.warning("pipeline job %s was delivered again while its first delivery runs: that one reports it", job_id)
        return {"status": "DUPLICATE", "pipelineJobId": job_id}
    reporter = Reporter(job_id)
    handler = ACTIONS.get(action)
    try:
        if problem:
            outcome = failure(problem)
        elif handler is None or any(not isinstance(params.get(k), str) or not params.get(k) for k in PARAMS):
            outcome = failure(f"UserParameters must name {', '.join(PARAMS)}; got action {action!r}")
        else:
            outcome = handler(params, data, deadline_seconds, reporter)
    except Exception as exc:  # noqa: BLE001 - every failure is the action's result
        logger.exception("pipeline job %s (%s) failed", job_id, action)
        outcome = failure(errors.from_exception(exc).message)
    outcome = reporter.report(outcome)
    _mark_reported(job_id, continuation)
    return {"status": outcome.kind.upper(), "pipelineJobId": job_id, "action": action, "message": outcome.message}


def _params(data: dict) -> tuple[dict, str | None]:
    """UserParameters as a dict, or ({}, the reason it is unusable): never raises."""
    configuration = (
        (data.get("actionConfiguration") or {}).get("configuration")
        if isinstance(data.get("actionConfiguration"), dict)
        else None
    )
    raw = configuration.get("UserParameters") if isinstance(configuration, dict) else None
    if raw is None:
        return {}, "the action has no UserParameters"
    try:
        params = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
    except (TypeError, ValueError) as exc:
        return {}, f"UserParameters are not JSON: {type(exc).__name__}"
    if not isinstance(params, dict):
        return {}, f"UserParameters must be a JSON object, not {type(params).__name__}"
    return params, None


def _claim_key(job_id: str, continuation: str | None) -> dict:
    # a continuation round is a new delivery of the action: it gets its own claim
    suffix = hashlib.sha256((continuation or "").encode()).hexdigest()[:16] if continuation else "first"
    return {"PK": f"PIPELINEJOB#{job_id}", "SK": f"PIPELINEJOB#{suffix}"}


def _claim(job_id: str, continuation: str | None, deadline_seconds: float | None) -> bool:
    """Claim the delivery. A second delivery of the same job (and round) is ignored while the first may still be running; once
    the first has reported, or is older than the worker's limit (it died without reporting), the delivery runs again: the
    platform job runs at most once (`jobs.run` returns a job that is not QUEUED as it is), so the action only re-reports."""
    key = _claim_key(job_id, continuation)
    now = int(time.time())
    item = {**key, "state": "RUNNING", "claimedAt": now, "createdAt": db.now_iso(), "ttl": now + CLAIM_TTL_SECONDS}
    try:
        db.put(item, condition=Attr("PK").not_exists())
        return True
    except errors.ApiError as exc:
        if exc.code != "CONFLICT":
            raise
    previous = db.get(key, consistent=True) or {}
    if previous.get("state") != "REPORTED" and now - int(previous.get("claimedAt") or now) < CLAIM_STALE_SECONDS:
        return False
    try:  # take the claim over, once
        db.put(item, condition=Attr("claimedAt").eq(previous.get("claimedAt")))
    except errors.ApiError:
        return False
    return True


def _mark_reported(job_id: str, continuation: str | None) -> None:
    item = db.get(_claim_key(job_id, continuation), consistent=True)
    if item is not None:
        item["state"] = "REPORTED"
        db.put(item)


def _input_revision(data: dict) -> str | None:
    for artifact in data.get("inputArtifacts") or []:
        if artifact.get("revision"):
            return artifact["revision"]
    return None


def _owned(params: dict, data: dict) -> tuple[dict | None, Outcome | None]:
    """The release when it follows this execution and its snapshot; otherwise the outcome to report (fail, or wait while the API
    records the execution it has just started)."""
    release = db.get(db.release_key(params["projectId"], params["releaseId"]), consistent=True)
    if release is None:
        return None, failure(f"release {params['releaseId']} of project {params['projectId']} does not exist")
    pipeline = release.get("pipeline") or {}
    execution_id = params["executionId"]
    if pipeline.get("executionId") != execution_id:
        if not pipeline.get("executionId") and pipeline.get("startToken") and release.get("status") == "EVALUATING":
            rounds = _wait_rounds(data)
            if rounds < WAIT_ROUNDS:
                message = f"waiting for the platform to record execution {execution_id} (round {rounds + 1})"
                return None, Outcome("wait", message, continuation=json.dumps({"wait": rounds + 1}))
        return None, failure(
            f"execution {execution_id} does not own release {params['releaseId']} "
            f"(it follows {pipeline.get('executionId') or 'no execution'}); start runs from the platform"
        )
    revision = _input_revision(data)
    if revision and pipeline.get("sourceVersionId") and revision != pipeline["sourceVersionId"]:
        return None, failure(
            f"source revision {revision} is not the snapshot {pipeline['sourceVersionId']} release "
            f"{params['releaseId']} was started with"
        )
    return release, None


def _wait_rounds(data: dict) -> int:
    try:
        return int(json.loads(data.get("continuationToken") or "{}").get("wait") or 0)
    except (ValueError, AttributeError, TypeError):
        return 0


def _run_job(job_id: str | None, deadline_seconds: float | None, reporter: Reporter, what: str) -> dict:
    """Run the platform job (once: a job that is not QUEUED any more is returned as it is)."""
    if not job_id:
        raise errors.conflict(f"{what}: the release has no job for this stage")

    def timed_out(job: dict, error: dict) -> None:
        reporter.report(failure(f"{what}: {error['message']}"))

    return jobs.run({"jobId": job_id}, deadline_seconds=deadline_seconds, on_timeout=timed_out)


def _job_failure(job: dict, what: str) -> Outcome:
    error = job.get("error") or {}
    message = error.get("message") if isinstance(error, dict) else str(error)
    return failure(
        f"{what} {job.get('status')}: {error.get('code') if isinstance(error, dict) else ''} {message or ''}".strip()
    )


def deploy_dev(params: dict, data: dict, deadline_seconds: float | None, reporter: Reporter) -> Outcome:
    release, refusal = _owned(params, data)
    if refusal:
        return refusal
    pipeline = release["pipeline"]
    job = _run_job(
        pipeline.get("releaseJobId") or pipeline.get("jobId"),
        deadline_seconds,
        reporter,
        "the Dev deployment",
    )
    if job.get("status") != "SUCCEEDED":
        return _job_failure(job, "the Dev deployment")
    output = job.get("output") or {}
    if output.get("skipped"):
        return failure(f"the Dev deployment was skipped: {output.get('reason')}")
    version = release.get("blueprintVersion")
    return success(
        f"Dev runs blueprint version {version}"
        + (
            " (already deployed)"
            if output.get("deploySkipped")
            else f" (harness version {(output.get('deploy') or {}).get('harnessVersion')})"
        )
    )


def evaluate(params: dict, data: dict, deadline_seconds: float | None, reporter: Reporter) -> Outcome:
    release, refusal = _owned(params, data)
    if refusal:
        return refusal
    job = _run_job(
        release["pipeline"].get("evaluationJobId"),
        deadline_seconds,
        reporter,
        "the evaluation",
    )
    if job.get("status") != "SUCCEEDED":
        return _job_failure(job, "the evaluation")
    release = db.get(db.release_key(params["projectId"], params["releaseId"]), consistent=True) or {}
    record = release.get("evaluation") or {}
    if release.get("status") == "PENDING_APPROVAL" and record.get("passed"):
        return success(f"evaluation PASSED (run {record.get('runId')}): waiting for the Domain approver")
    if release.get("status") == "BLOCKED":
        failed = ", ".join(
            f"{f.get('evaluator')} {f.get('actual')} vs {f.get('threshold')}" for f in record.get("failures") or []
        )
        return failure(f"evaluation BLOCKED the release (run {record.get('runId')}): {failed or 'thresholds not met'}")
    return failure(f"the evaluation finished but release {params['releaseId']} is {release.get('status')}")


def promote_prod(params: dict, data: dict, deadline_seconds: float | None, reporter: Reporter) -> Outcome:
    release, refusal = _owned(params, data)
    if refusal:
        return refusal
    execution_id = params["executionId"]
    approval, promotion = release.get("approval") or {}, release.get("promotion") or {}
    if (
        release.get("status") not in ("PROMOTING", "PROMOTED")
        or approval.get("decision") != "APPROVED"
        or approval.get("executionId") != execution_id
        or promotion.get("executionId") != execution_id
        or not promotion.get("jobId")
    ):
        return failure(
            f"release {params['releaseId']} is {release.get('status')}: Prod is deployed only after the Domain approver has "
            "approved the release and asked for the promotion in the platform (an approval answered elsewhere does not count)"
        )
    job = _run_job(promotion["jobId"], deadline_seconds, reporter, "the Prod deployment")
    if job.get("status") != "SUCCEEDED":
        return _job_failure(job, "the Prod deployment")
    output = job.get("output") or {}
    return success(f"Prod runs harness version {output.get('harnessVersion')} on endpoint {output.get('endpoint')}")


ACTIONS = {"deploy-dev": deploy_dev, "evaluate": evaluate, "promote": promote_prod}
