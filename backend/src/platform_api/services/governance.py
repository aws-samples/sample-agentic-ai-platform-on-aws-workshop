"""GET /governance, the gateway policy-mode switch, the model gateway sign-in and the project form's pick lists (contract section 4)."""

from __future__ import annotations

import logging

from botocore.exceptions import ClientError
from platform_sdk.litellm import LiteLLMError

from platform_sdk import agentcore

from .. import db, errors, settings
from . import clients

logger = logging.getLogger(__name__)
GATEWAY_MODES = ("LOG_ONLY", "ENFORCE")


def _guardrail_name_resolver(baseline: dict | None):
    """Guardrail name by identifier (id or ARN): seeded with the baseline guardrail, one GetGuardrail per other identifier."""
    names: dict[str, str | None] = {}
    if baseline and baseline.get("name"):
        for key in (baseline.get("id"), baseline.get("arn")):
            if key:
                names[key] = baseline["name"]

    def resolve(identifier: str | None, version: str | None) -> str | None:
        if not identifier:
            return None
        if identifier not in names:
            try:
                detail = clients.bedrock().get_guardrail(
                    guardrailIdentifier=identifier, guardrailVersion=str(version or "DRAFT")
                )
                names[identifier] = detail.get("name")
            except Exception as exc:  # noqa: BLE001 - the table still renders, with the id instead of the name
                logger.warning("GetGuardrail(%s) failed: %s", identifier, exc)
                names[identifier] = None
        return names[identifier]

    return resolve


def models(baseline: dict | None = None) -> list[dict]:
    """Approved models: LiteLLM /model/info merged with the SSM list; the SSM list alone when the proxy does not answer.

    Each model's `guardrail` carries the guardrail's `name` next to its id and version (resolved through `baseline`, the
    result of `guardrail()`, or GetGuardrail) so the UI can show a readable label."""
    cfg = clients.cfg()
    if cfg.get("model-gateway/models") is None:  # the landing zone (module 1) is not deployed yet: nothing to approve
        return []
    approved = {m["alias"]: m for m in cfg.approved_models}
    guardrail_name = _guardrail_name_resolver(baseline)
    try:
        rows = clients.litellm_admin().model_info()
    except (LiteLLMError, OSError, ValueError) as exc:
        logger.warning("LiteLLM /model/info unavailable (%s); falling back to the SSM models list", exc)
        return [{**m, "source": "ssm"} for m in approved.values()]
    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        alias = row.get("model_name")
        params = row.get("litellm_params") or {}
        info = row.get("model_info") or {}
        guardrail = params.get("guardrailConfig") or {}
        base = approved.get(alias, {})
        seen.add(alias)
        out.append(
            {
                "alias": alias,
                "modelId": base.get("modelId") or params.get("model"),
                "litellmModel": params.get("model"),
                "provider": base.get("provider", "bedrock"),
                "tier": base.get("tier"),
                "description": base.get("description"),
                "inputCostPerToken": params.get("input_cost_per_token", info.get("input_cost_per_token")),
                "outputCostPerToken": params.get("output_cost_per_token", info.get("output_cost_per_token")),
                "region": params.get("aws_region_name"),
                "guardrail": (
                    {
                        "id": guardrail.get("guardrailIdentifier"),
                        "version": guardrail.get("guardrailVersion"),
                        "name": guardrail_name(guardrail.get("guardrailIdentifier"), guardrail.get("guardrailVersion")),
                    }
                    if guardrail
                    else None
                ),
                "source": "litellm",
            }
        )
    for alias, base in approved.items():
        if alias not in seen:
            out.append({**base, "source": "ssm"})
    return out


def guardrail() -> dict:
    cfg = clients.cfg()
    gid, version = cfg.get("guardrail/id"), cfg.get("guardrail/version")
    result: dict = {"id": gid, "version": version, "arn": cfg.get("guardrail/arn"), "name": None, "summary": {}}
    if not gid:
        return result
    try:
        detail = clients.bedrock().get_guardrail(guardrailIdentifier=gid, guardrailVersion=str(version or "DRAFT"))
    except Exception as exc:  # noqa: BLE001 - the screen still renders
        logger.warning("GetGuardrail failed: %s", exc)
        result["summary"] = {"error": errors.message_of(exc)}
        return result
    result["name"] = detail.get("name")
    result["status"] = detail.get("status")
    result["description"] = detail.get("description")
    result["summary"] = {
        "topics": [t.get("name") for t in (detail.get("topicPolicy") or {}).get("topics", [])],
        "contentFilters": [
            {"type": f.get("type"), "input": f.get("inputStrength"), "output": f.get("outputStrength")}
            for f in (detail.get("contentPolicy") or {}).get("filters", [])
        ],
        "piiEntities": [
            {"type": p.get("type"), "action": p.get("action")}
            for p in (detail.get("sensitiveInformationPolicy") or {}).get("piiEntities", [])
        ],
        "blockedInputMessaging": detail.get("blockedInputMessaging"),
        "blockedOutputsMessaging": detail.get("blockedOutputsMessaging"),
    }
    return result


def gateway_resources() -> list[dict]:
    """Every registered `gateway` resource across all projects (contract section 3)."""
    rows: list[dict] = []
    for project in db.query_gsi1("PROJECT"):
        for item in db.query(project["PK"], db.resource_prefix()):
            if item.get("type") == "gateway":
                rows.append(db.strip_keys(item))
    return rows


def _gateway_identifier(resource: dict) -> str | None:
    if resource.get("id"):
        return resource["id"]
    arn = resource.get("arn") or ""
    return arn.rsplit("/", 1)[-1] if "/" in arn else None


def gateways() -> list[dict]:
    """Registered gateways with their live policy-engine mode (GetGateway)."""
    client = clients.agentcore_control()
    out: list[dict] = []
    for resource in gateway_resources():
        gateway_id = _gateway_identifier(resource)
        row = {
            "projectId": resource.get("projectId"),
            "env": resource.get("env"),
            "gatewayId": gateway_id,
            "name": resource.get("name"),
            "arn": resource.get("arn"),
            "url": resource.get("url"),
            "mode": None,
            "policyEngineArn": None,
            "status": None,
        }
        if gateway_id:
            try:
                live = client.get_gateway(gatewayIdentifier=gateway_id)
                engine = live.get("policyEngineConfiguration") or {}
                row.update(
                    name=live.get("name", row["name"]),
                    arn=live.get("gatewayArn", row["arn"]),
                    url=live.get("gatewayUrl", row["url"]),
                    mode=engine.get("mode"),
                    policyEngineArn=engine.get("arn"),
                    status=live.get("status"),
                )
            except ClientError as exc:  # a stale or malformed registration must not break the screen
                code = exc.response.get("Error", {}).get("Code", "")
                # a malformed id (wrong suffix length) is rejected by IAM as AccessDenied, not as ResourceNotFound (seen 8 Oct 2026)
                row["status"] = {"ResourceNotFoundException": "NOT_FOUND"}.get(code, "INVALID")
                row["error"] = errors.message_of(exc)
            except Exception as exc:  # noqa: BLE001
                row["status"] = "ERROR"
                row["error"] = errors.message_of(exc)
        out.append(row)
    return out


def summary() -> dict:
    current = settings.load()
    baseline = guardrail()
    return {
        "models": models(baseline),
        "guardrail": baseline,
        "policyEngine": {"id": current.get("policyEngineId"), "arn": current.get("policyEngineArn")},
        "gateways": gateways(),
        "registry": {"id": current.get("registryId"), "arn": current.get("registryArn")},
        "modelGateway": model_gateway(),
    }


def model_gateway() -> dict:
    """Where the model gateway is (SSM at run time), or `deployed: False` while the landing zone (module 1) does not exist.

    `private` and `reachableFrom` tell the Governance screen that the proxy's load balancer is internal (tier 3 landing zone):
    the admin UI cannot be opened from a browser, and every admin call of this backend travels through the relay function named
    here. A public landing zone (no `model-gateway/private` in SSM) reports `private: False`."""
    cfg = clients.cfg()
    url = (cfg.get("model-gateway/url") or "").rstrip("/")
    if not url:
        return {
            "deployed": False,
            "url": None,
            "openaiBase": None,
            "adminUiUrl": None,
            "private": False,
            "reachableFrom": None,
        }
    private = cfg.model_gateway_private
    relay = cfg.model_gateway_relay_function_name
    if private:
        reachable_from = f"inside the VPC (relay function {relay})" if relay else "inside the VPC"
    else:
        reachable_from = "anywhere (internet-facing load balancer)"
    return {
        "deployed": True,
        "url": url,
        "openaiBase": url + "/v1",
        "adminUiUrl": url + "/ui",
        "private": private,
        "reachableFrom": reachable_from,
    }


def model_gateway_credentials() -> dict:
    """The LiteLLM admin UI sign-in (admin route): user `admin`, password = the master key the backend already holds."""
    cfg = clients.cfg()
    if cfg.get("model-gateway/url") is None:
        raise errors.not_found(
            "the model gateway is not deployed yet: module 1 of the learning path creates it (workshop apply 1)"
        )
    return {
        "url": cfg.model_gateway_url,
        "adminUiUrl": cfg.model_gateway_url + "/ui",
        "username": "admin",
        "password": clients.master_key(),
        "secretArn": cfg.model_gateway_master_key_secret_arn,
    }


# ---- pick lists for the project form (allowed tool ARNs, knowledge base ids) ---------------------------------------------
MAX_PAGES = 50  # defensive: no AgentCore / Bedrock list in a workshop account comes anywhere near this


def _pages(client, operation: str, result_key: str, **kwargs) -> list[dict]:
    """Every item of a nextToken-paginated AgentCore / Bedrock list operation (bounded; a repeated token stops the walk)."""
    items: list[dict] = []
    token = None
    for _ in range(MAX_PAGES):
        response = getattr(client, operation)(**kwargs, **({"nextToken": token} if token else {}))
        items.extend(response.get(result_key) or [])
        next_token = response.get("nextToken")
        if not next_token or next_token == token:
            return items
        token = next_token
    logger.warning("%s: stopped after %s pages", operation, MAX_PAGES)
    return items


def _is_lambda_arn(value) -> bool:
    """A Lambda function ARN in any partition (`arn:<partition>:lambda:<region>:<account>:function:<name>[:<qualifier>]`)."""
    parts = value.split(":") if isinstance(value, str) else []
    return len(parts) >= 7 and parts[0] == "arn" and parts[2] == "lambda" and parts[5] == "function"


def _not_found(exc: Exception) -> bool:
    return isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException"


def lambda_function_name(arn: str) -> str:
    """`arn:aws:lambda:<region>:<account>:function:<name>[:<qualifier>]` -> `<name>`."""
    tail = arn.split(":function:", 1)[1] if ":function:" in arn else arn.rsplit(":", 1)[-1]
    return tail.split(":", 1)[0]


def _registered_targets() -> list[dict]:
    """Every registered `gateway_target` / `tool` / `knowledge_base` resource across all projects."""
    rows: list[dict] = []
    for project in db.query_gsi1("PROJECT"):
        for item in db.query(project["PK"], db.resource_prefix()):
            if item.get("type") in ("gateway_target", "tool", "knowledge_base"):
                rows.append(db.strip_keys(item))
    return rows


def tool_options(registered: list[dict] | None = None) -> tuple[list[dict], list[str]]:
    """Lambda tool candidates: the scenario tools (SSM), the Lambda targets of every AgentCore gateway, registered targets."""
    cfg = clients.cfg()
    warnings: list[str] = []
    options: dict[str, dict] = {}

    def add(arn: str | None, source: str, description: str | None = None) -> None:
        if not _is_lambda_arn(arn):
            return
        row = options.setdefault(
            arn, {"arn": arn, "name": lambda_function_name(arn), "description": description, "sources": []}
        )
        if description and not row["description"]:
            row["description"] = description
        if source not in row["sources"]:
            row["sources"].append(source)

    add(cfg.get("scenario/tools/hr-lambda-arn"), "scenario", "AnyCompany HR tools (employee profile, leave, holidays)")
    add(cfg.get("scenario/tools/finance-lambda-arn"), "scenario", "AnyCompany Finance tools (invoice status)")
    for item in registered if registered is not None else _registered_targets():
        if item.get("type") == "knowledge_base":
            continue
        attributes = item.get("attributes") or {}
        arn = attributes.get("lambdaArn") or item.get("arn")
        add(arn, f"registered: {item.get('projectId')}/{item.get('env')} target {item.get('name')}")
    # The live gateways: one GetGatewayTarget per Lambda target (ListGatewayTargets carries no configuration). A gateway or
    # target that disappears between list and get is skipped silently; any other failure is reported per gateway / target so
    # the rest of the pick list is still complete.
    try:
        client = clients.agentcore_control()
        gateways = _pages(client, "list_gateways", "items")
    except Exception as exc:  # noqa: BLE001 - the pick list still offers the scenario and registered tools
        logger.warning("ListGateways failed: %s", exc)
        warnings.append(f"AgentCore gateway targets could not be listed ({errors.message_of(exc)})")
        gateways = []
    for gateway in gateways:
        gateway_id, gateway_name = gateway.get("gatewayId"), gateway.get("name") or gateway.get("gatewayId")
        try:
            targets = _pages(client, "list_gateway_targets", "items", gatewayIdentifier=gateway_id)
        except Exception as exc:  # noqa: BLE001
            if not _not_found(exc):
                logger.warning("ListGatewayTargets failed for %s: %s", gateway_name, exc)
                warnings.append(f"gateway {gateway_name}: targets could not be listed ({errors.message_of(exc)})")
            continue
        for target in targets:
            target_id = target.get("targetId")
            if not target_id or target.get("targetType") not in (None, "LAMBDA"):
                continue  # connector, MCP server and other target types carry no Lambda ARN
            try:
                detail = client.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
            except Exception as exc:  # noqa: BLE001
                if not _not_found(exc):
                    logger.warning("GetGatewayTarget failed for %s/%s: %s", gateway_name, target_id, exc)
                    warnings.append(
                        f"gateway {gateway_name}: target {target.get('name') or target_id} could not be read "
                        f"({errors.message_of(exc)})"
                    )
                continue
            lambda_cfg = ((detail.get("targetConfiguration") or {}).get("mcp") or {}).get("lambda") or {}
            add(lambda_cfg.get("lambdaArn"), f"gateway {gateway_name} target {detail.get('name') or target_id}")
    return sorted(options.values(), key=lambda row: (row["name"], row["arn"])), warnings


def knowledge_base_options(registered: list[dict] | None = None) -> tuple[list[dict], list[str]]:
    """Knowledge base candidates: Bedrock ListKnowledgeBases, the scenario knowledge base (SSM), registered targets."""
    cfg = clients.cfg()
    warnings: list[str] = []
    options: dict[str, dict] = {}

    def add(
        kb_id: str | None, source: str, name: str | None = None, description: str | None = None, status=None
    ) -> None:
        if not isinstance(kb_id, str) or not kb_id.strip():
            return
        kb_id = kb_id.rsplit("/", 1)[-1] if kb_id.startswith("arn:") else kb_id.strip()
        row = options.setdefault(
            kb_id,
            {
                "id": kb_id,
                "arn": f"arn:aws:bedrock:{cfg.region}:{cfg.account_id}:knowledge-base/{kb_id}",
                "name": None,
                "description": None,
                "status": None,
                "sources": [],
            },
        )
        row["name"] = row["name"] or name
        row["description"] = row["description"] or description
        row["status"] = row["status"] or status
        if source not in row["sources"]:
            row["sources"].append(source)

    try:
        for kb in _pages(clients.bedrock_agent(), "list_knowledge_bases", "knowledgeBaseSummaries"):
            add(kb.get("knowledgeBaseId"), "bedrock", kb.get("name"), kb.get("description"), kb.get("status"))
    except Exception as exc:  # noqa: BLE001 - without bedrock:ListKnowledgeBases the scenario knowledge base is still offered
        logger.warning("ListKnowledgeBases failed: %s", exc)
        warnings.append(f"Bedrock knowledge bases could not be listed ({errors.message_of(exc)})")
    add(
        cfg.get("scenario/kb-id"),
        "scenario",
        "HR policy knowledge base (scenario)",
        "AnyCompany HR and Finance policy documents",
    )
    for item in registered if registered is not None else _registered_targets():
        attributes = item.get("attributes") or {}
        kb_id = attributes.get("knowledgeBaseId") or (item.get("id") if item.get("type") == "knowledge_base" else None)
        add(kb_id, f"registered: {item.get('projectId')}/{item.get('env')} target {item.get('name')}")
    # a registered knowledge base that ListKnowledgeBases did not return (or could not list): one GetKnowledgeBase for its name
    for row in options.values():
        if row["name"]:
            continue
        try:
            detail = clients.bedrock_agent().get_knowledge_base(knowledgeBaseId=row["id"]).get("knowledgeBase") or {}
            row["name"], row["description"], row["status"] = (
                detail.get("name"),
                detail.get("description"),
                detail.get("status"),
            )
        except Exception as exc:  # noqa: BLE001 - the id is still a valid label
            logger.info("GetKnowledgeBase(%s) failed: %s", row["id"], exc)
        row["name"] = row["name"] or row["id"]
    return sorted(options.values(), key=lambda row: (row["name"], row["id"])), warnings


def project_options() -> dict:
    """GET /governance/project-options: what the project form offers for allowedToolArns and knowledgeBaseIds."""
    registered = _registered_targets()
    tools, tool_warnings = tool_options(registered)
    knowledge_bases, kb_warnings = knowledge_base_options(registered)
    return {"tools": tools, "knowledgeBases": knowledge_bases, "warnings": tool_warnings + kb_warnings}


def set_gateway_mode(gateway_id: str, mode: str) -> dict:
    """UpdateGateway with the policy engine in `mode` (LOG_ONLY | ENFORCE); waits for READY (about 6 s)."""
    if mode not in GATEWAY_MODES:
        raise errors.validation(f"mode must be one of {list(GATEWAY_MODES)}")
    client = clients.agentcore_control()
    gateway = client.get_gateway(gatewayIdentifier=gateway_id)  # ResourceNotFoundException -> 404 by the error mapping
    engine_arn = (gateway.get("policyEngineConfiguration") or {}).get("arn") or settings.load().get("policyEngineArn")
    if not engine_arn:
        raise errors.conflict(
            "the gateway has no policy engine attached and settings has no policyEngineArn: create the policy engine "
            'agent_platform_policy_engine (module 4) and store it with PUT /settings {"policyEngineId", "policyEngineArn"}',
            code="POLICY_ENGINE_NOT_CONFIGURED",
        )
    updated = agentcore.set_gateway_policy_mode(gateway_id, engine_arn, mode, region=clients.cfg().region)
    engine = updated.get("policyEngineConfiguration") or {}
    return {
        "gatewayId": updated.get("gatewayId", gateway_id),
        "name": updated.get("name"),
        "mode": engine.get("mode"),
        "policyEngineArn": engine.get("arn"),
        "status": updated.get("status"),
    }
