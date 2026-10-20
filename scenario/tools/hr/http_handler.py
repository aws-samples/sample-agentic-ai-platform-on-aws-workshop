"""AnyCompany HR tools behind an HTTP API: the entry point of the on-behalf-of (OBO) Lambda function (module 2).

Why this file exists. In modules 1-7 the gateway reaches the HR tools through a Lambda target: it invokes ``handler.handler``
with its own service role, and the tool never learns who the end user is. Module 2 proves a complete on-behalf-of chain
instead. The employee's Keycloak access token is exchanged by AgentCore Identity for the harness, exchanged again for the
gateway's target, and the gateway presents that exchanged token (``Authorization: Bearer``) to an Amazon API Gateway HTTP API
whose JWT authorizer validates signature, issuer, audience and expiry before the function runs. Only OpenAPI (and MCP server)
targets can carry an exchanged OAuth token - Lambda targets accept the gateway service role only - so the same tools are also
exposed as an HTTP API, and this adapter is the piece in between: API Gateway hands it an HTTP event, it unwraps the tool
name and arguments, calls the unchanged ``handler.handler`` and wraps the answer back into an HTTP response. The business
logic in ``handler.py`` is not touched; both Lambda functions ship the same code directory.

Contract. HTTP API payload format 2.0, route ``POST /tools/{tool}``: the tool name is ``pathParameters.tool`` (the same name
as in the gateway schema, so the OpenAPI ``operationId`` and the path agree), the arguments are the JSON object in the body
(empty body = no arguments), and the validated token claims are ``requestContext.authorizer.jwt.claims``. Per call the adapter
  1. prints ONE JSON log line ``{"event": "obo_token_received", "tool": ..., "sub": ..., "preferred_username": ...,
     "employee_id": ..., "aud": [...], "azp": ..., "iss": ..., "exp": ..., "requestId": ...}`` - the evidence that the token
     the tools received is the employee's (same ``sub``) and was exchanged for this API (``aud``, requester in ``azp``).
     ``workshop obo`` reads this line back from CloudWatch Logs. Missing claims are null; the token itself is never logged;
  2. calls ``handler.handler({**arguments, "tool": tool}, context)``;
  3. answers HTTP 200 with the tool's result as the JSON body, unchanged - including the tool's own ``{"error": ...}``
     dicts, which the agent reads and explains - plus ``X-OBO-Subject`` / ``X-OBO-Audience`` headers summarising the identity.
A body that is not a JSON object (or not valid base64 when ``isBase64Encoded``) is HTTP 400; a missing tool name is HTTP 400;
an unexpected exception is HTTP 500; all three carry ``{"error": "<message>"}`` so a caller always gets JSON.

Facts (API Gateway docs, 2026-10-16). The JWT authorizer checks ``iss`` against the configured issuer, ``aud`` against the
configured audiences, ``exp``/``nbf``/``iat``, and only RSA signatures; the claims then reach the integration as a flat object
whose values are strings. A multi-valued claim (``aud`` with several audiences) is therefore expected as the string
``"[a b]"`` rather than a list - ``_claim_list`` turns either form into a list. Environment: the same ``EMPLOYEES_TABLE``,
``LEAVE_REQUESTS_TABLE`` and ``HOLIDAYS_JSON`` variables as ``handler.py``.
"""

from __future__ import annotations

import base64
import binascii
import json
import traceback

import handler as hr_tools  # the unchanged HR tools next to this file (Lambda puts the task root on sys.path)

LOG_EVENT = "obo_token_received"
LOGGED_CLAIMS = ("sub", "preferred_username", "employee_id", "aud", "azp", "iss", "exp")
JSON_HEADERS = {"Content-Type": "application/json"}


def _claim_list(value) -> list[str] | None:
    """A claim as a list of strings: a list passes through, API Gateway's ``"[a b]"`` form is split, any other string is [string]."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    text = str(value).strip()
    if text.startswith("[") and text.endswith("]"):
        return [part.strip("\"'") for part in text[1:-1].replace(",", " ").split() if part.strip("\"'")]
    return [text]


def _claims(event: dict) -> dict:
    """The validated JWT claims, or {} when the route was invoked without the authorizer (local tests)."""
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    claims = (authorizer.get("jwt") or {}).get("claims") or {}
    return claims if isinstance(claims, dict) else {}


def _identity(tool: str, event: dict) -> dict:
    """The log record: event name, tool, the identity claims (null when absent) and the API Gateway request id."""
    claims = _claims(event)
    record = {"event": LOG_EVENT, "tool": tool}
    record.update({name: claims.get(name) for name in LOGGED_CLAIMS})
    record["aud"] = _claim_list(claims.get("aud"))
    record["requestId"] = (event.get("requestContext") or {}).get("requestId")
    return record


def _arguments(event: dict) -> dict:
    """The tool arguments: the JSON object in the request body ({} for no body). ValueError for anything else."""
    body = event.get("body")
    if body in (None, ""):
        return {}
    if event.get("isBase64Encoded"):
        try:
            body = base64.b64decode(body, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise ValueError(f"request body is not valid base64-encoded UTF-8: {exc}") from exc
    try:
        arguments = json.loads(body)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError(f"request body is not valid JSON: {exc}") from exc
    if not isinstance(arguments, dict):
        raise ValueError(f"request body must be a JSON object of tool arguments, got {type(arguments).__name__}")
    return arguments


def _response(status: int, payload, headers: dict | None = None) -> dict:
    """An HTTP API (payload 2.0) response with a JSON body."""
    return {
        "statusCode": status,
        "headers": {**JSON_HEADERS, **(headers or {})},
        "body": json.dumps(payload, default=str),
    }


def handler(event, context):
    """API Gateway entry point: POST /tools/{tool} -> ``handler.handler`` with the body as the tool arguments."""
    event = event if isinstance(event, dict) else {}
    tool = str((event.get("pathParameters") or {}).get("tool") or "").strip()
    identity = _identity(tool, event)
    print(json.dumps(identity, default=str))  # one line per call: the OBO evidence (claims only, never the token)
    headers = {}
    if identity["sub"] is not None:
        headers["X-OBO-Subject"] = str(identity["sub"])
    if identity["aud"]:
        headers["X-OBO-Audience"] = ",".join(identity["aud"])
    if not tool:
        return _response(400, {"error": "The tool name is missing: call POST /tools/{tool}."}, headers)
    try:
        arguments = _arguments(event)
    except ValueError as exc:
        return _response(400, {"error": str(exc)}, headers)
    try:
        result = hr_tools.handler({**arguments, "tool": tool}, context)
    except Exception as exc:  # handler.handler reports tool failures itself; this guards the adapter around it
        traceback.print_exc()
        return _response(500, {"error": f"{tool} failed: {exc}"}, headers)
    return _response(200, result, headers)
