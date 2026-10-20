#!/usr/bin/env python3
"""End-to-end dry run of modules 3-6 against the deployed foundation (Phase 0 / Phase 4A integration).

Everything an attendee does in modules 3-6, scripted with `platform_sdk` only, for project `hr-assistant` in environment
`dev`:

1. module 5  LiteLLM team for the project and a service-account key for (dev, model), stored in AgentCore Identity
2. module 3  the two project roles from the platform templates (HR tools Lambda and the policy knowledge base only)
3. module 4  policy engine, gateway (AWS_IAM) with the `hrtools` Lambda target and the `hrkb` knowledge-base connector,
             Cedar permits for every tool, the `days > 10` forbid on submit_leave_request, LOG_ONLY -> ENFORCE
4. module 6  memory with the blueprint's strategies
5. module 6  blueprint rendered with prompt v1 into the Dev harness, warm-up (cold start), sample prompts, spend on the key
6. module 6  10-scenario dataset run that must BLOCK on v1, prompt fix (v2), second run that must pass
7. cleanup   of everything the run created (default; `--keep` skips it, `--only-cleanup` does only this)

    AWS_PROFILE=workshop uv run python scripts/e2e_dryrun.py                      # full run with cleanup
    AWS_PROFILE=workshop uv run python scripts/e2e_dryrun.py --skip-eval --keep    # infrastructure + sample prompts only
    AWS_PROFILE=workshop uv run python scripts/e2e_dryrun.py --only-cleanup

Every creator looks up by name first, so the script can be re-run after a failure. Resources are tagged
Project=workshop-agent-platform, ManagedBy=dryrun. Results go to .claude/docs/dryrun-results/ (run summary with timings,
the two dataset RunSummary files, a small state file). Nothing here depends on a particular account: everything comes from
SSM (`platform_sdk.config`) and the environment's credentials and region.
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
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "platform_sdk"))

from platform_sdk.state import WorkshopState  # noqa: E402
from platform_sdk.templates import roles  # noqa: E402

from platform_sdk import agentcore, blueprint, config, evaluation, identity, litellm, names  # noqa: E402

PROJECT_ID, ENV, DOMAIN = "hr-assistant", "dev", "people-ops"
SCENARIO = ROOT / "scenario"
BLUEPRINT_DIR = SCENARIO / "blueprint"
BLUEPRINT_FILE = BLUEPRINT_DIR / "blueprint.yaml"
PROJECT_FILE = BLUEPRINT_DIR / "project.yaml"
DATASET_FILE = SCENARIO / "evaluation" / "hr-assistant.json"
PROMPT_V2 = "prompts/hr-assistant-v2.md"
DEFAULT_RESULTS_DIR = ROOT / ".claude" / "docs" / "dryrun-results"
DRYRUN_TAGS = {"ManagedBy": "dryrun"}
FORBID_RULE, FORBID_TOOL, FORBID_DAYS = "forbid_long_leave", "submit_leave_request", 10
POLICY_SCENARIOS = ("policy-remote-work-days", "policy-annual-leave-accrual-carry-over", "policy-parental-leave-weeks")
WARM_UP_PROMPT = "Hello! In one sentence, what can you help me with?"
SAMPLE_PROMPTS = [
    ("leave balance E1001", "What is my leave balance? My employee id is E1001."),
    ("remote-work policy", "How many days per week can I work remotely, and which day do I have to be in the office?"),
    ("denied topic (guardrail)", "Can I sue my manager for denying my leave?"),
    ("policy probe: 12-day request", "Submit an annual leave request for employee E1012: 12 days starting 2027-02-01."),
]
STARTED = time.monotonic()


def log(message: str) -> None:
    print(f"{datetime.now(UTC):%H:%M:%S} {time.monotonic() - STARTED:7.1f}s  {message}", flush=True)


class Timer:
    """Records step durations: `with timer.step("label"): ...` prints [ok]/[FAIL] with the seconds taken."""

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


@dataclass
class Context:
    """Everything the steps share: configuration, names, the LiteLLM admin client, the state file and the run record."""

    cfg: Any
    n: names.ProjectNames
    args: argparse.Namespace
    admin: litellm.LiteLLMAdmin
    state: WorkshopState
    timer: Timer
    project_spec: dict
    record: dict = field(default_factory=dict)

    @property
    def region(self) -> str:
        return self.cfg.region

    @property
    def model(self) -> str:
        return self.args.model

    def resolve(self, value: Any) -> Any:
        """project.yaml placeholders `ssm:/agent-platform/<key>` -> the published SSM value."""
        if isinstance(value, str) and value.startswith("ssm:"):
            key = value[len("ssm:") :].removeprefix(config.PREFIX).strip("/")
            return self.cfg.require(key)
        return value


# ---- helpers -------------------------------------------------------------------------------------------------------
def litellm_admin(cfg) -> litellm.LiteLLMAdmin:
    """Master-key client for the model gateway (the key lives in Secrets Manager, ARN from SSM): through the relay Lambda on the
    private landing zone (the proxy's load balancer is internal), direct HTTP on a public one (`litellm.admin_client`)."""
    return litellm.admin_client(cfg, litellm.read_master_key(cfg))


def load_blueprint(model: str, version: int) -> blueprint.Blueprint:
    """The scenario blueprint with the model alias overridden and, for version 2, the fixed prompt."""
    document = yaml.safe_load(BLUEPRINT_FILE.read_text())
    document["spec"]["model"] = {"ref": f"model/{model}"}
    if version >= 2:
        document["metadata"]["version"] = version
        document["spec"]["systemPrompt"] = {"file": PROMPT_V2}
    return blueprint.load(document, base_dir=BLUEPRINT_DIR)


def wait_gone(lister, label: str, timeout: float = 180, every: float = 5) -> None:
    started = time.monotonic()
    while lister():
        if time.monotonic() - started > timeout:
            raise agentcore.AgentCoreError(f"{label} still present after {timeout:.0f} s")
        time.sleep(every)


def converse(ctx: Context, harness_arn: str, label: str, prompt: str) -> dict:
    """One invocation of the Dev harness; prints text, tools, usage and seconds; never raises."""
    row: dict[str, Any] = {"label": label, "prompt": prompt}
    try:
        result = agentcore.invoke(harness_arn, prompt, region=ctx.region)
        row.update(result.summary())
        log(
            f"  {label}: {result.seconds:.1f} s, tools {result.tools}, stop {result.stop_reason}, "
            f"usage {result.usage['inputTokens']}/{result.usage['outputTokens']} tokens in {result.usage['modelCalls']} calls"
        )
        log(f"    > {result.text.strip()[:400]!r}")
    except agentcore.HarnessInvokeError as exc:
        row.update({"error": exc.message, "budgetExceeded": exc.budget_exceeded, "sessionId": exc.session_id})
        log(f"  {label}: HarnessInvokeError budget_exceeded={exc.budget_exceeded}: {exc.message[:300]}")
    return row


def print_scores(summary: evaluation.RunSummary, verdict: evaluation.Verdict) -> None:
    evaluators = list(summary.scores)
    width = max(len(r["scenarioId"] or "") for r in summary.results) if summary.results else 20
    print(f"\n  {'scenario':<{width}}  " + "  ".join(f"{evaluation.evaluator_key(e):>22}" for e in evaluators))
    for row in summary.results:
        cells = []
        for e in evaluators:
            if e in row["scores"]:
                cells.append(f"{row['scores'][e]:>22.2f}")
            elif e in row["errors"]:
                cells.append(f"{'n/a (' + row['errors'][e][:12] + ')':>22}")
            else:
                cells.append(f"{'-':>22}")
        status = "" if row["status"] == "COMPLETED" else f"  <- {row['status']} {str(row.get('error'))[:60]}"
        print(f"  {row['scenarioId']:<{width}}  " + "  ".join(cells) + status)
    print(
        f"\n  {'evaluator':<24} {'mean':>6} {'min':>6} {'passRate':>9} {'count':>6} {'errors':>7} {'threshold':>10} result"
    )
    for name, detail in verdict.details.items():
        score = summary.scores.get(name) or {}
        print(
            f"  {name:<24} {str(score.get('mean')):>6} {str(score.get('min')):>6} {str(score.get('passRate')):>9} "
            f"{score.get('count', 0):>6} {score.get('errors', 0):>7} {detail['op'] + str(detail['threshold']):>10} "
            f"{'PASS' if detail['passed'] else 'FAIL'}"
        )
    print(f"\n  verdict: {verdict.status} ({len(summary.results)} scenarios in {summary.seconds} s)\n", flush=True)


# ---- steps ---------------------------------------------------------------------------------------------------------
def ensure_team_and_key(ctx: Context, bp: blueprint.Blueprint) -> dict:
    """Module 5: team per project, service-account key per (env, model), raw key stored as a credential provider."""
    spec, caps = ctx.project_spec, ctx.project_spec["caps"]
    team = ctx.admin.create_team(
        ctx.n.team,
        ctx.n.team,
        spec["models"],
        caps["maxBudget"],
        caps["budgetDuration"],
        caps["tpmLimit"],
        caps["rpmLimit"],
        {"domain": DOMAIN, "project": PROJECT_ID},
    )
    info = team.get("team_info") or {}
    log(
        f"  team {team.get('team_id')}: models {info.get('models')}, max_budget {info.get('max_budget')} {info.get('budget_duration')}"
    )
    alias, provider_name = ctx.n.key_alias(ctx.model), ctx.n.credential_provider(ctx.model)
    existing = ctx.admin.find_key(alias)
    provider_arn = identity.get_provider_arn(provider_name, ctx.region)
    if existing and provider_arn:
        token = existing.get("token") or existing.get("token_id")
        log(f"  key {alias} exists (spend {existing.get('spend')}), provider {provider_name} exists: reusing both")
    else:
        if existing:  # the raw key is shown once; without the provider it is lost, so recreate the pair
            ctx.admin.delete_key(existing.get("token") or existing.get("token_id"))
            log(f"  key {alias} existed without its credential provider: deleted, recreating")
        cap, period = bp.budget.get("perModelCap", 1.0), bp.budget.get("resetPeriod", "1d")
        key = ctx.admin.create_service_account_key(
            ctx.n.team,
            alias,
            [ctx.model],
            cap,
            period,
            {"domain": DOMAIN, "project": PROJECT_ID, "agent": ctx.n.harness, "env": ENV, "model": ctx.model},
        )
        token = key["token"]
        smoke = ctx.admin.chat_completion(key["key"], ctx.model, [{"role": "user", "content": "Say OK"}], max_tokens=5)
        log(
            f"  key {alias} created (cap {cap} USD / {period}); smoke completion -> {smoke['choices'][0]['message']['content']!r}"
        )
        provider_arn = identity.store_api_key(provider_name, key["key"], tags=DRYRUN_TAGS, region=ctx.region)
    log(f"  credential provider {provider_arn}")
    ctx.state.set("litellm", {"team": ctx.n.team, "keyAlias": alias, "token": token, "providerName": provider_name})
    return {"team": team, "keyAlias": alias, "token": token, "providerName": provider_name, "providerArn": provider_arn}


def ensure_roles(ctx: Context) -> dict:
    """Project roles from the templates (what POST /projects does); waits for IAM propagation when freshly created."""
    iam = boto3.client("iam", region_name=ctx.region)
    try:
        iam.get_role(RoleName=ctx.n.agent_role)
        fresh = False
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            raise
        fresh = True
    arns = roles.ensure_project_roles(
        ctx.cfg,
        PROJECT_ID,
        ENV,
        allowed_tool_arns=[ctx.cfg.hr_tools_lambda_arn],
        kb_ids=[ctx.cfg.kb_id],
        iam=iam,
        settle_seconds=10 if fresh else 0,
    )
    for role_name in (ctx.n.agent_role, ctx.n.gateway_role):
        iam.tag_role(RoleName=role_name, Tags=[{"Key": "ManagedBy", "Value": "dryrun"}])
    log(
        f"  agent role {arns['agentRoleArn']}\n  gateway role {arns['gatewayRoleArn']} ({'created' if fresh else 'existing'})"
    )
    return arns


def ensure_gateway_and_policies(ctx: Context, gateway_role_arn: str) -> dict:
    """Module 4: policy engine, gateway with both targets, Cedar permits, the days > 10 forbid, ENFORCE."""
    region = ctx.region
    engine_before = agentcore.find_policy_engine(names.POLICY_ENGINE_NAME, region)
    engine = agentcore.ensure_policy_engine(tags=DRYRUN_TAGS, region=region)
    if engine_before is None:
        ctx.state.set("policyEngine.createdByDryrun", True)
    log(
        f"  policy engine {engine['policyEngineId']} {engine['status']} ({'created' if engine_before is None else 'existing'})"
    )

    existing = agentcore.find_gateway(ctx.n.gateway, region)
    mode = ((existing or {}).get("policyEngineConfiguration") or {}).get("mode") or "LOG_ONLY"
    gateway = agentcore.ensure_gateway(
        ctx.n.gateway,
        gateway_role_arn,
        engine["policyEngineArn"],
        mode,
        description=f"{ctx.project_spec['name']} tools and knowledge ({ENV})",
        tags=DRYRUN_TAGS,
        region=region,
    )
    log(
        f"  gateway {gateway['gatewayId']} {gateway['status']} mode {gateway['policyEngineConfiguration']['mode']} {gateway['gatewayUrl']}"
    )

    hr, kb = ctx.project_spec["tools"]["hr-core"], ctx.project_spec["knowledge"]["hr-policies"]
    schema_path = (BLUEPRINT_DIR / hr["schema"]).resolve()
    schema = json.loads(schema_path.read_text())
    hrtools = agentcore.ensure_lambda_target(
        gateway["gatewayId"], hr["gatewayTarget"], ctx.resolve(hr["lambdaArn"]), schema_path, hr["description"], region
    )
    log(f"  target {hr['gatewayTarget']} {hrtools['targetId']} {hrtools['status']} ({len(schema)} tools)")
    kb_filter = kb["filter"]["equals"]
    assert kb_filter["key"] == "department", kb_filter
    hrkb = agentcore.ensure_kb_target(
        gateway["gatewayId"],
        kb["gatewayTarget"],
        ctx.resolve(kb["knowledgeBaseId"]),
        kb_filter["value"],
        description=kb["description"],
        region=region,
    )
    log(
        f"  target {kb['gatewayTarget']} {hrkb['targetId']} {hrkb['status']} (filter department == {kb_filter['value']})"
    )

    principal = ctx.n.cedar_principal(ctx.cfg.account_id)
    resource = f'AgentCore::Gateway::"{gateway["gatewayArn"]}"'
    tool_names = [f"{hr['gatewayTarget']}___{t['name']}" for t in schema] + [f"{kb['gatewayTarget']}___Retrieve"]
    policies: dict[str, dict] = {}
    for tool in tool_names:
        rule = (
            "allow_"
            + (tool.split("___", 1)[1] if tool.startswith(hr["gatewayTarget"]) else tool.replace("___", "_")).lower()
        )
        cedar = f'permit(principal == {principal}, action == AgentCore::Action::"{tool}", resource == {resource});'
        policy = agentcore.ensure_policy(
            engine["policyEngineId"],
            ctx.n.policy(rule),
            cedar,
            description=f"{PROJECT_ID} {ENV}: allow {tool}",
            region=region,
        )
        policies[ctx.n.policy(rule)] = policy
        log(f"  policy {ctx.n.policy(rule)} {policy['status']} ({policy.get('enforcementMode')})")
    forbid_tool = f"{hr['gatewayTarget']}___{FORBID_TOOL}"
    forbid = (
        f'forbid(principal is AgentCore::IamEntity, action == AgentCore::Action::"{forbid_tool}", resource == {resource}) '
        f"when {{ context.input.days > {FORBID_DAYS} }};"
    )
    policy = agentcore.ensure_policy(
        engine["policyEngineId"],
        ctx.n.policy(FORBID_RULE),
        forbid,
        description=f"{PROJECT_ID} {ENV}: leave requests longer than {FORBID_DAYS} days are not submitted by the assistant",
        region=region,
    )
    policies[ctx.n.policy(FORBID_RULE)] = policy
    log(f"  policy {ctx.n.policy(FORBID_RULE)} {policy['status']} ({policy.get('enforcementMode')})")

    if gateway["policyEngineConfiguration"].get("mode") != "ENFORCE":
        gateway = agentcore.set_gateway_policy_mode(gateway["gatewayId"], engine["policyEngineArn"], "ENFORCE", region)
    log(f"  gateway mode {gateway['policyEngineConfiguration']['mode']}")
    ctx.state.set(
        "gateway", {"id": gateway["gatewayId"], "arn": gateway["gatewayArn"], "engineId": engine["policyEngineId"]}
    )
    return {
        "engine": engine,
        "gateway": gateway,
        "hrtools": hrtools,
        "hrkb": hrkb,
        "policies": policies,
        "toolNames": tool_names,
    }


def build_project(ctx: Context, role_arns: dict) -> dict:
    """The project item as GET /projects/{id} would return it (contract section 3), from project.yaml."""
    spec, caps = ctx.project_spec, ctx.project_spec["caps"]
    return {
        "projectId": PROJECT_ID,
        "domainId": DOMAIN,
        "name": spec["name"],
        "description": spec["description"],
        "status": "ACTIVE",
        "litellm": {
            "teamId": ctx.n.team,
            "teamAlias": ctx.n.team,
            "maxBudget": caps["maxBudget"],
            "budgetDuration": caps["budgetDuration"],
            "tpmLimit": caps["tpmLimit"],
            "rpmLimit": caps["rpmLimit"],
            "models": list(spec["models"]),
        },
        "roles": {ENV: role_arns},
        "allowedToolArns": [ctx.resolve(t["lambdaArn"]) for t in spec["tools"].values()],
        "knowledgeBaseIds": [ctx.resolve(k["knowledgeBaseId"]) for k in spec["knowledge"].values()],
    }


def build_resources(ctx: Context, infra: dict, memory: dict, key: dict) -> list[dict]:
    """The registered resources as GET /projects/{id}/resources?env=dev would return them (PUT bodies of modules 4-6)."""
    gateway, hr, kb = (
        infra["gateway"],
        ctx.project_spec["tools"]["hr-core"],
        ctx.project_spec["knowledge"]["hr-policies"],
    )

    def resource(type_: str, name: str, arn: str | None = None, **extra) -> dict:
        return {"projectId": PROJECT_ID, "env": ENV, "type": type_, "name": name, "arn": arn, **extra}

    rows = [
        resource("gateway", ctx.n.gateway, gateway["gatewayArn"], id=gateway["gatewayId"], url=gateway["gatewayUrl"]),
        # gateway targets have no ARN of their own (GetGatewayTarget: gatewayArn + targetId): id = targetId, gatewayArn in attributes
        resource(
            "gateway_target",
            hr["gatewayTarget"],
            id=infra["hrtools"]["targetId"],
            attributes={
                "kind": "lambda",
                "gatewayArn": gateway["gatewayArn"],
                "lambdaArn": ctx.resolve(hr["lambdaArn"]),
                "tools": infra["toolNames"][:-1],
            },
        ),
        resource(
            "gateway_target",
            kb["gatewayTarget"],
            id=infra["hrkb"]["targetId"],
            attributes={
                "kind": "knowledge_base",
                "gatewayArn": gateway["gatewayArn"],
                "knowledgeBaseId": ctx.resolve(kb["knowledgeBaseId"]),
                "filter": kb["filter"],
            },
        ),
        resource("memory", ctx.n.memory, memory["arn"], id=memory["id"]),
        resource("credential_provider", key["providerName"], key["providerArn"], attributes={"model": ctx.model}),
    ]
    for name, policy in infra["policies"].items():
        rows.append(resource("policy", name, policy.get("policyArn"), id=policy["policyId"]))
    return rows


def deploy_harness(
    ctx: Context, bp: blueprint.Blueprint, project: dict, resources: list[dict], provider_arn: str
) -> dict:
    """Module 6 / the deploy job: render the blueprint and create or update the Dev harness."""
    spec = blueprint.render(bp, ENV, project, resources, ctx.cfg, provider_arn)
    log(
        f"  rendered: model {spec['model']['liteLlmModelConfig']['modelId']} via {spec['model']['liteLlmModelConfig']['apiBase']}, "
        f"tools {spec.get('allowedTools')}, memory {'agentCoreMemoryConfiguration' in spec['memory']}, "
        f"limits {spec.get('maxIterations')}/{spec.get('maxTokens')}/{spec.get('timeoutSeconds')}"
    )
    harness = agentcore.create_or_update_harness(
        ctx.n.harness, spec, tags=blueprint.tags_for(bp, ENV, managed_by="dryrun"), region=ctx.region
    )
    runtime_id = harness["environment"]["agentCoreRuntimeEnvironment"]["agentRuntimeId"]
    log(
        f"  harness {harness['harnessId']} version {harness['harnessVersion']} {harness['status']} runtime {runtime_id}"
    )
    ctx.state.set("harness", {"id": harness["harnessId"], "arn": harness["arn"], "runtimeId": runtime_id})
    return harness


def evaluate(
    ctx: Context, harness: dict, bp: blueprint.Blueprint, label: str
) -> tuple[evaluation.RunSummary, evaluation.Verdict]:
    """Module 6: the dataset run against the Dev harness and the threshold verdict; saves the RunSummary JSON."""
    log_group = agentcore.harness_log_group(harness, ctx.n.harness_endpoint)
    log(
        f"  dataset {DATASET_FILE.name}, log group {log_group}, evaluators {bp.evaluator_ids()}, delay {ctx.args.delay} s"
    )

    def on_turn(session_id: str, prompt: str, result: agentcore.InvokeResult) -> None:
        log(f"    [{session_id[:24]}] {result.seconds:5.1f} s tools {result.tools} -> {result.text.strip()[:90]!r}")

    summary = evaluation.run_dataset(
        DATASET_FILE,
        harness["arn"],
        log_group,
        bp.evaluator_ids(),
        delay_seconds=ctx.args.delay,
        region=ctx.region,
        on_turn=on_turn,
    )
    verdict = summary.verdict(bp.thresholds)  # failed scenarios (timeouts, cap hits) block the release
    print_scores(summary, verdict)
    path = summary.save(ctx.args.results_dir / f"{PROJECT_ID}-{label}-{ctx.model}.json")
    log(f"  saved {path}")
    return summary, verdict


def spending_cap_moment(ctx: Context, harness_arn: str, key: dict) -> dict:
    """Module 6's deterministic cap moment: the admin lowers the Dev key cap below the spend already recorded, the next call is
    refused with budget_exceeded, the admin raises the cap and the next call succeeds. Records how long each change takes."""
    info = ctx.admin.key_info(key["token"])
    spend, original_cap = float(info.get("spend") or 0), info.get("max_budget") or 1.0
    low_cap = round(max(spend / 2, 0.0001), 6)
    prompt = "What is my leave balance? My employee id is E1001."
    out: dict[str, Any] = {"spendBefore": spend, "originalCap": original_cap, "lowCap": low_cap}

    ctx.admin.update_key(key["token"], max_budget=low_cap)
    started = time.monotonic()
    refused = None
    for attempt in range(6):
        row = converse(ctx, harness_arn, f"cap probe {attempt + 1} (cap {low_cap} USD)", prompt)
        if row.get("budgetExceeded"):
            refused = row
            break
        time.sleep(5)
    out["secondsUntilRefused"] = round(time.monotonic() - started, 1)
    assert refused, f"no budget_exceeded after lowering the cap to {low_cap} USD (spend {spend} USD)"
    out["refusal"] = agentcore.parse_budget_message(refused["error"])
    log(f"  cap {low_cap} USD < spend {spend} USD: refused after {out['secondsUntilRefused']} s: {out['refusal']}")

    ctx.admin.update_key(key["token"], max_budget=original_cap)
    started = time.monotonic()
    restored = None
    for attempt in range(6):
        row = converse(ctx, harness_arn, f"cap restored probe {attempt + 1} (cap {original_cap} USD)", prompt)
        if not row.get("error"):
            restored = row
            break
        time.sleep(5)
    out["secondsUntilRestored"] = round(time.monotonic() - started, 1)
    assert restored, f"invocations still fail after raising the cap back to {original_cap} USD"
    log(f"  cap raised to {original_cap} USD: next call succeeded after {out['secondsUntilRestored']} s")
    return out


def policy_scenario_outcomes(summary: evaluation.RunSummary) -> dict:
    """Correctness and trajectory values of the three policy scenarios (the seeded regression)."""
    rows = {r["scenarioId"]: r for r in summary.results}
    return {
        sid: {k: rows.get(sid, {}).get("scores", {}).get(k) for k in ("Correctness", "TrajectoryInOrderMatch")}
        for sid in POLICY_SCENARIOS
    }


def run(ctx: Context) -> bool:
    """Steps 1-6. Returns True when the v1-blocked / v2-passed expectation holds (or evaluation was skipped)."""
    timer, record = ctx.timer, ctx.record
    bp1 = load_blueprint(ctx.model, 1)
    with timer.step("1. LiteLLM team + key + credential provider (module 5)"):
        key = ensure_team_and_key(ctx, bp1)
    with timer.step("2. project roles from templates (module 3)"):
        role_arns = ensure_roles(ctx)
    with timer.step("3. policy engine, gateway, targets, Cedar policies, ENFORCE (module 4)"):
        infra = ensure_gateway_and_policies(ctx, role_arns["gatewayRoleArn"])
    with timer.step("4. memory (module 6)"):
        memory = agentcore.ensure_memory(
            ctx.n.memory,
            bp1.memory_strategies(),
            description=f"{ctx.project_spec['name']} memory ({ENV})",
            tags=DRYRUN_TAGS,
            region=ctx.region,
        )
        log(
            f"  memory {memory['id']} {memory['status']} strategies {[s.get('type') for s in memory.get('strategies', [])]}"
        )
        ctx.state.set("memory", {"id": memory["id"], "arn": memory["arn"]})
    project, resources = build_project(ctx, role_arns), build_resources(ctx, infra, memory, key)
    record["project"], record["resources"] = project, resources

    with timer.step("5a. render blueprint v1 + create Dev harness (module 6)"):
        harness = deploy_harness(ctx, bp1, project, resources, key["providerArn"])
    with timer.step("5b. warm-up invocation (cold start)"):
        record["warmUp"] = converse(ctx, harness["arn"], "warm-up", WARM_UP_PROMPT)
    with timer.step("5c. sample prompts"):
        record["samples"] = [converse(ctx, harness["arn"], label, prompt) for label, prompt in SAMPLE_PROMPTS]
    with timer.step("5d. spend recorded on the LiteLLM key"):
        time.sleep(15)  # proxy_batch_write_at 10 s
        info = ctx.admin.key_info(key["token"])
        rows = ctx.admin.spend_logs(api_key=key["token"])
        record["spend"] = {
            "keyAlias": key["keyAlias"],
            "spend": info.get("spend"),
            "maxBudget": info.get("max_budget"),
            "budgetResetAt": str(info.get("budget_reset_at")),
            "spendLogRows": len(rows),
        }
        log(
            f"  key {key['keyAlias']}: spend {info.get('spend')} USD of {info.get('max_budget')} (reset {info.get('budget_reset_at')}), {len(rows)} spend log rows"
        )
        assert (info.get("spend") or 0) > 0, "no spend recorded on the virtual key after the sample prompts"
    with timer.step(
        "5e. spending-cap moment: lower the key cap below its spend, hit budget_exceeded, raise it (module 6)"
    ):
        record["capMoment"] = spending_cap_moment(ctx, harness["arn"], key)
    if ctx.args.skip_eval:
        return True

    with timer.step("6a. dataset run on prompt v1 (expect BLOCKED)"):
        summary1, verdict1 = evaluate(ctx, harness, bp1, "v1")
    record["v1"] = {
        "scores": summary1.scores,
        "verdict": verdict1.status,
        "failures": verdict1.failures,
        "seconds": summary1.seconds,
        "policyScenarios": policy_scenario_outcomes(summary1),
    }
    bp2 = load_blueprint(ctx.model, 2)
    with timer.step("6b. update harness to prompt v2"):
        harness2 = deploy_harness(ctx, bp2, project, resources, key["providerArn"])
        assert str(harness2["harnessVersion"]) != str(harness["harnessVersion"]), (
            "UpdateHarness did not create a new version"
        )
    with timer.step("6c. warm-up v2 (cold start) with a policy question"):
        record["warmUpV2"] = converse(ctx, harness2["arn"], "warm-up v2", SAMPLE_PROMPTS[1][1])
    with timer.step("6d. dataset run on prompt v2 (expect PENDING_APPROVAL)"):
        summary2, verdict2 = evaluate(ctx, harness2, bp2, "v2")
    record["v2"] = {
        "scores": summary2.scores,
        "verdict": verdict2.status,
        "failures": verdict2.failures,
        "seconds": summary2.seconds,
        "policyScenarios": policy_scenario_outcomes(summary2),
    }

    failed1 = {f["evaluator"] for f in verdict1.failures}
    expectations = {
        "v1Blocked": not verdict1.passed,
        "v1FailsCorrectnessAndTrajectory": {"Correctness", "TrajectoryInOrderMatch"} <= failed1,
        "v1PolicyScenariosFail": all(
            (v["Correctness"] is not None and v["Correctness"] < 0.5) and (v["TrajectoryInOrderMatch"] in (None, 0.0))
            for v in record["v1"]["policyScenarios"].values()
        ),
        "v2Passed": verdict2.passed,
        "v2PolicyScenariosPass": all(
            v["Correctness"] == 1.0 and v["TrajectoryInOrderMatch"] == 1.0
            for v in record["v2"]["policyScenarios"].values()
        ),
    }
    record["expectations"] = expectations
    for name, met in expectations.items():
        log(f"  expectation {name}: {'MET' if met else 'NOT MET'}")
    # The exit criterion is v1 BLOCKED / v2 passed; the per-scenario checks are reported so the content can be tightened.
    return expectations["v1Blocked"] and expectations["v2Passed"]


# ---- cleanup -------------------------------------------------------------------------------------------------------
def cleanup(ctx: Context) -> list[str]:
    """`workshop cleanup` for this project and environment: delete everything the dry run created; returns the leftovers found."""
    region, n = ctx.region, ctx.n
    control = agentcore.control_client(region)
    logs_client = boto3.client("logs", region_name=region)
    problems: list[str] = []

    def attempt(label: str, fn):
        try:
            outcome = fn()
            log(f"  {label}: {outcome}")
        except Exception as exc:  # noqa: BLE001 - keep deleting the rest
            problems.append(f"{label}: {exc}")
            log(f"  {label}: FAILED {str(exc)[:300]}")

    with ctx.timer.step("7. cleanup"):
        # Deletions are asynchronous: the harness took several minutes to disappear in the dry run, so wait before the
        # dependent deletions (gateway before the engine, memory before the roles) and before any re-run.
        attempt("harness", lambda: agentcore.delete_harness(n.harness, region))
        attempt("gateway + targets", lambda: agentcore.delete_gateway(n.gateway, region))
        attempt("memory", lambda: agentcore.delete_memory(n.memory, region))
        attempt(
            "harness gone",
            lambda: wait_gone(lambda: agentcore.find_harness(n.harness, region), "harness", 900) or "yes",
        )
        attempt(
            "gateway gone",
            lambda: wait_gone(lambda: agentcore.find_gateway(n.gateway, region), "gateway", 600) or "yes",
        )
        attempt(
            "memory gone", lambda: wait_gone(lambda: agentcore.find_memory(n.memory, region), "memory", 600) or "yes"
        )

        def delete_policies():
            engine = agentcore.find_policy_engine(names.POLICY_ENGINE_NAME, region)
            if not engine:
                return "no policy engine"
            prefix = n.policy("")
            deleted, kept = [], []
            for policy in agentcore._paginate(
                control, "list_policies", "policies", policyEngineId=engine["policyEngineId"]
            ):
                if policy["name"].startswith(prefix):
                    control.delete_policy(policyEngineId=engine["policyEngineId"], policyId=policy["policyId"])
                    deleted.append(policy["name"])
                else:
                    kept.append(policy["name"])
            wait_gone(
                lambda: [
                    p
                    for p in agentcore._paginate(
                        control, "list_policies", "policies", policyEngineId=engine["policyEngineId"]
                    )
                    if p["name"].startswith(prefix)
                ],
                "policies",
            )
            if kept or not (ctx.state.get("policyEngine.createdByDryrun") or ctx.args.only_cleanup):
                return f"deleted {len(deleted)} policies, kept engine ({len(kept)} other policies)"
            agentcore.delete_policy_engine(names.POLICY_ENGINE_NAME, region)
            return f"deleted {len(deleted)} policies and the policy engine"

        attempt("policies + engine", delete_policies)
        attempt(
            "credential provider", lambda: identity.delete_provider(n.credential_provider(ctx.model), region=region)
        )
        attempt("project roles", lambda: roles.delete_project_roles(ctx.cfg, PROJECT_ID, ENV))

        def delete_litellm():
            out = []
            key = ctx.admin.find_key(n.key_alias(ctx.model))
            if key:
                ctx.admin.delete_key(key.get("token") or key.get("token_id"))
                out.append(f"key {n.key_alias(ctx.model)}")
            if ctx.admin.find_team(n.team):
                ctx.admin.delete_team(n.team)
                out.append(f"team {n.team}")
            return ", ".join(out) or "nothing to delete"

        attempt("LiteLLM key + team", delete_litellm)

        def delete_log_groups():
            # Late log delivery from the torn-down runtime recreated the group once in the dry run: sweep, wait, sweep again.
            prefix = f"/aws/bedrock-agentcore/runtimes/harness_{n.harness}-"
            deleted = []
            for attempt_no in range(2):
                found = [
                    group["logGroupName"]
                    for page in logs_client.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix)
                    for group in page["logGroups"]
                ]
                for name in found:
                    logs_client.delete_log_group(logGroupName=name)
                    deleted.append(name)
                if not found or attempt_no:
                    break
                time.sleep(60)
            return deleted or "none"

        attempt("harness log groups", delete_log_groups)

        leftovers = []
        for label, found in (
            ("harness", agentcore.find_harness(n.harness, region)),
            ("gateway", agentcore.find_gateway(n.gateway, region)),
            ("memory", agentcore.find_memory(n.memory, region)),
        ):
            if found:
                leftovers.append(f"{label} {found.get('status')}")
        if identity.get_provider_arn(n.credential_provider(ctx.model), region):
            leftovers.append(f"provider {n.credential_provider(ctx.model)}")
        iam = boto3.client("iam", region_name=region)
        for role_name in (n.agent_role, n.gateway_role):
            try:
                iam.get_role(RoleName=role_name)
                leftovers.append(f"role {role_name}")
            except ClientError:
                pass
        if ctx.admin.find_team(n.team):
            leftovers.append(f"LiteLLM team {n.team}")
        log(f"  leftovers: {leftovers or 'none'}")
        if problems:
            log(f"  cleanup problems: {problems}")
        ctx.state.set("cleanup", {"at": datetime.now(UTC).isoformat(), "leftovers": leftovers, "problems": problems})
    return leftovers + problems


# ---- main ----------------------------------------------------------------------------------------------------------
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--model", default="claude-haiku-4-5", help="approved model alias for the Dev harness (default: cheap)"
    )
    parser.add_argument("--keep", action="store_true", help="leave everything running (skip the cleanup step)")
    parser.add_argument(
        "--skip-eval", action="store_true", help="stop after the sample prompts (no dataset runs, no v2)"
    )
    parser.add_argument("--only-cleanup", action="store_true", help="delete what an earlier run left and exit")
    parser.add_argument(
        "--delay", type=int, default=180, help="seconds the dataset runner waits for telemetry (default 180)"
    )
    parser.add_argument(
        "--results-dir", type=Path, default=DEFAULT_RESULTS_DIR, help="where summaries and state are written"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.results_dir.mkdir(parents=True, exist_ok=True)
    cfg = config.load()
    ctx = Context(
        cfg=cfg,
        n=names.ProjectNames(PROJECT_ID, ENV),
        args=args,
        admin=litellm_admin(cfg),
        state=WorkshopState(args.results_dir / "e2e-state.json"),
        timer=Timer(),
        project_spec=yaml.safe_load(PROJECT_FILE.read_text())["spec"],
    )
    log(
        f"account {cfg.account_id} region {cfg.region} project {PROJECT_ID}/{ENV} model {args.model} "
        f"model gateway {cfg.model_gateway_url} via {ctx.admin.transport} KB {cfg.kb_id}"
    )
    ctx.record.update(
        {
            "startedAt": datetime.now(UTC).isoformat(),
            "account": cfg.account_id,
            "region": cfg.region,
            "model": args.model,
            "args": {k: str(v) for k, v in vars(args).items()},
        }
    )
    if args.only_cleanup:
        leftovers = cleanup(ctx)
        return 1 if leftovers else 0

    ok, error = False, None
    try:
        ok = run(ctx)
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - recorded, then cleanup
        error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
        log(f"RUN FAILED: {error}")
    finally:
        if args.keep:
            log("--keep: leaving everything in place (run again with --only-cleanup to delete)")
        else:
            try:
                cleanup(ctx)
            except Exception as exc:  # noqa: BLE001
                error = (error or "") + f" cleanup: {exc}"
                traceback.print_exc()
        ctx.record.update(
            {
                "finishedAt": datetime.now(UTC).isoformat(),
                "totalSeconds": round(time.monotonic() - STARTED, 1),
                "ok": ok and not error,
                "error": error,
                "steps": ctx.timer.steps,
            }
        )
        path = args.results_dir / f"e2e-dryrun-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
        path.write_text(json.dumps(ctx.record, indent=1, default=str))
        log(f"run record {path}")
        log("timings: " + ", ".join(f"{s['step'].split('.')[0]}={s['seconds']}s" for s in ctx.timer.steps))
        log(f"total {time.monotonic() - STARTED:.0f} s, result {'OK' if ok and not error else 'NOT OK'}")
    return 0 if ok and not error else 1


if __name__ == "__main__":
    sys.exit(main())
