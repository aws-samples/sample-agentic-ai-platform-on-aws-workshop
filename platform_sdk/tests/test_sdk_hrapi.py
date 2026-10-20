"""hrapi (OpenAPI document for the OBO gateway target) and the HR API Lambda adapter scenario/tools/hr/http_handler.py.

The adapter is loaded from its file with importlib, the way the Lambda runtime would find it (its directory on sys.path so
that ``import handler`` resolves to the HR tools next to it). No test touches AWS: the tools that need DynamoDB are replaced
by a recorder, and the real paths exercised are the ones that need no table (unknown tool, public holidays).
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys

import pytest
from sdk_testlib import ROOT

from platform_sdk import hrapi

HR_DIR = ROOT / "scenario" / "tools" / "hr"
SCHEMA_PATH = HR_DIR / "schema.json"
BASE_URL = "https://abc123.execute-api.us-east-1.amazonaws.com"
TOKEN = "eyJhbGciOiJSUzI1NiJ9.secret-token-body.signature"
CLAIMS = {  # what API Gateway hands over after validating T2 (string values, as the HTTP API does)
    "sub": "6f1c2d3e-0000-4000-8000-00000000e1001",
    "preferred_username": "alex.rivera",
    "employee_id": "E1001",
    "aud": "hr-tools-api",
    "azp": "hr-obo-gateway",
    "iss": "https://kc.example.test/realms/anycompany",
    "exp": "1760600000",
    "iat": "1760599700",
    "typ": "Bearer",
    "scope": "profile email",
}


# ----------------------------------------------------------------------------- hrapi: OpenAPI document
@pytest.fixture(scope="module")
def tools() -> list[dict]:
    return hrapi.load_tools(SCHEMA_PATH)


@pytest.fixture(scope="module")
def spec(tools) -> dict:
    return hrapi.openapi_spec(BASE_URL + "/", tools)


def test_load_tools_reads_the_gateway_schema(tools):
    assert [t["name"] for t in tools] == [
        "get_employee_profile",
        "get_leave_balance",
        "submit_leave_request",
        "get_public_holidays",
    ]
    assert all({"name", "description", "inputSchema"} <= set(t) for t in tools)


def test_load_tools_rejects_a_file_that_is_not_a_list(tmp_path):
    path = tmp_path / "schema.json"
    path.write_text(json.dumps({"name": "x"}))
    with pytest.raises(ValueError, match="expected a JSON list"):
        hrapi.load_tools(path)


def test_spec_has_one_post_path_per_tool_with_the_tool_name_as_operation_id(spec, tools):
    assert spec["openapi"] == "3.0.3"
    assert set(spec["paths"]) == {f"/tools/{t['name']}" for t in tools}
    for tool in tools:
        operation = spec["paths"][f"/tools/{tool['name']}"]["post"]
        assert operation["operationId"] == tool["name"]  # the gateway publishes the tool as <target>___<operationId>
        assert operation["description"] == tool["description"]
        assert operation["summary"] and tool["description"].startswith(operation["summary"])
        assert operation["requestBody"]["required"] is True
        assert operation["responses"] == {
            "200": {"description": "Tool result", "content": {"application/json": {"schema": {"type": "object"}}}}
        }


def test_request_body_schemas_equal_the_input_schemas(spec, tools):
    for tool in tools:
        body = spec["paths"][f"/tools/{tool['name']}"]["post"]["requestBody"]["content"]["application/json"]
        assert body["schema"] == tool["inputSchema"]
    holidays = spec["paths"]["/tools/get_public_holidays"]["post"]["requestBody"]["content"]["application/json"]
    assert holidays["schema"]["properties"]["year"]["type"] == "integer"
    assert "required" not in holidays["schema"]  # both parameters of get_public_holidays are optional


def test_input_schema_is_deep_copied_and_schema_key_dropped():
    tool = {
        "name": "echo",
        "description": "Echo. Returns the input.",
        "inputSchema": {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object", "properties": {}},
    }
    spec = hrapi.openapi_spec(BASE_URL, [tool])
    schema = spec["paths"]["/tools/echo"]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert "$schema" not in schema
    assert "$schema" in tool["inputSchema"]  # the caller's dict is untouched
    schema["properties"]["added"] = {"type": "string"}
    assert "added" not in tool["inputSchema"]["properties"]
    assert spec["paths"]["/tools/echo"]["post"]["summary"] == "Echo."


def test_servers_url_has_no_trailing_slash(tools):
    for url in (BASE_URL, BASE_URL + "/", BASE_URL + "//", " " + BASE_URL + "/ "):
        assert hrapi.openapi_spec(url, tools)["servers"] == [{"url": BASE_URL}]


def test_no_security_scheme_in_the_document(spec):
    # the gateway lists securitySchemes as unsupported: outbound auth is the target's credential configuration, not the document
    assert "security" not in spec and "components" not in spec
    assert spec["info"]["title"] == "AnyCompany HR API"
    assert spec["info"]["version"] == "1.0.0"
    custom = hrapi.openapi_spec(BASE_URL, [], title="HR", version="2.0.0")
    assert custom["info"]["title"] == "HR" and custom["info"]["version"] == "2.0.0" and custom["paths"] == {}


def test_openapi_spec_accepts_the_schema_path_and_rejects_bad_input(tools):
    assert hrapi.openapi_spec(BASE_URL, SCHEMA_PATH) == hrapi.openapi_spec(BASE_URL, tools)
    with pytest.raises(ValueError, match="absolute http"):
        hrapi.openapi_spec("abc123.execute-api.us-east-1.amazonaws.com", tools)
    with pytest.raises(ValueError, match="absolute http"):
        hrapi.openapi_spec("", tools)
    with pytest.raises(ValueError, match="duplicate tool name"):
        hrapi.openapi_spec(BASE_URL, [tools[0], tools[0]])
    with pytest.raises(ValueError, match="non-empty name"):
        hrapi.openapi_spec(BASE_URL, [{"description": "nameless"}])


def test_spec_json_round_trips_and_is_compact(tools):
    text = hrapi.spec_json(BASE_URL + "/", tools)
    spec = hrapi.openapi_spec(BASE_URL, tools)
    assert json.loads(text) == spec
    assert text == json.dumps(spec, separators=(",", ":"))  # compact: the inlinePayload stays small
    assert "\n" not in text and len(text) < len(json.dumps(spec))
    assert json.loads(hrapi.spec_json(BASE_URL, tools, title="HR"))["info"]["title"] == "HR"


# ----------------------------------------------------------------------------- the HTTP API adapter
@pytest.fixture(scope="module")
def adapter():
    """scenario/tools/hr/http_handler.py loaded like the Lambda runtime loads it (its directory on sys.path)."""
    previous = sys.modules.pop("handler", None)  # make sure ``import handler`` resolves to the HR tools, nothing else
    sys.path.insert(0, str(HR_DIR))
    try:
        spec = importlib.util.spec_from_file_location("hr_http_handler", HR_DIR / "http_handler.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(HR_DIR))
        sys.modules.pop("handler", None)
        if previous is not None:
            sys.modules["handler"] = previous
    assert set(module.hr_tools.TOOLS) == {t["name"] for t in hrapi.load_tools(SCHEMA_PATH)}
    return module


@pytest.fixture
def recorder(adapter, monkeypatch):
    """Replace handler.handler with a recorder so no tool touches DynamoDB; returns the list of events it received."""
    calls: list[dict] = []

    def fake_handler(event, context):
        calls.append(event)
        return {"ok": True, "tool": event.get("tool")}

    monkeypatch.setattr(adapter.hr_tools, "handler", fake_handler)
    return calls


def http_event(tool, body=None, claims=CLAIMS, base64_body=False, request_id="req-0001"):
    """An API Gateway HTTP API payload 2.0 event for POST /tools/{tool}."""
    event = {
        "version": "2.0",
        "routeKey": "POST /tools/{tool}",
        "rawPath": f"/tools/{tool}",
        "rawQueryString": "",
        "headers": {"content-type": "application/json", "authorization": f"Bearer {TOKEN}"},
        "requestContext": {
            "accountId": "123456789012",
            "apiId": "abc123",
            "domainName": "abc123.execute-api.us-east-1.amazonaws.com",
            "http": {"method": "POST", "path": f"/tools/{tool}", "protocol": "HTTP/1.1", "sourceIp": "192.0.2.1"},
            "requestId": request_id,
            "routeKey": "POST /tools/{tool}",
            "stage": "$default",
            "timeEpoch": 1760600000000,
        },
        "pathParameters": {"tool": tool},
        "isBase64Encoded": False,
    }
    if claims is not None:
        event["requestContext"]["authorizer"] = {"jwt": {"claims": claims, "scopes": []}}
    if body is not None:
        text = body if isinstance(body, str) else json.dumps(body)
        if base64_body:
            event["body"] = base64.b64encode(text.encode()).decode()
            event["isBase64Encoded"] = True
        else:
            event["body"] = text
    return event


def _log_line(capsys) -> dict:
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 1, lines
    return json.loads(lines[0])


def test_adapter_calls_the_tool_with_the_path_name_and_the_body_arguments(adapter, recorder):
    response = adapter.handler(http_event("get_leave_balance", {"employee_id": "E1001"}), context=None)
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == "application/json"
    assert json.loads(response["body"]) == {"ok": True, "tool": "get_leave_balance"}
    assert recorder == [{"employee_id": "E1001", "tool": "get_leave_balance"}]


def test_adapter_path_name_wins_over_a_tool_key_in_the_body(adapter, recorder):
    adapter.handler(http_event("get_employee_profile", {"employee_id": "E1002", "tool": "submit_leave_request"}), None)
    assert recorder == [{"employee_id": "E1002", "tool": "get_employee_profile"}]


def test_adapter_prints_one_json_evidence_line_without_the_token(adapter, recorder, capsys):
    adapter.handler(http_event("get_leave_balance", {"employee_id": "E1001"}, request_id="req-42"), None)
    out = capsys.readouterr().out
    assert TOKEN not in out and "Bearer" not in out
    lines = [line for line in out.splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0]) == {
        "event": "obo_token_received",
        "tool": "get_leave_balance",
        "sub": CLAIMS["sub"],
        "preferred_username": "alex.rivera",
        "employee_id": "E1001",
        "aud": ["hr-tools-api"],
        "azp": "hr-obo-gateway",
        "iss": "https://kc.example.test/realms/anycompany",
        "exp": "1760600000",
        "requestId": "req-42",
    }


def test_adapter_returns_the_identity_summary_in_headers_not_in_the_body(adapter, recorder):
    response = adapter.handler(http_event("get_leave_balance", {"employee_id": "E1001"}), None)
    assert response["headers"]["X-OBO-Subject"] == CLAIMS["sub"]
    assert response["headers"]["X-OBO-Audience"] == "hr-tools-api"
    assert set(json.loads(response["body"])) == {"ok", "tool"}


def test_adapter_normalises_multi_valued_audiences(adapter, recorder, capsys):
    claims = {**CLAIMS, "aud": "[hr-tools-api account]"}  # how the HTTP API serialises an array claim
    response = adapter.handler(http_event("get_leave_balance", {"employee_id": "E1001"}, claims=claims), None)
    assert _log_line(capsys)["aud"] == ["hr-tools-api", "account"]
    assert response["headers"]["X-OBO-Audience"] == "hr-tools-api,account"
    claims = {**CLAIMS, "aud": ["hr-tools-api", "account"]}  # a real list passes through
    adapter.handler(http_event("get_leave_balance", {"employee_id": "E1001"}, claims=claims), None)
    assert _log_line(capsys)["aud"] == ["hr-tools-api", "account"]


def test_adapter_decodes_a_base64_body(adapter, recorder):
    response = adapter.handler(http_event("get_leave_balance", {"employee_id": "E1003"}, base64_body=True), None)
    assert response["statusCode"] == 200
    assert recorder == [{"employee_id": "E1003", "tool": "get_leave_balance"}]


def test_adapter_treats_an_empty_body_as_no_arguments(adapter, recorder):
    assert adapter.handler(http_event("get_public_holidays"), None)["statusCode"] == 200
    assert adapter.handler(http_event("get_public_holidays", ""), None)["statusCode"] == 200
    assert recorder == [{"tool": "get_public_holidays"}, {"tool": "get_public_holidays"}]


@pytest.mark.parametrize(
    "body, base64_body, message",
    [
        ('{"employee_id": ', False, "not valid JSON"),
        ("[1, 2]", False, "must be a JSON object"),
        ('"E1001"', False, "must be a JSON object"),
        ("%%%not-base64%%%", True, "not valid base64"),
    ],
)
def test_adapter_rejects_a_body_that_is_not_a_json_object(adapter, recorder, body, base64_body, message):
    event = http_event("get_leave_balance", body)
    if base64_body:
        event["isBase64Encoded"] = True  # the body is left as is: not decodable
    response = adapter.handler(event, None)
    assert response["statusCode"] == 400
    assert message in json.loads(response["body"])["error"]
    assert response["headers"]["Content-Type"] == "application/json"
    assert recorder == []


def test_adapter_without_the_authorizer_still_works_and_logs_nulls(adapter, recorder, capsys):
    response = adapter.handler(http_event("get_leave_balance", {"employee_id": "E1001"}, claims=None), None)
    assert response["statusCode"] == 200
    assert "X-OBO-Subject" not in response["headers"] and "X-OBO-Audience" not in response["headers"]
    line = _log_line(capsys)
    assert line["event"] == "obo_token_received" and line["tool"] == "get_leave_balance"
    assert {line[k] for k in ("sub", "preferred_username", "employee_id", "aud", "azp", "iss", "exp")} == {None}
    assert line["requestId"] == "req-0001"


def test_adapter_missing_tool_path_parameter_is_400(adapter, recorder):
    event = http_event("get_leave_balance", {"employee_id": "E1001"})
    event["pathParameters"] = {}
    response = adapter.handler(event, None)
    assert response["statusCode"] == 400
    assert "tool name is missing" in json.loads(response["body"])["error"]
    assert recorder == []


def test_adapter_unknown_tool_is_answered_by_the_tool_handler_with_200(adapter):
    response = adapter.handler(http_event("delete_everything", {}), None)  # the real handler.handler, no AWS call
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["error"].startswith("Unknown tool 'delete_everything'")


def test_adapter_runs_a_real_tool_that_needs_no_table(adapter):
    response = adapter.handler(http_event("get_public_holidays", {"year": 2026, "month": 1}), None)
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["count"] >= 1 and all(h["date"].startswith("2026-01") for h in body["holidays"])


def test_adapter_unexpected_exception_is_500(adapter, monkeypatch, capsys):
    def boom(event, context):
        raise RuntimeError("table on fire")

    monkeypatch.setattr(adapter.hr_tools, "handler", boom)
    response = adapter.handler(http_event("get_leave_balance", {"employee_id": "E1001"}), None)
    assert response["statusCode"] == 500
    assert json.loads(response["body"])["error"] == "get_leave_balance failed: table on fire"
    assert "obo_token_received" in capsys.readouterr().out  # the evidence line is printed before the call
