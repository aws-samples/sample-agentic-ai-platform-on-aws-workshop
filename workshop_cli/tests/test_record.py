from pathlib import Path

import pytest
import yaml

from workshop_cli import record


def test_defaults_build_the_reference_platform():
    assert record.inputs({}, 3)["project"] == "hr-assistant"
    assert record.inputs({}, 4)["knowledge_filter"] == {"department": "HR"}
    assert record.inputs({}, 6)["prompt_version"] == "v1"
    assert record.validate({}) == []


def test_shipped_template_is_valid():
    path = Path(__file__).resolve().parents[2] / "design-record.yaml"
    document = record.load(path)
    assert record.validate(document) == [], record.validate(document)
    assert record.inputs(document, 3)["budget"]["usd"] == 5
    assert record.decisions(document, 1)["accounts"]["workshop"] == "one-account"


def test_partial_inputs_merge_with_defaults():
    document = {"module_3": {"inputs": {"project": "people-helper", "budget": {"usd": 2}}}}
    merged = record.inputs(document, 3)
    assert merged["project"] == "people-helper"
    assert merged["budget"] == {"usd": 2, "period": "7d", "tpm": 200000, "rpm": 200}
    assert merged["domain"] == "people-ops"


@pytest.mark.parametrize(
    "document, fragment",
    [
        ({"module_3": {"inputs": {"project": "HR Assistant"}}}, "module_3.inputs.project"),
        ({"module_3": {"inputs": {"project": "invoice-helper"}}}, "fixed Finance names"),
        ({"module_3": {"inputs": {"model": "gpt-9"}}}, "module_3.inputs.model"),
        ({"module_3": {"inputs": {"budget": {"period": "weekly"}}}}, "budget.period"),
        ({"module_4": {"inputs": {"gateway_mode": "AUDIT"}}}, "gateway_mode"),
        ({"module_4": {"inputs": {"max_leave_days": 0}}}, "max_leave_days"),
        ({"module_4": {"inputs": {"knowledge_filter": {"team": "HR"}}}}, "knowledge_filter"),
        ({"module_5": {"inputs": {"key_cap": {"usd": -1}}}}, "key_cap.usd"),
        ({"module_6": {"inputs": {"prompt_version": "v3"}}}, "prompt_version"),
        ({"module_6": {"inputs": {"thresholds": {"Correctness": 2}}}}, "thresholds.Correctness"),
        ({"module_6": {"inputs": {"export": "crewai"}}}, "export"),
        ({"module_7": {"inputs": {"prod_traffic": 500}}}, "prod_traffic"),
    ],
)
def test_validation_names_the_field(document, fragment):
    problems = record.validate(document)
    assert problems and any(fragment in p for p in problems), problems


def test_inputs_none_means_defaults(tmp_path):
    path = tmp_path / "design-record.yaml"
    path.write_text(yaml.safe_dump({"module_3": {"inputs": "none"}, "module_4": {"inputs": None}}))
    document = record.load(path)
    assert record.inputs(document, 3)["project"] == "hr-assistant"
    assert record.inputs(document, 4)["max_leave_days"] == 10


def test_describe_marks_origin():
    text = record.describe({"module_5": {"inputs": {"key_cap": {"usd": 0.5}}}})
    assert "key_cap" in text and "(record)" in text and "(default)" in text


def test_missing_file_is_empty(tmp_path):
    assert record.load(tmp_path / "nope.yaml") == {}
