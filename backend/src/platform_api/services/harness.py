"""Harness lifecycle shared by the deploy and promote jobs (build contract section 4, backend-design flows).

    ensure_key       the KEY item for (env, blueprint model) exists: created through LiteLLM + AgentCore Identity when missing
    render           platform_sdk.blueprint.render with the registered resources and the key's credential provider
    deploy           Dev: create_or_update_harness (waits READY) + resources `harness` and `harness_endpoint` DEFAULT
    promote          Prod: the same (the DEFAULT endpoint follows the new version) plus the online evaluation config

Everything AgentCore-related goes through `platform_sdk.agentcore` (verified live in the dry run); nothing is re-implemented here.
"""

from __future__ import annotations

import logging
import time

from botocore.exceptions import ClientError

from platform_sdk import agentcore, blueprint, names

from .. import db, settings
from ..handlers import resources as resources_store
from . import clients
from . import litellm_provisioning as litellm

logger = logging.getLogger(__name__)
ONLINE_EVALUATORS = ("Builtin.Helpfulness", "Builtin.Harmfulness", "Builtin.ToolSelectionAccuracy")
ONLINE_SAMPLING_PERCENTAGE = 100.0
ONLINE_SESSION_TIMEOUT_MINUTES = 1
DEFAULT_ENDPOINT = names.DEFAULT_ENDPOINT


def blueprint_from_item(project: dict, item: dict) -> blueprint.Blueprint:
    """Rebuild the Blueprint of a stored version (`spec` + `systemPrompt`)."""
    return blueprint.from_stored(
        item["spec"], item["systemPrompt"], project["projectId"], item["version"], domain=project.get("domainId")
    )


def key_cap(bp: blueprint.Blueprint) -> tuple[float | int, str]:
    """Cap of the agent key: the blueprint's `spec.budget` (perModelCap / resetPeriod) when present, else settings.defaultKeyCap."""
    defaults = settings.load()["defaultKeyCap"]
    budget = bp.budget
    return budget.get("perModelCap", defaults["maxBudget"]), str(budget.get("resetPeriod", defaults["budgetDuration"]))


def ensure_key(project: dict, env: str, bp: blueprint.Blueprint, created_by: str) -> tuple[dict, bool]:
    """The KEY item for (env, blueprint model), created when missing; registers the credential_provider resource like module 5.

    Returns (key item without table keys, created)."""
    project_id, alias = project["projectId"], bp.model_alias
    existing = db.get(db.key_key(project_id, env, alias))
    created = False
    if existing is None:
        cap, duration = key_cap(bp)
        logger.info("creating the %s key for %s/%s (cap %s / %s)", env, project_id, alias, cap, duration)
        existing = litellm.create_key(project, env, alias, cap, duration, created_by)
        db.put(existing)
        created = True
    item = db.strip_keys(existing)
    provider_name = item.get("credentialProviderName")
    if provider_name and not db.get(db.resource_key(project_id, env, "credential_provider", provider_name)):
        resources_store.put_resource(
            project_id,
            env,
            "credential_provider",
            provider_name,
            arn=item.get("credentialProviderArn"),
            attributes={"model": alias, "keyAlias": item.get("alias")},
            by=created_by,
        )
    return item, created


def render(project: dict, bp: blueprint.Blueprint, env: str, key_item: dict | None = None) -> dict:
    """CreateHarness kwargs for one environment from the registered resources (BLUEPRINT_UNRESOLVED names the missing resource and the module that creates it)."""
    return blueprint.render(
        bp,
        env,
        project,
        resources_store.registered(project["projectId"], env),
        clients.cfg(),
        credential_provider_arn=(key_item or {}).get("credentialProviderArn"),
        defaults=settings.load()["defaultLimits"],
    )


def render_version(project: dict, item: dict, env: str) -> dict:
    """GET .../render: the CreateHarness payload plus `tags` (dry run, no side effects).

    The key comes from the KEY item of (env, model) when it exists, else from the registered credential_provider resource."""
    bp = blueprint_from_item(project, item)
    key_item = db.get(db.key_key(project["projectId"], env, bp.model_alias))
    return {**render(project, bp, env, key_item), "tags": blueprint.tags_for(bp, env)}


def endpoint_details(harness: dict, endpoint_name: str) -> dict | None:
    """GetHarnessEndpoint for `endpoint_name` (DEFAULT), or None when the service does not expose it."""
    try:
        response = clients.agentcore_control().get_harness_endpoint(
            harnessId=harness["harnessId"], endpointName=endpoint_name
        )
    except ClientError as exc:
        logger.warning("GetHarnessEndpoint %s/%s failed: %s", harness.get("harnessId"), endpoint_name, exc)
        return None
    return response.get("endpoint") or response


def wait_endpoint(harness: dict, endpoint_name: str) -> dict:
    """GetHarnessEndpoint until `endpoint_name` is READY on the harness's new version; {} when the service does not expose it.

    DEFAULT follows CreateHarness/UpdateHarness asynchronously: right after the new version is READY the endpoint can still
    answer UPDATING with the previous `liveVersion`, and a `harness_endpoint` row registered from that answer would stay stale
    until the next deployment."""
    version = str(harness.get("harnessVersion"))
    control = clients.agentcore_control()

    def current() -> dict:
        endpoint = control.get_harness_endpoint(harnessId=harness["harnessId"], endpointName=endpoint_name)["endpoint"]
        live = endpoint.get("liveVersion")
        if live is not None and str(live) != version and endpoint.get("status") in agentcore.ENDPOINT_OK:
            return {**endpoint, "status": "UPDATING"}
        return endpoint

    try:
        return agentcore.wait_for(
            current,
            agentcore.ENDPOINT_OK,
            agentcore.ENDPOINT_BAD,
            every=3,
            timeout=300,
            label=f"endpoint {endpoint_name}",
        )
    except ClientError as exc:
        logger.warning("GetHarnessEndpoint %s/%s failed: %s", harness.get("harnessId"), endpoint_name, exc)
        return {}


def register_harness(
    project_id: str,
    env: str,
    harness: dict,
    endpoint_name: str,
    bp: blueprint.Blueprint,
    blueprint_version: int,
    job_id: str | None,
    by: str,
    endpoint: dict | None = None,
) -> dict:
    """Register the `harness` and `harness_endpoint` resources (contract section 3) of a deployed version."""
    n = names.ProjectNames(project_id, env)
    log_group = agentcore.harness_log_group(harness, endpoint_name)
    runtime_id = ((harness.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {}).get("agentRuntimeId")
    version = str(harness.get("harnessVersion"))
    harness_row = resources_store.put_resource(
        project_id,
        env,
        "harness",
        n.harness,
        arn=harness["arn"],
        id=harness["harnessId"],
        attributes={
            "version": version,
            "blueprintVersion": int(blueprint_version),
            "model": bp.model_alias,
            "endpoint": endpoint_name,
            "runtimeId": runtime_id,
            "logGroup": log_group,
            "jobId": job_id,
            "deployedAt": db.now_iso(),
        },
        by=by,
    )
    endpoint = endpoint or endpoint_details(harness, endpoint_name) or {}
    endpoint_row = resources_store.put_resource(
        project_id,
        env,
        "harness_endpoint",
        endpoint_name,
        arn=endpoint.get("arn"),
        id=endpoint.get("endpointName") or endpoint_name,
        attributes={
            "harnessId": harness["harnessId"],
            "harnessArn": harness["arn"],
            "version": str(endpoint.get("liveVersion") or endpoint.get("targetVersion") or version),
            "qualifier": None if endpoint_name == DEFAULT_ENDPOINT else endpoint_name,
            "status": endpoint.get("status"),
            "logGroup": log_group,
            "serviceName": names.harness_service_name(n.harness, endpoint_name),
        },
        by=by,
    )
    return {"harness": harness_row, "endpoint": endpoint_row, "logGroup": log_group}


def deploy_harness(project: dict, item: dict, env: str, by: str) -> dict:
    """Key + render + CreateHarness / UpdateHarness (waits for the new version to be READY). Returns the pieces."""
    bp = blueprint_from_item(project, item)
    key_item, key_created = ensure_key(project, env, bp, by)
    spec = render(project, bp, env, key_item)
    n = names.ProjectNames(project["projectId"], env)
    harness = agentcore.create_or_update_harness(
        n.harness, spec, tags=blueprint.tags_for(bp, env), region=clients.cfg().region
    )
    return {"blueprint": bp, "key": key_item, "keyCreated": key_created, "spec": spec, "harness": harness}


def _output(
    harness: dict, pieces: dict, item: dict, env: str, endpoint_name: str, log_group: str, started: float
) -> dict:
    return {
        "projectId": pieces["key"].get("projectId"),
        "env": env,
        "blueprintVersion": int(item["version"]),
        "model": pieces["blueprint"].model_alias,
        "harnessName": harness.get("harnessName"),
        "harnessArn": harness["arn"],
        "harnessId": harness["harnessId"],
        "harnessVersion": str(harness.get("harnessVersion")),
        "endpoint": endpoint_name,
        "logGroup": log_group,
        "keyAlias": pieces["key"].get("alias"),
        "keyCreated": pieces["keyCreated"],
        "credentialProviderArn": pieces["key"].get("credentialProviderArn"),
        "seconds": round(time.monotonic() - started, 1),
    }


def deploy(project: dict, item: dict, env: str, by: str, job_id: str | None = None) -> dict:
    """The deploy job: Dev harness from a blueprint version; registers harness + harness_endpoint DEFAULT."""
    started = time.monotonic()
    pieces = deploy_harness(project, item, env, by)
    harness = pieces["harness"]
    endpoint = wait_endpoint(harness, DEFAULT_ENDPOINT)
    registered = register_harness(
        project["projectId"], env, harness, DEFAULT_ENDPOINT, pieces["blueprint"], item["version"], job_id, by, endpoint
    )
    return _output(harness, pieces, item, env, DEFAULT_ENDPOINT, registered["logGroup"], started)


def promote(project: dict, item: dict, by: str, job_id: str | None = None) -> dict:
    """The promote job: Prod harness version (its DEFAULT endpoint follows it), online evaluation config, resources registered.

    No named endpoint: CreateHarnessEndpoint is denied by SCP in the Workshop Studio event accounts (2026-10-22), and the Prod
    harness only ever changes through this job, so DEFAULT is always the last promoted version."""
    started = time.monotonic()
    env = "prod"
    cfg = clients.cfg()
    n = names.ProjectNames(project["projectId"], env)
    pieces = deploy_harness(project, item, env, by)
    harness = pieces["harness"]
    endpoint_name = n.harness_endpoint
    endpoint = wait_endpoint(harness, endpoint_name)
    registered = register_harness(
        project["projectId"], env, harness, endpoint_name, pieces["blueprint"], item["version"], job_id, by, endpoint
    )
    config = agentcore.ensure_online_evaluation(
        n.online_evaluation,
        registered["logGroup"],
        n.harness_service_name,
        list(ONLINE_EVALUATORS),
        cfg.evaluation_role_arn,
        sampling_percentage=ONLINE_SAMPLING_PERCENTAGE,
        session_timeout_minutes=ONLINE_SESSION_TIMEOUT_MINUTES,
        tags={"ManagedBy": "platform", "ProjectId": project["projectId"], "Environment": env},
        region=cfg.region,
    )
    resources_store.put_resource(
        project["projectId"],
        env,
        "online_evaluation",
        n.online_evaluation,
        arn=config.get("onlineEvaluationConfigArn"),
        id=config.get("onlineEvaluationConfigId"),
        attributes={
            "evaluators": list(ONLINE_EVALUATORS),
            "logGroup": registered["logGroup"],
            "serviceName": n.harness_service_name,
            "status": config.get("status"),
            "samplingPercentage": ONLINE_SAMPLING_PERCENTAGE,
            "jobId": job_id,
        },
        by=by,
    )
    output = _output(harness, pieces, item, env, endpoint_name, registered["logGroup"], started)
    output.update(
        endpointArn=endpoint.get("arn"),
        endpointStatus=endpoint.get("status"),
        onlineEvaluationConfigId=config.get("onlineEvaluationConfigId"),
        onlineEvaluationConfigArn=config.get("onlineEvaluationConfigArn"),
    )
    return output
