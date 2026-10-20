"""The evaluate job: the release's dataset run against the Dev harness, inside the worker (contract section 3).

`workshop apply 6` runs the same dataset from the learner's machine and posts the summary with POST /releases/{id}/evaluation;
a release created in the UI (or with `autoRun`) is evaluated here instead: the dataset file comes from the scenario dataset bucket
(SSM `scenario/dataset-bucket`, key `spec.evaluation.dataset` of the blueprint), the harness and its log group from the registered
`harness` resource of the Dev environment, the evaluators from the blueprint thresholds, and `platform_sdk.evaluation.run_dataset`
does the work (10 scenarios, 3 at a time, 180 s telemetry delay, then the built-in evaluators: 5 to 6 minutes).

Before the run the Dev key of the blueprint model is checked the way the workshop command does: a suspended or exhausted key
would make every scenario fail and the release BLOCKED for the wrong reason, so the job fails fast with BUDGET_EXCEEDED instead.
"""

from __future__ import annotations

import logging
from pathlib import Path

from platform_sdk import blueprint, evaluation

from .. import db, errors
from ..handlers import resources as resources_store
from . import clients

logger = logging.getLogger(__name__)
EVALUATION_DELAY_SECONDS = (
    180  # telemetry lands in CloudWatch before the evaluators read the spans (same as `workshop apply 6`)
)
MAX_CONCURRENT_SCENARIOS = 3
# Bounds that keep the worst case inside the worker's 15-minute limit: 10 scenarios three at a time are four rounds, so hung
# invocations cost at most 4 x 120 s before the 180 s delay, and missing spans at most 4 x 90 s after it (the two do not stack:
# a failed invocation has no spans to wait for). `workshop apply 6` keeps the SDK defaults (180 s / 300 s) on a laptop.
INVOKE_TIMEOUT_SECONDS = 120
SPAN_WAIT_SECONDS = 90
DATASET_DIR = Path("/tmp/platform-datasets")  # the Lambda's writable disk; the dataset runner reads a file

run_dataset = evaluation.run_dataset  # module attribute so the unit tests can replace the run


def dev_harness(project_id: str) -> dict | None:
    """The registered `harness` resource of the Dev environment (arn, attributes.blueprintVersion, attributes.logGroup)."""
    return resources_store.find_registered(project_id, "dev", "harness")


def harness_runs_version(harness: dict | None, version: int | str) -> bool:
    attributes = (harness or {}).get("attributes") or {}
    return bool(harness) and str(attributes.get("blueprintVersion")) == str(version)


def check_dev_key(project: dict, bp: blueprint.Blueprint) -> dict:
    """The Dev key for the blueprint model exists, is active and has budget left (LiteLLM /key/info, best effort)."""
    project_id = project["projectId"]
    key_item = db.get(db.key_key(project_id, "dev", bp.model_alias))
    if key_item is None:
        raise errors.conflict(
            f"project {project_id!r} has no Dev key for model {bp.model_alias!r}; deploying the blueprint version creates one",
            code="KEY_NOT_FOUND",
        )
    alias = key_item.get("alias")
    if key_item.get("status") != "active":
        raise errors.conflict(
            f"the Dev key {alias} is {key_item.get('status')}: every scenario would be refused; reactivate it in the project's Keys tab",
            code="KEY_SUSPENDED",
        )
    try:
        info = clients.litellm_admin().key_info(key_item["litellmKeyId"])
    except Exception as exc:  # noqa: BLE001 - the run itself decides; the pre-check is a courtesy
        logger.warning("key_info for %s failed before the evaluation: %s", alias, exc)
        return db.strip_keys(key_item)
    max_budget, spend = info.get("max_budget"), info.get("spend")
    details = {
        "alias": alias,
        "maxBudget": max_budget,
        "spend": spend,
        "budgetResetAt": info.get("budget_reset_at"),
        "blocked": bool(info.get("blocked")),
    }
    if info.get("blocked"):
        raise errors.ApiError(
            409, "KEY_BLOCKED", f"the Dev key {alias} is blocked in the model gateway", details=details
        )
    if max_budget is not None and spend is not None and float(spend) >= float(max_budget):
        raise errors.ApiError(
            409,
            "BUDGET_EXCEEDED",
            f"the Dev key {alias} has spent {spend} of its {max_budget} USD cap: every scenario would be refused. Raise the cap in "
            "the project's Keys tab (or wait for the reset), then run the evaluation again",
            details=details,
        )
    return db.strip_keys(key_item)


def load_dataset(key: str) -> Path:
    """Download `key` from the dataset bucket to the Lambda's disk and return the path."""
    bucket = clients.cfg().dataset_bucket
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    destination = DATASET_DIR / Path(key).name
    clients.s3().download_file(bucket, key, str(destination))
    return destination


def run(bp: blueprint.Blueprint, harness: dict, on_turn=None) -> evaluation.RunSummary:
    """Run the blueprint's dataset against the Dev harness; the caller records the summary on the release."""
    dataset_key = bp.dataset
    if not dataset_key:
        raise errors.conflict(
            "the blueprint has no spec.evaluation.dataset; add one (a key in the dataset bucket) and create a new version",
            code="DATASET_NOT_CONFIGURED",
        )
    attributes = harness.get("attributes") or {}
    log_group = attributes.get("logGroup")
    if not harness.get("arn") or not log_group:
        raise errors.conflict(
            "the registered Dev harness has no arn or logGroup attribute; deploy the version again",
            code="HARNESS_NOT_DEPLOYED",
        )
    path = load_dataset(dataset_key)
    return run_dataset(
        path,
        harness["arn"],
        log_group,
        bp.evaluator_ids(),
        delay_seconds=EVALUATION_DELAY_SECONDS,
        max_concurrent=MAX_CONCURRENT_SCENARIOS,
        invoke_timeout=INVOKE_TIMEOUT_SECONDS,
        span_wait_seconds=SPAN_WAIT_SECONDS,
        region=clients.cfg().region,
        on_turn=on_turn,
    )
