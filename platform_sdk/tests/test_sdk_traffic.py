"""platform_sdk.traffic: the module 7 traffic generator, with a fake platform client (no network, no AWS)."""

from __future__ import annotations

import re
import threading
import time

import pytest
from platform_sdk.api import PlatformApiError

from platform_sdk import agentcore, traffic

SESSION_ID_RE = re.compile(
    r"^[a-zA-Z0-9][a-zA-Z0-9_-]{32,99}$"
)  # the POST /invoke rule (InvokeHarness runtimeSessionId)


class FakeClient:
    """Speaks the two methods the generator uses: invoke -> {jobId, sessionId}; wait_for_job -> the finished job."""

    def __init__(self, fail_when=None, raise_when=None, delay=0.0):
        self.invokes: list[dict] = []
        self.waited: list[str] = []
        self.fail_when = fail_when or (lambda prompt: False)
        self.raise_when = raise_when or (lambda prompt: None)
        self.delay = delay
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0

    def invoke(self, project_id, env, prompt, session_id=None):
        exc = self.raise_when(prompt)
        if exc:
            raise exc
        with self.lock:
            self.invokes.append({"projectId": project_id, "env": env, "prompt": prompt, "sessionId": session_id})
            job_id = f"job-{len(self.invokes)}"
        return {"jobId": job_id, "sessionId": session_id or "generated-by-the-backend-0123456789abcdef"}

    def wait_for_job(self, job_id, timeout=300, poll=2, raise_on_failure=False):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.waited.append(job_id)
            prompt = self.invokes[int(job_id.split("-")[1]) - 1]["prompt"]
        try:
            if self.delay:
                time.sleep(self.delay)
            if self.fail_when(prompt):
                return {
                    "jobId": job_id,
                    "status": "FAILED",
                    "error": {"code": "BUDGET_EXCEEDED", "message": "Budget has been exceeded! Key=hr-assistant-prod"},
                }
            return {
                "jobId": job_id,
                "status": "SUCCEEDED",
                "output": {
                    "text": f"answer to: {prompt}",
                    "toolsUsed": ["hrtools___get_leave_balance"] if "leave balance" in prompt else [],
                    "usage": {"inputTokens": 1000, "outputTokens": 50, "totalTokens": 1050, "modelCalls": 2},
                    "stopReason": "end_turn",
                    "seconds": 4.2,
                },
            }
        finally:
            with self.lock:
                self.active -= 1


def test_scenario_prompts_reads_the_dataset(scenario_dir):
    prompts = traffic.scenario_prompts(scenario_dir / "evaluation" / "hr-assistant.json")
    assert len(prompts) == 10
    assert prompts[0] == "What is my leave balance? My employee id is E1001."
    assert prompts == list(traffic.DEFAULT_PROMPTS)  # the fixed default set mirrors the scenario file


def test_scenario_prompts_rejects_an_empty_dataset(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text('{"scenarios": [{"turns": []}]}')
    with pytest.raises(ValueError):
        traffic.scenario_prompts(path)


def test_platform_transport_cycles_prompts_and_uses_one_session_per_conversation():
    client = FakeClient()
    done: list[int] = []
    report = traffic.generate(
        count=5,
        prompts=["a", "b", "c"],
        client=client,
        project_id="hr-assistant",
        env="prod",
        concurrency=1,
        on_done=lambda c: done.append(c.index),
    )
    assert [c.prompt for c in report.conversations] == ["a", "b", "c", "a", "b"]
    assert [c.index for c in report.conversations] == [1, 2, 3, 4, 5] and done == [1, 2, 3, 4, 5]
    sessions = report.session_ids()
    assert len(set(sessions)) == 5 and all(SESSION_ID_RE.match(s) for s in sessions)
    assert all(s.endswith("-traffic") for s in sessions)
    assert [i["env"] for i in client.invokes] == ["prod"] * 5 and {i["projectId"] for i in client.invokes} == {
        "hr-assistant"
    }
    assert [i["sessionId"] for i in client.invokes] == sessions  # the generator chose the ids, the backend kept them
    assert client.waited == ["job-1", "job-2", "job-3", "job-4", "job-5"]
    assert report.transport == "platform" and report.env == "prod"
    assert all(c.ok and c.job_id and c.text.startswith("answer to:") for c in report.conversations)
    assert report.conversations[0].usage["inputTokens"] == 1000 and report.conversations[0].harness_seconds == 4.2
    summary = report.summary()
    assert summary["count"] == 5 and summary["succeeded"] == 5 and summary["failed"] == 0
    assert summary["inputTokens"] == 5000 and summary["outputTokens"] == 250 and summary["modelCalls"] == 10
    assert summary["errorCodes"] == {} and summary["secondsP50"] is not None


def test_failures_are_rows_of_the_report_not_exceptions():
    def raise_for(prompt):
        if prompt == "boom":
            return PlatformApiError("HARNESS_NOT_DEPLOYED", "no prod harness yet", 409)
        if prompt == "slow":
            return TimeoutError("job job-x still RUNNING after 300 s")
        return None

    client = FakeClient(fail_when=lambda p: p == "capped", raise_when=raise_for)
    report = traffic.generate(
        count=4,
        prompts=["What is my leave balance? E1001", "capped", "boom", "slow"],
        client=client,
        project_id="hr-assistant",
        env="prod",
        concurrency=1,
    )
    statuses = [(c.status, c.error_code) for c in report.conversations]
    assert statuses == [
        ("SUCCEEDED", None),
        ("FAILED", "BUDGET_EXCEEDED"),
        ("FAILED", "HARNESS_NOT_DEPLOYED"),
        ("FAILED", "TimeoutError"),
    ]
    assert "Budget has been exceeded" in report.conversations[1].error
    assert "no prod harness yet" in report.conversations[2].error
    summary = report.summary()
    assert summary["succeeded"] == 1 and summary["failed"] == 3
    assert summary["errorCodes"] == {"BUDGET_EXCEEDED": 1, "HARNESS_NOT_DEPLOYED": 1, "TimeoutError": 1}
    assert summary["toolCalls"] == {"hrtools___get_leave_balance": 1}
    table = report.table()
    assert table.count("\n") == 4 + 2  # header, rule, four rows, summary line
    assert "BUDGET_EXCEEDED" in table and "1/4 succeeded" in table and "hrtools___get_leave_balance" in table


def test_concurrency_runs_in_parallel_and_keeps_the_order():
    client = FakeClient(delay=0.2)
    started = time.monotonic()
    report = traffic.generate(count=6, prompts=["p"], client=client, project_id="hr-assistant", concurrency=3)
    elapsed = time.monotonic() - started
    assert elapsed < 6 * 0.2  # three at a time: two rounds of 0.2 s, not six
    assert client.max_active >= 2
    assert [c.index for c in report.conversations] == [1, 2, 3, 4, 5, 6]
    assert report.seconds >= 0.3 and all(c.ok for c in report.conversations)


def test_direct_transport_uses_agentcore_invoke(monkeypatch):
    calls: list[dict] = []

    def fake_invoke(harness_arn, prompt, qualifier=None, session_id=None, timeout=180, actor_id=None, region=None):
        calls.append({"arn": harness_arn, "prompt": prompt, "qualifier": qualifier, "session": session_id})
        if prompt == "refused":
            raise agentcore.HarnessInvokeError(
                "Budget has been exceeded! Key=hr-assistant-prod Current cost: 0.02, Max budget: 0.01",
                budget_exceeded=True,
                session_id=session_id,
            )
        if prompt == "broken":
            raise agentcore.HarnessInvokeError("runtimeClientError", budget_exceeded=False, session_id=session_id)
        return agentcore.InvokeResult(
            text="12 days",
            tools=["hrtools___get_leave_balance"],
            usage={"inputTokens": 900, "outputTokens": 40, "totalTokens": 940, "modelCalls": 2},
            stop_reasons=["end_turn"],
            hooks=[],
            session_id=session_id,
            seconds=5.5,
        )

    monkeypatch.setattr(traffic.agentcore, "invoke", fake_invoke)
    report = traffic.generate(
        count=3,
        prompts=["balance?", "refused", "broken"],
        harness_arn="arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/h1",
        qualifier="live",
        concurrency=1,
    )
    assert report.transport == "direct" and report.env == "live"
    assert [c["qualifier"] for c in calls] == ["live"] * 3 and all(c["session"].endswith("-traffic") for c in calls)
    assert [c.status for c in report.conversations] == ["SUCCEEDED", "FAILED", "FAILED"]
    assert (
        report.conversations[0].tools == ["hrtools___get_leave_balance"] and report.conversations[0].text == "12 days"
    )
    assert report.conversations[0].harness_seconds == 5.5
    assert report.conversations[1].error_code == "BUDGET_EXCEEDED" and "Max budget" in report.conversations[1].error
    assert report.conversations[2].error_code == "INVOKE_FAILED"


def test_generate_validates_its_arguments():
    client = FakeClient()
    with pytest.raises(ValueError, match="count"):
        traffic.generate(count=0, client=client, project_id="p")
    with pytest.raises(ValueError, match="either client"):
        traffic.generate(count=1)
    with pytest.raises(ValueError, match="either client"):
        traffic.generate(count=1, client=client, project_id="p", harness_arn="arn:x")
    with pytest.raises(ValueError, match="project_id"):
        traffic.generate(count=1, client=client)
    with pytest.raises(ValueError, match="concurrency"):
        traffic.generate(count=1, client=client, project_id="p", concurrency=0)
    with pytest.raises(ValueError, match="prompts"):
        traffic.generate(count=1, prompts=["", "  "], client=client, project_id="p")


def test_default_prompts_are_used_when_none_are_given():
    client = FakeClient()
    report = traffic.generate(count=12, client=client, project_id="hr-assistant", env="dev", concurrency=1)
    prompts = [c.prompt for c in report.conversations]
    assert prompts[:10] == list(traffic.DEFAULT_PROMPTS) and prompts[10:] == list(traffic.DEFAULT_PROMPTS[:2])
    assert report.conversations[0].as_dict()["index"] == 1
