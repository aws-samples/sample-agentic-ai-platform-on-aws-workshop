"""Platform worker Lambda: {"jobId": ...} -> platform_api.jobs.run with the deploy / promote / invoke / release / evaluate handlers
registered by platform_api.worker_jobs (imported below). The invocation's remaining time is handed to `jobs.run`, whose watchdog
records a TIMEOUT on the job (and the release it owns) shortly before the 15-minute limit kills the container.

The handler drops the Lambda runtime's X-Ray trace header before any AWS call: botocore's recursion-detection handler copies
`_X_AMZN_TRACE_ID` (always `Sampled=0` for a function without active tracing) into `X-Amzn-Trace-Id` on every request, and a
harness invoked with that header honours the parent's sampling decision and records no spans - so Playground sessions were
invisible to online evaluation and GenAI Observability (backend-issues-wf3 issue 1). Without the header the harness starts a
sampled root trace, exactly like a direct SDK invocation."""

from __future__ import annotations

import json
import logging
import os

from platform_api import (
    jobs,
    worker_jobs,  # noqa: F401 - registers the job handlers in jobs.HANDLERS
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    # The Python runtime re-sets the variable on every invocation, so it is removed here and not at import time.
    os.environ.pop("_X_AMZN_TRACE_ID", None)
    logger.info("worker event: %s", json.dumps(event, default=str)[:4000])
    worker_jobs.register()  # idempotent; the handlers are registered at import as well
    remaining = getattr(context, "get_remaining_time_in_millis", None)
    deadline = remaining() / 1000 if callable(remaining) else None
    return jobs.run(event if isinstance(event, dict) else {}, deadline_seconds=deadline)
