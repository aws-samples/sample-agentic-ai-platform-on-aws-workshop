"""Agent blueprint: load, validate and render to an AgentCore Harness specification (build contract section 6).

A blueprint is a small YAML document (`apiVersion: platform/v1`, `kind: AgentBlueprint`). `load()` reads it and resolves
`systemPrompt.file` relative to the blueprint file; `render()` resolves the references against the project's registered
resources for one environment and returns the exact `CreateHarness` keyword arguments (shapes verified against the botocore
model and the harness spikes). `for_update()` turns that into `UpdateHarness` keyword arguments.

    bp = blueprint.load("scenario/blueprint/blueprint.yaml")
    spec = blueprint.render(bp, "dev", project, resources, cfg)   # key: the registered credential_provider resource
    control.create_harness(**spec, tags=blueprint.tags_for(bp, "dev"))

Unresolved references raise `BlueprintError(code="BLUEPRINT_UNRESOLVED")` naming the missing resource and the module that
creates it (gateway and knowledge-base target: module 4, memory: module 6, key: module 5, roles: project creation in module 3).
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import names

API_VERSION = "platform/v1"
KIND = "AgentBlueprint"
MEMORY_STRATEGIES = ("SUMMARIZATION", "USER_PREFERENCE", "SEMANTIC")
LIMIT_KEYS = ("maxIterations", "maxTokens", "timeoutSeconds", "maxResponseTokens")
DEFAULT_RESPONSE_TOKENS = 2048
BUDGET_DURATION_RE = re.compile(r"^\d+(s|m|h|d|mo)$")
PROJECT_TAG = "workshop-agent-platform"

MODULES = {  # where each referenced resource is created: the hint of a BLUEPRINT_UNRESOLVED error
    "gateway": "module 4 (the tool plane: `workshop apply 4`)",
    "gateway_target": "module 4 (the tool plane: `workshop apply 4`)",
    "memory": "module 6 (AgentOps: `workshop apply 6`)",
    "key": "module 5 (the model gateway: `workshop apply 5`) or the deploy job",
    "roles": "project creation in the UI (module 3, POST /projects)",
}


class BlueprintError(Exception):
    """Invalid blueprint (`BLUEPRINT_INVALID`) or a reference that no registered resource satisfies (`BLUEPRINT_UNRESOLVED`)."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass
class Blueprint:
    """Parsed blueprint. `spec["system_prompt"]` holds the resolved prompt text (the API stores it as `systemPrompt`)."""

    api_version: str
    kind: str
    metadata: dict
    spec: dict
    source_path: str | None = field(default=None, repr=False)

    @property
    def domain(self) -> str | None:
        return self.metadata.get("domain")

    @property
    def project_id(self) -> str:
        return self.metadata.get("project")

    @property
    def version(self) -> int | None:
        return self.metadata.get("version")

    @property
    def model_alias(self) -> str:
        return _ref_name(self.spec.get("model", {}).get("ref"), "model")

    @property
    def system_prompt(self) -> str:
        return self.spec.get("system_prompt") or ""

    @property
    def limits(self) -> dict:
        return dict(self.spec.get("limits") or {})

    @property
    def budget(self) -> dict:
        return dict(self.spec.get("budget") or {})

    @property
    def thresholds(self) -> dict[str, float]:
        return dict((self.spec.get("evaluation") or {}).get("thresholds") or {})

    @property
    def dataset(self) -> str | None:
        return (self.spec.get("evaluation") or {}).get("dataset")

    def evaluator_ids(self) -> list[str]:
        """Built-in evaluator ids for the dataset runner, derived from the threshold keys (`Builtin.` prefix added)."""
        return [k if k.startswith("Builtin.") else f"Builtin.{k}" for k in self.thresholds]

    def memory_strategies(self) -> list[str]:
        memory = self.spec.get("memory")
        return list((memory or {}).get("strategies") or []) if isinstance(memory, dict) else []

    def api_spec(self) -> dict:
        """The spec as stored by `POST /projects/{id}/blueprints` (without the resolved prompt text, which travels separately)."""
        return {k: v for k, v in self.spec.items() if k != "system_prompt"}

    def to_api_body(self, notes: str = "") -> dict:
        """Body for `POST /projects/{id}/blueprints`: {spec, systemPrompt, notes}."""
        return {"spec": self.api_spec(), "systemPrompt": self.system_prompt, "notes": notes}

    def validate(self) -> Blueprint:
        problems = _validate(self)
        if problems:
            raise BlueprintError(
                "BLUEPRINT_INVALID", "invalid blueprint: " + "; ".join(problems), {"problems": problems}
            )
        return self


# ---- loading -------------------------------------------------------------------------------------------------------
def load(path_or_dict: str | Path | dict, base_dir: str | Path | None = None) -> Blueprint:
    """Load a blueprint from a YAML/JSON file or an already parsed dict and resolve `systemPrompt.file`.

    `base_dir` is where relative prompt paths are resolved (defaults to the blueprint file's directory, or the working
    directory for dicts). The returned blueprint is validated.
    """
    source_path: str | None = None
    if isinstance(path_or_dict, (str, Path)):
        path = Path(path_or_dict)
        if not path.is_file():
            raise BlueprintError("BLUEPRINT_INVALID", f"blueprint file not found: {path}")
        document = yaml.safe_load(path.read_text()) or {}
        source_path = str(path)
        base = Path(base_dir) if base_dir else path.parent
    elif isinstance(path_or_dict, dict):
        document = path_or_dict
        base = Path(base_dir) if base_dir else Path.cwd()
    else:
        raise BlueprintError("BLUEPRINT_INVALID", f"load() expects a path or a dict, not {type(path_or_dict).__name__}")
    if not isinstance(document, dict):
        raise BlueprintError("BLUEPRINT_INVALID", "the blueprint document must be a mapping")

    spec = dict(document.get("spec") or {})
    spec["system_prompt"] = _resolve_prompt(spec, base)
    blueprint = Blueprint(
        api_version=str(document.get("apiVersion", "")),
        kind=str(document.get("kind", "")),
        metadata=dict(document.get("metadata") or {}),
        spec=spec,
        source_path=source_path,
    )
    return blueprint.validate()


def from_stored(
    spec: dict, system_prompt: str, project_id: str, version: int | str, domain: str | None = None
) -> Blueprint:
    """Rebuild a Blueprint from what the API stores for a version (`spec` + `systemPrompt`), for rendering in the backend.

    `domain` is optional (the stored version has none; it only ends up in the harness tags): pass `project["domainId"]`
    when it is at hand.
    """
    document = {
        "apiVersion": API_VERSION,
        "kind": KIND,
        "metadata": {"domain": domain, "project": project_id, "version": int(version)},
        "spec": {**spec, "system_prompt": system_prompt},
    }
    return load(document)


def _resolve_prompt(spec: dict, base: Path) -> str:
    if spec.get("system_prompt"):
        return str(spec["system_prompt"])
    prompt = spec.get("systemPrompt")
    if isinstance(prompt, str):
        return prompt
    if not isinstance(prompt, dict):
        return ""
    if prompt.get("text"):
        return str(prompt["text"])
    if prompt.get("file"):
        file_path = Path(prompt["file"])
        if not file_path.is_absolute():
            file_path = base / file_path
        if not file_path.is_file():
            raise BlueprintError(
                "BLUEPRINT_INVALID", f"systemPrompt.file {prompt['file']!r} not found (looked at {file_path})"
            )
        return file_path.read_text()
    return ""


def _ref_name(ref: Any, kind: str) -> str:
    """`model/claude-sonnet-5` -> `claude-sonnet-5` (raises on the wrong prefix)."""
    if not isinstance(ref, str) or not ref.startswith(kind + "/") or len(ref) <= len(kind) + 1:
        raise BlueprintError("BLUEPRINT_INVALID", f"expected a reference of the form {kind}/<name>, got {ref!r}")
    return ref[len(kind) + 1 :]


def _validate(bp: Blueprint) -> list[str]:
    problems: list[str] = []
    if bp.api_version != API_VERSION:
        problems.append(f"apiVersion must be {API_VERSION!r} (got {bp.api_version!r})")
    if bp.kind != KIND:
        problems.append(f"kind must be {KIND!r} (got {bp.kind!r})")
    try:
        names.validate_project_id(str(bp.metadata.get("project") or ""))
    except ValueError as exc:
        problems.append(f"metadata.project: {exc}")
    version = bp.metadata.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        problems.append(f"metadata.version must be a positive integer (got {version!r})")
    # metadata.domain is optional: stored versions carry none, and it is only used for the Domain tag

    spec = bp.spec
    try:
        _ref_name((spec.get("model") or {}).get("ref"), "model")
    except BlueprintError as exc:
        problems.append(f"spec.model.ref: {exc.message}")
    if not bp.system_prompt.strip():
        problems.append("spec.systemPrompt must resolve to a non-empty text ({file: ...} or {text: ...})")

    tools = spec.get("tools") or []
    if not isinstance(tools, list):
        problems.append("spec.tools must be a list of {ref: tool/<name>}")
    else:
        for i, tool in enumerate(tools):
            if not isinstance(tool, dict):
                problems.append(f"spec.tools[{i}] must be a mapping with ref")
                continue
            try:
                _ref_name(tool.get("ref"), "tool")
            except BlueprintError as exc:
                problems.append(f"spec.tools[{i}].ref: {exc.message}")
            allow = tool.get("allow")
            if allow is not None and (not isinstance(allow, list) or not all(isinstance(a, str) and a for a in allow)):
                problems.append(
                    f"spec.tools[{i}].allow must be a list of gateway tool names such as hrtools___get_leave_balance"
                )

    knowledge = spec.get("knowledge") or []
    if not isinstance(knowledge, list):
        problems.append("spec.knowledge must be a list of {ref: kb/<name>}")
    else:
        for i, kb in enumerate(knowledge):
            try:
                _ref_name((kb or {}).get("ref") if isinstance(kb, dict) else None, "kb")
            except BlueprintError as exc:
                problems.append(f"spec.knowledge[{i}].ref: {exc.message}")

    memory = spec.get("memory")
    if memory is not None:
        strategies = memory.get("strategies") if isinstance(memory, dict) else None
        if not isinstance(strategies, list) or not strategies:
            problems.append("spec.memory.strategies must be a non-empty list")
        else:
            bad = [s for s in strategies if s not in MEMORY_STRATEGIES]
            if bad:
                problems.append(f"spec.memory.strategies has unknown entries {bad}; allowed: {list(MEMORY_STRATEGIES)}")

    limits = spec.get("limits") or {}
    if not isinstance(limits, dict):
        problems.append("spec.limits must be a mapping")
    else:
        for key, value in limits.items():
            if key not in LIMIT_KEYS:
                problems.append(f"spec.limits.{key} is not a known limit; allowed: {list(LIMIT_KEYS)}")
            elif not isinstance(value, int) or isinstance(value, bool) or value < 1:
                problems.append(f"spec.limits.{key} must be a positive integer (got {value!r})")

    budget = spec.get("budget") or {}
    if budget:
        cap = budget.get("perModelCap")
        if not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0:
            problems.append(f"spec.budget.perModelCap must be a positive number in USD (got {cap!r})")
        period = budget.get("resetPeriod")
        if period is not None and not BUDGET_DURATION_RE.match(str(period)):
            problems.append(f"spec.budget.resetPeriod must look like 30s, 30m, 24h, 1d or 1mo (got {period!r})")

    evaluation = spec.get("evaluation") or {}
    if evaluation:
        if not isinstance(evaluation.get("dataset"), str) or not evaluation.get("dataset"):
            problems.append("spec.evaluation.dataset must be a path such as evaluation/hr-assistant.json")
        thresholds = evaluation.get("thresholds") or {}
        if not isinstance(thresholds, dict) or not thresholds:
            problems.append("spec.evaluation.thresholds must map evaluator names to numbers in [0, 1]")
        else:
            for key, value in thresholds.items():
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 1:
                    problems.append(f"spec.evaluation.thresholds.{key} must be a number in [0, 1] (got {value!r})")
    return problems


# ---- rendering -----------------------------------------------------------------------------------------------------
def find_resource(resources: list[dict], env: str, type_: str, name: str | None = None, **attributes) -> dict | None:
    """First registered resource (from GET /projects/{id}/resources) matching env, type, optional name and attributes."""
    for resource in resources or []:
        if resource.get("env") != env or resource.get("type") != type_:
            continue
        if name is not None and resource.get("name") != name:
            continue
        attrs = resource.get("attributes") or {}
        if all(attrs.get(k) == v for k, v in attributes.items()):
            return resource
    return None


def _unresolved(what: str, env: str, module_key: str, project_id: str, extra: str = "") -> BlueprintError:
    message = f"{what} for project {project_id!r} environment {env!r} is not registered; create it in {MODULES[module_key]}"
    if extra:
        message += f" ({extra})"
    return BlueprintError("BLUEPRINT_UNRESOLVED", message, {"missing": module_key, "env": env, "project": project_id})


def render(
    blueprint: Blueprint,
    env: str,
    project: dict,
    resources: list[dict],
    cfg,
    credential_provider_arn: str | None = None,
    defaults: dict | None = None,
) -> dict:
    """Render `CreateHarness` keyword arguments for one environment (contract section 6, shapes verified 8 Oct 2026).

    `project` is the API project item (section 3), `resources` the list from GET /projects/{id}/resources, `cfg` a
    PlatformConfig (model gateway URL and approved models), `credential_provider_arn` the AgentCore Identity provider that
    holds the agent's LiteLLM key for this environment and model; when omitted it is taken from the registered
    `credential_provider` resource whose `attributes.model` is the blueprint's model alias (module 5 registers it).
    `defaults` may carry platform default limits (settings.defaultLimits) used when the blueprint does not set them.
    When `cfg.harness_network()` is present (private landing zone) the harness gets the VPC `environment` block.
    Tags are not included: see `tags_for`.
    """
    names.validate_env(env)
    project_id = blueprint.project_id
    project_names = names.ProjectNames(project_id, env)
    spec = blueprint.spec
    alias = blueprint.model_alias

    approved = _approved_aliases(cfg)
    if approved is not None and alias not in approved:
        raise BlueprintError(
            "BLUEPRINT_UNRESOLVED",
            f"model alias {alias!r} is not an approved model (approved: {approved}); pick one from GET /governance",
            {"missing": "model", "alias": alias},
        )
    allowed = (project.get("litellm") or {}).get("models")
    if allowed and alias not in allowed:
        raise BlueprintError(
            "BLUEPRINT_UNRESOLVED",
            f"model alias {alias!r} is not enabled for project {project_id!r} (project models: {allowed}); "
            "ask the platform admin to add it (PATCH /projects/{id} models)",
            {"missing": "model", "alias": alias},
        )
    if not credential_provider_arn:
        provider = find_resource(resources, env, "credential_provider", model=alias)
        credential_provider_arn = (provider or {}).get("arn")
    if not credential_provider_arn:
        raise _unresolved(
            f"the LiteLLM key for model {alias!r} (credential provider {project_names.credential_provider(alias)!r})",
            env,
            "key",
            project_id,
        )

    role_arn = ((project.get("roles") or {}).get(env) or {}).get("agentRoleArn")
    if not role_arn:
        raise _unresolved(f"the agent execution role ({project_names.agent_role})", env, "roles", project_id)

    limits = {**(defaults or {}), **blueprint.limits}
    model_cfg: dict[str, Any] = {
        "modelId": f"openai/{alias}",
        "apiBase": cfg.model_gateway_url + "/v1",
        "apiKeyArn": credential_provider_arn,
        "maxTokens": int(limits.get("maxResponseTokens") or DEFAULT_RESPONSE_TOKENS),
    }
    model_spec = spec.get("model") or {}
    for key in ("temperature", "topP"):
        if key in model_spec:
            model_cfg[key] = model_spec[key]

    rendered: dict[str, Any] = {
        "harnessName": project_names.harness,
        "executionRoleArn": role_arn,
        "model": {"liteLlmModelConfig": model_cfg},
        "systemPrompt": [{"text": blueprint.system_prompt}],
    }

    tools = spec.get("tools") or []
    if tools:
        gateway = find_resource(resources, env, "gateway")
        if not gateway or not gateway.get("arn"):
            raise _unresolved(f"the gateway ({project_names.gateway})", env, "gateway", project_id)
        rendered["tools"] = [
            {
                "type": "agentcore_gateway",
                "name": names.GATEWAY_TOOL_NAME,
                "config": {"agentCoreGateway": {"gatewayArn": gateway["arn"], "outboundAuth": {"awsIam": {}}}},
            }
        ]
        explicit = [tool_name for tool in tools for tool_name in (tool.get("allow") or [])]
        rendered["allowedTools"] = (
            [f"@{names.GATEWAY_TOOL_NAME}/{t}" for t in explicit] if explicit else [f"@{names.GATEWAY_TOOL_NAME}"]
        )

    if spec.get("knowledge"):
        kb_target = find_resource(resources, env, "gateway_target", kind="knowledge_base")
        if not kb_target:
            raise _unresolved(
                "the knowledge-base connector target",
                env,
                "gateway_target",
                project_id,
                "register it with PUT /projects/{id}/resources as type gateway_target with attributes.kind = knowledge_base",
            )

    if spec.get("memory"):
        memory = find_resource(resources, env, "memory")
        if not memory or not memory.get("arn"):
            raise _unresolved(f"the memory ({project_names.memory})", env, "memory", project_id)
        memory_cfg: dict[str, Any] = {"arn": memory["arn"]}
        for key in ("actorId", "messagesCount", "retrievalConfig"):
            if key in spec["memory"]:
                memory_cfg[key] = spec["memory"][key]
        rendered["memory"] = {"agentCoreMemoryConfiguration": memory_cfg}
    else:
        rendered["memory"] = {"disabled": {}}

    if limits.get("maxIterations"):
        rendered["maxIterations"] = int(limits["maxIterations"])
    if limits.get("maxTokens"):
        rendered["maxTokens"] = int(limits["maxTokens"])
    if limits.get("timeoutSeconds"):
        rendered["timeoutSeconds"] = int(limits["timeoutSeconds"])
    if spec.get("hooks"):
        rendered["hooks"] = spec["hooks"]
    network = _harness_network(cfg)
    if network:
        rendered["environment"] = {
            "agentCoreRuntimeEnvironment": {
                "networkConfiguration": {
                    "networkMode": "VPC",
                    "networkModeConfig": {
                        "subnets": list(network["subnets"]),
                        "securityGroups": list(network["securityGroups"]),
                    },
                }
            }
        }
    return rendered


def _harness_network(cfg) -> dict | None:
    """`cfg.harness_network()` when the Network stack published the harness subnets and security group, else None.

    On the private landing zone (tier 3) the model gateway's load balancer is internal, so a harness on AgentCore's managed
    network could not reach it: the harness runs in VPC mode, its ENIs in the `app` subnets next to the proxy. The managed
    harness image is then pulled through the ECR endpoints and the S3 gateway endpoint of that VPC. `requireServiceS3Endpoint`
    is deliberately not sent: CreateHarness rejects it ("can only be set via UpdateHarness", seen live on 16 Oct 2026) and the
    route table's S3 gateway endpoint already carries the layer downloads. A public landing zone publishes no network keys and
    the harness stays on the managed (PUBLIC) network.
    """
    getter = getattr(cfg, "harness_network", None)
    return getter() if callable(getter) else None


def _approved_aliases(cfg) -> list[str] | None:
    """Approved aliases from the config, or None when the models list is not published (skip the check)."""
    if cfg is None or cfg.get("model-gateway/models") is None:
        return None
    return cfg.model_aliases


def for_update(spec: dict) -> dict:
    """`UpdateHarness` keyword arguments from a rendered spec: drops harnessName and tags, wraps memory in optionalValue.

    UpdateHarness keeps the previous version's `tools`, `allowedTools` and `hooks` when a member is omitted ("if not
    specified, the existing value is retained"), so a blueprint that dropped them would silently keep them: the three lists
    are sent explicitly, empty when the blueprint has none. `environment` (the VPC network block) passes through unchanged,
    so an update keeps the harness in the VPC.
    """
    update = {k: v for k, v in spec.items() if k not in ("harnessName", "tags", "harnessId", "clientToken")}
    if "memory" in update and "optionalValue" not in update["memory"]:
        update["memory"] = {"optionalValue": update["memory"]}
    if "authorizerConfiguration" in update and "optionalValue" not in update["authorizerConfiguration"]:
        update["authorizerConfiguration"] = {
            "optionalValue": update["authorizerConfiguration"]
        }  # same wrapper as memory
    for key in ("tools", "allowedTools", "hooks"):
        update.setdefault(key, [])
    return update


def tags_for(blueprint: Blueprint, env: str, managed_by: str = "platform") -> dict[str, str]:
    """Tags for CreateHarness (tags are rejected by UpdateHarness, so keep them out of the rendered spec)."""
    tags = {
        "Project": PROJECT_TAG,
        "ManagedBy": managed_by,
        "ProjectId": blueprint.project_id,
        "Environment": env,
    }
    if blueprint.domain:
        tags["Domain"] = str(blueprint.domain)
    if blueprint.version is not None:
        tags["BlueprintVersion"] = str(blueprint.version)
    return tags


# ---- the on-behalf-of variant of a rendered harness (module 6) ------------------------------------------------------------------
def with_jwt_authorizer(spec: dict, discovery_url: str, allowed_audience: list[str]) -> dict:
    """A copy of a rendered spec whose callers must present a bearer JWT from `discovery_url` with one of `allowed_audience`.

    With a JWT authorizer the harness receives the end user's token, which is what lets AgentCore Identity exchange it on the
    user's behalf for the tools (harness-security.html: SigV4 callers get no per-user identity downstream).
    """
    patched = copy.deepcopy(spec)
    patched["authorizerConfiguration"] = {
        "customJWTAuthorizer": {"discoveryUrl": discovery_url, "allowedAudience": list(allowed_audience)}
    }
    return patched


def with_oauth_gateway(
    spec: dict,
    gateway_arn: str,
    provider_arn: str,
    audience: str,
    scopes: list[str] | None = None,
    custom_parameters: dict | None = None,
    grant_type: str = "TOKEN_EXCHANGE",
) -> dict:
    """A copy of a rendered spec whose gateway tool points at `gateway_arn` and authenticates with OAuth instead of SigV4.

    `TOKEN_EXCHANGE` is on-behalf-of: before each tool call the harness asks AgentCore Identity to exchange the inbound user token
    (through the OAuth2 credential provider `provider_arn`) for a token whose audience is the gateway. `custom_parameters` carry
    what the IdP needs; Keycloak wants `subject_token_type` = access token and the `audience` it should mint for.
    """
    patched = copy.deepcopy(spec)
    oauth: dict[str, Any] = {
        "providerArn": provider_arn,
        "scopes": list(scopes or []),
        "grantType": grant_type,
        "customParameters": {
            "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "audience": audience,
            **(custom_parameters or {}),
        },
    }
    tool = {
        "type": "agentcore_gateway",
        "name": names.GATEWAY_TOOL_NAME,
        "config": {"agentCoreGateway": {"gatewayArn": gateway_arn, "outboundAuth": {"oauth": oauth}}},
    }
    others = [t for t in patched.get("tools") or [] if t.get("type") != "agentcore_gateway"]
    patched["tools"] = [tool, *others]
    # the OBO gateway publishes other tool names (hrapi___*), so explicit `@gateway/<tool>` allow entries of the Dev render
    # would name tools that do not exist there: allow the whole gateway
    patched["allowedTools"] = [f"@{names.GATEWAY_TOOL_NAME}"] + [
        t for t in patched.get("allowedTools") or [] if not t.startswith(f"@{names.GATEWAY_TOOL_NAME}")
    ]
    return patched
