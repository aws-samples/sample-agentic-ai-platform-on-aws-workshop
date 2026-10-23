"""The live runner must wait for a real release verdict, never fabricate passing scores."""

import runpy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


def test_live_runner_requires_the_native_verdict():
    runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/lifecycle_test.py"))
    developer = Mock()
    developer.get_release.return_value = {"releaseId": "R1", "status": "PENDING_APPROVAL"}
    ctx = SimpleNamespace(api={"developer": developer})
    result = runner["wait_release"](ctx, {"releaseId": "R1", "status": "DRAFT"}, "PENDING_APPROVAL")
    assert result["status"] == "PENDING_APPROVAL"
    developer.submit_evaluation.assert_not_called()


def test_live_runner_fails_on_blocked_instead_of_claiming_success():
    runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/lifecycle_test.py"))
    ctx = SimpleNamespace(api={"developer": Mock()})
    with pytest.raises(AssertionError, match="BLOCKED"):
        runner["wait_release"](ctx, {"releaseId": "R1", "status": "BLOCKED"}, "PENDING_APPROVAL")


def test_live_runner_waits_for_native_approval_readiness():
    runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/lifecycle_test.py"))
    pending = {"releaseId": "R1", "status": "APPROVED", "pipeline": {"executionId": "EX1", "approvalReady": False}}
    ready = {**pending, "pipeline": {"executionId": "EX1", "approvalReady": True}}
    developer = Mock()
    developer.get_release.return_value = ready
    result = runner["wait_release"](SimpleNamespace(api={"developer": developer}), pending, "APPROVED")
    assert result["pipeline"]["approvalReady"]
