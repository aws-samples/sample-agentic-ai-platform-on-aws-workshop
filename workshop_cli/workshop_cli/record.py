"""design-record.yaml: the learner's file. `inputs` are read by `workshop apply`; `decisions` are the learner's notes.

    module_3:
      inputs:                       # read by: workshop apply 3
        domain: people-ops
        project: hr-assistant
        ...
      decisions:
        tenancy: {workshop: templates, my_call: "..."}

Missing inputs fall back to the workshop defaults below, so an empty record still builds the reference platform.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml

from platform_sdk import names

RECORD_FILE = "design-record.yaml"
PERIOD_RE = re.compile(r"^\d+[dhm]$")
NO_INPUT_NOTES = {
    1: "none: `workshop apply 1` deploys the landing zone as designed (network, shared services, model gateway)",
}

DEFAULTS: dict[str, dict[str, Any]] = {
    "module_2": {
        "employee": "E1001",  # the employee who signs in at the corporate IdP (Keycloak); the on-behalf-of chain acts for this person
    },
    "module_3": {
        "domain": "people-ops",
        "domain_name": "People Operations",
        "domain_description": "HR services for AnyCompany employees",
        "project": "hr-assistant",
        "owner": "margaret.chen@anycompany.example",
        "models": ["claude-sonnet-5-5", "claude-haiku-4-5"],
        "model": "claude-sonnet-5-5",
        "budget": {"usd": 5, "period": "7d", "tpm": 200000, "rpm": 200},
    },
    "module_4": {
        "knowledge_filter": {"department": "HR"},
        "max_leave_days": 10,
        "gateway_mode": "LOG_ONLY",
    },
    "module_5": {
        "key_cap": {"usd": 1.0, "period": "7d"},
    },
    "module_6": {
        "prompt_version": "v1",
        "thresholds": {"Correctness": 0.8, "GoalSuccessRate": 0.9, "TrajectoryInOrderMatch": 0.9, "Harmfulness": 0.0},
        "export": "strands",
    },
    "module_7": {
        "prod_traffic": 10,
    },
}

# the Finance side of the isolation proof: fixed, never learner input
FINANCE = {
    "domain": "finance-ops",
    "domain_name": "Finance Operations",
    "domain_description": "Accounts payable and expenses",
    "domain_owner": "finance.lead@anycompany.example",
    "project": "invoice-helper",
    "project_name": "Invoice Helper",
    "project_description": (
        "Accounts-payable assistant of the Finance domain: supplier invoice status. One tool, no agent: it exists to "
        "prove project isolation."
    ),
    "models": ["claude-haiku-4-5"],
    "budget": {"usd": 1, "period": "7d", "tpm": 50000, "rpm": 50},
}


class RecordError(Exception):
    """The record cannot be used; the message lists every problem."""


def load(path: str | Path) -> dict:
    """The parsed record (an empty dict when the file does not exist)."""
    path = Path(path)
    if not path.exists():
        return {}
    document = yaml.safe_load(path.read_text()) or {}
    if not isinstance(document, dict):
        raise RecordError(f"{path}: the record must be a mapping of module_N blocks")
    return document


def inputs(record: dict, module: int) -> dict:
    """The inputs of a module with the workshop defaults filled in (`inputs: none` means defaults only)."""
    block = record.get(f"module_{module}") or {}
    given = block.get("inputs") if isinstance(block, dict) else None
    merged = copy.deepcopy(DEFAULTS.get(f"module_{module}", {}))
    if isinstance(given, dict):
        for key, value in given.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    return merged


def decisions(record: dict, module: int) -> dict:
    block = record.get(f"module_{module}") or {}
    found = block.get("decisions") if isinstance(block, dict) else None
    return dict(found) if isinstance(found, dict) else {}


def validate(record: dict) -> list[str]:
    """Problems with the record's inputs (empty list = usable). Only inputs are checked; decisions are free text."""
    problems: list[str] = []
    m3 = inputs(record, 3)
    for key in ("domain", "project"):
        try:
            names.validate_project_id(str(m3.get(key, "")))
        except ValueError as exc:
            problems.append(f"module_3.inputs.{key}: {exc}")
    if m3["project"] == FINANCE["project"] or m3["domain"] == FINANCE["domain"]:
        problems.append(
            f"module_3.inputs: {FINANCE['project']} / {FINANCE['domain']} are the fixed Finance names; pick others"
        )
    models = m3.get("models")
    if not isinstance(models, list) or not models or not all(isinstance(m, str) for m in models):
        problems.append("module_3.inputs.models: a list of approved model aliases is required")
    elif m3.get("model") not in models:
        problems.append(f"module_3.inputs.model: {m3.get('model')!r} must be one of models {models}")
    budget = m3.get("budget") or {}
    for key, kind in (("usd", (int, float)), ("tpm", int), ("rpm", int)):
        if not isinstance(budget.get(key), kind) or budget.get(key) <= 0:
            problems.append(f"module_3.inputs.budget.{key}: a positive number is required")
    if not PERIOD_RE.match(str(budget.get("period", ""))):
        problems.append("module_3.inputs.budget.period: use a LiteLLM duration such as 7d")

    m4 = inputs(record, 4)
    filt = m4.get("knowledge_filter")
    if (
        not isinstance(filt, dict)
        or set(filt) != {"department"}
        or not isinstance(filt.get("department"), str)
        or not filt["department"]
    ):
        problems.append(
            "module_4.inputs.knowledge_filter: {department: <value>}; the sample documents carry one metadata key, department "
            "(change the value, for example Finance, to see what the HR agent then finds)"
        )
    if not isinstance(m4.get("max_leave_days"), int) or m4["max_leave_days"] < 1:
        problems.append("module_4.inputs.max_leave_days: a positive whole number of days")
    if m4.get("gateway_mode") not in ("LOG_ONLY", "ENFORCE"):
        problems.append("module_4.inputs.gateway_mode: LOG_ONLY or ENFORCE")

    cap = inputs(record, 5).get("key_cap") or {}
    if not isinstance(cap.get("usd"), (int, float)) or cap["usd"] <= 0:
        problems.append("module_5.inputs.key_cap.usd: a positive amount in USD")
    if not PERIOD_RE.match(str(cap.get("period", ""))):
        problems.append("module_5.inputs.key_cap.period: use a LiteLLM duration such as 7d")

    m6 = inputs(record, 6)
    if m6.get("prompt_version") not in ("v1", "v2"):
        problems.append("module_6.inputs.prompt_version: v1 or v2")
    thresholds = m6.get("thresholds") or {}
    for key, value in thresholds.items():
        if not isinstance(value, (int, float)) or not 0 <= value <= 1:
            problems.append(f"module_6.inputs.thresholds.{key}: a number between 0 and 1")
    if m6.get("export") not in ("strands", "langgraph"):
        problems.append("module_6.inputs.export: strands or langgraph")

    employee = str(inputs(record, 2).get("employee") or "")
    if not re.fullmatch(r"E\d{4}", employee):
        problems.append("module_2.inputs.employee: an employee id like E1001 (one of scenario/data/employees.json)")
    traffic = inputs(record, 7).get("prod_traffic")
    if not isinstance(traffic, int) or not 1 <= traffic <= 50:
        problems.append("module_7.inputs.prod_traffic: a whole number between 1 and 50")
    return problems


def describe(record: dict) -> str:
    """A compact printout of every module's effective inputs (defaults applied) and recorded calls."""
    lines = []
    for module in range(1, 8):
        block = record.get(f"module_{module}") or {}
        effective = inputs(record, module)
        lines.append(f"module_{module}:")
        if effective:
            for key, value in effective.items():
                given = (
                    (block.get("inputs") or {})
                    if isinstance(block, dict) and isinstance(block.get("inputs"), dict)
                    else {}
                )
                origin = "record" if key in given else "default"
                lines.append(f"  input    {key:<18} {value!s:<60} ({origin})")
        else:
            lines.append(f"  input    {NO_INPUT_NOTES.get(module, 'none')}")
        for key, value in decisions(record, module).items():
            workshop = value.get("workshop") if isinstance(value, dict) else value
            my_call = value.get("my_call") if isinstance(value, dict) else None
            lines.append(f"  decision {key:<20} workshop {workshop!s:<28} my_call {my_call!s}")
    return "\n".join(lines)
