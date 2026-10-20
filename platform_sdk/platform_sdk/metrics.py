"""CloudWatch metrics for harness endpoints, policy decisions and online evaluations (dashboard and module 7).

Sources verified in the spikes (7 Oct 2026): built-in `AWS/Bedrock-AgentCore` metrics Invocations, Errors, UserErrors,
SystemErrors, Throttles, Latency, Duration, Sessions with dimension `Name = harness_<name>::<endpoint>` (plus a `Resource`
dimension), policy `AllowDecisions` / `DenyDecisions` with PolicyEngine / TargetResource / ToolName / Mode, and
`Bedrock-AgentCore/Evaluations` metrics named by evaluator with `onlineEvaluationConfigId`, `service.name` and `label`.
Dimension sets are discovered with ListMetrics so the code does not depend on their exact composition.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import boto3

from . import names

NAMESPACE = "AWS/Bedrock-AgentCore"
EVALUATIONS_NAMESPACE = "Bedrock-AgentCore/Evaluations"
HARNESS_METRICS = {
    # output key -> (metric name, statistic)
    "invocations": ("Invocations", "Sum"),
    "errors": ("Errors", "Sum"),
    "userErrors": ("UserErrors", "Sum"),
    "systemErrors": ("SystemErrors", "Sum"),
    "throttles": ("Throttles", "Sum"),
    "sessions": ("Sessions", "Sum"),
    "latencyP50": ("Latency", "p50"),
    "latencyP90": ("Latency", "p90"),
}
Series = list[list]  # [[iso timestamp, value], ...] sorted by time


def _cloudwatch(cfg):
    return boto3.client("cloudwatch", region_name=getattr(cfg, "region", None))


def _window(hours: int, period: int = 60) -> tuple[datetime, datetime]:
    """[start, end): start floored to a period boundary, end one period past now so the current bucket is included."""
    now = datetime.now(UTC).replace(microsecond=0)
    start = now - timedelta(hours=hours)
    start -= timedelta(seconds=start.timestamp() % period)
    return start, now + timedelta(seconds=period)


def list_metric_dimensions(
    cw, namespace: str, metric_name: str | None = None, dimension_filter: list[dict] | None = None
) -> list[dict]:
    """ListMetrics -> [{MetricName, Dimensions}] (every dimension set published for the filter)."""
    kwargs: dict[str, Any] = {"Namespace": namespace}
    if metric_name:
        kwargs["MetricName"] = metric_name
    if dimension_filter:
        kwargs["Dimensions"] = dimension_filter
    metrics: list[dict] = []
    for page in cw.get_paginator("list_metrics").paginate(**kwargs):
        for m in page.get("Metrics", []):
            entry = {
                "MetricName": m["MetricName"],
                "Dimensions": sorted(m.get("Dimensions", []), key=lambda d: d["Name"]),
            }
            if entry not in metrics:  # one entry per (metric, dimension set)
                metrics.append(entry)
    return metrics


def _query(index: int, namespace: str, metric_name: str, dimensions: list[dict], stat: str, period: int) -> dict:
    return {
        "Id": f"q{index}",
        "MetricStat": {
            "Metric": {"Namespace": namespace, "MetricName": metric_name, "Dimensions": dimensions},
            "Period": period,
            "Stat": stat,
        },
        "ReturnData": True,
    }


def get_metric_data(cw, queries: list[dict], start: datetime, end: datetime) -> dict[str, Series]:
    """GetMetricData for up to 500 queries (batched by 100), returning {Id: series} with ISO timestamps, sorted ascending."""
    series: dict[str, Series] = {}
    for offset in range(0, len(queries), 100):
        batch = queries[offset : offset + 100]
        kwargs: dict[str, Any] = {
            "MetricDataQueries": batch,
            "StartTime": start,
            "EndTime": end,
            "ScanBy": "TimestampAscending",
        }
        while True:
            page = cw.get_metric_data(**kwargs)
            for result in page.get("MetricDataResults", []):
                points = series.setdefault(result["Id"], [])
                points.extend(
                    [ts.isoformat(), float(v)]
                    for ts, v in zip(result.get("Timestamps", []), result.get("Values", []), strict=False)
                    if v is not None
                )
            if not page.get("NextToken"):
                break
            kwargs["NextToken"] = page["NextToken"]
    for points in series.values():
        points.sort(key=lambda p: p[0])
    return series


def select_dimension_sets(dimension_sets: list[list[dict]]) -> list[list[dict]]:
    """Keep only the dimension sets with the fewest dimension names.

    AgentCore publishes every harness metric under four equivalent dimension sets (verified live 8 Oct 2026: `[Name, Operation, Resource]`,
    `[EndpointQualifier, HarnessId, Name, Operation]`, `[ComputeType, Name, Operation, Resource]` and the union of all five). Each carries the
    same datapoints, so querying all of them multiplies the totals; sets that share the minimal name set (for example different `Operation`
    values) partition the data and are kept together.
    """
    if not dimension_sets:
        return []
    by_names: dict[frozenset, list[list[dict]]] = {}
    for dims in dimension_sets:
        by_names.setdefault(frozenset(d["Name"] for d in dims), []).append(dims)
    smallest = min(by_names, key=len)
    return by_names[smallest]


def _merge(series_list: list[Series]) -> Series:
    """Sum several series point-wise by timestamp (used when a metric is published under several dimension sets)."""
    totals: dict[str, float] = {}
    for series in series_list:
        for ts, value in series:
            totals[ts] = totals.get(ts, 0.0) + value
    return [[ts, totals[ts]] for ts in sorted(totals)]


def harness_metrics(cfg, harness_name: str, endpoint: str, hours: int = 24, period: int = 300, cw=None) -> dict:
    """Built-in metrics of one harness endpoint as series: invocations, errors, userErrors, systemErrors, throttles, sessions,
    latencyP50, latencyP90 (ms), plus `totals` and the discovered `dimensions`."""
    cw = cw or _cloudwatch(cfg)
    metric_name_value = names.harness_metric_name(harness_name, endpoint)
    discovered = list_metric_dimensions(cw, NAMESPACE, dimension_filter=[{"Name": "Name", "Value": metric_name_value}])
    dimension_sets: dict[str, list[list[dict]]] = {}
    for metric in discovered:
        dims = sorted(metric["Dimensions"], key=lambda d: d["Name"])
        if dims not in dimension_sets.setdefault(metric["MetricName"], []):
            dimension_sets[metric["MetricName"]].append(dims)
    fallback = [[{"Name": "Name", "Value": metric_name_value}]]

    queries: list[dict] = []
    mapping: dict[str, list[str]] = {}
    for key, (metric_name, stat) in HARNESS_METRICS.items():
        for dims in select_dimension_sets(dimension_sets.get(metric_name, [])) or fallback:
            query_id = f"q{len(queries)}"
            queries.append(_query(len(queries), NAMESPACE, metric_name, dims, stat, period))
            mapping.setdefault(key, []).append(query_id)
    start, end = _window(hours, period)
    data = get_metric_data(cw, queries, start, end)

    out: dict[str, Any] = {}
    for key, ids in mapping.items():
        parts = [data.get(i, []) for i in ids]
        out[key] = _merge(parts)
    out["totals"] = {
        key: round(sum(v for _, v in out.get(key, [])), 2)
        for key in ("invocations", "errors", "userErrors", "systemErrors", "throttles", "sessions")
    }
    latency = [v for _, v in out.get("latencyP90", [])]
    out["totals"]["latencyP90Max"] = round(max(latency), 1) if latency else None
    out["dimensions"] = {name: sets for name, sets in dimension_sets.items()}
    out["window"] = {"start": start.isoformat(), "end": end.isoformat(), "period": period}
    return out


POLICY_TOTAL_DIMENSIONS = ("OperationName", "PolicyEngine")  # engine-wide partition of the decisions
POLICY_TARGET_DIMENSIONS = ("OperationName", "PolicyEngine", "TargetResource")  # the same, split per gateway
POLICY_TOOL_DIMENSIONS = ("OperationName", "PolicyEngine", "TargetResource", "ToolName")  # per tool evaluation
POLICY_NON_PARTITION_DIMENSIONS = ("ToolName", "Policy")  # sets with these names do not partition the request count


def choose_partition(dimension_sets: list[list[dict]], preferred, required, excluded=()) -> list[list[dict]]:
    """One partition of a metric: the sets whose dimension names are exactly `preferred` when published, else the sets of the
    smallest name set that contains every `required` name and none of the `excluded` ones; [] when nothing qualifies."""
    by_names: dict[frozenset, list[list[dict]]] = {}
    for dims in dimension_sets:
        by_names.setdefault(frozenset(d["Name"] for d in dims), []).append(dims)
    if frozenset(preferred) in by_names:
        return by_names[frozenset(preferred)]
    candidates = [n for n in by_names if frozenset(required) <= n and not (frozenset(excluded) & n)]
    return by_names[min(candidates, key=len)] if candidates else []


def _target_matches(dims: list[dict], target_resource: str) -> bool:
    """TargetResource dimension == the gateway id (or the last segment of a gateway ARN)."""
    value = next((d["Value"] for d in dims if d["Name"] == "TargetResource"), None)
    return value is not None and value in (target_resource, target_resource.rsplit("/", 1)[-1])


def policy_metrics(
    cfg, policy_engine_id: str, hours: int = 24, period: int = 300, cw=None, target_resource: str | None = None
) -> dict:
    """Allow / deny decisions of a policy engine, optionally of one gateway (`target_resource` = gateway id or ARN):
    {allow: series, deny: series, byTool: {tool: {allow, deny}}, totals: {allow, deny}, targetResource, dimensions, window}.

    The service publishes every decision under many overlapping dimension sets (16 name sets seen live on 9 Oct 2026: with and
    without Mode, Policy, TargetResource, ToolName and their unions), so summing everything that mentions the engine multiplies
    the counts about tenfold. One partition is queried per purpose: `[OperationName, PolicyEngine]` (or `[..., TargetResource]`
    filtered on the gateway) for the totals and series, `[OperationName, PolicyEngine, TargetResource, ToolName]` for `byTool`.
    `byTool` counts tool evaluations, not requests: a tools/list request (PartiallyAuthorizeActions) evaluates every tool once,
    so the byTool sums are higher than the totals and must not be added to them.
    """
    cw = cw or _cloudwatch(cfg)
    start, end = _window(hours, period)
    out: dict[str, Any] = {
        "allow": [],
        "deny": [],
        "byTool": {},
        "totals": {"allow": 0.0, "deny": 0.0},
        "targetResource": target_resource,
        "dimensions": {},
    }
    total_names = POLICY_TARGET_DIMENSIONS if target_resource else POLICY_TOTAL_DIMENSIONS
    required = ("PolicyEngine", "TargetResource") if target_resource else ("PolicyEngine",)
    queries: list[dict] = []
    meta: list[tuple[str, str | None]] = []  # (decision, tool name | None for the totals partition)
    for decision, metric_name in (("allow", "AllowDecisions"), ("deny", "DenyDecisions")):
        engine_sets = [
            m["Dimensions"]
            for m in list_metric_dimensions(cw, NAMESPACE, metric_name=metric_name)
            if any(policy_engine_id in str(d["Value"]) for d in m["Dimensions"] if d["Name"] == "PolicyEngine")
        ]
        if target_resource:
            engine_sets = [dims for dims in engine_sets if _target_matches(dims, target_resource)]
        total_sets = choose_partition(engine_sets, total_names, required, POLICY_NON_PARTITION_DIMENSIONS)
        tool_sets = choose_partition(engine_sets, POLICY_TOOL_DIMENSIONS, (*required, "ToolName"), ("Policy",))
        out["dimensions"][metric_name] = {
            "totals": sorted(d["Name"] for d in total_sets[0]) if total_sets else None,
            "byTool": sorted(d["Name"] for d in tool_sets[0]) if tool_sets else None,
        }
        for dims in total_sets:
            queries.append(_query(len(queries), NAMESPACE, metric_name, dims, "Sum", period))
            meta.append((decision, None))
        for dims in tool_sets:
            queries.append(_query(len(queries), NAMESPACE, metric_name, dims, "Sum", period))
            meta.append((decision, next(d["Value"] for d in dims if d["Name"] == "ToolName")))
    data = get_metric_data(cw, queries, start, end) if queries else {}
    per_decision: dict[str, list[Series]] = {"allow": [], "deny": []}
    for index, (decision, tool) in enumerate(meta):
        series = data.get(f"q{index}", [])
        if tool is None:
            per_decision[decision].append(series)
        else:
            tool_entry = out["byTool"].setdefault(tool, {"allow": 0.0, "deny": 0.0})
            tool_entry[decision] = round(tool_entry[decision] + sum(v for _, v in series), 2)
    for decision in ("allow", "deny"):
        out[decision] = _merge(per_decision[decision])
        out["totals"][decision] = round(sum(v for _, v in out[decision]), 2)
    out["window"] = {"start": start.isoformat(), "end": end.isoformat(), "period": period}
    return out


def evaluation_metrics(cfg, online_config_id: str, hours: int = 24, period: int = 3600, cw=None) -> dict:
    """Online evaluation scores: {evaluators: {name: {average: series, labels: {label: count}, mean, samples}}, window}.

    The service publishes one aggregate dimension set per evaluator (config + service.name) plus one set per label
    (verified live on 8 Oct 2026). `average`, `mean` and `samples` come from the aggregate set; the labelled sets only feed
    `labels` (sample count per label). Without an aggregate set the labels are combined as a sample-weighted average
    (sum of Average x SampleCount over the labels, divided by the samples); adding the averages would overstate the score.
    """
    cw = cw or _cloudwatch(cfg)
    start, end = _window(hours, period)
    discovered = list_metric_dimensions(
        cw, EVALUATIONS_NAMESPACE, dimension_filter=[{"Name": "onlineEvaluationConfigId", "Value": online_config_id}]
    )
    queries: list[dict] = []
    meta: list[tuple[str, str | None, str]] = []  # (evaluator, label, stat)
    for metric in discovered:
        dims = metric["Dimensions"]
        label = next((d["Value"] for d in dims if d["Name"] == "label"), None)
        queries.append(_query(len(queries), EVALUATIONS_NAMESPACE, metric["MetricName"], dims, "Average", period))
        meta.append((metric["MetricName"], label, "Average"))
        queries.append(_query(len(queries), EVALUATIONS_NAMESPACE, metric["MetricName"], dims, "SampleCount", period))
        meta.append((metric["MetricName"], label, "SampleCount"))
    data = get_metric_data(cw, queries, start, end) if queries else {}
    series_by_label: dict[tuple[str, str | None], dict[str, Series]] = {}
    for index, (evaluator, label, stat) in enumerate(meta):
        name = evaluator[len("Builtin.") :] if evaluator.startswith("Builtin.") else evaluator
        series_by_label.setdefault((name, label), {})[stat] = data.get(f"q{index}", [])
    evaluators: dict[str, dict] = {}
    for name in sorted({n for n, _ in series_by_label}):
        labelled = {label: stats for (n, label), stats in series_by_label.items() if n == name and label}
        aggregate = series_by_label.get((name, None))
        entry: dict[str, Any] = {"average": [], "labels": {}, "mean": None, "samples": 0}
        for label, stats in labelled.items():
            entry["labels"][label] = round(sum(v for _, v in stats.get("SampleCount", [])), 2)
        weighted: dict[str, float] = {}  # timestamp -> sum(average x samples)
        counts: dict[str, float] = {}  # timestamp -> samples
        for stats in [aggregate] if aggregate else list(labelled.values()):
            per_timestamp = {ts: v for ts, v in stats.get("SampleCount", [])}
            for ts, average in stats.get("Average", []):
                n = per_timestamp.get(ts, 1.0)
                weighted[ts] = weighted.get(ts, 0.0) + average * n
                counts[ts] = counts.get(ts, 0.0) + n
        total = sum(counts.values())
        entry["average"] = [[ts, round(weighted[ts] / counts[ts], 4)] for ts in sorted(weighted) if counts[ts]]
        entry["samples"] = round(total, 2)
        entry["mean"] = round(sum(weighted.values()) / total, 4) if total else None
        evaluators[name] = entry
    return {"evaluators": evaluators, "window": {"start": start.isoformat(), "end": end.isoformat(), "period": period}}
