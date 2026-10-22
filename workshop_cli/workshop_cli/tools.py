"""The small verbs of the hands-on: call, cap, ask, mode, approve, promote, inventory, traffic, scores."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import boto3

from platform_sdk import agentcore, api, identity, litellm, mcp, metrics, names

from .context import Context, WorkshopError, head, look, ok, say, warn

TOOL_SHORTCUTS = {
    "get_leave_balance": ("hrtools___get_leave_balance", ("employee_id",)),
    "get_employee_profile": ("hrtools___get_employee_profile", ("employee_id",)),
    "get_public_holidays": ("hrtools___get_public_holidays", ("year",)),
    "submit_leave_request": ("hrtools___submit_leave_request", ("employee_id", "days")),
    "retrieve": ("hrkb___Retrieve", ("query",)),
    "get_invoice_status": ("fintools___get_invoice_status", ("invoice_id",)),
}


def _gateway(ctx: Context, key: str) -> dict:
    gateways = ctx.state.get("gateways") or {}
    project_id, env = {
        "hr-dev": (ctx.project_id, "dev"),
        "hr-prod": (ctx.project_id, "prod"),
        "finance-dev": (ctx.finance["project"], "dev"),
    }.get(key, (None, None))
    if project_id is None:
        raise WorkshopError(f"unknown gateway {key!r}: hr-dev | hr-prod | finance-dev")
    gw = (gateways.get(project_id) or {}).get(env)
    if not gw:
        found = agentcore.find_gateway(names.ProjectNames(project_id, env).gateway, ctx.region)
        if not found:
            raise WorkshopError(f"gateway for {project_id}/{env} not found: run `workshop apply 4` first")
        gw = {"id": found["gatewayId"], "arn": found["gatewayArn"], "url": found["gatewayUrl"]}
    return gw


def _caller_session(ctx: Context, caller: str):
    if caller == "me":
        return None
    project_id, env = {
        "hr-dev": (ctx.project_id, "dev"),
        "hr-prod": (ctx.project_id, "prod"),
        "finance-dev": (ctx.finance["project"], "dev"),
    }.get(caller, (None, None))
    if project_id is None:
        raise WorkshopError(f"unknown caller {caller!r}: hr-dev | hr-prod | finance-dev | me")
    project = ctx.developer.get_project(project_id)
    role_arn = project["roles"][env]["agentRoleArn"]
    return mcp.assume_role_session(role_arn, session_name=f"workshop-{caller}", region=ctx.region, wait_seconds=60)


def _arguments(tool: str, args: list[str]) -> tuple[str, dict]:
    if tool in TOOL_SHORTCUTS:
        full, params = TOOL_SHORTCUTS[tool]
    elif "___" in tool:
        full, params = tool, ()
    else:
        raise WorkshopError(
            f"unknown tool {tool!r}; shortcuts: {', '.join(TOOL_SHORTCUTS)}, or a full name such as hrtools___get_leave_balance"
        )
    if args and args[0].startswith("{"):
        return full, json.loads(" ".join(args))
    arguments: dict = {}
    if full == "hrkb___Retrieve":
        return full, {"retrievalQuery": {"text": " ".join(args) or "how many days per week can I work remotely"}}
    if full == "hrtools___submit_leave_request":
        employee = args[0] if args else "E1001"
        days = int(args[1]) if len(args) > 1 else 15
        start = (datetime.now(UTC) + timedelta(days=45)).date().isoformat()
        return full, {"employee_id": employee, "leave_type": "annual", "start_date": start, "days": days}
    if full == "hrtools___get_public_holidays":
        return full, {"year": int(args[0])} if args else {"year": datetime.now(UTC).year}
    for name, value in zip(params, args, strict=False):
        arguments[name] = value
    if not arguments and params:
        arguments[params[0]] = "E1001" if params[0] == "employee_id" else "INV-1003"
    return full, arguments


def _hosting_gateway(full_tool: str, caller: str) -> str:
    """The gateway that hosts a tool: Finance tools live on finance-dev, HR tools on the caller's HR gateway (dev unless hr-prod).

    The caller may be another project's role on purpose: `--as finance-dev get_leave_balance` sends the Finance agent role to the HR
    gateway, where IAM refuses it before any policy runs (the isolation proof of module 4)."""
    if full_tool.startswith("fintools___"):
        return "finance-dev"
    return "hr-prod" if caller == "hr-prod" else "hr-dev"


def call(ctx: Context, tool: str, args: list[str], caller: str = "hr-dev", gateway: str | None = None) -> int:
    if tool == "tools":
        full, arguments = None, None
        gateway_key = gateway or {"finance-dev": "finance-dev", "hr-prod": "hr-prod"}.get(caller, "hr-dev")
    else:
        full, arguments = _arguments(tool, args)
        gateway_key = gateway or _hosting_gateway(full, caller)
    gw = _gateway(ctx, gateway_key)
    session = _caller_session(ctx, caller)
    head(f"{caller} -> gateway {gateway_key}")
    if full is None:
        outcome = mcp.try_list(gw["url"], session=session, region=ctx.region)
    else:
        say(f"  tools/call {full} {json.dumps(arguments)}")
        outcome = mcp.try_call(gw["url"], full, arguments, session=session, region=ctx.region)
    kind = outcome["outcome"]
    detail = json.dumps(outcome.get("detail"), default=str)[:400]
    if kind == "ok":
        ok(f"allowed: {detail}")
    elif kind == "denied" and outcome.get("code") == -32002:
        ok(f"DENIED by the policy engine: JSON-RPC error {outcome.get('code')}: {outcome.get('detail')}")
    elif kind == "denied":
        ok(
            f"the gateway answered JSON-RPC error {outcome.get('code')}: {outcome.get('detail')} (not a policy decision; no such tool here?)"
        )
    elif kind == "http_error":
        ok(f"REFUSED before any policy: HTTP {outcome.get('status')} from IAM ({detail[:160]})")
    else:
        ok(f"the tool itself answered with an error (Lambda business logic): {detail}")
    return 0


def cap(ctx: Context, value: str) -> int:
    admin = ctx.admin
    model_alias = ctx.model_alias
    dev_key = next(
        (k for k in admin.list_keys(ctx.project_id) if k["env"] == "dev" and k["model"] == model_alias), None
    )
    if not dev_key:
        raise WorkshopError("no Dev key yet: run `workshop apply 5` first")
    spend_row = next(
        (k for k in admin.spend(ctx.project_id, "dev").get("keys", []) if k.get("alias") == dev_key["alias"]), {}
    )
    spend_now = float(spend_row.get("spend") or 0)
    record_cap = float(ctx.inputs(5)["key_cap"]["usd"])
    if value == "low":
        new_cap = max(round(min(0.01, spend_now / 2), 6), 0.0001) if spend_now > 0 else 0.01
    elif value == "restore":
        new_cap = max(record_cap, float(dev_key.get("maxBudget") or 0) if spend_now <= record_cap else record_cap)
        new_cap = record_cap
    else:
        new_cap = float(value)
    updated = admin.update_key(ctx.project_id, "dev", model_alias, max_budget=new_cap)
    head(f"[Platform admin] PATCH /projects/{ctx.project_id}/keys/dev/{model_alias} {{maxBudget: {new_cap}}}")
    say(
        f"  key {updated.get('alias')}: spend so far {spend_now} USD, cap now {updated.get('maxBudget')} USD per {updated.get('budgetDuration')}, status {updated.get('status')}"
    )
    if value == "low":
        _show_the_refusal(ctx, dev_key, model_alias)
        say("  then `workshop cap --dev restore`")
    look("Keys tab", f"{ctx.ui_url}/domains")
    return 0


TOKEN_VAULT_SECRET_PREFIX = "bedrock-agentcore-identity!default/apikey/"


def dev_key_from_vault(ctx: Context, provider_name: str, secrets_client=None) -> str | None:
    """The raw Dev key as the token vault keeps it: the API-key credential provider stores it in Secrets Manager under
    `bedrock-agentcore-identity!default/apikey/<provider>-<suffix>` as `{"api_key_value": "sk-..."}` (seen live on 16 Oct 2026).
    The learner's credentials are the account's administrator, so the CLI may read it; the harness reads it through AgentCore
    Identity instead. None when the secret is not there (module 5 not run)."""
    import boto3

    client = secrets_client or boto3.client("secretsmanager", region_name=ctx.region)
    prefix = TOKEN_VAULT_SECRET_PREFIX + provider_name
    page = client.list_secrets(Filters=[{"Key": "name", "Values": [prefix]}], MaxResults=20)
    for row in page.get("SecretList", []):
        if str(row.get("Name", "")).startswith(prefix):
            value = client.get_secret_value(SecretId=row["ARN"]).get("SecretString") or ""
            try:
                return json.loads(value).get("api_key_value") or value
            except ValueError:
                return value
    return None


def _show_the_refusal(ctx: Context, dev_key: dict, model_alias: str) -> None:
    """Module 5 has no agent yet, so the lowered cap is demonstrated with one direct call through the model gateway (through
    the relay on the private landing zone): the proxy refuses it before any model is called, gracefully, with BUDGET_EXCEEDED."""
    provider_name = ctx.names("dev").credential_provider(model_alias)
    try:
        raw_key = dev_key_from_vault(ctx, provider_name)
    except Exception as exc:  # a missing permission on the vault's secret must not fail the cap change itself
        warn(
            f"  could not read the key from the token vault ({exc}); the refusal shows up on the agent's next call instead"
        )
        return
    if not raw_key:
        warn("  the token vault has no key for this provider yet: run `workshop apply 5` first")
        return
    proxy = litellm.admin_client(ctx.cfg, litellm.read_master_key(ctx.cfg), timeout=60)
    say(f"  one more call with key {dev_key.get('alias')} through the model gateway ...")
    try:
        proxy.chat_completion(
            raw_key, model_alias, [{"role": "user", "content": "Reply with the single word OK."}], max_tokens=5
        )
    except litellm.LiteLLMError as exc:
        if exc.budget_exceeded:
            ok(f"BUDGET_EXCEEDED, gracefully: HTTP {exc.status}, {exc.message[:160]}")
        else:
            warn(f"  the proxy refused for another reason: HTTP {exc.status} {exc.message[:160]}")
        return
    warn(
        "  the call went through: the proxy applies a lowered cap on its next spend update; run `workshop cap --dev low` once more"
    )


def ask(ctx: Context, prompt: str, env: str = "dev", session_id: str | None = None) -> int:
    from .apply_6 import converse

    head(f"POST /invoke ({env}): the Playground path")
    result = converse(ctx, env, prompt, f"developer -> {env}", session_id=session_id)
    if result.get("status") != "SUCCEEDED":
        error = result.get("error") or {}
        if error.get("code") == "BUDGET_EXCEEDED":
            details = error.get("details") or {}
            ok(
                f"BUDGET_EXCEEDED, gracefully: key {details.get('alias')} spent {details.get('spend')} USD against a cap of {details.get('maxBudget')} USD; resets {details.get('budgetResetAt')}"
            )
        else:
            warn(f"the conversation failed: {error.get('code')}: {str(error.get('message'))[:200]}")
    return 0


def mode(ctx: Context, new_mode: str, gateway: str = "all") -> int:
    control = agentcore.control_client(ctx.region)
    keys = ["hr-dev", "hr-prod", "finance-dev"] if gateway == "all" else [gateway]
    head(f"[Platform admin] policy mode -> {new_mode}")
    for key in keys:
        gw = _gateway(ctx, key)
        ctx.admin.set_gateway_mode(gw["id"], new_mode)
        current = agentcore.wait_for(
            lambda gateway_id=gw["id"]: control.get_gateway(gatewayIdentifier=gateway_id),
            agentcore.GATEWAY_OK,
            agentcore.GATEWAY_BAD,
            label=f"gateway {gw['id']}",
        )
        attached = current.get("policyEngineConfiguration") or {}
        say(f"  {key:<12} engine {'attached' if attached.get('arn') else 'MISSING'} mode {attached.get('mode')}")
    look("Governance screen", f"{ctx.ui_url}/governance")
    return 0


def approve(ctx: Context, what: str, release_id: str | None = None) -> int:
    if what == "catalogue":
        head("[Platform admin] approve the pending catalogue records")
        reason = "Reviewed in module 5: approved for the People Operations domain"
        count = 0
        for record in ctx.admin.list_catalogue():
            record_id, status = record.get("recordId") or record.get("id"), record.get("status")
            if status == "PENDING_APPROVAL":
                ctx.admin.set_catalogue_status(record_id, "APPROVED", reason)
                count += 1
            say(
                f"  {record.get('name'):<28} {record.get('recordType', ''):<7} {status} -> {'APPROVED' if status == 'PENDING_APPROVAL' else status}"
            )
        ok(f"{count} record(s) approved")
        for name, status in {
            r.get("name"): "APPROVED" for r in ctx.admin.list_catalogue() if r.get("status") == "APPROVED"
        }.items():
            if ctx.state.get(f"registry.records.{name}"):
                ctx.state.set(f"registry.records.{name}.status", status)
        return 0
    release = _pick_release(ctx, release_id, "PENDING_APPROVAL")
    head(f"[Domain approver] approve release {release['releaseId']} (blueprint v{release['blueprintVersion']})")
    for persona in ("developer", "admin"):
        try:
            ctx.api(persona).approve_release(release["releaseId"], "should be refused")
            warn(f"{persona} could approve (unexpected)")
        except api.PlatformApiError as exc:
            say(f"  {persona:<10} POST /releases/{{id}}/approve -> {exc.status} {exc.code} (separation of duties)")
    updated = ctx.approver.approve_release(release["releaseId"], "workshop: every threshold met")
    ok(f"release {updated['releaseId']} -> {updated['status']} by {updated['approval']['by']}")
    say("  next: `workshop promote`")
    return 0


def _pick_release(ctx: Context, release_id: str | None, wanted_status: str) -> dict:
    if release_id:
        return ctx.approver.get_release(release_id)
    candidates = [r for r in ctx.approver.list_releases(ctx.project_id) if r.get("status") == wanted_status]
    candidates.sort(key=lambda r: r.get("createdAt") or "")
    if not candidates:
        raise WorkshopError(f"no release in status {wanted_status} for {ctx.project_id}")
    return candidates[-1]


def promote(ctx: Context, release_id: str | None = None) -> int:
    release = _pick_release(ctx, release_id, "APPROVED")
    head(f"[Domain approver] promote release {release['releaseId']} to Prod")
    accepted = ctx.approver.promote_release(release["releaseId"])
    started = time.monotonic()
    job = ctx.approver.wait_for_job(accepted["jobId"], timeout=900)
    if job.get("status") != "SUCCEEDED":
        raise WorkshopError(
            f"promote job {accepted['jobId']} {job.get('status')}: {json.dumps(job.get('error'), default=str)}"
        )
    out = job["output"]
    say(f"  job SUCCEEDED in {time.monotonic() - started:.0f} s")
    say(
        f"  Prod harness {out.get('harnessName')} version {out.get('harnessVersion')}; endpoint {out.get('endpoint')} {out.get('endpointStatus')}"
    )
    say(
        f"  Prod key {out.get('keyAlias')} ({'created' if out.get('keyCreated') else 'existed'}); online evaluation {out.get('onlineEvaluationConfigId')}"
    )
    hr_prod = ctx.names("prod")
    rows = {(r["type"], r["name"]): r for r in ctx.developer.list_resources(ctx.project_id, env="prod")}
    harness_row = rows.get(("harness", hr_prod.harness))
    online_row = rows.get(("online_evaluation", hr_prod.online_evaluation))
    if harness_row:
        ctx.state.set(
            "harness.prod",
            {
                "id": harness_row["id"],
                "arn": harness_row["arn"],
                "version": harness_row["attributes"].get("version"),
                "blueprintVersion": harness_row["attributes"].get("blueprintVersion"),
                "logGroup": harness_row["attributes"].get("logGroup"),
                "endpoint": out.get("endpoint") or hr_prod.harness_endpoint,
            },
        )
    if online_row:
        ctx.state.set(
            "onlineEvaluation", {"id": online_row["id"], "arn": online_row.get("arn"), "name": online_row["name"]}
        )
    ok(
        "Prod answers through its DEFAULT endpoint, now on the promoted version; the first answer takes about 35 s (cold start)"
    )
    say('  try: `workshop ask --env prod "Which US public holidays are there in November 2026?"`')
    return 0


def inventory(ctx: Context) -> int:
    cfg = ctx.cfg
    admin = ctx.admin
    control = agentcore.control_client(cfg.region)
    head("the platform, by name")
    domains = admin.list_domains()
    projects = admin.list_projects()
    say(f"  domains   {[d.get('domainId') for d in domains]}")
    for p in projects:
        litellm = p.get("litellm") or {}
        say(
            f"  project   {p.get('projectId'):<18} domain {p.get('domainId'):<14} team budget {litellm.get('maxBudget')} USD/{litellm.get('budgetDuration')}  roles {len(p.get('roles') or {})} envs"
        )
    settings = admin.get_settings()
    say(f"  registry  {settings.get('registryId') or '-'}")
    if settings.get("registryId"):
        registry_control = boto3.client("agent-registry-control", region_name=cfg.region)
        for r in registry_control.list_registry_records(registryId=settings["registryId"]).get("registryRecords", []):
            say(f"    record  {r['name']:<28} {r['recordType']:<7} {r['status']}")
    say(f"  engine    {settings.get('policyEngineId') or '-'}")
    policies = 0
    if settings.get("policyEngineId"):
        token = None
        while True:
            page = control.list_policies(
                policyEngineId=settings["policyEngineId"], **({"nextToken": token} if token else {})
            )
            policies += len(page.get("policies", []))
            token = page.get("nextToken")
            if not token:
                break
        say(f"    policies {policies}")
    for g in admin.governance().get("gateways") or []:
        say(f"  gateway   {g.get('name', ''):<26} {g.get('projectId')}/{g.get('env')}  mode {g.get('mode')}")
    for env in ("dev", "prod"):
        n = ctx.names(env)
        memory = agentcore.find_memory(n.memory, cfg.region)
        harness = agentcore.find_harness(n.harness, cfg.region)
        say(f"  {env:<5}     memory {'yes' if memory else 'no':<4} harness {'yes' if harness else 'no'}")
    keys = admin.list_keys(ctx.project_id)
    for k in keys:
        say(
            f"  key       {k.get('alias'):<40} cap {k.get('maxBudget')} USD/{k.get('budgetDuration')}  {k.get('status')}"
        )
    providers = identity.list_providers(cfg.region)
    say(f"  providers {[p['name'] for p in providers]}")
    releases = admin.list_releases(ctx.project_id)
    for r in sorted(releases, key=lambda r: r.get("createdAt") or ""):
        say(f"  release   {r['releaseId']:<28} blueprint v{r['blueprintVersion']} {r['status']}")
    shared_ok = (
        bool(settings.get("registryId")) and bool(settings.get("policyEngineId")) and policies > 0 and bool(keys)
    )
    if shared_ok:
        ok(
            "platform complete: registry, policy engine with policies, gateways, roles, team and key exist"
            + ("; no agent yet" if not agentcore.find_harness(ctx.names("dev").harness, cfg.region) else "")
        )
    else:
        warn(
            "not complete yet: "
            + ", ".join(
                what
                for what, present in (
                    ("registry", bool(settings.get("registryId"))),
                    ("policy engine", bool(settings.get("policyEngineId"))),
                    ("policies", policies > 0),
                    ("a key", bool(keys)),
                )
                if not present
            )
            + " missing"
        )
    return 0


def traffic(ctx: Context, count: int = 3) -> int:
    from .apply_7 import prod_rows, send_traffic

    prod_rows(ctx)
    head(f"traffic: {count} Prod conversations")
    report = send_traffic(ctx, count)
    ok(
        f"{report.summary().get('succeeded')} of {count} answered; online evaluation scores follow 8 to 11 minutes later (`workshop scores`)"
    )
    return 0


def register_agents(ctx: Context) -> int:
    """The Catalogue's Agents tab for agents promoted before the promote job wrote AGENT records, or after a registry failure:
    POST /catalogue/agents/sync as the Platform admin (the same step the promote job runs), one line per project."""
    head("[Platform admin] register the promoted Prod agents in the catalogue")
    outcomes = ctx.admin.sync_catalogue_agents()
    for entry in outcomes:
        if entry.get("skipped"):
            state = f"skipped: {entry['skipped']}"
        elif entry.get("error"):
            state = f"ERROR: {entry['error']}"
        else:
            state = f"{entry.get('status')} ({entry.get('action')}, record {entry.get('recordId')}, version {entry.get('recordVersion')})"
            if entry.get("warning"):
                state += f"; {entry['warning']}"
        say(f"  {str(entry.get('projectId', '?')):<20} {str(entry.get('name', '')):<28} {state}")
    registered = sum(1 for e in outcomes if e.get("recordId"))
    ok(f"{registered} agent record(s) registered")
    look("Catalogue screen", f"{ctx.ui_url}/catalogue?tab=agent")
    return 0


def scores(ctx: Context) -> int:
    from .apply_7 import prod_rows

    _, online_row = prod_rows(ctx)
    head("online evaluation results (last 3 hours)")
    found = metrics.evaluation_metrics(ctx.cfg, online_row["id"], hours=3, period=300)["evaluators"]
    shown = False
    for name, e in found.items():
        if (e.get("samples") or 0) > 0:
            shown = True
            say(f"  {name:<24} mean {e.get('mean')}  samples {e.get('samples')}  labels {e.get('labels')}")
    if not shown:
        say("  none yet: sessions are scored 8 to 11 minutes after they go quiet")
    view = (ctx.developer.metrics(ctx.project_id, "prod", hours=3) or {}).get("evaluation")
    say(f"  platform view (GET /metrics?env=prod): {json.dumps(view, default=str)[:300]}")
    look("Overview", f"{ctx.ui_url}/")
    return 0
