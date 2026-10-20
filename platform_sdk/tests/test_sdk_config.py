import json

import boto3
import pytest
from moto import mock_aws
from sdk_testlib import CONFIG

from platform_sdk import config


def test_override_from_environment(cfg):
    assert cfg.api_url == "https://api.example.test"
    assert cfg.model_gateway_openai_base == "https://llm.example.test/v1"
    assert cfg.client_id == "testclientid123"
    assert cfg.account_id == "123456789012"
    assert cfg.model_aliases == ["claude-sonnet-5-5", "claude-haiku-4-5", "nova-2-lite"]
    assert cfg.model_entry("nova-2-lite")["tier"] == "economy"
    assert cfg.model_entry("gpt-4") is None
    assert cfg.kb_id == "KB123"
    assert cfg.get("missing") is None
    with pytest.raises(KeyError, match="scenario/docs-bucket"):
        _ = cfg.docs_bucket


@mock_aws
def test_load_reads_the_ssm_tree_once_and_caches(monkeypatch):
    ssm = boto3.client("ssm", region_name="us-east-1")
    for key, value in CONFIG.items():
        if key == "account-id":
            continue
        ssm.put_parameter(Name=f"/agent-platform/{key}", Value=value, Type="String")
    config.reset_cache()
    cfg = config.load()
    assert cfg.user_pool_id == "us-east-1_TESTPOOL"
    assert cfg.approved_models[0]["alias"] == "claude-sonnet-5-5"
    assert cfg.account_id == "123456789012"  # moto's STS account
    assert cfg.scenario_table("employees") if "scenario/tables/employees" in CONFIG else True
    # a second load hits the cache (parameter added afterwards is invisible until reset)
    ssm.put_parameter(Name="/agent-platform/scenario/docs-bucket", Value="bucket", Type="String")
    assert config.load().get("scenario/docs-bucket") is None
    config.reset_cache()
    assert config.load().docs_bucket == "bucket"


def test_override_normalises_keys_and_values(monkeypatch):
    monkeypatch.setenv("PLATFORM_CONFIG_JSON", json.dumps({"/api/url/": "https://x.test/", "guardrail/version": 2}))
    config.reset_cache()
    cfg = config.load()
    assert cfg.api_url == "https://x.test"
    assert cfg.guardrail_version == "2"
    assert cfg.region == "us-east-1"


def test_public_landing_zone_has_no_relay_and_no_harness_network(cfg):
    """Without the tier-3 keys the SDK keeps the public behaviour: direct URL, managed harness network."""
    assert cfg.model_gateway_relay_function_name is None and cfg.model_gateway_relay_function_arn is None
    assert cfg.model_gateway_private is False and cfg.network_private is False
    assert cfg.vpc_id is None and cfg.harness_network() is None


def test_private_landing_zone_keys(private_cfg):
    assert private_cfg.model_gateway_relay_function_name == "agent-platform-model-gateway-relay"
    assert private_cfg.model_gateway_relay_function_arn.endswith(":function:agent-platform-model-gateway-relay")
    assert private_cfg.model_gateway_private is True and private_cfg.network_private is True
    assert private_cfg.vpc_id == "vpc-0private"
    assert private_cfg.harness_network() == {
        "subnets": ["subnet-0a1", "subnet-0a2", "subnet-0a4"],
        "securityGroups": ["sg-0harness"],
    }


def test_harness_network_needs_both_subnets_and_security_group(monkeypatch):
    base = {**CONFIG, "network/harness-subnet-ids": " subnet-1 , subnet-2,", "model-gateway/private": "True"}
    monkeypatch.setenv("PLATFORM_CONFIG_JSON", json.dumps(base))
    config.reset_cache()
    cfg = config.load()
    assert cfg.harness_network() is None  # no security group yet
    assert cfg.model_gateway_private is True  # the flag is case-insensitive
    monkeypatch.setenv(
        "PLATFORM_CONFIG_JSON",
        json.dumps({**base, "network/harness-security-group-id": "sg-1", "network/private": "false"}),
    )
    config.reset_cache()
    cfg = config.load()
    assert cfg.harness_network() == {"subnets": ["subnet-1", "subnet-2"], "securityGroups": ["sg-1"]}
    assert cfg.network_private is False
