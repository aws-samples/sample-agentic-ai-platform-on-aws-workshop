import json

import boto3
from moto import mock_aws
from platform_sdk.templates import roles

ACCOUNT = "123456789012"
HR_LAMBDA = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:agent-platform-scenario-hr-tools"


def test_agent_role_template_renders_valid_documents_without_placeholders(cfg):
    rendered = roles.render_agent_role(cfg, "hr-assistant", "dev")
    text = json.dumps(rendered)
    assert "{{" not in text and "_comment" not in rendered
    trust = rendered["trust"]
    service = next(s for s in trust["Statement"] if "Service" in s["Principal"])
    assert service["Principal"]["Service"] == "bedrock-agentcore.amazonaws.com"
    assert service["Condition"] == {
        "StringEquals": {"aws:SourceAccount": ACCOUNT},
        "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:*"},
    }
    root = next(s for s in trust["Statement"] if "AWS" in s["Principal"])
    assert root["Principal"]["AWS"] == f"arn:aws:iam::{ACCOUNT}:root"
    statements = {s["Sid"]: s for s in rendered["policy"]["Statement"]}
    assert (
        statements["InvokeProjectGateways"]["Resource"]
        == f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:gateway/hr-assistant-dev-gw*"
    )
    assert (
        statements["ProjectMemoryDataPlane"]["Resource"]
        == f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:memory/hr_assistant_dev_memory*"
    )
    assert "bedrock-agentcore:CreateEvent" in statements["ProjectMemoryDataPlane"]["Action"]
    assert set(statements["WorkloadIdentityAndApiKeys"]["Action"]) >= {
        "bedrock-agentcore:GetResourceApiKey",
        "bedrock-agentcore:GetWorkloadAccessToken",
    }
    assert statements["WorkloadIdentityAndApiKeys"]["Resource"] == [
        f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:workload-identity-directory/default",
        f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:workload-identity-directory/default/workload-identity/harness_hr_assistant_dev-*",
        f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:token-vault/default",
        f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:token-vault/default/apikeycredentialprovider/litellm-hr-assistant-*",
    ]
    assert statements["ReadProjectLiteLlmKeys"]["Resource"] == (
        f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:bedrock-agentcore-identity!default/apikey/litellm-hr-assistant-*"
    )
    # observability scoped like the documented runtime execution role: no action on "*" except X-Ray and namespaced metrics
    assert statements["RuntimeLogGroups"]["Resource"] == (
        f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/bedrock-agentcore/runtimes/*"
    )
    assert statements["RuntimeLogResourcePolicy"]["Resource"] == (
        f"arn:aws:logs:us-east-1:{ACCOUNT}:log-group:/aws/bedrock-agentcore/runtimes/harness_hr_assistant_dev-*"
    )
    assert statements["RuntimeLogStreams"]["Resource"].endswith("/aws/bedrock-agentcore/runtimes/*:log-stream:*")
    assert statements["Metrics"] == {
        "Sid": "Metrics",
        "Effect": "Allow",
        "Action": "cloudwatch:PutMetricData",
        "Resource": "*",
        "Condition": {"StringEquals": {"cloudwatch:namespace": "bedrock-agentcore"}},
    }
    assert "xray:PutTraceSegments" in statements["Tracing"]["Action"]
    star = [s["Sid"] for s in rendered["policy"]["Statement"] if s["Resource"] == "*"]
    assert star == ["Tracing", "Metrics", "HarnessVpcEcrAuth"]


def test_agent_role_template_carries_the_harness_vpc_mode_ecr_statements(cfg):
    """Private landing zone: a harness in VPC mode pulls its managed image from the service's regional repository harness-<region>
    through the ECR endpoints, which needs the three image actions on that repository and GetAuthorizationToken on * (harness
    security page, private-landing-zone-design.md section 2); everything else in the template is unchanged."""
    rendered = roles.render_agent_role(cfg, "hr-assistant", "dev")
    statements = {s["Sid"]: s for s in rendered["policy"]["Statement"]}
    assert statements["HarnessVpcImagePull"] == {
        "Sid": "HarnessVpcImagePull",
        "Effect": "Allow",
        "Action": ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"],
        "Resource": "arn:aws:ecr:us-east-1:*:repository/harness-*",
    }
    assert statements["HarnessVpcEcrAuth"] == {
        "Sid": "HarnessVpcEcrAuth",
        "Effect": "Allow",
        "Action": "ecr:GetAuthorizationToken",
        "Resource": "*",
    }
    assert list(statements) == [
        "InvokeProjectGateways",
        "ProjectMemoryDataPlane",
        "WorkloadIdentityAndApiKeys",
        "ReadProjectLiteLlmKeys",
        "RuntimeLogGroups",
        "RuntimeLogResourcePolicy",
        "DescribeLogGroups",
        "RuntimeLogStreams",
        "Tracing",
        "Metrics",
        "HarnessVpcImagePull",
        "HarnessVpcEcrAuth",
    ]
    # still no model access on the role: models come through the model gateway key
    assert not any("bedrock:InvokeModel" in json.dumps(s.get("Action")) for s in rendered["policy"]["Statement"])


def test_gateway_role_template_lists_and_star_fallback(cfg):
    rendered = roles.render_gateway_role(
        cfg, "hr-assistant", "prod", [HR_LAMBDA], ["KB123", f"arn:aws:bedrock:us-east-1:{ACCOUNT}:knowledge-base/KB456"]
    )
    assert "{{" not in json.dumps(rendered)
    statements = {s["Sid"]: s for s in rendered["policy"]["Statement"]}
    # policy-engine integration (AgentCore developer guide, policy-permissions; verified live on 8 Oct 2026)
    assert statements["PolicyEngineConfiguration"]["Action"] == ["bedrock-agentcore:GetPolicyEngine"]
    assert (
        statements["PolicyEngineConfiguration"]["Resource"]
        == f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:policy-engine/*"
    )
    assert statements["PolicyEngineAuthorization"]["Action"] == [
        "bedrock-agentcore:AuthorizeAction",
        "bedrock-agentcore:PartiallyAuthorizeActions",
    ]
    assert statements["PolicyEngineAuthorization"]["Resource"] == [
        f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:policy-engine/*",
        f"arn:aws:bedrock-agentcore:us-east-1:{ACCOUNT}:gateway/hr-assistant-prod-gw*",
    ]
    assert statements["InvokeAllowedTools"]["Action"] == ["lambda:InvokeFunction"]
    assert statements["InvokeAllowedTools"]["Resource"] == [HR_LAMBDA]
    assert statements["QueryProjectKnowledgeBases"]["Resource"] == [
        f"arn:aws:bedrock:us-east-1:{ACCOUNT}:knowledge-base/KB123",
        f"arn:aws:bedrock:us-east-1:{ACCOUNT}:knowledge-base/KB456",
    ]
    assert set(statements["QueryProjectKnowledgeBases"]["Action"]) == {
        "bedrock:Retrieve",
        "bedrock:GetKnowledgeBase",
        "bedrock:AgenticRetrieveStream",
    }
    empty = roles.render_gateway_role(cfg, "hr-assistant", "prod", [], [])
    for sid in ("InvokeAllowedTools", "QueryProjectKnowledgeBases"):
        assert next(s for s in empty["policy"]["Statement"] if s["Sid"] == sid)["Resource"] == "*"


def test_template_files_are_valid_json_with_known_placeholders_only():
    allowed = {"account_id", "region", "project_id", "env", "snake", "allowed_tool_arns", "kb_arns"}
    for name in ("agent_role.json", "gateway_role.json"):
        text = (roles.TEMPLATE_DIR / name).read_text()
        json.loads(text)
        found = {chunk.split("}}")[0] for chunk in text.split("{{")[1:]}
        assert found <= allowed, f"{name}: unknown placeholders {found - allowed}"


@mock_aws
def test_ensure_project_roles_creates_then_updates(cfg):
    iam = boto3.client("iam", region_name="us-east-1")
    arns = roles.ensure_project_roles(cfg, "hr-assistant", "dev", [HR_LAMBDA], ["KB123"], iam=iam)
    assert arns == {
        "agentRoleArn": f"arn:aws:iam::{ACCOUNT}:role/agent-platform-proj-hr-assistant-dev-agent",
        "gatewayRoleArn": f"arn:aws:iam::{ACCOUNT}:role/agent-platform-proj-hr-assistant-dev-gateway",
    }
    role = iam.get_role(RoleName="agent-platform-proj-hr-assistant-dev-agent")["Role"]
    tags = {t["Key"]: t["Value"] for t in role["Tags"]}
    assert tags == {
        "Project": "workshop-agent-platform",
        "ProjectId": "hr-assistant",
        "Environment": "dev",
        "ManagedBy": "platform",
    }
    policy = iam.get_role_policy(RoleName="agent-platform-proj-hr-assistant-dev-gateway", PolicyName=roles.POLICY_NAME)[
        "PolicyDocument"
    ]
    assert next(s for s in policy["Statement"] if s["Sid"] == "InvokeAllowedTools")["Resource"] == [HR_LAMBDA]
    # second call with a new tool list updates in place (idempotent, no error)
    other = f"arn:aws:lambda:us-east-1:{ACCOUNT}:function:other"
    again = roles.ensure_project_roles(cfg, "hr-assistant", "dev", [HR_LAMBDA, other], [], iam=iam)
    assert again == arns
    policy = iam.get_role_policy(RoleName="agent-platform-proj-hr-assistant-dev-gateway", PolicyName=roles.POLICY_NAME)[
        "PolicyDocument"
    ]
    statements = {s["Sid"]: s for s in policy["Statement"]}
    assert statements["InvokeAllowedTools"]["Resource"] == [HR_LAMBDA, other]
    assert statements["QueryProjectKnowledgeBases"]["Resource"] == "*"
    assert roles.delete_project_roles(cfg, "hr-assistant", "dev", iam=iam) == [
        "agent-platform-proj-hr-assistant-dev-agent",
        "agent-platform-proj-hr-assistant-dev-gateway",
    ]
    assert roles.delete_project_roles(cfg, "hr-assistant", "dev", iam=iam) == []
