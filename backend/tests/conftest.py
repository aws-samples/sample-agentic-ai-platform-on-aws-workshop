"""Backend test fixtures: fake credentials, a moto DynamoDB table and in-memory doubles for LiteLLM, Identity and IAM roles.

No unit test talks to AWS or to the model gateway. The live suite (`backend/tests/live`) runs only with PLATFORM_LIVE=1 and uses
`platform_sdk` against the deployed stack.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / "backend" / "src", Path(__file__).resolve().parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from backend_testlib import (  # noqa: E402
    ACCOUNT,
    CONFIG,
    REGION,
    FakeAgentCore,
    FakeControl,
    FakeIdentity,
    FakeLambda,
    FakeLiteLLMServer,
    FakeRegistry,
    FakeRoles,
    FakeSession,
    call,
    register,
)
from platform_api import worker_jobs  # noqa: E402
from platform_api.services import clients  # noqa: E402
from platform_api.services import registry as registry_service  # noqa: E402
from platform_sdk.litellm import LiteLLMAdmin  # noqa: E402

from platform_sdk import agentcore, blueprint  # noqa: E402

BLUEPRINT_FILE = ROOT / "scenario" / "blueprint" / "blueprint.yaml"
HR_LAMBDA = CONFIG["scenario/tools/hr-lambda-arn"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("TABLE_NAME", "agent-platform")
    monkeypatch.setenv("WORKER_FUNCTION_NAME", "agent-platform-worker")
    monkeypatch.setenv("PLATFORM_CONFIG_JSON", json.dumps(CONFIG))
    from platform_api import db
    from platform_api.services import clients

    db.reset()
    clients.reset()
    registry_service.reset()
    yield
    db.reset()
    clients.reset()
    registry_service.reset()


@pytest.fixture
def table():
    """A moto DynamoDB table with the contract's key schema and GSI1."""
    from moto import mock_aws

    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName="agent-platform",
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
                {"AttributeName": "GSI1PK", "AttributeType": "S"},
                {"AttributeName": "GSI1SK", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                        {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield "agent-platform"


@pytest.fixture
def fakes(monkeypatch, table):
    """Handlers wired to an in-memory LiteLLM proxy (behind the real LiteLLMAdmin), fake Identity providers and fake IAM roles."""
    from platform_api.services import clients, litellm_provisioning
    from platform_api.services import roles as roles_service

    server = FakeLiteLLMServer()
    admin = LiteLLMAdmin("https://llm.example.test", "sk-master", session=FakeSession(server.handle))
    identity = FakeIdentity()
    roles = FakeRoles()
    monkeypatch.setattr(clients, "litellm_admin", lambda: admin)
    monkeypatch.setattr(clients, "iam", lambda: None)
    for name in ("store_api_key", "get_provider_arn", "delete_provider"):
        monkeypatch.setattr(litellm_provisioning.identity, name, getattr(identity, name))
    monkeypatch.setattr(roles_service.role_templates, "ensure_project_roles", roles.ensure_project_roles)
    monkeypatch.setattr(roles_service.role_templates, "delete_project_roles", roles.delete_project_roles)
    return SimpleNamespace(litellm=server, admin=admin, identity=identity, roles=roles)


@pytest.fixture
def ready(fakes, monkeypatch):
    """Domain, project (both environments registered with gateway, targets and memory) and the AgentCore fakes installed."""
    fake, fake_lambda, fake_registry = FakeAgentCore(), FakeLambda(), FakeRegistry()
    worker_jobs.register()  # another test module may have unregistered handlers through monkeypatch
    for name in ("create_or_update_harness", "ensure_endpoint", "ensure_online_evaluation", "invoke"):
        monkeypatch.setattr(agentcore, name, getattr(fake, name))
    monkeypatch.setattr(clients, "agentcore_control", lambda: FakeControl(fake))
    monkeypatch.setattr(clients, "lambda_client", lambda: fake_lambda)
    monkeypatch.setattr(clients, "cloudwatch", lambda: None)
    monkeypatch.setattr(clients, "registry_control", lambda: fake_registry)  # the promote job's AGENT record
    status, _ = call("POST", "/domains", body={"domainId": "people-ops", "name": "People Ops"})
    assert status == 201
    status, project = call(
        "POST",
        "/projects",
        body={
            "projectId": "hr-assistant",
            "domainId": "people-ops",
            "name": "HR Assistant",
            "models": ["claude-sonnet-5", "gpt-6-1-sol"],
            "allowedToolArns": [HR_LAMBDA],
            "knowledgeBaseIds": ["KB123"],
        },
    )
    assert status == 201, project
    for env in ("dev", "prod"):
        gw_id = f"hr-assistant-{env}-gw-abcdefghij"
        gw_arn = f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:gateway/{gw_id}"
        register(
            "hr-assistant", env, "gateway", f"hr-assistant-{env}-gw", arn=gw_arn, id=gw_id, url=f"https://{gw_id}/mcp"
        )
        register(
            "hr-assistant",
            env,
            "gateway_target",
            "hrtools",
            id="T1",
            attributes={"kind": "lambda", "gatewayArn": gw_arn},
        )
        register(
            "hr-assistant",
            env,
            "gateway_target",
            "hrkb",
            id="T2",
            attributes={"kind": "knowledge_base", "gatewayArn": gw_arn},
        )
        memory_name = f"hr_assistant_{env}_memory"
        register(
            "hr-assistant",
            env,
            "memory",
            memory_name,
            arn=f"arn:aws:bedrock-agentcore:{REGION}:{ACCOUNT}:memory/{memory_name}-xyz",
            id=f"{memory_name}-xyz",
        )
    bp = blueprint.load(BLUEPRINT_FILE)
    return SimpleNamespace(
        agentcore=fake, lambda_client=fake_lambda, registry=fake_registry, fakes=fakes, project=project, blueprint=bp
    )
