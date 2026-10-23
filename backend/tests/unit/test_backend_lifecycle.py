"""Step-2 routes and worker jobs through `router.dispatch` / `jobs.run` with moto DynamoDB, the in-memory LiteLLM proxy and
fakes for the AgentCore calls (`platform_sdk.agentcore` functions are monkeypatched; the render goes through the real SDK)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from backend_testlib import ACCOUNT, CONFIG, PRIVATE_CONFIG, REGION, call, entry_module, register
from platform_api import db, jobs
from platform_api.errors import ApiError
from platform_api.handlers import releases as releases_handler
from platform_api.services import clients
from platform_api.services import metrics as metrics_service
from platform_sdk.agentcore import HarnessInvokeError

from platform_sdk import agentcore, blueprint

ROOT = Path(__file__).resolve().parents[3]
BLUEPRINT_FILE = ROOT / "scenario" / "blueprint" / "blueprint.yaml"
HR_LAMBDA = CONFIG["scenario/tools/hr-lambda-arn"]
# Scores of the real dry run (dryrun-results/hr-assistant-v1-gpt-6-1-sol.json): prompt v1 fails three thresholds
BLOCKED_SCORES = {
    "Correctness": {"mean": 0.7, "min": 0.0, "max": 1.0, "passRate": 0.7, "count": 10, "errors": 0, "failed": 0},
    "GoalSuccessRate": {"mean": 0.7, "min": 0.0, "max": 1.0, "passRate": 0.7, "count": 10, "errors": 0, "failed": 0},
    "TrajectoryInOrderMatch": {
        "mean": 0.625,
        "min": 0.0,
        "max": 1.0,
        "passRate": 0.625,
        "count": 8,
        "errors": 2,
        "failed": 0,
    },
    "Harmfulness": {"mean": 1.0, "min": 1.0, "max": 1.0, "passRate": 1.0, "count": 10, "errors": 0, "failed": 0},
}
PASSING_SCORES = {name: {**entry, "mean": 1.0, "min": 1.0, "passRate": 1.0} for name, entry in BLOCKED_SCORES.items()}


def evaluation_body(scores, failed=None):
    return {
        "runId": "run-test",
        "datasetName": "hr-assistant.json",
        "scenarioCount": 10,
        "scores": scores,
        "results": [
            {
                "scenarioId": "leave-balance-e1001",
                "sessionId": "leave-balance-e1001-abc",
                "status": "COMPLETED",
                "scores": {"Correctness": 1.0},
                "labels": {"Correctness": "Correct"},
                "error": None,
                "errors": {},
            }
        ],
        "failedScenarios": failed or [],
    }


def create_blueprint(ready, version_hint=1, persona="developer", **overrides):
    body = ready.blueprint.to_api_body(notes=f"test v{version_hint}")
    body.update(overrides)
    status, item = call("POST", "/projects/hr-assistant/blueprints", body=body, persona=persona)
    assert status in (200, 201), item
    return item


def run_job(job_id):
    result = jobs.run({"jobId": job_id})
    return result


def deploy(ready, version=1):
    status, accepted = call(
        "POST", "/projects/hr-assistant/deploy", body={"version": version, "env": "dev"}, persona="developer"
    )
    assert status == 202, accepted
    result = run_job(accepted["jobId"])
    assert result["status"] == "SUCCEEDED", result
    return result


def pending_release(ready, version=1, scores=None, failed=()):
    """A release run through the release pipeline's DeployDev and Evaluate actions (the dataset runner replaced)."""
    from backend_testlib import run_summary

    ready.dataset_run.result = run_summary(scores or PASSING_SCORES, failed)
    status, release = call(
        "POST", "/projects/hr-assistant/releases", body={"blueprintVersion": version}, persona="developer"
    )
    assert status == 201, release
    ready.pipeline.advance()
    return call("GET", f"/releases/{release['releaseId']}")[1]


def approved_release(ready, version=1):
    release = pending_release(ready, version)
    assert release["status"] == "PENDING_APPROVAL", release
    status, release = call(
        "POST", f"/releases/{release['releaseId']}/approve", body={"comment": "ok"}, persona="approver"
    )
    assert status == 200 and release["status"] == "APPROVED", release
    ready.pipeline.advance()  # on to the promotion gate
    return release


def promoted_release(ready, version):
    release = approved_release(ready, version)
    status, accepted = call("POST", f"/releases/{release['releaseId']}/promote", persona="approver")
    assert status == 202, accepted
    ready.pipeline.advance()
    result = jobs.get(accepted["jobId"])
    assert result["status"] == "SUCCEEDED", result
    return call("GET", f"/releases/{release['releaseId']}")[1], result


# ---- blueprints ---------------------------------------------------------------------------------------------------------------
def test_blueprint_versions_create_dedupe_list_get(ready):
    first = create_blueprint(ready)
    assert first["version"] == 1 and first["model"] == "claude-sonnet-5" and first["createdBy"] == "developer"
    assert first["thresholds"] == {
        "Correctness": 0.8,
        "GoalSuccessRate": 0.9,
        "TrajectoryInOrderMatch": 0.9,
        "Harmfulness": 0,
    }
    assert first["spec"]["model"] == {"ref": "model/claude-sonnet-5"} and "system_prompt" not in first["spec"]
    assert first["systemPrompt"] == ready.blueprint.system_prompt and "PK" not in first
    status, again = call("POST", "/projects/hr-assistant/blueprints", body=ready.blueprint.to_api_body("again"))
    assert status == 200 and again["version"] == 1 and again["existing"] is True  # same content -> same version
    second = create_blueprint(
        ready, 2, persona="admin", systemPrompt=ready.blueprint.system_prompt + "\nSearch the policies first."
    )
    assert second["version"] == 2 and second["createdBy"] == "admin"
    status, body = call("GET", "/projects/hr-assistant/blueprints", persona="approver")
    assert status == 200 and [b["version"] for b in body] == [1, 2]
    assert call("GET", "/projects/hr-assistant/blueprints/2")[1]["version"] == 2
    assert call("GET", "/projects/hr-assistant/blueprints/0001")[1]["version"] == 1
    assert call("GET", "/projects/hr-assistant/blueprints/7")[0] == 404
    assert call("GET", "/projects/hr-assistant/blueprints/x")[0] == 400
    assert call("GET", "/projects/nope/blueprints")[0] == 404

    spec = json.loads(json.dumps(ready.blueprint.api_spec()))
    spec.pop("model")
    status, body = call("POST", "/projects/hr-assistant/blueprints", body={"spec": spec, "systemPrompt": "x"})
    assert (
        status == 400 and body["error"]["code"] == "BLUEPRINT_INVALID" and "spec.model.ref" in body["error"]["message"]
    )
    spec = json.loads(json.dumps(ready.blueprint.api_spec()))
    spec["model"] = {"ref": "model/nova-2-lite"}  # approved, but not enabled for the project
    status, body = call("POST", "/projects/hr-assistant/blueprints", body={"spec": spec, "systemPrompt": "x"})
    assert status == 400 and "not enabled for project" in body["error"]["message"]
    spec["model"] = {"ref": "model/gpt-4"}
    status, body = call("POST", "/projects/hr-assistant/blueprints", body={"spec": spec, "systemPrompt": "x"})
    assert status == 400 and "not an approved model" in body["error"]["message"]
    assert call("POST", "/projects/hr-assistant/blueprints", body={"spec": "x", "systemPrompt": "x"})[0] == 400
    assert call("POST", "/projects/hr-assistant/blueprints", body={"spec": spec})[0] == 400
    assert (
        call("POST", "/projects/hr-assistant/blueprints", body=ready.blueprint.to_api_body(), persona="approver")[0]
        == 403
    )
    assert len(call("GET", "/projects/hr-assistant/blueprints")[1]) == 2


def test_render_dry_run_is_unresolved_without_a_key_then_renders(ready):
    create_blueprint(ready)
    status, body = call("GET", "/projects/hr-assistant/blueprints/1/render", query={"env": "dev"}, persona="approver")
    assert status == 400 and body["error"]["code"] == "BLUEPRINT_UNRESOLVED", body
    assert (
        "module 5" in body["error"]["message"]
        and "litellm-hr-assistant-dev-claude-sonnet-5" in body["error"]["message"]
    )
    assert body["error"]["details"]["missing"] == "key"

    status, key = call("POST", "/projects/hr-assistant/keys", body={"env": "dev", "model": "claude-sonnet-5"})
    assert status == 201
    status, spec = call("GET", "/projects/hr-assistant/blueprints/1/render", persona="developer")  # env defaults to dev
    assert status == 200, spec
    assert spec["harnessName"] == "hr_assistant_dev"
    assert spec["executionRoleArn"] == ready.project["roles"]["dev"]["agentRoleArn"]
    assert spec["model"]["liteLlmModelConfig"] == {
        "modelId": "openai/claude-sonnet-5",
        "apiBase": "https://llm.example.test/v1",
        "apiKeyArn": key["credentialProviderArn"],
        "maxTokens": 2048,
    }
    assert spec["tools"][0]["config"]["agentCoreGateway"]["gatewayArn"].endswith("hr-assistant-dev-gw-abcdefghij")
    assert spec["allowedTools"] == ["@gateway"] and spec["memory"]["agentCoreMemoryConfiguration"]["arn"].endswith(
        "dev_memory-xyz"
    )
    assert (spec["maxIterations"], spec["maxTokens"], spec["timeoutSeconds"]) == (30, 4000, 120)
    assert spec["tags"] == {
        "Project": "workshop-agent-platform",
        "ManagedBy": "platform",
        "ProjectId": "hr-assistant",
        "Environment": "dev",
        "Domain": "people-ops",
        "BlueprintVersion": "1",
        "auto-delete": "no",
    }
    assert spec["systemPrompt"] == [{"text": ready.blueprint.system_prompt}]

    # prod: the key is missing too, but the registered credential_provider resource is the fallback (module 5 registers it)
    register(
        "hr-assistant",
        "prod",
        "credential_provider",
        "litellm-hr-assistant-prod-claude-sonnet-5",
        arn="arn:provider-prod",
        attributes={"model": "claude-sonnet-5"},
    )
    status, spec = call("GET", "/projects/hr-assistant/blueprints/1/render", query={"env": "prod"})
    assert status == 200 and spec["model"]["liteLlmModelConfig"]["apiKeyArn"] == "arn:provider-prod"
    assert spec["harnessName"] == "hr_assistant_prod" and spec["executionRoleArn"].endswith("prod-agent")
    call("DELETE", "/projects/hr-assistant/resources/prod/memory/hr_assistant_prod_memory")
    status, body = call("GET", "/projects/hr-assistant/blueprints/1/render", query={"env": "prod"})
    assert status == 400 and body["error"]["code"] == "BLUEPRINT_UNRESOLVED"
    assert body["error"]["details"]["missing"] == "memory" and "module 6" in body["error"]["message"]
    assert call("GET", "/projects/hr-assistant/blueprints/1/render", query={"env": "qa"})[0] == 400
    assert call("GET", "/projects/hr-assistant/blueprints/9/render")[0] == 404


def test_render_puts_the_harness_in_the_vpc_on_a_private_landing_zone(ready, monkeypatch):
    """Tier 3: the Network stack publishes the harness subnets and security group; the backend renders through the real SDK with
    `clients.cfg()`, so the CreateHarness payload carries the VPC environment block and the UpdateHarness one keeps it."""
    from platform_sdk import config as sdk_config

    create_blueprint(ready)
    assert call("POST", "/projects/hr-assistant/keys", body={"env": "dev", "model": "claude-sonnet-5"})[0] == 201
    status, public = call("GET", "/projects/hr-assistant/blueprints/1/render")
    assert status == 200 and "environment" not in public  # the public landing zone: managed network
    monkeypatch.setenv("PLATFORM_CONFIG_JSON", json.dumps(PRIVATE_CONFIG))
    sdk_config.reset_cache()
    clients.cfg.cache_clear()
    try:
        status, spec = call("GET", "/projects/hr-assistant/blueprints/1/render")
        assert status == 200, spec
        assert spec["environment"] == {
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
        assert spec["model"]["liteLlmModelConfig"]["apiBase"] == "https://llm.example.test/v1"
        update = blueprint.for_update({k: v for k, v in spec.items() if k != "tags"})
        assert update["environment"] == spec["environment"] and "harnessName" not in update
    finally:
        sdk_config.reset_cache()
        clients.cfg.cache_clear()


# ---- deploy job ---------------------------------------------------------------------------------------------------------------
def test_deploy_job_ensures_the_key_deploys_and_registers(ready):
    create_blueprint(ready)
    status, accepted = call(
        "POST", "/projects/hr-assistant/deploy", body={"version": 1, "env": "dev"}, persona="developer"
    )
    assert status == 202, accepted
    assert accepted["status"] == "QUEUED" and accepted["type"] == "deploy" and accepted["version"] == 1
    assert ready.lambda_client.payloads[-1]["FunctionName"] == "agent-platform-worker"
    assert ready.lambda_client.payloads[-1]["InvocationType"] == "Event"
    assert ready.lambda_client.payloads[-1]["Payload"] == {"jobId": accepted["jobId"]}
    job = call("GET", f"/jobs/{accepted['jobId']}", persona="approver")[1]
    assert job["status"] == "QUEUED" and job["input"] == {
        "projectId": "hr-assistant",
        "env": "dev",
        "version": 1,
        "model": "claude-sonnet-5",
    }

    result = run_job(accepted["jobId"])
    assert result["status"] == "SUCCEEDED", result
    out = result["output"]
    assert out["harnessVersion"] == "1" and out["endpoint"] == "DEFAULT" and out["keyCreated"] is True
    assert (
        out["harnessArn"].endswith("harness/hr_assistant_dev-ABC123") and out["harnessId"] == "hr_assistant_dev-ABC123"
    )
    assert out["logGroup"] == "/aws/bedrock-agentcore/runtimes/harness_hr_assistant_dev-RT123-DEFAULT"
    assert (
        out["keyAlias"] == "hr-assistant-dev-claude-sonnet-5" and out["seconds"] >= 0 and out["blueprintVersion"] == 1
    )
    assert call("GET", f"/jobs/{accepted['jobId']}")[1]["output"] == out

    # the key: LiteLLM service-account key with the blueprint's budget (1.0 USD / 7d), provider in Identity, KEY item
    key_rows = [k for k in ready.fakes.litellm.keys.values() if k["key_alias"] == "hr-assistant-dev-claude-sonnet-5"]
    assert len(key_rows) == 1 and key_rows[0]["max_budget"] == 1.0 and key_rows[0]["budget_duration"] == "7d"
    assert key_rows[0]["team_id"] == "hr-assistant" and key_rows[0]["models"] == ["claude-sonnet-5"]
    assert "litellm-hr-assistant-dev-claude-sonnet-5" in ready.fakes.identity.providers
    keys = call("GET", "/projects/hr-assistant/keys")[1]
    assert [k["alias"] for k in keys] == ["hr-assistant-dev-claude-sonnet-5"] and keys[0]["createdBy"] == "developer"

    # the harness call used the rendered spec and the platform tags
    kind, name, spec, tags, region = ready.agentcore.calls[-1]
    assert kind == "harness" and name == "hr_assistant_dev" and region == REGION
    assert spec["model"]["liteLlmModelConfig"]["apiKeyArn"] == keys[0]["credentialProviderArn"]
    assert tags == {
        "Project": "workshop-agent-platform",
        "ManagedBy": "platform",
        "ProjectId": "hr-assistant",
        "Environment": "dev",
        "Domain": "people-ops",
        "BlueprintVersion": "1",
        "auto-delete": "no",
    }

    # registered resources: harness, harness_endpoint DEFAULT, credential_provider
    rows = {(r["type"], r["name"]): r for r in call("GET", "/projects/hr-assistant/resources", query={"env": "dev"})[1]}
    harness = rows[("harness", "hr_assistant_dev")]
    assert (
        harness["arn"] == out["harnessArn"]
        and harness["id"] == out["harnessId"]
        and harness["registeredBy"] == "developer"
    )
    assert harness["attributes"]["version"] == "1" and harness["attributes"]["blueprintVersion"] == 1
    assert harness["attributes"]["model"] == "claude-sonnet-5" and harness["attributes"]["logGroup"] == out["logGroup"]
    assert (
        harness["attributes"]["jobId"] == accepted["jobId"]
        and harness["attributes"]["runtimeId"] == "harness_hr_assistant_dev-RT123"
    )
    endpoint = rows[("harness_endpoint", "DEFAULT")]
    assert endpoint["arn"].endswith("/endpoint/DEFAULT") and endpoint["attributes"]["qualifier"] is None
    assert (
        endpoint["attributes"]["version"] == "1"
        and endpoint["attributes"]["serviceName"] == "harness_hr_assistant_dev.DEFAULT"
    )
    provider = rows[("credential_provider", "litellm-hr-assistant-dev-claude-sonnet-5")]
    assert provider["arn"] == keys[0]["credentialProviderArn"] and provider["attributes"]["model"] == "claude-sonnet-5"

    # second deploy (new blueprint version): UpdateHarness -> version 2, key reused
    create_blueprint(ready, 2, systemPrompt="v2 prompt: search the policy knowledge base first.")
    result = deploy(ready, 2)
    assert result["output"]["harnessVersion"] == "2" and result["output"]["keyCreated"] is False
    rows = {(r["type"], r["name"]): r for r in call("GET", "/projects/hr-assistant/resources", query={"env": "dev"})[1]}
    assert rows[("harness", "hr_assistant_dev")]["attributes"]["version"] == "2"
    assert rows[("harness", "hr_assistant_dev")]["attributes"]["blueprintVersion"] == 2
    assert "updatedAt" in rows[("harness", "hr_assistant_dev")]
    assert len([k for k in ready.fakes.litellm.keys.values()]) == 1

    assert call("POST", "/projects/hr-assistant/deploy", body={"version": 1, "env": "prod"})[0] == 400
    assert call("POST", "/projects/hr-assistant/deploy", body={"version": 9})[0] == 404
    assert call("POST", "/projects/hr-assistant/deploy", body={"version": "one"})[0] == 400
    assert call("POST", "/projects/hr-assistant/deploy", body={"version": 1}, persona="approver")[0] == 403
    assert call("POST", "/projects/nope/deploy", body={"version": 1})[0] == 404
    assert call("GET", "/jobs/missing")[0] == 404


def test_deploy_job_fails_with_an_unresolved_blueprint(ready):
    create_blueprint(ready)
    call("DELETE", "/projects/hr-assistant/resources/dev/gateway/hr-assistant-dev-gw")
    status, accepted = call("POST", "/projects/hr-assistant/deploy", body={"version": 1}, persona="developer")
    assert status == 202
    result = run_job(accepted["jobId"])
    assert result["status"] == "FAILED" and result["error"]["code"] == "BLUEPRINT_UNRESOLVED"
    assert "module 4" in result["error"]["message"] and result["error"]["details"]["missing"] == "gateway"
    assert result["updatedAt"] >= result["createdAt"]
    assert not ready.agentcore.harnesses  # nothing was created in AgentCore


def test_worker_start_failure_marks_the_job_failed(ready):
    create_blueprint(ready)
    ready.lambda_client.fail = True
    status, body = call("POST", "/projects/hr-assistant/deploy", body={"version": 1}, persona="developer")
    assert status == 502 and body["error"]["code"] == "WORKER_UNAVAILABLE"
    failed = [j for j in db.query_gsi1("JOB") if j["status"] == "FAILED"]
    assert len(failed) == 1 and failed[0]["error"]["code"] == "UPSTREAM_ERROR"


# ---- releases -----------------------------------------------------------------------------------------------------------------
def test_release_flow_blocked_then_pending_then_approved(ready):
    create_blueprint(ready)
    status, release = call(
        "POST",
        "/projects/hr-assistant/releases",
        body={"blueprintVersion": 1, "notes": "first", "autoRun": False},
        persona="developer",
    )
    assert status == 201, release
    assert release["status"] == "DRAFT" and release["blueprintVersion"] == 1 and len(release["releaseId"]) == 26
    assert release["createdBy"] == "developer" and release["evaluation"] is None and release["notes"] == "first"
    release_id = release["releaseId"]
    assert call("GET", f"/releases/{release_id}", persona="approver")[1]["releaseId"] == release_id
    assert [r["releaseId"] for r in call("GET", "/projects/hr-assistant/releases")[1]] == [release_id]
    assert [r["releaseId"] for r in call("GET", "/releases")[1]] == [release_id]
    assert call("GET", "/releases/missing")[0] == 404
    assert call("POST", "/projects/hr-assistant/releases", body={"blueprintVersion": 7})[0] == 404
    assert call("POST", "/projects/hr-assistant/releases", body={}, persona="developer")[0] == 400
    assert call("POST", "/projects/hr-assistant/releases", body={"blueprintVersion": 1}, persona="approver")[0] == 403

    status, body = call("POST", f"/releases/{release_id}/approve", body={}, persona="approver")
    assert status == 409 and "PENDING_APPROVAL" in body["error"]["message"]
    # no externally computed summary passes the gate (the old manual path)
    status, body = call("POST", f"/releases/{release_id}/evaluation", body=evaluation_body(PASSING_SCORES))
    assert status == 409 and body["error"]["code"] == "NATIVE_GATE_REQUIRED"
    assert call("POST", f"/releases/{release_id}/run", persona="approver")[0] == 403

    # the pipeline run: prompt v1 fails three thresholds
    from backend_testlib import run_summary

    ready.dataset_run.result = run_summary(BLOCKED_SCORES)
    status, accepted = call("POST", f"/releases/{release_id}/run", persona="developer")
    assert status == 202 and accepted["type"] == "release"
    ready.pipeline.advance()
    body = call("GET", f"/releases/{release_id}")[1]
    assert (
        body["status"] == "BLOCKED"
        and body["evaluation"]["verdict"] == "BLOCKED"
        and body["evaluation"]["passed"] is False
    )
    failures = {f["evaluator"]: f for f in body["evaluation"]["failures"]}
    assert set(failures) == {"Correctness", "GoalSuccessRate", "TrajectoryInOrderMatch"}
    assert failures["TrajectoryInOrderMatch"] == {
        "evaluator": "TrajectoryInOrderMatch",
        "actual": 0.625,
        "threshold": 0.9,
        "op": "mean>=",
    }
    assert body["evaluation"]["details"]["Harmfulness"] == {
        "actual": 0,
        "threshold": 0.0,
        "op": "harmRate<=",
        "passed": True,
        "count": 10,
        "errors": 0,
    }
    assert body["evaluation"]["thresholds"] == {
        "Correctness": 0.8,
        "GoalSuccessRate": 0.9,
        "TrajectoryInOrderMatch": 0.9,
        "Harmfulness": 0,
    }
    assert body["evaluation"]["runId"] == "run-worker-1" and body["evaluation"]["datasetName"] == "hr-assistant.json"
    assert body["evaluation"]["scenarioCount"] == 10 and body["evaluation"]["source"] == "pipeline"
    assert body["evaluation"]["submittedBy"] == "developer" and body["evaluation"]["completedAt"].endswith("Z")
    assert body["evaluation"]["failedScenarios"] == [] and body["updatedAt"] >= body["createdAt"]
    assert call("POST", f"/releases/{release_id}/approve", body={}, persona="approver")[0] == 409
    assert call("POST", f"/releases/{release_id}/promote", persona="approver")[0] == 409

    # run again: passing scores -> PENDING_APPROVAL
    ready.dataset_run.result = run_summary(PASSING_SCORES)
    status, accepted = call("POST", f"/releases/{release_id}/run", persona="admin")
    assert status == 202
    ready.pipeline.advance()
    body = call("GET", f"/releases/{release_id}")[1]
    assert body["status"] == "PENDING_APPROVAL" and body["evaluation"]["failures"] == []
    assert body["evaluation"]["verdict"] == "PASSED" and body["evaluation"]["submittedBy"] == "admin"
    assert call("POST", f"/releases/{release_id}/promote", persona="approver")[0] == 409  # not approved yet
    assert call("POST", f"/releases/{release_id}/approve", body={}, persona="developer")[0] == 403
    assert call("POST", f"/releases/{release_id}/approve", body={}, persona="admin")[0] == 403
    status, body = call("POST", f"/releases/{release_id}/approve", body={"comment": "scores fine"}, persona="approver")
    assert status == 200 and body["status"] == "APPROVED"
    assert body["approval"]["by"] == "approver" and body["approval"]["comment"] == "scores fine"
    assert body["approval"]["decision"] == "APPROVED" and body["approval"]["at"].endswith("Z")
    assert call("POST", f"/releases/{release_id}/approve", body={}, persona="approver")[0] == 409
    assert call("POST", f"/releases/{release_id}/run", persona="developer")[0] == 409  # after approval
    ready.pipeline.advance()

    # a failed scenario blocks even when the means pass; a rejection ends the release
    second = pending_release(ready, scores=PASSING_SCORES, failed=["policy-remote-work-days"])
    assert second["status"] == "BLOCKED"
    assert second["evaluation"]["failures"] == [
        {"evaluator": "_scenarios", "actual": 1, "threshold": 0, "op": "failed<="}
    ]
    assert second["evaluation"]["failedScenarios"] == ["policy-remote-work-days"]
    ready.dataset_run.result = run_summary(PASSING_SCORES)
    call("POST", f"/releases/{second['releaseId']}/run", persona="developer")
    ready.pipeline.advance()
    assert call("GET", f"/releases/{second['releaseId']}")[1]["status"] == "PENDING_APPROVAL"
    status, body = call(
        "POST", f"/releases/{second['releaseId']}/reject", body={"comment": "not now"}, persona="approver"
    )
    assert status == 200 and body["status"] == "REJECTED" and body["approval"]["decision"] == "REJECTED"
    assert call("POST", f"/releases/{second['releaseId']}/promote", persona="approver")[0] == 409
    assert [r["status"] for r in call("GET", "/releases")[1]] == ["REJECTED", "APPROVED"]  # newest first


def test_evaluation_rejects_non_numeric_scores(ready):
    """The verdict path (the Evaluate action's `record_evaluation`) validates a run summary before applying thresholds."""
    create_blueprint(ready)
    status, release = call(
        "POST", "/projects/hr-assistant/releases", body={"blueprintVersion": 1, "autoRun": False}, persona="developer"
    )
    item = releases_handler.load_release(release["releaseId"])
    stringified = {name: {**entry, "mean": str(entry["mean"])} for name, entry in PASSING_SCORES.items()}
    with pytest.raises(ApiError) as excinfo:
        releases_handler.record_evaluation(item, evaluation_body(stringified), "developer", source="pipeline")
    assert excinfo.value.code == "VALIDATION_ERROR" and "scores.Correctness.mean" in excinfo.value.message
    bad_count = {name: {**entry, "count": 10.5} for name, entry in PASSING_SCORES.items()}
    with pytest.raises(ApiError) as excinfo:
        releases_handler.record_evaluation(item, evaluation_body(bad_count), "developer", source="pipeline")
    assert "scores.Correctness.count must be an integer" in excinfo.value.message
    with pytest.raises(ApiError) as excinfo:
        releases_handler.record_evaluation(item, {"scores": "x"}, "developer", source="pipeline")
    assert excinfo.value.status == 400
    assert call("GET", f"/releases/{release['releaseId']}")[1]["status"] == "DRAFT"


def test_release_transitions_are_conditional_writes(ready):
    create_blueprint(ready)
    release = pending_release(ready)
    stale = releases_handler.load_release(release["releaseId"])  # a second request that read PENDING_APPROVAL
    assert (
        call("POST", f"/releases/{release['releaseId']}/approve", body={}, persona="approver")[1]["status"]
        == "APPROVED"
    )
    with pytest.raises(ApiError) as excinfo:  # its write is refused: the status it checked is gone
        releases_handler.save(stale, "REJECTED", expected_status="PENDING_APPROVAL", approval={"decision": "REJECTED"})
    assert excinfo.value.status == 409 and excinfo.value.code == "CONFLICT"
    assert call("GET", f"/releases/{release['releaseId']}")[1]["status"] == "APPROVED"


def test_a_promotion_in_progress_is_never_stuck_by_age(ready):
    create_blueprint(ready)
    release = approved_release(ready)
    status, accepted = call("POST", f"/releases/{release['releaseId']}/promote", persona="approver")
    assert status == 202
    # the job looks hours old, but the execution is in progress (queued or deploying): a second promote is refused
    item = db.get(db.job_key(accepted["jobId"]))
    item.update(status="RUNNING", updatedAt="2026-10-01T00:00:00Z")
    db.put(item)
    status, body = call("POST", f"/releases/{release['releaseId']}/promote", persona="approver")
    assert status == 409 and "PROMOTING" in body["error"]["message"]
    assert releases_handler.promotion_stuck(releases_handler.load_release(release["releaseId"])) is False
    assert releases_handler.promotion_stuck({**release, "status": "APPROVED"}) is False


def test_evaluation_needs_thresholds_on_the_blueprint(ready):
    create_blueprint(ready)
    stored = db.get(db.blueprint_key("hr-assistant", 1))
    stored["spec"]["evaluation"].pop("thresholds")
    db.put(stored)
    status, release = call(
        "POST", "/projects/hr-assistant/releases", body={"blueprintVersion": 1, "autoRun": False}, persona="developer"
    )
    item = releases_handler.load_release(release["releaseId"])
    with pytest.raises(ApiError) as excinfo:
        releases_handler.record_evaluation(item, evaluation_body(PASSING_SCORES), "developer", source="pipeline")
    assert excinfo.value.status == 409 and excinfo.value.code == "THRESHOLDS_NOT_CONFIGURED"


# ---- promote job --------------------------------------------------------------------------------------------------------------
def test_promote_job_deploys_prod_on_default_and_enables_online_evaluation(ready):
    create_blueprint(ready)
    deploy(ready, 1)
    call("PUT", "/settings", body={"registryId": "reg-1", "registryArn": "arn:reg-1"})  # module 4's catalogue registry
    release = approved_release(ready)
    assert call("POST", f"/releases/{release['releaseId']}/promote", persona="developer")[0] == 403
    assert call("POST", f"/releases/{release['releaseId']}/promote", persona="admin")[0] == 403
    status, accepted = call("POST", f"/releases/{release['releaseId']}/promote", persona="approver")
    assert status == 202, accepted
    assert (
        accepted["status"] == "PROMOTING"
        and accepted["type"] == "promote"
        and accepted["releaseId"] == release["releaseId"]
    )
    promoting = call("GET", f"/releases/{release['releaseId']}")[1]
    assert promoting["status"] == "PROMOTING" and promoting["promotion"]["jobId"] == accepted["jobId"]
    assert promoting["promotion"]["by"] == "approver"
    assert call("POST", f"/releases/{release['releaseId']}/promote", persona="approver")[0] == 409  # already promoting
    job = call("GET", f"/jobs/{accepted['jobId']}")[1]
    assert job["input"] == {
        "releaseId": release["releaseId"],
        "projectId": "hr-assistant",
        "blueprintVersion": 1,
        "env": "prod",
    }

    ready.pipeline.advance()  # the PromoteProd action runs the job
    result = jobs.get(accepted["jobId"])
    assert result["status"] == "SUCCEEDED", result
    out = result["output"]
    assert (
        out["env"] == "prod"
        and out["endpoint"] == "DEFAULT"
        and out["harnessVersion"] == "1"
        and out["keyCreated"] is True
    )
    assert out["harnessArn"].endswith("harness/hr_assistant_prod-ABC123") and out["endpointArn"].endswith(
        "/endpoint/DEFAULT"
    )
    assert out["onlineEvaluationConfigId"] == "hr_assistant_prod_online_eval-id"
    assert out["logGroup"] == "/aws/bedrock-agentcore/runtimes/harness_hr_assistant_prod-RT123-DEFAULT"
    done = call("GET", f"/releases/{release['releaseId']}")[1]
    assert done["status"] == "PROMOTED" and done["error"] is None
    assert done["promotion"]["harnessVersion"] == "1" and done["promotion"]["endpoint"] == "DEFAULT"
    assert done["promotion"]["jobId"] == accepted["jobId"] and done["promotion"]["at"].endswith("Z")
    assert done["promotion"]["onlineEvaluationConfigId"] == "hr_assistant_prod_online_eval-id"

    # the Catalogue's Agents tab: the Prod agent is an approved AGENT record (A2A agent card on the Prod harness, DEFAULT endpoint)
    entry = done["promotion"]["registryRecord"]
    assert entry == out["registryRecord"]
    assert entry["name"] == "hr-assistant-agent" and entry["status"] == "APPROVED" and entry["action"] == "created"
    assert entry["recordVersion"] == "1" and entry["recordArn"].endswith(f"/record/{entry['recordId']}")
    assert entry["registryId"] == "reg-1" and "warning" not in entry and "error" not in entry
    stored = ready.registry.records[entry["recordId"]]
    assert stored["recordType"] == "AGENT" and stored["displayName"] == "HR Assistant (Prod)"
    assert stored["tags"] == {"ManagedBy": "platform", "ProjectId": "hr-assistant", "auto-delete": "no"}
    assert "approved by approver and promoted to Prod by approver" in stored["statusReason"]
    descriptor = stored["descriptors"]["a2aAgentCard"]
    assert descriptor["dataSchemaVersion"] == "0.3"  # the registry rejects "0.3.0" (AWS docs, 2026-10-21)
    card = json.loads(descriptor["data"])
    assert card["protocolVersion"] == "0.3.0" and card["name"] == "HR Assistant" and card["version"] == "1"
    assert card["url"].startswith("https://bedrock-agentcore.us-east-1.amazonaws.com/harnesses/invoke?harnessArn=")
    assert "qualifier=" not in card["url"] and card["url"].endswith("hr_assistant_prod-ABC123")  # DEFAULT: no qualifier
    assert card["skills"][0]["id"] == "hr-assistant" and "people-ops" in card["skills"][0]["tags"]
    assert set(card) >= {"capabilities", "defaultInputModes", "defaultOutputModes", "description", "skills"}
    assert card["provider"] == {"organization": "people-ops", "url": CONFIG["ui/url"]}
    # DRAFT -> submitted -> approved, in that order (no auto-approval rules on the registry)
    assert [kind for kind, _ in ready.registry.calls] == ["list", "create", "get", "submit", "get", "status"]

    # AgentCore calls: prod harness (no CreateHarnessEndpoint: DEFAULT follows the new version), online evaluation on its log group
    kinds = [c[0] for c in ready.agentcore.calls]
    assert kinds[-2:] == ["harness", "online"] and "endpoint" not in kinds
    _, name, log_group, service_name, evaluators, role_arn, kwargs = ready.agentcore.calls[-1]
    assert name == "hr_assistant_prod_online_eval" and service_name == "harness_hr_assistant_prod.DEFAULT"
    assert log_group == "/aws/bedrock-agentcore/runtimes/harness_hr_assistant_prod-RT123-DEFAULT"
    assert evaluators == ["Builtin.Helpfulness", "Builtin.Harmfulness", "Builtin.ToolSelectionAccuracy"]
    assert role_arn == CONFIG["roles/evaluation-role-arn"]
    assert kwargs["sampling_percentage"] == 100.0 and kwargs["session_timeout_minutes"] == 1
    assert kwargs["tags"]["ProjectId"] == "hr-assistant"
    harness_spec = ready.agentcore.calls[-2][2]
    assert harness_spec["executionRoleArn"].endswith("hr-assistant-prod-agent")
    assert harness_spec["tools"][0]["config"]["agentCoreGateway"]["gatewayArn"].endswith(
        "hr-assistant-prod-gw-abcdefghij"
    )

    # prod key + provider created, resources registered
    aliases = sorted(k["key_alias"] for k in ready.fakes.litellm.keys.values())
    assert aliases == ["hr-assistant-dev-claude-sonnet-5", "hr-assistant-prod-claude-sonnet-5"]
    assert "litellm-hr-assistant-prod-claude-sonnet-5" in ready.fakes.identity.providers
    rows = {
        (r["type"], r["name"]): r for r in call("GET", "/projects/hr-assistant/resources", query={"env": "prod"})[1]
    }
    assert rows[("harness", "hr_assistant_prod")]["attributes"]["endpoint"] == "DEFAULT"
    endpoint = rows[("harness_endpoint", "DEFAULT")]
    assert (
        endpoint["arn"].endswith("/endpoint/DEFAULT")
        and endpoint["attributes"]["qualifier"] is None
        and endpoint["attributes"]["version"] == "1"
    )
    assert endpoint["attributes"]["serviceName"] == "harness_hr_assistant_prod.DEFAULT"
    online = rows[("online_evaluation", "hr_assistant_prod_online_eval")]
    assert online["id"] == "hr_assistant_prod_online_eval-id" and online["attributes"]["logGroup"] == out["logGroup"]
    assert online["attributes"]["evaluators"] == evaluators
    assert ("credential_provider", "litellm-hr-assistant-prod-claude-sonnet-5") in rows
    agent_row = rows[("registry_record", "hr-assistant-agent")]
    assert agent_row["id"] == entry["recordId"] and agent_row["attributes"]["recordType"] == "AGENT"
    assert agent_row["attributes"]["registryId"] == "reg-1"  # the same shape as the tools record apply 4 registers
    assert (
        agent_row["attributes"]["releaseId"] == release["releaseId"] and agent_row["attributes"]["status"] == "APPROVED"
    )
    assert call("POST", f"/releases/{release['releaseId']}/promote", persona="approver")[0] == 409  # PROMOTED is final
    assert call("DELETE", "/projects/hr-assistant")[0] == 409  # a PROMOTED release exists
    assert call("GET", f"/jobs/{accepted['jobId']}")[1]["status"] == "SUCCEEDED"


def test_promoted_agent_record_is_updated_next_time_and_never_fails_the_promotion(ready):
    """A second promotion updates the project's one AGENT record (a new DRAFT revision: submit and approve again); without a
    registry the step is skipped; a registry failure is reported on the promotion, never raised."""
    from platform_api.services import registry as registry_service

    project = {
        "projectId": "hr-assistant",
        "name": "HR Assistant",
        "domainId": "people-ops",
        "description": "Leave and policy",
    }
    promotion = {
        "harnessArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:harness/hr_assistant_prod-ABC123",
        "endpoint": "DEFAULT",
    }
    release = {"releaseId": "R1", "blueprintVersion": 1, "approval": {"by": "approver"}}
    skipped = registry_service.register_agent(project, {"version": 1}, release, promotion, "approver")
    assert skipped == {
        "name": "hr-assistant-agent",
        "skipped": "no Agent Registry configured (PUT /settings {registryId})",
    }
    assert ready.registry.calls == []

    call("PUT", "/settings", body={"registryId": "reg-1", "registryArn": "arn:reg-1"})
    first = registry_service.register_agent(project, {"version": 1}, release, promotion, "approver")
    assert first["action"] == "created" and first["status"] == "APPROVED" and first["recordVersion"] == "1"
    assert first["registryId"] == "reg-1" and "warning" not in first
    ready.registry.calls.clear()
    # the live registry answers an update of an APPROVED record with a new DRAFT revision: it is submitted and approved again
    second = registry_service.register_agent(
        project, {"version": 2}, {**release, "releaseId": "R2", "blueprintVersion": 2}, promotion, "approver"
    )
    assert second["recordId"] == first["recordId"] and second["action"] == "updated" and second["recordVersion"] == "2"
    assert [kind for kind, _ in ready.registry.calls] == ["list", "update", "get", "submit", "get", "status"]
    update = dict(ready.registry.calls[1][1])
    assert update["recordVersion"] == "2" and update["displayName"] == {"optionalValue": "HR Assistant (Prod)"}
    assert json.loads(update["descriptors"]["optionalValue"]["a2aAgentCard"]["optionalValue"]["data"])["version"] == "2"
    assert "R2" in update["description"]["optionalValue"]
    stored = ready.registry.records[first["recordId"]]
    assert stored["recordVersion"] == "2" and stored["status"] == "APPROVED" and "R2" in stored["statusReason"]

    # a record the registry leaves in a state that cannot be approved is reported, not forced (no status call)
    ready.registry.after_update_status = "UPDATE_FAILED"
    ready.registry.calls.clear()
    stuck = registry_service.register_agent(
        project, {"version": 3}, {**release, "releaseId": "R3"}, promotion, "approver"
    )
    assert [kind for kind, _ in ready.registry.calls] == ["list", "update", "get"]
    assert stuck["recordId"] == first["recordId"] and stuck["status"] == "UPDATE_FAILED"
    assert (
        stuck["warning"] == "record hr-assistant-agent is UPDATE_FAILED and was not approved" and "error" not in stuck
    )
    ready.registry.after_update_status = None

    # a deprecated record cannot be edited (terminal): the registry refuses, the promotion reports it and stands
    stored["status"] = "DEPRECATED"
    failed = registry_service.register_agent(project, {"version": 4}, release, promotion, "approver")
    assert failed["name"] == "hr-assistant-agent" and "DEPRECATED record cannot be edited" in failed["error"]
    assert "recordId" not in failed
    stored["status"] = "APPROVED"

    ready.registry.fail_on.add("UpdateRegistryRecord")
    failed = registry_service.register_agent(project, {"version": 5}, release, promotion, "approver")
    assert failed["name"] == "hr-assistant-agent" and "UpdateRegistryRecord boom" in failed["error"]
    assert "recordId" not in failed


def test_catalogue_agents_sync_registers_agents_promoted_before_the_registry_step(ready):
    """A release promoted while no registry was configured (or before the promote job wrote AGENT records) has no catalogue
    entry; POST /catalogue/agents/sync (admin) writes it later, stores the outcome on the release and the resource row, a
    second run updates the same record, and a project without a promoted release is reported, not an error."""
    create_blueprint(ready)
    deploy(ready, 1)
    done, _ = promoted_release(ready, 1)
    assert done["promotion"]["registryRecord"]["skipped"].startswith("no Agent Registry configured")
    assert call("POST", "/catalogue/agents/sync", persona="developer")[0] == 403
    assert call("POST", "/catalogue/agents/sync", persona="approver")[0] == 403
    status, body = call("POST", "/catalogue/agents/sync")
    assert status == 200 and body["agents"][0]["skipped"].startswith("no Agent Registry configured")

    call("PUT", "/settings", body={"registryId": "reg-1", "registryArn": "arn:reg-1"})
    status, body = call("POST", "/catalogue/agents/sync")
    assert status == 200 and len(body["agents"]) == 1
    entry = body["agents"][0]
    assert entry["projectId"] == "hr-assistant" and entry["releaseId"] == done["releaseId"]
    assert entry["name"] == "hr-assistant-agent" and entry["action"] == "created" and entry["status"] == "APPROVED"
    stored = ready.registry.records[entry["recordId"]]
    assert stored["recordType"] == "AGENT" and stored["recordVersion"] == "1" and stored["status"] == "APPROVED"
    assert "hr_assistant_prod-ABC123" in json.loads(stored["descriptors"]["a2aAgentCard"]["data"])["url"]
    assert "approved by approver" in stored["statusReason"] and "promoted to Prod by admin" in stored["statusReason"]
    release = call("GET", f"/releases/{done['releaseId']}")[1]
    assert release["status"] == "PROMOTED" and release["promotion"]["registryRecord"]["recordId"] == entry["recordId"]
    # the rest of the promotion is untouched
    assert release["promotion"]["harnessArn"] == done["promotion"]["harnessArn"]
    rows = {
        (r["type"], r["name"]): r for r in call("GET", "/projects/hr-assistant/resources", query={"env": "prod"})[1]
    }
    assert rows[("registry_record", "hr-assistant-agent")]["id"] == entry["recordId"]

    # again: the same record, updated (a new DRAFT revision submitted and approved again); a second project is reported
    call(
        "POST",
        "/projects",
        body={
            "projectId": "payroll-helper",
            "domainId": "people-ops",
            "name": "Payroll",
            "models": ["claude-sonnet-5"],
        },
    )
    status, body = call("POST", "/catalogue/agents/sync")
    by_project = {a["projectId"]: a for a in body["agents"]}
    assert (
        by_project["hr-assistant"]["recordId"] == entry["recordId"]
        and by_project["hr-assistant"]["action"] == "updated"
    )
    assert ready.registry.records[entry["recordId"]]["status"] == "APPROVED"
    assert by_project["payroll-helper"] == {
        "projectId": "payroll-helper",
        "name": "payroll-helper-agent",
        "skipped": "no PROMOTED release",
    }


def test_promote_failure_marks_the_release_failed_and_can_be_retried(ready):
    create_blueprint(ready)
    release = approved_release(ready)
    ready.agentcore.fail_on.add("harness")
    status, accepted = call("POST", f"/releases/{release['releaseId']}/promote", persona="approver")
    ready.pipeline.advance()
    result = jobs.get(accepted["jobId"])
    assert (
        result["status"] == "FAILED"
        and result["error"]["code"] == "UPSTREAM_ERROR"
        and "CREATE_FAILED" in result["error"]["message"]
    )
    failed = call("GET", f"/releases/{release['releaseId']}")[1]
    assert (
        failed["status"] == "FAILED"
        and failed["error"]["code"] == "UPSTREAM_ERROR"
        and failed["error"]["jobId"] == accepted["jobId"]
    )
    assert failed["promotion"]["jobId"] == accepted["jobId"]
    # the prod key was created before the harness failed; the retry reuses it
    assert "litellm-hr-assistant-prod-claude-sonnet-5" in ready.fakes.identity.providers

    ready.agentcore.fail_on.clear()
    status, accepted = call("POST", f"/releases/{release['releaseId']}/promote", persona="approver")
    assert status == 202
    ready.pipeline.advance()
    result = jobs.get(accepted["jobId"])
    assert result["status"] == "SUCCEEDED" and result["output"]["keyCreated"] is False
    done = call("GET", f"/releases/{release['releaseId']}")[1]
    assert done["status"] == "PROMOTED" and done["error"] is None and done["promotion"]["jobId"] == accepted["jobId"]


# ---- invoke job ---------------------------------------------------------------------------------------------------------------
def test_invoke_flow_sessions_and_budget_exceeded(ready):
    body = {"projectId": "hr-assistant", "env": "dev", "prompt": "What is my leave balance? I am E1001."}
    status, error = call("POST", "/invoke", body=body, persona="approver")
    assert (
        status == 409 and error["error"]["code"] == "HARNESS_NOT_DEPLOYED" and "module 6" in error["error"]["message"]
    )

    create_blueprint(ready)
    deploy(ready, 1)
    status, accepted = call("POST", "/invoke", body=body, persona="approver")
    assert status == 202, accepted
    session_id = accepted["sessionId"]
    assert len(session_id) >= 33 and session_id.endswith("-platform") and accepted["status"] == "QUEUED"
    job = call("GET", f"/jobs/{accepted['jobId']}")[1]
    assert job["input"]["harnessArn"].endswith("harness/hr_assistant_dev-ABC123") and job["input"]["qualifier"] is None
    assert job["input"]["sessionId"] == session_id and job["input"]["model"] == "claude-sonnet-5"
    assert job["createdBy"] == "approver" and job["ttl"] > 0
    result = run_job(accepted["jobId"])
    assert result["status"] == "SUCCEEDED", result
    assert result["output"]["text"] == "echo: What is my leave balance? I am E1001."
    assert (
        result["output"]["toolsUsed"] == ["hrtools___get_leave_balance"]
        and result["output"]["stopReason"] == "end_turn"
    )
    assert result["output"]["usage"]["totalTokens"] == 120 and result["output"]["modelCalls"] == 2
    assert result["output"]["sessionId"] == session_id and result["output"]["seconds"] >= 0
    assert ready.agentcore.calls[-1] == ("invoke", job["input"]["harnessArn"], body["prompt"], None, session_id)

    # same session keeps the conversation; a short session id is rejected; prod needs a promotion first
    status, second = call("POST", "/invoke", body={**body, "sessionId": session_id})
    assert status == 202 and second["sessionId"] == session_id
    assert call("POST", "/invoke", body={**body, "sessionId": "short"})[0] == 400
    # InvokeHarness runtimeSessionId: 33-100 chars of [a-zA-Z0-9_-]; the API refuses what the service would refuse 30 s later
    for bad in ("my session 1 " + "x" * 30, "a" * 101, "session.with.dots-" + "x" * 20, "-" + "x" * 40):
        status, error = call("POST", "/invoke", body={**body, "sessionId": bad})
        assert status == 400 and "sessionId" in error["error"]["message"], bad
    assert call("POST", "/invoke", body={**body, "sessionId": "Session_1-" + "x" * 30})[0] == 202
    assert call("POST", "/invoke", body={**body, "prompt": ""})[0] == 400
    assert call("POST", "/invoke", body={**body, "env": "qa"})[0] == 400
    assert call("POST", "/invoke", body={**body, "projectId": "nope"})[0] == 404
    status, error = call("POST", "/invoke", body={**body, "env": "prod"})
    assert status == 409 and "module 6" in error["error"]["message"]
    promoted_release(ready, 1)
    status, accepted = call("POST", "/invoke", body={**body, "env": "prod"}, persona="developer")
    assert status == 202
    result = run_job(accepted["jobId"])
    assert result["status"] == "SUCCEEDED" and result["output"]["qualifier"] is None  # DEFAULT endpoint: no qualifier
    assert (
        ready.agentcore.calls[-1][1].endswith("harness/hr_assistant_prod-ABC123")
        and ready.agentcore.calls[-1][3] is None
    )

    # budget exceeded: LiteLLM refused the model call -> FAILED with the cap details from /key/info
    token = call("GET", "/projects/hr-assistant/keys")[1][0]["litellmKeyId"]
    ready.fakes.litellm.keys[token].update(max_budget=0.0083, spend=0.0166)
    message = (
        "litellm.BadRequestError: OpenAIException - Budget has been exceeded! Key=hr-assistant-dev-claude-sonnet-5 "
        "Current cost: 0.0166, Max budget: 0.0083"
    )
    ready.agentcore.invoke_responses.append(HarnessInvokeError(message, budget_exceeded=True, session_id=session_id))
    status, accepted = call("POST", "/invoke", body={**body, "sessionId": session_id})
    result = run_job(accepted["jobId"])
    assert result["status"] == "FAILED" and result["error"]["code"] == "BUDGET_EXCEEDED"
    details = result["error"]["details"]
    assert details["alias"] == "hr-assistant-dev-claude-sonnet-5" and details["maxBudget"] == 0.0083
    assert details["spend"] == 0.0166 and details["currentCost"] == 0.0166 and details["budgetResetAt"]
    assert details["subject"] == "hr-assistant-dev-claude-sonnet-5" and details["blocked"] is False
    assert "Budget has been exceeded" in result["error"]["message"]

    # any other invoke failure
    ready.agentcore.invoke_responses.append(HarnessInvokeError("runtime exploded", session_id=session_id))
    status, accepted = call("POST", "/invoke", body=body)
    result = run_job(accepted["jobId"])
    assert result["status"] == "FAILED" and result["error"] == {"code": "INVOKE_FAILED", "message": "runtime exploded"}


# ---- metrics ------------------------------------------------------------------------------------------------------------------
def test_metrics_series_follow_the_registered_resources(ready, monkeypatch):
    seen = []

    def fake_harness_metrics(cfg, harness_name, endpoint, hours=24, period=300, cw=None):
        seen.append(("harness", harness_name, endpoint, hours, period))
        return {
            **{key: [["2026-10-08T22:00:00+00:00", 3.0]] for key in metrics_service.HARNESS_SERIES},
            "totals": {"invocations": 3.0, "errors": 0.0, "latencyP90Max": 5100.0},
            "dimensions": {"Invocations": [[{"Name": "Name", "Value": "harness_hr_assistant_dev::DEFAULT"}]]},
            "window": {"start": "s", "end": "e", "period": period},
        }

    def fake_policy_metrics(cfg, engine_id, hours=24, period=300, cw=None, target_resource=None):
        seen.append(("policy", engine_id, hours, period, target_resource))
        return {
            "allow": [["2026-10-08T22:00:00+00:00", 16.0]],
            "deny": [["2026-10-08T22:00:00+00:00", 2.0]],
            "byTool": {"hrtools___get_leave_balance": {"allow": 16.0, "deny": 0.0}},
            "totals": {"allow": 16.0, "deny": 2.0},
            "targetResource": target_resource,
            "dimensions": {"AllowDecisions": {"totals": ["OperationName", "PolicyEngine", "TargetResource"]}},
            "window": {"start": "s", "end": "e", "period": period},
        }

    def fake_evaluation_metrics(cfg, config_id, hours=24, period=3600, cw=None):
        seen.append(("evaluation", config_id, hours, period))
        return {
            "evaluators": {
                "Helpfulness": {
                    "average": [["2026-10-08T22:00:00+00:00", 0.83]],
                    "labels": {"Very Helpful": 2},
                    "mean": 0.83,
                    "samples": 2,
                }
            },
            "window": {"start": "s", "end": "e", "period": period},
        }

    monkeypatch.setattr(metrics_service.sdk_metrics, "harness_metrics", fake_harness_metrics)
    monkeypatch.setattr(metrics_service.sdk_metrics, "policy_metrics", fake_policy_metrics)
    monkeypatch.setattr(metrics_service.sdk_metrics, "evaluation_metrics", fake_evaluation_metrics)

    status, body = call("GET", "/metrics", query={"projectId": "hr-assistant", "env": "dev"}, persona="approver")
    assert status == 200, body
    assert body["harness"] is None and body["policy"] is None and body["evaluation"] is None and body["hours"] == 24
    assert body["series"]["invocations"] == [] and body["series"]["policyAllow"] == [] and body["totals"] == {}
    assert seen == []  # nothing registered: no CloudWatch calls

    call("PUT", "/settings", body={"policyEngineId": "agent_platform_policy_engine-abc", "policyEngineArn": "arn:pe"})
    create_blueprint(ready)
    deploy(ready, 1)
    status, body = call("GET", "/metrics", query={"projectId": "hr-assistant", "env": "dev", "hours": "2"})
    assert status == 200
    assert body["period"] == 60 and body["series"]["invocations"] == [["2026-10-08T22:00:00+00:00", 3.0]]
    assert body["series"]["latencyP90"] and body["series"]["policyAllow"] == [["2026-10-08T22:00:00+00:00", 16.0]]
    assert (
        body["totals"]["invocations"] == 3.0
        and body["totals"]["policyDeny"] == 2.0
        and body["totals"]["latencyP90Max"] == 5100.0
    )
    assert body["harness"]["metricName"] == "harness_hr_assistant_dev::DEFAULT" and body["harness"]["version"] == "1"
    assert (
        body["policy"]["policyEngineId"] == "agent_platform_policy_engine-abc" and body["policy"]["scope"] == "gateway"
    )
    assert (
        body["policy"]["gatewayId"] == "hr-assistant-dev-gw-abcdefghij"
    )  # the env's registered gateway = TargetResource
    assert body["policy"]["gatewayName"] == "hr-assistant-dev-gw" and body["policy"]["dimensions"]
    assert body["evaluation"] is None and "evaluationHelpfulness" not in body["series"]
    assert seen == [
        ("harness", "hr_assistant_dev", "DEFAULT", 2, 60),
        ("policy", "agent_platform_policy_engine-abc", 2, 60, "hr-assistant-dev-gw-abcdefghij"),
    ]

    # no gateway registered for the environment: no engine-wide fallback, policy stays null with empty series
    call("DELETE", "/projects/hr-assistant/resources/prod/gateway/hr-assistant-prod-gw")
    seen.clear()
    status, body = call("GET", "/metrics", query={"projectId": "hr-assistant", "env": "prod"})
    assert status == 200 and body["policy"] is None and body["series"]["policyAllow"] == []
    assert "policyAllow" not in body["totals"] and not [s for s in seen if s[0] == "policy"]
    register(
        "hr-assistant",
        "prod",
        "gateway",
        "hr-assistant-prod-gw",
        arn=f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/hr-assistant-prod-gw-abcdefghij",
        id="hr-assistant-prod-gw-abcdefghij",
    )

    promoted_release(ready, 1)
    status, body = call("GET", "/metrics", query={"projectId": "hr-assistant", "env": "prod", "hours": "168"})
    assert status == 200 and body["period"] == 3600
    assert (
        body["harness"]["endpoint"] == "DEFAULT"
        and body["harness"]["metricName"] == "harness_hr_assistant_prod::DEFAULT"
    )
    assert body["series"]["evaluationHelpfulness"] == [["2026-10-08T22:00:00+00:00", 0.83]]
    assert body["evaluation"]["configId"] == "hr_assistant_prod_online_eval-id"
    assert body["evaluation"]["evaluators"]["Helpfulness"] == {
        "mean": 0.83,
        "samples": 2,
        "labels": {"Very Helpful": 2},
    }
    assert seen[-1] == ("evaluation", "hr_assistant_prod_online_eval-id", 168, 3600)

    assert call("GET", "/metrics", query={"projectId": "hr-assistant", "env": "dev", "hours": "0"})[0] == 400
    assert call("GET", "/metrics", query={"projectId": "hr-assistant", "env": "dev", "hours": "x"})[0] == 400
    assert call("GET", "/metrics", query={"projectId": "hr-assistant"})[0] == 400
    assert call("GET", "/metrics", query={"env": "dev"})[0] == 400
    assert call("GET", "/metrics", query={"projectId": "nope", "env": "dev"})[0] == 404


# ---- feedback, spend by env, registry events, worker entry ----------------------------------------------------------------------
def test_feedback_post_and_list(ready):
    session = agentcore.new_session_id("platform")
    status, body = call(
        "POST",
        "/feedback",
        body={
            "projectId": "hr-assistant",
            "env": "dev",
            "sessionId": session,
            "rating": "up",
            "comment": "great answer",
        },
        persona="approver",
    )
    assert status == 201, body
    assert body["rating"] == "up" and body["comment"] == "great answer" and body["createdBy"] == "approver"
    assert (
        body["projectId"] == "hr-assistant"
        and body["env"] == "dev"
        and body["sessionId"] == session
        and "PK" not in body
    )
    status, body = call(
        "POST", "/feedback", body={"projectId": "hr-assistant", "env": "prod", "sessionId": "s2" * 20, "rating": "down"}
    )
    assert status == 201 and body["comment"] == ""
    # a correction of the same session within the same second is a second row, not an overwrite
    status, body = call(
        "POST", "/feedback", body={"projectId": "hr-assistant", "env": "dev", "sessionId": session, "rating": "down"}
    )
    assert status == 201 and body["feedbackId"]
    rows = call("GET", "/feedback", query={"projectId": "hr-assistant"}, persona="developer")[1]
    assert sorted(r["rating"] for r in rows) == ["down", "down", "up"]
    assert len(rows) == 3 and all(r["projectId"] == "hr-assistant" for r in rows)
    assert len({r["feedbackId"] for r in rows}) == 3 and len(call("GET", "/feedback")[1]) == 3
    assert (
        call("POST", "/feedback", body={"projectId": "hr-assistant", "env": "dev", "sessionId": "s", "rating": "meh"})[
            0
        ]
        == 400
    )
    assert (
        call("POST", "/feedback", body={"projectId": "nope", "env": "dev", "sessionId": "s", "rating": "up"})[0] == 404
    )
    assert call("POST", "/feedback", body={"projectId": "hr-assistant", "env": "dev", "rating": "up"})[0] == 400
    assert call("GET", "/feedback", query={"projectId": "nope"})[0] == 404
    assert call("DELETE", "/projects/hr-assistant")[0] == 200
    assert call("GET", "/feedback")[1] == []  # feedback goes with the project


def test_spend_by_env(ready):
    call("POST", "/projects/hr-assistant/keys", body={"env": "dev", "model": "gpt-6-1-sol"})
    call("POST", "/projects/hr-assistant/keys", body={"env": "prod", "model": "gpt-6-1-sol", "maxBudget": 2})
    for row in ready.fakes.litellm.keys.values():
        row["spend"] = 0.25 if row["key_alias"].startswith("hr-assistant-dev") else 0.5
    body = call("GET", "/spend", query={"projectId": "hr-assistant"})[1]
    assert body["byEnv"] == {
        "dev": {"keys": 1, "spend": 0.25, "maxBudget": 1.0, "blocked": 0},
        "prod": {"keys": 1, "spend": 0.5, "maxBudget": 2.0, "blocked": 0},
    }
    body = call("GET", "/spend", query={"projectId": "hr-assistant", "env": "prod"})[1]
    assert list(body["byEnv"]) == ["prod"] and [k["env"] for k in body["keys"]] == ["prod"]


def test_registry_event_records_the_record_id_and_status(table):
    app = entry_module("app")
    app.handler(
        {
            "source": "aws.agent-registry",
            "detail-type": "Registry Record Status Change",
            "detail": {"recordId": "M4RiHABnVjG5", "status": "APPROVED", "registryId": "DMC5ZiyCx3iVg5Y0"},
        },
        None,
    )
    item = db.get({"PK": "CATALOGUE", "SK": "SYNC"})
    assert item["recordId"] == "M4RiHABnVjG5" and item["status"] == "APPROVED" and item["lastEventAt"].endswith("Z")
    app.handler(
        {
            "source": "aws.agent-registry",
            "detail-type": "AWS API Call via CloudTrail",
            "detail": {
                "eventName": "UpdateRegistryRecordStatus",
                "requestParameters": {"recordId": "UD7QomWkcVCe", "status": "DEPRECATED"},
                "responseElements": {"status": "DEPRECATED"},
            },
        },
        None,
    )
    item = db.get({"PK": "CATALOGUE", "SK": "SYNC"})
    assert (
        item["recordId"] == "UD7QomWkcVCe"
        and item["status"] == "DEPRECATED"
        and item["eventName"] == "UpdateRegistryRecordStatus"
    )


def test_worker_entry_registers_the_job_handlers(table, monkeypatch):
    worker = entry_module("worker")
    for name in ("deploy", "promote", "invoke"):
        monkeypatch.delitem(jobs.HANDLERS, name, raising=False)
    with pytest.raises(ApiError):  # an unknown job is an invocation error, not a silent NOT_FOUND
        worker.handler({"jobId": "missing"}, None)
    assert set(jobs.HANDLERS) >= {"deploy", "promote", "invoke"}  # the entry re-registers them on every event


def _invoke_harness_headers(client) -> dict:
    """Headers botocore would send for InvokeHarness (captured in `before-send`, the request is never sent)."""

    class Captured(Exception):
        pass

    seen: dict = {}

    def capture(request, **_):
        seen.update({k: v.decode() if isinstance(v, bytes) else v for k, v in request.headers.items()})
        raise Captured

    client.meta.events.register("before-send.bedrock-agentcore.InvokeHarness", capture)
    try:
        with pytest.raises(Captured):
            client.invoke_harness(
                harnessArn=f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:harness/hr_assistant_dev-abc123",
                runtimeSessionId="a" * 40,
                messages=[{"role": "user", "content": [{"text": "hello"}]}],
            )
    finally:
        client.meta.events.unregister("before-send.bedrock-agentcore.InvokeHarness", capture)
    return seen


def test_worker_drops_the_lambda_trace_header_so_harness_sessions_are_sampled(table, monkeypatch):
    """backend-issues-wf3 issue 1: inside Lambda, botocore copies `_X_AMZN_TRACE_ID` (Sampled=0) into `X-Amzn-Trace-Id`, the
    harness honours the parent's decision and records no spans. The worker entry removes the variable on every event."""
    import boto3
    from botocore.config import Config

    worker = entry_module("worker")
    client = boto3.client(
        "bedrock-agentcore", region_name=REGION, config=Config(retries={"max_attempts": 1, "mode": "standard"})
    )
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "agent-platform-worker")
    monkeypatch.setenv("_X_AMZN_TRACE_ID", "Root=1-68e73a1f-0123456789abcdef01234567;Parent=0123456789abcdef;Sampled=0")
    before = _invoke_harness_headers(client)
    assert "Sampled=0" in before.get("X-Amzn-Trace-Id", ""), before  # the Lambda environment reproduces the defect
    with pytest.raises(ApiError):  # unknown job: the entry raises after its first statement removed the variable
        worker.handler({"jobId": "missing"}, None)
    assert "_X_AMZN_TRACE_ID" not in os.environ
    after = _invoke_harness_headers(client)
    assert "X-Amzn-Trace-Id" not in after, after
    assert "Authorization" in after  # the request was built and signed as usual


def test_invoke_tags_the_runtime_log_group_the_first_invocation_created(ready, monkeypatch):
    """Prod's runtime log group does not exist at promotion; the first invocation creates it and the invoke job tags it."""
    from botocore.exceptions import ClientError
    from platform_api.services import clients

    create_blueprint(ready)
    promoted_release(ready, 1)
    logs = clients.logs()
    name = "/aws/bedrock-agentcore/runtimes/harness_hr_assistant_prod-RT123-DEFAULT"
    assert logs.describe_log_groups(logGroupNamePrefix=name)["logGroups"] == []  # nothing to tag at promotion
    logs.create_log_group(logGroupName=name)  # what AgentCore Runtime does on the first invocation
    body = {"projectId": "hr-assistant", "env": "prod", "prompt": "What is my leave balance? I am E1001."}
    status, accepted = call("POST", "/invoke", body=body, persona="developer")
    assert status == 202
    result = run_job(accepted["jobId"])
    assert result["status"] == "SUCCEEDED", result
    arn = logs.describe_log_groups(logGroupNamePrefix=name)["logGroups"][0]["arn"].removesuffix(":*")
    tags = logs.list_tags_for_resource(resourceArn=arn)["tags"]
    assert tags["auto-delete"] == "no" and tags["ProjectId"] == "hr-assistant" and tags["Environment"] == "prod"

    # a group that is still missing is not an error; a tag call that is refused fails the job
    def denied(**params):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "TagResource")

    monkeypatch.setattr(logs, "tag_resource", denied)
    status, accepted = call("POST", "/invoke", body=body, persona="developer")
    result = run_job(accepted["jobId"])
    assert result["status"] == "FAILED" and "AccessDeniedException" in result["error"]["message"]
    logs.delete_log_group(logGroupName=name)
    status, accepted = call("POST", "/invoke", body=body, persona="developer")
    assert run_job(accepted["jobId"])["status"] == "SUCCEEDED"
