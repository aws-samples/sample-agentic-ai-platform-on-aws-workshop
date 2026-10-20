"""Platform: DynamoDB table, backend role, API and worker Lambdas, HTTP API with the Cognito authorizer, EventBridge rule,
and the UI runtime configuration (config.json) written into the Web bucket.

No reference to the ModelGateway stack on purpose: the backend reads the model gateway URL and master key from SSM at run time
and the UI takes the URL from GET /governance, so the control plane deploys before the landing zone (learning path, module 1)."""

from __future__ import annotations

import json

from aws_cdk import (
    ArnFormat,
    Duration,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lam
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import custom_resources as cr
from constructs import Construct

from . import config
from .auth_stack import AuthStack
from .common import log_group, publish
from .web_stack import WebStack


class PlatformStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        auth: AuthStack,
        web: WebStack,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- State ---------------------------------------------------------------------------------------------
        table = dynamodb.Table(
            self,
            "Table",
            table_name=config.TABLE_NAME,
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            time_to_live_attribute="ttl",
            removal_policy=RemovalPolicy.DESTROY,
            deletion_protection=False,
        )
        table.add_global_secondary_index(
            index_name="GSI1",
            partition_key=dynamodb.Attribute(name="GSI1PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="GSI1SK", type=dynamodb.AttributeType.STRING),
        )
        self.table = table

        # --- One role for both Lambdas -------------------------------------------------------------------------
        # The ARNs below are built by hand on purpose: a reference to the worker function from the role's own policy would
        # make the function depend on that policy (CloudFormation cycle). Lambda and Secrets Manager ARNs use ":" before the
        # resource name; the CDK default is "/", which never matches (review finding, verified with simulate-principal-policy
        # on 2026-10-08), so the format is explicit. SSM parameter and IAM role ARNs use "/" (the default).
        colon = ArnFormat.COLON_RESOURCE_NAME
        worker_arn = self.format_arn(
            service="lambda", resource="function", resource_name=config.WORKER_FUNCTION_NAME, arn_format=colon
        )
        master_key_secret_arn = self.format_arn(
            service="secretsmanager",
            resource="secret",
            resource_name=f"{config.MASTER_KEY_SECRET_NAME}-*",
            arn_format=colon,
        )
        users_secret_arn = self.format_arn(
            service="secretsmanager",
            resource="secret",
            resource_name=f"{config.USERS_SECRET_NAME}-*",
            arn_format=colon,
        )
        token_vault_secrets_arn = self.format_arn(
            service="secretsmanager",
            resource="secret",
            resource_name=f"{config.TOKEN_VAULT_SECRET_PREFIX}/*",
            arn_format=colon,
        )
        evaluation_role_arn = self.format_arn(
            service="iam", region="", resource="role", resource_name=config.EVALUATION_ROLE_NAME
        )
        role = iam.Role(
            self,
            "BackendRole",
            role_name=config.BACKEND_ROLE_NAME,
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Platform API and worker: state, configuration, AgentCore, LiteLLM provisioning, project roles",
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
            ],
        )
        table.grant_read_write_data(role)
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
                resources=[
                    # GetParametersByPath is authorised against the path itself as well as the parameters below it
                    self.format_arn(service="ssm", resource="parameter", resource_name=config.PREFIX),
                    self.format_arn(service="ssm", resource="parameter", resource_name=f"{config.PREFIX}/*"),
                ],
            )
        )
        # Two platform secrets are read: the LiteLLM master key (team and key provisioning, /spend, /governance) and the persona
        # passwords (POST /me/switch signs in as another persona for the UI's "Switch persona" menu, a workshop shortcut).
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"], resources=[master_key_secret_arn, users_secret_arn]
            )
        )
        # AgentCore Identity keeps API-key credential providers in Secrets Manager secrets named
        # bedrock-agentcore-identity!default/apikey/<provider> and creates, updates and deletes them with forward-access
        # sessions under the caller's identity (CloudTrail, 2026-10-08), so Create/Update/DeleteApiKeyCredentialProvider
        # from the backend (POST/PATCH/DELETE keys, deploy and promote jobs) need these actions.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "secretsmanager:CreateSecret",
                    "secretsmanager:PutSecretValue",
                    "secretsmanager:UpdateSecret",
                    "secretsmanager:DeleteSecret",
                    "secretsmanager:DescribeSecret",
                    "secretsmanager:GetSecretValue",
                    "secretsmanager:TagResource",
                ],
                resources=[token_vault_secrets_arn],
            )
        )
        role.add_to_policy(iam.PolicyStatement(actions=["bedrock-agentcore:*", "agent-registry:*"], resources=["*"]))
        # Metrics for the Overview, and the log reads of the evaluate job: the dataset runner's span collector reads the
        # harness log group (FilterLogEvents / GetLogEvents / Logs Insights queries) before the evaluators score the sessions.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "cloudwatch:GetMetricData",
                    "cloudwatch:ListMetrics",
                    "logs:StartQuery",
                    "logs:StopQuery",
                    "logs:GetQueryResults",
                    "logs:FilterLogEvents",
                    "logs:GetLogEvents",
                    "logs:DescribeLogGroups",
                    "logs:DescribeLogStreams",
                ],
                resources=["*"],
            )
        )
        # The evaluate job reads the blueprint's dataset (spec.evaluation.dataset, e.g. evaluation/hr-assistant.json) from the
        # scenario dataset bucket. Its name is deterministic, so no reference to the Scenario stack is needed.
        dataset_bucket_arn = f"arn:aws:s3:::{config.DATASET_BUCKET_PREFIX}-{self.account}"
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["s3:GetObject", "s3:ListBucket"], resources=[dataset_bucket_arn, f"{dataset_bucket_arn}/*"]
            )
        )
        # Everything platform_sdk.templates.roles does to the project roles (create, re-stamp, tag, delete)
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "iam:CreateRole",
                    "iam:GetRole",
                    "iam:DeleteRole",
                    "iam:UpdateAssumeRolePolicy",
                    "iam:PutRolePolicy",
                    "iam:DeleteRolePolicy",
                    "iam:ListRolePolicies",
                    "iam:ListAttachedRolePolicies",
                    "iam:DetachRolePolicy",
                    "iam:TagRole",
                    "iam:UntagRole",
                ],
                resources=[config.PROJECT_ROLE_ARN_PATTERN],
            )
        )
        # Roles handed to AgentCore: the project roles (CreateGateway, CreateHarness) and the evaluation role
        # (CreateOnlineEvaluationConfig on promotion). The service the role is passed to is always bedrock-agentcore.
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[config.PROJECT_ROLE_ARN_PATTERN, evaluation_role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "bedrock-agentcore.amazonaws.com"}},
            )
        )
        # The API Lambda starts the worker (InvocationType Event) for POST /invoke, deploy and promote. The model gateway is
        # private: every LiteLLM call from the backend (teams, keys, spend, health) goes through the relay Lambda inside the VPC;
        # its ARN is built by hand for the same reason as the worker's (and the Platform stack never references ModelGateway).
        relay_arn = self.format_arn(
            service="lambda", resource="function", resource_name=config.RELAY_FUNCTION_NAME, arn_format=colon
        )
        role.add_to_policy(iam.PolicyStatement(actions=["lambda:InvokeFunction"], resources=[worker_arn, relay_arn]))
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "bedrock:ListFoundationModels",
                    "bedrock:GetGuardrail",
                    "bedrock:ListGuardrails",
                    "bedrock:ListKnowledgeBases",  # GET /governance/project-options: the knowledge-base pick list
                    "bedrock:GetKnowledgeBase",
                    "bedrock:Retrieve",
                ],
                resources=["*"],
            )
        )
        # AgentCore Runtime creates two service-linked roles with the caller's permissions: runtime-identity on the account's
        # first CreateHarness (failed with 403 from the backend role in a fresh account, 15 Oct 2026) and network on the first
        # harness in VPC mode (its ENIs in our subnets). The backend may create exactly these two, as a second line of defence
        # behind the custom resources below.
        slr_services = {
            "Runtime": config.AGENTCORE_RUNTIME_SLR_SERVICE,
            "Network": config.AGENTCORE_NETWORK_SLR_SERVICE,
        }
        slr_arns = {
            name: self.format_arn(
                service="iam", region="", resource="role", resource_name=f"aws-service-role/{service}/*"
            )
            for name, service in slr_services.items()
        }
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:CreateServiceLinkedRole"],
                resources=list(slr_arns.values()),
                conditions={"StringEquals": {"iam:AWSServiceName": list(slr_services.values())}},
            )
        )
        self.backend_role = role

        # --- AgentCore service-linked roles, created up front -----------------------------------------------------
        # Idempotent: InvalidInput means the role exists already (same pattern as the model gateway's ECS roles). Nothing on
        # delete: the roles are account-level and harmless, like the Transaction Search setting.
        sdk_logs = log_group(self, "SdkCallLogs", f"/aws/lambda/{config.PREFIX}-platform-sdk-calls")
        for name, service in slr_services.items():
            slr_call = cr.AwsSdkCall(
                service="IAM",
                action="CreateServiceLinkedRole",
                parameters={"AWSServiceName": service},
                physical_resource_id=cr.PhysicalResourceId.of(f"slr-{service}"),
                ignore_error_codes_matching="InvalidInput.*",
            )
            cr.AwsCustomResource(
                self,
                f"AgentCore{name}ServiceLinkedRole",
                resource_type=f"Custom::AgentCore{name}ServiceLinkedRole",
                on_create=slr_call,
                log_group=sdk_logs,
                policy=cr.AwsCustomResourcePolicy.from_statements(
                    [iam.PolicyStatement(actions=["iam:CreateServiceLinkedRole"], resources=[slr_arns[name]])]
                ),
            )

        # --- Lambdas (same bundle: backend/src + platform_sdk + pyyaml + requests) -----------------------------
        code = lam.Code.from_asset(str(config.BUILD_BACKEND_DIR))
        environment = {"TABLE_NAME": table.table_name, "WORKER_FUNCTION_NAME": config.WORKER_FUNCTION_NAME}
        api_fn = lam.Function(
            self,
            "ApiFunction",
            function_name=config.API_FUNCTION_NAME,
            runtime=lam.Runtime.PYTHON_3_13,
            handler="app.handler",
            code=code,
            timeout=Duration.seconds(29),
            memory_size=512,
            role=role,
            environment=environment,
            description="Platform API (HTTP API proxy handler + EventBridge events)",
            log_group=log_group(self, "ApiFunctionLogs", f"/aws/lambda/{config.API_FUNCTION_NAME}"),
        )
        worker_fn = lam.Function(
            self,
            "WorkerFunction",
            function_name=config.WORKER_FUNCTION_NAME,
            runtime=lam.Runtime.PYTHON_3_13,
            handler="worker.handler",
            code=code,
            timeout=Duration.minutes(15),
            memory_size=1024,
            role=role,
            environment=environment,
            description="Platform worker (invoke, deploy, promote, release and evaluate jobs)",
            log_group=log_group(self, "WorkerFunctionLogs", f"/aws/lambda/{config.WORKER_FUNCTION_NAME}"),
            # One attempt per job: Lambda's default two asynchronous retries would replay a deploy (another harness
            # version), a promote or a billed invoke after a timeout or crash. The job record carries the outcome instead.
            retry_attempts=0,
        )

        # --- HTTP API ------------------------------------------------------------------------------------------
        integration = integrations.HttpLambdaIntegration("ApiIntegration", api_fn)
        authorizer = authorizers.HttpUserPoolAuthorizer(
            "UserPoolAuthorizer", auth.user_pool, user_pool_clients=[auth.client]
        )
        api = apigwv2.HttpApi(
            self,
            "HttpApi",
            api_name=config.API_NAME,
            description="Agentic AI platform control-plane API",
            cors_preflight=apigwv2.CorsPreflightOptions(
                # Only the deployed UI and the Vite dev server are browser callers (review finding: `*` let any page that holds
                # a token drive the control plane from its own origin); the SDK and the `workshop` command send no Origin header.
                allow_origins=[web.url, config.LOCAL_UI_URL.rstrip("/")],
                allow_headers=[
                    "Authorization",
                    "Content-Type",
                    "X-Amz-Date",
                    "X-Api-Key",
                    "X-Amz-Security-Token",
                    "X-Requested-With",
                ],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.PUT,
                    apigwv2.CorsHttpMethod.PATCH,
                    apigwv2.CorsHttpMethod.DELETE,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
                max_age=Duration.hours(1),
            ),
            default_authorizer=authorizer,
            default_integration=integration,  # $default route, protected by the user pool authorizer
        )
        api.add_routes(
            path="/health",
            methods=[apigwv2.HttpMethod.GET],
            integration=integration,
            authorizer=apigwv2.HttpNoneAuthorizer(),
        )
        # Browser preflight: without this route OPTIONS requests fall into $default and the JWT authorizer answers 401
        # (verified 2026-10-08). API Gateway does not short-circuit preflights when a route matches, so the API Lambda
        # answers OPTIONS with an empty 204 and API Gateway adds the CORS headers (backend contract).
        api.add_routes(
            path="/{proxy+}",
            methods=[apigwv2.HttpMethod.OPTIONS],
            integration=integration,
            authorizer=apigwv2.HttpNoneAuthorizer(),
        )
        access_logs = log_group(self, "ApiAccessLogs", f"/aws/vendedlogs/{config.API_NAME}-access")
        stage = api.default_stage.node.default_child
        assert isinstance(stage, apigwv2.CfnStage)
        stage.access_log_settings = apigwv2.CfnStage.AccessLogSettingsProperty(
            destination_arn=access_logs.log_group_arn,
            format=json.dumps(
                {
                    "requestId": "$context.requestId",
                    "ip": "$context.identity.sourceIp",
                    "requestTime": "$context.requestTime",
                    "httpMethod": "$context.httpMethod",
                    "path": "$context.path",
                    "routeKey": "$context.routeKey",
                    "status": "$context.status",
                    "responseLength": "$context.responseLength",
                    "integrationError": "$context.integrationErrorMessage",
                    "user": "$context.authorizer.claims.email",
                }
            ),
        )
        self.api = api
        self.api_url = api.api_endpoint  # https://<id>.execute-api.<region>.amazonaws.com (no trailing slash)

        # --- Agent Registry events -> API Lambda ---------------------------------------------------------------
        events.Rule(
            self,
            "RegistryEvents",
            rule_name=f"{config.PREFIX}-registry-events",
            description="Agent Registry record events refresh the catalogue cache",
            event_pattern=events.EventPattern(source=["aws.agent-registry"]),
            targets=[targets.LambdaFunction(api_fn)],
        )

        # --- UI runtime configuration --------------------------------------------------------------------------
        s3deploy.BucketDeployment(
            self,
            "UiConfig",
            destination_bucket=web.bucket,
            sources=[
                s3deploy.Source.json_data(
                    "config.json",
                    {
                        "apiUrl": self.api_url,
                        "region": self.region,
                        "userPoolId": auth.user_pool.user_pool_id,
                        "clientId": auth.client.user_pool_client_id,
                        "authDomain": auth.auth_domain,
                        "uiUrl": web.url,
                    },
                )
            ],
            prune=False,
            distribution=web.distribution,
            distribution_paths=["/config.json"],
            log_group=log_group(self, "UiConfigLogs", f"/aws/lambda/{config.PREFIX}-ui-config"),
        )

        publish(self, "api/url", self.api_url, "Platform API base URL")
        publish(self, "table/name", table.table_name, "Platform DynamoDB table")
        publish(self, "backend/worker-function-name", worker_fn.function_name, "Async worker Lambda")
        publish(self, "roles/backend-role-arn", role.role_arn, "Backend Lambda role")
