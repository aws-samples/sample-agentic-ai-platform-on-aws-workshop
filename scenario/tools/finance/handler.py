"""AnyCompany Finance tool: one Lambda function, one tool (``get_invoice_status``).

The Finance project exists only to prove project isolation: the HR assistant's gateway never exposes this tool, and a
Cedar policy denies it for the HR project's execution role. Same Lambda contract as the HR tools (see tools/hr/handler.py):
tool name from ``context.client_context.custom["bedrockAgentCoreToolName"]`` (``fintools___get_invoice_status``) or
``event["tool"]`` locally; errors as ``{"error": "..."}``. Environment: ``INVOICES_TABLE``.
"""

from __future__ import annotations

import decimal
import os
import re
import traceback

import boto3

INVOICE_ID_RE = re.compile(r"^INV-\d{4}$")
_dynamodb = None


def _error(message: str) -> dict:
    return {"error": message}


def _table():
    global _dynamodb
    name = os.environ.get("INVOICES_TABLE")
    if not name:
        raise RuntimeError("environment variable INVOICES_TABLE is not set")
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb.Table(name)


def _reset_clients() -> None:
    global _dynamodb
    _dynamodb = None


def _plain(value):
    if isinstance(value, decimal.Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


def get_invoice_status(args: dict) -> dict:
    """Vendor, amount, currency, payment status and due date of one invoice (ids look like INV-1001)."""
    invoice_id = str(args.get("invoice_id") or "").strip().upper()
    if not INVOICE_ID_RE.match(invoice_id):
        return _error(f"invoice_id is required and looks like INV-1001 (got {args.get('invoice_id')!r}).")
    item = _table().get_item(Key={"invoice_id": invoice_id}).get("Item")
    if not item:
        return _error(f"Unknown invoice id {invoice_id}.")
    return _plain(item)


TOOLS = {"get_invoice_status": get_invoice_status}


def _tool_name(event: dict, context) -> str:
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
    except Exception as exc:
        traceback.print_exc()
        return _error(f"{tool} failed: {exc}")
