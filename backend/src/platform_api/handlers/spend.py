"""GET /spend?projectId=&env=."""

from __future__ import annotations

from .. import errors, validation
from ..services import spend as spend_service
from .projects import load_project


def get(request) -> dict:
    project_id = (request.query.get("projectId") or "").strip()
    if not project_id:
        raise errors.validation("projectId query parameter is required")
    env = validation.env(request.query.get("env"), required=False)
    return spend_service.project_spend(load_project(project_id), env)
