"""Platform settings: defaults merged with the SETTINGS item (build contract section 3).

Stored keys: registryId, registryArn, policyEngineId, policyEngineArn (module 4), defaultLimits, defaultProjectCap, defaultKeyCap.
Only overrides are stored; `load()` merges them over the defaults so every consumer sees every key. The nested defaults are
validated against `NESTED_SCHEMA` when written (400 naming the path) and sanitised when read (an invalid stored value falls
back to the default with a warning), so POST /projects, POST /keys and the renderer never forward an unchecked default.
"""

from __future__ import annotations

import copy
import logging

from boto3.dynamodb.conditions import Attr

from . import db, errors, validation

logger = logging.getLogger(__name__)
DEFAULTS: dict = {
    "registryId": None,
    "registryArn": None,
    "policyEngineId": None,
    "policyEngineArn": None,
    "defaultLimits": {"maxIterations": 30, "maxTokens": 4000, "timeoutSeconds": 120},
    "defaultProjectCap": {"maxBudget": 5, "budgetDuration": "7d", "tpmLimit": 200000, "rpmLimit": 200},
    "defaultKeyCap": {"maxBudget": 1, "budgetDuration": "7d"},
}
NESTED_KEYS = ("defaultLimits", "defaultProjectCap", "defaultKeyCap")
SCALAR_KEYS = ("registryId", "registryArn", "policyEngineId", "policyEngineArn")
ALLOWED_KEYS = SCALAR_KEYS + NESTED_KEYS
# value kinds: ("integer", minimum) | ("number", minimum) | ("duration", None) — the contract's schema for the nested defaults
NESTED_SCHEMA: dict[str, dict[str, tuple]] = {
    "defaultLimits": {"maxIterations": ("integer", 1), "maxTokens": ("integer", 1), "timeoutSeconds": ("integer", 1)},
    "defaultProjectCap": {
        "maxBudget": ("number", 0),
        "budgetDuration": ("duration", None),
        "tpmLimit": ("integer", 1),
        "rpmLimit": ("integer", 1),
    },
    "defaultKeyCap": {"maxBudget": ("number", 0), "budgetDuration": ("duration", None)},
}


def check_nested_value(path: str, kind: str, minimum, value) -> None:
    """Raise 400 VALIDATION_ERROR unless `value` fits the schema entry (`path` names the setting, e.g. defaultLimits.maxTokens)."""
    if kind == "duration":
        if not isinstance(value, str) or not validation.BUDGET_DURATION_RE.match(value):
            raise errors.validation(
                f"{path} must look like 30s, 30m, 24h, 7d or 1mo (LiteLLM budget_duration), got {value!r}"
            )
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise errors.validation(
            f"{path} must be a {'positive integer' if kind == 'integer' else 'number'}, got {value!r}"
        )
    if kind == "integer" and not float(value).is_integer():
        raise errors.validation(f"{path} must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise errors.validation(f"{path} must be >= {minimum}, got {value!r}")


def validate_nested(key: str, value: dict) -> dict:
    """The nested object of `key` (defaultLimits | defaultProjectCap | defaultKeyCap) checked against the contract schema."""
    schema = NESTED_SCHEMA[key]
    unknown = sorted(k for k in value if k not in schema)
    if unknown:
        raise errors.validation(f"unknown keys {unknown} in {key}; allowed: {list(schema)}")
    for inner, inner_value in value.items():
        if inner_value is None:
            continue  # null resets the inner key to its default
        kind, minimum = schema[inner]
        check_nested_value(f"{key}.{inner}", kind, minimum, inner_value)
    return value


def sanitise_nested(key: str, value) -> dict:
    """The stored override of `key` without the entries that fail the schema (logged); a non-object override is dropped."""
    if not isinstance(value, dict):
        logger.warning("settings.%s is not an object (%r): using the defaults", key, value)
        return {}
    clean: dict = {}
    for inner, inner_value in value.items():
        try:
            if inner not in NESTED_SCHEMA[key]:
                raise errors.validation(f"unknown key {key}.{inner}")
            kind, minimum = NESTED_SCHEMA[key][inner]
            check_nested_value(f"{key}.{inner}", kind, minimum, inner_value)
        except errors.ApiError as exc:
            logger.warning("ignoring stored setting %s.%s=%r: %s", key, inner, inner_value, exc.message)
            continue
        clean[inner] = inner_value
    return clean


def deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def stored() -> dict:
    """The SETTINGS item as stored (overrides only), without key attributes."""
    return db.strip_keys(db.get(db.settings_key())) or {}


def load() -> dict:
    """Defaults merged with the (sanitised) stored overrides, plus updatedAt / updatedBy when the item exists."""
    item = stored()
    for key in NESTED_KEYS:
        if key in item:
            item[key] = sanitise_nested(key, item[key])
    merged = deep_merge(copy.deepcopy(DEFAULTS), item)
    merged.setdefault("updatedAt", None)
    merged.setdefault("updatedBy", None)
    merged.pop("revision", None)  # the optimistic-concurrency token of `update`, not a setting
    return merged


def update(fields: dict, by: str) -> dict:
    """PUT /settings semantics: nested keys deep-merge (values validated against the contract schema, an inner `null` resets
    that key), scalars replace, a top-level `null` removes the key (reset). Returns `load()`.

    The write is conditional on the `revision` that was read (a ULID per write; `updatedAt` only has one-second resolution), so
    two concurrent PUTs cannot silently lose one of them: the second answers 409 CONFLICT."""
    if not isinstance(fields, dict):
        raise errors.validation("settings body must be a JSON object")
    unknown = sorted(k for k in fields if k not in ALLOWED_KEYS)
    if unknown:
        raise errors.validation(f"unknown settings keys {unknown}; allowed: {list(ALLOWED_KEYS)}")
    current = stored()
    previous_revision = current.get("revision")
    for key, value in fields.items():
        if value is None:
            current.pop(key, None)
        elif key in NESTED_KEYS:
            if not isinstance(value, dict):
                raise errors.validation(f"{key} must be an object")
            validate_nested(key, value)
            merged = deep_merge(current.get(key, {}), {k: v for k, v in value.items() if v is not None})
            for inner, inner_value in value.items():
                if inner_value is None:
                    merged.pop(inner, None)
            current[key] = merged
        else:
            if not isinstance(value, str) or not value.strip():
                raise errors.validation(f"{key} must be a non-empty string (or null to reset it)")
            current[key] = value.strip()
    current["updatedAt"] = db.now_iso()
    current["updatedBy"] = by
    current["revision"] = db.new_id()
    condition = Attr("revision").eq(previous_revision) if previous_revision else Attr("revision").not_exists()
    try:
        db.put({**db.settings_key(), **current}, condition=condition)
    except errors.ApiError as exc:
        if exc.code != "CONFLICT":
            raise
        raise errors.conflict("the settings changed while this request was processed: re-read them and retry") from exc
    return load()


def registry_id() -> str:
    """The shared Agent Registry id, or 409 REGISTRY_NOT_CONFIGURED (module 4 creates `agent_platform_catalogue` and stores it)."""
    value = load().get("registryId")
    if not value:
        raise errors.conflict(
            "no Agent Registry is configured: create the registry agent_platform_catalogue (module 4) and store it with "
            'PUT /settings {"registryId": "<id>", "registryArn": "<arn>"}',
            code="REGISTRY_NOT_CONFIGURED",
        )
    return value
