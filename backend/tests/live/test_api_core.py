"""Live check of the Phase 2A core routes against the deployed API (runs only with PLATFORM_LIVE=1).

    PLATFORM_LIVE=1 AWS_PROFILE=workshop uv run pytest backend/tests/live -q -s

Logs in as the three personas, exercises the public / 401 / 403 cases, then the full core lifecycle of domain `lt-ops` and project
`lt-hr-assistant` (never `hr-assistant`: the workshop command owns that one) and verifies the side effects in LiteLLM, IAM and AgentCore
Identity. Everything it creates is deleted at the end (and leftovers of a failed run are swept at the start). Latencies are printed.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import boto3
import pytest
import requests
from botocore.exceptions import ClientError
from platform_sdk.api import PlatformApiError, PlatformClient
from platform_sdk.templates import roles as role_templates

from platform_sdk import auth, config, identity, names
from platform_sdk import litellm as litellm_mod

pytestmark = pytest.mark.skipif(
    os.environ.get("PLATFORM_LIVE") != "1", reason="live API test: set PLATFORM_LIVE=1 (and AWS_PROFILE=workshop)"
)

DOMAIN, PROJECT = "lt-ops", "lt-hr-assistant"
MODEL, MODEL2 = "claude-sonnet-5", "gpt-6-1-sol"
TIMINGS: list[tuple[str, float]] = []
STATE: dict = {}


def timed(label: str, fn):
    started = time.perf_counter()
    try:
        return fn()
    finally:
        TIMINGS.append((label, round(time.perf_counter() - started, 3)))


def expect_error(fn, status: int, code: str | None = None) -> PlatformApiError:
    with pytest.raises(PlatformApiError) as excinfo:
        fn()
    assert excinfo.value.status == status, excinfo.value
    if code:
        assert excinfo.value.code == code, excinfo.value
    return excinfo.value


# ---- fixtures ---------------------------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def cfg():
    return config.load()


@pytest.fixture(scope="module")
def personas(cfg):
    return {p: timed(f"Cognito login ({p})", lambda p=p: auth.login(p, cfg=cfg)) for p in auth.PERSONAS}


@pytest.fixture(scope="module")
def api(cfg, personas):
    clients = {p: PlatformClient(cfg, t.id_token, timeout=60) for p, t in personas.items()}
    clients["anon"] = PlatformClient(cfg, None, timeout=60)
    return clients


@pytest.fixture(scope="module")
def aws(cfg):
    session = boto3.Session(region_name=cfg.region)
    secret = session.client("secretsmanager").get_secret_value(SecretId=cfg.model_gateway_master_key_secret_arn)
    return SimpleNamespace(
        iam=session.client("iam"),
        litellm=litellm_mod.LiteLLMAdmin(cfg.model_gateway_url, secret["SecretString"]),
    )


@pytest.fixture(scope="module", autouse=True)
def clean_slate(api, aws, cfg):
    """Remove leftovers of a previous failed run first, and everything this run created afterwards."""

    def sweep():
        for fn in (
            lambda: api["admin"].delete_project(PROJECT, force=True),
            lambda: api["admin"].delete_domain(DOMAIN),
        ):
            try:
                fn()
            except PlatformApiError as exc:
                if exc.status != 404:
                    raise
        for env in names.ENVIRONMENTS:
            n = names.ProjectNames(PROJECT, env)
            for model in (MODEL, MODEL2):
                key = aws.litellm.find_key(n.key_alias(model))
                if key:
                    aws.litellm.delete_key(key.get("token") or key.get("token_id"))
                identity.delete_provider(n.credential_provider(model), region=cfg.region)
            role_templates.delete_project_roles(cfg, PROJECT, env, iam=aws.iam)
        if aws.litellm.find_team(PROJECT):
            aws.litellm.delete_team(PROJECT)

    sweep()
    yield
    sweep()
    print("\nlatencies (seconds):")
    for label, seconds in TIMINGS:
        print(f"  {label:<48} {seconds:8.3f}")


# ---- public, auth, matrix -----------------------------------------------------------------------------------------------------
def test_health_is_public(cfg):
    response = timed("GET /health (no token, first call)", lambda: requests.get(cfg.api_url + "/health", timeout=30))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok" and body["version"] and body["time"].endswith("Z")
    assert set(body) == {"status", "version", "time"}
    timed("GET /health (no token, warm)", lambda: requests.get(cfg.api_url + "/health", timeout=30))


def test_routes_need_a_token(api):
    expect_error(api["anon"].me, 401)
    expect_error(api["anon"].list_projects, 401)


def test_me_shows_the_role(api):
    expected = {
        "admin": ("admin", "platform-admins"),
        "developer": ("developer", "agent-developers"),
        "approver": ("approver", "approvers"),
    }
    for persona, (role, group) in expected.items():
        me = timed(f"GET /me ({persona})", api[persona].me)
        assert me["role"] == role and group in me["groups"], me
        assert me["username"] == persona and me["email"] == f"{persona}@anycompany.example", me


def test_forbidden_cases(api):
    expect_error(lambda: api["developer"].put_settings(defaultLimits={"maxTokens": 4000}), 403, "FORBIDDEN")
    expect_error(
        lambda: api["approver"].create_project(projectId=PROJECT, domainId=DOMAIN, models=[MODEL]), 403, "FORBIDDEN"
    )
    expect_error(lambda: api["developer"].create_domain(DOMAIN, "x"), 403, "FORBIDDEN")
    expect_error(lambda: api["approver"].set_gateway_mode("gw", "ENFORCE"), 403, "FORBIDDEN")
    expect_error(lambda: api["anon"].get_settings(), 401)


def test_settings_get_and_put(api):
    before = timed("GET /settings", api["admin"].get_settings)
    assert before["defaultProjectCap"]["budgetDuration"] == "7d" and before["defaultKeyCap"]["maxBudget"] == 1
    after = timed(
        "PUT /settings (defaultLimits only)", lambda: api["admin"].put_settings(defaultLimits=before["defaultLimits"])
    )
    assert after["defaultLimits"] == before["defaultLimits"] and after["updatedBy"] == "admin"
    for key in ("registryId", "registryArn", "policyEngineId", "policyEngineArn"):
        assert after[key] == before[key], key  # never overwrite what the modules stored
    assert api["developer"].get_settings()["defaultLimits"] == before["defaultLimits"]
    STATE["settings"] = after


# ---- core lifecycle -----------------------------------------------------------------------------------------------------------
def test_create_domain(api):
    domain = timed(
        "POST /domains",
        lambda: api["admin"].create_domain(DOMAIN, "Lifecycle test ops", "backend live test", "backend-test"),
    )
    assert domain["domainId"] == DOMAIN and domain["name"] == "Lifecycle test ops" and domain["createdBy"] == "admin"
    expect_error(lambda: api["admin"].create_domain(DOMAIN, "again"), 409, "CONFLICT")
    assert timed("GET /domains/{id}", lambda: api["developer"].get_domain(DOMAIN))["projectCount"] == 0
    assert DOMAIN in [d["domainId"] for d in timed("GET /domains", api["approver"].list_domains)]


def test_create_project_provisions_team_and_roles(api, aws, cfg):
    body = {
        "projectId": PROJECT,
        "domainId": DOMAIN,
        "name": "Lifecycle test HR assistant",
        "description": "created by backend/tests/live/test_api_core.py",
        "models": [MODEL],
        "maxBudget": 2,
        "budgetDuration": "7d",
        "tpmLimit": 100000,
        "rpmLimit": 100,
        "allowedToolArns": [cfg.hr_tools_lambda_arn],
        "knowledgeBaseIds": [cfg.kb_id],
    }
    project = timed("POST /projects (team + 4 roles)", lambda: api["admin"].create_project(body))
    assert project["projectId"] == PROJECT and project["status"] == "ACTIVE" and project["createdBy"] == "admin"
    assert project["litellm"] == {
        "teamId": PROJECT,
        "teamAlias": PROJECT,
        "maxBudget": 2,
        "budgetDuration": "7d",
        "tpmLimit": 100000,
        "rpmLimit": 100,
        "models": [MODEL],
    }
    assert project["allowedToolArns"] == [cfg.hr_tools_lambda_arn] and project["knowledgeBaseIds"] == [cfg.kb_id]
    for env in names.ENVIRONMENTS:
        n = names.ProjectNames(PROJECT, env)
        assert project["roles"][env] == {
            "agentRoleArn": n.agent_role_arn(cfg.account_id),
            "gatewayRoleArn": n.gateway_role_arn(cfg.account_id),
        }
        for role_name in (n.agent_role, n.gateway_role):
            aws.iam.get_role(RoleName=role_name)
            tags = {t["Key"]: t["Value"] for t in aws.iam.list_role_tags(RoleName=role_name)["Tags"]}
            assert tags["ManagedBy"] == "platform" and tags["ProjectId"] == PROJECT and tags["Environment"] == env
            assert "agent-platform-project" in aws.iam.list_role_policies(RoleName=role_name)["PolicyNames"]
    info = aws.litellm.get_team(PROJECT)["team_info"]
    assert info["team_alias"] == PROJECT and info["models"] == [MODEL] and float(info["max_budget"]) == 2.0
    assert info["budget_duration"] == "7d" and info["tpm_limit"] == 100000 and info["rpm_limit"] == 100
    assert (info.get("metadata") or {}).get("project") == PROJECT and (info.get("metadata") or {}).get(
        "domain"
    ) == DOMAIN

    assert timed("GET /projects/{id}", lambda: api["developer"].get_project(PROJECT)) == project
    assert [
        p["projectId"] for p in timed("GET /projects?domainId=", lambda: api["approver"].list_projects(DOMAIN))
    ] == [PROJECT]
    expect_error(lambda: api["admin"].create_project(body), 409, "CONFLICT")
    expect_error(lambda: api["admin"].delete_domain(DOMAIN), 409, "CONFLICT")
    expect_error(
        lambda: api["admin"].create_project({**body, "projectId": "lt-bad", "models": ["gpt-4"]}),
        400,
        "VALIDATION_ERROR",
    )
    STATE["project"] = project


def test_patch_project_updates_the_team(api, aws):
    updated = timed(
        "PATCH /projects/{id} (caps + models)",
        lambda: api["admin"].update_project(PROJECT, maxBudget=3, models=[MODEL, MODEL2], rpmLimit=120),
    )
    assert updated["litellm"]["maxBudget"] == 3 and updated["litellm"]["models"] == [MODEL, MODEL2]
    assert updated["litellm"]["rpmLimit"] == 120 and updated["litellm"]["tpmLimit"] == 100000
    info = aws.litellm.get_team(PROJECT)["team_info"]
    assert float(info["max_budget"]) == 3.0 and sorted(info["models"]) == sorted([MODEL, MODEL2])
    assert info["rpm_limit"] == 120 and info["tpm_limit"] == 100000
    expect_error(lambda: api["developer"].update_project(PROJECT, maxBudget=1), 403, "FORBIDDEN")


def test_resources_upsert_list_delete(api, cfg):
    dev = api["developer"]
    gw_name, gw_id = (
        f"{PROJECT}-dev-gw",
        f"{PROJECT}-dev-gw-livetest00",
    )  # well-formed id (10-char suffix) that does not exist
    gw_arn = f"arn:aws:bedrock-agentcore:{cfg.region}:{cfg.account_id}:gateway/{gw_id}"
    first = timed(
        "PUT /projects/{id}/resources (gateway)",
        lambda: dev.register_resource(
            PROJECT, "dev", "gateway", gw_name, arn=gw_arn, id=gw_id, url="https://example.invalid/mcp"
        ),
    )
    assert first["registeredBy"] == "developer" and first["arn"] == gw_arn and first["id"] == gw_id
    target = dev.register_resource(
        PROJECT, "dev", "gateway_target", "hrkb", id="T1", attributes={"kind": "knowledge_base", "gatewayArn": gw_arn}
    )
    assert target["arn"] is None and target["attributes"]["kind"] == "knowledge_base"
    memory_name = f"{names.snake(PROJECT)}_prod_memory"
    dev.register_resource(
        PROJECT,
        "prod",
        "memory",
        memory_name,
        arn=f"arn:aws:bedrock-agentcore:{cfg.region}:{cfg.account_id}:memory/{memory_name}-x",
    )
    rows = timed("GET /projects/{id}/resources?env=dev", lambda: api["approver"].list_resources(PROJECT, "dev"))
    assert [(r["type"], r["name"]) for r in rows] == [("gateway", gw_name), ("gateway_target", "hrkb")]
    assert len(api["admin"].list_resources(PROJECT)) == 3
    expect_error(lambda: api["approver"].register_resource(PROJECT, "dev", "tool", "x", arn="arn:x"), 403, "FORBIDDEN")
    expect_error(lambda: dev.register_resource(PROJECT, "dev", "widget", "x", arn="arn:x"), 400, "VALIDATION_ERROR")
    timed("DELETE /projects/{id}/resources/...", lambda: dev.delete_resource(PROJECT, "prod", "memory", memory_name))
    assert len(api["admin"].list_resources(PROJECT)) == 2
    expect_error(lambda: dev.delete_resource(PROJECT, "prod", "memory", memory_name), 404, "NOT_FOUND")


def test_keys_create_and_patch(api, aws, cfg):
    key = timed(
        "POST /projects/{id}/keys (LiteLLM key + provider)", lambda: api["developer"].create_key(PROJECT, "dev", MODEL)
    )
    n = names.ProjectNames(PROJECT, "dev")
    assert key["alias"] == n.key_alias(MODEL) == f"{PROJECT}-dev-{MODEL}"
    assert key["credentialProviderName"] == n.credential_provider(MODEL) == f"litellm-{PROJECT}-dev-{MODEL}"
    assert (
        key["status"] == "active"
        and key["maxBudget"] == 1
        and key["budgetDuration"] == "7d"
        and key["adopted"] is False
    )
    assert "key" not in key and not str(key["litellmKeyId"]).startswith("sk-")
    info = aws.litellm.key_info(key["litellmKeyId"])
    assert info["key_alias"] == key["alias"] and info["team_id"] == PROJECT and info["models"] == [MODEL]
    assert float(info["max_budget"]) == 1.0 and (info.get("metadata") or {}).get("agent") == n.harness
    assert identity.get_provider_arn(n.credential_provider(MODEL), region=cfg.region) == key["credentialProviderArn"]
    expect_error(lambda: api["developer"].create_key(PROJECT, "dev", MODEL), 409, "CONFLICT")
    expect_error(lambda: api["developer"].create_key(PROJECT, "dev", "nova-2-lite"), 400, "VALIDATION_ERROR")

    suspended = timed(
        "PATCH keys (suspend)", lambda: api["admin"].update_key(PROJECT, "dev", MODEL, status="suspended")
    )
    assert suspended["status"] == "suspended"
    assert aws.litellm.key_info(key["litellmKeyId"])["blocked"] is True
    raised = timed(
        "PATCH keys (raise cap + resume)",
        lambda: api["admin"].update_key(PROJECT, "dev", MODEL, max_budget=2.5, status="active"),
    )
    assert raised["status"] == "active" and raised["maxBudget"] == 2.5
    info = aws.litellm.key_info(key["litellmKeyId"])
    assert float(info["max_budget"]) == 2.5 and not info["blocked"]
    expect_error(lambda: api["developer"].update_key(PROJECT, "dev", MODEL, max_budget=1), 403, "FORBIDDEN")
    STATE["dev_key"] = key


def test_keys_adopt_a_hand_made_key(api, aws, cfg):
    n = names.ProjectNames(PROJECT, "prod")
    made = aws.litellm.create_service_account_key(
        PROJECT,
        n.key_alias(MODEL2),
        [MODEL2],
        0.5,
        "7d",
        {"domain": DOMAIN, "project": PROJECT, "agent": n.harness, "env": "prod", "model": MODEL2},
    )
    provider_arn = identity.store_api_key(
        n.credential_provider(MODEL2), made["key"], tags={"ManagedBy": "dryrun"}, region=cfg.region
    )
    adopted = timed(
        "POST keys (adopt)",
        lambda: api["developer"].create_key(
            PROJECT,
            "prod",
            MODEL2,
            adopt={
                "litellmKeyId": made["token"],
                "credentialProviderArn": provider_arn,
                "maxBudget": 0.5,
                "budgetDuration": "7d",
            },
        ),
    )
    assert adopted["adopted"] is True and adopted["litellmKeyId"] == made["token"]
    assert adopted["credentialProviderName"] == n.credential_provider(MODEL2) and adopted["maxBudget"] == 0.5
    keys = timed("GET /projects/{id}/keys", lambda: api["approver"].list_keys(PROJECT))
    assert sorted(k["alias"] for k in keys) == sorted([f"{PROJECT}-dev-{MODEL}", n.key_alias(MODEL2)])


def test_spend_shows_the_team_and_keys(api):
    spend = timed("GET /spend", lambda: api["developer"].spend(PROJECT))
    assert spend["team"]["teamId"] == PROJECT and float(spend["team"]["maxBudget"]) == 3.0
    assert len(spend["team"]["keys"]) == 2 and isinstance(spend["logs"], list)
    rows = {k["alias"]: k for k in spend["keys"]}
    dev_row = rows[f"{PROJECT}-dev-{MODEL}"]
    assert dev_row["blocked"] is False and float(dev_row["spend"]) == 0.0 and float(dev_row["maxBudget"]) == 2.5
    assert dev_row["budgetResetAt"]
    assert [k["env"] for k in api["developer"].spend(PROJECT, "prod")["keys"]] == ["prod"]


def test_governance(api, cfg):
    gov = timed("GET /governance", lambda: api["approver"].governance())
    assert {m["alias"] for m in gov["models"]} >= set(cfg.model_aliases)
    live_models = [m for m in gov["models"] if m["source"] == "litellm"]
    assert live_models, "LiteLLM /model/info did not answer"
    assert all(m["guardrail"] == {"id": cfg.guardrail_id, "version": str(cfg.guardrail_version)} for m in live_models)
    assert gov["guardrail"]["id"] == cfg.guardrail_id and str(gov["guardrail"]["version"]) == str(cfg.guardrail_version)
    assert gov["guardrail"]["name"] == "hr-baseline" and gov["guardrail"]["status"] == "READY"
    assert "PROMPT_ATTACK" in {f["type"] for f in gov["guardrail"]["summary"]["contentFilters"]}
    assert set(gov["policyEngine"]) == {"id", "arn"} and set(gov["registry"]) == {"id", "arn"}
    mine = [g for g in gov["gateways"] if g["projectId"] == PROJECT]
    assert (
        len(mine) == 1 and mine[0]["env"] == "dev" and mine[0]["status"] == "NOT_FOUND"
    )  # fake registration, no gateway
    STATE["governance"] = gov


def test_catalogue(api):
    if STATE["settings"].get("registryId"):
        rows = timed("GET /catalogue", api["developer"].list_catalogue)
        assert isinstance(rows, list)
        found = timed("GET /catalogue/search?q=hr", lambda: api["developer"].search_catalogue("hr"))
        assert isinstance(found, list)
    else:
        timed(
            "GET /catalogue (no registry -> 409)",
            lambda: expect_error(api["developer"].list_catalogue, 409, "REGISTRY_NOT_CONFIGURED"),
        )
        expect_error(lambda: api["developer"].search_catalogue("hr"), 409, "REGISTRY_NOT_CONFIGURED")
    expect_error(lambda: api["developer"].set_catalogue_status("some-record", "APPROVED"), 403, "FORBIDDEN")


def test_delete_project_removes_team_keys_providers_roles(api, aws, cfg):
    summary = timed("DELETE /projects/{id}", lambda: api["admin"].delete_project(PROJECT))
    assert (
        summary["deleted"] == PROJECT
        and summary["team"] is True
        and len(summary["roles"]) == 4
        and len(summary["keys"]) == 2
    )
    assert aws.litellm.find_team(PROJECT) is None
    for env, model in (("dev", MODEL), ("prod", MODEL2)):
        n = names.ProjectNames(PROJECT, env)
        assert aws.litellm.find_key(n.key_alias(model)) is None
        assert identity.get_provider_arn(n.credential_provider(model), region=cfg.region) is None
        for role_name in (n.agent_role, n.gateway_role):
            with pytest.raises(ClientError) as excinfo:
                aws.iam.get_role(RoleName=role_name)
            assert excinfo.value.response["Error"]["Code"] == "NoSuchEntity"
    expect_error(lambda: api["admin"].get_project(PROJECT), 404, "NOT_FOUND")
    expect_error(lambda: api["admin"].list_resources(PROJECT), 404, "NOT_FOUND")
    expect_error(lambda: api["admin"].list_keys(PROJECT), 404, "NOT_FOUND")


def test_delete_domain(api):
    assert timed("DELETE /domains/{id}", lambda: api["admin"].delete_domain(DOMAIN)) == {"deleted": DOMAIN}
    expect_error(lambda: api["admin"].get_domain(DOMAIN), 404, "NOT_FOUND")
