"""LiteLLM provisioning (build contract section 7): one team per project, one service-account key per agent, environment and model.

The raw virtual key is stored in AgentCore Identity (`platform_sdk.identity.store_api_key`) and forgotten; DynamoDB keeps the hashed
token (`litellmKeyId`) for /key/info, /key/update, /key/block, /key/unblock, /key/delete and /spend/logs?api_key=.
"""

from __future__ import annotations

import hashlib
import logging
import re

from platform_sdk.litellm import LiteLLMError

from platform_sdk import identity, names

from .. import db, errors, validation
from . import clients

logger = logging.getLogger(__name__)
HASHED_TOKEN_RE = re.compile(
    r"^[0-9a-f]{64}$"
)  # LiteLLM's key id = sha256 hex of the raw virtual key (probed 9 Oct 2026)
PROVIDER_ARN_RE = re.compile(
    r"^arn:aws[a-z-]*:bedrock-agentcore:[a-z0-9-]+:\d{12}:token-vault/[A-Za-z0-9_-]+/apikeycredentialprovider/([A-Za-z0-9_.-]+)$"
)


def team_metadata(domain_id: str, project_id: str) -> dict:
    return {"domain": domain_id, "project": project_id}


def key_metadata(domain_id: str, project_id: str, env: str, model: str) -> dict:
    return {
        "domain": domain_id,
        "project": project_id,
        "agent": names.ProjectNames(project_id, env).harness,
        "env": env,
        "model": model,
    }


def _is_missing(exc: LiteLLMError) -> bool:
    return exc.status == 404 or "not found" in exc.message.lower() or "does not exist" in exc.message.lower()


# ---- teams ----------------------------------------------------------------------------------------------------------------
def ensure_team(project_id: str, domain_id: str, models: list[str], caps: dict) -> dict:
    """Create the project's team (team_id = team_alias = projectId); align caps/models when it already exists.

    Returns {"team": <GET /team/info>, "created": bool}."""
    admin = clients.litellm_admin()
    existed = admin.find_team(project_id) is not None
    team = admin.create_team(
        project_id,
        project_id,
        models,
        caps["maxBudget"],
        caps["budgetDuration"],
        caps["tpmLimit"],
        caps["rpmLimit"],
        team_metadata(domain_id, project_id),
    )
    if existed:
        admin.update_team(
            project_id,
            models=list(models),
            max_budget=caps["maxBudget"],
            budget_duration=caps["budgetDuration"],
            tpm_limit=caps["tpmLimit"],
            rpm_limit=caps["rpmLimit"],
            metadata=team_metadata(domain_id, project_id),
        )
        team = admin.get_team(project_id)
    return {"team": team, "created": not existed}


def update_team(project_id: str, **fields) -> dict:
    """POST /team/update with LiteLLM field names (models, max_budget, budget_duration, tpm_limit, rpm_limit)."""
    fields = {k: v for k, v in fields.items() if v is not None}
    if not fields:
        return {}
    return clients.litellm_admin().update_team(project_id, **fields)


def delete_team(project_id: str) -> bool:
    """Delete the team when it exists; False when there was none."""
    admin = clients.litellm_admin()
    if admin.find_team(project_id) is None:
        return False
    admin.delete_team(project_id)
    return True


# ---- keys -----------------------------------------------------------------------------------------------------------------
def _key_item(
    project: dict,
    env: str,
    model: str,
    token: str,
    provider_arn: str,
    provider_name: str,
    max_budget,
    budget_duration: str,
    status: str,
    created_by: str,
    adopted: bool = False,
) -> dict:
    project_id = project["projectId"]
    return {
        **db.key_key(project_id, env, model),
        "projectId": project_id,
        "alias": names.ProjectNames(project_id, env).key_alias(model),
        "env": env,
        "model": model,
        "litellmKeyId": token,
        "credentialProviderArn": provider_arn,
        "credentialProviderName": provider_name,
        "maxBudget": max_budget,
        "budgetDuration": budget_duration,
        "status": status,
        "adopted": adopted,
        "createdAt": db.now_iso(),
        "createdBy": created_by,
    }


def create_key(project: dict, env: str, model: str, max_budget, budget_duration: str, created_by: str) -> dict:
    """Create the LiteLLM service-account key and its credential provider; returns the KEY item (with key attributes).

    Idempotent like the dry run: an alias that already exists together with its provider is adopted as is (the raw key already
    sits in the token vault); an alias without a provider is deleted and recreated (the raw key is shown once by LiteLLM)."""
    project_id, domain_id = project["projectId"], project["domainId"]
    n = names.ProjectNames(project_id, env)
    alias, provider_name = n.key_alias(model), n.credential_provider(model)
    admin, cfg = clients.litellm_admin(), clients.cfg()
    existing = admin.find_key(alias)
    provider_arn = identity.get_provider_arn(provider_name, region=cfg.region)
    if existing and provider_arn:
        token = existing.get("token") or existing.get("token_id")
        logger.info("key %s and provider %s exist: adopting", alias, provider_name)
        return _key_item(
            project,
            env,
            model,
            token,
            provider_arn,
            provider_name,
            existing.get("max_budget", max_budget),
            existing.get("budget_duration") or budget_duration,
            "suspended" if existing.get("blocked") else "active",
            created_by,
            adopted=True,
        )
    if existing:
        logger.info("key %s exists without its credential provider: recreating the pair", alias)
        admin.delete_key(existing.get("token") or existing.get("token_id"))
    key = admin.create_service_account_key(
        project_id, alias, [model], max_budget, budget_duration, key_metadata(domain_id, project_id, env, model)
    )
    token = key["token"]
    try:
        provider_arn = identity.store_api_key(
            provider_name, key["key"], tags={"ProjectId": project_id, "Environment": env}, region=cfg.region
        )
    except Exception as exc:  # roll back the LiteLLM key: without the provider its raw value is lost
        try:
            admin.delete_key(token)
        except Exception:  # noqa: BLE001 - best effort
            logger.exception("rollback of key %s failed", alias)
        raise errors.ApiError(
            502, "PROVISIONING_FAILED", f"step credential_provider ({provider_name}): {errors.message_of(exc)}"
        ) from exc
    return _key_item(
        project, env, model, token, provider_arn, provider_name, max_budget, budget_duration, "active", created_by
    )


def adopt_key(project: dict, env: str, model: str, adopt: dict, created_by: str) -> dict:
    """Record a key the attendee created by hand (module 5) without creating anything.

    The body is checked before anything is stored (every failure is a 400 naming the field): `litellmKeyId` must be LiteLLM's
    hashed token (a raw `sk-...` key is hashed, never stored or returned), the key must belong to this project's team under the
    alias `<project>-<env>-<model>`, `credentialProviderArn` must be the ARN of the provider `litellm-<project>-<env>-<model>` and
    that provider must exist in AgentCore Identity; `maxBudget` / `budgetDuration` default to the key's own values."""
    project_id = project["projectId"]
    n = names.ProjectNames(project_id, env)
    alias, provider_name = n.key_alias(model), n.credential_provider(model)
    token = adopt.get("litellmKeyId")
    if not isinstance(token, str) or not token.strip():
        raise errors.validation("adopt.litellmKeyId (the hashed token from /key/service-account/generate) is required")
    token = token.strip()
    if token.startswith(
        "sk-"
    ):  # the raw virtual key: LiteLLM identifies it by its sha256; the raw value lives in the token vault only
        token = hashlib.sha256(token.encode()).hexdigest()
    if not HASHED_TOKEN_RE.match(token):
        raise errors.validation(
            "adopt.litellmKeyId must be the hashed token of the key (64 hexadecimal characters: `token` / `token_id` returned by "
            "/key/service-account/generate, or `token` from /key/list), not its alias"
        )
    provider_arn = adopt.get("credentialProviderArn")
    if not isinstance(provider_arn, str) or not provider_arn.strip():
        raise errors.validation("adopt.credentialProviderArn is required")
    provider_arn = provider_arn.strip()
    match = PROVIDER_ARN_RE.match(provider_arn)
    if not match or match.group(1) != provider_name:
        raise errors.validation(
            f"adopt.credentialProviderArn must be the ARN of the API-key credential provider {provider_name!r} "
            f"(arn:aws:bedrock-agentcore:<region>:<account>:token-vault/default/apikeycredentialprovider/{provider_name})"
        )
    admin, cfg = clients.litellm_admin(), clients.cfg()
    try:
        info = admin.key_info(token)
    except LiteLLMError as exc:
        if not _is_missing(exc):
            raise
        raise errors.validation(
            f"adopt.litellmKeyId is not a key the model gateway knows (/key/info: {exc.message}); create the key first"
        ) from exc
    if info.get("team_id") != project_id or info.get("key_alias") != alias:
        raise errors.validation(
            f"adopt.litellmKeyId belongs to team {info.get('team_id')!r} with alias {info.get('key_alias')!r}; the key adopted "
            f"for {project_id}/{env}/{model} must have team_id {project_id!r} and alias {alias!r}"
        )
    live_arn = identity.get_provider_arn(provider_name, region=cfg.region)
    if live_arn != provider_arn:
        raise errors.validation(
            f"credential provider {provider_name!r} "
            + ("does not exist in AgentCore Identity" if live_arn is None else f"has the ARN {live_arn}")
            + "; store the raw key with platform_sdk.identity.store_api_key first (module 5)"
        )
    max_budget = validation.number(adopt, "maxBudget", default=info.get("max_budget"), minimum=0)
    budget_duration = validation.budget_duration(
        validation.string(adopt, "budgetDuration", required=False, default=info.get("budget_duration")),
        key="adopt.budgetDuration",
    )
    return _key_item(
        project,
        env,
        model,
        token,
        provider_arn,
        provider_name,
        max_budget,
        budget_duration,
        "suspended" if info.get("blocked") else "active",
        created_by,
        adopted=True,
    )


def update_key_cap(token: str, max_budget) -> dict:
    return clients.litellm_admin().update_key(token, max_budget=max_budget)


def set_key_status(token: str, status: str) -> dict:
    admin = clients.litellm_admin()
    return admin.block_key(token) if status == "suspended" else admin.unblock_key(token)


def delete_key(key_item: dict) -> dict:
    """Delete the LiteLLM key and the credential provider of a KEY item; both tolerate "already gone"."""
    result = {"alias": key_item.get("alias"), "litellmKey": False, "credentialProvider": False}
    token = key_item.get("litellmKeyId")
    if token:
        try:
            clients.litellm_admin().delete_key(token)
            result["litellmKey"] = True
        except LiteLLMError as exc:
            if not _is_missing(exc):
                raise
    provider_name = key_item.get("credentialProviderName")
    if provider_name:
        result["credentialProvider"] = identity.delete_provider(provider_name, region=clients.cfg().region)
    return result
