"""On-behalf-of (modules 2, 4 and 6) helpers: the on-behalf-of harness patches, Keycloak representations and the OAuth2 provider configuration."""

from __future__ import annotations

import base64
import json

from platform_sdk import agentcore, blueprint, identity, keycloak, names


def jwt(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.sig"


def test_decode_claims_reads_the_payload_without_verifying():
    claims = keycloak.decode_claims(jwt({"sub": "abc", "aud": ["hr-agent"], "employee_id": "E1001"}))
    assert claims["sub"] == "abc" and claims["aud"] == ["hr-agent"] and claims["employee_id"] == "E1001"


def test_client_spec_representation_carries_the_exchange_switch_and_mappers():
    spec = keycloak.ClientSpec(
        "hr-agent",
        "agent",
        token_exchange=True,
        service_account=True,
        audiences=["hr-obo-gateway"],
        claims=["employee_id"],
    )
    rep = spec.representation()
    assert rep["publicClient"] is False and rep["attributes"]["standard.token.exchange.enabled"] == "true"
    assert rep["serviceAccountsEnabled"] is True and rep["fullScopeAllowed"] is False
    kinds = {m["protocolMapper"]: m for m in rep["protocolMappers"]}
    assert kinds["oidc-audience-mapper"]["config"]["included.client.audience"] == "hr-obo-gateway"
    assert kinds["oidc-usermodel-attribute-mapper"]["config"]["claim.name"] == "employee_id"
    public = keycloak.ClientSpec("hr-portal", "portal", public=True, direct_access_grants=True, service_account=True)
    rep = public.representation()
    assert (
        rep["publicClient"] is True
        and rep["directAccessGrantsEnabled"] is True
        and rep["serviceAccountsEnabled"] is False
    )
    assert rep["attributes"]["standard.token.exchange.enabled"] == "false"


def test_oauth2_provider_config_is_custom_token_exchange_without_actor_token():
    cfg = identity.oauth2_provider_config(
        "https://kc/realms/anycompany/.well-known/openid-configuration", "hr-agent", "s3cret"
    )
    custom = cfg["customOauth2ProviderConfig"]
    assert custom["oauthDiscovery"]["discoveryUrl"].endswith("/.well-known/openid-configuration")
    assert custom["clientId"] == "hr-agent" and custom["clientSecret"] == "s3cret"
    assert custom["clientAuthenticationMethod"] == "CLIENT_SECRET_POST"
    assert custom["onBehalfOfTokenExchangeConfig"] == {
        "grantType": "TOKEN_EXCHANGE",
        "tokenExchangeGrantTypeConfig": {"actorTokenContent": "NONE"},
    }
    assert (
        "onBehalfOfTokenExchangeConfig"
        not in identity.oauth2_provider_config("https://d", "c", "s", obo=False)["customOauth2ProviderConfig"]
    )


def test_gateway_helpers_build_the_documented_shapes():
    auth = agentcore.jwt_authorizer(
        "https://kc/realms/r/.well-known/openid-configuration", allowed_audience=["hr-obo-gateway"]
    )
    assert auth == {
        "customJWTAuthorizer": {
            "discoveryUrl": "https://kc/realms/r/.well-known/openid-configuration",
            "allowedAudience": ["hr-obo-gateway"],
        }
    }
    cred = agentcore.oauth_credential_configuration(
        "arn:provider", custom_parameters={**identity.OBO_CUSTOM_PARAMETERS, "audience": "hr-tools-api"}
    )
    assert cred["credentialProviderType"] == "OAUTH"
    oauth = cred["credentialProvider"]["oauthCredentialProvider"]
    assert oauth["providerArn"] == "arn:provider" and oauth["grantType"] == "TOKEN_EXCHANGE" and oauth["scopes"] == []
    assert oauth["customParameters"]["subject_token_type"] == "urn:ietf:params:oauth:token-type:access_token"
    assert oauth["customParameters"]["audience"] == "hr-tools-api"


def test_rendered_spec_patches_for_the_obo_harness():
    rendered = {
        "harnessName": "hr_assistant_dev",
        "executionRoleArn": "arn:role",
        "model": {"liteLlmModelConfig": {"modelId": "openai/x", "apiBase": "u", "apiKeyArn": "k"}},
        "systemPrompt": [{"text": "hi"}],
        "tools": [
            {
                "type": "agentcore_gateway",
                "name": "gateway",
                "config": {"agentCoreGateway": {"gatewayArn": "arn:old", "outboundAuth": {"awsIam": {}}}},
            }
        ],
        "allowedTools": ["@gateway"],
        "memory": {"agentCoreMemoryConfiguration": {"arn": "arn:mem"}},
    }
    spec = blueprint.with_jwt_authorizer(rendered, "https://kc/realms/r/.well-known/openid-configuration", ["hr-agent"])
    spec = blueprint.with_oauth_gateway(spec, "arn:obo-gw", "arn:provider", "hr-obo-gateway")
    assert rendered["tools"][0]["config"]["agentCoreGateway"]["gatewayArn"] == "arn:old"  # the input is untouched
    assert spec["authorizerConfiguration"]["customJWTAuthorizer"]["allowedAudience"] == ["hr-agent"]
    tool = spec["tools"][0]["config"]["agentCoreGateway"]
    assert tool["gatewayArn"] == "arn:obo-gw"
    oauth = tool["outboundAuth"]["oauth"]
    assert oauth["providerArn"] == "arn:provider" and oauth["grantType"] == "TOKEN_EXCHANGE"
    assert oauth["customParameters"] == {
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": "hr-obo-gateway",
    }
    assert spec["allowedTools"] == ["@gateway"]
    update = blueprint.for_update(spec)
    assert "optionalValue" in update["authorizerConfiguration"] and "optionalValue" in update["memory"]
    assert "harnessName" not in update


def test_obo_names_follow_the_dev_names():
    n = names.ProjectNames("hr-assistant", "dev")
    assert n.obo_harness == "hr_assistant_dev_obo" and n.obo_gateway == "hr-assistant-dev-gw-obo"
    assert n.obo_gateway.startswith(n.gateway)  # inside the roles' `<project>-<env>-gw*` resource patterns


def test_realm_file_matches_the_generator():
    """foundation/keycloak/realm.json is what Keycloak imports; regenerate it with
    `uv run python -c "from platform_sdk import keycloak; open('foundation/keycloak/realm.json','w').write(keycloak.realm_json())"`."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert (root / keycloak.REALM_FILE).read_text() == keycloak.realm_json()
    realm = json.loads(keycloak.realm_json())
    assert realm["realm"] == "anycompany" and realm["sslRequired"] == "external"
    by_id = {c["clientId"]: c for c in realm["clients"]}
    assert set(by_id) == {"hr-portal", "hr-agent", "hr-obo-gateway", "hr-tools-api"}
    assert by_id["hr-agent"]["secret"] == "${KC_SECRET_HR_AGENT}" and "secret" not in by_id["hr-portal"]
    assert by_id["hr-agent"]["attributes"]["standard.token.exchange.enabled"] == "true"
    profile = json.loads(
        realm["components"]["org.keycloak.userprofile.UserProfileProvider"][0]["config"]["kc.user.profile.config"][0]
    )
    assert {a["name"] for a in profile["attributes"]} >= {
        "username",
        "email",
        "firstName",
        "lastName",
        "employee_id",
        "department",
    }
    assert profile["unmanagedAttributePolicy"] == "ENABLED"
