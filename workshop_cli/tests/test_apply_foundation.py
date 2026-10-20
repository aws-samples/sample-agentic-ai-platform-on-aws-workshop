"""Module 1's look at the private VPC and module 5's answer helper, without AWS (a canned EC2 client)."""

from __future__ import annotations

from workshop_cli import apply_5, apply_foundation

VPC = "vpc-0private"


class FakeEC2:
    """The four describe calls `vpc_facts` makes, answered with the shapes boto3 returns for the private landing zone."""

    def __init__(self, igws=(), nats=(), public_ip_subnets=()):
        self.igws, self.nats, self.public_ip_subnets = list(igws), list(nats), set(public_ip_subnets)
        self.filters: list = []

    def describe_subnets(self, Filters):
        self.filters.append(Filters)
        rows = []
        for group, zone, subnet_id in (
            ("app", "us-east-1a", "subnet-a1"),
            ("app", "us-east-1b", "subnet-a2"),
            ("app", "us-east-1c", "subnet-a3"),
            ("data", "us-east-1a", "subnet-d1"),
            ("data", "us-east-1b", "subnet-d2"),
            ("data", "us-east-1c", "subnet-d3"),
        ):
            rows.append(
                {
                    "SubnetId": subnet_id,
                    "AvailabilityZone": zone,
                    "MapPublicIpOnLaunch": subnet_id in self.public_ip_subnets,
                    "Tags": [{"Key": "aws-cdk:subnet-name", "Value": group}, {"Key": "Name", "Value": f"x/{group}"}],
                }
            )
        rows.append({"SubnetId": "subnet-untagged", "AvailabilityZone": "us-east-1a", "MapPublicIpOnLaunch": False})
        return {"Subnets": rows}

    def describe_vpc_endpoints(self, Filters):
        services = [
            "secretsmanager",
            "logs",
            "monitoring",
            "xray",
            "ecr.api",
            "ecr.dkr",
            "bedrock-runtime",
            "bedrock-agentcore",
            "bedrock-agentcore.gateway",
        ]
        rows = [{"ServiceName": f"com.amazonaws.us-east-1.{s}", "VpcEndpointType": "Interface"} for s in services]
        rows.append({"ServiceName": "com.amazonaws.us-east-1.s3", "VpcEndpointType": "Gateway"})
        return {"VpcEndpoints": rows}

    def describe_internet_gateways(self, Filters):
        assert Filters == [{"Name": "attachment.vpc-id", "Values": [VPC]}]
        return {"InternetGateways": [{"InternetGatewayId": i} for i in self.igws]}

    def describe_nat_gateways(self, Filters):
        assert {"Name": "state", "Values": ["available", "pending"]} in Filters
        return {"NatGateways": [{"NatGatewayId": n} for n in self.nats]}


def test_vpc_facts_describe_the_private_landing_zone():
    facts = apply_foundation.vpc_facts(FakeEC2(), VPC)
    assert facts["subnets"] == {
        "app": ["subnet-a1", "subnet-a2", "subnet-a3"],
        "data": ["subnet-d1", "subnet-d2", "subnet-d3"],
        "other": ["subnet-untagged"],
    }
    assert facts["zones"] == ["us-east-1a", "us-east-1b", "us-east-1c"]
    assert facts["internet_gateways"] == [] and facts["nat_gateways"] == [] and facts["public_ip_subnets"] == []
    assert facts["interface_endpoints"] == [
        "bedrock-agentcore",
        "bedrock-agentcore.gateway",
        "bedrock-runtime",
        "ecr.api",
        "ecr.dkr",
        "logs",
        "monitoring",
        "secretsmanager",
        "xray",
    ]
    assert facts["gateway_endpoints"] == ["s3"]


def test_vpc_facts_report_a_public_path_when_one_exists():
    facts = apply_foundation.vpc_facts(FakeEC2(igws=["igw-1"], nats=["nat-1"], public_ip_subnets={"subnet-a1"}), VPC)
    assert facts["internet_gateways"] == ["igw-1"] and facts["nat_gateways"] == ["nat-1"]
    assert facts["public_ip_subnets"] == ["subnet-a1"]


def test_endpoint_service_strips_the_prefix_and_region():
    assert (
        apply_foundation.endpoint_service("com.amazonaws.us-east-1.bedrock-agentcore.gateway")
        == "bedrock-agentcore.gateway"
    )
    assert apply_foundation.endpoint_service("com.amazonaws.us-east-1.s3") == "s3"
    assert apply_foundation.endpoint_service("odd") == "odd"


def test_answer_text_reads_the_completion_shape():
    assert apply_5._answer_text({"choices": [{"message": {"content": "Hello"}}]}) == "Hello"
    assert (
        apply_5._answer_text({"choices": [{"message": {"content": None}, "finish_reason": "content_filter"}]}) is None
    )
    assert apply_5._answer_text({}) is None
