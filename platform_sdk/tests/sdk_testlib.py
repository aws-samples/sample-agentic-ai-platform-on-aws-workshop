"""Test doubles shared by the platform_sdk tests (not a pytest plugin)."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACCOUNT = "123456789012"
REGION = "us-east-1"
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
    "model-gateway/models": json.dumps(
        [
            {
                "alias": "claude-sonnet-5-5",
                "modelId": "us.anthropic.claude-sonnet-5-5",
                "provider": "bedrock",
                "tier": "standard",
            },
            {
                "alias": "claude-haiku-4-5",
                "modelId": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
                "provider": "bedrock",
                "tier": "fast",
            },
            {"alias": "nova-2-lite", "modelId": "us.amazon.nova-2-lite-v1:0", "provider": "bedrock", "tier": "economy"},
        ]
    ),
    "guardrail/id": "gr123",
    "guardrail/arn": f"arn:aws:bedrock:{REGION}:{ACCOUNT}:guardrail/gr123",
    "guardrail/version": "1",
    "roles/evaluation-role-arn": f"arn:aws:iam::{ACCOUNT}:role/agent-platform-evaluation",
    "roles/backend-role-arn": f"arn:aws:iam::{ACCOUNT}:role/agent-platform-backend",
    "scenario/kb-id": "KB123",
}
RELAY_FUNCTION = "agent-platform-model-gateway-relay"
HARNESS_SUBNETS = ["subnet-0a1", "subnet-0a2", "subnet-0a4"]
HARNESS_SECURITY_GROUP = "sg-0harness"
# the same account with the private landing zone (tier 3): what the Network and ModelGateway stacks publish in addition
PRIVATE_CONFIG = {
    **CONFIG,
    "model-gateway/relay-function-name": RELAY_FUNCTION,
    "model-gateway/relay-function-arn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{RELAY_FUNCTION}",
    "model-gateway/private": "true",
    "network/private": "true",
    "network/vpc-id": "vpc-0private",
    "network/app-subnet-ids": ",".join(HARNESS_SUBNETS),
    "network/harness-subnet-ids": ",".join(HARNESS_SUBNETS),
    "network/harness-security-group-id": HARNESS_SECURITY_GROUP,
    "network/relay-security-group-id": "sg-0relay",
    "network/availability-zones": "us-east-1a,us-east-1b,us-east-1c",
}


class FakeResponse:
    """Minimal stand-in for requests.Response."""

    def __init__(self, status_code=200, json_body=None, text=None, reason="OK", headers=None):
        self.status_code = status_code
        self._json = json_body
        self.text = text if text is not None else (json.dumps(json_body) if json_body is not None else "")
        self.content = self.text.encode()
        self.reason = reason
        self.headers = dict(headers or {})

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Records every request and answers from a queue of (predicate | None, FakeResponse) or a handler function."""

    def __init__(self, handler=None):
        self.calls: list[dict] = []
        self.handler = handler
        self.queue: list[FakeResponse] = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None, stream=False):
        call = {
            "method": method,
            "url": url,
            "params": params,
            "json": json,
            "headers": headers or {},
            "timeout": timeout,
        }
        self.calls.append(call)
        if self.handler:
            return self.handler(call)
        if self.queue:
            return self.queue.pop(0)
        raise AssertionError(f"unexpected request {method} {url}")


class FakePayload:
    """The StreamingBody of a Lambda Invoke response."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeLambdaClient:
    """A boto3 Lambda client double for the relay: records every Invoke event and answers from `handler(event)`.

    The handler returns the relay's documented response dict ({"status", "headers", "body"}); it may raise a ClientError (an IAM
    refusal) or return a `FakeLambdaClient.function_error(...)` tuple to play a Lambda that crashed.
    """

    def __init__(self, handler):
        self.handler = handler
        self.events: list[dict] = []
        self.invocations: list[dict] = []

    @staticmethod
    def function_error(error_type: str, message: str) -> tuple:
        return ("FunctionError", {"errorType": error_type, "errorMessage": message})

    def invoke(self, FunctionName, Payload, InvocationType="RequestResponse", **kwargs):
        event = json.loads(Payload)
        self.events.append(event)
        self.invocations.append({"FunctionName": FunctionName, "InvocationType": InvocationType})
        result = self.handler(event)
        response = {"StatusCode": 200}
        if isinstance(result, tuple) and result and result[0] == "FunctionError":
            response["FunctionError"] = "Unhandled"
            result = result[1]
        response["Payload"] = FakePayload(json.dumps(result).encode())
        return response
