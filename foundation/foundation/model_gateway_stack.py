"""Model gateway: LiteLLM on ECS Express Mode with RDS PostgreSQL, secrets and the S3-hosted config.yaml.

Facts used here were verified on 2026-10-08 (see .claude/docs/phase0-foundation-notes.md): aws-cdk-lib 2.272.0 ships
`CfnExpressGatewayService`; the Express infrastructure role uses the managed policy
`service-role/AmazonECSInfrastructureRoleforExpressGatewayServices`; `db.t4g.micro` is not orderable for PostgreSQL 18 in us-east-1,
so the instance is `db.t3.micro` on PostgreSQL 18.6.

Private landing zone (.claude/docs/private-landing-zone-design.md): the tasks run on the isolated `app` subnets, so Express
provisions an *internal* load balancer (same `https://<id>.ecs.<region>.on.aws` URL shape, resolving to private addresses), the
image is copied into a private ECR repository at deploy time because the tasks cannot reach ghcr.io, and a relay Lambda inside
the VPC is the only way for the backend, the CLI and the doctor to reach the proxy.
"""

from __future__ import annotations

import hashlib
import json

from aws_cdk import (
    CfnOutput,
    CfnResource,
    CfnTag,
    CustomResource,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lam
from aws_cdk import aws_rds as rds
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_secretsmanager as sm
from aws_cdk import custom_resources as cr
from cdk_ecr_deployment import DockerImageName, ECRDeployment
from constructs import Construct

from . import config
from .common import log_group, publish, read_lambda_code
from .network_stack import NetworkStack
from .shared_services_stack import SharedServicesStack


class ModelGatewayStack(Stack):
    def __init__(
        self, scope: Construct, construct_id: str, *, network: NetworkStack, shared: SharedServicesStack, **kwargs
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        vpc = network.vpc

        # --- Secrets -------------------------------------------------------------------------------------------
        # Master key: an empty secret filled once by a tiny Lambda so the value starts with "sk-" (LiteLLM requires it).
        master_secret = sm.CfnSecret(
            self,
            "MasterKeySecret",
            name=config.MASTER_KEY_SECRET_NAME,
            description="LiteLLM master key (raw string, starts with sk-). Also the password of the LiteLLM admin UI (user admin).",
        )
        master_secret.apply_removal_policy(RemovalPolicy.DESTROY)
        master_fn = lam.Function(
            self,
            "MasterKeyFunction",
            runtime=lam.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lam.Code.from_inline(read_lambda_code("master_key.py")),
            timeout=Duration.seconds(30),
            description="Writes the LiteLLM master key into the empty secret (once)",
            log_group=log_group(self, "MasterKeyFunctionLogs", f"/aws/lambda/{config.PREFIX}-master-key"),
        )
        master_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"],
                resources=[master_secret.attr_id],
            )
        )
        master_provider = cr.Provider(
            self,
            "MasterKeyProvider",
            on_event_handler=master_fn,
            log_group=log_group(self, "MasterKeyProviderLogs", f"/aws/lambda/{config.PREFIX}-master-key-provider"),
        )
        master_key_value = CustomResource(
            self,
            "MasterKeyValue",
            service_token=master_provider.service_token,
            resource_type="Custom::LiteLLMMasterKey",
            properties={"SecretArn": master_secret.attr_id},
        )
        self.master_key_secret_arn = master_secret.attr_id

        # Salt key: generated once by Secrets Manager, never rotated (LiteLLM encrypts stored provider credentials with it).
        salt_secret = sm.Secret(
            self,
            "SaltKeySecret",
            secret_name=config.SALT_KEY_SECRET_NAME,
            description="LiteLLM salt key (generated once, never rotate)",
            generate_secret_string=sm.SecretStringGenerator(exclude_punctuation=True, password_length=40),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # --- Database ------------------------------------------------------------------------------------------
        task_sg = ec2.SecurityGroup(
            self, "TaskSecurityGroup", vpc=vpc, description="LiteLLM tasks", allow_all_outbound=True
        )
        # The Express-managed (internal) load balancer lives in the same app subnets, so the VPC CIDR covers its nodes.
        task_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block), ec2.Port.tcp(config.LITELLM_PORT), "Load balancer to LiteLLM"
        )
        db_sg = ec2.SecurityGroup(
            self, "DbSecurityGroup", vpc=vpc, description="LiteLLM database", allow_all_outbound=False
        )
        db_sg.add_ingress_rule(task_sg, ec2.Port.tcp(5432), "LiteLLM tasks to PostgreSQL")

        db = rds.DatabaseInstance(
            self,
            "Database",
            engine=rds.DatabaseInstanceEngine.postgres(version=rds.PostgresEngineVersion.of(*config.DB_ENGINE_VERSION)),
            instance_type=ec2.InstanceType(f"{config.DB_INSTANCE_CLASS}.{config.DB_INSTANCE_SIZE}"),
            vpc=vpc,
            vpc_subnets=network.data_subnets,
            security_groups=[db_sg],
            credentials=rds.Credentials.from_generated_secret(config.DB_USERNAME, secret_name=config.DB_SECRET_NAME),
            database_name=config.DB_NAME,
            allocated_storage=20,
            storage_type=rds.StorageType.GP3,
            multi_az=False,
            storage_encrypted=True,
            publicly_accessible=False,
            deletion_protection=False,
            removal_policy=RemovalPolicy.DESTROY,
            backup_retention=Duration.days(0),
            delete_automated_backups=True,
            # One-day workshop on a pinned engine version: no automatic minor upgrades, and a fixed maintenance window
            # (RDS otherwise picks a random weekly 30-minute window that may fall inside the event and restart the only database).
            auto_minor_version_upgrade=False,
            preferred_maintenance_window=config.DB_MAINTENANCE_WINDOW,
        )
        db_secret = db.secret
        assert db_secret is not None

        # --- LiteLLM configuration in S3 -----------------------------------------------------------------------
        config_bucket = s3.Bucket(
            self,
            "ConfigBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        template = config.LITELLM_CONFIG_TEMPLATE.read_text(encoding="utf-8")
        rendered = (
            template.replace("{{GUARDRAIL_ID}}", shared.guardrail_id)
            .replace("{{GUARDRAIL_VERSION}}", shared.guardrail_version)
            .replace("{{REGION}}", self.region)
        )
        # LiteLLM reads config.yaml only at start-up: a hash of the template in the task environment makes every template
        # change a new service revision, so `cdk deploy` restarts the task and the new aliases are live. The guardrail id
        # and version are substituted at deploy time, so they go into the environment as well (see below).
        config_hash = hashlib.sha256(template.encode("utf-8")).hexdigest()[:16]
        config_deployment = s3deploy.BucketDeployment(
            self,
            "ConfigDeployment",
            destination_bucket=config_bucket,
            sources=[s3deploy.Source.data(config.LITELLM_CONFIG_KEY, rendered)],
            prune=False,
            log_group=log_group(self, "ConfigDeploymentLogs", f"/aws/lambda/{config.PREFIX}-config-deployment"),
        )

        # --- Roles ---------------------------------------------------------------------------------------------
        infra_role = iam.Role(
            self,
            "ExpressInfrastructureRole",
            assumed_by=iam.ServicePrincipal("ecs.amazonaws.com"),
            description="ECS Express Mode: creates the load balancer, certificate, scaling and logs on our behalf",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSInfrastructureRoleforExpressGatewayServices"
                )
            ],
        )
        exec_role = iam.Role(
            self,
            "TaskExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Pulls the LiteLLM image, writes logs and injects secrets into the container",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSTaskExecutionRolePolicy")
            ],
        )
        exec_role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[master_secret.attr_id, salt_secret.secret_arn, db_secret.secret_arn],
            )
        )
        task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="LiteLLM: call Bedrock models and the baseline guardrail, read config.yaml",
        )
        task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:ApplyGuardrail"],
                resources=["*"],
            )
        )
        config_bucket.grant_read(task_role, config.LITELLM_CONFIG_KEY)

        # --- LiteLLM image in a private repository -------------------------------------------------------------
        # The tasks have no internet path, so the image cannot be pulled from ghcr.io at run time. cdk-ecr-deployment copies it
        # into the account's repository at deploy time (a Lambda outside the VPC streams the layers; no Docker on the laptop; an
        # ECR pull-through cache was rejected because ghcr.io upstreams need GitHub credentials) and the tasks pull it through
        # the ecr.api, ecr.dkr and S3 endpoints. The copy adds about 2 to 4 minutes to the first deploy.
        repository = ecr.Repository(
            self,
            "LiteLLMRepository",
            repository_name=config.LITELLM_REPOSITORY_NAME,
            empty_on_delete=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        image_uri = f"{repository.repository_uri}:{config.LITELLM_IMAGE_TAG}"
        image_copy = ECRDeployment(
            self,
            "LiteLLMImageCopy",
            src=DockerImageName(config.LITELLM_IMAGE),
            dest=DockerImageName(image_uri),
            memory_limit=1024,  # headroom for the 23-layer, 401 MB image: a failed copy costs a whole stack rollback
            retry_configs={
                "numAttempts": 3,
                "baseDelay": 5,
                "maxDelay": 30,
            },  # ghcr.io hiccups while 40 accounts deploy
        )
        repository.grant_pull(exec_role)
        # The construct's singleton Lambda gets a CDK-managed log group with the library's defaults (two years, retained after
        # the stack). Align it with every other log group here: 30 days, deleted with the stack (a retained group would linger
        # after every destroy; cleanup.sh would have to sweep it).
        copy_log_groups = [
            c
            for c in self.node.find_all()
            if isinstance(c, CfnResource)
            and c.cfn_resource_type == "AWS::Logs::LogGroup"
            and "CDKECRDeployment"
            in c.node.path  # the singleton's path is <stack>/Custom::CDKECRDeployment<hash>/LogGroup
        ]
        if len(copy_log_groups) != 1:  # the library changed; look at the synthesized template before adjusting this
            raise RuntimeError(f"expected one cdk-ecr-deployment log group, found {len(copy_log_groups)}")
        copy_log_groups[0].apply_removal_policy(RemovalPolicy.DESTROY)
        copy_log_groups[0].add_property_override("RetentionInDays", config.LOG_RETENTION_DAYS)

        # --- ECS Express service -------------------------------------------------------------------------------
        # Fresh accounts have no service-linked roles for ELB and Application Auto Scaling. Express creates them on the
        # fly and then calls CreateLoadBalancer one second later, which fails with AccessDenied (IAM propagation; seen on
        # 2026-10-08). Create them up front, idempotently (InvalidInput = already exists), minutes before the service.
        self._sdk_logs = log_group(self, "SdkCallLogs", f"/aws/lambda/{config.PREFIX}-model-gateway-sdk-calls")
        service_linked_roles = [
            self._service_linked_role(name) for name in ("elasticloadbalancing", "ecs.application-autoscaling", "ecs")
        ]
        # Probes the ECS control plane makes with the infrastructure role beyond its managed policy (seen in CloudTrail).
        infra_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "ecs:DescribeServices",
                    "ecs:UpdateService",
                    "cloudwatch:DescribeAlarms",
                    "ec2:DescribeAccountAttributes",
                ],
                resources=["*"],
            )
        )
        cluster = ecs.Cluster(self, "Cluster", cluster_name=config.LITELLM_CLUSTER_NAME, vpc=vpc)
        service_logs = log_group(self, "ServiceLogs", f"/{config.PREFIX}/model-gateway")

        def env(name: str, value: str) -> ecs.CfnExpressGatewayService.KeyValuePairProperty:
            return ecs.CfnExpressGatewayService.KeyValuePairProperty(name=name, value=value)

        def secret(name: str, value_from: str) -> ecs.CfnExpressGatewayService.SecretProperty:
            return ecs.CfnExpressGatewayService.SecretProperty(name=name, value_from=value_from)

        service = ecs.CfnExpressGatewayService(
            self,
            "Service",
            service_name=config.LITELLM_SERVICE_NAME,
            cluster=cluster.cluster_name,
            infrastructure_role_arn=infra_role.role_arn,
            execution_role_arn=exec_role.role_arn,
            task_role_arn=task_role.role_arn,
            cpu=config.LITELLM_CPU,
            memory=config.LITELLM_MEMORY,
            health_check_path=config.LITELLM_HEALTH_PATH,
            primary_container=ecs.CfnExpressGatewayService.ExpressGatewayContainerProperty(
                image=image_uri,
                container_port=config.LITELLM_PORT,
                environment=[
                    env("DATABASE_HOST", db.db_instance_endpoint_address),
                    env("DATABASE_USERNAME", config.DB_USERNAME),
                    env("DATABASE_NAME", config.DB_NAME),
                    env("LITELLM_CONFIG_BUCKET_NAME", config_bucket.bucket_name),
                    env("LITELLM_CONFIG_BUCKET_OBJECT_KEY", config.LITELLM_CONFIG_KEY),
                    env("LITELLM_CONFIG_HASH", config_hash),
                    env("GUARDRAIL_ID", shared.guardrail_id),  # any guardrail change restarts the task (new revision)
                    env("GUARDRAIL_VERSION", shared.guardrail_version),
                    env("LITELLM_LOCAL_MODEL_COST_MAP", "True"),
                    env("LITELLM_LOG", "INFO"),
                    env("AWS_REGION", self.region),
                    env("AWS_DEFAULT_REGION", self.region),
                    env("UI_USERNAME", "admin"),
                ],
                secrets=[
                    secret("LITELLM_MASTER_KEY", master_secret.attr_id),
                    secret("LITELLM_SALT_KEY", salt_secret.secret_arn),
                    secret("DATABASE_PASSWORD", f"{db_secret.secret_arn}:password::"),
                ],
                aws_logs_configuration=ecs.CfnExpressGatewayService.ExpressGatewayServiceAwsLogsConfigurationProperty(
                    log_group=service_logs.log_group_name, log_stream_prefix="litellm"
                ),
            ),
            # Isolated subnets: Express then provisions an internal load balancer (documented behaviour). The first service in
            # a VPC fixes the load balancer's subnets, so the VPC is private from the start; no in-place public->private flip.
            network_configuration=ecs.CfnExpressGatewayService.ExpressGatewayServiceNetworkConfigurationProperty(
                subnets=vpc.select_subnets(subnet_group_name="app").subnet_ids,
                security_groups=[task_sg.security_group_id],
            ),
            scaling_target=ecs.CfnExpressGatewayService.ExpressGatewayScalingTargetProperty(
                min_task_count=1, max_task_count=1
            ),
            # ServiceName, Cluster, InfrastructureRoleArn and Tags are create-only on AWS::ECS::ExpressGatewayService: any change
            # replaces the service (a second service with this fixed name cannot be created, and the endpoint would change).
            # The tags are therefore frozen here instead of inherited from the app-wide Tags aspect (excluded in app.py).
            tags=[CfnTag(key="ManagedBy", value="cdk"), CfnTag(key="Project", value=config.PROJECT_TAG)],
        )
        # The first task must find config.yaml, a master key value and a reachable database.
        service.node.add_dependency(config_deployment)
        service.node.add_dependency(master_key_value)
        service.node.add_dependency(db)
        service.node.add_dependency(cluster)
        service.node.add_dependency(image_copy)  # the image must be in the repository before the first task starts
        for slr in service_linked_roles:
            service.node.add_dependency(slr)

        # Long streaming responses: raise the managed load balancer's idle timeout (default 60 s).
        alb_call = cr.AwsSdkCall(
            service="ELBv2",  # SDK v2 constructor name; "ElasticLoadBalancingV2" maps to a non-existent v3 package
            action="ModifyLoadBalancerAttributes",
            parameters={
                "LoadBalancerArn": service.attr_ecs_managed_resource_arns_ingress_path_load_balancer_arn,
                "Attributes": [{"Key": "idle_timeout.timeout_seconds", "Value": config.ALB_IDLE_TIMEOUT_SECONDS}],
            },
            physical_resource_id=cr.PhysicalResourceId.of(f"{config.LITELLM_SERVICE_NAME}-alb-idle-timeout"),
        )
        cr.AwsCustomResource(
            self,
            "AlbIdleTimeout",
            resource_type="Custom::AlbIdleTimeout",
            on_create=alb_call,
            on_update=alb_call,
            log_group=self._sdk_logs,
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [iam.PolicyStatement(actions=["elasticloadbalancing:ModifyLoadBalancerAttributes"], resources=["*"])]
            ),
        )

        self.service = service
        self.url = self._https_url(service.attr_endpoint)

        # --- Relay: the only way in from outside the VPC ----------------------------------------------------------
        # One HTTP request per invocation, forwarded to the proxy over the internal load balancer; no credentials of its own
        # (the caller passes the Authorization header). Its role gets nothing beyond VPC access and logs. Fixed log group name
        # is fine: unlike the custom-resource providers, the relay never runs during a stack deletion.
        relay = lam.Function(
            self,
            "RelayFunction",
            function_name=config.RELAY_FUNCTION_NAME,
            runtime=lam.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lam.Code.from_inline(read_lambda_code("model_gateway_relay.py")),
            timeout=Duration.seconds(config.RELAY_TIMEOUT_SECONDS),
            memory_size=512,
            vpc=vpc,
            vpc_subnets=network.app_subnets,
            security_groups=[network.relay_security_group],
            environment={"MODEL_GATEWAY_URL": self.url},
            description="Forwards one HTTP request per invocation to the private LiteLLM proxy (no credentials of its own)",
            log_group=log_group(self, "RelayFunctionLogs", f"/aws/lambda/{config.RELAY_FUNCTION_NAME}"),
        )
        self.relay = relay

        # --- Outputs -------------------------------------------------------------------------------------------
        publish(self, "model-gateway/url", self.url, "LiteLLM base URL (OpenAI base = url + /v1); private, VPC only")
        publish(
            self,
            "model-gateway/relay-function-name",
            relay.function_name,
            "Relay Lambda (reach the proxy from outside)",
        )
        publish(self, "model-gateway/relay-function-arn", relay.function_arn, "Relay Lambda ARN")
        publish(self, "model-gateway/private", "true", "The proxy is reachable only from inside the VPC")
        publish(
            self, "model-gateway/master-key-secret-arn", master_secret.attr_id, "Secret holding the LiteLLM master key"
        )
        publish(
            self,
            "model-gateway/models",
            json.dumps(
                [{k: m[k] for k in ("alias", "modelId", "provider", "tier", "description")} for m in config.MODELS]
            ),
            "Approved model aliases",
        )
        CfnOutput(
            self,
            "RawEndpoint",
            value=service.attr_endpoint,
            description="Endpoint attribute as returned by ECS Express",
        )

    def _service_linked_role(self, service: str) -> cr.AwsCustomResource:
        """Ensure the service-linked role for `<service>.amazonaws.com` exists (no-op when it already does)."""
        call = cr.AwsSdkCall(
            service="IAM",
            action="CreateServiceLinkedRole",
            parameters={"AWSServiceName": f"{service}.amazonaws.com"},
            physical_resource_id=cr.PhysicalResourceId.of(f"slr-{service}"),
            ignore_error_codes_matching="InvalidInput.*",
        )
        return cr.AwsCustomResource(
            self,
            "ServiceLinkedRole" + "".join(part.title() for part in service.replace(".", "-").split("-")),
            resource_type="Custom::ServiceLinkedRole",
            on_create=call,
            log_group=self._sdk_logs,
            policy=cr.AwsCustomResourcePolicy.from_statements(
                [
                    iam.PolicyStatement(
                        actions=["iam:CreateServiceLinkedRole"],
                        resources=[f"arn:aws:iam::*:role/aws-service-role/{service}.amazonaws.com/*"],
                    )
                ]
            ),
        )

    @staticmethod
    def _https_url(endpoint: str) -> str:
        """Full https URL (no trailing slash) from the Express `Endpoint` attribute.

        Verified 2026-10-08: the attribute is the bare host name `ag-<hex>.ecs.us-east-1.on.aws` (no scheme, no slash). With
        private subnets the load balancer is internal: the same name resolves to private addresses (VPC residents only).
        """
        return f"https://{endpoint}"
