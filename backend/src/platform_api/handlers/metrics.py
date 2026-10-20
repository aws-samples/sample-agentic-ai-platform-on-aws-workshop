"""GET /metrics?projectId=&env=&hours=24 (CloudWatch series for the dashboard; contract section 4)."""

from __future__ import annotations

from .. import errors, validation
from ..services import metrics as metrics_service
from .projects import load_project

MAX_HOURS = 24 * 7


def get(request) -> dict:
    project_id = (request.query.get("projectId") or "").strip()
    if not project_id:
        raise errors.validation("projectId query parameter is required")
    project = load_project(project_id)
    env = validation.env(request.query.get("env"))
    raw_hours = (request.query.get("hours") or "24").strip()
    if not raw_hours.isdigit() or not 1 <= int(raw_hours) <= MAX_HOURS:
        raise errors.validation(f"hours must be an integer between 1 and {MAX_HOURS} (got {raw_hours!r})")
    return metrics_service.project_metrics(project, env, int(raw_hours))
