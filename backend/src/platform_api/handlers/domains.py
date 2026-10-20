"""Domains (contract section 4): list / create / get / delete; delete is refused while projects exist."""

from __future__ import annotations

from .. import db, errors, validation


def load_domain(domain_id: str) -> dict:
    item = db.get(db.domain_key(domain_id))
    if item is None:
        raise errors.not_found(f"domain {domain_id!r} does not exist")
    return item


def project_items(domain_id: str) -> list[dict]:
    return db.query_gsi1("PROJECT", f"{domain_id}#")


def list_domains(request) -> list:
    return [db.strip_keys(item) for item in db.query_gsi1("DOMAIN")]


def create(request) -> tuple[int, dict]:
    body = request.require_body()
    domain_id = validation.identifier(body, "domainId")
    if db.get(db.domain_key(domain_id)) is not None:
        raise errors.conflict(f"domain {domain_id!r} already exists")
    name = validation.string(body, "name", required=False) or domain_id
    item = {
        **db.domain_key(domain_id),
        **db.domain_gsi(name),
        "domainId": domain_id,
        "name": name,
        "description": validation.string(body, "description", required=False, default="") or "",
        "owner": validation.string(body, "owner", required=False, default="") or "",
        "createdAt": db.now_iso(),
        "createdBy": request.principal.username,
    }
    db.put(item)
    return 201, db.strip_keys(item)


def get(request) -> dict:
    domain = load_domain(request.params["domainId"])
    view = db.strip_keys(domain)
    view["projectCount"] = len(project_items(domain["domainId"]))
    return view


def delete(request) -> dict:
    domain_id = request.params["domainId"]
    load_domain(domain_id)
    projects = project_items(domain_id)
    if projects:
        ids = sorted(p.get("projectId") for p in projects)
        raise errors.conflict(f"domain {domain_id!r} still has projects {ids}; delete them first")
    db.delete(db.domain_key(domain_id))
    return {"deleted": domain_id}
