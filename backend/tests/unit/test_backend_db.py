import re
import time
from decimal import Decimal

import pytest
from botocore.exceptions import ClientError
from platform_api import db, errors
from platform_sdk.agentcore import AgentCoreError
from platform_sdk.litellm import LiteLLMError


def test_key_builders_follow_contract_section_3():
    assert db.settings_key() == {"PK": "SETTINGS", "SK": "PLATFORM"}
    assert db.domain_key("people-ops") == {"PK": "DOMAIN#people-ops", "SK": "META"}
    assert db.domain_gsi("People Ops") == {"GSI1PK": "DOMAIN", "GSI1SK": "People Ops"}
    assert db.project_key("hr-assistant") == {"PK": "PROJECT#hr-assistant", "SK": "META"}
    assert db.project_gsi("people-ops", "hr-assistant") == {"GSI1PK": "PROJECT", "GSI1SK": "people-ops#hr-assistant"}
    assert db.resource_key("hr-assistant", "dev", "gateway", "hr-assistant-dev-gw") == {
        "PK": "PROJECT#hr-assistant",
        "SK": "RESOURCE#dev#gateway#hr-assistant-dev-gw",
    }
    assert db.resource_prefix() == "RESOURCE#" and db.resource_prefix("prod") == "RESOURCE#prod#"
    assert db.blueprint_key("hr-assistant", 3)["SK"] == "BLUEPRINT#0003"
    assert db.blueprint_key("hr-assistant", "12")["SK"] == "BLUEPRINT#0012"
    assert db.release_key("hr-assistant", "01J")["SK"] == "RELEASE#01J"
    assert db.release_gsi("2026-10-08T10:00:00Z", "01J") == {"GSI1PK": "RELEASE", "GSI1SK": "2026-10-08T10:00:00Z#01J"}
    assert db.key_key("hr-assistant", "dev", "claude-haiku-4-5")["SK"] == "KEY#dev#claude-haiku-4-5"
    assert db.job_key("j1") == {"PK": "JOB#j1", "SK": "META"}
    assert db.job_gsi("t", "j1") == {"GSI1PK": "JOB", "GSI1SK": "t#j1"}
    assert db.feedback_key("hr-assistant", "t", "s", "u1") == {"PK": "FEEDBACK#hr-assistant", "SK": "t#s#u1"}
    assert db.feedback_gsi("t", "u1") == {"GSI1PK": "FEEDBACK", "GSI1SK": "t#u1"}


def test_decimal_round_trip_and_strip_keys():
    item = {"PK": "a", "SK": "b", "GSI1PK": "c", "GSI1SK": "d", "cap": 5.5, "n": 3, "nested": {"x": 0.1, "l": [1, 2.5]}}
    stored = db.to_dynamo(item)
    assert stored["cap"] == Decimal("5.5") and stored["nested"]["x"] == Decimal("0.1")
    assert stored["nested"]["l"] == [1, Decimal("2.5")] and stored["n"] == 3
    back = db.from_dynamo(stored)
    assert back["cap"] == 5.5 and isinstance(back["n"], int) and back["nested"]["l"] == [1, 2.5]
    assert db.from_dynamo(Decimal("5")) == 5 and isinstance(db.from_dynamo(Decimal("5")), int)
    assert db.strip_keys(stored) == {"cap": 5.5, "n": 3, "nested": {"x": 0.1, "l": [1, 2.5]}}
    assert db.strip_keys(None) is None


def test_ids_and_timestamps():
    first = db.new_id()
    time.sleep(0.002)
    second = db.new_id()
    assert len(first) == 26 and re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{26}", first)
    assert second > first  # time-ordered
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", db.now_iso())


def test_query_helpers_with_moto(table):
    db.put({**db.domain_key("ops"), **db.domain_gsi("Ops"), "domainId": "ops"})
    db.put({**db.project_key("p1"), **db.project_gsi("ops", "p1"), "projectId": "p1", "cap": 1.5})
    db.put({**db.project_key("p2"), **db.project_gsi("fin", "p2"), "projectId": "p2"})
    db.put({**db.resource_key("p1", "dev", "gateway", "g"), "type": "gateway"})
    db.put({**db.resource_key("p1", "prod", "memory", "m"), "type": "memory"})
    assert [p["projectId"] for p in db.query_gsi1("PROJECT")] == ["p2", "p1"]  # fin#p2 < ops#p1
    assert [p["projectId"] for p in db.query_gsi1("PROJECT", "ops#")] == ["p1"]
    assert db.get(db.project_key("p1"))["cap"] == 1.5
    assert len(db.query("PROJECT#p1")) == 3
    assert [r["type"] for r in db.query("PROJECT#p1", db.resource_prefix("dev"))] == ["gateway"]
    assert db.delete_many(db.query("PROJECT#p1")) == 3
    assert db.query("PROJECT#p1") == [] and db.get(db.project_key("p1")) is None
    db.delete(db.project_key("p2"))
    assert db.query_gsi1("PROJECT") == []


def test_table_requires_the_environment_variable(monkeypatch):
    monkeypatch.delenv("TABLE_NAME")
    db.reset()
    with pytest.raises(RuntimeError):
        db.table()


def test_error_mapping():
    client_error = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "GetGateway")
    mapped = errors.from_exception(client_error)
    assert (mapped.status, mapped.code) == (
        502,
        "UPSTREAM_ERROR",
    ) and "GetGateway failed: AccessDeniedException: nope" == mapped.message
    missing = errors.from_exception(
        ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}}, "GetGateway")
    )
    assert (missing.status, missing.code) == (404, "NOT_FOUND")
    lite = errors.from_exception(LiteLLMError(400, "Budget has been exceeded!", type="budget_exceeded"))
    assert (lite.status, lite.code, lite.message) == (502, "UPSTREAM_ERROR", "LiteLLM 400: Budget has been exceeded!")
    assert errors.from_exception(AgentCoreError("gateway failed")).status == 502
    assert errors.from_exception(ValueError("bad id")).code == "VALIDATION_ERROR"
    internal = errors.from_exception(KeyError("x"))
    assert (internal.status, internal.code) == (500, "INTERNAL_ERROR")
    assert errors.from_exception(errors.not_found("x")).body() == {"error": {"code": "NOT_FOUND", "message": "x"}}
