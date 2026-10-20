#!/usr/bin/env python3
"""Sweep what `cdk destroy --all` leaves behind, so the same account can be redeployed from scratch.

Only names with the `agent-platform` / `AgentPlatform-` prefixes are touched. Secrets scheduled for deletion are force-deleted
(CloudFormation leaves them in a recovery window, and a redeploy cannot create a secret with the same name while it is
pending deletion). Log groups and buckets are only swept when no `AgentPlatform-*` stack exists any more, and a bucket only
when it carries the foundation's `ManagedBy=cdk` tag (a Workshop Studio assets bucket such as `agent-platform-ws-assets-<account>`,
tagged `ManagedBy=manual`, is kept and reported). Everything else (ECS, RDS, Cognito, knowledge bases, tables, roles) is
reported, not deleted; the resources of the Workshop Studio deployer stack (`agent-platform-deployer-*`) are left out of the
report because that stack is deleted by its owner, not by this sweep. Dry run unless `--yes` is given.

Usage: `uv run python scripts/cleanup.py [--yes]` with AWS credentials in the environment (e.g. AWS_PROFILE=workshop).
"""

from __future__ import annotations

import argparse
import sys

import boto3
from botocore.exceptions import ClientError

PREFIX = "agent-platform"
STACK_PREFIX = "AgentPlatform-"
LOG_GROUP_PREFIXES = (
    f"/aws/lambda/{STACK_PREFIX}",  # auto-created log groups of CDK provider Lambdas (S3 auto-delete, bucket deployment)
    f"/aws/lambda/{PREFIX}-",
    f"/{PREFIX}/",
    f"/aws/vendedlogs/{PREFIX}-",
    STACK_PREFIX,  # unnamed (generated-name) log groups of stack Lambdas, recreated by late log delivery after the stack is gone
)
AGENTCORE_LOG_GROUP_PREFIX = "/aws/bedrock-agentcore/"  # harness runtimes and evaluation results: created by the modules, reported only
BUCKET_PREFIXES = (f"{PREFIX}-", "agentplatform-")
BUCKET_TAG = ("ManagedBy", "cdk")  # every foundation bucket carries it (Tags aspect in foundation/app.py)
DEPLOYER_PREFIX = (
    f"{PREFIX}-deployer"  # workshop-studio/deployer.yaml stack: its role, Lambda and project are not leftovers
)


def live_stacks(cfn) -> list[str]:
    names = []
    for page in cfn.get_paginator("list_stacks").paginate():
        for s in page["StackSummaries"]:
            if s["StackName"].startswith(STACK_PREFIX) and not s["StackStatus"].startswith("DELETE_COMPLETE"):
                names.append(f"{s['StackName']} ({s['StackStatus']})")
    return names


def sweep_secrets(session, apply: bool, stacks_gone: bool) -> None:
    sm = session.client("secretsmanager")
    for page in sm.get_paginator("list_secrets").paginate(IncludePlannedDeletion=True):
        for s in page["SecretList"]:
            if not s["Name"].startswith(f"{PREFIX}/"):
                continue
            scheduled = "DeletedDate" in s
            if not scheduled and not stacks_gone:
                print(f"  keep   secret {s['Name']} (in use by a stack)")
                continue
            why = "scheduled for deletion" if scheduled else "orphaned"
            print(f"  {'DELETE' if apply else 'would delete'} secret {s['Name']} ({why})")
            if apply:
                sm.delete_secret(SecretId=s["ARN"], ForceDeleteWithoutRecovery=True)


def sweep_log_groups(session, apply: bool) -> None:
    logs = session.client("logs")
    for prefix in LOG_GROUP_PREFIXES:
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
            for g in page["logGroups"]:
                print(f"  {'DELETE' if apply else 'would delete'} log group {g['logGroupName']}")
                if apply:
                    logs.delete_log_group(logGroupName=g["logGroupName"])


def bucket_managed_by(s3_client, name: str) -> str | None:
    """Value of the bucket's ManagedBy tag (None when untagged or gone)."""
    try:
        tags = s3_client.get_bucket_tagging(Bucket=name)["TagSet"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchTagSet", "NoSuchBucket"):
            return None
        raise
    return {t["Key"]: t["Value"] for t in tags}.get(BUCKET_TAG[0])


def sweep_buckets(session, apply: bool) -> list[str]:
    """Delete the orphaned foundation buckets (prefix + ManagedBy=cdk tag); keep and report every other prefixed bucket."""
    s3 = session.resource("s3")
    client = session.client("s3")
    deleted = []
    for bucket in s3.buckets.all():
        if not bucket.name.startswith(BUCKET_PREFIXES):
            continue
        managed_by = bucket_managed_by(client, bucket.name)
        if managed_by != BUCKET_TAG[1]:
            print(
                f"  keep   bucket {bucket.name} (ManagedBy={managed_by or 'untagged'}: not created by the foundation stacks)"
            )
            continue
        print(f"  {'DELETE' if apply else 'would delete'} bucket {bucket.name}")
        deleted.append(bucket.name)
        if apply:
            bucket.object_versions.delete()
            bucket.objects.all().delete()
            bucket.delete()
    return deleted


def report_leftovers(session) -> None:
    """Print resources with the platform prefix that still exist (deleted by their stacks normally)."""
    found = []

    def probe(label, fn):
        try:
            items = fn()
        except ClientError as exc:
            items = [f"? {exc.response['Error']['Code']}"]
        for item in items:
            found.append(f"{label}: {item}")

    ecs = session.client("ecs")
    probe("ecs cluster", lambda: [a for a in ecs.list_clusters()["clusterArns"] if f"/{PREFIX}" in a])
    rds = session.client("rds")
    probe(
        "rds instance",
        lambda: [
            d["DBInstanceIdentifier"]
            for d in rds.describe_db_instances()["DBInstances"]
            if d["DBInstanceIdentifier"].startswith("agentplatform")
        ],
    )
    idp = session.client("cognito-idp")
    probe(
        "cognito pool",
        lambda: [p["Name"] for p in idp.list_user_pools(MaxResults=60)["UserPools"] if p["Name"].startswith(PREFIX)],
    )
    agent = session.client("bedrock-agent")
    probe(
        "knowledge base",
        lambda: [
            f"{k['name']} {k['knowledgeBaseId']}"
            for k in agent.list_knowledge_bases()["knowledgeBaseSummaries"]
            if k["name"].startswith(PREFIX)
        ],
    )
    ddb = session.client("dynamodb")
    probe("dynamodb table", lambda: [t for t in ddb.list_tables()["TableNames"] if t.startswith(PREFIX)])
    iam = session.client("iam")
    probe(
        "iam role",
        lambda: [
            r["RoleName"]
            for page in iam.get_paginator("list_roles").paginate()
            for r in page["Roles"]
            if r["RoleName"].startswith(PREFIX) and not r["RoleName"].startswith(DEPLOYER_PREFIX)
        ],
    )
    lam = session.client("lambda")
    probe(
        "lambda",
        lambda: [
            f["FunctionName"]
            for page in lam.get_paginator("list_functions").paginate()
            for f in page["Functions"]
            if f["FunctionName"].startswith((PREFIX, STACK_PREFIX))
            and not f["FunctionName"].startswith(DEPLOYER_PREFIX)
        ],
    )
    ssm = session.client("ssm")
    probe(
        "ssm parameter",
        lambda: [
            p["Name"]
            for page in ssm.get_paginator("get_parameters_by_path").paginate(Path=f"/{PREFIX}/", Recursive=True)
            for p in page["Parameters"]
        ],
    )
    logs = session.client("logs")
    probe(
        "AgentCore log group (harness or evaluation; `workshop cleanup` / lifecycle_test --only-cleanup remove them)",
        lambda: [
            g["logGroupName"]
            for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=AGENTCORE_LOG_GROUP_PREFIX)
            for g in page["logGroups"]
        ],
    )
    xray = session.client("xray")
    probe(
        "x-ray destination (account setting, kept on purpose)",
        lambda: [xray.get_trace_segment_destination()["Destination"]],
    )
    print("Leftovers:" if found else "Leftovers: none beyond the account-level settings")
    for line in found:
        print("  " + line)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="apply the deletions (default: dry run)")
    args = parser.parse_args()
    session = boto3.Session()
    stacks = live_stacks(session.client("cloudformation"))
    stacks_gone = not stacks
    print(f"AgentPlatform stacks present: {', '.join(stacks) if stacks else 'none'}")
    print("Secrets:")
    sweep_secrets(session, args.yes, stacks_gone)
    if stacks_gone:
        print("Log groups:")
        sweep_log_groups(session, args.yes)
        print("Buckets:")
        sweep_buckets(session, args.yes)
    else:
        print("Log groups and buckets: skipped while stacks exist (destroy them first)")
    report_leftovers(session)
    if not args.yes:
        print("Dry run. Re-run with --yes to apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
