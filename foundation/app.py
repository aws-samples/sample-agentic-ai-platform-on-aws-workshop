#!/usr/bin/env python3
"""CDK application for the workshop foundation.

Two independent chains (CDK derives the order from the cross-stack references), plus the independent Scenario and Idp stacks:
    landing zone:   Network -> SharedServices -> ModelGateway        (module 1 of the learning path: `workshop apply 1`)
    control plane:  Web -> Auth -> Platform                           (deployed before the day, with Scenario)
    corporate IdP:  Idp (Keycloak + the HR API behind it)             (module 2 of the learning path: `workshop apply 2`)
`npx cdk deploy --all` from this directory deploys everything at once: the backend bundle is assembled at synth time.
The Platform stack reads nothing from the ModelGateway stack: the backend finds the model gateway through SSM at run time, so
the control plane can exist before the model gateway does.
The landing zone is private (no internet gateway, no NAT; .claude/docs/private-landing-zone-design.md); its three availability
zones are the ones AgentCore Runtime supports for VPC mode, resolved by name per account at synth time (foundation/azs.py).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from aws_cdk import App, Environment, Stack, Tags

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from build_backend import ensure_bundle  # noqa: E402
from foundation.auth_stack import AuthStack  # noqa: E402
from foundation.azs import resolve_harness_azs  # noqa: E402
from foundation.idp_stack import IdpStack  # noqa: E402
from foundation.model_gateway_stack import ModelGatewayStack  # noqa: E402
from foundation.network_stack import NetworkStack  # noqa: E402
from foundation.platform_stack import PlatformStack  # noqa: E402
from foundation.scenario_stack import ScenarioStack  # noqa: E402
from foundation.shared_services_stack import SharedServicesStack  # noqa: E402
from foundation.web_stack import WebStack  # noqa: E402

from foundation import config  # noqa: E402


def build_app(context: dict | None = None) -> App:
    """The CDK app. `context` lets tests pass the cdk.json feature flags the CLI would otherwise supply."""
    ensure_bundle()  # build/backend = backend/src + platform_sdk + pure-Python deps

    app = App(context=context)
    env = Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION") or "us-east-1",
    )

    # Zone names for the AgentCore-supported zone ids: cached context, else one EC2 call, else the fallback (never fails).
    az_names = resolve_harness_azs(app, account=env.account, region=env.region)
    network = NetworkStack(app, config.STACK_NETWORK, az_names=az_names, env=env)
    shared = SharedServicesStack(app, config.STACK_SHARED, env=env)
    ModelGatewayStack(app, config.STACK_MODEL_GATEWAY, network=network, shared=shared, env=env)
    web = WebStack(app, config.STACK_WEB, env=env)
    auth = AuthStack(app, config.STACK_AUTH, ui_url=web.url, env=env)
    PlatformStack(app, config.STACK_PLATFORM, auth=auth, web=web, env=env)

    # Sample scenario (Phase 4A): independent of the other stacks, publishes /agent-platform/scenario/* itself.
    ScenarioStack(app, config.STACK_SCENARIO, env=env)

    # Module 2 (on-behalf-of): Keycloak as the corporate IdP in a public VPC of its own, plus the HR API behind a JWT
    # authorizer. References no other stack; its two zones are the first two of the names resolved above.
    IdpStack(app, config.STACK_IDP, az_names=az_names, env=env)

    # The ECS Express service carries the same two tags explicitly (its Tags property is create-only: a changed tag
    # list would replace the service), so the aspect skips that resource type.
    express = ["AWS::ECS::ExpressGatewayService"]
    Tags.of(app).add("Project", config.PROJECT_TAG, exclude_resource_types=express)
    Tags.of(app).add("ManagedBy", "cdk", exclude_resource_types=express)
    for child in app.node.children:
        if isinstance(child, Stack):
            child.termination_protection = False
    return app


if __name__ == "__main__":
    build_app().synth()
