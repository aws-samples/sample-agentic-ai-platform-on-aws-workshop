"""OpenAPI description of the HR tools API: what the on-behalf-of gateway target needs (module 4).

What. The HR tools are published twice. Modules 1-7 use a Lambda gateway target fed by `scenario/tools/hr/schema.json`, the
list of ``{name, description, inputSchema}`` the gateway accepts as ``toolSchema.inlinePayload``. The IdP stack of module 2 puts the same
tools behind an API Gateway HTTP API (route ``POST /tools/{tool}``, Lambda ``scenario/tools/hr/http_handler.py``) and the
gateway's OpenAPI target needs an OpenAPI document for it. `openapi_spec` derives that document from the same schema.json
so the two publications cannot drift: one path ``/tools/<name>`` per tool with a ``post`` operation whose ``operationId`` is
the tool name and whose JSON request body schema is the tool's ``inputSchema``. `spec_json` is the compact string that goes
into ``targetConfiguration.mcp.openApiSchema.inlinePayload``; `load_tools` reads a schema.json file.

Why an OpenAPI target. Lambda targets receive no caller identity and accept only the gateway service role as outbound
credentials. The on-behalf-of chain needs a target type that can present an exchanged OAuth token, and OpenAPI (and MCP
server) targets are the ones that support ``credentialProviderType: OAUTH`` with ``grantType: TOKEN_EXCHANGE``. Tool names
stay what they were: the gateway publishes an OpenAPI operation as ``<target>___<operationId>``, so ``hrapi___get_leave_balance``
replaces ``hrtools___get_leave_balance`` and the blueprint's tool list needs no other change.

Verified facts (AgentCore Gateway "OpenAPI schema targets", "tool naming" and "outbound authorization" pages, API Gateway
HTTP API docs, botocore 1.43.109; 2026-10-16):
- OpenAPI 3.0 and 3.1 are accepted (Swagger 2.0 is not); ``operationId`` is mandatory for every operation exposed as a tool
  and becomes the tool name; the ``servers`` URL must be the real endpoint (static, fully qualified; private IP ranges are
  blocked); ``application/json`` is the fully supported media type; ``oneOf``/``anyOf``/``allOf`` are unsupported.
- Security schemes inside the document are not used by the gateway: outbound authentication comes from the target's
  credential provider configuration. The ``bearer`` scheme emitted here tells human readers that the API expects a JWT.
- ``openApiSchema`` is an ``ApiSchemaConfiguration`` union: ``inlinePayload`` (a string) or ``s3 {uri, bucketOwnerAccountId}``.
- API Gateway HTTP API JWT authorizers validate ``iss`` against the configured issuer and ``aud`` against the configured
  audiences, which is why the exchanged token's audience must be the HR API's and not the gateway's.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

OPENAPI_VERSION = "3.0.3"
TOOL_PATH_PREFIX = "/tools"
TOOL_RESULT_RESPONSES = {
    "200": {"description": "Tool result", "content": {"application/json": {"schema": {"type": "object"}}}}
}


def load_tools(schema_path: str | Path) -> list[dict]:
    """The gateway tool list ({name, description, inputSchema}) from a schema.json file."""
    tools = json.loads(Path(schema_path).read_text())
    if not isinstance(tools, list):
        raise ValueError(f"{schema_path}: expected a JSON list of tools, got {type(tools).__name__}")
    return tools


def tool_path(name: str) -> str:
    """Route of a tool on the HR API: /tools/<name> (the HTTP API route is POST /tools/{tool})."""
    return f"{TOOL_PATH_PREFIX}/{name}"


def _summary(description: str, name: str) -> str:
    """First sentence of the tool description (the operation summary); the tool name when there is none."""
    text = " ".join(description.split())
    if not text:
        return name
    head, separator, _rest = text.partition(". ")
    return head + ("." if separator else "")


def _operation(tool: dict) -> dict:
    """The ``post`` operation of one tool: operationId = name, request body = inputSchema (a copy without ``$schema``)."""
    name = tool.get("name") if isinstance(tool, dict) else None
    if not isinstance(name, str) or not name:
        raise ValueError(f"every tool needs a non-empty name, got {tool!r}")
    description = str(tool.get("description") or "")
    schema = copy.deepcopy(tool.get("inputSchema") or {"type": "object"})
    schema.pop("$schema", None)
    return {
        "operationId": name,
        "summary": _summary(description, name),
        "description": description or name,
        "requestBody": {"required": True, "content": {"application/json": {"schema": schema}}},
        "responses": copy.deepcopy(TOOL_RESULT_RESPONSES),
    }


def openapi_spec(
    base_url: str, tools: list[dict] | str | Path, title: str = "AnyCompany HR API", version: str = "1.0.0"
) -> dict:
    """OpenAPI 3.0.3 document for the HR API at `base_url`: one ``POST /tools/<name>`` operation per tool.

    `tools` is the schema.json list (or the path of the file). The trailing slash of `base_url` is dropped so that the
    server URL and the paths join cleanly; the URL must be absolute http(s), as the gateway requires.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base.startswith(("https://", "http://")):
        raise ValueError(f"base_url must be an absolute http(s) URL, got {base_url!r}")
    if isinstance(tools, (str, Path)):
        tools = load_tools(tools)
    paths: dict[str, dict] = {}
    for tool in tools:
        operation = _operation(tool)
        path = tool_path(operation["operationId"])
        if path in paths:
            raise ValueError(f"duplicate tool name {operation['operationId']!r}")
        paths[path] = {"post": operation}
    return {
        "openapi": OPENAPI_VERSION,
        "info": {
            "title": title,
            "version": version,
            "description": (
                "HR tools of the AnyCompany sample scenario exposed over HTTP. Each tool is POST /tools/<name> with a "
                "JSON object of arguments; every call carries a bearer JWT that API Gateway validates before the tool runs."
            ),
        },
        "servers": [{"url": base}],
        "paths": paths,
    }


def spec_json(base_url: str, tools: list[dict] | str | Path, **kwargs) -> str:
    """`openapi_spec` as compact JSON: the value for ``targetConfiguration.mcp.openApiSchema.inlinePayload``."""
    return json.dumps(openapi_spec(base_url, tools, **kwargs), separators=(",", ":"))
