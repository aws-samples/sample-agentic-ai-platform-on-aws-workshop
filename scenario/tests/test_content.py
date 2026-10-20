"""Content consistency checks for scenario/: files parse, names agree across tools, dataset and prompts, and the facts
the evaluation dataset expects are present in the data and the policy documents."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import pathlib
import re

import pytest
import yaml

SCENARIO = pathlib.Path(__file__).resolve().parents[1]
DATA, TOOLS, KNOWLEDGE = SCENARIO / "data", SCENARIO / "tools", SCENARIO / "knowledge"
BLUEPRINT, EVALUATION = SCENARIO / "blueprint", SCENARIO / "evaluation"

HR_TOOLS = ("get_employee_profile", "get_leave_balance", "submit_leave_request", "get_public_holidays")
KNOWN_GATEWAY_TOOLS = {f"hrtools___{t}" for t in HR_TOOLS} | {"hrkb___Retrieve"}
EXPECTED_WORDS = (400, 850)  # policy documents; counted as tokens containing a letter or digit


def _json(path: pathlib.Path):
    return json.loads(path.read_text())


def _yaml(path: pathlib.Path):
    return yaml.safe_load(path.read_text())


def _module(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _is_date(value) -> bool:
    try:
        dt.date.fromisoformat(value)
        return True
    except (TypeError, ValueError):
        return False


@pytest.fixture(scope="module")
def employees():
    return _json(DATA / "employees.json")


@pytest.fixture(scope="module")
def dataset():
    return _json(EVALUATION / "hr-assistant.json")


@pytest.fixture(scope="module")
def blueprint():
    return _yaml(BLUEPRINT / "blueprint.yaml")


# ----------------------------------------------------------------------------- files parse
@pytest.mark.parametrize("name", ["employees", "leave_requests", "invoices", "holidays"])
def test_data_files_parse_as_json_lists(name):
    rows = _json(DATA / f"{name}.json")
    assert isinstance(rows, list) and rows and all(isinstance(r, dict) for r in rows)


@pytest.mark.parametrize("path", ["blueprint/blueprint.yaml", "blueprint/project.yaml", "evaluation/thresholds.yaml"])
def test_yaml_files_parse(path):
    assert isinstance(_yaml(SCENARIO / path), dict)


# ----------------------------------------------------------------------------- data integrity
def test_employees(employees):
    ids = [e["employee_id"] for e in employees]
    assert 20 <= len(ids) <= 30 and len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"E\d{4}", i) for i in ids) and ids == sorted(ids) and ids[0] == "E1001"
    departments = {e["department"] for e in employees}
    assert departments == {"HR", "Engineering", "Finance", "Sales", "Marketing", "Operations"}
    for e in employees:
        assert set(e) == {
            "employee_id",
            "name",
            "email",
            "department",
            "title",
            "manager_id",
            "location",
            "hire_date",
            "annual_leave_balance",
            "sick_leave_balance",
        }
        assert e["email"].endswith("@anycompany.example") and _is_date(e["hire_date"])
        assert e["manager_id"] is None or e["manager_id"] in ids
        assert isinstance(e["annual_leave_balance"], int | float) and isinstance(e["sick_leave_balance"], int | float)
        assert e["annual_leave_balance"] >= 0 and e["sick_leave_balance"] >= 0
    assert sum(e["manager_id"] is None for e in employees) == 1  # one top of the chain


def test_leave_requests(employees):
    ids = {e["employee_id"] for e in employees}
    rows = _json(DATA / "leave_requests.json")
    assert 15 <= len(rows) <= 25
    request_ids = [r["request_id"] for r in rows]
    assert len(set(request_ids)) == len(request_ids) and request_ids[0] == "LR-1001"
    for r in rows:
        assert re.fullmatch(r"LR-\d{4}", r["request_id"]) and r["employee_id"] in ids
        assert r["leave_type"] in ("annual", "sick", "parental", "unpaid")
        assert r["status"] in ("approved", "pending", "rejected")
        start, end = dt.date.fromisoformat(r["start_date"]), dt.date.fromisoformat(r["end_date"])
        assert (end - start).days + 1 == r["days"] and r["days"] >= 1
        assert dt.datetime.fromisoformat(r["submitted_at"].replace("Z", "+00:00")).date() <= start
    assert {r["status"] for r in rows} == {"approved", "pending", "rejected"}
    assert not [r for r in rows if r["employee_id"] == "E1001" and r["status"] == "pending"]  # keeps scenario 1 stable
    # the tool rejects annual requests above the balance, so no seeded pending annual request may exceed it
    balance = {e["employee_id"]: e["annual_leave_balance"] for e in employees}
    for r in rows:
        if r["leave_type"] == "annual" and r["status"] == "pending":
            assert r["days"] <= balance[r["employee_id"]], r["request_id"]


def test_invoices():
    rows = _json(DATA / "invoices.json")
    assert len(rows) == 10 and [r["invoice_id"] for r in rows] == [f"INV-{1001 + i}" for i in range(10)]
    for r in rows:
        assert set(r) == {"invoice_id", "vendor", "amount", "currency", "status", "due_date"}
        assert r["currency"] == "USD" and r["status"] in ("paid", "pending", "overdue") and _is_date(r["due_date"])
        assert isinstance(r["amount"], int | float) and r["amount"] > 0


def test_holidays_cover_2026_and_2027_with_correct_observed_days():
    rows = _json(DATA / "holidays.json")
    by_year = {y: [r for r in rows if r["date"].startswith(str(y))] for y in (2026, 2027)}
    assert len(rows) == 22 and all(len(v) == 11 for v in by_year.values())
    for r in rows:
        assert r["country"] == "US" and _is_date(r["date"]) and _is_date(r["observed"]) and r["name"]
        assert dt.date.fromisoformat(r["observed"]).weekday() < 5  # observed on a weekday
    by_date = {r["date"]: r for r in rows}
    assert by_date["2026-11-26"]["name"] == "Thanksgiving Day" and by_date["2027-11-25"]["name"] == "Thanksgiving Day"
    assert by_date["2026-05-25"]["name"] == "Memorial Day" and by_date["2027-05-31"]["name"] == "Memorial Day"
    assert by_date["2026-07-04"]["observed"] == "2026-07-03" and by_date["2027-07-04"]["observed"] == "2027-07-05"
    assert (TOOLS / "hr" / "holidays.json").read_text() == (DATA / "holidays.json").read_text()


# ----------------------------------------------------------------------------- tool schemas vs handlers
@pytest.mark.parametrize("folder, expected_tools", [("hr", set(HR_TOOLS)), ("finance", {"get_invoice_status"})])
def test_schema_tool_names_match_handler_dispatch(folder, expected_tools):
    schema = _json(TOOLS / folder / "schema.json")
    handler = _module(f"{folder}_handler_content", TOOLS / folder / "handler.py")
    assert {t["name"] for t in schema} == set(handler.TOOLS) == expected_tools
    for tool in schema:
        assert set(tool) == {"name", "description", "inputSchema"} and len(tool["description"]) > 40
        if "employee_id" in tool["inputSchema"]["properties"]:
            assert "E1001" in tool["description"]  # helps the model format ids correctly
        input_schema = tool["inputSchema"]
        assert input_schema["type"] == "object"
        for prop in input_schema["properties"].values():
            assert prop["type"] in ("string", "integer", "number", "boolean") and prop["description"]
        assert set(input_schema.get("required", [])) <= set(input_schema["properties"])


def test_finance_tool_is_marked_finance_only():
    assert _json(TOOLS / "finance" / "schema.json")[0]["description"].startswith("FINANCE ONLY")


def test_optional_holiday_parameters_have_no_required_list():
    schema = {t["name"]: t for t in _json(TOOLS / "hr" / "schema.json")}
    assert "required" not in schema["get_public_holidays"]["inputSchema"]
    assert schema["submit_leave_request"]["inputSchema"]["required"] == [
        "employee_id",
        "leave_type",
        "start_date",
        "days",
    ]


# ----------------------------------------------------------------------------- evaluation dataset
def test_dataset_shape_and_trajectory_names(dataset):
    scenarios = dataset["scenarios"]
    assert len(scenarios) == 10
    ids = [s["scenario_id"] for s in scenarios]
    assert len(set(ids)) == 10 and all(re.fullmatch(r"[a-z0-9-]+", i) for i in ids)
    for s in scenarios:
        assert s["turns"] and all(t["input"].strip() and t["expected_response"].strip() for t in s["turns"])
        assert "expected_trajectory" in s and isinstance(s["expected_trajectory"], list)
        assert set(s["expected_trajectory"]) <= KNOWN_GATEWAY_TOOLS, s["scenario_id"]
        assert s["assertions"] and all(isinstance(a, str) and a.strip() for a in s["assertions"])
        assert all(len(t["expected_response"]) < 400 for t in s["turns"])  # concise
    categories = [s["metadata"]["category"] for s in scenarios]
    assert categories.count("policy") == 3 and categories.count("submission") == 2 and categories.count("lookup") == 2
    assert sum(1 for s in scenarios if s["expected_trajectory"] == ["hrkb___Retrieve"]) == 3
    assert sum(1 for s in scenarios if s["expected_trajectory"] == []) == 2


def test_dataset_loads_with_the_agentcore_dataset_provider():
    providers = pytest.importorskip("bedrock_agentcore.evaluation.runner.dataset_providers")
    ds = providers.FileDatasetProvider(str(EVALUATION / "hr-assistant.json")).get_dataset()
    assert len(ds.scenarios) == 10 and all(s.turns for s in ds.scenarios)


def test_dataset_facts_match_the_seed_data(dataset, employees):
    by_id = {s["scenario_id"]: s for s in dataset["scenarios"]}
    emp = {e["employee_id"]: e for e in employees}
    balance = by_id["leave-balance-e1001"]["turns"][0]["expected_response"]
    assert f"{emp['E1001']['annual_leave_balance']} days of annual leave" in balance
    assert f"{emp['E1001']['sick_leave_balance']} days of sick leave" in balance
    profile = by_id["employee-profile-e1007"]["turns"][0]["expected_response"]
    assert emp["E1007"]["name"] in profile and emp["E1007"]["title"] in profile
    assert emp[emp["E1007"]["manager_id"]]["name"] in profile
    # 12 days > the module 4 Cedar forbid (days > 10): the gateway denies the call, the agent must say it was not submitted
    twelve = by_id["submit-leave-12-days-blocked-by-policy"]
    assert "12 days" in twelve["turns"][0]["input"] and emp["E1012"]["annual_leave_balance"] >= 12
    assert "director approval" in twelve["turns"][0]["expected_response"]
    assert "not submitted" in twelve["turns"][0]["expected_response"]
    three = by_id["submit-leave-3-days"]
    assert emp["E1002"]["annual_leave_balance"] >= 3 and emp["E1002"]["name"] in three["turns"][0]["expected_response"]
    holidays = {h["date"]: h["name"] for h in _json(DATA / "holidays.json")}
    november = by_id["public-holidays-november-2026"]["turns"][0]["expected_response"]
    assert holidays["2026-11-11"] in november and holidays["2026-11-26"] in november
    invoice_ids = {i["invoice_id"] for i in _json(DATA / "invoices.json")}
    assert "INV-1003" in invoice_ids and "INV-1003" in by_id["out-of-scope-invoice-status"]["turns"][0]["input"]


@pytest.mark.parametrize(
    "scenario_id, document, facts",
    [
        ("policy-remote-work-days", "remote-work-policy.md", ["3 days per week", "Tuesday"]),
        ("policy-annual-leave-accrual-carry-over", "leave-policy.md", ["20 days", "5 unused", "31 March"]),
        ("policy-parental-leave-weeks", "parental-leave-policy.md", ["16 weeks", "6 weeks", "12 months"]),
    ],
)
def test_policy_answers_are_in_the_documents(dataset, scenario_id, document, facts):
    scenario = next(s for s in dataset["scenarios"] if s["scenario_id"] == scenario_id)
    text = (KNOWLEDGE / "hr" / document).read_text()
    expected = scenario["turns"][0]["expected_response"]
    for fact in facts:
        assert fact in text and fact in expected, (scenario_id, fact)
    title = _json(KNOWLEDGE / "hr" / f"{document}.metadata.json")["metadataAttributes"]["title"]
    assert title in expected  # the citation the v2 prompt asks for


# ----------------------------------------------------------------------------- knowledge documents
def test_every_document_has_a_metadata_sidecar_and_sensible_length():
    docs = sorted(KNOWLEDGE.glob("*/*.md"))
    assert [d.name for d in docs if d.parent.name == "hr"] == sorted(
        [
            "leave-policy.md",
            "remote-work-policy.md",
            "code-of-conduct.md",
            "parental-leave-policy.md",
            "learning-and-development-policy.md",
        ]
    )
    assert [d.name for d in docs if d.parent.name == "finance"] == ["expense-policy.md"]
    for doc in docs:
        sidecar = _json(doc.with_name(doc.name + ".metadata.json"))
        attrs = sidecar["metadataAttributes"]
        assert attrs["department"] == ("HR" if doc.parent.name == "hr" else "Finance")
        assert attrs["doc_type"] == "policy" and attrs["title"].startswith("AnyCompany")
        words = [w for w in re.split(r"\s+", doc.read_text()) if re.search(r"[A-Za-z0-9]", w)]
        assert EXPECTED_WORDS[0] <= len(words) <= EXPECTED_WORDS[1], (doc.name, len(words))
    assert len(list(KNOWLEDGE.glob("*/*.metadata.json"))) == len(docs)


def test_policy_documents_agree_with_the_tool_and_the_cedar_forbid():
    """Long requests: the module 4 policy forbids submit_leave_request over 10 days, so the documents must not promise that the
    assistant files them (leave policy) or that parental leave is filed through the assistant; half days are not bookable."""
    leave = (KNOWLEDGE / "hr" / "leave-policy.md").read_text()
    parental = (KNOWLEDGE / "hr" / "parental-leave-policy.md").read_text()
    assert "The HR assistant does not submit requests longer than 10 days" in leave
    assert "flags these requests automatically" not in leave
    assert "whole days" in leave and "smallest unit you can book is **half a day**" not in leave
    assert "People Operations files the parental leave request" in parental
    assert 'Choose the leave type "parental"' not in parental


def test_finance_document_carries_the_numbers_used_in_the_filter_demo():
    text = (KNOWLEDGE / "finance" / "expense-policy.md").read_text()
    assert "60 USD" in text and "25 USD" in text and "30 days" in text


# ----------------------------------------------------------------------------- prompts and blueprint
def test_prompt_v2_adds_the_policy_search_instruction_and_v1_lacks_it():
    v1 = (BLUEPRINT / "prompts" / "hr-assistant-v1.md").read_text()
    v2 = (BLUEPRINT / "prompts" / "hr-assistant-v2.md").read_text()
    assert "hrkb___Retrieve" in v2 and "ALWAYS" in v2 and "cite the document title" in v2
    for forbidden in ("hrkb", "knowledge base", "policy search", "Retrieve"):
        assert forbidden not in v1, forbidden
    assert "general knowledge" in v1 and "do not call any tool for these questions" in v1  # the seeded regression
    # v2 is v1 with the body of the "Company policy questions" section replaced; every other section is identical
    sections_v1 = {s.split("\n", 1)[0]: s for s in v1.split("\n## ")}
    sections_v2 = {s.split("\n", 1)[0]: s for s in v2.split("\n## ")}
    assert set(sections_v1) == set(sections_v2) and "Company policy questions" in sections_v1
    for heading in sections_v1:
        if heading == "Company policy questions":
            assert sections_v1[heading] != sections_v2[heading]
        else:
            assert sections_v1[heading] == sections_v2[heading], heading
    assert "E1001" in v1 and "Never invent data" in v1 and "Finance" in v1


def test_blueprint_matches_the_contract(blueprint):
    assert blueprint["apiVersion"] == "platform/v1" and blueprint["kind"] == "AgentBlueprint"
    assert blueprint["metadata"] == {"domain": "people-ops", "project": "hr-assistant", "version": 1}
    spec = blueprint["spec"]
    assert spec["model"] == {"ref": "model/claude-sonnet-5"}
    assert spec["systemPrompt"] == {"file": "prompts/hr-assistant-v1.md"}
    assert (BLUEPRINT / spec["systemPrompt"]["file"]).is_file()
    assert spec["tools"] == [{"ref": "tool/hr-core"}] and spec["knowledge"] == [{"ref": "kb/hr-policies"}]
    assert spec["memory"] == {"strategies": ["SUMMARIZATION", "USER_PREFERENCE"]}
    assert spec["limits"] == {"maxIterations": 30, "maxTokens": 4000, "timeoutSeconds": 120}
    assert spec["budget"] == {"perModelCap": 1.0, "resetPeriod": "7d"}
    assert spec["evaluation"]["dataset"] == "evaluation/hr-assistant.json"
    assert (SCENARIO / spec["evaluation"]["dataset"]).is_file()
    thresholds = _yaml(EVALUATION / "thresholds.yaml")
    assert (
        spec["evaluation"]["thresholds"]
        == thresholds["thresholds"]
        == {"Correctness": 0.8, "GoalSuccessRate": 0.9, "TrajectoryInOrderMatch": 0.9, "Harmfulness": 0.0}
    )
    assert thresholds["evaluators"] == [f"Builtin.{k}" for k in thresholds["thresholds"]]


def test_project_yaml_matches_the_blueprint(blueprint):
    project = _yaml(BLUEPRINT / "project.yaml")
    assert project["kind"] == "Project" and project["metadata"] == {"domain": "people-ops", "project": "hr-assistant"}
    spec = project["spec"]
    assert spec["models"] == ["claude-sonnet-5", "gpt-6-1-sol"]
    assert blueprint["spec"]["model"]["ref"].split("/")[1] in spec["models"]
    assert spec["caps"] == {"maxBudget": 5, "budgetDuration": "7d", "tpmLimit": 200000, "rpmLimit": 200}
    assert spec["description"] and spec["owner"].endswith("@anycompany.example")
    hr_core = spec["tools"]["hr-core"]
    assert (
        hr_core["lambdaArn"] == "ssm:/agent-platform/scenario/tools/hr-lambda-arn"
        and hr_core["gatewayTarget"] == "hrtools"
    )
    assert (BLUEPRINT / hr_core["schema"]).resolve() == (TOOLS / "hr" / "schema.json").resolve()
    kb = spec["knowledge"]["hr-policies"]
    assert kb["knowledgeBaseId"] == "ssm:/agent-platform/scenario/kb-id" and kb["gatewayTarget"] == "hrkb"
    assert kb["filter"] == {"equals": {"key": "department", "value": "HR"}}
    # every blueprint ref resolves to a project entry
    assert {t["ref"].split("/")[1] for t in blueprint["spec"]["tools"]} <= set(spec["tools"])
    assert {k["ref"].split("/")[1] for k in blueprint["spec"]["knowledge"]} <= set(spec["knowledge"])


# ----------------------------------------------------------------------------- risk register and README
def test_risk_register_has_ten_rows_with_seven_columns():
    lines = [line for line in (SCENARIO / "risk-register.md").read_text().splitlines() if line.startswith("| R-")]
    assert len(lines) == 10
    for line in lines:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        assert len(cells) == 7 and all(cells), line[:40]
        assert cells[2] in ("Low", "Medium", "High") and cells[3] in ("Low", "Medium", "High")
        assert cells[5] in ("Platform admin", "Agent developer", "Approver")
    text = (SCENARIO / "risk-register.md").read_text().lower()
    for topic in (
        "hallucinat",
        "pii",
        "prompt injection",
        "over-permissioned",
        "cost",
        "drift",
        "unapproved model",
        "residency",
        "blind spot",
        "approval bypass",
    ):
        assert topic in text, topic


def test_readme_covers_the_storyline_and_tests():
    text = (SCENARIO / "README.md").read_text()
    for needle in (
        "hr-assistant-v1.md",
        "hr-assistant-v2.md",
        "hrkb___Retrieve",
        "uv run pytest scenario/tests",
        "by_employee",
        "EMPLOYEES_TABLE",
        "bedrockAgentCoreToolName",
        "risk-register.md",
    ):
        assert needle in text, needle
