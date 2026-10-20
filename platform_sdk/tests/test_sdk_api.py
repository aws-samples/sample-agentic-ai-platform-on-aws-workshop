import pytest
from sdk_testlib import FakeResponse, FakeSession

from platform_sdk import api

BASE = "https://api.example.test"


def client(handler=None, token="id-token"):
    return api.PlatformClient(BASE, token, session=FakeSession(handler))


def test_base_url_from_config_and_bearer_header(cfg):
    session = FakeSession(
        lambda call: FakeResponse(200, {"username": "developer", "groups": ["agent-developers"], "role": "developer"})
    )
    c = api.PlatformClient(cfg, "tok", session=session)
    assert c.me()["role"] == "developer"
    call = session.calls[0]
    assert call["url"] == "https://api.example.test/me" and call["method"] == "GET"
    assert call["headers"]["Authorization"] == "Bearer tok" and call["timeout"] == 30


def test_contract_error_shape_is_mapped():
    c = client(lambda call: FakeResponse(403, {"error": {"code": "FORBIDDEN", "message": "platform-admins only"}}))
    with pytest.raises(api.PlatformApiError) as excinfo:
        c.create_domain("people-ops", "People Operations")
    err = excinfo.value
    assert (err.code, err.status, err.message) == ("FORBIDDEN", 403, "platform-admins only")
    assert "FORBIDDEN (403)" in str(err)


def test_non_contract_errors_get_http_codes():
    c = client(lambda call: FakeResponse(401, {"message": "Unauthorized"}))
    with pytest.raises(api.PlatformApiError) as excinfo:
        c.list_projects()
    assert excinfo.value.code == "HTTP_401" and excinfo.value.message == "Unauthorized"
    c = client(lambda call: FakeResponse(502, None, text="<html>bad gateway</html>", reason="Bad Gateway"))
    with pytest.raises(api.PlatformApiError) as excinfo:
        c.health()
    assert excinfo.value.code == "HTTP_502" and "bad gateway" in excinfo.value.message
    c = client(lambda call: FakeResponse(200, None, text="not json"))
    with pytest.raises(api.PlatformApiError) as excinfo:
        c.health()
    assert excinfo.value.code == "BAD_RESPONSE"


def test_routes_methods_paths_and_bodies():
    session = FakeSession(
        lambda call: FakeResponse(
            200, {"ok": True, "jobId": "j1", "sessionId": "s1", "version": 2, "status": "SUCCEEDED", "items": []}
        )
    )
    c = api.PlatformClient(BASE, "tok", session=session)
    c.health()
    c.me()
    c.get_settings()
    c.put_settings(registryId="r1")
    c.list_domains()
    c.create_domain("people-ops", "People Ops", "desc", "owner@x")
    c.get_domain("people-ops")
    c.delete_domain("people-ops")
    c.list_projects("people-ops")
    c.create_project({"projectId": "hr-assistant", "domainId": "people-ops"}, models=["claude-sonnet-5"])
    c.get_project("hr-assistant")
    c.update_project("hr-assistant", maxBudget=10)
    c.delete_project("hr-assistant", force=True)
    c.list_resources("hr-assistant", env="dev")
    c.register_resource(
        "hr-assistant",
        "dev",
        "gateway",
        "hr-assistant-dev-gw",
        "arn:gw",
        id="gw-1",
        url="https://gw/mcp",
        attributes={"mode": "LOG_ONLY"},
    )
    c.delete_resource("hr-assistant", "dev", "gateway", "hr-assistant-dev-gw")
    c.list_blueprints("hr-assistant")
    c.create_blueprint("hr-assistant", {"model": {"ref": "model/x"}}, "prompt", "v1")
    c.get_blueprint("hr-assistant", 1)
    c.render_blueprint("hr-assistant", 1, env="prod")
    c.deploy("hr-assistant", 1)
    c.list_releases("hr-assistant")
    c.create_release("hr-assistant", 1, "first")
    c.get_release("rel-1")
    c.run_release("rel-1")
    c.submit_evaluation("rel-1", {"datasetName": "d", "scenarioCount": 1, "scores": {}, "results": []})
    c.approve_release("rel-1", "ok")
    c.reject_release("rel-1", "no")
    c.promote_release("rel-1")
    c.list_keys("hr-assistant")
    c.create_key("hr-assistant", "dev", "claude-sonnet-5", max_budget=0.01, budget_duration="1d")
    c.create_key(
        "hr-assistant",
        "dev",
        "claude-sonnet-5",
        adopt={"litellmKeyId": "hash", "credentialProviderArn": "arn", "maxBudget": 1, "budgetDuration": "1d"},
    )
    c.update_key("hr-assistant", "dev", "claude-sonnet-5", max_budget=1.0, status="active")
    c.list_catalogue(status="APPROVED")
    c.search_catalogue("leave policy")
    c.get_catalogue_record("rec1")
    c.set_catalogue_status("rec1", "APPROVED", "fine")
    c.governance()
    c.set_gateway_mode("gw-1", "ENFORCE")
    c.metrics("hr-assistant", "dev", hours=6)
    c.spend("hr-assistant", "dev")
    c.invoke("hr-assistant", "dev", "hello", session_id="s1")
    c.get_job("j1")
    c.feedback("hr-assistant", "prod", "s1", "up", "nice")
    c.list_feedback("hr-assistant")

    seen = [(call["method"], call["url"].replace(BASE, ""), call["params"], call["json"]) for call in session.calls]
    expected = [
        ("GET", "/health", None, None),
        ("GET", "/me", None, None),
        ("GET", "/settings", None, None),
        ("PUT", "/settings", None, {"registryId": "r1"}),
        ("GET", "/domains", None, None),
        (
            "POST",
            "/domains",
            None,
            {"domainId": "people-ops", "name": "People Ops", "description": "desc", "owner": "owner@x"},
        ),
        ("GET", "/domains/people-ops", None, None),
        ("DELETE", "/domains/people-ops", None, None),
        ("GET", "/projects", {"domainId": "people-ops"}, None),
        (
            "POST",
            "/projects",
            None,
            {"projectId": "hr-assistant", "domainId": "people-ops", "models": ["claude-sonnet-5"]},
        ),
        ("GET", "/projects/hr-assistant", None, None),
        ("PATCH", "/projects/hr-assistant", None, {"maxBudget": 10}),
        ("DELETE", "/projects/hr-assistant", {"force": "true"}, None),
        ("GET", "/projects/hr-assistant/resources", {"env": "dev"}, None),
        (
            "PUT",
            "/projects/hr-assistant/resources",
            None,
            {
                "env": "dev",
                "type": "gateway",
                "name": "hr-assistant-dev-gw",
                "arn": "arn:gw",
                "id": "gw-1",
                "url": "https://gw/mcp",
                "attributes": {"mode": "LOG_ONLY"},
            },
        ),
        ("DELETE", "/projects/hr-assistant/resources/dev/gateway/hr-assistant-dev-gw", None, None),
        ("GET", "/projects/hr-assistant/blueprints", None, None),
        (
            "POST",
            "/projects/hr-assistant/blueprints",
            None,
            {"spec": {"model": {"ref": "model/x"}}, "systemPrompt": "prompt", "notes": "v1"},
        ),
        ("GET", "/projects/hr-assistant/blueprints/1", None, None),
        ("GET", "/projects/hr-assistant/blueprints/1/render", {"env": "prod"}, None),
        ("POST", "/projects/hr-assistant/deploy", None, {"version": 1, "env": "dev"}),
        ("GET", "/projects/hr-assistant/releases", None, None),
        ("POST", "/projects/hr-assistant/releases", None, {"blueprintVersion": 1, "notes": "first", "autoRun": True}),
        ("GET", "/releases/rel-1", None, None),
        ("POST", "/releases/rel-1/run", None, {}),
        (
            "POST",
            "/releases/rel-1/evaluation",
            None,
            {"datasetName": "d", "scenarioCount": 1, "scores": {}, "results": []},
        ),
        ("POST", "/releases/rel-1/approve", None, {"comment": "ok"}),
        ("POST", "/releases/rel-1/reject", None, {"comment": "no"}),
        ("POST", "/releases/rel-1/promote", None, {}),
        ("GET", "/projects/hr-assistant/keys", None, None),
        (
            "POST",
            "/projects/hr-assistant/keys",
            None,
            {"env": "dev", "model": "claude-sonnet-5", "maxBudget": 0.01, "budgetDuration": "1d"},
        ),
        (
            "POST",
            "/projects/hr-assistant/keys",
            None,
            {
                "env": "dev",
                "model": "claude-sonnet-5",
                "adopt": {
                    "litellmKeyId": "hash",
                    "credentialProviderArn": "arn",
                    "maxBudget": 1,
                    "budgetDuration": "1d",
                },
            },
        ),
        ("PATCH", "/projects/hr-assistant/keys/dev/claude-sonnet-5", None, {"maxBudget": 1.0, "status": "active"}),
        ("GET", "/catalogue", {"status": "APPROVED"}, None),
        ("GET", "/catalogue/search", {"q": "leave policy"}, None),
        ("GET", "/catalogue/rec1", None, None),
        ("POST", "/catalogue/rec1/status", None, {"status": "APPROVED", "reason": "fine"}),
        ("GET", "/governance", None, None),
        ("POST", "/governance/gateways/gw-1/mode", None, {"mode": "ENFORCE"}),
        ("GET", "/metrics", {"projectId": "hr-assistant", "env": "dev", "hours": 6}, None),
        ("GET", "/spend", {"projectId": "hr-assistant", "env": "dev"}, None),
        ("POST", "/invoke", None, {"projectId": "hr-assistant", "env": "dev", "prompt": "hello", "sessionId": "s1"}),
        ("GET", "/jobs/j1", None, None),
        (
            "POST",
            "/feedback",
            None,
            {"projectId": "hr-assistant", "env": "prod", "sessionId": "s1", "rating": "up", "comment": "nice"},
        ),
        ("GET", "/feedback", {"projectId": "hr-assistant"}, None),
    ]
    assert seen == expected


def test_submit_evaluation_accepts_a_run_summary():
    from platform_sdk.evaluation import RunSummary

    session = FakeSession(lambda call: FakeResponse(200, {"status": "PENDING_APPROVAL"}))
    summary = RunSummary("run-1", "hr-assistant.json", 1, {"Correctness": {"mean": 1.0}}, [{"scenarioId": "s1"}], 3.0)
    api.PlatformClient(BASE, "tok", session=session).submit_evaluation("rel-1", summary)
    assert session.calls[0]["json"] == {
        "runId": "run-1",
        "datasetName": "hr-assistant.json",
        "scenarioCount": 1,
        "scores": {"Correctness": {"mean": 1.0}},
        "results": [{"scenarioId": "s1"}],
        "failedScenarios": [],
    }


def test_register_resource_without_arn_omits_the_field():
    """Gateway targets have no ARN of their own (GetGatewayTarget returns gatewayArn + targetId only)."""
    session = FakeSession(lambda call: FakeResponse(200, {"ok": True}))
    api.PlatformClient(BASE, "tok", session=session).register_resource(
        "hr-assistant",
        "dev",
        "gateway_target",
        "hrkb",
        id="T2",
        attributes={"kind": "knowledge_base", "gatewayArn": "arn:gw"},
    )
    assert session.calls[0]["json"] == {
        "env": "dev",
        "type": "gateway_target",
        "name": "hrkb",
        "id": "T2",
        "attributes": {"kind": "knowledge_base", "gatewayArn": "arn:gw"},
    }


def test_wait_for_job_polls_until_terminal(monkeypatch):
    states = iter(["QUEUED", "RUNNING", "SUCCEEDED"])
    session = FakeSession(
        lambda call: FakeResponse(200, {"jobId": "j1", "status": next(states), "output": {"text": "hi"}})
    )
    sleeps = []
    monkeypatch.setattr(api.time, "sleep", lambda s: sleeps.append(s))
    job = api.PlatformClient(BASE, "tok", session=session).wait_for_job("j1", timeout=30, poll=2)
    assert job["status"] == "SUCCEEDED" and job["output"]["text"] == "hi"
    assert sleeps == [2, 2] and len(session.calls) == 3


def test_wait_for_job_failure_and_timeout(monkeypatch):
    failed = FakeResponse(
        200,
        {
            "jobId": "j1",
            "status": "FAILED",
            "error": {"code": "BUDGET_EXCEEDED", "message": "Budget has been exceeded", "maxBudget": 0.01},
        },
    )
    c = api.PlatformClient(BASE, "tok", session=FakeSession(lambda call: failed))
    assert c.wait_for_job("j1")["error"]["code"] == "BUDGET_EXCEEDED"  # returned, not raised, by default
    with pytest.raises(api.PlatformApiError) as excinfo:
        c.wait_for_job("j1", raise_on_failure=True)
    assert excinfo.value.code == "BUDGET_EXCEEDED" and excinfo.value.body["error"]["maxBudget"] == 0.01

    running = FakeResponse(200, {"jobId": "j2", "status": "RUNNING"})
    c = api.PlatformClient(BASE, "tok", session=FakeSession(lambda call: running))
    clock = iter([0, 0, 1, 2, 3, 4, 5, 6])
    monkeypatch.setattr(api.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(api.time, "sleep", lambda s: None)
    with pytest.raises(TimeoutError):
        c.wait_for_job("j2", timeout=3, poll=1)


def test_ask_combines_invoke_and_wait(monkeypatch):
    responses = iter(
        [
            FakeResponse(202, {"jobId": "j9", "sessionId": "sess-9"}),
            FakeResponse(
                200,
                {
                    "jobId": "j9",
                    "status": "SUCCEEDED",
                    "output": {"text": "12 days", "toolsUsed": ["hrtools___get_leave_balance"]},
                },
            ),
        ]
    )
    c = api.PlatformClient(BASE, "tok", session=FakeSession(lambda call: next(responses)))
    job = c.ask("hr-assistant", "dev", "leave balance for E1001?")
    assert job["output"]["toolsUsed"] == ["hrtools___get_leave_balance"] and job["sessionId"] == "sess-9"


def test_no_token_means_no_authorization_header():
    session = FakeSession(lambda call: FakeResponse(200, {"status": "ok"}))
    api.PlatformClient(BASE, session=session).health()
    assert "Authorization" not in session.calls[0]["headers"]


def test_list_routes_accept_bare_lists_and_envelopes():
    c = client(lambda call: FakeResponse(200, [{"domainId": "people-ops"}]))
    assert c.list_domains() == [{"domainId": "people-ops"}]
    c = client(lambda call: FakeResponse(200, {"projects": [{"projectId": "hr-assistant"}]}))
    assert c.list_projects() == [{"projectId": "hr-assistant"}]
    c = client(lambda call: FakeResponse(200, {"items": [{"type": "gateway"}]}))
    assert c.list_resources("hr-assistant", "dev") == [{"type": "gateway"}]
    c = client(lambda call: FakeResponse(200, {"records": []}))
    assert c.list_catalogue("APPROVED") == [] and c.search_catalogue("leave") == []
    c = client(lambda call: FakeResponse(200, {"unexpected": 1}))
    with pytest.raises(api.PlatformApiError) as excinfo:
        c.list_keys("hr-assistant")
    assert excinfo.value.code == "BAD_RESPONSE"
