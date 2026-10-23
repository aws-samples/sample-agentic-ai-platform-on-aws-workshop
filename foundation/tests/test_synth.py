"""Synthesis tests: the app synthesizes for a fictitious account and the key resources exist per stack.

Run with `uv run pytest foundation/tests` from the repository root (no AWS credentials needed).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml
from aws_cdk import App
from aws_cdk.assertions import Match, Template

FOUNDATION = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FOUNDATION))
os.environ.setdefault("CDK_DEFAULT_ACCOUNT", "123456789012")
os.environ.setdefault("CDK_DEFAULT_REGION", "us-east-1")
# The CDK CLI passes the cdk.json feature flags (partition literals, minimized policies, ...) as context; the tests do the
# same so the assertions below see the templates exactly as `cdk synth` renders them.
CDK_CONTEXT = json.loads((FOUNDATION / "cdk.json").read_text())["context"]
# The zone names app.py would otherwise look up with ec2:DescribeAvailabilityZones (foundation/azs.py): a cached entry means no
# AWS call from the tests; us-east-1d is not in the fallback list, which proves the context entry is what the stacks use.
HARNESS_AZS = ["us-east-1a", "us-east-1b", "us-east-1d"]
CDK_CONTEXT["agent-platform:harness-azs:123456789012:us-east-1"] = HARNESS_AZS

from app import build_app  # noqa: E402

from foundation import config  # noqa: E402


def test_release_pipeline_owns_the_gated_sequence(templates):
    from platform_sdk import names

    template = templates[config.STACK_PLATFORM]
    pipelines = template.find_resources("AWS::CodePipeline::Pipeline")
    assert len(pipelines) == 1
    pipeline = next(iter(pipelines.values()))["Properties"]
    assert pipeline["Name"] == names.RELEASE_PIPELINE_NAME
    assert pipeline["PipelineType"] == "V2"
    assert pipeline["ExecutionMode"] == "QUEUED"
    assert [stage["Name"] for stage in pipeline["Stages"]] == list(names.RELEASE_STAGES)
    assert [v["Name"] for v in pipeline["Variables"]] == list(names.RELEASE_PIPELINE_VARIABLES)
    source = pipeline["Stages"][0]["Actions"][0]
    assert source["Configuration"]["S3ObjectKey"] == names.RELEASE_SOURCE_KEY
    assert str(source["Configuration"]["PollForSourceChanges"]).lower() == "false"
    validation = pipeline["Stages"][1]
    assert validation["Name"] == "Validate"
    assert [action["Name"] for action in validation["Actions"]] == ["DeployDev", "Evaluate"]
    assert [action["RunOrder"] for action in validation["Actions"]] == [1, 2]
    for action in validation["Actions"]:
        assert action["ActionTypeId"]["Provider"] == "Lambda"
        parameters = json.loads(action["Configuration"]["UserParameters"])
        assert parameters["executionId"] == "#{codepipeline.PipelineExecutionId}"
        assert parameters["releaseId"] == "#{variables.releaseId}"
    assert pipeline["Stages"][2]["Actions"][0]["ActionTypeId"]["Category"] == "Approval"
    promotion = pipeline["Stages"][3]["Actions"]
    assert [a["ActionTypeId"]["Category"] for a in promotion] == ["Approval", "Invoke"]
    assert promotion[1]["RunOrder"] > promotion[0]["RunOrder"]
    buckets = template.find_resources("AWS::S3::Bucket")
    assert len(buckets) == 2
    assert any(
        resource["Properties"].get("VersioningConfiguration", {}).get("Status") == "Enabled"
        for resource in buckets.values()
    )
    for resource in buckets.values():
        props = resource["Properties"]
        assert all(props["PublicAccessBlockConfiguration"].values())
        assert props["BucketEncryption"]


def test_supported_foundation_resources_have_retention_tag(templates):
    tagged_types = {
        "AWS::S3::Bucket",
        "AWS::Lambda::Function",
        "AWS::IAM::Role",
        "AWS::Logs::LogGroup",
        "AWS::DynamoDB::Table",
        "AWS::CodePipeline::Pipeline",
        "AWS::ECR::Repository",
        "AWS::SecretsManager::Secret",
        "AWS::SSM::Parameter",
        "AWS::EC2::VPC",
        "AWS::EC2::Subnet",
        "AWS::EC2::SecurityGroup",
        "AWS::RDS::DBInstance",
        "AWS::ECS::Cluster",
        "AWS::ECS::ExpressGatewayService",
        "AWS::ApiGatewayV2::Api",
        "AWS::CloudFront::Distribution",
    }
    for stack_name, template in templates.items():
        for logical_id, resource in template.to_json()["Resources"].items():
            if resource["Type"] in tagged_types:
                tags = resource["Properties"].get("Tags", [])
                values = tags if isinstance(tags, dict) else {tag["Key"]: tag["Value"] for tag in tags}
                assert values.get("auto-delete") == "no", f"{stack_name}/{logical_id}"


def test_all_buckets_declare_encryption(templates):
    for stack_name, template in templates.items():
        for logical_id, resource in template.find_resources("AWS::S3::Bucket").items():
            assert resource["Properties"].get("BucketEncryption"), f"{stack_name}/{logical_id}"


@pytest.fixture(scope="module")
def app() -> App:
    return build_app(context=CDK_CONTEXT)


@pytest.fixture(scope="module")
def templates(app: App) -> dict[str, Template]:
    return {
        name: Template.from_stack(app.node.find_child(name))
        for name in [
            config.STACK_NETWORK,
            config.STACK_SHARED,
            config.STACK_MODEL_GATEWAY,
            config.STACK_WEB,
            config.STACK_AUTH,
            config.STACK_PLATFORM,
            config.STACK_SCENARIO,
            config.STACK_IDP,
        ]
    }


def ssm_names(template: Template) -> set[str]:
    return {r["Properties"]["Name"] for r in template.find_resources("AWS::SSM::Parameter").values()}


def role_statements(template: Template, role_name: str) -> list[dict]:
    """Every statement of the inline and attached policies of a role, given its RoleName or its logical id (lists normalised)."""
    roles = template.find_resources("AWS::IAM::Role", {"Properties": {"RoleName": role_name}})
    if not roles:
        roles = {role_name: template.find_resources("AWS::IAM::Role")[role_name]}
    assert len(roles) == 1, f"role {role_name} not found"
    logical_id, role = next(iter(roles.items()))
    documents = [p["PolicyDocument"] for p in role["Properties"].get("Policies", [])]
    for policy in template.find_resources("AWS::IAM::Policy").values():
        if {"Ref": logical_id} in policy["Properties"].get("Roles", []):
            documents.append(policy["Properties"]["PolicyDocument"])
    statements = []
    for document in documents:
        for statement in document["Statement"]:
            actions = statement.get("Action", [])
            resources = statement.get("Resource", [])
            statements.append(
                {
                    **statement,
                    "Action": actions if isinstance(actions, list) else [actions],
                    "Resource": resources if isinstance(resources, list) else [resources],
                }
            )
    return statements


def resources_for(statements: list[dict], action: str) -> set:
    """Union of the resources of the Allow statements that grant `action` (literal resources only)."""
    found: set = set()
    for s in statements:
        if s.get("Effect") == "Allow" and action in s["Action"]:
            found.update(r for r in s["Resource"] if isinstance(r, str))
    return found


def test_stack_names(app: App) -> None:
    names = {child.node.id for child in app.node.children}
    assert names >= {
        "AgentPlatform-Network",
        "AgentPlatform-SharedServices",
        "AgentPlatform-ModelGateway",
        "AgentPlatform-Auth",
        "AgentPlatform-Platform",
        "AgentPlatform-Web",
        "AgentPlatform-Scenario",
        "AgentPlatform-Idp",
    }


def test_synth_makes_no_context_lookup(app: App) -> None:
    """`cdk synth` must work without credentials for any account: the Network stack seeds the CLI's `availability-zones:...`
    key with the resolved names (the Vpc construct validates explicit names against it), so the assembly reports no missing
    context and the CLI never calls EC2 itself. The fixture seeds only the harness-azs key, not the CLI's."""
    assembly = app.synth()
    # read the manifest file: jsii cannot hand `assembly.manifest` to Python (the tree artifact has no templateFile)
    manifest = json.loads((Path(assembly.directory) / "manifest.json").read_text(encoding="utf-8"))
    assert "missing" not in manifest, [m["key"] for m in manifest["missing"]]
    network = app.node.find_child(config.STACK_NETWORK)
    assert network.node.try_get_context("availability-zones:account=123456789012:region=us-east-1") == HARNESS_AZS


def test_network(templates) -> None:
    """Private landing zone: isolated subnets in the three resolved zones, no internet path, VPC endpoints instead."""
    t = templates[config.STACK_NETWORK]
    t.resource_count_is("AWS::EC2::VPC", 1)
    t.has_resource_properties("AWS::EC2::VPC", {"CidrBlock": "10.0.0.0/16"})
    t.resource_count_is("AWS::EC2::Subnet", 6)
    for never in ("InternetGateway", "VPCGatewayAttachment", "NatGateway", "EIP", "Route"):
        t.resource_count_is(f"AWS::EC2::{never}", 0)
    resources = t.to_json()["Resources"]
    subnets = {lid: r["Properties"] for lid, r in resources.items() if r["Type"] == "AWS::EC2::Subnet"}
    assert not any(s.get("MapPublicIpOnLaunch") for s in subnets.values())
    # one app and one data subnet per zone; the zones are the cached context entry, not the credential-less fallback
    assert config.HARNESS_AZS_CONTEXT_KEY == "agent-platform:harness-azs"
    assert sorted(s["AvailabilityZone"] for s in subnets.values()) == sorted(HARNESS_AZS * 2)
    assert sorted(s["AvailabilityZone"] for lid, s in subnets.items() if "appSubnet" in lid) == HARNESS_AZS
    # the S3 gateway endpoint on all six route tables, nine interface endpoints with private DNS in the first two zones
    endpoints = [r["Properties"] for r in resources.values() if r["Type"] == "AWS::EC2::VPCEndpoint"]
    gateway = [e for e in endpoints if e.get("VpcEndpointType", "Gateway") == "Gateway"]
    interface = [e for e in endpoints if e.get("VpcEndpointType") == "Interface"]
    assert len(gateway) == 1 and len(interface) == 9
    assert ".s3" in json.dumps(gateway[0]["ServiceName"]) and len(gateway[0]["RouteTableIds"]) == 6
    assert {e["ServiceName"] for e in interface} == {
        f"com.amazonaws.us-east-1.{service}"
        for service in (
            "secretsmanager",
            "logs",
            "monitoring",
            "xray",
            "ecr.api",
            "ecr.dkr",
            "bedrock-runtime",
            "bedrock-agentcore",
            "bedrock-agentcore.gateway",
        )
    }
    for endpoint in interface:
        assert endpoint["PrivateDnsEnabled"] is True
        placed_in = [subnets[ref["Ref"]]["AvailabilityZone"] for ref in endpoint["SubnetIds"]]
        assert placed_in == HARNESS_AZS[: config.ENDPOINT_AZ_COUNT], endpoint["ServiceName"]
        assert all("appSubnet" in ref["Ref"] for ref in endpoint["SubnetIds"])
        assert len(endpoint["SecurityGroupIds"]) == 1
    # security groups: the endpoints accept 443 from the VPC CIDR only; harness ENIs and the relay are egress-only
    groups = {
        r["Properties"]["GroupName"]: r["Properties"]
        for r in resources.values()
        if r["Type"] == "AWS::EC2::SecurityGroup"
    }
    assert set(groups) == {"agent-platform-endpoints", "agent-platform-harness", "agent-platform-relay"}
    ingress = groups["agent-platform-endpoints"]["SecurityGroupIngress"]
    assert [(i["IpProtocol"], i["FromPort"], i["ToPort"]) for i in ingress] == [("tcp", 443, 443)]
    assert "CidrBlock" in json.dumps(ingress[0]["CidrIp"])
    for name in ("agent-platform-harness", "agent-platform-relay"):
        assert "SecurityGroupIngress" not in groups[name]
        assert groups[name]["SecurityGroupEgress"][0]["CidrIp"] == "0.0.0.0/0"
    # nothing Lambda-backed is left: the VPC Block Public Access exclusion went away with the internet gateway
    t.resource_count_is("AWS::Lambda::Function", 0)
    t.resource_count_is("Custom::VpcBlockPublicAccessExclusion", 0)
    params = {
        r["Properties"]["Name"]: r["Properties"]["Value"] for r in t.find_resources("AWS::SSM::Parameter").values()
    }
    assert set(params) == {
        "/agent-platform/network/vpc-id",
        "/agent-platform/network/app-subnet-ids",
        "/agent-platform/network/harness-subnet-ids",
        "/agent-platform/network/harness-security-group-id",
        "/agent-platform/network/relay-security-group-id",
        "/agent-platform/network/availability-zones",
        "/agent-platform/network/private",
    }
    assert params["/agent-platform/network/private"] == "true"
    assert params["/agent-platform/network/availability-zones"] == ",".join(HARNESS_AZS)
    assert params["/agent-platform/network/harness-subnet-ids"] == params["/agent-platform/network/app-subnet-ids"]
    assert "appSubnet" in json.dumps(params["/agent-platform/network/app-subnet-ids"])
    assert {"VpcId", "AppSubnetIds", "DataSubnetIds"} <= set(t.to_json()["Outputs"])


def test_shared_services(templates) -> None:
    t = templates[config.STACK_SHARED]
    t.has_resource_properties(
        "AWS::Bedrock::Guardrail",
        {
            "Name": "hr-baseline",
            "BlockedInputMessaging": "Sorry, I can't help with that topic. Please contact HR directly.",
            "TopicPolicyConfig": {"TopicsConfig": [Match.object_like({"Name": "LegalAdvice", "Type": "DENY"})]},
        },
    )
    t.has_resource_properties(
        "AWS::Bedrock::Guardrail",
        {
            "ContentPolicyConfig": {
                "FiltersConfig": [{"Type": "PROMPT_ATTACK", "InputStrength": "HIGH", "OutputStrength": "NONE"}]
            },
            "SensitiveInformationPolicyConfig": {
                "PiiEntitiesConfig": Match.array_with([{"Type": "US_SOCIAL_SECURITY_NUMBER", "Action": "BLOCK"}])
            },
        },
    )
    t.resource_count_is("AWS::Bedrock::GuardrailVersion", 1)
    t.has_resource_properties("AWS::Bedrock::GuardrailVersion", {"Description": config.GUARDRAIL_VERSION_LABEL})
    # Evaluation role: confused-deputy conditions and the documented permissions (evaluations-prerequisites, 8 Oct 2026)
    t.has_resource_properties(
        "AWS::IAM::Role",
        {
            "RoleName": "agent-platform-evaluation-role",
            "AssumeRolePolicyDocument": {
                "Statement": [
                    Match.object_like(
                        {
                            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
                            "Condition": {
                                "StringEquals": {
                                    "aws:SourceAccount": "123456789012",
                                    "aws:ResourceAccount": "123456789012",
                                },
                                "ArnLike": {
                                    "aws:SourceArn": [
                                        "arn:aws:bedrock-agentcore:us-east-1:123456789012:evaluator/*",
                                        "arn:aws:bedrock-agentcore:us-east-1:123456789012:online-evaluation-config/*",
                                    ]
                                },
                            },
                        }
                    )
                ]
            },
        },
    )
    eval_statements = role_statements(t, "agent-platform-evaluation-role")
    assert resources_for(eval_statements, "logs:PutIndexPolicy") == {
        "arn:aws:logs:us-east-1:123456789012:log-group:aws/spans",
        "arn:aws:logs:us-east-1:123456789012:log-group:aws/spans:*",
    }
    assert resources_for(eval_statements, "logs:PutLogEvents") == {
        "arn:aws:logs:us-east-1:123456789012:log-group:/aws/bedrock-agentcore/evaluations/*"
    }
    assert resources_for(eval_statements, "bedrock:InvokeModel*") == {
        "arn:aws:bedrock:us-east-1::foundation-model/*",
        "arn:aws:bedrock:us-east-1:123456789012:inference-profile/*",
    }
    assert ssm_names(t) == {
        "/agent-platform/guardrail/id",
        "/agent-platform/guardrail/arn",
        "/agent-platform/guardrail/version",
        "/agent-platform/roles/evaluation-role-arn",
    }
    # Transaction Search: three ordered SDK calls, nothing on delete
    t.resource_count_is("Custom::TransactionSearchLogsPolicy", 1)
    t.resource_count_is("Custom::TransactionSearchDestination", 1)
    t.resource_count_is("Custom::TransactionSearchIndexing", 1)
    destination = list(t.find_resources("Custom::TransactionSearchDestination").values())[0]
    assert "Delete" not in destination["Properties"]
    assert json.loads(destination["Properties"]["Create"])["parameters"] == {"Destination": "CloudWatchLogs"}


def test_model_gateway(templates) -> None:
    t = templates[config.STACK_MODEL_GATEWAY]
    t.has_resource_properties(
        "AWS::ECS::ExpressGatewayService",
        {
            "ServiceName": "agent-platform-model-gateway",
            "Cpu": "1024",
            "Memory": "2048",
            "HealthCheckPath": "/health/liveliness",
            "PrimaryContainer": Match.object_like({"ContainerPort": 4000}),
            "ScalingTarget": {"MinTaskCount": 1, "MaxTaskCount": 1},
        },
    )
    # The image comes from the private repository filled by the deploy-time copy, never from ghcr.io at run time
    assert config.LITELLM_IMAGE.endswith(f":{config.LITELLM_IMAGE_TAG}")
    service_resource = list(t.find_resources("AWS::ECS::ExpressGatewayService").values())[0]
    service = service_resource["Properties"]
    image = json.dumps(service["PrimaryContainer"]["Image"])
    assert "ghcr.io" not in image and ".dkr.ecr." in image and image.endswith(':v1.104.1"]]}')
    t.has_resource_properties(
        "AWS::ECR::Repository", {"RepositoryName": "agent-platform/litellm", "EmptyOnDelete": True}
    )
    copies = t.find_resources("Custom::CDKECRDeployment")
    assert len(copies) == 1
    copy_id, copy = next(iter(copies.items()))
    assert (
        copy["Properties"]["SrcImage"] == "docker://ghcr.io/berriai/litellm:v1.104.1"
    )  # DockerImageName adds the scheme
    assert "LiteLLMRepository" in json.dumps(copy["Properties"]["DestImage"])
    assert copy_id in service_resource["DependsOn"]
    # Isolated app subnets (-> internal load balancer), referenced from the Network stack; never the data subnets
    subnets = json.dumps(service["NetworkConfiguration"]["Subnets"])
    assert len(service["NetworkConfiguration"]["Subnets"]) == 3
    assert subnets.count("appSubnet") == 3 and "dataSubnet" not in subnets and "AgentPlatform-Network" in subnets
    # Tags are create-only on the Express service and supplied explicitly.
    assert service["Tags"] == [
        {"Key": "auto-delete", "Value": "no"},
        {"Key": "ManagedBy", "Value": "cdk"},
        {"Key": "Project", "Value": "workshop-agent-platform"},
    ]
    env_names = [e["Name"] for e in service["PrimaryContainer"]["Environment"]]
    assert {"LITELLM_CONFIG_HASH", "GUARDRAIL_ID", "GUARDRAIL_VERSION", "AWS_REGION"} <= set(env_names)
    # every other resource still carries the app-wide tags
    cluster = list(t.find_resources("AWS::ECS::Cluster").values())[0]["Properties"]
    assert {"Key": "Project", "Value": "workshop-agent-platform"} in cluster["Tags"]
    t.has_resource_properties(
        "AWS::RDS::DBInstance",
        {
            "Engine": "postgres",
            "DBInstanceClass": "db.t3.micro",
            "DBName": "litellm",
            "PubliclyAccessible": False,
            "BackupRetentionPeriod": 0,
            "DeleteAutomatedBackups": True,
            "AutoMinorVersionUpgrade": False,
            "PreferredMaintenanceWindow": "sun:06:00-sun:06:30",
        },
    )
    t.has_resource("AWS::RDS::DBInstance", {"DeletionPolicy": "Delete"})
    t.has_resource_properties("AWS::SecretsManager::Secret", {"Name": "agent-platform/litellm-master-key"})
    t.has_resource_properties("AWS::SecretsManager::Secret", {"Name": "agent-platform/litellm-salt-key"})
    t.resource_count_is("Custom::LiteLLMMasterKey", 1)
    t.resource_count_is("Custom::AlbIdleTimeout", 1)
    # every log group, including the one cdk-ecr-deployment creates for its own Lambda, keeps logs 30 days (and is destroyable)
    retentions = {r["Properties"].get("RetentionInDays") for r in t.find_resources("AWS::Logs::LogGroup").values()}
    assert retentions == {config.LOG_RETENTION_DAYS}
    # The relay: inside the VPC on the app subnets, the proxy URL in its environment, no permissions beyond VPC access + logs
    relays = t.find_resources(
        "AWS::Lambda::Function", {"Properties": {"FunctionName": "agent-platform-model-gateway-relay"}}
    )
    assert len(relays) == 1
    relay = next(iter(relays.values()))["Properties"]
    assert (relay["Runtime"], relay["Handler"], relay["Timeout"], relay["MemorySize"]) == (
        "python3.13",
        "index.handler",
        120,
        512,
    )
    assert "MODEL_GATEWAY_URL" in relay["Code"]["ZipFile"] and "boto3" not in relay["Code"]["ZipFile"]
    assert (
        len(relay["VpcConfig"]["SubnetIds"]) == 3
        and json.dumps(relay["VpcConfig"]["SubnetIds"]).count("appSubnet") == 3
    )
    assert "RelaySecurityGroup" in json.dumps(relay["VpcConfig"]["SecurityGroupIds"])
    url = relay["Environment"]["Variables"]["MODEL_GATEWAY_URL"]
    assert json.dumps(url).count("Endpoint") == 1
    # one scheme whatever the Endpoint attribute carries: "https://ag-" + the part after the single "ag-" marker
    assert url["Fn::Join"][1][0] == "https://ag-" and url["Fn::Join"][1][1]["Fn::Select"][0] == 1
    assert url["Fn::Join"][1][1]["Fn::Select"][1]["Fn::Split"][0] == "ag-"
    relay_role = relay["Role"]["Fn::GetAtt"][0]
    managed = json.dumps(t.to_json()["Resources"][relay_role]["Properties"]["ManagedPolicyArns"])
    assert "AWSLambdaVPCAccessExecutionRole" in managed and "AWSLambdaBasicExecutionRole" in managed
    assert role_statements(t, relay_role) == []
    assert ssm_names(t) == {
        "/agent-platform/model-gateway/url",
        "/agent-platform/model-gateway/master-key-secret-arn",
        "/agent-platform/model-gateway/models",
        "/agent-platform/model-gateway/relay-function-name",
        "/agent-platform/model-gateway/relay-function-arn",
        "/agent-platform/model-gateway/private",
    }
    private = [
        r for r in t.find_resources("AWS::SSM::Parameter").values() if r["Properties"]["Name"].endswith("/private")
    ]
    assert private[0]["Properties"]["Value"] == "true"
    models_param = [
        r for r in t.find_resources("AWS::SSM::Parameter").values() if r["Properties"]["Name"].endswith("/models")
    ][0]
    aliases = [m["alias"] for m in json.loads(models_param["Properties"]["Value"])]
    assert aliases == ["claude-opus-5", "claude-sonnet-5", "gpt-6-1-sol", "nova-2-lite"]


def test_litellm_config_template_matches_models() -> None:
    template = config.LITELLM_CONFIG_TEMPLATE.read_text()
    assert "us-east-1" not in template  # the region is rendered from the CDK environment, never hardcoded
    text = (
        template.replace("{{GUARDRAIL_ID}}", "gr")
        .replace("{{GUARDRAIL_VERSION}}", "1")
        .replace("{{REGION}}", "eu-west-1")
    )
    assert "{{" not in text
    doc = yaml.safe_load(text)
    by_alias = {m["model_name"]: m for m in doc["model_list"]}
    assert list(by_alias) == [m["alias"] for m in config.MODELS]
    for m in config.MODELS:
        params = by_alias[m["alias"]]["litellm_params"]
        assert params["model"] == f"bedrock/converse/{m['modelId']}"
        assert params["aws_region_name"] == "eu-west-1"
        assert params["guardrailConfig"] == {"guardrailIdentifier": "gr", "guardrailVersion": "1", "trace": "enabled"}
        assert params["input_cost_per_token"] == m["inputCostPerToken"]
        assert params["output_cost_per_token"] == m["outputCostPerToken"]
    assert doc["general_settings"] == {
        "master_key": "os.environ/LITELLM_MASTER_KEY",
        "store_model_in_db": False,
        "database_connection_pool_limit": 5,
        "proxy_batch_write_at": 10,
    }
    assert doc["litellm_settings"] == {"drop_params": True}


def test_auth(templates) -> None:
    t = templates[config.STACK_AUTH]
    t.has_resource_properties(
        "AWS::Cognito::UserPool", {"UserPoolName": "agent-platform-users", "MfaConfiguration": "OFF"}
    )
    t.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {
            "ClientName": "agent-platform-ui",
            "GenerateSecret": False,
            "AllowedOAuthFlows": ["code"],
            "ExplicitAuthFlows": Match.array_with(["ALLOW_USER_PASSWORD_AUTH", "ALLOW_USER_SRP_AUTH"]),
            "CallbackURLs": Match.array_with(["http://localhost:5173/"]),
        },
    )
    # groups, users and passwords are created by `workshop apply 2` (module 2), not by the stack
    t.resource_count_is("AWS::Cognito::UserPoolGroup", 0)
    t.resource_count_is("AWS::Cognito::UserPoolUser", 0)
    t.resource_count_is("AWS::Cognito::UserPoolUserToGroupAttachment", 0)
    t.resource_count_is("AWS::Lambda::Function", 0)
    t.has_resource_properties("AWS::SecretsManager::Secret", {"Name": "agent-platform/users"})
    assert ssm_names(t) == {
        "/agent-platform/auth/user-pool-id",
        "/agent-platform/auth/client-id",
        "/agent-platform/auth/domain",
        "/agent-platform/auth/users-secret-arn",
    }


def test_platform_does_not_depend_on_the_model_gateway(templates) -> None:
    """The control plane (Web -> Auth -> Platform) deploys before the landing zone: no reference to the ModelGateway stack."""
    assert config.STACK_MODEL_GATEWAY not in json.dumps(templates[config.STACK_PLATFORM].to_json())
    assert config.STACK_MODEL_GATEWAY not in json.dumps(templates[config.STACK_AUTH].to_json())
    assert config.PRE_DAY_STACKS == [config.STACK_WEB, config.STACK_AUTH, config.STACK_PLATFORM, config.STACK_SCENARIO]
    assert config.LANDING_ZONE_STACKS == [config.STACK_NETWORK, config.STACK_SHARED, config.STACK_MODEL_GATEWAY]
    assert config.OBO_STACKS == [config.STACK_IDP]


def test_platform(templates) -> None:
    t = templates[config.STACK_PLATFORM]
    t.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": "agent-platform",
            "BillingMode": "PAY_PER_REQUEST",
            "TimeToLiveSpecification": {"AttributeName": "ttl", "Enabled": True},
            "GlobalSecondaryIndexes": [Match.object_like({"IndexName": "GSI1"})],
        },
    )
    t.has_resource_properties(
        "AWS::Lambda::Function",
        {"FunctionName": "agent-platform-api", "Handler": "app.handler", "Runtime": "python3.13", "Timeout": 29},
    )
    t.has_resource_properties(
        "AWS::Lambda::Function", {"FunctionName": "agent-platform-worker", "Handler": "worker.handler", "Timeout": 900}
    )
    t.has_resource_properties("AWS::ApiGatewayV2::Authorizer", {"AuthorizerType": "JWT"})
    t.has_resource_properties("AWS::ApiGatewayV2::Route", {"RouteKey": "GET /health", "AuthorizationType": "NONE"})
    t.has_resource_properties("AWS::ApiGatewayV2::Route", {"RouteKey": "$default", "AuthorizationType": "JWT"})
    t.has_resource_properties(
        "AWS::ApiGatewayV2::Route", {"RouteKey": "OPTIONS /{proxy+}", "AuthorizationType": "NONE"}
    )
    t.has_resource_properties("AWS::Events::Rule", {"EventPattern": {"source": ["aws.agent-registry"]}})
    t.has_resource_properties("AWS::IAM::Role", {"RoleName": "agent-platform-backend-role"})
    # The two AgentCore Runtime service-linked roles (first CreateHarness; first harness in VPC mode): created up front, and
    # creatable by the backend as a second line of defence (fresh accounts, 15 Oct 2026)
    for name, service in (
        ("Runtime", "runtime-identity.bedrock-agentcore.amazonaws.com"),
        ("Network", "network.bedrock-agentcore.amazonaws.com"),
    ):
        t.resource_count_is(f"Custom::AgentCore{name}ServiceLinkedRole", 1)
        t.has_resource_properties(
            f"Custom::AgentCore{name}ServiceLinkedRole",
            {
                "Create": Match.serialized_json(
                    Match.object_like(
                        {
                            "action": "CreateServiceLinkedRole",
                            "parameters": {"AWSServiceName": service},
                            "ignoreErrorCodesMatching": "InvalidInput.*",
                        }
                    )
                )
            },
        )
    slr_statements = [
        s for s in role_statements(t, "agent-platform-backend-role") if "iam:CreateServiceLinkedRole" in s["Action"]
    ]
    assert len(slr_statements) == 1
    assert set(slr_statements[0]["Condition"]["StringEquals"]["iam:AWSServiceName"]) == {
        "runtime-identity.bedrock-agentcore.amazonaws.com",
        "network.bedrock-agentcore.amazonaws.com",
    }
    assert set(slr_statements[0]["Resource"]) == {
        "arn:aws:iam::123456789012:role/aws-service-role/runtime-identity.bedrock-agentcore.amazonaws.com/*",
        "arn:aws:iam::123456789012:role/aws-service-role/network.bedrock-agentcore.amazonaws.com/*",
    }
    # Backend role grants (review of 8 Oct 2026: Lambda and Secrets Manager ARNs use ":" before the resource name). The relay
    # is how the backend reaches the private model gateway; its ARN is a string so the stack stays independent of ModelGateway.
    statements = role_statements(t, "agent-platform-backend-role")
    assert resources_for(statements, "lambda:InvokeFunction") == {
        "arn:aws:lambda:us-east-1:123456789012:function:agent-platform-worker",
        "arn:aws:lambda:us-east-1:123456789012:function:agent-platform-model-gateway-relay",
    }
    secrets = resources_for(statements, "secretsmanager:GetSecretValue")
    assert secrets == {
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:agent-platform/litellm-master-key-*",
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:agent-platform/users-*",  # POST /me/switch
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:bedrock-agentcore-identity!default/*",
    }
    assert not any("secret/" in r for r in secrets)
    assert resources_for(statements, "secretsmanager:CreateSecret") == {
        "arn:aws:secretsmanager:us-east-1:123456789012:secret:bedrock-agentcore-identity!default/*"
    }
    # GET /governance (guardrail names) and GET /governance/project-options (knowledge-base pick list)
    assert resources_for(statements, "bedrock:GetGuardrail") == {"*"}
    assert resources_for(statements, "bedrock:ListKnowledgeBases") == {"*"}
    assert resources_for(statements, "ssm:GetParametersByPath") == {
        "arn:aws:ssm:us-east-1:123456789012:parameter/agent-platform",
        "arn:aws:ssm:us-east-1:123456789012:parameter/agent-platform/*",
    }
    for action in (
        "iam:UpdateAssumeRolePolicy",
        "iam:ListAttachedRolePolicies",
        "iam:DetachRolePolicy",
        "iam:UntagRole",
        "iam:PutRolePolicy",
    ):
        assert resources_for(statements, action) == {"arn:aws:iam::*:role/agent-platform-proj-*"}, action
    pass_role = [s for s in statements if "iam:PassRole" in s["Action"]]
    assert len(pass_role) == 1
    assert set(pass_role[0]["Resource"]) == {
        "arn:aws:iam::*:role/agent-platform-proj-*",
        "arn:aws:iam::123456789012:role/agent-platform-evaluation-role",
    }
    assert pass_role[0]["Condition"] == {"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}}
    assert ssm_names(t) == {
        "/agent-platform/api/url",
        "/agent-platform/table/name",
        "/agent-platform/backend/worker-function-name",
        "/agent-platform/roles/backend-role-arn",
        "/agent-platform/release-pipeline/name",
        "/agent-platform/release-pipeline/source-bucket",
    }


def test_web(templates) -> None:
    t = templates[config.STACK_WEB]
    t.resource_count_is("AWS::CloudFront::Distribution", 1)
    t.has_resource_properties(
        "AWS::CloudFront::Distribution",
        {
            "DistributionConfig": Match.object_like(
                {
                    "DefaultRootObject": "index.html",
                    "PriceClass": "PriceClass_100",
                }
            )
        },
    )
    t.has_resource_properties(
        "AWS::S3::Bucket", {"PublicAccessBlockConfiguration": Match.object_like({"BlockPublicAcls": True})}
    )
    assert ssm_names(t) == {"/agent-platform/ui/url"}


def test_scenario(templates) -> None:
    t = templates[config.STACK_SCENARIO]
    t.resource_count_is("AWS::DynamoDB::Table", 3)
    t.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "TableName": "agent-platform-leave-requests",
            "BillingMode": "PAY_PER_REQUEST",
            "KeySchema": [{"AttributeName": "request_id", "KeyType": "HASH"}],
            "GlobalSecondaryIndexes": [
                Match.object_like({"IndexName": "by_employee", "Projection": {"ProjectionType": "ALL"}})
            ],
        },
    )
    t.has_resource_properties("AWS::DynamoDB::Table", {"TableName": "agent-platform-employees"})
    t.has_resource_properties("AWS::DynamoDB::Table", {"TableName": "agent-platform-invoices"})
    for name, variables in (
        ("agent-platform-hr-tools", {"EMPLOYEES_TABLE", "LEAVE_REQUESTS_TABLE", "INVOICES_TABLE"}),
        ("agent-platform-finance-tools", {"INVOICES_TABLE"}),
    ):
        functions = t.find_resources("AWS::Lambda::Function", {"Properties": {"FunctionName": name}})
        assert len(functions) == 1, name
        props = next(iter(functions.values()))["Properties"]
        assert props["Handler"] == "handler.handler" and props["Runtime"] == "python3.13" and props["Timeout"] == 30
        assert set(props["Environment"]["Variables"]) == variables, name
    # the HR tools only read employees (balances, profiles) and write leave requests; Finance only reads invoices
    hr_statements = role_statements(t, _role_of(t, "agent-platform-hr-tools"))
    hr_write = json.dumps([r for s in hr_statements if "dynamodb:PutItem" in s["Action"] for r in s["Resource"]])
    assert "TableLeaveRequests" in hr_write and "TableEmployees" not in hr_write  # table ARNs are GetAtt references
    hr_read = json.dumps([r for s in hr_statements if "dynamodb:GetItem" in s["Action"] for r in s["Resource"]])
    assert "TableEmployees" in hr_read
    # one shared log group for the three bucket deployments (BucketDeployment uses one Lambda per stack)
    log_group_names = {r["Properties"]["LogGroupName"] for r in t.find_resources("AWS::Logs::LogGroup").values()}
    assert "/aws/lambda/agent-platform-scenario-deployments" in log_group_names
    assert not any("scenario-dataset" in n or "scenario-docs" in n for n in log_group_names)
    t.has_resource_properties(
        "AWS::Bedrock::KnowledgeBase",
        {
            "Name": "agent-platform-policies",
            "KnowledgeBaseConfiguration": {
                "Type": "MANAGED",
                "ManagedKnowledgeBaseConfiguration": {"EmbeddingModelType": "MANAGED"},
            },
        },
    )
    t.has_resource_properties(
        "AWS::Bedrock::DataSource",
        {
            "Name": "policy-docs",
            "DataDeletionPolicy": "DELETE",
            "DataSourceConfiguration": {
                "Type": "MANAGED_KNOWLEDGE_BASE_CONNECTOR",
                "ManagedKnowledgeBaseConnectorConfiguration": {
                    "ConnectorParameters": Match.object_like({"type": "S3", "version": "1"})
                },
            },
        },
    )
    t.has_resource_properties("AWS::IAM::Role", {"RoleName": "agent-platform-kb-role"})
    t.resource_count_is("Custom::ScenarioSeed", 1)
    t.resource_count_is("Custom::KnowledgeBaseIngestion", 1)
    t.resource_count_is("Custom::CDKBucketDeployment", 3)  # docs, dataset/evaluation, dataset/blueprint
    t.resource_count_is("AWS::S3::Bucket", 2)
    assert ssm_names(t) == {
        "/agent-platform/scenario/kb-id",
        "/agent-platform/scenario/kb-arn",
        "/agent-platform/scenario/docs-bucket",
        "/agent-platform/scenario/tools/hr-lambda-arn",
        "/agent-platform/scenario/tools/finance-lambda-arn",
        "/agent-platform/scenario/tables/employees",
        "/agent-platform/scenario/tables/leave-requests",
        "/agent-platform/scenario/tables/invoices",
        "/agent-platform/scenario/dataset-bucket",
    }
    # the seed asset carries the handler and the three data files
    staged = sorted(p.name for p in (config.BUILD_SEED_DIR / "data").iterdir())
    assert staged == ["employees.json", "holidays.json", "invoices.json", "leave_requests.json"]
    assert (config.BUILD_SEED_DIR / "handler.py").exists()


def test_idp(templates) -> None:
    """Module 2: Keycloak (the corporate IdP) on Fargate behind an internal load balancer, exposed over HTTPS by an HTTP API with
    a VPC link, in a public VPC of its own; the HR API behind a JWT authorizer. A third independent chain: no reference to any
    other stack, the table names are the Scenario stack's constants."""
    t = templates[config.STACK_IDP]
    body = json.dumps(t.to_json())
    assert "Fn::ImportValue" not in body and "AgentPlatform-Scenario" not in body
    # A small public VPC: two public subnets in the first two resolved zones, an internet gateway, no NAT, no endpoints
    t.resource_count_is("AWS::EC2::VPC", 1)
    t.has_resource_properties("AWS::EC2::VPC", {"CidrBlock": "10.1.0.0/16"})
    t.resource_count_is("AWS::EC2::InternetGateway", 1)
    t.resource_count_is("AWS::EC2::VPCGatewayAttachment", 1)
    t.resource_count_is("AWS::EC2::NatGateway", 0)
    t.resource_count_is("AWS::EC2::VPCEndpoint", 0)
    subnets = [r["Properties"] for r in t.find_resources("AWS::EC2::Subnet").values()]
    assert len(subnets) == 2 and all(s["MapPublicIpOnLaunch"] is True for s in subnets)
    assert sorted(s["AvailabilityZone"] for s in subnets) == HARNESS_AZS[:2]
    # The VPC Block Public Access exclusion is back (the dev account blocks ingress; commit 894f915): inline Lambda, unnamed logs
    t.resource_count_is("Custom::VpcBlockPublicAccessExclusion", 1)
    t.has_resource_properties(
        "Custom::VpcBlockPublicAccessExclusion",
        {"Name": "agent-platform-idp-vpc", "Project": "workshop-agent-platform"},
    )
    bpa_functions = [
        r["Properties"]
        for r in t.find_resources("AWS::Lambda::Function").values()
        if "create_vpc_block_public_access_exclusion" in r["Properties"].get("Code", {}).get("ZipFile", "")
    ]
    assert len(bpa_functions) == 1 and bpa_functions[0]["Timeout"] == 300
    log_groups = t.find_resources("AWS::Logs::LogGroup")
    assert {r["Properties"].get("RetentionInDays") for r in log_groups.values()} == {config.LOG_RETENTION_DAYS}
    # five unnamed groups: the BPA Lambda and its provider, the SDK-call group of the service-linked-role custom resource, the
    # readiness Lambda and its provider (they run during stack deletion; a fixed name would block the next deploy after a
    # rollback)
    assert sum("LogGroupName" not in r["Properties"] for r in log_groups.values()) == 5
    named = {r["Properties"]["LogGroupName"] for r in log_groups.values() if "LogGroupName" in r["Properties"]}
    assert named == {
        "/agent-platform/keycloak",
        "/aws/lambda/agent-platform-hr-api",
        "/agent-platform/hr-api-access",
        "/agent-platform/idp-access",
    }
    # Express Mode is gone (no health-check grace period; Keycloak needs ~85 s to start): standard Fargate behind an internal ALB
    t.resource_count_is("AWS::ECS::ExpressGatewayService", 0)
    assert "AmazonECSInfrastructureRoleforExpressGatewayServices" not in body
    t.has_resource_properties("AWS::ECS::Cluster", {"ClusterName": "agent-platform-idp"})
    apis = {r["Properties"]["Name"]: lid for lid, r in t.find_resources("AWS::ApiGatewayV2::Api").items()}
    assert set(apis) == {"agent-platform-idp", "agent-platform-hr-api"}
    idp_api_id, hr_api_id = apis["agent-platform-idp"], apis["agent-platform-hr-api"]
    # the task definition: an ephemeral volume realm-init fills with realm.json, which Keycloak imports at start-up
    t.resource_count_is("AWS::ECS::TaskDefinition", 1)
    task_def = next(iter(t.find_resources("AWS::ECS::TaskDefinition").values()))["Properties"]
    assert (task_def["Cpu"], task_def["Memory"], task_def["NetworkMode"], task_def["RequiresCompatibilities"]) == (
        "1024",
        "2048",
        "awsvpc",
        ["FARGATE"],
    )
    assert task_def["Volumes"] == [{"Name": "import"}]
    containers = {c["Name"]: c for c in task_def["ContainerDefinitions"]}
    assert list(containers) == ["realm-init", "keycloak"]
    realm_init = containers["realm-init"]
    assert realm_init["Image"] == "public.ecr.aws/docker/library/busybox:1.37" and realm_init["Essential"] is False
    assert realm_init["Command"] == [
        "sh",
        "-c",
        "printf '%s' \"$REALM_JSON\" > /import/anycompany.json && echo wrote /import/anycompany.json",
    ]
    assert realm_init["MountPoints"] == [{"ContainerPath": "/import", "SourceVolume": "import", "ReadOnly": False}]
    assert realm_init["LogConfiguration"]["Options"]["awslogs-stream-prefix"] == "realm-init"
    realm_env = {e["Name"]: e["Value"] for e in realm_init["Environment"]}
    assert list(realm_env) == ["REALM_JSON"]
    # Fn::Sub with the Keycloak placeholders escaped (`${!...}`): lint rule E1029 forbids a bare `${...}` in a template string;
    # CloudFormation renders the escape back, so the container gets the file verbatim
    realm_body = realm_env["REALM_JSON"]["Fn::Sub"]
    assert realm_body.replace("${!", "${") == config.KEYCLOAK_REALM_FILE.read_text(encoding="utf-8")
    assert '"realm": "anycompany"' in realm_body and "${!KC_SECRET_HR_AGENT}" in realm_body
    assert "${KC_SECRET" not in realm_body  # no unescaped placeholder survives
    container = containers["keycloak"]
    assert container["Image"] == "quay.io/keycloak/keycloak:26.8.1" and container["Essential"] is True
    assert container["Command"] == ["start-dev", "--import-realm"]
    assert container["DependsOn"] == [{"ContainerName": "realm-init", "Condition": "SUCCESS"}]
    assert container["MountPoints"] == [
        {"ContainerPath": "/opt/keycloak/data/import", "SourceVolume": "import", "ReadOnly": True}
    ]
    assert [(p["ContainerPort"], p["Protocol"]) for p in container["PortMappings"]] == [(8080, "tcp")]
    assert {e["Name"]: e["Value"] for e in container["Environment"]} == {
        "KC_HTTP_ENABLED": "true",
        "KC_HTTP_PORT": "8080",
        "KC_HOSTNAME": {"Fn::GetAtt": [idp_api_id, "ApiEndpoint"]},
        "KC_HOSTNAME_STRICT": "false",
        "KC_PROXY_HEADERS": "xforwarded",
        "KC_HEALTH_ENABLED": "true",
        "KC_BOOTSTRAP_ADMIN_USERNAME": "admin",
        "KC_LOG_LEVEL": "INFO",
    }
    # three generated secrets: the bootstrap admin password and the two confidential clients' secrets the realm import resolves
    secrets = {r["Properties"]["Name"]: lid for lid, r in t.find_resources("AWS::SecretsManager::Secret").items()}
    assert set(secrets) == {
        "agent-platform/keycloak-admin",
        "agent-platform/keycloak-client-hr-agent",
        "agent-platform/keycloak-client-hr-obo-gateway",
    }
    secret_id = secrets["agent-platform/keycloak-admin"]
    assert {s["Name"]: s["ValueFrom"] for s in container["Secrets"]} == {
        "KC_BOOTSTRAP_ADMIN_PASSWORD": {"Ref": secret_id},
        "KC_SECRET_HR_AGENT": {"Ref": secrets["agent-platform/keycloak-client-hr-agent"]},
        "KC_SECRET_HR_OBO_GATEWAY": {"Ref": secrets["agent-platform/keycloak-client-hr-obo-gateway"]},
    }
    assert container["LogConfiguration"]["LogDriver"] == "awslogs"
    assert container["LogConfiguration"]["Options"]["awslogs-stream-prefix"] == "keycloak"
    t.has_resource_properties(
        "AWS::SecretsManager::Secret",
        {
            "Name": "agent-platform/keycloak-admin",
            "GenerateSecretString": {"ExcludePunctuation": True, "PasswordLength": 32},
        },
    )
    for name in ("agent-platform/keycloak-client-hr-agent", "agent-platform/keycloak-client-hr-obo-gateway"):
        t.has_resource_properties(
            "AWS::SecretsManager::Secret",
            {"Name": name, "GenerateSecretString": {"ExcludePunctuation": True, "PasswordLength": 40}},
        )
    # the service: one task, public address for the pulls, five-minute grace period, stop-before-start, circuit breaker
    t.resource_count_is("AWS::ECS::Service", 1)
    service_resource = next(iter(t.find_resources("AWS::ECS::Service").values()))
    service = service_resource["Properties"]
    assert service["ServiceName"] == "agent-platform-keycloak" and service["LaunchType"] == "FARGATE"
    assert service["DesiredCount"] == 1 and service["HealthCheckGracePeriodSeconds"] == 300
    assert service["DeploymentConfiguration"] == {
        "MinimumHealthyPercent": 0,
        "MaximumPercent": 100,
        "DeploymentCircuitBreaker": {"Enable": True, "Rollback": False},
    }
    awsvpc = service["NetworkConfiguration"]["AwsvpcConfiguration"]
    assert awsvpc["AssignPublicIp"] == "ENABLED"
    assert len(awsvpc["Subnets"]) == 2 and json.dumps(awsvpc["Subnets"]).count("publicSubnet") == 2
    assert [(lb["ContainerName"], lb["ContainerPort"]) for lb in service["LoadBalancers"]] == [("keycloak", 8080)]
    # the internal load balancer and its health check
    t.resource_count_is("AWS::ElasticLoadBalancingV2::LoadBalancer", 1)
    alb_id, alb_resource = next(iter(t.find_resources("AWS::ElasticLoadBalancingV2::LoadBalancer").items()))
    alb = alb_resource["Properties"]
    assert alb["Scheme"] == "internal" and alb["Type"] == "application"
    assert len(alb["Subnets"]) == 2 and json.dumps(alb["Subnets"]).count("publicSubnet") == 2
    t.resource_count_is("AWS::ElasticLoadBalancingV2::Listener", 1)
    listener_id = next(iter(t.find_resources("AWS::ElasticLoadBalancingV2::Listener")))
    t.has_resource_properties("AWS::ElasticLoadBalancingV2::Listener", {"Port": 80, "Protocol": "HTTP"})
    t.resource_count_is("AWS::ElasticLoadBalancingV2::TargetGroup", 1)
    t.has_resource_properties(
        "AWS::ElasticLoadBalancingV2::TargetGroup",
        {
            "Port": 8080,
            "Protocol": "HTTP",
            "TargetType": "ip",
            "HealthCheckPath": "/realms/master",
            "HealthCheckIntervalSeconds": 15,
            "HealthCheckTimeoutSeconds": 5,
            "HealthyThresholdCount": 2,
            "UnhealthyThresholdCount": 5,
            "Matcher": {"HttpCode": "200"},
            "TargetGroupAttributes": Match.array_with([{"Key": "deregistration_delay.timeout_seconds", "Value": "30"}]),
        },
    )
    # the service waits for the listener (target group attached), the ECS service-linked role and the exclusion (whole VPC)
    slrs = t.find_resources("Custom::ServiceLinkedRole")
    assert len(slrs) == 1
    slr_call = json.loads(next(iter(slrs.values()))["Properties"]["Create"])
    assert slr_call["action"] == "CreateServiceLinkedRole" and slr_call["ignoreErrorCodesMatching"] == "InvalidInput.*"
    assert slr_call["parameters"] == {"AWSServiceName": "ecs.amazonaws.com"}
    assert "Delete" not in next(iter(slrs.values()))["Properties"]
    exclusion_id = next(iter(t.find_resources("Custom::VpcBlockPublicAccessExclusion")))
    assert {listener_id, exclusion_id} | set(slrs) <= set(service_resource["DependsOn"])
    # security groups: VPC link -> load balancer (80) -> task (8080); no inline rules, nothing from the internet
    security_groups = t.find_resources("AWS::EC2::SecurityGroup")
    assert len(security_groups) == 3
    assert not any("SecurityGroupIngress" in r["Properties"] for r in security_groups.values())
    vpc_link_id, vpc_link = next(iter(t.find_resources("AWS::ApiGatewayV2::VpcLink").items()))
    task_sg_id = awsvpc["SecurityGroups"][0]["Fn::GetAtt"][0]
    alb_sg_id = alb["SecurityGroups"][0]["Fn::GetAtt"][0]
    link_sg_id = vpc_link["Properties"]["SecurityGroupIds"][0]["Fn::GetAtt"][0]
    assert {task_sg_id, alb_sg_id, link_sg_id} == set(security_groups)
    rules = {
        (r["Properties"]["GroupId"]["Fn::GetAtt"][0], r["Properties"]["SourceSecurityGroupId"]["Fn::GetAtt"][0]): (
            r["Properties"]["IpProtocol"],
            r["Properties"]["FromPort"],
            r["Properties"]["ToPort"],
        )
        for r in t.find_resources("AWS::EC2::SecurityGroupIngress").values()
    }
    assert rules == {(task_sg_id, alb_sg_id): ("tcp", 8080, 8080), (alb_sg_id, link_sg_id): ("tcp", 80, 80)}
    # roles: execution (pull, logs, the admin secret), task (nothing: Keycloak calls no AWS API)
    roles = {lid: r["Properties"] for lid, r in t.find_resources("AWS::IAM::Role").items()}
    exec_id = task_def["ExecutionRoleArn"]["Fn::GetAtt"][0]
    assert "AmazonECSTaskExecutionRolePolicy" in json.dumps(roles[exec_id]["ManagedPolicyArns"])
    exec_secret = [s for s in role_statements(t, exec_id) if "secretsmanager:GetSecretValue" in s["Action"]]
    assert (
        len(exec_secret) == 1
    )  # one merged statement (minimizePolicies): the admin password and the two client secrets
    assert sorted(json.dumps(r) for r in exec_secret[0]["Resource"]) == sorted(
        json.dumps({"Ref": lid}) for lid in secrets.values()
    )
    task_id = task_def["TaskRoleArn"]["Fn::GetAtt"][0]
    assert role_statements(t, task_id) == [] and "ManagedPolicyArns" not in roles[task_id]
    # the HTTPS door: an HTTP API proxying every path to the listener through the VPC link, no authorizer (Keycloak authenticates)
    t.resource_count_is("AWS::ApiGatewayV2::VpcLink", 1)
    assert len(vpc_link["Properties"]["SubnetIds"]) == 2
    assert json.dumps(vpc_link["Properties"]["SubnetIds"]).count("publicSubnet") == 2
    routes = {
        r["Properties"]["RouteKey"]: r["Properties"] for r in t.find_resources("AWS::ApiGatewayV2::Route").values()
    }
    assert set(routes) == {"ANY /{proxy+}", "ANY /", "POST /tools/{tool}"}
    for key in ("ANY /{proxy+}", "ANY /"):
        assert routes[key]["ApiId"] == {"Ref": idp_api_id} and routes[key].get("AuthorizationType", "NONE") == "NONE"
    assert routes["POST /tools/{tool}"]["ApiId"] == {"Ref": hr_api_id}
    assert routes["POST /tools/{tool}"]["AuthorizationType"] == "JWT"
    integrations = {
        r["Properties"]["ApiId"]["Ref"]: r["Properties"]
        for r in t.find_resources("AWS::ApiGatewayV2::Integration").values()
    }
    assert set(integrations) == {idp_api_id, hr_api_id}  # one integration per API: the two Keycloak routes share theirs
    keycloak = integrations[idp_api_id]
    assert keycloak["IntegrationType"] == "HTTP_PROXY" and keycloak["ConnectionType"] == "VPC_LINK"
    assert keycloak["ConnectionId"] == {"Ref": vpc_link_id} and keycloak["IntegrationUri"] == {"Ref": listener_id}
    assert keycloak["IntegrationMethod"] == "ANY"
    assert integrations[hr_api_id]["IntegrationType"] == "AWS_PROXY"
    assert integrations[hr_api_id]["PayloadFormatVersion"] == "2.0"
    t.has_resource_properties("AWS::Lambda::Permission", {"Principal": "apigateway.amazonaws.com"})
    # two $default stages, both with an access log: the IdP's records who calls the token endpoint and the admin console
    stages = {
        r["Properties"]["ApiId"]["Ref"]: r["Properties"] for r in t.find_resources("AWS::ApiGatewayV2::Stage").values()
    }
    assert set(stages) == {idp_api_id, hr_api_id}
    assert all(s["StageName"] == "$default" and s["AutoDeploy"] is True for s in stages.values())
    idp_stage = stages[idp_api_id]
    assert "IdpAccessLogs" in json.dumps(idp_stage["AccessLogSettings"]["DestinationArn"])
    assert {"ip", "method", "path", "status", "userAgent"} <= set(json.loads(idp_stage["AccessLogSettings"]["Format"]))
    stage = stages[hr_api_id]
    assert "HrApiAccessLogs" in json.dumps(stage["AccessLogSettings"]["DestinationArn"])
    log_format = json.loads(stage["AccessLogSettings"]["Format"])
    assert {"requestId", "ip", "requestTime", "routeKey", "status", "integrationError"} <= set(log_format)
    assert log_format["integrationError"] == "$context.integrationErrorMessage"
    for claim in ("sub", "aud", "azp", "employee_id"):
        assert log_format[claim] == f"$context.authorizer.claims.{claim}"
    # HR API JWT authorizer: issuer = the HTTPS door + /realms/anycompany, audience hr-tools-api
    t.resource_count_is("AWS::ApiGatewayV2::Authorizer", 1)
    authorizer = next(iter(t.find_resources("AWS::ApiGatewayV2::Authorizer").values()))["Properties"]
    assert authorizer["ApiId"] == {"Ref": hr_api_id} and authorizer["AuthorizerType"] == "JWT"
    assert authorizer["IdentitySource"] == ["$request.header.Authorization"]
    assert authorizer["JwtConfiguration"]["Audience"] == ["hr-tools-api"]
    issuer = {"Fn::Join": ["", [{"Fn::GetAtt": [idp_api_id, "ApiEndpoint"]}, "/realms/anycompany"]]}
    assert authorizer["JwtConfiguration"]["Issuer"] == issuer
    # the readiness gate: API Gateway fetches the discovery document when it creates the authorizer and fails with "Invalid
    # issuer" if the realm is not there yet (2026-10-16), so the authorizer waits for a poll of that document, which waits for
    # the Fargate service (created = task healthy = realm imported). Plain urllib, no VPC, 14-minute budget, unnamed logs.
    t.resource_count_is("Custom::KeycloakReady", 1)
    ready_id, ready = next(iter(t.find_resources("Custom::KeycloakReady").items()))
    assert ready["Properties"]["Issuer"] == issuer
    assert ready["Properties"]["Url"] == {
        "Fn::Join": [
            "",
            [{"Fn::GetAtt": [idp_api_id, "ApiEndpoint"]}, "/realms/anycompany/.well-known/openid-configuration"],
        ]
    }
    assert next(iter(t.find_resources("AWS::ECS::Service"))) in ready["DependsOn"]
    assert ready_id in next(iter(t.find_resources("AWS::ApiGatewayV2::Authorizer").values()))["DependsOn"]
    ready_functions = [
        r["Properties"]
        for r in t.find_resources("AWS::Lambda::Function").values()
        if "keycloak-ready:" in r["Properties"].get("Code", {}).get("ZipFile", "")
    ]
    assert len(ready_functions) == 1 and ready_functions[0]["Timeout"] == 840 and "VpcConfig" not in ready_functions[0]
    assert "boto3" not in ready_functions[0]["Code"]["ZipFile"]  # it only talks to the public API URL
    # the Lambda: the HR tools asset with the HTTP adapter as handler, table names as constants, read/write on the three tables
    functions = t.find_resources("AWS::Lambda::Function", {"Properties": {"FunctionName": "agent-platform-hr-api"}})
    assert len(functions) == 1
    props = next(iter(functions.values()))["Properties"]
    assert (props["Handler"], props["Runtime"], props["Timeout"], props["MemorySize"]) == (
        "http_handler.handler",
        "python3.13",
        30,
        256,
    )
    assert props["Environment"]["Variables"] == {
        "EMPLOYEES_TABLE": "agent-platform-employees",
        "LEAVE_REQUESTS_TABLE": "agent-platform-leave-requests",
    }
    assert "S3Key" in props["Code"]  # an asset (scenario/tools/hr), not inline code
    # the Scenario stack's grants for the same code: employees read-only, leave-requests read/write, no invoices
    statements = role_statements(t, _role_of(t, "agent-platform-hr-api"))
    employees = "arn:aws:dynamodb:us-east-1:123456789012:table/agent-platform-employees"
    leave = "arn:aws:dynamodb:us-east-1:123456789012:table/agent-platform-leave-requests"
    assert resources_for(statements, "dynamodb:GetItem") == {
        employees,
        f"{employees}/index/*",
        leave,
        f"{leave}/index/*",
    }
    assert resources_for(statements, "dynamodb:PutItem") == {leave, f"{leave}/index/*"}
    assert resources_for(statements, "dynamodb:UpdateItem") == {leave, f"{leave}/index/*"}
    assert not any("invoices" in r for s_ in statements for r in s_["Resource"] if isinstance(r, str))
    # SSM: what `workshop apply 2`, `4` and `6` (the on-behalf-of builders) and `workshop obo` read
    params = {
        r["Properties"]["Name"]: r["Properties"]["Value"] for r in t.find_resources("AWS::SSM::Parameter").values()
    }
    assert set(params) == {
        "/agent-platform/idp/url",
        "/agent-platform/idp/issuer",
        "/agent-platform/idp/realm",
        "/agent-platform/idp/admin-secret-arn",
        "/agent-platform/idp/admin-username",
        "/agent-platform/idp/secret-hr-agent-arn",
        "/agent-platform/idp/secret-hr-obo-gateway-arn",
        "/agent-platform/idp/alb-dns",
        "/agent-platform/hr-api/url",
        "/agent-platform/hr-api/audience",
        "/agent-platform/hr-api/function-name",
        "/agent-platform/hr-api/log-group",
        "/agent-platform/hr-api/access-log-group",
    }
    assert params["/agent-platform/idp/url"] == {"Fn::GetAtt": [idp_api_id, "ApiEndpoint"]}
    assert params["/agent-platform/idp/issuer"] == issuer
    assert params["/agent-platform/idp/realm"] == "anycompany"
    assert params["/agent-platform/idp/admin-username"] == "admin"
    assert params["/agent-platform/idp/admin-secret-arn"] == {"Ref": secret_id}
    for client_id in ("hr-agent", "hr-obo-gateway"):
        assert params[f"/agent-platform/idp/secret-{client_id}-arn"] == {
            "Ref": secrets[f"agent-platform/keycloak-client-{client_id}"]
        }
    assert params["/agent-platform/idp/alb-dns"] == {"Fn::GetAtt": [alb_id, "DNSName"]}
    assert params["/agent-platform/hr-api/url"] == {"Fn::GetAtt": [hr_api_id, "ApiEndpoint"]}  # no trailing slash
    assert params["/agent-platform/hr-api/audience"] == "hr-tools-api"
    assert params["/agent-platform/hr-api/log-group"] == "/aws/lambda/agent-platform-hr-api"
    assert params["/agent-platform/hr-api/access-log-group"] == "/agent-platform/hr-api-access"
    outputs = set(t.to_json()["Outputs"])
    assert {"VpcBpaExclusionId", "VpcBpaMode", "KeycloakReadySeconds"} <= outputs and "RawEndpoint" not in outputs


def test_keycloak_realm_file() -> None:
    """The realm Keycloak imports at start-up (foundation/keycloak/realm.json, generated by platform_sdk.keycloak.realm_json(),
    whose test checks the file matches): the four clients of the on-behalf-of chain, the two confidential clients' secrets as
    placeholders for the environment variables the Idp stack feeds from Secrets Manager."""
    text = config.KEYCLOAK_REALM_FILE.read_text(encoding="utf-8")
    realm = json.loads(text)
    assert realm["realm"] == config.KEYCLOAK_REALM == "anycompany" and realm["enabled"] is True
    clients = {c["clientId"]: c for c in realm["clients"]}
    assert set(clients) == {"hr-portal", "hr-agent", "hr-obo-gateway", "hr-tools-api"}
    assert set(config.KEYCLOAK_CLIENT_SECRET_NAMES) == {"hr-agent", "hr-obo-gateway"}
    for client_id in config.KEYCLOAK_CLIENT_SECRET_NAMES:
        env_name = "KC_SECRET_" + client_id.upper().replace("-", "_")
        assert clients[client_id]["publicClient"] is False
        assert clients[client_id]["secret"] == f"${{{env_name}}}"
        assert clients[client_id]["attributes"]["standard.token.exchange.enabled"] == "true"
    assert clients["hr-portal"]["publicClient"] is True and "secret" not in clients["hr-portal"]
    assert "${" not in json.dumps([c for c in realm["clients"] if c["clientId"] in ("hr-portal", "hr-tools-api")])
    assert (
        len(text) < 32_000
    )  # it travels as an environment variable of the init container (task definitions cap at 64 KiB)


def _role_of(template: Template, function_name: str) -> str:
    """Logical id of the CDK-generated role of a Lambda function (resolved through the Role GetAtt)."""
    fn = next(
        iter(template.find_resources("AWS::Lambda::Function", {"Properties": {"FunctionName": function_name}}).values())
    )
    return fn["Properties"]["Role"]["Fn::GetAtt"][0]


def test_everything_is_destroyable(templates) -> None:
    for name, t in templates.items():
        for logical_id, resource in t.to_json()["Resources"].items():
            if resource["Type"] in ("AWS::CDK::Metadata",):
                continue
            assert resource.get("DeletionPolicy", "Delete") in ("Delete",), (
                f"{name}/{logical_id} has DeletionPolicy {resource.get('DeletionPolicy')}"
            )
