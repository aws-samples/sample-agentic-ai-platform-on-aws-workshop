"""GET /spend: LiteLLM team summary, per-key spend vs cap, last 50 spend-log rows of the project (build contract section 4)."""

from __future__ import annotations

import logging

from platform_sdk.litellm import LiteLLMError

from .. import db
from . import clients

logger = logging.getLogger(__name__)
LOG_ROWS = 50


def _is_missing(exc: LiteLLMError) -> bool:
    return exc.status == 404 or "not found" in exc.message.lower() or "does not exist" in exc.message.lower()


def _as_float(value) -> float:
    """Tolerant number conversion for the byEnv sums: one odd row (None, a string) must not fail the whole GET /spend."""
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def log_row(row: dict) -> dict:
    metadata = row.get("metadata") or {}
    return {
        "requestId": row.get("request_id"),
        "startTime": row.get("startTime"),
        "endTime": row.get("endTime"),
        "model": row.get("model"),
        "spend": row.get("spend"),
        "promptTokens": row.get("prompt_tokens"),
        "completionTokens": row.get("completion_tokens"),
        "totalTokens": row.get("total_tokens"),
        "keyAlias": metadata.get("user_api_key_alias") or row.get("api_key_alias"),
        "teamId": row.get("team_id"),
        "callType": row.get("call_type"),
    }


def project_spend(project: dict, env: str | None = None) -> dict:
    project_id = project["projectId"]
    admin = clients.litellm_admin()
    team = None
    try:
        team = admin.team_spend_summary(project_id)
    except LiteLLMError as exc:
        if not _is_missing(exc):
            raise
        logger.info("team %s not found in LiteLLM", project_id)
    keys_out: list[dict] = []
    logs: list[dict] = []
    for item in db.query(db.project_pk(project_id), "KEY#"):
        if env and item.get("env") != env:
            continue
        row = {
            "alias": item.get("alias"),
            "env": item.get("env"),
            "model": item.get("model"),
            "status": item.get("status"),
            "litellmKeyId": item.get("litellmKeyId"),
            "credentialProviderArn": item.get("credentialProviderArn"),
            "maxBudget": item.get("maxBudget"),
            "budgetDuration": item.get("budgetDuration"),
            "spend": None,
            "budgetResetAt": None,
            "blocked": None,
        }
        token = item.get("litellmKeyId")
        if token:
            try:
                info = admin.key_info(token)
                row.update(
                    spend=info.get("spend", 0.0),
                    maxBudget=info.get("max_budget", row["maxBudget"]),
                    budgetDuration=info.get("budget_duration") or row["budgetDuration"],
                    budgetResetAt=info.get("budget_reset_at"),
                    blocked=bool(info.get("blocked")),
                    models=info.get("models") or [],
                )
            except LiteLLMError as exc:
                row["error"] = exc.message
            try:
                logs.extend(log_row(r) for r in admin.spend_logs(api_key=token))
            except LiteLLMError as exc:
                logger.warning("spend logs for %s failed: %s", item.get("alias"), exc.message)
        keys_out.append(row)
    logs.sort(key=lambda r: str(r.get("startTime") or ""), reverse=True)
    by_env: dict[str, dict] = {}
    for row in keys_out:
        entry = by_env.setdefault(row["env"], {"keys": 0, "spend": 0.0, "maxBudget": 0.0, "blocked": 0})
        entry["keys"] += 1
        entry["spend"] = round(entry["spend"] + _as_float(row.get("spend")), 6)
        entry["maxBudget"] = round(entry["maxBudget"] + _as_float(row.get("maxBudget")), 6)
        entry["blocked"] += 1 if row.get("blocked") else 0
    return {
        "projectId": project_id,
        "env": env,
        "team": team,
        "keys": keys_out,
        "byEnv": by_env,
        "logs": logs[:LOG_ROWS],
    }
