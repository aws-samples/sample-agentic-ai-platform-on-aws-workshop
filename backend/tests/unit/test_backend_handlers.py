"""Handler tests through `router.dispatch` with moto DynamoDB and in-memory LiteLLM / Identity / IAM-role doubles."""

import json
import time
from datetime import UTC, datetime

import pytest
import requests
from backend_testlib import (
    ACCOUNT,
    CONFIG,
    PRIVATE_CONFIG,
    REGION,
    RELAY_FUNCTION,
    call,
    entry_module,
    hashed,
    make_event,
    raw_key,
)
from botocore.exceptions import ClientError
from platform_api import db, errors, jobs
from platform_api.services import clients
from platform_api.services import governance as governance_service
from platform_sdk.litellm import LiteLLMError

HR_LAMBDA = CONFIG["scenario/tools/hr-lambda-arn"]
NOT_FOUND = ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "GetGateway")


def create_domain(domain_id="people-ops", **extra):
    status, body = call(
        "POST",
        "/domains",
        body={"domainId": domain_id, "name": "People Ops", "description": "HR", "owner": "margaret", **extra},
    )
    assert status == 201, body
    return body


def project_body(project_id="hr-assistant", domain_id="people-ops", **extra):
    return {
        "projectId": project_id,
        "domainId": domain_id,
        "name": "HR Assistant",
        "description": "Employee-facing HR assistant",
        "models": ["claude-sonnet-5", "gpt-6-1-sol"],
        "maxBudget": 5,
        "budgetDuration": "7d",
        "tpmLimit": 200000,
        "rpmLimit": 200,
        "allowedToolArns": [HR_LAMBDA],
        "knowledgeBaseIds": ["KB123"],
        **extra,
    }


def create_project(project_id="hr-assistant", domain_id="people-ops", **extra):
    status, body = call("POST", "/projects", body=project_body(project_id, domain_id, **extra))
    assert status == 201, body
    return body


# ---- settings ---------------------------------------------------------------------------------------------------------------
def test_settings_defaults_merge_and_reset(table):
    status, body = call("GET", "/settings", persona="developer")
    assert status == 200
    assert body["defaultLimits"] == {"maxIterations": 30, "maxTokens": 4000, "timeoutSeconds": 120}
    assert body["defaultProjectCap"] == {"maxBudget": 5, "budgetDuration": "7d", "tpmLimit": 200000, "rpmLimit": 200}
    assert body["defaultKeyCap"] == {"maxBudget": 1, "budgetDuration": "7d"}
    assert body["registryId"] is None and body["policyEngineArn"] is None and body["updatedAt"] is None

    status, body = call(
        "PUT",
        "/settings",
        body={"registryId": "reg-1", "registryArn": "arn:reg-1", "defaultLimits": {"maxTokens": 8000}},
    )
    assert status == 200 and body["registryId"] == "reg-1" and body["registryArn"] == "arn:reg-1"
    assert body["defaultLimits"] == {"maxIterations": 30, "maxTokens": 8000, "timeoutSeconds": 120}
    assert body["updatedBy"] == "admin" and body["updatedAt"].endswith("Z")

    status, body = call("PUT", "/settings", body={"policyEngineId": "pe-1", "policyEngineArn": "arn:pe-1"})
    assert (
        body["registryId"] == "reg-1"
        and body["defaultLimits"]["maxTokens"] == 8000
        and body["policyEngineId"] == "pe-1"
    )

    status, body = call("PUT", "/settings", body={"registryId": None, "defaultLimits": None})
    assert status == 200 and body["registryId"] is None and body["defaultLimits"]["maxTokens"] == 4000
    assert body["policyEngineId"] == "pe-1"

    assert call("PUT", "/settings", body={"bogus": 1})[0] == 400
    assert call("PUT", "/settings", body={"registryId": 5})[0] == 400
    assert call("PUT", "/settings", body={"defaultLimits": "x"})[0] == 400
    assert call("PUT", "/settings", body=[])[0] == 400


def test_settings_nested_defaults_are_validated_and_sanitised(table):
    from platform_api import settings as settings_store

    rejected = [
        ({"defaultLimits": {"maxTokens": "abc", "maxIterations": -5}}, "defaultLimits.maxTokens"),
        ({"defaultLimits": {"maxIterations": -5}}, "defaultLimits.maxIterations must be >= 1"),
        ({"defaultLimits": {"timeoutSeconds": 1.5}}, "must be an integer"),
        ({"defaultProjectCap": {"maxBudget": -5, "bogus": 1, "budgetDuration": "soon"}}, "unknown keys ['bogus']"),
        (
            {"defaultProjectCap": {"maxBudget": -5, "budgetDuration": "soon"}},
            "defaultProjectCap.maxBudget must be >= 0",
        ),
        ({"defaultProjectCap": {"budgetDuration": "soon"}}, "defaultProjectCap.budgetDuration"),
        ({"defaultProjectCap": {"tpmLimit": 0}}, "defaultProjectCap.tpmLimit must be >= 1"),
        ({"defaultKeyCap": {"maxBudget": "two"}}, "defaultKeyCap.maxBudget must be a number"),
        ({"defaultKeyCap": {"maxBudget": True}}, "defaultKeyCap.maxBudget must be a number"),
    ]
    for body, fragment in rejected:
        status, response = call("PUT", "/settings", body=body)
        assert status == 400 and response["error"]["code"] == "VALIDATION_ERROR", (body, response)
        assert fragment in response["error"]["message"], (body, response)
    assert settings_store.stored() == {}  # nothing of the rejected bodies was stored

    status, body = call(
        "PUT", "/settings", body={"defaultKeyCap": {"maxBudget": 2.5}, "defaultLimits": {"maxTokens": 8000}}
    )
    assert status == 200 and body["defaultKeyCap"] == {"maxBudget": 2.5, "budgetDuration": "7d"}
    assert body["defaultLimits"]["maxTokens"] == 8000
    status, body = call("PUT", "/settings", body={"defaultLimits": {"maxTokens": None}})  # inner null resets that key
    assert status == 200 and body["defaultLimits"]["maxTokens"] == 4000 and body["defaultKeyCap"]["maxBudget"] == 2.5

    # a bad value written behind the API's back is ignored on read (the default applies) instead of reaching LiteLLM or the renderer
    item = db.get(db.settings_key())
    item["defaultKeyCap"] = {"maxBudget": "two", "budgetDuration": "7d"}
    item["defaultProjectCap"] = {"maxBudget": -5, "bogus": 1, "tpmLimit": 100}
    item["defaultLimits"] = "broken"
    db.put(item)
    loaded = settings_store.load()
    assert loaded["defaultKeyCap"] == {"maxBudget": 1, "budgetDuration": "7d"}
    assert loaded["defaultProjectCap"] == {"maxBudget": 5, "budgetDuration": "7d", "tpmLimit": 100, "rpmLimit": 200}
    assert loaded["defaultLimits"] == {"maxIterations": 30, "maxTokens": 4000, "timeoutSeconds": 120}


def test_settings_concurrent_update_is_a_conflict(table, monkeypatch):
    from platform_api import settings as settings_store

    call("PUT", "/settings", body={"registryId": "reg-1", "registryArn": "arn:reg-1"})
    stale = settings_store.stored()  # what a second writer read before the first one wrote
    first = settings_store.update({"policyEngineId": "pe-1", "policyEngineArn": "arn:pe-1"}, by="lab03")
    assert first["policyEngineId"] == "pe-1"
    monkeypatch.setattr(settings_store, "stored", lambda: dict(stale))
    with pytest.raises(errors.ApiError) as excinfo:
        settings_store.update({"registryId": "reg-2"}, by="ui")
    assert excinfo.value.status == 409 and excinfo.value.code == "CONFLICT"
    monkeypatch.undo()
    loaded = settings_store.load()
    assert loaded["registryId"] == "reg-1" and loaded["policyEngineId"] == "pe-1"  # the lost update never happened


# ---- domains ----------------------------------------------------------------------------------------------------------------
def test_domain_lifecycle(fakes):
    assert call("GET", "/domains", persona="approver") == (200, [])
    created = create_domain()
    assert created["domainId"] == "people-ops" and created["name"] == "People Ops" and created["owner"] == "margaret"
    assert created["createdBy"] == "admin" and "PK" not in created and "GSI1PK" not in created
    status, body = call("POST", "/domains", body={"domainId": "people-ops"})
    assert status == 409 and body["error"]["code"] == "CONFLICT"
    status, body = call("POST", "/domains", body={"domainId": "People Ops"})
    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR"
    status, body = call("POST", "/domains", body={"name": "no id"})
    assert status == 400 and "domainId" in body["error"]["message"]
    status, body = call("POST", "/domains", body={"domainId": "fin"})  # minimal body: name defaults to the id
    assert status == 201 and body["name"] == "fin" and body["description"] == "" and body["owner"] == ""

    status, body = call("GET", "/domains/people-ops", persona="developer")
    assert status == 200 and body["projectCount"] == 0
    create_project()
    assert call("GET", "/domains/people-ops")[1]["projectCount"] == 1
    assert [d["domainId"] for d in call("GET", "/domains")[1]] == ["people-ops", "fin"]  # GSI1SK = name order

    status, body = call("DELETE", "/domains/people-ops")
    assert status == 409 and "hr-assistant" in body["error"]["message"]
    assert call("DELETE", "/projects/hr-assistant")[0] == 200
    assert call("DELETE", "/domains/people-ops") == (200, {"deleted": "people-ops"})
    assert call("GET", "/domains/people-ops")[0] == 404
    assert call("DELETE", "/domains/people-ops")[0] == 404


# ---- projects ---------------------------------------------------------------------------------------------------------------
def test_create_project_validation(fakes):
    create_domain()
    cases = [
        ({"projectId": "HR", "domainId": "people-ops", "models": ["gpt-6-1-sol"]}, "kebab-case"),
        ({"projectId": "hr-assistant", "domainId": "missing", "models": ["gpt-6-1-sol"]}, "does not exist"),
        ({"projectId": "hr-assistant", "domainId": "people-ops", "models": ["gpt-4"]}, "not approved"),
        ({"projectId": "hr-assistant", "domainId": "people-ops", "models": []}, "at least one"),
        ({"projectId": "hr-assistant", "domainId": "people-ops"}, "models is required"),
        ({"domainId": "people-ops", "models": ["nova-2-lite"]}, "projectId is required"),
        (
            {
                "projectId": "hr-assistant",
                "domainId": "people-ops",
                "models": ["nova-2-lite"],
                "budgetDuration": "weekly",
            },
            "budgetDuration",
        ),
        (
            {"projectId": "hr-assistant", "domainId": "people-ops", "models": ["nova-2-lite"], "maxBudget": "5"},
            "number",
        ),
        ({"projectId": "hr-assistant", "domainId": "people-ops", "models": ["nova-2-lite"], "tpmLimit": 0}, ">= 1"),
    ]
    for payload, fragment in cases:
        status, body = call("POST", "/projects", body=payload)
        assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR", (payload, body)
        assert fragment in body["error"]["message"], (payload, body)
    assert fakes.litellm.teams == {} and fakes.roles.calls == []


def test_create_project_provisions_team_roles_and_item(fakes):
    create_domain()
    project = create_project()
    assert project["projectId"] == "hr-assistant" and project["domainId"] == "people-ops"
    assert project["status"] == "ACTIVE" and project["name"] == "HR Assistant"
    assert project["litellm"] == {
        "teamId": "hr-assistant",
        "teamAlias": "hr-assistant",
        "maxBudget": 5,
        "budgetDuration": "7d",
        "tpmLimit": 200000,
        "rpmLimit": 200,
        "models": ["claude-sonnet-5", "gpt-6-1-sol"],
    }
    assert project["roles"] == {
        "dev": {
            "agentRoleArn": "arn:aws:iam::123456789012:role/agent-platform-proj-hr-assistant-dev-agent",
            "gatewayRoleArn": "arn:aws:iam::123456789012:role/agent-platform-proj-hr-assistant-dev-gateway",
        },
        "prod": {
            "agentRoleArn": "arn:aws:iam::123456789012:role/agent-platform-proj-hr-assistant-prod-agent",
            "gatewayRoleArn": "arn:aws:iam::123456789012:role/agent-platform-proj-hr-assistant-prod-gateway",
        },
    }
    assert project["allowedToolArns"] == [HR_LAMBDA] and project["knowledgeBaseIds"] == ["KB123"]
    assert project["createdBy"] == "admin" and project["createdAt"].endswith("Z") and "PK" not in project
    team = fakes.litellm.teams["hr-assistant"]
    assert team["team_alias"] == "hr-assistant" and team["max_budget"] == 5 and team["budget_duration"] == "7d"
    assert team["tpm_limit"] == 200000 and team["rpm_limit"] == 200
    assert team["models"] == ["claude-sonnet-5", "gpt-6-1-sol"]
    assert team["metadata"] == {"domain": "people-ops", "project": "hr-assistant"}
    assert [c["env"] for c in fakes.roles.calls] == ["dev", "prod"]
    assert fakes.roles.calls[0] == {"project": "hr-assistant", "env": "dev", "tools": [HR_LAMBDA], "kbs": ["KB123"]}

    assert call("GET", "/projects/hr-assistant", persona="approver") == (200, project)
    assert call("GET", "/projects", query={"domainId": "people-ops"}, persona="developer") == (200, [project])
    assert call("GET", "/projects", query={"domainId": "other"}) == (200, [])
    assert call("GET", "/projects")[1] == [project]
    status, body = call("POST", "/projects", body=project_body())
    assert status == 409 and body["error"]["code"] == "CONFLICT"

    create_domain("finance-ops")
    status, body = call(
        "POST", "/projects", body={"projectId": "invoice-helper", "domainId": "finance-ops", "models": ["nova-2-lite"]}
    )
    assert status == 201 and body["name"] == "invoice-helper" and body["description"] == ""
    assert body["litellm"]["maxBudget"] == 5 and body["litellm"]["budgetDuration"] == "7d"  # settings defaults
    assert body["allowedToolArns"] == [] and body["knowledgeBaseIds"] == []
    assert fakes.roles.calls[-1]["tools"] == [] and fakes.roles.calls[-1]["kbs"] == []
    assert [p["projectId"] for p in call("GET", "/projects")[1]] == ["invoice-helper", "hr-assistant"]


def test_create_project_rolls_back_when_roles_fail(fakes):
    create_domain()
    fakes.roles.fail = True
    status, body = call("POST", "/projects", body=project_body())
    assert status == 502 and body["error"]["code"] == "PROVISIONING_FAILED"
    assert body["error"]["message"].startswith("step roles:")
    assert fakes.litellm.teams == {}  # the team created by this request was rolled back
    assert call("GET", "/projects/hr-assistant")[0] == 404


def test_create_project_keeps_a_pre_existing_team_when_rolling_back(fakes):
    create_domain()
    fakes.admin.create_team("hr-assistant", "hr-assistant", ["nova-2-lite"], 1, "1d", 10, 10, {})
    fakes.roles.fail = True
    status, body = call("POST", "/projects", body=project_body())
    assert status == 502 and "hr-assistant" in fakes.litellm.teams


def test_create_project_aligns_a_pre_existing_team(fakes):
    create_domain()
    fakes.admin.create_team("hr-assistant", "hr-assistant", ["nova-2-lite"], 1, "1d", 10, 10, {})
    project = create_project()
    team = fakes.litellm.teams["hr-assistant"]
    assert team["models"] == project["litellm"]["models"] and team["max_budget"] == 5 and team["tpm_limit"] == 200000


def test_create_project_fails_when_litellm_is_down(fakes):
    create_domain()
    fakes.litellm.fail.add("/team/new")
    status, body = call("POST", "/projects", body=project_body())
    assert status == 502 and body["error"]["code"] == "PROVISIONING_FAILED"
    assert body["error"]["message"].startswith("step litellm_team: LiteLLM 500")
    assert fakes.roles.calls == [] and call("GET", "/projects/hr-assistant")[0] == 404


def test_patch_project_updates_team_and_roles(fakes):
    create_domain()
    create_project()
    status, body = call(
        "PATCH", "/projects/hr-assistant", body={"maxBudget": 8, "models": ["gpt-6-1-sol"], "rpmLimit": 50}
    )
    assert status == 200
    assert body["litellm"]["maxBudget"] == 8 and body["litellm"]["models"] == ["gpt-6-1-sol"]
    assert body["litellm"]["rpmLimit"] == 50 and body["litellm"]["tpmLimit"] == 200000
    assert body["updatedBy"] == "admin"
    team = fakes.litellm.teams["hr-assistant"]
    assert team["max_budget"] == 8 and team["models"] == ["gpt-6-1-sol"] and team["rpm_limit"] == 50
    assert team["tpm_limit"] == 200000
    update_calls = [c for c in fakes.litellm.calls if c[1] == "/team/update"]
    assert update_calls[-1][2] == {
        "team_id": "hr-assistant",
        "models": ["gpt-6-1-sol"],
        "max_budget": 8,
        "rpm_limit": 50,
    }

    before = len(fakes.roles.calls)
    fin = HR_LAMBDA.replace("hr-tools", "finance-tools")
    status, body = call(
        "PATCH", "/projects/hr-assistant", body={"allowedToolArns": [HR_LAMBDA, fin], "description": "updated"}
    )
    assert status == 200 and body["allowedToolArns"] == [HR_LAMBDA, fin] and body["description"] == "updated"
    assert body["knowledgeBaseIds"] == ["KB123"]
    assert len(fakes.roles.calls) == before + 2
    assert fakes.roles.calls[-1] == {
        "project": "hr-assistant",
        "env": "prod",
        "tools": [HR_LAMBDA, fin],
        "kbs": ["KB123"],
    }
    assert len([c for c in fakes.litellm.calls if c[1] == "/team/update"]) == len(update_calls)  # no team call

    assert call("PATCH", "/projects/hr-assistant", body={})[0] == 400
    assert call("PATCH", "/projects/hr-assistant", body={"models": ["gpt"]})[0] == 400
    assert call("PATCH", "/projects/hr-assistant", body={"budgetDuration": "fortnight"})[0] == 400
    assert call("PATCH", "/projects/nope", body={"maxBudget": 1})[0] == 404
    assert call("PATCH", "/projects/hr-assistant", body={"maxBudget": 1}, persona="developer")[0] == 403


def test_delete_project_removes_team_keys_providers_roles_and_items(fakes):
    create_domain()
    create_project()
    assert (
        call(
            "PUT",
            "/projects/hr-assistant/resources",
            body={"env": "dev", "type": "memory", "name": "m", "arn": "arn:m"},
        )[0]
        == 200
    )
    assert call("POST", "/projects/hr-assistant/keys", body={"env": "dev", "model": "gpt-6-1-sol"})[0] == 201
    assert fakes.litellm.keys and fakes.identity.providers
    status, body = call("DELETE", "/projects/hr-assistant")
    assert status == 200
    assert body["deleted"] == "hr-assistant" and body["team"] is True and len(body["roles"]) == 4
    assert body["keys"] == [{"alias": "hr-assistant-dev-gpt-6-1-sol", "litellmKey": True, "credentialProvider": True}]
    assert body["items"] == 3  # META + resource + key
    assert fakes.litellm.teams == {} and fakes.litellm.keys == {}
    assert fakes.identity.providers == {} and fakes.roles.roles == {}
    assert call("GET", "/projects/hr-assistant")[0] == 404 and call("GET", "/projects")[1] == []
    assert call("DELETE", "/projects/hr-assistant")[0] == 404

    create_project()
    db.put({**db.release_key("hr-assistant", "r1"), "releaseId": "r1", "status": "PROMOTED"})
    status, body = call("DELETE", "/projects/hr-assistant")
    assert status == 409 and "PROMOTED" in body["error"]["message"]
    status, body = call("DELETE", "/projects/hr-assistant", query={"force": "true"})
    assert status == 200 and body["items"] == 2


def test_delete_project_fails_fast_when_litellm_refuses(fakes):
    create_domain()
    create_project()
    fakes.litellm.fail.add("/team/delete")
    status, body = call("DELETE", "/projects/hr-assistant")
    assert status == 502 and body["error"]["code"] == "UPSTREAM_ERROR"
    assert call("GET", "/projects/hr-assistant")[0] == 200  # record kept so the delete can be retried
    fakes.litellm.fail.clear()
    assert call("DELETE", "/projects/hr-assistant")[0] == 200


# ---- resources ----------------------------------------------------------------------------------------------------------------
def test_resources_upsert_list_delete(fakes):
    create_domain()
    create_project()
    gw_arn = "arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/hr-assistant-dev-gw-abc"
    gateway = {
        "env": "dev",
        "type": "gateway",
        "name": "hr-assistant-dev-gw",
        "arn": gw_arn,
        "id": "hr-assistant-dev-gw-abc",
        "url": "https://hr-assistant-dev-gw-abc.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp",
    }
    status, body = call("PUT", "/projects/hr-assistant/resources", body=gateway, persona="developer")
    assert status == 200
    assert body["projectId"] == "hr-assistant" and body["registeredBy"] == "developer" and body["attributes"] == {}
    assert body["arn"] == gw_arn and body["id"] == "hr-assistant-dev-gw-abc" and "updatedAt" not in body

    target = {
        "env": "dev",
        "type": "gateway_target",
        "name": "hrkb",
        "id": "T1",
        "attributes": {"kind": "knowledge_base", "gatewayArn": gw_arn},
    }
    status, body = call("PUT", "/projects/hr-assistant/resources", body=target)
    assert status == 200 and body["arn"] is None and body["attributes"]["kind"] == "knowledge_base"
    first = body["registeredAt"]
    status, body = call(
        "PUT",
        "/projects/hr-assistant/resources",
        body={
            **target,
            "attributes": {**target["attributes"], "filter": {"equals": {"key": "department", "value": "HR"}}},
        },
        persona="developer",
    )
    assert status == 200 and body["registeredAt"] == first and body["updatedBy"] == "developer" and "updatedAt" in body
    assert body["attributes"]["filter"] == {"equals": {"key": "department", "value": "HR"}}
    assert (
        call(
            "PUT",
            "/projects/hr-assistant/resources",
            body={"env": "prod", "type": "memory", "name": "hr_assistant_prod_memory", "arn": "arn:mem"},
        )[0]
        == 200
    )

    status, body = call("GET", "/projects/hr-assistant/resources", query={"env": "dev"}, persona="approver")
    assert status == 200 and [(r["type"], r["name"]) for r in body] == [
        ("gateway", "hr-assistant-dev-gw"),
        ("gateway_target", "hrkb"),
    ]
    assert len(call("GET", "/projects/hr-assistant/resources")[1]) == 3
    assert [r["name"] for r in call("GET", "/projects/hr-assistant/resources", query={"env": "prod"})[1]] == [
        "hr_assistant_prod_memory"
    ]
    assert call("GET", "/projects/hr-assistant/resources", query={"env": "staging"})[0] == 400
    assert call("GET", "/projects/nope/resources")[0] == 404

    assert call("PUT", "/projects/hr-assistant/resources", body={**gateway, "type": "widget"})[0] == 400
    assert call("PUT", "/projects/hr-assistant/resources", body={"type": "gateway", "name": "x"})[0] == 400
    assert call("PUT", "/projects/hr-assistant/resources", body={**gateway, "name": "a#b"})[0] == 400
    assert call("PUT", "/projects/hr-assistant/resources", body={**gateway, "attributes": "x"})[0] == 400
    assert call("PUT", "/projects/nope/resources", body=gateway)[0] == 404
    assert call("PUT", "/projects/hr-assistant/resources", body=gateway, persona="approver")[0] == 403

    status, body = call("DELETE", "/projects/hr-assistant/resources/dev/gateway_target/hrkb", persona="developer")
    assert status == 200 and body == {
        "deleted": {"projectId": "hr-assistant", "env": "dev", "type": "gateway_target", "name": "hrkb"}
    }
    assert call("DELETE", "/projects/hr-assistant/resources/dev/gateway_target/hrkb")[0] == 404
    assert (
        call("DELETE", "/projects/hr-assistant/resources/dev/gateway/hr-assistant-dev-gw", persona="approver")[0] == 403
    )
    assert len(call("GET", "/projects/hr-assistant/resources", query={"env": "dev"})[1]) == 1


# ---- keys ---------------------------------------------------------------------------------------------------------------------
def test_key_create_patch_and_adopt(fakes):
    create_domain()
    create_project()
    status, body = call(
        "POST", "/projects/hr-assistant/keys", body={"env": "dev", "model": "gpt-6-1-sol"}, persona="developer"
    )
    assert status == 201, body
    assert body["alias"] == "hr-assistant-dev-gpt-6-1-sol" and body["env"] == "dev" and body["model"] == "gpt-6-1-sol"
    assert body["litellmKeyId"] == hashed("hr-assistant-dev-gpt-6-1-sol")
    assert body["credentialProviderName"] == "litellm-hr-assistant-dev-gpt-6-1-sol"
    assert body["credentialProviderArn"].endswith("/apikeycredentialprovider/litellm-hr-assistant-dev-gpt-6-1-sol")
    assert body["maxBudget"] == 1 and body["budgetDuration"] == "7d" and body["status"] == "active"
    assert body["adopted"] is False and body["createdBy"] == "developer" and "key" not in body
    row = fakes.litellm.keys[body["litellmKeyId"]]
    assert row["team_id"] == "hr-assistant" and row["models"] == ["gpt-6-1-sol"] and row["max_budget"] == 1
    assert row["metadata"] == {
        "domain": "people-ops",
        "project": "hr-assistant",
        "agent": "hr_assistant_dev",
        "env": "dev",
        "model": "gpt-6-1-sol",
    }
    assert fakes.identity.keys["litellm-hr-assistant-dev-gpt-6-1-sol"] == raw_key("hr-assistant-dev-gpt-6-1-sol")

    status, body = call("POST", "/projects/hr-assistant/keys", body={"env": "dev", "model": "gpt-6-1-sol"})
    assert status == 409 and body["error"]["code"] == "CONFLICT"
    assert call("POST", "/projects/hr-assistant/keys", body={"env": "dev", "model": "nova-2-lite"})[0] == 400
    assert call("POST", "/projects/hr-assistant/keys", body={"env": "qa", "model": "gpt-6-1-sol"})[0] == 400
    assert (
        call("POST", "/projects/hr-assistant/keys", body={"env": "prod", "model": "gpt-6-1-sol", "maxBudget": -1})[0]
        == 400
    )
    assert (
        call("POST", "/projects/hr-assistant/keys", body={"env": "prod", "model": "gpt-6-1-sol"}, persona="approver")[0]
        == 403
    )
    assert call("POST", "/projects/nope/keys", body={"env": "dev", "model": "gpt-6-1-sol"})[0] == 404

    status, body = call("PATCH", "/projects/hr-assistant/keys/dev/gpt-6-1-sol", body={"status": "suspended"})
    assert status == 200 and body["status"] == "suspended" and row["blocked"] is True
    status, body = call("PATCH", "/projects/hr-assistant/keys/dev/gpt-6-1-sol", body={"maxBudget": 0.01})
    assert status == 200 and body["maxBudget"] == 0.01 and row["max_budget"] == 0.01 and body["status"] == "suspended"
    status, body = call(
        "PATCH", "/projects/hr-assistant/keys/dev/gpt-6-1-sol", body={"status": "active", "maxBudget": 2}
    )
    assert status == 200 and body["status"] == "active" and row["blocked"] is False and row["max_budget"] == 2
    assert call("PATCH", "/projects/hr-assistant/keys/dev/gpt-6-1-sol", body={})[0] == 400
    assert call("PATCH", "/projects/hr-assistant/keys/dev/gpt-6-1-sol", body={"status": "paused"})[0] == 400
    assert call("PATCH", "/projects/hr-assistant/keys/dev/nova-2-lite", body={"maxBudget": 1})[0] == 404
    assert (
        call("PATCH", "/projects/hr-assistant/keys/dev/gpt-6-1-sol", body={"maxBudget": 1}, persona="developer")[0]
        == 403
    )

    # module 5: the attendee creates the LiteLLM key and the credential provider by hand, then the platform adopts the pair
    made = fakes.admin.create_service_account_key(
        "hr-assistant", "hr-assistant-prod-claude-sonnet-5", ["claude-sonnet-5"], 1, "7d", {}
    )
    provider_arn = fakes.identity.store_api_key("litellm-hr-assistant-prod-claude-sonnet-5", made["key"])
    adopt = {
        "env": "prod",
        "model": "claude-sonnet-5",
        "adopt": {
            "litellmKeyId": made["token"],
            "credentialProviderArn": provider_arn,
            "maxBudget": 1,
            "budgetDuration": "7d",
        },
    }
    status, body = call("POST", "/projects/hr-assistant/keys", body=adopt, persona="developer")
    assert status == 201 and body["adopted"] is True and body["litellmKeyId"] == made["token"]
    assert body["credentialProviderName"] == "litellm-hr-assistant-prod-claude-sonnet-5"
    assert body["credentialProviderArn"] == provider_arn and body["alias"] == "hr-assistant-prod-claude-sonnet-5"
    assert len(fakes.litellm.keys) == 2  # nothing created by the adoption
    status, body = call("POST", "/projects/hr-assistant/keys", body=adopt)  # adopting twice is an upsert
    assert status == 201 and "updatedAt" in body
    status, body = call(
        "POST",
        "/projects/hr-assistant/keys",
        body={"env": "prod", "model": "claude-sonnet-5", "adopt": {"maxBudget": 1}},
    )
    assert status == 400 and "litellmKeyId" in body["error"]["message"]
    keys = call("GET", "/projects/hr-assistant/keys", persona="approver")[1]
    assert [k["alias"] for k in keys] == ["hr-assistant-dev-gpt-6-1-sol", "hr-assistant-prod-claude-sonnet-5"]


def test_key_adopt_validates_ownership_and_never_stores_a_raw_key(fakes):
    create_domain()
    create_project()
    alias, provider_name = "hr-assistant-dev-claude-sonnet-5", "litellm-hr-assistant-dev-claude-sonnet-5"
    made = fakes.admin.create_service_account_key("hr-assistant", alias, ["claude-sonnet-5"], 0.5, "7d", {})
    provider_arn = fakes.identity.store_api_key(provider_name, made["key"])
    path, base = "/projects/hr-assistant/keys", {"env": "dev", "model": "claude-sonnet-5"}

    def adopt(**fields):
        return call("POST", path, body={**base, "adopt": {"credentialProviderArn": provider_arn, **fields}})

    # the raw virtual key is hashed on the way in: the KEY item, GET /keys and GET /spend carry the hash only
    assert made["key"].startswith("sk-")
    status, body = adopt(litellmKeyId=made["key"])
    assert status == 201 and body["litellmKeyId"] == made["token"] and body["litellmKeyId"] == hashed(alias)
    assert body["maxBudget"] == 0.5 and body["budgetDuration"] == "7d" and body["status"] == "active"  # from /key/info
    for route, query in (("/projects/hr-assistant/keys", None), ("/spend", {"projectId": "hr-assistant"})):
        status, listed = call("GET", route, query=query, persona="approver")
        assert status == 200 and "sk-" not in json.dumps(listed), listed
    assert "sk-" not in json.dumps(db.get(db.key_key("hr-assistant", "dev", "claude-sonnet-5")))

    # a blocked key is adopted as suspended; explicit caps are validated
    fakes.litellm.keys[made["token"]]["blocked"] = True
    status, body = adopt(litellmKeyId=made["token"], maxBudget=2, budgetDuration="1d")
    assert status == 201 and body["status"] == "suspended" and body["maxBudget"] == 2 and body["budgetDuration"] == "1d"
    fakes.litellm.keys[made["token"]]["blocked"] = False

    rejected = {
        "not a hash": adopt(litellmKeyId="not-a-real-hash"),
        "unknown hash": adopt(litellmKeyId="0" * 64),
        "bad cap": adopt(litellmKeyId=made["token"], maxBudget="lots"),
        "negative cap": adopt(litellmKeyId=made["token"], maxBudget=-1),
        "bad duration": adopt(litellmKeyId=made["token"], budgetDuration="whenever"),
        "bad arn": call(
            "POST",
            path,
            body={**base, "adopt": {"litellmKeyId": made["token"], "credentialProviderArn": "arn:aws:x/y"}},
        ),
        "other provider": call(
            "POST",
            path,
            body={
                **base,
                "adopt": {"litellmKeyId": made["token"], "credentialProviderArn": fakes.identity.arn("litellm-other")},
            },
        ),
    }
    for label, (status, body) in rejected.items():
        assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR", (label, body)
    assert "64 hexadecimal" in rejected["not a hash"][1]["error"]["message"]
    assert "not a key the model gateway knows" in rejected["unknown hash"][1]["error"]["message"]
    assert "maxBudget must be a number" in rejected["bad cap"][1]["error"]["message"]
    assert "adopt.budgetDuration" in rejected["bad duration"][1]["error"]["message"]
    assert provider_name in rejected["bad arn"][1]["error"]["message"]

    # the key must belong to this project's team under the contract alias; the provider must exist in Identity
    create_domain("finance-ops")
    create_project("invoice-helper", "finance-ops", models=["claude-sonnet-5"])
    foreign = fakes.admin.create_service_account_key(
        "invoice-helper", "invoice-helper-dev-claude-sonnet-5", ["claude-sonnet-5"], 1, "7d", {}
    )
    status, body = adopt(litellmKeyId=foreign["token"])
    assert status == 400 and "belongs to team 'invoice-helper'" in body["error"]["message"]
    fakes.identity.delete_provider(provider_name)
    status, body = adopt(litellmKeyId=made["token"])
    assert status == 400 and "does not exist in AgentCore Identity" in body["error"]["message"]
    # the earlier adoption is still the stored row (nothing above overwrote it)
    assert call("GET", path)[1][0]["litellmKeyId"] == made["token"]


def test_key_create_adopts_existing_alias_with_provider_and_recreates_orphans(fakes):
    create_domain()
    create_project()
    made = fakes.admin.create_service_account_key(
        "hr-assistant", "hr-assistant-dev-gpt-6-1-sol", ["gpt-6-1-sol"], 0.5, "7d", {}
    )
    fakes.identity.store_api_key("litellm-hr-assistant-dev-gpt-6-1-sol", made["key"])
    status, body = call("POST", "/projects/hr-assistant/keys", body={"env": "dev", "model": "gpt-6-1-sol"})
    assert status == 201 and body["adopted"] is True and body["litellmKeyId"] == made["token"]
    assert body["maxBudget"] == 0.5 and body["status"] == "active"
    assert (
        len([c for c in fakes.litellm.calls if c[1] == "/key/service-account/generate"]) == 1
    )  # only the hand-made one

    fakes.admin.create_service_account_key(
        "hr-assistant", "hr-assistant-prod-gpt-6-1-sol", ["gpt-6-1-sol"], 0.5, "7d", {}
    )
    status, body = call("POST", "/projects/hr-assistant/keys", body={"env": "prod", "model": "gpt-6-1-sol"})
    assert status == 201 and body["adopted"] is False and body["maxBudget"] == 1  # recreated with the default cap
    assert "litellm-hr-assistant-prod-gpt-6-1-sol" in fakes.identity.providers
    deletes = [c for c in fakes.litellm.calls if c[1] == "/key/delete"]
    assert deletes and deletes[-1][2] == {"keys": [hashed("hr-assistant-prod-gpt-6-1-sol")]}


def test_key_create_rolls_back_when_identity_fails(fakes):
    create_domain()
    create_project()
    fakes.identity.fail = True
    status, body = call("POST", "/projects/hr-assistant/keys", body={"env": "dev", "model": "gpt-6-1-sol"})
    assert status == 502 and body["error"]["code"] == "PROVISIONING_FAILED"
    assert "credential_provider" in body["error"]["message"]
    assert fakes.litellm.keys == {} and call("GET", "/projects/hr-assistant/keys")[1] == []


# ---- spend --------------------------------------------------------------------------------------------------------------------
def test_spend_summary(fakes):
    create_domain()
    create_project()
    key = call("POST", "/projects/hr-assistant/keys", body={"env": "dev", "model": "gpt-6-1-sol"})[1]
    token, alias = key["litellmKeyId"], key["alias"]
    fakes.litellm.keys[token]["spend"] = 0.0168
    fakes.litellm.logs[token] = [
        {
            "request_id": "r1",
            "startTime": "2026-10-08T10:00:00",
            "model": "gpt-6-1-sol",
            "spend": 0.001,
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
            "metadata": {"user_api_key_alias": alias},
        },
        {"request_id": "r2", "startTime": "2026-10-08T11:00:00", "model": "gpt-6-1-sol", "spend": 0.002},
    ]
    status, body = call("GET", "/spend", query={"projectId": "hr-assistant", "env": "dev"}, persona="developer")
    assert status == 200 and body["projectId"] == "hr-assistant" and body["env"] == "dev"
    assert (
        body["team"]["teamId"] == "hr-assistant"
        and body["team"]["maxBudget"] == 5
        and body["team"]["budgetDuration"] == "7d"
    )
    assert body["team"]["keys"][0]["alias"] == alias
    assert (
        body["keys"][0]["spend"] == 0.0168 and body["keys"][0]["blocked"] is False and body["keys"][0]["budgetResetAt"]
    )
    assert body["keys"][0]["maxBudget"] == 1 and body["keys"][0]["status"] == "active"
    assert [row["requestId"] for row in body["logs"]] == ["r2", "r1"] and body["logs"][1]["keyAlias"] == alias
    assert body["logs"][1]["promptTokens"] == 100
    assert call("GET", "/spend", query={"projectId": "hr-assistant", "env": "prod"})[1]["keys"] == []
    fakes.litellm.teams.clear()
    status, body = call("GET", "/spend", query={"projectId": "hr-assistant"})
    assert status == 200 and body["team"] is None and len(body["keys"]) == 1
    assert call("GET", "/spend")[0] == 400
    assert call("GET", "/spend", query={"projectId": "nope"})[0] == 404
    assert call("GET", "/spend", query={"projectId": "hr-assistant", "env": "qa"})[0] == 400


def test_upstream_errors_map_to_502(fakes):
    create_domain()
    create_project()
    fakes.litellm.fail.add("/team/info")
    status, body = call("GET", "/spend", query={"projectId": "hr-assistant"})
    assert status == 502 and body["error"]["code"] == "UPSTREAM_ERROR" and "fake failure" in body["error"]["message"]
    # a hung or unreachable proxy (requests transport errors) is an upstream problem too, not a backend bug
    for exc in (requests.exceptions.ReadTimeout("read timed out"), requests.exceptions.ConnectionError("refused")):
        mapped = errors.from_exception(exc)
        assert (
            mapped.status == 502 and mapped.code == "UPSTREAM_ERROR" and "model gateway unreachable" in mapped.message
        )
    conditional = ClientError({"Error": {"Code": "ConditionalCheckFailedException", "Message": "x"}}, "PutItem")
    assert errors.from_exception(conditional).status == 409 and errors.from_exception(conditional).code == "CONFLICT"


def test_spend_tolerates_an_odd_cap_value(fakes):
    create_domain()
    create_project()
    key = call("POST", "/projects/hr-assistant/keys", body={"env": "dev", "model": "gpt-6-1-sol"})[1]
    item = db.get(db.key_key("hr-assistant", "dev", "gpt-6-1-sol"))
    item["maxBudget"] = "lots"  # written behind the API's back; /key/info fails too, so the row keeps the stored value
    db.put(item)
    fakes.litellm.keys.pop(key["litellmKeyId"])
    status, body = call("GET", "/spend", query={"projectId": "hr-assistant"})
    assert status == 200 and body["keys"][0]["error"] and body["byEnv"]["dev"]["maxBudget"] == 0.0


# ---- governance ---------------------------------------------------------------------------------------------------------------
class FakeControl:
    def __init__(self, gateways):
        self.gateways = gateways

    def get_gateway(self, gatewayIdentifier):
        if (
            gatewayIdentifier == "bad-id"
        ):  # a malformed id is rejected by IAM, not by the service (seen live 8 Oct 2026)
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "GetGateway")
        if gatewayIdentifier not in self.gateways:
            raise NOT_FOUND
        return {"gatewayId": gatewayIdentifier, **self.gateways[gatewayIdentifier]}


class FakeBedrock:
    def get_guardrail(self, guardrailIdentifier, guardrailVersion):
        assert (guardrailIdentifier, guardrailVersion) == ("gr123", "2")
        return {
            "name": "hr-baseline",
            "status": "READY",
            "topicPolicy": {"topics": [{"name": "LegalAdvice"}]},
            "contentPolicy": {
                "filters": [{"type": "PROMPT_ATTACK", "inputStrength": "HIGH", "outputStrength": "NONE"}]
            },
            "sensitiveInformationPolicy": {"piiEntities": [{"type": "US_SOCIAL_SECURITY_NUMBER", "action": "BLOCK"}]},
            "blockedInputMessaging": "Sorry, I can't help with that topic. Please contact HR directly.",
        }


def test_governance_summary(fakes, monkeypatch):
    create_domain()
    create_project()
    for env, gateway_id in (("dev", "gw-dev-1"), ("prod", "gw-prod-1")):
        call(
            "PUT",
            "/projects/hr-assistant/resources",
            body={
                "env": env,
                "type": "gateway",
                "name": f"hr-assistant-{env}-gw",
                "id": gateway_id,
                "arn": f"arn:gw/{gateway_id}",
            },
        )
    create_domain("finance-ops")
    create_project("invoice-helper", "finance-ops")
    call(
        "PUT",
        "/projects/invoice-helper/resources",
        body={"env": "dev", "type": "gateway", "name": "invoice-helper-dev-gw", "id": "bad-id", "arn": "arn:gw/bad-id"},
    )
    call(
        "PUT",
        "/settings",
        body={"policyEngineId": "pe1", "policyEngineArn": "arn:pe1", "registryId": "reg1", "registryArn": "arn:reg1"},
    )
    control = FakeControl(
        {
            "gw-dev-1": {
                "name": "hr-assistant-dev-gw",
                "gatewayArn": "arn:gw/gw-dev-1",
                "gatewayUrl": "https://gw",
                "status": "READY",
                "policyEngineConfiguration": {"arn": "arn:pe1", "mode": "LOG_ONLY"},
            }
        }
    )
    monkeypatch.setattr(clients, "agentcore_control", lambda: control)
    monkeypatch.setattr(clients, "bedrock", lambda: FakeBedrock())

    status, body = call("GET", "/governance", persona="approver")
    assert status == 200
    assert [m["alias"] for m in body["models"]] == ["claude-opus-5", "claude-sonnet-5", "gpt-6-1-sol", "nova-2-lite"]
    assert body["models"][0]["tier"] == "premium"
    sonnet = body["models"][1]
    assert sonnet["source"] == "litellm" and sonnet["modelId"] == "us.anthropic.claude-sonnet-5"
    assert sonnet["litellmModel"] == "bedrock/converse/us.anthropic.claude-sonnet-5" and sonnet["tier"] == "standard"
    assert sonnet["guardrail"] == {"id": "gr123", "version": "2", "name": "hr-baseline"}
    assert sonnet["inputCostPerToken"] == 1e-06
    assert (
        body["guardrail"]["id"] == "gr123"
        and body["guardrail"]["version"] == "2"
        and body["guardrail"]["name"] == "hr-baseline"
    )
    assert body["guardrail"]["summary"]["topics"] == ["LegalAdvice"]
    assert body["guardrail"]["summary"]["contentFilters"] == [
        {"type": "PROMPT_ATTACK", "input": "HIGH", "output": "NONE"}
    ]
    assert body["guardrail"]["summary"]["blockedInputMessaging"].startswith("Sorry")
    assert body["policyEngine"] == {"id": "pe1", "arn": "arn:pe1"} and body["registry"] == {
        "id": "reg1",
        "arn": "arn:reg1",
    }
    gateways = {g["gatewayId"]: g for g in body["gateways"]}
    assert gateways["gw-dev-1"]["mode"] == "LOG_ONLY" and gateways["gw-dev-1"]["policyEngineArn"] == "arn:pe1"
    assert gateways["gw-dev-1"]["status"] == "READY" and gateways["gw-dev-1"]["env"] == "dev"
    assert gateways["gw-dev-1"]["projectId"] == "hr-assistant" and gateways["gw-dev-1"]["url"] == "https://gw"
    assert gateways["gw-prod-1"]["status"] == "NOT_FOUND" and gateways["gw-prod-1"]["mode"] is None
    assert gateways["bad-id"]["status"] == "INVALID" and "not authorized" in gateways["bad-id"]["error"]
    assert gateways["bad-id"]["projectId"] == "invoice-helper"

    fakes.litellm.fail.add("/model/info")
    body = call("GET", "/governance")[1]
    assert len(body["models"]) == 4 and all(m["source"] == "ssm" for m in body["models"])
    assert body["models"][2]["alias"] == "gpt-6-1-sol"


def test_governance_without_anything_configured(fakes, monkeypatch):
    class Broken:
        def get_guardrail(self, **kwargs):
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "GetGuardrail")

    monkeypatch.setattr(clients, "bedrock", lambda: Broken())
    status, body = call("GET", "/governance")
    assert status == 200 and body["gateways"] == [] and body["policyEngine"] == {"id": None, "arn": None}
    assert body["guardrail"]["name"] is None and "GetGuardrail failed" in body["guardrail"]["summary"]["error"]


def test_governance_before_the_landing_zone_exists(fakes, monkeypatch):
    """The control plane is deployed before the model gateway (module 1): GET /governance answers without the SSM list and the
    credentials route says what is missing instead of failing with a KeyError."""
    from platform_sdk import config as sdk_config

    reduced = {k: v for k, v in CONFIG.items() if not k.startswith("model-gateway/")}
    monkeypatch.setenv("PLATFORM_CONFIG_JSON", json.dumps(reduced))
    sdk_config.reset_cache()
    clients.cfg.cache_clear()
    try:
        status, body = call("GET", "/governance")
        assert status == 200 and body["models"] == []
        assert body["modelGateway"] == {
            "deployed": False,
            "url": None,
            "openaiBase": None,
            "adminUiUrl": None,
            "private": False,
            "reachableFrom": None,
        }
        status, body = call("GET", "/governance/model-gateway/credentials")
        assert status == 404 and "module 1" in body["error"]["message"]
    finally:
        sdk_config.reset_cache()
        clients.cfg.cache_clear()


def test_governance_model_gateway_reports_whether_the_proxy_is_private(fakes, monkeypatch):
    """Tier 3 landing zone: the ModelGateway stack publishes `model-gateway/private` and the relay function; the Governance screen
    says the admin UI is not reachable from a browser. The public shape (no such keys) stays `private: False`."""
    from platform_sdk import config as sdk_config

    monkeypatch.setattr(clients, "bedrock", lambda: FakeBedrock())
    body = call("GET", "/governance")[1]
    assert body["modelGateway"] == {
        "deployed": True,
        "url": "https://llm.example.test",
        "openaiBase": "https://llm.example.test/v1",
        "adminUiUrl": "https://llm.example.test/ui",
        "private": False,
        "reachableFrom": "anywhere (internet-facing load balancer)",
    }
    monkeypatch.setenv("PLATFORM_CONFIG_JSON", json.dumps(PRIVATE_CONFIG))
    sdk_config.reset_cache()
    clients.cfg.cache_clear()
    try:
        body = call("GET", "/governance")[1]
        assert body["modelGateway"]["private"] is True
        assert body["modelGateway"]["reachableFrom"] == f"inside the VPC (relay function {RELAY_FUNCTION})"
        assert body["modelGateway"]["adminUiUrl"] == "https://llm.example.test/ui"  # still shown, with the credentials
        # the credentials route is unchanged: the master key is the admin UI password wherever the UI is reached from
        monkeypatch.setattr(clients, "master_key", lambda: "sk-master")
        status, body = call("GET", "/governance/model-gateway/credentials")
        assert status == 200 and body["password"] == "sk-master" and body["adminUiUrl"] == "https://llm.example.test/ui"
    finally:
        sdk_config.reset_cache()
        clients.cfg.cache_clear()


def test_clients_litellm_admin_uses_the_relay_only_on_a_private_landing_zone(monkeypatch):
    """`clients.litellm_admin()` is built by `platform_sdk.litellm.admin_client`: plain requests against the public URL, the relay
    Lambda (shared Lambda client, API timeout) when SSM names one."""
    from platform_sdk import config as sdk_config
    from platform_sdk import litellm as sdk_litellm

    monkeypatch.setattr(clients, "master_key", lambda: "sk-master")
    direct = clients.litellm_admin()
    assert not direct.via_relay and direct.timeout == clients.LITELLM_TIMEOUT and direct.master_key == "sk-master"
    assert direct.base_url == "https://llm.example.test"
    monkeypatch.setenv("PLATFORM_CONFIG_JSON", json.dumps(PRIVATE_CONFIG))
    clients.reset()
    sdk_config.reset_cache()
    monkeypatch.setattr(clients, "master_key", lambda: "sk-master")
    try:
        relayed = clients.litellm_admin()
        assert relayed.via_relay and isinstance(relayed.session, sdk_litellm.LambdaRelaySession)
        assert relayed.session.function_name == RELAY_FUNCTION and relayed.timeout == clients.LITELLM_TIMEOUT
        assert relayed.session.lambda_client is clients.lambda_client()  # the shared client, one per container
        assert clients.litellm_admin() is relayed  # cached like every other client
    finally:
        clients.reset()
        sdk_config.reset_cache()


def test_model_gateway_credentials(fakes, monkeypatch):
    monkeypatch.setattr(clients, "master_key", lambda: "sk-master")
    status, body = call("GET", "/governance/model-gateway/credentials")
    assert status == 200
    assert body == {
        "url": "https://llm.example.test",
        "adminUiUrl": "https://llm.example.test/ui",
        "username": "admin",
        "password": "sk-master",
        "secretArn": CONFIG["model-gateway/master-key-secret-arn"],
    }
    for persona in ("developer", "approver"):
        assert call("GET", "/governance/model-gateway/credentials", persona=persona)[0] == 403, persona
    # never cached by the browser; the shared GET /governance never carries the password
    from platform_api import router

    response = router.dispatch(make_event("GET", "/governance/model-gateway/credentials"))
    assert (
        response["headers"]["Cache-Control"] == "no-store" and response["headers"]["Content-Type"] == "application/json"
    )
    monkeypatch.setattr(clients, "bedrock", lambda: FakeBedrock())
    assert "sk-master" not in json.dumps(call("GET", "/governance")[1])
    assert "Cache-Control" not in router.dispatch(make_event("GET", "/governance"))["headers"]


def test_governance_models_resolve_other_guardrail_names(fakes, monkeypatch):
    """A model guarded by another guardrail than the baseline gets its name through one cached GetGuardrail call."""
    calls = []

    class NamedBedrock:
        def get_guardrail(self, guardrailIdentifier, guardrailVersion):
            calls.append((guardrailIdentifier, guardrailVersion))
            if guardrailIdentifier == "gr-finance":
                return {"name": "finance-strict", "status": "READY"}
            return FakeBedrock().get_guardrail(guardrailIdentifier, guardrailVersion)

    rows = fakes.admin.model_info()
    for row in rows[1:]:
        row["litellm_params"]["guardrailConfig"] = {"guardrailIdentifier": "gr-finance", "guardrailVersion": "1"}
    rows[0]["litellm_params"].pop("guardrailConfig")
    monkeypatch.setattr(fakes.admin, "model_info", lambda: rows)
    monkeypatch.setattr(clients, "bedrock", lambda: NamedBedrock())
    body = call("GET", "/governance")[1]
    by_alias = {m["alias"]: m for m in body["models"]}
    assert by_alias["claude-opus-5"]["guardrail"] is None
    assert by_alias["claude-sonnet-5"]["guardrail"] == {"id": "gr-finance", "version": "1", "name": "finance-strict"}
    assert by_alias["gpt-6-1-sol"]["guardrail"]["name"] == "finance-strict"
    assert by_alias["nova-2-lite"]["guardrail"]["name"] == "finance-strict"
    assert calls == [("gr123", "2"), ("gr-finance", "1")]  # the baseline once, the second guardrail once (cached)


HR_TOOLS_ARN = CONFIG["scenario/tools/hr-lambda-arn"]
EXTRA_TOOL_ARN = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:payroll-tools:live"


class FakeTargetsControl:
    """ListGateways (two pages) / ListGatewayTargets / GetGatewayTarget: Lambda targets, a connector target (never fetched), a
    target whose GetGatewayTarget fails, and a gateway that vanished between the two list calls."""

    def __init__(self):
        self.fetched: list[str] = []

    def list_gateways(self, nextToken=None):
        if nextToken is None:
            return {"items": [{"gatewayId": "gw-dev-1", "name": "hr-assistant-dev-gw"}], "nextToken": "page2"}
        return {
            "items": [
                {"gatewayId": "gw-fin-1", "name": "invoice-helper-dev-gw"},
                {"gatewayId": "gw-gone", "name": "deleted-meanwhile"},
            ]
        }

    def list_gateway_targets(self, gatewayIdentifier, nextToken=None):
        if gatewayIdentifier == "gw-gone":
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "ListGatewayTargets")
        targets = {
            "gw-dev-1": [
                {"targetId": "t1", "name": "hrtools", "targetType": "LAMBDA"},
                {"targetId": "t2", "name": "hrkb", "targetType": "CONNECTOR"},
                {"targetId": "t4", "name": "broken", "targetType": "LAMBDA"},
            ],
            "gw-fin-1": [{"targetId": "t3", "name": "payroll"}],  # an older summary without targetType
        }
        return {"items": targets[gatewayIdentifier]}

    def get_gateway_target(self, gatewayIdentifier, targetId):
        self.fetched.append(targetId)
        if targetId == "t4":
            raise ClientError({"Error": {"Code": "InternalServerException", "Message": "boom"}}, "GetGatewayTarget")
        configs = {
            "t1": {"mcp": {"lambda": {"lambdaArn": HR_TOOLS_ARN}}},
            "t3": {"mcp": {"lambda": {"lambdaArn": EXTRA_TOOL_ARN}}},
        }
        names = {"t1": "hrtools", "t3": "payroll"}
        return {"targetId": targetId, "name": names[targetId], "targetConfiguration": configs[targetId]}


class FakeBedrockAgent:
    def list_knowledge_bases(self, nextToken=None):
        if nextToken is None:
            return {
                "knowledgeBaseSummaries": [
                    {
                        "knowledgeBaseId": "KB123",
                        "name": "agent-platform-policies",
                        "description": "HR docs",
                        "status": "ACTIVE",
                    }
                ],
                "nextToken": "more",
            }
        return {"knowledgeBaseSummaries": [{"knowledgeBaseId": "KB999", "name": "legal-docs", "status": "ACTIVE"}]}

    def get_knowledge_base(self, knowledgeBaseId):
        assert knowledgeBaseId == "KB777", (
            knowledgeBaseId
        )  # only the registered id the list did not return is looked up
        return {"knowledgeBase": {"knowledgeBaseId": "KB777", "name": "hr-policies-registered", "status": "ACTIVE"}}


def register_target(env, name, target_id, attributes):
    status, body = call(
        "PUT",
        "/projects/hr-assistant/resources",
        body={"env": env, "type": "gateway_target", "name": name, "id": target_id, "attributes": attributes},
    )
    assert status == 200, body


def test_project_options(fakes, monkeypatch):
    create_domain()
    create_project()
    register_target("dev", "hrtools", "t1", {"kind": "lambda", "lambdaArn": HR_TOOLS_ARN})
    register_target("dev", "hrkb", "t2", {"kind": "knowledge_base", "knowledgeBaseId": "KB777"})
    kb_arn = f"arn:aws:bedrock:us-east-1:{ACCOUNT}:knowledge-base/KB123"
    register_target("prod", "hrkb", "t2p", {"kind": "knowledge_base", "knowledgeBaseId": kb_arn})
    control = FakeTargetsControl()
    monkeypatch.setattr(clients, "agentcore_control", lambda: control)
    monkeypatch.setattr(clients, "bedrock_agent", lambda: FakeBedrockAgent())

    status, body = call("GET", "/governance/project-options")
    assert status == 200, body
    # the vanished gateway is skipped silently; the unreadable target is reported by name; everything else is listed
    assert body["warnings"] == [
        "gateway hr-assistant-dev-gw: target broken could not be read (GetGatewayTarget failed: InternalServerException: boom)"
    ]
    assert control.fetched == ["t1", "t4", "t3"]  # the CONNECTOR target is never fetched
    tools = {t["arn"]: t for t in body["tools"]}
    assert [t["name"] for t in body["tools"]] == ["agent-platform-hr-tools", "payroll-tools"]
    assert tools[HR_TOOLS_ARN]["description"].startswith("AnyCompany HR tools")
    assert tools[HR_TOOLS_ARN]["sources"] == [
        "scenario",
        "registered: hr-assistant/dev target hrtools",
        "gateway hr-assistant-dev-gw target hrtools",
    ]
    assert tools[EXTRA_TOOL_ARN]["sources"] == ["gateway invoice-helper-dev-gw target payroll"]
    kbs = {k["id"]: k for k in body["knowledgeBases"]}
    assert set(kbs) == {"KB123", "KB999", "KB777"}
    assert kbs["KB123"]["name"] == "agent-platform-policies" and kbs["KB123"]["status"] == "ACTIVE"
    # a knowledge base registered by ARN merges into its id row
    assert kbs["KB123"]["sources"] == ["bedrock", "scenario", "registered: hr-assistant/prod target hrkb"]
    assert kbs["KB123"]["arn"] == kb_arn
    # a registered id the list did not return gets its name from GetKnowledgeBase
    assert kbs["KB777"]["name"] == "hr-policies-registered"
    assert kbs["KB777"]["sources"] == ["registered: hr-assistant/dev target hrkb"]
    assert call("GET", "/governance/project-options", persona="developer")[0] == 403


def test_pages_is_bounded():
    class Looping:
        def __init__(self):
            self.calls = 0

        def list_things(self, nextToken=None):
            self.calls += 1
            return {"items": [{"n": self.calls}], "nextToken": "same-token"}

    client = Looping()
    items = governance_service._pages(client, "list_things", "items")
    assert client.calls == 2 and len(items) == 2  # a repeated token ends the walk

    class Endless(Looping):
        def list_things(self, nextToken=None):
            self.calls += 1
            return {"items": [], "nextToken": f"token-{self.calls}"}

    endless = Endless()
    governance_service._pages(endless, "list_things", "items")
    assert endless.calls == governance_service.MAX_PAGES


def test_project_options_degrade_without_permissions(fakes, monkeypatch):
    class Denied:
        def __getattr__(self, name):
            def _raise(**kwargs):
                raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, name)

            return _raise

    monkeypatch.setattr(clients, "agentcore_control", lambda: Denied())
    monkeypatch.setattr(clients, "bedrock_agent", lambda: Denied())
    status, body = call("GET", "/governance/project-options")
    assert status == 200
    assert [t["arn"] for t in body["tools"]] == [HR_TOOLS_ARN] and body["tools"][0]["sources"] == ["scenario"]
    assert [k["id"] for k in body["knowledgeBases"]] == ["KB123"]
    assert body["knowledgeBases"][0]["name"] == "HR policy knowledge base (scenario)"
    assert len(body["warnings"]) == 2 and all("not authorized" in w for w in body["warnings"])


def test_gateway_mode_switch(fakes, monkeypatch):
    seen = {}
    control = FakeControl(
        {
            "gw-1": {"name": "gw-one", "policyEngineConfiguration": {"arn": "arn:pe-attached", "mode": "LOG_ONLY"}},
            "gw-2": {"name": "gw-two"},
        }
    )
    monkeypatch.setattr(clients, "agentcore_control", lambda: control)

    def fake_set(gateway_id, engine_arn, mode, region=None):
        seen.update(gateway_id=gateway_id, engine=engine_arn, mode=mode, region=region)
        return {
            "gatewayId": gateway_id,
            "name": control.gateways[gateway_id]["name"],
            "status": "READY",
            "policyEngineConfiguration": {"arn": engine_arn, "mode": mode},
        }

    monkeypatch.setattr(governance_service.agentcore, "set_gateway_policy_mode", fake_set)
    status, body = call("POST", "/governance/gateways/gw-1/mode", body={"mode": "ENFORCE"})
    assert status == 200
    assert body == {
        "gatewayId": "gw-1",
        "name": "gw-one",
        "mode": "ENFORCE",
        "policyEngineArn": "arn:pe-attached",
        "status": "READY",
    }
    assert seen == {"gateway_id": "gw-1", "engine": "arn:pe-attached", "mode": "ENFORCE", "region": REGION}

    status, body = call("POST", "/governance/gateways/gw-2/mode", body={"mode": "LOG_ONLY"})
    assert status == 409 and body["error"]["code"] == "POLICY_ENGINE_NOT_CONFIGURED"
    call("PUT", "/settings", body={"policyEngineId": "pe", "policyEngineArn": "arn:pe-settings"})
    status, body = call("POST", "/governance/gateways/gw-2/mode", body={"mode": "LOG_ONLY"})
    assert status == 200 and seen["engine"] == "arn:pe-settings" and body["mode"] == "LOG_ONLY"

    assert call("POST", "/governance/gateways/gw-1/mode", body={"mode": "OFF"})[0] == 400
    assert call("POST", "/governance/gateways/gw-1/mode", body={})[0] == 400
    assert call("POST", "/governance/gateways/missing/mode", body={"mode": "ENFORCE"})[0] == 404
    assert call("POST", "/governance/gateways/gw-1/mode", body={"mode": "ENFORCE"}, persona="developer")[0] == 403


# ---- catalogue ----------------------------------------------------------------------------------------------------------------
def test_catalogue_requires_a_registry_then_lists_searches_and_updates(fakes, monkeypatch):
    status, body = call("GET", "/catalogue")
    assert (
        status == 409 and body["error"]["code"] == "REGISTRY_NOT_CONFIGURED" and "module 4" in body["error"]["message"]
    )
    assert call("GET", "/catalogue/search", query={"q": "hr"})[0] == 409
    assert call("POST", "/catalogue/r1/status", body={"status": "APPROVED"})[0] == 409
    call("PUT", "/settings", body={"registryId": "reg-1", "registryArn": "arn:reg-1"})

    class Control:
        def __init__(self):
            self.calls = []

        def list_registry_records(self, **kwargs):
            self.calls.append(kwargs)
            if "nextToken" not in kwargs:
                return {
                    "registryRecords": [
                        {
                            "recordId": "r1",
                            "name": "hr-assistant-tools",
                            "recordType": "MCP",
                            "status": "DRAFT",
                            "createdAt": datetime(2026, 10, 8, tzinfo=UTC),
                        }
                    ],
                    "nextToken": "n1",
                }
            return {
                "registryRecords": [
                    {"recordId": "r2", "name": "kb-hr-policies", "recordType": "CUSTOM", "status": "APPROVED"}
                ]
            }

        def update_registry_record_status(self, **kwargs):
            self.calls.append(kwargs)
            return {
                "recordId": kwargs["recordId"],
                "recordArn": "arn:r",
                "status": kwargs["status"],
                "statusReason": kwargs.get("statusReason"),
                "updatedAt": datetime(2026, 10, 8, 12, tzinfo=UTC),
            }

        def get_registry_record(self, **kwargs):
            self.calls.append(kwargs)
            if kwargs["recordId"] != "r1":
                raise ClientError(
                    {"Error": {"Code": "ResourceNotFoundException", "Message": "no such record"}}, "GetRegistryRecord"
                )
            return {
                "recordId": "r1",
                "recordArn": "arn:r1",
                "name": "hr-assistant-tools",
                "recordType": "MCP",
                "status": "PENDING_APPROVAL",
                "descriptors": {"mcpServer": {"data": "{}", "dataSchemaVersion": "2025-12-11"}},
                "createdAt": datetime(2026, 10, 8, tzinfo=UTC),
            }

    class Data:
        def search_discoverable_registry_records(self, **kwargs):
            self.kwargs = kwargs
            return {"registryRecords": [{"recordId": "r2", "name": "kb-hr-policies", "status": "APPROVED"}]}

    control, data = Control(), Data()
    monkeypatch.setattr(clients, "registry_control", lambda: control)
    monkeypatch.setattr(clients, "registry_data", lambda: data)

    status, body = call("GET", "/catalogue", persona="developer")
    assert status == 200 and [r["recordId"] for r in body] == ["r1", "r2"]
    assert body[0]["createdAt"] == "2026-10-08T00:00:00+00:00" and body[0]["recordType"] == "MCP"
    assert control.calls[0] == {"registryId": "reg-1", "maxResults": 100} and control.calls[1]["nextToken"] == "n1"
    call("GET", "/catalogue", query={"status": "APPROVED"})
    assert control.calls[-2]["filters"] == [{"name": "status", "values": ["APPROVED"]}]
    assert call("GET", "/catalogue", query={"status": "WHATEVER"})[0] == 400

    status, body = call("GET", "/catalogue/search", query={"q": "policies"}, persona="approver")
    assert status == 200 and body[0]["recordId"] == "r2"
    assert data.kwargs == {"searchQuery": "policies", "registryIds": ["reg-1"], "maxResults": 20}
    assert call("GET", "/catalogue/search")[0] == 400
    status, body = call("GET", "/catalogue/search", query={"q": "x" * 257})  # the search API caps searchQuery at 256
    assert status == 400 and "256" in body["error"]["message"]

    # one record with its descriptors (the detail panel; DRAFT / PENDING_APPROVAL records are not searchable)
    status, body = call("GET", "/catalogue/r1", persona="developer")
    assert status == 200 and body["recordId"] == "r1" and body["status"] == "PENDING_APPROVAL"
    assert body["descriptors"]["mcpServer"]["dataSchemaVersion"] == "2025-12-11" and body["createdAt"].startswith(
        "2026"
    )
    assert control.calls[-1] == {"registryId": "reg-1", "recordId": "r1"}
    status, body = call("GET", "/catalogue/missing")
    assert status == 404 and body["error"]["code"] == "NOT_FOUND"

    status, body = call("POST", "/catalogue/r1/status", body={"status": "APPROVED", "reason": "reviewed in module 5"})
    assert status == 200
    assert body == {
        "recordId": "r1",
        "recordArn": "arn:r",
        "status": "APPROVED",
        "statusReason": "reviewed in module 5",
        "updatedAt": "2026-10-08T12:00:00+00:00",
    }
    assert control.calls[-1] == {
        "registryId": "reg-1",
        "recordId": "r1",
        "status": "APPROVED",
        "statusReason": "reviewed in module 5",
    }
    call("POST", "/catalogue/r1/status", body={"status": "REJECTED"})
    assert "statusReason" not in control.calls[-1]
    assert call("POST", "/catalogue/r1/status", body={"status": "DRAFT"})[0] == 400
    status, body = call(
        "POST", "/catalogue/r1/status", body={"status": "REJECTED", "reason": "r" * 256}
    )  # API limit 255
    assert status == 400 and "255" in body["error"]["message"]
    assert call("POST", "/catalogue/r1/status", body={"status": "REJECTED", "reason": "r" * 255})[0] == 200
    assert call("POST", "/catalogue/r1/status", body={"status": "APPROVED"}, persona="developer")[0] == 403


def test_registry_event_touches_the_sync_item(table):
    app = entry_module("app")
    result = app.handler(
        {
            "source": "aws.agent-registry",
            "detail-type": "Registry Record Status Change",
            "detail": {"recordId": "r1", "status": "APPROVED"},
        },
        None,
    )
    assert result == {"ok": True, "detailType": "Registry Record Status Change"}
    item = db.get({"PK": "CATALOGUE", "SK": "SYNC"})
    assert item["lastDetailType"] == "Registry Record Status Change" and item["lastDetail"]["recordId"] == "r1"
    assert app.handler({"hello": "world"}, None) == {"ok": True}
    assert app.handler("not a dict", None) == {"ok": True}


# ---- jobs (plumbing for step 2) ---------------------------------------------------------------------------------------------
def test_jobs_plumbing_and_dispatcher(table, monkeypatch):
    job = jobs.create("invoke", {"projectId": "hr-assistant", "prompt": "hi"}, "developer")
    assert job["status"] == "QUEUED" and job["type"] == "invoke" and len(job["jobId"]) == 26
    assert job["ttl"] > time.time() and job["createdBy"] == "developer"
    assert jobs.get(job["jobId"])["input"]["prompt"] == "hi" and jobs.get("missing") is None

    monkeypatch.delitem(
        jobs.HANDLERS, "invoke", raising=False
    )  # the worker registers it; here the dispatcher is under test
    result = jobs.run({"jobId": job["jobId"]})
    assert result["status"] == "FAILED" and result["error"]["code"] == "NOT_IMPLEMENTED"

    job2 = jobs.create("deploy", {"version": 1}, "developer")
    runs = []

    def fake_deploy(j):
        runs.append(j["jobId"])
        return {"harnessArn": "arn:h", "version": j["input"]["version"]}

    monkeypatch.setitem(jobs.HANDLERS, "deploy", fake_deploy)
    result = jobs.run({"jobId": job2["jobId"]})
    assert result["status"] == "SUCCEEDED" and result["output"] == {"harnessArn": "arn:h", "version": 1}
    assert jobs.get(job2["jobId"])["status"] == "SUCCEEDED"
    # a replayed event (Lambda retry, manual re-invoke) for a finished job is a no-op: the handler does not run again
    replay = jobs.run({"jobId": job2["jobId"]})
    assert replay["status"] == "SUCCEEDED" and replay["output"]["version"] == 1 and runs == [job2["jobId"]]
    jobs.set_status(job2["jobId"], "RUNNING")
    assert jobs.run({"jobId": job2["jobId"]})["status"] == "RUNNING" and runs == [job2["jobId"]]

    job3 = jobs.create("promote", {}, "approver")

    def boom(j):
        raise LiteLLMError(400, "Budget has been exceeded!", type="budget_exceeded")

    monkeypatch.setitem(jobs.HANDLERS, "promote", boom)
    result = jobs.run({"jobId": job3["jobId"]})
    assert result["status"] == "FAILED" and result["error"] == {
        "code": "UPSTREAM_ERROR",
        "message": "LiteLLM 400: Budget has been exceeded!",
    }
    # a missing job is an error of the invocation (visible in the Lambda error metric), never a silent NOT_FOUND
    with pytest.raises(errors.ApiError) as excinfo:
        jobs.run({"jobId": "missing"})
    assert excinfo.value.status == 404 and jobs.run({})["status"] == "IGNORED"
    with pytest.raises(errors.ApiError):
        jobs.create("bogus", {}, "x")

    class FakeLambda:
        def invoke(self, **kwargs):
            self.kwargs = kwargs

    fake = FakeLambda()
    monkeypatch.setattr(clients, "lambda_client", lambda: fake)
    jobs.enqueue(job)
    assert fake.kwargs["FunctionName"] == "agent-platform-worker" and fake.kwargs["InvocationType"] == "Event"
    assert json.loads(fake.kwargs["Payload"]) == {"jobId": job["jobId"]}

    worker = entry_module("worker")
    with pytest.raises(errors.ApiError):
        worker.handler({"jobId": "missing"}, None)
    assert worker.handler(None, None)["status"] == "IGNORED"


# ---- POST /me/switch (the UI's "Switch persona" menu, a workshop shortcut) -------------------------------------------------------
def fake_persona_login(calls: list):
    """A platform_sdk.auth.login double: records (persona, client id, cognito client, secrets client), returns the persona's tokens."""
    from platform_sdk import auth as sdk_auth

    groups = {"admin": ["platform-admins"], "developer": ["agent-developers"], "approver": ["approvers"]}

    def login(persona, password=None, cfg=None, cognito_client=None, secrets_client=None):
        calls.append((persona, cfg.client_id, cognito_client, secrets_client))
        claims = {
            "sub": f"sub-{persona}",
            "iss": f"https://cognito-idp.{REGION}.amazonaws.com/{CONFIG['auth/user-pool-id']}",
            "aud": cfg.client_id,
            "token_use": "id",
            "cognito:username": persona,
            "cognito:groups": groups[persona],
            "exp": 1_800_028_800,
            "iat": 1_800_000_000,
        }
        return sdk_auth.Tokens(
            id_token=f"id-token-{persona}",
            access_token=f"access-token-{persona}",
            refresh_token=f"refresh-token-{persona}",
            claims=claims,
            username=persona,
            groups=groups[persona],
            expires_in=28800,
        )

    return login


def test_switch_persona_returns_the_tokens_no_store_and_logs_the_caller(monkeypatch, caplog):
    from platform_api import router
    from platform_api.handlers import me as me_handler

    calls: list = []
    monkeypatch.setattr(me_handler.sdk_auth, "login", fake_persona_login(calls))
    with caplog.at_level("INFO", logger="platform_api.handlers.me"):
        response = router.dispatch(make_event("POST", "/me/switch", body={"persona": "developer"}, persona="admin"))
    assert response["statusCode"] == 200
    assert (
        response["headers"]["Cache-Control"] == "no-store" and response["headers"]["Content-Type"] == "application/json"
    )
    body = json.loads(response["body"])
    assert {k: v for k, v in body.items() if k != "claims"} == {
        "persona": "developer",
        "username": "developer",
        "idToken": "id-token-developer",
        "accessToken": "access-token-developer",
        "refreshToken": "refresh-token-developer",
        "expiresIn": 28800,
    }
    assert body["claims"]["cognito:username"] == "developer" and body["claims"]["cognito:groups"] == [
        "agent-developers"
    ]
    assert body["claims"]["exp"] == 1_800_028_800 and body["claims"]["sub"] == "sub-developer"
    # the SDK login ran against the SPA client with the backend's cached boto3 clients (one per container)
    (persona, client_id, cognito_client, secrets_client), *_ = calls
    assert persona == "developer" and client_id == CONFIG["auth/client-id"]
    assert cognito_client.meta.service_model.service_name == "cognito-idp"
    assert secrets_client.meta.service_model.service_name == "secretsmanager"
    assert cognito_client is clients.cognito_idp() and secrets_client is clients.secrets_manager()
    # the audit line names the caller and the persona, never a token
    assert "admin switched to developer" in caplog.text
    assert "id-token-developer" not in caplog.text and "refresh-token-developer" not in caplog.text
    # every signed-in persona may switch (ANY); no token is 401
    for caller in ("developer", "approver"):
        assert call("POST", "/me/switch", body={"persona": "admin"}, persona=caller)[0] == 200, caller
    status, body = call("POST", "/me/switch", body={"persona": "admin"}, persona=None)
    assert status == 401 and body["error"]["code"] == "UNAUTHORIZED"


def test_switch_persona_validates_the_persona_and_maps_a_refused_sign_in(monkeypatch):
    from platform_api.handlers import me as me_handler

    from platform_sdk import auth as sdk_auth

    calls: list = []
    monkeypatch.setattr(me_handler.sdk_auth, "login", fake_persona_login(calls))
    status, body = call("POST", "/me/switch", body={"persona": "root"})
    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR"
    assert "persona must be one of ['admin', 'developer', 'approver']" in body["error"]["message"]
    status, body = call("POST", "/me/switch", body={})
    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR"
    status, body = call("POST", "/me/switch")
    assert status == 400 and body["error"]["code"] == "VALIDATION_ERROR"
    assert calls == []  # nothing reached Cognito

    def refused(*args, **kwargs):
        raise sdk_auth.AuthError(
            "Cognito refused the sign-in for 'developer' (Incorrect username or password). Check the users secret"
        )

    monkeypatch.setattr(me_handler.sdk_auth, "login", refused)
    status, body = call("POST", "/me/switch", body={"persona": "developer"})
    assert status == 502 and body["error"]["code"] == "SIGN_IN_FAILED" and "Cognito refused" in body["error"]["message"]

    def corrupt(*args, **kwargs):  # a hand-edited users secret without the password key
        raise KeyError("password")

    monkeypatch.setattr(me_handler.sdk_auth, "login", corrupt)
    status, body = call("POST", "/me/switch", body={"persona": "developer"})
    assert status == 502 and body["error"]["code"] == "SIGN_IN_FAILED" and "users secret" in body["error"]["message"]

    def throttled(*args, **kwargs):
        raise ClientError({"Error": {"Code": "TooManyRequestsException", "Message": "Rate exceeded"}}, "InitiateAuth")

    monkeypatch.setattr(me_handler.sdk_auth, "login", throttled)
    status, body = call("POST", "/me/switch", body={"persona": "developer"})
    assert status == 502 and body["error"]["code"] == "UPSTREAM_ERROR" and "InitiateAuth" in body["error"]["message"]
