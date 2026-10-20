import base64
import json
from decimal import Decimal

from backend_testlib import call, make_event
from platform_api import router
from platform_api.auth import ADMIN, ANY, APPROVER, DEVELOPER, PUBLIC

# The authorization matrix of build-contracts section 4 for the step-1 routes
EXPECTED_MATRIX = {
    ("GET", "/health"): PUBLIC,
    ("GET", "/me"): ANY,
    ("POST", "/me/switch"): ANY,
    ("GET", "/settings"): ANY,
    ("PUT", "/settings"): (ADMIN,),
    ("GET", "/domains"): ANY,
    ("POST", "/domains"): (ADMIN,),
    ("GET", "/domains/{domainId}"): ANY,
    ("DELETE", "/domains/{domainId}"): (ADMIN,),
    ("GET", "/projects"): ANY,
    ("POST", "/projects"): (ADMIN,),
    ("GET", "/projects/{projectId}"): ANY,
    ("PATCH", "/projects/{projectId}"): (ADMIN,),
    ("DELETE", "/projects/{projectId}"): (ADMIN,),
    ("GET", "/projects/{projectId}/resources"): ANY,
    ("PUT", "/projects/{projectId}/resources"): (DEVELOPER, ADMIN),
    ("DELETE", "/projects/{projectId}/resources/{env}/{type}/{name}"): (DEVELOPER, ADMIN),
    ("GET", "/projects/{projectId}/keys"): ANY,
    ("POST", "/projects/{projectId}/keys"): (DEVELOPER, ADMIN),
    ("PATCH", "/projects/{projectId}/keys/{env}/{model}"): (ADMIN,),
    ("GET", "/catalogue"): ANY,
    ("GET", "/catalogue/search"): ANY,
    ("GET", "/catalogue/{recordId}"): ANY,
    ("POST", "/catalogue/{recordId}/status"): (ADMIN,),
    ("GET", "/governance"): ANY,
    ("GET", "/governance/model-gateway/credentials"): (ADMIN,),
    ("GET", "/governance/project-options"): (ADMIN,),
    ("POST", "/governance/gateways/{gatewayId}/mode"): (ADMIN,),
    ("GET", "/spend"): ANY,
    # step 2: "dev" routes of the contract allow admin as well (admins see everything); approvals are approver-only
    ("GET", "/projects/{projectId}/blueprints"): ANY,
    ("POST", "/projects/{projectId}/blueprints"): (DEVELOPER, ADMIN),
    ("GET", "/projects/{projectId}/blueprints/{version}"): ANY,
    ("GET", "/projects/{projectId}/blueprints/{version}/render"): ANY,
    ("POST", "/projects/{projectId}/deploy"): (DEVELOPER, ADMIN),
    ("GET", "/projects/{projectId}/releases"): ANY,
    ("POST", "/projects/{projectId}/releases"): (DEVELOPER, ADMIN),
    ("GET", "/releases"): ANY,
    ("GET", "/releases/{releaseId}"): ANY,
    ("POST", "/releases/{releaseId}/run"): (DEVELOPER, ADMIN),
    ("POST", "/releases/{releaseId}/evaluation"): (DEVELOPER, ADMIN),
    ("POST", "/releases/{releaseId}/approve"): (APPROVER,),
    ("POST", "/releases/{releaseId}/reject"): (APPROVER,),
    ("POST", "/releases/{releaseId}/promote"): (APPROVER,),
    ("GET", "/metrics"): ANY,
    ("POST", "/invoke"): ANY,
    ("GET", "/jobs/{jobId}"): ANY,
    ("POST", "/feedback"): ANY,
    ("GET", "/feedback"): ANY,
}


def test_route_table_matches_the_contract_matrix():
    table = {(r.method, r.template): r.roles for r in router.ROUTES}
    assert table == EXPECTED_MATRIX
    approver_only = {key for key, roles in table.items() if roles == (APPROVER,)}
    assert approver_only == {
        ("POST", "/releases/{releaseId}/approve"),
        ("POST", "/releases/{releaseId}/reject"),
        ("POST", "/releases/{releaseId}/promote"),
    }


def test_match_static_and_parameters_with_url_decoding():
    route, params = router.match("GET", "/projects/hr-assistant/resources")
    assert route.handler.__name__ == "list_resources" and params == {"projectId": "hr-assistant"}
    route, params = router.match(
        "DELETE", "/projects/hr-assistant/resources/dev/policy/hr_assistant_dev_allow_hrtools___get_leave_balance"
    )
    assert params == {
        "projectId": "hr-assistant",
        "env": "dev",
        "type": "policy",
        "name": "hr_assistant_dev_allow_hrtools___get_leave_balance",
    }
    _, params = router.match("PATCH", "/projects/hr-assistant/keys/dev/claude%2Dhaiku%2D4%2D5")
    assert params["model"] == "claude-haiku-4-5"
    # static segments win over parameters
    assert router.match("GET", "/catalogue/search")[0].handler.__name__ == "search"
    assert router.match("GET", "/catalogue/M4RiHABnVjG5")[0].handler.__name__ == "get_record"
    assert router.match("GET", "/projects/")[0].handler.__name__ == "list_projects"


def test_unknown_path_is_404_and_wrong_method_is_405():
    status, body = call("GET", "/nothing-here")
    assert status == 404 and body["error"]["code"] == "NOT_FOUND"
    status, body = call("POST", "/me")
    assert status == 405 and body["error"]["code"] == "METHOD_NOT_ALLOWED"
    status, body = call("GET", "/me/switch")
    assert status == 405 and body["error"]["code"] == "METHOD_NOT_ALLOWED"


def test_options_preflight_is_an_empty_204_without_claims():
    response = router.dispatch(make_event("OPTIONS", "/projects", persona=None))
    assert response == {"statusCode": 204}


def test_health_is_public_and_me_needs_a_token():
    status, body = call("GET", "/health", persona=None)
    assert status == 200 and body["status"] == "ok" and body["version"] and body["time"].endswith("Z")
    status, body = call("GET", "/me", persona=None)
    assert status == 401 and body["error"]["code"] == "UNAUTHORIZED"


def test_me_returns_the_principal_for_each_persona():
    for persona, role in (("admin", "admin"), ("developer", "developer"), ("approver", "approver")):
        status, body = call("GET", "/me", persona=persona)
        assert status == 200
        assert body == {
            "username": persona,
            "email": f"{persona}@anycompany.example",
            "groups": [{"admin": "platform-admins", "developer": "agent-developers", "approver": "approvers"}[persona]],
            "role": role,
            "roles": [role],
        }
    status, body = call("GET", "/me", persona="someone", groups=[])
    assert status == 200 and body["role"] is None and body["groups"] == [] and body["roles"] == []
    # a member of several groups keeps every role for authorization; `role` stays the display role (admin > developer > approver)
    status, body = call("GET", "/me", persona="lead", groups=["agent-developers", "approvers"])
    assert status == 200 and body["role"] == "developer" and body["roles"] == ["developer", "approver"]


def test_forbidden_matrix_cases(table):
    status, body = call("PUT", "/settings", body={"defaultLimits": {"maxTokens": 1}}, persona="developer")
    assert status == 403 and body["error"]["code"] == "FORBIDDEN" and "admin" in body["error"]["message"]
    status, body = call("POST", "/projects", body={}, persona="approver")
    assert status == 403
    status, body = call("POST", "/domains", body={}, persona="developer")
    assert status == 403
    status, body = call("PATCH", "/projects/p/keys/dev/m", body={"maxBudget": 1}, persona="developer")
    assert status == 403
    status, _ = call("GET", "/settings", persona="someone", groups=[])  # any signed-in user, even without a group
    assert status == 200
    # developer + approvers: the approver route is open (authorization uses every role, not only the display role)
    status, body = call(
        "POST", "/releases/nope/approve", body={}, persona="lead", groups=["agent-developers", "approvers"]
    )
    assert status == 404, body  # past the 403, the release lookup answers
    status, body = call("POST", "/releases/nope/approve", body={}, persona="dev", groups=["agent-developers"])
    assert status == 403 and "role developer" in body["error"]["message"]


def test_body_parsing_errors_and_base64_bodies(table):
    status, body = call("POST", "/domains", raw_body="{not json")
    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR" and "JSON" in body["error"]["message"]
    status, body = call("POST", "/domains", raw_body="[1, 2]")
    assert status == 400 and "object" in body["error"]["message"]
    event = make_event(
        "POST", "/domains", raw_body=base64.b64encode(json.dumps({"domainId": "ops-x"}).encode()).decode()
    )
    event["isBase64Encoded"] = True
    response = router.dispatch(event)
    assert response["statusCode"] == 201 and json.loads(response["body"])["domainId"] == "ops-x"


def test_respond_serialises_decimals_and_sets_json_header():
    response = router.respond(200, {"a": Decimal("5"), "b": Decimal("0.5"), "c": [Decimal("1")]})
    assert response["headers"] == {"Content-Type": "application/json"}
    assert json.loads(response["body"]) == {"a": 5, "b": 0.5, "c": [1]}
    assert router.respond(204) == {"statusCode": 204, "headers": {"Content-Type": "application/json"}}
