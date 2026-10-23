"""AgentCore Identity API-key credential providers: where the LiteLLM virtual keys live (build contract section 2.2).

Provider names follow `names.ProjectNames.credential_provider(alias)` = `litellm-<project>-<env>-<model alias>`. ARNs look like
`arn:aws:bedrock-agentcore:<region>:<account>:token-vault/default/apikeycredentialprovider/<name>`; the harness references the
ARN as `liteLlmModelConfig.apiKeyArn` and fetches the key at run time with `bedrock-agentcore:GetResourceApiKey`.
"""

from __future__ import annotations

import json
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError

from . import tags as tag_rules
from .config import default_region

PROJECT_TAG = "workshop-agent-platform"
DEFAULT_TAGS = tag_rules.with_required({"Project": PROJECT_TAG, "ManagedBy": "platform"})


@lru_cache(maxsize=8)
def control_client(region: str | None = None):
    """Cached `bedrock-agentcore-control` client (region rule: `config.default_region`)."""
    return boto3.client("bedrock-agentcore-control", region_name=default_region(region))


def provider_arn(region: str, account_id: str, name: str) -> str:
    """The deterministic ARN of an API-key credential provider."""
    return f"arn:aws:bedrock-agentcore:{region}:{account_id}:token-vault/default/apikeycredentialprovider/{name}"


def _is_conflict(exc: ClientError) -> bool:
    error = exc.response.get("Error", {})
    return error.get("Code") == "ConflictException" or "already exist" in (error.get("Message") or "").lower()


def store_api_key(name: str, api_key: str, tags: dict | None = None, region: str | None = None, client=None) -> str:
    """Create the provider holding `api_key`, or update the key when the provider exists. Returns the provider ARN."""
    client = client or control_client(region)
    try:
        response = client.create_api_key_credential_provider(
            name=name, apiKey=api_key, tags=tag_rules.with_required({**DEFAULT_TAGS, **(tags or {})})
        )
    except ClientError as exc:
        if not _is_conflict(exc):
            raise
        response = client.update_api_key_credential_provider(name=name, apiKey=api_key)
    return response["credentialProviderArn"]


def get_provider_arn(name: str, region: str | None = None, client=None) -> str | None:
    """ARN of the provider with this name, or None when it does not exist."""
    client = client or control_client(region)
    try:
        return client.get_api_key_credential_provider(name=name)["credentialProviderArn"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None
        raise


def list_providers(region: str | None = None, client=None) -> list[dict]:
    """All API-key credential providers as {name, arn}."""
    client = client or control_client(region)
    providers: list[dict] = []
    for page in client.get_paginator("list_api_key_credential_providers").paginate():
        providers.extend(
            {"name": p["name"], "arn": p["credentialProviderArn"]} for p in page.get("credentialProviders", [])
        )
    return providers


def delete_provider(name: str, region: str | None = None, client=None) -> bool:
    """Delete the provider; False when it did not exist."""
    client = client or control_client(region)
    try:
        client.delete_api_key_credential_provider(name=name)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return False
        raise


# ---- OAuth2 credential providers (module 2: on-behalf-of token exchange against the corporate IdP) ---------------------------
# Verified against botocore 1.43.109 (CreateOauth2CredentialProvider) and the dev guide page on-behalf-of-token-exchange.html on
# 2026-10-16: a CustomOauth2 provider carries the IdP discovery URL, the client credentials AgentCore Identity uses to
# authenticate the exchange, and `onBehalfOfTokenExchangeConfig` (RFC 8693 TOKEN_EXCHANGE; actorTokenContent NONE because
# Keycloak's standard exchange identifies the acting client from the client authentication alone and rejects actor tokens).
# The exchanged token's audience and `subject_token_type` are set per use (gateway target, harness tool, GetResourceOauth2Token)
# through `customParameters`; they are not part of the provider.
OBO_CUSTOM_PARAMETERS = {"subject_token_type": "urn:ietf:params:oauth:token-type:access_token"}


def oauth2_provider_arn(region: str, account_id: str, name: str) -> str:
    return f"arn:aws:bedrock-agentcore:{region}:{account_id}:token-vault/default/oauth2credentialprovider/{name}"


def oauth2_provider_config(
    discovery_url: str, client_id: str, client_secret: str, obo: bool = True, auth_method: str = "CLIENT_SECRET_POST"
) -> dict:
    """`oauth2ProviderConfigInput` for a custom (any OIDC) provider, with the on-behalf-of exchange switched on by default."""
    custom: dict = {
        "oauthDiscovery": {"discoveryUrl": discovery_url},
        "clientId": client_id,
        "clientSecret": client_secret,
        "clientAuthenticationMethod": auth_method,
    }
    if obo:
        custom["onBehalfOfTokenExchangeConfig"] = {
            "grantType": "TOKEN_EXCHANGE",
            "tokenExchangeGrantTypeConfig": {"actorTokenContent": "NONE"},
        }
    return {"customOauth2ProviderConfig": custom}


def get_oauth2_provider(name: str, region: str | None = None, client=None) -> dict | None:
    """GetOauth2CredentialProvider as {name, arn, secretArn, callbackUrl, vendor}, or None."""
    client = client or control_client(region)
    try:
        response = client.get_oauth2_credential_provider(name=name)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None
        raise
    return _oauth2_summary(response)


def _oauth2_summary(response: dict) -> dict:
    secret = response.get("clientSecretArn") or {}
    return {
        "name": response["name"],
        "arn": response["credentialProviderArn"],
        "secretArn": secret.get("secretArn") if isinstance(secret, dict) else secret,
        "callbackUrl": response.get("callbackUrl"),
        "vendor": response.get("credentialProviderVendor"),
    }


def ensure_oauth2_provider(
    name: str,
    discovery_url: str,
    client_id: str,
    client_secret: str,
    obo: bool = True,
    auth_method: str = "CLIENT_SECRET_POST",
    tags: dict | None = None,
    region: str | None = None,
    client=None,
) -> dict:
    """Create the custom OAuth2 provider, or update its configuration when it exists (the client secret may have rotated).

    Returns {name, arn, secretArn, callbackUrl, vendor}. The secret ARN is the Secrets Manager secret AgentCore Identity keeps the
    client secret in (`bedrock-agentcore-identity!default/oauth2/<name>-*`); the gateway and harness roles need GetSecretValue on it.
    """
    client = client or control_client(region)
    config = oauth2_provider_config(discovery_url, client_id, client_secret, obo=obo, auth_method=auth_method)
    try:
        response = client.create_oauth2_credential_provider(
            name=name,
            credentialProviderVendor="CustomOauth2",
            oauth2ProviderConfigInput=config,
            tags=tag_rules.with_required({**DEFAULT_TAGS, **(tags or {})}),
        )
    except ClientError as exc:
        if not _is_conflict(exc):
            raise
        response = client.update_oauth2_credential_provider(
            name=name, credentialProviderVendor="CustomOauth2", oauth2ProviderConfigInput=config
        )
    return _oauth2_summary({**response, "name": name})


def list_oauth2_providers(region: str | None = None, client=None) -> list[dict]:
    """All OAuth2 credential providers as {name, arn, vendor}."""
    client = client or control_client(region)
    providers: list[dict] = []
    for page in client.get_paginator("list_oauth2_credential_providers").paginate():
        providers.extend(
            {"name": p["name"], "arn": p["credentialProviderArn"], "vendor": p.get("credentialProviderVendor")}
            for p in page.get("credentialProviders", [])
        )
    return providers


def delete_oauth2_provider(name: str, region: str | None = None, client=None) -> bool:
    """Delete the OAuth2 provider; False when it did not exist."""
    client = client or control_client(region)
    try:
        client.delete_oauth2_credential_provider(name=name)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return False
        raise


# ---- workload identities and tokens (the demo performs the first exchange by hand to show what the harness does) --------------
@lru_cache(maxsize=8)
def data_client(region: str | None = None):
    """Cached `bedrock-agentcore` data-plane client."""
    return boto3.client("bedrock-agentcore", region_name=default_region(region))


def ensure_workload_identity(name: str, region: str | None = None, client=None) -> str:
    """Create (or find) a workload identity owned by the caller; returns its ARN. Service-managed identities (harness, gateway)
    cannot be used from outside, so the demo brings its own."""
    client = client or control_client(region)
    try:
        return client.create_workload_identity(name=name, tags=tag_rules.with_required(DEFAULT_TAGS))[
            "workloadIdentityArn"
        ]
    except ClientError as exc:
        if not _is_conflict(exc):
            raise
        return client.get_workload_identity(name=name)["workloadIdentityArn"]


def delete_workload_identity(name: str, region: str | None = None, client=None) -> bool:
    client = client or control_client(region)
    try:
        client.delete_workload_identity(name=name)
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return False
        raise


def workload_access_token_for_jwt(workload_name: str, user_token: str, region: str | None = None, client=None) -> str:
    """GetWorkloadAccessTokenForJWT: the opaque token that binds the workload to the user and carries the inbound JWT as the
    subject of a later exchange."""
    client = client or data_client(region)
    return client.get_workload_access_token_for_jwt(workloadName=workload_name, userToken=user_token)[
        "workloadAccessToken"
    ]


def obo_token(
    workload_access_token: str,
    provider_name: str,
    audience: str,
    scopes: list[str] | None = None,
    custom_parameters: dict | None = None,
    region: str | None = None,
    client=None,
) -> str:
    """GetResourceOauth2Token with oauth2Flow ON_BEHALF_OF_TOKEN_EXCHANGE: AgentCore Identity sends the inbound JWT as the subject
    token to the provider's IdP and returns the token minted for `audience`."""
    client = client or data_client(region)
    response = client.get_resource_oauth2_token(
        workloadIdentityToken=workload_access_token,
        resourceCredentialProviderName=provider_name,
        scopes=list(scopes or []),
        oauth2Flow="ON_BEHALF_OF_TOKEN_EXCHANGE",
        customParameters={**OBO_CUSTOM_PARAMETERS, "audience": audience, **(custom_parameters or {})},
    )
    if "accessToken" not in response:
        raise RuntimeError(f"no accessToken in the exchange response: {json.dumps(response, default=str)[:300]}")
    return response["accessToken"]
