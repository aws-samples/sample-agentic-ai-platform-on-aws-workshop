"""`workshop cap --dev low` demonstrates the graceful BUDGET_EXCEEDED refusal itself (module 5 has no agent to ask yet)."""

from __future__ import annotations

from types import SimpleNamespace

from platform_sdk import litellm, names
from workshop_cli import tools


class FakeAdmin:
    def __init__(self):
        self.updated = []

    def list_keys(self, project_id):
        return [
            {
                "env": "dev",
                "model": "claude-sonnet-5-5",
                "alias": "hr-assistant-dev-claude-sonnet-5-5",
                "maxBudget": 1.0,
            }
        ]

    def spend(self, project_id, env):
        return {"keys": [{"alias": "hr-assistant-dev-claude-sonnet-5-5", "spend": 0.001}]}

    def update_key(self, project_id, env, model_alias, max_budget):
        self.updated.append(max_budget)
        return {
            "alias": "hr-assistant-dev-claude-sonnet-5-5",
            "maxBudget": max_budget,
            "budgetDuration": "7d",
            "status": "active",
        }


class FakeSecrets:
    def list_secrets(self, Filters, MaxResults):
        return {
            "SecretList": [
                {
                    "Name": "bedrock-agentcore-identity!default/apikey/litellm-hr-assistant-dev-claude-sonnet-5-5-078f",
                    "ARN": "arn:secret",
                }
            ]
        }

    def get_secret_value(self, SecretId):
        assert SecretId == "arn:secret"
        return {"SecretString": '{"api_key_value": "sk-test-key"}'}


class RefusingProxy:
    def __init__(self):
        self.calls = []

    def chat_completion(self, api_key, model, messages, **kwargs):
        self.calls.append((api_key, model))
        raise litellm.LiteLLMError(
            400, "Budget has been exceeded! Current cost: 0.001, Max budget: 0.0005", type="budget_exceeded"
        )


def make_ctx(admin):
    return SimpleNamespace(
        admin=admin,
        model_alias="claude-sonnet-5-5",
        project_id="hr-assistant",
        inputs=lambda module: {"key_cap": {"usd": 1.0, "period": "7d"}},
        ui_url="https://ui.example.test",
        names=lambda env: names.ProjectNames("hr-assistant", env),
        cfg=SimpleNamespace(region="us-east-1"),
        region="us-east-1",
    )


def test_dev_key_from_vault_reads_the_api_key_value():
    ctx = make_ctx(FakeAdmin())
    key = tools.dev_key_from_vault(ctx, "litellm-hr-assistant-dev-claude-sonnet-5-5", secrets_client=FakeSecrets())
    assert key == "sk-test-key"


def test_cap_low_lowers_the_cap_and_shows_the_refusal(monkeypatch, capsys):
    admin, proxy = FakeAdmin(), RefusingProxy()
    monkeypatch.setattr(tools, "dev_key_from_vault", lambda ctx, provider, secrets_client=None: "sk-test-key")
    monkeypatch.setattr(litellm, "admin_client", lambda cfg, master_key, **kw: proxy)
    monkeypatch.setattr(litellm, "read_master_key", lambda cfg, secrets_client=None: "sk-master")
    assert tools.cap(make_ctx(admin), "low") == 0
    assert admin.updated == [0.0005]  # half the spend so far
    assert proxy.calls == [("sk-test-key", "claude-sonnet-5-5")]
    out = capsys.readouterr().out
    assert "BUDGET_EXCEEDED, gracefully" in out and "workshop cap --dev restore" in out


def test_cap_restore_puts_the_record_cap_back(monkeypatch, capsys):
    admin = FakeAdmin()
    assert tools.cap(make_ctx(admin), "restore") == 0
    assert admin.updated == [1.0]
    assert "BUDGET_EXCEEDED" not in capsys.readouterr().out
