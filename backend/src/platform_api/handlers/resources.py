"""Resource registrations per environment (contract section 3): list by env, upsert, delete.

`put_resource` is the shared upsert (the handler validates the body, the deploy / promote jobs register what they create).
"""

from __future__ import annotations

from .. import db, errors, validation
from .projects import load_project

TYPES = (
    "gateway",
    "gateway_target",
    "policy",
    "memory",
    "harness",
    "harness_endpoint",
    "credential_provider",
    "registry_record",
    "online_evaluation",
    "knowledge_base",
    "tool",
)


def registered(project_id: str, env: str | None = None) -> list[dict]:
    """The registered resources of a project (optionally one environment) in the contract shape (no key attributes)."""
    return [db.strip_keys(item) for item in db.query(db.project_pk(project_id), db.resource_prefix(env))]


def find_registered(project_id: str, env: str, type_: str, name: str | None = None) -> dict | None:
    """One registered resource by env, type and (optionally) name."""
    if name is not None:
        return db.strip_keys(db.get(db.resource_key(project_id, env, type_, name)))
    return next((row for row in registered(project_id, env) if row.get("type") == type_), None)


def put_resource(
    project_id: str,
    env: str,
    type_: str,
    name: str,
    arn: str | None = None,
    id: str | None = None,  # noqa: A002 - the contract's field name
    url: str | None = None,
    attributes: dict | None = None,
    by: str = "platform",
) -> dict:
    """Upsert one resource row: keeps the first registeredAt/registeredBy, adds updatedAt/updatedBy on later writes."""
    key = db.resource_key(project_id, env, type_, name)
    existing = db.get(key)
    now = db.now_iso()
    item = {
        **key,
        "projectId": project_id,
        "env": env,
        "type": type_,
        "name": name,
        "arn": arn,
        "id": id,
        "url": url,
        "attributes": attributes or {},
        "registeredAt": existing.get("registeredAt", now) if existing else now,
        "registeredBy": existing.get("registeredBy", by) if existing else by,
    }
    if existing:
        item["updatedAt"] = now
        item["updatedBy"] = by
    db.put(item)
    return db.strip_keys(item)


def list_resources(request) -> list:
    project_id = request.params["projectId"]
    load_project(project_id)
    env = validation.env(request.query.get("env"), required=False)
    return registered(project_id, env)


def upsert(request) -> dict:
    project_id = request.params["projectId"]
    load_project(project_id)
    body = request.require_body()
    env = validation.env(validation.string(body, "env"))
    type_ = validation.choice(body, "type", TYPES)
    name = validation.string(body, "name", max_length=512)
    if "#" in name:
        raise errors.validation("name must not contain '#'")
    return put_resource(
        project_id,
        env,
        type_,
        name,
        arn=validation.string(body, "arn", required=False, max_length=2048),
        id=validation.string(body, "id", required=False, max_length=512),
        url=validation.string(body, "url", required=False, max_length=2048),
        attributes=validation.mapping(body, "attributes", default={}) or {},
        by=request.principal.username,
    )


def delete(request) -> dict:
    project_id, env, type_, name = (request.params[k] for k in ("projectId", "env", "type", "name"))
    load_project(project_id)
    validation.env(env)
    key = db.resource_key(project_id, env, type_, name)
    if db.get(key) is None:
        raise errors.not_found(f"resource {env}/{type_}/{name} is not registered for project {project_id!r}")
    db.delete(key)
    return {"deleted": {"projectId": project_id, "env": env, "type": type_, "name": name}}
