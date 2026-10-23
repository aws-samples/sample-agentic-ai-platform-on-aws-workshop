"""The release run on the native release pipeline (CodePipeline V2, QUEUED): Source (the release snapshot, pinned by S3 object
version) -> DeployDev -> Evaluate -> Approve -> Promote. The CodePipeline client is the API-shaped double of backend_testlib
(requests validated against botocore's model) over a versioned moto bucket; its Lambda actions are real worker invocations with
CodePipeline job events. The dataset runner is replaced; jobs, harness deploys, keys, verdicts and approvals are the real code.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import datetime

import pytest
from backend_testlib import PIPELINE_NAME, SOURCE_BUCKET, call, register, run_summary
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from platform_api import db, jobs
from platform_api.handlers import releases as releases_handler
from platform_api.services import clients
from platform_api.services import release_pipeline as pipeline_service
from test_backend_lifecycle import (
    BLOCKED_SCORES,
    PASSING_SCORES,
    create_blueprint,
    deploy,
)

EVALUATORS = {"Correctness", "GoalSuccessRate", "TrajectoryInOrderMatch", "Harmfulness"}
OLD = "2026-10-01T00:00:00Z"  # hours before "now": a job or a start this old is past any 15-minute rule


def create_release(version=1, **body):
    status, release = call(
        "POST",
        "/projects/hr-assistant/releases",
        body={"blueprintVersion": version, **body},
        persona="developer",
    )
    assert status == 201, release
    return release


def get_release(release_id):
    status, release = call("GET", f"/releases/{release_id}")
    assert status == 200, release
    return release


def dev_harness():
    rows = call("GET", "/projects/hr-assistant/resources")[1]
    return next(r for r in rows if r["type"] == "harness" and r["env"] == "dev")


def pending(ready, version=1, **body):
    """A release run through DeployDev and Evaluate: PENDING_APPROVAL, the execution waiting at the Approve stage."""
    release = create_release(version, **body)
    ready.pipeline.advance()
    after = get_release(release["releaseId"])
    assert after["status"] == "PENDING_APPROVAL", after
    return after


def approve(release_id, comment="ok"):
    return call(
        "POST",
        f"/releases/{release_id}/approve",
        body={"comment": comment},
        persona="approver",
    )


def promote(release_id):
    return call("POST", f"/releases/{release_id}/promote", persona="approver")


def snapshot_of(version_id):
    body = clients.s3().get_object(Bucket=SOURCE_BUCKET, Key="release-source/release.zip", VersionId=version_id)["Body"]
    with zipfile.ZipFile(io.BytesIO(body.read())) as archive:
        return {name: json.loads(archive.read(name)) for name in archive.namelist()}


def age(release_id):
    """Every job of the release and its start look hours old (the 15-minute rule of the job-based runner)."""
    release = releases_handler.load_release(release_id)
    pipeline = release["pipeline"]
    for job_id in {
        pipeline.get("jobId"),
        pipeline.get("releaseJobId"),
        pipeline.get("evaluationJobId"),
    } - {None}:
        item = db.get(db.job_key(job_id))
        item.update(createdAt=OLD, updatedAt=OLD)
        db.put(item)
    release["pipeline"]["startedAt"] = OLD
    release["updatedAt"] = OLD
    db.put(release)


# ---- start: the snapshot and the pinned execution ------------------------------------------------------------------------
def test_run_starts_one_execution_pinned_to_an_immutable_snapshot_version(ready):
    item = create_blueprint(ready)
    release = create_release(notes="first")
    assert release["status"] == "EVALUATING" and release["error"] is None and release["evaluation"] is None
    pipeline = release["pipeline"]
    assert pipeline["pipelineName"] == PIPELINE_NAME and pipeline["executionId"].startswith("exec-0001")
    assert pipeline["executionStatus"] == "InProgress" and pipeline["stage"] == "DEPLOYING"
    assert pipeline["sourceBucket"] == SOURCE_BUCKET and pipeline["sourceKey"] == "release-source/release.zip"
    assert pipeline["consoleUrl"].endswith(f"/executions/{pipeline['executionId']}/timeline?region=us-east-1")
    assert pipeline["jobId"] == pipeline["releaseJobId"] == pipeline["deployJobId"] and pipeline["by"] == "developer"
    # no direct worker start: the pipeline runs the job
    assert ready.lambda_client.payloads == []
    job = jobs.get(pipeline["jobId"])
    assert job["type"] == "release" and job["status"] == "QUEUED" and job["input"]["blueprintVersion"] == 1

    ((operation, params),) = [c for c in ready.pipeline.calls if c[0] == "StartPipelineExecution"]
    assert params["name"] == PIPELINE_NAME and params["clientRequestToken"] == pipeline["startToken"]
    assert params["sourceRevisions"] == [
        {
            "actionName": "ReleaseSnapshot",
            "revisionType": "S3_OBJECT_VERSION_ID",
            "revisionValue": pipeline["sourceVersionId"],
        }
    ]
    assert {v["name"]: v["value"] for v in params["variables"]} == {
        "releaseId": release["releaseId"],
        "projectId": "hr-assistant",
        "blueprintVersion": "1",
    }
    snapshot = snapshot_of(pipeline["sourceVersionId"])
    assert snapshot["release.json"]["releaseId"] == release["releaseId"]
    assert snapshot["release.json"]["startToken"] == pipeline["startToken"]
    assert snapshot["blueprint.json"]["version"] == 1 and snapshot["blueprint.json"]["spec"] == item["spec"]
    tags = clients.s3().get_object_tagging(
        Bucket=SOURCE_BUCKET,
        Key="release-source/release.zip",
        VersionId=pipeline["sourceVersionId"],
    )["TagSet"]
    assert {"Key": "auto-delete", "Value": "no"} in tags

    # a second release is a second object version: the first execution still reads its own snapshot
    second = create_release()
    assert second["pipeline"]["sourceVersionId"] != pipeline["sourceVersionId"]
    assert snapshot_of(pipeline["sourceVersionId"])["release.json"]["releaseId"] == release["releaseId"]
    assert (
        ready.pipeline.executions[second["pipeline"]["executionId"]]["revision"]
        == second["pipeline"]["sourceVersionId"]
    )


def test_start_and_approval_requests_have_the_codepipeline_shape(monkeypatch):
    """botocore Stubber on a real client: the exact StartPipelineExecution and GetPipelineState / PutApprovalResult calls."""
    import boto3

    client = boto3.client("codepipeline", region_name="us-east-1")
    monkeypatch.setattr(clients, "codepipeline", lambda: client)
    cfg = pipeline_service.Settings(name=PIPELINE_NAME, bucket=SOURCE_BUCKET)
    release = {
        "releaseId": "01RELEASE",
        "projectId": "hr-assistant",
        "blueprintVersion": 2,
    }
    with Stubber(client) as stub:
        stub.add_response(
            "start_pipeline_execution",
            {"pipelineExecutionId": "11111111-2222-3333-4444-555555555555"},
            {
                "name": PIPELINE_NAME,
                "variables": [
                    {"name": "releaseId", "value": "01RELEASE"},
                    {"name": "projectId", "value": "hr-assistant"},
                    {"name": "blueprintVersion", "value": "2"},
                ],
                "clientRequestToken": "token-1",
                "sourceRevisions": [
                    {
                        "actionName": "ReleaseSnapshot",
                        "revisionType": "S3_OBJECT_VERSION_ID",
                        "revisionValue": "v-9",
                    }
                ],
            },
        )
        stub.add_response(
            "get_pipeline_state",
            {
                "pipelineName": PIPELINE_NAME,
                "stageStates": [
                    {
                        "stageName": "Approve",
                        "latestExecution": {
                            "pipelineExecutionId": "other-execution",
                            "status": "InProgress",
                        },
                        "actionStates": [
                            {
                                "actionName": "ApproveRelease",
                                "latestExecution": {
                                    "status": "InProgress",
                                    "token": "t-other",
                                },
                            }
                        ],
                    }
                ],
            },
            {"name": PIPELINE_NAME},
        )
        stub.add_response(
            "get_pipeline_state",
            {
                "pipelineName": PIPELINE_NAME,
                "stageStates": [
                    {
                        "stageName": "Approve",
                        "latestExecution": {
                            "pipelineExecutionId": "11111111-2222-3333-4444-555555555555",
                            "status": "InProgress",
                        },
                        "actionStates": [
                            {
                                "actionName": "ApproveRelease",
                                "latestExecution": {
                                    "status": "InProgress",
                                    "token": "t-mine",
                                },
                            }
                        ],
                    }
                ],
            },
            {"name": PIPELINE_NAME},
        )
        stub.add_response(
            "put_approval_result",
            {"approvedAt": datetime(2026, 10, 23)},
            {
                "pipelineName": PIPELINE_NAME,
                "stageName": "Approve",
                "actionName": "ApproveRelease",
                "result": {"summary": "approved by approver", "status": "Approved"},
                "token": "t-mine",
            },
        )
        execution_id = pipeline_service.start(cfg, release, "v-9", "token-1")
        assert execution_id == "11111111-2222-3333-4444-555555555555"
        # another execution waits at the stage: its token is never used for this release
        assert pipeline_service.approval_token(cfg, execution_id, "Approve", "ApproveRelease") is None
        token = pipeline_service.approval_token(cfg, execution_id, "Approve", "ApproveRelease")
        pipeline_service.put_approval(cfg, "Approve", "ApproveRelease", token, True, "approved by approver")
        stub.assert_no_pending_responses()


def test_without_a_configured_pipeline_nothing_falls_back(ready, monkeypatch):
    create_blueprint(ready)
    monkeypatch.delenv("RELEASE_PIPELINE_NAME")
    monkeypatch.delenv("RELEASE_SOURCE_BUCKET")
    status, body = call(
        "POST",
        "/projects/hr-assistant/releases",
        body={"blueprintVersion": 1},
        persona="developer",
    )
    assert status == 503 and body["error"]["code"] == "PIPELINE_NOT_CONFIGURED"
    assert call("GET", "/projects/hr-assistant/releases")[1] == []  # nothing stored for a run that cannot start
    draft = create_release(autoRun=False)
    assert draft["status"] == "DRAFT" and draft["pipeline"] is None
    status, body = call("POST", f"/releases/{draft['releaseId']}/run", persona="developer")
    assert status == 503 and body["error"]["code"] == "PIPELINE_NOT_CONFIGURED"
    assert ready.lambda_client.payloads == [] and ready.pipeline.calls == []
    assert get_release(draft["releaseId"])["status"] == "DRAFT"


def test_a_pipeline_that_cannot_start_leaves_the_release_draft(ready, monkeypatch):
    create_blueprint(ready)

    def refuse(**params):
        raise ClientError(
            {"Error": {"Code": "PipelineNotFoundException", "Message": "no pipeline"}},
            "StartPipelineExecution",
        )

    monkeypatch.setattr(ready.pipeline, "start_pipeline_execution", refuse)
    release = create_release()
    assert release["status"] == "DRAFT" and release["error"]["code"] == "PIPELINE_UNAVAILABLE"
    assert "PipelineNotFoundException" in release["error"]["message"]
    assert jobs.get(release["error"]["jobId"])["status"] == "FAILED"
    stored = get_release(release["releaseId"])
    assert stored["status"] == "DRAFT" and stored["pipeline"]["stage"] == "FAILED"
    assert stored["pipeline"]["retryable"] is True and stored["pipeline"]["retryAction"] == "run"


# ---- DeployDev and Evaluate: the gate --------------------------------------------------------------------------------------
def test_pipeline_deploys_dev_evaluates_and_waits_for_the_approver(ready, dataset_run):
    create_blueprint(ready)
    release = create_release()
    pipeline = release["pipeline"]
    ready.pipeline.advance()

    harness = dev_harness()
    assert harness["attributes"]["blueprintVersion"] == 1
    final = get_release(release["releaseId"])
    assert final["status"] == "PENDING_APPROVAL" and final["error"] is None
    assert final["evaluation"]["source"] == "pipeline" and final["evaluation"]["submittedBy"] == "developer"
    assert final["evaluation"]["runId"] == "run-worker-1" and final["evaluation"]["passed"] is True
    assert final["evaluation"]["thresholds"]["Correctness"] == 0.8
    assert final["pipeline"]["stage"] == "DONE" and final["pipeline"]["seconds"] == 301.5
    assert final["pipeline"]["executionId"] == pipeline["executionId"]
    assert final["pipeline"]["executionStatus"] == "InProgress"  # waiting at the Approve stage
    assert final["pipeline"]["retryable"] is False
    release_job, evaluate_job = jobs.get(pipeline["jobId"]), jobs.get(final["pipeline"]["evaluationJobId"])
    assert release_job["status"] == "SUCCEEDED" and release_job["output"]["deploy"]["harnessVersion"] == "1"
    assert evaluate_job["status"] == "SUCCEEDED" and evaluate_job["output"]["verdict"] == "PASSED"
    assert final["pipeline"]["jobId"] == evaluate_job["jobId"]  # the UI follows the evaluate job last
    (run,) = dataset_run.calls
    assert run["harnessArn"] == harness["arn"] and run["logGroup"] == harness["attributes"]["logGroup"]
    assert {e.removeprefix("Builtin.") for e in run["evaluators"]} == EVALUATORS

    # every action reported to CodePipeline exactly once; the execution sits at the approval
    results = [job["result"]["kind"] for job in ready.pipeline.jobs.values()]
    assert results == ["success", "success"]
    execution = ready.pipeline.executions[pipeline["executionId"]]
    assert execution["stage"] == 2 and execution["token"]  # Approve / ApproveRelease waiting


def test_deploy_is_skipped_when_the_dev_harness_runs_the_version(ready, dataset_run):
    create_blueprint(ready)
    deploy(ready, 1)
    calls_before = len(ready.agentcore.calls)
    release = create_release()
    assert release["pipeline"]["deploySkipped"] is True and release["pipeline"]["stage"] == "EVALUATING"
    ready.pipeline.advance()
    assert get_release(release["releaseId"])["status"] == "PENDING_APPROVAL"
    assert len(ready.agentcore.calls) == calls_before  # no second harness version


def test_blocked_verdict_fails_the_evaluate_action_and_the_release_can_run_again(ready, dataset_run):
    create_blueprint(ready)
    dataset_run.result = run_summary(BLOCKED_SCORES)
    release = create_release()
    first_execution = release["pipeline"]["executionId"]
    ready.pipeline.advance()

    blocked = get_release(release["releaseId"])
    assert blocked["status"] == "BLOCKED" and blocked["evaluation"]["passed"] is False
    assert sorted(f["evaluator"] for f in blocked["evaluation"]["failures"]) == [
        "Correctness",
        "GoalSuccessRate",
        "TrajectoryInOrderMatch",
    ]
    execution = ready.pipeline.executions[first_execution]
    assert execution["status"] == "Failed" and execution["failedStage"] == "Validate"
    failure = list(ready.pipeline.jobs.values())[-1]["result"]
    assert failure["kind"] == "failure" and "BLOCKED" in failure["failureDetails"]["message"]
    assert blocked["pipeline"]["executionStatus"] == "Failed"
    assert blocked["pipeline"]["retryable"] is True and blocked["pipeline"]["retryAction"] == "run"
    status, body = approve(release["releaseId"])
    assert status == 409 and "PENDING_APPROVAL" in body["error"]["message"]

    # the fixed prompt passes: a new execution with a new snapshot version
    dataset_run.result = run_summary(PASSING_SCORES)
    status, accepted = call("POST", f"/releases/{release['releaseId']}/run", persona="developer")
    assert status == 202 and accepted["executionId"] != first_execution
    assert accepted["pipelineName"] == PIPELINE_NAME and accepted["executionStatus"] == "InProgress"
    assert jobs.get(accepted["jobId"])["type"] == "release"
    ready.pipeline.advance()
    again = get_release(release["releaseId"])
    assert again["status"] == "PENDING_APPROVAL" and again["pipeline"]["executionId"] == accepted["executionId"]
    assert again["pipeline"]["sourceVersionId"] != blocked["pipeline"]["sourceVersionId"]


def test_failed_evaluation_returns_the_release_to_draft(ready, dataset_run):
    create_blueprint(ready)
    dataset_run.error = RuntimeError("dataset bucket unreachable")
    release = create_release()
    ready.pipeline.advance()
    failed = get_release(release["releaseId"])
    assert failed["status"] == "DRAFT" and failed["evaluation"] is None
    assert failed["error"]["stage"] == "EVALUATING" and "dataset bucket unreachable" in failed["error"]["message"]
    assert ready.pipeline.executions[release["pipeline"]["executionId"]]["failedStage"] == "Validate"
    assert failed["pipeline"]["retryAction"] == "run"


def test_external_evaluations_cannot_bypass_the_gate(ready):
    create_blueprint(ready)
    draft = create_release(autoRun=False)
    assert draft["status"] == "DRAFT" and draft["pipeline"] is None and ready.pipeline.calls == []
    body = {"scores": PASSING_SCORES, "results": [], "failedScenarios": []}
    for persona in ("developer", "admin"):
        status, error = call(
            "POST",
            f"/releases/{draft['releaseId']}/evaluation",
            body=body,
            persona=persona,
        )
        assert status == 409 and error["error"]["code"] == "NATIVE_GATE_REQUIRED"
    assert (
        call(
            "POST",
            f"/releases/{draft['releaseId']}/evaluation",
            body=body,
            persona="approver",
        )[0]
        == 403
    )
    assert get_release(draft["releaseId"])["status"] == "DRAFT"
    status, body = approve(draft["releaseId"])
    assert status == 409


def test_thresholds_missing_fail_the_run(ready, dataset_run):
    create_blueprint(ready)
    stored = db.get(db.blueprint_key("hr-assistant", 1))
    stored["spec"]["evaluation"].pop("thresholds")  # a version stored before thresholds were required
    db.put(stored)
    release = create_release(1)
    ready.pipeline.advance()
    failed = get_release(release["releaseId"])
    # fails closed: the blueprint validator refuses the version before any verdict could pass the gate
    assert failed["status"] == "DRAFT" and failed["error"]["code"] == "BLUEPRINT_INVALID"
    assert "thresholds" in failed["error"]["message"] and failed["evaluation"] is None
    assert (
        ready.pipeline.executions[release["pipeline"]["executionId"]]["failedStage"] == "Validate"
    )  # rendering refuses it


# ---- ownership: queues, stale executions, duplicates, races ------------------------------------------------------------------
def test_a_queued_execution_is_never_stuck_by_age(ready):
    create_blueprint(ready)
    release = create_release()  # not advanced: queued / in progress for hours
    age(release["releaseId"])
    status, body = call("POST", f"/releases/{release['releaseId']}/run", persona="developer")
    assert status == 409 and body["error"]["code"] == "EXECUTION_ACTIVE"
    view = get_release(release["releaseId"])
    assert view["status"] == "EVALUATING" and view["pipeline"]["executionStatus"] == "InProgress"
    assert view["pipeline"]["retryable"] is False and view["pipeline"]["retryAction"] is None
    assert releases_handler.run_stuck(releases_handler.load_release(release["releaseId"])) is False

    # CodePipeline cannot answer: the status is unknown, which is never a reason to start again
    def unavailable(**params):
        raise ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "GetPipelineExecution",
        )

    ready.pipeline.get_pipeline_execution = unavailable
    assert get_release(release["releaseId"])["pipeline"]["executionStatus"] == "Unknown"
    assert call("POST", f"/releases/{release['releaseId']}/run", persona="developer")[0] == 409
    del ready.pipeline.get_pipeline_execution

    # stopped in the console: native status allows the run again, and the release is reconciled on read
    ready.pipeline.stop_pipeline_execution(
        pipelineName=PIPELINE_NAME,
        pipelineExecutionId=release["pipeline"]["executionId"],
        abandon=True,
    )
    view = get_release(release["releaseId"])
    assert view["status"] == "DRAFT" and view["error"]["code"] == "PIPELINE_STOPPED"
    status, accepted = call("POST", f"/releases/{release['releaseId']}/run", persona="developer")
    assert status == 202 and accepted["executionId"] != release["pipeline"]["executionId"]


def test_an_execution_the_release_does_not_follow_changes_nothing(ready, dataset_run):
    create_blueprint(ready)
    release = create_release()
    # someone starts the pipeline by hand with this release's variables (and the snapshot version of the platform's run)
    foreign = ready.pipeline.start_pipeline_execution(
        name=PIPELINE_NAME,
        variables=[
            {"name": "releaseId", "value": release["releaseId"]},
            {"name": "projectId", "value": "hr-assistant"},
            {"name": "blueprintVersion", "value": "1"},
        ],
        clientRequestToken="console-start",
        sourceRevisions=[
            {
                "actionName": "ReleaseSnapshot",
                "revisionType": "S3_OBJECT_VERSION_ID",
                "revisionValue": release["pipeline"]["sourceVersionId"],
            }
        ],
    )["pipelineExecutionId"]
    ready.pipeline.advance()
    assert ready.pipeline.executions[foreign]["status"] == "Failed"
    foreign_results = [j["result"] for j in ready.pipeline.jobs.values() if j["execution"] == foreign]
    assert foreign_results[0]["kind"] == "failure" and "does not own" in foreign_results[0]["failureDetails"]["message"]
    assert get_release(release["releaseId"])["status"] == "PENDING_APPROVAL"  # the platform's own run went on
    assert len(dataset_run.calls) == 1

    # a replay of an old execution's action after the release moved on to a new run: refused, no job runs
    # a replay of the old execution's action after the release moved on to a new run: refused, no job runs
    old_event = next(
        e
        for e in ready.pipeline.events
        if json.loads(e["CodePipeline.job"]["data"]["actionConfiguration"]["configuration"]["UserParameters"])[
            "executionId"
        ]
        == release["pipeline"]["executionId"]
    )
    ready.pipeline.expire(release["pipeline"]["executionId"])
    assert get_release(release["releaseId"])["status"] == "DRAFT"
    status, accepted = call("POST", f"/releases/{release['releaseId']}/run", persona="developer")
    assert status == 202 and accepted["executionId"] != release["pipeline"]["executionId"]
    replay = json.loads(json.dumps(old_event))
    replay["CodePipeline.job"]["id"] = "cp-job-replayed"
    ready.pipeline.jobs["cp-job-replayed"] = {"execution": release["pipeline"]["executionId"], "result": None}
    deploys_before = len(ready.agentcore.calls)
    out = ready.pipeline.deliver(replay)
    assert out["status"] == "FAILURE" and "does not own" in out["message"]
    assert len(ready.agentcore.calls) == deploys_before
    assert jobs.get(accepted["jobId"])["status"] == "QUEUED"  # the new run's job is untouched


def test_a_snapshot_version_other_than_the_pinned_one_is_refused(ready):
    create_blueprint(ready)
    release = create_release()
    execution = ready.pipeline.executions[release["pipeline"]["executionId"]]
    execution["revision"] = clients.s3().put_object(
        Bucket=SOURCE_BUCKET, Key="release-source/release.zip", Body=b"tampered"
    )["VersionId"]
    ready.pipeline.advance()
    result = list(ready.pipeline.jobs.values())[0]["result"]
    assert result["kind"] == "failure" and "is not the snapshot" in result["failureDetails"]["message"]
    assert jobs.get(release["pipeline"]["jobId"])["status"] == "QUEUED"  # never ran


def test_a_duplicate_action_delivery_runs_the_deployment_once(ready, dataset_run):
    create_blueprint(ready)
    release = create_release()
    ready.pipeline.advance()
    harness_calls = [c for c in ready.agentcore.calls if c[0] == "harness"]
    first_event = ready.pipeline.events[0]
    job_id = first_event["CodePipeline.job"]["id"]

    # delivered again after it reported: the platform job is not run again, the result is re-sent (CodePipeline keeps the first)
    out = ready.pipeline.deliver(first_event)
    assert out["status"] == "SUCCESS" and [c for c in ready.agentcore.calls if c[0] == "harness"] == harness_calls

    # delivered again while the first delivery still runs: ignored, the first one reports
    claim = db.get({"PK": f"PIPELINEJOB#{job_id}", "SK": "PIPELINEJOB#first"})
    claim["state"] = "RUNNING"
    db.put(claim)
    assert ready.pipeline.deliver(first_event) == {"status": "DUPLICATE", "pipelineJobId": job_id}

    # the first delivery died without reporting (older than the worker limit): the next delivery takes over and reports
    claim["claimedAt"] = int(claim["claimedAt"]) - 17 * 60
    db.put(claim)
    out = ready.pipeline.deliver(first_event)
    assert out["status"] == "SUCCESS" and [c for c in ready.agentcore.calls if c[0] == "harness"] == harness_calls
    assert get_release(release["releaseId"])["status"] == "PENDING_APPROVAL"


def test_a_result_codepipeline_refuses_is_not_treated_as_reported(ready, monkeypatch):
    create_blueprint(ready)
    create_release()

    def denied(**params):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}}, "PutJobSuccessResult")

    monkeypatch.setattr(ready.pipeline, "put_job_success_result", denied)
    e = ready.pipeline.latest()
    e.update(entered=True, stage=1, action=0, actionStatus=None)  # Validate, DeployDev
    event = ready.pipeline.event(e, "deploy-dev")
    with pytest.raises(ClientError):
        ready.pipeline.deliver(event)
    job_id = event["CodePipeline.job"]["id"]
    assert ready.pipeline.jobs[job_id]["result"] is None
    claim = db.get({"PK": f"PIPELINEJOB#{job_id}", "SK": "PIPELINEJOB#first"})
    assert claim["state"] == "RUNNING"  # not marked reported: a later delivery may report it


@pytest.mark.parametrize(
    "user_parameters",
    [
        None,
        "not json",
        "[1, 2]",
        7,
        json.dumps({"action": "deploy-dev"}),
        json.dumps({"action": "drop-table", "releaseId": "x", "projectId": "y", "executionId": "z"}),
    ],
)
def test_malformed_user_parameters_fail_the_action_safely(ready, user_parameters):
    configuration = {"FunctionName": "agent-platform-worker"}
    if user_parameters is not None:
        configuration["UserParameters"] = user_parameters
    event = {
        "CodePipeline.job": {
            "id": "cp-job-bad",
            "accountId": "123456789012",
            "data": {"actionConfiguration": {"configuration": configuration}},
        }
    }
    ready.pipeline.jobs["cp-job-bad"] = {"execution": None, "result": None}
    out = ready.pipeline.deliver(event)
    assert out["status"] == "FAILURE"
    assert ready.pipeline.jobs["cp-job-bad"]["result"]["kind"] == "failure"


def test_a_continuation_round_is_a_new_delivery_not_a_duplicate(ready, dataset_run, monkeypatch):
    """Even when CodePipeline re-sends the same job id with a continuation token, the round runs."""
    create_blueprint(ready)
    original = pipeline_service.start
    rounds = []

    def start_and_race(cfg, release, version_id, token):
        execution_id = original(cfg, release, version_id, token)
        e = ready.pipeline.executions[execution_id]
        e.update(entered=True, stage=1, action=0, actionStatus=None)
        event = ready.pipeline.event(e, "deploy-dev")
        rounds.append(ready.pipeline.deliver(event))  # first round: the execution is not recorded yet
        job_id = event["CodePipeline.job"]["id"]
        continuation = ready.pipeline.jobs[job_id]["result"]["continuationToken"]
        again = json.loads(json.dumps(event))
        again["CodePipeline.job"]["data"]["continuationToken"] = continuation
        ready.pipeline.jobs[job_id]["result"] = None  # the same job id, a new round
        rounds.append(again)
        return execution_id

    monkeypatch.setattr(pipeline_service, "start", start_and_race)
    release = create_release()
    monkeypatch.setattr(pipeline_service, "start", original)
    assert rounds[0]["status"] == "WAIT"
    out = ready.pipeline.deliver(rounds[1])
    assert out["status"] == "SUCCESS", out
    assert dev_harness()["attributes"]["blueprintVersion"] == 1
    assert jobs.get(release["pipeline"]["jobId"])["status"] == "SUCCEEDED"


def test_reads_after_writes_are_consistent_even_when_the_index_lags(ready, monkeypatch):
    create_blueprint(ready)
    draft = create_release(autoRun=False)
    stale = [dict(item) for item in db.query_gsi1("RELEASE", descending=True)]  # the index as it was: DRAFT
    status, accepted = call("POST", f"/releases/{draft['releaseId']}/run", persona="developer")
    assert status == 202
    monkeypatch.setattr(db, "query_gsi1", lambda *a, **k: [dict(i) for i in stale])
    view = get_release(draft["releaseId"])
    assert view["status"] == "EVALUATING" and view["pipeline"]["executionId"] == accepted["executionId"]
    assert call("GET", "/releases/01UNKNOWN0000000000000000")[0] == 404


def test_an_action_that_arrives_before_the_execution_is_recorded_waits(ready, dataset_run, monkeypatch):
    create_blueprint(ready)
    original = pipeline_service.start

    def start_and_race(cfg, release, version_id, token):
        execution_id = original(cfg, release, version_id, token)
        ready.pipeline.advance()  # CodePipeline is faster than the API's next write
        return execution_id

    monkeypatch.setattr(pipeline_service, "start", start_and_race)
    release = create_release()
    monkeypatch.setattr(pipeline_service, "start", original)
    waits = [j["result"] for j in ready.pipeline.jobs.values() if j["result"].get("continuationToken")]
    assert waits and "waiting for the platform to record execution" in waits[0]["executionDetails"]["summary"]
    ready.pipeline.advance()
    assert get_release(release["releaseId"])["status"] == "PENDING_APPROVAL"
    assert len([c for c in ready.agentcore.calls if c[0] == "harness"]) == 1


def test_an_interrupted_start_resumes_the_same_execution(ready, dataset_run, monkeypatch):
    create_blueprint(ready)

    def lost_answer(
        release, pipeline, execution_id, cfg
    ):  # CodePipeline started it, the API died before writing it down
        raise RuntimeError("Lambda timed out")

    with monkeypatch.context() as patched, pytest.raises(RuntimeError):
        patched.setattr(releases_handler, "_record_execution", lost_answer)
        releases_handler.start_run(
            releases_handler.load_release(create_release(autoRun=False)["releaseId"]),
            "developer",
        )
    (stored,) = call("GET", "/projects/hr-assistant/releases")[1]
    assert stored["status"] == "EVALUATING" and stored["pipeline"]["executionId"] is None
    started = list(ready.pipeline.executions)
    status, accepted = call("POST", f"/releases/{stored['releaseId']}/run", persona="developer")
    assert status == 202 and accepted["executionId"] == started[0]  # the start token returned the same execution
    assert len(ready.pipeline.executions) == 1 and accepted["jobId"] == stored["pipeline"]["jobId"]
    ready.pipeline.advance()
    assert get_release(stored["releaseId"])["status"] == "PENDING_APPROVAL"


def test_a_direct_dev_deploy_cannot_change_the_harness_under_evaluation(ready, dataset_run):
    create_blueprint(ready)
    create_blueprint(ready, version_hint=2, systemPrompt="v2 prompt")
    release = create_release()
    status, body = call(
        "POST",
        "/projects/hr-assistant/deploy",
        body={"version": 2, "env": "dev"},
        persona="developer",
    )
    assert status == 409 and body["error"]["code"] == "EXECUTION_ACTIVE"

    # a deploy that slipped in during the dataset run voids the verdict
    def deploy_during_run(path, harness_arn, log_group, evaluator_ids, **kwargs):
        register(
            "hr-assistant",
            "dev",
            "harness",
            "hr_assistant_dev",
            arn=harness_arn,
            attributes={"version": "99", "blueprintVersion": 2},
        )
        return run_summary()

    from platform_api.services import evaluation_run

    evaluation_run.run_dataset = deploy_during_run
    ready.pipeline.advance()
    failed = get_release(release["releaseId"])
    assert failed["status"] == "DRAFT" and failed["error"]["code"] == "HARNESS_CHANGED"
    assert ready.pipeline.executions[release["pipeline"]["executionId"]]["failedStage"] == "Validate"
    # with no run active, Dev deploys are allowed again
    assert (
        call(
            "POST",
            "/projects/hr-assistant/deploy",
            body={"version": 2, "env": "dev"},
            persona="developer",
        )[0]
        == 202
    )


def test_artifact_credentials_and_events_are_never_logged(ready, dataset_run, caplog):
    create_blueprint(ready)
    caplog.set_level(logging.DEBUG)
    release = create_release()
    ready.pipeline.advance()
    approve(release["releaseId"])
    promote(release["releaseId"])
    ready.pipeline.advance()
    text = caplog.text
    assert "artifact-secret" not in text and "ASIAFAKEARTIFACTKEY" not in text and "artifact-token" not in text
    assert "approval-token-" not in text and "artifactCredentials" not in text
    assert "pipeline job cp-job-0001: action deploy-dev" in text


# ---- approvals and the promotion -------------------------------------------------------------------------------------------
def test_approve_and_promote_answer_the_executions_gates_with_the_platform_record(ready, dataset_run):
    create_blueprint(ready)
    release = pending(ready)
    execution_id = release["pipeline"]["executionId"]
    assert promote(release["releaseId"])[0] == 409  # not approved yet
    assert (
        call(
            "POST",
            f"/releases/{release['releaseId']}/approve",
            body={},
            persona="developer",
        )[0]
        == 403
    )
    assert (
        call(
            "POST",
            f"/releases/{release['releaseId']}/approve",
            body={},
            persona="admin",
        )[0]
        == 403
    )
    status, approved = approve(release["releaseId"], "scores fine")
    assert status == 200 and approved["status"] == "APPROVED"
    assert approved["approval"]["by"] == "approver" and approved["approval"]["executionId"] == execution_id
    assert approved["approval"]["decision"] == "APPROVED" and approved["approval"]["comment"] == "scores fine"
    execution = ready.pipeline.executions[execution_id]
    assert execution["approvals"] == [
        (
            "ApproveRelease",
            "Approved",
            "approved in the platform by approver: scores fine",
        )
    ]
    assert approve(release["releaseId"])[0] == 409
    early = get_release(release["releaseId"])["pipeline"]
    assert early["approvalGate"] == "promotion" and early["approvalReady"] is False  # CodePipeline has not moved on yet
    status, body = promote(release["releaseId"])
    assert status == 409 and body["error"]["code"] == "APPROVAL_NOT_WAITING"
    assert body["error"]["details"]["retryAfterSeconds"] == 5
    assert get_release(release["releaseId"])["status"] == "APPROVED"  # nothing changed, no promote job left behind

    ready.pipeline.advance()  # on to the promotion gate: nothing deploys Prod on its own
    assert get_release(release["releaseId"])["pipeline"]["approvalReady"] is True
    assert execution["stage"] == 3 and execution["token"] and get_release(release["releaseId"])["status"] == "APPROVED"
    assert call("POST", f"/releases/{release['releaseId']}/promote", persona="developer")[0] == 403
    status, accepted = promote(release["releaseId"])
    assert status == 202 and accepted["type"] == "promote" and accepted["executionId"] == execution_id
    assert accepted["pipelineName"] == PIPELINE_NAME
    promoting = get_release(release["releaseId"])
    assert promoting["status"] == "PROMOTING" and promoting["promotion"]["jobId"] == accepted["jobId"]
    assert promoting["promotion"]["by"] == "approver" and promoting["promotion"]["mode"] == "approve"
    assert promote(release["releaseId"])[0] == 409  # already promoting, the execution is in progress

    ready.pipeline.advance()
    done = get_release(release["releaseId"])
    assert done["status"] == "PROMOTED" and done["promotion"]["endpoint"] == "DEFAULT"
    assert jobs.get(accepted["jobId"])["status"] == "SUCCEEDED"
    assert execution["status"] == "Succeeded" and done["pipeline"]["executionStatus"] == "Succeeded"
    assert execution["approvals"][-1] == (
        "ApprovePromotion",
        "Approved",
        "promotion requested in the platform by approver",
    )
    assert done["pipeline"]["retryable"] is False


def test_reject_fails_the_execution_and_ends_the_release(ready, dataset_run):
    create_blueprint(ready)
    release = pending(ready)
    status, body = call(
        "POST",
        f"/releases/{release['releaseId']}/reject",
        body={"comment": "not now"},
        persona="approver",
    )
    assert status == 200 and body["status"] == "REJECTED" and body["approval"]["decision"] == "REJECTED"
    execution = ready.pipeline.executions[release["pipeline"]["executionId"]]
    assert execution["status"] == "Failed" and execution["approvals"][0][1] == "Rejected"
    assert promote(release["releaseId"])[0] == 409
    view = get_release(release["releaseId"])
    assert view["status"] == "REJECTED" and view["pipeline"]["retryable"] is False


def test_a_stale_approval_token_reverts_the_platform_record(ready, dataset_run, monkeypatch):
    create_blueprint(ready)
    release = pending(ready)

    def gone(**params):
        raise ClientError(
            {
                "Error": {
                    "Code": "ApprovalAlreadyCompletedException",
                    "Message": "already answered",
                }
            },
            "PutApprovalResult",
        )

    monkeypatch.setattr(ready.pipeline, "put_approval_result", gone)
    status, body = approve(release["releaseId"])
    assert status == 409 and body["error"]["code"] == "APPROVAL_NOT_WAITING"
    view = get_release(release["releaseId"])
    assert view["status"] == "PENDING_APPROVAL" and view["approval"] is None


def test_approval_before_the_stage_is_reached_is_refused(ready, dataset_run):
    create_blueprint(ready)
    first = pending(ready)
    second = create_release()
    ready.pipeline.advance()  # QUEUED: the second execution waits until the first leaves the Approve stage
    waiting = get_release(second["releaseId"])
    assert waiting["status"] == "PENDING_APPROVAL" and waiting["pipeline"]["executionStatus"] == "InProgress"
    assert waiting["pipeline"]["approvalGate"] == "release" and waiting["pipeline"]["approvalReady"] is False
    status, body = approve(second["releaseId"])
    assert status == 409 and body["error"]["code"] == "APPROVAL_NOT_WAITING"
    assert body["error"]["details"] == {"retryAfterSeconds": 5, "executionStatus": "InProgress"}
    assert get_release(second["releaseId"])["approval"] is None
    ready_view = get_release(first["releaseId"])["pipeline"]
    assert ready_view["approvalGate"] == "release" and ready_view["approvalReady"] is True
    assert "token" not in json.dumps(ready_view).lower().replace("starttoken", "")
    assert approve(first["releaseId"])[0] == 200
    ready.pipeline.advance()
    assert approve(second["releaseId"])[0] == 200


def test_an_expired_approval_returns_the_release_to_draft(ready, dataset_run):
    create_blueprint(ready)
    release = pending(ready)
    ready.pipeline.expire(release["pipeline"]["executionId"])
    status, body = approve(release["releaseId"])
    assert status == 409 and body["error"]["code"] == "APPROVAL_EXPIRED"
    view = get_release(release["releaseId"])
    assert view["status"] == "DRAFT" and view["error"]["code"] == "APPROVAL_EXPIRED"
    assert view["pipeline"]["retryAction"] == "run"
    assert call("POST", f"/releases/{release['releaseId']}/run", persona="developer")[0] == 202


def test_a_console_approval_cannot_promote(ready, dataset_run):
    create_blueprint(ready)
    release = pending(ready)
    execution = ready.pipeline.executions[release["pipeline"]["executionId"]]
    # both gates answered outside the platform
    for stage, action in (
        ("Approve", "ApproveRelease"),
        ("Promote", "ApprovePromotion"),
    ):
        ready.pipeline.put_approval_result(
            pipelineName=PIPELINE_NAME,
            stageName=stage,
            actionName=action,
            result={"summary": "console", "status": "Approved"},
            token=execution["token"],
        )
        ready.pipeline.advance()
    assert execution["status"] == "Failed" and execution["failedStage"] == "Promote"
    result = list(ready.pipeline.jobs.values())[-1]["result"]
    assert "approved the release and asked for the promotion in the platform" in result["failureDetails"]["message"]
    view = get_release(release["releaseId"])
    # the execution ended without the platform's approval: the release goes back to DRAFT, nothing reached Prod
    assert view["status"] == "DRAFT" and view["approval"] is None and view["promotion"] is None
    assert view["error"]["code"] == "APPROVAL_EXPIRED" and "outside the platform" in view["error"]["message"]
    assert not [c for c in ready.agentcore.calls if c[0] == "harness" and "prod" in c[1]]
    assert promote(release["releaseId"])[0] == 409


def test_a_failed_promotion_is_retried_on_the_same_execution(ready, dataset_run):
    create_blueprint(ready)
    release = pending(ready)
    approve(release["releaseId"])
    ready.pipeline.advance()
    ready.agentcore.fail_on.add("harness")
    status, first = promote(release["releaseId"])
    ready.pipeline.advance()
    failed = get_release(release["releaseId"])
    assert failed["status"] == "FAILED" and failed["error"]["jobId"] == first["jobId"]
    assert failed["pipeline"]["executionStatus"] == "Failed" and failed["pipeline"]["retryAction"] == "promote"
    assert jobs.get(first["jobId"])["status"] == "FAILED"

    ready.agentcore.fail_on.clear()
    status, retried = promote(release["releaseId"])
    assert status == 202 and retried["jobId"] != first["jobId"]
    assert [c[0] for c in ready.pipeline.calls].count("RetryStageExecution") == 1
    assert get_release(release["releaseId"])["promotion"]["mode"] == "retry"
    ready.pipeline.advance()
    done = get_release(release["releaseId"])
    assert done["status"] == "PROMOTED" and done["promotion"]["jobId"] == retried["jobId"]
    assert done["promotion"]["attempt"] == 2 and done["pipeline"]["executionStatus"] == "Succeeded"


def test_a_prod_deployment_that_timed_out_is_reconciled_and_retried(ready, dataset_run):
    create_blueprint(ready)
    release = pending(ready)
    approve(release["releaseId"])
    ready.pipeline.advance()
    ready.pipeline.fail_next["PromoteProd"] = "the action timed out"
    status, accepted = promote(release["releaseId"])
    ready.pipeline.advance()
    # the release still says PROMOTING; the execution says Failed: the read reconciles, age plays no part
    assert releases_handler.load_release(release["releaseId"])["status"] == "PROMOTING"
    view = get_release(release["releaseId"])
    assert view["status"] == "FAILED" and view["error"]["code"] == "PIPELINE_FAILED"
    assert view["pipeline"]["retryAction"] == "promote"
    status, retried = promote(release["releaseId"])
    assert status == 202
    ready.pipeline.advance()
    assert get_release(release["releaseId"])["status"] == "PROMOTED"


def test_the_watchdog_reports_a_timeout_to_codepipeline(ready, dataset_run, monkeypatch):
    create_blueprint(ready)
    release = create_release()
    real_run = jobs.run

    def run_out_of_time(event, deadline_seconds=None, on_timeout=None):
        job = jobs.get(event["jobId"])
        error = {"code": "TIMEOUT", "message": "the worker ran out of time"}
        jobs.set_status(job["jobId"], "FAILED", error=error)
        from platform_api import worker_jobs

        worker_jobs.on_run_timeout(job, error)
        on_timeout(job, error)
        return jobs.get(job["jobId"])

    monkeypatch.setattr(jobs, "run", run_out_of_time)
    ready.pipeline.advance()
    monkeypatch.setattr(jobs, "run", real_run)
    (result,) = [j["result"] for j in ready.pipeline.jobs.values()]
    assert result["kind"] == "failure" and "ran out of time" in result["failureDetails"]["message"]
    view = get_release(release["releaseId"])
    assert view["status"] == "DRAFT" and view["error"]["code"] == "TIMEOUT"


def test_runtime_log_groups_get_the_required_tags(ready):
    logs = clients.logs()
    name = "/aws/bedrock-agentcore/runtimes/harness_hr_assistant_dev-RT123-DEFAULT"
    logs.create_log_group(logGroupName=name)
    from platform_api.services import harness as harness_service

    assert harness_service.tag_log_group(name, "hr-assistant", "dev") is True
    arn = logs.describe_log_groups(logGroupNamePrefix=name)["logGroups"][0]["arn"].removesuffix(":*")
    tags = logs.list_tags_for_resource(resourceArn=arn)["tags"]
    assert tags["auto-delete"] == "no" and tags["ProjectId"] == "hr-assistant"
    assert harness_service.tag_log_group("/aws/bedrock-agentcore/runtimes/not-yet", "hr-assistant", "dev") is False


def test_pipeline_jobs_never_expire_while_queued_and_expire_a_day_after_they_finish(ready, dataset_run):
    import time

    create_blueprint(ready)
    first = pending(ready)
    approve(first["releaseId"])
    second = create_release()  # waits behind the first execution's gates, for as long as they take
    queued = db.get(db.job_key(second["pipeline"]["jobId"]))
    assert queued["status"] == "QUEUED" and "ttl" not in queued
    ready.pipeline.advance()
    promote_job = promote(first["releaseId"])[1]
    assert "ttl" not in db.get(db.job_key(promote_job["jobId"]))
    ready.pipeline.advance()
    now = int(time.time())
    for job_id in (first["pipeline"]["releaseJobId"], first["pipeline"]["evaluationJobId"], promote_job["jobId"]):
        item = db.get(db.job_key(job_id))
        assert item["status"] == "SUCCEEDED" and now + 23 * 3600 <= item["ttl"] <= now + 25 * 3600
    # an execution that ends before running its job finishes that job, which then expires a day later
    third = create_release()
    ready.pipeline.stop_pipeline_execution(
        pipelineName=PIPELINE_NAME, pipelineExecutionId=third["pipeline"]["executionId"], abandon=True
    )
    assert get_release(third["releaseId"])["status"] == "DRAFT"
    abandoned = db.get(db.job_key(third["pipeline"]["jobId"]))
    assert abandoned["status"] == "FAILED" and abandoned["error"]["code"] == "PIPELINE_ENDED"
    assert now + 23 * 3600 <= abandoned["ttl"] <= int(time.time()) + 25 * 3600
    # an ordinary job keeps its 24 hours from creation
    deploy_job = jobs.create("deploy", {}, "developer")
    assert db.get(db.job_key(deploy_job["jobId"]))["ttl"] <= int(time.time()) + 24 * 3600


def test_a_log_group_that_cannot_be_tagged_fails_the_run(ready, dataset_run, monkeypatch):
    """Only a group that does not exist yet is skipped; AccessDenied fails the Dev deployment (and so the run)."""
    logs = clients.logs()
    name = "/aws/bedrock-agentcore/runtimes/harness_hr_assistant_dev-RT123-DEFAULT"
    logs.create_log_group(logGroupName=name)

    def denied(**params):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "not authorized"}}, "TagResource")

    monkeypatch.setattr(logs, "tag_resource", denied)
    from platform_api.services import harness as harness_service

    with pytest.raises(ClientError):
        harness_service.tag_log_group(name, "hr-assistant", "dev")
    create_blueprint(ready)
    release = create_release()
    ready.pipeline.advance()
    failed = get_release(release["releaseId"])
    assert failed["status"] == "DRAFT" and "AccessDeniedException" in failed["error"]["message"]
    assert ready.pipeline.executions[release["pipeline"]["executionId"]]["failedStage"] == "Validate"
    assert dataset_run.calls == []  # never evaluated without the tag


def current_action(e: dict) -> str:
    import backend_testlib

    return backend_testlib.PIPELINE_STAGES[e["stage"]][1][e["action"]][0]


def test_a_second_run_cannot_change_dev_until_the_first_evaluation_completes(ready, dataset_run):
    """QUEUED locks the Validate stage (DeployDev, then Evaluate) for one execution: release B waits at its boundary and cannot
    deploy Dev while release A's dataset runs against it."""
    create_blueprint(ready)
    create_blueprint(ready, version_hint=2, systemPrompt="v2 prompt")
    ready.pipeline.paused.add("Evaluate")  # A has deployed Dev and its evaluation has not finished
    first = create_release(1)
    ready.pipeline.advance()
    a = ready.pipeline.executions[first["pipeline"]["executionId"]]
    assert current_action(a) == "Evaluate" and dev_harness()["attributes"]["blueprintVersion"] == 1

    second = create_release(2)  # B runs Source, then queues at Validate
    ready.pipeline.advance()
    b = ready.pipeline.executions[second["pipeline"]["executionId"]]
    assert dev_harness()["attributes"]["blueprintVersion"] == 1  # Dev still runs what A is being evaluated on
    assert (
        b["status"] == "InProgress" and not b["entered"] and current_action(b) == "DeployDev"
    )  # waiting at the boundary
    assert jobs.get(second["pipeline"]["jobId"])["status"] == "QUEUED"
    assert get_release(second["releaseId"])["pipeline"]["retryable"] is False  # queued is not stuck

    ready.pipeline.paused.clear()
    ready.pipeline.advance()
    a_done, b_done = get_release(first["releaseId"]), get_release(second["releaseId"])
    assert a_done["status"] == "PENDING_APPROVAL" and a_done["evaluation"]["passed"] is True
    assert dataset_run.calls[0]["harnessArn"] == dataset_run.calls[1]["harnessArn"]
    assert len(dataset_run.calls) == 2  # A on version 1, then B after deploying version 2
    assert b_done["status"] == "PENDING_APPROVAL" and dev_harness()["attributes"]["blueprintVersion"] == 2
    assert b_done["pipeline"]["approvalReady"] is False  # A holds the Approve stage
