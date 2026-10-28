"""`scripts/doctor.py` summary: the corporate IdP (module 2) lines, with no AWS calls and no passwords."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from rich.console import Console

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "doctor.py"


@pytest.fixture(scope="module")
def doctor():
    spec = importlib.util.spec_from_file_location("doctor_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["doctor_script"] = module
    spec.loader.exec_module(module)
    return module


class FakeConfig:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, key: str):
        return self.values.get(key)


BASE = {
    "ui/url": "https://ui.example",
    "api/url": "https://api.example",
    "auth/domain": "https://auth.example",
}


def _render(doctor, monkeypatch, values: dict[str, str]) -> str:
    console = Console(record=True, width=400)
    monkeypatch.setattr(doctor, "console", console)
    doctor.render(doctor.Report(), FakeConfig({**BASE, **values}))
    return console.export_text()


def test_render_prints_idp_url_and_admin_console(doctor, monkeypatch):
    out = _render(
        doctor,
        monkeypatch,
        {
            "idp/url": "https://kc.example",
            "idp/realm": "anycompany",
            "idp/admin-secret-arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:agent-platform/keycloak-admin",
        },
    )
    assert "Keycloak IdP URL: https://kc.example (realm anycompany)" in out
    assert "Keycloak admin: https://kc.example/admin/master/console/#/anycompany/clients" in out
    assert "agent-platform/keycloak-admin" in out


def test_render_idp_before_module_2(doctor, monkeypatch):
    out = _render(doctor, monkeypatch, {})
    assert "Keycloak IdP URL: not deployed (module 2: workshop apply 2)" in out
    assert "Keycloak admin" not in out


def test_render_idp_realm_defaults_to_anycompany(doctor, monkeypatch):
    out = _render(doctor, monkeypatch, {"idp/url": "https://kc.example/"})
    assert "Keycloak admin: https://kc.example/admin/master/console/#/anycompany/clients" in out
