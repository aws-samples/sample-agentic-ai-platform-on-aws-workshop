"""SigV4-signed MCP client for AgentCore Gateway URLs (build contract section 5, Phase 3A).

    tools = mcp.list_tools(gateway_url)                                    # as the caller's own AWS identity
    result = mcp.call_tool(gateway_url, "hrtools___get_leave_balance", {"employee_id": "E1001"})
    print(result.text)                                                     # the tool's text content
    agent = mcp.assume_role_session(agent_role_arn)                        # module 4: act as a project's agent role
    tools = mcp.list_tools(gateway_url, session=agent)                     # what that principal is allowed to see

JSON-RPC 2.0 over the MCP streamable HTTP transport: every call is one POST to the gateway URL with
`Accept: application/json, text/event-stream`; the gateway answers either with a JSON body or with a server-sent-events
stream whose `data:` lines carry the JSON-RPC response. Requests are signed with SigV4 for service `bedrock-agentcore`
(the gateways of the modules use the AWS_IAM inbound authorizer), so the caller needs `bedrock-agentcore:InvokeGateway` on
the gateway ARN. IAM refusals surface as `McpHttpError` (HTTP 403); refusals by the gateway or the policy engine surface as
`McpError` (a JSON-RPC error) or as a `ToolResult` with `is_error` set.

The region comes from `config.default_region` (the same rule as every other client in the SDK) unless given explicitly.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError

from .config import default_region

SERVICE = "bedrock-agentcore"
ACCEPT = "application/json, text/event-stream"
PROTOCOL_VERSION = "2025-06-18"
DEFAULT_TIMEOUT = 60
CLIENT_INFO = {"name": "platform_sdk.mcp", "version": "0.1.0"}


class McpError(Exception):
    """The gateway answered with a JSON-RPC error (unknown tool, invalid arguments, policy denial, ...)."""

    def __init__(self, code: int | None, message: str, data: Any = None, method: str | None = None):
        super().__init__(f"MCP error {code}: {message}" if code is not None else f"MCP error: {message}")
        self.code = code
        self.message = message
        self.data = data
        self.method = method


class McpHttpError(Exception):
    """The HTTP layer refused the request (403 = the caller may not invoke this gateway, 404 = wrong URL, ...)."""

    def __init__(self, status: int, text: str, url: str):
        super().__init__(f"HTTP {status} from {url}: {text[:300]}")
        self.status = status
        self.text = text
        self.url = url

    @property
    def access_denied(self) -> bool:
        return self.status == 403


@dataclass
class ToolResult:
    """Parsed `tools/call` result: `content` blocks, `is_error`, optional `structured` content and the raw result."""

    name: str
    content: list[dict] = field(default_factory=list)
    is_error: bool = False
    structured: Any = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def text(self) -> str:
        """All text blocks joined with newlines (tool Lambdas return their JSON as one text block)."""
        return "\n".join(block.get("text", "") for block in self.content if block.get("type") == "text")

    def json(self) -> Any:
        """The structured content, or the text parsed as JSON when it is JSON, else the text itself."""
        if self.structured is not None:
            return self.structured
        text = self.text
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return text


# ---- credentials and signing ---------------------------------------------------------------------------------------
def assume_role_session(
    role_arn: str,
    session_name: str = "platform-lab",
    duration_seconds: int = 3600,
    region: str | None = None,
    base_session: boto3.Session | None = None,
    wait_seconds: float = 0,
) -> boto3.Session:
    """A boto3 Session holding temporary credentials of `role_arn` (the project roles trust the account root).

    `wait_seconds` retries AccessDenied for that long: IAM is eventually consistent and a role stamped a moment ago may
    refuse the first AssumeRole for about ten seconds.
    """
    base = base_session or boto3.Session()
    region = default_region(region or base.region_name)
    sts = base.client("sts", region_name=region)
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            creds = sts.assume_role(RoleArn=role_arn, RoleSessionName=session_name, DurationSeconds=duration_seconds)[
                "Credentials"
            ]
            break
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "AccessDenied" or time.monotonic() >= deadline:
                raise
            time.sleep(3)
    return boto3.Session(
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
        region_name=region,
    )


def sign(
    url: str, body: str, session: boto3.Session | None = None, region: str | None = None, headers: dict | None = None
) -> dict[str, str]:
    """SigV4 headers (Authorization, X-Amz-Date, X-Amz-Security-Token, ...) for one POST of `body` to `url`."""
    session = session or boto3.Session()
    region = default_region(region or session.region_name)
    credentials = session.get_credentials()
    if credentials is None:
        raise McpHttpError(401, "no AWS credentials in the session (configure a profile or pass session=)", url)
    request = AWSRequest(
        method="POST",
        url=url,
        data=body.encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": ACCEPT, **(headers or {})},
    )
    SigV4Auth(credentials.get_frozen_credentials(), SERVICE, region).add_auth(request)
    return dict(request.headers.items())


# ---- transport -----------------------------------------------------------------------------------------------------
def parse_sse(text: str) -> list[dict]:
    """JSON payloads of every `data:` event in a server-sent-events body (multi-line data fields are joined)."""
    messages: list[dict] = []
    data_lines: list[str] = []

    def flush() -> None:
        if data_lines:
            payload = "\n".join(data_lines).strip()
            data_lines.clear()
            if payload:
                try:
                    messages.append(json.loads(payload))
                except ValueError:
                    pass

    for line in text.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line.strip() == "":
            flush()
    flush()
    return messages


def parse_response(content_type: str, text: str, request_id: str | None) -> dict:
    """The JSON-RPC message answering `request_id` from a JSON or SSE body (the last message when ids do not match)."""
    if "text/event-stream" in (content_type or "").lower():
        messages = parse_sse(text)
    else:
        stripped = text.strip()
        if not stripped:
            raise McpError(None, "empty response body")
        try:
            parsed = json.loads(stripped)
        except ValueError as exc:
            raise McpError(None, f"non-JSON response: {stripped[:200]}") from exc
        messages = parsed if isinstance(parsed, list) else [parsed]
    replies = [m for m in messages if isinstance(m, dict) and ("result" in m or "error" in m)]
    if not replies:
        raise McpError(None, f"no JSON-RPC response in the body: {text[:200]}")
    for message in replies:
        if request_id is not None and str(message.get("id")) == str(request_id):
            return message
    return replies[-1]


def http_error_text(text: str) -> str:
    """The JSON-RPC error message when an HTTP error body carries one (IAM denials do: 403 + {"error": {"code", "message"}})."""
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return text
    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    if isinstance(parsed, dict) and parsed.get("message"):
        return str(parsed["message"])
    return text


def request(
    gateway_url: str,
    method: str,
    params: dict | None = None,
    session: boto3.Session | None = None,
    region: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    http=None,
    headers: dict | None = None,
    bearer_token: str | None = None,
) -> Any:
    """One JSON-RPC request; returns the `result` member or raises McpError / McpHttpError.

    Signed with SigV4 (an IAM gateway) unless `bearer_token` is given: a CUSTOM_JWT gateway (module 4) wants
    `Authorization: Bearer <jwt>` and no signature.
    """
    request_id = str(uuid.uuid4())
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    body = json.dumps(payload)
    if bearer_token:
        signed = {
            "Content-Type": "application/json",
            "Accept": ACCEPT,
            **(headers or {}),
            "Authorization": f"Bearer {bearer_token}",
        }
    else:
        signed = sign(gateway_url, body, session, region, headers)
    response = (http or requests).post(gateway_url, data=body.encode("utf-8"), headers=signed, timeout=timeout)
    if response.status_code >= 400:
        raise McpHttpError(response.status_code, http_error_text(response.text or ""), gateway_url)
    message = parse_response(response.headers.get("Content-Type", ""), response.text, request_id)
    if "error" in message:
        error = message["error"] or {}
        raise McpError(error.get("code"), error.get("message") or str(error), error.get("data"), method)
    return message.get("result")


# ---- MCP methods ---------------------------------------------------------------------------------------------------
def initialize(gateway_url: str, session: boto3.Session | None = None, region: str | None = None, **kw) -> dict:
    """`initialize`: the gateway's server info and capabilities (the gateway is stateless; no session id is needed)."""
    params = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO}
    return request(gateway_url, "initialize", params, session, region, **kw) or {}


def list_tools(gateway_url: str, session: boto3.Session | None = None, region: str | None = None, **kw) -> list[dict]:
    """`tools/list` (all pages): tool dicts with `name` (`<target>___<tool>`), `description`, `inputSchema`."""
    tools: list[dict] = []
    cursor: str | None = None
    while True:
        params = {"cursor": cursor} if cursor else {}
        result = request(gateway_url, "tools/list", params, session, region, **kw) or {}
        tools.extend(result.get("tools", []))
        cursor = result.get("nextCursor")
        if not cursor:
            return tools


def tool_names(tools: list[dict]) -> list[str]:
    return sorted(t.get("name", "") for t in tools)


def call_tool(
    gateway_url: str,
    name: str,
    arguments: dict | None = None,
    session: boto3.Session | None = None,
    region: str | None = None,
    **kw,
) -> ToolResult:
    """`tools/call` -> ToolResult. Gateway-level denials arrive as McpError; tool-level failures as `is_error=True`."""
    result = request(gateway_url, "tools/call", {"name": name, "arguments": arguments or {}}, session, region, **kw)
    result = result or {}
    return ToolResult(
        name=name,
        content=list(result.get("content") or []),
        is_error=bool(result.get("isError")),
        structured=result.get("structuredContent"),
        raw=result,
    )


def try_list(gateway_url: str, **kw) -> dict:
    """List tools and describe the outcome without raising: {"outcome": "ok" | "denied" | "http_error", "detail": ...}."""
    try:
        return {"outcome": "ok", "detail": tool_names(list_tools(gateway_url, **kw))}
    except McpHttpError as exc:
        return {"outcome": "http_error", "status": exc.status, "detail": exc.text[:300]}
    except McpError as exc:
        return {"outcome": "denied", "code": exc.code, "detail": exc.message[:300], "data": exc.data}


def try_call(gateway_url: str, name: str, arguments: dict | None = None, **kw) -> dict:
    """Call a tool and describe the outcome without raising: {"outcome": "ok" | "tool_error" | "denied" | "http_error",
    "detail": ...}. Used by the module 4 denial tests, where a refusal is the expected result."""
    try:
        result = call_tool(gateway_url, name, arguments, **kw)
    except McpHttpError as exc:
        return {"outcome": "http_error", "status": exc.status, "detail": exc.text[:300]}
    except McpError as exc:
        return {"outcome": "denied", "code": exc.code, "detail": exc.message[:300], "data": exc.data}
    if result.is_error:
        return {"outcome": "tool_error", "detail": result.text[:300]}
    return {"outcome": "ok", "detail": result.json()}
