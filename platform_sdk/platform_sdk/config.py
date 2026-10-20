"""Configuration discovery (build contract section 1).

The foundation publishes everything under SSM `/agent-platform/...`. `load()` reads the tree once and caches it.
Tests and Lambda functions can bypass SSM by setting `PLATFORM_CONFIG_JSON` to a JSON object keyed by parameter suffix
(for example {"api/url": "https://..."}).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache

import boto3

PREFIX = "/agent-platform/"


@dataclass(frozen=True)
class PlatformConfig:
    region: str
    account_id: str
    params: dict[str, str] = field(default_factory=dict, repr=False)

    def get(self, key: str, default: str | None = None) -> str | None:
        """Look up a parameter by its suffix (for example "api/url")."""
        return self.params.get(key.strip("/"), default)

    def require(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise KeyError(
                f"SSM parameter {PREFIX}{key} is missing. Deploy the foundation stacks first (npx cdk deploy --all)."
            )
        return value

    # Convenience accessors used across the `workshop` command, backend and UI build
    @property
    def api_url(self) -> str:
        return self.require("api/url").rstrip("/")

    @property
    def ui_url(self) -> str:
        return self.require("ui/url").rstrip("/")

    @property
    def user_pool_id(self) -> str:
        return self.require("auth/user-pool-id")

    @property
    def client_id(self) -> str:
        return self.require("auth/client-id")

    @property
    def auth_domain(self) -> str:
        return self.require("auth/domain").rstrip("/")

    @property
    def users_secret_arn(self) -> str:
        return self.require("auth/users-secret-arn")

    @property
    def table_name(self) -> str:
        return self.require("table/name")

    @property
    def worker_function_name(self) -> str:
        return self.require("backend/worker-function-name")

    @property
    def model_gateway_url(self) -> str:
        return https_url(self.require("model-gateway/url"))

    @property
    def model_gateway_openai_base(self) -> str:
        return self.model_gateway_url + "/v1"

    @property
    def model_gateway_master_key_secret_arn(self) -> str:
        return self.require("model-gateway/master-key-secret-arn")

    # ---- private landing zone (tier 3): the proxy is reachable only from inside the VPC ----------------------------
    @property
    def model_gateway_relay_function_name(self) -> str | None:
        """The relay Lambda inside the VPC that forwards HTTP calls to the private proxy, or None on a public landing zone.

        `litellm.admin_client` picks the transport from this: present -> Lambda Invoke through the relay, absent -> direct HTTP.
        """
        return self.get("model-gateway/relay-function-name") or None

    @property
    def model_gateway_relay_function_arn(self) -> str | None:
        return self.get("model-gateway/relay-function-arn") or None

    @property
    def model_gateway_private(self) -> bool:
        """True when the ModelGateway stack published `model-gateway/private = true` (internal load balancer, no public path)."""
        return _flag(self.get("model-gateway/private"))

    @property
    def network_private(self) -> bool:
        """True when the Network stack published `network/private = true` (no internet gateway, no NAT, endpoints only)."""
        return _flag(self.get("network/private"))

    @property
    def vpc_id(self) -> str | None:
        return self.get("network/vpc-id") or None

    def harness_network(self) -> dict | None:
        """The VPC-mode network for harnesses, `{"subnets": [...], "securityGroups": [...]}`, or None without a private network.

        Read from `network/harness-subnet-ids` (comma list) and `network/harness-security-group-id`; `blueprint.render` turns
        it into the harness `environment` block so the agent's ENIs sit next to the private proxy. None keeps the harness on
        AgentCore's managed (PUBLIC) network, which is what a public landing zone needs.
        """
        subnets = _csv(self.get("network/harness-subnet-ids"))
        security_group = (self.get("network/harness-security-group-id") or "").strip()
        if not subnets or not security_group:
            return None
        return {"subnets": subnets, "securityGroups": [security_group]}

    @property
    def approved_models(self) -> list[dict]:
        return json.loads(self.require("model-gateway/models"))

    @property
    def model_aliases(self) -> list[str]:
        """Approved model aliases (the `alias` of every entry in the models list)."""
        return [m["alias"] for m in self.approved_models]

    def model_entry(self, alias: str) -> dict | None:
        """The approved-models entry for one alias, or None when the alias is not approved."""
        return next((m for m in self.approved_models if m.get("alias") == alias), None)

    @property
    def guardrail_id(self) -> str:
        return self.require("guardrail/id")

    @property
    def guardrail_arn(self) -> str:
        return self.require("guardrail/arn")

    @property
    def guardrail_version(self) -> str:
        return self.require("guardrail/version")

    @property
    def evaluation_role_arn(self) -> str:
        return self.require("roles/evaluation-role-arn")

    @property
    def backend_role_arn(self) -> str:
        return self.require("roles/backend-role-arn")

    @property
    def kb_id(self) -> str:
        return self.require("scenario/kb-id")

    @property
    def kb_arn(self) -> str:
        return self.require("scenario/kb-arn")

    @property
    def docs_bucket(self) -> str:
        return self.require("scenario/docs-bucket")

    @property
    def dataset_bucket(self) -> str:
        return self.require("scenario/dataset-bucket")

    @property
    def hr_tools_lambda_arn(self) -> str:
        return self.require("scenario/tools/hr-lambda-arn")

    @property
    def finance_tools_lambda_arn(self) -> str:
        return self.require("scenario/tools/finance-lambda-arn")

    def scenario_table(self, name: str) -> str:
        return self.require(f"scenario/tables/{name}")


_SCHEMES = re.compile(r"^(?:https?://)+", re.IGNORECASE)


def https_url(value: str) -> str:
    """`https://<host>` with one scheme and no trailing slash. The ECS Express `Endpoint` attribute came back with its scheme
    already in some accounts (2026-10-20), so an SSM value of `https://https://ag-...` must still work everywhere."""
    return "https://" + _SCHEMES.sub("", value.strip()).rstrip("/")


def _flag(value: str | None) -> bool:
    return str(value or "").strip().lower() in ("true", "1", "yes")


def _csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _read_ssm(session: boto3.Session) -> dict[str, str]:
    ssm = session.client("ssm")
    params: dict[str, str] = {}
    paginator = ssm.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=PREFIX, Recursive=True, WithDecryption=True):
        for p in page["Parameters"]:
            params[p["Name"][len(PREFIX) :]] = p["Value"]
    return params


def default_region(region: str | None = None) -> str:
    """The region the SDK uses when none is given: AWS_REGION, then AWS_DEFAULT_REGION, then us-east-1.

    Every client in the SDK (`agentcore`, `identity`, `load`) resolves its region through this one rule, so a caller that
    omits `region=cfg.region` still talks to the region the platform was deployed in.
    """
    return region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


@lru_cache(maxsize=4)
def load(profile: str | None = None, region: str | None = None) -> PlatformConfig:
    """Load the platform configuration. Cached per (profile, region)."""
    region = default_region(region)
    session = boto3.Session(profile_name=profile, region_name=region)
    override = os.environ.get("PLATFORM_CONFIG_JSON")
    if override:
        params = {k.strip("/"): str(v) for k, v in json.loads(override).items()}
        account_id = params.get("account-id") or os.environ.get("PLATFORM_ACCOUNT_ID", "000000000000")
    else:
        params = _read_ssm(session)
        account_id = params.get("account-id") or session.client("sts").get_caller_identity()["Account"]
    return PlatformConfig(region=region, account_id=account_id, params=params)


def reset_cache() -> None:
    load.cache_clear()
