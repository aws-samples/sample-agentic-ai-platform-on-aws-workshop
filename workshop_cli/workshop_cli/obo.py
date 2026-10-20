"""`workshop obo`: walk the on-behalf-of chain as far as the modules built it and show the token at every hop.

Three shapes, chosen from what exists: after module 2 the employee's token and the exchange by hand (T0, T1); after module 4 the
same, then a tool call on the on-behalf-of gateway with T1 (the gateway exchanges it for T2 and calls the HR API); after module 6
the real thing, the on-behalf-of harness invoked with T0, which exchanges on its own.

1. The employee signs in at Keycloak (client hr-portal, Direct Access Grants): T0, audience hr-agent.
2. The exchange by hand, with a scratch workload identity: GetWorkloadAccessTokenForJWT(T0), then GetResourceOauth2Token with
   ON_BEHALF_OF_TOKEN_EXCHANGE through the provider kc-hr-agent -> T1, same person, audience hr-obo-gateway, azp hr-agent.
   T1 is then shown to the OBO gateway (tools/list with `Authorization: Bearer T1`): the gateway accepts a token it never issued
   because it trusts the realm. This is exactly what the harness does on its own before every tool call.
3. The real call: InvokeHarness over HTTPS with `Authorization: Bearer T0`. The harness exchanges T0 for T1 (gateway tool,
   OAuth TOKEN_EXCHANGE), the gateway exchanges T1 for T2 (target hrapi, OAUTH TOKEN_EXCHANGE), API Gateway validates T2.
4. The evidence: the HR API adapter logs the claims of the token it received (T2): same sub and employee id, audience
   hr-tools-api, azp hr-obo-gateway.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

from platform_sdk import agentcore, identity, keycloak, mcp, names

from .context import Context, WorkshopError, head, look, ok, say, warn

DEFAULT_PROMPT = "What is my annual leave balance? My employee id is {employee_id}."
SHOWN_CLAIMS = ("sub", "preferred_username", names.KC_EMPLOYEE_CLAIM, "azp", "iss")


def show_claims(label: str, token: str) -> dict:
    """Print the claims that tell the story of a hop (no signature check: the services verified the token)."""
    claims = keycloak.decode_claims(token)
    aud = claims.get("aud")
    aud = aud if isinstance(aud, list) else [aud] if aud else []
    say(f"  {label}")
    for key in SHOWN_CLAIMS:
        say(f"    {key:<20} {claims.get(key)}")
    say(f"    {'aud':<20} {aud}")
    if claims.get("exp"):
        say(f"    {'exp':<20} {datetime.fromtimestamp(int(claims['exp']), UTC).strftime('%H:%M:%S')} UTC")
    return claims


def employee_credentials(ctx: Context) -> dict:
    secrets_manager = boto3.client("secretsmanager", region_name=ctx.region)
    current = json.loads(
        secrets_manager.get_secret_value(SecretId=ctx.cfg.users_secret_arn).get("SecretString") or "{}"
    )
    record = current.get("employee")
    if not record:
        raise WorkshopError("no employee login in the users secret: run `workshop apply 2` first")
    return record


def adapter_evidence(ctx: Context, since_ms: int, attempts: int = 12, every: float = 5) -> list[dict]:
    """The `obo_token_received` lines the HR API Lambda logged after `since_ms` (log delivery lags by a few seconds)."""
    log_group = ctx.cfg.get("hr-api/log-group")
    if not log_group:
        return []
    logs = boto3.client("logs", region_name=ctx.region)
    for _ in range(attempts):
        found: list[dict] = []
        kwargs = {"logGroupName": log_group, "startTime": since_ms, "filterPattern": '"obo_token_received"'}
        try:
            while True:
                page = logs.filter_log_events(**kwargs)
                for event in page.get("events", []):
                    message = event.get("message", "")
                    start = message.find("{")
                    if start >= 0:
                        try:
                            found.append(json.loads(message[start:]))
                        except json.JSONDecodeError:
                            pass
                if not page.get("nextToken"):
                    break
                kwargs["nextToken"] = page["nextToken"]
        except logs.exceptions.ResourceNotFoundException:
            return []
        if found:
            return found
        time.sleep(every)
    return []


def chain_state(ctx: Context) -> tuple[dict | None, dict | None]:
    """(harness, gateway) of the on-behalf-of chain as far as the modules built them (state file first, then by name)."""
    obo = ctx.state.get("obo") or {}
    n = ctx.names("dev")
    harness = obo.get("harness")
    if not harness or not harness.get("arn"):
        found = agentcore.find_harness(n.obo_harness, ctx.region)
        harness = {"arn": found["arn"], "id": found["harnessId"]} if found and found.get("status") == "READY" else None
    gateway = obo.get("gateway")
    if not gateway or not gateway.get("url"):
        found = agentcore.find_gateway(n.obo_gateway, ctx.region)
        gateway = (
            {"id": found["gatewayId"], "arn": found["gatewayArn"], "url": found["gatewayUrl"]}
            if found and found.get("status") == "READY"
            else None
        )
    return harness, gateway


def run(ctx: Context, prompt: str | None = None, manual: bool = True) -> int:
    cfg = ctx.cfg
    base = cfg.get("idp/url")
    realm = cfg.get("idp/realm") or names.KC_REALM
    if not base:
        raise WorkshopError(
            "the corporate IdP is not deployed (no SSM /agent-platform/idp/url): run `workshop apply 2` first"
        )
    harness, gateway = chain_state(ctx)
    mode = "harness" if harness else "gateway" if gateway else "identity"
    manual = manual or mode != "harness"  # without the harness, the exchange by hand is the only way to get T1
    creds = employee_credentials(ctx)
    employee_id = creds["employee_id"]
    step = 0

    def next_step(title: str) -> None:
        nonlocal step
        step += 1
        head(f"{step}  {title}")

    next_step(
        f"the employee signs in at Keycloak: {creds['username']} ({employee_id}), client {names.KC_CLIENT_PORTAL}"
    )
    try:
        tokens = keycloak.password_grant(base, realm, names.KC_CLIENT_PORTAL, creds["username"], creds["password"])
    except keycloak.KeycloakError as exc:
        raise WorkshopError(
            f"the employee cannot sign in ({exc}). After a Keycloak task restart the realm is imported again but the employee "
            "is gone: run `workshop apply 2` again"
        ) from exc
    t0 = tokens["access_token"]
    show_claims(f"T0, the employee's own token (what the HR portal would hold; audience {names.KC_CLIENT_AGENT})", t0)

    t1 = None
    if manual:
        next_step("the exchange by hand: what AgentCore Identity does for the harness before a tool call")
        say(
            f"  CreateWorkloadIdentity {names.OBO_DEMO_WORKLOAD} -> GetWorkloadAccessTokenForJWT(T0) -> "
            f"GetResourceOauth2Token(ON_BEHALF_OF_TOKEN_EXCHANGE, {names.OBO_PROVIDER_AGENT}, audience {names.KC_CLIENT_GATEWAY})"
        )
        started = time.monotonic()
        try:
            identity.ensure_workload_identity(names.OBO_DEMO_WORKLOAD, region=cfg.region)
            wat = identity.workload_access_token_for_jwt(names.OBO_DEMO_WORKLOAD, t0, region=cfg.region)
            t1 = identity.obo_token(wat, names.OBO_PROVIDER_AGENT, names.KC_CLIENT_GATEWAY, region=cfg.region)
        except (ClientError, RuntimeError) as exc:
            raise WorkshopError(
                f"the exchange by hand failed ({exc}). Your credentials need bedrock-agentcore:CreateWorkloadIdentity, "
                f"GetWorkloadAccessTokenForJWT and GetResourceOauth2Token; the provider {names.OBO_PROVIDER_AGENT} comes from "
                "`workshop apply 2`"
            ) from exc
        say(
            f"  exchanged in {time.monotonic() - started:.1f} s (Keycloak made the decision; AgentCore Identity held the client secret)"
        )
        show_claims(
            f"T1, same person, minted for the gateway (azp = {names.KC_CLIENT_AGENT}, the client that asked)", t1
        )
        if gateway:
            outcome = mcp.try_list(gateway["url"], bearer_token=t1, region=cfg.region)
            if outcome["outcome"] == "ok":
                ok(f"the on-behalf-of gateway accepts T1 (tools/list): {outcome['detail']}")
            else:
                warn(f"the on-behalf-of gateway refused T1: {outcome}")

    if mode == "identity":
        ok(f"the employee's token can be exchanged on {creds['username']}'s behalf; nothing downstream accepts it yet")
        say(
            "\n  module 4 adds the gateway and the HR API that take T1; module 6 adds the agent that does all of this for the employee"
        )
        ctx.mark("obo", mode=mode)
        return 0

    since_ms = int(time.time() * 1000) - 5000
    if mode == "gateway":
        next_step(
            f"the gateway hop: hrapi___get_leave_balance on {ctx.names('dev').obo_gateway}, Authorization: Bearer T1"
        )
        assert t1 is not None
        say(
            f"  the gateway exchanges T1 for T2 ({names.OBO_PROVIDER_GATEWAY}, audience {names.KC_CLIENT_HR_API}) and calls the "
            "HR API; the HR API answers:"
        )
        result = mcp.call_tool(
            gateway["url"],
            "hrapi___get_leave_balance",
            {"employee_id": employee_id},
            bearer_token=t1,
            region=cfg.region,
        )
        say(f"  < {result.text.strip()[:500]}")
        if result.is_error:
            warn("the tool answered with an error; the evidence below shows whether the HR API was reached")
        tools_used = ["hrapi___get_leave_balance"]
    else:
        next_step("the real call: the employee asks the on-behalf-of harness (HTTPS, Authorization: Bearer T0)")
        text = prompt or DEFAULT_PROMPT.format(employee_id=employee_id)
        say(f"  > {text}")
        try:
            result = agentcore.invoke(
                harness["arn"],
                text,
                session_id=agentcore.new_session_id("obo"),
                timeout=240,
                region=cfg.region,
                bearer_token=t0,
            )
        except agentcore.HarnessInvokeError as exc:
            raise WorkshopError(f"the harness call failed: {exc}") from exc
        say(f"  < {result.text.strip()[:700]}")
        say(
            f"  tools used: {result.tools or 'none'}; {result.usage.get('modelCalls', 0)} model call(s); {result.seconds:.0f} s"
        )
        tools_used = result.tools

    next_step("the evidence: the token the HR API received (T2), from the adapter's log")
    lines = adapter_evidence(ctx, since_ms)
    if not lines:
        warn("no obo_token_received line yet (no tool was called, or log delivery is late); run `workshop obo` again")
    for line in lines[-3:]:
        say(
            f"  tool {line.get('tool')}: sub {line.get('sub')}, employee_id {line.get(names.KC_EMPLOYEE_CLAIM)}, "
            f"aud {line.get('aud')}, azp {line.get('azp')}"
        )
    if lines:
        last = lines[-1]
        same_person = last.get("sub") == keycloak.decode_claims(t0).get("sub")
        ok(
            f"on behalf of {creds['username']}: the HR API saw sub {'identical to T0' if same_person else last.get('sub')}, "
            f"audience {last.get('aud')} (not the agent's), azp {last.get('azp')} (the gateway asked for it)"
        )
    look(
        "HR API log group",
        ctx.console(
            f"cloudwatch/home#logsV2:log-groups/log-group/{(cfg.get('hr-api/log-group') or '').replace('/', '$252F')}"
        ),
    )
    look("Keycloak admin console", f"{base}/admin/master/console/#/{realm}/clients")
    ctx.mark("obo", mode=mode, tools=tools_used, evidence=len(lines))
    return 0
