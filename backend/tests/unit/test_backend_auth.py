import pytest
from platform_api import auth, errors


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[platform-admins]", ["platform-admins"]),
        ("[platform-admins approvers]", ["platform-admins", "approvers"]),
        ("[platform-admins, approvers]", ["platform-admins", "approvers"]),
        ("platform-admins,approvers", ["platform-admins", "approvers"]),
        (["agent-developers"], ["agent-developers"]),
        (("approvers",), ["approvers"]),
        ("[]", []),
        ("", []),
        (None, []),
    ],
)
def test_parse_groups_handles_the_http_api_string_quirk(raw, expected):
    assert auth.parse_groups(raw) == expected


def test_role_precedence_and_unknown_groups():
    assert auth.role_for(["approvers", "platform-admins"]) == "admin"
    assert auth.role_for(["approvers", "agent-developers"]) == "developer"
    assert auth.role_for(["approvers"]) == "approver"
    assert auth.role_for(["something-else"]) is None
    assert auth.role_for([]) is None


def test_principal_from_event_and_missing_claims():
    event = {
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {
                        "cognito:username": "developer",
                        "email": "developer@anycompany.example",
                        "cognito:groups": "[agent-developers]",
                    }
                }
            }
        }
    }
    principal = auth.principal_from_event(event)
    assert principal == auth.Principal("developer", "developer@anycompany.example", ("agent-developers",), "developer")
    assert principal.to_dict()["groups"] == ["agent-developers"]
    assert auth.principal_from_event({"requestContext": {}}) is None
    assert auth.principal_from_event({"requestContext": {"authorizer": {"jwt": {"claims": {}}}}}) is None
    no_groups = auth.principal_from_claims({"cognito:username": "x", "email": "x@y"})
    assert no_groups.groups == () and no_groups.role is None


def test_require_enforces_the_matrix():
    admin = auth.Principal("admin", None, ("platform-admins",), "admin")
    nobody = auth.Principal("nobody", None, (), None)
    assert auth.require(None, auth.PUBLIC) is None
    assert auth.require(admin, auth.ANY) is admin
    assert auth.require(nobody, auth.ANY) is nobody
    assert auth.require(admin, (auth.ADMIN,)) is admin
    assert auth.require(admin, (auth.DEVELOPER, auth.ADMIN)) is admin
    with pytest.raises(errors.ApiError) as excinfo:
        auth.require(None, auth.ANY)
    assert excinfo.value.status == 401 and excinfo.value.code == "UNAUTHORIZED"
    with pytest.raises(errors.ApiError) as excinfo:
        auth.require(nobody, (auth.ADMIN,))
    assert excinfo.value.status == 403 and excinfo.value.code == "FORBIDDEN"
    assert "no platform role" in excinfo.value.message
    with pytest.raises(errors.ApiError) as excinfo:
        auth.require(auth.Principal("dev", None, ("agent-developers",), "developer"), (auth.APPROVER,))
    assert "approver" in excinfo.value.message and "developer" in excinfo.value.message
