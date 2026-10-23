"""Job records (JOB#<id>) and the worker dispatcher (build contract sections 3 and 4).

The API Lambda writes a JOB item (QUEUED, 24 h TTL), starts the worker Lambda with an `Event` invocation and answers 202; the
worker runs the handler registered in `HANDLERS` for the job's type (`platform_api.worker_jobs` registers invoke, deploy,
promote, release and evaluate) with the status transitions QUEUED -> RUNNING -> SUCCEEDED | FAILED and `updatedAt`; errors are mapped with
`errors.from_exception` into `error {code, message[, details]}`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable

from . import db, errors
from .services import clients

logger = logging.getLogger(__name__)
JOB_TTL_SECONDS = 24 * 3600
# A job the release pipeline runs may wait in its QUEUED execution behind any number of other releases' manual approvals: it
# has no TTL while it waits (`ttl_seconds=None`) and expires 24 hours after it has finished (`set_status`). A job whose
# execution ended before running it is finished as FAILED by the release reconciliation, which gives it its TTL.
PIPELINE_JOB_TTL_SECONDS = None
TERMINAL_JOB_STATUSES = ("SUCCEEDED", "FAILED")
JOB_TYPES = ("invoke", "promote", "deploy", "release", "evaluate")
TIMEOUT_MARGIN_SECONDS = 20  # the watchdog fires this long before the Lambda limit, time enough for two DynamoDB writes
HANDLERS: dict[str, Callable[[dict], dict]] = {}
TIMEOUT_HANDLERS: dict[str, Callable[[dict, dict], None]] = {}  # job type -> what else to mark when the watchdog fires


def create(type_: str, input_: dict, created_by: str, ttl_seconds: int | None = JOB_TTL_SECONDS) -> dict:
    if type_ not in JOB_TYPES:
        raise errors.validation(f"job type must be one of {list(JOB_TYPES)}")
    job_id, now = db.new_id(), db.now_iso()
    item = {
        **db.job_key(job_id),
        **db.job_gsi(now, job_id),
        "jobId": job_id,
        "type": type_,
        "status": "QUEUED",
        "input": input_,
        "output": {},
        "error": None,
        "createdBy": created_by,
        "createdAt": now,
        "updatedAt": now,
    }
    if ttl_seconds is not None:
        item["ttl"] = int(time.time()) + ttl_seconds
    db.put(item)
    return db.strip_keys(item)


def get(job_id: str, consistent: bool = False) -> dict | None:
    return db.strip_keys(db.get(db.job_key(job_id), consistent=consistent))


def load(job_id: str) -> dict:
    job = get(job_id)
    if job is None:
        raise errors.not_found(f"job {job_id!r} does not exist (jobs expire after 24 hours)")
    return job


def set_status(job_id: str, status: str, output: dict | None = None, error: dict | str | None = None) -> dict:
    item = db.get(db.job_key(job_id))
    if item is None:
        raise errors.not_found(f"job {job_id!r} does not exist")
    item["status"] = status
    if status in TERMINAL_JOB_STATUSES:  # finished: kept for a day from now, however long it waited
        expires = int(time.time()) + JOB_TTL_SECONDS
        item["ttl"] = min(int(item["ttl"]), expires) if item.get("ttl") else expires
    if output is not None:
        item["output"] = output
    if error is not None:
        item["error"] = error
    item["updatedAt"] = db.now_iso()
    db.put(item)
    return db.strip_keys(item)


def enqueue(job: dict) -> None:
    """Start the worker Lambda asynchronously for this job."""
    function_name = os.environ.get("WORKER_FUNCTION_NAME") or clients.cfg().worker_function_name
    clients.lambda_client().invoke(
        FunctionName=function_name, InvocationType="Event", Payload=json.dumps({"jobId": job["jobId"]}).encode()
    )


def submit(type_: str, input_: dict, created_by: str) -> dict:
    """Create the JOB item and start the worker; the job is marked FAILED when the worker cannot be started."""
    job = create(type_, input_, created_by)
    try:
        enqueue(job)
    except Exception as exc:
        api_error = errors.from_exception(exc)
        logger.exception("could not start the worker for job %s", job["jobId"])
        set_status(job["jobId"], "FAILED", error=api_error.as_job_error())
        raise errors.upstream(
            f"the worker could not be started: {api_error.message}", code="WORKER_UNAVAILABLE"
        ) from exc
    return job


def run(
    event: dict, deadline_seconds: float | None = None, on_timeout: Callable[[dict, dict], None] | None = None
) -> dict:
    """Worker entry: {"jobId"} -> run the registered handler for the job's type with status transitions and error capture.

    The JOB item is read with a strongly consistent GetItem (the API wrote it milliseconds before the Event invocation; an
    eventually consistent replica may not have it yet). A missing job raises, so the Lambda invocation is reported as an error
    instead of leaving the job QUEUED forever; a job that is not QUEUED any more is returned unchanged (a replayed event, a manual
    re-invoke or a duplicate delivery never runs a deploy, promote or invoke twice). `on_timeout(job, error)` runs as well when
    the watchdog fires (the release pipeline's action reports the failure to CodePipeline)."""
    job_id = (event or {}).get("jobId")
    if not job_id:
        logger.info(
            "worker event without jobId (keys %s)", sorted(event or {})
        )  # never the event: it may carry credentials
        return {"status": "IGNORED", "message": "no jobId in the event"}
    job = get(job_id, consistent=True)
    if job is None:
        raise errors.not_found(f"job {job_id!r} does not exist (the worker was invoked for an unknown or expired job)")
    if job.get("status") != "QUEUED":
        logger.warning("job %s (%s) is %s: not running it again", job_id, job.get("type"), job.get("status"))
        return job
    handler = HANDLERS.get(job["type"])
    if handler is None:
        message = f"no worker handler registered for job type {job['type']!r}"
        return set_status(job_id, "FAILED", error={"code": "NOT_IMPLEMENTED", "message": message})
    set_status(job_id, "RUNNING")
    started = time.monotonic()
    timed_out = threading.Event()
    watchdog = _watchdog(job, deadline_seconds, timed_out, on_timeout)
    try:
        output = handler(job)
    except Exception as exc:  # noqa: BLE001 - the job record carries the error
        if timed_out.is_set():
            return get(job_id, consistent=True)
        logger.exception("job %s (%s) failed after %.1f s", job_id, job["type"], time.monotonic() - started)
        return set_status(job_id, "FAILED", error=errors.from_exception(exc).as_job_error())
    finally:
        if watchdog is not None:
            watchdog.cancel()
    if timed_out.is_set():  # the watchdog already recorded the TIMEOUT: the late result is not written over it
        logger.warning("job %s (%s) finished after its timeout was recorded; keeping FAILED", job_id, job["type"])
        return get(job_id, consistent=True)
    logger.info("job %s (%s) succeeded in %.1f s", job_id, job["type"], time.monotonic() - started)
    return set_status(job_id, "SUCCEEDED", output=output or {})


def _watchdog(
    job: dict,
    deadline_seconds: float | None,
    timed_out: threading.Event,
    extra: Callable[[dict, dict], None] | None = None,
) -> threading.Timer | None:
    """A timer that records a TIMEOUT on the job (and, through `TIMEOUT_HANDLERS`, on what it owns) shortly before the Lambda
    is killed; `deadline_seconds` is the invocation's remaining time (None: no watchdog, as in the unit tests)."""
    if deadline_seconds is None:
        return None
    delay = max(deadline_seconds - TIMEOUT_MARGIN_SECONDS, 1.0)

    def fire() -> None:
        timed_out.set()
        message = f"the worker ran out of time ({job['type']} job, {delay:.0f} s): the Lambda limit is {deadline_seconds:.0f} s"
        error = {"code": "TIMEOUT", "message": message}
        logger.error("job %s: %s", job["jobId"], message)
        try:
            set_status(job["jobId"], "FAILED", error=error)
            for on_timeout in (TIMEOUT_HANDLERS.get(job["type"]), extra):
                if on_timeout is not None:
                    on_timeout(job, error)
        except Exception:  # noqa: BLE001 - best effort in the last seconds of the invocation
            logger.exception("job %s: the timeout could not be recorded", job["jobId"])

    timer = threading.Timer(delay, fire)
    timer.daemon = True
    timer.start()
    return timer
