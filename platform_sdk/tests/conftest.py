"""Shared fixtures. No test here talks to AWS: credentials are fake and every client is stubbed or mocked."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sdk_testlib import CONFIG, PRIVATE_CONFIG, REGION, ROOT, FakeSession  # noqa: E402


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    """Fake credentials and region so that no client can reach a real account, plus fresh caches per test."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_REGION", REGION)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("PLATFORM_CONFIG_JSON", raising=False)
    monkeypatch.delenv("MODEL_GATEWAY_BASE", raising=False)  # the exported modules read it: the tests set it on purpose
    from platform_sdk import agentcore, config, identity

    def clear_caches():
        config.reset_cache()
        for fn in (agentcore.control_client, agentcore.data_client, identity.control_client):
            if hasattr(fn, "cache_clear"):  # a test may have monkeypatched the factory with a plain function
                fn.cache_clear()

    clear_caches()
    yield
    clear_caches()


@pytest.fixture
def cfg(monkeypatch):
    from platform_sdk import config

    monkeypatch.setenv("PLATFORM_CONFIG_JSON", json.dumps(CONFIG))
    config.reset_cache()
    return config.load()


@pytest.fixture
def private_cfg(monkeypatch):
    """The configuration of a private landing zone (tier 3): relay function, private flags, harness subnets and security group."""
    from platform_sdk import config

    monkeypatch.setenv("PLATFORM_CONFIG_JSON", json.dumps(PRIVATE_CONFIG))
    config.reset_cache()
    return config.load()


@pytest.fixture
def scenario_dir() -> Path:
    return ROOT / "scenario"


@pytest.fixture
def fake_session():
    return FakeSession()
