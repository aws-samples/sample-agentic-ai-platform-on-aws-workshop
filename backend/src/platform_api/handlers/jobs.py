"""GET /jobs/{jobId}: status, output and error of an asynchronous job (clients poll every 2 s)."""

from __future__ import annotations

from .. import jobs


def get(request) -> dict:
    return jobs.load(request.params["jobId"])
