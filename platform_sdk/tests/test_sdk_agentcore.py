import json

import boto3
import pytest
from botocore.stub import Stubber

from platform_sdk import agentcore

ACCOUNT = "123456789012"
GW_ARN = f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:gateway/hr-assistant-dev-gw-abc123"
HARNESS = {
    "harnessId": "hr_assistant_dev-AbC123",
    "harnessName": "hr_assistant_dev",
    "arn": f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:harness/hr_assistant_dev-AbC123",
    "status": "READY",
    "harnessVersion": "2",
    "executionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/agent-platform-proj-hr-assistant-dev-agent",
    "createdAt": 1,
    "updatedAt": 2,
    "model": {"liteLlmModelConfig": {"modelId": "openai/claude-sonnet-5"}},
    "systemPrompt": [{"text": "hi"}],
    "tools": [],
    "skills": [],
    "allowedTools": [],
    "truncation": {"strategy": "none"},
    "environment": {
        "agentCoreRuntimeEnvironment": {
            "agentRuntimeArn": f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:runtime/harness_hr_assistant_dev-ZNY19dHLR0",
            "agentRuntimeName": "harness_hr_assistant_dev",
            "agentRuntimeId": "harness_hr_assistant_dev-ZNY19dHLR0",
            "lifecycleConfiguration": {},
            "networkConfiguration": {"networkMode": "PUBLIC"},
        }
    },
}


@pytest.fixture
def control(monkeypatch):
    client = boto3.client("bedrock-agentcore-control", region_name="us-east-1")
    monkeypatch.setattr(agentcore, "control_client", lambda region=None: client)
    with Stubber(client) as stub:
        yield client, stub
        stub.assert_no_pending_responses()


def test_wait_for_ok_bad_and_timeout(monkeypatch):
    monkeypatch.setattr(agentcore.time, "sleep", lambda s: None)
    states = iter([{"status": "CREATING"}, {"status": "CREATING"}, {"status": "READY", "arn": "x"}])
    assert agentcore.wait_for(lambda: next(states), ("READY",), ("FAILED",), every=0, label="gw")["arn"] == "x"
    with pytest.raises(agentcore.AgentCoreError, match="gw failed: FAILED \\['boom'\\]"):
        agentcore.wait_for(
            lambda: {"status": "FAILED", "statusReasons": ["boom"]}, ("READY",), ("FAILED",), every=0, label="gw"
        )
    clock = iter([0, 0, 1, 2, 100])
    monkeypatch.setattr(agentcore.time, "monotonic", lambda: next(clock))
    with pytest.raises(agentcore.AgentCoreError, match="still CREATING"):
        agentcore.wait_for(lambda: {"status": "CREATING"}, ("READY",), (), every=0, timeout=10, label="gw")


def test_session_ids_are_long_enough():
    sid = agentcore.new_session_id()
    assert len(sid) >= 33 and sid.endswith("-sdk")
    assert agentcore.new_session_id("leave-balance").endswith("-leave-balance")


def test_parse_stream_like_the_spike():
    events = [
        {"messageStart": {"role": "assistant"}},
        {
            "contentBlockStart": {
                "contentBlockIndex": 0,
                "start": {"toolUse": {"toolUseId": "t1", "name": "hrtools___get_leave_balance"}},
            }
        },
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"toolUse": {"input": '{"employee_id": "E1001"}'}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
        {
            "metadata": {
                "usage": {"inputTokens": 912, "outputTokens": 63, "totalTokens": 975},
                "metrics": {"latencyMs": 1200},
            }
        },
        {"hookEvent": {"hookEventId": "h1", "name": "tool_audit", "type": "before_tool_call", "decision": "allow"}},
        {
            "contentBlockStart": {
                "contentBlockIndex": 1,
                "start": {"toolResult": {"toolUseId": "t1", "status": "success"}},
            }
        },
        {"messageStop": {"stopReason": "tool_result"}},
        {"contentBlockDelta": {"contentBlockIndex": 2, "delta": {"text": "You have 12 days"}}},
        {"contentBlockDelta": {"contentBlockIndex": 2, "delta": {"text": " of annual leave."}}},
        {"messageStop": {"stopReason": "end_turn"}},
        {
            "metadata": {
                "usage": {"inputTokens": 1011, "outputTokens": 49, "totalTokens": 1060},
                "metrics": {"latencyMs": 900},
            }
        },
    ]
    result = agentcore.parse_stream(iter(events), agentcore.time.monotonic(), "sess-1")
    assert result.text == "You have 12 days of annual leave."
    assert result.tools == ["hrtools___get_leave_balance"]
    assert result.stop_reasons == ["tool_use", "tool_result", "end_turn"] and result.stop_reason == "end_turn"
    assert result.usage == {"inputTokens": 1923, "outputTokens": 112, "totalTokens": 2035, "modelCalls": 2}
    assert result.hooks == [{"name": "tool_audit", "type": "before_tool_call", "decision": "allow", "reason": None}]
    assert result.summary()["toolsUsed"] == ["hrtools___get_leave_balance"]
    json.dumps(result.summary())


def test_parse_stream_budget_exceeded_runtime_client_error():
    events = [
        {"contentBlockDelta": {"contentBlockIndex": 0, "delta": {"text": "Let me check"}}},
        {
            "runtimeClientError": {
                "message": "litellm.BudgetExceededError: Budget has been exceeded! Key=hr-assistant-dev-claude-sonnet-5 Current cost: 0.0121, Max budget: 0.01"
            }
        },
    ]
    with pytest.raises(agentcore.HarnessInvokeError) as excinfo:
        agentcore.parse_stream(iter(events), 0.0, "sess-2")
    err = excinfo.value
    assert err.budget_exceeded and err.session_id == "sess-2" and err.partial_text == "Let me check"
    assert agentcore.parse_budget_message(err.message) == {
        "budgetExceeded": True,
        "currentCost": 0.0121,
        "maxBudget": 0.01,
        "subjectType": "key",
        "subject": "hr-assistant-dev-claude-sonnet-5",
    }
    assert agentcore.is_budget_exceeded({"runtimeClientError": {"message": "budget_exceeded"}})
    assert not agentcore.is_budget_exceeded("ThrottlingException: rate exceeded")
    # mentions of a budget and something exceeded are not cap hits unless LiteLLM's own wording is present
    assert not agentcore.is_budget_exceeded("The team budget duration exceeds the limit")
    assert agentcore.is_budget_exceeded("litellm.BudgetExceededError: Key=x Current cost: 1.2, Max budget: 1.0")
    with pytest.raises(agentcore.HarnessInvokeError) as excinfo:
        agentcore.parse_stream(iter([{"internalServerException": {"message": "oops"}}]), 0.0, "s")
    assert not excinfo.value.budget_exceeded


def test_invoke_rejects_short_session_ids():
    with pytest.raises(ValueError, match="33"):
        agentcore.invoke("arn:harness", "hi", session_id="short")


def test_find_memory_matches_by_id_prefix_then_name(control):
    client, stub = control
    stub.add_response(
        "list_memories",
        {
            "memories": [
                {
                    "id": "hr_assistant_dev_memory_old-AAAA",
                    "arn": "a",
                    "status": "ACTIVE",
                    "createdAt": 1,
                    "updatedAt": 1,
                },
                {
                    "id": "hr_assistant_dev_memory-BBBB",
                    "arn": "arn:mem",
                    "status": "ACTIVE",
                    "createdAt": 1,
                    "updatedAt": 1,
                },
            ]
        },
        {},
    )
    stub.add_response(
        "get_memory",
        {
            "memory": {
                "arn": "arn:mem",
                "id": "hr_assistant_dev_memory-BBBB",
                "name": "hr_assistant_dev_memory",
                "eventExpiryDuration": 30,
                "status": "ACTIVE",
                "createdAt": 1,
                "updatedAt": 1,
            }
        },
        {"memoryId": "hr_assistant_dev_memory-BBBB"},
    )
    memory = agentcore.find_memory("hr_assistant_dev_memory")
    assert memory["arn"] == "arn:mem"
    stub.add_response("list_memories", {"memories": []}, {})
    assert agentcore.find_memory("nothing") is None


def test_memory_strategy_shapes():
    assert agentcore.memory_strategies(["SUMMARIZATION", "USER_PREFERENCE", "SEMANTIC"]) == [
        {"summaryMemoryStrategy": {"name": "summary", "namespaces": ["/summaries/{actorId}/{sessionId}"]}},
        {"userPreferenceMemoryStrategy": {"name": "preferences", "namespaces": ["/preferences/{actorId}"]}},
        {"semanticMemoryStrategy": {"name": "facts", "namespaces": ["/facts/{actorId}"]}},
    ]
    with pytest.raises(ValueError):
        agentcore.memory_strategies(["EPISODIC"])


def test_create_or_update_harness_updates_existing_with_wrapped_memory(control, monkeypatch):
    client, stub = control
    monkeypatch.setattr(agentcore.time, "sleep", lambda s: None)
    spec = {
        "harnessName": "hr_assistant_dev",
        "executionRoleArn": HARNESS["executionRoleArn"],
        "model": {
            "liteLlmModelConfig": {
                "modelId": "openai/claude-sonnet-5",
                "apiBase": "https://llm/v1",
                "apiKeyArn": "arn:cp",
                "maxTokens": 2048,
            }
        },
        "systemPrompt": [{"text": "v2"}],
        "tools": [
            {
                "type": "agentcore_gateway",
                "name": "gateway",
                "config": {"agentCoreGateway": {"gatewayArn": GW_ARN, "outboundAuth": {"awsIam": {}}}},
            }
        ],
        "allowedTools": ["@gateway"],
        "memory": {"disabled": {}},
        "maxIterations": 30,
    }
    stub.add_response(
        "list_harnesses",
        {
            "harnesses": [
                {
                    "harnessId": HARNESS["harnessId"],
                    "harnessName": "hr_assistant_dev",
                    "arn": HARNESS["arn"],
                    "status": "READY",
                    "createdAt": 1,
                    "updatedAt": 1,
                }
            ]
        },
        {},
    )
    stub.add_response("get_harness", {"harness": HARNESS}, {"harnessId": HARNESS["harnessId"]})
    stub.add_response(
        "update_harness",
        {"harness": {**HARNESS, "status": "UPDATING", "harnessVersion": "3"}},  # same shape as GetHarness
        {
            "harnessId": HARNESS["harnessId"],
            **{k: v for k, v in spec.items() if k != "harnessName"},
            "memory": {"optionalValue": {"disabled": {}}},
            "hooks": [],  # for_update sends the three list members explicitly (UpdateHarness retains omitted ones)
        },
    )
    stub.add_response(
        "get_harness", {"harness": {**HARNESS, "status": "UPDATING"}}, {"harnessId": HARNESS["harnessId"]}
    )
    stub.add_response(
        "get_harness", {"harness": {**HARNESS, "harnessVersion": "3"}}, {"harnessId": HARNESS["harnessId"]}
    )
    harness = agentcore.create_or_update_harness("hr_assistant_dev", spec, tags={"ManagedBy": "dryrun"})
    assert harness["harnessVersion"] == "3"
    assert (
        agentcore.harness_log_group(harness, "DEFAULT")
        == "/aws/bedrock-agentcore/runtimes/harness_hr_assistant_dev-ZNY19dHLR0-DEFAULT"
    )


def test_create_or_update_harness_creates_with_tags(control, monkeypatch):
    client, stub = control
    spec = {
        "harnessName": "hr_assistant_dev",
        "executionRoleArn": "arn:role",
        "model": {"liteLlmModelConfig": {"modelId": "openai/x", "apiKeyArn": "arn:cp"}},
        "systemPrompt": [{"text": "v1"}],
        "memory": {"disabled": {}},
    }
    stub.add_response("list_harnesses", {"harnesses": []}, {})
    stub.add_response(
        "create_harness",
        {"harness": {**HARNESS, "status": "CREATING", "harnessVersion": "1"}},
        {
            "harnessName": "hr_assistant_dev",
            "tags": {"Project": "workshop-agent-platform", "ManagedBy": "workshop", "ProjectId": "hr-assistant"},
            **{k: v for k, v in spec.items() if k != "harnessName"},
        },
    )
    # the wait polls until GetHarness shows the version CreateHarness returned (version 1 here)
    stub.add_response(
        "get_harness", {"harness": {**HARNESS, "harnessVersion": "1"}}, {"harnessId": HARNESS["harnessId"]}
    )
    assert (
        agentcore.create_or_update_harness("hr_assistant_dev", spec, tags={"ProjectId": "hr-assistant"})["arn"]
        == HARNESS["arn"]
    )


def test_create_or_update_harness_recreates_a_failed_harness(control, monkeypatch):
    """A CREATE_FAILED harness (the account's first CreateHarness before the service-linked role existed) is deleted first."""
    client, stub = control
    monkeypatch.setattr(agentcore.time, "sleep", lambda s: None)
    spec = {
        "harnessName": "hr_assistant_dev",
        "executionRoleArn": "arn:role",
        "model": {"liteLlmModelConfig": {"modelId": "openai/x", "apiKeyArn": "arn:cp"}},
        "systemPrompt": [{"text": "v1"}],
        "memory": {"disabled": {}},
    }
    failed = {
        "harnessId": HARNESS["harnessId"],
        "harnessName": "hr_assistant_dev",
        "arn": HARNESS["arn"],
        "status": "CREATE_FAILED",
        "createdAt": 1,
        "updatedAt": 1,
    }
    stub.add_response("list_harnesses", {"harnesses": [failed]}, {})
    stub.add_response(
        "get_harness", {"harness": {**HARNESS, "status": "CREATE_FAILED"}}, {"harnessId": HARNESS["harnessId"]}
    )
    stub.add_response("list_harness_endpoints", {"endpoints": []}, {"harnessId": HARNESS["harnessId"]})
    stub.add_response(
        "delete_harness", {"harness": {**HARNESS, "status": "DELETING"}}, {"harnessId": HARNESS["harnessId"]}
    )
    stub.add_response("list_harnesses", {"harnesses": []}, {})  # the name is free again
    stub.add_response(
        "create_harness",
        {"harness": {**HARNESS, "status": "CREATING", "harnessVersion": "1"}},
        {
            "harnessName": "hr_assistant_dev",
            "tags": {"Project": "workshop-agent-platform", "ManagedBy": "workshop"},
            **{k: v for k, v in spec.items() if k != "harnessName"},
        },
    )
    stub.add_response(
        "get_harness", {"harness": {**HARNESS, "harnessVersion": "1"}}, {"harnessId": HARNESS["harnessId"]}
    )
    assert agentcore.create_or_update_harness("hr_assistant_dev", spec)["arn"] == HARNESS["arn"]


def test_ensure_kb_target_payload_matches_the_spike(control, monkeypatch):
    client, stub = control
    target = {
        "gatewayArn": GW_ARN,
        "targetId": "T2",
        "status": "READY",
        "name": "hrkb",
        "createdAt": 1,
        "updatedAt": 1,
        "targetConfiguration": {"mcp": {"connector": {"source": {"connectorId": "bedrock-knowledge-bases"}}}},
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    }
    stub.add_response("list_gateway_targets", {"items": []}, {"gatewayIdentifier": "gw-1"})
    stub.add_response(
        "create_gateway_target",
        {**target, "status": "CREATING"},
        {
            "gatewayIdentifier": "gw-1",
            "name": "hrkb",
            "description": "AnyCompany HR policy documents",
            "targetConfiguration": {
                "mcp": {
                    "connector": {
                        "source": {"connectorId": "bedrock-knowledge-bases"},
                        "configurations": [
                            {
                                "name": "Retrieve",
                                "description": "AnyCompany HR policy documents",
                                "parameterValues": {
                                    "knowledgeBaseId": "KB123",
                                    "retrievalConfiguration": {
                                        "managedSearchConfiguration": {
                                            "numberOfResults": 3,
                                            "filter": {"equals": {"key": "department", "value": "HR"}},
                                        }
                                    },
                                },
                                "parameterOverrides": [
                                    {
                                        "path": "$.retrievalQuery.text",
                                        "description": "The search query.",
                                        "visible": True,
                                    }
                                ],
                            }
                        ],
                    }
                }
            },
            "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        },
    )
    stub.add_response("get_gateway_target", target, {"gatewayIdentifier": "gw-1", "targetId": "T2"})
    assert (
        agentcore.ensure_kb_target("gw-1", "hrkb", "KB123", "HR", description="AnyCompany HR policy documents")[
            "targetId"
        ]
        == "T2"
    )


def test_ensure_policy_engine_finds_existing(control):
    client, stub = control
    engine = {
        "policyEngineId": "agent_platform_policy_engine-abc",
        "name": "agent_platform_policy_engine",
        "createdAt": 1,
        "updatedAt": 1,
        "policyEngineArn": f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:policy-engine/agent_platform_policy_engine-abc",
        "status": "ACTIVE",
        "statusReasons": [],
    }
    stub.add_response("list_policy_engines", {"policyEngines": [engine]}, {})
    stub.add_response("get_policy_engine", engine, {"policyEngineId": engine["policyEngineId"]})
    stub.add_response("get_policy_engine", engine, {"policyEngineId": engine["policyEngineId"]})
    assert agentcore.ensure_policy_engine()["policyEngineArn"].endswith(
        ":policy-engine/agent_platform_policy_engine-abc"
    )


def test_set_gateway_policy_mode_copies_required_fields(control, monkeypatch):
    client, stub = control
    gateway = {
        "gatewayArn": GW_ARN,
        "gatewayId": "gw-1",
        "gatewayUrl": "https://gw/mcp",
        "createdAt": 1,
        "updatedAt": 1,
        "status": "READY",
        "name": "hr-assistant-dev-gw",
        "roleArn": "arn:role",
        "protocolType": "MCP",
        "authorizerType": "AWS_IAM",
        "description": "HR dev gateway",
        "policyEngineConfiguration": {"arn": "arn:engine", "mode": "LOG_ONLY"},
        "workloadIdentityDetails": {"workloadIdentityArn": "arn:wi"},
    }
    stub.add_response("get_gateway", gateway, {"gatewayIdentifier": "gw-1"})
    stub.add_response(
        "update_gateway",
        {**gateway, "status": "UPDATING"},
        {
            "gatewayIdentifier": "gw-1",
            "name": "hr-assistant-dev-gw",
            "roleArn": "arn:role",
            "protocolType": "MCP",
            "authorizerType": "AWS_IAM",
            "description": "HR dev gateway",
            "policyEngineConfiguration": {"arn": "arn:engine", "mode": "ENFORCE"},
        },
    )
    stub.add_response(
        "get_gateway",
        {**gateway, "policyEngineConfiguration": {"arn": "arn:engine", "mode": "ENFORCE"}},
        {"gatewayIdentifier": "gw-1"},
    )
    assert (
        agentcore.set_gateway_policy_mode("gw-1", "arn:engine", "ENFORCE")["policyEngineConfiguration"]["mode"]
        == "ENFORCE"
    )


def test_ensure_policy_deletes_a_failed_policy_and_explains(control, monkeypatch):
    client, stub = control
    monkeypatch.setattr(agentcore.time, "sleep", lambda s: None)
    engine_id = "agent_platform_policy_engine-abc"
    cedar = 'forbid(principal is AgentCore::IamEntity, action == AgentCore::Action::"hrtools___submit_leave_request", resource) when { context.input.days > 10 };'
    policy = {
        "policyId": "hr_assistant_dev_forbid_long_leave-nj3j55cj90",
        "name": "hr_assistant_dev_forbid_long_leave",
        "policyEngineId": engine_id,
        "createdAt": 1,
        "updatedAt": 1,
        "policyArn": f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:policy-engine/{engine_id}/policy/hr_assistant_dev_forbid_long_leave-nj3j55cj90",
        "status": "CREATING",
        "enforcementMode": "ACTIVE",
        "definition": {"cedar": {"statement": cedar}},
        "statusReasons": [],
    }
    stub.add_response("list_policies", {"policies": []}, {"policyEngineId": engine_id})
    stub.add_response(
        "create_policy",
        policy,
        {
            "name": policy["name"],
            "policyEngineId": engine_id,
            "definition": {"cedar": {"statement": cedar}},
            "validationMode": "FAIL_ON_ANY_FINDINGS",
            "enforcementMode": "ACTIVE",
        },
    )
    stub.add_response(
        "get_policy",
        {
            **policy,
            "status": "CREATE_FAILED",
            "statusReasons": ["Overly Restrictive: Policy Engine will deny every request ..."],
        },
        {"policyEngineId": engine_id, "policyId": "hr_assistant_dev_forbid_long_leave-nj3j55cj90"},
    )
    stub.add_response(
        "delete_policy",
        {**policy, "status": "DELETING"},
        {"policyEngineId": engine_id, "policyId": "hr_assistant_dev_forbid_long_leave-nj3j55cj90"},
    )
    with pytest.raises(agentcore.AgentCoreError) as excinfo:
        agentcore.ensure_policy(engine_id, policy["name"], cedar)
    assert "Overly Restrictive" in str(excinfo.value) and "permit" in str(excinfo.value)


ONLINE_EVAL_SUMMARY = {
    "onlineEvaluationConfigArn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:online-evaluation-config/cfg-1",
    "onlineEvaluationConfigId": "cfg-1",
    "onlineEvaluationConfigName": "hr_assistant_prod_online_eval",
    "status": "ACTIVE",
    "executionStatus": "ENABLED",
    "createdAt": 1,
    "updatedAt": 1,
}
ONLINE_EVAL_RULE = {"samplingConfig": {"samplingPercentage": 100.0}, "sessionConfig": {"sessionTimeoutMinutes": 1}}


def _source(endpoint: str) -> dict:
    return {
        "cloudWatchLogs": {
            "logGroupNames": [f"/aws/bedrock-agentcore/runtimes/harness_hr_assistant_prod-RT123-{endpoint}"],
            "serviceNames": [f"harness_hr_assistant_prod.{endpoint}"],
        }
    }


def test_ensure_online_evaluation_repoints_a_config_that_watches_another_endpoint(control, monkeypatch):
    """An account promoted before 2026-10-22 watched the `live` endpoint; Prod now answers through DEFAULT."""
    client, stub = control
    monkeypatch.setattr(agentcore.time, "sleep", lambda s: None)
    old, new = _source("live"), _source("DEFAULT")
    config = {**ONLINE_EVAL_SUMMARY, "rule": ONLINE_EVAL_RULE, "dataSourceConfig": old}
    stub.add_response("list_online_evaluation_configs", {"onlineEvaluationConfigs": [ONLINE_EVAL_SUMMARY]}, {})
    stub.add_response("get_online_evaluation_config", config, {"onlineEvaluationConfigId": "cfg-1"})
    stub.add_response(
        "update_online_evaluation_config",
        {
            "onlineEvaluationConfigArn": ONLINE_EVAL_SUMMARY["onlineEvaluationConfigArn"],
            "onlineEvaluationConfigId": "cfg-1",
            "status": "UPDATING",
            "executionStatus": "ENABLED",
            "updatedAt": 2,
        },
        {"onlineEvaluationConfigId": "cfg-1", "dataSourceConfig": new},
    )
    stub.add_response(
        "get_online_evaluation_config", {**config, "status": "UPDATING"}, {"onlineEvaluationConfigId": "cfg-1"}
    )
    stub.add_response(
        "get_online_evaluation_config", {**config, "dataSourceConfig": new}, {"onlineEvaluationConfigId": "cfg-1"}
    )
    result = agentcore.ensure_online_evaluation(
        "hr_assistant_prod_online_eval",
        new["cloudWatchLogs"]["logGroupNames"][0],
        "harness_hr_assistant_prod.DEFAULT",
        ["Builtin.Helpfulness"],
        "arn:aws:iam::123456789012:role/eval",
    )
    assert result["status"] == "ACTIVE" and result["dataSourceConfig"] == new


def test_ensure_online_evaluation_keeps_a_config_that_already_matches(control):
    client, stub = control
    source = _source("DEFAULT")
    config = {**ONLINE_EVAL_SUMMARY, "rule": ONLINE_EVAL_RULE, "dataSourceConfig": source}
    stub.add_response("list_online_evaluation_configs", {"onlineEvaluationConfigs": [ONLINE_EVAL_SUMMARY]}, {})
    stub.add_response("get_online_evaluation_config", config, {"onlineEvaluationConfigId": "cfg-1"})
    stub.add_response("get_online_evaluation_config", config, {"onlineEvaluationConfigId": "cfg-1"})  # the ACTIVE wait
    result = agentcore.ensure_online_evaluation(
        "hr_assistant_prod_online_eval",
        source["cloudWatchLogs"]["logGroupNames"][0],
        "harness_hr_assistant_prod.DEFAULT",
        ["Builtin.Helpfulness"],
        "arn:aws:iam::123456789012:role/eval",
    )
    assert (
        result["onlineEvaluationConfigId"] == "cfg-1"
    )  # no UpdateOnlineEvaluationConfig was stubbed, so none was called
