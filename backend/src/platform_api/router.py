"""Route table and request plumbing for HTTP API v2 events.

Authorization lives here, in the table, never in the handlers (build contract section 4: admin = platform-admins, dev =
agent-developers, approver = approvers, "any" = any signed-in user). `dispatch(event)` returns the Lambda proxy response.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import unquote

from . import auth, errors
from .auth import ADMIN, ANY, APPROVER, DEVELOPER, PUBLIC
from .handlers import (
    blueprints,
    catalogue,
    domains,
    feedback,
    governance,
    health,
    invoke,
    keys,
    me,
    metrics,
    projects,
    releases,
    resources,
    settings,
    spend,
)
from .handlers import jobs as job_routes

logger = logging.getLogger(__name__)
JSON_HEADERS = {"Content-Type": "application/json"}


@dataclass
class Request:
    method: str
    path: str
    params: dict[str, str]
    query: dict[str, str]
    body: Any
    principal: auth.Principal | None
    event: dict = field(default_factory=dict, repr=False)

    def require_body(self) -> dict:
        return errors_validation_object(self.body)


def errors_validation_object(body: Any) -> dict:
    if not isinstance(body, dict):
        raise errors.validation("request body must be a JSON object")
    return body


@dataclass(frozen=True)
class Route:
    method: str
    template: str
    handler: Any
    roles: Any  # PUBLIC | ANY | tuple of roles

    @property
    def segments(self) -> tuple[str, ...]:
        return tuple(s for s in self.template.strip("/").split("/") if s)

    def match(self, method: str, path: str) -> dict[str, str] | None:
        if method != self.method:
            return None
        parts = tuple(s for s in path.strip("/").split("/") if s)
        if len(parts) != len(self.segments):
            return None
        params: dict[str, str] = {}
        for segment, part in zip(self.segments, parts, strict=True):
            if segment.startswith("{") and segment.endswith("}"):
                params[segment[1:-1]] = unquote(part)
            elif segment != part:
                return None
        return params


# ---- the table (contract section 4; static segments win over parameters because they are listed first) -------------------
ROUTES: tuple[Route, ...] = (
    Route("GET", "/health", health.get, PUBLIC),
    Route("GET", "/me", me.get, ANY),
    # workshop shortcut: the tokens of another persona (the UI's "Switch persona" menu)
    Route("POST", "/me/switch", me.switch, ANY),
    Route("GET", "/settings", settings.get, ANY),
    Route("PUT", "/settings", settings.put, (ADMIN,)),
    Route("GET", "/domains", domains.list_domains, ANY),
    Route("POST", "/domains", domains.create, (ADMIN,)),
    Route("GET", "/domains/{domainId}", domains.get, ANY),
    Route("DELETE", "/domains/{domainId}", domains.delete, (ADMIN,)),
    Route("GET", "/projects", projects.list_projects, ANY),
    Route("POST", "/projects", projects.create, (ADMIN,)),
    Route("GET", "/projects/{projectId}", projects.get, ANY),
    Route("PATCH", "/projects/{projectId}", projects.patch, (ADMIN,)),
    Route("DELETE", "/projects/{projectId}", projects.delete, (ADMIN,)),
    Route("GET", "/projects/{projectId}/resources", resources.list_resources, ANY),
    Route("PUT", "/projects/{projectId}/resources", resources.upsert, (DEVELOPER, ADMIN)),
    Route("DELETE", "/projects/{projectId}/resources/{env}/{type}/{name}", resources.delete, (DEVELOPER, ADMIN)),
    Route("GET", "/projects/{projectId}/keys", keys.list_keys, ANY),
    Route("POST", "/projects/{projectId}/keys", keys.create, (DEVELOPER, ADMIN)),
    Route("PATCH", "/projects/{projectId}/keys/{env}/{model}", keys.patch, (ADMIN,)),
    Route("GET", "/catalogue", catalogue.list_records, ANY),
    Route("GET", "/catalogue/search", catalogue.search, ANY),
    Route("POST", "/catalogue/agents/sync", catalogue.sync_agents, (ADMIN,)),
    Route("GET", "/catalogue/{recordId}", catalogue.get_record, ANY),
    Route("POST", "/catalogue/{recordId}/status", catalogue.set_status, (ADMIN,)),
    Route("GET", "/governance", governance.get, ANY),
    Route("GET", "/governance/model-gateway/credentials", governance.model_gateway_credentials, (ADMIN,)),
    Route("GET", "/governance/project-options", governance.project_options, (ADMIN,)),
    Route("POST", "/governance/gateways/{gatewayId}/mode", governance.set_gateway_mode, (ADMIN,)),
    Route("GET", "/spend", spend.get, ANY),
    # ---- step 2: blueprints, deploy, releases, invoke, jobs, metrics, feedback ------------------------------------------
    Route("GET", "/projects/{projectId}/blueprints", blueprints.list_blueprints, ANY),
    Route("POST", "/projects/{projectId}/blueprints", blueprints.create, (DEVELOPER, ADMIN)),
    Route("GET", "/projects/{projectId}/blueprints/{version}", blueprints.get, ANY),
    Route("GET", "/projects/{projectId}/blueprints/{version}/render", blueprints.render, ANY),
    Route("POST", "/projects/{projectId}/deploy", blueprints.deploy, (DEVELOPER, ADMIN)),
    Route("GET", "/projects/{projectId}/releases", releases.list_releases, ANY),
    Route("POST", "/projects/{projectId}/releases", releases.create, (DEVELOPER, ADMIN)),
    Route("GET", "/releases", releases.list_all, ANY),
    Route("GET", "/releases/{releaseId}", releases.get, ANY),
    Route("POST", "/releases/{releaseId}/run", releases.run, (DEVELOPER, ADMIN)),
    Route("POST", "/releases/{releaseId}/evaluation", releases.submit_evaluation, (DEVELOPER, ADMIN)),
    Route("POST", "/releases/{releaseId}/approve", releases.approve, (APPROVER,)),
    Route("POST", "/releases/{releaseId}/reject", releases.reject, (APPROVER,)),
    Route("POST", "/releases/{releaseId}/promote", releases.promote, (APPROVER,)),
    Route("GET", "/metrics", metrics.get, ANY),
    Route("POST", "/invoke", invoke.create, ANY),
    Route("GET", "/jobs/{jobId}", job_routes.get, ANY),
    Route("POST", "/feedback", feedback.create, ANY),
    Route("GET", "/feedback", feedback.list_feedback, ANY),
)


def match(method: str, path: str) -> tuple[Route, dict[str, str]]:
    """The first route whose method and template match; 404 NOT_FOUND otherwise (405 when only the method differs)."""
    path_matched = False
    for route in ROUTES:
        params = route.match(method, path)
        if params is not None:
            return route, params
        if route.match(route.method, path) is not None:
            path_matched = True
    if path_matched:
        raise errors.ApiError(405, "METHOD_NOT_ALLOWED", f"{method} is not supported on {path}")
    raise errors.not_found(f"No route for {method} {path}")


# ---- JSON plumbing ------------------------------------------------------------------------------------------------------------
def parse_body(event: dict) -> Any:
    raw = event.get("body")
    if raw is None or raw == "":
        return None
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise errors.validation(f"request body is not valid JSON: {exc}") from exc


def _json_default(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, (set, tuple)):
        return list(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def respond(status: int, body: Any = None, headers: dict | None = None) -> dict:
    all_headers = {**JSON_HEADERS, **(headers or {})}
    if status == 204 or body is None:
        return {"statusCode": status, "headers": all_headers}
    return {"statusCode": status, "headers": all_headers, "body": json.dumps(body, default=_json_default)}


def dispatch(event: dict) -> dict:
    """Handle one HTTP API v2 proxy event: OPTIONS -> 204, otherwise match, authorize, run the handler, map errors."""
    http = (event.get("requestContext") or {}).get("http") or {}
    method = (http.get("method") or "GET").upper()
    path = event.get("rawPath") or http.get("path") or "/"
    if method == "OPTIONS":  # CORS preflight: API Gateway adds the headers, the body must be an empty 2xx
        return {"statusCode": 204}
    try:
        route, params = match(method, path)
        principal = auth.principal_from_event(event)
        auth.require(principal, route.roles)
        request = Request(
            method=method,
            path=path,
            params=params,
            query=event.get("queryStringParameters") or {},
            body=parse_body(event),
            principal=principal,
            event=event,
        )
        result = route.handler(request)
        # a handler returns the body, (status, body) or (status, body, extra headers)
        status, body, headers = (*result, None)[:3] if isinstance(result, tuple) else (200, result, None)
        return respond(status, body, headers)
    except Exception as exc:  # noqa: BLE001 - every error becomes the contract error shape
        api_error = errors.from_exception(exc)
        if api_error.status >= 500:
            logger.exception("%s %s -> %s %s", method, path, api_error.status, api_error.code)
        else:
            logger.info("%s %s -> %s %s: %s", method, path, api_error.status, api_error.code, api_error.message)
        return respond(api_error.status, api_error.body())
