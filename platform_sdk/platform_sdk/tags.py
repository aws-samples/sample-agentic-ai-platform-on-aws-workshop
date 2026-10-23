"""Tags every AWS resource of the platform carries, whoever creates it (the CDK app, the backend, the SDK or the `workshop` command).

`REQUIRED_TAGS` wins over any caller-supplied value: `auto-delete=no` marks the resources as owned by the workshop's own cleanup
(`workshop cleanup`, `cdk destroy`), never by an account-wide sweeper. The CDK app applies the same tags (`foundation/app.py`).
"""

from __future__ import annotations

PROJECT_TAG = "workshop-agent-platform"
AUTO_DELETE_KEY = "auto-delete"
REQUIRED_TAGS = {AUTO_DELETE_KEY: "no"}


def with_required(tags: dict | None = None) -> dict[str, str]:
    """`tags` (key -> value) plus the required tags, which cannot be overridden."""
    return {**{str(k): str(v) for k, v in (tags or {}).items()}, **REQUIRED_TAGS}


def as_list(tags: dict | None = None) -> list[dict[str, str]]:
    """The required tags merged into `tags`, as the `[{"Key": ..., "Value": ...}]` list of IAM, S3 and CloudFormation."""
    return [{"Key": k, "Value": v} for k, v in with_required(tags).items()]
