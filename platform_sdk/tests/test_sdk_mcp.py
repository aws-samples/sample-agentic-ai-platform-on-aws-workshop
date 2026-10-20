"""platform_sdk.mcp: SigV4 signing, JSON and SSE response parsing, errors, pagination (no network, no AWS)."""

from __future__ import annotations

import json

import boto3
import pytest

from platform_sdk import mcp

URL = "https://gw-abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"


class FakeHttpResponse:
    def __init__(self, status_code=200, body="", content_type="application/json"):
        self.status_code = status_code
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.headers = {"Content-Type": content_type}


class FakeHttp:
    """Stand-in for `requests`: records every POST and answers from a handler."""

    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    def post(self, url, data=None, headers=None, timeout=None):
        payload = json.loads(data.decode("utf-8"))
        self.calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        return self.handler(payload)


def session():
    return boto3.Session(
        aws_access_key_id="AKIAEXAMPLE",
        aws_secret_access_key="secret",
        aws_session_token="token123",
        region_name="us-east-1",
    )


def reply(payload, result=None, error=None):
    message = {"jsonrpc": "2.0", "id": payload["id"]}
    if error is not None:
        message["error"] = error
    else:
        message["result"] = result
    return message


def test_sign_produces_sigv4_headers_for_bedrock_agentcore():
    headers = mcp.sign(URL, '{"jsonrpc": "2.0"}', session=session(), region="us-east-1")
    assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIAEXAMPLE/")
    assert "/us-east-1/bedrock-agentcore/aws4_request" in headers["Authorization"]
    assert headers["X-Amz-Security-Token"] == "token123"
    assert "X-Amz-Date" in headers
    assert headers["Content-Type"] == "application/json"
    assert headers["Accept"] == "application/json, text/event-stream"


def test_sign_without_credentials_raises():
    empty = boto3.Session(region_name="us-east-1")
    empty.get_credentials = lambda: None  # type: ignore[method-assign]
    with pytest.raises(mcp.McpHttpError) as excinfo:
        mcp.sign(URL, "{}", session=empty)
    assert excinfo.value.status == 401


def test_list_tools_json_response_and_pagination():
    pages = {
        None: {"tools": [{"name": "hrtools___get_leave_balance"}, {"name": "hrkb___Retrieve"}], "nextCursor": "p2"},
        "p2": {"tools": [{"name": "hrtools___submit_leave_request"}]},
    }

    def handler(payload):
        assert payload["method"] == "tools/list"
        cursor = (payload.get("params") or {}).get("cursor")
        return FakeHttpResponse(200, reply(payload, pages[cursor]))

    http = FakeHttp(handler)
    tools = mcp.list_tools(URL, session=session(), region="us-east-1", http=http)
    assert mcp.tool_names(tools) == ["hrkb___Retrieve", "hrtools___get_leave_balance", "hrtools___submit_leave_request"]
    assert len(http.calls) == 2
    assert http.calls[0]["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256")
    assert http.calls[0]["payload"]["jsonrpc"] == "2.0"
    assert http.calls[1]["payload"]["params"] == {"cursor": "p2"}


def test_call_tool_parses_sse_response_with_matching_id():
    def handler(payload):
        assert payload["method"] == "tools/call"
        assert payload["params"] == {"name": "hrtools___get_leave_balance", "arguments": {"employee_id": "E1001"}}
        other = {"jsonrpc": "2.0", "id": "unrelated", "result": {"content": [{"type": "text", "text": "no"}]}}
        wanted = reply(
            payload,
            {"content": [{"type": "text", "text": json.dumps({"annual_leave_balance": 12})}], "isError": False},
        )
        body = f"event: message\ndata: {json.dumps(other)}\n\nevent: message\ndata: {json.dumps(wanted)}\n\n"
        return FakeHttpResponse(200, body, content_type="text/event-stream; charset=utf-8")

    result = mcp.call_tool(
        URL, "hrtools___get_leave_balance", {"employee_id": "E1001"}, session=session(), http=FakeHttp(handler)
    )
    assert isinstance(result, mcp.ToolResult)
    assert result.json() == {"annual_leave_balance": 12}
    assert result.is_error is False and result.name == "hrtools___get_leave_balance"


def test_parse_sse_joins_multiline_data_and_skips_comments():
    body = ': keep-alive\ndata: {"jsonrpc": "2.0",\ndata: "id": 1, "result": {"ok": true}}\n\n'
    assert mcp.parse_sse(body) == [{"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}]


def test_jsonrpc_error_becomes_mcp_error():
    def handler(payload):
        return FakeHttpResponse(
            200, reply(payload, error={"code": -32602, "message": "Access denied by policy", "data": {"tool": "x"}})
        )

    with pytest.raises(mcp.McpError) as excinfo:
        mcp.call_tool(
            URL, "fintools___get_invoice_status", {"invoice_id": "INV-1"}, session=session(), http=FakeHttp(handler)
        )
    err = excinfo.value
    assert (err.code, err.message, err.data, err.method) == (
        -32602,
        "Access denied by policy",
        {"tool": "x"},
        "tools/call",
    )


def test_http_403_becomes_mcp_http_error_with_access_denied():
    http = FakeHttp(lambda payload: FakeHttpResponse(403, '{"message": "User is not authorized"}'))
    with pytest.raises(mcp.McpHttpError) as excinfo:
        mcp.list_tools(URL, session=session(), http=http)
    assert excinfo.value.status == 403 and excinfo.value.access_denied
    assert "not authorized" in str(excinfo.value)


def test_tool_error_result_and_try_call_outcomes():
    def handler(payload):
        name = payload["params"]["name"]
        if name == "boom":
            return FakeHttpResponse(
                200, reply(payload, {"content": [{"type": "text", "text": "insufficient balance"}], "isError": True})
            )
        if name == "denied":
            return FakeHttpResponse(200, reply(payload, error={"code": -32000, "message": "denied"}))
        if name == "forbidden":
            return FakeHttpResponse(403, "no")
        return FakeHttpResponse(200, reply(payload, {"content": [{"type": "text", "text": '{"ok": 1}'}]}))

    http = FakeHttp(handler)
    kw = {"session": session(), "http": http}
    assert mcp.try_call(URL, "boom", {}, **kw) == {"outcome": "tool_error", "detail": "insufficient balance"}
    assert mcp.try_call(URL, "denied", {}, **kw)["outcome"] == "denied"
    assert mcp.try_call(URL, "forbidden", {}, **kw) == {"outcome": "http_error", "status": 403, "detail": "no"}
    assert mcp.try_call(URL, "fine", {}, **kw) == {"outcome": "ok", "detail": {"ok": 1}}


def test_initialize_sends_protocol_version_and_client_info():
    def handler(payload):
        assert payload["method"] == "initialize"
        assert payload["params"]["protocolVersion"] == mcp.PROTOCOL_VERSION
        assert payload["params"]["clientInfo"]["name"] == "platform_sdk.mcp"
        return FakeHttpResponse(200, reply(payload, {"serverInfo": {"name": "gateway"}, "capabilities": {"tools": {}}}))

    out = mcp.initialize(URL, session=session(), http=FakeHttp(handler))
    assert out["serverInfo"] == {"name": "gateway"}


def test_parse_response_rejects_empty_and_non_json_bodies():
    with pytest.raises(mcp.McpError):
        mcp.parse_response("application/json", "", "1")
    with pytest.raises(mcp.McpError):
        mcp.parse_response("application/json", "<html>", "1")
    with pytest.raises(mcp.McpError):
        mcp.parse_response("text/event-stream", "event: ping\n\n", "1")


def test_try_list_outcomes():
    http = FakeHttp(lambda payload: FakeHttpResponse(200, reply(payload, {"tools": [{"name": "b"}, {"name": "a"}]})))
    assert mcp.try_list(URL, session=session(), http=http) == {"outcome": "ok", "detail": ["a", "b"]}
    http = FakeHttp(lambda payload: FakeHttpResponse(403, "denied by IAM"))
    assert mcp.try_list(URL, session=session(), http=http)["outcome"] == "http_error"
    http = FakeHttp(lambda payload: FakeHttpResponse(200, reply(payload, error={"code": -32002, "message": "no"})))
    assert mcp.try_list(URL, session=session(), http=http)["outcome"] == "denied"


def test_http_error_text_extracts_jsonrpc_message():
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "x",
            "error": {"code": -32002, "message": "User is not authorized to invoke this gateway"},
        }
    )
    assert mcp.http_error_text(body) == "User is not authorized to invoke this gateway"
    assert mcp.http_error_text('{"message": "Forbidden"}') == "Forbidden"
    assert mcp.http_error_text("<html>nope</html>") == "<html>nope</html>"
    http = FakeHttp(lambda payload: FakeHttpResponse(403, body))
    with pytest.raises(mcp.McpHttpError) as excinfo:
        mcp.list_tools(URL, session=session(), http=http)
    assert excinfo.value.text == "User is not authorized to invoke this gateway"
