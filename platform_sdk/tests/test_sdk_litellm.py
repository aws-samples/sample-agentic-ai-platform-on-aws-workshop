import json

import pytest
import requests
from botocore.exceptions import ClientError
from sdk_testlib import RELAY_FUNCTION, FakeLambdaClient, FakeResponse, FakeSession

from platform_sdk import litellm

BASE = "https://llm.example.test"
TEAM_INFO = {
    "team_id": "hr-assistant",
    "team_info": {
        "team_alias": "hr-assistant",
        "spend": 0.42,
        "max_budget": 5.0,
        "budget_duration": "1d",
        "budget_reset_at": "2026-10-09T00:00:00Z",
        "models": ["claude-sonnet-5-5"],
        "tpm_limit": 200000,
        "rpm_limit": 200,
    },
    "keys": [
        {
            "key_alias": "hr-assistant-dev-claude-sonnet-5-5",
            "token": "hash1",
            "spend": 0.4,
            "max_budget": 1.0,
            "budget_reset_at": "2026-10-09T00:00:00Z",
            "models": ["claude-sonnet-5-5"],
            "blocked": False,
            "metadata": {"env": "dev"},
        },
        {
            "key_alias": "hr-assistant-prod-claude-sonnet-5-5",
            "token": "hash2",
            "spend": 0.02,
            "max_budget": 1.0,
            "models": ["claude-sonnet-5-5"],
            "blocked": True,
        },
    ],
}


def admin(handler):
    return litellm.LiteLLMAdmin(BASE, "sk-master", session=FakeSession(handler))


def test_create_team_is_idempotent_on_already_exists():
    calls = []

    def handler(call):
        calls.append(call)
        if call["url"].endswith("/team/new"):
            return FakeResponse(
                400,
                {
                    "error": {
                        "message": "Team id = hr-assistant already exists. Please use a different team id.",
                        "type": "team_id_exists",
                        "code": "400",
                    }
                },
            )
        if call["url"].endswith("/team/info"):
            assert call["params"] == {"team_id": "hr-assistant"}
            return FakeResponse(200, TEAM_INFO)
        raise AssertionError(call)

    team = admin(handler).create_team(
        "hr-assistant",
        "hr-assistant",
        ["claude-sonnet-5-5"],
        5.0,
        "1d",
        200000,
        200,
        {"domain": "people-ops", "project": "hr-assistant"},
    )
    assert team["team_info"]["max_budget"] == 5.0
    body = calls[0]["json"]
    assert body == {
        "team_id": "hr-assistant",
        "team_alias": "hr-assistant",
        "models": ["claude-sonnet-5-5"],
        "max_budget": 5.0,
        "budget_duration": "1d",
        "tpm_limit": 200000,
        "rpm_limit": 200,
        "metadata": {"domain": "people-ops", "project": "hr-assistant"},
    }
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-master" and calls[0]["timeout"] == 30


def test_parse_error_budget_detection_uses_litellm_wording_only():
    assert litellm.parse_error(
        400, {"error": {"message": "Budget has been exceeded! Key=k Current cost: 2, Max budget: 1"}}
    ).budget_exceeded
    assert not litellm.parse_error(400, {"error": {"message": "budget duration exceeds the maximum"}}).budget_exceeded


def test_create_team_other_errors_propagate():
    def handler(call):
        return FakeResponse(
            401, {"error": {"message": "Authentication Error, invalid master key", "type": "auth_error"}}
        )

    with pytest.raises(litellm.LiteLLMError) as excinfo:
        admin(handler).create_team("t", "t", ["m"])
    assert excinfo.value.status == 401 and excinfo.value.type == "auth_error" and not excinfo.value.budget_exceeded


def test_service_account_key_generate_with_fallback_and_hash():
    calls = []

    def handler(call):
        calls.append(call)
        if call["url"].endswith("/key/service-account/generate"):
            return FakeResponse(404, {"detail": "Not Found"})
        if call["url"].endswith("/key/generate"):
            return FakeResponse(
                200,
                {"key": "sk-abc", "token_id": "hashed-abc", "key_alias": call["json"]["key_alias"], "max_budget": 1.0},
            )
        raise AssertionError(call)

    key = admin(handler).create_service_account_key(
        "hr-assistant", "hr-assistant-dev-claude-sonnet-5-5", ["claude-sonnet-5-5"], 1.0, "1d", {"env": "dev"}
    )
    assert key["key"] == "sk-abc" and key["token"] == "hashed-abc"
    assert [c["url"].split(BASE)[1] for c in calls] == ["/key/service-account/generate", "/key/generate"]
    assert calls[0]["json"] == {
        "team_id": "hr-assistant",
        "key_alias": "hr-assistant-dev-claude-sonnet-5-5",
        "models": ["claude-sonnet-5-5"],
        "max_budget": 1.0,
        "budget_duration": "1d",
        "metadata": {"env": "dev"},
    }


def test_service_account_key_direct_success_keeps_token():
    def handler(call):
        return FakeResponse(200, {"key": "sk-xyz", "token": "hash-xyz"})

    assert admin(handler).create_service_account_key("t", "a", ["m"])["token"] == "hash-xyz"


def test_budget_exceeded_error_mapping_from_chat_completion():
    def handler(call):
        assert call["headers"]["Authorization"] == "Bearer sk-virtual"
        return FakeResponse(
            422,
            {
                "error": {
                    "message": "Budget has been exceeded! Key=hr-assistant-dev Current cost: 0.00309, Max budget: 0.003",
                    "type": "budget_exceeded",
                    "param": None,
                    "code": "400",
                }
            },
        )

    with pytest.raises(litellm.LiteLLMError) as excinfo:
        admin(handler).chat_completion(
            "sk-virtual", "claude-sonnet-5-5", [{"role": "user", "content": "hi"}], max_tokens=5
        )
    err = excinfo.value
    assert err.status == 422 and err.budget_exceeded and "Max budget: 0.003" in err.message


def test_budget_exceeded_inferred_from_message_without_type():
    err = litellm.parse_error(
        400, {"detail": {"error": "ExceededBudget: Budget has been exceeded for team hr-assistant"}}
    )
    assert err.type == "budget_exceeded" and err.budget_exceeded
    plain = litellm.parse_error(500, None, "Internal Server Error")
    assert plain.message == "Internal Server Error" and plain.type is None


def test_key_admin_calls_and_team_spend_summary():
    calls = []

    def handler(call):
        calls.append(call)
        path = call["url"].split(BASE)[1]
        if path == "/key/info":
            return FakeResponse(
                200,
                {
                    "key": "hash1",
                    "info": {
                        "key_alias": "hr-assistant-dev-claude-sonnet-5-5",
                        "spend": 0.4,
                        "max_budget": 1.0,
                        "blocked": False,
                    },
                },
            )
        if path in ("/key/update", "/key/block", "/key/unblock", "/team/update"):
            return FakeResponse(200, {"ok": True, **call["json"]})
        if path == "/team/info":
            return FakeResponse(200, TEAM_INFO)
        if path == "/spend/logs":
            return FakeResponse(
                200, [{"model": "bedrock/converse/us.anthropic.claude-sonnet-5-5", "spend": 0.001, "total_tokens": 900}]
            )
        if path == "/v1/models":
            return FakeResponse(200, {"data": [{"id": "claude-sonnet-5-5"}, {"id": "claude-haiku-4-5"}]})
        raise AssertionError(path)

    a = admin(handler)
    info = a.key_info("hash1")
    assert info["spend"] == 0.4 and info["token"] == "hash1"
    assert a.update_key("hash1", max_budget=2.0)["max_budget"] == 2.0
    assert a.block_key("hash1")["key"] == "hash1" and a.unblock_key("hash1")["key"] == "hash1"
    assert a.update_team("hr-assistant", models=["claude-sonnet-5-5", "nova-2-lite"])["team_id"] == "hr-assistant"
    logs = a.spend_logs(api_key="hash1", start_date="2026-10-01", end_date="2026-10-08")
    assert logs[0]["total_tokens"] == 900
    assert [c for c in calls if c["url"].endswith("/spend/logs")][0]["params"] == {
        "api_key": "hash1",
        "start_date": "2026-10-01",
        "end_date": "2026-10-08",
    }
    assert a.models() == ["claude-sonnet-5-5", "claude-haiku-4-5"]
    summary = a.team_spend_summary("hr-assistant")
    assert (
        summary["spend"] == 0.42 and summary["maxBudget"] == 5.0 and summary["budgetResetAt"] == "2026-10-09T00:00:00Z"
    )
    assert summary["keys"][0]["alias"] == "hr-assistant-dev-claude-sonnet-5-5" and summary["keys"][0]["spend"] == 0.4
    assert summary["keys"][1]["blocked"] is True and summary["keys"][1]["budgetResetAt"] is None


def test_chat_completion_success_and_streaming_fold():
    def handler(call):
        if call.get("timeout") and call["json"].get("stream"):
            resp = FakeResponse(200, None, text="", headers={"Content-Type": "text/event-stream"})
            resp.iter_lines = lambda decode_unicode=True: iter(
                [
                    'data: {"choices": [{"delta": {"content": "Hel"}}]}',
                    "",
                    'data: {"choices": [{"delta": {"content": "lo"}}]}',
                    'data: {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}',
                    "data: [DONE]",
                ]
            )
            return resp
        return FakeResponse(
            200,
            {"choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 8, "completion_tokens": 1}},
            headers={"X-LiteLLM-Response-Cost": "0.000123", "Content-Type": "application/json"},
        )

    a = admin(handler)
    assert a.last_response_headers == {}
    assert (
        a.chat_completion("sk-v", "claude-haiku-4-5", [{"role": "user", "content": "Say OK"}])["choices"][0]["message"][
            "content"
        ]
        == "OK"
    )
    assert a.last_response_headers["x-litellm-response-cost"] == "0.000123"  # lower-cased, what module 5 prints
    streamed = a.chat_completion("sk-v", "claude-haiku-4-5", [{"role": "user", "content": "hi"}], stream=True)
    assert streamed == {"text": "Hello", "usage": {"prompt_tokens": 5, "completion_tokens": 2}, "chunks": 3}
    assert a.last_response_headers == {"content-type": "text/event-stream"}
    assert a.transport == "direct" and not a.via_relay


def test_list_keys_walks_every_page_and_filters_by_team():
    calls = []

    def handler(call):
        calls.append(call)
        page_no = int(call["params"]["page"])
        rows = [
            {"key_alias": f"k{page_no}-{i}", "token": f"h{page_no}{i}", "team_id": "hr-assistant"} for i in range(2)
        ]
        if page_no == 1:
            rows.append("not-a-dict")
        return FakeResponse(200, {"keys": rows, "total_count": 4, "current_page": page_no, "total_pages": 2})

    rows = admin(handler).list_keys(team_id="hr-assistant", page_size=2)
    assert [r["key_alias"] for r in rows] == ["k1-0", "k1-1", "k2-0", "k2-1"]
    assert [c["params"]["page"] for c in calls] == [1, 2]
    assert calls[0]["params"]["team_id"] == "hr-assistant" and calls[0]["params"]["return_full_object"] == "true"
    assert calls[0]["url"].endswith("/key/list")


def test_list_keys_stops_on_a_short_page_without_total_pages():
    def handler(call):
        return FakeResponse(200, {"keys": [{"key_alias": "only", "token": "h"}]})

    assert [r["key_alias"] for r in admin(handler).list_keys()] == ["only"]


# ---- the relay: the private proxy is reached through the relay Lambda inside the VPC ----------------------------------------
SSE_BODY = (
    'data: {"choices": [{"delta": {"content": "Hel"}}]}\n\n'
    'data: {"choices": [{"delta": {"content": "lo"}}]}\n\n'
    'data: {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2}}\n\n'
    "data: [DONE]\n\n"
)


def relay_admin(handler, timeout=30):
    client = FakeLambdaClient(handler)
    session = litellm.LambdaRelaySession(RELAY_FUNCTION, client)
    return litellm.LiteLLMAdmin(BASE, "sk-master", timeout=timeout, session=session), client


def test_relay_session_turns_requests_into_the_documented_event_and_response():
    def handler(event):
        if event["path"] == "/key/info":
            assert event == {
                "method": "GET",
                "path": "/key/info",
                "query": {"key": "hash1"},
                "headers": {"Authorization": "Bearer sk-master", "Accept": "application/json"},
                "body": None,
                "timeout": 30,
            }
            return {
                "status": 200,
                "headers": {"Content-Type": "application/json", "x-litellm-version": "1.104.1"},
                "body": json.dumps({"key": "hash1", "info": {"key_alias": "a", "spend": 0.4, "max_budget": 1.0}}),
            }
        if event["path"] == "/key/update":
            assert event["method"] == "POST" and event["query"] is None
            assert event["headers"]["Content-Type"] == "application/json"  # added for a JSON body
            assert json.loads(event["body"]) == {"key": "hash1", "max_budget": 2.0}
            return {"status": 200, "headers": {}, "body": json.dumps({"key": "hash1", "max_budget": 2.0})}
        raise AssertionError(event)

    a, client = relay_admin(handler)
    info = a.key_info("hash1")
    assert info["spend"] == 0.4 and info["token"] == "hash1"
    assert a.update_key("hash1", max_budget=2.0)["max_budget"] == 2.0
    assert client.invocations == [{"FunctionName": RELAY_FUNCTION, "InvocationType": "RequestResponse"}] * 2
    assert a.via_relay and a.transport == f"relay {RELAY_FUNCTION}"
    assert a.session.calls == 2


def test_relay_session_event_merges_query_strings_and_caps_the_timeout():
    session = litellm.LambdaRelaySession(RELAY_FUNCTION, FakeLambdaClient(lambda e: None))
    event = session.event(
        "get", "/spend/logs?team_id=hr&x=", params={"api_key": "h", "page": 2, "skip": None}, timeout=500
    )
    assert event["method"] == "GET" and event["path"] == "/spend/logs"
    assert event["query"] == {"team_id": "hr", "x": "", "api_key": "h", "page": "2"}
    assert event["timeout"] == litellm.RELAY_MAX_TIMEOUT and event["headers"] is None and event["body"] is None
    raw = session.event("POST", BASE + "/v1/chat/completions", data=b'{"a": 1}', headers={"Content-Type": "text/plain"})
    assert (
        raw["body"] == '{"a": 1}'
        and raw["headers"] == {"Content-Type": "text/plain"}
        and raw["path"] == "/v1/chat/completions"
    )
    assert session.event("GET", BASE)["path"] == "/"


def test_relay_errors_from_the_proxy_are_litellm_errors_with_the_cost_header_kept():
    def handler(event):
        return {
            "status": 422,
            "headers": {"content-type": "application/json"},
            "body": json.dumps(
                {
                    "error": {
                        "message": "Budget has been exceeded! Key=k Current cost: 2, Max budget: 1",
                        "type": "budget_exceeded",
                    }
                }
            ),
        }

    a, _ = relay_admin(handler)
    with pytest.raises(litellm.LiteLLMError) as excinfo:
        a.chat_completion("sk-virtual", "claude-haiku-4-5", [{"role": "user", "content": "hi"}], max_tokens=5)
    assert excinfo.value.status == 422 and excinfo.value.budget_exceeded
    assert a.last_response_headers == {"content-type": "application/json"}


def test_relay_failures_surface_as_relay_error_an_os_error():
    """A crashed relay, an IAM refusal or a malformed payload: one exception type, an OSError like requests' own, so callers
    that fall back when the proxy is unreachable (the governance screen) keep working."""
    assert issubclass(litellm.RelayError, OSError)
    a, _ = relay_admin(lambda e: FakeLambdaClient.function_error("URLError", "<urlopen error timed out>"))
    with pytest.raises(litellm.RelayError, match="URLError: <urlopen error timed out>"):
        a.health()

    def denied(event):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "Invoke")

    a, _ = relay_admin(denied)
    with pytest.raises(litellm.RelayError, match="could not be invoked.*AccessDenied"):
        a.models()
    a, _ = relay_admin(lambda e: {"headers": {}, "body": "no status"})
    with pytest.raises(litellm.RelayError, match="without a status"):
        a.models()


def test_relay_streaming_chat_completion_folds_the_buffered_sse_body():
    """The relay returns the whole body: a streaming completion arrives as one SSE text that iter_lines splits."""

    def handler(event):
        body = json.loads(event["body"])
        assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
        assert event["headers"]["Authorization"] == "Bearer sk-v"
        assert (
            event["timeout"] == litellm.RELAY_MAX_TIMEOUT
        )  # chat allows 120 s; the relay must answer before its own timeout
        return {"status": 200, "headers": {"content-type": "text/event-stream"}, "body": SSE_BODY}

    a, _ = relay_admin(handler)
    streamed = a.chat_completion("sk-v", "claude-haiku-4-5", [{"role": "user", "content": "hi"}], stream=True)
    assert streamed == {"text": "Hello", "usage": {"prompt_tokens": 5, "completion_tokens": 2}, "chunks": 3}
    assert a.last_response_headers == {"content-type": "text/event-stream"}


def test_relay_non_streaming_chat_completion_and_response_helpers():
    def handler(event):
        return {
            "status": 200,
            "headers": {"X-LiteLLM-Response-Cost": "0.0007", "content-type": "application/json"},
            "body": json.dumps({"choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 8}}),
        }

    a, _ = relay_admin(handler)
    payload = a.chat_completion("sk-v", "claude-haiku-4-5", [{"role": "user", "content": "Say OK"}])
    assert payload["choices"][0]["message"]["content"] == "OK"
    assert a.last_response_headers["x-litellm-response-cost"] == "0.0007"
    response = litellm.RelayResponse(200, {}, "a\r\nb\nc")
    assert list(response.iter_lines()) == ["a", "b", "c"] and list(response.iter_lines(decode_unicode=False)) == [
        b"a",
        b"b",
        b"c",
    ]
    assert response.ok and response.content == b"a\r\nb\nc"
    with pytest.raises(ValueError):
        response.json()
    assert not litellm.RelayResponse(500, {}, "").ok


def test_admin_client_factory_picks_the_relay_only_on_a_private_landing_zone(cfg, private_cfg):
    direct = litellm.admin_client(cfg, "sk-master", timeout=15)
    assert isinstance(direct.session, requests.Session) and direct.timeout == 15 and direct.transport == "direct"
    assert direct.base_url == "https://llm.example.test"
    fake = FakeLambdaClient(lambda e: {"status": 200, "headers": {}, "body": "ok"})
    relayed = litellm.admin_client(private_cfg, "sk-master", lambda_client=fake)
    assert (
        relayed.via_relay and relayed.session.function_name == RELAY_FUNCTION and relayed.session.lambda_client is fake
    )
    assert relayed.health() == "ok" and fake.events[0]["path"] == "/health/liveliness"
    built = litellm.admin_client(private_cfg, "sk-master")  # no client given: one is built with retries off
    retries = (
        built.session.lambda_client.meta.config.retries
    )  # botocore normalises max_attempts 0 to one attempt in total
    assert built.via_relay and retries["total_max_attempts"] == 1
    assert built.session.lambda_client.meta.config.read_timeout == litellm.RELAY_READ_TIMEOUT
    assert built.session.lambda_client.meta.region_name == "us-east-1"


def test_read_master_key_reads_the_secret_named_in_ssm(cfg):
    class Secrets:
        def get_secret_value(self, SecretId):
            assert SecretId == cfg.model_gateway_master_key_secret_arn
            return {"SecretString": "sk-master-from-secret"}

    assert litellm.read_master_key(cfg, secrets_client=Secrets()) == "sk-master-from-secret"


def test_local_relay_proxy_serves_the_relay_on_localhost():
    """`workshop export --run`: an OpenAI client on the laptop talks plain HTTP to 127.0.0.1 and the proxy carries it through the
    relay, headers and body unchanged, framing headers rewritten."""
    seen = []

    def handler(event):
        seen.append(event)
        if event["path"] == "/v1/models":
            return {
                "status": 200,
                "headers": {
                    "content-type": "application/json",
                    "transfer-encoding": "chunked",
                    "x-litellm-version": "1",
                },
                "body": json.dumps({"data": [{"id": "claude-haiku-4-5"}]}),
            }
        if event["path"] == "/v1/chat/completions":
            return {"status": 200, "headers": {"content-type": "text/event-stream"}, "body": SSE_BODY}
        if event["path"] == "/boom":
            return FakeLambdaClient.function_error("RuntimeError", "relay crashed")
        return {"status": 404, "headers": {}, "body": "not found"}

    session = litellm.LambdaRelaySession(RELAY_FUNCTION, FakeLambdaClient(handler))
    with litellm.LocalRelayProxy(session) as proxy:
        assert proxy.base_url.startswith("http://127.0.0.1:")
        r = requests.get(proxy.base_url + "/v1/models?x=1", headers={"Authorization": "Bearer sk-v"}, timeout=5)
        assert r.status_code == 200 and r.json() == {"data": [{"id": "claude-haiku-4-5"}]}
        assert r.headers["x-litellm-version"] == "1" and r.headers["Content-Length"] == str(len(r.content))
        assert seen[0]["method"] == "GET" and seen[0]["path"] == "/v1/models" and seen[0]["query"] == {"x": "1"}
        assert seen[0]["headers"]["Authorization"] == "Bearer sk-v" and "Host" not in seen[0]["headers"]
        assert seen[0]["body"] is None
        r = requests.post(
            proxy.base_url + "/v1/chat/completions",
            json={"model": "claude-haiku-4-5", "stream": True},
            headers={"Authorization": "Bearer sk-v"},
            timeout=5,
            stream=True,
        )
        assert r.status_code == 200 and r.headers["content-type"] == "text/event-stream"
        lines = [line for line in r.iter_lines(decode_unicode=True) if line]
        assert lines[-1] == "data: [DONE]" and len(lines) == 4
        assert json.loads(seen[1]["body"]) == {"model": "claude-haiku-4-5", "stream": True}
        assert seen[1]["headers"]["Content-Type"] == "application/json"
        r = requests.get(proxy.base_url + "/boom", timeout=5)
        assert r.status_code == 502 and r.json()["error"]["type"] == "relay_error" and "relay crashed" in r.text
        assert requests.delete(proxy.base_url + "/key/x", timeout=5).status_code == 404
    with pytest.raises(requests.ConnectionError):  # stopped with the context
        requests.get(proxy.base_url + "/v1/models", timeout=2)
