"""One place for names and constants shared by all foundation stacks.

Nothing here may depend on a particular AWS account: account and region come from the CDK environment at synth time.
"""

from __future__ import annotations

from pathlib import Path

# Repository layout (paths are resolved from this file so `cdk synth` works from any working directory)
REPO_ROOT = Path(__file__).resolve().parents[2]
FOUNDATION_DIR = REPO_ROOT / "foundation"
BUILD_BACKEND_DIR = REPO_ROOT / "build" / "backend"
UI_DIST_DIR = REPO_ROOT / "frontend" / "dist"
PLACEHOLDER_DIR = FOUNDATION_DIR / "placeholder"
LITELLM_CONFIG_TEMPLATE = FOUNDATION_DIR / "litellm" / "config.yaml.tpl"
LAMBDAS_DIR = FOUNDATION_DIR / "foundation" / "lambdas"

# Naming
PREFIX = "agent-platform"
SSM_PREFIX = "/agent-platform/"
PROJECT_TAG = "workshop-agent-platform"

STACK_NETWORK = "AgentPlatform-Network"
STACK_SHARED = "AgentPlatform-SharedServices"
STACK_AUTH = "AgentPlatform-Auth"
STACK_MODEL_GATEWAY = "AgentPlatform-ModelGateway"
STACK_PLATFORM = "AgentPlatform-Platform"
STACK_WEB = "AgentPlatform-Web"
STACK_SCENARIO = "AgentPlatform-Scenario"
STACK_IDP = "AgentPlatform-Idp"
# Two independent chains (app.py). The learning path deploys the control plane and the sample data before the day and lets the
# learners deploy the landing zone in module 1 (`workshop apply 1`); `scripts/deploy.sh --all` still deploys everything at once.
PRE_DAY_STACKS = [STACK_WEB, STACK_AUTH, STACK_PLATFORM, STACK_SCENARIO]
LANDING_ZONE_STACKS = [STACK_NETWORK, STACK_SHARED, STACK_MODEL_GATEWAY]
# Module 2 (on-behalf-of with Keycloak; .claude/docs/keycloak-obo-design.md): a third independent chain, deployed by
# `workshop apply 2` and destroyed with the rest by `workshop cleanup --stacks`.
OBO_STACKS = [STACK_IDP]

LOG_RETENTION_DAYS = 30

# Model gateway (LiteLLM) - build contract section 7
LITELLM_IMAGE = "ghcr.io/berriai/litellm:v1.104.1"
LITELLM_PORT = 4000
LITELLM_HEALTH_PATH = "/health/liveliness"
LITELLM_CPU = "1024"
LITELLM_MEMORY = "2048"
LITELLM_SERVICE_NAME = f"{PREFIX}-model-gateway"
LITELLM_CLUSTER_NAME = PREFIX
LITELLM_CONFIG_KEY = "config.yaml"
MASTER_KEY_SECRET_NAME = f"{PREFIX}/litellm-master-key"
SALT_KEY_SECRET_NAME = f"{PREFIX}/litellm-salt-key"
DB_SECRET_NAME = f"{PREFIX}/litellm-db"
DB_NAME = "litellm"
DB_USERNAME = "litellm"
# db.t4g.micro is not orderable for PostgreSQL 17/18 in us-east-1 (checked 2026-10-08); db.t3.micro is the smallest class that is.
DB_ENGINE_VERSION = ("18.6", "18")  # (full version, major version) for rds.PostgresEngineVersion.of
DB_INSTANCE_CLASS = "t3"
DB_INSTANCE_SIZE = "micro"
DB_MAINTENANCE_WINDOW = "sun:06:00-sun:06:30"  # UTC; outside any one-day workshop
ALB_IDLE_TIMEOUT_SECONDS = "600"

# Private landing zone (design: .claude/docs/private-landing-zone-design.md). The VPC has no internet gateway and no NAT: the
# LiteLLM tasks, the harness ENIs and the relay Lambda sit on isolated `app` subnets and reach AWS through VPC endpoints.
VPC_CIDR = "10.0.0.0/16"
# AgentCore Runtime in VPC mode only accepts subnets in these zone ids (us-east-1; a documentation table, update it when the
# table changes). The zone *names* carrying them differ per account, so foundation/azs.py resolves the names at synth time
# and caches them in cdk.context.json under HARNESS_AZS_CONTEXT_KEY:<account>:<region>; FALLBACK_AZS serve a synth without
# credentials (unit tests).
HARNESS_AZ_IDS = ["use1-az1", "use1-az2", "use1-az4"]
HARNESS_AZS_CONTEXT_KEY = "agent-platform:harness-azs"
FALLBACK_AZS = ["us-east-1a", "us-east-1b", "us-east-1c"]
# Interface endpoints cost about 0.01 USD per AZ-hour each; two of the three zones are enough (the third reaches them cross-AZ).
ENDPOINT_AZ_COUNT = 2
ENDPOINTS_SECURITY_GROUP_NAME = f"{PREFIX}-endpoints"
HARNESS_SECURITY_GROUP_NAME = f"{PREFIX}-harness"
RELAY_SECURITY_GROUP_NAME = f"{PREFIX}-relay"
# The LiteLLM image is copied from ghcr.io into this private repository at deploy time (cdk-ecr-deployment) because the tasks
# have no internet path; the tag is the version part of LITELLM_IMAGE (tests/test_synth.py checks they agree).
LITELLM_REPOSITORY_NAME = f"{PREFIX}/litellm"
LITELLM_IMAGE_TAG = "v1.104.1"
# The only door into the private proxy from outside the VPC: a Lambda inside the VPC forwarding one HTTP request per invocation
# (backend, CLI, doctor and scripts call LiteLLM through it; lambda:InvokeFunction on it is the access control).
RELAY_FUNCTION_NAME = f"{PREFIX}-model-gateway-relay"
RELAY_TIMEOUT_SECONDS = 120

# Model aliases installed during bootstrap. Access varies by account and Region;
# configuration alone does not prove provider invocation access. Sonnet 5 remains the HR default.
# Keep these mappings/prices aligned with litellm/config.yaml.tpl (synthesis tests check parity).
MODELS = [
    {
        "alias": "claude-opus-5",
        "modelId": "us.anthropic.claude-opus-5",
        "provider": "bedrock",
        "tier": "premium",
        "description": "Claude Opus 5: strongest reasoning and tool use; for the hardest production agents",
        "inputCostPerToken": 5.5e-06,
        "outputCostPerToken": 2.75e-05,
    },
    {
        "alias": "claude-sonnet-5",
        "modelId": "us.anthropic.claude-sonnet-5",
        "provider": "bedrock",
        "tier": "standard",
        "description": "Claude Sonnet 5: strong reasoning and tool use at a lower price; the default for this workshop's agents",
        "inputCostPerToken": 2.2e-06,
        "outputCostPerToken": 1.1e-05,
    },
    {
        "alias": "gpt-6-1-sol",
        "modelId": "us.openai.gpt-6.1-sol",
        "provider": "bedrock",
        "tier": "standard",
        "description": "OpenAI GPT-6.1 Sol on Bedrock: a second vendor behind the same gateway, guardrail and caps",
        "inputCostPerToken": 2.2e-06,
        "outputCostPerToken": 1.1e-05,
    },
    {
        "alias": "nova-2-lite",
        "modelId": "us.amazon.nova-2-lite-v1:0",
        "provider": "bedrock",
        "tier": "economy",
        "description": "Amazon Nova 2 Lite: lowest cost; simple classification and extraction tasks",
        "inputCostPerToken": 3.3e-07,
        "outputCostPerToken": 2.75e-06,
    },
    {
        "alias": "claude-opus-4-8",
        "modelId": "us.anthropic.claude-opus-4-8",
        "provider": "bedrock",
        "tier": "premium",
        "description": "Claude Opus 4.8: verify provider access in your environment",
        "inputCostPerToken": 5.5e-06,
        "outputCostPerToken": 2.75e-05,
    },
    {
        "alias": "claude-fable-5",
        "modelId": "us.anthropic.claude-fable-5",
        "provider": "bedrock",
        "tier": "premium",
        "description": "Claude Fable 5: verify provider access in your environment",
        "inputCostPerToken": 1.1e-05,
        "outputCostPerToken": 5.5e-05,
    },
    {
        "alias": "claude-fable-5-1",
        "modelId": "us.anthropic.claude-fable-5-1",
        "provider": "bedrock",
        "tier": "premium",
        "description": "Claude Fable 5.1: verify provider access in your environment",
        "inputCostPerToken": 1.1e-05,
        "outputCostPerToken": 5.5e-05,
    },
    {
        "alias": "claude-opus-5-5",
        "modelId": "us.anthropic.claude-opus-5-5",
        "provider": "bedrock",
        "tier": "premium",
        "description": "Claude Opus 5.5: verify provider access in your environment",
        "inputCostPerToken": 4.4e-06,
        "outputCostPerToken": 2.2e-05,
    },
    {
        "alias": "claude-sonnet-5-5",
        "modelId": "us.anthropic.claude-sonnet-5-5",
        "provider": "bedrock",
        "tier": "standard",
        "description": "Claude Sonnet 5.5: verify provider access in your environment",
        "inputCostPerToken": 2.2e-06,
        "outputCostPerToken": 1.1e-05,
    },
    {
        "alias": "claude-haiku-5-5",
        "modelId": "us.anthropic.claude-haiku-5-5",
        "provider": "bedrock",
        "tier": "economy",
        "description": "Claude Haiku 5.5: verify provider access in your environment",
        "inputCostPerToken": 1.1e-07,
        "outputCostPerToken": 5.5e-07,
    },
]

# Identity - build contract section 2.1. The groups and the three persona users are created by `workshop apply 2` (module 2 of the
# learning path) from the canonical table in platform_sdk.auth (GROUP_DESCRIPTIONS, PERSONA_USERS); the Auth stack ships the user
# pool, the app client and the empty users secret the command fills.
USER_POOL_NAME = f"{PREFIX}-users"
UI_CLIENT_NAME = f"{PREFIX}-ui"
USERS_SECRET_NAME = f"{PREFIX}/users"
LOCAL_UI_URL = "http://localhost:5173/"

# Platform (API, table, Lambdas) - build contract sections 3 and 4
TABLE_NAME = PREFIX
# Module 6 release pipeline (names of the pipeline, stages and actions: platform_sdk.names)
RELEASE_SOURCE_BUCKET_PREFIX = f"{PREFIX}-release-source"
RELEASE_ARTIFACT_BUCKET_PREFIX = f"{PREFIX}-release-artifacts"
RELEASE_ARTIFACT_DAYS = 30  # old snapshot versions and stage artifacts; executions read theirs within the day
RELEASE_APPROVAL_DAYS = 7  # a manual approval left unanswered fails the execution (the release returns to DRAFT)
REQUIRED_TAGS = {
    "auto-delete": "no"
}  # every taggable resource (platform_sdk.tags.REQUIRED_TAGS for run-time resources)
API_NAME = f"{PREFIX}-api"
API_FUNCTION_NAME = f"{PREFIX}-api"
WORKER_FUNCTION_NAME = f"{PREFIX}-worker"
BACKEND_ROLE_NAME = f"{PREFIX}-backend-role"
EVALUATION_ROLE_NAME = f"{PREFIX}-evaluation-role"
PROJECT_ROLE_ARN_PATTERN = "arn:aws:iam::*:role/agent-platform-proj-*"
# Secrets Manager prefix of the secrets AgentCore Identity creates for API-key credential providers (token vault "default")
TOKEN_VAULT_SECRET_PREFIX = "bedrock-agentcore-identity!default"
# AgentCore Runtime creates this service-linked role on the first CreateHarness of an account and needs iam:CreateServiceLinkedRole
# for it (seen as "Failed creating service linked role ... 403" in a fresh account, 15 Oct 2026). The Platform stack creates the
# role up front (idempotently) and the backend role may create it too, as a second line of defence.
AGENTCORE_RUNTIME_SLR_SERVICE = "runtime-identity.bedrock-agentcore.amazonaws.com"
AGENTCORE_RUNTIME_SLR_NAME = "AWSServiceRoleForBedrockAgentCoreRuntimeIdentity"
# The first harness in VPC mode (ENIs in our subnets) creates a second service-linked role the same way.
AGENTCORE_NETWORK_SLR_SERVICE = "network.bedrock-agentcore.amazonaws.com"
AGENTCORE_NETWORK_SLR_NAME = "AWSServiceRoleForBedrockAgentCoreNetwork"

# Shared services
GUARDRAIL_NAME = "hr-baseline"
# Description of the numbered guardrail version. Bump it (v2 -> v3 ...) whenever the guardrail definition changes: CloudFormation
# only creates a GuardrailVersion when the resource is replaced, and the model gateway aliases pin that version number.
GUARDRAIL_VERSION_LABEL = "v2"
TRANSACTION_SEARCH_POLICY_NAME = "TransactionSearchXRayAccess"

# Sample scenario (Phase 4A content in scenario/, deployed by the Scenario stack) - build contract sections 1 and 8
SCENARIO_DIR = REPO_ROOT / "scenario"
SCENARIO_DATA_DIR = SCENARIO_DIR / "data"
SCENARIO_KNOWLEDGE_DIR = SCENARIO_DIR / "knowledge"
SCENARIO_EVALUATION_DIR = SCENARIO_DIR / "evaluation"
SCENARIO_BLUEPRINT_DIR = SCENARIO_DIR / "blueprint"
SCENARIO_HR_TOOLS_DIR = SCENARIO_DIR / "tools" / "hr"
SCENARIO_FINANCE_TOOLS_DIR = SCENARIO_DIR / "tools" / "finance"
SEED_HANDLER_DIR = FOUNDATION_DIR / "seed"
BUILD_SEED_DIR = REPO_ROOT / "build" / "scenario-seed"
SCENARIO_TABLES = {  # SSM key suffix -> table name; the JSON seed files are data/<key with - replaced by _>.json
    "employees": f"{PREFIX}-employees",
    "leave-requests": f"{PREFIX}-leave-requests",
    "invoices": f"{PREFIX}-invoices",
}
HR_TOOLS_FUNCTION_NAME = f"{PREFIX}-hr-tools"
FINANCE_TOOLS_FUNCTION_NAME = f"{PREFIX}-finance-tools"
KB_NAME = f"{PREFIX}-policies"
KB_DATA_SOURCE_NAME = "policy-docs"
KB_ROLE_NAME = f"{PREFIX}-kb-role"
DOCS_BUCKET_PREFIX = f"{PREFIX}-scenario-docs"  # + -<account>
DATASET_BUCKET_PREFIX = f"{PREFIX}-scenario-dataset"  # + -<account>

# Corporate IdP and the HR API behind it (module 2, on-behalf-of; design: .claude/docs/keycloak-obo-design.md). Keycloak plays
# the IdP a company runs or buys as SaaS: its own small public VPC outside the private landing zone, one Fargate task behind an
# internal load balancer, HTTPS from an API Gateway HTTP API with a VPC link (no domain or certificate to own). The realm and its
# clients are imported by Keycloak at every start from keycloak/realm.json (an init container writes it into the import volume);
# `workshop apply 2` only adds the demo employee through the admin REST API.
IDP_VPC_NAME = f"{PREFIX}-idp"
# Disjoint from the landing zone's 10.0.0.0/16; the two are never peered, the IdP is reached over the internet.
IDP_VPC_CIDR = "10.1.0.0/16"
IDP_CLUSTER_NAME = f"{PREFIX}-idp"
# The HTTPS door: its `https://<id>.execute-api.<region>.amazonaws.com` URL is Keycloak's KC_HOSTNAME and the host of every issuer.
IDP_API_NAME = f"{PREFIX}-idp"
KEYCLOAK_SERVICE_NAME = f"{PREFIX}-keycloak"
# Pulled straight from quay.io at task start: the public subnets have an internet path (no ECR copy, unlike LiteLLM).
KEYCLOAK_IMAGE = "quay.io/keycloak/keycloak:26.8.1"
# Fargate task size (1 vCPU / 2 GB). Keycloak `start-dev` needs about 85 s to listen on 1 vCPU (measured 2026-10-16); the
# service's five-minute health-check grace period in idp_stack.py is sized for that.
KEYCLOAK_CPU = 1024
KEYCLOAK_MEMORY = 2048
KEYCLOAK_PORT = 8080
# On the application port: the management port (9000, /health) is not behind the load balancer.
KEYCLOAK_HEALTH_PATH = "/realms/master"
KEYCLOAK_ADMIN_SECRET_NAME = f"{PREFIX}/keycloak-admin"
KEYCLOAK_ADMIN_USERNAME = "admin"
KEYCLOAK_REALM = "anycompany"
# The realm Keycloak imports at every start (generated by platform_sdk.keycloak.realm_json(); its test checks the file matches).
# An init container writes it into Keycloak's import directory, so no custom image is needed. The two confidential clients'
# secrets are `${KC_SECRET_<CLIENT>}` placeholders Keycloak resolves from the container environment at import time, fed from
# these Secrets Manager secrets (client id -> secret name; the env var is KC_SECRET_ + the client id upper-cased, - -> _).
KEYCLOAK_REALM_FILE = FOUNDATION_DIR / "keycloak" / "realm.json"
KEYCLOAK_CLIENT_SECRET_NAMES = {
    "hr-agent": f"{PREFIX}/keycloak-client-hr-agent",
    "hr-obo-gateway": f"{PREFIX}/keycloak-client-hr-obo-gateway",
}
KEYCLOAK_REALM_INIT_IMAGE = (
    "public.ecr.aws/docker/library/busybox:1.37"  # writes realm.json into the import volume (pinned tag)
)
HR_API_NAME = f"{PREFIX}-hr-api"
HR_API_FUNCTION_NAME = f"{PREFIX}-hr-api"
# The `aud` the HR API's JWT authorizer requires: only the last token of the on-behalf-of chain carries it.
HR_API_AUDIENCE = "hr-tools-api"
