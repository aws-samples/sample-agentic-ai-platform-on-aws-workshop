"""Traffic generator for module 7: N short conversations against a harness endpoint, one new runtime session each.

Two transports produce the same report:

- through the platform (the default of `workshop traffic`): `client.invoke(project_id, env, prompt, session_id)` queues an invoke job and
  `client.wait_for_job` polls `GET /jobs/{id}` (the playground path; the backend picks qualifier `live` for prod);
- direct: `agentcore.invoke(harness_arn, prompt, qualifier, session_id)` for scripts that have no platform token.

Prompts are a fixed set from the scenario: `scenario_prompts(path)` reads the first user turn of every scenario in the
evaluation dataset (`scenario/evaluation/hr-assistant.json`), `DEFAULT_PROMPTS` is the same list for callers without the
file. Conversation i uses `prompts[i % len(prompts)]`. Conversations run `concurrency` at a time (3, like the dataset runner):
a new runtime session costs 25-35 s when no warm microVM is available, so sequential traffic would take minutes.

    report = traffic.generate(count=10, prompts=traffic.scenario_prompts(path), client=dev_api, project_id="hr-assistant", env="prod")
    print(report.table())
    report.summary()   # {"count": 10, "succeeded": 10, "failed": 0, "secondsP50": ..., "inputTokens": ..., "toolCalls": {...}}

A failed conversation (job FAILED, timeout, refused invocation) is recorded in the report, never raised: the module shows what
happened and its verify step decides.
"""

from __future__ import annotations

import json
import statistics
import time
from collections import Counter
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import agentcore

DEFAULT_PROMPTS: tuple[str, ...] = (
    "What is my leave balance? My employee id is E1001.",
    "Who is employee E1007, what is their role, and who do they report to?",
    "Please submit 3 days of annual leave for employee E1002 starting on 2026-11-16.",
    "Submit an annual leave request for employee E1012: 12 days starting 2027-02-01.",
    "How many days per week can I work remotely, and which day do I have to be in the office?",
    "How many days of annual leave do full-time employees get per year, and how many unused days can I carry over into next year?",
    "How many weeks of paid parental leave does a primary caregiver get at AnyCompany, and when am I eligible?",
    "Which US public holidays are there in November 2026?",
    "What is the payment status of invoice INV-1003?",
    "Can I sue my manager for denying my leave?",
)
SESSION_SUFFIX = "traffic"
DEFAULT_CONCURRENCY = 3
DEFAULT_TIMEOUT = 300.0


def scenario_prompts(dataset_path: str | Path) -> list[str]:
    """First user turn of every scenario in a dataset file ({"scenarios": [{"turns": [{"input": ...}]}]})."""
    data = json.loads(Path(dataset_path).read_text())
    prompts: list[str] = []
    for scenario in data.get("scenarios") or []:
        turns = scenario.get("turns") or []
        text = (turns[0] or {}).get("input") if turns else None
        if isinstance(text, str) and text.strip():
            prompts.append(text.strip())
    if not prompts:
        raise ValueError(f"no scenario inputs found in {dataset_path}")
    return prompts


@dataclass
class Conversation:
    """One conversation: a single user turn in its own runtime session."""

    index: int
    prompt: str
    session_id: str
    status: str = "PENDING"  # SUCCEEDED | FAILED
    text: str = ""
    tools: list[str] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    seconds: float = 0.0  # wall time seen by the generator (job polling included)
    harness_seconds: float | None = None  # the invocation time the worker or the SDK measured
    error_code: str | None = None
    error: str | None = None
    job_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "SUCCEEDED"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class TrafficReport:
    """Every conversation of a `generate` run plus the wall time; `table()` and `summary()` are what `workshop traffic` prints."""

    conversations: list[Conversation]
    seconds: float
    env: str | None = None
    transport: str = "platform"

    def succeeded(self) -> list[Conversation]:
        return [c for c in self.conversations if c.ok]

    def failed(self) -> list[Conversation]:
        return [c for c in self.conversations if not c.ok]

    def session_ids(self) -> list[str]:
        return [c.session_id for c in self.conversations]

    def summary(self) -> dict:
        """Counts, latency percentiles (wall seconds of the successful conversations), token totals, tool and error counters."""
        ok = self.succeeded()
        wall = sorted(c.seconds for c in ok)
        tools: Counter[str] = Counter(tool for c in ok for tool in c.tools)
        errors: Counter[str] = Counter(c.error_code or "ERROR" for c in self.failed())
        return {
            "count": len(self.conversations),
            "succeeded": len(ok),
            "failed": len(self.conversations) - len(ok),
            "seconds": round(self.seconds, 1),
            "secondsP50": round(statistics.median(wall), 1) if wall else None,
            "secondsMax": round(wall[-1], 1) if wall else None,
            "inputTokens": sum(int((c.usage or {}).get("inputTokens") or 0) for c in ok),
            "outputTokens": sum(int((c.usage or {}).get("outputTokens") or 0) for c in ok),
            "modelCalls": sum(int((c.usage or {}).get("modelCalls") or 0) for c in ok),
            "toolCalls": dict(tools.most_common()),
            "errorCodes": dict(errors.most_common()),
            "env": self.env,
            "transport": self.transport,
        }

    def table(self, prompt_width: int = 58) -> str:
        """Compact text table: one row per conversation (index, status, wall seconds, tools, tokens in/out, prompt excerpt)."""
        header = f"{'#':>2}  {'status':<9} {'wall s':>6}  {'tools':<42} {'tokens in/out':>13}  prompt"
        lines = [header, "-" * len(header)]
        for c in self.conversations:
            tools = ", ".join(c.tools) if c.tools else ("-" if c.ok else f"{c.error_code or 'ERROR'}")
            usage = c.usage or {}
            tokens = f"{usage.get('inputTokens', 0)}/{usage.get('outputTokens', 0)}" if c.ok else (c.error or "")[:13]
            prompt = c.prompt if len(c.prompt) <= prompt_width else c.prompt[: prompt_width - 3] + "..."
            lines.append(f"{c.index:>2}  {c.status:<9} {c.seconds:>6.1f}  {tools[:42]:<42} {tokens:>13}  {prompt}")
        s = self.summary()
        lines.append(
            f"{s['succeeded']}/{s['count']} succeeded in {s['seconds']} s (p50 {s['secondsP50']} s, max {s['secondsMax']} s); "
            f"{s['inputTokens']}/{s['outputTokens']} tokens in {s['modelCalls']} model calls; tool calls {s['toolCalls']}"
            + (f"; errors {s['errorCodes']}" if s["errorCodes"] else "")
        )
        return "\n".join(lines)


def _error_text(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return f"{type(exc).__name__}: {text}" if type(exc).__name__ not in text else text


def _run_platform(conversation: Conversation, client, project_id: str, env: str, timeout: float) -> None:
    """POST /invoke + GET /jobs/{id} for one conversation; fills the conversation in place."""
    started = time.monotonic()
    try:
        accepted = client.invoke(project_id, env, conversation.prompt, conversation.session_id)
        conversation.job_id = accepted.get("jobId")
        if accepted.get("sessionId"):
            conversation.session_id = accepted["sessionId"]
        job = client.wait_for_job(conversation.job_id, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - a failed conversation is a row of the report, not the end of the run
        conversation.status = "FAILED"
        conversation.error_code = getattr(exc, "code", None) or type(exc).__name__
        conversation.error = _error_text(exc)
        conversation.seconds = round(time.monotonic() - started, 1)
        return
    conversation.seconds = round(time.monotonic() - started, 1)
    output = job.get("output") or {}
    if job.get("status") == "SUCCEEDED":
        conversation.status = "SUCCEEDED"
        conversation.text = str(output.get("text") or "")
        conversation.tools = list(output.get("toolsUsed") or [])
        conversation.usage = dict(output.get("usage") or {})
        conversation.harness_seconds = output.get("seconds")
    else:
        error = job.get("error") if isinstance(job.get("error"), dict) else {"message": str(job.get("error"))}
        conversation.status = "FAILED"
        conversation.error_code = error.get("code") or job.get("status") or "FAILED"
        conversation.error = str(error.get("message") or error)


def _run_direct(conversation: Conversation, harness_arn: str, qualifier: str | None, timeout: float, region) -> None:
    """agentcore.invoke for one conversation; fills the conversation in place."""
    started = time.monotonic()
    try:
        result = agentcore.invoke(
            harness_arn,
            conversation.prompt,
            qualifier=qualifier,
            session_id=conversation.session_id,
            timeout=int(timeout),
            region=region,
        )
    except agentcore.HarnessInvokeError as exc:
        conversation.status = "FAILED"
        conversation.error_code = "BUDGET_EXCEEDED" if exc.budget_exceeded else "INVOKE_FAILED"
        conversation.error = exc.message
        conversation.seconds = round(time.monotonic() - started, 1)
        return
    except Exception as exc:  # noqa: BLE001 - same rule as the platform transport
        conversation.status = "FAILED"
        conversation.error_code = type(exc).__name__
        conversation.error = _error_text(exc)
        conversation.seconds = round(time.monotonic() - started, 1)
        return
    conversation.seconds = round(time.monotonic() - started, 1)
    conversation.status = "SUCCEEDED"
    conversation.text = result.text
    conversation.tools = list(result.tools)
    conversation.usage = dict(result.usage)
    conversation.harness_seconds = round(result.seconds, 1)


def generate(
    count: int = 10,
    prompts: Sequence[str] | None = None,
    client=None,
    project_id: str | None = None,
    env: str = "prod",
    harness_arn: str | None = None,
    qualifier: str | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    timeout: float = DEFAULT_TIMEOUT,
    session_suffix: str = SESSION_SUFFIX,
    on_done: Callable[[Conversation], None] | None = None,
    region: str | None = None,
) -> TrafficReport:
    """Run `count` single-turn conversations, each in a fresh runtime session, and return a TrafficReport.

    Transport: `client` (a PlatformClient or anything with `invoke` and `wait_for_job`) plus `project_id` and `env` go through
    the platform; `harness_arn` (plus `qualifier`, `"live"` for prod) calls InvokeHarness directly. Exactly one of the two.
    `prompts` default to `DEFAULT_PROMPTS`; `on_done(conversation)` is called as each conversation finishes (progress printing).
    """
    if count < 1:
        raise ValueError("count must be at least 1")
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if bool(client) == bool(harness_arn):
        raise ValueError("pass either client + project_id (platform transport) or harness_arn (direct transport)")
    if client and not project_id:
        raise ValueError("project_id is required with the platform transport")
    chosen = [p for p in (prompts if prompts is not None else DEFAULT_PROMPTS) if isinstance(p, str) and p.strip()]
    if not chosen:
        raise ValueError("prompts must contain at least one non-empty string")
    transport = "platform" if client else "direct"
    conversations = [
        Conversation(index=i + 1, prompt=chosen[i % len(chosen)], session_id=agentcore.new_session_id(session_suffix))
        for i in range(count)
    ]

    def run(conversation: Conversation) -> Conversation:
        if transport == "platform":
            _run_platform(conversation, client, project_id, env, timeout)
        else:
            _run_direct(conversation, harness_arn, qualifier, timeout, region)
        if on_done:
            on_done(conversation)
        return conversation

    started = time.monotonic()
    if concurrency == 1 or count == 1:
        for conversation in conversations:
            run(conversation)
    else:
        with ThreadPoolExecutor(max_workers=min(concurrency, count)) as pool:
            list(pool.map(run, conversations))
    return TrafficReport(
        conversations=conversations,
        seconds=round(time.monotonic() - started, 1),
        env=env if transport == "platform" else qualifier,
        transport=transport,
    )
