import copy
import json

import pytest

from platform_sdk import blueprint, names

ACCOUNT = "123456789012"
GATEWAY_ARN = f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:gateway/hr-assistant-dev-gw-abc123"
MEMORY_ARN = f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:memory/hr_assistant_dev_memory-xyz789"
PROVIDER_ARN = f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:token-vault/default/apikeycredentialprovider/litellm-hr-assistant-dev-claude-sonnet-5"


def project(**overrides):
    base = {
        "projectId": "hr-assistant",
        "domainId": "people-ops",
        "litellm": {"teamId": "hr-assistant", "models": ["claude-sonnet-5", "gpt-6-1-sol"]},
        "roles": {
            "dev": {
                "agentRoleArn": f"arn:aws:iam::{ACCOUNT}:role/agent-platform-proj-hr-assistant-dev-agent",
                "gatewayRoleArn": f"arn:aws:iam::{ACCOUNT}:role/agent-platform-proj-hr-assistant-dev-gateway",
            },
            "prod": {
                "agentRoleArn": f"arn:aws:iam::{ACCOUNT}:role/agent-platform-proj-hr-assistant-prod-agent",
                "gatewayRoleArn": f"arn:aws:iam::{ACCOUNT}:role/agent-platform-proj-hr-assistant-prod-gateway",
            },
        },
    }
    base.update(overrides)
    return base


def resources(env="dev"):
    n = names.ProjectNames("hr-assistant", env)
    return [
        {
            "projectId": "hr-assistant",
            "env": env,
            "type": "gateway",
            "name": n.gateway,
            "arn": GATEWAY_ARN,
            "id": "hr-assistant-dev-gw-abc123",
        },
        {
            "projectId": "hr-assistant",
            "env": env,
            "type": "gateway_target",
            "name": "hrtools",
            "arn": GATEWAY_ARN + "/target/T1",
            "attributes": {"kind": "lambda"},
        },
        {
            "projectId": "hr-assistant",
            "env": env,
            "type": "gateway_target",
            "name": "hrkb",
            "arn": GATEWAY_ARN + "/target/T2",
            "attributes": {"kind": "knowledge_base"},
        },
        {"projectId": "hr-assistant", "env": env, "type": "memory", "name": n.memory, "arn": MEMORY_ARN},
    ]


@pytest.fixture
def hr_blueprint(scenario_dir):
    return blueprint.load(scenario_dir / "blueprint" / "blueprint.yaml")


def test_load_resolves_the_prompt_file_and_exposes_the_metadata(hr_blueprint, scenario_dir):
    bp = hr_blueprint
    assert bp.api_version == "platform/v1" and bp.kind == "AgentBlueprint"
    assert bp.project_id == "hr-assistant" and bp.domain == "people-ops" and bp.version == 1
    assert bp.model_alias == "claude-sonnet-5"
    assert bp.system_prompt == (scenario_dir / "blueprint" / "prompts" / "hr-assistant-v1.md").read_text()
    assert bp.memory_strategies() == ["SUMMARIZATION", "USER_PREFERENCE"]
    assert bp.thresholds == {
        "Correctness": 0.8,
        "GoalSuccessRate": 0.9,
        "TrajectoryInOrderMatch": 0.9,
        "Harmfulness": 0.0,
    }
    assert bp.evaluator_ids() == [
        "Builtin.Correctness",
        "Builtin.GoalSuccessRate",
        "Builtin.TrajectoryInOrderMatch",
        "Builtin.Harmfulness",
    ]
    body = bp.to_api_body(notes="v1")
    assert body["systemPrompt"].startswith("# AnyCompany HR Assistant")
    assert "system_prompt" not in body["spec"] and body["spec"]["systemPrompt"] == {
        "file": "prompts/hr-assistant-v1.md"
    }


def test_render_golden_create_harness_payload(hr_blueprint, cfg):
    spec = blueprint.render(hr_blueprint, "dev", project(), resources(), cfg, PROVIDER_ARN)
    expected = {
        "harnessName": "hr_assistant_dev",
        "executionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/agent-platform-proj-hr-assistant-dev-agent",
        "model": {
            "liteLlmModelConfig": {
                "modelId": "openai/claude-sonnet-5",
                "apiBase": "https://llm.example.test/v1",
                "apiKeyArn": PROVIDER_ARN,
                "maxTokens": 2048,
            }
        },
        "systemPrompt": [{"text": hr_blueprint.system_prompt}],
        "tools": [
            {
                "type": "agentcore_gateway",
                "name": "gateway",
                "config": {"agentCoreGateway": {"gatewayArn": GATEWAY_ARN, "outboundAuth": {"awsIam": {}}}},
            }
        ],
        "allowedTools": ["@gateway"],
        "memory": {"agentCoreMemoryConfiguration": {"arn": MEMORY_ARN}},
        "maxIterations": 30,
        "maxTokens": 4000,
        "timeoutSeconds": 120,
    }
    assert spec == expected
    assert "tags" not in spec
    json.dumps(spec)  # serialisable for the API dry run


def test_render_for_prod_uses_prod_role_and_resources(hr_blueprint, cfg):
    prod_resources = resources("prod")
    prod_resources[0]["arn"] = GATEWAY_ARN.replace("dev", "prod")
    prod_resources[3]["arn"] = MEMORY_ARN.replace("dev", "prod")
    spec = blueprint.render(hr_blueprint, "prod", project(), prod_resources, cfg, PROVIDER_ARN.replace("dev", "prod"))
    assert spec["harnessName"] == "hr_assistant_prod"
    assert spec["executionRoleArn"].endswith("hr-assistant-prod-agent")
    assert spec["tools"][0]["config"]["agentCoreGateway"]["gatewayArn"].endswith("hr-assistant-prod-gw-abc123")
    assert spec["memory"]["agentCoreMemoryConfiguration"]["arn"].endswith("hr_assistant_prod_memory-xyz789")


def test_render_explicit_allow_list_and_response_tokens(hr_blueprint, cfg):
    bp = copy.deepcopy(hr_blueprint)
    bp.spec["tools"] = [{"ref": "tool/hr-core", "allow": ["hrtools___get_leave_balance", "hrkb___Retrieve"]}]
    bp.spec["limits"]["maxResponseTokens"] = 1024
    bp.spec["model"]["temperature"] = 0.2
    spec = blueprint.render(bp, "dev", project(), resources(), cfg, PROVIDER_ARN)
    assert spec["allowedTools"] == ["@gateway/hrtools___get_leave_balance", "@gateway/hrkb___Retrieve"]
    assert spec["model"]["liteLlmModelConfig"]["maxTokens"] == 1024
    assert spec["model"]["liteLlmModelConfig"]["temperature"] == 0.2


def test_render_without_memory_tools_or_limits_uses_defaults(hr_blueprint, cfg):
    bp = copy.deepcopy(hr_blueprint)
    bp.spec.pop("memory")
    bp.spec.pop("tools")
    bp.spec.pop("knowledge")
    bp.spec["limits"] = {}
    spec = blueprint.render(
        bp,
        "dev",
        project(),
        [],
        cfg,
        PROVIDER_ARN,
        defaults={"maxIterations": 20, "maxTokens": 3000, "timeoutSeconds": 90},
    )
    assert spec["memory"] == {"disabled": {}}
    assert "tools" not in spec and "allowedTools" not in spec
    assert (spec["maxIterations"], spec["maxTokens"], spec["timeoutSeconds"]) == (20, 3000, 90)


@pytest.mark.parametrize(
    "drop_type,module_hint,missing",
    [
        ("gateway", "module 4", "gateway"),
        ("memory", "module 6", "memory"),
        ("gateway_target", "module 4", "gateway_target"),
    ],
)
def test_render_unresolved_resource_names_the_module(hr_blueprint, cfg, drop_type, module_hint, missing):
    remaining = [r for r in resources() if r["type"] != drop_type]
    with pytest.raises(blueprint.BlueprintError) as excinfo:
        blueprint.render(hr_blueprint, "dev", project(), remaining, cfg, PROVIDER_ARN)
    assert excinfo.value.code == "BLUEPRINT_UNRESOLVED"
    assert module_hint in str(excinfo.value)
    assert excinfo.value.details["missing"] == missing


def test_render_unresolved_key_and_roles(hr_blueprint, cfg):
    with pytest.raises(blueprint.BlueprintError) as excinfo:
        blueprint.render(hr_blueprint, "dev", project(), resources(), cfg, None)
    assert excinfo.value.code == "BLUEPRINT_UNRESOLVED" and "module 5" in str(excinfo.value)
    assert "litellm-hr-assistant-dev-claude-sonnet-5" in str(excinfo.value)
    with pytest.raises(blueprint.BlueprintError) as excinfo:
        blueprint.render(hr_blueprint, "dev", project(roles={}), resources(), cfg, PROVIDER_ARN)
    assert excinfo.value.code == "BLUEPRINT_UNRESOLVED" and "project creation" in str(excinfo.value)


def test_render_rejects_models_outside_approved_or_project_lists(hr_blueprint, cfg):
    bp = copy.deepcopy(hr_blueprint)
    bp.spec["model"] = {"ref": "model/gpt-4"}
    with pytest.raises(blueprint.BlueprintError, match="not an approved model"):
        blueprint.render(bp, "dev", project(), resources(), cfg, PROVIDER_ARN)
    bp.spec["model"] = {"ref": "model/nova-2-lite"}  # approved but not enabled for the project
    with pytest.raises(blueprint.BlueprintError, match="not enabled for project"):
        blueprint.render(bp, "dev", project(), resources(), cfg, PROVIDER_ARN)


def test_for_update_drops_name_and_tags_and_wraps_memory(hr_blueprint, cfg):
    spec = blueprint.render(hr_blueprint, "dev", project(), resources(), cfg, PROVIDER_ARN)
    spec_with_tags = {**spec, "tags": {"a": "b"}}
    update = blueprint.for_update(spec_with_tags)
    assert "harnessName" not in update and "tags" not in update
    assert update["memory"] == {"optionalValue": {"agentCoreMemoryConfiguration": {"arn": MEMORY_ARN}}}
    assert blueprint.for_update({"memory": {"disabled": {}}})["memory"] == {"optionalValue": {"disabled": {}}}
    assert blueprint.for_update(update)["memory"] == update["memory"]  # idempotent
    assert spec_with_tags["memory"] == {"agentCoreMemoryConfiguration": {"arn": MEMORY_ARN}}  # input untouched


def test_tags_for(hr_blueprint):
    assert blueprint.tags_for(hr_blueprint, "dev") == {
        "Project": "workshop-agent-platform",
        "ManagedBy": "platform",
        "ProjectId": "hr-assistant",
        "Environment": "dev",
        "Domain": "people-ops",
        "BlueprintVersion": "1",
        "auto-delete": "no",
    }
    assert blueprint.tags_for(hr_blueprint, "prod", managed_by="workshop")["ManagedBy"] == "workshop"


def test_from_stored_round_trip(hr_blueprint, cfg):
    body = hr_blueprint.to_api_body()
    rebuilt = blueprint.from_stored(body["spec"], body["systemPrompt"], "hr-assistant", 3, domain="people-ops")
    assert rebuilt.version == 3 and rebuilt.system_prompt == hr_blueprint.system_prompt
    assert blueprint.render(rebuilt, "dev", project(), resources(), cfg, PROVIDER_ARN)["systemPrompt"] == [
        {"text": hr_blueprint.system_prompt}
    ]


def test_from_stored_without_domain_renders(hr_blueprint, cfg):
    """The stored version item has no domain (contract section 3); the backend rebuilds and renders it anyway."""
    body = hr_blueprint.to_api_body()
    rebuilt = blueprint.from_stored(body["spec"], body["systemPrompt"], "hr-assistant", 1)
    assert rebuilt.domain is None
    spec = blueprint.render(rebuilt, "dev", project(), resources(), cfg, PROVIDER_ARN)
    assert spec["harnessName"] == "hr_assistant_dev"
    assert "Domain" not in blueprint.tags_for(rebuilt, "dev")


def test_render_takes_the_key_from_the_registered_credential_provider(hr_blueprint, cfg):
    """Without an explicit provider ARN, render() uses the credential_provider resource for the blueprint's model (module 5)."""
    rows = resources() + [
        {
            "projectId": "hr-assistant",
            "env": "dev",
            "type": "credential_provider",
            "name": "litellm-hr-assistant-dev-claude-sonnet-5",
            "arn": PROVIDER_ARN,
            "attributes": {"model": "claude-sonnet-5"},
        }
    ]
    spec = blueprint.render(hr_blueprint, "dev", project(), rows, cfg)
    assert spec["model"]["liteLlmModelConfig"]["apiKeyArn"] == PROVIDER_ARN
    with pytest.raises(blueprint.BlueprintError, match="module 5"):  # a provider for another model does not count
        rows[-1]["attributes"] = {"model": "gpt-6-1-sol"}
        blueprint.render(hr_blueprint, "dev", project(), rows, cfg)


def test_for_update_sends_empty_tool_and_hook_lists(hr_blueprint, cfg):
    """UpdateHarness retains omitted members, so a blueprint without tools must send tools: [] to drop them."""
    bp = copy.deepcopy(hr_blueprint)
    bp.spec.pop("tools")
    bp.spec.pop("knowledge")
    update = blueprint.for_update(blueprint.render(bp, "dev", project(), resources(), cfg, PROVIDER_ARN))
    assert update["tools"] == [] and update["allowedTools"] == [] and update["hooks"] == []
    full = blueprint.for_update(blueprint.render(hr_blueprint, "dev", project(), resources(), cfg, PROVIDER_ARN))
    assert (
        full["tools"][0]["type"] == "agentcore_gateway" and full["allowedTools"] == ["@gateway"] and full["hooks"] == []
    )


def test_load_from_dict_with_inline_text_and_validation_errors():
    doc = {
        "apiVersion": "platform/v1",
        "kind": "AgentBlueprint",
        "metadata": {"domain": "people-ops", "project": "hr-assistant", "version": 1},
        "spec": {"model": {"ref": "model/gpt-6-1-sol"}, "systemPrompt": {"text": "Be brief."}},
    }
    bp = blueprint.load(doc)
    assert bp.system_prompt == "Be brief." and bp.memory_strategies() == []

    bad = copy.deepcopy(doc)
    bad["apiVersion"] = "v2"
    bad["metadata"]["project"] = "HR Assistant"
    bad["spec"]["model"] = {"ref": "claude"}
    bad["spec"]["memory"] = {"strategies": ["EPISODIC"]}
    bad["spec"]["limits"] = {"maxTokens": 0, "foo": 1}
    bad["spec"]["budget"] = {"perModelCap": -1, "resetPeriod": "daily"}
    bad["spec"]["evaluation"] = {"dataset": "", "thresholds": {"Correctness": 2}}
    with pytest.raises(blueprint.BlueprintError) as excinfo:
        blueprint.load(bad)
    message = str(excinfo.value)
    assert excinfo.value.code == "BLUEPRINT_INVALID"
    for fragment in (
        "apiVersion",
        "metadata.project",
        "spec.model.ref",
        "EPISODIC",
        "spec.limits.maxTokens",
        "spec.limits.foo",
        "perModelCap",
        "resetPeriod",
        "dataset",
        "thresholds.Correctness",
    ):
        assert fragment in message


def test_missing_prompt_file_is_reported(tmp_path):
    (tmp_path / "bp.yaml").write_text(
        "apiVersion: platform/v1\nkind: AgentBlueprint\nmetadata: {domain: d, project: hr-assistant, version: 1}\n"
        "spec:\n  model: {ref: model/gpt-6-1-sol}\n  systemPrompt: {file: prompts/missing.md}\n"
    )
    with pytest.raises(blueprint.BlueprintError, match="prompts/missing.md"):
        blueprint.load(tmp_path / "bp.yaml")


HARNESS_NETWORK = {
    "agentCoreRuntimeEnvironment": {
        "networkConfiguration": {
            "networkMode": "VPC",
            "networkModeConfig": {
                "subnets": ["subnet-0a1", "subnet-0a2", "subnet-0a4"],
                "securityGroups": ["sg-0harness"],
            },
        }
    }
}


def test_render_without_a_private_network_has_no_environment_block(hr_blueprint, cfg):
    """A public landing zone publishes no network keys: the harness stays on AgentCore's managed network."""
    spec = blueprint.render(hr_blueprint, "dev", project(), resources(), cfg, PROVIDER_ARN)
    assert "environment" not in spec
    assert "environment" not in blueprint.for_update(spec)


def test_render_with_the_private_network_puts_the_harness_in_the_vpc(hr_blueprint, private_cfg):
    """Private landing zone: the proxy's load balancer is internal, so the harness runs in VPC mode in the harness subnets with the
    harness security group; requireServiceS3Endpoint is not sent because CreateHarness rejects it."""
    spec = blueprint.render(hr_blueprint, "dev", project(), resources(), private_cfg, PROVIDER_ARN)
    assert spec["environment"] == HARNESS_NETWORK
    assert spec["model"]["liteLlmModelConfig"]["apiBase"] == "https://llm.example.test/v1"  # the private URL, unchanged
    json.dumps(spec)
    update = blueprint.for_update({**spec, "tags": {"a": "b"}})
    assert update["environment"] == HARNESS_NETWORK  # an update keeps the harness in the VPC
    assert "harnessName" not in update and "tags" not in update
    assert spec["environment"] is not update["environment"] or update["environment"] == HARNESS_NETWORK


def test_render_tolerates_a_config_without_harness_network(hr_blueprint):
    """Duck-typed configs (older callers, scripts with a stub) render without the environment block."""

    class Minimal:
        model_gateway_url = "https://llm.example.test"

        def get(self, key, default=None):
            return default

    spec = blueprint.render(hr_blueprint, "dev", project(), resources(), Minimal(), PROVIDER_ARN)
    assert "environment" not in spec
