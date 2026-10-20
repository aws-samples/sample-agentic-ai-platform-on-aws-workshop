"""Agent keys (contract section 4): list, create (LiteLLM key + credential provider), adopt, patch (cap, suspend/resume)."""

from __future__ import annotations

from .. import db, errors, settings, validation
from ..services import litellm_provisioning as litellm
from .projects import load_project

STATUSES = ("active", "suspended")


def list_keys(request) -> list:
    project_id = request.params["projectId"]
    load_project(project_id)
    return [db.strip_keys(item) for item in db.query(db.project_pk(project_id), "KEY#")]


def _check_model(project: dict, model: str) -> str:
    allowed = (project.get("litellm") or {}).get("models") or []
    if model not in allowed:
        raise errors.validation(f"model {model!r} is not in the project's models {allowed}")
    return model


def create(request) -> tuple[int, dict]:
    project_id = request.params["projectId"]
    project = load_project(project_id)
    body = request.require_body()
    env = validation.env(validation.string(body, "env"))
    model = _check_model(project, validation.string(body, "model"))
    adopt = validation.mapping(body, "adopt", required=False)
    existing = db.get(db.key_key(project_id, env, model))
    if adopt is not None:
        item = litellm.adopt_key(project, env, model, adopt, request.principal.username)
        if existing:
            item["createdAt"] = existing.get("createdAt", item["createdAt"])
            item["updatedAt"] = db.now_iso()
        db.put(item)
        return 201, db.strip_keys(item)
    if existing:
        raise errors.conflict(
            f"key {existing.get('alias')} already exists for {env}/{model}; PATCH it or delete the project to recreate it"
        )
    defaults = settings.load()["defaultKeyCap"]
    max_budget = validation.number(body, "maxBudget", default=defaults["maxBudget"], minimum=0)
    budget_duration = validation.budget_duration(
        validation.string(body, "budgetDuration", required=False, default=defaults["budgetDuration"])
    )
    item = litellm.create_key(project, env, model, max_budget, budget_duration, request.principal.username)
    db.put(item)
    return 201, db.strip_keys(item)


def patch(request) -> dict:
    project_id, env, model = request.params["projectId"], request.params["env"], request.params["model"]
    load_project(project_id)
    validation.env(env)
    body = request.require_body()
    item = db.get(db.key_key(project_id, env, model))
    if item is None:
        raise errors.not_found(f"no key for {env}/{model} in project {project_id!r}")
    max_budget = validation.number(body, "maxBudget", required=False, minimum=0)
    status = validation.choice(body, "status", STATUSES, required=False)
    if max_budget is None and status is None:
        raise errors.validation("send maxBudget and/or status (active | suspended)")
    token = item.get("litellmKeyId")
    if max_budget is not None:
        litellm.update_key_cap(token, max_budget)
        item["maxBudget"] = max_budget
    if status is not None and status != item.get("status"):
        litellm.set_key_status(token, status)
        item["status"] = status
    item["updatedAt"] = db.now_iso()
    item["updatedBy"] = request.principal.username
    db.put(item)
    return db.strip_keys(item)
