"""Test doubles for the backend unit tests (not a pytest plugin)."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError
from platform_sdk.agentcore import AgentCoreError, InvokeResult

SRC = Path(__file__).resolve().parents[1] / "src"
ACCOUNT = "123456789012"
REGION = "us-east-1"
# the approved aliases, mirroring foundation.config.MODELS (four aliases, three vendors; hr-assistant enables two of them)
MODELS = [
    {"alias": "claude-opus-5", "modelId": "us.anthropic.claude-opus-5", "provider": "bedrock", "tier": "premium"},
    {"alias": "claude-sonnet-5", "modelId": "us.anthropic.claude-sonnet-5", "provider": "bedrock", "tier": "standard"},
    {"alias": "gpt-6-1-sol", "modelId": "us.openai.gpt-6.1-sol", "provider": "bedrock", "tier": "standard"},
    {"alias": "nova-2-lite", "modelId": "us.amazon.nova-2-lite-v1:0", "provider": "bedrock", "tier": "economy"},
]
CONFIG = {
    "account-id": ACCOUNT,
    "api/url": "https://api.example.test",
    "ui/url": "https://ui.example.test",
    "auth/user-pool-id": "us-east-1_TESTPOOL",
    "auth/client-id": "testclientid123",
    "auth/domain": "https://agent-platform.auth.us-east-1.amazoncognito.com",
    "auth/users-secret-arn": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agent-platform/users-AbCdEf",
    "table/name": "agent-platform",
    "backend/worker-function-name": "agent-platform-worker",
    "model-gateway/url": "https://llm.example.test",
    "model-gateway/master-key-secret-arn": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:agent-platform/litellm-master-key-XyZ",
    "model-gateway/models": json.dumps(MODELS),
    "guardrail/id": "gr123",
    "guardrail/arn": f"arn:aws:bedrock:{REGION}:{ACCOUNT}:guardrail/gr123",
    "guardrail/version": "2",
    "roles/evaluation-role-arn": f"arn:aws:iam::{ACCOUNT}:role/agent-platform-evaluation-role",
    "roles/backend-role-arn": f"arn:aws:iam::{ACCOUNT}:role/agent-platform-backend-role",
    "scenario/kb-id": "KB123",
    "scenario/tools/hr-lambda-arn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:agent-platform-hr-tools",
}
RELAY_FUNCTION = "agent-platform-model-gateway-relay"
# the same account with the private landing zone (tier 3): the extra keys the Network and ModelGateway stacks publish
PRIVATE_CONFIG = {
    **CONFIG,
    "model-gateway/relay-function-name": RELAY_FUNCTION,
    "model-gateway/relay-function-arn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{RELAY_FUNCTION}",
    "model-gateway/private": "true",
    "network/private": "true",
    "network/vpc-id": "vpc-0private",
    "network/harness-subnet-ids": "subnet-0a1,subnet-0a2,subnet-0a4",
    "network/harness-security-group-id": "sg-0harness",
}
GROUPS = {"admin": "platform-admins", "developer": "agent-developers", "approver": "approvers"}


def raw_key(alias: str) -> str:
    """The raw virtual key the fake LiteLLM proxy hands out for an alias (shown once, like the real proxy)."""
    return f"sk-raw-{alias}"


def hashed(alias: str) -> str:
    """LiteLLM's key id for that alias: the sha256 hex of the raw key (the scheme of the real proxy, probed 9 Oct 2026)."""
    return hashlib.sha256(raw_key(alias).encode()).hexdigest()


def entry_module(name: str):
    """Load backend/src/<name>.py (the Lambda entry points `app` and `worker`) under a unique module name, so the repo-wide test
    run cannot confuse it with foundation/app.py."""
    module_name = f"backend_entry_{name}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, SRC / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# ---- HTTP API v2 events ---------------------------------------------------------------------------------------------------
def make_event(
    method: str,
    path: str,
    body: Any = None,
    persona: str | None = "admin",
    groups=None,
    raw_groups=None,
    query: dict | None = None,
    raw_body: str | None = None,
) -> dict:
    """An HTTP API v2 proxy event. `persona` None = no authorizer claims (public route or direct call)."""
    event: dict = {
        "version": "2.0",
        "routeKey": "$default",
        "rawPath": path,
        "rawQueryString": "",
        "headers": {"content-type": "application/json"},
        "requestContext": {"http": {"method": method, "path": path, "protocol": "HTTP/1.1"}, "stage": "$default"},
        "isBase64Encoded": False,
    }
    if query:
        event["queryStringParameters"] = query
    if raw_body is not None:
        event["body"] = raw_body
    elif body is not None:
        event["body"] = json.dumps(body)
    if persona is not None:
        claims = {
            "cognito:username": persona,
            "email": f"{persona}@anycompany.example",
            "email_verified": "true",
            "sub": f"sub-{persona}",
            "token_use": "id",
            "aud": CONFIG["auth/client-id"],
        }
        if raw_groups is not None:
            claims["cognito:groups"] = raw_groups
        else:
            group_list = list(groups) if groups is not None else ([GROUPS[persona]] if persona in GROUPS else [])
            if group_list:
                claims["cognito:groups"] = "[" + " ".join(group_list) + "]"  # the HTTP API quirk: a string
        event["requestContext"]["authorizer"] = {"jwt": {"claims": claims, "scopes": None}}
    return event


def call(method: str, path: str, **kwargs) -> tuple[int, Any]:
    """Dispatch an event through the router; returns (status, parsed body)."""
    from platform_api import router

    response = router.dispatch(make_event(method, path, **kwargs))
    body = json.loads(response["body"]) if response.get("body") else None
    return response["statusCode"], body


# ---- requests doubles -------------------------------------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code=200, json_body=None, text=None, reason="OK"):
        self.status_code = status_code
        self._json = json_body
        self.text = text if text is not None else (json.dumps(json_body) if json_body is not None else "")
        self.content = self.text.encode()
        self.reason = reason

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls: list[dict] = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None, stream=False):  # noqa: A002
        call_ = {"method": method, "url": url, "params": params, "json": json, "headers": headers or {}}
        self.calls.append(call_)
        return self.handler(call_)


class FakeLiteLLMServer:
    """In-memory LiteLLM admin API with the response shapes seen on v1.104.1 (teams, service-account keys, spend logs)."""

    BASE = "https://llm.example.test"

    def __init__(self):
        self.teams: dict[str, dict] = {}
        self.keys: dict[str, dict] = {}  # hashed token -> row
        self.logs: dict[str, list] = {}  # hashed token -> spend log rows
        self.fail: set[str] = set()  # paths that answer 500
        self.calls: list[tuple[str, str, Any]] = []

    def _error(self, status, message, type_=None):
        return FakeResponse(status, {"error": {"message": message, "type": type_ or "error", "code": str(status)}})

    @staticmethod
    def _token(key_or_hash):
        """Like the real proxy, /key/info, /key/update and /key/block accept the raw key as well as its hash."""
        if isinstance(key_or_hash, str) and key_or_hash.startswith("sk-"):
            return hashlib.sha256(key_or_hash.encode()).hexdigest()
        return key_or_hash

    def handle(self, call_):
        method, path = call_["method"], call_["url"][len(self.BASE) :]
        params, body = call_["params"] or {}, call_["json"] or {}
        self.calls.append((method, path, dict(params) if method == "GET" else dict(body)))
        if path in self.fail:
            return self._error(500, f"fake failure on {path}")
        if path == "/team/new":
            tid = body["team_id"]
            if tid in self.teams:
                return self._error(
                    400, f"Team id = {tid} already exists. Please use a different team id.", "team_id_exists"
                )
            self.teams[tid] = {
                "team_id": tid,
                "team_alias": body.get("team_alias"),
                "models": list(body.get("models") or []),
                "max_budget": body.get("max_budget"),
                "budget_duration": body.get("budget_duration"),
                "tpm_limit": body.get("tpm_limit"),
                "rpm_limit": body.get("rpm_limit"),
                "metadata": body.get("metadata") or {},
                "spend": 0.0,
                "budget_reset_at": "2026-10-15T00:00:00Z",
                "blocked": False,
            }
            return FakeResponse(200, self.teams[tid])
        if path == "/team/info":
            tid = params.get("team_id")
            if tid not in self.teams:
                return FakeResponse(404, {"detail": {"error": f"Team not found, passed team_id={tid}"}})
            return FakeResponse(
                200,
                {
                    "team_id": tid,
                    "team_info": self.teams[tid],
                    "keys": [k for k in self.keys.values() if k["team_id"] == tid],
                },
            )
        if path == "/team/update":
            tid = body["team_id"]
            if tid not in self.teams:
                return FakeResponse(404, {"detail": {"error": "Team not found"}})
            self.teams[tid].update({k: v for k, v in body.items() if k != "team_id"})
            return FakeResponse(200, {"team_id": tid, "data": self.teams[tid]})
        if path == "/team/delete":
            deleted = []
            for tid in body.get("team_ids", []):
                if tid in self.teams:
                    deleted.append(self.teams.pop(tid))
                    for token in [t for t, k in self.keys.items() if k["team_id"] == tid]:
                        self.keys.pop(token)
            return FakeResponse(200, deleted)
        if path == "/key/service-account/generate":
            alias = body["key_alias"]
            if any(k["key_alias"] == alias for k in self.keys.values()):
                return self._error(
                    400, f"Key with alias {alias} already exists. Unique key aliases across all keys are required."
                )
            token = hashed(alias)
            self.keys[token] = {
                "token": token,
                "key_alias": alias,
                "team_id": body.get("team_id"),
                "models": list(body.get("models") or []),
                "max_budget": body.get("max_budget"),
                "budget_duration": body.get("budget_duration"),
                "metadata": body.get("metadata") or {},
                "spend": 0.0,
                "blocked": False,
                "budget_reset_at": "2026-10-15T00:00:00Z",
            }
            return FakeResponse(200, {"key": raw_key(alias), "token_id": token, "token": token, "key_alias": alias})
        if path == "/key/list":
            alias = params.get("key_alias")
            rows = [k for k in self.keys.values() if alias is None or k["key_alias"] == alias]
            return FakeResponse(200, {"keys": rows, "total_count": len(rows)})
        if path == "/key/info":
            token = self._token(params.get("key"))
            if token not in self.keys:
                return FakeResponse(404, {"detail": {"error": "Key not found"}})
            return FakeResponse(200, {"key": token, "info": self.keys[token]})
        if path in ("/key/update", "/key/block", "/key/unblock"):
            token = self._token(body["key"])
            if token not in self.keys:
                return FakeResponse(404, {"detail": {"error": "Key not found"}})
            if path == "/key/update":
                self.keys[token].update({k: v for k, v in body.items() if k != "key"})
            else:
                self.keys[token]["blocked"] = path == "/key/block"
            return FakeResponse(200, {"key": token, **self.keys[token]})
        if path == "/key/delete":
            for token in body.get("keys", []):
                if token not in self.keys:
                    return FakeResponse(404, {"detail": {"error": f"key {token} not found"}})
                self.keys.pop(token)
            return FakeResponse(200, {"deleted_keys": body.get("keys", [])})
        if path == "/spend/logs":
            return FakeResponse(200, list(self.logs.get(params.get("api_key"), [])))
        if path == "/model/info":
            return FakeResponse(
                200,
                {
                    "data": [
                        {
                            "model_name": m["alias"],
                            "litellm_params": {
                                "model": f"bedrock/converse/{m['modelId']}",
                                "aws_region_name": REGION,
                                "guardrailConfig": {"guardrailIdentifier": "gr123", "guardrailVersion": "2"},
                                "input_cost_per_token": 1e-06,
                                "output_cost_per_token": 5e-06,
                            },
                            "model_info": {"id": f"id-{m['alias']}"},
                        }
                        for m in MODELS
                    ]
                },
            )
        raise AssertionError(f"unexpected LiteLLM call {method} {path}")


class FakeIdentity:
    """AgentCore Identity API-key providers: name -> ARN."""

    def __init__(self):
        self.providers: dict[str, str] = {}
        self.keys: dict[str, str] = {}
        self.fail = False

    def arn(self, name):
        return f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:token-vault/default/apikeycredentialprovider/{name}"

    def store_api_key(self, name, api_key, tags=None, region=None, client=None):
        if self.fail:
            raise RuntimeError("fake identity failure")
        self.providers[name] = self.arn(name)
        self.keys[name] = api_key
        return self.providers[name]

    def get_provider_arn(self, name, region=None, client=None):
        return self.providers.get(name)

    def delete_provider(self, name, region=None, client=None):
        return self.providers.pop(name, None) is not None


class FakeRoles:
    """platform_sdk.templates.roles stand-in recording every stamp."""

    def __init__(self):
        self.roles: dict[tuple[str, str], dict] = {}
        self.calls: list[dict] = []
        self.fail = False

    def ensure_project_roles(
        self, cfg, project_id, env, allowed_tool_arns=None, kb_ids=None, iam=None, settle_seconds=0
    ):
        self.calls.append(
            {"project": project_id, "env": env, "tools": list(allowed_tool_arns or []), "kbs": list(kb_ids or [])}
        )
        if self.fail:
            raise RuntimeError("fake IAM failure")
        arns = {
            "agentRoleArn": f"arn:aws:iam::{ACCOUNT}:role/agent-platform-proj-{project_id}-{env}-agent",
            "gatewayRoleArn": f"arn:aws:iam::{ACCOUNT}:role/agent-platform-proj-{project_id}-{env}-gateway",
        }
        self.roles[(project_id, env)] = arns
        return arns

    def delete_project_roles(self, cfg, project_id, env, iam=None):
        if (project_id, env) in self.roles:
            self.roles.pop((project_id, env))
            return [f"agent-platform-proj-{project_id}-{env}-agent", f"agent-platform-proj-{project_id}-{env}-gateway"]
        return []


# ---- fakes for platform_sdk.agentcore and the worker's Lambda client (the `ready` fixture in conftest wires them) -------------
class FakeAgentCore:
    """create_or_update_harness / ensure_endpoint / ensure_online_evaluation / invoke with the shapes seen live."""

    def __init__(self):
        self.versions: dict[str, int] = {}
        self.harnesses: dict[str, dict] = {}
        self.endpoints: dict[tuple[str, str], dict] = {}
        self.online: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail_on: set[str] = set()
        self.invoke_responses: list = []

    @staticmethod
    def harness_id(name):
        return f"{name}-ABC123"

    def create_or_update_harness(self, name, spec, tags=None, region=None):
        self.calls.append(("harness", name, spec, tags, region))
        if "harness" in self.fail_on:
            raise AgentCoreError(f"harness {name} failed: CREATE_FAILED ['boom']")
        version = self.versions.get(name, 0) + 1
        self.versions[name] = version
        harness = {
            "harnessId": self.harness_id(name),
            "harnessName": name,
            "arn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:harness/{self.harness_id(name)}",
            "status": "READY",
            "harnessVersion": str(version),
            "environment": {"agentCoreRuntimeEnvironment": {"agentRuntimeId": f"harness_{name}-RT123"}},
        }
        self.harnesses[name] = harness
        return harness

    def ensure_endpoint(self, harness_id, name, version, description=None, region=None):
        self.calls.append(("endpoint", harness_id, name, str(version)))
        if "endpoint" in self.fail_on:
            raise AgentCoreError(f"endpoint {name} failed: CREATE_FAILED")
        endpoint = {
            "harnessId": harness_id,
            "endpointName": name,
            "arn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:harness/{harness_id}/endpoint/{name}",
            "status": "READY",
            "liveVersion": str(version),
        }
        self.endpoints[(harness_id, name)] = endpoint
        return endpoint

    def ensure_online_evaluation(self, name, log_group, service_name, evaluator_ids, role_arn, **kwargs):
        self.calls.append(("online", name, log_group, service_name, list(evaluator_ids), role_arn, kwargs))
        config = {
            "onlineEvaluationConfigId": f"{name}-id",
            "onlineEvaluationConfigArn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:online-evaluation-config/{name}-id",
            "onlineEvaluationConfigName": name,
            "status": "ACTIVE",
        }
        self.online[name] = config
        return config

    def invoke(self, harness_arn, prompt, qualifier=None, session_id=None, timeout=180, actor_id=None, region=None):
        self.calls.append(("invoke", harness_arn, prompt, qualifier, session_id))
        response = self.invoke_responses.pop(0) if self.invoke_responses else None
        if isinstance(response, Exception):
            raise response
        return response or InvokeResult(
            text=f"echo: {prompt}",
            tools=["hrtools___get_leave_balance"],
            usage={"inputTokens": 100, "outputTokens": 20, "totalTokens": 120, "modelCalls": 2},
            stop_reasons=["end_turn"],
            hooks=[],
            session_id=session_id,
            seconds=4.2,
        )


class FakeControl:
    """bedrock-agentcore-control: GetHarnessEndpoint (DEFAULT exists implicitly for every harness)."""

    def __init__(self, fake: FakeAgentCore):
        self.fake = fake

    def get_harness_endpoint(self, harnessId, endpointName):
        endpoint = self.fake.endpoints.get((harnessId, endpointName))
        if endpoint is None and endpointName == "DEFAULT":
            name = harnessId.rsplit("-", 1)[0]
            endpoint = {
                "harnessId": harnessId,
                "endpointName": "DEFAULT",
                "arn": f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:harness/{harnessId}/endpoint/DEFAULT",
                "status": "READY",
                "liveVersion": str(self.fake.versions.get(name, 1)),
            }
        if endpoint is None:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "no endpoint"}}, "GetHarnessEndpoint"
            )
        return {"endpoint": endpoint}


class FakeRegistry:
    """agent-registry-control with the record lifecycle of the AWS docs (read 2026-10-21): a new record is DRAFT at once here
    (CREATING for about 1.5 s live); submit -> PENDING_APPROVAL; UpdateRegistryRecordStatus allows PENDING_APPROVAL -> APPROVED |
    REJECTED, REJECTED -> APPROVED and APPROVED -> DEPRECATED (ConflictException otherwise); an update of an APPROVED record is a
    new DRAFT revision, a DEPRECATED record cannot be edited; `after_update_status` forces the status an update leaves."""

    TRANSITIONS = {
        ("PENDING_APPROVAL", "APPROVED"),
        ("PENDING_APPROVAL", "REJECTED"),
        ("REJECTED", "APPROVED"),
        ("APPROVED", "DEPRECATED"),
    }

    def __init__(self):
        self.records: dict[str, dict] = {}
        self.calls: list[tuple[str, dict]] = []
        self.fail_on: set[str] = set()
        self.after_update_status: str | None = None
        self.counter = 0
        self.clock = 0

    def _stamp(self):
        from datetime import UTC, datetime, timedelta

        self.clock += 1
        return datetime(2026, 10, 21, tzinfo=UTC) + timedelta(seconds=self.clock)

    def _check(self, operation):
        if operation in self.fail_on:
            raise ClientError({"Error": {"Code": "InternalServerException", "Message": f"{operation} boom"}}, operation)

    def _arn(self, registry_id, record_id):
        return f"arn:aws:agent-registry:{REGION}:{ACCOUNT}:registry/{registry_id}/record/{record_id}"

    def list_registry_records(self, **kwargs):
        self.calls.append(("list", kwargs))
        self._check("ListRegistryRecords")
        rows = list(self.records.values())
        for flt in kwargs.get("filters") or []:
            rows = [r for r in rows if r.get(flt["name"]) in flt["values"]]
        return {"registryRecords": [{k: v for k, v in r.items() if k != "descriptors"} for r in rows]}

    def create_registry_record(self, **kwargs):
        self.calls.append(("create", kwargs))
        self._check("CreateRegistryRecord")
        if any(r["name"] == kwargs["name"] for r in self.records.values()):
            raise ClientError(
                {"Error": {"Code": "ConflictException", "Message": f"record {kwargs['name']} exists"}},
                "CreateRegistryRecord",
            )
        self.counter += 1
        record_id = f"rec{self.counter:03d}"
        now = self._stamp()
        self.records[record_id] = {
            "recordId": record_id,
            "recordArn": self._arn(kwargs["registryId"], record_id),
            "name": kwargs["name"],
            "displayName": kwargs.get("displayName"),
            "description": kwargs.get("description"),
            "recordType": kwargs["recordType"],
            "recordVersion": kwargs.get("recordVersion"),
            "descriptors": kwargs.get("descriptors"),
            "status": "DRAFT",
            "tags": kwargs.get("tags") or {},
            "createdAt": now,
            "updatedAt": now,
        }
        return {"recordArn": self.records[record_id]["recordArn"], "status": "CREATING"}

    def update_registry_record(self, **kwargs):
        self.calls.append(("update", kwargs))
        self._check("UpdateRegistryRecord")
        record = self.records[kwargs["recordId"]]
        if record["status"] == "DEPRECATED":
            raise ClientError(
                {"Error": {"Code": "ConflictException", "Message": "a DEPRECATED record cannot be edited"}},
                "UpdateRegistryRecord",
            )
        for key in ("displayName", "description"):
            if key in kwargs:
                record[key] = kwargs[key]["optionalValue"]
        if "recordVersion" in kwargs:
            record["recordVersion"] = kwargs["recordVersion"]
        if "descriptors" in kwargs:
            fields = kwargs["descriptors"]["optionalValue"]
            record["descriptors"] = {k: v["optionalValue"] for k, v in fields.items()}
        if self.after_update_status:
            record["status"] = self.after_update_status
        elif record["status"] in ("APPROVED", "PENDING_APPROVAL", "REJECTED"):
            record["status"] = "DRAFT"  # the live registry opens a new DRAFT revision (GetRegistryRecord returns it)
        record["updatedAt"] = self._stamp()
        return {**record, "status": "UPDATING"}

    def get_registry_record(self, **kwargs):
        self.calls.append(("get", kwargs))
        self._check("GetRegistryRecord")
        record = self.records.get(kwargs["recordId"])
        if record is None:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "no such record"}}, "GetRegistryRecord"
            )
        return dict(record)

    def submit_registry_record_for_approval(self, **kwargs):
        self.calls.append(("submit", kwargs))
        self._check("SubmitRegistryRecordForApproval")
        record = self.records[kwargs["recordId"]]
        if record["status"] != "DRAFT":
            raise ClientError(
                {
                    "Error": {
                        "Code": "ConflictException",
                        "Message": f"only DRAFT records are submitted ({record['status']})",
                    }
                },
                "SubmitRegistryRecordForApproval",
            )
        record["status"], record["updatedAt"] = "PENDING_APPROVAL", self._stamp()
        return {"status": "PENDING_APPROVAL"}

    def update_registry_record_status(self, **kwargs):
        self.calls.append(("status", kwargs))
        self._check("UpdateRegistryRecordStatus")
        record = self.records[kwargs["recordId"]]
        if (record["status"], kwargs["status"]) not in self.TRANSITIONS:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ConflictException",
                        "Message": f"{record['status']} -> {kwargs['status']} is not allowed",
                    }
                },
                "UpdateRegistryRecordStatus",
            )
        record["status"], record["statusReason"] = kwargs["status"], kwargs.get("statusReason")
        record["updatedAt"] = self._stamp()
        return {"recordId": record["recordId"], "recordArn": record["recordArn"], "status": record["status"]}


class FakeLambda:
    def __init__(self):
        self.payloads: list[dict] = []
        self.fail = False

    def invoke(self, **kwargs):
        if self.fail:
            raise ClientError({"Error": {"Code": "ServiceException", "Message": "lambda down"}}, "Invoke")
        self.payloads.append({**kwargs, "Payload": json.loads(kwargs["Payload"])})
        return {"StatusCode": 202}


def register(project_id, env, type_, name, **fields):
    status, body = call(
        "PUT", f"/projects/{project_id}/resources", body={"env": env, "type": type_, "name": name, **fields}
    )
    assert status == 200, body
    return body
