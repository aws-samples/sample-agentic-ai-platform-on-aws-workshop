#!/usr/bin/env python3
"""Scripted project lifecycle against the deployed platform API (Phase 2A exit criterion), with its own AgentCore resources.

Project `lt-hr-assistant` in domain `lt-ops` (never `hr-assistant`: the learning path owns that one), model `claude-haiku-4-5`:

  1. domain + project through the API (LiteLLM team + four roles)        module 3
  2. gateways lt-hr-assistant-{dev,prod}-gw with hrtools + hrkb targets,
     the shared policy engine from settings attached in LOG_ONLY (if any) module 4   (platform_sdk.agentcore, like `workshop apply 4`)
  3. memories for dev and prod                                            module 6
  4. blueprint v1 (prompt v1) -> render dry run (unresolved: no key yet) -> deploy to Dev (job) -> render -> invoke x2 (one session)   module 6
  5. release 1 + synthetic BLOCKED evaluation body; blueprint v2 -> deploy; release 2 + passing body -> PENDING_APPROVAL
     -> approve (approver) -> promote (job) -> PROMOTED, endpoint live, online evaluation registered -> invoke Prod      module 6
  6. metrics, spend, feedback                                                                                           module 7
  7. cleanup: harnesses, online evaluation config, gateways, memories, then DELETE /projects (keys, providers, roles, team)
     and the domain; verifies nothing with the lt- prefix remains                                                       `workshop cleanup`

    AWS_PROFILE=workshop uv run python scripts/lifecycle_test.py                 # full run with cleanup (15-20 min)
    AWS_PROFILE=workshop uv run python scripts/lifecycle_test.py --keep          # leave the resources for inspection
    AWS_PROFILE=workshop uv run python scripts/lifecycle_test.py --only-cleanup  # delete what an earlier run left

Every creator looks up by name first; a failed run is swept at the start of the next one. Resources created directly with the
SDK are tagged ManagedBy=dryrun. Exit code 0 only when every expectation holds and no lt- resource is left behind.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import yaml
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform_sdk"))

from platform_sdk.api import PlatformApiError, PlatformClient  # noqa: E402
from platform_sdk.templates import roles as role_templates  # noqa: E402

from platform_sdk import agentcore, auth, blueprint, config, identity, litellm, names  # noqa: E402

DOMAIN, PROJECT = "lt-ops", "lt-hr-assistant"
SCENARIO = ROOT / "scenario"
BLUEPRINT_DIR = SCENARIO / "blueprint"
BLUEPRINT_FILE = BLUEPRINT_DIR / "blueprint.yaml"
HR_SCHEMA = SCENARIO / "tools" / "hr" / "schema.json"
PROMPT_V2 = "prompts/hr-assistant-v2.md"
DEFAULT_RESULTS_DIR = Path("/home/ubuntu/.claude/jobs/e052367b/tmp/backend-lifecycle")
DRYRUN_TAGS = {"ManagedBy": "dryrun"}
PROMPTS = [
    "What is my leave balance? My employee id is E1001.",
    "Thanks. And how many sick days do I have left?",
]
PROD_PROMPT = "Which public holidays are there in November 2026?"
# Dry-run numbers of 8 Oct 2026 (dryrun-results/hr-assistant-v1-claude-haiku-4-5.json): prompt v1 misses three thresholds
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
PASSING_SCORES = {k: {**v, "mean": 1.0, "min": 1.0, "passRate": 1.0} for k, v in BLOCKED_SCORES.items()}
STARTED = time.monotonic()


def log(message: str) -> None:
    print(f"{datetime.now(UTC):%H:%M:%S} {time.monotonic() - STARTED:7.1f}s  {message}", flush=True)


class Timer:
    def __init__(self):
        self.steps: list[dict] = []

    @contextmanager
    def step(self, label: str):
        started = time.monotonic()
        log(f"--- {label}")
        try:
            yield
        except BaseException as exc:
            seconds = round(time.monotonic() - started, 1)
            self.steps.append({"step": label, "seconds": seconds, "ok": False, "error": str(exc)[:400]})
            log(f"[FAIL] {label} ({seconds} s): {exc}")
            raise
        seconds = round(time.monotonic() - started, 1)
        self.steps.append({"step": label, "seconds": seconds, "ok": True})
        log(f"[ok] {label} ({seconds} s)")


def synthetic_evaluation(scores: dict, run_id: str) -> dict:
    """A RunSummary.to_api_body() look-alike (the real dataset run is module 6's job, 6 minutes each)."""
    return {
        "runId": run_id,
        "datasetName": "hr-assistant.json",
        "scenarioCount": 10,
        "scores": scores,
        "results": [
            {
                "scenarioId": "leave-balance-e1001",
                "sessionId": "leave-balance-e1001-synthetic",
                "status": "COMPLETED",
                "scores": {k: 1.0 for k in scores},
                "labels": {"Correctness": "Correct"},
                "error": None,
                "errors": {},
            },
            {
                "scenarioId": "policy-remote-work-days",
                "sessionId": "policy-remote-work-days-synthetic",
                "status": "COMPLETED",
                "scores": {k: (1.0 if scores[k]["mean"] == 1.0 else 0.0) for k in scores},
                "error": None,
                "errors": {},
            },
        ],
        "failedScenarios": [],
    }


@dataclass
class Context:
    cfg: Any
    args: argparse.Namespace
    api: dict[str, PlatformClient]
    admin: litellm.LiteLLMAdmin
    control: Any
    timer: Timer
    record: dict = field(default_factory=dict)
    state: dict = field(default_factory=dict)
    problems: list = field(default_factory=list)

    @property
    def region(self) -> str:
        return self.cfg.region

    @property
    def model(self) -> str:
        return self.args.model

    def n(self, env: str) -> names.ProjectNames:
        return names.ProjectNames(PROJECT, env)

    def check(self, condition: bool, message: str) -> None:
        """Record an expectation; the run fails at the end when any expectation is unmet."""
        if condition:
            log(f"  check ok: {message}")
        else:
            self.problems.append(message)
            log(f"  CHECK FAILED: {message}")


def expect_error(fn, status: int, code: str | None = None) -> PlatformApiError | None:
    try:
        fn()
    except PlatformApiError as exc:
        if exc.status != status or (code and exc.code != code):
            raise AssertionError(f"expected {status} {code or ''}, got {exc.status} {exc.code}: {exc.message}") from exc
        return exc
    raise AssertionError(f"expected an error {status} {code or ''} but the call succeeded")


def load_blueprint(model: str, version: int) -> blueprint.Blueprint:
    document = yaml.safe_load(BLUEPRINT_FILE.read_text())
    document["metadata"]["project"] = PROJECT
    document["metadata"]["domain"] = DOMAIN
    document["metadata"]["version"] = version
    document["spec"]["model"] = {"ref": f"model/{model}"}
    if version >= 2:
        document["spec"]["systemPrompt"] = {"file": PROMPT_V2}
    return blueprint.load(document, base_dir=BLUEPRINT_DIR)


def wait_gone(lister, label: str, timeout: float, every: float = 5) -> float:
    started = time.monotonic()
    while lister():
        if time.monotonic() - started > timeout:
            raise agentcore.AgentCoreError(f"{label} still present after {timeout:.0f} s")
        time.sleep(every)
    return round(time.monotonic() - started, 1)


def retry(fn, attempts: int = 4, delay: float = 10, label: str = "call"):
    """Retry on ClientError (IAM propagation of freshly stamped roles: CreateGateway rejects the role for ~10 s)."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except (ClientError, agentcore.AgentCoreError) as exc:
            if attempt == attempts:
                raise
            log(f"  {label} attempt {attempt} failed ({str(exc)[:160]}); retrying in {delay:.0f} s")
            time.sleep(delay)


def wait_job(ctx: Context, client: PlatformClient, job_id: str, timeout: float, label: str) -> dict:
    started = time.monotonic()
    job = client.wait_for_job(job_id, timeout=timeout, poll=2)
    seconds = round(time.monotonic() - started, 1)
    status = job.get("status")
    if status == "SUCCEEDED":
        log(f"  job {job_id} ({label}) SUCCEEDED in {seconds} s (worker {job.get('output', {}).get('seconds')} s)")
    else:
        log(f"  job {job_id} ({label}) {status} in {seconds} s: {json.dumps(job.get('error'), default=str)[:400]}")
    ctx.record.setdefault("jobs", []).append({"label": label, "jobId": job_id, "status": status, "seconds": seconds})
    return job


# ---- sweep / cleanup -----------------------------------------------------------------------------------------------
def sweep(ctx: Context, label: str = "cleanup") -> list[str]:
    """Delete everything of the lifecycle project (AgentCore first, then the API's own resources). Returns leftovers."""
    region, control = ctx.region, ctx.control
    api = ctx.api["admin"]
    deleted: list[str] = []

    def attempt(what: str, fn):
        try:
            out = fn()
            if out:
                deleted.append(what)
            log(f"  {what}: {out}")
        except Exception as exc:  # noqa: BLE001 - keep deleting
            ctx.problems.append(f"{label} {what}: {exc}")
            log(f"  {what}: FAILED {str(exc)[:300]}")

    def delete_online_eval():
        found = agentcore.find_online_evaluation(ctx.n("prod").online_evaluation, region)
        if not found:
            return False
        control.delete_online_evaluation_config(onlineEvaluationConfigId=found["onlineEvaluationConfigId"])
        return found["onlineEvaluationConfigId"]

    attempt("online evaluation config", delete_online_eval)
    for env in names.ENVIRONMENTS:
        attempt(f"harness {env}", lambda env=env: agentcore.delete_harness(ctx.n(env).harness, region))
    for env in names.ENVIRONMENTS:
        attempt(f"gateway {env}", lambda env=env: agentcore.delete_gateway(ctx.n(env).gateway, region))
    for env in names.ENVIRONMENTS:
        attempt(f"memory {env}", lambda env=env: agentcore.delete_memory(ctx.n(env).memory, region))
    for env in names.ENVIRONMENTS:
        n = ctx.n(env)
        attempt(
            f"harness {env} gone",
            lambda n=n: wait_gone(lambda: agentcore.find_harness(n.harness, region), f"harness {n.harness}", 900),
        )
        attempt(
            f"gateway {env} gone",
            lambda n=n: wait_gone(lambda: agentcore.find_gateway(n.gateway, region), f"gateway {n.gateway}", 600),
        )
        attempt(
            f"memory {env} gone",
            lambda n=n: wait_gone(lambda: agentcore.find_memory(n.memory, region), f"memory {n.memory}", 600),
        )

    def delete_project():
        try:
            return api.delete_project(PROJECT, force=True)
        except PlatformApiError as exc:
            if exc.status == 404:
                return False
            raise

    def delete_domain():
        try:
            return api.delete_domain(DOMAIN)
        except PlatformApiError as exc:
            if exc.status == 404:
                return False
            raise

    attempt("DELETE /projects/lt-hr-assistant?force=true (keys, providers, team, roles, items)", delete_project)
    attempt("DELETE /domains/lt-ops", delete_domain)

    # belt and braces: anything the API did not know about (a failed run before registration)
    def direct_sweep():
        out = []
        for env in names.ENVIRONMENTS:
            n = ctx.n(env)
            for alias in {ctx.model, "claude-sonnet-5-5", "claude-haiku-4-5", "nova-2-lite"}:
                key = ctx.admin.find_key(n.key_alias(alias))
                if key:
                    ctx.admin.delete_key(key.get("token") or key.get("token_id"))
                    out.append(f"key {n.key_alias(alias)}")
                if identity.get_provider_arn(n.credential_provider(alias), region=region):
                    identity.delete_provider(n.credential_provider(alias), region=region)
                    out.append(f"provider {n.credential_provider(alias)}")
            out.extend(role_templates.delete_project_roles(ctx.cfg, PROJECT, env))
        if ctx.admin.find_team(PROJECT):
            ctx.admin.delete_team(PROJECT)
            out.append(f"team {PROJECT}")
        return out or False

    attempt("direct sweep of keys / providers / roles / team", direct_sweep)

    def delete_log_groups():
        logs_client = boto3.client("logs", region_name=region)
        prefix = f"/aws/bedrock-agentcore/runtimes/harness_{names.snake(PROJECT)}_"
        removed = []
        for attempt_no in range(2):
            found = [
                group["logGroupName"]
                for page in logs_client.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix)
                for group in page["logGroups"]
            ]
            for name in found:
                logs_client.delete_log_group(logGroupName=name)
                removed.append(name)
            if not found or attempt_no:
                break
            time.sleep(60)  # late log delivery recreated a runtime log group once in the dry run
        return removed or False

    attempt("harness runtime log groups", delete_log_groups)
    return inventory(ctx)


def inventory(ctx: Context) -> list[str]:
    """Everything that still carries the lifecycle prefix (AgentCore, IAM, Identity, LiteLLM, the table)."""
    region, control = ctx.region, ctx.control
    left: list[str] = []
    snake = names.snake(PROJECT)
    for item in agentcore._paginate(control, "list_harnesses", "harnesses"):
        if item.get("harnessName", "").startswith(snake):
            left.append(f"harness {item['harnessName']} {item.get('status')}")
    for item in agentcore._paginate(control, "list_gateways", "items"):
        if item.get("name", "").startswith(PROJECT):
            left.append(f"gateway {item['name']} {item.get('status')}")
    for item in agentcore._paginate(control, "list_memories", "memories"):
        if item.get("id", "").startswith(snake):
            left.append(f"memory {item['id']} {item.get('status')}")
    for item in agentcore._paginate(control, "list_online_evaluation_configs", "onlineEvaluationConfigs"):
        if item.get("onlineEvaluationConfigName", "").startswith(snake):
            left.append(f"online evaluation {item['onlineEvaluationConfigName']}")
    for provider in identity.list_providers(region):
        if provider["name"].startswith(f"litellm-{PROJECT}-"):
            left.append(f"credential provider {provider['name']}")
    iam = boto3.client("iam", region_name=region)
    for page in iam.get_paginator("list_roles").paginate(PathPrefix="/"):
        for role in page["Roles"]:
            if role["RoleName"].startswith(f"agent-platform-proj-{PROJECT}-"):
                left.append(f"role {role['RoleName']}")
    if ctx.admin.find_team(PROJECT):
        left.append(f"LiteLLM team {PROJECT}")
    for env in names.ENVIRONMENTS:
        for alias in (ctx.model, "claude-sonnet-5-5", "claude-haiku-4-5", "nova-2-lite"):
            if ctx.admin.find_key(ctx.n(env).key_alias(alias)):
                left.append(f"LiteLLM key {ctx.n(env).key_alias(alias)}")
    table = boto3.resource("dynamodb", region_name=region).Table(ctx.cfg.table_name)
    for pk in (f"PROJECT#{PROJECT}", f"DOMAIN#{DOMAIN}", f"FEEDBACK#{PROJECT}"):
        items = table.query(KeyConditionExpression=Key("PK").eq(pk), ProjectionExpression="PK, SK")["Items"]
        left.extend(f"table item {i['PK']} / {i['SK']}" for i in items)
    log(f"  leftovers with the lt- prefix: {left or 'none'}")
    return left


# ---- steps -----------------------------------------------------------------------------------------------------------
def create_project(ctx: Context) -> dict:
    admin, cfg = ctx.api["admin"], ctx.cfg
    try:
        domain = admin.create_domain(DOMAIN, "Lifecycle test ops", "scripts/lifecycle_test.py", "backend-test")
    except PlatformApiError as exc:
        if exc.status != 409:
            raise
        domain = admin.get_domain(DOMAIN)
    log(f"  domain {domain['domainId']} ({domain.get('name')})")
    body = {
        "projectId": PROJECT,
        "domainId": DOMAIN,
        "name": "Lifecycle test HR assistant",
        "description": "created by scripts/lifecycle_test.py",
        "models": [ctx.model],
        "maxBudget": 2,
        "budgetDuration": "7d",
        "tpmLimit": 100000,
        "rpmLimit": 100,
        "allowedToolArns": [cfg.hr_tools_lambda_arn],
        "knowledgeBaseIds": [cfg.kb_id],
    }
    started = time.monotonic()
    project = admin.create_project(body)
    log(
        f"  project {project['projectId']} created in {time.monotonic() - started:.1f} s: team {project['litellm']['teamId']}"
    )
    for env in names.ENVIRONMENTS:
        log(f"  roles {env}: {project['roles'][env]}")
    expect_error(lambda: ctx.api["developer"].create_project(body), 403, "FORBIDDEN")
    ctx.state["project"] = project
    return project


def create_gateways(ctx: Context, project: dict) -> None:
    """Module 4 for both environments: gateway (engine from settings in LOG_ONLY), hrtools + hrkb targets, registered."""
    region, dev = ctx.region, ctx.api["developer"]
    settings = ctx.api["admin"].get_settings()
    engine_arn = settings.get("policyEngineArn")
    log(f"  shared policy engine: {engine_arn or 'none in settings (skipping the engine)'}")
    schema = json.loads(HR_SCHEMA.read_text())
    tool_names = [f"hrtools___{t['name']}" for t in schema]
    time.sleep(10)  # IAM propagation of the freshly stamped gateway roles (the dry run needed ~10 s)
    for env in names.ENVIRONMENTS:
        n = ctx.n(env)
        role_arn = project["roles"][env]["gatewayRoleArn"]
        gateway = retry(
            lambda n=n, role_arn=role_arn, env=env: agentcore.ensure_gateway(
                n.gateway,
                role_arn,
                engine_arn,
                "LOG_ONLY",
                description=f"lifecycle test gateway ({env})",
                tags=DRYRUN_TAGS,
                region=region,
            ),
            label=f"CreateGateway {n.gateway}",
        )
        mode = (gateway.get("policyEngineConfiguration") or {}).get("mode")
        log(f"  gateway {gateway['gatewayId']} {gateway['status']} mode {mode}")
        hrtools = agentcore.ensure_lambda_target(
            gateway["gatewayId"], "hrtools", ctx.cfg.hr_tools_lambda_arn, HR_SCHEMA, "AnyCompany HR tools", region
        )
        hrkb = agentcore.ensure_kb_target(
            gateway["gatewayId"], "hrkb", ctx.cfg.kb_id, "HR", description="AnyCompany HR policies", region=region
        )
        log(f"  targets hrtools {hrtools['targetId']} {hrtools['status']}, hrkb {hrkb['targetId']} {hrkb['status']}")
        dev.register_resource(
            PROJECT,
            env,
            "gateway",
            n.gateway,
            arn=gateway["gatewayArn"],
            id=gateway["gatewayId"],
            url=gateway["gatewayUrl"],
        )
        dev.register_resource(
            PROJECT,
            env,
            "gateway_target",
            "hrtools",
            id=hrtools["targetId"],
            attributes={
                "kind": "lambda",
                "gatewayArn": gateway["gatewayArn"],
                "lambdaArn": ctx.cfg.hr_tools_lambda_arn,
                "tools": tool_names,
            },
        )
        dev.register_resource(
            PROJECT,
            env,
            "gateway_target",
            "hrkb",
            id=hrkb["targetId"],
            attributes={
                "kind": "knowledge_base",
                "gatewayArn": gateway["gatewayArn"],
                "knowledgeBaseId": ctx.cfg.kb_id,
                "filter": {"equals": {"key": "department", "value": "HR"}},
                "tools": ["hrkb___Retrieve"],
            },
        )
        ctx.state[f"gateway.{env}"] = {"id": gateway["gatewayId"], "arn": gateway["gatewayArn"], "mode": mode}


def create_memories(ctx: Context, bp: blueprint.Blueprint) -> None:
    region, dev = ctx.region, ctx.api["developer"]
    pending = {}
    for env in names.ENVIRONMENTS:
        n = ctx.n(env)
        memory = agentcore.ensure_memory(
            n.memory,
            bp.memory_strategies(),
            description=f"lifecycle test memory ({env})",
            tags=DRYRUN_TAGS,
            region=region,
            wait=False,
        )
        pending[env] = memory
        log(f"  memory {memory['id']} {memory.get('status')} (created, waiting for ACTIVE)")
    for env, memory in pending.items():
        memory = agentcore.wait_memory(memory["id"], region)
        log(f"  memory {memory['id']} {memory['status']}")
        dev.register_resource(
            PROJECT,
            env,
            "memory",
            ctx.n(env).memory,
            arn=memory["arn"],
            id=memory["id"],
            attributes={"strategies": bp.memory_strategies()},
        )
        ctx.state[f"memory.{env}"] = {"id": memory["id"], "arn": memory["arn"]}


def create_blueprint(ctx: Context, version: int) -> dict:
    bp = load_blueprint(ctx.model, version)
    dev = ctx.api["developer"]
    item = dev.create_blueprint(PROJECT, bp.api_spec(), bp.system_prompt, notes=f"lifecycle test v{version}")
    log(f"  blueprint version {item['version']} model {item.get('model')} thresholds {item.get('thresholds')}")
    ctx.check(item["version"] == version, f"blueprint version is {version}")
    again = dev.create_blueprint(PROJECT, bp.api_spec(), bp.system_prompt, notes="again")
    ctx.check(
        again.get("version") == version and again.get("existing") is True, "re-posting the same blueprint is idempotent"
    )
    return item


def deploy(ctx: Context, version: int) -> dict:
    dev = ctx.api["developer"]
    expect_error(lambda: ctx.api["approver"].deploy(PROJECT, version, "dev"), 403, "FORBIDDEN")
    expect_error(lambda: dev.deploy(PROJECT, version, "prod"), 400, "VALIDATION_ERROR")
    accepted = dev.deploy(PROJECT, version, "dev")
    log(f"  POST /deploy -> 202 job {accepted['jobId']} ({accepted['status']})")
    job = wait_job(ctx, dev, accepted["jobId"], 600, f"deploy v{version}")
    ctx.check(job.get("status") == "SUCCEEDED", f"deploy job v{version} succeeded")
    out = job.get("output") or {}
    log(f"  output: {json.dumps(out, default=str)[:600]}")
    ctx.check(str(out.get("harnessVersion")) == str(version), f"harness version {version} after deploy v{version}")
    rows = {(r["type"], r["name"]): r for r in dev.list_resources(PROJECT, "dev")}
    ctx.check(("harness", ctx.n("dev").harness) in rows, "harness resource registered for dev")
    ctx.check(("harness_endpoint", "DEFAULT") in rows, "harness_endpoint DEFAULT registered for dev")
    endpoint = rows.get(("harness_endpoint", "DEFAULT")) or {}
    log(f"  DEFAULT endpoint resource: arn {endpoint.get('arn')} attributes {endpoint.get('attributes')}")
    ctx.state["harness.dev"] = {
        "arn": out.get("harnessArn"),
        "id": out.get("harnessId"),
        "version": out.get("harnessVersion"),
    }
    return out


def invoke(ctx: Context, env: str, prompt: str, session_id: str | None, label: str, timeout: float = 420) -> dict:
    client = ctx.api["developer"]
    accepted = client.invoke(PROJECT, env, prompt, session_id)
    ctx.check(len(accepted.get("sessionId", "")) >= 33, f"{label}: session id has at least 33 characters")
    if session_id:
        ctx.check(accepted["sessionId"] == session_id, f"{label}: the given session id is kept")
    job = wait_job(ctx, client, accepted["jobId"], timeout, label)
    out = job.get("output") or {}
    if job.get("status") == "SUCCEEDED":
        log(
            f"  {label}: {out.get('seconds')} s, tools {out.get('toolsUsed')}, stop {out.get('stopReason')}, "
            f"usage {out.get('usage')}"
        )
        log(f"    > {str(out.get('text', '')).strip()[:300]!r}")
    ctx.check(job.get("status") == "SUCCEEDED", f"{label}: invoke job succeeded")
    ctx.check(bool(str(out.get("text", "")).strip()), f"{label}: the answer has text")
    return {**job, "sessionId": accepted["sessionId"]}


def release_flow(ctx: Context) -> dict:
    dev, approver, admin = ctx.api["developer"], ctx.api["approver"], ctx.api["admin"]

    release1 = dev.create_release(PROJECT, 1, notes="lifecycle release 1 (prompt v1)", auto_run=False)
    ctx.check(release1["status"] == "DRAFT", "release 1 starts as DRAFT (autoRun off: the evaluation is posted below)")
    expect_error(lambda: approver.approve_release(release1["releaseId"], "too early"), 409)
    blocked = dev.submit_evaluation(release1["releaseId"], synthetic_evaluation(BLOCKED_SCORES, "run-synthetic-v1"))
    failed = sorted(f["evaluator"] for f in blocked["evaluation"]["failures"])
    log(f"  release 1 -> {blocked['status']}, failures {failed}")
    ctx.check(blocked["status"] == "BLOCKED", "release 1 is BLOCKED by the v1 scores")
    ctx.check(
        failed == ["Correctness", "GoalSuccessRate", "TrajectoryInOrderMatch"],
        "release 1 failures name the three evaluators",
    )
    expect_error(lambda: approver.promote_release(release1["releaseId"]), 409)

    with ctx.timer.step("5b. blueprint v2 (prompt v2) + deploy to Dev"):
        create_blueprint(ctx, 2)
        out = deploy(ctx, 2)
        ctx.record["deployV2"] = out

    release2 = dev.create_release(PROJECT, 2, notes="lifecycle release 2 (prompt v2)", auto_run=False)
    pending = dev.submit_evaluation(release2["releaseId"], synthetic_evaluation(PASSING_SCORES, "run-synthetic-v2"))
    log(f"  release 2 -> {pending['status']} (verdict {pending['evaluation']['verdict']})")
    ctx.check(pending["status"] == "PENDING_APPROVAL", "release 2 is PENDING_APPROVAL with passing scores")
    expect_error(lambda: dev.approve_release(release2["releaseId"], "dev cannot approve"), 403, "FORBIDDEN")
    expect_error(lambda: admin.approve_release(release2["releaseId"], "admin cannot approve"), 403, "FORBIDDEN")
    approved = approver.approve_release(release2["releaseId"], "scores above every threshold")
    ctx.check(
        approved["status"] == "APPROVED" and approved["approval"]["by"] == "approver",
        "release 2 APPROVED by the approver",
    )
    expect_error(lambda: dev.promote_release(release2["releaseId"]), 403, "FORBIDDEN")
    expect_error(lambda: dev.invoke(PROJECT, "prod", PROD_PROMPT), 409, "HARNESS_NOT_DEPLOYED")

    accepted = approver.promote_release(release2["releaseId"])
    log(f"  POST /promote -> 202 job {accepted['jobId']} ({accepted['status']})")
    ctx.check(
        approver.get_release(release2["releaseId"])["status"] == "PROMOTING",
        "release 2 is PROMOTING while the job runs",
    )
    job = wait_job(ctx, approver, accepted["jobId"], 900, "promote release 2")
    ctx.check(job.get("status") == "SUCCEEDED", "promote job succeeded")
    out = job.get("output") or {}
    log(f"  output: {json.dumps(out, default=str)[:700]}")
    final = approver.get_release(release2["releaseId"])
    log(f"  release 2 -> {final['status']} promotion {json.dumps(final.get('promotion'), default=str)[:300]}")
    ctx.check(final["status"] == "PROMOTED", "release 2 is PROMOTED")
    ctx.check((final.get("promotion") or {}).get("endpoint") == "live", "promotion endpoint is live")
    rows = {(r["type"], r["name"]): r for r in dev.list_resources(PROJECT, "prod")}
    n = ctx.n("prod")
    ctx.check(("harness", n.harness) in rows, "prod harness registered")
    ctx.check(("harness_endpoint", "live") in rows, "prod harness_endpoint live registered")
    ctx.check(("online_evaluation", n.online_evaluation) in rows, "online evaluation config registered")
    ctx.check(("credential_provider", n.credential_provider(ctx.model)) in rows, "prod credential provider registered")
    live = rows.get(("harness_endpoint", "live")) or {}
    log(f"  live endpoint resource: arn {live.get('arn')} attributes {live.get('attributes')}")
    online = agentcore.find_online_evaluation(n.online_evaluation, ctx.region)
    ctx.check(bool(online) and online.get("status") == "ACTIVE", "online evaluation config is ACTIVE in AgentCore")
    keys = {k["env"]: k for k in dev.list_keys(PROJECT)}
    ctx.check("prod" in keys, "prod key created by the promotion")
    if "prod" in keys:
        info = ctx.admin.key_info(keys["prod"]["litellmKeyId"])
        provider = identity.get_provider_arn(n.credential_provider(ctx.model), region=ctx.region)
        ctx.check(info.get("key_alias") == n.key_alias(ctx.model), "prod LiteLLM key alias matches the contract")
        ctx.check(provider == keys["prod"]["credentialProviderArn"], "prod credential provider holds the key")
    ctx.state["harness.prod"] = {
        "arn": out.get("harnessArn"),
        "id": out.get("harnessId"),
        "version": out.get("harnessVersion"),
    }
    ctx.record["promote"] = out
    return final


def observe(ctx: Context, dev_session: str) -> None:
    dev = ctx.api["developer"]
    for env in names.ENVIRONMENTS:
        metrics = dev.metrics(PROJECT, env, hours=1)
        series = metrics.get("series") or {}
        log(
            f"  metrics {env}: harness {bool(metrics.get('harness'))}, policy {bool(metrics.get('policy'))}, "
            f"evaluation {bool(metrics.get('evaluation'))}, totals {metrics.get('totals')}"
        )
        ctx.check(isinstance(series.get("invocations"), list), f"metrics {env} has an invocations series")
        ctx.check(metrics.get("harness") is not None, f"metrics {env} found the registered harness")
    # CloudWatch publishes the built-in metrics within a few minutes: poll briefly, report, never block the run on it
    deadline = time.monotonic() + 180
    invocations = 0.0
    while time.monotonic() < deadline:
        invocations = float((dev.metrics(PROJECT, "dev", hours=1).get("totals") or {}).get("invocations") or 0)
        if invocations > 0:
            break
        time.sleep(15)
    log(f"  dev invocations visible in CloudWatch: {invocations}")
    ctx.record["devInvocationsMetric"] = invocations

    time.sleep(15)  # LiteLLM writes spend in batches (proxy_batch_write_at 10 s)
    spend = dev.spend(PROJECT)
    by_env = spend.get("byEnv") or {}
    log(
        f"  spend: team {spend.get('team', {}).get('spend')} USD, byEnv {by_env}, {len(spend.get('logs') or [])} log rows"
    )
    ctx.check(float(by_env.get("dev", {}).get("spend") or 0) > 0, "dev key shows spend after the invocations")
    ctx.check("prod" in by_env, "spend has a prod row")
    ctx.record["spend"] = {"byEnv": by_env, "logRows": len(spend.get("logs") or [])}

    feedback = dev.feedback(PROJECT, "dev", dev_session, "up", "lifecycle test: good answer")
    ctx.check(feedback.get("rating") == "up", "feedback stored")
    rows = dev.list_feedback(PROJECT)
    ctx.check(any(r.get("sessionId") == dev_session for r in rows), "feedback listed for the project")
    expect_error(lambda: dev.get_job("01J00000000000000000000000"), 404, "NOT_FOUND")
    gov = ctx.api["approver"].governance()
    mine = [g for g in gov.get("gateways", []) if g.get("projectId") == PROJECT]
    log(
        f"  governance shows {len(mine)} lifecycle gateways: {[(g['env'], g.get('mode'), g.get('status')) for g in mine]}"
    )
    ctx.check(len(mine) == 2, "governance lists both lifecycle gateways")


def run(ctx: Context) -> None:
    timer = ctx.timer
    with timer.step("0. sweep leftovers of earlier runs"):
        sweep(ctx, "sweep")
    with timer.step("1. domain + project through the API (module 3)"):
        project = create_project(ctx)
    with timer.step("2. gateways + targets for dev and prod, engine LOG_ONLY (module 4)"):
        create_gateways(ctx, project)
    bp1 = load_blueprint(ctx.model, 1)
    with timer.step("3. memories for dev and prod (module 6)"):
        create_memories(ctx, bp1)
    with timer.step("4a. blueprint v1 + render dry run without a key (module 6)"):
        create_blueprint(ctx, 1)
        error = expect_error(
            lambda: ctx.api["developer"].render_blueprint(PROJECT, 1, "dev"), 400, "BLUEPRINT_UNRESOLVED"
        )
        log(f"  render before deploy -> 400 {error.code}: {error.message[:160]}")
        ctx.check("module 5" in error.message, "the unresolved render names module 5 (the key)")
    with timer.step("4b. deploy v1 to Dev (job: key + harness + resources)"):
        ctx.record["deployV1"] = deploy(ctx, 1)
    with timer.step("4c. render dry run after deploy"):
        spec = ctx.api["approver"].render_blueprint(PROJECT, 1, "dev")
        model_cfg = spec["model"]["liteLlmModelConfig"]
        key = {k["env"]: k for k in ctx.api["developer"].list_keys(PROJECT)}["dev"]
        log(
            f"  rendered model {model_cfg['modelId']} key {model_cfg['apiKeyArn'][-60:]} tools {spec.get('allowedTools')}"
        )
        ctx.check(model_cfg["modelId"] == f"openai/{ctx.model}", "render uses the blueprint model through LiteLLM")
        ctx.check(model_cfg["apiKeyArn"] == key["credentialProviderArn"], "render uses the deploy-created key")
        ctx.check(spec["tags"]["Environment"] == "dev", "render carries the tags")
    with timer.step("4d. invoke Dev twice in one session"):
        first = invoke(ctx, "dev", PROMPTS[0], None, "invoke dev 1 (new session)")
        second = invoke(ctx, "dev", PROMPTS[1], first["sessionId"], "invoke dev 2 (same session)")
        ctx.record["invokeDev"] = [first.get("output", {}).get("seconds"), second.get("output", {}).get("seconds")]
        ctx.state["devSession"] = first["sessionId"]
    with timer.step("5a. release 1 (BLOCKED) ... release 2 (PENDING_APPROVAL -> APPROVED -> PROMOTED) (module 6)"):
        release_flow(ctx)
    with timer.step("6a. invoke Prod (qualifier live)"):
        prod = invoke(ctx, "prod", PROD_PROMPT, None, "invoke prod (new session)")
        ctx.check((prod.get("output") or {}).get("qualifier") == "live", "prod invoke used the live qualifier")
        ctx.record["invokeProd"] = (prod.get("output") or {}).get("seconds")
    with timer.step("6b. metrics, spend, feedback, governance (module 7)"):
        observe(ctx, ctx.state["devSession"])


# ---- main ------------------------------------------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", default="claude-haiku-4-5", help="approved model alias (default: claude-haiku-4-5)")
    parser.add_argument("--keep", action="store_true", help="leave everything running (skip the cleanup)")
    parser.add_argument("--only-cleanup", action="store_true", help="delete what an earlier run left and exit")
    parser.add_argument("--results-dir", type=Path, default=DEFAULT_RESULTS_DIR, help="where the run record is written")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.results_dir.mkdir(parents=True, exist_ok=True)
    cfg = config.load()
    secret = litellm.read_master_key(cfg)
    # through the relay on the private landing zone (the proxy's load balancer is internal), direct HTTP on a public one
    admin = litellm.admin_client(cfg, secret)
    log(
        f"account {cfg.account_id} region {cfg.region} API {cfg.api_url} project {PROJECT}/{DOMAIN} model {args.model} "
        f"model gateway via {admin.transport}"
    )
    api = {}
    for persona in auth.PERSONAS:
        started = time.monotonic()
        tokens = auth.login(persona, cfg=cfg)
        api[persona] = PlatformClient(cfg, tokens.id_token, timeout=60)
        log(f"  signed in as {persona} ({tokens.role}) in {time.monotonic() - started:.1f} s")
    ctx = Context(
        cfg=cfg,
        args=args,
        api=api,
        admin=admin,
        control=agentcore.control_client(cfg.region),
        timer=Timer(),
    )
    ctx.record.update({"startedAt": datetime.now(UTC).isoformat(), "account": cfg.account_id, "model": args.model})
    if args.only_cleanup:
        with ctx.timer.step("cleanup only"):
            leftovers = sweep(ctx)
        return 1 if leftovers or ctx.problems else 0

    error = None
    leftovers: list[str] = []
    try:
        run(ctx)
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - recorded, then cleanup
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        log(f"RUN FAILED: {error}")
    finally:
        if args.keep:
            log("--keep: leaving everything in place (run again with --only-cleanup to delete)")
        else:
            try:
                with ctx.timer.step("7. cleanup (workshop cleanup)"):
                    leftovers = sweep(ctx)
            except Exception as exc:  # noqa: BLE001
                error = (error or "") + f" cleanup: {exc}"
                traceback.print_exc()
        ok = not error and not ctx.problems and not leftovers
        ctx.record.update(
            {
                "finishedAt": datetime.now(UTC).isoformat(),
                "totalSeconds": round(time.monotonic() - STARTED, 1),
                "ok": ok,
                "error": error,
                "unmetChecks": ctx.problems,
                "leftovers": leftovers,
                "steps": ctx.timer.steps,
                "state": ctx.state,
            }
        )
        path = args.results_dir / f"lifecycle-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
        path.write_text(json.dumps(ctx.record, indent=1, default=str))
        log(f"run record {path}")
        log("timings: " + ", ".join(f"{s['step'].split('.')[0]}={s['seconds']}s" for s in ctx.timer.steps))
        if ctx.problems:
            log(f"unmet checks ({len(ctx.problems)}): {ctx.problems}")
        log(f"total {time.monotonic() - STARTED:.0f} s, result {'OK' if ok else 'NOT OK'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
