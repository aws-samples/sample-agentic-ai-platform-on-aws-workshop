import json
import logging
from pathlib import Path

import pytest

from platform_sdk import evaluation

SPIKE_RESULTS = Path(__file__).resolve().parents[2] / ".claude" / "spikes" / "dataset_results.json"


def scenario(scenario_id, status="COMPLETED", error=None, **evaluators):
    """evaluators: name -> list of result dicts (or a single number / label)."""
    evaluator_results = []
    for name, results in evaluators.items():
        if not isinstance(results, list):
            results = [results]
        entries = []
        for r in results:
            if isinstance(r, dict):
                entries.append(r)
            elif isinstance(r, str):
                entries.append({"label": r})
            else:
                entries.append({"value": r, "label": "x"})
        evaluator_results.append({"evaluator_id": f"Builtin.{name}", "results": entries})
    return {
        "scenario_id": scenario_id,
        "session_id": f"{scenario_id}-1234",
        "status": status,
        "error": error,
        "evaluator_results": evaluator_results,
    }


def test_summarize_means_passrate_errors_and_label_mapping():
    result = {
        "scenario_results": [
            scenario("s1", Correctness=1.0, GoalSuccessRate=1.0, TrajectoryInOrderMatch=1.0, Harmfulness=1.0),
            scenario(
                "s2", Correctness=0.5, GoalSuccessRate="Yes", TrajectoryInOrderMatch="No", Harmfulness="Not Harmful"
            ),
            scenario(
                "s3",
                Correctness=[{"value": 1.0}, {"value": 0.0}],
                GoalSuccessRate=0.0,
                TrajectoryInOrderMatch={
                    "errorCode": "EvaluationRequestValidationError",
                    "errorMessage": "no trajectory",
                },
                Harmfulness="Harmful",
            ),
            scenario("s4", status="FAILED", error="invoke timed out"),
        ]
    }
    scores, rows = evaluation.summarize(result)
    # s4 failed: it counts as 0.0 on the three floor evaluators (count 4) and lowers every passRate (denominator 4)
    assert scores["Correctness"] == {
        "mean": 0.5,
        "min": 0.0,
        "max": 1.0,
        "passRate": 0.75,
        "count": 4,
        "errors": 0,
        "failed": 1,
    }
    assert scores["GoalSuccessRate"]["mean"] == 0.5 and scores["GoalSuccessRate"]["passRate"] == 0.5
    assert scores["TrajectoryInOrderMatch"] == {
        "mean": 0.3333,
        "min": 0.0,
        "max": 1.0,
        "passRate": 0.3333,
        "count": 3,
        "errors": 1,
        "failed": 1,
    }
    # Harmfulness is an upper bound on the harm rate: the failed scenario gets no value, only a lower passRate
    assert scores["Harmfulness"]["mean"] == 0.6667 and scores["Harmfulness"]["min"] == 0.0
    assert scores["Harmfulness"]["count"] == 3 and scores["Harmfulness"]["passRate"] == 0.5
    assert scores["Harmfulness"]["failed"] == 1
    assert evaluation.failed_scenario_ids(rows) == ["s4"]
    assert [r["scenarioId"] for r in rows] == ["s1", "s2", "s3", "s4"]
    assert rows[1]["scores"] == {
        "Correctness": 0.5,
        "GoalSuccessRate": 1.0,
        "TrajectoryInOrderMatch": 0.0,
        "Harmfulness": 1.0,
    }
    assert rows[1]["labels"]["Harmfulness"] == "Not Harmful"
    assert rows[2]["scores"]["Correctness"] == 0.5 and "TrajectoryInOrderMatch" in rows[2]["errors"]
    assert rows[3] == {
        "scenarioId": "s4",
        "sessionId": "s4-1234",
        "status": "FAILED",
        "scores": {},
        "labels": {},
        "error": "invoke timed out",
        "errors": {},
    }
    json.dumps(scores)


@pytest.mark.skipif(not SPIKE_RESULTS.exists(), reason="spike fixture lives outside the repository")
def test_summarize_accepts_the_real_spike_results_file():
    scores, rows = evaluation.summarize(json.loads(SPIKE_RESULTS.read_text()))
    assert set(scores) == {"GoalSuccessRate", "Correctness", "TrajectoryAnyOrderMatch", "Helpfulness"}
    assert scores["GoalSuccessRate"]["mean"] == 1.0 and scores["Correctness"]["mean"] == 1.0
    assert scores["Helpfulness"]["mean"] == pytest.approx(0.7767, abs=1e-3)
    # the scenario without expected_trajectory produced an error result for the trajectory evaluator
    assert scores["TrajectoryAnyOrderMatch"]["count"] == 2 and scores["TrajectoryAnyOrderMatch"]["errors"] == 1
    assert len(rows) == 3 and all(r["status"] == "COMPLETED" for r in rows)
    assert all(v["failed"] == 0 for v in scores.values()) and evaluation.failed_scenario_ids(rows) == []


def test_failed_scenario_blocks_the_release():
    """9 scenarios scoring 1.0 plus one whose invocation failed (budget_exceeded) -> BLOCKED, not PENDING_APPROVAL."""
    evaluators = ("Correctness", "GoalSuccessRate", "TrajectoryInOrderMatch", "Harmfulness")
    good = [scenario(f"s{i}", **{e: 1.0 for e in evaluators}) for i in range(9)]
    failed = scenario(
        "s9", status="FAILED", error="Budget has been exceeded! Key=hr-assistant-dev Current cost: 1.2, Max budget: 1.0"
    )
    scores, rows = evaluation.summarize(
        {"scenario_results": good + [failed]}, evaluator_ids=[f"Builtin.{e}" for e in evaluators]
    )
    assert scores["Correctness"] == {
        "mean": 0.9,
        "min": 0.0,
        "max": 1.0,
        "passRate": 0.9,
        "count": 10,
        "errors": 0,
        "failed": 1,
    }
    assert scores["Harmfulness"]["mean"] == 1.0 and scores["Harmfulness"]["passRate"] == 0.9
    thresholds = {"Correctness": 0.8, "GoalSuccessRate": 0.9, "TrajectoryInOrderMatch": 0.9, "Harmfulness": 0.0}
    verdict = evaluation.apply_thresholds(scores, thresholds)  # `failed` is read from the score entries
    assert verdict.status == "BLOCKED"
    assert {"evaluator": "_scenarios", "actual": 1, "threshold": 0, "op": "failed<="} in verdict.failures
    assert verdict.details["_scenarios"]["passed"] is False and verdict.details["Correctness"]["passed"] is True
    # the backend passes the body's failedScenarios list explicitly (same verdict)
    assert evaluation.apply_thresholds(scores, thresholds, failed_scenarios=["s9"]).status == "BLOCKED"
    # a summary of the same run carries the ids and reaches the same verdict
    summary = evaluation.RunSummary("run", "d.json", 10, scores, rows, 1.0, failed_scenarios=["s9"])
    assert summary.to_api_body()["failedScenarios"] == ["s9"] and summary.verdict(thresholds).status == "BLOCKED"
    # without failures the same scores pass
    clean, _ = evaluation.summarize({"scenario_results": good}, evaluator_ids=[f"Builtin.{e}" for e in evaluators])
    assert evaluation.apply_thresholds(clean, thresholds).status == "PENDING_APPROVAL"


def test_apply_thresholds_floor_and_harmfulness_upper_bound():
    scores = {
        "Correctness": {"mean": 0.7, "min": 0.0, "passRate": 0.7, "count": 10, "errors": 0},
        "Builtin.GoalSuccessRate": {"mean": 0.9, "min": 0.0, "passRate": 0.9, "count": 10, "errors": 0},
        "TrajectoryInOrderMatch": {"mean": 0.7, "min": 0.0, "passRate": 0.7, "count": 10, "errors": 0},
        "Harmfulness": {"mean": 1.0, "min": 1.0, "passRate": 1.0, "count": 10, "errors": 0},
    }
    thresholds = {"Correctness": 0.8, "GoalSuccessRate": 0.9, "Builtin.TrajectoryInOrderMatch": 0.9, "Harmfulness": 0.0}
    verdict = evaluation.apply_thresholds(scores, thresholds)
    assert not verdict and verdict.status == "BLOCKED"
    assert [f["evaluator"] for f in verdict.failures] == ["Correctness", "TrajectoryInOrderMatch"]
    assert verdict.failures[0] == {"evaluator": "Correctness", "actual": 0.7, "threshold": 0.8, "op": "mean>="}
    assert verdict.details["Harmfulness"] == {
        "actual": 0.0,
        "threshold": 0.0,
        "op": "harmRate<=",
        "passed": True,
        "count": 10,
        "errors": 0,
    }
    assert verdict.details["GoalSuccessRate"]["passed"] is True

    scores["Correctness"]["mean"] = 0.9
    scores["TrajectoryInOrderMatch"]["mean"] = 1.0
    passing = evaluation.apply_thresholds(scores, thresholds)
    assert passing.passed and passing.status == "PENDING_APPROVAL" and passing.failures == []


def test_apply_thresholds_harmful_answer_blocks_and_missing_evaluator_blocks():
    scores = {"Harmfulness": {"mean": 0.9, "min": 0.0, "passRate": 0.9, "count": 10, "errors": 0}}
    verdict = evaluation.apply_thresholds(scores, {"Harmfulness": 0.0})
    assert verdict.failures == [{"evaluator": "Harmfulness", "actual": 0.1, "threshold": 0.0, "op": "harmRate<="}]
    assert evaluation.apply_thresholds(scores, {"Harmfulness": 0.1}).passed
    missing = evaluation.apply_thresholds(scores, {"Correctness": 0.8})
    assert missing.failures == [{"evaluator": "Correctness", "actual": None, "threshold": 0.8, "op": "mean>="}]
    assert evaluation.apply_thresholds({}, {}).passed


def test_run_summary_api_body_and_helpers(tmp_path):
    summary = evaluation.RunSummary(
        "run-1",
        "hr-assistant.json",
        2,
        {"Correctness": {"mean": 1.0, "min": 1.0, "max": 1.0, "passRate": 1.0, "count": 2, "errors": 0}},
        [{"scenarioId": "s1", "status": "COMPLETED", "scores": {"Correctness": 1.0}}],
        12.5,
        ["Builtin.Correctness"],
    )
    body = summary.to_api_body()
    assert body == {
        "runId": "run-1",
        "datasetName": "hr-assistant.json",
        "scenarioCount": 2,
        "scores": summary.scores,
        "results": summary.results,
        "failedScenarios": [],
    }
    assert summary.table() == [
        {"evaluator": "Correctness", "mean": 1.0, "min": 1.0, "max": 1.0, "passRate": 1.0, "count": 2, "errors": 0}
    ]
    saved = summary.save(tmp_path / "out" / "run.json")
    assert json.loads(saved.read_text())["run_id"] == "run-1"
    loaded = evaluation.RunSummary.load(saved)  # a finished run can be resubmitted without another dataset run
    assert loaded == summary and loaded.to_api_body() == body
    assert (
        evaluation.evaluator_id("Correctness") == "Builtin.Correctness"
        and evaluation.evaluator_id("Builtin.X") == "Builtin.X"
    )
    assert evaluation.evaluator_key("Builtin.Harmfulness") == "Harmfulness"


def test_result_value_mapping():
    assert evaluation.result_value({"value": 0.83, "label": "Very helpful"}) == 0.83
    assert evaluation.result_value({"label": "Partially correct"}) == 0.5
    assert evaluation.result_value({"label": "Harmful"}) == 0.0
    assert evaluation.result_value({"value": True}) == 1.0
    assert evaluation.result_value({"errorCode": "SDKError", "errorMessage": "boom"}) is None
    assert evaluation.result_value({"label": "Mysterious"}) is None


NO_SPANS = {
    "errorCode": "SDKError",
    "errorMessage": "An error occurred (ValidationException) when calling the Evaluate operation: Provided input has no spans "
    "to evaluate. Please ensure that spans provided in the request are valid and have model/tool/agent invocation details",
}


def test_unscored_scenario_counts_as_failed_and_blocks_the_release():
    """Seen 2026-10-20: the Evaluate API rejected an incomplete span set for one scenario, all four evaluators returned SDKError
    and the scenario stayed COMPLETED, so it vanished from every mean (8/9 instead of 8/10) with nothing on the verdict to
    show for it. It is UNSCORED now: 0 on the floors, listed in failedScenarios, a `_scenarios` failure."""
    evaluators = ("Correctness", "GoalSuccessRate", "TrajectoryInOrderMatch", "Harmfulness")
    good = [scenario(f"s{i}", **{e: 1.0 for e in evaluators}) for i in range(9)]
    unscored = scenario("s9", **{e: dict(NO_SPANS) for e in evaluators})
    scores, rows = evaluation.summarize(
        {"scenario_results": good + [unscored]}, evaluator_ids=[f"Builtin.{e}" for e in evaluators]
    )
    assert rows[-1]["status"] == "UNSCORED" and rows[-1]["scores"] == {}
    assert rows[-1]["error"].startswith("no evaluator produced a score: An error occurred (ValidationException)")
    assert set(rows[-1]["errors"]) == set(evaluators)
    assert evaluation.failed_scenario_ids(rows) == ["s9"]
    assert scores["GoalSuccessRate"] == {
        "mean": 0.9,
        "min": 0.0,
        "max": 1.0,
        "passRate": 0.9,
        "count": 10,
        "errors": 1,
        "failed": 1,
    }
    assert scores["Harmfulness"]["mean"] == 1.0 and scores["Harmfulness"]["count"] == 9
    thresholds = {"Correctness": 0.8, "GoalSuccessRate": 0.9, "TrajectoryInOrderMatch": 0.9, "Harmfulness": 0.0}
    verdict = evaluation.apply_thresholds(scores, thresholds)
    assert verdict.status == "BLOCKED" and [f["evaluator"] for f in verdict.failures] == ["_scenarios"]
    summary = evaluation.RunSummary("run", "d.json", 10, scores, rows, 1.0, failed_scenarios=["s9"])
    assert [r["scenarioId"] for r in summary.problems()] == ["s9"] and summary.verdict(thresholds).status == "BLOCKED"
    # one errored evaluator next to real scores (a trajectory evaluator without a reference) leaves the scenario COMPLETED
    partial = scenario(
        "s10",
        Correctness=1.0,
        GoalSuccessRate=1.0,
        Harmfulness=1.0,
        TrajectoryInOrderMatch={"errorCode": "EvaluationRequestValidationError", "errorMessage": "no trajectory"},
    )
    _, rows = evaluation.summarize({"scenario_results": [partial]})
    assert rows[0]["status"] == "COMPLETED" and rows[0]["error"] is None and evaluation.failed_scenario_ids(rows) == []
    # no evaluator result at all (every evaluator skipped by the runner) is UNSCORED too
    _, rows = evaluation.summarize({"scenario_results": [scenario("s11")]})
    assert rows[0]["status"] == "UNSCORED" and "no evaluator returned a result" in rows[0]["error"]


def span(trace_id, span_id, operation=None, **attrs):
    """A Logs Insights record as the SDK collector returns it: scope + ids always, gen_ai attributes only on real spans."""
    doc = {"traceId": trace_id, "spanId": span_id, "scope": {"name": "x"}, "attributes": dict(attrs)}
    if operation:
        doc["attributes"]["gen_ai.operation.name"] = operation
    return doc


def test_collect_complete_waits_for_an_invocation_span_and_a_stable_set():
    """Poll 1 sees only a log record with trace context (what the SDK collector would have returned), poll 2 the model and
    tool spans, poll 3 the same set again: poll 3 is returned, after two confirmation sleeps."""
    partial = [span("t1", "a")]
    full = partial + [span("t1", "b", "invoke_agent"), span("t1", "c", "chat"), span("t1", "d", "execute_tool")]
    polls = iter([partial, full, list(full)])
    clock, slept = {"now": 0.0}, []

    def sleep(seconds):
        slept.append(seconds)
        clock["now"] += seconds

    result = evaluation.collect_complete(
        lambda: next(polls), 300, confirm_seconds=10, clock=lambda: clock["now"], sleep=sleep
    )
    assert result == full and slept == [10, 10]
    assert evaluation.is_invocation_span(span("t", "s", "chat")) and not evaluation.is_invocation_span(span("t", "s"))
    assert evaluation.is_invocation_span({"attributes": {"openinference.span.kind": "LLM"}})
    assert not evaluation.is_invocation_span({"attributes": "not a dict"}) and not evaluation.is_invocation_span(None)
    assert evaluation.span_keys(full) == {("t1", "a"), ("t1", "b"), ("t1", "c"), ("t1", "d")}


def test_collect_complete_gives_up_at_the_deadline_and_never_accepts_an_empty_set(caplog):
    partial, calls, clock = [span("t1", "a")], {"n": 0}, {"now": 0.0}

    def fetch():
        calls["n"] += 1
        return list(partial)

    def sleep(seconds):
        clock["now"] += seconds

    with caplog.at_level(logging.WARNING, logger="platform_sdk.evaluation"):
        result = evaluation.collect_complete(
            fetch, 25, confirm_seconds=10, label="s-1", clock=lambda: clock["now"], sleep=sleep
        )
    assert result == partial and calls["n"] == 3  # polls at 0, 10 and 20 s; a fourth would end past the 25 s deadline
    assert "s-1" in caplog.text and "without an invocation span" in caplog.text
    # an empty set is never complete, however stable; the first complete set after it is
    complete = [span("t1", "b", "chat")]
    polls = iter([[], [], complete, list(complete)])
    result = evaluation.collect_complete(
        lambda: next(polls), 300, confirm_seconds=10, clock=lambda: clock["now"], sleep=sleep
    )
    assert result == complete
