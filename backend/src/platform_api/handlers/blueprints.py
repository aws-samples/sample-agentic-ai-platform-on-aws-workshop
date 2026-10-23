"""Blueprint versions (contract sections 3, 4 and 6): list, create, get, render dry run, and the deploy job.

A version is immutable; `POST` with the same `spec` and `systemPrompt` as an existing version returns that version (repeated
`workshop apply` runs are safe). Rendering goes through `platform_sdk.blueprint.render` with the project's registered resources.
"""

from __future__ import annotations

import json

from .. import db, errors, jobs, validation
from ..services import clients
from ..services import harness as harness_service
from .projects import load_project

MAX_PROMPT_CHARS = 200_000


def parse_version(value) -> int:
    text = str(value).strip()
    if not text.isdigit() or int(text) < 1:
        raise errors.validation(f"version must be a positive integer (got {value!r})")
    return int(text)


def load_blueprint(project_id: str, version) -> dict:
    item = db.get(db.blueprint_key(project_id, parse_version(version)))
    if item is None:
        raise errors.not_found(f"blueprint version {version} of project {project_id!r} does not exist")
    return item


def versions(project_id: str) -> list[dict]:
    return db.query(db.project_pk(project_id), "BLUEPRINT#")


def _normalised(value) -> str:
    """JSON text of a spec after the DynamoDB number round trip, so a stored version compares equal to a fresh body."""
    return json.dumps(db.from_dynamo(db.to_dynamo(value)), sort_keys=True, default=str)


def _prompt_text(body: dict) -> str:
    """`systemPrompt` verbatim (no whitespace stripping: the text is what the harness receives), non-blank, size-capped."""
    value = body.get("systemPrompt")
    if not isinstance(value, str) or not value.strip():
        raise errors.validation("systemPrompt must be the resolved prompt text (a non-empty string)")
    if len(value) > MAX_PROMPT_CHARS:
        raise errors.validation(f"systemPrompt is longer than {MAX_PROMPT_CHARS} characters")
    return value


def _check_model(project: dict, alias: str) -> None:
    approved = clients.cfg().model_aliases
    if alias not in approved:
        raise errors.validation(f"spec.model.ref model/{alias} is not an approved model alias (approved: {approved})")
    allowed = (project.get("litellm") or {}).get("models") or []
    if allowed and alias not in allowed:
        raise errors.validation(
            f"spec.model.ref model/{alias} is not enabled for project {project['projectId']!r} (project models: "
            f"{allowed}); ask the platform admin to add it with PATCH /projects/{{id}} models"
        )


def list_blueprints(request) -> list:
    project_id = request.params["projectId"]
    load_project(project_id)
    return [db.strip_keys(item) for item in versions(project_id)]


def create(request):
    project_id = request.params["projectId"]
    project = load_project(project_id)
    body = request.require_body()
    spec = validation.mapping(body, "spec", required=True)
    system_prompt = _prompt_text(body)
    notes = validation.string(body, "notes", required=False, default="", max_length=4000) or ""
    existing = versions(project_id)
    for item in existing:
        if _normalised(item.get("spec")) == _normalised(spec) and item.get("systemPrompt") == system_prompt:
            return 200, {**db.strip_keys(item), "existing": True}
    version = max((int(item["version"]) for item in existing), default=0) + 1
    bp = harness_service.blueprint_from_item(project, {"spec": spec, "systemPrompt": system_prompt, "version": version})
    _check_model(project, bp.model_alias)
    item = {
        **db.blueprint_key(project_id, version),
        "projectId": project_id,
        "version": version,
        "spec": bp.api_spec(),
        "systemPrompt": bp.system_prompt,
        "notes": notes,
        "model": bp.model_alias,
        "thresholds": bp.thresholds,
        "createdAt": db.now_iso(),
        "createdBy": request.principal.username,
    }
    db.put(item)
    return 201, db.strip_keys(item)


def get(request) -> dict:
    project_id = request.params["projectId"]
    load_project(project_id)
    return db.strip_keys(load_blueprint(project_id, request.params["version"]))


def render(request) -> dict:
    """Dry run: the CreateHarness payload for `?env=` (default dev), no side effects."""
    project_id = request.params["projectId"]
    project = load_project(project_id)
    item = load_blueprint(project_id, request.params["version"])
    env = validation.env(request.query.get("env") or "dev")
    return harness_service.render_version(project, item, env)


def deploy(request):
    """POST /projects/{id}/deploy {version, env: dev} -> 202 {jobId}; the worker ensures the key, renders and deploys."""
    project_id = request.params["projectId"]
    load_project(project_id)
    body = request.require_body()
    version = parse_version(body.get("version"))
    item = load_blueprint(project_id, version)
    env = validation.env(validation.string(body, "env", required=False, default="dev"))
    if env != "dev":
        raise errors.validation("deploy targets env 'dev' only; Prod is reached by promoting an approved release")
    from .releases import refuse_dev_change  # releases imports this module

    refuse_dev_change(project_id, env)
    job = jobs.submit(
        "deploy",
        {"projectId": project_id, "env": env, "version": int(item["version"]), "model": item.get("model")},
        request.principal.username,
    )
    return 202, {
        "jobId": job["jobId"],
        "status": job["status"],
        "type": "deploy",
        "projectId": project_id,
        "env": env,
        "version": int(item["version"]),
    }
