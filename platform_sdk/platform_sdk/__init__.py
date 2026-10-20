"""Shared helpers for the workshop agentic AI platform.

Modules (see .claude/docs/build-contracts.md section 5):
    config     configuration discovery from SSM (/agent-platform/*)
    names      derived resource names for a project and environment
    auth       Cognito sign-in for the three personas
    api        platform API client
    blueprint  blueprint loading and rendering to a Harness specification
    agentcore  AgentCore helpers (wait, ensure, invoke)
    litellm    model gateway (LiteLLM) admin client; on the private landing zone it calls the proxy through the relay Lambda
    identity   AgentCore Identity credential providers (API keys; OAuth2 with on-behalf-of exchange) and workload identities
    keycloak   the corporate IdP of module 2: the realm file Keycloak imports, the employee through the admin API, logins, raw token exchange, JWT inspection
    hrapi      the OpenAPI document of the HR API (the on-behalf-of gateway target of module 4) generated from the tool schema
    mcp        MCP client for gateway URLs (SigV4, or a bearer JWT for CUSTOM_JWT gateways)
    evaluation dataset runs and threshold verdicts
    export     Strands Agents or LangGraph skeleton generated from a harness configuration (`workshop export`, module 6)
    metrics    CloudWatch metrics for harness endpoints
    state      small JSON state store of the `workshop` command (.workshop/state/<project>.json)
    traffic    traffic generator against a harness endpoint (module 7)
    templates  IAM role templates and the helpers that stamp project roles

Submodules are imported lazily (`from platform_sdk import evaluation` or `platform_sdk.evaluation`), so importing the package
inside the backend Lambda bundle pulls in nothing beyond boto3; `evaluation.run_dataset` imports `bedrock_agentcore` only when
called.
"""

from __future__ import annotations

import importlib

__version__ = "0.1.0"
__all__ = [
    "agentcore",
    "api",
    "auth",
    "blueprint",
    "config",
    "evaluation",
    "export",
    "hrapi",
    "identity",
    "keycloak",
    "litellm",
    "mcp",
    "metrics",
    "names",
    "state",
    "templates",
    "traffic",
]


def __getattr__(name: str):
    if name in __all__:
        module = importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
