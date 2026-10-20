import base64
import json

import boto3
import pytest
from botocore.stub import Stubber
from moto import mock_aws

from platform_sdk import auth


def make_jwt(claims: dict) -> str:
    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()

    return f"{seg({'alg': 'RS256', 'kid': 'k1'})}.{seg(claims)}.c2lnbmF0dXJl"


CLAIMS = {
    "sub": "11111111-2222-3333-4444-555555555555",
    "cognito:groups": ["agent-developers"],
    "cognito:username": "developer",
    "email": "developer@anycompany.example",
    "iss": "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_TESTPOOL",
    "aud": "testclientid123",
    "token_use": "id",
    "exp": 1790000000,
}
USERS = {
    "admin": {"username": "admin", "password": "Adm1n!pass", "email": "admin@anycompany.example"},
    "developer": {"username": "developer", "password": "Dev3l0per!pass", "email": "developer@anycompany.example"},
    "approver": {"username": "approver", "password": "Appr0ver!pass", "email": "approver@anycompany.example"},
}


def test_decode_claims_without_verification():
    claims = auth.decode_claims(make_jwt(CLAIMS))
    assert claims["email"] == "developer@anycompany.example" and claims["cognito:groups"] == ["agent-developers"]
    with pytest.raises(auth.AuthError):
        auth.decode_claims("not-a-jwt")


@mock_aws
def test_personas_reads_the_users_secret(cfg):
    secrets = boto3.client("secretsmanager", region_name="us-east-1")
    secrets.create_secret(Name="agent-platform/users", SecretString=json.dumps(USERS))
    cfg.params["auth/users-secret-arn"] = (
        "agent-platform/users"  # moto generates its own ARN suffix; the name works as SecretId
    )
    assert auth.personas(cfg) == USERS
    secrets.create_secret(Name="agent-platform/empty")  # no value yet: AWS answers ResourceNotFound for AWSCURRENT
    cfg.params["auth/users-secret-arn"] = "agent-platform/empty"
    with pytest.raises(auth.AuthError, match="empty or missing"):
        auth.personas(cfg)


def cognito_stub(response=None, error=None, expected=None):
    client = boto3.client("cognito-idp", region_name="us-east-1")
    stub = Stubber(client)
    if error:
        stub.add_client_error(
            "initiate_auth",
            service_error_code=error[0],
            service_message=error[1],
            http_status_code=400,
            expected_params=expected,
        )
    else:
        stub.add_response("initiate_auth", response, expected)
    stub.activate()
    return client


@mock_aws
def test_login_as_persona_returns_tokens_with_groups(cfg):
    secrets = boto3.client("secretsmanager", region_name="us-east-1")
    secrets.create_secret(Name="agent-platform/users", SecretString=json.dumps(USERS))
    cfg.params["auth/users-secret-arn"] = "agent-platform/users"
    id_token = make_jwt(CLAIMS)
    client = cognito_stub(
        {
            "AuthenticationResult": {
                "IdToken": id_token,
                "AccessToken": "access",
                "RefreshToken": "refresh",
                "ExpiresIn": 3600,
                "TokenType": "Bearer",
            },
            "ChallengeParameters": {},
        },
        expected={
            "AuthFlow": "USER_PASSWORD_AUTH",
            "ClientId": "testclientid123",
            "AuthParameters": {"USERNAME": "developer", "PASSWORD": "Dev3l0per!pass"},
        },
    )
    tokens = auth.login("developer", cfg=cfg, cognito_client=client)
    assert tokens.id_token == id_token and tokens.access_token == "access" and tokens.refresh_token == "refresh"
    assert tokens.username == "developer" and tokens.groups == ["agent-developers"] and tokens.role == "developer"
    assert tokens.email == "developer@anycompany.example"
    assert tokens.auth_header == {"Authorization": f"Bearer {id_token}"}
    assert tokens.summary()["audience"] == "testclientid123"


def test_login_with_explicit_username_and_password(cfg):
    claims = {**CLAIMS, "cognito:groups": "[platform-admins approvers]", "cognito:username": "admin"}
    client = cognito_stub(
        {
            "AuthenticationResult": {
                "IdToken": make_jwt(claims),
                "AccessToken": "a",
                "ExpiresIn": 3600,
                "TokenType": "Bearer",
            }
        },
        expected={
            "AuthFlow": "USER_PASSWORD_AUTH",
            "ClientId": "testclientid123",
            "AuthParameters": {"USERNAME": "admin@anycompany.example", "PASSWORD": "pw"},
        },
    )
    tokens = auth.login("admin@anycompany.example", "pw", cfg=cfg, cognito_client=client)
    assert tokens.groups == ["platform-admins", "approvers"] and tokens.role == "admin" and tokens.refresh_token is None


@pytest.mark.parametrize(
    "code,message,fragment",
    [
        ("NotAuthorizedException", "Incorrect username or password.", "users secret"),
        ("UserNotFoundException", "User does not exist.", "does not exist"),
        (
            "InvalidParameterException",
            "USER_PASSWORD_AUTH flow not enabled for this client",
            "ALLOW_USER_PASSWORD_AUTH",
        ),
    ],
)
def test_login_errors_are_explained(cfg, code, message, fragment):
    client = cognito_stub(error=(code, message))
    with pytest.raises(auth.AuthError, match=fragment):
        auth.login("developer", "wrong", cfg=cfg, cognito_client=client)


def test_login_challenge_is_reported(cfg):
    client = cognito_stub(
        {
            "ChallengeName": "NEW_PASSWORD_REQUIRED",
            "Session": "session-token-0123456789abcdef",
            "ChallengeParameters": {},
        }
    )
    with pytest.raises(auth.AuthError, match="NEW_PASSWORD_REQUIRED"):
        auth.login("developer", "pw", cfg=cfg, cognito_client=client)


class FakeSecrets:
    def get_secret_value(self, SecretId):
        return {"SecretString": json.dumps(USERS)}


def test_unknown_persona(cfg):
    with pytest.raises(auth.AuthError, match="unknown persona"):
        auth.login("auditor", cfg=cfg, secrets_client=FakeSecrets())


def test_login_rejects_the_old_argument_order_and_loads_config_by_default(cfg, monkeypatch):
    with pytest.raises(TypeError, match="login\\(persona, cfg=cfg\\)"):
        auth.login(cfg, "developer")
    # without cfg the platform configuration is loaded from SSM (contract: auth.login("developer") in the `workshop` command)
    monkeypatch.setattr(auth, "load_config", lambda: cfg)
    with pytest.raises(auth.AuthError, match="unknown persona"):
        auth.login("auditor", secrets_client=FakeSecrets())
