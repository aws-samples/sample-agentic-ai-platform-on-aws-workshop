"""Projects (contract section 4): list by domain, create (LiteLLM team + four roles), get, patch, delete.

The stored item is exactly what `GET /projects/{id}` returns and what `scripts/e2e_dryrun.py` builds in memory:
projectId, domainId, name, description, status, litellm{teamId, teamAlias, maxBudget, budgetDuration, tpmLimit, rpmLimit, models},
roles{dev{agentRoleArn, gatewayRoleArn}, prod{...}}, allowedToolArns, knowledgeBaseIds, createdAt, createdBy.
"""

from __future__ import annotations

import logging

from platform_sdk import names

from .. import db, errors, settings, validation
from ..services import clients
from ..services import litellm_provisioning as litellm
from ..services import roles as roles_service

logger = logging.getLogger(__name__)


def load_project(project_id: str) -> dict:
    item = db.get(db.project_key(project_id))
    if item is None:
        raise errors.not_found(f"project {project_id!r} does not exist")
    return item


def _caps_from_body(body: dict, defaults: dict) -> dict:
    return {
        "maxBudget": validation.number(body, "maxBudget", default=defaults["maxBudget"], minimum=0),
        "budgetDuration": validation.budget_duration(
            validation.string(body, "budgetDuration", required=False, default=defaults["budgetDuration"])
        ),
        "tpmLimit": validation.integer(body, "tpmLimit", default=defaults["tpmLimit"], minimum=1),
        "rpmLimit": validation.integer(body, "rpmLimit", default=defaults["rpmLimit"], minimum=1),
    }


def list_projects(request) -> list:
    domain_id = request.query.get("domainId")
    items = db.query_gsi1("PROJECT", f"{domain_id}#" if domain_id else None)
    return [db.strip_keys(item) for item in items]


def create(request) -> tuple[int, dict]:
    body = request.require_body()
    try:
        project_id = names.validate_project_id(validation.string(body, "projectId"))
    except ValueError as exc:
        raise errors.validation(str(exc)) from exc
    domain_id = validation.identifier(body, "domainId")
    if db.get(db.domain_key(domain_id)) is None:
        raise errors.validation(f"domain {domain_id!r} does not exist; create it with POST /domains first")
    if db.get(db.project_key(project_id)) is not None:
        raise errors.conflict(f"project {project_id!r} already exists")
    cfg = clients.cfg()
    models = validation.models(validation.string_list(body, "models", required=True), cfg.model_aliases)
    caps = _caps_from_body(body, settings.load()["defaultProjectCap"])
    allowed_tool_arns = validation.string_list(body, "allowedToolArns", default=[])
    kb_ids = validation.string_list(body, "knowledgeBaseIds", default=[])
    name = validation.string(body, "name", required=False) or project_id
    description = validation.string(body, "description", required=False, default="") or ""
    owner = validation.string(body, "owner", required=False)

    step, created_team = "litellm_team", False
    try:
        created_team = litellm.ensure_team(project_id, domain_id, models, caps)["created"]
        step = "roles"
        role_arns = roles_service.ensure_roles(project_id, allowed_tool_arns, kb_ids)
    except Exception as exc:
        logger.exception("project %s provisioning failed at step %s", project_id, step)
        if created_team:
            try:
                litellm.delete_team(project_id)
            except Exception:  # noqa: BLE001 - best-effort rollback
                logger.exception("rollback: deleting team %s failed", project_id)
        try:
            roles_service.delete_roles(project_id)
        except Exception:  # noqa: BLE001 - best-effort rollback
            logger.exception("rollback: deleting roles of %s failed", project_id)
        raise errors.ApiError(502, "PROVISIONING_FAILED", f"step {step}: {errors.message_of(exc)}") from exc

    item = {
        **db.project_key(project_id),
        **db.project_gsi(domain_id, project_id),
        "projectId": project_id,
        "domainId": domain_id,
        "name": name,
        "description": description,
        "status": "ACTIVE",
        "litellm": {"teamId": project_id, "teamAlias": project_id, **caps, "models": models},
        "roles": role_arns,
        "allowedToolArns": allowed_tool_arns,
        "knowledgeBaseIds": kb_ids,
        "createdAt": db.now_iso(),
        "createdBy": request.principal.username,
    }
    if owner:
        item["owner"] = owner
    db.put(item)
    return 201, db.strip_keys(item)


def get(request) -> dict:
    return db.strip_keys(load_project(request.params["projectId"]))


def patch(request) -> dict:
    project_id = request.params["projectId"]
    body = request.require_body()
    project = load_project(project_id)
    changes: dict = {}
    for key in ("name", "description", "owner"):
        if key in body:
            changes[key] = validation.string(body, key, required=(key == "name")) or ""
    litellm_cfg = dict(project.get("litellm") or {})
    team_fields: dict = {}
    if "models" in body:
        models = validation.models(validation.string_list(body, "models", required=True), clients.cfg().model_aliases)
        litellm_cfg["models"], team_fields["models"] = models, models
    if "maxBudget" in body:
        value = validation.number(body, "maxBudget", required=True, minimum=0)
        litellm_cfg["maxBudget"], team_fields["max_budget"] = value, value
    if "budgetDuration" in body:
        value = validation.budget_duration(validation.string(body, "budgetDuration"))
        litellm_cfg["budgetDuration"], team_fields["budget_duration"] = value, value
    if "tpmLimit" in body:
        value = validation.integer(body, "tpmLimit", required=True, minimum=1)
        litellm_cfg["tpmLimit"], team_fields["tpm_limit"] = value, value
    if "rpmLimit" in body:
        value = validation.integer(body, "rpmLimit", required=True, minimum=1)
        litellm_cfg["rpmLimit"], team_fields["rpm_limit"] = value, value
    role_change = False
    allowed_tool_arns = list(project.get("allowedToolArns") or [])
    kb_ids = list(project.get("knowledgeBaseIds") or [])
    if "allowedToolArns" in body:
        allowed_tool_arns, role_change = validation.string_list(body, "allowedToolArns", required=True), True
    if "knowledgeBaseIds" in body:
        kb_ids, role_change = validation.string_list(body, "knowledgeBaseIds", required=True), True
    if not (changes or team_fields or role_change):
        raise errors.validation(
            "nothing to update: send any of name, description, owner, models, maxBudget, budgetDuration, tpmLimit, "
            "rpmLimit, allowedToolArns, knowledgeBaseIds"
        )
    if team_fields:
        litellm.update_team(project_id, **team_fields)
    if role_change:
        project["roles"] = roles_service.ensure_roles(project_id, allowed_tool_arns, kb_ids)
        project["allowedToolArns"], project["knowledgeBaseIds"] = allowed_tool_arns, kb_ids
    project.update(changes)
    project["litellm"] = litellm_cfg
    project["updatedAt"] = db.now_iso()
    project["updatedBy"] = request.principal.username
    db.put(project)
    return db.strip_keys(project)


def delete(request) -> dict:
    """Remove the team, keys, credential providers and roles, then every item of the project (not the attendee-created
    AgentCore resources: `workshop cleanup` deletes those)."""
    project_id = request.params["projectId"]
    load_project(project_id)
    force = str(request.query.get("force", "")).lower() in ("true", "1", "yes")
    pk = db.project_pk(project_id)
    promoted = [r for r in db.query(pk, "RELEASE#") if r.get("status") == "PROMOTED"]
    if promoted and not force:
        raise errors.conflict(
            f"project {project_id!r} has {len(promoted)} PROMOTED release(s); delete the Prod harness first or pass ?force=true"
        )
    summary: dict = {"deleted": project_id, "keys": [], "team": False, "roles": [], "items": 0}
    for key_item in db.query(pk, "KEY#"):
        summary["keys"].append(litellm.delete_key(key_item))
    summary["team"] = litellm.delete_team(project_id)
    summary["roles"] = roles_service.delete_roles(project_id)
    items = db.query(pk) + db.query(f"FEEDBACK#{project_id}")
    summary["items"] = db.delete_many(items)
    return summary
