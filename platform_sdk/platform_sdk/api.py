"""Platform API client: one method per route of build contract section 4.

    client = api.PlatformClient(cfg, tokens.id_token)
    client.me()
    job = client.wait_for_job(client.invoke("hr-assistant", "dev", "What is my leave balance? I am E1001.")["jobId"])

Errors come back as `PlatformApiError(code, message, status)` built from the contract error shape
`{"error": {"code": "<UPPER_SNAKE>", "message": "..."}}`. Long operations (invoke, deploy, promote) return `{"jobId": ...}`;
`wait_for_job` polls `GET /jobs/{id}` every 2 s.
"""

from __future__ import annotations

import time
from typing import Any

import requests

DEFAULT_TIMEOUT = 30
TERMINAL_JOB_STATES = ("SUCCEEDED", "FAILED")


class PlatformApiError(Exception):
    """The platform API answered with an error (HTTP status >= 400) or an unusable body."""

    def __init__(self, code: str, message: str, status: int | None = None, body: Any = None):
        super().__init__(f"{code} ({status}): {message}" if status else f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
        self.body = body


def _as_list(response: Any, *keys: str) -> list:
    """Accept a bare JSON list or an envelope such as {"domains": [...]} / {"items": [...]} for list routes."""
    if response is None:
        return []
    if isinstance(response, list):
        return response
    if isinstance(response, dict):
        for key in (*keys, "items", "data"):
            if isinstance(response.get(key), list):
                return response[key]
    raise PlatformApiError("BAD_RESPONSE", f"expected a list, got {str(response)[:200]}")


class PlatformClient:
    """Thin `requests` wrapper (timeout 30 s) over the platform API. `cfg_or_base_url` is a PlatformConfig or a URL."""

    def __init__(self, cfg_or_base_url, id_token: str | None = None, timeout: float = DEFAULT_TIMEOUT, session=None):
        base = cfg_or_base_url.api_url if hasattr(cfg_or_base_url, "api_url") else str(cfg_or_base_url)
        self.base_url = base.rstrip("/")
        self.id_token = id_token
        self.timeout = timeout
        self.session = session or requests.Session()

    # ---- plumbing -------------------------------------------------------------------------------------------------
    def _request(self, method: str, path: str, params: dict | None = None, json: Any = None) -> Any:
        headers = {"Accept": "application/json"}
        if self.id_token:
            headers["Authorization"] = f"Bearer {self.id_token}"
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        response = self.session.request(
            method, self.base_url + path, params=params or None, json=json, headers=headers, timeout=self.timeout
        )
        if response.status_code >= 400:
            raise self._error(response)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise PlatformApiError(
                "BAD_RESPONSE", f"non-JSON body: {response.text[:200]}", response.status_code
            ) from exc

    @staticmethod
    def _error(response) -> PlatformApiError:
        body: Any = None
        try:
            body = response.json()
        except ValueError:
            body = response.text
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            code = error.get("code") or f"HTTP_{response.status_code}"
            message = error.get("message") or str(body)
        elif isinstance(body, dict) and "message" in body:  # API Gateway's own errors ({"message": "Unauthorized"})
            code = f"HTTP_{response.status_code}"
            message = str(body["message"])
        else:
            code = f"HTTP_{response.status_code}"
            message = str(body)[:500] or response.reason
        return PlatformApiError(code, message, response.status_code, body)

    def _get(self, path, **params):
        return self._request("GET", path, params=params or None)

    def _post(self, path, body=None):
        return self._request("POST", path, json=body if body is not None else {})

    def _put(self, path, body):
        return self._request("PUT", path, json=body)

    def _patch(self, path, body):
        return self._request("PATCH", path, json=body)

    def _delete(self, path, **params):
        return self._request("DELETE", path, params=params or None)

    # ---- public, me, settings -------------------------------------------------------------------------------------
    def health(self) -> dict:
        return self._get("/health")

    def me(self) -> dict:
        return self._get("/me")

    def get_settings(self) -> dict:
        return self._get("/settings")

    def put_settings(self, settings: dict | None = None, **fields) -> dict:
        """PUT /settings merges the given fields (registryId, policyEngineId, defaultLimits, ...)."""
        return self._put("/settings", {**(settings or {}), **fields})

    # ---- domains and projects -------------------------------------------------------------------------------------
    def list_domains(self) -> list:
        return _as_list(self._get("/domains"), "domains")

    def create_domain(self, domain_id: str, name: str, description: str = "", owner: str = "") -> dict:
        return self._post("/domains", {"domainId": domain_id, "name": name, "description": description, "owner": owner})

    def get_domain(self, domain_id: str) -> dict:
        return self._get(f"/domains/{domain_id}")

    def delete_domain(self, domain_id: str) -> None:
        return self._delete(f"/domains/{domain_id}")

    def list_projects(self, domain_id: str | None = None) -> list:
        return _as_list(self._get("/projects", domainId=domain_id), "projects")

    def create_project(self, project: dict | None = None, **fields) -> dict:
        """POST /projects with {projectId, domainId, name, description, models[], maxBudget, budgetDuration, tpmLimit, rpmLimit,
        allowedToolArns[], knowledgeBaseIds[]}; creates the LiteLLM team and the four project roles."""
        return self._post("/projects", {**(project or {}), **fields})

    def get_project(self, project_id: str) -> dict:
        return self._get(f"/projects/{project_id}")

    def update_project(self, project_id: str, **fields) -> dict:
        return self._patch(f"/projects/{project_id}", fields)

    def delete_project(self, project_id: str, force: bool = False) -> None:
        return self._delete(f"/projects/{project_id}", force="true" if force else None)

    # ---- resources ------------------------------------------------------------------------------------------------
    def list_resources(self, project_id: str, env: str | None = None) -> list:
        return _as_list(self._get(f"/projects/{project_id}/resources", env=env), "resources")

    def register_resource(
        self,
        project_id: str,
        env: str,
        type: str,  # noqa: A002 - mirrors the contract's field name
        name: str,
        arn: str | None = None,
        id: str | None = None,  # noqa: A002
        url: str | None = None,
        attributes: dict | None = None,
    ) -> dict:
        """PUT /projects/{id}/resources upserts one resource reference (type in gateway, gateway_target, policy, memory, harness,
        harness_endpoint, credential_provider, registry_record, online_evaluation, knowledge_base, tool).

        `arn` is optional: gateway targets have none (pass `id=targetId` and `attributes={"gatewayArn": ..., "kind": ...}`)."""
        body: dict = {"env": env, "type": type, "name": name}
        if arn:
            body["arn"] = arn
        if id:
            body["id"] = id
        if url:
            body["url"] = url
        if attributes:
            body["attributes"] = attributes
        return self._put(f"/projects/{project_id}/resources", body)

    def delete_resource(self, project_id: str, env: str, type: str, name: str) -> None:  # noqa: A002
        return self._delete(f"/projects/{project_id}/resources/{env}/{type}/{name}")

    # ---- blueprints and deploy ------------------------------------------------------------------------------------
    def list_blueprints(self, project_id: str) -> list:
        return _as_list(self._get(f"/projects/{project_id}/blueprints"), "blueprints")

    def create_blueprint(self, project_id: str, spec: dict, system_prompt: str, notes: str = "") -> dict:
        """POST /projects/{id}/blueprints -> {"version": n}. `spec` is the parsed blueprint spec, `system_prompt` the resolved text."""
        return self._post(
            f"/projects/{project_id}/blueprints", {"spec": spec, "systemPrompt": system_prompt, "notes": notes}
        )

    def get_blueprint(self, project_id: str, version: int | str) -> dict:
        return self._get(f"/projects/{project_id}/blueprints/{version}")

    def render_blueprint(self, project_id: str, version: int | str, env: str = "dev") -> dict:
        """Dry run: the harness payload the platform would create for this environment (no side effects)."""
        return self._get(f"/projects/{project_id}/blueprints/{version}/render", env=env)

    def deploy(self, project_id: str, version: int | str, env: str = "dev") -> dict:
        """POST /projects/{id}/deploy -> 202 {"jobId"}; the worker renders and creates or updates the Dev harness."""
        return self._post(f"/projects/{project_id}/deploy", {"version": version, "env": env})

    # ---- releases -------------------------------------------------------------------------------------------------
    def list_releases(self, project_id: str) -> list:
        return _as_list(self._get(f"/projects/{project_id}/releases"), "releases")

    def create_release(
        self, project_id: str, blueprint_version: int | str, notes: str = "", auto_run: bool = True
    ) -> dict:
        """POST /projects/{id}/releases. With `auto_run` (the default) the platform deploys the version to Dev when needed, runs
        the evaluation dataset and records the verdict (the release comes back EVALUATING with a `pipeline`); pass
        `auto_run=False` to run the dataset yourself and post it with `submit_evaluation`."""
        return self._post(
            f"/projects/{project_id}/releases",
            {"blueprintVersion": blueprint_version, "notes": notes, "autoRun": auto_run},
        )

    def run_release(self, release_id: str) -> dict:
        """POST /releases/{id}/run -> 202 {"jobId", "stage"}: start the deploy-and-evaluate run of a DRAFT or BLOCKED release."""
        return self._post(f"/releases/{release_id}/run")

    def get_release(self, release_id: str) -> dict:
        return self._get(f"/releases/{release_id}")

    def submit_evaluation(self, release_id: str, evaluation) -> dict:
        """POST /releases/{id}/evaluation with a `RunSummary` (from evaluation.run_dataset) or the equivalent dict
        {runId?, datasetName, scenarioCount, scores{...}, results[...]}; the backend applies the blueprint thresholds."""
        body = evaluation.to_api_body() if hasattr(evaluation, "to_api_body") else evaluation
        return self._post(f"/releases/{release_id}/evaluation", body)

    def approve_release(self, release_id: str, comment: str = "") -> dict:
        return self._post(f"/releases/{release_id}/approve", {"comment": comment})

    def reject_release(self, release_id: str, comment: str = "") -> dict:
        return self._post(f"/releases/{release_id}/reject", {"comment": comment})

    def promote_release(self, release_id: str) -> dict:
        """POST /releases/{id}/promote -> 202 {"jobId"}; the worker renders for prod and updates the Prod harness (a new version,
        which its DEFAULT endpoint follows)."""
        return self._post(f"/releases/{release_id}/promote")

    # ---- keys -----------------------------------------------------------------------------------------------------
    def list_keys(self, project_id: str) -> list:
        return _as_list(self._get(f"/projects/{project_id}/keys"), "keys")

    def create_key(
        self,
        project_id: str,
        env: str,
        model: str,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        adopt: dict | None = None,
    ) -> dict:
        """POST /projects/{id}/keys. Without `adopt` the platform creates the LiteLLM key and the credential provider; with
        `adopt={litellmKeyId, credentialProviderArn, maxBudget, budgetDuration}` it only records a key made by hand (module 5)."""
        body: dict[str, Any] = {"env": env, "model": model}
        if max_budget is not None:
            body["maxBudget"] = max_budget
        if budget_duration is not None:
            body["budgetDuration"] = budget_duration
        if adopt:
            body["adopt"] = adopt
        return self._post(f"/projects/{project_id}/keys", body)

    def update_key(
        self, project_id: str, env: str, model: str, max_budget: float | None = None, status: str | None = None
    ) -> dict:
        """PATCH /projects/{id}/keys/{env}/{model} with maxBudget and/or status ("active" | "suspended")."""
        body: dict[str, Any] = {}
        if max_budget is not None:
            body["maxBudget"] = max_budget
        if status is not None:
            body["status"] = status
        return self._patch(f"/projects/{project_id}/keys/{env}/{model}", body)

    # ---- catalogue and governance ---------------------------------------------------------------------------------
    def list_catalogue(self, status: str | None = None) -> list:
        return _as_list(self._get("/catalogue", status=status), "records")

    def search_catalogue(self, q: str) -> list:
        return _as_list(self._get("/catalogue/search", q=q), "records")

    def get_catalogue_record(self, record_id: str) -> dict:
        """GET /catalogue/{recordId}: one record with its descriptors (also for DRAFT / PENDING_APPROVAL records)."""
        return self._get(f"/catalogue/{record_id}")

    def set_catalogue_status(self, record_id: str, status: str, reason: str = "") -> dict:
        """status: APPROVED | REJECTED | DEPRECATED."""
        return self._post(f"/catalogue/{record_id}/status", {"status": status, "reason": reason})

    def sync_catalogue_agents(self) -> list:
        """POST /catalogue/agents/sync (admin): every project's promoted Prod agent as an approved AGENT record of the registry;
        one outcome per project ({projectId, releaseId?, name, recordId? | skipped | error})."""
        return _as_list(self._post("/catalogue/agents/sync"), "agents")

    def governance(self) -> dict:
        return self._get("/governance")

    def set_gateway_mode(self, gateway_id: str, mode: str) -> dict:
        """mode: LOG_ONLY | ENFORCE."""
        return self._post(f"/governance/gateways/{gateway_id}/mode", {"mode": mode})

    # ---- metrics and spend ----------------------------------------------------------------------------------------
    def metrics(self, project_id: str, env: str, hours: int = 24) -> dict:
        return self._get("/metrics", projectId=project_id, env=env, hours=hours)

    def spend(self, project_id: str, env: str | None = None) -> dict:
        return self._get("/spend", projectId=project_id, env=env)

    # ---- playground (asynchronous jobs) ---------------------------------------------------------------------------
    def invoke(self, project_id: str, env: str, prompt: str, session_id: str | None = None) -> dict:
        """POST /invoke -> 202 {"jobId", "sessionId"}; the worker runs InvokeHarness on the environment's harness (DEFAULT endpoint)."""
        body: dict[str, Any] = {"projectId": project_id, "env": env, "prompt": prompt}
        if session_id:
            body["sessionId"] = session_id
        return self._post("/invoke", body)

    def get_job(self, job_id: str) -> dict:
        return self._get(f"/jobs/{job_id}")

    def wait_for_job(self, job_id: str, timeout: float = 300, poll: float = 2, raise_on_failure: bool = False) -> dict:
        """Poll GET /jobs/{id} until SUCCEEDED or FAILED. Returns the job; raises TimeoutError after `timeout` seconds and
        PlatformApiError(code=job error code or JOB_FAILED) when `raise_on_failure` and the job failed."""
        deadline = time.monotonic() + timeout
        while True:
            job = self.get_job(job_id)
            status = job.get("status")
            if status in TERMINAL_JOB_STATES:
                if status == "FAILED" and raise_on_failure:
                    error = job.get("error")
                    code = error.get("code", "JOB_FAILED") if isinstance(error, dict) else "JOB_FAILED"
                    message = error.get("message", str(error)) if isinstance(error, dict) else str(error)
                    raise PlatformApiError(code, message, None, job)
                return job
            if time.monotonic() >= deadline:
                raise TimeoutError(f"job {job_id} still {status} after {timeout:.0f} s")
            time.sleep(poll)

    def ask(self, project_id: str, env: str, prompt: str, session_id: str | None = None, timeout: float = 300) -> dict:
        """Convenience for the `workshop` command and scripts: invoke, wait, and return the finished job (output.text, output.toolsUsed, output.usage)."""
        accepted = self.invoke(project_id, env, prompt, session_id)
        job = self.wait_for_job(accepted["jobId"], timeout=timeout)
        job.setdefault("sessionId", accepted.get("sessionId"))
        return job

    # ---- feedback -------------------------------------------------------------------------------------------------
    def feedback(self, project_id: str, env: str, session_id: str, rating: str, comment: str = "") -> dict:
        """rating: up | down."""
        return self._post(
            "/feedback",
            {"projectId": project_id, "env": env, "sessionId": session_id, "rating": rating, "comment": comment},
        )

    def list_feedback(self, project_id: str) -> list:
        return _as_list(self._get("/feedback", projectId=project_id), "feedback")
