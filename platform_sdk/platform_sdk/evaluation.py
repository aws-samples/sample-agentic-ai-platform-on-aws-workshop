"""Dataset evaluation runs and release-gate verdicts (build contract section 5, "score semantics").

`run_dataset` drives the AgentCore dataset runner (`bedrock_agentcore.evaluation`, public preview) against a harness: every
scenario is invoked, telemetry is given `delay_seconds` to land in CloudWatch, then the built-in evaluators score each
scenario. `summarize` folds the result into `scores[evaluator] = {mean, min, passRate, count, errors, failed}` plus compact
per-scenario results, and `apply_thresholds` gives the verdict the platform stores on the release.

A scenario whose invocation failed (timeout, budget_exceeded refusal, missing spans: status FAILED, no evaluator results)
is never dropped silently: it scores 0.0 on every floor evaluator, lowers every passRate, is listed in `failedScenarios`, and
`apply_thresholds` adds a `_scenarios` failure so the release is BLOCKED.

Evaluator scales (read from GetEvaluator on 8 Oct 2026): every built-in evaluator returns `value` in [0, 1] with 1 = good
(Correctness: Incorrect 0 / Partially correct 0.5 / Correct 1; GoalSuccessRate, Trajectory*, ToolSelectionAccuracy: No 0 / Yes 1;
Helpfulness: 7 labels normalised to 0..1; Harmfulness: Harmful 0 / Not Harmful 1). A Harmfulness threshold is therefore an upper
bound on the *harm rate* (`1 - mean`): `Harmfulness: 0.0` means no harmful answer is tolerated.

`bedrock_agentcore` (with pydantic) is imported lazily inside `run_dataset` so that the API Lambda and the SDK's lighter consumers
never load it; the worker bundle ships it (`scripts/build_backend.py`) for the evaluate job, the CLI has it as a dependency.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

BUILTIN_PREFIX = "Builtin."
PASS_VALUE = 0.5  # a scenario "passes" an evaluator when its value is at least 0.5 (1 = good on every built-in scale)
LABEL_VALUES = {
    "yes": 1.0,
    "no": 0.0,
    "correct": 1.0,
    "partially correct": 0.5,
    "incorrect": 0.0,
    "not harmful": 1.0,
    "harmful": 0.0,
    "pass": 1.0,
    "fail": 0.0,
    "true": 1.0,
    "false": 0.0,
}


def evaluator_key(name: str) -> str:
    """`Builtin.Correctness` and `Correctness` both map to `Correctness` (threshold keys may carry either form)."""
    return name[len(BUILTIN_PREFIX) :] if name.startswith(BUILTIN_PREFIX) else name


def evaluator_id(name: str) -> str:
    return name if name.startswith(BUILTIN_PREFIX) or ":" in name else BUILTIN_PREFIX + name


def is_upper_bound(evaluator: str) -> bool:
    """Harmfulness thresholds cap the harm rate (1 - mean); every other threshold is a floor on the mean."""
    return "harmfulness" in evaluator_key(evaluator).lower()


# ---- results ------------------------------------------------------------------------------------------------------
@dataclass
class RunSummary:
    """What a dataset run produced, in the shape `POST /releases/{id}/evaluation` expects."""

    run_id: str
    dataset_name: str
    scenario_count: int
    scores: dict
    results: list
    seconds: float
    evaluator_ids: list = field(default_factory=list)
    raw: dict | None = field(default=None, repr=False)
    failed_scenarios: list = field(default_factory=list)  # scenario ids whose invocation or scoring failed

    def to_api_body(self) -> dict:
        return {
            "runId": self.run_id,
            "datasetName": self.dataset_name,
            "scenarioCount": self.scenario_count,
            "scores": self.scores,
            "results": self.results,
            "failedScenarios": self.failed_scenarios,
        }

    def verdict(self, thresholds: dict) -> Verdict:
        """`apply_thresholds` on this run (failed scenarios included)."""
        return apply_thresholds(self.scores, thresholds, failed_scenarios=len(self.failed_scenarios))

    def table(self) -> list[dict]:
        """Rows for a quick printout: one per evaluator with mean, min, passRate and error count."""
        return [{"evaluator": k, **v} for k, v in self.scores.items()]

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**asdict(self), "raw": self.raw}, indent=1, default=str))
        return path

    @classmethod
    def load(cls, path: str | Path) -> RunSummary:
        """Read a file written by `save` (resubmit a finished run: `client.submit_evaluation(release_id, RunSummary.load(p))`)."""
        data = json.loads(Path(path).read_text())
        known = {f for f in cls.__dataclass_fields__}  # ignore keys a later version may add
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Verdict:
    passed: bool
    failures: list
    details: dict

    def __bool__(self) -> bool:
        return self.passed

    @property
    def status(self) -> str:
        """Release status after the evaluation: PENDING_APPROVAL when every threshold holds, else BLOCKED."""
        return "PENDING_APPROVAL" if self.passed else "BLOCKED"


def _as_dict(result: Any) -> dict:
    if hasattr(result, "model_dump"):
        return result.model_dump()
    if isinstance(result, dict):
        return result
    raise TypeError(f"summarize() expects an EvaluationResult or its dict form, not {type(result).__name__}")


def result_value(entry: dict) -> float | None:
    """Numeric value of one evaluator result; label-only results are mapped with LABEL_VALUES; errors give None."""
    if not isinstance(entry, dict) or entry.get("errorCode") or entry.get("errorMessage"):
        return None
    value = entry.get("value")
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    label = str(entry.get("label") or "").strip().lower()
    return LABEL_VALUES.get(label)


def is_failed_row(row: dict) -> bool:
    """A per-scenario result row whose invocation or scoring failed (status other than COMPLETED, or an error message)."""
    return (row.get("status") or "COMPLETED") != "COMPLETED" or bool(row.get("error"))


def failed_scenario_ids(results: list[dict]) -> list:
    return [row.get("scenarioId") for row in results if is_failed_row(row)]


def summarize(result: Any, evaluator_ids: list[str] | None = None) -> tuple[dict, list]:
    """(scores, results) from a dataset-runner EvaluationResult (or its `model_dump()`).

    scores: {evaluator: {mean, min, max, passRate, count, errors, failed}} where evaluator keys drop the `Builtin.` prefix,
    values are the per-scenario numbers (a scenario with several trace-level results contributes their mean), error results
    (for example a trajectory evaluator without a reference) are excluded from the statistics and counted in `errors`.
    A failed scenario (status != COMPLETED or `error` set) counts as 0.0 on every floor evaluator and as "not passed" in
    every passRate; the Harmfulness harm rate ignores it (no answer is not a harmful answer) and `failed` carries the count.
    `evaluator_ids` names the evaluators of the run (`run_dataset` passes them); without it the evaluators seen in the
    scored scenarios are used. results: [{scenarioId, sessionId, status, scores{evaluator: value}, labels{}, error,
    errors{evaluator: message}}].
    """
    data = _as_dict(result)
    per_evaluator: dict[str, list[float]] = {evaluator_key(e): [] for e in evaluator_ids or []}
    error_counts: dict[str, int] = {}
    results: list[dict] = []
    for scenario in data.get("scenario_results") or []:
        row: dict[str, Any] = {
            "scenarioId": scenario.get("scenario_id"),
            "sessionId": scenario.get("session_id"),
            "status": scenario.get("status") or ("FAILED" if scenario.get("error") else "COMPLETED"),
            "scores": {},
            "labels": {},
            "error": scenario.get("error"),
            "errors": {},
        }
        for evaluator in scenario.get("evaluator_results") or []:
            key = evaluator_key(evaluator.get("evaluator_id", ""))
            per_evaluator.setdefault(key, [])
            error_counts.setdefault(key, 0)
            values: list[float] = []
            labels: list[str] = []
            for entry in evaluator.get("results") or []:
                value = result_value(entry)
                if value is None:
                    error_counts[key] += 1
                    row["errors"][key] = str(
                        entry.get("errorMessage") or entry.get("errorCode") or entry.get("explanation") or "no value"
                    )
                    continue
                values.append(value)
                if entry.get("label"):
                    labels.append(str(entry["label"]))
            if values:
                scenario_value = round(sum(values) / len(values), 4)
                row["scores"][key] = scenario_value
                per_evaluator[key].append(scenario_value)
                if labels:
                    row["labels"][key] = labels[0] if len(labels) == 1 else labels
        results.append(row)

    failed = len(failed_scenario_ids(results))
    scores: dict[str, dict] = {}
    for key, values in per_evaluator.items():
        if failed and not is_upper_bound(key):
            values = values + [0.0] * failed  # a scenario without an answer scores 0 on every floor evaluator
        scored = len(values) + (failed if is_upper_bound(key) else 0)  # passRate denominator: every scenario
        if values:
            scores[key] = {
                "mean": round(sum(values) / len(values), 4),
                "min": round(min(values), 4),
                "max": round(max(values), 4),
                "passRate": round(sum(1 for v in values if v >= PASS_VALUE) / scored, 4),
                "count": len(values),
                "errors": error_counts.get(key, 0),
                "failed": failed,
            }
        else:
            scores[key] = {
                "mean": None,
                "min": None,
                "max": None,
                "passRate": None,
                "count": 0,
                "errors": error_counts.get(key, 0),
                "failed": failed,
            }
    return scores, results


def apply_thresholds(scores: dict, thresholds: dict, failed_scenarios: int | list | None = None) -> Verdict:
    """Compare per-evaluator means with the blueprint thresholds.

    Floors: `mean >= threshold`. Harmfulness: harm rate `1 - mean <= threshold`. Threshold keys and score keys may carry the
    `Builtin.` prefix or not. A threshold for an evaluator with no numeric score (all errors, never run) fails with actual None.
    Failed scenarios block the release: `failed_scenarios` is their count (or the list of ids from `failedScenarios`);
    when not given it is read from the `failed` field `summarize` puts on every score entry.
    """
    normalized_scores = {evaluator_key(k): v for k, v in (scores or {}).items()}
    failures: list[dict] = []
    details: dict[str, dict] = {}
    if failed_scenarios is None:
        failed = max((int((v or {}).get("failed") or 0) for v in normalized_scores.values()), default=0)
    else:
        failed = len(failed_scenarios) if isinstance(failed_scenarios, list) else int(failed_scenarios)
    if failed:
        details["_scenarios"] = {"actual": failed, "threshold": 0, "op": "failed<=", "passed": False}
        failures.append({"evaluator": "_scenarios", "actual": failed, "threshold": 0, "op": "failed<="})
    for raw_name, threshold in (thresholds or {}).items():
        name = evaluator_key(raw_name)
        entry = normalized_scores.get(name) or {}
        mean = entry.get("mean")
        threshold = float(threshold)
        if is_upper_bound(name):
            actual = None if mean is None else round(1 - mean, 4)
            passed = actual is not None and actual <= threshold
            op = "harmRate<="
        else:
            actual = mean
            passed = actual is not None and actual >= threshold
            op = "mean>="
        details[name] = {
            "actual": actual,
            "threshold": threshold,
            "op": op,
            "passed": passed,
            "count": entry.get("count", 0),
            "errors": entry.get("errors", 0),
        }
        if not passed:
            failures.append({"evaluator": name, "actual": actual, "threshold": threshold, "op": op})
    return Verdict(passed=not failures, failures=failures, details=details)


# ---- running ------------------------------------------------------------------------------------------------------
def run_dataset(
    dataset_path: str | Path,
    harness_arn: str,
    log_group_name: str,
    evaluator_ids: list[str],
    qualifier: str | None = None,
    delay_seconds: int = 180,
    max_concurrent: int = 3,
    region: str | None = None,
    invoke_timeout: int = 180,
    span_wait_seconds: int = 300,
    on_turn=None,
) -> RunSummary:
    """Invoke the harness for every scenario of the dataset file, wait for telemetry, score with the evaluators.

    `log_group_name` is the endpoint's log group (`agentcore.harness_log_group(harness, "DEFAULT" | "live")`), `qualifier`
    `"live"` for the Prod endpoint. Expect about 4-6 minutes for 10 scenarios (the 180 s delay included). `on_turn(session_id,
    prompt, result)` is called after each invocation (for printing progress).
    """
    from bedrock_agentcore.evaluation import (  # heavy dependency: only the worker's evaluate job and the CLI reach this line
        AgentInvokerInput,
        AgentInvokerOutput,
        CloudWatchAgentSpanCollector,
        EvaluationRunConfig,
        EvaluatorConfig,
        FileDatasetProvider,
        OnDemandEvaluationDatasetRunner,
    )

    from . import agentcore

    region = region or agentcore.control_client().meta.region_name
    dataset_path = Path(dataset_path)
    evaluator_ids = [evaluator_id(e) for e in evaluator_ids]

    def agent_invoker(inp: AgentInvokerInput) -> AgentInvokerOutput:
        prompt = inp.payload if isinstance(inp.payload, str) else json.dumps(inp.payload)
        result = agentcore.invoke(
            harness_arn, prompt, qualifier=qualifier, session_id=inp.session_id, timeout=invoke_timeout, region=region
        )
        if on_turn:
            on_turn(inp.session_id, prompt, result)
        return AgentInvokerOutput(agent_output=result.text)

    started = time.monotonic()
    dataset = FileDatasetProvider(str(dataset_path)).get_dataset()
    runner = OnDemandEvaluationDatasetRunner(region=region)
    raw = runner.run(
        config=EvaluationRunConfig(
            evaluator_config=EvaluatorConfig(evaluator_ids=evaluator_ids),
            evaluation_delay_seconds=delay_seconds,
            max_concurrent_scenarios=max_concurrent,
        ),
        dataset=dataset,
        agent_invoker=agent_invoker,
        span_collector=CloudWatchAgentSpanCollector(
            log_group_name=log_group_name, region=region, max_wait_seconds=span_wait_seconds
        ),
    )
    scores, results = summarize(raw, evaluator_ids)
    return RunSummary(
        run_id=f"run-{uuid.uuid4().hex[:12]}",
        dataset_name=dataset_path.name,
        scenario_count=len(dataset.scenarios),
        scores=scores,
        results=results,
        seconds=round(time.monotonic() - started, 1),
        evaluator_ids=evaluator_ids,
        raw=raw.model_dump(),
        failed_scenarios=failed_scenario_ids(results),
    )
