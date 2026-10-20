"""The model gateway relay (foundation/foundation/lambdas/model_gateway_relay.py): one HTTP request per event, no credentials
of its own, the proxy's answer (4xx/5xx included) returned as {status, headers, body}.

Standard library only, so the test swaps urllib.request.urlopen for a fake and inspects the Request the handler built.
"""

from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

HANDLER = Path(__file__).resolve().parents[1] / "foundation" / "lambdas" / "model_gateway_relay.py"
BASE = "https://ag-1234.ecs.us-east-1.on.aws"


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.setenv("MODEL_GATEWAY_URL", BASE + "/")  # a trailing slash in the SSM value must not double up
    spec = importlib.util.spec_from_file_location("model_gateway_relay", HANDLER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_base_url_keeps_one_scheme(monkeypatch):
    """The Express endpoint attribute arrived with its scheme already in a fresh account (2026-10-20): `https://https://...`
    in MODEL_GATEWAY_URL made every relay call fail; the module normalises the value at import."""
    for value in (BASE, BASE + "/", "https://" + BASE, "https://" + BASE + "/", BASE.removeprefix("https://")):
        monkeypatch.setenv("MODEL_GATEWAY_URL", value)
        spec = importlib.util.spec_from_file_location("model_gateway_relay_scheme", HANDLER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert module.BASE_URL == BASE, value


def message(headers: dict[str, str]) -> Message:
    msg = Message()
    for name, value in headers.items():
        msg[name] = value
    return msg


class FakeResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status, self.headers, self._body = status, message(headers), body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


def install(monkeypatch, relay, response=None, error=None) -> dict:
    seen: dict = {}

    def fake_urlopen(request, timeout):
        seen["request"], seen["timeout"] = request, timeout
        if error is not None:
            raise error
        return response

    monkeypatch.setattr(relay.urllib.request, "urlopen", fake_urlopen)
    return seen


def test_get_with_query_and_headers(relay, monkeypatch) -> None:
    headers = {"Content-Type": "application/json", "X-LiteLLM-Response-Cost": "0.0012"}
    seen = install(monkeypatch, relay, FakeResponse(200, headers, b'{"ok": true}'))
    event = {
        "method": "get",
        "path": "key/info",
        "query": {"key": "sk-abc"},
        "headers": {"Authorization": "Bearer sk-m"},
    }

    out = relay.handler(event, None)

    request = seen["request"]
    assert request.full_url == f"{BASE}/key/info?key=sk-abc"
    assert request.get_method() == "GET" and request.data is None
    assert request.get_header("Authorization") == "Bearer sk-m"  # passed through, never added by the relay
    assert seen["timeout"] == 30.0
    assert out == {
        "status": 200,
        "headers": {"content-type": "application/json", "x-litellm-response-cost": "0.0012"},
        "body": '{"ok": true}',
    }


def test_post_body_is_sent_as_utf8(relay, monkeypatch) -> None:
    seen = install(monkeypatch, relay, FakeResponse(201, {}, b""))
    body = json.dumps({"team_alias": "people-ops", "note": "café"})
    event = {"method": "POST", "path": "/team/new", "headers": {"Content-Type": "application/json"}, "body": body}

    out = relay.handler({**event, "timeout": 5}, None)

    assert seen["request"].get_method() == "POST" and seen["request"].data == body.encode("utf-8")
    assert seen["request"].get_header("Content-type") == "application/json"
    assert seen["timeout"] == 5.0
    assert out == {"status": 201, "headers": {}, "body": ""}


def test_http_errors_are_answers_not_failures(relay, monkeypatch) -> None:
    payload = io.BytesIO(b'{"error": {"message": "Authentication Error"}}')
    error = urllib.error.HTTPError(BASE, 401, "Unauthorized", message({"Content-Type": "application/json"}), payload)
    install(monkeypatch, relay, error=error)

    out = relay.handler({"method": "GET", "path": "/key/info", "headers": {"Authorization": "Bearer wrong"}}, None)

    assert out["status"] == 401
    assert out["headers"] == {"content-type": "application/json"}
    assert json.loads(out["body"])["error"]["message"] == "Authentication Error"


def test_transport_failures_propagate_as_a_function_error(relay, monkeypatch) -> None:
    install(monkeypatch, relay, error=urllib.error.URLError("connection refused"))
    with pytest.raises(urllib.error.URLError):
        relay.handler({"method": "GET", "path": "/health/liveliness"}, None)


def test_only_ever_the_configured_host(relay, monkeypatch) -> None:
    """A path that starts with "//" would otherwise be read as a new authority."""
    seen = install(monkeypatch, relay, FakeResponse(404, {}, b""))
    relay.handler({"method": "GET", "path": "//evil.example/steal"}, None)
    assert seen["request"].full_url == f"{BASE}/evil.example/steal"


def test_timeout_is_capped_below_the_function_timeout(relay, monkeypatch) -> None:
    seen = install(monkeypatch, relay, FakeResponse(200, {}, b""))
    relay.handler({"method": "GET", "path": "/health", "timeout": 600}, None)
    assert seen["timeout"] == relay.MAX_TIMEOUT < 120


def test_defaults_for_a_minimal_event(relay, monkeypatch) -> None:
    seen = install(monkeypatch, relay, FakeResponse(200, {}, b"\xff"))  # undecodable byte: replaced, never raised
    out = relay.handler({}, None)
    assert seen["request"].full_url == f"{BASE}/" and seen["request"].get_method() == "GET"
    assert out["body"] == "�"
