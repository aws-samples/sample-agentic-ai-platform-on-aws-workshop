"""Module 6: AgentOps (memories, blueprint to Dev, release and evaluation).

`workshop apply 6` with prompt_version v1 creates the memories, stores blueprint v1, deploys it to Dev, creates release 1 and runs
the dataset evaluation (BLOCKED is the expected verdict). With prompt_version v2 it stores blueprint v2, redeploys, creates release 2
and evaluates it (PENDING_APPROVAL). Approval and promotion are the Domain approver's clicks in the UI (`workshop approve release`
and `workshop promote` are the fallbacks).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime

import yaml

from platform_sdk import agentcore, api, blueprint, evaluation

from .context import Context, WorkshopError, head, look, ok, say

EVALUATION_DELAY_SECONDS = 180
RUN_POLL_SECONDS = 10
RUN_WAIT_SECONDS = 15 * 60  # the worker's limit: a platform run still EVALUATING after this has stalled


def registered(client, project_id: str, env: str, type_: str, name: str | None = None) -> dict | None:
    rows = client.list_resources(project_id, env=env)
    return next((r for r in rows if r["type"] == type_ and (name is None or r["name"] == name)), None)


# ---- memory -----------------------------------------------------------------------------------------------------------
def ensure_memories(ctx: Context, strategies: list[str]) -> dict:
    developer = ctx.developer
    pending = {}
    for env in ("dev", "prod"):
        n = ctx.names(env)
        pending[env] = agentcore.ensure_memory(
            n.memory,
            strategies,
            description=f"HR Assistant conversation memory ({env})",
            tags={"ProjectId": ctx.project_id, "Environment": env},
            region=ctx.region,
            wait=False,
        )
        say(f"  {n.memory:<28} {pending[env]['status']:<8} {pending[env]['id']}")
    memories = {}
    started = time.monotonic()
    for env, created in pending.items():
        memory = agentcore.wait_memory(created["id"], ctx.region)
        memories[env] = {
            "id": memory["id"],
            "arn": memory["arn"],
            "strategies": {s["type"]: s["strategyId"] for s in memory.get("strategies", [])},
        }
        developer.register_resource(
            ctx.project_id,
            env,
            "memory",
            ctx.names(env).memory,
            memory["arn"],
            id=memory["id"],
            attributes={"strategies": strategies},
        )
    say(f"  both memories ACTIVE and registered after {time.monotonic() - started:.0f} s")
    ctx.state.set("memory", {**(ctx.state.get("memory") or {}), **memories})
    if not ctx.state.get("memory.sample"):
        data = agentcore.data_client(ctx.region)
        actor_id, session_id = "E1001", f"workshop-{uuid.uuid4()}"
        turns = [
            ("USER", "Hi, I am E1001. I prefer to take my annual leave in two-week blocks in August."),
            ("ASSISTANT", "Noted: you prefer two-week blocks in August. You currently have 12 days of annual leave."),
        ]
        for role, text in turns:
            data.create_event(
                memoryId=memories["dev"]["id"],
                actorId=actor_id,
                sessionId=session_id,
                eventTimestamp=datetime.now(UTC),
                payload=[{"conversational": {"content": {"text": text}, "role": role}}],
            )
        ctx.state.set("memory.sample", {"actorId": actor_id, "sessionId": session_id, "env": "dev"})
        say(
            f"  wrote {len(turns)} sample events for actor {actor_id} (long-term records appear minutes later; module 7 reads them)"
        )
    return memories


# ---- blueprint -----------------------------------------------------------------------------------------------------------
def load_blueprint(ctx: Context, prompt_version: str, thresholds: dict | None) -> blueprint.Blueprint:
    document = yaml.safe_load((ctx.blueprint_dir / "blueprint.yaml").read_text())
    document["metadata"]["version"] = 1 if prompt_version == "v1" else 2
    document["metadata"]["project"] = ctx.project_id
    document["metadata"]["domain"] = ctx.domain_id
    document["spec"]["systemPrompt"] = {"file": f"prompts/hr-assistant-{prompt_version}.md"}
    document["spec"]["model"] = {"ref": f"model/{ctx.model_alias}"}
    if thresholds:
        document["spec"].setdefault("evaluation", {})["thresholds"] = {k: float(v) for k, v in thresholds.items()}
    return blueprint.load(document, base_dir=ctx.blueprint_dir)


def store_blueprint(ctx: Context, bp: blueprint.Blueprint, prompt_version: str) -> int:
    stored = ctx.developer.create_blueprint(
        ctx.project_id,
        bp.api_spec(),
        bp.system_prompt,
        notes=f"workshop: prompt {prompt_version} (scenario/blueprint/prompts/hr-assistant-{prompt_version}.md)",
    )
    version = int(stored["version"])
    how = "already stored (same spec and prompt)" if stored.get("existing") else "created"
    say(
        f"  POST /projects/{ctx.project_id}/blueprints -> version {version} ({how}); model {stored.get('model')}, thresholds {stored.get('thresholds')}"
    )
    ctx.state.set(
        f"blueprint.{prompt_version}",
        {"version": version, "model": stored.get("model"), "prompt": f"prompts/hr-assistant-{prompt_version}.md"},
    )
    return version


def deploy_to_dev(ctx: Context, version: int) -> dict | None:
    developer = ctx.developer
    hr_dev = ctx.names("dev")
    current = registered(developer, ctx.project_id, "dev", "harness", hr_dev.harness)
    attrs = (current or {}).get("attributes") or {}
    if current and str(attrs.get("blueprintVersion")) == str(version):
        say(
            f"  Dev harness {hr_dev.harness} already runs blueprint version {version} (harness version {attrs.get('version')})"
        )
        return None
    accepted = developer.deploy(ctx.project_id, version, "dev")
    started = time.monotonic()
    job = developer.wait_for_job(accepted["jobId"], timeout=600)
    if job.get("status") != "SUCCEEDED":
        message = f"deploy job {accepted['jobId']} {job.get('status')}: {json.dumps(job.get('error'), default=str)}"
        if "service linked role" in message.lower():
            message += (
                "\n  The account lacks the AgentCore Runtime service-linked role and the backend may not create it: this foundation "
                "version predates the fix (Platform stack, 15 Oct 2026). Create it once with admin credentials, then run apply 6 again:\n"
                "  uv run python -c \"import boto3; boto3.client('iam').create_service_linked_role("
                "AWSServiceName='runtime-identity.bedrock-agentcore.amazonaws.com')\""
            )
        raise WorkshopError(message)
    out = job["output"]
    say(
        f"  deploy job SUCCEEDED in {time.monotonic() - started:.0f} s: harness {out.get('harnessName')} version {out.get('harnessVersion')}, endpoint {out.get('endpoint')}; key {out.get('keyAlias')} ({'created by the job' if out.get('keyCreated') else 'the module 5 key'})"
    )
    ctx.state.set(
        "harness.dev",
        {
            **(ctx.state.get("harness.dev") or {}),
            "id": out.get("harnessId"),
            "arn": out.get("harnessArn"),
            "version": str(out.get("harnessVersion")),
            "blueprintVersion": int(version),
            "logGroup": out.get("logGroup"),
            "endpoint": out.get("endpoint") or "DEFAULT",
        },
    )
    return out


def converse(
    ctx: Context, env: str, prompt: str, label: str, session_id: str | None = None, timeout: float = 420
) -> dict:
    started = time.monotonic()
    job = ctx.developer.ask(
        ctx.project_id, env, prompt, session_id or agentcore.new_session_id("workshop"), timeout=timeout
    )
    wall = round(time.monotonic() - started, 1)
    out = job.get("output") or {}
    if job.get("status") != "SUCCEEDED":
        error = job.get("error") or {}
        say(
            f"  [{label}] job {job.get('status')} after {wall} s: {error.get('code')}: {str(error.get('message'))[:200]}"
        )
        return {"status": job.get("status"), "error": error, "sessionId": job.get("sessionId")}
    usage = out.get("usage") or {}
    say(
        f"  [{label}] {wall} s, tools {out.get('toolsUsed')}, {usage.get('inputTokens')}/{usage.get('outputTokens')} tokens"
    )
    say("     > " + " ".join(str(out.get("text", "")).split())[:400])
    return {"status": "SUCCEEDED", **out, "sessionId": job.get("sessionId") or out.get("sessionId")}


# ---- releases ------------------------------------------------------------------------------------------------------------
def find_release(ctx: Context, version: int, include_rejected: bool = False) -> dict | None:
    matching = [
        r
        for r in ctx.developer.list_releases(ctx.project_id)
        if int(r.get("blueprintVersion", 0)) == int(version) and (include_rejected or r.get("status") != "REJECTED")
    ]
    matching.sort(key=lambda r: r.get("createdAt") or "")
    return matching[-1] if matching else None


def ensure_release(ctx: Context, version: int, notes: str) -> dict:
    existing = find_release(ctx, version)
    if existing:
        say(f"  release {existing['releaseId']} for blueprint version {version} exists: {existing['status']}")
        return existing
    # autoRun off: this command runs the dataset itself (progress on the terminal, the run saved locally) and posts the summary;
    # a release created on the Releases screen is deployed and evaluated by the platform's worker instead.
    created = ctx.developer.create_release(ctx.project_id, version, notes, auto_run=False)
    say(
        f"  POST /projects/{ctx.project_id}/releases {{blueprintVersion: {version}, autoRun: false}} -> {created['status']} release {created['releaseId']}"
    )
    return created


def wait_for_platform_run(ctx: Context, release: dict) -> dict:
    """A release created on the Releases screen (or run again there) is deployed and evaluated by the platform's worker, not by
    this command: follow it to its verdict instead of starting a second run."""
    developer = ctx.developer
    started, last_stage = time.monotonic(), None
    while release.get("status") == "EVALUATING":
        pipeline = release.get("pipeline") or {}
        if pipeline.get("stage") != last_stage:
            last_stage = pipeline.get("stage")
            say(
                f"  {time.monotonic() - started:5.0f} s  the platform is running release {release['releaseId']} (job "
                f"{pipeline.get('jobId')}): {str(last_stage).lower()}"
            )
        if time.monotonic() - started > RUN_WAIT_SECONDS:
            raise WorkshopError(
                f"release {release['releaseId']} is still EVALUATING after {RUN_WAIT_SECONDS // 60} minutes: open the Releases "
                "screen; `Run again` appears there once the job has stalled, then run this command again"
            )
        time.sleep(RUN_POLL_SECONDS)
        release = developer.get_release(release["releaseId"])
    error = release.get("error") or {}
    if release.get("status") == "DRAFT" and error:
        say(f"  the platform's run failed in stage {error.get('stage')}: {error.get('code')}: {error.get('message')}")
    else:
        print_verdict(release)
    return release


def print_verdict(outcome: dict) -> None:
    ev = outcome.get("evaluation") or {}
    for name, detail in (ev.get("details") or {}).items():
        say(
            f"  {name:<24} actual {str(detail.get('actual')):>6}  {detail.get('op')}{detail.get('threshold')}  {'PASS' if detail.get('passed') else 'FAIL'}"
        )
    say(f"  -> release {outcome.get('status')} (verdict {ev.get('verdict')})")


def run_evaluation(ctx: Context, release: dict, label: str, evaluators: list[str]) -> dict:
    developer = ctx.developer
    hr_dev = ctx.names("dev")
    harness_row = registered(developer, ctx.project_id, "dev", "harness", hr_dev.harness)
    attrs = harness_row["attributes"]
    if str(attrs.get("blueprintVersion")) != str(release["blueprintVersion"]):
        raise WorkshopError(
            f"the Dev harness runs blueprint version {attrs.get('blueprintVersion')}, the release needs {release['blueprintVersion']}"
        )
    key_now = next(
        k for k in developer.list_keys(ctx.project_id) if k["env"] == "dev" and k["model"] == ctx.model_alias
    )
    key_spend = next(
        (k for k in developer.spend(ctx.project_id, "dev").get("keys", []) if k.get("alias") == key_now["alias"]), {}
    )
    if key_now.get("status") != "active" or float(key_now.get("maxBudget") or 0) <= float(key_spend.get("spend") or 0):
        raise WorkshopError(
            f"the Dev key {key_now['alias']} would refuse every scenario (cap {key_now.get('maxBudget')} USD, spend {key_spend.get('spend')} USD): run `workshop cap --dev restore`"
        )
    scenarios = json.loads(ctx.dataset_path.read_text())["scenarios"]
    say(
        f"  dataset {ctx.dataset_path.name}: {len(scenarios)} scenarios, 3 at a time; evaluators {evaluators}; telemetry delay {EVALUATION_DELAY_SECONDS} s -> 5 to 6 minutes"
    )
    started = time.monotonic()

    def on_turn(session_id, prompt, result):
        say(
            f"    {time.monotonic() - started:5.0f} s  {result.seconds:5.1f} s  {str(result.tools):<44} {' '.join(result.text.split())[:60]!r}"
        )

    summary = evaluation.run_dataset(
        ctx.dataset_path,
        harness_row["arn"],
        attrs["logGroup"],
        evaluators,
        delay_seconds=EVALUATION_DELAY_SECONDS,
        region=ctx.region,
        on_turn=on_turn,
    )
    runs_dir = ctx.state.path.parent / "evaluations"
    saved = summary.save(runs_dir / f"{ctx.project_id}-{label}-{summary.run_id}.json")
    say(f"  run {summary.run_id}: {summary.scenario_count} scenarios in {summary.seconds} s; saved {saved.name}")
    try:
        outcome = developer.submit_evaluation(release["releaseId"], summary)
    except api.PlatformApiError as exc:
        if exc.status != 409 or "EVALUATING" not in str(exc.message):
            raise
        # someone clicked Run evaluation on the Releases screen meanwhile: the platform's run owns the release now
        say(f"  the platform started its own run of this release meanwhile ({exc.message}); following that run instead")
        return wait_for_platform_run(ctx, developer.get_release(release["releaseId"]))
    print_verdict(outcome)
    return outcome


def release_state(release: dict) -> dict:
    ev = release.get("evaluation") or {}
    return {
        "releaseId": release["releaseId"],
        "blueprintVersion": release["blueprintVersion"],
        "status": release["status"],
        "verdict": ev.get("verdict"),
        "scores": {k: (v or {}).get("mean") for k, v in (ev.get("scores") or {}).items()},
        "approval": release.get("approval"),
        "promotion": release.get("promotion"),
    }


def run(ctx: Context, skip_evaluation: bool = False) -> None:
    m6 = ctx.inputs(6)
    prompt_version = str(m6["prompt_version"])
    expect_pass = prompt_version == "v2"
    developer = ctx.developer

    head("memory: one per project and environment")
    bp = load_blueprint(ctx, prompt_version, m6.get("thresholds"))
    strategies = bp.memory_strategies()
    ensure_memories(ctx, strategies)

    head(f"blueprint: prompt {prompt_version}, model {ctx.model_alias}, thresholds {bp.thresholds}")
    section = (
        bp.system_prompt.split("## Company policy questions")[1].split("##")[0].strip()
        if "## Company policy questions" in bp.system_prompt
        else ""
    )
    if section:
        say("  the 'Company policy questions' section of this prompt:\n    " + section.replace("\n", "\n    ")[:600])
    version = store_blueprint(ctx, bp, prompt_version)
    rendered = developer.render_blueprint(ctx.project_id, version, "dev")
    model_cfg = rendered["model"]["liteLlmModelConfig"]
    say(
        f"  render for dev: model {model_cfg['modelId']} via the model gateway, key {model_cfg['apiKeyArn'].rsplit('/', 1)[-1]}, tools {[t['type'] for t in rendered['tools']]}, memory {bool(rendered.get('memory'))}"
    )

    head("deploy to Dev")
    deploy_to_dev(ctx, version)
    harness_row = registered(developer, ctx.project_id, "dev", "harness", ctx.names("dev").harness)
    ctx.state.set(
        "harness.dev",
        {
            **(ctx.state.get("harness.dev") or {}),
            "id": harness_row["id"],
            "arn": harness_row["arn"],
            "blueprintVersion": harness_row["attributes"].get("blueprintVersion"),
            "logGroup": harness_row["attributes"].get("logGroup"),
            "endpoint": "DEFAULT",
        },
    )
    converse(
        ctx,
        "dev",
        "How many days per week can I work remotely, and which day do I have to be in the office?",
        f"warm-up of {prompt_version}: the policy question",
    )

    # the on-behalf-of twin of the Dev harness (the gateway of module 4, the IdP of module 2)
    from . import obo_build

    obo_build.module_6(ctx)

    head(f"release for blueprint version {version} and its evaluation")
    release = ensure_release(ctx, version, f"workshop: prompt {prompt_version}")
    release = developer.get_release(release["releaseId"])
    status = release["status"]
    if status == "EVALUATING" and not skip_evaluation:
        say(
            f"  release {release['releaseId']} is EVALUATING: it was created on the Releases screen, so the platform runs it"
        )
        release = wait_for_platform_run(ctx, release)
        status = release["status"]
    needs_run = status == "DRAFT" or (status == "BLOCKED" and expect_pass)
    if skip_evaluation:
        say(f"  --skip-evaluation: release {release['releaseId']} left {status}")
    elif needs_run:
        run_evaluation(ctx, release, prompt_version, bp.evaluator_ids())
        release = developer.get_release(release["releaseId"])
    else:
        ev = release.get("evaluation") or {}
        say(
            f"  release {release['releaseId']} is {status}: evaluation {ev.get('runId')} -> {ev.get('verdict')} (no new run)"
        )
        print_verdict({"status": status, "evaluation": ev})
    ctx.state.set(f"releases.{prompt_version}", release_state(release))

    say("")
    if release["status"] == "BLOCKED":
        ok(
            f"release {release['releaseId']} is BLOCKED: the gate caught prompt {prompt_version}. Set prompt_version: v2 in the record and run `workshop apply 6` again"
        )
    elif release["status"] == "PENDING_APPROVAL":
        ok(
            f"release {release['releaseId']} passed the gate: PENDING_APPROVAL. Approve and promote it as the Domain approver on the Releases screen"
        )
        say("      fallbacks: `workshop approve release` then `workshop promote`")
    else:
        ok(f"release {release['releaseId']} is {release['status']}")
    look("Releases screen", f"{ctx.ui_url}/releases/{release['releaseId']}")
    look("Playground", f"{ctx.ui_url}/playground  (environment dev: ask a policy question)")
