"""Admin client for the model gateway (LiteLLM proxy, open-source edition) — build contract section 7.

Teams map to projects (`team_id = team_alias = project id`), service-account keys to one agent, environment and model.
Spend and budgets come from `/team/info`, `/key/info` and `/spend/logs` (verified 8 Oct 2026 on v1.104.1). Plain `requests`,
no openai dependency: `chat_completion` exists for smoke tests only.

    admin = litellm.LiteLLMAdmin(cfg.model_gateway_url, master_key)
    admin.create_team("hr-assistant", "hr-assistant", ["claude-sonnet-5-5"], 5.0, "1d", 200000, 200, {"domain": "people-ops"})
    key = admin.create_service_account_key("hr-assistant", "hr-assistant-dev-claude-sonnet-5-5", ["claude-sonnet-5-5"], 1.0, "1d", {...})
    key["key"]    # raw sk-... key: store it in AgentCore Identity and forget it
    key["token"]  # hashed token id: keep it in DynamoDB for /key/info, /key/update, /key/block, /spend/logs?api_key=

Private landing zone (tier 3, decided 16 Oct 2026): the proxy's load balancer is internal, so nothing outside the VPC can open a
TCP connection to it. `admin_client(cfg, master_key)` therefore returns the same `LiteLLMAdmin` on top of a `LambdaRelaySession`
when SSM publishes `model-gateway/relay-function-name`: every call becomes one `lambda:InvokeFunction` of the relay Lambda inside
the VPC, which forwards the HTTP request unchanged and returns the whole response. Without that parameter (a public landing zone)
the client talks `requests` to the URL as before. `LocalRelayProxy` serves the same relay on 127.0.0.1 for code that only speaks
HTTP (the exported agents under `workshop export --run`).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import boto3
import requests
from botocore.exceptions import BotoCoreError, ClientError

from . import config

json_module = json  # the Session API's `json=` keyword shadows the module inside request()

DEFAULT_TIMEOUT = 30
# The relay may wait RELAY_MAX_TIMEOUT s for the proxy: below its own Lambda timeout (120 s), so it answers instead of dying;
# boto3 waits RELAY_READ_TIMEOUT s for the Invoke: above that Lambda timeout, so a slow completion is not cut off client-side.
RELAY_MAX_TIMEOUT = 110
RELAY_READ_TIMEOUT = 125
BUDGET_EXCEEDED = "budget_exceeded"
# LiteLLM's own wording for a cap hit ("Budget has been exceeded! Key=... Current cost: X, Max budget: Y", error type
# budget_exceeded, exception class BudgetExceededError); anything else that mentions a budget is not a cap hit.
BUDGET_PATTERNS = (
    "budget has been exceeded",
    "budget_exceeded",
    "budgetexceedederror",
    "exceededbudget",
    "exceeded budget",
)


class LiteLLMError(Exception):
    """The proxy answered with an error. `type` is LiteLLM's error type when given (for example `budget_exceeded`)."""

    def __init__(self, status: int, message: str, body: Any = None, type: str | None = None):  # noqa: A002
        super().__init__(f"LiteLLM {status}: {message}")
        self.status = status
        self.message = message
        self.body = body
        self.type = type

    @property
    def budget_exceeded(self) -> bool:
        return self.type == BUDGET_EXCEEDED


def parse_error(status: int, body: Any, text: str = "") -> LiteLLMError:
    """Map LiteLLM's error bodies ({"error": {"message", "type"}} or {"detail": ...}) to a LiteLLMError."""
    message: str = ""
    error_type: str | None = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "")
            error_type = error.get("type")
        elif isinstance(error, str):
            message = error
        detail = body.get("detail")
        if not message and detail is not None:
            if isinstance(detail, dict):
                message = str(detail.get("error") or detail.get("message") or json.dumps(detail))
                error_type = error_type or detail.get("type")
            else:
                message = str(detail)
        message = message or body.get("message") or json.dumps(body)
    else:
        message = text or str(body)
    lowered = message.lower()
    if error_type is None and any(p in lowered for p in BUDGET_PATTERNS):
        error_type = BUDGET_EXCEEDED
    return LiteLLMError(status, message, body, error_type)


# ---- transport: the relay Lambda for the private proxy -----------------------------------------------------------------
class RelayError(OSError):
    """The relay could not be invoked or failed before the proxy answered (IAM, a Lambda error, a timeout).

    An OSError like `requests.RequestException`, so callers that fall back when the proxy is unreachable (the backend's
    governance screen, the cleanup) treat both transports the same way.
    """


@dataclass
class RelayResponse:
    """What the relay returned, with the `requests.Response` surface `LiteLLMAdmin` reads. The whole body is buffered."""

    status_code: int
    headers: dict[str, str]
    text: str

    @property
    def content(self) -> bytes:
        return self.text.encode()

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self) -> Any:
        return json_module.loads(self.text)  # ValueError on a non-JSON body, like requests

    def iter_lines(self, decode_unicode: bool = True, chunk_size: int | None = None, delimiter=None):
        """The buffered body line by line: the relay cannot stream, so an SSE body arrives whole and is split here."""
        for line in self.text.splitlines():
            yield line if decode_unicode else line.encode()


class LambdaRelaySession:
    """A `requests.Session` look-alike that carries every HTTP call through the relay Lambda inside the VPC.

    The proxy's load balancer is internal (tier 3 landing zone), so laptops and the backend Lambdas cannot reach it. The relay
    is a plain HTTP pipe: it only ever calls the proxy's URL and adds no credentials, so the Authorization header given here is
    the one the proxy sees, and `lambda:InvokeFunction` on the relay is the access control for the admin API. Event and response
    shapes are the contract in private-landing-zone-design.md section 3:

        {"method": "GET", "path": "/key/info", "query": {"key": "..."} | null, "headers": {...} | null, "body": "..." | null,
         "timeout": 30}  ->  {"status": 200, "headers": {lower-case name: value}, "body": "<text>"}
    """

    def __init__(self, function_name: str, lambda_client, max_timeout: int = RELAY_MAX_TIMEOUT):
        self.function_name = function_name
        self.lambda_client = lambda_client
        self.max_timeout = max_timeout
        self.calls = 0

    def request(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        json: Any = None,  # noqa: A002 - the requests keyword
        headers: dict | None = None,
        timeout: float | None = None,
        stream: bool = False,
        data: bytes | str | None = None,
    ) -> RelayResponse:
        """`requests.Session.request` for the relay: `url` may be absolute (base URL + path) or just a path; `stream` is accepted
        and ignored because the relay buffers."""
        event = self.event(method, url, params=params, json=json, headers=headers, timeout=timeout, data=data)
        return self.invoke(event)

    def event(
        self,
        method: str,
        url: str,
        params: dict | None = None,
        json: Any = None,  # noqa: A002
        headers: dict | None = None,
        timeout: float | None = None,
        data: bytes | str | None = None,
    ) -> dict:
        """The relay event for one HTTP call: the URL is reduced to its path, query parameters become strings."""
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        if params:
            query.update({k: str(v) for k, v in params.items() if v is not None})
        sent_headers = {str(k): str(v) for k, v in (headers or {}).items()}
        body: str | None = None
        if json is not None:
            body = json_module.dumps(json)
            if not any(k.lower() == "content-type" for k in sent_headers):
                sent_headers["Content-Type"] = "application/json"
        elif data is not None:
            body = data.decode() if isinstance(data, bytes) else str(data)
        return {
            "method": method.upper(),
            "path": parts.path or "/",
            "query": query or None,
            "headers": sent_headers or None,
            "body": body,
            "timeout": int(min(timeout or DEFAULT_TIMEOUT, self.max_timeout)),
        }

    def invoke(self, event: dict) -> RelayResponse:
        """One synchronous Invoke; Lambda errors and SDK failures surface as RelayError with the relay's name."""
        self.calls += 1
        try:
            response = self.lambda_client.invoke(
                FunctionName=self.function_name,
                InvocationType="RequestResponse",
                Payload=json_module.dumps(event).encode(),
            )
        except (ClientError, BotoCoreError) as exc:
            raise RelayError(f"relay {self.function_name} could not be invoked: {exc}") from exc
        raw = response["Payload"].read()
        try:
            payload = json_module.loads(raw or b"null")
        except ValueError as exc:
            raise RelayError(f"relay {self.function_name} returned a non-JSON payload: {raw[:200]!r}") from exc
        if response.get("FunctionError"):
            detail = payload if isinstance(payload, dict) else {"errorMessage": str(payload)}
            raise RelayError(
                f"relay {self.function_name} failed: {detail.get('errorType', 'error')}: "
                f"{detail.get('errorMessage', raw[:200])}"
            )
        if not isinstance(payload, dict) or "status" not in payload:
            raise RelayError(f"relay {self.function_name} answered without a status: {raw[:200]!r}")
        headers = {str(k).lower(): str(v) for k, v in (payload.get("headers") or {}).items()}
        body = payload.get("body")
        text = "" if body is None else (body if isinstance(body, str) else json_module.dumps(body))
        return RelayResponse(status_code=int(payload["status"]), headers=headers, text=text)


def _headers_of(response) -> dict[str, str]:
    """Lower-cased response headers (requests' CaseInsensitiveDict or the relay's dict; test doubles may carry none)."""
    headers = getattr(response, "headers", None) or {}
    return {str(k).lower(): str(v) for k, v in headers.items()}


class LiteLLMAdmin:
    """Master-key client for the LiteLLM admin API.

    `session` is anything with `requests.Session.request`'s signature: the default `requests.Session` for a public proxy, a
    `LambdaRelaySession` for the private one (`admin_client` picks). `last_response_headers` keeps the headers of the latest
    `chat_completion` (LiteLLM's `x-litellm-response-cost` is what module 5 prints).
    """

    def __init__(self, base_url: str, master_key: str, timeout: float = DEFAULT_TIMEOUT, session=None):
        self.base_url = base_url.rstrip("/")
        self.master_key = master_key
        self.timeout = timeout
        self.session = session or requests.Session()
        self.last_response_headers: dict[str, str] = {}

    @property
    def via_relay(self) -> bool:
        return isinstance(self.session, LambdaRelaySession)

    @property
    def transport(self) -> str:
        """`relay <function name>` on a private landing zone, `direct` against a public URL (the doctor prints it)."""
        return f"relay {self.session.function_name}" if self.via_relay else "direct"

    # ---- plumbing -------------------------------------------------------------------------------------------------
    def _request(
        self, method: str, path: str, params: dict | None = None, json: Any = None, api_key: str | None = None
    ) -> Any:  # noqa: A002
        headers = {"Authorization": f"Bearer {api_key or self.master_key}", "Accept": "application/json"}
        response = self.session.request(
            method, self.base_url + path, params=params, json=json, headers=headers, timeout=self.timeout
        )
        body: Any
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code >= 400:
            raise parse_error(response.status_code, body, response.text)
        return body if body is not None else response.text

    def _get(self, path: str, **params):
        return self._request("GET", path, params={k: v for k, v in params.items() if v is not None} or None)

    def _post(self, path: str, body: dict | None = None):
        return self._request("POST", path, json=body or {})

    def health(self) -> Any:
        return self._get("/health/liveliness")

    # ---- models ---------------------------------------------------------------------------------------------------
    def models(self) -> list[str]:
        """Approved aliases: ids from GET /v1/models."""
        return [m["id"] for m in self._get("/v1/models").get("data", [])]

    def model_info(self) -> list[dict]:
        """GET /model/info rows (model_name, litellm_params incl. guardrailConfig, model_info with prices)."""
        return self._get("/model/info").get("data", [])

    # ---- teams (= projects) ---------------------------------------------------------------------------------------
    def create_team(
        self,
        team_id: str,
        alias: str,
        models: list[str],
        max_budget: float | None = None,
        budget_duration: str | None = None,
        tpm_limit: int | None = None,
        rpm_limit: int | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """POST /team/new (idempotent: an existing team is fetched instead). Returns `get_team(team_id)`."""
        body: dict[str, Any] = {"team_id": team_id, "team_alias": alias, "models": list(models)}
        for key, value in (
            ("max_budget", max_budget),
            ("budget_duration", budget_duration),
            ("tpm_limit", tpm_limit),
            ("rpm_limit", rpm_limit),
            ("metadata", metadata),
        ):
            if value is not None:
                body[key] = value
        try:
            self._post("/team/new", body)
        except LiteLLMError as exc:
            if exc.status not in (400, 409) or "already exist" not in exc.message.lower():
                raise
        return self.get_team(team_id)

    def get_team(self, team_id: str) -> dict:
        """GET /team/info -> {"team_id", "team_info": {spend, max_budget, budget_reset_at, models, ...}, "keys": [...]}."""
        return self._get("/team/info", team_id=team_id)

    def find_team(self, team_id: str) -> dict | None:
        try:
            return self.get_team(team_id)
        except LiteLLMError as exc:
            if exc.status == 404 or "not found" in exc.message.lower():
                return None
            raise

    def update_team(self, team_id: str, **fields) -> dict:
        """POST /team/update with any of models, max_budget, budget_duration, tpm_limit, rpm_limit, metadata, blocked."""
        return self._post("/team/update", {"team_id": team_id, **fields})

    def delete_team(self, team_id: str) -> Any:
        return self._post("/team/delete", {"team_ids": [team_id]})

    # ---- keys (= agent + environment + model) --------------------------------------------------------------------
    def create_service_account_key(
        self,
        team_id: str,
        alias: str,
        models: list[str],
        max_budget: float | None = None,
        budget_duration: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """POST /key/service-account/generate (falls back to /key/generate on 404) -> the response with `key` (raw, shown once)
        and `token` (hashed id used by /key/info, /key/update, /key/block and /spend/logs?api_key=)."""
        body: dict[str, Any] = {"team_id": team_id, "key_alias": alias, "models": list(models)}
        for key, value in (("max_budget", max_budget), ("budget_duration", budget_duration), ("metadata", metadata)):
            if value is not None:
                body[key] = value
        try:
            response = self._post("/key/service-account/generate", body)
        except LiteLLMError as exc:
            if exc.status != 404:
                raise
            response = self._post("/key/generate", body)
        response.setdefault("token", response.get("token_id"))
        return response

    def list_keys(self, team_id: str | None = None, page_size: int = 100) -> list[dict]:
        """Every key row (GET /key/list?return_full_object=true, all pages), optionally of one team: `workshop cleanup` sweeps keys made by hand."""
        rows: list[dict] = []
        page_no = 1
        while True:
            page = self._get("/key/list", team_id=team_id, return_full_object="true", page=page_no, size=page_size)
            keys = page.get("keys", []) if isinstance(page, dict) else page
            rows.extend(row for row in keys if isinstance(row, dict))
            total_pages = page.get("total_pages") if isinstance(page, dict) else None
            if not keys or len(keys) < page_size or (total_pages is not None and page_no >= int(total_pages)):
                return rows
            page_no += 1

    def find_key(self, alias: str) -> dict | None:
        """The key row with this alias (GET /key/list?key_alias=...&return_full_object=true), or None."""
        page = self._get("/key/list", key_alias=alias, return_full_object="true")
        keys = page.get("keys", []) if isinstance(page, dict) else page
        for row in keys:
            if isinstance(row, dict) and row.get("key_alias") == alias:
                return row
        return None

    def key_info(self, key_or_hash: str) -> dict:
        """GET /key/info -> the `info` row (spend, max_budget, budget_reset_at, models, blocked, key_alias, team_id)."""
        body = self._get("/key/info", key=key_or_hash)
        info = body.get("info", body) if isinstance(body, dict) else body
        if isinstance(info, dict) and "key" in body:
            info.setdefault("token", body["key"])
        return info

    def update_key(self, key_or_hash: str, **fields) -> dict:
        """POST /key/update with any of max_budget, budget_duration, models, metadata, blocked."""
        return self._post("/key/update", {"key": key_or_hash, **fields})

    def block_key(self, key_or_hash: str) -> dict:
        return self._post("/key/block", {"key": key_or_hash})

    def unblock_key(self, key_or_hash: str) -> dict:
        return self._post("/key/unblock", {"key": key_or_hash})

    def delete_key(self, key_or_hash: str) -> Any:
        return self._post("/key/delete", {"keys": [key_or_hash]})

    # ---- spend ----------------------------------------------------------------------------------------------------
    def spend_logs(
        self,
        api_key: str | None = None,
        team_id: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict]:
        """GET /spend/logs rows (model, spend, prompt_tokens, completion_tokens, startTime, metadata.user_api_key_alias, ...)."""
        rows = self._get("/spend/logs", api_key=api_key, team_id=team_id, start_date=start_date, end_date=end_date)
        return rows if isinstance(rows, list) else rows.get("data", [])

    def team_spend_summary(self, team_id: str) -> dict:
        """Team spend vs cap with one row per key: {teamId, alias, spend, maxBudget, budgetResetAt, models, keys[...]}."""
        team = self.get_team(team_id)
        info = team.get("team_info") or {}
        keys = []
        for row in team.get("keys") or []:
            keys.append(
                {
                    "alias": row.get("key_alias"),
                    "token": row.get("token"),
                    "spend": row.get("spend", 0.0),
                    "maxBudget": row.get("max_budget"),
                    "budgetResetAt": row.get("budget_reset_at"),
                    "models": row.get("models") or [],
                    "blocked": bool(row.get("blocked")),
                    "metadata": row.get("metadata") or {},
                }
            )
        return {
            "teamId": team.get("team_id", team_id),
            "alias": info.get("team_alias"),
            "spend": info.get("spend", 0.0),
            "maxBudget": info.get("max_budget"),
            "budgetDuration": info.get("budget_duration"),
            "budgetResetAt": info.get("budget_reset_at"),
            "tpmLimit": info.get("tpm_limit"),
            "rpmLimit": info.get("rpm_limit"),
            "models": info.get("models") or [],
            "blocked": bool(info.get("blocked")),
            "keys": keys,
        }

    # ---- smoke test -----------------------------------------------------------------------------------------------
    def chat_completion(
        self, api_key: str, model: str, messages: list[dict], max_tokens: int = 64, stream: bool = False, **params
    ) -> dict:
        """POST /v1/chat/completions with a virtual key. Non-streaming returns the response JSON; streaming returns
        {"text", "usage", "chunks"} folded from the SSE events (usage from the final chunk with stream_options).

        Both paths work through the relay as well: it returns the whole body, so the streaming fold reads the buffered SSE
        text line by line. The response headers (cost header `x-litellm-response-cost`) stay in `last_response_headers`."""
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": max_tokens, **params}
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        if not stream:
            response = self.session.request(
                "POST",
                self.base_url + "/v1/chat/completions",
                json=body,
                headers=headers,
                timeout=max(self.timeout, 120),
            )
            self.last_response_headers = _headers_of(response)
            try:
                payload = response.json()
            except ValueError:
                payload = None
            if response.status_code >= 400:
                raise parse_error(response.status_code, payload, response.text)
            return payload
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
        response = self.session.request(
            "POST",
            self.base_url + "/v1/chat/completions",
            json=body,
            headers=headers,
            timeout=max(self.timeout, 120),
            stream=True,
        )
        self.last_response_headers = _headers_of(response)
        if response.status_code >= 400:
            try:
                payload = response.json()
            except ValueError:
                payload = None
            raise parse_error(response.status_code, payload, response.text)
        text, usage, chunks = [], None, 0
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            chunks += 1
            for choice in chunk.get("choices") or []:
                content = (choice.get("delta") or {}).get("content")
                if content:
                    text.append(content)
            if chunk.get("usage"):
                usage = chunk["usage"]
        return {"text": "".join(text), "usage": usage, "chunks": chunks}


# ---- building the client ------------------------------------------------------------------------------------------------
def relay_lambda_client(region: str | None = None):
    """A Lambda client for the relay: retries off (a retried POST would create a key twice), read timeout above the relay's."""
    from botocore.config import Config

    return boto3.client(
        "lambda",
        region_name=config.default_region(region),
        config=Config(connect_timeout=10, read_timeout=RELAY_READ_TIMEOUT, retries={"max_attempts": 0}),
    )


def admin_client(cfg, master_key: str, lambda_client=None, timeout: float = DEFAULT_TIMEOUT) -> LiteLLMAdmin:
    """The admin client for the platform's model gateway: through the relay on a private landing zone, direct otherwise.

    `cfg` is a PlatformConfig. `model_gateway_relay_function_name` (SSM `model-gateway/relay-function-name`, published by the
    ModelGateway stack) selects the relay, because the proxy's load balancer is internal and only VPC residents reach it; without
    it the URL is called with `requests`. `lambda_client` lets the backend reuse its cached client and tests pass a fake; by
    default `relay_lambda_client` builds one.
    """
    function_name = getattr(cfg, "model_gateway_relay_function_name", None)
    if not function_name:
        return LiteLLMAdmin(cfg.model_gateway_url, master_key, timeout=timeout)
    session = LambdaRelaySession(function_name, lambda_client or relay_lambda_client(cfg.region))
    return LiteLLMAdmin(cfg.model_gateway_url, master_key, timeout=timeout, session=session)


def read_master_key(cfg, secrets_client=None) -> str:
    """The proxy's master key from Secrets Manager (ARN from SSM): what the CLI and the scripts build their admin client with."""
    client = secrets_client or boto3.client("secretsmanager", region_name=cfg.region)
    return client.get_secret_value(SecretId=cfg.model_gateway_master_key_secret_arn)["SecretString"]


# ---- a localhost front for code that only speaks HTTP --------------------------------------------------------------------
class LocalRelayProxy:
    """A 127.0.0.1 HTTP server that forwards every request through the relay: the private proxy for plain HTTP clients.

    `workshop export --run` starts one and runs the exported module with `MODEL_GATEWAY_BASE=http://127.0.0.1:<port>/v1`, so an
    OpenAI client on the laptop reaches the proxy inside the VPC without a change to the generated code. Requests are buffered
    (the relay returns whole bodies): a streaming completion arrives as one SSE body, which OpenAI clients parse as usual. The
    Authorization header of the caller is forwarded as is; the server adds no credentials.

        with litellm.LocalRelayProxy(admin.session) as proxy:
            subprocess.run([...], env={**os.environ, "MODEL_GATEWAY_BASE": proxy.base_url + "/v1"})
    """

    # hop-by-hop and framing headers: the server frames its own response, the relay already decoded the upstream body
    DROP_HEADERS = frozenset(
        {
            "connection",
            "keep-alive",
            "transfer-encoding",
            "content-length",
            "content-encoding",
            "host",
            "accept-encoding",
            "te",
            "trailer",
            "upgrade",
            "proxy-connection",
        }
    )

    def __init__(self, session: LambdaRelaySession, host: str = "127.0.0.1", port: int = 0):
        self.session = session
        self._server = ThreadingHTTPServer((host, port), self._handler_class())
        self._thread = threading.Thread(target=self._server.serve_forever, name="relay-proxy", daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> LocalRelayProxy:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> LocalRelayProxy:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _handler_class(self):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):  # noqa: A002 - the BaseHTTPRequestHandler signature
                pass  # the caller's own output is the story; the relay logs in CloudWatch

            def _forward(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k: v for k, v in self.headers.items() if k.lower() not in proxy.DROP_HEADERS}
                try:
                    response = proxy.session.request(
                        self.command, self.path, headers=headers, data=body or None, timeout=proxy.session.max_timeout
                    )
                    status, response_headers, content = response.status_code, response.headers, response.content
                except RelayError as exc:
                    status, response_headers = 502, {"content-type": "application/json"}
                    content = json_module.dumps({"error": {"message": str(exc), "type": "relay_error"}}).encode()
                self.send_response(status)
                for name, value in response_headers.items():
                    if name.lower() not in proxy.DROP_HEADERS:
                        self.send_header(name, value)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _forward

        return Handler
