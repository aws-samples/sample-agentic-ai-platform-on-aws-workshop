"""Corporate IdP and the HR API behind it: Keycloak on Fargate behind an internal load balancer, exposed over HTTPS by an API
Gateway HTTP API with a VPC link, in a small public VPC of its own; and the HR HTTP API whose JWT authorizer admits only the
tokens Keycloak minted for the audience `hr-tools-api`.

Module 2 of the learning path (on-behalf-of; design: .claude/docs/keycloak-obo-design.md). The module proves a real on-behalf-of
chain: the employee's token is exchanged twice at Keycloak (by AgentCore Identity for the harness, then for the gateway) and the
last token reaches this API, which logs the claims it received. Keycloak therefore plays the corporate IdP a company runs or buys
as SaaS and lives *outside* the private landing zone: AgentCore Identity and API Gateway must fetch its discovery document and
call its token endpoint over the internet. Hence a second, deliberately simple VPC (two public subnets, an internet gateway, no
NAT) and one Fargate task running `start-dev` on the embedded H2 file database. The dev account has VPC Block Public Access
switched on (block-ingress), so the exclusion custom resource of the first, public landing zone is back (commit 894f915).

Exposure, decided after a live attempt on 2026-10-16: ECS Express Mode was tried first because it gives HTTPS for free, but its
managed service has no health-check grace period (healthCheckGracePeriodSeconds 0, target group probing /realms/master every 30 s
with an unhealthy threshold of 2) while Keycloak `start-dev` needs about 85 s to listen on 1 vCPU (33 s Quarkus augmentation,
28 s bootstrap, then the start): ECS killed every task after 60 s and the stack never stabilised. So: a standard Fargate service
with a five-minute grace period behind an *internal* application load balancer, and an API Gateway HTTP API with a VPC link as
the HTTPS door. That gives TLS on `https://<id>.execute-api.<region>.amazonaws.com` without owning a domain or a certificate, and
Keycloak never faces the internet itself. KC_HOSTNAME is that API URL, so every issuer and redirect Keycloak mints is stable and
is exactly what the three verifiers (the HR API's authorizer, the AgentCore Identity providers, the harness's JWT authorizer) are
configured with.

Realm and readiness, after the second live attempt the same day: API Gateway's JWT authorizer fetches the discovery document when
it is created and fails with "Invalid issuer" if the realm does not exist yet, so the realm cannot be left to `workshop apply 2`.
Keycloak imports foundation/keycloak/realm.json (the realm, its four clients, their mappers and the token-exchange switch; generated
by platform_sdk.keycloak.realm_json()) at every start: a busybox init container writes the file, carried in its environment, into
an ephemeral volume Keycloak mounts at data/import, so no custom image is needed, and a replaced task is a complete IdP again.
The two confidential clients' secrets are `${KC_SECRET_...}` placeholders Keycloak resolves from environment variables fed from
Secrets Manager. A small custom resource then polls the realm's discovery URL once the service exists, and the authorizer depends
on it. `workshop apply 2` only adds the demo employee through the admin REST API.

The HR API runs the same tool code as the HR tools Lambda of the Scenario stack, reached over HTTP instead of as a Lambda target:
AgentCore Gateway passes an exchanged OAuth token to OpenAPI and MCP-server targets only, never to Lambda targets. The stack
references no other stack (the table names are constants) and publishes `/agent-platform/idp/*` and `/agent-platform/hr-api/*`.
"""

from __future__ import annotations

import json

from aws_cdk import CfnOutput, CustomResource, Duration, Fn, RemovalPolicy, Stack, Token
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_elasticloadbalancingv2 as elbv2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_secretsmanager as sm
from aws_cdk import custom_resources as cr
from constructs import Construct

from . import config
from .common import log_group, publish, read_lambda_code


def client_secret_env(client_id: str) -> str:
    """Environment variable the realm file references for a confidential client's secret (`${KC_SECRET_HR_AGENT}` ...)."""
    return "KC_SECRET_" + client_id.upper().replace("-", "_")


class IdpStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, az_names: list[str], **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Network: a small public VPC of its own ------------------------------------------------------------
        # Two zones are plenty for one task and its load balancer. They are the first two of the names app.py resolved for the
        # landing zone (any two zones of the account would do; these are known to exist). The Vpc construct checks explicit zone
        # names against Stack.availability_zones, which the CDK CLI otherwise looks up with credentials, so the CLI's context key
        # is seeded here exactly like in the Network stack: synth stays credential-free, a cached CLI entry is left alone.
        zones = list(az_names)[:2]
        if not Token.is_unresolved(self.account) and not Token.is_unresolved(self.region):
            cli_key = f"availability-zones:account={self.account}:region={self.region}"
            if self.node.try_get_context(cli_key) is None:
                self.node.set_context(cli_key, list(az_names))
        self.vpc = ec2.Vpc(
            self,
            "Vpc",
            vpc_name=config.IDP_VPC_NAME,
            ip_addresses=ec2.IpAddresses.cidr(config.IDP_VPC_CIDR),
            availability_zones=zones,
            nat_gateways=0,
            restrict_default_security_group=False,  # avoids an extra custom resource; the default SG is unused
            subnet_configuration=[
                # Public subnets: the task gets a public address to pull the images from quay.io and public ECR and to reach
                # Secrets Manager and CloudWatch Logs (no NAT, no endpoints: this VPC is a stand-in for a SaaS IdP). Nothing
                # listens to the internet here: the load balancer is internal and the VPC link's interfaces are only reachable
                # by API Gateway.
                ec2.SubnetConfiguration(
                    name="public", subnet_type=ec2.SubnetType.PUBLIC, cidr_mask=24, map_public_ip_on_launch=True
                ),
            ],
        )
        public_subnets = ec2.SubnetSelection(subnet_group_name="public")
        bpa_exclusion = self._ensure_block_public_access_exclusion()

        # --- Security groups: API Gateway VPC link -> load balancer (80) -> task (8080) -------------------------
        link_sg = ec2.SecurityGroup(
            self,
            "VpcLinkSecurityGroup",
            vpc=self.vpc,
            description="API Gateway VPC link to the Keycloak load balancer",
            allow_all_outbound=True,
        )
        alb_sg = ec2.SecurityGroup(
            self,
            "LoadBalancerSecurityGroup",
            vpc=self.vpc,
            description="Keycloak load balancer (internal): HTTP from the VPC link only",
            allow_all_outbound=True,
        )
        alb_sg.add_ingress_rule(link_sg, ec2.Port.tcp(80), "API Gateway VPC link to the load balancer")
        # The task accepts 8080 from the load balancer's group only; CDK adds that rule when the service is registered as the
        # listener's target below (and the task's public address stays closed to the internet).
        task_sg = ec2.SecurityGroup(
            self,
            "TaskSecurityGroup",
            vpc=self.vpc,
            description="Keycloak task: 8080 from the load balancer only",
            allow_all_outbound=True,
        )

        # --- Keycloak: secrets, roles, cluster -----------------------------------------------------------------
        # Keycloak 26 creates its first admin from KC_BOOTSTRAP_ADMIN_USERNAME/PASSWORD on an empty database; `workshop apply 2`
        # logs in with them to add the demo employee. Generated once, never rotated (a rotation would not reach the running task).
        admin_secret = sm.Secret(
            self,
            "KeycloakAdminSecret",
            secret_name=config.KEYCLOAK_ADMIN_SECRET_NAME,
            description="Keycloak bootstrap admin password (user admin): the admin console and the admin REST API",
            generate_secret_string=sm.SecretStringGenerator(exclude_punctuation=True, password_length=32),
            removal_policy=RemovalPolicy.DESTROY,
        )
        # The two confidential clients' secrets. The realm file carries `${KC_SECRET_<CLIENT>}` placeholders that Keycloak
        # resolves from the container environment at import time (import/export guide), so the values are generated here,
        # injected into the container and published for `workshop apply 2`, which hands them to the AgentCore Identity OAuth2
        # providers. Letters and digits only: they travel through a shell-free env var and an OAuth client_secret parameter.
        client_secrets = {
            client_id: sm.Secret(
                self,
                "ClientSecret" + "".join(part.title() for part in client_id.split("-")),
                secret_name=name,
                description=f"Keycloak client secret of {client_id} (imported with the realm; its AgentCore Identity provider uses it)",
                generate_secret_string=sm.SecretStringGenerator(exclude_punctuation=True, password_length=40),
                removal_policy=RemovalPolicy.DESTROY,
            )
            for client_id, name in config.KEYCLOAK_CLIENT_SECRET_NAMES.items()
        }
        realm_json = config.KEYCLOAK_REALM_FILE.read_text(encoding="utf-8")
        for (
            client_id
        ) in client_secrets:  # the generator and the secret names must agree, or the import gets an empty secret
            placeholder = f"${{{client_secret_env(client_id)}}}"
            if placeholder not in realm_json:
                raise RuntimeError(
                    f"{config.KEYCLOAK_REALM_FILE} does not contain {placeholder}; "
                    "regenerate it with platform_sdk.keycloak.realm_json() or fix config.KEYCLOAK_CLIENT_SECRET_NAMES"
                )
        exec_role = iam.Role(
            self,
            "TaskExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Pulls the images, writes logs and injects the Keycloak secrets into the container",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AmazonECSTaskExecutionRolePolicy")
            ],
        )  # + secretsmanager:GetSecretValue on the three secrets, granted by the container's secret bindings below
        # Keycloak itself calls no AWS API (H2 file database, no SES, no S3): the task role exists because ECS wants one.
        task_role = iam.Role(
            self,
            "TaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            description="Keycloak task: no AWS permissions",
        )
        # ECS creates its service-linked role on the first cluster of an account, and a service created seconds later can still
        # fail to assume it (IAM propagation; the same race broke Express in a fresh account on 2026-10-08). Create it up front,
        # idempotently (InvalidInput = already exists), minutes before the service.
        # Unnamed on purpose: the custom-resource Lambda also runs during stack deletion, and late log delivery into a fixed
        # name would make the next deploy fail with "log group already exists" (seen on 2026-10-16 after a rollback).
        self._sdk_logs = self._unnamed_log_group("SdkCallLogs")
        ecs_slr = self._service_linked_role("ecs")
        cluster = ecs.Cluster(self, "Cluster", cluster_name=config.IDP_CLUSTER_NAME, vpc=self.vpc)
        cluster.node.add_dependency(ecs_slr)
        service_logs = log_group(self, "ServiceLogs", f"/{config.PREFIX}/keycloak")

        # --- The HTTPS door: HTTP API + VPC link ---------------------------------------------------------------
        # Declared before the task definition because KC_HOSTNAME is this API's URL. The chain is acyclic: API -> integration ->
        # listener/load balancer; task definition -> API endpoint; service -> task definition + target group.
        vpc_link = apigwv2.VpcLink(
            self,
            "VpcLink",
            vpc=self.vpc,
            subnets=public_subnets,
            security_groups=[link_sg],
            vpc_link_name=config.IDP_API_NAME,
        )
        idp_api = apigwv2.HttpApi(
            self,
            "IdpHttpApi",
            api_name=config.IDP_API_NAME,
            description="Keycloak (the corporate IdP) over HTTPS: every path proxied to the internal load balancer",
            create_default_stage=True,  # `$default`: paths map 1:1, so /realms/<realm> is the realm's issuer path
        )
        self.url = idp_api.api_endpoint  # https://<id>.execute-api.<region>.amazonaws.com (no trailing slash)
        # Every request to the IdP (token endpoint, admin console, admin API) is logged: source IP, path, status.
        idp_access_logs = log_group(self, "IdpAccessLogs", f"/{config.PREFIX}/idp-access")
        idp_stage = idp_api.default_stage.node.default_child
        assert isinstance(idp_stage, apigwv2.CfnStage)
        idp_stage.access_log_settings = apigwv2.CfnStage.AccessLogSettingsProperty(
            destination_arn=idp_access_logs.log_group_arn,
            format=json.dumps(
                {
                    "requestId": "$context.requestId",
                    "ip": "$context.identity.sourceIp",
                    "requestTime": "$context.requestTime",
                    "method": "$context.httpMethod",
                    "path": "$context.path",
                    "status": "$context.status",
                    "userAgent": "$context.identity.userAgent",
                }
            ),
        )
        # The OIDC issuer the three verifiers validate against (the HR API's authorizer below, the AgentCore Identity providers
        # and the harness's JWT authorizer); discovery at <issuer>/.well-known/openid-configuration once the realm is imported.
        self.issuer = f"{self.url}/realms/{config.KEYCLOAK_REALM}"

        # --- Keycloak task definition: realm-init writes the realm file, Keycloak imports it --------------------
        task_definition = ecs.FargateTaskDefinition(
            self,
            "TaskDefinition",
            cpu=config.KEYCLOAK_CPU,
            memory_limit_mib=config.KEYCLOAK_MEMORY,
            execution_role=exec_role,
            task_role=task_role,
        )
        # An ephemeral bind volume (no host, EFS or Docker configuration) shared by the two containers: realm-init fills it,
        # Keycloak reads it. The realm travels in realm-init's environment (about 8 KB, no secret in it) because a Fargate task
        # cannot carry files of its own without a custom image, and the CDK asset route would need an S3 pull in the container.
        task_definition.add_volume(name="import")
        realm_path = f"/import/{config.KEYCLOAK_REALM}.json"
        # The realm's `${KC_SECRET_...}` placeholders are Keycloak's, not CloudFormation's, but a bare `${...}` in a template
        # string trips lint rule E1029, which the CDK validation enforces at synth. Fn::Sub with the placeholders escaped as
        # `${!...}` is the documented way to say "literal": CloudFormation renders `${...}` back, so the container receives the
        # file byte for byte.
        realm_json_template = Fn.sub(realm_json.replace("${", "${!"))
        realm_init = task_definition.add_container(
            "realm-init",
            image=ecs.ContainerImage.from_registry(config.KEYCLOAK_REALM_INIT_IMAGE),
            essential=False,  # exits after writing the file; Keycloak waits for its SUCCESS (dependency below)
            command=["sh", "-c", f"printf '%s' \"$REALM_JSON\" > {realm_path} && echo wrote {realm_path}"],
            environment={"REALM_JSON": realm_json_template},
            logging=ecs.LogDrivers.aws_logs(stream_prefix="realm-init", log_group=service_logs),
        )
        realm_init.add_mount_points(ecs.MountPoint(container_path="/import", source_volume="import", read_only=False))
        keycloak_container = task_definition.add_container(
            "keycloak",
            image=ecs.ContainerImage.from_registry(
                config.KEYCLOAK_IMAGE
            ),  # pulled from quay.io through the internet gateway
            # Development mode: plain HTTP behind the load balancer, H2 file database, no build step; --import-realm reads
            # data/import at start-up (an existing realm is skipped, so the flag is harmless on an H2 file that survived).
            # API Gateway terminates TLS and forwards over HTTP, so Keycloak is told its public URL outright (KC_HOSTNAME:
            # issuers, redirects and the admin console all use it), trusts the X-Forwarded-* headers (KC_PROXY_HEADERS) and
            # does not insist on the Host header matching (KC_HOSTNAME_STRICT=false).
            command=["start-dev", "--import-realm"],
            port_mappings=[ecs.PortMapping(container_port=config.KEYCLOAK_PORT)],
            logging=ecs.LogDrivers.aws_logs(stream_prefix="keycloak", log_group=service_logs),
            environment={
                "KC_HTTP_ENABLED": "true",
                "KC_HTTP_PORT": str(config.KEYCLOAK_PORT),
                "KC_HOSTNAME": self.url,
                "KC_HOSTNAME_STRICT": "false",
                "KC_PROXY_HEADERS": "xforwarded",
                "KC_HEALTH_ENABLED": "true",
                "KC_BOOTSTRAP_ADMIN_USERNAME": config.KEYCLOAK_ADMIN_USERNAME,
                "KC_LOG_LEVEL": "INFO",
            },
            secrets={
                "KC_BOOTSTRAP_ADMIN_PASSWORD": ecs.Secret.from_secrets_manager(admin_secret),
                **{
                    client_secret_env(client_id): ecs.Secret.from_secrets_manager(secret)
                    for client_id, secret in client_secrets.items()
                },
            },
        )
        keycloak_container.add_mount_points(
            ecs.MountPoint(container_path="/opt/keycloak/data/import", source_volume="import", read_only=True)
        )
        keycloak_container.add_container_dependencies(
            ecs.ContainerDependency(container=realm_init, condition=ecs.ContainerDependencyCondition.SUCCESS)
        )

        # --- Fargate service behind an internal load balancer --------------------------------------------------
        service = ecs.FargateService(
            self,
            "Service",
            cluster=cluster,
            task_definition=task_definition,
            service_name=config.KEYCLOAK_SERVICE_NAME,
            desired_count=1,
            assign_public_ip=True,  # the image pulls and the AWS API calls go out through the internet gateway (no NAT)
            vpc_subnets=public_subnets,
            security_groups=[task_sg],
            # Keycloak needs about 85 s to listen (see the module docstring), plus the realm import; the target group may not
            # count failed probes against the task before that. Five minutes leaves room for a slow pull of the 450 MB image.
            health_check_grace_period=Duration.minutes(5),
            # One task at a time: the H2 database lives in the task's ephemeral storage, so two tasks would be two IdPs with
            # different users and sessions. A redeploy stops the old task first (a couple of minutes of downtime, acceptable).
            min_healthy_percent=0,
            max_healthy_percent=100,
            # Fail the deployment instead of letting CloudFormation wait hours for a service that never stabilises.
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=False),
        )
        service.node.add_dependency(ecs_slr)
        # The first task pulls through the internet gateway: with Block Public Access on, only after the exclusion (which itself
        # waits for the whole VPC, routes included).
        service.node.add_dependency(bpa_exclusion)

        # Internal: private addresses in the public subnets; only the VPC link's interfaces may talk to it (security group).
        load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "LoadBalancer",
            vpc=self.vpc,
            internet_facing=False,
            vpc_subnets=public_subnets,
            security_group=alb_sg,
        )
        listener = load_balancer.add_listener(
            "Http", port=80, protocol=elbv2.ApplicationProtocol.HTTP, open=False
        )  # open=False: no 0.0.0.0/0 ingress rule; the VPC link rule above is the only way in
        listener.add_targets(
            "Keycloak",
            port=config.KEYCLOAK_PORT,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[service.load_balancer_target(container_name="keycloak", container_port=config.KEYCLOAK_PORT)],
            # /realms/master answers 200 on the application port as soon as Keycloak accepts requests (the management port
            # 9000 with /health is not behind the load balancer). Fast probes once up; five misses before a task is replaced.
            health_check=elbv2.HealthCheck(
                path=config.KEYCLOAK_HEALTH_PATH,
                interval=Duration.seconds(15),
                timeout=Duration.seconds(5),
                healthy_threshold_count=2,
                unhealthy_threshold_count=5,
                healthy_http_codes="200",
            ),
            deregistration_delay=Duration.seconds(30),
        )
        # One HTTP_PROXY integration shared by both routes: API Gateway forwards the path untouched, so Keycloak's realm, token,
        # JWKS, account and admin console URLs all work under the API's host name. No authorizer here: Keycloak authenticates.
        keycloak = integrations.HttpAlbIntegration("Keycloak", listener, vpc_link=vpc_link)
        idp_api.add_routes(path="/{proxy+}", methods=[apigwv2.HttpMethod.ANY], integration=keycloak)
        idp_api.add_routes(path="/", methods=[apigwv2.HttpMethod.ANY], integration=keycloak)
        self.idp_api = idp_api
        self.service = service
        self.load_balancer = load_balancer

        # --- Readiness gate: the realm's discovery document must be served before the JWT authorizer exists --------
        # CloudFormation considers the service created once its task passed the load balancer's health check (Keycloak up, the
        # realm imported at start-up), so the poll normally succeeds at once; the 14-minute budget covers a slow first task. No
        # VPC: the Lambda calls the public API URL like API Gateway and AgentCore Identity will. Delete, and an Update that keeps
        # the URL, are no-ops. Unnamed log groups for the same reason as the exclusion's.
        discovery_url = f"{self.issuer}/.well-known/openid-configuration"
        ready_fn = lam.Function(
            self,
            "KeycloakReadyFunction",
            runtime=lam.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lam.Code.from_inline(read_lambda_code("keycloak_ready.py")),
            timeout=Duration.minutes(14),  # the Provider framework's own limit is 15
            description="Waits until Keycloak serves the realm's discovery document with the expected issuer (custom resource)",
            log_group=self._unnamed_log_group("KeycloakReadyLogs"),
        )
        ready_provider = cr.Provider(
            self,
            "KeycloakReadyProvider",
            on_event_handler=ready_fn,
            log_group=self._unnamed_log_group("KeycloakReadyProviderLogs"),
        )
        keycloak_ready = CustomResource(
            self,
            "KeycloakReady",
            service_token=ready_provider.service_token,
            resource_type="Custom::KeycloakReady",
            properties={"Url": discovery_url, "Issuer": self.issuer},
        )
        keycloak_ready.node.add_dependency(service)
        CfnOutput(
            self,
            "KeycloakReadySeconds",
            value=keycloak_ready.get_att_string("Seconds"),
            description="Seconds the stack waited for the realm's discovery document after the service was created",
        )

        # --- HR API: the OBO chain's last hop --------------------------------------------------------------------
        # Same code asset as the HR tools Lambda (scenario/tools/hr); http_handler.handler unwraps the HTTP API event, logs the
        # claims of the exchanged token API Gateway verified, and calls handler.handler unchanged. The table names are the
        # Scenario stack's constants, not references: this stack deploys and destroys on its own.
        hr_api_log_group_name = f"/aws/lambda/{config.HR_API_FUNCTION_NAME}"
        hr_api_fn = lam.Function(
            self,
            "HrApiFunction",
            function_name=config.HR_API_FUNCTION_NAME,
            runtime=lam.Runtime.PYTHON_3_13,
            handler="http_handler.handler",
            code=lam.Code.from_asset(str(config.SCENARIO_HR_TOOLS_DIR)),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={  # the HR tools read profiles and balances and write leave requests; invoices are Finance's
                "EMPLOYEES_TABLE": config.SCENARIO_TABLES["employees"],
                "LEAVE_REQUESTS_TABLE": config.SCENARIO_TABLES["leave-requests"],
            },
            description="AnyCompany HR tools over HTTP: the HR tools handler behind the JWT authorizer (on-behalf-of module)",
            log_group=log_group(self, "HrApiFunctionLogs", hr_api_log_group_name),
        )
        # The same grants the Scenario stack gives this code: employees read-only, leave-requests read/write (its by_employee
        # index included), nothing on invoices. This is the one internet-reachable path to the tools, so it gets no more.
        employees_arn = self.format_arn(
            service="dynamodb", resource="table", resource_name=config.SCENARIO_TABLES["employees"]
        )
        leave_arn = self.format_arn(
            service="dynamodb", resource="table", resource_name=config.SCENARIO_TABLES["leave-requests"]
        )
        hr_api_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadEmployees",
                actions=["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"],
                resources=[employees_arn, f"{employees_arn}/index/*"],
            )
        )
        hr_api_fn.add_to_role_policy(
            iam.PolicyStatement(
                sid="ReadWriteLeaveRequests",
                actions=[
                    "dynamodb:GetItem",
                    "dynamodb:Query",
                    "dynamodb:Scan",
                    "dynamodb:PutItem",
                    "dynamodb:UpdateItem",
                ],
                resources=[leave_arn, f"{leave_arn}/index/*"],
            )
        )
        # API Gateway validates signature, expiry, issuer and audience of the bearer token against the realm's JWKS; the Lambda
        # never sees an unverified token. Only the final exchanged token (aud hr-tools-api) passes; T0 and T1 are rejected.
        authorizer = authorizers.HttpJwtAuthorizer("KeycloakJwt", self.issuer, jwt_audience=[config.HR_API_AUDIENCE])
        http_api = apigwv2.HttpApi(
            self,
            "HrHttpApi",
            api_name=config.HR_API_NAME,
            description="AnyCompany HR tools API: POST /tools/{tool} with a Keycloak token for audience hr-tools-api",
            create_default_stage=True,
            default_authorizer=authorizer,
        )
        http_api.add_routes(
            path="/tools/{tool}",
            methods=[apigwv2.HttpMethod.POST],
            integration=integrations.HttpLambdaIntegration("HrApiIntegration", hr_api_fn),
        )
        # The authorizer resource exists only now (HttpJwtAuthorizer binds when the first route uses it); it must wait for the
        # readiness gate, and the route waits for the authorizer by reference.
        cfn_authorizers = [c for c in http_api.node.find_all() if isinstance(c, apigwv2.CfnAuthorizer)]
        if (
            len(cfn_authorizers) != 1
        ):  # the construct library changed; look at the synthesized template before adjusting this
            raise RuntimeError(f"expected one authorizer on the HR API, found {len(cfn_authorizers)}")
        cfn_authorizers[0].node.add_dependency(keycloak_ready)
        # Access log with the verified claims: the evidence `workshop obo` prints (sub, aud and azp show the exchange chain,
        # employee_id the custom claim). If CloudFormation rejects `$context.authorizer.claims.*` for a JWT authorizer, drop
        # those four keys; the Lambda logs the same claims (design, open point 3).
        access_log_group_name = f"/{config.PREFIX}/hr-api-access"
        access_logs = log_group(self, "HrApiAccessLogs", access_log_group_name)
        stage = http_api.default_stage.node.default_child
        assert isinstance(stage, apigwv2.CfnStage)
        stage.access_log_settings = apigwv2.CfnStage.AccessLogSettingsProperty(
            destination_arn=access_logs.log_group_arn,
            format=json.dumps(
                {
                    "requestId": "$context.requestId",
                    "ip": "$context.identity.sourceIp",
                    "requestTime": "$context.requestTime",
                    "routeKey": "$context.routeKey",
                    "status": "$context.status",
                    "integrationError": "$context.integrationErrorMessage",
                    "sub": "$context.authorizer.claims.sub",
                    "aud": "$context.authorizer.claims.aud",
                    "azp": "$context.authorizer.claims.azp",
                    "employee_id": "$context.authorizer.claims.employee_id",
                }
            ),
        )
        self.http_api = http_api
        self.api_url = http_api.api_endpoint  # https://<id>.execute-api.<region>.amazonaws.com (no trailing slash)

        # --- Outputs -------------------------------------------------------------------------------------------
        publish(self, "idp/url", self.url, "Keycloak base URL (the HTTPS door; admin console at /admin)")
        publish(
            self,
            "idp/issuer",
            self.issuer,
            "OIDC issuer of the workshop realm (discovery at /.well-known/openid-configuration)",
        )
        publish(
            self,
            "idp/realm",
            config.KEYCLOAK_REALM,
            "Keycloak realm, imported at every start from foundation/keycloak/realm.json",
        )
        publish(
            self,
            "idp/admin-secret-arn",
            admin_secret.secret_arn,
            "Secret holding the Keycloak bootstrap admin password",
        )
        publish(self, "idp/admin-username", config.KEYCLOAK_ADMIN_USERNAME, "Keycloak bootstrap admin user")
        for client_id, secret in client_secrets.items():
            publish(
                self,
                f"idp/secret-{client_id}-arn",
                secret.secret_arn,
                f"Secret holding the Keycloak client secret of {client_id}",
            )
        publish(
            self,
            "idp/alb-dns",
            load_balancer.load_balancer_dns_name,
            "Internal load balancer in front of Keycloak (VPC only; for debugging)",
        )
        publish(
            self, "hr-api/url", self.api_url, "HR tools HTTP API base URL (POST /tools/{tool}, bearer token required)"
        )
        publish(self, "hr-api/audience", config.HR_API_AUDIENCE, "Audience the HR API's JWT authorizer requires")
        publish(self, "hr-api/function-name", hr_api_fn.function_name, "HR API Lambda")
        publish(
            self, "hr-api/log-group", hr_api_log_group_name, "Log group of the HR API Lambda (the claims it received)"
        )
        publish(self, "hr-api/access-log-group", access_log_group_name, "API Gateway access log of the HR API")

    def _unnamed_log_group(self, id_: str) -> logs.LogGroup:
        """A 30-day log group with a generated name, deleted with the stack: for the custom-resource Lambdas, which also run
        during stack deletion, when late log delivery into a fixed name would block a same-account redeploy (README gotcha)."""
        return logs.LogGroup(self, id_, retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.DESTROY)

    def _ensure_block_public_access_exclusion(self) -> CustomResource:
        """Lambda-backed custom resource: DescribeVpcBlockPublicAccessOptions and, when the mode is not "off", an
        allow-bidirectional exclusion for this VPC (adopted when one exists, created otherwise; deleted on stack deletion only
        when created here)."""
        fn = lam.Function(
            self,
            "VpcBpaExclusionFunction",
            runtime=lam.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lam.Code.from_inline(read_lambda_code("vpc_bpa_exclusion.py")),
            timeout=Duration.minutes(5),
            description="Adopts or creates the VPC Block Public Access exclusion for the IdP VPC",
            log_group=self._unnamed_log_group("VpcBpaExclusionLogs"),
        )
        fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=[
                    "ec2:DescribeVpcBlockPublicAccessOptions",
                    "ec2:DescribeVpcBlockPublicAccessExclusions",
                    "ec2:CreateVpcBlockPublicAccessExclusion",
                    "ec2:DeleteVpcBlockPublicAccessExclusion",
                    "ec2:CreateTags",
                ],
                resources=["*"],
            )
        )
        provider = cr.Provider(
            self,
            "VpcBpaExclusionProvider",
            on_event_handler=fn,
            log_group=self._unnamed_log_group("VpcBpaExclusionProviderLogs"),
        )
        exclusion = CustomResource(
            self,
            "VpcBpaExclusion",
            service_token=provider.service_token,
            resource_type="Custom::VpcBlockPublicAccessExclusion",
            properties={"VpcId": self.vpc.vpc_id, "Name": f"{config.IDP_VPC_NAME}-vpc", "Project": config.PROJECT_TAG},
        )
        exclusion.node.add_dependency(self.vpc)
        CfnOutput(self, "VpcBpaExclusionId", value=exclusion.get_att_string("ExclusionId"))
        CfnOutput(self, "VpcBpaMode", value=exclusion.get_att_string("Mode"))
        return exclusion

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
