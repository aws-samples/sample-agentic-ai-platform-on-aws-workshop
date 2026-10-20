"""Tool handler tests (moto). The three tables are created exactly as the Scenario stack creates them and seeded from
scenario/data/*.json, then every tool path is exercised through the Lambda ``handler`` entry point."""

from __future__ import annotations

import decimal
import importlib.util
import json
import pathlib
import types

import boto3
import pytest
from moto import mock_aws

SCENARIO = pathlib.Path(__file__).resolve().parents[1]
DATA = SCENARIO / "data"
REGION = "us-east-1"
TABLE_NAMES = {
    "employees": "scenario-employees",
    "leave_requests": "scenario-leave-requests",
    "invoices": "scenario-invoices",
}


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_rows(name: str) -> list[dict]:
    # DynamoDB rejects Python floats: parse numbers as Decimal (the Scenario stack's seeder must do the same)
    return json.loads((DATA / f"{name}.json").read_text(), parse_float=decimal.Decimal)


def _create_tables(dynamodb) -> None:
    """Same key schema as the Scenario stack: employees PK employee_id; leave_requests PK request_id + GSI by_employee;
    invoices PK invoice_id."""
    dynamodb.create_table(
        TableName=TABLE_NAMES["employees"],
        KeySchema=[{"AttributeName": "employee_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "employee_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=TABLE_NAMES["leave_requests"],
        KeySchema=[{"AttributeName": "request_id", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "request_id", "AttributeType": "S"},
            {"AttributeName": "employee_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "by_employee",
                "KeySchema": [{"AttributeName": "employee_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    dynamodb.create_table(
        TableName=TABLE_NAMES["invoices"],
        KeySchema=[{"AttributeName": "invoice_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "invoice_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    for name, table_name in TABLE_NAMES.items():
        table = dynamodb.Table(table_name)
        with table.batch_writer() as batch:
            for row in _seed_rows(name):
                batch.put_item(Item=row)


class GatewayContext:
    """Minimal stand-in for the Lambda context the gateway provides."""

    def __init__(self, tool_name: str):
        self.client_context = types.SimpleNamespace(custom={"bedrockAgentCoreToolName": tool_name})


@pytest.fixture()
def tables(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("EMPLOYEES_TABLE", TABLE_NAMES["employees"])
    monkeypatch.setenv("LEAVE_REQUESTS_TABLE", TABLE_NAMES["leave_requests"])
    monkeypatch.setenv("INVOICES_TABLE", TABLE_NAMES["invoices"])
    monkeypatch.delenv("HOLIDAYS_JSON", raising=False)
    with mock_aws():
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        _create_tables(dynamodb)
        yield dynamodb


@pytest.fixture()
def hr(tables):
    module = _load("hr_handler", SCENARIO / "tools" / "hr" / "handler.py")
    module._reset_clients()
    return module


@pytest.fixture()
def finance(tables):
    module = _load("finance_handler", SCENARIO / "tools" / "finance" / "handler.py")
    module._reset_clients()
    return module


def call(module, tool: str, **args):
    """Invoke through the gateway form <target>___<tool> and check the result is JSON-serialisable."""
    target = "fintools" if tool == "get_invoice_status" else "hrtools"
    result = module.handler(args, GatewayContext(f"{target}___{tool}"))
    json.dumps(result)
    return result


# ----------------------------------------------------------------------------- dispatch
def test_dispatch_from_gateway_client_context(hr):
    result = call(hr, "get_leave_balance", employee_id="E1001")
    assert result["annual_leave_balance"] == 12 and result["sick_leave_balance"] == 5


def test_dispatch_falls_back_to_event_tool_for_local_calls(hr):
    result = hr.handler({"tool": "get_leave_balance", "employee_id": "e1001"}, None)
    assert result["employee_id"] == "E1001" and result["annual_leave_balance"] == 12


def test_unknown_tool_returns_error(hr):
    result = hr.handler({"employee_id": "E1001"}, GatewayContext("hrtools___delete_everything"))
    assert "error" in result and "delete_everything" in result["error"]


def test_missing_table_configuration_is_reported_not_raised(hr, monkeypatch):
    monkeypatch.delenv("EMPLOYEES_TABLE")
    result = call(hr, "get_leave_balance", employee_id="E1001")
    assert "error" in result and "EMPLOYEES_TABLE" in result["error"]


# ----------------------------------------------------------------------------- get_employee_profile
def test_profile_happy_path_resolves_manager(hr):
    result = call(hr, "get_employee_profile", employee_id="E1007")
    assert result["name"] == "Emily Johnson"
    assert result["title"] == "Senior Software Engineer" and result["department"] == "Engineering"
    assert result["location"] == "Seattle, WA" and result["hire_date"] == "2021-03-15"
    assert result["manager"] == {"employee_id": "E1006", "name": "Wei Zhang", "title": "VP Engineering"}
    assert "annual_leave_balance" not in result and "sick_leave_balance" not in result


def test_profile_top_of_chain_has_no_manager(hr):
    assert call(hr, "get_employee_profile", employee_id="E1020")["manager"] is None


def test_profile_unknown_employee(hr):
    result = call(hr, "get_employee_profile", employee_id="E9999")
    assert "error" in result and "E9999" in result["error"] and "E1001" in result["error"]


@pytest.mark.parametrize("bad", ["", None, "1001", "Alex Rivera", "E10"])
def test_profile_bad_employee_id(hr, bad):
    result = call(hr, "get_employee_profile", employee_id=bad)
    assert "error" in result and "E1001" in result["error"]


# ----------------------------------------------------------------------------- get_leave_balance
def test_balance_e1001_exact_values_used_by_the_dataset(hr):
    result = call(hr, "get_leave_balance", employee_id="E1001")
    assert result == {
        "employee_id": "E1001",
        "name": "Alex Rivera",
        "annual_leave_balance": 12,
        "sick_leave_balance": 5,
        "unit": "days",
        "pending_requests": [],
    }


def test_balance_half_days_come_back_as_plain_floats(hr):
    result = call(hr, "get_leave_balance", employee_id="E1003")
    assert result["annual_leave_balance"] == 6.5 and isinstance(result["annual_leave_balance"], float)
    assert result["sick_leave_balance"] == 10 and isinstance(result["sick_leave_balance"], int)


def test_balance_lists_pending_requests_via_gsi(hr):
    result = call(hr, "get_leave_balance", employee_id="E1008")
    assert result["pending_requests"] == [
        {
            "request_id": "LR-1012",
            "leave_type": "annual",
            "start_date": "2026-11-23",
            "end_date": "2026-11-25",
            "days": 3,
        }
    ]


def test_balance_unknown_employee(hr):
    assert "error" in call(hr, "get_leave_balance", employee_id="E4242")


# ----------------------------------------------------------------------------- submit_leave_request
def test_submit_happy_path_creates_pending_request(hr, tables):
    result = call(hr, "submit_leave_request", employee_id="E1002", leave_type="annual", start_date="2026-11-16", days=3)
    assert "error" not in result
    assert result["request_id"] == "LR-1021"  # seed ends at LR-1020
    assert result["start_date"] == "2026-11-16" and result["end_date"] == "2026-11-18" and result["days"] == 3
    assert result["status"] == "pending" and result["requires_director_approval"] is False
    assert result["submitted_at"].endswith("Z") and "LR-1021" in result["message"]
    stored = tables.Table(TABLE_NAMES["leave_requests"]).get_item(Key={"request_id": "LR-1021"})["Item"]
    assert stored["employee_id"] == "E1002" and stored["status"] == "pending" and stored["days"] == 3
    # the balance is untouched until approval, but the request now shows as pending
    balance = call(hr, "get_leave_balance", employee_id="E1002")
    assert balance["annual_leave_balance"] == 14
    assert [p["request_id"] for p in balance["pending_requests"]] == ["LR-1021"]


def test_submit_request_ids_increment(hr):
    first = call(hr, "submit_leave_request", employee_id="E1002", leave_type="annual", start_date="2026-11-16", days=1)
    second = call(hr, "submit_leave_request", employee_id="E1007", leave_type="sick", start_date="2026-11-17", days=1)
    assert (first["request_id"], second["request_id"]) == ("LR-1021", "LR-1022")


def test_submit_over_ten_days_requires_director_approval(hr):
    result = call(
        hr, "submit_leave_request", employee_id="E1012", leave_type="annual", start_date="2027-02-01", days=12
    )
    assert "error" not in result
    assert result["requires_director_approval"] is True
    assert result["end_date"] == "2027-02-12" and result["status"] == "pending"
    assert "director approval" in result["message"]


def test_submit_exactly_ten_days_does_not_need_director(hr):
    result = call(
        hr, "submit_leave_request", employee_id="E1010", leave_type="annual", start_date="2026-12-01", days=10
    )
    assert result["requires_director_approval"] is False and result["end_date"] == "2026-12-10"


def test_submit_insufficient_annual_balance(hr, tables):
    result = call(hr, "submit_leave_request", employee_id="E1011", leave_type="annual", start_date="2026-12-01", days=5)
    assert "error" in result and "Insufficient" in result["error"] and "2" in result["error"]
    assert tables.Table(TABLE_NAMES["leave_requests"]).scan(Select="COUNT")["Count"] == 20


def test_submit_sick_and_unpaid_are_not_balance_checked(hr):
    assert "error" not in call(
        hr, "submit_leave_request", employee_id="E1011", leave_type="sick", start_date="2026-12-01", days=5
    )
    assert "error" not in call(
        hr, "submit_leave_request", employee_id="E1025", leave_type="unpaid", start_date="2026-12-01", days=5
    )


def test_submit_accepts_days_as_numeric_string_and_mixed_case_type(hr):
    result = call(
        hr, "submit_leave_request", employee_id="E1002", leave_type="Annual", start_date="2026-11-16", days="3"
    )
    assert "error" not in result and result["days"] == 3 and result["leave_type"] == "annual"


@pytest.mark.parametrize(
    "args, fragment",
    [
        ({"employee_id": "E7777", "leave_type": "annual", "start_date": "2026-11-16", "days": 2}, "Unknown employee"),
        ({"employee_id": "E1002", "leave_type": "vacation", "start_date": "2026-11-16", "days": 2}, "leave_type"),
        ({"employee_id": "E1002", "leave_type": "annual", "start_date": "16/11/2026", "days": 2}, "start_date"),
        ({"employee_id": "E1002", "leave_type": "annual", "start_date": "2026-11-16", "days": 0}, "days"),
        ({"employee_id": "E1002", "leave_type": "annual", "start_date": "2026-11-16", "days": 2.5}, "days"),
        ({"employee_id": "E1002", "leave_type": "annual", "start_date": "2026-11-16", "days": "three"}, "days"),
        ({"employee_id": "E1002", "leave_type": "parental", "start_date": "2026-11-16", "days": 181}, "days"),
        ({"employee_id": "E1002", "leave_type": "annual", "start_date": "2026-11-16"}, "days"),
    ],
)
def test_submit_bad_input_returns_error(hr, tables, args, fragment):
    result = call(hr, "submit_leave_request", **args)
    assert "error" in result and fragment in result["error"]
    assert tables.Table(TABLE_NAMES["leave_requests"]).scan(Select="COUNT")["Count"] == 20


# ----------------------------------------------------------------------------- get_public_holidays
def test_holidays_without_filter_returns_both_years(hr):
    result = call(hr, "get_public_holidays")
    assert result["count"] == 22 and result["years_available"] == [2026, 2027]
    assert all(h["country"] == "US" for h in result["holidays"])


def test_holidays_november_2026_as_used_by_the_dataset(hr):
    result = call(hr, "get_public_holidays", year=2026, month=11)
    assert [(h["date"], h["name"], h["weekday"]) for h in result["holidays"]] == [
        ("2026-11-11", "Veterans Day", "Wednesday"),
        ("2026-11-26", "Thanksgiving Day", "Thursday"),
    ]


def test_holidays_year_only_and_string_arguments(hr):
    assert call(hr, "get_public_holidays", year="2027")["count"] == 11
    assert call(hr, "get_public_holidays", year=2026, month="7")["holidays"][0]["name"] == "Independence Day"


def test_holidays_weekend_dates_have_observed_weekday(hr):
    by_date = {h["date"]: h for h in call(hr, "get_public_holidays")["holidays"]}
    assert by_date["2026-07-04"]["observed"] == "2026-07-03"
    assert by_date["2027-06-19"]["observed"] == "2027-06-18"
    assert by_date["2027-07-04"]["observed"] == "2027-07-05"
    assert by_date["2027-12-25"]["observed"] == "2027-12-24"
    assert by_date["2026-11-26"]["observed"] == "2026-11-26"


def test_holidays_year_without_data(hr):
    result = call(hr, "get_public_holidays", year=2025)
    assert result["holidays"] == [] and "2026-2027" in result["note"]


@pytest.mark.parametrize("args", [{"month": 13}, {"month": 0}, {"year": "twenty-six"}, {"month": "x"}])
def test_holidays_bad_filters(hr, args):
    assert "error" in call(hr, "get_public_holidays", **args)


def test_holidays_path_from_environment(hr, monkeypatch):
    monkeypatch.setenv("HOLIDAYS_JSON", str(DATA / "holidays.json"))
    hr._reset_clients()
    assert call(hr, "get_public_holidays", year=2026)["count"] == 11


def test_bundled_holidays_copy_matches_data_file():
    assert json.loads((SCENARIO / "tools" / "hr" / "holidays.json").read_text()) == json.loads(
        (DATA / "holidays.json").read_text()
    )


# ----------------------------------------------------------------------------- finance tool
def test_invoice_status_happy_path(finance):
    result = call(finance, "get_invoice_status", invoice_id="INV-1003")
    assert result["vendor"] == "Summit Staffing Partners" and result["status"] == "pending"
    assert result["amount"] == 7325.0 and result["currency"] == "USD" and result["due_date"] == "2026-10-30"


def test_invoice_status_normalises_case(finance):
    assert call(finance, "get_invoice_status", invoice_id=" inv-1001 ")["status"] == "paid"


def test_invoice_unknown_and_bad_ids(finance):
    assert "error" in call(finance, "get_invoice_status", invoice_id="INV-9999")
    assert "error" in call(finance, "get_invoice_status", invoice_id="1003")
    assert "error" in call(finance, "get_invoice_status")


def test_invoice_unknown_tool(finance):
    assert "error" in finance.handler({"invoice_id": "INV-1001"}, GatewayContext("fintools___pay_invoice"))
