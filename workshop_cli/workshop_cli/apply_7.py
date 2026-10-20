"""Module 7: operate: Prod traffic, metrics and spend, online evaluation, feedback, memory revisited."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from platform_sdk import agentcore, metrics
from platform_sdk import traffic as traffic_module

from .context import Context, WorkshopError, head, look, ok, say, warn


def prod_rows(ctx: Context) -> tuple[dict, dict]:
    rows = {(r["type"], r["name"]): r for r in ctx.developer.list_resources(ctx.project_id, env="prod")}
    hr_prod = ctx.names("prod")
    harness_row = rows.get(("harness", hr_prod.harness))
    online_row = rows.get(("online_evaluation", hr_prod.online_evaluation))
    if harness_row is None or online_row is None:
        raise WorkshopError("no Prod agent yet: approve and promote release 2 first (module 6)")
    return harness_row, online_row


def send_traffic(ctx: Context, count: int) -> traffic_module.TrafficReport:
    prompts = traffic_module.scenario_prompts(ctx.dataset_path)
    say(
        f"  {count} conversations to Prod, 3 at a time, prompts from {ctx.dataset_path.name} (a cold session takes 25 to 40 s)"
    )
    report = traffic_module.generate(
        count=count,
        prompts=prompts,
        client=ctx.developer,
        project_id=ctx.project_id,
        env="prod",
        concurrency=3,
        timeout=420,
        on_done=lambda c: say(
            f"    {c.index:>2} {c.status:<9} {c.seconds:5.1f} s  {c.tools if c.ok else c.error_code}"
        ),
    )
    for c in report.failed():
        say(f"    {c.index}: {c.error_code}: {c.error}")
    summary = report.summary()
    ctx.state.set(
        "traffic.workshop",
        {"startedAt": datetime.now(UTC).isoformat(), "summary": summary, "sessionIds": report.session_ids()},
    )
    return report


def run(ctx: Context) -> None:
    cfg = ctx.cfg
    developer = ctx.developer
    count = int(ctx.inputs(7)["prod_traffic"])
    harness_row, online_row = prod_rows(ctx)
    control = agentcore.control_client(cfg.region)

    head("the online evaluation registered at promotion")
    online = control.get_online_evaluation_config(onlineEvaluationConfigId=online_row["id"])
    rule = online.get("rule") or {}
    say(
        f"  {online.get('onlineEvaluationConfigName')}: status {online.get('status')}, sampling {(rule.get('samplingConfig') or {}).get('samplingPercentage')} %, quiet {(rule.get('sessionConfig') or {}).get('sessionTimeoutMinutes')} min, evaluators {[e.get('evaluatorId') for e in online.get('evaluators', [])]}"
    )

    head(f"traffic: {count} Prod conversations")
    baseline = {
        name: float(e.get("samples") or 0)
        for name, e in metrics.evaluation_metrics(cfg, online_row["id"], hours=3, period=300)["evaluators"].items()
    }
    report = send_traffic(ctx, count)
    summary = report.summary()
    ok(f"{summary.get('succeeded')} of {count} conversations answered")

    head("metrics and spend, per environment")
    deadline = time.monotonic() + 180
    while True:
        view = developer.metrics(ctx.project_id, "prod", hours=1)
        totals = view.get("totals") or {}
        if (totals.get("invocations") or 0) >= max(1, summary.get("succeeded", 1)) or time.monotonic() > deadline:
            break
        time.sleep(15)
    policy = view.get("policy") or {}
    say(
        f"  prod: invocations {totals.get('invocations')}, errors {totals.get('errors')}, sessions {totals.get('sessions')}, latency p90 max {totals.get('latencyP90Max')} ms"
    )
    say(
        f"  policy decisions on {policy.get('gatewayName')}: allow {totals.get('policyAllow')}, deny {totals.get('policyDeny')} (CloudWatch may lag a minute or two)"
    )
    time.sleep(10)
    spend_all = developer.spend(ctx.project_id)
    team = spend_all.get("team") or {}
    say(f"  team spend {team.get('spend')} of {team.get('maxBudget')} USD; by environment {spend_all.get('byEnv')}")

    head("feedback: one thumbs-up for a Prod answer")
    rated = next((c for c in report.conversations if c.ok), None)
    if rated:
        feedback = developer.feedback(
            ctx.project_id, "prod", rated.session_id, "up", f"workshop: good answer to {rated.prompt[:60]!r}"
        )
        say(f"  POST /feedback -> {feedback['feedbackId']} ({feedback['rating']})")

    head("memory revisited: long-term records of the sample actor")
    sample = ctx.state.get("memory.sample") or {}
    memory_id = ctx.state.get("memory.dev.id")
    if memory_id and sample:
        data = agentcore.data_client(cfg.region)
        records = data.retrieve_memory_records(
            memoryId=memory_id,
            namespace=f"/preferences/{sample.get('actorId', 'E1001')}",
            searchCriteria={"searchQuery": "when does the employee like to take leave", "topK": 3},
        )["memoryRecordSummaries"]
        say(
            f"  preference records for {sample.get('actorId')}: {len(records)}"
            + ("" if records else " (extraction runs asynchronously; check again later)")
        )

    head("online evaluation scores")
    found = {
        name: e
        for name, e in metrics.evaluation_metrics(cfg, online_row["id"], hours=3, period=300)["evaluators"].items()
        if float(e.get("samples") or 0) > baseline.get(name, 0.0)
    }
    earlier = {
        name: e
        for name, e in metrics.evaluation_metrics(cfg, online_row["id"], hours=3, period=300)["evaluators"].items()
        if (e.get("samples") or 0) > 0
    }
    if earlier:
        for name, e in earlier.items():
            say(f"  {name:<24} mean {e.get('mean')}  samples {e.get('samples')}")
    else:
        say(
            "  none yet: sessions are scored 8 to 11 minutes after they go quiet. `workshop scores` shows them when they arrive"
        )
    if found:
        ok("new scores from this traffic are already in")
    look("Overview", f"{ctx.ui_url}/  (invocations, spend per environment, policy decisions, evaluation scores)")
    look("CloudWatch GenAI observability", ctx.console("cloudwatch/home#gen-ai-observability/agent-core"))
    if (summary.get("succeeded") or 0) < max(1, count - 2):
        warn("several conversations failed; read the error codes above")
