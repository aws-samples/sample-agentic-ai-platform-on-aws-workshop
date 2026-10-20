"""Custom resource: keep a public VPC reachable when the account has VPC Block Public Access switched on.

When InternetGatewayBlockMode is not "off", an allow-bidirectional exclusion must exist for the VPC, otherwise the Keycloak
task of the IdP stack (public subnets, no NAT) cannot pull its image from quay.io or read its secret, and nothing reaches
its load balancer. An existing exclusion is adopted; a missing one is created and deleted again on stack deletion
(ownership is encoded in the physical id).
"""

import time

import boto3

ec2 = boto3.client("ec2")
ACTIVE = ("create-in-progress", "create-complete", "update-in-progress", "update-complete")
DONE = ("create-complete", "update-complete")


def find(vpc_id):
    token = None
    while True:
        kwargs = {"MaxResults": 100}
        if token:
            kwargs["NextToken"] = token
        page = ec2.describe_vpc_block_public_access_exclusions(**kwargs)
        for ex in page.get("VpcBlockPublicAccessExclusions", []):
            if ex.get("ResourceArn", "").endswith("/" + vpc_id) and ex.get("State") in ACTIVE:
                return ex
        token = page.get("NextToken")
        if not token:
            return None


def handler(event, context):
    props = event["ResourceProperties"]
    vpc_id = props["VpcId"]
    physical = event.get("PhysicalResourceId") or ""
    if event["RequestType"] == "Delete":
        if physical.startswith("bpa:created:"):
            try:
                ec2.delete_vpc_block_public_access_exclusion(ExclusionId=physical.split(":", 2)[2])
            except ec2.exceptions.ClientError as exc:  # already gone
                if "NotFound" not in exc.response["Error"]["Code"]:
                    raise
        return {"PhysicalResourceId": physical or "bpa:none"}
    options = ec2.describe_vpc_block_public_access_options()["VpcBlockPublicAccessOptions"]
    mode = options.get("InternetGatewayBlockMode", "off")
    if mode == "off":
        return {"PhysicalResourceId": "bpa:off", "Data": {"Mode": mode, "ExclusionId": "", "Created": "false"}}
    exclusion = find(vpc_id)
    if exclusion:
        created = physical == "bpa:created:" + exclusion["ExclusionId"]
    else:
        exclusion = ec2.create_vpc_block_public_access_exclusion(
            VpcId=vpc_id,
            InternetGatewayExclusionMode="allow-bidirectional",
            TagSpecifications=[
                {
                    "ResourceType": "vpc-block-public-access-exclusion",
                    "Tags": [
                        {"Key": "Name", "Value": props.get("Name", "agent-platform")},
                        {"Key": "Project", "Value": props.get("Project", "workshop-agent-platform")},
                        {"Key": "ManagedBy", "Value": "cdk"},
                    ],
                }
            ],
        )["VpcBlockPublicAccessExclusion"]
        created = True
    deadline = time.time() + 240
    while exclusion.get("State") not in DONE and time.time() < deadline:
        time.sleep(10)
        exclusion = find(vpc_id) or exclusion
    prefix = "bpa:created:" if created else "bpa:adopted:"
    return {
        "PhysicalResourceId": prefix + exclusion["ExclusionId"],
        "Data": {
            "Mode": mode,
            "ExclusionId": exclusion["ExclusionId"],
            "State": exclusion.get("State", ""),
            "Created": "true" if created else "false",
        },
    }
