"""Catalogue routes over the Agent Registry, the AGENT record of a promoted Prod agent (written by the promote job, or for every
project at once by POST /catalogue/agents/sync), plus the EventBridge hook for registry events."""

from __future__ import annotations

import json
import logging

from .. import db, errors, validation
from ..services import registry
from . import blueprints, releases
from . import resources as resources_store

logger = logging.getLogger(__name__)
SYNC_KEY = {"PK": "CATALOGUE", "SK": "SYNC"}
# Agent Registry limits (botocore model, checked 9 Oct 2026): SearchDiscoverableRegistryRecords.searchQuery max 256 characters,
# UpdateRegistryRecordStatus.statusReason max 255; exceeding them would surface as 502 UPSTREAM_ERROR instead of 400.
SEARCH_QUERY_MAX = 256
STATUS_REASON_MAX = 255


def list_records(request) -> list:
    status = request.query.get("status")
    if status and status not in registry.RECORD_STATUSES:
        raise errors.validation(f"status must be one of {list(registry.RECORD_STATUSES)}")
    return registry.list_records(status or None)


def search(request) -> list:
    query = (request.query.get("q") or "").strip()
    if not query:
        raise errors.validation("q (search text) is required")
    if len(query) > SEARCH_QUERY_MAX:
        raise errors.validation(f"q is longer than {SEARCH_QUERY_MAX} characters (the Agent Registry search limit)")
    return registry.search(query)


def get_record(request) -> dict:
    """GET /catalogue/{recordId}: the record with its descriptors (DRAFT and PENDING_APPROVAL records are not searchable)."""
    return registry.get_record(request.params["recordId"])


def set_status(request) -> dict:
    body = request.require_body()
    status = validation.choice(body, "status", registry.DECISION_STATUSES)
    reason = validation.string(body, "reason", required=False, default="", max_length=STATUS_REASON_MAX) or ""
    return registry.set_status(request.params["recordId"], status, reason)


# ---- the AGENT record of a promoted Prod agent -----------------------------------------------------------------------------
def register_promoted_agent(project: dict, item: dict, release: dict, promotion: dict, by: str) -> dict:
    """The AGENT record of a promoted release in the shared Agent Registry, registered as a `registry_record` resource of the
    project's Prod environment when it was written. Never raises; a record that was written stays in the result even when the
    resource row could not be."""
    name = registry.agent_record_name(project["projectId"])
    result: dict = {"name": name}
    try:
        result = registry.register_agent(project, item, release, promotion, by)
        if result.get("recordId"):
            resources_store.put_resource(
                project["projectId"],
                "prod",
                "registry_record",
                result["name"],
                arn=result.get("recordArn"),
                id=result["recordId"],
                attributes={
                    "registryId": result.get("registryId"),
                    "recordType": "AGENT",
                    "status": result.get("status"),
                    "recordVersion": result.get("recordVersion"),
                    "releaseId": release.get("releaseId"),
                    "blueprintVersion": release.get("blueprintVersion"),
                },
                by=by,
            )
        return result
    except Exception as exc:  # noqa: BLE001 - the promotion stands; the catalogue entry is reported, not enforced
        logger.exception("registering the promoted agent of release %s failed", release.get("releaseId"))
        key = "resourceError" if result.get("recordId") else "error"
        return {**result, key: errors.message_of(exc)}


def latest_promoted_release(project_id: str) -> dict | None:
    """The newest PROMOTED release of a project (GSI1 lists releases newest first), with its table keys."""
    for item in db.query_gsi1("RELEASE", descending=True):
        if item.get("projectId") == project_id and item.get("status") == "PROMOTED":
            return item
    return None


def sync_promoted_agents(by: str) -> list[dict]:
    """Every project's current Prod agent into the Agent Registry: the catch-up for releases promoted before the promote job
    wrote AGENT records, and the repair after a promotion whose registry step failed. One outcome per project; the newest
    PROMOTED release carries the result in its `promotion.registryRecord`, as a fresh promotion would."""
    outcomes: list[dict] = []
    for project in db.query_gsi1("PROJECT"):
        project_id = project.get("projectId")
        name = registry.agent_record_name(project_id)
        release = latest_promoted_release(project_id)
        if release is None:
            outcomes.append({"projectId": project_id, "name": name, "skipped": "no PROMOTED release"})
            continue
        promotion = release.get("promotion") or {}
        base = {"projectId": project_id, "releaseId": release.get("releaseId"), "name": name}
        if not promotion.get("harnessArn"):
            outcomes.append({**base, "skipped": "the promotion carries no Prod harness ARN"})
            continue
        try:
            item = blueprints.load_blueprint(project_id, release["blueprintVersion"])
        except errors.ApiError as exc:
            outcomes.append({**base, "error": exc.message})
            continue
        entry = register_promoted_agent(project, item, release, promotion, by)
        releases.save(release, promotion={**promotion, "registryRecord": entry})
        outcomes.append({**base, **entry})
    return outcomes


def sync_agents(request) -> dict:
    """POST /catalogue/agents/sync (admin): register every project's promoted Prod agent as an approved AGENT record."""
    return {"agents": sync_promoted_agents(request.principal.username if request.principal else "platform")}


def _record_id(detail: dict) -> str | None:
    """recordId of a registry event: top level for record events, request/response parameters for CloudTrail-shaped ones."""
    for source in (detail, detail.get("requestParameters") or {}, detail.get("responseElements") or {}):
        if isinstance(source, dict) and source.get("recordId"):
            return str(source["recordId"])
    return None


def sync_event(event: dict) -> dict:
    """EventBridge `aws.agent-registry` events: log them and touch the CATALOGUE#SYNC item (nice to have)."""
    detail_type = event.get("detail-type")
    logger.info("registry event %s: %s", detail_type, json.dumps(event.get("detail", {}), default=str)[:2000])
    detail = event.get("detail") if isinstance(event.get("detail"), dict) else {}
    try:
        db.put(
            {
                **SYNC_KEY,
                "lastEventAt": db.now_iso(),
                "lastDetailType": detail_type,
                "lastSource": event.get("source"),
                "recordId": _record_id(detail),
                "status": detail.get("status") or (detail.get("responseElements") or {}).get("status"),
                "eventName": detail.get("eventName"),
                "lastDetail": json.loads(json.dumps(detail, default=str)),
            }
        )
    except Exception:  # noqa: BLE001 - never fail the event delivery over the cache item
        logger.exception("could not write the catalogue sync item")
    return {"ok": True, "detailType": detail_type}
