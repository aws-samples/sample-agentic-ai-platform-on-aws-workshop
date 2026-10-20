"""Availability zones of the landing zone, resolved at synth time.

AgentCore Runtime in VPC mode only accepts subnets in a few availability-zone *ids* (config.HARNESS_AZ_IDS), and the zone *name*
that carries a given id differs per account (one account's us-east-1a is another account's use1-az6). CDK creates subnets by
name, so app.py asks EC2 which names carry the supported ids in the target account and hands them to the Network stack
(`ec2.Vpc(availability_zones=...)`, three zones). The answer is cached in cdk.context.json under
`agent-platform:harness-azs:<account>:<region>`, next to the CLI's own context lookups, so later synths and diffs make no call.
(The CLI may rewrite that file from its own in-memory copy after one of its lookups; the next synth then simply looks up and
caches again.)

The lookup never fails a synth: without credentials or network (unit tests, a laptop without a profile) the resolver warns on
stderr and falls back to config.FALLBACK_AZS. A wrong name only bites at the first CreateHarness, and the laptop that runs
`workshop apply 1` always has credentials, so the fallback is a test-time convenience, not a deployment path.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import boto3
from aws_cdk import App

from . import config

CONTEXT_FILE = (
    config.FOUNDATION_DIR / "cdk.context.json"
)  # the CLI's context cache, loaded into the App's context every synth


def context_key(account: str, region: str) -> str:
    return f"{config.HARNESS_AZS_CONTEXT_KEY}:{account}:{region}"


def describe_zone_names(region: str, zone_ids: list[str], client_factory: Callable[[], Any] | None = None) -> list[str]:
    """Zone names for `zone_ids`, in that order, from ec2:DescribeAvailabilityZones. Raises when any id is not available."""
    ec2 = client_factory() if client_factory else boto3.client("ec2", region_name=region)
    zones = ec2.describe_availability_zones(ZoneIds=list(zone_ids))["AvailabilityZones"]
    names = {zone["ZoneId"]: zone["ZoneName"] for zone in zones if zone.get("State") == "available"}
    missing = [zone_id for zone_id in zone_ids if zone_id not in names]
    if missing:
        raise LookupError(f"availability zone ids {missing} are not available in {region}")
    return [names[zone_id] for zone_id in zone_ids]


def resolve_harness_azs(
    app: App,
    account: str | None,
    region: str,
    *,
    client_factory: Callable[[], Any] | None = None,
    context_file: Path = CONTEXT_FILE,
) -> list[str]:
    """Zone names for config.HARNESS_AZ_IDS: the cached context entry, else a live lookup (cached for next time), else the
    fallback. `account` is None when the synth has neither CDK_DEFAULT_ACCOUNT nor credentials; nothing is cached then."""
    key = context_key(account, region) if account else None
    cached = _as_list(app.node.try_get_context(key)) if key else None
    if cached:
        return cached
    try:
        names = describe_zone_names(region, config.HARNESS_AZ_IDS, client_factory)
    except Exception as exc:  # no credentials, no network, unknown ids: the synth goes on with the fallback
        print(
            f"[azs] {type(exc).__name__}: {exc}; using {config.FALLBACK_AZS} "
            "(the harness subnets may not be in the AgentCore-supported zones)",
            file=sys.stderr,
        )
        return list(config.FALLBACK_AZS)
    if key:
        _cache(context_file, key, names)
    return names


def _as_list(value: Any) -> list[str] | None:
    """Context values arrive as a list (cdk.context.json) or as a comma-separated string (`cdk -c key=a,b,c`)."""
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",") if item.strip()]
    return list(value) if value else None


def _cache(context_file: Path, key: str, names: list[str]) -> None:
    """Merge the entry into cdk.context.json (the CLI's own lookups live there too). A write failure is no reason to fail."""
    try:
        existing = json.loads(context_file.read_text(encoding="utf-8")) if context_file.exists() else {}
        existing[key] = names
        context_file.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(f"[azs] could not cache the zone names in {context_file}: {exc}", file=sys.stderr)
