"""Custom resource: seed the scenario DynamoDB tables from the bundled JSON files.

The Scenario stack stages this file together with copies of `scenario/data/*.json` (in `data/`) into one Lambda asset.
On Create and Update every row is written (PutItem overwrites, so re-deploys reset the seed rows and leave rows added
in the modules alone); Delete does nothing. Numbers are parsed as Decimal because DynamoDB rejects Python floats (6.5).
"""

from __future__ import annotations

import decimal
import json
import pathlib

import boto3

HERE = pathlib.Path(__file__).resolve().parent
FILES = {
    "EmployeesTable": "employees.json",
    "LeaveRequestsTable": "leave_requests.json",
    "InvoicesTable": "invoices.json",
}
dynamodb = boto3.resource("dynamodb")


def seed_table(table_name: str, filename: str) -> int:
    rows = json.loads((HERE / "data" / filename).read_text(encoding="utf-8"), parse_float=decimal.Decimal)
    table = dynamodb.Table(table_name)
    with table.batch_writer() as batch:
        for row in rows:
            batch.put_item(Item=row)
    return len(rows)


def handler(event, context):
    props = event["ResourceProperties"]
    physical_id = "seed:" + props["EmployeesTable"]
    if event["RequestType"] == "Delete":
        return {"PhysicalResourceId": event.get("PhysicalResourceId", physical_id)}
    counts = {prop: seed_table(props[prop], filename) for prop, filename in FILES.items()}
    print("seeded", counts)
    return {"PhysicalResourceId": physical_id, "Data": {f"{k}Rows": str(v) for k, v in counts.items()}}
