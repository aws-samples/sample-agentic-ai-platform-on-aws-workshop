"""Module 6: memory, blueprint and a CodePipeline release.

The command prepares the blueprint and starts the same pipeline as the Releases screen.
CodePipeline deploys Dev and evaluates the dataset. The v1 prompt should be BLOCKED;
v2 should reach PENDING_APPROVAL. The approver approves and promotes through the UI
or CLI, which releases the pipeline's manual gates. --skip-evaluation creates a draft.
"""

from __future__ import annotations

import time
import uuid
from datetime import UTC, datetime

import yaml

from platform_sdk import agentcore, blueprint, evaluation

from .context import Context, WorkshopError, head, look, ok, say, warn

RUN_POLL_SECONDS = 10
RUN_WAIT_SECONDS = 60 * 60  # includes time queued behind another release; never stops the execution


def registered(client, project_id: str, env: str, type_: str, name: str | None = None) -> dict | None:
    rows = client.list_resources(project_id, env=env)
    return next((r for r in rows if r["type"] == type_ and (name is None or r["name"] == name)), None)


# ---- the gateway's policy mode ------------------------------------------------------------------------------------------
def dev_gateway_mode(ctx: Context) -> tuple[str, str | None]:
    """(name, policy mode) of the project's Dev gateway: ENFORCE | LOG_ONLY, or None when no policy engine is attached or the
    gateway is not registered (module 4 not applied yet)."""
    name = ctx.names("dev").gateway
    row = registered(ctx.developer, ctx.project_id, "dev", "gateway", name)
    if not row or not row.get("id"):
        return name, None
    gateway = agentcore.control_client(ctx.region).get_gateway(gatewayIdentifier=row["id"])
    return name, (gateway.get("policyEngineConfiguration") or {}).get("mode")


def gateway_mode_problem(mode: str | None, gateway_name: str, max_leave_days: int) -> str | None:
    """Why a dataset run would lose its long-leave scenario on this gateway; None when the gateway enforces its policies."""
    if mode == "ENFORCE":
        return None
    state = f"is {mode}" if mode else "has no policy engine attached"
    return (
        f"the Dev gateway {gateway_name} {state}: the dataset expects the module 4 deny of leave requests over "
        f"{max_leave_days} days, which only ENFORCE applies (the 12-day scenario scores 0 otherwise). Flip {gateway_name} to "
        "ENFORCE on the Governance screen or run `workshop mode ENFORCE --gateway hr-dev`, then run `workshop apply 6` again"
    )


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
    # Create a draft first so the gateway-mode guard runs before CodePipeline starts.
    created = ctx.developer.create_release(ctx.project_id, version, notes, auto_run=False)
    say(
        f"  POST /projects/{ctx.project_id}/releases {{blueprintVersion: {version}, autoRun: false}} -> {created['status']} release {created['releaseId']}"
    )
    return created


def wait_for_platform_run(ctx: Context, release: dict) -> dict:
    """Follow CodePipeline to its evaluation verdict without running a second evaluation locally."""
    developer = ctx.developer
    started, last_stage = time.monotonic(), None
    while release.get("status") == "EVALUATING":
        pipeline = release.get("pipeline") or {}
        if pipeline.get("stage") != last_stage:
            last_stage = pipeline.get("stage")
            say(
                f"  {time.monotonic() - started:5.0f} s  the platform is running release {release['releaseId']} (job "
                f"{pipeline.get('jobId')}, execution {pipeline.get('executionId')}): {str(last_stage).lower()}"
            )
        if time.monotonic() - started > RUN_WAIT_SECONDS:
            raise WorkshopError(
                f"release {release['releaseId']} is still EVALUATING after {RUN_WAIT_SECONDS // 60} minutes: open the Releases "
                "screen to inspect its CodePipeline execution. It may be queued behind an approval; the execution continues"
            )
        time.sleep(RUN_POLL_SECONDS)
        release = developer.get_release(release["releaseId"])
    error = release.get("error") or {}
    if release.get("status") == "DRAFT" and error:
        raise WorkshopError(
            f"release {release['releaseId']} failed in stage {error.get('stage')}: "
            f"{error.get('code')}: {error.get('message')}"
        )
    else:
        print_verdict(release)
    return release


def print_problem_rows(rows: list) -> None:
    """The scenarios that produced no score (FAILED invocation, UNSCORED evaluation) and what to do about them."""
    problems = [r for r in rows if evaluation.is_failed_row(r)]
    for row in problems:
        say(f"  {str(row.get('status')):<9} {row.get('scenarioId')}: {str(row.get('error') or '')[:140]}")
    if any(r.get("status") == "UNSCORED" for r in problems):
        say(
            "  UNSCORED: the evaluators read an incomplete span set for that session; each such scenario counts as 0. "
            "Run this command again (or `Run again` on the Releases screen) to evaluate the release once more"
        )


def print_verdict(outcome: dict) -> None:
    ev = outcome.get("evaluation") or {}
    for name, detail in (ev.get("details") or {}).items():
        label = "scenarios without a score" if name == "_scenarios" else name
        say(
            f"  {label:<24} actual {str(detail.get('actual')):>6}  {detail.get('op')}{detail.get('threshold')}  {'PASS' if detail.get('passed') else 'FAIL'}"
        )
    print_problem_rows(ev.get("results") or [])
    say(f"  -> release {outcome.get('status')} (verdict {ev.get('verdict')})")


def blocked_reasons(ev: dict) -> str:
    """One clause per failed threshold, with the ids of the scenarios without a score."""
    parts = []
    for name, detail in (ev.get("details") or {}).items():
        if detail.get("passed"):
            continue
        if name == "_scenarios":
            ids = ", ".join(str(s) for s in ev.get("failedScenarios") or []) or "see the run file"
            parts.append(f"{detail.get('actual')} scenario(s) without a score ({ids})")
        else:
            parts.append(f"{name} {detail.get('actual')} vs {detail.get('op')}{detail.get('threshold')}")
    return "; ".join(parts) or "see the verdict above"


def closing_line(release: dict, prompt_version: str, expect_pass: bool) -> tuple[str, str]:
    """(level, text) of the command's last line: `ok` for the expected outcomes, `warn` for a v2 release that is BLOCKED (the
    v1 wording, "set prompt_version: v2", would send the learner to a setting that is already in place)."""
    status, release_id = release.get("status"), release.get("releaseId")
    if status == "BLOCKED" and not expect_pass:
        return (
            "ok",
            f"release {release_id} is BLOCKED: the gate caught prompt {prompt_version}. Set prompt_version: v2 in the record "
            "and run `workshop apply 6` again",
        )
    if status == "BLOCKED":
        return (
            "warn",
            f"release {release_id} is BLOCKED although prompt {prompt_version} was expected to pass: "
            f"{blocked_reasons(release.get('evaluation') or {})}. Run `workshop apply 6` again to evaluate it once more "
            "(or `Run again` on the Releases screen)",
        )
    if status == "PENDING_APPROVAL":
        return (
            "ok",
            f"release {release_id} passed the gate: PENDING_APPROVAL. Approve and promote it as the Domain approver on the "
            "Releases screen",
        )
    return "ok", f"release {release_id} is {status}"


def run_release_pipeline(ctx: Context, release: dict, expect_pass: bool) -> dict:
    """Start or follow the native release pipeline; evaluation never runs on this machine."""
    if release["status"] == "EVALUATING":
        return wait_for_platform_run(ctx, release)
    if release["status"] == "DRAFT" or (release["status"] == "BLOCKED" and expect_pass):
        gateway_name, mode = dev_gateway_mode(ctx)
        problem = gateway_mode_problem(mode, gateway_name, int(ctx.inputs(4).get("max_leave_days", 10)))
        if problem and expect_pass:
            raise WorkshopError(f"{problem}. Release {release['releaseId']} stays {release['status']}")
        if problem:
            warn(problem)
        ctx.developer.run_release(release["releaseId"])
        return wait_for_platform_run(ctx, ctx.developer.get_release(release["releaseId"]))
    print_verdict(release)
    return release


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

    head(f"CodePipeline release for blueprint version {version}")
    release = ensure_release(ctx, version, f"workshop: prompt {prompt_version}")
    release = developer.get_release(release["releaseId"])
    if skip_evaluation:
        say(f"  --skip-evaluation: release {release['releaseId']} left {release['status']}; no new pipeline started")
    else:
        release = run_release_pipeline(ctx, release, expect_pass)

    # The pipeline owns Dev deployment. Read its result only after the evaluation has finished.
    if not skip_evaluation and release["status"] in ("BLOCKED", "PENDING_APPROVAL", "APPROVED", "PROMOTED"):
        harness_row = registered(developer, ctx.project_id, "dev", "harness", ctx.names("dev").harness)
        if harness_row:
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
        from . import obo_build

        obo_build.module_6(ctx)

    ctx.state.set(f"releases.{prompt_version}", release_state(release))

    say("")
    level, text = closing_line(release, prompt_version, expect_pass)
    (warn if level == "warn" else ok)(text)
    if release["status"] == "PENDING_APPROVAL":
        say("      fallbacks: `workshop approve release` then `workshop promote`")
    look("Releases screen", f"{ctx.ui_url}/releases/{release['releaseId']}")
    look("Playground", f"{ctx.ui_url}/playground  (environment dev: ask a policy question)")
