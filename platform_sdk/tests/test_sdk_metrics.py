from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from platform_sdk import metrics


def _put(cw, namespace, name, dims, values, unit="Count"):
    now = datetime.now(UTC)
    cw.put_metric_data(
        Namespace=namespace,
        MetricData=[
            {
                "MetricName": name,
                "Dimensions": dims,
                "Timestamp": now - timedelta(minutes=5 * i),
                "Value": v,
                "Unit": unit,
            }
            for i, v in enumerate(values)
        ],
    )


@mock_aws
def test_harness_metrics_discovers_dimensions_and_returns_series(cfg, monkeypatch):
    # moto cannot compute percentile statistics (p50/p90); the production mapping keeps them, the test queries averages instead
    assert metrics.HARNESS_METRICS["latencyP50"] == ("Latency", "p50") and metrics.HARNESS_METRICS["latencyP90"] == (
        "Latency",
        "p90",
    )
    monkeypatch.setitem(metrics.HARNESS_METRICS, "latencyP50", ("Latency", "Average"))
    monkeypatch.setitem(metrics.HARNESS_METRICS, "latencyP90", ("Latency", "Maximum"))
    cw = boto3.client("cloudwatch", region_name="us-east-1")
    dims = [
        {"Name": "Name", "Value": "harness_hr_assistant_dev::DEFAULT"},
        {
            "Name": "Resource",
            "Value": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/harness_hr_assistant_dev-abc",
        },
    ]
    _put(cw, metrics.NAMESPACE, "Invocations", dims, [1, 2, 3])
    _put(cw, metrics.NAMESPACE, "Errors", dims, [0, 1])
    _put(cw, metrics.NAMESPACE, "Sessions", dims, [1])
    _put(cw, metrics.NAMESPACE, "Latency", dims, [1200, 800], unit="Milliseconds")
    _put(cw, metrics.NAMESPACE, "Invocations", [{"Name": "Name", "Value": "harness_other::DEFAULT"}], [50])
    out = metrics.harness_metrics(cfg, "hr_assistant_dev", "DEFAULT", hours=2, period=300, cw=cw)
    assert set(out) >= {
        "invocations",
        "errors",
        "userErrors",
        "systemErrors",
        "throttles",
        "sessions",
        "latencyP50",
        "latencyP90",
        "totals",
        "dimensions",
        "window",
    }
    assert out["totals"]["invocations"] == 6 and out["totals"]["errors"] == 1 and out["totals"]["sessions"] == 1
    assert out["totals"]["throttles"] == 0  # no data, fallback dimension set
    assert out["totals"]["latencyP90Max"] == 1200.0 and len(out["latencyP50"]) >= 1
    assert all(isinstance(ts, str) and isinstance(v, float) for ts, v in out["invocations"])
    assert out["invocations"] == sorted(out["invocations"])
    assert out["dimensions"]["Invocations"][0][0]["Name"] == "Name"
    assert out["window"]["period"] == 300


@mock_aws
def test_policy_metrics_filters_by_engine_and_groups_by_tool(cfg):
    cw = boto3.client("cloudwatch", region_name="us-east-1")
    engine = "agent_platform_policy_engine-abc123"
    base = [
        {"Name": "OperationName", "Value": "AuthorizeAction"},
        {"Name": "PolicyEngine", "Value": engine},
        {"Name": "TargetResource", "Value": "hr-assistant-dev-gw-abcdefghij"},
    ]
    # the request-level partition (one datapoint per request) and the per-tool partition carry the same decisions
    _put(cw, metrics.NAMESPACE, "AllowDecisions", base, [3, 2])
    _put(cw, metrics.NAMESPACE, "DenyDecisions", base, [1])
    _put(
        cw,
        metrics.NAMESPACE,
        "AllowDecisions",
        base + [{"Name": "ToolName", "Value": "hrtools___get_leave_balance"}],
        [3, 2],
    )
    _put(
        cw,
        metrics.NAMESPACE,
        "DenyDecisions",
        base + [{"Name": "ToolName", "Value": "fintools___get_invoice_status"}],
        [1],
    )
    _put(
        cw,
        metrics.NAMESPACE,
        "DenyDecisions",
        [{"Name": "OperationName", "Value": "AuthorizeAction"}, {"Name": "PolicyEngine", "Value": "other-engine"}],
        [9],
    )
    out = metrics.policy_metrics(cfg, engine, hours=2, cw=cw)
    assert out["totals"] == {"allow": 5.0, "deny": 1.0}
    assert out["byTool"] == {
        "hrtools___get_leave_balance": {"allow": 5.0, "deny": 0.0},
        "fintools___get_invoice_status": {"allow": 0.0, "deny": 1.0},
    }
    assert sum(v for _, v in out["allow"]) == 5.0 and sum(v for _, v in out["deny"]) == 1.0
    assert out["targetResource"] is None and out["dimensions"]["AllowDecisions"]["totals"] == [
        "OperationName",
        "PolicyEngine",
        "TargetResource",
    ]
    # per gateway: the TargetResource partition filtered on the gateway id (or its ARN)
    per_gateway = metrics.policy_metrics(cfg, engine, hours=2, cw=cw, target_resource="hr-assistant-dev-gw-abcdefghij")
    assert per_gateway["totals"] == {"allow": 5.0, "deny": 1.0} and per_gateway["byTool"] == out["byTool"]
    by_arn = metrics.policy_metrics(
        cfg,
        engine,
        hours=2,
        cw=cw,
        target_resource="arn:aws:bedrock-agentcore:us-east-1:1:gateway/hr-assistant-dev-gw-abcdefghij",
    )
    assert by_arn["totals"] == {"allow": 5.0, "deny": 1.0}
    other = metrics.policy_metrics(cfg, engine, hours=2, cw=cw, target_resource="hr-assistant-prod-gw-0000000000")
    assert other["totals"] == {"allow": 0.0, "deny": 0.0} and other["byTool"] == {} and other["allow"] == []


class _FakePolicyCloudWatch:
    """ListMetrics with the 16 overlapping dimension name sets the policy engine publishes (seen live 9 Oct 2026) for two
    gateways; GetMetricData answers one datapoint of 1.0 per query."""

    ENGINE = "agent_platform_policy_engine-9q9z8407ow"
    GATEWAYS = ("hr-assistant-dev-gw-tprllkjtys", "invoice-helper-dev-gw-5j3cbar8ai")
    TOOLS = ("hrtools___get_leave_balance", "hrkb___Retrieve")

    def __init__(self):
        self.queries = []

    def _sets(self):
        sets = []
        for mode in (None, "ENFORCE"):
            for gateway in (None, *self.GATEWAYS):
                for tool in (None, *self.TOOLS):
                    for policy in (None, "hr_assistant_dev_allow_get_leave_balance-c_tcezwxf5"):
                        if tool and policy:
                            continue  # the service never combines Policy with ToolName
                        if (tool or policy) and not gateway:
                            continue  # ToolName / Policy sets always carry the TargetResource
                        dims = [
                            {"Name": "OperationName", "Value": "AuthorizeAction"},
                            {"Name": "PolicyEngine", "Value": self.ENGINE},
                        ]
                        if mode:
                            dims.append({"Name": "Mode", "Value": mode})
                        if gateway:
                            dims.append({"Name": "TargetResource", "Value": gateway})
                        if tool:
                            dims.append({"Name": "ToolName", "Value": tool})
                        if policy:
                            dims.append({"Name": "Policy", "Value": policy})
                        sets.append(sorted(dims, key=lambda d: d["Name"]))
                        if not policy and not tool and gateway is None:
                            # the engine-less variants the service publishes as well ([OperationName], [Mode, OperationName])
                            sets.append([d for d in dims if d["Name"] != "PolicyEngine"])
        return sets

    def get_paginator(self, operation):
        assert operation == "list_metrics"
        fake = self

        class P:
            def paginate(self, **kwargs):
                yield {"Metrics": [{"MetricName": kwargs["MetricName"], "Dimensions": dims} for dims in fake._sets()]}

        return P()

    def get_metric_data(self, **kwargs):
        self.queries.extend(kwargs["MetricDataQueries"])
        return {
            "MetricDataResults": [
                {"Id": q["Id"], "Timestamps": [datetime(2026, 10, 9, 1, 0, tzinfo=UTC)], "Values": [1.0]}
                for q in kwargs["MetricDataQueries"]
            ]
        }


def test_policy_metrics_counts_overlapping_dimension_sets_once(cfg):
    cw = _FakePolicyCloudWatch()
    out = metrics.policy_metrics(cfg, cw.ENGINE, hours=1, period=60, cw=cw)
    # engine-wide: exactly one [OperationName, PolicyEngine] set per decision metric -> one datapoint of 1.0, not the sum of 20+ sets
    assert out["totals"] == {"allow": 1.0, "deny": 1.0}
    assert out["dimensions"]["AllowDecisions"]["totals"] == ["OperationName", "PolicyEngine"]
    assert out["dimensions"]["AllowDecisions"]["byTool"] == [
        "OperationName",
        "PolicyEngine",
        "TargetResource",
        "ToolName",
    ]
    # byTool: one set per (gateway, tool) in the ToolName partition, Mode variants ignored
    assert out["byTool"] == {tool: {"allow": 2.0, "deny": 2.0} for tool in cw.TOOLS}
    totals_queries = [
        q
        for q in cw.queries
        if sorted(d["Name"] for d in q["MetricStat"]["Metric"]["Dimensions"]) == ["OperationName", "PolicyEngine"]
    ]
    assert len(totals_queries) == 2  # one per decision metric
    assert not any("Mode" in {d["Name"] for d in q["MetricStat"]["Metric"]["Dimensions"]} for q in cw.queries)

    cw.queries.clear()
    per_gateway = metrics.policy_metrics(cfg, cw.ENGINE, hours=1, period=60, cw=cw, target_resource=cw.GATEWAYS[0])
    assert per_gateway["totals"] == {
        "allow": 1.0,
        "deny": 1.0,
    }  # the one [OperationName, PolicyEngine, TargetResource] set of that gateway
    assert per_gateway["dimensions"]["DenyDecisions"]["totals"] == ["OperationName", "PolicyEngine", "TargetResource"]
    assert per_gateway["byTool"] == {tool: {"allow": 1.0, "deny": 1.0} for tool in cw.TOOLS}
    assert all(
        {d["Name"]: d["Value"] for d in q["MetricStat"]["Metric"]["Dimensions"]}["TargetResource"] == cw.GATEWAYS[0]
        for q in cw.queries
    )


def test_choose_partition_prefers_the_exact_name_set_then_the_smallest_superset():
    a = [{"Name": "OperationName", "Value": "A"}, {"Name": "PolicyEngine", "Value": "e"}]
    b = [
        {"Name": "Mode", "Value": "ENFORCE"},
        {"Name": "OperationName", "Value": "A"},
        {"Name": "PolicyEngine", "Value": "e"},
    ]
    c = [*a, {"Name": "ToolName", "Value": "t"}]
    assert metrics.choose_partition([c, b, a], ("OperationName", "PolicyEngine"), ("PolicyEngine",), ("ToolName",)) == [
        a
    ]
    assert metrics.choose_partition([c, b], ("OperationName", "PolicyEngine"), ("PolicyEngine",), ("ToolName",)) == [b]
    assert metrics.choose_partition([c], ("OperationName", "PolicyEngine"), ("PolicyEngine",), ("ToolName",)) == []
    assert metrics.choose_partition([], ("OperationName", "PolicyEngine"), ("PolicyEngine",)) == []


@mock_aws
def test_evaluation_metrics_by_evaluator_and_label(cfg):
    cw = boto3.client("cloudwatch", region_name="us-east-1")
    config_id = "hr_assistant_prod_online_eval-XyZ"
    base = [
        {"Name": "onlineEvaluationConfigId", "Value": config_id},
        {"Name": "service.name", "Value": "harness_hr_assistant_prod.live"},
    ]
    _put(
        cw,
        metrics.EVALUATIONS_NAMESPACE,
        "Builtin.Helpfulness",
        base + [{"Name": "label", "Value": "Very helpful"}],
        [0.83, 0.83],
        unit="None",
    )
    _put(  # a second label of the same evaluator in the same hour: the mean must be sample-weighted, not summed
        cw,
        metrics.EVALUATIONS_NAMESPACE,
        "Builtin.Helpfulness",
        base + [{"Name": "label", "Value": "Helpful"}],
        [0.67],
        unit="None",
    )
    _put(
        cw, metrics.EVALUATIONS_NAMESPACE, "Builtin.Helpfulness", base, [0.83, 0.83, 0.67], unit="None"
    )  # aggregate set
    _put(
        cw,
        metrics.EVALUATIONS_NAMESPACE,
        "Builtin.Harmfulness",
        base + [{"Name": "label", "Value": "Not Harmful"}],
        [1.0],
        unit="None",
    )
    _put(
        cw,
        metrics.EVALUATIONS_NAMESPACE,
        "Builtin.Harmfulness",
        [{"Name": "onlineEvaluationConfigId", "Value": "other"}, {"Name": "label", "Value": "Harmful"}],
        [0.0],
        unit="None",
    )
    out = metrics.evaluation_metrics(cfg, config_id, hours=2, period=3600, cw=cw)
    assert set(out["evaluators"]) == {"Helpfulness", "Harmfulness"}
    helpfulness = out["evaluators"]["Helpfulness"]
    # the aggregate (label-less) set drives mean and samples; the labelled sets only feed the label counts
    assert helpfulness["labels"] == {"Very helpful": 2.0, "Helpful": 1.0} and helpfulness["samples"] == 3.0
    assert helpfulness["mean"] == pytest.approx((0.83 * 2 + 0.67) / 3, abs=1e-3)  # 0.7767, never above 1
    assert all(0 <= v <= 1 for _, v in helpfulness["average"])
    # Harmfulness has no aggregate set here: the labelled sets are combined weighted by their sample counts
    assert out["evaluators"]["Harmfulness"]["mean"] == 1.0
    assert (
        out["evaluators"]["Harmfulness"]["labels"] == {"Not Harmful": 1.0}
        and out["evaluators"]["Harmfulness"]["samples"] == 1.0
    )


def test_merge_sums_point_wise():
    merged = metrics._merge([[["t1", 1.0], ["t2", 2.0]], [["t2", 3.0], ["t0", 1.0]]])
    assert merged == [["t0", 1.0], ["t1", 1.0], ["t2", 5.0]]


class _FakeCloudWatch:
    """Answers ListMetrics with the four real dimension sets per metric and GetMetricData with one datapoint per query."""

    def __init__(self):
        self.queries = []
        name = {"Name": "Name", "Value": "harness_hr_assistant_dev::DEFAULT"}
        op = {"Name": "Operation", "Value": "InvokeAgentRuntime"}
        res = {
            "Name": "Resource",
            "Value": "arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/harness_hr_assistant_dev-abc",
        }
        ep = {"Name": "EndpointQualifier", "Value": "DEFAULT"}
        hid = {
            "Name": "HarnessId",
            "Value": "arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/hr_assistant_dev-xyz",
        }
        ct = {"Name": "ComputeType", "Value": "MicroVM"}
        self.sets = [[name, op, res], [ep, hid, name, op], [ct, name, op, res], [ep, hid, name, op, res]]

    def get_paginator(self, operation):
        assert operation == "list_metrics"
        fake = self

        class P:
            def paginate(self, **kwargs):
                metrics = [
                    {"MetricName": m, "Dimensions": dims}
                    for m in ("Invocations", "Sessions", "Latency")
                    for dims in fake.sets
                ]
                yield {"Metrics": metrics}

        return P()

    def get_metric_data(self, **kwargs):
        self.queries.extend(kwargs["MetricDataQueries"])
        return {
            "MetricDataResults": [
                {"Id": q["Id"], "Timestamps": [datetime(2026, 10, 8, 15, 0, tzinfo=UTC)], "Values": [1.0]}
                for q in kwargs["MetricDataQueries"]
            ]
        }


def test_harness_metrics_queries_one_dimension_set_per_metric(cfg):
    cw = _FakeCloudWatch()
    out = metrics.harness_metrics(cfg, "hr_assistant_dev", "DEFAULT", hours=1, period=60, cw=cw)
    assert (
        out["totals"]["invocations"] == 1.0 and out["totals"]["sessions"] == 1.0
    )  # not multiplied by the four equivalent sets
    invocation_queries = [q for q in cw.queries if q["MetricStat"]["Metric"]["MetricName"] == "Invocations"]
    assert len(invocation_queries) == 1
    assert sorted(d["Name"] for d in invocation_queries[0]["MetricStat"]["Metric"]["Dimensions"]) == [
        "Name",
        "Operation",
        "Resource",
    ]
    assert len(out["dimensions"]["Invocations"]) == 4  # all discovered sets are still reported


def test_select_dimension_sets_keeps_partitions_of_the_smallest_name_set():
    a = [{"Name": "Name", "Value": "x"}, {"Name": "Operation", "Value": "A"}]
    b = [{"Name": "Name", "Value": "x"}, {"Name": "Operation", "Value": "B"}]
    c = [{"Name": "Name", "Value": "x"}, {"Name": "Operation", "Value": "A"}, {"Name": "Resource", "Value": "r"}]
    assert metrics.select_dimension_sets([c, a, b]) == [a, b]
    assert metrics.select_dimension_sets([]) == []
