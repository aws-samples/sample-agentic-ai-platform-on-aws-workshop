"""Thin boto3 helpers over `bedrock-agentcore-control` and `bedrock-agentcore` (lookup by name first, then create).

Payload shapes and status values come from the spikes of 7-8 Oct 2026 (`.claude/spikes/*.py`): gateway READY in about 6 s,
policy engine ACTIVE in about 11 s, harness READY in 12-14 s, endpoint READY in 6-7 s; the first invocation of a new
harness version takes about 40 s (microVM cold start), later ones 4-8 s.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from . import names
from .config import default_region
from .litellm import BUDGET_PATTERNS

PROJECT_TAG = "workshop-agent-platform"
DEFAULT_TAGS = {"Project": PROJECT_TAG, "ManagedBy": "workshop"}
MIN_SESSION_ID_LENGTH = 33

# Status vocabularies seen in the spikes and the botocore model.
GATEWAY_OK, GATEWAY_BAD = ("READY",), ("FAILED", "UPDATE_UNSUCCESSFUL")
TARGET_OK, TARGET_BAD = ("READY",), ("FAILED", "UPDATE_UNSUCCESSFUL", "SYNCHRONIZE_UNSUCCESSFUL")
ENGINE_OK, ENGINE_BAD = ("ACTIVE",), ("CREATE_FAILED", "UPDATE_FAILED")
POLICY_OK, POLICY_BAD = ("ACTIVE",), ("CREATE_FAILED", "UPDATE_FAILED")
MEMORY_OK, MEMORY_BAD = ("ACTIVE",), ("FAILED",)
HARNESS_OK, HARNESS_BAD = ("READY",), ("CREATE_FAILED", "UPDATE_FAILED", "FAILED")
HARNESS_RECREATE = ("CREATE_FAILED",)  # create_or_update_harness deletes such a harness and creates it again
ENDPOINT_OK, ENDPOINT_BAD = ("READY",), ("CREATE_FAILED", "UPDATE_FAILED")
ONLINE_EVAL_OK, ONLINE_EVAL_BAD = ("ACTIVE",), ("CREATE_FAILED", "UPDATE_FAILED", "ERROR")

MEMORY_STRATEGY_SHAPES = {
    # strategy keyword -> (CreateMemory strategy key, strategy name, namespaces)
    "SUMMARIZATION": ("summaryMemoryStrategy", "summary", ["/summaries/{actorId}/{sessionId}"]),
    "USER_PREFERENCE": ("userPreferenceMemoryStrategy", "preferences", ["/preferences/{actorId}"]),
    "SEMANTIC": ("semanticMemoryStrategy", "facts", ["/facts/{actorId}"]),
}


class AgentCoreError(Exception):
    """A resource ended in a failed status or did not reach the expected status in time."""


class HarnessInvokeError(Exception):
    """InvokeHarness failed. `budget_exceeded` is True when LiteLLM refused the model call because a cap was hit."""

    def __init__(
        self, message: str, budget_exceeded: bool = False, session_id: str | None = None, partial_text: str = ""
    ):
        super().__init__(message)
        self.message = message
        self.budget_exceeded = budget_exceeded
        self.session_id = session_id
        self.partial_text = partial_text


# ---- clients -------------------------------------------------------------------------------------------------------
@lru_cache(maxsize=8)
def control_client(region: str | None = None):
    """Cached `bedrock-agentcore-control` client (region rule: `config.default_region`, the same one `config.load` uses)."""
    return boto3.client("bedrock-agentcore-control", region_name=default_region(region))


@lru_cache(maxsize=8)
def data_client(region: str | None = None, read_timeout: int = 180):
    """Cached `bedrock-agentcore` data-plane client with a long read timeout and no automatic retries (invocations are not idempotent)."""
    return boto3.client(
        "bedrock-agentcore",
        region_name=default_region(region),
        config=Config(read_timeout=read_timeout, connect_timeout=10, retries={"max_attempts": 1, "mode": "standard"}),
    )


def _tags(extra: dict | None) -> dict:
    return {**DEFAULT_TAGS, **(extra or {})}


def _paginate(client, operation: str, result_key: str, **kwargs) -> list[dict]:
    items: list[dict] = []
    for page in client.get_paginator(operation).paginate(**kwargs):
        items.extend(page.get(result_key, []))
    return items


def _get_or_none(getter: Callable[..., dict], key: str | None = None, **kwargs) -> dict | None:
    """Run a Get call (optionally unwrapping `key`); None when the resource vanished between the List and the Get call
    (deletion race seen in the dry run: ListGateways still returned a gateway whose GetGateway raised ResourceNotFound)."""
    try:
        response = getter(**kwargs)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            return None
        raise
    return response[key] if key else response


# ---- waiting -------------------------------------------------------------------------------------------------------
def wait_for(
    getter: Callable[[], dict],
    ok: tuple[str, ...] | list[str],
    bad: tuple[str, ...] | list[str] = (),
    every: float = 5,
    timeout: float = 600,
    label: str = "",
) -> dict:
    """Poll `getter()` until its `status` (or `Status`) is in `ok`; raise AgentCoreError on `bad` or after `timeout` seconds."""
    started = time.monotonic()
    while True:
        current = getter()
        status = current.get("status") or current.get("Status")
        if status in ok:
            return current
        if status in bad:
            reason = current.get("statusReasons") or current.get("failureReason") or current.get("statusReason")
            raise AgentCoreError(f"{label or 'resource'} failed: {status} {reason or ''}".strip())
        if time.monotonic() - started > timeout:
            raise AgentCoreError(f"{label or 'resource'} still {status} after {timeout:.0f} s")
        time.sleep(every)


# ---- gateways and targets ------------------------------------------------------------------------------------------
def find_gateway(name: str, region: str | None = None) -> dict | None:
    """GetGateway output for the gateway with this name, or None."""
    client = control_client(region)
    for item in _paginate(client, "list_gateways", "items"):
        if item.get("name") == name:
            return _get_or_none(client.get_gateway, gatewayIdentifier=item["gatewayId"])
    return None


def ensure_gateway(
    name: str,
    role_arn: str,
    policy_engine_arn: str | None = None,
    mode: str = "LOG_ONLY",
    description: str | None = None,
    tags: dict | None = None,
    region: str | None = None,
    authorizer_type: str = "AWS_IAM",
    authorizer_configuration: dict | None = None,
) -> dict:
    """Create (or find) an MCP gateway; attach the policy engine in `mode` when given. Waits for READY.

    Inbound auth is IAM (SigV4) by default. `authorizer_type="CUSTOM_JWT"` with `authorizer_configuration` =
    `{"customJWTAuthorizer": {"discoveryUrl": ..., "allowedAudience": [...]}}` makes the gateway validate bearer JWTs from the
    OIDC issuer instead (module 4: the on-behalf-of gateway trusts the corporate IdP). An existing gateway is adopted; when a JWT
    authorizer is asked for and the existing one differs (another discovery URL after the IdP stack was recreated, other
    audiences), UpdateGateway brings it to the wanted configuration and the result carries `reconciled: True`.
    """
    client = control_client(region)
    existing = find_gateway(name, region)
    if existing:
        gateway = wait_for(
            lambda: client.get_gateway(gatewayIdentifier=existing["gatewayId"]),
            GATEWAY_OK,
            GATEWAY_BAD,
            label=f"gateway {name}",
        )
        current = gateway.get("policyEngineConfiguration") or {}
        if policy_engine_arn and (current.get("arn") != policy_engine_arn or current.get("mode") != mode):
            gateway = set_gateway_policy_mode(gateway["gatewayId"], policy_engine_arn, mode, region)
        if authorizer_configuration and not _same_authorizer(gateway, authorizer_type, authorizer_configuration):
            gateway = update_gateway_authorizer(gateway["gatewayId"], authorizer_type, authorizer_configuration, region)
            gateway = {**gateway, "reconciled": True}
        return gateway
    kwargs: dict[str, Any] = dict(
        name=name, roleArn=role_arn, protocolType="MCP", authorizerType=authorizer_type, tags=_tags(tags)
    )
    if authorizer_configuration:
        kwargs["authorizerConfiguration"] = authorizer_configuration
    if description:
        kwargs["description"] = description
    if policy_engine_arn:
        kwargs["policyEngineConfiguration"] = {"arn": policy_engine_arn, "mode": mode}
    created = client.create_gateway(**kwargs)
    return wait_for(
        lambda: client.get_gateway(gatewayIdentifier=created["gatewayId"]),
        GATEWAY_OK,
        GATEWAY_BAD,
        label=f"gateway {name}",
    )


def _same_authorizer(gateway: dict, authorizer_type: str, wanted: dict) -> bool:
    """True when the gateway already validates with `wanted` (discovery URL and the sorted audience/client/scope lists)."""
    if gateway.get("authorizerType") != authorizer_type:
        return False
    have = (gateway.get("authorizerConfiguration") or {}).get("customJWTAuthorizer") or {}
    want = wanted.get("customJWTAuthorizer") or {}
    if have.get("discoveryUrl") != want.get("discoveryUrl"):
        return False
    return all(
        sorted(have.get(k) or []) == sorted(want.get(k) or [])
        for k in ("allowedAudience", "allowedClients", "allowedScopes")
    )


def update_gateway_authorizer(
    gateway_id: str, authorizer_type: str, authorizer_configuration: dict, region: str | None = None
) -> dict:
    """UpdateGateway keeping name/role/protocol/policy engine and replacing the inbound authorizer; waits for READY."""
    client = control_client(region)
    gateway = client.get_gateway(gatewayIdentifier=gateway_id)
    kwargs: dict[str, Any] = dict(
        gatewayIdentifier=gateway_id,
        name=gateway["name"],
        roleArn=gateway["roleArn"],
        protocolType=gateway.get("protocolType", "MCP"),
        authorizerType=authorizer_type,
        authorizerConfiguration=authorizer_configuration,
    )
    for key in ("description", "protocolConfiguration", "exceptionLevel", "policyEngineConfiguration"):
        if gateway.get(key):
            kwargs[key] = gateway[key]
    client.update_gateway(**kwargs)
    return wait_for(
        lambda: client.get_gateway(gatewayIdentifier=gateway_id),
        GATEWAY_OK,
        GATEWAY_BAD,
        label=f"gateway {gateway['name']} authorizer",
    )


def set_gateway_policy_mode(
    gateway_id: str, policy_engine_arn: str | None, mode: str, region: str | None = None
) -> dict:
    """UpdateGateway with the current name/role/protocol/authorizer and the policy engine in LOG_ONLY or ENFORCE (None detaches)."""
    client = control_client(region)
    gateway = client.get_gateway(gatewayIdentifier=gateway_id)
    kwargs: dict[str, Any] = dict(
        gatewayIdentifier=gateway_id,
        name=gateway["name"],
        roleArn=gateway["roleArn"],
        protocolType=gateway.get("protocolType", "MCP"),
        authorizerType=gateway["authorizerType"],
    )
    for key in ("description", "protocolConfiguration", "authorizerConfiguration", "exceptionLevel"):
        if gateway.get(key):
            kwargs[key] = gateway[key]
    if policy_engine_arn:
        kwargs["policyEngineConfiguration"] = {"arn": policy_engine_arn, "mode": mode}
    client.update_gateway(**kwargs)
    return wait_for(
        lambda: client.get_gateway(gatewayIdentifier=gateway_id),
        GATEWAY_OK,
        GATEWAY_BAD,
        label=f"gateway {gateway['name']} {mode}",
    )


def find_gateway_target(gateway_id: str, name: str, region: str | None = None) -> dict | None:
    client = control_client(region)
    for item in _paginate(client, "list_gateway_targets", "items", gatewayIdentifier=gateway_id):
        if item.get("name") == name:
            return _get_or_none(client.get_gateway_target, gatewayIdentifier=gateway_id, targetId=item["targetId"])
    return None


def _wait_target(gateway_id: str, target_id: str, label: str, region: str | None) -> dict:
    client = control_client(region)
    return wait_for(
        lambda: client.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id),
        TARGET_OK,
        TARGET_BAD,
        label=label,
    )


def ensure_lambda_target(
    gateway_id: str,
    name: str,
    lambda_arn: str,
    tool_schema: list[dict] | str | Path,
    description: str | None = None,
    region: str | None = None,
) -> dict:
    """Create (or find) a Lambda target. `tool_schema` is the inlinePayload list of {name, description, inputSchema} or a JSON file path."""
    existing = find_gateway_target(gateway_id, name, region)
    if existing:
        return _wait_target(gateway_id, existing["targetId"], f"target {name}", region)
    if isinstance(tool_schema, (str, Path)):
        tool_schema = json.loads(Path(tool_schema).read_text())
    client = control_client(region)
    created = client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=name,
        description=description or f"{name} tools",
        targetConfiguration={
            "mcp": {"lambda": {"lambdaArn": lambda_arn, "toolSchema": {"inlinePayload": tool_schema}}}
        },
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    )
    return _wait_target(gateway_id, created["targetId"], f"target {name}", region)


def jwt_authorizer(
    discovery_url: str, allowed_audience: list[str] | None = None, allowed_clients: list[str] | None = None
) -> dict:
    """`authorizerConfiguration` for CUSTOM_JWT inbound auth (gateway and harness share the shape; one of the lists is required)."""
    config: dict[str, Any] = {"discoveryUrl": discovery_url}
    if allowed_audience:
        config["allowedAudience"] = list(allowed_audience)
    if allowed_clients:
        config["allowedClients"] = list(allowed_clients)
    return {"customJWTAuthorizer": config}


def oauth_credential_configuration(
    provider_arn: str,
    grant_type: str = "TOKEN_EXCHANGE",
    scopes: list[str] | None = None,
    custom_parameters: dict | None = None,
) -> dict:
    """One `credentialProviderConfigurations` entry for OAuth outbound auth of a target.

    `TOKEN_EXCHANGE` is the on-behalf-of mode: the gateway hands the caller's inbound JWT to AgentCore Identity, which exchanges
    it at the provider's IdP for a token minted for the target (`customParameters` carry what the IdP needs, for Keycloak
    `subject_token_type` and `audience`). Supported for OpenAPI and MCP-server targets (outbound-auth support matrix, 2026-10-16).
    """
    oauth: dict[str, Any] = {"providerArn": provider_arn, "scopes": list(scopes or []), "grantType": grant_type}
    if custom_parameters:
        oauth["customParameters"] = dict(custom_parameters)
    return {"credentialProviderType": "OAUTH", "credentialProvider": {"oauthCredentialProvider": oauth}}


def ensure_openapi_target(
    gateway_id: str,
    name: str,
    openapi_spec: str | dict,
    credential_configuration: dict,
    description: str | None = None,
    region: str | None = None,
) -> dict:
    """Create an OpenAPI schema target (inline payload) with the given outbound credential configuration, or reconcile it.

    The gateway publishes every operation as `<name>___<operationId>`; the HR API of module 4 is such a target, called with an
    on-behalf-of token (see `oauth_credential_configuration`). An existing target is kept when its schema and credential
    configuration match, updated when they differ (`reconciled: True` in the result), re-created when it is FAILED or
    SYNCHRONIZE_UNSUCCESSFUL.
    """
    payload = openapi_spec if isinstance(openapi_spec, str) else json.dumps(openapi_spec)
    target_configuration = {"mcp": {"openApiSchema": {"inlinePayload": payload}}}
    client = control_client(region)
    existing = find_gateway_target(gateway_id, name, region)
    if existing and existing.get("status") in TARGET_BAD:
        # a target whose creation or schema synchronisation failed cannot be fixed in place: start again
        client.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=existing["targetId"])
        _wait_gone(lambda: find_gateway_target(gateway_id, name, region), f"target {name} ({existing.get('status')})")
        existing = None
    if existing:
        same_spec = _same_json(
            ((existing.get("targetConfiguration") or {}).get("mcp") or {})
            .get("openApiSchema", {})
            .get("inlinePayload"),
            payload,
        )
        same_credentials = (existing.get("credentialProviderConfigurations") or []) == [credential_configuration]
        if same_spec and same_credentials:
            return _wait_target(gateway_id, existing["targetId"], f"target {name}", region)
        # the HR API moved (new execute-api id) or the exchange parameters changed: UpdateGatewayTarget re-synchronises the schema
        client.update_gateway_target(
            gatewayIdentifier=gateway_id,
            targetId=existing["targetId"],
            name=name,
            description=existing.get("description") or description or f"{name} (OpenAPI)",
            targetConfiguration=target_configuration,
            credentialProviderConfigurations=[credential_configuration],
        )
        target = _wait_target(gateway_id, existing["targetId"], f"target {name} update", region)
        return {**target, "reconciled": True}
    created = client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=name,
        description=description or f"{name} (OpenAPI)",
        targetConfiguration=target_configuration,
        credentialProviderConfigurations=[credential_configuration],
    )
    return _wait_target(gateway_id, created["targetId"], f"target {name}", region)


def _same_json(stored: str | None, wanted: str) -> bool:
    """Compare two JSON documents by value (the service may re-serialise the inline payload it stores)."""
    if not stored:
        return False
    try:
        return json.loads(stored) == json.loads(wanted)
    except (TypeError, ValueError):
        return stored == wanted


def ensure_kb_target(
    gateway_id: str,
    name: str,
    kb_id: str,
    department_filter: str | None,
    description: str | None = None,
    number_of_results: int = 3,
    region: str | None = None,
) -> dict:
    """Create (or find) the Managed Knowledge Base connector target (`bedrock-knowledge-bases`) exposing only `retrievalQuery.text`.

    The department filter is pinned by the admin in `parameterValues` (project-level access control enforced by the gateway);
    the tool is published as `<name>___Retrieve`. Exactly the shape of `.claude/spikes/spike_managed_kb.py`.
    """
    existing = find_gateway_target(gateway_id, name, region)
    if existing:
        return _wait_target(gateway_id, existing["targetId"], f"target {name}", region)
    search: dict[str, Any] = {"numberOfResults": number_of_results}
    if department_filter:
        search["filter"] = {"equals": {"key": "department", "value": department_filter}}
    client = control_client(region)
    created = client.create_gateway_target(
        gatewayIdentifier=gateway_id,
        name=name,
        description=description
        or f"Knowledge base {kb_id}" + (f" ({department_filter} documents)" if department_filter else ""),
        targetConfiguration={
            "mcp": {
                "connector": {
                    "source": {"connectorId": "bedrock-knowledge-bases"},
                    "configurations": [
                        {
                            "name": "Retrieve",
                            "description": description
                            or f"Search the {department_filter or ''} policy documents.".replace("  ", " "),
                            "parameterValues": {
                                "knowledgeBaseId": kb_id,
                                "retrievalConfiguration": {"managedSearchConfiguration": search},
                            },
                            "parameterOverrides": [
                                {"path": "$.retrievalQuery.text", "description": "The search query.", "visible": True}
                            ],
                        }
                    ],
                }
            }
        },
        credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    )
    return _wait_target(gateway_id, created["targetId"], f"target {name}", region)


# ---- policy engine and policies ------------------------------------------------------------------------------------
def find_policy_engine(name: str, region: str | None = None) -> dict | None:
    client = control_client(region)
    for engine in _paginate(client, "list_policy_engines", "policyEngines"):
        if engine.get("name") == name:
            return _get_or_none(client.get_policy_engine, policyEngineId=engine["policyEngineId"])
    return None


def ensure_policy_engine(
    name: str = names.POLICY_ENGINE_NAME,
    description: str | None = None,
    tags: dict | None = None,
    region: str | None = None,
) -> dict:
    """Create (or find) the shared policy engine; waits for ACTIVE."""
    client = control_client(region)
    existing = find_policy_engine(name, region)
    engine_id = (
        existing["policyEngineId"]
        if existing
        else client.create_policy_engine(
            name=name, description=description or "Workshop agent platform policy engine", tags=_tags(tags)
        )["policyEngineId"]
    )
    return wait_for(
        lambda: client.get_policy_engine(policyEngineId=engine_id), ENGINE_OK, ENGINE_BAD, label=f"policy engine {name}"
    )


def find_policy(engine_id: str, name: str, region: str | None = None) -> dict | None:
    client = control_client(region)
    for policy in _paginate(client, "list_policies", "policies", policyEngineId=engine_id):
        if policy.get("name") == name:
            return _get_or_none(client.get_policy, policyEngineId=engine_id, policyId=policy["policyId"])
    return None


def ensure_policy(
    engine_id: str,
    name: str,
    cedar: str,
    enforcement: str = "ACTIVE",
    validation: str = "FAIL_ON_ANY_FINDINGS",
    description: str | None = None,
    region: str | None = None,
) -> dict:
    """Create (or find and, when the Cedar text or enforcement mode differs, update) a policy; waits for ACTIVE.

    Facts from the live runs (7-8 Oct 2026): scope `forbid` rules with `principal is AgentCore::IamEntity` (an unscoped
    `forbid(principal, ...)` is rejected for OAuthUser principals), and create the `permit` for a tool before a `forbid` on the
    same tool: with no permit the analyzer rejects the forbid as "Overly Restrictive" (it would deny every request). A policy
    that ends CREATE_FAILED is deleted again so the name can be reused; the error carries the analyzer's findings.
    """
    client = control_client(region)
    existing = find_policy(engine_id, name, region)
    if existing and existing.get("status") in POLICY_BAD:
        client.delete_policy(policyEngineId=engine_id, policyId=existing["policyId"])
        _wait_gone(lambda: [p for p in [find_policy(engine_id, name, region)] if p], f"failed policy {name}")
        existing = None
    if existing:
        current = ((existing.get("definition") or {}).get("cedar") or {}).get("statement")
        if current != cedar or existing.get("enforcementMode") != enforcement:
            client.update_policy(
                policyEngineId=engine_id,
                policyId=existing["policyId"],
                definition={"cedar": {"statement": cedar}},
                validationMode=validation,
                enforcementMode=enforcement,
            )
        policy_id = existing["policyId"]
    else:
        kwargs: dict[str, Any] = dict(
            name=name,
            policyEngineId=engine_id,
            definition={"cedar": {"statement": cedar}},
            validationMode=validation,
            enforcementMode=enforcement,
        )
        if description:
            kwargs["description"] = description
        policy_id = client.create_policy(**kwargs)["policyId"]
    try:
        return wait_for(
            lambda: client.get_policy(policyEngineId=engine_id, policyId=policy_id),
            POLICY_OK,
            POLICY_BAD,
            every=3,
            timeout=120,
            label=f"policy {name}",
        )
    except AgentCoreError as exc:
        if not existing:  # remove the failed resource so the next attempt can reuse the name
            try:
                client.delete_policy(policyEngineId=engine_id, policyId=policy_id)
            except ClientError:
                pass
        raise AgentCoreError(
            f"{exc} -- hints: create the permit policies for the same tool before a forbid, scope forbids with "
            "`principal is AgentCore::IamEntity`, or pass validation='IGNORE_ALL_FINDINGS' to bypass the analyzer"
        ) from exc


# ---- memory --------------------------------------------------------------------------------------------------------
def find_memory(name: str, region: str | None = None) -> dict | None:
    """GetMemory output for the memory with this name (ListMemories has no name: candidates are matched by id prefix)."""
    client = control_client(region)
    for summary in _paginate(client, "list_memories", "memories"):
        memory_id = summary.get("id", "")
        if memory_id == name or memory_id.startswith(name + "-"):
            memory = _get_or_none(client.get_memory, "memory", memoryId=memory_id)
            if memory and memory.get("name") == name:
                return memory
    return None


def memory_strategies(strategies: list[str]) -> list[dict]:
    """CreateMemory `memoryStrategies` for SUMMARIZATION | USER_PREFERENCE | SEMANTIC with `/{actorId}/...` namespaces."""
    shapes = []
    for strategy in strategies:
        if strategy not in MEMORY_STRATEGY_SHAPES:
            raise ValueError(f"unknown memory strategy {strategy!r}; allowed: {list(MEMORY_STRATEGY_SHAPES)}")
        key, strategy_name, namespaces = MEMORY_STRATEGY_SHAPES[strategy]
        shapes.append({key: {"name": strategy_name, "namespaces": list(namespaces)}})
    return shapes


def ensure_memory(
    name: str,
    strategies: list[str],
    event_expiry_days: int = 30,
    description: str | None = None,
    tags: dict | None = None,
    region: str | None = None,
    wait: bool = True,
) -> dict:
    """Create (or find) a memory with the given long-term strategies; waits for ACTIVE (the Memory dict with arn and id).

    A memory with two strategies takes about 150 s to become ACTIVE. `wait=False` returns right after CreateMemory (status
    CREATING) so a module can create several memories and then `wait_memory` once for each.
    """
    client = control_client(region)
    existing = find_memory(name, region)
    if existing:
        memory_id = existing["id"]
        if not wait:
            return existing
    else:
        kwargs: dict[str, Any] = dict(
            name=name,
            eventExpiryDuration=event_expiry_days,
            memoryStrategies=memory_strategies(strategies),
            tags=_tags(tags),
        )
        if description:
            kwargs["description"] = description
        created = client.create_memory(**kwargs)["memory"]
        memory_id = created["id"]
        if not wait:
            return created
    return wait_memory(memory_id, region)


def wait_memory(memory_id: str, region: str | None = None) -> dict:
    """Wait until the memory is ACTIVE (about 2.5 minutes for a fresh one with two strategies); returns the Memory dict."""
    client = control_client(region)
    return wait_for(
        lambda: client.get_memory(memoryId=memory_id)["memory"], MEMORY_OK, MEMORY_BAD, label=f"memory {memory_id}"
    )


# ---- harness and endpoints -----------------------------------------------------------------------------------------
def find_harness(name: str, region: str | None = None) -> dict | None:
    """GetHarness `harness` dict for the harness with this name, or None."""
    client = control_client(region)
    for summary in _paginate(client, "list_harnesses", "harnesses"):
        if summary.get("harnessName") == name:
            return _get_or_none(client.get_harness, "harness", harnessId=summary["harnessId"])
    return None


def create_or_update_harness(name: str, spec: dict, tags: dict | None = None, region: str | None = None) -> dict:
    """Create the harness from a rendered spec, or update it (new immutable version) when it exists; waits for READY.

    `spec` is the output of `blueprint.render` (CreateHarness kwargs). UpdateHarness rejects `tags` and wraps `memory` in
    `optionalValue`, which `blueprint.for_update` takes care of.
    """
    from .blueprint import for_update

    client = control_client(region)
    existing = find_harness(name, region)
    if existing and existing.get("status") in HARNESS_RECREATE:
        # A harness whose creation failed (for example the account's first CreateHarness before the Runtime service-linked
        # role existed, 15 Oct 2026) cannot be updated: delete it, wait until the name is free, create it again.
        for endpoint in _paginate(client, "list_harness_endpoints", "endpoints", harnessId=existing["harnessId"]):
            endpoint_name = endpoint.get("endpointName") or endpoint.get("name")
            if endpoint_name and endpoint_name != "DEFAULT":
                client.delete_harness_endpoint(harnessId=existing["harnessId"], endpointName=endpoint_name)
        client.delete_harness(harnessId=existing["harnessId"])
        _wait_gone(
            lambda: find_harness(name, region),
            f"harness {name} ({existing['status']}, deleted before re-creation)",
            timeout=600,
        )
        existing = None
    if existing:
        # CreateHarness and UpdateHarness answer {"harness": {...}} like GetHarness; the update carries the new version.
        updated = client.update_harness(harnessId=existing["harnessId"], **for_update(spec))["harness"]
        harness_id, version = existing["harnessId"], updated.get("harnessVersion")
    else:
        payload = {k: v for k, v in spec.items() if k not in ("harnessName", "tags")}
        created = client.create_harness(harnessName=name, tags=_tags(tags), **payload)["harness"]
        harness_id, version = created["harnessId"], created.get("harnessVersion")

    def current() -> dict:
        harness = client.get_harness(harnessId=harness_id)["harness"]
        if version is not None and str(harness.get("harnessVersion")) != str(version):
            # GetHarness may still describe the previous version as READY right after UpdateHarness: keep polling.
            return {**harness, "status": "UPDATING"}
        return harness

    return wait_for(current, HARNESS_OK, HARNESS_BAD, every=3, label=f"harness {name}")


def find_endpoint(harness_id: str, name: str, region: str | None = None) -> dict | None:
    client = control_client(region)
    for endpoint in _paginate(client, "list_harness_endpoints", "endpoints", harnessId=harness_id):
        if endpoint.get("endpointName") == name or endpoint.get("name") == name:
            return _get_or_none(client.get_harness_endpoint, "endpoint", harnessId=harness_id, endpointName=name)
    return None


def ensure_endpoint(
    harness_id: str, name: str, version: str | int, description: str | None = None, region: str | None = None
) -> dict:
    """Create the named endpoint pinned to `version`, or repoint it when it targets another version; waits for READY.

    Unused by the platform since 2026-10-22: the Workshop Studio event accounts deny CreateHarnessEndpoint by SCP, so both
    environments are reached through DEFAULT. Kept for scripts and for accounts that still carry a `live` endpoint."""
    client = control_client(region)
    version = str(version)
    existing = find_endpoint(harness_id, name, region)
    if existing:
        if str(existing.get("targetVersion") or existing.get("liveVersion")) != version:
            client.update_harness_endpoint(harnessId=harness_id, endpointName=name, targetVersion=version)
    else:
        kwargs: dict[str, Any] = dict(harnessId=harness_id, endpointName=name, targetVersion=version)
        if description:
            kwargs["description"] = description
        client.create_harness_endpoint(**kwargs)
    return wait_for(
        lambda: client.get_harness_endpoint(harnessId=harness_id, endpointName=name)["endpoint"],
        ENDPOINT_OK,
        ENDPOINT_BAD,
        every=3,
        label=f"endpoint {name}",
    )


def harness_log_group(harness: dict, endpoint: str) -> str:
    """CloudWatch log group of a harness endpoint, from the GetHarness dict (agent runtime id) and the endpoint name."""
    runtime_id = ((harness.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {}).get("agentRuntimeId")
    if not runtime_id:
        raise AgentCoreError(
            "the harness dict has no environment.agentCoreRuntimeEnvironment.agentRuntimeId (pass GetHarness output)"
        )
    return names.harness_log_group(runtime_id, endpoint)


# ---- online evaluation ---------------------------------------------------------------------------------------------
def find_online_evaluation(name: str, region: str | None = None) -> dict | None:
    client = control_client(region)
    for config in _paginate(client, "list_online_evaluation_configs", "onlineEvaluationConfigs"):
        if config.get("onlineEvaluationConfigName") == name:
            return _get_or_none(
                client.get_online_evaluation_config, onlineEvaluationConfigId=config["onlineEvaluationConfigId"]
            )
    return None


def ensure_online_evaluation(
    name: str,
    log_group: str,
    service_name: str,
    evaluator_ids: list[str],
    role_arn: str,
    sampling_percentage: float = 100.0,
    session_timeout_minutes: int = 1,
    tags: dict | None = None,
    region: str | None = None,
) -> dict:
    """Create (or find) an online evaluation config over one harness endpoint's log group and service name; waits for ACTIVE.

    An existing config that watches another log group or service name is repointed (UpdateOnlineEvaluationConfig): accounts
    promoted before 2026-10-22 watched the `live` endpoint, and Prod now answers through DEFAULT."""
    client = control_client(region)
    source = {"cloudWatchLogs": {"logGroupNames": [log_group], "serviceNames": [service_name]}}
    existing = find_online_evaluation(name, region)
    if existing:
        config_id = existing["onlineEvaluationConfigId"]
        current = (existing.get("dataSourceConfig") or {}).get("cloudWatchLogs") or {}
        if sorted(current.get("logGroupNames") or []) != [log_group] or sorted(current.get("serviceNames") or []) != [
            service_name
        ]:
            client.update_online_evaluation_config(onlineEvaluationConfigId=config_id, dataSourceConfig=source)
    else:
        config_id = client.create_online_evaluation_config(
            onlineEvaluationConfigName=name,
            description=f"Online evaluation of {service_name}",
            rule={
                "samplingConfig": {"samplingPercentage": sampling_percentage},
                "sessionConfig": {"sessionTimeoutMinutes": session_timeout_minutes},
            },
            dataSourceConfig=source,
            evaluators=[{"evaluatorId": e} for e in evaluator_ids],
            evaluationExecutionRoleArn=role_arn,
            enableOnCreate=True,
            tags=_tags(tags),
        )["onlineEvaluationConfigId"]
    return wait_for(
        lambda: client.get_online_evaluation_config(onlineEvaluationConfigId=config_id),
        ONLINE_EVAL_OK,
        ONLINE_EVAL_BAD,
        every=3,
        label=f"online evaluation {name}",
    )


# ---- invocation ----------------------------------------------------------------------------------------------------
@dataclass
class InvokeResult:
    """Parsed InvokeHarness stream: answer text, tools used (gateway form `hrtools___get_leave_balance`), usage and events."""

    text: str
    tools: list[str]
    usage: dict
    stop_reasons: list[str]
    hooks: list[dict]
    session_id: str
    seconds: float
    usage_per_call: list[dict] = field(default_factory=list, repr=False)

    @property
    def stop_reason(self) -> str | None:
        return self.stop_reasons[-1] if self.stop_reasons else None

    def summary(self) -> dict:
        return {
            "text": self.text,
            "toolsUsed": self.tools,
            "usage": self.usage,
            "stopReason": self.stop_reason,
            "sessionId": self.session_id,
            "seconds": round(self.seconds, 1),
        }


def new_session_id(suffix: str = "sdk") -> str:
    """Runtime session ids must be at least 33 characters: uuid4 plus a suffix (`<uuid>-sdk`)."""
    session_id = f"{uuid.uuid4()}-{suffix}"
    assert len(session_id) >= MIN_SESSION_ID_LENGTH
    return session_id


def is_budget_exceeded(error: Any) -> bool:
    """True when an error (exception, event dict or text) carries LiteLLM's budget_exceeded refusal.

    Only LiteLLM's own phrases count (`litellm.BUDGET_PATTERNS`): a message that merely mentions a budget and something
    exceeded (for example a duration limit) is not a cap hit.
    """
    text = json.dumps(error, default=str) if isinstance(error, dict) else str(error)
    lowered = text.lower()
    return any(p in lowered for p in BUDGET_PATTERNS)


_BUDGET_NUMBERS = re.compile(r"current cost:\s*([0-9.]+).*?max budget:\s*([0-9.]+)", re.IGNORECASE | re.DOTALL)
_BUDGET_SUBJECT = re.compile(r"\b(Key|Team|User|Organization)=([^\s,]+)", re.IGNORECASE)


def parse_budget_message(message: str) -> dict:
    """Cap details from LiteLLM's message ("Budget has been exceeded! Key=hr-assistant-dev Current cost: 0.00309, Max budget: 0.003")."""
    info: dict[str, Any] = {"budgetExceeded": is_budget_exceeded(message)}
    numbers = _BUDGET_NUMBERS.search(message or "")
    if numbers:
        info["currentCost"] = float(numbers.group(1))
        info["maxBudget"] = float(numbers.group(2))
    subject = _BUDGET_SUBJECT.search(message or "")
    if subject:
        info["subjectType"] = subject.group(1).lower()
        info["subject"] = subject.group(2)
    return info


def parse_stream(stream, started: float, session_id: str) -> InvokeResult:
    """Fold InvokeHarness stream events into an InvokeResult (same rules as `.claude/spikes/harness_util.py`)."""
    text: list[str] = []
    tools: list[str] = []
    stops: list[str] = []
    hooks: list[dict] = []
    usage: list[dict] = []
    errors: list[str] = []
    for event in stream:
        if "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            if "text" in delta:
                text.append(delta["text"])
        elif "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                tools.append(start["toolUse"].get("name"))
        elif "messageStop" in event:
            stops.append(event["messageStop"].get("stopReason"))
        elif "hookEvent" in event:
            hooks.append({k: event["hookEvent"].get(k) for k in ("name", "type", "decision", "reason")})
        elif "metadata" in event:
            usage.append(event["metadata"].get("usage") or {})
        elif "runtimeClientError" in event:
            errors.append(event["runtimeClientError"].get("message") or "runtimeClientError")
        elif "internalServerException" in event:
            errors.append(event["internalServerException"].get("message") or "internalServerException")
        elif "validationException" in event:
            errors.append(event["validationException"].get("message") or "validationException")
    answer = "".join(text)
    if errors:
        message = "; ".join(errors)
        raise HarnessInvokeError(
            message, budget_exceeded=is_budget_exceeded(message), session_id=session_id, partial_text=answer
        )
    totals = {
        "inputTokens": sum(u.get("inputTokens", 0) for u in usage),
        "outputTokens": sum(u.get("outputTokens", 0) for u in usage),
        "totalTokens": sum(u.get("totalTokens", 0) for u in usage),
        "modelCalls": len(usage),
    }
    return InvokeResult(answer, tools, totals, stops, hooks, session_id, time.monotonic() - started, usage)


def bearer_client(bearer_token: str, region: str | None = None, read_timeout: int = 180):
    """A `bedrock-agentcore` client that sends `Authorization: Bearer <token>` instead of a SigV4 signature.

    A harness (or runtime) with a custom JWT authorizer is invoked over the same HTTPS endpoint and request shape as with SigV4
    (harness-security.html: `POST /harnesses/invoke?harnessArn=...` with the bearer header), so an unsigned botocore client whose
    request gets the header right before it is sent keeps the event-stream parsing of `parse_stream`.
    """
    from botocore import UNSIGNED
    from botocore.config import Config

    client = boto3.client(
        "bedrock-agentcore",
        region_name=default_region(region),
        config=Config(signature_version=UNSIGNED, read_timeout=read_timeout, retries={"max_attempts": 1}),
    )

    def add_bearer(request, **_):
        request.headers["Authorization"] = f"Bearer {bearer_token}"

    client.meta.events.register("before-send.bedrock-agentcore.*", add_bearer)
    return client


def invoke(
    harness_arn: str,
    prompt: str,
    qualifier: str | None = None,
    session_id: str | None = None,
    timeout: int = 180,
    actor_id: str | None = None,
    region: str | None = None,
    bearer_token: str | None = None,
) -> InvokeResult:
    """InvokeHarness with one user message and parse the stream. `qualifier` names an endpoint other than DEFAULT (the platform's
    Dev and Prod harnesses are both reached through DEFAULT, so it stays None).

    `bearer_token` invokes a JWT-protected harness as the token's user (module 6) instead of signing with the caller's IAM
    credentials. Raises HarnessInvokeError (with `budget_exceeded=True` when LiteLLM refused the call because the key or team cap
    was hit).
    """
    session_id = session_id or new_session_id()
    if len(session_id) < MIN_SESSION_ID_LENGTH:
        raise ValueError(
            f"runtimeSessionId must be at least {MIN_SESSION_ID_LENGTH} characters (got {len(session_id)}); use new_session_id()"
        )
    kwargs: dict[str, Any] = dict(
        harnessArn=harness_arn,
        runtimeSessionId=session_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
    )
    if qualifier:
        kwargs["qualifier"] = qualifier
    if actor_id:
        kwargs["actorId"] = actor_id
    started = time.monotonic()
    try:
        client = bearer_client(bearer_token, region, timeout) if bearer_token else data_client(region, timeout)
        response = client.invoke_harness(**kwargs)
        return parse_stream(response["stream"], started, session_id)
    except ClientError as exc:
        message = exc.response.get("Error", {}).get("Message") or str(exc)
        raise HarnessInvokeError(message, budget_exceeded=is_budget_exceeded(message), session_id=session_id) from exc


# ---- deletion (`workshop cleanup` and dry-run cleanup) -------------------------------------------------------------
ENDPOINT_DELETE_TIMEOUT = 900  # seconds to wait for a harness endpoint to disappear before DeleteHarness


def delete_harness(name: str, region: str | None = None) -> bool:
    """Delete a harness and its endpoints by name; True when something was deleted."""
    client = control_client(region)
    harness = find_harness(name, region)
    if not harness:
        return False
    for endpoint in _paginate(client, "list_harness_endpoints", "endpoints", harnessId=harness["harnessId"]):
        endpoint_name = endpoint.get("endpointName") or endpoint.get("name")
        if endpoint_name and endpoint_name != "DEFAULT":
            client.delete_harness_endpoint(harnessId=harness["harnessId"], endpointName=endpoint_name)
    # DeleteHarness is refused while a non-DEFAULT endpoint exists (accounts promoted before 2026-10-22 still have a `live`
    # endpoint); deleting a named endpoint usually takes seconds but was
    # seen taking more than five minutes (9 Oct 2026), so the wait is generous.
    _wait_gone(
        lambda: [
            e
            for e in _paginate(client, "list_harness_endpoints", "endpoints", harnessId=harness["harnessId"])
            if (e.get("endpointName") or e.get("name")) != "DEFAULT"
        ],
        "harness endpoints",
        timeout=ENDPOINT_DELETE_TIMEOUT,
    )
    client.delete_harness(harnessId=harness["harnessId"])
    return True


def delete_gateway(name: str, region: str | None = None) -> bool:
    """Delete a gateway and all its targets by name."""
    client = control_client(region)
    gateway = find_gateway(name, region)
    if not gateway:
        return False
    gateway_id = gateway["gatewayId"]
    for target in _paginate(client, "list_gateway_targets", "items", gatewayIdentifier=gateway_id):
        client.delete_gateway_target(gatewayIdentifier=gateway_id, targetId=target["targetId"])
    _wait_gone(
        lambda: _paginate(client, "list_gateway_targets", "items", gatewayIdentifier=gateway_id), "gateway targets"
    )
    client.delete_gateway(gatewayIdentifier=gateway_id)
    return True


def delete_memory(name: str, region: str | None = None) -> bool:
    memory = find_memory(name, region)
    if not memory:
        return False
    control_client(region).delete_memory(memoryId=memory["id"])
    return True


def delete_policy_engine(name: str, region: str | None = None) -> bool:
    """Delete a policy engine and its policies by name (detach it from gateways first)."""
    client = control_client(region)
    engine = find_policy_engine(name, region)
    if not engine:
        return False
    engine_id = engine["policyEngineId"]
    for policy in _paginate(client, "list_policies", "policies", policyEngineId=engine_id):
        client.delete_policy(policyEngineId=engine_id, policyId=policy["policyId"])
    _wait_gone(lambda: _paginate(client, "list_policies", "policies", policyEngineId=engine_id), "policies")
    client.delete_policy_engine(policyEngineId=engine_id)
    return True


def _wait_gone(lister: Callable[[], list], label: str, timeout: float = 120, every: float = 3) -> None:
    started = time.monotonic()
    while lister():
        if time.monotonic() - started > timeout:
            raise AgentCoreError(f"{label} still present after {timeout:.0f} s")
        time.sleep(every)
