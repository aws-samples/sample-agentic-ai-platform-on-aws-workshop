"""Module 2 SDK paths that only a re-run exercises: provider update on conflict, the exchange request, gateway and target reconciliation."""

from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError

from platform_sdk import agentcore, identity


class FakeControl:
    """Just enough of the bedrock-agentcore-control client for the on-behalf-of helpers."""

    def __init__(self, gateway: dict | None = None, target: dict | None = None, provider_exists: bool = False):
        self.gateway = gateway
        self.target = target
        self.provider_exists = provider_exists
        self.calls: list[tuple[str, dict]] = []

    def _record(self, operation, kwargs):
        self.calls.append((operation, kwargs))

    def get_paginator(self, operation):
        fake = self

        class Paginator:
            def paginate(self, **kwargs):
                yield getattr(fake, operation)(**kwargs)

        return Paginator()

    # gateways
    def list_gateways(self, **kwargs):
        return {"items": [self.gateway] if self.gateway else []}

    def get_gateway(self, gatewayIdentifier):
        return self.gateway

    def update_gateway(self, **kwargs):
        self._record("update_gateway", kwargs)
        self.gateway = {
            **self.gateway,
            "authorizerType": kwargs["authorizerType"],
            "authorizerConfiguration": kwargs["authorizerConfiguration"],
        }
        return self.gateway

    def create_gateway(self, **kwargs):
        self._record("create_gateway", kwargs)
        self.gateway = {
            **kwargs,
            "gatewayId": "gw-1",
            "status": "READY",
            "gatewayArn": "arn:gw",
            "gatewayUrl": "https://gw",
        }
        return self.gateway

    # targets
    def list_gateway_targets(self, **kwargs):
        return {"items": [self.target] if self.target else []}

    def get_gateway_target(self, gatewayIdentifier, targetId):
        return self.target

    def update_gateway_target(self, **kwargs):
        self._record("update_gateway_target", kwargs)
        self.target = {
            **self.target,
            "status": "READY",
            **{k: kwargs[k] for k in ("targetConfiguration", "credentialProviderConfigurations")},
        }
        return self.target

    def delete_gateway_target(self, **kwargs):
        self._record("delete_gateway_target", kwargs)
        self.target = None

    def create_gateway_target(self, **kwargs):
        self._record("create_gateway_target", kwargs)
        self.target = {**kwargs, "targetId": "t-1", "status": "READY"}
        return self.target

    # OAuth2 providers
    def create_oauth2_credential_provider(self, **kwargs):
        self._record("create_oauth2_credential_provider", kwargs)
        if self.provider_exists:
            raise ClientError(
                {"Error": {"Code": "ConflictException", "Message": "already exists"}}, "CreateOauth2CredentialProvider"
            )
        return {
            "credentialProviderArn": "arn:p",
            "clientSecretArn": {"secretArn": "arn:s"},
            "callbackUrl": "https://cb",
        }

    def update_oauth2_credential_provider(self, **kwargs):
        self._record("update_oauth2_credential_provider", kwargs)
        return {"credentialProviderArn": "arn:p", "clientSecretArn": {"secretArn": "arn:s2"}}


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(agentcore.time, "sleep", lambda *_: None)


DISCOVERY = "https://idp/realms/anycompany/.well-known/openid-configuration"
NEW_DISCOVERY = "https://idp2/realms/anycompany/.well-known/openid-configuration"


def jwt_gateway(discovery: str) -> dict:
    return {
        "gatewayId": "gw-1",
        "name": "hr-assistant-dev-gw-obo",
        "roleArn": "arn:role",
        "protocolType": "MCP",
        "status": "READY",
        "gatewayArn": "arn:gw",
        "gatewayUrl": "https://gw",
        "authorizerType": "CUSTOM_JWT",
        "authorizerConfiguration": {
            "customJWTAuthorizer": {"discoveryUrl": discovery, "allowedAudience": ["hr-obo-gateway"]}
        },
        "description": "obo",
    }


def test_ensure_gateway_updates_a_stale_jwt_authorizer(monkeypatch, no_wait):
    control = FakeControl(gateway=jwt_gateway(DISCOVERY))
    monkeypatch.setattr(agentcore, "control_client", lambda region=None: control)
    wanted = agentcore.jwt_authorizer(NEW_DISCOVERY, allowed_audience=["hr-obo-gateway"])
    gateway = agentcore.ensure_gateway(
        "hr-assistant-dev-gw-obo", "arn:role", authorizer_type="CUSTOM_JWT", authorizer_configuration=wanted
    )
    assert gateway["reconciled"] is True
    ((name, kwargs),) = control.calls
    assert name == "update_gateway"
    assert (
        kwargs["authorizerConfiguration"] == wanted
        and kwargs["roleArn"] == "arn:role"
        and kwargs["description"] == "obo"
    )
    assert gateway["authorizerConfiguration"] == wanted


def test_ensure_gateway_keeps_an_identical_jwt_authorizer(monkeypatch, no_wait):
    control = FakeControl(gateway=jwt_gateway(DISCOVERY))
    monkeypatch.setattr(agentcore, "control_client", lambda region=None: control)
    wanted = agentcore.jwt_authorizer(DISCOVERY, allowed_audience=["hr-obo-gateway"])
    gateway = agentcore.ensure_gateway(
        "hr-assistant-dev-gw-obo", "arn:role", authorizer_type="CUSTOM_JWT", authorizer_configuration=wanted
    )
    assert control.calls == [] and "reconciled" not in gateway


def spec(url: str) -> str:
    return json.dumps({"openapi": "3.0.3", "servers": [{"url": url}], "paths": {}})


def credential(provider: str) -> dict:
    return agentcore.oauth_credential_configuration(provider, custom_parameters={"audience": "hr-tools-api"})


def test_ensure_openapi_target_updates_when_the_api_url_moved(monkeypatch, no_wait):
    stored = {
        "targetId": "t-1",
        "name": "hrapi",
        "status": "READY",
        "description": "d",
        "targetConfiguration": {
            "mcp": {"openApiSchema": {"inlinePayload": json.dumps(json.loads(spec("https://old")), indent=1)}}
        },
        "credentialProviderConfigurations": [credential("arn:p")],
    }
    control = FakeControl(target=stored)
    monkeypatch.setattr(agentcore, "control_client", lambda region=None: control)
    same = agentcore.ensure_openapi_target("gw-1", "hrapi", spec("https://old"), credential("arn:p"))
    assert control.calls == [] and "reconciled" not in same  # re-serialised JSON compares by value
    moved = agentcore.ensure_openapi_target("gw-1", "hrapi", spec("https://new"), credential("arn:p"))
    assert moved["reconciled"] is True
    ((name, kwargs),) = control.calls
    assert name == "update_gateway_target" and kwargs["targetId"] == "t-1"
    assert json.loads(kwargs["targetConfiguration"]["mcp"]["openApiSchema"]["inlinePayload"])["servers"] == [
        {"url": "https://new"}
    ]


def test_ensure_openapi_target_recreates_a_failed_target(monkeypatch, no_wait):
    control = FakeControl(target={"targetId": "t-0", "name": "hrapi", "status": "SYNCHRONIZE_UNSUCCESSFUL"})
    monkeypatch.setattr(agentcore, "control_client", lambda region=None: control)
    target = agentcore.ensure_openapi_target("gw-1", "hrapi", spec("https://api"), credential("arn:p"))
    assert [c[0] for c in control.calls] == ["delete_gateway_target", "create_gateway_target"]
    assert target["targetId"] == "t-1" and target["status"] == "READY"


def test_ensure_oauth2_provider_updates_on_conflict():
    control = FakeControl(provider_exists=True)
    provider = identity.ensure_oauth2_provider("kc-hr-agent", DISCOVERY, "hr-agent", "s3cret", client=control)
    names = [c[0] for c in control.calls]
    assert names == ["create_oauth2_credential_provider", "update_oauth2_credential_provider"]
    update = control.calls[1][1]
    custom = update["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
    assert update["credentialProviderVendor"] == "CustomOauth2" and custom["clientSecret"] == "s3cret"
    assert custom["onBehalfOfTokenExchangeConfig"]["tokenExchangeGrantTypeConfig"] == {"actorTokenContent": "NONE"}
    assert provider == {
        "name": "kc-hr-agent",
        "arn": "arn:p",
        "secretArn": "arn:s2",
        "callbackUrl": None,
        "vendor": None,
    }


class FakeData:
    def __init__(self):
        self.calls = []

    def get_resource_oauth2_token(self, **kwargs):
        self.calls.append(kwargs)
        return {"accessToken": "T1"}


def test_obo_token_sends_the_keycloak_parameters():
    data = FakeData()
    token = identity.obo_token("WAT", "kc-hr-agent", "hr-obo-gateway", client=data)
    assert token == "T1"
    (call,) = data.calls
    assert (
        call["oauth2Flow"] == "ON_BEHALF_OF_TOKEN_EXCHANGE"
        and call["scopes"] == []
        and call["workloadIdentityToken"] == "WAT"
    )
    assert call["customParameters"] == {
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": "hr-obo-gateway",
    }
