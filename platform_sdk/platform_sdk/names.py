"""Derived names for a project and environment (build contract section 2.2).

Both the backend and the `workshop` command use these so that a resource created in a module is the one the platform expects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ENVIRONMENTS = ("dev", "prod")
PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,30}$")
ROLE_PREFIX = "agent-platform-proj"
POLICY_ENGINE_NAME = "agent_platform_policy_engine"
REGISTRY_NAME = "agent_platform_catalogue"
GATEWAY_TOOL_NAME = "gateway"  # the single harness tool entry that points at the environment's gateway
# Module 2 (on-behalf-of with Keycloak): the OBO objects sit next to the Dev ones and carry this suffix; the Keycloak realm and
# clients are fixed names (one corporate IdP per account), as are the two AgentCore Identity OAuth2 providers and the scratch
# workload identity the demo uses for the exchange by hand.
OBO_SUFFIX = "obo"
KC_REALM = "anycompany"
KC_CLIENT_PORTAL = "hr-portal"  # the employee-facing app: public client, Direct Access Grants (the demo's login)
KC_CLIENT_AGENT = "hr-agent"  # the harness: exchanges the user token for a gateway token
KC_CLIENT_GATEWAY = "hr-obo-gateway"  # the gateway: exchanges the gateway token for an HR API token
KC_CLIENT_HR_API = "hr-tools-api"  # the HR API: an audience only
KC_EMPLOYEE_CLAIM = "employee_id"
OBO_PROVIDER_AGENT = "kc-hr-agent"
OBO_PROVIDER_GATEWAY = "kc-hr-obo-gateway"
OBO_DEMO_WORKLOAD = "workshop_obo_demo"
OBO_ROLE_POLICY = "obo-token-exchange"  # inline policy name on the Dev agent and gateway roles
POLICY_NAME_MAX = 48  # CreatePolicy `name`: 1-48 characters matching [A-Za-z][A-Za-z0-9_]* (botocore model, 8 Oct 2026)
POLICY_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def validate_project_id(project_id: str) -> str:
    if not PROJECT_ID_RE.match(project_id or ""):
        raise ValueError(
            f"project id {project_id!r} must be kebab-case: start with a letter, 3-31 chars of [a-z0-9-] (example: hr-assistant)"
        )
    return project_id


def validate_env(env: str) -> str:
    if env not in ENVIRONMENTS:
        raise ValueError(f"environment {env!r} must be one of {ENVIRONMENTS}")
    return env


def snake(project_id: str) -> str:
    return project_id.replace("-", "_")


@dataclass(frozen=True)
class ProjectNames:
    """Names of everything that belongs to one project in one environment."""

    project_id: str
    env: str

    def __post_init__(self) -> None:
        validate_project_id(self.project_id)
        validate_env(self.env)

    # AgentCore resources (created in the modules)
    @property
    def gateway(self) -> str:
        return f"{self.project_id}-{self.env}-gw"

    @property
    def memory(self) -> str:
        return f"{snake(self.project_id)}_{self.env}_memory"

    @property
    def harness(self) -> str:
        return f"{snake(self.project_id)}_{self.env}"

    @property
    def obo_harness(self) -> str:
        """Module 2: the JWT-protected copy of the Dev harness (`hr_assistant_dev_obo`)."""
        return f"{self.harness}_{OBO_SUFFIX}"

    @property
    def obo_gateway(self) -> str:
        """Module 2: the JWT-protected gateway with the HR API target (`hr-assistant-dev-gw-obo`, inside the roles' `-gw*` patterns)."""
        return f"{self.gateway}-{OBO_SUFFIX}"

    @property
    def harness_endpoint(self) -> str:
        """Dev is reached through the DEFAULT endpoint (latest version); Prod through `live` pinned to the approved version."""
        return "DEFAULT" if self.env == "dev" else "live"

    @property
    def harness_service_name(self) -> str:
        """Service name of the harness endpoint in CloudWatch spans (`harness_<name>.<endpoint>`), used by online evaluation."""
        return harness_service_name(self.harness, self.harness_endpoint)

    @property
    def harness_metric_name(self) -> str:
        """`Name` dimension of the AWS/Bedrock-AgentCore built-in metrics for this harness endpoint."""
        return harness_metric_name(self.harness, self.harness_endpoint)

    @property
    def online_evaluation(self) -> str:
        return f"{snake(self.project_id)}_{self.env}_online_eval"

    def policy(self, rule: str) -> str:
        """Cedar policy name `<snake>_<env>_<rule>`; raises ValueError when it would exceed the 48-character API limit."""
        name = f"{snake(self.project_id)}_{self.env}_{rule}"
        if len(name) > POLICY_NAME_MAX or not POLICY_NAME_RE.match(name):
            raise ValueError(
                f"policy name {name!r} is {len(name)} characters; Cedar policy names are limited to {POLICY_NAME_MAX} "
                "characters of [A-Za-z0-9_] starting with a letter (CreatePolicy). Shorten the rule or the project id."
            )
        return name

    # IAM roles (stamped by the platform when the project is created)
    @property
    def agent_role(self) -> str:
        return f"{ROLE_PREFIX}-{self.project_id}-{self.env}-agent"

    @property
    def gateway_role(self) -> str:
        return f"{ROLE_PREFIX}-{self.project_id}-{self.env}-gateway"

    def agent_role_arn(self, account_id: str) -> str:
        return f"arn:aws:iam::{account_id}:role/{self.agent_role}"

    def gateway_role_arn(self, account_id: str) -> str:
        return f"arn:aws:iam::{account_id}:role/{self.gateway_role}"

    def cedar_principal(self, account_id: str) -> str:
        """The Cedar entity the policy engine sees when the harness calls the gateway with IAM auth."""
        return f'AgentCore::IamEntity::"arn:aws:sts::{account_id}:assumed-role/{self.agent_role}"'

    # Model gateway (LiteLLM) and AgentCore Identity
    @property
    def team(self) -> str:
        return self.project_id

    def key_alias(self, model_alias: str) -> str:
        return f"{self.project_id}-{self.env}-{model_alias}"

    def credential_provider(self, model_alias: str) -> str:
        return f"litellm-{self.project_id}-{self.env}-{model_alias}"

    # Catalogue
    @property
    def registry_record_tools(self) -> str:
        return f"{self.project_id}-tools"

    @property
    def registry_record_agent(self) -> str:
        """The AGENT record the promote job writes for the project's Prod agent (one per project, whatever the environment)."""
        return f"{self.project_id}-agent"


def for_project(project_id: str) -> dict[str, ProjectNames]:
    """Names for both environments of a project, keyed by environment."""
    return {env: ProjectNames(project_id, env) for env in ENVIRONMENTS}


# Observability names (verified in spike 3, 7 Oct 2026): the log group uses the harness's underlying agent runtime id
# (`GetHarness -> environment.agentCoreRuntimeEnvironment.agentRuntimeId`, looks like `harness_<name>-<suffix>`), not the harness id.
def harness_log_group(agent_runtime_id: str, endpoint: str) -> str:
    """CloudWatch log group of one harness endpoint: `/aws/bedrock-agentcore/runtimes/<agentRuntimeId>-<endpoint>`."""
    return f"/aws/bedrock-agentcore/runtimes/{agent_runtime_id}-{endpoint}"


def harness_service_name(harness_name: str, endpoint: str) -> str:
    """`service.name` seen in spans and online evaluation configs: `harness_<name>.<endpoint>`."""
    return f"harness_{harness_name}.{endpoint}"


def harness_metric_name(harness_name: str, endpoint: str) -> str:
    """`Name` dimension of the built-in AWS/Bedrock-AgentCore metrics: `harness_<name>::<endpoint>`."""
    return f"harness_{harness_name}::{endpoint}"
