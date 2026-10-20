from platform_sdk.keycloak import workshop_clients
from workshop_cli.obo_build import obo_policy

from workshop_cli import cli, record


def test_record_module_2_employee_default_and_validation():
    assert record.inputs({}, 2) == {"employee": "E1001"}
    assert record.validate({"module_2": {"inputs": {"employee": "E1001"}}}) == []
    problems = record.validate({"module_2": {"inputs": {"employee": "alex"}}})
    assert any("module_2.inputs.employee" in p for p in problems)
    assert "module_8" not in record.DEFAULTS


def test_cli_accepts_module_2_no_deploy_and_the_obo_command():
    parser = cli._parser()
    args = parser.parse_args(["apply", "2", "--no-deploy"])
    assert args.module == 2 and args.no_deploy is True
    import pytest

    with pytest.raises(SystemExit):
        parser.parse_args(["apply", "8"])
    args = parser.parse_args(["obo", "What is my leave balance?", "--skip-manual"])
    assert args.command == "obo" and args.skip_manual is True and args.prompt.startswith("What")
    args = parser.parse_args(["deploy", "idp"])
    assert args.scope == "idp"


def test_realm_clients_form_an_audience_chain():
    specs = workshop_clients("employee_id")
    assert specs["portal"].public and specs["portal"].direct_access_grants
    assert specs["portal"].audiences == ["hr-agent"]
    assert specs["agent"].token_exchange and specs["agent"].audiences == ["hr-obo-gateway"]
    assert specs["gateway"].token_exchange and specs["gateway"].audiences == ["hr-tools-api"]
    assert not specs["api"].token_exchange and specs["api"].audiences == []
    for key in ("portal", "agent", "gateway"):
        assert "employee_id" in specs[key].claims


class _Cfg:
    region = "us-east-1"
    account_id = "123456789012"


def test_obo_role_policy_scopes_to_the_workload_and_the_provider():
    policy = obo_policy(
        _Cfg(), "harness_hr_assistant_dev_obo-*", {"arn": "arn:provider", "secretArn": "arn:secret"}, api_key=True
    )
    sids = {s["Sid"]: s for s in policy["Statement"]}
    assert "harness_hr_assistant_dev_obo-*" in sids["WorkloadTokens"]["Resource"][1]
    assert "bedrock-agentcore:GetResourceApiKey" in sids["WorkloadTokens"]["Action"]
    gateway_policy = obo_policy(_Cfg(), "hr-assistant-dev-gw-obo-*", {"arn": "arn:provider"})
    assert "bedrock-agentcore:GetResourceApiKey" not in gateway_policy["Statement"][0]["Action"]
    assert "arn:provider" in sids["OnBehalfOfExchange"]["Resource"]
    assert sids["ProviderSecret"]["Resource"] == "arn:secret"
    assert "ProviderSecret" not in {s["Sid"] for s in obo_policy(_Cfg(), "x-*", {"arn": "a"})["Statement"]}
