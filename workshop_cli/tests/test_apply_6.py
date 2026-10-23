"""Module 6 guards that need no AWS: the gateway-mode check before an evaluation, the closing line, the verdict printout."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from workshop_cli import apply_6


def test_failed_native_execution_fails_the_command():
    failed = {"releaseId": "R1", "status": "DRAFT", "error": {"code": "PIPELINE_FAILED", "message": "deployment failed"}}
    with pytest.raises(apply_6.WorkshopError, match="deployment failed"):
        apply_6.wait_for_platform_run(SimpleNamespace(developer=Mock()), failed)


def test_release_runs_in_codepipeline_and_cli_only_follows(monkeypatch):
    developer = Mock()
    release = {"releaseId": "R1", "status": "DRAFT"}
    running = {"releaseId": "R1", "status": "EVALUATING", "pipeline": {"executionId": "execution-1"}}
    passed = {"releaseId": "R1", "status": "PENDING_APPROVAL"}
    developer.get_release.return_value = running
    ctx = SimpleNamespace(developer=developer, inputs=lambda module: {"max_leave_days": 10})
    monkeypatch.setattr(apply_6, "dev_gateway_mode", lambda ctx: ("hr-dev", "ENFORCE"))
    follow = Mock(return_value=passed)
    monkeypatch.setattr(apply_6, "wait_for_platform_run", follow)
    assert apply_6.run_release_pipeline(ctx, release, expect_pass=True) == passed
    developer.run_release.assert_called_once_with("R1")
    follow.assert_called_once_with(ctx, running)
    developer.submit_evaluation.assert_not_called()
    developer.deploy.assert_not_called()


def test_running_release_is_followed_without_starting_another_execution(monkeypatch):
    developer = Mock()
    ctx = SimpleNamespace(developer=developer)
    running = {"releaseId": "R1", "status": "EVALUATING"}
    follow = Mock(return_value={"releaseId": "R1", "status": "BLOCKED"})
    monkeypatch.setattr(apply_6, "wait_for_platform_run", follow)
    assert apply_6.run_release_pipeline(ctx, running, expect_pass=False)["status"] == "BLOCKED"
    developer.run_release.assert_not_called()


def test_v2_gateway_guard_stops_before_starting_pipeline(monkeypatch):
    developer = Mock()
    ctx = SimpleNamespace(developer=developer, inputs=lambda module: {"max_leave_days": 10})
    monkeypatch.setattr(apply_6, "dev_gateway_mode", lambda ctx: ("hr-dev", "LOG_ONLY"))
    with pytest.raises(apply_6.WorkshopError, match="ENFORCE"):
        apply_6.run_release_pipeline(ctx, {"releaseId": "R1", "status": "BLOCKED"}, expect_pass=True)
    developer.run_release.assert_not_called()


def test_gateway_mode_problem_only_while_the_gateway_does_not_enforce():
    assert apply_6.gateway_mode_problem("ENFORCE", "hr-assistant-dev-gw", 10) is None
    text = apply_6.gateway_mode_problem("LOG_ONLY", "hr-assistant-dev-gw", 10)
    assert "hr-assistant-dev-gw is LOG_ONLY" in text and "over 10 days" in text
    assert "`workshop mode ENFORCE --gateway hr-dev`" in text and "`workshop apply 6` again" in text
    assert "has no policy engine attached" in apply_6.gateway_mode_problem(None, "hr-assistant-dev-gw", 10)


BLOCKED_V2 = {
    "releaseId": "R2",
    "status": "BLOCKED",
    "evaluation": {
        "verdict": "BLOCKED",
        "details": {
            "_scenarios": {"actual": 1, "threshold": 0, "op": "failed<=", "passed": False},
            "Correctness": {"actual": 0.9, "threshold": 0.8, "op": "mean>=", "passed": True},
            "GoalSuccessRate": {"actual": 0.8889, "threshold": 0.9, "op": "mean>=", "passed": False},
        },
        "failedScenarios": ["denied-topic-legal-advice"],
        "results": [
            {"scenarioId": "leave-balance-e1001", "status": "COMPLETED", "scores": {"GoalSuccessRate": 1.0}},
            {
                "scenarioId": "denied-topic-legal-advice",
                "status": "UNSCORED",
                "scores": {},
                "error": "no evaluator produced a score: An error occurred (ValidationException) ... no spans to evaluate",
            },
        ],
    },
}


def test_closing_line_tells_the_expected_v1_block_from_a_v2_that_should_have_passed():
    level, text = apply_6.closing_line({"releaseId": "R1", "status": "BLOCKED"}, "v1", expect_pass=False)
    assert level == "ok" and "the gate caught prompt v1" in text and "Set prompt_version: v2" in text
    level, text = apply_6.closing_line(BLOCKED_V2, "v2", expect_pass=True)
    assert level == "warn" and "Set prompt_version" not in text
    assert "expected to pass" in text and "GoalSuccessRate 0.8889 vs mean>=0.9" in text
    assert "1 scenario(s) without a score (denied-topic-legal-advice)" in text and "`workshop apply 6` again" in text
    level, text = apply_6.closing_line({"releaseId": "R3", "status": "PENDING_APPROVAL"}, "v2", expect_pass=True)
    assert level == "ok" and "PENDING_APPROVAL" in text and "Domain approver" in text
    assert apply_6.closing_line({"releaseId": "R4", "status": "PROMOTED"}, "v2", True) == (
        "ok",
        "release R4 is PROMOTED",
    )
    assert apply_6.blocked_reasons({}) == "see the verdict above"


def test_print_verdict_names_the_scenarios_without_a_score(capsys):
    apply_6.print_verdict(BLOCKED_V2)
    out = capsys.readouterr().out
    assert "scenarios without a score" in out and "failed<=0  FAIL" in out
    assert "GoalSuccessRate" in out and "mean>=0.9  FAIL" in out and "Correctness" in out
    assert "UNSCORED  denied-topic-legal-advice: no evaluator produced a score" in out
    assert "Run this command again" in out and "-> release BLOCKED (verdict BLOCKED)" in out
    # a clean verdict prints no problem rows and no advice
    apply_6.print_verdict(
        {"status": "PENDING_APPROVAL", "evaluation": {"verdict": "PASSED", "details": {}, "results": []}}
    )
    assert "UNSCORED" not in capsys.readouterr().out
