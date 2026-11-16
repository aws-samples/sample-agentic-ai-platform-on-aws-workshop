"""The HR reference model is one choice across the repository: the CLI fallback, the shipped record, the HR templates, the
independent test runners, the UI smoke scripts and the guides agree on it. Explicit project model selections are preserved.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest
import yaml

from workshop_cli import record

ROOT = Path(__file__).resolve().parents[2]
REFERENCE = "claude-opus-5"
ALTERNATIVE = "claude-sonnet-5"
# participant and facilitator material that names the keys, records and blueprint ref the reference run creates
GUIDES = [
    "docs/facilitator-guide.html",
    "docs/workshop-guide.html",
    "docs/v2/course/module-3-rev.html",
    "docs/v2/course/module-5-rev.html",
    "docs/v2/course/module-6-rev.html",
    "docs/v2/course/arch/budget-layers.drawio",
    "docs/v2/course/arch/budget-layers.svg",
]
REFERENCE_ARTIFACTS = re.compile(
    rf"hr-assistant-(?:dev|prod)-{ALTERNATIVE}\b(?!-)|model/{ALTERNATIVE}\b(?!-)|model-{ALTERNATIVE}\b(?!-)"
    rf"|\[{ALTERNATIVE}, gpt-6-1-sol\]|'{ALTERNATIVE}', 'gpt-6-1-sol'"
)


def _script(name: str, path: Path | None = None):
    spec = importlib.util.spec_from_file_location(f"{name}_defaults", path or ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module while the script executes
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(ROOT / "scripts"))
        sys.modules.pop(spec.name, None)
    return module


def _foundation_models() -> dict:
    sys.path.insert(0, str(ROOT / "foundation"))
    try:
        from foundation import config
    finally:
        sys.path.remove(str(ROOT / "foundation"))
    return {m["alias"]: m for m in config.MODELS}


def test_cli_fallback_selects_the_reference_model():
    m3 = record.inputs({}, 3)
    assert m3["model"] == REFERENCE and m3["models"] == [REFERENCE, "gpt-6-1-sol"]


def test_shipped_record_and_hr_templates_match_the_fallback():
    fallback = record.inputs({}, 3)
    shipped = record.inputs(record.load(ROOT / record.RECORD_FILE), 3)
    assert (shipped["model"], shipped["models"]) == (
        fallback["model"],
        fallback["models"],
    )
    blueprint = yaml.safe_load((ROOT / "scenario/blueprint/blueprint.yaml").read_text())
    project = yaml.safe_load((ROOT / "scenario/blueprint/project.yaml").read_text())
    assert blueprint["spec"]["model"] == {"ref": f"model/{fallback['model']}"}
    assert project["spec"]["models"] == fallback["models"]


@pytest.mark.parametrize("name", ["lifecycle_test", "e2e_dryrun"])
def test_independent_runners_default_to_the_reference_model(name):
    assert _script(name).parse_args([]).model == REFERENCE
    assert _script(name).parse_args(["--model", ALTERNATIVE]).model == ALTERNATIVE


def test_ui_smoke_scripts_expect_the_lifecycle_runner_key():
    helpers = (ROOT / "frontend/e2e/helpers.ts").read_text()
    assert f"`${{LT_PROJECT}}-dev-{REFERENCE}`" in helpers
    smoke = (ROOT / "frontend/scripts/smoke.mjs").read_text()
    assert f"-dev-{ALTERNATIVE}" not in smoke and f"-dev-{REFERENCE}" in smoke


def test_model_descriptions_name_the_reference_default():
    models = _foundation_models()
    assert "default" in models[REFERENCE]["description"]
    assert "default" not in models[ALTERNATIVE]["description"]


@pytest.mark.parametrize("path", GUIDES)
def test_guides_name_the_reference_keys_and_records(path):
    text = (ROOT / path).read_text()
    assert not REFERENCE_ARTIFACTS.findall(text), REFERENCE_ARTIFACTS.findall(text)


def test_explicit_model_selection_is_preserved():
    models = _foundation_models()
    assert models[ALTERNATIVE]["modelId"] == f"us.anthropic.{ALTERNATIVE}"
    template = (ROOT / "foundation/litellm/config.yaml.tpl").read_text()
    assert f"model_name: {ALTERNATIVE}\n" in template
    explicit = {"module_3": {"inputs": {"models": [ALTERNATIVE, "gpt-6-1-sol"], "model": ALTERNATIVE}}}
    assert record.validate(explicit) == []
    assert record.inputs(explicit, 3)["model"] == ALTERNATIVE
    assert ALTERNATIVE in (ROOT / "MODEL_CATALOG.md").read_text()
