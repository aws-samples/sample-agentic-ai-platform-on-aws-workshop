"""Catalogue routes over the Agent Registry, plus the EventBridge hook for registry events."""

from __future__ import annotations

import json
import logging

from .. import db, errors, validation
from ..services import registry

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
