"""Agent Registry (catalogue): list, search and status changes against the shared registry stored in settings (module 4)."""

from __future__ import annotations

from datetime import datetime

from .. import settings
from . import clients

RECORD_STATUSES = (
    "DRAFT",
    "PENDING_APPROVAL",
    "APPROVED",
    "REJECTED",
    "DEPRECATED",
    "CREATING",
    "UPDATING",
    "CREATE_FAILED",
    "UPDATE_FAILED",
)
DECISION_STATUSES = ("APPROVED", "REJECTED", "DEPRECATED")


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def record_view(row: dict) -> dict:
    """The compact record the UI table shows (timestamps as ISO strings)."""
    view = {
        "recordId": row.get("recordId"),
        "recordArn": row.get("recordArn"),
        "name": row.get("name"),
        "displayName": row.get("displayName"),
        "description": row.get("description"),
        "recordType": row.get("recordType"),
        "recordVersion": row.get("recordVersion"),
        "status": row.get("status"),
        "statusReason": row.get("statusReason"),
        "createdAt": _iso(row.get("createdAt")),
        "updatedAt": _iso(row.get("updatedAt")),
        "createdBy": row.get("createdBy"),
    }
    if "descriptors" in row:
        view["descriptors"] = row["descriptors"]
    if "customMetadata" in row:
        view["customMetadata"] = row["customMetadata"]
    return view


def list_records(status: str | None = None) -> list[dict]:
    registry_id = settings.registry_id()
    client = clients.registry_control()
    kwargs: dict = {"registryId": registry_id, "maxResults": 100}
    if status:
        kwargs["filters"] = [{"name": "status", "values": [status]}]
    rows: list[dict] = []
    while True:
        page = client.list_registry_records(**kwargs)
        rows.extend(page.get("registryRecords", []))
        token = page.get("nextToken")
        if not token:
            break
        kwargs["nextToken"] = token
    return [record_view(r) for r in rows]


SEARCH_MAX_RESULTS = (
    20  # SearchDiscoverableRegistryRecords caps maxResults at 20 (ValidationException above it, seen live 9 Oct 2026)
)


def search(query: str) -> list[dict]:
    registry_id = settings.registry_id()
    page = clients.registry_data().search_discoverable_registry_records(
        searchQuery=query, registryIds=[registry_id], maxResults=SEARCH_MAX_RESULTS
    )
    return [record_view(r) for r in page.get("registryRecords", [])]


def get_record(record_id: str) -> dict:
    registry_id = settings.registry_id()
    return record_view(clients.registry_control().get_registry_record(registryId=registry_id, recordId=record_id))


def set_status(record_id: str, status: str, reason: str | None = None) -> dict:
    registry_id = settings.registry_id()
    kwargs: dict = {"registryId": registry_id, "recordId": record_id, "status": status}
    if reason:
        kwargs["statusReason"] = reason
    response = clients.registry_control().update_registry_record_status(**kwargs)
    return {
        "recordId": response.get("recordId", record_id),
        "recordArn": response.get("recordArn"),
        "status": response.get("status", status),
        "statusReason": response.get("statusReason", reason),
        "updatedAt": _iso(response.get("updatedAt")),
    }
