"""Project roles for both environments, stamped from `platform_sdk/templates` (build contract section 2.3)."""

from __future__ import annotations

from platform_sdk.templates import roles as role_templates

from platform_sdk import names

from . import clients


def ensure_roles(project_id: str, allowed_tool_arns: list[str], kb_ids: list[str]) -> dict:
    """{"dev": {"agentRoleArn", "gatewayRoleArn"}, "prod": {...}} (create or re-stamp)."""
    cfg, iam = clients.cfg(), clients.iam()
    return {
        env: role_templates.ensure_project_roles(
            cfg, project_id, env, allowed_tool_arns=list(allowed_tool_arns), kb_ids=list(kb_ids), iam=iam
        )
        for env in names.ENVIRONMENTS
    }


def delete_roles(project_id: str) -> list[str]:
    """Delete the four project roles; returns the names that existed."""
    cfg, iam = clients.cfg(), clients.iam()
    deleted: list[str] = []
    for env in names.ENVIRONMENTS:
        deleted.extend(role_templates.delete_project_roles(cfg, project_id, env, iam=iam))
    return deleted
