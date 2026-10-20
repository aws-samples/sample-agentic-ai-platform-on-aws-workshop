"""Cached configuration and clients (one per Lambda container). Tests monkeypatch these factories."""

from __future__ import annotations

from functools import lru_cache

import boto3
from platform_sdk.litellm import LiteLLMAdmin, admin_client

from platform_sdk import agentcore, config

LITELLM_TIMEOUT = 15  # seconds; the API Lambda has 29 s, so a hung proxy surfaces as 502 instead of a Lambda timeout


@lru_cache(maxsize=1)
def cfg() -> config.PlatformConfig:
    """Platform configuration from SSM /agent-platform/* (read once per container)."""
    return config.load()


@lru_cache(maxsize=1)
def master_key() -> str:
    """The LiteLLM master key from Secrets Manager (ARN from SSM)."""
    secrets = boto3.client("secretsmanager", region_name=cfg().region)
    return secrets.get_secret_value(SecretId=cfg().model_gateway_master_key_secret_arn)["SecretString"]


@lru_cache(maxsize=1)
def litellm_admin() -> LiteLLMAdmin:
    """The model gateway admin client: through the relay Lambda when the landing zone is private, direct HTTP otherwise.

    The proxy's load balancer is internal (tier 3), so this Lambda, which stays outside the VPC, reaches it only through the relay
    (`platform_sdk.litellm.admin_client` picks the transport from SSM `model-gateway/relay-function-name`). The shared Lambda
    client is reused; the relay call lasts as long as the HTTP call it carries, well inside the API's 29 s."""
    configuration = cfg()
    client = lambda_client() if configuration.model_gateway_relay_function_name else None
    return admin_client(configuration, master_key(), lambda_client=client, timeout=LITELLM_TIMEOUT)


@lru_cache(maxsize=1)
def iam():
    return boto3.client("iam", region_name=cfg().region)


def agentcore_control():
    return agentcore.control_client(cfg().region)


@lru_cache(maxsize=1)
def registry_control():
    return boto3.client("agent-registry-control", region_name=cfg().region)


@lru_cache(maxsize=1)
def registry_data():
    return boto3.client("agent-registry", region_name=cfg().region)


@lru_cache(maxsize=1)
def bedrock():
    return boto3.client("bedrock", region_name=cfg().region)


@lru_cache(maxsize=1)
def bedrock_agent():
    """Knowledge bases (ListKnowledgeBases for the project form's pick list)."""
    return boto3.client("bedrock-agent", region_name=cfg().region)


@lru_cache(maxsize=1)
def lambda_client():
    return boto3.client("lambda", region_name=cfg().region)


@lru_cache(maxsize=1)
def s3():
    """The evaluation dataset bucket (SSM `scenario/dataset-bucket`) is read by the evaluate job."""
    return boto3.client("s3", region_name=cfg().region)


@lru_cache(maxsize=1)
def cloudwatch():
    return boto3.client("cloudwatch", region_name=cfg().region)


@lru_cache(maxsize=1)
def cognito_idp():
    """User-pool sign-in for POST /me/switch (InitiateAuth evaluates no IAM policy; the SPA client allows USER_PASSWORD_AUTH)."""
    return boto3.client("cognito-idp", region_name=cfg().region)


@lru_cache(maxsize=1)
def secrets_manager():
    """The users secret for POST /me/switch (platform_sdk.auth.login reads it on every switch, so passwords stay fresh)."""
    return boto3.client("secretsmanager", region_name=cfg().region)


def reset() -> None:
    """Forget every cached client (tests)."""
    for factory in (
        cfg,
        master_key,
        litellm_admin,
        iam,
        registry_control,
        registry_data,
        bedrock,
        bedrock_agent,
        lambda_client,
        cloudwatch,
        cognito_idp,
        secrets_manager,
    ):
        if hasattr(factory, "cache_clear"):  # a test may have monkeypatched the factory with a plain function
            factory.cache_clear()
    config.reset_cache()
