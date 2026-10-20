"""`scripts/cleanup.py` bucket sweep: only orphaned foundation buckets (prefix + ManagedBy=cdk) are deleted.

The Workshop Studio assets bucket (`agent-platform-ws-assets-<account>`, ManagedBy=manual) and untagged buckets with the
prefix must survive a `cleanup.sh --yes`. Runs against moto, no credentials needed.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "cleanup.py"


@pytest.fixture(scope="module")
def cleanup():
    spec = importlib.util.spec_from_file_location("cleanup_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cleanup_script"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def aws_env(monkeypatch):
    for key in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SECURITY_TOKEN", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(key, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)


def _bucket(s3, name: str, tags: dict[str, str] | None = None, objects: int = 0) -> None:
    s3.create_bucket(Bucket=name)
    if tags:
        s3.put_bucket_tagging(Bucket=name, Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]})
    for i in range(objects):
        s3.put_object(Bucket=name, Key=f"k{i}", Body=b"x")


@mock_aws
def test_sweep_deletes_only_cdk_tagged_prefixed_buckets(cleanup, aws_env, capsys):
    s3 = boto3.client("s3", region_name="us-east-1")
    _bucket(s3, "agent-platform-scenario-docs-123456789012", {"ManagedBy": "cdk", "Project": "x"}, objects=3)
    _bucket(s3, "agentplatform-web-uibucket-abc", {"ManagedBy": "cdk"}, objects=1)
    _bucket(s3, "agent-platform-ws-assets-123456789012", {"ManagedBy": "manual", "Project": "x"}, objects=2)
    _bucket(s3, "agent-platform-untagged-leftover")
    _bucket(s3, "unrelated-bucket", {"ManagedBy": "cdk"})

    deleted = cleanup.sweep_buckets(boto3.Session(region_name="us-east-1"), apply=True)

    assert sorted(deleted) == ["agent-platform-scenario-docs-123456789012", "agentplatform-web-uibucket-abc"]
    remaining = sorted(b["Name"] for b in s3.list_buckets()["Buckets"])
    assert remaining == [
        "agent-platform-untagged-leftover",
        "agent-platform-ws-assets-123456789012",
        "unrelated-bucket",
    ]
    out = capsys.readouterr().out
    assert "keep   bucket agent-platform-ws-assets-123456789012 (ManagedBy=manual" in out
    assert "keep   bucket agent-platform-untagged-leftover (ManagedBy=untagged" in out


@mock_aws
def test_dry_run_deletes_nothing(cleanup, aws_env):
    s3 = boto3.client("s3", region_name="us-east-1")
    _bucket(s3, "agent-platform-scenario-docs-123456789012", {"ManagedBy": "cdk"}, objects=1)

    would = cleanup.sweep_buckets(boto3.Session(region_name="us-east-1"), apply=False)

    assert would == ["agent-platform-scenario-docs-123456789012"]
    assert [b["Name"] for b in s3.list_buckets()["Buckets"]] == ["agent-platform-scenario-docs-123456789012"]


def test_deployer_resources_are_not_leftovers(cleanup):
    assert cleanup.DEPLOYER_PREFIX == "agent-platform-deployer"
    assert cleanup.DEPLOYER_PREFIX.startswith(cleanup.PREFIX)
