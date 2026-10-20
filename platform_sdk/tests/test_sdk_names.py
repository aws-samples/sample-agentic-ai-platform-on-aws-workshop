import pytest

from platform_sdk import names


def test_all_derived_names_for_dev_and_prod():
    dev = names.ProjectNames("hr-assistant", "dev")
    prod = names.ProjectNames("hr-assistant", "prod")
    assert dev.gateway == "hr-assistant-dev-gw"
    assert prod.gateway == "hr-assistant-prod-gw"
    assert dev.memory == "hr_assistant_dev_memory"
    assert dev.harness == "hr_assistant_dev" and prod.harness == "hr_assistant_prod"
    assert dev.harness_endpoint == "DEFAULT" and prod.harness_endpoint == "live"
    assert prod.online_evaluation == "hr_assistant_prod_online_eval"
    assert dev.policy("allow_leave_balance") == "hr_assistant_dev_allow_leave_balance"
    assert dev.agent_role == "agent-platform-proj-hr-assistant-dev-agent"
    assert dev.gateway_role == "agent-platform-proj-hr-assistant-dev-gateway"
    assert (
        dev.agent_role_arn("123456789012")
        == "arn:aws:iam::123456789012:role/agent-platform-proj-hr-assistant-dev-agent"
    )
    assert dev.team == "hr-assistant"
    assert dev.key_alias("claude-sonnet-5") == "hr-assistant-dev-claude-sonnet-5"
    assert dev.credential_provider("claude-sonnet-5") == "litellm-hr-assistant-dev-claude-sonnet-5"
    assert dev.registry_record_tools == "hr-assistant-tools"
    assert names.POLICY_ENGINE_NAME == "agent_platform_policy_engine"
    assert names.REGISTRY_NAME == "agent_platform_catalogue"


def test_cedar_principal_matches_the_sts_assumed_role_form():
    dev = names.ProjectNames("hr-assistant", "dev")
    assert (
        dev.cedar_principal("123456789012")
        == 'AgentCore::IamEntity::"arn:aws:sts::123456789012:assumed-role/agent-platform-proj-hr-assistant-dev-agent"'
    )


def test_observability_names():
    prod = names.ProjectNames("hr-assistant", "prod")
    assert prod.harness_service_name == "harness_hr_assistant_prod.live"
    assert prod.harness_metric_name == "harness_hr_assistant_prod::live"
    assert names.harness_log_group("harness_hr_assistant_prod-AbC123", "live") == (
        "/aws/bedrock-agentcore/runtimes/harness_hr_assistant_prod-AbC123-live"
    )


@pytest.mark.parametrize("bad", ["HR", "1hr", "hr", "hr_assistant", "a" * 40, "", None])
def test_invalid_project_ids(bad):
    with pytest.raises(ValueError):
        names.validate_project_id(bad)


def test_invalid_env():
    with pytest.raises(ValueError):
        names.ProjectNames("hr-assistant", "staging")


def test_policy_names_are_checked_against_the_48_character_limit():
    # the module 4 rules (`allow_<tool>`) fit for hr-assistant ...
    assert names.ProjectNames("hr-assistant", "dev").policy("allow_get_employee_profile") == (
        "hr_assistant_dev_allow_get_employee_profile"
    )
    # ... but a 22-character project id overflows CreatePolicy's 48-character name limit
    with pytest.raises(ValueError, match="48"):
        names.ProjectNames("customer-support-agent", "dev").policy("allow_submit_leave_request")
    with pytest.raises(ValueError, match="A-Za-z0-9_"):
        names.ProjectNames("hr-assistant", "dev").policy("allow-dashes")


def test_for_project_gives_both_environments():
    both = names.for_project("invoice-helper")
    assert set(both) == {"dev", "prod"}
    assert both["prod"].harness == "invoice_helper_prod"
