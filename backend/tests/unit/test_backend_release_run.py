"""The release run (contract section 3): POST /projects/{id}/releases starts the `release` job (Dev deployment when the harness
does not run the version) and the `evaluate` job (the dataset run); POST /releases/{id}/run starts it again. The dataset runner
and the S3 download are replaced; everything else (jobs, harness deploy, keys, verdict) is the real code over the fakes."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from backend_testlib import call
from platform_api import db, jobs, worker_jobs
from platform_api.services import evaluation_run
from test_backend_lifecycle import BLOCKED_SCORES, PASSING_SCORES, create_blueprint, deploy, evaluation_body, run_job

from platform_sdk import evaluation

EVALUATORS = {"Correctness", "GoalSuccessRate", "TrajectoryInOrderMatch", "Harmfulness"}


def summary(scores, failed=()):
    return evaluation.RunSummary(
        run_id="run-worker-1",
        dataset_name="hr-assistant.json",
        scenario_count=10,
        scores=scores,
        results=[{"scenarioId": "leave-balance-e1001", "status": "COMPLETED", "scores": {"Correctness": 1.0}}],
        seconds=301.5,
        evaluator_ids=sorted(f"Builtin.{e}" for e in EVALUATORS),
        failed_scenarios=list(failed),
    )


@pytest.fixture
def dataset_run(monkeypatch):
    """The S3 download and the dataset runner replaced; `result` is what the evaluate job gets, `error` what it raises."""
    state = SimpleNamespace(result=summary(PASSING_SCORES), calls=[], error=None)

    def fake_run(path, harness_arn, log_group, evaluator_ids, **kwargs):
        state.calls.append(
            {
                "path": str(path),
                "harnessArn": harness_arn,
                "logGroup": log_group,
                "evaluators": list(evaluator_ids),
                **kwargs,
            }
        )
        if state.error:
            raise state.error
        return state.result

    monkeypatch.setattr(evaluation_run, "load_dataset", lambda key: Path("/tmp/platform-datasets") / Path(key).name)
    monkeypatch.setattr(evaluation_run, "run_dataset", fake_run)
    return state


def create_release(version=1, **body):
    status, release = call(
        "POST", "/projects/hr-assistant/releases", body={"blueprintVersion": version, **body}, persona="developer"
    )
    assert status == 201, release
    return release


def get_release(release_id):
    return call("GET", f"/releases/{release_id}")[1]


def dev_harness():
    rows = call("GET", "/projects/hr-assistant/resources")[1]
    return next(r for r in rows if r["type"] == "harness" and r["env"] == "dev")


def test_create_release_deploys_to_dev_then_evaluates(ready, dataset_run):
    create_blueprint(ready)
    release = create_release(notes="first")
    assert release["status"] == "EVALUATING" and release["error"] is None and release["evaluation"] is None
    pipeline = release["pipeline"]
    assert pipeline["stage"] == "DEPLOYING" and pipeline["deploySkipped"] is False
    assert pipeline["deployJobId"] == pipeline["jobId"] and pipeline["evaluationJobId"] is None
    assert pipeline["by"] == "developer" and pipeline["startedAt"]
    assert ready.lambda_client.payloads[-1]["Payload"] == {"jobId": pipeline["jobId"]}
    job = jobs.get(pipeline["jobId"])
    assert (
        job["type"] == "release"
        and job["input"]["blueprintVersion"] == 1
        and job["input"]["model"] == "claude-sonnet-5-5"
    )

    # stage one: the Dev harness is created and registered, the evaluate job queued
    result = run_job(pipeline["jobId"])
    assert result["status"] == "SUCCEEDED", result
    assert result["output"]["deploySkipped"] is False and result["output"]["deploy"]["harnessVersion"] == "1"
    evaluate_id = result["output"]["evaluationJobId"]
    harness = dev_harness()
    assert harness["attributes"]["blueprintVersion"] == 1
    after = get_release(release["releaseId"])
    assert after["status"] == "EVALUATING" and after["pipeline"]["stage"] == "EVALUATING"
    assert after["pipeline"]["jobId"] == evaluate_id == after["pipeline"]["evaluationJobId"]
    assert after["pipeline"]["deployJobId"] == pipeline["jobId"] and after["pipeline"]["harnessVersion"] == "1"
    assert (
        after["pipeline"]["harnessArn"] == harness["arn"]
        and after["pipeline"]["logGroup"] == harness["attributes"]["logGroup"]
    )
    assert ready.lambda_client.payloads[-1]["Payload"] == {"jobId": evaluate_id}
    assert (
        jobs.get(evaluate_id)["type"] == "evaluate"
        and jobs.get(evaluate_id)["input"]["deployJobId"] == pipeline["jobId"]
    )

    # stage two: the dataset run against that harness, the verdict on the release
    result = run_job(evaluate_id)
    assert result["status"] == "SUCCEEDED", result
    assert result["output"]["verdict"] == "PASSED" and result["output"]["status"] == "PENDING_APPROVAL"
    assert result["output"]["runId"] == "run-worker-1" and result["output"]["scenarioCount"] == 10
    final = get_release(release["releaseId"])
    assert final["status"] == "PENDING_APPROVAL" and final["error"] is None
    assert final["evaluation"]["source"] == "pipeline" and final["evaluation"]["submittedBy"] == "developer"
    assert final["evaluation"]["runId"] == "run-worker-1" and final["evaluation"]["passed"] is True
    assert final["evaluation"]["thresholds"]["Correctness"] == 0.8
    assert final["pipeline"]["stage"] == "DONE" and final["pipeline"]["runId"] == "run-worker-1"
    assert final["pipeline"]["seconds"] == 301.5 and final["pipeline"]["finishedAt"]
    (run,) = dataset_run.calls
    assert run["path"].endswith("hr-assistant.json") and run["harnessArn"] == harness["arn"]
    assert run["logGroup"] == harness["attributes"]["logGroup"]
    assert {e.removeprefix("Builtin.") for e in run["evaluators"]} == EVALUATORS
    assert run["delay_seconds"] == 180 and run["max_concurrent"] == 3

    # the approver takes over as before
    status, approved = call(
        "POST", f"/releases/{release['releaseId']}/approve", body={"comment": "ok"}, persona="approver"
    )
    assert status == 200 and approved["status"] == "APPROVED"
    assert [r["status"] for r in call("GET", "/releases")[1]] == ["APPROVED"]


def test_deploy_is_skipped_when_the_dev_harness_runs_the_version(ready, dataset_run):
    create_blueprint(ready)
    deploy(ready, 1)
    harness_calls = len([c for c in ready.agentcore.calls if c[0] == "harness"])
    dataset_run.result = summary(BLOCKED_SCORES)
    release = create_release()
    pipeline = release["pipeline"]
    assert pipeline["stage"] == "EVALUATING" and pipeline["deploySkipped"] is True and pipeline["deployJobId"] is None
    assert pipeline["harnessVersion"] == "1" and pipeline["harnessArn"] == dev_harness()["arn"]

    result = run_job(pipeline["jobId"])
    assert (
        result["status"] == "SUCCEEDED"
        and result["output"]["deploySkipped"] is True
        and "deploy" not in result["output"]
    )
    assert len([c for c in ready.agentcore.calls if c[0] == "harness"]) == harness_calls  # no new harness version
    evaluate_id = result["output"]["evaluationJobId"]
    result = run_job(evaluate_id)
    assert result["status"] == "SUCCEEDED" and result["output"]["verdict"] == "BLOCKED"
    blocked = get_release(release["releaseId"])
    assert blocked["status"] == "BLOCKED" and blocked["evaluation"]["passed"] is False
    assert sorted(f["evaluator"] for f in blocked["evaluation"]["failures"]) == [
        "Correctness",
        "GoalSuccessRate",
        "TrajectoryInOrderMatch",
    ]

    # a BLOCKED release can be run again; the previous verdict stays until the new one lands
    status, accepted = call("POST", f"/releases/{release['releaseId']}/run", persona="developer")
    assert status == 202, accepted
    assert accepted["type"] == "release" and accepted["status"] == "EVALUATING" and accepted["stage"] == "EVALUATING"
    again = get_release(release["releaseId"])
    assert again["status"] == "EVALUATING" and again["pipeline"]["jobId"] == accepted["jobId"]
    assert again["evaluation"]["verdict"] == "BLOCKED" and again["error"] is None

    # the superseded evaluate job does nothing when replayed
    assert worker_jobs.run_evaluate(jobs.get(evaluate_id))["skipped"] is True
    assert get_release(release["releaseId"])["status"] == "EVALUATING"
    dataset_run.result = summary(PASSING_SCORES)
    run_job(accepted["jobId"])
    run_job(get_release(release["releaseId"])["pipeline"]["jobId"])
    assert get_release(release["releaseId"])["status"] == "PENDING_APPROVAL"


def test_run_is_refused_while_running_unless_the_job_stalled(ready):
    create_blueprint(ready)
    release = create_release()
    status, body = call("POST", f"/releases/{release['releaseId']}/run", persona="developer")
    assert status == 409 and "EVALUATING" in body["error"]["message"]
    assert call("POST", f"/releases/{release['releaseId']}/run", persona="approver")[0] == 403
    assert call("POST", f"/releases/{release['releaseId']}/approve", body={}, persona="approver")[0] == 409
    assert call("POST", f"/releases/{release['releaseId']}/promote", persona="approver")[0] == 409
    assert call("POST", "/releases/missing/run", persona="developer")[0] == 404

    # the worker died hard: the job never left RUNNING and is older than the worker timeout -> run again
    job_id = release["pipeline"]["jobId"]
    jobs.set_status(job_id, "RUNNING")
    item = db.get(db.job_key(job_id))
    item["updatedAt"] = "2026-10-01T00:00:00Z"
    db.put(item)
    status, accepted = call("POST", f"/releases/{release['releaseId']}/run", persona="developer")
    assert status == 202 and accepted["jobId"] != job_id
    assert get_release(release["releaseId"])["pipeline"]["jobId"] == accepted["jobId"]
    # the old job, replayed by the worker, leaves the release alone
    assert worker_jobs.run_release(jobs.get(job_id))["skipped"] is True
    assert get_release(release["releaseId"])["pipeline"]["jobId"] == accepted["jobId"]


def test_failed_evaluation_returns_the_release_to_draft_and_runs_again(ready, dataset_run):
    create_blueprint(ready)
    deploy(ready, 1)
    release = create_release()
    run_job(release["pipeline"]["jobId"])
    evaluate_id = get_release(release["releaseId"])["pipeline"]["jobId"]
    dataset_run.error = RuntimeError("CloudWatch span collector: no spans after 300 s")
    result = run_job(evaluate_id)
    assert result["status"] == "FAILED" and "no spans" in result["error"]["message"]
    failed = get_release(release["releaseId"])
    assert failed["status"] == "DRAFT" and failed["evaluation"] is None
    assert failed["error"]["stage"] == "EVALUATING" and failed["error"]["jobId"] == evaluate_id
    assert "no spans" in failed["error"]["message"]
    assert failed["pipeline"]["stage"] == "FAILED" and failed["pipeline"]["error"]["jobId"] == evaluate_id
    assert failed["pipeline"]["finishedAt"]

    dataset_run.error = None
    status, accepted = call("POST", f"/releases/{release['releaseId']}/run", persona="developer")
    assert status == 202
    again = get_release(release["releaseId"])
    assert again["status"] == "EVALUATING" and again["error"] is None and again["pipeline"]["stage"] == "EVALUATING"
    assert again["pipeline"]["error"] is None and again["pipeline"]["deploySkipped"] is True
    run_job(accepted["jobId"])
    run_job(get_release(release["releaseId"])["pipeline"]["jobId"])
    assert get_release(release["releaseId"])["status"] == "PENDING_APPROVAL"


def test_failed_deploy_stage_is_reported_against_the_release_job(ready, dataset_run):
    create_blueprint(ready)
    ready.agentcore.fail_on.add("harness")
    release = create_release()
    result = run_job(release["pipeline"]["jobId"])
    assert result["status"] == "FAILED" and "CREATE_FAILED" in result["error"]["message"]
    failed = get_release(release["releaseId"])
    assert failed["status"] == "DRAFT" and failed["error"]["stage"] == "DEPLOYING"
    assert failed["error"]["jobId"] == release["pipeline"]["jobId"] and failed["pipeline"]["stage"] == "FAILED"
    assert not dataset_run.calls
    assert not [j for j in db.query_gsi1("JOB") if j["type"] == "evaluate"]


def test_an_exhausted_dev_key_fails_the_run_before_any_scenario(ready, dataset_run):
    create_blueprint(ready)
    deploy(ready, 1)
    for row in ready.fakes.litellm.keys.values():
        row["spend"] = row["max_budget"]
    release = create_release()
    run_job(release["pipeline"]["jobId"])
    evaluate_id = get_release(release["releaseId"])["pipeline"]["jobId"]
    result = run_job(evaluate_id)
    assert result["status"] == "FAILED" and result["error"]["code"] == "BUDGET_EXCEEDED"
    assert result["error"]["details"]["alias"] == "hr-assistant-dev-claude-sonnet-5-5"
    failed = get_release(release["releaseId"])
    assert failed["status"] == "DRAFT" and failed["error"]["code"] == "BUDGET_EXCEEDED"
    assert not dataset_run.calls


def test_worker_unavailable_leaves_the_release_draft_with_the_error(ready):
    create_blueprint(ready)
    ready.lambda_client.fail = True
    release = create_release()
    assert release["status"] == "DRAFT" and release["error"]["code"] == "WORKER_UNAVAILABLE"
    assert release["pipeline"]["stage"] == "FAILED" and release["pipeline"]["error"]["code"] == "WORKER_UNAVAILABLE"
    assert jobs.get(release["pipeline"]["jobId"])["status"] == "FAILED"
    # POST /run reports the same failure as 502
    status, body = call("POST", f"/releases/{release['releaseId']}/run", persona="developer")
    assert status == 502 and body["error"]["code"] == "WORKER_UNAVAILABLE"
    assert get_release(release["releaseId"])["status"] == "DRAFT"
    ready.lambda_client.fail = False
    assert call("POST", f"/releases/{release['releaseId']}/run", persona="developer")[0] == 202
    assert get_release(release["releaseId"])["status"] == "EVALUATING"


def test_auto_run_false_keeps_the_release_draft_for_a_manual_evaluation(ready):
    create_blueprint(ready)
    before = len(ready.lambda_client.payloads)
    release = create_release(autoRun=False)
    assert release["status"] == "DRAFT" and release["pipeline"] is None and release["error"] is None
    assert len(ready.lambda_client.payloads) == before
    assert not [j for j in db.query_gsi1("JOB") if j["type"] in ("release", "evaluate")]
    status, body = call(
        "POST", "/projects/hr-assistant/releases", body={"blueprintVersion": 1, "autoRun": "yes"}, persona="developer"
    )
    assert status == 400 and "autoRun" in body["error"]["message"]
    status, body = call(
        "POST",
        f"/releases/{release['releaseId']}/evaluation",
        body=evaluation_body(PASSING_SCORES),
        persona="developer",
    )
    assert status == 200 and body["status"] == "PENDING_APPROVAL" and body["evaluation"]["source"] == "api"
    assert body["evaluation"]["submittedBy"] == "developer"


def stall(job_id):
    """The worker died hard: the job never left RUNNING and is older than the worker timeout."""
    jobs.set_status(job_id, "RUNNING")
    item = db.get(db.job_key(job_id))
    item["updatedAt"] = "2026-10-01T00:00:00Z"
    db.put(item)


def test_manual_evaluation_is_refused_while_the_platform_run_is_live_and_supersedes_a_stalled_one(ready):
    create_blueprint(ready)
    release = create_release()
    job_id = release["pipeline"]["jobId"]
    status, body = call(
        "POST",
        f"/releases/{release['releaseId']}/evaluation",
        body=evaluation_body(PASSING_SCORES),
        persona="developer",
    )
    assert status == 409 and "EVALUATING" in body["error"]["message"] and job_id in body["error"]["message"]
    assert get_release(release["releaseId"])["status"] == "EVALUATING"
    # the run stalled: a summary computed elsewhere supersedes it, and the dead run is not shown any more
    stall(job_id)
    status, body = call(
        "POST",
        f"/releases/{release['releaseId']}/evaluation",
        body=evaluation_body(PASSING_SCORES),
        persona="developer",
    )
    assert status == 200 and body["status"] == "PENDING_APPROVAL" and body["evaluation"]["source"] == "api"
    assert body["pipeline"] is None and body["error"] is None
    # the stalled job, replayed by the worker, leaves the release alone
    assert worker_jobs.run_release(jobs.get(job_id))["skipped"] is True
    assert get_release(release["releaseId"])["status"] == "PENDING_APPROVAL"


def test_manual_evaluation_after_a_failed_run_clears_the_error(ready, dataset_run):
    create_blueprint(ready)
    deploy(ready, 1)
    release = create_release()
    run_job(release["pipeline"]["jobId"])
    dataset_run.error = RuntimeError("no spans")
    run_job(get_release(release["releaseId"])["pipeline"]["jobId"])
    failed = get_release(release["releaseId"])
    assert failed["status"] == "DRAFT" and failed["error"]["stage"] == "EVALUATING"
    # `workshop apply 6` finds the DRAFT release and posts its own summary: the failure goes with it
    status, body = call(
        "POST",
        f"/releases/{release['releaseId']}/evaluation",
        body=evaluation_body(BLOCKED_SCORES),
        persona="developer",
    )
    assert status == 200 and body["status"] == "BLOCKED"
    assert body["error"] is None and body["pipeline"] is None and body["evaluation"]["source"] == "api"


def test_release_that_moved_on_during_the_deploy_stage_fails_the_orphan_evaluate_job(ready, dataset_run, monkeypatch):
    from platform_api.handlers import releases as releases_handler
    from platform_api.services import harness as harness_service

    create_blueprint(ready)
    release = create_release()
    real_deploy = harness_service.deploy

    def deploy_then_move_on(project, item, env, by, job_id=None):
        output = real_deploy(project, item, env, by, job_id=job_id)
        moved = releases_handler.load_release(release["releaseId"])  # another actor: a stalled-run override, a cleanup
        releases_handler.save(moved, "DRAFT", pipeline=None)
        return output

    monkeypatch.setattr(worker_jobs.harness_service, "deploy", deploy_then_move_on)
    result = run_job(release["pipeline"]["jobId"])
    assert result["status"] == "FAILED" and result["error"]["code"] == "CONFLICT"
    evaluate_jobs = [j for j in db.query_gsi1("JOB") if j["type"] == "evaluate"]
    assert len(evaluate_jobs) == 1 and evaluate_jobs[0]["status"] == "FAILED"  # not left QUEUED until the TTL
    assert evaluate_jobs[0]["error"]["code"] == "CONFLICT"
    after = get_release(release["releaseId"])
    assert after["status"] == "DRAFT" and after["pipeline"] is None and after["error"] is None  # untouched by fail_run
    assert not dataset_run.calls


def test_watchdog_records_a_timeout_on_the_job_and_the_release(ready, dataset_run, monkeypatch):
    import time as _time

    create_blueprint(ready)
    deploy(ready, 1)
    release = create_release()
    run_job(release["pipeline"]["jobId"])
    evaluate_id = get_release(release["releaseId"])["pipeline"]["jobId"]

    def slow_run(*args, **kwargs):
        _time.sleep(1.5)
        return summary(PASSING_SCORES)

    monkeypatch.setattr(evaluation_run, "run_dataset", slow_run)
    # the invocation has TIMEOUT_MARGIN_SECONDS + 0.5 s left: the watchdog fires after 0.5 s, long before the run ends
    result = jobs.run({"jobId": evaluate_id}, deadline_seconds=jobs.TIMEOUT_MARGIN_SECONDS + 0.5)
    assert result["status"] == "FAILED" and result["error"]["code"] == "TIMEOUT", result
    timed_out = get_release(release["releaseId"])
    assert timed_out["status"] == "DRAFT" and timed_out["error"]["code"] == "TIMEOUT"
    assert timed_out["error"]["stage"] == "EVALUATING" and timed_out["error"]["jobId"] == evaluate_id
    assert timed_out["pipeline"]["stage"] == "FAILED"
    # the late verdict of the killed run was not written over the timeout
    assert timed_out["evaluation"] is None
    assert call("POST", f"/releases/{release['releaseId']}/run", persona="developer")[0] == 202
