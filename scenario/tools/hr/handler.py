"""AnyCompany HR tools: one Lambda function behind an AgentCore Gateway target, four tools.

Contract (build-contracts.md section 8): the gateway invokes the function with the tool's input object as the event and
sets ``context.client_context.custom["bedrockAgentCoreToolName"]`` to ``<target>___<tool>`` (for example
``hrtools___get_leave_balance``). The handler dispatches on the part after ``___``; local tests may pass the name as
``event["tool"]`` instead. Every tool returns a JSON-serialisable dict; problems (unknown employee, bad input) come back
as ``{"error": "<message>"}`` so the agent can explain them instead of the call failing.

Environment: ``EMPLOYEES_TABLE``, ``LEAVE_REQUESTS_TABLE`` (GSI ``by_employee`` on ``employee_id``) and ``HOLIDAYS_JSON``
(path of the bundled holiday file; defaults to ``holidays.json`` next to this module). Standard library + boto3 only.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import os
import pathlib
import re
import traceback

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

LEAVE_TYPES = ("annual", "sick", "parental", "unpaid")
MAX_DAYS = 180  # longest single request the tool accepts (parental leave is up to 16 weeks)
DIRECTOR_APPROVAL_DAYS = 10  # leave policy: requests over 10 consecutive days need director approval
EMPLOYEE_ID_RE = re.compile(r"^E\d{4}$")
HERE = pathlib.Path(__file__).resolve().parent

_dynamodb = None
_holidays_cache: list[dict] | None = None


# ----------------------------------------------------------------------------- helpers
def _error(message: str) -> dict:
    return {"error": message}


def _table(env_name: str):
    """DynamoDB table named by an environment variable (resource created lazily so tests can mock it)."""
    global _dynamodb
    name = os.environ.get(env_name)
    if not name:
        raise RuntimeError(f"environment variable {env_name} is not set")
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb.Table(name)


def _reset_clients() -> None:
    """Drop cached AWS clients and data (used by tests between mock sessions)."""
    global _dynamodb, _holidays_cache
    _dynamodb = None
    _holidays_cache = None


def _plain(value):
    """Convert DynamoDB Decimals into ints/floats so the result is JSON-serialisable."""
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def _normalise_employee_id(raw) -> str | None:
    employee_id = str(raw or "").strip().upper()
    return employee_id if EMPLOYEE_ID_RE.match(employee_id) else None


def _employee(raw_id) -> dict:
    """Load one employee or return an error dict."""
    employee_id = _normalise_employee_id(raw_id)
    if employee_id is None:
        return _error(f"employee_id is required and looks like E1001 (got {raw_id!r}).")
    item = _table("EMPLOYEES_TABLE").get_item(Key={"employee_id": employee_id}).get("Item")
    if not item:
        return _error(f"Unknown employee id {employee_id}. Employee ids look like E1001; check the id and try again.")
    return _plain(item)


def _parse_date(raw) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _parse_int(raw) -> int | None:
    """Accept ints, whole floats and digit strings; reject everything else."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str) and raw.strip():
        try:
            value = float(raw.strip())
        except ValueError:
            return None
        return int(value) if value.is_integer() else None
    return None


def _pending_requests(employee_id: str) -> list[dict]:
    """Pending requests for an employee, through the by_employee GSI."""
    table = _table("LEAVE_REQUESTS_TABLE")
    items, kwargs = [], {"IndexName": "by_employee", "KeyConditionExpression": Key("employee_id").eq(employee_id)}
    while True:
        page = table.query(**kwargs)
        items.extend(page.get("Items", []))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    pending = [i for i in items if i.get("status") == "pending"]
    pending.sort(key=lambda i: i.get("start_date", ""))
    keep = ("request_id", "leave_type", "start_date", "end_date", "days")
    return [_plain({k: i.get(k) for k in keep}) for i in pending]


def _next_request_number() -> int:
    """Highest LR-<n> in the table plus one (the table is small; a scan is fine for the workshop)."""
    table = _table("LEAVE_REQUESTS_TABLE")
    highest, kwargs = 1000, {"ProjectionExpression": "request_id"}
    while True:
        page = table.scan(**kwargs)
        for item in page.get("Items", []):
            suffix = str(item.get("request_id", "")).rsplit("-", 1)[-1]
            if suffix.isdigit():
                highest = max(highest, int(suffix))
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return highest + 1


def _holidays() -> list[dict]:
    global _holidays_cache
    if _holidays_cache is None:
        path = pathlib.Path(os.environ.get("HOLIDAYS_JSON") or HERE / "holidays.json")
        _holidays_cache = json.loads(path.read_text())
    return _holidays_cache


# ----------------------------------------------------------------------------- tools
def get_employee_profile(args: dict) -> dict:
    """Who an employee is: name, contact, department, title, location, hire date and manager (no leave data)."""
    emp = _employee(args.get("employee_id"))
    if "error" in emp:
        return emp
    profile = {k: v for k, v in emp.items() if k not in ("annual_leave_balance", "sick_leave_balance")}
    manager_id = emp.get("manager_id")
    if manager_id:
        manager = _table("EMPLOYEES_TABLE").get_item(Key={"employee_id": manager_id}).get("Item") or {}
        profile["manager"] = {"employee_id": manager_id, "name": manager.get("name"), "title": manager.get("title")}
    else:
        profile["manager"] = None
    return _plain(profile)


def get_leave_balance(args: dict) -> dict:
    """Annual and sick leave balances in days, plus any pending requests."""
    emp = _employee(args.get("employee_id"))
    if "error" in emp:
        return emp
    return {
        "employee_id": emp["employee_id"],
        "name": emp.get("name"),
        "annual_leave_balance": emp.get("annual_leave_balance"),
        "sick_leave_balance": emp.get("sick_leave_balance"),
        "unit": "days",
        "pending_requests": _pending_requests(emp["employee_id"]),
    }


def submit_leave_request(args: dict) -> dict:
    """Create a pending leave request; annual leave is checked against the balance, >10 days needs director approval."""
    emp = _employee(args.get("employee_id"))
    if "error" in emp:
        return emp
    leave_type = str(args.get("leave_type") or "").strip().lower()
    if leave_type not in LEAVE_TYPES:
        return _error(f"leave_type must be one of {', '.join(LEAVE_TYPES)} (got {args.get('leave_type')!r}).")
    start = _parse_date(args.get("start_date"))
    if start is None:
        return _error(f"start_date must be an ISO date like 2026-11-16 (got {args.get('start_date')!r}).")
    days = _parse_int(args.get("days"))
    if days is None or not 1 <= days <= MAX_DAYS:
        return _error(f"days must be a whole number between 1 and {MAX_DAYS} (got {args.get('days')!r}).")
    if leave_type == "annual" and days > emp.get("annual_leave_balance", 0):
        return _error(
            f"Insufficient annual leave balance: {emp['employee_id']} requested {days} days but has "
            f"{emp.get('annual_leave_balance', 0)} days available."
        )
    end = start + dt.timedelta(days=days - 1)
    requires_director_approval = days > DIRECTOR_APPROVAL_DAYS
    table = _table("LEAVE_REQUESTS_TABLE")
    number = _next_request_number()
    for _attempt in range(5):  # the conditional put protects against two requests picking the same number
        item = {
            "request_id": f"LR-{number}",
            "employee_id": emp["employee_id"],
            "leave_type": leave_type,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "days": days,
            "status": "pending",
            "submitted_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "requires_director_approval": requires_director_approval,
        }
        try:
            table.put_item(Item=item, ConditionExpression="attribute_not_exists(request_id)")
            break
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            number += 1
    else:
        return _error("Could not allocate a request id; please try again.")
    message = (
        f"Leave request {item['request_id']} submitted for {emp.get('name')} ({emp['employee_id']}): {days} day(s) of "
        f"{leave_type} leave from {item['start_date']} to {item['end_date']}. Status: pending manager approval."
    )
    if requires_director_approval:
        message += (
            f" Because it is longer than {DIRECTOR_APPROVAL_DAYS} consecutive days it also requires director approval."
        )
    return {**item, "message": message}


def get_public_holidays(args: dict) -> dict:
    """US public holidays observed by AnyCompany (2026-2027), optionally filtered by year and month."""
    rows = _holidays()
    years = sorted({int(r["date"][:4]) for r in rows})
    year = month = None
    if args.get("year") not in (None, ""):
        year = _parse_int(args.get("year"))
        if year is None:
            return _error(f"year must be a four-digit number like 2026 (got {args.get('year')!r}).")
    if args.get("month") not in (None, ""):
        month = _parse_int(args.get("month"))
        if month is None or not 1 <= month <= 12:
            return _error(f"month must be a number from 1 to 12 (got {args.get('month')!r}).")
    result = {"country": "US", "year": year, "month": month, "years_available": years}
    if year is not None and year not in years:
        return {
            **result,
            "holidays": [],
            "count": 0,
            "note": f"No holiday data for {year}; data covers {years[0]}-{years[-1]}.",
        }
    selected = []
    for r in rows:
        y, m = int(r["date"][:4]), int(r["date"][5:7])
        if (year is None or y == year) and (month is None or m == month):
            selected.append({**r, "weekday": dt.date.fromisoformat(r["date"]).strftime("%A")})
    return {**result, "count": len(selected), "holidays": selected}


TOOLS = {
    "get_employee_profile": get_employee_profile,
    "get_leave_balance": get_leave_balance,
    "submit_leave_request": submit_leave_request,
    "get_public_holidays": get_public_holidays,
}


def _tool_name(event: dict, context) -> str:
    """Gateway form <target>___<tool> from the Lambda client context; event["tool"] for local calls."""
    client_context = getattr(context, "client_context", None)
    custom = getattr(client_context, "custom", None) or {}
    name = custom.get("bedrockAgentCoreToolName") or event.get("tool") or ""
    return str(name).split("___")[-1]


def handler(event, context):
    """Lambda entry point: dispatch to the tool named by the gateway."""
    event = event if isinstance(event, dict) else {}
    tool = _tool_name(event, context)
    fn = TOOLS.get(tool)
    if fn is None:
        return _error(f"Unknown tool {tool!r}. Available tools: {', '.join(sorted(TOOLS))}.")
    try:
        return fn(event)
    except Exception as exc:  # keep the gateway call alive; the agent gets a message, CloudWatch gets the trace
        traceback.print_exc()
        return _error(f"{tool} failed: {exc}")
