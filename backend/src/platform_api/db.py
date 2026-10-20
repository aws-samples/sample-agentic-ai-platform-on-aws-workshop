"""DynamoDB single table `agent-platform` (build contract section 3): key builders, Decimal handling, ids and timestamps.

The table name comes from the environment variable TABLE_NAME (set by the Platform stack); the region from AWS_REGION (Lambda).
Items are stored with `float -> Decimal` and read back with `Decimal -> int | float`, so handlers never see Decimals.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from platform_sdk.config import default_region

from . import errors

KEY_ATTRS = ("PK", "SK", "GSI1PK", "GSI1SK")
GSI1 = "GSI1"
_table = None


def table():
    """The boto3 Table resource (created lazily so tests can point TABLE_NAME at a moto table)."""
    global _table
    if _table is None:
        name = os.environ.get("TABLE_NAME")
        if not name:
            raise RuntimeError("TABLE_NAME is not set (the Platform stack sets it on both Lambdas)")
        _table = boto3.resource("dynamodb", region_name=default_region()).Table(name)
    return _table


def reset() -> None:
    global _table
    _table = None


# ---- time and ids ----------------------------------------------------------------------------------------------------
def now_iso() -> str:
    """UTC timestamp like 2026-10-08T22:15:03Z (sorts lexically)."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_id() -> str:
    """26-character ULID (Crockford base32: 48-bit millisecond timestamp + 80 random bits), sortable by creation time."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_ULID_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))


# ---- key builders (contract section 3) ---------------------------------------------------------------------------------
def settings_key() -> dict:
    return {"PK": "SETTINGS", "SK": "PLATFORM"}


def domain_key(domain_id: str) -> dict:
    return {"PK": f"DOMAIN#{domain_id}", "SK": "META"}


def domain_gsi(name: str) -> dict:
    return {"GSI1PK": "DOMAIN", "GSI1SK": name}


def project_pk(project_id: str) -> str:
    return f"PROJECT#{project_id}"


def project_key(project_id: str) -> dict:
    return {"PK": project_pk(project_id), "SK": "META"}


def project_gsi(domain_id: str, project_id: str) -> dict:
    return {"GSI1PK": "PROJECT", "GSI1SK": f"{domain_id}#{project_id}"}


def resource_sk(env: str, type_: str, name: str) -> str:
    return f"RESOURCE#{env}#{type_}#{name}"


def resource_key(project_id: str, env: str, type_: str, name: str) -> dict:
    return {"PK": project_pk(project_id), "SK": resource_sk(env, type_, name)}


def resource_prefix(env: str | None = None) -> str:
    return f"RESOURCE#{env}#" if env else "RESOURCE#"


def blueprint_sk(version: int | str) -> str:
    return f"BLUEPRINT#{int(version):04d}"


def blueprint_key(project_id: str, version: int | str) -> dict:
    return {"PK": project_pk(project_id), "SK": blueprint_sk(version)}


def release_key(project_id: str, release_id: str) -> dict:
    return {"PK": project_pk(project_id), "SK": f"RELEASE#{release_id}"}


def release_gsi(created_at: str, release_id: str) -> dict:
    return {"GSI1PK": "RELEASE", "GSI1SK": f"{created_at}#{release_id}"}


def key_sk(env: str, model_alias: str) -> str:
    return f"KEY#{env}#{model_alias}"


def key_key(project_id: str, env: str, model_alias: str) -> dict:
    return {"PK": project_pk(project_id), "SK": key_sk(env, model_alias)}


def job_key(job_id: str) -> dict:
    return {"PK": f"JOB#{job_id}", "SK": "META"}


def job_gsi(created_at: str, job_id: str) -> dict:
    return {"GSI1PK": "JOB", "GSI1SK": f"{created_at}#{job_id}"}


def feedback_key(project_id: str, created_at: str, session_id: str, suffix: str) -> dict:
    """`suffix` (a ULID from `new_id()`) keeps two ratings of the same session within one second apart (contract section 3)."""
    return {"PK": f"FEEDBACK#{project_id}", "SK": f"{created_at}#{session_id}#{suffix}"}


def feedback_gsi(created_at: str, suffix: str) -> dict:
    return {"GSI1PK": "FEEDBACK", "GSI1SK": f"{created_at}#{suffix}"}


# ---- Decimal handling ----------------------------------------------------------------------------------------------------
def to_dynamo(value: Any) -> Any:
    """float -> Decimal (DynamoDB rejects floats), recursively."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_dynamo(v) for v in value]
    if isinstance(value, set):
        return [to_dynamo(v) for v in sorted(value)]
    return value


def from_dynamo(value: Any) -> Any:
    """Decimal -> int when integral, float otherwise, recursively."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo(v) for v in value]
    return value


def strip_keys(item: dict | None) -> dict | None:
    """The item without PK/SK/GSI attributes, Decimals converted (what the API returns)."""
    if item is None:
        return None
    return {k: v for k, v in from_dynamo(item).items() if k not in KEY_ATTRS}


# ---- access ---------------------------------------------------------------------------------------------------------------
def put(item: dict, condition=None) -> dict:
    """PutItem; `condition` is an optional boto3 `Attr(...)` expression. A failed check raises 409 CONFLICT (`errors.ApiError`),
    so handlers can catch the lost race without knowing botocore."""
    kwargs: dict[str, Any] = {"Item": to_dynamo(item)}
    if condition is not None:
        kwargs["ConditionExpression"] = condition
    try:
        table().put_item(**kwargs)
    except ClientError as exc:
        if (exc.response.get("Error") or {}).get("Code") != "ConditionalCheckFailedException":
            raise
        raise errors.conflict(
            f"{item.get('PK')}/{item.get('SK')} changed while the request was processed; re-read it and retry"
        ) from exc
    return item


def get(key: dict, consistent: bool = False) -> dict | None:
    """GetItem; `consistent=True` for readers that follow a write by milliseconds (the worker reading a job the API just wrote)."""
    item = table().get_item(Key=key, ConsistentRead=consistent).get("Item")
    return from_dynamo(item) if item is not None else None


def delete(key: dict) -> None:
    table().delete_item(Key=key)


def _query_all(**kwargs) -> list[dict]:
    items: list[dict] = []
    while True:
        page = table().query(**kwargs)
        items.extend(page.get("Items", []))
        token = page.get("LastEvaluatedKey")
        if not token:
            return from_dynamo(items)
        kwargs["ExclusiveStartKey"] = token


def query(pk: str, sk_prefix: str | None = None) -> list[dict]:
    """Every item of a partition, optionally only those whose SK starts with `sk_prefix` (SK order)."""
    condition = Key("PK").eq(pk)
    if sk_prefix:
        condition = condition & Key("SK").begins_with(sk_prefix)
    return _query_all(KeyConditionExpression=condition)


def query_gsi1(gsi1pk: str, sk_prefix: str | None = None, descending: bool = False) -> list[dict]:
    condition = Key("GSI1PK").eq(gsi1pk)
    if sk_prefix:
        condition = condition & Key("GSI1SK").begins_with(sk_prefix)
    return _query_all(IndexName=GSI1, KeyConditionExpression=condition, ScanIndexForward=not descending)


def delete_many(items: list[dict]) -> int:
    """Delete the given items (by PK/SK) in a batch writer; returns the count."""
    if not items:
        return 0
    with table().batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
    return len(items)
