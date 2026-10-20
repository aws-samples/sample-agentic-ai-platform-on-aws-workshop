"""GET /metrics: platform_sdk.metrics keyed by the registered resources of a project and environment.

    harness_metrics     for the registered `harness` resource (metric name harness_<name>::<endpoint>)
    policy_metrics      for the shared policy engine in settings, filtered on the environment's registered `gateway` id
                        (the TargetResource dimension); `policy` is null while the environment has no gateway registered
    evaluation_metrics  for the registered `online_evaluation` config, if any (Prod after the first promotion)

Every series is a list of [ISO timestamp, value] pairs; everything is empty (not an error) when nothing is registered yet.
"""

from __future__ import annotations

import logging

from platform_sdk import metrics as sdk_metrics
from platform_sdk import names

from .. import settings
from ..handlers import resources as resources_store
from . import clients

logger = logging.getLogger(__name__)
HARNESS_SERIES = tuple(sdk_metrics.HARNESS_METRICS)


def period_for(hours: int) -> int:
    """CloudWatch period: 1 min up to 3 h, 5 min up to 24 h, 1 h beyond (keeps the point count small for the charts)."""
    if hours <= 3:
        return 60
    if hours <= 24:
        return 300
    return 3600


def project_metrics(project: dict, env: str, hours: int) -> dict:
    cfg = clients.cfg()
    cw = clients.cloudwatch()
    project_id = project["projectId"]
    n = names.ProjectNames(project_id, env)
    period = period_for(hours)
    series: dict[str, list] = {}
    totals: dict = {}
    out: dict = {
        "projectId": project_id,
        "env": env,
        "hours": hours,
        "period": period,
        "window": None,
        "series": series,
        "totals": totals,
        "harness": None,
        "policy": None,
        "evaluation": None,
    }

    harness_row = resources_store.find_registered(project_id, env, "harness", n.harness)
    if harness_row:
        data = sdk_metrics.harness_metrics(cfg, n.harness, n.harness_endpoint, hours=hours, period=period, cw=cw)
        for key in HARNESS_SERIES:
            series[key] = data.get(key, [])
        totals.update(data.get("totals", {}))
        out["window"] = data.get("window")
        out["harness"] = {
            "name": n.harness,
            "endpoint": n.harness_endpoint,
            "metricName": n.harness_metric_name,
            "arn": harness_row.get("arn"),
            "version": (harness_row.get("attributes") or {}).get("version"),
            "dimensions": data.get("dimensions", {}),
        }
    else:
        for key in HARNESS_SERIES:
            series[key] = []

    engine_id = settings.load().get("policyEngineId")
    gateway_row = resources_store.find_registered(
        project_id, env, "gateway", n.gateway
    ) or resources_store.find_registered(project_id, env, "gateway")
    gateway_id = (gateway_row or {}).get("id")
    if engine_id and gateway_id:
        data = sdk_metrics.policy_metrics(cfg, engine_id, hours=hours, period=period, cw=cw, target_resource=gateway_id)
        series["policyAllow"] = data.get("allow", [])
        series["policyDeny"] = data.get("deny", [])
        totals["policyAllow"] = data.get("totals", {}).get("allow", 0.0)
        totals["policyDeny"] = data.get("totals", {}).get("deny", 0.0)
        out["window"] = out["window"] or data.get("window")
        out["policy"] = {
            "policyEngineId": engine_id,
            "gatewayId": gateway_id,
            "gatewayName": gateway_row.get("name"),
            "byTool": data.get("byTool", {}),
            "scope": "gateway",
            "dimensions": data.get("dimensions", {}),
        }
    else:
        series["policyAllow"], series["policyDeny"] = [], []

    evaluation_row = resources_store.find_registered(project_id, env, "online_evaluation", n.online_evaluation)
    config_id = (evaluation_row or {}).get("id")
    if config_id:
        data = sdk_metrics.evaluation_metrics(cfg, config_id, hours=hours, period=max(period, 300), cw=cw)
        evaluators = data.get("evaluators", {})
        for name, entry in evaluators.items():
            series[f"evaluation{name}"] = entry.get("average", [])
        out["window"] = out["window"] or data.get("window")
        out["evaluation"] = {
            "configId": config_id,
            "name": n.online_evaluation,
            "evaluators": {
                name: {"mean": entry.get("mean"), "samples": entry.get("samples"), "labels": entry.get("labels", {})}
                for name, entry in evaluators.items()
            },
        }
    return out
