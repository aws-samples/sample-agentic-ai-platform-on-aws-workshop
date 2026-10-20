"""Module 5: the model gateway in practice: the Dev key, the token vault, the first call, spend."""

from __future__ import annotations

import json
import time

from platform_sdk import identity, litellm

from .context import Context, head, look, ok, say, warn


def proxy_admin(ctx: Context) -> litellm.LiteLLMAdmin:
    """The master-key client of the model gateway. The proxy's load balancer is internal (private landing zone), so the client
    goes through the relay Lambda inside the VPC; `litellm.admin_client` reads the relay's name from SSM and falls back to direct
    HTTP on a public landing zone. The master key comes from Secrets Manager (ARN from SSM)."""
    return litellm.admin_client(ctx.cfg, litellm.read_master_key(ctx.cfg), timeout=60)


def _answer_text(completion: dict) -> str | None:
    """The assistant text of a /v1/chat/completions response (None when the model produced no visible text)."""
    choices = completion.get("choices") or [{}]
    return ((choices[0].get("message") or {}).get("content")) or None


def run(ctx: Context) -> None:
    cfg = ctx.cfg
    m5 = ctx.inputs(5)
    key_cap, key_period = float(m5["key_cap"]["usd"]), str(m5["key_cap"]["period"])
    admin, developer = ctx.admin, ctx.developer
    hr_dev = ctx.names("dev")
    model_alias = ctx.model_alias

    head("governance: approved models, guardrail, engine, gateways")
    governance = admin.governance()
    for model in governance.get("models", []):
        say(
            f"  model {model.get('alias', model.get('model_name')):<20} {('guardrail ' + str(model.get('guardrail'))) if model.get('guardrail') else ''}"
        )
    say(
        f"  guardrail {governance.get('guardrail', {}).get('name')}  policy engine {bool(governance.get('policyEngine'))}  gateways {[(g.get('projectId'), g.get('env'), g.get('mode')) for g in governance.get('gateways', [])]}"
    )

    head("the project's team in the model gateway")
    proxy = proxy_admin(ctx)
    team = proxy.team_spend_summary(ctx.project_id)
    say(
        f"  team {team['teamId']}: spend {team['spend']} of {team['maxBudget']} USD per {team['budgetDuration']}, tpm {team['tpmLimit']}, rpm {team['rpmLimit']}, models {team['models']}, keys {len(team['keys'])}"
    )

    head(f"the Dev key for {model_alias}: cap {key_cap} USD per {key_period}")
    key_alias, provider_name = hr_dev.key_alias(model_alias), hr_dev.credential_provider(model_alias)
    existing_key = proxy.find_key(key_alias)
    provider_arn = identity.get_provider_arn(provider_name, region=cfg.region)
    raw_key = None
    if existing_key and provider_arn:
        key_token = existing_key.get("token") or existing_key.get("token_id")
        say(
            f"  key {key_alias} exists (spend so far {existing_key.get('spend')} USD) and provider {provider_name} exists: reusing both"
        )
    else:
        if existing_key:
            proxy.delete_key(existing_key.get("token") or existing_key.get("token_id"))
            say(f"  key {key_alias} existed without its credential provider: deleted, creating a fresh pair")
        created = proxy.create_service_account_key(
            hr_dev.team,
            key_alias,
            [model_alias],
            key_cap,
            key_period,
            {
                "domain": ctx.domain_id,
                "project": ctx.project_id,
                "agent": hr_dev.harness,
                "env": "dev",
                "model": model_alias,
            },
        )
        raw_key, key_token = created["key"], created["token"]
        say(
            f"  created key {key_alias}: cap {key_cap} USD per {key_period}, restricted to {model_alias}; raw key {raw_key[:7]}...{raw_key[-4:]} (shown once)"
        )
    key_info = proxy.key_info(key_token)
    say(
        f"  hashed key id {key_token[:16]}...  spend {key_info.get('spend')} of {key_info.get('max_budget')} USD, resets {key_info.get('budget_reset_at')}"
    )

    if raw_key:
        head("first calls through the model gateway with the raw key")
        if proxy.via_relay:
            say(
                f"  the proxy is private (internal load balancer): the call goes through {proxy.transport}, a Lambda inside the "
                "VPC that forwards the HTTP request as is; the key travels in the Authorization header, the relay adds nothing"
            )
        started = time.monotonic()
        answer = proxy.chat_completion(
            raw_key,
            model_alias,
            [{"role": "user", "content": "In one sentence, what can an HR assistant help an employee with?"}],
            max_tokens=300,
        )
        cost_header = proxy.last_response_headers.get("x-litellm-response-cost")
        usage = answer.get("usage") or {}
        content = _answer_text(answer)
        say(f"  [{time.monotonic() - started:.1f} s] {content or '(no visible text)'}")
        say(
            f"  usage {usage.get('prompt_tokens')}/{usage.get('completion_tokens')} tokens; cost header x-litellm-response-cost: {cost_header}"
        )
        blocked = proxy.chat_completion(
            raw_key,
            model_alias,
            [{"role": "user", "content": "Can I sue my manager for denying my leave?"}],
            max_tokens=300,
        )
        choice = (blocked.get("choices") or [{}])[0]
        say(f"  guardrail: finish_reason={choice.get('finish_reason')} -> {_answer_text(blocked)!r}")
        other_alias = next((a for a in cfg.model_aliases if a != model_alias), None)
        if other_alias:
            try:
                proxy.chat_completion(raw_key, other_alias, [{"role": "user", "content": "hi"}], max_tokens=5)
                warn(f"{other_alias} with this key was ALLOWED (unexpected)")
            except litellm.LiteLLMError as exc:
                ok(f"{other_alias} with this key -> HTTP {exc.status}: the key is restricted to {model_alias}")

        head("store the key in the AgentCore Identity token vault")
        provider_arn = identity.store_api_key(
            provider_name,
            raw_key,
            tags={"ManagedBy": "workshop", "ProjectId": ctx.project_id, "Environment": "dev"},
            region=cfg.region,
        )
        raw_key = None
    say(f"  credential provider {provider_arn}")

    head("the platform adopts the key")
    developer.register_resource(
        ctx.project_id,
        "dev",
        "credential_provider",
        provider_name,
        provider_arn,
        attributes={"model": model_alias, "keyAlias": key_alias},
    )
    keys = developer.list_keys(ctx.project_id)
    adopted = next((k for k in keys if k.get("env") == "dev" and k.get("model") == model_alias), None)
    if adopted and adopted.get("litellmKeyId") == key_token and adopted.get("credentialProviderArn") == provider_arn:
        say(f"  key already known to the platform: {adopted.get('alias')} status {adopted.get('status')}")
    else:
        adopted = developer.create_key(
            ctx.project_id,
            "dev",
            model_alias,
            adopt={
                "litellmKeyId": key_token,
                "credentialProviderArn": provider_arn,
                "maxBudget": key_cap,
                "budgetDuration": key_period,
            },
        )
        say(
            f"  adopted: {json.dumps({k: adopted.get(k) for k in ('alias', 'env', 'model', 'maxBudget', 'budgetDuration', 'status')})}"
        )
    ctx.state.set(
        "litellm",
        {
            "team": hr_dev.team,
            "keyAlias": key_alias,
            "token": key_token,
            "providerName": provider_name,
            "providerArn": provider_arn,
            "maxBudget": key_cap,
            "budgetDuration": key_period,
        },
    )

    head("spend")
    deadline = time.monotonic() + 60
    while True:
        key_info = proxy.key_info(key_token)
        if float(key_info.get("spend") or 0) > 0 or time.monotonic() > deadline:
            break
        time.sleep(5)
    say(f"  key {key_alias}: spend {key_info.get('spend')} USD of {key_info.get('max_budget')}")
    spend = developer.spend(ctx.project_id, "dev")
    team = spend.get("team") or {}
    say(
        f"  team {team.get('teamId') or hr_dev.team}: spend {team.get('spend')} of {team.get('maxBudget')} USD per {team.get('budgetDuration')}"
    )

    pending = [r for r in admin.list_catalogue() if r.get("status") == "PENDING_APPROVAL"]
    ok("model access built")
    say(
        "\nnext: `workshop cap --dev low` (the cap drops below the spend and one more call is refused with BUDGET_EXCEEDED), then `workshop cap --dev restore`"
    )
    if pending:
        say(
            f"      {len(pending)} catalogue record(s) wait for approval: approve them on the Catalogue screen as Platform admin (`workshop approve catalogue` is the fallback)"
        )
    say("      then `workshop inventory`: platform complete")
    look("Keys tab", f"{ctx.ui_url}/domains  (the project's Keys tab: spend against cap)")
    look("Catalogue screen", f"{ctx.ui_url}/catalogue")
