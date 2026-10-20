"""Small body/query validators that raise 400 VALIDATION_ERROR with a message naming the field."""

from __future__ import annotations

import re
from typing import Any

from platform_sdk import names

from . import errors

ID_RE = names.PROJECT_ID_RE  # kebab-case ids: domains and projects share the rule
BUDGET_DURATION_RE = re.compile(r"^\d+(s|m|h|d|mo)$")
_MISSING = object()


def require_object(body: Any) -> dict:
    if not isinstance(body, dict):
        raise errors.validation("request body must be a JSON object")
    return body


def string(body: dict, key: str, required: bool = True, default: Any = None, max_length: int = 2000) -> str | None:
    value = body.get(key, _MISSING)
    if value is _MISSING or value is None:
        if required:
            raise errors.validation(f"{key} is required")
        return default
    if not isinstance(value, str):
        raise errors.validation(f"{key} must be a string")
    value = value.strip()
    if required and not value:
        raise errors.validation(f"{key} must not be empty")
    if len(value) > max_length:
        raise errors.validation(f"{key} is longer than {max_length} characters")
    return value


def identifier(body: dict, key: str) -> str:
    value = string(body, key)
    if not ID_RE.match(value):
        raise errors.validation(
            f"{key} {value!r} must be kebab-case: start with a letter, 3-31 characters of [a-z0-9-] (example: hr-assistant)"
        )
    return value


def number(
    body: dict, key: str, required: bool = False, default: Any = None, minimum: float | None = None
) -> float | int | None:
    value = body.get(key, _MISSING)
    if value is _MISSING or value is None:
        if required:
            raise errors.validation(f"{key} is required")
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise errors.validation(f"{key} must be a number")
    if minimum is not None and value < minimum:
        raise errors.validation(f"{key} must be >= {minimum}")
    return value


def integer(
    body: dict, key: str, required: bool = False, default: Any = None, minimum: int | None = None
) -> int | None:
    value = number(body, key, required, default, minimum)
    if value is None:
        return None
    if isinstance(value, float):
        if not value.is_integer():
            raise errors.validation(f"{key} must be an integer")
        value = int(value)
    return value


def string_list(body: dict, key: str, required: bool = False, default: Any = None) -> list[str] | None:
    value = body.get(key, _MISSING)
    if value is _MISSING or value is None:
        if required:
            raise errors.validation(f"{key} is required")
        return list(default) if default is not None else None
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise errors.validation(f"{key} must be a list of non-empty strings")
    return [v.strip() for v in value]


def mapping(body: dict, key: str, required: bool = False, default: Any = None) -> dict | None:
    value = body.get(key, _MISSING)
    if value is _MISSING or value is None:
        if required:
            raise errors.validation(f"{key} is required")
        return dict(default) if default is not None else None
    if not isinstance(value, dict):
        raise errors.validation(f"{key} must be an object")
    return value


def choice(body: dict, key: str, allowed, required: bool = True, default: Any = None) -> str | None:
    value = string(body, key, required=required, default=default)
    if value is None:
        return None
    if value not in allowed:
        raise errors.validation(f"{key} must be one of {list(allowed)} (got {value!r})")
    return value


def env(value: str | None, required: bool = True) -> str | None:
    if value is None or value == "":
        if required:
            raise errors.validation("env is required (dev or prod)")
        return None
    if value not in names.ENVIRONMENTS:
        raise errors.validation(f"env must be one of {list(names.ENVIRONMENTS)} (got {value!r})")
    return value


def budget_duration(value: str | None, key: str = "budgetDuration") -> str | None:
    if value is None:
        return None
    if not BUDGET_DURATION_RE.match(value):
        raise errors.validation(
            f"{key} must look like 30s, 30m, 24h, 7d or 1mo (LiteLLM budget_duration), got {value!r}"
        )
    return value


def models(values: list[str], approved: list[str], key: str = "models") -> list[str]:
    if not values:
        raise errors.validation(f"{key} must list at least one approved model alias (approved: {approved})")
    unknown = [m for m in values if m not in approved]
    if unknown:
        raise errors.validation(f"{key} contains aliases that are not approved: {unknown}; approved: {approved}")
    return list(dict.fromkeys(values))
