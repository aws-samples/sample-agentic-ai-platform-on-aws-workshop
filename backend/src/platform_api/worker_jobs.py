"""Worker handlers for the asynchronous jobs (contract section 4): deploy, promote, invoke, release and evaluate.

Importing this module registers the handlers in `jobs.HANDLERS`; `backend/src/worker.py` does that at start-up. Every handler
takes the JOB item and returns the `output` dict; exceptions become the job's `error` through `errors.from_exception`.

A release run is two jobs so that neither comes near the worker's 15-minute timeout: `release` deploys the blueprint version to
Dev when the Dev harness does not run it yet (a minute; four to five for a project's first harness) and queues `evaluate`, which
runs the dataset (five to six minutes) and records the verdict. Both check that the release still follows them
(`pipeline.jobId`): a run started again after a stall supersedes the old jobs, which then do nothing. Should a job still reach
the Lambda limit, `jobs.run`'s watchdog records a TIMEOUT on the job and, through `on_run_timeout`, on the release.
"""

from __future__ import annotations

import logging
import time

from platform_sdk import agentcore

from . import db, errors, jobs
from .handlers import blueprints, catalogue, projects, releases
from .services import clients, evaluation_run
from .services import harness as harness_service

logger = logging.getLogger(__name__)
INVOKE_TIMEOUT_SECONDS = 180


def run_deploy(job: dict) -> dict:
    inp = job["input"]
    project = projects.load_project(inp["projectId"])
    item = blueprints.load_blueprint(project["projectId"], inp["version"])
    return harness_service.deploy(project, item, inp.get("env") or "dev", job["createdBy"], job_id=job["jobId"])


def run_promote(job: dict) -> dict:
    inp = job["input"]
    release = db.get(db.release_key(inp["projectId"], inp["releaseId"]))
    if release is None:
        raise errors.not_found(f"release {inp['releaseId']!r} of project {inp['projectId']!r} does not exist")
    project = projects.load_project(inp["projectId"])
    item = blueprints.load_blueprint(project["projectId"], release["blueprintVersion"])
    try:
        output = harness_service.promote(project, item, job["createdBy"], job_id=job["jobId"])
    except Exception as exc:
        api_error = errors.from_exception(exc)
        logger.exception("promotion of release %s failed", release["releaseId"])
        releases.save(release, "FAILED", error={**api_error.as_job_error(), "jobId": job["jobId"]})
        raise
    promotion = {
        **(release.get("promotion") or {}),
        "harnessVersion": output["harnessVersion"],
        "harnessArn": output["harnessArn"],
        "endpoint": output["endpoint"],
        "onlineEvaluationConfigId": output.get("onlineEvaluationConfigId"),
        "at": db.now_iso(),
        "jobId": job["jobId"],
    }
    # Prod is live: say so first (one put, as before), then the catalogue entry, which may poll the registry for a while
    releases.save(release, "PROMOTED", promotion=promotion, error=None)
    # the Catalogue's Agents tab: the Prod agent becomes an approved AGENT record (best effort, the agent is live already;
    # POST /catalogue/agents/sync repeats this step for every project when it was skipped or failed)
    promotion["registryRecord"] = catalogue.register_promoted_agent(project, item, release, promotion, job["createdBy"])
    output["registryRecord"] = promotion["registryRecord"]
    releases.save(release, promotion=promotion)
    return output


# ---- release run: deploy stage, then the evaluation ----------------------------------------------------------------------
def _release_following(job: dict) -> dict | None:
    """The release of a `release` / `evaluate` job, or None when it no longer follows this job (superseded or moved on)."""
    inp = job["input"]
    release = db.get(db.release_key(inp["projectId"], inp["releaseId"]))
    if release is None:
        raise errors.not_found(f"release {inp['releaseId']!r} of project {inp['projectId']!r} does not exist")
    pipeline = release.get("pipeline") or {}
    if release.get("status") != "EVALUATING" or pipeline.get("jobId") != job["jobId"]:
        logger.warning(
            "job %s (%s): release %s is %s and follows job %s; nothing to do",
            job["jobId"],
            job["type"],
            release["releaseId"],
            release.get("status"),
            pipeline.get("jobId"),
        )
        return None
    return release


def run_release(job: dict) -> dict:
    """Stage one of a release run: deploy the blueprint version to Dev unless the Dev harness runs it, then queue `evaluate`."""
    release = _release_following(job)
    if release is None:
        return {"skipped": True, "reason": "the release no longer follows this job"}
    inp = job["input"]
    project = projects.load_project(inp["projectId"])
    item = blueprints.load_blueprint(project["projectId"], inp["blueprintVersion"])
    pipeline = dict(release.get("pipeline") or {})
    stage = "DEPLOYING"
    output: dict = {"deploySkipped": True}
    try:
        harness = evaluation_run.dev_harness(project["projectId"])
        if evaluation_run.harness_runs_version(harness, item["version"]):
            attributes = harness.get("attributes") or {}
            pipeline.update(
                deploySkipped=True,
                harnessVersion=attributes.get("version"),
                harnessArn=harness.get("arn"),
                logGroup=attributes.get("logGroup"),
            )
        else:
            pipeline.update(stage="DEPLOYING", deployJobId=job["jobId"], deploySkipped=False)
            releases.save(release, expected_status="EVALUATING", pipeline=pipeline)
            deployed = harness_service.deploy(project, item, "dev", job["createdBy"], job_id=job["jobId"])
            output = {"deploySkipped": False, "deploy": deployed}
            pipeline.update(
                harnessVersion=deployed.get("harnessVersion"),
                harnessArn=deployed.get("harnessArn"),
                logGroup=deployed.get("logGroup"),
            )
        stage = "EVALUATING"
        evaluate = jobs.create("evaluate", {**inp, "deployJobId": job["jobId"]}, job["createdBy"])
        pipeline.update(stage="EVALUATING", jobId=evaluate["jobId"], evaluationJobId=evaluate["jobId"])
        try:
            releases.save(release, expected_status="EVALUATING", pipeline=pipeline)
        except errors.ApiError as exc:  # the release moved on while Dev was deploying: this evaluate job never runs
            jobs.set_status(evaluate["jobId"], "FAILED", error={"code": "CONFLICT", "message": exc.message})
            raise
        try:
            jobs.enqueue(evaluate)
        except Exception as exc:
            api_error = errors.from_exception(exc)
            jobs.set_status(evaluate["jobId"], "FAILED", error=api_error.as_job_error())
            raise errors.upstream(
                f"the worker could not be started for the evaluation: {api_error.message}", code="WORKER_UNAVAILABLE"
            ) from exc
    except Exception as exc:
        api_error = errors.from_exception(exc)
        logger.exception("run of release %s failed in stage %s", release["releaseId"], stage)
        release["pipeline"] = {**pipeline, "jobId": job["jobId"]}  # the failure is reported against this job
        releases.fail_run(release, api_error.as_job_error(), job["jobId"], stage)
        raise
    output["evaluationJobId"] = evaluate["jobId"]
    return output


def run_evaluate(job: dict) -> dict:
    """Stage two: the dataset run against the Dev harness; the verdict moves the release to BLOCKED or PENDING_APPROVAL."""
    release = _release_following(job)
    if release is None:
        return {"skipped": True, "reason": "the release no longer follows this job"}
    inp = job["input"]
    project = projects.load_project(inp["projectId"])
    item = blueprints.load_blueprint(project["projectId"], inp["blueprintVersion"])
    started = time.monotonic()
    try:
        bp = harness_service.blueprint_from_item(project, item)
        harness = evaluation_run.dev_harness(project["projectId"])
        if not evaluation_run.harness_runs_version(harness, item["version"]):
            running = ((harness or {}).get("attributes") or {}).get("blueprintVersion")
            raise errors.conflict(
                f"the Dev harness runs blueprint version {running}, the release needs {item['version']}; run the release again",
                code="HARNESS_NOT_DEPLOYED",
            )
        evaluation_run.check_dev_key(project, bp)
        summary = evaluation_run.run(bp, harness)
        pipeline = {
            **(release.get("pipeline") or {}),
            "stage": "DONE",
            "finishedAt": db.now_iso(),
            "runId": summary.run_id,
            "seconds": summary.seconds,
        }
        updated = releases.record_evaluation(
            release, summary.to_api_body(), job["createdBy"], source="pipeline", pipeline=pipeline, error=None
        )
    except Exception as exc:
        api_error = errors.from_exception(exc)
        logger.exception("evaluation of release %s failed", release["releaseId"])
        releases.fail_run(release, api_error.as_job_error(), job["jobId"], "EVALUATING")
        raise
    return {
        "runId": summary.run_id,
        "datasetName": summary.dataset_name,
        "scenarioCount": summary.scenario_count,
        "verdict": updated["evaluation"]["verdict"],
        "status": updated["status"],
        "seconds": summary.seconds,
        "wallSeconds": round(time.monotonic() - started, 1),
        "scores": summary.scores,
        "failedScenarios": summary.failed_scenarios,
    }


def budget_details(inp: dict, message: str) -> dict:
    """Cap and spend of the agent key behind a budget_exceeded refusal (LiteLLM /key/info plus the numbers in the message)."""
    details: dict = {"projectId": inp.get("projectId"), "env": inp.get("env"), "model": inp.get("model")}
    details.update({k: v for k, v in agentcore.parse_budget_message(message).items() if k != "budgetExceeded"})
    key_item = (
        db.get(db.key_key(inp["projectId"], inp["env"], inp["model"])) if inp.get("model") and inp.get("env") else None
    )
    if key_item:
        details["alias"] = key_item.get("alias")
        details["maxBudget"] = key_item.get("maxBudget")
        details["budgetDuration"] = key_item.get("budgetDuration")
        try:
            info = clients.litellm_admin().key_info(key_item["litellmKeyId"])
            details.update(
                maxBudget=info.get("max_budget", details.get("maxBudget")),
                spend=info.get("spend"),
                budgetResetAt=info.get("budget_reset_at"),
                blocked=bool(info.get("blocked")),
            )
        except Exception as exc:  # noqa: BLE001 - the refusal itself is the result; the details are best effort
            logger.warning("key_info for %s failed: %s", key_item.get("alias"), exc)
    return details


def run_invoke(job: dict) -> dict:
    inp = job["input"]
    started = time.monotonic()
    try:
        result = agentcore.invoke(
            inp["harnessArn"],
            inp["prompt"],
            qualifier=inp.get("qualifier") or None,
            session_id=inp["sessionId"],
            timeout=INVOKE_TIMEOUT_SECONDS,
            region=clients.cfg().region,
        )
    except agentcore.HarnessInvokeError as exc:
        if exc.budget_exceeded:
            raise errors.ApiError(
                429, "BUDGET_EXCEEDED", exc.message, details=budget_details(inp, exc.message)
            ) from exc
        raise
    return {
        **result.summary(),
        "seconds": round(time.monotonic() - started, 1),
        "modelCalls": result.usage.get("modelCalls"),
        "harnessArn": inp["harnessArn"],
        "qualifier": inp.get("qualifier"),
        "env": inp.get("env"),
        "projectId": inp.get("projectId"),
    }


def on_run_timeout(job: dict, error: dict) -> None:
    """The worker Lambda is about to be killed (15 minutes): the release the job owns goes back to DRAFT with a TIMEOUT error
    instead of staying EVALUATING without a word until the stuck rule offers "Run again"."""
    release = _release_following(job)
    if release is None:
        return
    stage = (release.get("pipeline") or {}).get("stage") or ("DEPLOYING" if job["type"] == "release" else "EVALUATING")
    releases.fail_run(release, error, job["jobId"], stage)


def register() -> dict:
    """Register (idempotently) the handlers in `jobs.HANDLERS`; called at import and by the worker entry."""
    jobs.HANDLERS.update(
        {
            "deploy": run_deploy,
            "promote": run_promote,
            "invoke": run_invoke,
            "release": run_release,
            "evaluate": run_evaluate,
        }
    )
    jobs.TIMEOUT_HANDLERS.update({"release": on_run_timeout, "evaluate": on_run_timeout})
    return jobs.HANDLERS


register()
