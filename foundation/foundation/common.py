"""Small helpers shared by the stacks."""

from __future__ import annotations

import re

from aws_cdk import CfnOutput, RemovalPolicy
from aws_cdk import aws_logs as logs
from aws_cdk import aws_ssm as ssm
from constructs import Construct

from . import config


def publish(scope: Construct, key: str, value: str, description: str = "") -> ssm.StringParameter:
    """Publish a value as SSM parameter `/agent-platform/<key>` and as a CloudFormation output.

    `key` is the suffix from the build contract (for example "api/url").
    """
    logical = "Param" + re.sub(r"[^A-Za-z0-9]", "", key.title())
    param = ssm.StringParameter(
        scope,
        logical,
        parameter_name=f"{config.SSM_PREFIX}{key}",
        string_value=value,
        description=description or f"agent-platform {key}",
    )
    CfnOutput(scope, "Out" + logical[5:], value=value, description=description or key)
    return param


def log_group(scope: Construct, id: str, name: str) -> logs.LogGroup:
    """A log group with the workshop retention (30 days) that is deleted with the stack."""
    return logs.LogGroup(
        scope,
        id,
        log_group_name=name,
        retention=logs.RetentionDays.ONE_MONTH,
        removal_policy=RemovalPolicy.DESTROY,
    )


def read_lambda_code(filename: str) -> str:
    """Inline Lambda source for the small custom resources (CloudFormation caps inline code at 4096 characters)."""
    code = (config.LAMBDAS_DIR / filename).read_text(encoding="utf-8")
    if len(code) > 4096:
        raise ValueError(f"{filename} is {len(code)} characters; inline Lambda code must stay under 4096")
    return code
