"""Stamp the two project roles from the JSON templates (build contract section 2.3).

    arns = roles.ensure_project_roles(cfg, "hr-assistant", "dev", allowed_tool_arns=[hr_lambda_arn], kb_ids=[kb_id])
    # {"agentRoleArn": "arn:aws:iam::<acct>:role/agent-platform-proj-hr-assistant-dev-agent", "gatewayRoleArn": "..."}

Templates carry `{{account_id}}`, `{{region}}`, `{{project_id}}`, `{{env}}`, `{{snake}}` placeholders; the gateway template's
`{{allowed_tool_arns}}` and `{{kb_arns}}` resource placeholders become the project's lists, or `"*"` when a list is empty
(no tools or knowledge bases registered yet). IAM is eventually consistent: a role created a moment ago can take about 10 s
before AgentCore accepts it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from .. import names
from .. import tags as tag_rules

TEMPLATE_DIR = Path(__file__).parent
POLICY_NAME = "agent-platform-project"
PROJECT_TAG = "workshop-agent-platform"
LIST_PLACEHOLDERS = ("{{allowed_tool_arns}}", "{{kb_arns}}")


def _substitute(node: Any, scalars: dict[str, str], lists: dict[str, list[str]]) -> Any:
    """Replace placeholders recursively; a string equal to a list placeholder becomes the list (or "*" when empty)."""
    if isinstance(node, dict):
        return {k: _substitute(v, scalars, lists) for k, v in node.items() if k != "_comment"}
    if isinstance(node, list):
        return [_substitute(v, scalars, lists) for v in node]
    if isinstance(node, str):
        if node in lists:
            values = lists[node]
            return list(values) if values else "*"
        for key, value in scalars.items():
            node = node.replace("{{" + key + "}}", value)
        return node
    return node


def _render(template_name: str, cfg, project_id: str, env: str, lists: dict[str, list[str]] | None = None) -> dict:
    names.validate_project_id(project_id)
    names.validate_env(env)
    template = json.loads((TEMPLATE_DIR / template_name).read_text())
    scalars = {
        "account_id": str(cfg.account_id),
        "region": str(cfg.region),
        "project_id": project_id,
        "env": env,
        "snake": names.snake(project_id),
    }
    rendered = _substitute(template, scalars, lists or {})
    leftover = [m for m in json.dumps(rendered).split("{{")[1:]]
    if leftover:
        raise ValueError(f"{template_name}: unreplaced placeholders: {[m.split('}}')[0] for m in leftover]}")
    return rendered


def kb_arn(cfg, kb_id: str) -> str:
    return (
        kb_id if kb_id.startswith("arn:") else f"arn:aws:bedrock:{cfg.region}:{cfg.account_id}:knowledge-base/{kb_id}"
    )


def render_agent_role(
    cfg, project_id: str, env: str, allowed_tool_arns: list[str] | None = None, kb_ids: list[str] | None = None
) -> dict:
    """{"trust": <assume role policy>, "policy": <inline permission policy>} for the agent (harness execution) role."""
    return _render("agent_role.json", cfg, project_id, env)


def render_gateway_role(
    cfg, project_id: str, env: str, allowed_tool_arns: list[str] | None = None, kb_ids: list[str] | None = None
) -> dict:
    """{"trust", "policy"} for the gateway role: Lambda invoke on the allowed tools, Retrieve on the project's knowledge bases."""
    lists = {
        "{{allowed_tool_arns}}": list(allowed_tool_arns or []),
        "{{kb_arns}}": [kb_arn(cfg, k) for k in (kb_ids or [])],
    }
    return _render("gateway_role.json", cfg, project_id, env, lists)


def role_tags(project_id: str, env: str) -> list[dict]:
    return tag_rules.as_list(
        {"Project": PROJECT_TAG, "ProjectId": project_id, "Environment": env, "ManagedBy": "platform"}
    )


def ensure_role(iam, role_name: str, rendered: dict, project_id: str, env: str, description: str) -> str:
    """Create the role or refresh its trust policy, inline policy and tags; returns the ARN."""
    trust = json.dumps(rendered["trust"])
    try:
        arn = iam.get_role(RoleName=role_name)["Role"]["Arn"]
        iam.update_assume_role_policy(RoleName=role_name, PolicyDocument=trust)
        iam.tag_role(RoleName=role_name, Tags=role_tags(project_id, env))
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
            raise
        arn = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=trust,
            Description=description[:1000],
            Tags=role_tags(project_id, env),
        )["Role"]["Arn"]
    iam.put_role_policy(RoleName=role_name, PolicyName=POLICY_NAME, PolicyDocument=json.dumps(rendered["policy"]))
    return arn


def ensure_project_roles(
    cfg,
    project_id: str,
    env: str,
    allowed_tool_arns: list[str] | None = None,
    kb_ids: list[str] | None = None,
    iam=None,
    settle_seconds: float = 0,
) -> dict:
    """Create or update both roles for one project and environment; returns {"agentRoleArn", "gatewayRoleArn"}."""
    iam = iam or boto3.client("iam", region_name=cfg.region)
    project_names = names.ProjectNames(project_id, env)
    agent_arn = ensure_role(
        iam,
        project_names.agent_role,
        render_agent_role(cfg, project_id, env, allowed_tool_arns, kb_ids),
        project_id,
        env,
        f"Harness execution role of project {project_id} ({env}): gateway, memory, identity, logs",
    )
    gateway_arn = ensure_role(
        iam,
        project_names.gateway_role,
        render_gateway_role(cfg, project_id, env, allowed_tool_arns, kb_ids),
        project_id,
        env,
        f"Gateway role of project {project_id} ({env}): tool Lambdas and knowledge bases",
    )
    if settle_seconds:
        time.sleep(settle_seconds)  # IAM propagation before AgentCore validates the role
    return {"agentRoleArn": agent_arn, "gatewayRoleArn": gateway_arn}


def delete_project_roles(cfg, project_id: str, env: str, iam=None) -> list[str]:
    """Delete both roles (inline policies first); returns the names that existed."""
    iam = iam or boto3.client("iam", region_name=cfg.region)
    project_names = names.ProjectNames(project_id, env)
    deleted: list[str] = []
    for role_name in (project_names.agent_role, project_names.gateway_role):
        try:
            for policy_name in iam.list_role_policies(RoleName=role_name)["PolicyNames"]:
                iam.delete_role_policy(RoleName=role_name, PolicyName=policy_name)
            for attached in iam.list_attached_role_policies(RoleName=role_name)["AttachedPolicies"]:
                iam.detach_role_policy(RoleName=role_name, PolicyArn=attached["PolicyArn"])
            iam.delete_role(RoleName=role_name)
            deleted.append(role_name)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "NoSuchEntity":
                raise
    return deleted
