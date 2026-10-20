"""Error shape of the platform API and the mapping of upstream exceptions (build contract section 0).

Every error is `{"error": {"code": "<UPPER_SNAKE>", "message": "<human readable>"[, "details": {...}]}}` with the HTTP status.
The same mapping gives the `error` attribute of a FAILED job (`jobs.run`).
"""

from __future__ import annotations

import requests
from botocore.exceptions import ClientError
from platform_sdk.agentcore import AgentCoreError, HarnessInvokeError
from platform_sdk.auth import AuthError
from platform_sdk.blueprint import BlueprintError
from platform_sdk.litellm import LiteLLMError

NOT_FOUND_CODES = ("ResourceNotFoundException", "NoSuchEntity", "NotFoundException")
CONFLICT_CODES = ("ConditionalCheckFailedException",)


class ApiError(Exception):
    """An error with an HTTP status, an UPPER_SNAKE code, a message for humans and optional structured details."""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details

    def as_job_error(self) -> dict:
        """`{code, message[, details]}`: the `error` attribute of a FAILED job."""
        error: dict = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return error

    def body(self) -> dict:
        return {"error": self.as_job_error()}


def validation(message: str) -> ApiError:
    return ApiError(400, "VALIDATION_ERROR", message)


def unauthorized(message: str = "a Cognito ID token is required (Authorization: Bearer <idToken>)") -> ApiError:
    return ApiError(401, "UNAUTHORIZED", message)


def forbidden(message: str) -> ApiError:
    return ApiError(403, "FORBIDDEN", message)


def not_found(message: str) -> ApiError:
    return ApiError(404, "NOT_FOUND", message)


def conflict(message: str, code: str = "CONFLICT") -> ApiError:
    return ApiError(409, code, message)


def upstream(message: str, code: str = "UPSTREAM_ERROR") -> ApiError:
    return ApiError(502, code, message)


def from_exception(exc: BaseException) -> ApiError:
    """Map any exception raised while handling a request (or running a job) to an ApiError."""
    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, BlueprintError):
        # BLUEPRINT_INVALID (shape) or BLUEPRINT_UNRESOLVED (the message names the missing resource and the module that creates it)
        return ApiError(400, exc.code, exc.message, details=exc.details or None)
    if isinstance(exc, HarnessInvokeError):
        if exc.budget_exceeded:
            return ApiError(429, "BUDGET_EXCEEDED", exc.message)
        return upstream(exc.message, code="INVOKE_FAILED")
    if isinstance(exc, AuthError):
        # POST /me/switch: Cognito refused the persona's sign-in or the users secret is empty
        return upstream(str(exc), code="SIGN_IN_FAILED")
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {}) or {}
        code = error.get("Code") or "ClientError"
        message = error.get("Message") or str(exc)
        operation = getattr(exc, "operation_name", None) or "AWS call"
        if code in NOT_FOUND_CODES:
            return not_found(f"{operation}: {message}")
        if code in CONFLICT_CODES:  # a conditional write lost the race: the item changed since it was read
            return conflict(f"{operation}: the item changed while the request was processed; re-read it and retry")
        return upstream(f"{operation} failed: {code}: {message}")
    if isinstance(exc, LiteLLMError):
        return upstream(f"LiteLLM {exc.status}: {exc.message}")
    if isinstance(exc, requests.RequestException):  # the proxy hung (ReadTimeout) or is unreachable (ConnectionError)
        return upstream(f"model gateway unreachable: {type(exc).__name__}: {exc}")
    if isinstance(exc, AgentCoreError):
        return upstream(str(exc))
    if isinstance(exc, ValueError):
        return validation(str(exc))
    return ApiError(500, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")


def message_of(exc: BaseException) -> str:
    """Readable message of an upstream failure (used when a multi-step route reports which step failed)."""
    return from_exception(exc).message
