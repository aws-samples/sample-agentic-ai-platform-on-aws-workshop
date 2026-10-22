"""POST /invoke (playground): queue an InvokeHarness job for the project's Dev or Prod harness (contract section 4)."""

from __future__ import annotations

import re

from platform_sdk import agentcore, names

from .. import errors, jobs, validation
from . import resources as resources_store
from .projects import load_project

MAX_PROMPT_CHARS = 50_000
SESSION_SUFFIX = "platform"
# InvokeHarness runtimeSessionId (botocore model, 9 Oct 2026): 33-100 characters, pattern [a-zA-Z0-9][a-zA-Z0-9-_]*; the service
# rejects anything else only at invocation time, so the API checks it up front instead of queueing a job that fails 30 s later.
SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{32,99}$")
SESSION_ID_MAX_LENGTH = 100
MODULE_HINTS = {
    "dev": "deploy a blueprint version first (module 6: POST /projects/{id}/deploy)",
    "prod": "promote an approved release first (module 6: POST /releases/{id}/promote)",
}


def deployed_harness(project_id: str, env: str) -> dict:
    """The registered `harness` resource of the environment, or 409 HARNESS_NOT_DEPLOYED with the module hint."""
    n = names.ProjectNames(project_id, env)
    row = resources_store.find_registered(project_id, env, "harness", n.harness)
    if row is None or not row.get("arn"):
        raise errors.conflict(
            f"project {project_id!r} has no {env} harness ({n.harness}) yet: {MODULE_HINTS[env]}",
            code="HARNESS_NOT_DEPLOYED",
        )
    return row


def create(request):
    body = request.require_body()
    project_id = validation.string(body, "projectId")
    project = load_project(project_id)
    env = validation.env(validation.string(body, "env"))
    prompt = validation.string(body, "prompt", max_length=MAX_PROMPT_CHARS)
    session_id = validation.string(body, "sessionId", required=False, max_length=SESSION_ID_MAX_LENGTH)
    if session_id and not SESSION_ID_RE.match(session_id):
        raise errors.validation(
            f"sessionId must be {agentcore.MIN_SESSION_ID_LENGTH}-{SESSION_ID_MAX_LENGTH} characters of letters, digits, '-' or "
            "'_' starting with a letter or digit (InvokeHarness runtimeSessionId); omit it to get one"
        )
    harness = deployed_harness(project["projectId"], env)
    session_id = session_id or agentcore.new_session_id(SESSION_SUFFIX)
    job = jobs.submit(
        "invoke",
        {
            "projectId": project["projectId"],
            "env": env,
            "prompt": prompt,
            "sessionId": session_id,
            "harnessArn": harness["arn"],
            "harnessName": harness["name"],
            "qualifier": names.ProjectNames(project["projectId"], env).invoke_qualifier,
            "model": (harness.get("attributes") or {}).get("model"),
        },
        request.principal.username,
    )
    return 202, {
        "jobId": job["jobId"],
        "sessionId": session_id,
        "status": job["status"],
        "type": "invoke",
        "projectId": project["projectId"],
        "env": env,
    }
