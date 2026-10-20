"""Module 4: the tool plane (everything but the ENFORCE flip).

Creates the catalogue registry, the three gateways, the tool and knowledge targets (the record's filter), registers everything
with the platform, publishes the three catalogue records, creates the shared policy engine, attaches it to every gateway in the
record's mode (LOG_ONLY by default) and writes the Cedar policies (the record's leave-days rule). The learner flips ENFORCE on
the Governance screen (or with `workshop mode ENFORCE`).
"""

from __future__ import annotations

import json
import time

import boto3
import yaml

from platform_sdk import agentcore, mcp, names

from .context import Context, head, look, ok, say, warn

RECORD_TAGS = {"Project": "workshop-agent-platform", "ManagedBy": "workshop"}


def _list_all(operation, key, **kwargs):
    items, token = [], None
    while True:
        page = operation(**kwargs, **({"nextToken": token} if token else {}))
        items.extend(page.get(key, []))
        token = page.get("nextToken")
        if not token or not page.get(key):
            return items


def ensure_registry(ctx: Context):
    registry_control = boto3.client("agent-registry-control", region_name=ctx.region)
    name = names.REGISTRY_NAME
    existing = [r for r in _list_all(registry_control.list_registries, "registries") if r.get("name") == name]
    if existing:
        registry_id = existing[0]["registryId"]
        say(f"  registry {name} exists: {registry_id}")
    else:
        created = registry_control.create_registry(
            name=name,
            description="AnyCompany approved catalogue: tools, knowledge bases and models (manual approval)",
            approvalConfiguration={"autoApprovalRules": []},
            tags=RECORD_TAGS,
        )
        registry_id = created["registryArn"].split("/")[-1]
        say(f"  created registry {name}: {registry_id} (READY in about a minute)")
    registry = agentcore.wait_for(
        lambda: registry_control.get_registry(registryId=registry_id),
        ("READY",),
        ("CREATE_FAILED",),
        every=5,
        timeout=600,
        label=f"registry {name}",
    )
    ctx.admin.put_settings(registryId=registry["registryId"], registryArn=registry["registryArn"])
    ctx.state.set(
        "registry", {**(ctx.state.get("registry") or {}), "id": registry["registryId"], "arn": registry["registryArn"]}
    )
    return registry_control, registry


def ensure_gateways(ctx: Context) -> dict:
    hr_project = ctx.developer.get_project(ctx.project_id)
    finance_project = ctx.developer.get_project(ctx.finance["project"])
    plan = [
        (
            ctx.project_id,
            "dev",
            hr_project["roles"]["dev"]["gatewayRoleArn"],
            f"{hr_project['name']} tools and knowledge (dev)",
        ),
        (
            ctx.project_id,
            "prod",
            hr_project["roles"]["prod"]["gatewayRoleArn"],
            f"{hr_project['name']} tools and knowledge (prod)",
        ),
        (
            ctx.finance["project"],
            "dev",
            finance_project["roles"]["dev"]["gatewayRoleArn"],
            f"{finance_project['name']} tools (dev)",
        ),
    ]
    gateways: dict = {}
    for project_id, env, role_arn, description in plan:
        n = names.ProjectNames(project_id, env)
        started = time.monotonic()
        gateway = agentcore.ensure_gateway(
            n.gateway,
            role_arn,
            description=description,
            tags={"ProjectId": project_id, "Environment": env},
            region=ctx.region,
        )
        gateways.setdefault(project_id, {})[env] = {
            "id": gateway["gatewayId"],
            "arn": gateway["gatewayArn"],
            "url": gateway["gatewayUrl"],
        }
        say(f"  {n.gateway:<24} {gateway['status']:<6} {gateway['gatewayId']:<34} {time.monotonic() - started:4.1f} s")
    ctx.state.set("gateways", gateways)
    return gateways, hr_project, finance_project


def ensure_targets(
    ctx: Context, gateways: dict, filter_key: str, filter_value: str
) -> tuple[dict, dict, dict, list, list]:
    cfg = ctx.cfg
    project_spec = yaml.safe_load((ctx.blueprint_dir / "project.yaml").read_text())["spec"]
    hr_tools_spec, hr_kb_spec = project_spec["tools"]["hr-core"], project_spec["knowledge"]["hr-policies"]
    hr_schema_path = ctx.scenario_dir / "tools" / "hr" / "schema.json"
    finance_schema_path = ctx.scenario_dir / "tools" / "finance" / "schema.json"
    hr_schema = json.loads(hr_schema_path.read_text())
    finance_schema = json.loads(finance_schema_path.read_text())
    targets: dict = {}
    for env in ("dev", "prod"):
        gateway_id = gateways[ctx.project_id][env]["id"]
        started = time.monotonic()
        hrtools = agentcore.ensure_lambda_target(
            gateway_id,
            hr_tools_spec["gatewayTarget"],
            cfg.hr_tools_lambda_arn,
            hr_schema_path,
            hr_tools_spec["description"],
            cfg.region,
        )
        hrkb = agentcore.ensure_kb_target(
            gateway_id,
            hr_kb_spec["gatewayTarget"],
            cfg.kb_id,
            filter_value,
            description=hr_kb_spec["description"],
            region=cfg.region,
        )
        targets.setdefault(ctx.project_id, {})[env] = {"hrtools": hrtools["targetId"], "hrkb": hrkb["targetId"]}
        say(
            f"  {ctx.project_id}/{env}: hrtools {hrtools['status']}, hrkb {hrkb['status']} (filter {filter_key} == {filter_value})  {time.monotonic() - started:.1f} s"
        )
    started = time.monotonic()
    fintools = agentcore.ensure_lambda_target(
        gateways[ctx.finance["project"]]["dev"]["id"],
        "fintools",
        cfg.finance_tools_lambda_arn,
        finance_schema_path,
        "AnyCompany Finance tools (invoice status)",
        cfg.region,
    )
    targets.setdefault(ctx.finance["project"], {})["dev"] = {"fintools": fintools["targetId"]}
    say(f"  {ctx.finance['project']}/dev: fintools {fintools['status']}  {time.monotonic() - started:.1f} s")
    ctx.state.set("targets", targets)
    return targets, hr_tools_spec, hr_kb_spec, hr_schema, finance_schema


def register_resources(
    ctx: Context,
    gateways: dict,
    targets: dict,
    hr_tools_spec: dict,
    hr_kb_spec: dict,
    hr_schema: list,
    finance_schema: list,
    kb_filter: dict,
) -> int:
    cfg = ctx.cfg
    dev_api = ctx.developer
    count = 0
    for project_id, by_env in gateways.items():
        for env, gw in by_env.items():
            dev_api.register_resource(
                project_id,
                env,
                "gateway",
                names.ProjectNames(project_id, env).gateway,
                gw["arn"],
                id=gw["id"],
                url=gw["url"],
            )
            count += 1
    for env in ("dev", "prod"):
        gw = gateways[ctx.project_id][env]
        dev_api.register_resource(
            ctx.project_id,
            env,
            "gateway_target",
            hr_tools_spec["gatewayTarget"],
            id=targets[ctx.project_id][env]["hrtools"],
            attributes={
                "kind": "lambda",
                "gatewayArn": gw["arn"],
                "lambdaArn": cfg.hr_tools_lambda_arn,
                "tools": [f"{hr_tools_spec['gatewayTarget']}___{t['name']}" for t in hr_schema],
            },
        )
        dev_api.register_resource(
            ctx.project_id,
            env,
            "gateway_target",
            hr_kb_spec["gatewayTarget"],
            id=targets[ctx.project_id][env]["hrkb"],
            attributes={
                "kind": "knowledge_base",
                "gatewayArn": gw["arn"],
                "knowledgeBaseId": cfg.kb_id,
                "filter": kb_filter,
                "tools": [f"{hr_kb_spec['gatewayTarget']}___Retrieve"],
            },
        )
        count += 2
    dev_api.register_resource(
        ctx.finance["project"],
        "dev",
        "gateway_target",
        "fintools",
        id=targets[ctx.finance["project"]]["dev"]["fintools"],
        attributes={
            "kind": "lambda",
            "gatewayArn": gateways[ctx.finance["project"]]["dev"]["arn"],
            "lambdaArn": cfg.finance_tools_lambda_arn,
            "tools": [f"fintools___{t['name']}" for t in finance_schema],
        },
    )
    return count + 1


def publish_records(
    ctx: Context, registry_control, registry: dict, dev_url: str, tools: list, hr_kb_spec: dict, kb_filter: dict
) -> dict:
    cfg = ctx.cfg
    registry_id = registry["registryId"]
    hr_dev = ctx.names("dev")
    server_json = json.dumps(
        {
            "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
            "name": f"io.anycompany/{ctx.project_id}-tools",
            "description": "AnyCompany HR tools and policy search through the People Operations dev gateway"[:100],
            "version": "1.0.0",
            "remotes": [{"type": "streamable-http", "url": dev_url}],
        }
    )
    tools_json = json.dumps(
        {
            "tools": [
                {
                    "name": t["name"],
                    "description": t.get("description", "")[:300],
                    "inputSchema": t.get("inputSchema", {"type": "object"}),
                }
                for t in tools
            ]
        }
    )
    specs = {
        hr_dev.registry_record_tools: dict(
            displayName="HR Assistant tools (MCP)",
            recordType="MCP",
            recordVersion="1.0",
            description="Leave balance, leave requests, employee profile, public holidays and HR policy search for the People Operations domain",
            descriptors={
                "mcpServer": {
                    "data": server_json,
                    "dataSchemaVersion": "2025-12-11",
                    "additionalData": {"tools": {"data": tools_json, "dataSchemaVersion": "2024-11-05"}},
                }
            },
        ),
        "kb-hr-policies": dict(
            displayName="HR policy knowledge base",
            recordType="CUSTOM",
            recordVersion="1.0",
            description="Bedrock managed knowledge base with the AnyCompany HR policies (leave, remote work, code of conduct, parental leave, learning)",
            descriptors={
                "custom": {
                    "data": json.dumps(
                        {
                            "kind": "knowledge-base",
                            "knowledgeBaseId": cfg.kb_id,
                            "knowledgeBaseArn": cfg.kb_arn,
                            "domain": ctx.domain_id,
                            "filter": kb_filter,
                        }
                    )
                }
            },
        ),
        f"model-{ctx.model_alias}": dict(
            displayName=f"Approved model {ctx.model_alias}",
            recordType="CUSTOM",
            recordVersion="1.0",
            description=f"Approved model alias {ctx.model_alias} in the model gateway; carries the baseline guardrail",
            descriptors={
                "custom": {
                    "data": json.dumps(
                        {
                            "kind": "model",
                            "alias": ctx.model_alias,
                            **(cfg.model_entry(ctx.model_alias) or {}),
                            "modelGateway": cfg.model_gateway_openai_base,
                        }
                    )
                }
            },
        ),
    }

    def ensure_record(name, spec):
        existing = {
            r.get("name"): r
            for r in _list_all(registry_control.list_registry_records, "registryRecords", registryId=registry_id)
        }
        if name in existing:
            record_id = existing[name]["recordId"]
        else:
            created = registry_control.create_registry_record(
                registryId=registry_id, name=name, tags=RECORD_TAGS, **spec
            )
            record_id = created["recordArn"].split("/")[-1]
        record = agentcore.wait_for(
            lambda: registry_control.get_registry_record(registryId=registry_id, recordId=record_id),
            ("DRAFT", "PENDING_APPROVAL", "APPROVED", "REJECTED", "DEPRECATED"),
            ("CREATE_FAILED",),
            every=3,
            timeout=300,
            label=f"record {name}",
        )
        if record["status"] == "DRAFT":
            registry_control.submit_registry_record_for_approval(registryId=registry_id, recordId=record_id)
            record = agentcore.wait_for(
                lambda: registry_control.get_registry_record(registryId=registry_id, recordId=record_id),
                ("PENDING_APPROVAL", "APPROVED"),
                ("CREATE_FAILED", "REJECTED"),
                every=3,
                timeout=300,
                label=f"record {name} submitted",
            )
        return record

    records = {}
    for name, spec in specs.items():
        record = ensure_record(name, spec)
        records[name] = {
            "id": record["recordId"],
            "arn": record["recordArn"],
            "status": record["status"],
            "type": record["recordType"],
        }
        say(f"  {name:<28} {record['recordType']:<7} {record['status']}")
    ctx.state.set("registry.records", records)
    tools_record = records[hr_dev.registry_record_tools]
    ctx.admin.register_resource(
        ctx.project_id,
        "dev",
        "registry_record",
        hr_dev.registry_record_tools,
        tools_record["arn"],
        id=tools_record["id"],
        attributes={"registryId": registry_id, "recordType": "MCP", "status": tools_record["status"]},
    )
    return records


def ensure_policies(ctx: Context, gateways: dict, hr_schema: list, forbid_days: int, mode: str) -> tuple[dict, dict]:
    cfg = ctx.cfg
    control = agentcore.control_client(cfg.region)
    engine = agentcore.ensure_policy_engine(region=cfg.region)
    engine_id, engine_arn = engine["policyEngineId"], engine["policyEngineArn"]
    ctx.admin.put_settings(policyEngineId=engine_id, policyEngineArn=engine_arn)
    ctx.state.set("policyEngine", {"id": engine_id, "arn": engine_arn})
    say(f"  policy engine {engine_id} {engine['status']}")

    for project_id, by_env in gateways.items():
        for env, gw in by_env.items():
            ctx.admin.set_gateway_mode(gw["id"], mode)
            current = agentcore.wait_for(
                lambda gateway_id=gw["id"]: control.get_gateway(gatewayIdentifier=gateway_id),
                agentcore.GATEWAY_OK,
                agentcore.GATEWAY_BAD,
                label=f"gateway {gw['id']}",
            )
            attached = current.get("policyEngineConfiguration") or {}
            say(
                f"  {project_id}/{env:<5} engine {'attached' if attached.get('arn') == engine_arn else 'MISSING'} mode {attached.get('mode')}"
            )

    hr_tools = [f"hrtools___{t['name']}" for t in hr_schema] + ["hrkb___Retrieve"]

    def permit(principal, tool, gateway_arn):
        return f'permit(principal == {principal}, action == AgentCore::Action::"{tool}", resource == AgentCore::Gateway::"{gateway_arn}");'

    def forbid_long_leave(gateway_arn):
        return (
            f'forbid(principal is AgentCore::IamEntity, action == AgentCore::Action::"hrtools___submit_leave_request", '
            f'resource == AgentCore::Gateway::"{gateway_arn}") when {{ context.input.days > {forbid_days} }};'
        )

    def rule_name(tool):
        return (
            "allow_" + (tool.split("___", 1)[1] if tool.startswith("hrtools___") else tool.replace("___", "_")).lower()
        )

    plan = []
    for env in ("dev", "prod"):
        n = ctx.names(env)
        principal, gateway_arn = n.cedar_principal(cfg.account_id), gateways[ctx.project_id][env]["arn"]
        for tool in hr_tools:
            plan.append(
                (
                    ctx.project_id,
                    env,
                    rule_name(tool),
                    tool,
                    permit(principal, tool, gateway_arn),
                    f"{ctx.project_id} {env}: allow {tool}",
                )
            )
        plan.append(
            (
                ctx.project_id,
                env,
                "forbid_long_leave",
                "hrtools___submit_leave_request",
                forbid_long_leave(gateway_arn),
                f"{ctx.project_id} {env}: no leave requests longer than {forbid_days} days through the assistant",
            )
        )
    fin = ctx.names("dev", ctx.finance["project"])
    plan.append(
        (
            ctx.finance["project"],
            "dev",
            "allow_get_invoice_status",
            "fintools___get_invoice_status",
            permit(
                fin.cedar_principal(cfg.account_id),
                "fintools___get_invoice_status",
                gateways[ctx.finance["project"]]["dev"]["arn"],
            ),
            f"{ctx.finance['project']} dev: allow fintools___get_invoice_status",
        )
    )

    policies = {}
    started = time.monotonic()
    for project_id, env, rule, tool, cedar, description in plan:
        name = names.ProjectNames(project_id, env).policy(rule)
        policy = agentcore.ensure_policy(engine_id, name, cedar, description=description, region=cfg.region)
        policies[name] = {
            "id": policy["policyId"],
            "arn": policy.get("policyArn"),
            "env": env,
            "project": project_id,
            "tool": tool,
        }
        ctx.admin.register_resource(
            project_id,
            env,
            "policy",
            name,
            policy.get("policyArn"),
            id=policy["policyId"],
            attributes={"tool": tool, "effect": "forbid" if rule.startswith("forbid") else "permit"},
        )
    say(
        f"  {len(policies)} Cedar policies ACTIVE in {time.monotonic() - started:.0f} s (permit per tool; forbid leave over {forbid_days} days)"
    )
    ctx.state.set("policies", policies)
    return engine, policies


def run(ctx: Context) -> None:
    cfg = ctx.cfg
    m4 = ctx.inputs(4)
    ((filter_key, filter_value),) = m4["knowledge_filter"].items()
    kb_filter = {"equals": {"key": filter_key, "value": filter_value}}

    head("the catalogue registry (AWS Agent Registry, manual approval)")
    registry_control, registry = ensure_registry(ctx)

    head("three gateways: HR dev, HR prod, Finance dev")
    gateways, hr_project, _ = ensure_gateways(ctx)

    head("targets: HR tools, the HR knowledge base with its filter, the Finance tool")
    targets, hr_tools_spec, hr_kb_spec, hr_schema, finance_schema = ensure_targets(
        ctx, gateways, filter_key, filter_value
    )

    head("tools/list on the HR Dev gateway (MCP, signed with SigV4)")
    dev_url = gateways[ctx.project_id]["dev"]["url"]
    control = agentcore.control_client(cfg.region)
    mode_now = (
        control.get_gateway(gatewayIdentifier=gateways[ctx.project_id]["dev"]["id"]).get("policyEngineConfiguration")
        or {}
    ).get("mode")
    caller = None
    if mode_now == "ENFORCE":
        caller = mcp.assume_role_session(
            hr_project["roles"]["dev"]["agentRoleArn"], session_name="workshop-m4", region=cfg.region, wait_seconds=60
        )
        say("  the gateway already enforces policies: listing as the HR Dev agent role")
    tools = mcp.list_tools(dev_url, session=caller, region=cfg.region)
    for tool in sorted(tools, key=lambda t: t["name"]):
        say(f"  - {tool['name']:<34} {tool.get('description', '')[:80]}")

    head("register the resources with the control plane")
    count = register_resources(ctx, gateways, targets, hr_tools_spec, hr_kb_spec, hr_schema, finance_schema, kb_filter)
    ok(f"{count} resources registered (Resources tab of the projects)")

    head("publish three catalogue records (submitted for approval)")
    records = publish_records(ctx, registry_control, registry, dev_url, tools, hr_kb_spec, kb_filter)
    pending = sum(1 for r in records.values() if r["status"] == "PENDING_APPROVAL")

    head(f"the shared policy engine and its policies; gateways attached in {m4['gateway_mode']}")
    ensure_policies(ctx, gateways, hr_schema, int(m4["max_leave_days"]), m4["gateway_mode"])

    # the on-behalf-of gateway and the HR API target (module 2 brought the IdP and the credential providers)
    from . import obo_build

    obo_build.module_4(ctx)

    ok("tool plane built")
    say(
        "\nnext: `workshop call --as hr-dev get_leave_balance E1001`, `--as finance-dev` (403), `submit_leave_request E1001 15`; "
        "`workshop obo` (the employee's token through the on-behalf-of gateway)"
    )
    look(
        "Governance screen",
        f"{ctx.ui_url}/governance  (the policy mode toggle per gateway; flip hr-assistant-dev to ENFORCE)",
    )
    look(
        "Catalogue screen",
        f"{ctx.ui_url}/catalogue  ({pending} record(s) PENDING_APPROVAL; approved in module 5)"
        if pending
        else f"{ctx.ui_url}/catalogue  (the three records are approved)",
    )
    if mode_now != "ENFORCE":
        warn("policy metrics in CloudWatch can lag by up to four minutes")
