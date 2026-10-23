"""The module 6 release pipeline: CodePipeline V2 `agent-platform-release` (QUEUED) with the stages Source ->
Validate (DeployDev then Evaluate, under one stage lock) -> Approve (manual) -> Promote (manual, then the Prod deployment).

Every release run is one execution. The backend writes an immutable snapshot of the release (release.json + the stored blueprint
version) as a new object version of the one source key and starts the execution pinned to that version id
(`sourceRevisions` S3_OBJECT_VERSION_ID) with the pipeline variables releaseId, projectId and blueprintVersion. The Lambda actions
run in the worker (`platform_api.pipeline_actions`); the two manual approvals are answered by the API on behalf of the approver
(`PutApprovalResult` with the token of the release's own execution), so the application's approval record always comes first.

The pipeline is configured through the environment the Platform stack sets on both Lambdas (RELEASE_PIPELINE_NAME,
RELEASE_SOURCE_BUCKET); without it a run is refused with 503 PIPELINE_NOT_CONFIGURED: there is no fallback to another runner.
"""

from __future__ import annotations

import io
import json
import logging
import os
import zipfile
from dataclasses import dataclass
from urllib.parse import quote

from botocore.exceptions import ClientError

from platform_sdk import names
from platform_sdk import tags as tag_rules

from .. import db, errors
from . import clients

logger = logging.getLogger(__name__)
STARTING = "Starting"  # the platform's own status before CodePipeline has answered StartPipelineExecution
UNKNOWN = "Unknown"  # GetPipelineExecution failed or does not know the execution: never treated as finished
ACTIVE_STATUSES = ("InProgress", "Stopping")
RETRYABLE_STATUSES = ("Failed", "Stopped", "Superseded", "Cancelled")
TERMINAL_STATUSES = ("Succeeded", *RETRYABLE_STATUSES)
APPROVAL_GONE_CODES = (
    "InvalidApprovalTokenException",
    "ApprovalAlreadyCompletedException",
    "ActionNotFoundException",
)
RETRY_REFUSED_CODES = (
    "StageNotRetryableException",
    "NotLatestPipelineExecutionException",
    "ConflictException",
)
SUMMARY_MAX = 512  # PutApprovalResult result.summary
FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)  # the snapshot bytes depend on the release only


@dataclass(frozen=True)
class Settings:
    name: str
    bucket: str
    key: str = names.RELEASE_SOURCE_KEY


def settings() -> Settings:
    """The pipeline the Platform stack configured; 503 PIPELINE_NOT_CONFIGURED otherwise."""
    name = os.environ.get("RELEASE_PIPELINE_NAME") or clients.cfg().get("release-pipeline/name")
    bucket = os.environ.get("RELEASE_SOURCE_BUCKET") or clients.cfg().get("release-pipeline/source-bucket")
    if not name or not bucket:
        raise errors.ApiError(
            503,
            "PIPELINE_NOT_CONFIGURED",
            "the release pipeline is not configured (RELEASE_PIPELINE_NAME / RELEASE_SOURCE_BUCKET are set by the Platform "
            "stack): deploy AgentPlatform-Platform first",
        )
    return Settings(name=name, bucket=bucket)


def snapshot(release: dict, blueprint_item: dict, start_token: str) -> bytes:
    """The release snapshot (a ZIP with release.json and blueprint.json); deterministic for the same inputs."""
    manifest = {
        "releaseId": release["releaseId"],
        "projectId": release["projectId"],
        "blueprintVersion": int(release["blueprintVersion"]),
        "startToken": start_token,
        "requestedBy": (release.get("pipeline") or {}).get("by"),
    }
    blueprint = {k: v for k, v in db.strip_keys(blueprint_item).items() if k not in ("ttl",)}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in (
            ("release.json", manifest),
            ("blueprint.json", blueprint),
        ):
            info = zipfile.ZipInfo(name, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, json.dumps(content, sort_keys=True, indent=2, default=str))
    return buffer.getvalue()


def upload_snapshot(cfg: Settings, release: dict, blueprint_item: dict, start_token: str) -> str:
    """Write the snapshot as a new version of the source key; returns the S3 version id the execution is pinned to."""
    response = clients.s3().put_object(
        Bucket=cfg.bucket,
        Key=cfg.key,
        Body=snapshot(release, blueprint_item, start_token),
        ContentType="application/zip",
        Metadata={
            "release-id": release["releaseId"],
            "project-id": release["projectId"],
            "blueprint-version": str(release["blueprintVersion"]),
        },
        Tagging="&".join(f"{quote(k)}={quote(v)}" for k, v in tag_rules.REQUIRED_TAGS.items()),
    )
    version_id = response.get("VersionId")
    if not version_id or version_id == "null":
        raise errors.ApiError(
            500,
            "PIPELINE_MISCONFIGURED",
            f"s3://{cfg.bucket}/{cfg.key} returned no object version: the release source bucket must be versioned",
        )
    return version_id


def start(cfg: Settings, release: dict, version_id: str, start_token: str) -> str:
    """StartPipelineExecution pinned to the snapshot version; the start token makes a repeated call return the same execution."""
    response = clients.codepipeline().start_pipeline_execution(
        name=cfg.name,
        variables=[
            {"name": "releaseId", "value": release["releaseId"]},
            {"name": "projectId", "value": release["projectId"]},
            {"name": "blueprintVersion", "value": str(release["blueprintVersion"])},
        ],
        clientRequestToken=start_token,
        sourceRevisions=[
            {
                "actionName": names.RELEASE_SOURCE_ACTION,
                "revisionType": "S3_OBJECT_VERSION_ID",
                "revisionValue": version_id,
            }
        ],
    )
    return response["pipelineExecutionId"]


def execution_status(cfg: Settings, execution_id: str) -> str:
    """The execution's CodePipeline status; UNKNOWN when CodePipeline does not know it (never a reason to retry)."""
    try:
        execution = clients.codepipeline().get_pipeline_execution(
            pipelineName=cfg.name, pipelineExecutionId=execution_id
        )["pipelineExecution"]
    except ClientError as exc:
        if (exc.response.get("Error") or {}).get("Code") == "PipelineExecutionNotFoundException":
            return UNKNOWN
        raise
    return execution.get("status") or UNKNOWN


def approval_token(cfg: Settings, execution_id: str, stage: str, action: str) -> str | None:
    """The token of the manual approval `stage/action` when it is waiting for exactly this execution, else None."""
    state = clients.codepipeline().get_pipeline_state(name=cfg.name)
    for stage_state in state.get("stageStates") or []:
        if stage_state.get("stageName") != stage:
            continue
        latest = stage_state.get("latestExecution") or {}
        if latest.get("pipelineExecutionId") != execution_id or latest.get("status") != "InProgress":
            return None
        for action_state in stage_state.get("actionStates") or []:
            run = action_state.get("latestExecution") or {}
            if action_state.get("actionName") == action and run.get("status") == "InProgress" and run.get("token"):
                return run["token"]
        return None
    return None


def put_approval(cfg: Settings, stage: str, action: str, token: str, approved: bool, summary: str) -> None:
    clients.codepipeline().put_approval_result(
        pipelineName=cfg.name,
        stageName=stage,
        actionName=action,
        result={
            "summary": summary[:SUMMARY_MAX],
            "status": "Approved" if approved else "Rejected",
        },
        token=token,
    )


def retry_promotion(cfg: Settings, execution_id: str) -> None:
    """Run the failed Prod deployment action of the execution again (the promotion approval stays answered)."""
    clients.codepipeline().retry_stage_execution(
        pipelineName=cfg.name,
        stageName=names.RELEASE_PROMOTE_STAGE,
        pipelineExecutionId=execution_id,
        retryMode="FAILED_ACTIONS",
    )


def stop(cfg: Settings, execution_id: str, reason: str) -> None:
    """Abandon an execution that no release follows any more (best effort: it may have finished already)."""
    try:
        clients.codepipeline().stop_pipeline_execution(
            pipelineName=cfg.name,
            pipelineExecutionId=execution_id,
            abandon=True,
            reason=reason[:200],
        )
    except ClientError as exc:
        logger.warning(
            "could not stop execution %s: %s",
            execution_id,
            (exc.response.get("Error") or {}).get("Code"),
        )


def console_url(region: str, cfg: Settings, execution_id: str) -> str:
    return (
        f"https://{region}.console.aws.amazon.com/codesuite/codepipeline/pipelines/{cfg.name}/executions/"
        f"{execution_id}/timeline?region={region}"
    )


def error_code(exc: BaseException) -> str | None:
    if isinstance(exc, ClientError):
        return (exc.response.get("Error") or {}).get("Code")
    return None
