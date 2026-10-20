"""Network: one private VPC with no internet gateway, no NAT and no public IP anywhere.

Design: .claude/docs/private-landing-zone-design.md. Three availability zones chosen for AgentCore Runtime's VPC mode (the names
are resolved in app.py from config.HARNESS_AZ_IDS) and two isolated subnet groups: `app` for the LiteLLM tasks and their internal
load balancer, the harness ENIs, the relay Lambda and the interface endpoints; `data` for RDS. AWS services are reached through the
S3 gateway endpoint and nine interface endpoints (private DNS on) placed in the first ENDPOINT_AZ_COUNT zones; callers in the
third zone reach them cross-AZ. The AgentCore control-plane endpoint is deliberately absent: nothing inside the VPC calls it.
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Stack, Token
from aws_cdk import aws_ec2 as ec2
from constructs import Construct

from . import config
from .common import publish

# Construct id prefix -> service. ECR (api + dkr) and S3 serve the image pulls of the LiteLLM tasks and of the harness's managed
# container; Secrets Manager, Logs, Monitoring and X-Ray serve the tasks, the relay and the harness; bedrock-runtime serves
# LiteLLM; the AgentCore data plane (Identity, Memory, Runtime) and the Gateway endpoint serve the harness.
INTERFACE_ENDPOINTS: dict[str, ec2.InterfaceVpcEndpointAwsService] = {
    "SecretsManager": ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
    "Logs": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
    "Monitoring": ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_MONITORING,
    "XRay": ec2.InterfaceVpcEndpointAwsService.XRAY,
    "EcrApi": ec2.InterfaceVpcEndpointAwsService.ECR,
    "EcrDocker": ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
    "BedrockRuntime": ec2.InterfaceVpcEndpointAwsService.BEDROCK_RUNTIME,
    "AgentCore": ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENTCORE,
    "AgentCoreGateway": ec2.InterfaceVpcEndpointAwsService.BEDROCK_AGENTCORE_GATEWAY,
}


class NetworkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, az_names: list[str], **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        self.az_names = list(az_names)
        # The Vpc construct checks explicit zone names against Stack.availability_zones, which the CDK CLI otherwise resolves
        # with its own EC2 lookup (context key `availability-zones:account=..:region=..`: credentials needed, a second call).
        # This stack's zones *are* the three resolved names, so when the CLI has no cached entry they are seeded under that
        # key here, before any child exists: the check holds and the CLI never looks anything up (synth works without
        # credentials, as app.py's fallback promises). A cached CLI entry is left alone, so it still validates the names.
        # Env-agnostic stacks (no account) get Fn::GetAZs tokens from the construct instead, and the check is skipped.
        if not Token.is_unresolved(self.account) and not Token.is_unresolved(self.region):
            cli_key = f"availability-zones:account={self.account}:region={self.region}"
            if self.node.try_get_context(cli_key) is None:
                self.node.set_context(cli_key, self.az_names)

        # Explicit zone names instead of max_azs: the harness subnets must be in the supported zone ids. Isolated-only subnet
        # groups make CDK create no internet gateway; nat_gateways=0 is spelled out so nobody adds one by accident.
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=config.PREFIX,
            ip_addresses=ec2.IpAddresses.cidr(config.VPC_CIDR),
            availability_zones=self.az_names,
            nat_gateways=0,
            restrict_default_security_group=False,  # avoids an extra custom resource; the default SG is unused
            subnet_configuration=[
                ec2.SubnetConfiguration(name="app", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24),
                ec2.SubnetConfiguration(name="data", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED, cidr_mask=24),
            ],
        )
        self.app_subnets = ec2.SubnetSelection(subnet_group_name="app")
        self.data_subnets = ec2.SubnetSelection(subnet_group_name="data")
        app_subnet_ids = self.vpc.select_subnets(subnet_group_name="app").subnet_ids
        data_subnet_ids = self.vpc.select_subnets(subnet_group_name="data").subnet_ids
        vpc_cidr = ec2.Peer.ipv4(self.vpc.vpc_cidr_block)

        # S3 gateway endpoint on every route table: ECR keeps image layers in S3, LiteLLM reads config.yaml, the harness pulls
        # its managed container. Open policy; scoping it to the service buckets is a production-path item (design, section 5).
        self.vpc.add_gateway_endpoint("S3Endpoint", service=ec2.GatewayVpcEndpointAwsService.S3)

        endpoints_sg = ec2.SecurityGroup(
            self,
            "EndpointsSecurityGroup",
            vpc=self.vpc,
            security_group_name=config.ENDPOINTS_SECURITY_GROUP_NAME,
            description="Interface endpoints: HTTPS from anything in the VPC",
            allow_all_outbound=False,  # endpoints never initiate connections
        )
        endpoints_sg.add_ingress_rule(vpc_cidr, ec2.Port.tcp(443), "VPC residents to the endpoints")
        # Each interface endpoint costs about 0.01 USD per AZ-hour: two of the three zones (about 0.18 USD/h for the nine).
        endpoint_subnets = ec2.SubnetSelection(
            subnet_group_name="app", availability_zones=self.az_names[: config.ENDPOINT_AZ_COUNT]
        )
        self.interface_endpoints = {
            name: self.vpc.add_interface_endpoint(
                f"{name}Endpoint",
                service=service,
                subnets=endpoint_subnets,
                security_groups=[endpoints_sg],
                private_dns_enabled=True,
            )
            for name, service in INTERFACE_ENDPOINTS.items()
        }

        # The harness ENIs (AgentCore Runtime in VPC mode) and the relay Lambda only ever make outbound connections.
        self.harness_security_group = ec2.SecurityGroup(
            self,
            "HarnessSecurityGroup",
            vpc=self.vpc,
            security_group_name=config.HARNESS_SECURITY_GROUP_NAME,
            description="AgentCore Runtime harness ENIs (VPC mode)",
            allow_all_outbound=True,
        )
        self.relay_security_group = ec2.SecurityGroup(
            self,
            "RelaySecurityGroup",
            vpc=self.vpc,
            security_group_name=config.RELAY_SECURITY_GROUP_NAME,
            description="Model gateway relay Lambda",
            allow_all_outbound=True,
        )

        # What the backend and the SDK need to put a harness into the VPC and to describe the posture (build contract, section 1).
        publish(self, "network/vpc-id", self.vpc.vpc_id, "Platform VPC (private: no internet gateway, no NAT)")
        publish(
            self, "network/app-subnet-ids", ",".join(app_subnet_ids), "Isolated app subnets (LiteLLM, harness, relay)"
        )
        publish(self, "network/harness-subnet-ids", ",".join(app_subnet_ids), "Subnets for harnesses in VPC mode")
        publish(
            self,
            "network/harness-security-group-id",
            self.harness_security_group.security_group_id,
            "Security group for harnesses in VPC mode",
        )
        publish(
            self,
            "network/relay-security-group-id",
            self.relay_security_group.security_group_id,
            "Security group of the model gateway relay Lambda",
        )
        publish(
            self, "network/availability-zones", ",".join(self.az_names), "Zone names carrying the supported zone ids"
        )
        publish(self, "network/private", "true", "The landing zone has no internet path")
        CfnOutput(self, "VpcId", value=self.vpc.vpc_id)
        CfnOutput(self, "AppSubnetIds", value=",".join(app_subnet_ids))
        CfnOutput(self, "DataSubnetIds", value=",".join(data_subnet_ids))
