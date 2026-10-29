"""`scripts/doctor.py` probes the model the participant chose (`module_3.inputs.model`), with no AWS access in these tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml
from rich.console import Console

from workshop_cli import record

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "doctor.py"
DEFAULT_MODEL = record.DEFAULTS["module_3"]["model"]
PROBES = (
    "LiteLLM chat completion",
    "Guardrail blocks legal advice",
    "Guardrail blocks prompt attack",
)


@pytest.fixture
def doctor(monkeypatch):
    spec = importlib.util.spec_from_file_location("doctor_model_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["doctor_model_script"] = module
    spec.loader.exec_module(module)
    monkeypatch.delenv("WORKSHOP_RECORD", raising=False)
    monkeypatch.setattr(module, "console", Console(record=True, width=400))
    return module


class FakeConfig:
    """Every SSM parameter present except the scenario's (or the landing zone's, when `landing_zone` is False)."""

    region = "us-east-1"
    api_url = "https://api.example"
    ui_url = "https://ui.example"
    model_gateway_url = "https://gateway.example"
    model_gateway_private = True
    model_gateway_relay_function_name = "relay"
    model_gateway_master_key_secret_arn = "arn:aws:secretsmanager:us-east-1:111122223333:secret:master"
    users_secret_arn = "arn:aws:secretsmanager:us-east-1:111122223333:secret:users"
    guardrail_version = "1"
    approved_models = [{"alias": "claude-sonnet-5"}, {"alias": "gpt-6-1-sol"}]

    def __init__(self, landing_zone: bool = True) -> None:
        self.landing_zone = landing_zone

    def get(self, key: str):
        if key.startswith("scenario/"):
            return None
        if not self.landing_zone and key in ("model-gateway/url", "guardrail/id"):
            return None
        return "x"

    def __getattr__(self, name):
        return "x"


class FakeAdmin:
    transport = "relay"
    last_response_headers: dict = {}

    def __init__(self) -> None:
        self.models_called: list[str] = []

    def health(self):
        return {"status": "ok"}

    def models(self):
        return ["claude-sonnet-5", "gpt-6-1-sol"]

    def chat_completion(self, key, model, messages, max_tokens):
        self.models_called.append(model)
        return {
            "choices": [
                {
                    "finish_reason": "content_filter",
                    "message": {"content": "Sorry, I can't help with that topic"},
                }
            ]
        }


class NoAws:
    def __init__(self, *args, **kwargs) -> None:
        self.region_name = "us-east-1"

    def client(self, *args, **kwargs):
        return NoAwsClient()


class NoAwsClient:
    def __getattr__(self, name):
        def call(*args, **kwargs):
            raise RuntimeError("no AWS in tests")

        return call


def _wire(doctor, monkeypatch, landing_zone: bool = True) -> FakeAdmin:
    admin = FakeAdmin()
    monkeypatch.setattr(doctor.platform_config, "load", lambda: FakeConfig(landing_zone))
    monkeypatch.setattr(doctor.boto3, "Session", NoAws)
    monkeypatch.setattr(doctor, "secret_string", lambda session, arn: "sk-test")
    monkeypatch.setattr(doctor.platform_litellm, "admin_client", lambda cfg, key, timeout: admin)
    monkeypatch.setattr(
        doctor.requests,
        "get",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no HTTP")),
    )
    return admin


def _write(
    path: Path,
    model: str | None,
    models: list[str] | None = None,
    extra: dict | None = None,
) -> Path:
    inputs = {} if model is None else {"model": model}
    if models is not None:
        inputs["models"] = models
    document = {"module_3": {"inputs": inputs}, **(extra or {})}
    path.write_text(yaml.safe_dump(document))
    return path


def _labels(doctor) -> str:
    return doctor.console.export_text()


def test_probes_use_the_configured_alias_and_leave_the_record_alone(doctor, monkeypatch, tmp_path):
    path = _write(
        tmp_path / "design-record.yaml",
        "gpt-6-1-sol",
        ["claude-sonnet-5", "gpt-6-1-sol"],
    )
    before = path.read_bytes()
    admin = _wire(doctor, monkeypatch)
    doctor.main(["--record", str(path)])
    assert admin.models_called == ["gpt-6-1-sol"] * 3
    out = _labels(doctor)
    for probe in PROBES:
        assert f"{probe} (gpt-6-1-sol)" in out
    assert "claude-sonnet-5)" not in out
    assert path.read_bytes() == before


def test_missing_record_uses_the_loader_default(doctor, monkeypatch, tmp_path):
    admin = _wire(doctor, monkeypatch)
    doctor.main(["--record", str(tmp_path / "absent.yaml")])
    assert admin.models_called == [DEFAULT_MODEL] * 3


def test_missing_model_field_uses_the_loader_default(doctor, monkeypatch, tmp_path):
    path = _write(tmp_path / "design-record.yaml", None)
    admin = _wire(doctor, monkeypatch)
    doctor.main(["--record", str(path)])
    assert admin.models_called == [DEFAULT_MODEL] * 3


def test_workshop_record_env_applies_and_record_option_wins(doctor, monkeypatch, tmp_path):
    env_path = _write(tmp_path / "env.yaml", "gpt-6-1-sol", ["claude-sonnet-5", "gpt-6-1-sol"])
    arg_path = _write(tmp_path / "arg.yaml", "claude-sonnet-5", ["claude-sonnet-5", "gpt-6-1-sol"])
    monkeypatch.setenv("WORKSHOP_RECORD", str(env_path))
    admin = _wire(doctor, monkeypatch)
    doctor.main([])
    assert admin.models_called == ["gpt-6-1-sol"] * 3
    admin.models_called.clear()
    doctor.main(["--record", str(arg_path)])
    assert admin.models_called == ["claude-sonnet-5"] * 3


def test_skipped_probes_name_the_alias(doctor, monkeypatch, tmp_path):
    path = _write(
        tmp_path / "design-record.yaml",
        "gpt-6-1-sol",
        ["claude-sonnet-5", "gpt-6-1-sol"],
    )
    admin = _wire(doctor, monkeypatch, landing_zone=False)
    doctor.main(["--record", str(path)])
    assert admin.models_called == []
    out = _labels(doctor)
    for probe in PROBES:
        assert f"{probe} (gpt-6-1-sol)" in out


def test_unrelated_record_problems_do_not_block_the_probes(doctor, monkeypatch, tmp_path):
    path = _write(
        tmp_path / "design-record.yaml",
        "gpt-6-1-sol",
        ["claude-sonnet-5", "gpt-6-1-sol"],
        extra={"module_6": {"inputs": {"prompt_version": "v9"}}},
    )
    admin = _wire(doctor, monkeypatch)
    doctor.main(["--record", str(path)])
    assert admin.models_called == ["gpt-6-1-sol"] * 3


def test_invalid_model_fails_before_any_aws_call(doctor, monkeypatch, tmp_path):
    path = _write(tmp_path / "design-record.yaml", "gpt-9", ["claude-sonnet-5"])
    monkeypatch.setattr(
        doctor.platform_config,
        "load",
        lambda: pytest.fail("AWS configuration was read"),
    )
    assert doctor.main(["--record", str(path)]) == 1
    out = _labels(doctor)
    assert "module_3.inputs.model" in out and str(path) in out


def test_unparsable_record_fails_before_aws_without_echoing_its_content(doctor, monkeypatch, tmp_path):
    path = tmp_path / "design-record.yaml"
    path.write_text("module_3:\n  inputs: {model: sk-not-a-secret-but-private\n")
    monkeypatch.setattr(
        doctor.platform_config,
        "load",
        lambda: pytest.fail("AWS configuration was read"),
    )
    assert doctor.main(["--record", str(path)]) == 1
    out = _labels(doctor)
    assert str(path) in out and "sk-not-a-secret-but-private" not in out


@pytest.mark.parametrize(
    "extra",
    [
        {"module_6": {"inputs": {"thresholds": "bad-string"}}},
        {"module_5": {"inputs": {"key_cap": "bad-string"}}},
        {
            "module_3": {
                "inputs": {"model": "gpt-6-1-sol", "models": ["claude-sonnet-5", "gpt-6-1-sol"], "budget": "bad"}
            }
        },
    ],
)
def test_malformed_unrelated_fields_never_block_the_probes(doctor, monkeypatch, tmp_path, extra):
    path = _write(tmp_path / "design-record.yaml", "gpt-6-1-sol", ["claude-sonnet-5", "gpt-6-1-sol"], extra=extra)
    admin = _wire(doctor, monkeypatch)
    doctor.main(["--record", str(path)])
    assert admin.models_called == ["gpt-6-1-sol"] * 3


def test_default_record_is_the_repository_root_one_from_any_cwd(doctor, monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _write(root / record.RECORD_FILE, "gpt-6-1-sol", ["claude-sonnet-5", "gpt-6-1-sol"])
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(doctor, "REPO_ROOT", root)
    admin = _wire(doctor, monkeypatch)
    doctor.main([])
    assert admin.models_called == ["gpt-6-1-sol"] * 3


def test_unreadable_record_fails_locally_without_a_traceback(doctor, monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.platform_config, "load", lambda: pytest.fail("AWS configuration was read"))
    assert doctor.main(["--record", str(tmp_path)]) == 1  # a directory: reading it raises OSError
    out = _labels(doctor)
    assert str(tmp_path) in out and "cannot read" in out
