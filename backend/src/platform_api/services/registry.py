"""Agent Registry (catalogue): list, search and status changes against the shared registry stored in settings (module 4),
the category of every record (the Catalogue screen's tabs) and the AGENT record the promote job writes for a Prod agent.

Categories: `model`, `knowledge-base`, `tool`, `agent` or `other`. AGENT records are agents and MCP / GATEWAY records are tools
by type; a CUSTOM record is classified by the `kind` the platform writes into its custom descriptor (`{"kind": "knowledge-base",
...}` for the HR policy knowledge base, `{"kind": "model", ...}` for an approved model alias). ListRegistryRecords returns no
descriptors, so CUSTOM rows cost one GetRegistryRecord each, answered from a per-container cache while the record's updatedAt
does not change; search results and single records carry their descriptors already.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote

from platform_sdk import names

from .. import errors, settings
from . import clients

logger = logging.getLogger(__name__)

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
CATEGORIES = ("model", "knowledge-base", "tool", "agent", "other")
_TYPE_CATEGORIES = {"AGENT": "agent", "MCP": "tool", "GATEWAY": "tool"}
_KIND_CATEGORIES = {
    "knowledge-base": "knowledge-base",
    "knowledge_base": "knowledge-base",
    "knowledgebase": "knowledge-base",
    "kb": "knowledge-base",
    "model": "model",
    "tool": "tool",
    "tools": "tool",
    "mcp": "tool",
    "agent": "agent",
}
CATEGORY_FETCH_WORKERS = 8
_CATEGORY_CACHE: dict[tuple[str, str], tuple[str | None, str]] = {}  # (registryId, recordId) -> (updatedAt, category)


def reset() -> None:
    """Forget the cached categories (tests)."""
    _CATEGORY_CACHE.clear()


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


def custom_kind(descriptors) -> str | None:
    """The `kind` of a CUSTOM record's descriptor payload (`descriptors.custom.data` is a JSON string), lower-cased."""
    if not isinstance(descriptors, dict):
        return None
    data = (descriptors.get("custom") or {}).get("data") if isinstance(descriptors.get("custom"), dict) else None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    kind = data.get("kind") if isinstance(data, dict) else None
    return str(kind).strip().lower() if kind else None


def category_of(record: dict) -> str | None:
    """model | knowledge-base | tool | agent | other; None for a CUSTOM record whose descriptors are not at hand."""
    record_type = record.get("recordType")
    if record_type in _TYPE_CATEGORIES:
        return _TYPE_CATEGORIES[record_type]
    if record_type == "CUSTOM":
        if "descriptors" not in record:
            return None
        return _KIND_CATEGORIES.get(custom_kind(record.get("descriptors")) or "", "other")
    return "other"


def record_view(row: dict) -> dict:
    """The compact record the UI table shows (timestamps as ISO strings); `category` when it can be told from the row."""
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
    category = row.get("category") or category_of(row)
    if category is not None:
        view["category"] = category
    if "descriptors" in row:
        view["descriptors"] = row["descriptors"]
    if "customMetadata" in row:
        view["customMetadata"] = row["customMetadata"]
    return view


def _categorise(rows: list[dict], registry_id: str) -> None:
    """Set `category` on the CUSTOM list rows: one GetRegistryRecord each (in parallel), cached per container while the record's
    updatedAt stands. A record that cannot be read this time is `other` for this response only (it still shows under the All
    tab) and is read again next time: a throttled call must not misfile a record for the life of the container."""
    pending: list[dict] = []
    for row in rows:
        if category_of(row) is not None or not row.get("recordId"):
            continue
        cached = _CATEGORY_CACHE.get((registry_id, row["recordId"]))
        if cached and cached[0] == _iso(row.get("updatedAt")):
            row["category"] = cached[1]
            continue
        pending.append(row)
    if not pending:
        return
    client = clients.registry_control()

    def fetch(row: dict) -> str | None:
        try:
            detail = client.get_registry_record(registryId=registry_id, recordId=row["recordId"])
            return category_of(detail) or "other"
        except Exception as exc:  # noqa: BLE001 - the row keeps its place in the list
            logger.warning("GetRegistryRecord(%s) failed while classifying: %s", row["recordId"], exc)
            return None

    with ThreadPoolExecutor(max_workers=min(CATEGORY_FETCH_WORKERS, len(pending))) as pool:
        for row, category in zip(pending, pool.map(fetch, pending), strict=True):
            row["category"] = category or "other"
            if category is not None:
                _CATEGORY_CACHE[(registry_id, row["recordId"])] = (_iso(row.get("updatedAt")), category)


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
    _categorise(rows, registry_id)
    return [record_view(r) for r in rows]


SEARCH_MAX_RESULTS = (
    20  # SearchDiscoverableRegistryRecords caps maxResults at 20 (ValidationException above it, seen live 9 Oct 2026)
)


def search(query: str) -> list[dict]:
    registry_id = settings.registry_id()
    page = clients.registry_data().search_discoverable_registry_records(
        searchQuery=query, registryIds=[registry_id], maxResults=SEARCH_MAX_RESULTS
    )
    rows = page.get("registryRecords", [])
    _categorise(rows, registry_id)  # search hits carry descriptors; a hit without them is read once
    return [record_view(r) for r in rows]


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


# ---- the AGENT record of a promoted release --------------------------------------------------------------------------------
# The promote job registers the Prod agent as an AGENT record (an A2A agent card pointing at the live endpoint) so the Catalogue
# screen lists it under its Agents tab. One record per project, updated on every promotion (an update of an APPROVED record is a
# new DRAFT revision: submit and approve again); the domain approver approved the release, so the record is approved straight
# away (the registry has no auto-approval rules). AWS docs read 2026-10-21: the descriptor's dataSchemaVersion must be "0.3"
# ("0.3.0" is rejected) while the card's own protocolVersion is "0.3.0"; the card is validated against A2A 0.3.0 AgentCard
# (required: name, description, url, version, protocolVersion, capabilities, defaultInputModes, defaultOutputModes, skills with
# id, name, description and tags).
A2A_SCHEMA_VERSION = "0.3"
A2A_PROTOCOL_VERSION = "0.3.0"
AGENT_RECORD_TAGS = {"ManagedBy": "platform"}
RECORD_SETTLE_SECONDS = 30  # CREATING -> DRAFT takes about 1.5 s live (probe of 8 Oct 2026)
STATUS_REASON_MAX = 255
DISPLAY_NAME_MAX = 255
TRANSIENT_STATUSES = ("CREATING", "UPDATING")
# UpdateRegistryRecordStatus(APPROVED) is documented from PENDING_APPROVAL and from REJECTED ("a curator can directly approve a
# rejected record"); DEPRECATED is terminal and the failed statuses have nothing to approve
APPROVABLE_STATUSES = ("PENDING_APPROVAL", "REJECTED")


def agent_record_name(project_id: str) -> str:
    return names.ProjectNames(project_id, "prod").registry_record_agent


def harness_invoke_url(harness_arn: str, region: str, qualifier: str = "live") -> str:
    """The InvokeHarness endpoint of one harness endpoint (SigV4-signed POST; the A2A card's `url`)."""
    return f"https://bedrock-agentcore.{region}.amazonaws.com/harnesses/invoke?harnessArn={quote(harness_arn, safe='')}&qualifier={qualifier}"


def agent_card(project: dict, item: dict, promotion: dict, region: str, ui_url: str | None = None) -> dict:
    """An A2A 0.3.0 agent card for the promoted agent: what the registry stores as the AGENT record's descriptor. Only the
    required fields plus the transport; `provider` needs both an organization and a URL, so it is set only with a UI URL."""
    project_id = project["projectId"]
    name = project.get("name") or project_id
    description = str(
        project.get("description")
        or (item.get("spec") or {}).get("description")
        or f"{name}: the {project.get('domainId') or 'platform'} agent promoted to Prod"
    )[:1000]
    card = {
        "protocolVersion": A2A_PROTOCOL_VERSION,
        "name": name,
        "description": description,
        "url": harness_invoke_url(promotion["harnessArn"], region, promotion.get("endpoint") or "live"),
        "preferredTransport": "JSONRPC",
        "version": str(item.get("version") or promotion.get("harnessVersion") or "1"),
        "capabilities": {"streaming": True, "pushNotifications": False, "stateTransitionHistory": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": project_id,
                "name": name,
                "description": description,
                "tags": [t for t in (project.get("domainId"), "prod", f"blueprint-v{item.get('version')}") if t],
            }
        ],
    }
    if ui_url:
        card["provider"] = {"organization": project.get("domainId") or "platform", "url": ui_url}
    return card


def _find_record(client, registry_id: str, name: str) -> dict | None:
    page = client.list_registry_records(registryId=registry_id, filters=[{"name": "name", "values": [name]}])
    for row in page.get("registryRecords", []):
        if row.get("name") == name:
            return row
    return None


def _settled(client, registry_id: str, record_id: str, sleep=time.sleep, clock=time.monotonic) -> dict:
    """The record once it has left CREATING / UPDATING (the status the next step depends on)."""
    deadline = clock() + RECORD_SETTLE_SECONDS
    while True:
        record = client.get_registry_record(registryId=registry_id, recordId=record_id)
        if record.get("status") not in TRANSIENT_STATUSES or clock() >= deadline:
            return record
        sleep(1)


def register_agent(project: dict, item: dict, release: dict, promotion: dict, by: str) -> dict:
    """Create or update the project's AGENT record for a promoted release and approve it. Best effort: the Prod agent is live
    whatever happens here, so a failure is returned (`error`) and logged instead of raised; no registry -> `skipped`."""
    project_id = project["projectId"]
    name = agent_record_name(project_id)
    registry_id = settings.load().get("registryId")
    if not registry_id:
        return {"name": name, "skipped": "no Agent Registry configured (PUT /settings {registryId})"}
    cfg = clients.cfg()
    version = str(item.get("version") or promotion.get("harnessVersion") or "1")
    approval = release.get("approval") or {}
    reason = (
        f"release {release.get('releaseId')} (blueprint version {version}) approved by {approval.get('by') or by} "
        f"and promoted to Prod by {by}"
    )[:STATUS_REASON_MAX]
    card = agent_card(project, item, promotion, cfg.region, cfg.get("ui/url"))
    descriptor = {"data": json.dumps(card), "dataSchemaVersion": A2A_SCHEMA_VERSION}
    display_name = f"{project.get('name') or project_id} (Prod)"[:DISPLAY_NAME_MAX]
    description = f"{card['description']} Promoted release {release.get('releaseId')}, blueprint version {version}."
    try:
        client = clients.registry_control()
        existing = _find_record(client, registry_id, name)
        if existing is None:
            created = client.create_registry_record(
                registryId=registry_id,
                name=name,
                displayName=display_name,
                description=description,
                recordType="AGENT",
                recordVersion=version,
                descriptors={"a2aAgentCard": descriptor},
                tags={**AGENT_RECORD_TAGS, "ProjectId": project_id},
            )
            record_id = created.get("recordId") or str(created.get("recordArn", "")).rsplit("/", 1)[-1]
            action = "created"
        else:
            record_id = existing["recordId"]
            client.update_registry_record(
                registryId=registry_id,
                recordId=record_id,
                displayName={"optionalValue": display_name},
                description={"optionalValue": description},
                recordVersion=version,
                descriptors={"optionalValue": {"a2aAgentCard": {"optionalValue": descriptor}}},
            )
            action = "updated"
        record = _settled(client, registry_id, record_id)
        # a new record, or the new DRAFT revision that an update of an approved record creates
        if record.get("status") == "DRAFT":
            client.submit_registry_record_for_approval(registryId=registry_id, recordId=record_id)
            record = _settled(client, registry_id, record_id)
        result = {
            "name": name,
            "recordId": record_id,
            "recordArn": record.get("recordArn"),
            "registryId": registry_id,
            "status": record.get("status"),
            "recordVersion": version,
            "action": action,
        }
        if record.get("status") in APPROVABLE_STATUSES:
            client.update_registry_record_status(
                registryId=registry_id, recordId=record_id, status="APPROVED", statusReason=reason
            )
            result["status"] = "APPROVED"
        elif record.get("status") != "APPROVED":
            # still CREATING / UPDATING after the settle window, CREATE_FAILED / UPDATE_FAILED, or DEPRECATED (terminal): the
            # record is reported as it is; approving it is not a valid transition
            status = record.get("status")
            result["warning"] = f"record {name} is {status} and was not approved" + (
                f" (it had not settled within {RECORD_SETTLE_SECONDS} s)" if status in TRANSIENT_STATUSES else ""
            )
            logger.warning("Prod agent record %s is %s after the promotion; not approved", name, status)
        return result
    except Exception as exc:  # noqa: BLE001 - never fail a promotion over the catalogue entry
        logger.warning("registering the Prod agent %s in the Agent Registry failed: %s", name, exc)
        return {"name": name, "error": errors.message_of(exc)}
