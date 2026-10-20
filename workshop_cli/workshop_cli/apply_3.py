"""Module 3: onboard the domains and projects. The HR project takes the record's inputs; Finance is fixed."""

from __future__ import annotations

import json
import time

import boto3
import yaml

from platform_sdk import api, config, mcp

from .context import Context, head, look, ok, say, warn


def ensure_domain(client, domain_id: str, name: str, description: str, owner: str) -> dict:
    existing = {d.get("domainId"): d for d in client.list_domains()}
    if domain_id in existing:
        say(f"  domain {domain_id} exists (created by {existing[domain_id].get('createdBy')})")
        return existing[domain_id]
    created = client.create_domain(domain_id, name, description, owner)
    say(f"  created domain {domain_id}: {name}")
    return created


def ensure_project(client, body: dict) -> dict:
    """Create the project, or adopt an existing one (UI-created or an earlier run) and align models, tools and knowledge."""
    try:
        project = client.get_project(body["projectId"])
    except api.PlatformApiError as exc:
        if exc.status != 404:
            raise
        started = time.monotonic()
        project = client.create_project(body)
        say(
            f"  created project {body['projectId']} in {time.monotonic() - started:.1f} s: a model-gateway team and 4 IAM roles"
        )
        return project
    current = {
        "models": list((project.get("litellm") or {}).get("models") or []),
        "allowedToolArns": list(project.get("allowedToolArns") or []),
        "knowledgeBaseIds": list(project.get("knowledgeBaseIds") or []),
    }
    changes, added = {}, []
    for key, values in current.items():
        missing = [v for v in body[key] if v not in values]
        if missing:
            changes[key] = list(dict.fromkeys([*values, *missing]))
            added.append(f"{key} += {missing}")
    if changes:
        project = client.update_project(body["projectId"], **changes)
        say(f"  project {body['projectId']} existed: aligned it ({'; '.join(added)}); IAM needs ~10 s to settle")
    else:
        say(f"  project {body['projectId']} exists (status {project.get('status')})")
    return project


def run(ctx: Context) -> None:
    cfg = ctx.cfg
    m3 = ctx.inputs(3)
    fin = ctx.finance
    admin = ctx.admin

    head("domains")
    ensure_domain(admin, m3["domain"], m3["domain_name"], m3["domain_description"], m3["owner"])
    ensure_domain(admin, fin["domain"], fin["domain_name"], fin["domain_description"], fin["domain_owner"])

    head("projects")
    project_spec = yaml.safe_load((ctx.blueprint_dir / "project.yaml").read_text())["spec"]

    def resolve(value):
        if isinstance(value, str) and value.startswith("ssm:"):
            return cfg.require(value[len("ssm:") :].removeprefix(config.PREFIX).strip("/"))
        return value

    models = list(m3["models"])
    if ctx.model_alias not in models:
        models.append(ctx.model_alias)
    budget = m3["budget"]
    hr_body = {
        "projectId": m3["project"],
        "domainId": m3["domain"],
        "name": m3.get("project_name") or project_spec["name"],
        "description": m3.get("project_description") or project_spec["description"],
        "models": models,
        "maxBudget": budget["usd"],
        "budgetDuration": budget["period"],
        "tpmLimit": budget["tpm"],
        "rpmLimit": budget["rpm"],
        "allowedToolArns": [resolve(t["lambdaArn"]) for t in project_spec["tools"].values()],
        "knowledgeBaseIds": [resolve(k["knowledgeBaseId"]) for k in project_spec["knowledge"].values()],
    }
    finance_body = {
        "projectId": fin["project"],
        "domainId": fin["domain"],
        "name": fin["project_name"],
        "description": fin["project_description"],
        "models": fin["models"],
        "maxBudget": fin["budget"]["usd"],
        "budgetDuration": fin["budget"]["period"],
        "tpmLimit": fin["budget"]["tpm"],
        "rpmLimit": fin["budget"]["rpm"],
        "allowedToolArns": [cfg.finance_tools_lambda_arn],
        "knowledgeBaseIds": [],
    }
    hr_project = ensure_project(admin, hr_body)
    finance_project = ensure_project(admin, finance_body)
    hr_project = admin.get_project(m3["project"])
    finance_project = admin.get_project(fin["project"])

    head("what the platform stamped for the HR project")
    litellm = hr_project.get("litellm") or {}
    say(
        f"  model-gateway team {litellm.get('teamId')}: models {litellm.get('models')}, budget {litellm.get('maxBudget')} USD per {litellm.get('budgetDuration')}"
    )
    for env, roles in (hr_project.get("roles") or {}).items():
        say(f"  {env:<5} agent role   {roles.get('agentRoleArn', '').split('/')[-1]}")
        say(f"        gateway role {roles.get('gatewayRoleArn', '').split('/')[-1]}")

    head("the boundary, proven from the agent role")
    iam = boto3.client("iam", region_name=cfg.region)
    hr_dev = ctx.names("dev")
    try:
        role = iam.get_role(RoleName=hr_dev.agent_role)["Role"]
        say(
            f"  trust: {[s['Principal'] for s in role['AssumeRolePolicyDocument']['Statement']]}  (account root: a workshop default)"
        )
        session = mcp.assume_role_session(
            hr_project["roles"]["dev"]["agentRoleArn"], session_name="workshop-m3", region=cfg.region, wait_seconds=60
        )
        outcome = {}
        for label, arn, payload in (
            ("agent role -> Finance tool Lambda", cfg.finance_tools_lambda_arn, {"invoice_id": "INV-1003"}),
            ("agent role -> HR tools Lambda", cfg.hr_tools_lambda_arn, {"employee_id": "E1001"}),
        ):
            try:
                session.client("lambda", region_name=cfg.region).invoke(
                    FunctionName=arn, Payload=json.dumps(payload).encode()
                )
                outcome[label] = "ALLOWED"
            except Exception as exc:  # AccessDeniedException is the expected answer
                outcome[label] = type(exc).__name__ if not hasattr(exc, "response") else exc.response["Error"]["Code"]
            say(f"  {label:<36} {outcome[label]}")
        if all(v == "AccessDeniedException" for v in outcome.values()):
            ok("the agent role reaches no Lambda directly: tools are reached only through its gateway (module 4)")
        else:
            warn("unexpected Lambda access from the agent role; check the role template")
    except Exception as exc:
        warn(f"could not inspect the agent role ({type(exc).__name__}: {str(exc)[:120]}); IAM may still be propagating")

    st = ctx.state
    st.set("model.alias", ctx.model_alias)
    st.set("project", {"id": m3["project"], "domainId": m3["domain"], "roles": hr_project["roles"]})
    st.set("finance", {"projectId": fin["project"], "domainId": fin["domain"], "roles": finance_project["roles"]})
    ok(f"state file {st.path}")
    look("Platform UI, Domains and projects", f"{ctx.ui_url}/domains  (the project card: caps, models, roles)")
    look("IAM console", ctx.console(f"iam/home#/roles/details/{hr_dev.agent_role}"))
