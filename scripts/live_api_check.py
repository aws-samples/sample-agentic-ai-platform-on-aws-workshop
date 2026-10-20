#!/usr/bin/env python3
"""Smoke and latency check of the deployed platform API (read-only routes).

    AWS_PROFILE=workshop uv run python scripts/live_api_check.py [--persona admin] [--repeat 3]

Logs in as the persona, calls GET /health (without a token), /me, /settings, /domains, /projects, /governance, /spend for the first
project (if any) and prints the latency of every call plus the cold/warm difference of /health. Exit code 1 on any failure.
"""

from __future__ import annotations

import argparse
import sys
import time

import requests
from platform_sdk.api import PlatformApiError, PlatformClient

from platform_sdk import auth, config


def timed(label: str, fn, rows: list) -> object:
    started = time.perf_counter()
    try:
        result = fn()
        rows.append((label, time.perf_counter() - started, "OK", ""))
        return result
    except PlatformApiError as exc:
        rows.append((label, time.perf_counter() - started, "FAIL", f"{exc.status} {exc.code}: {exc.message[:80]}"))
    except Exception as exc:  # noqa: BLE001 - report and keep going
        rows.append((label, time.perf_counter() - started, "FAIL", f"{type(exc).__name__}: {exc}"[:100]))
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--persona", default="admin", choices=auth.PERSONAS)
    parser.add_argument("--repeat", type=int, default=3, help="how many times to call GET /health (default 3)")
    args = parser.parse_args(argv)
    cfg = config.load()
    rows: list = []
    for i in range(max(1, args.repeat)):
        timed(
            f"GET /health #{i + 1} (no token)",
            lambda: requests.get(cfg.api_url + "/health", timeout=30).raise_for_status(),
            rows,
        )
    tokens = timed(f"Cognito login ({args.persona})", lambda: auth.login(args.persona, cfg=cfg), rows)
    if tokens is None:
        return _report(rows, cfg)
    client = PlatformClient(cfg, tokens.id_token, timeout=60)
    me = timed("GET /me", client.me, rows)
    timed("GET /settings", client.get_settings, rows)
    timed("GET /domains", client.list_domains, rows)
    projects = timed("GET /projects", client.list_projects, rows) or []
    timed("GET /governance", client.governance, rows)
    if projects:
        project_id = projects[0]["projectId"]
        timed(f"GET /projects/{project_id}", lambda: client.get_project(project_id), rows)
        timed(f"GET /projects/{project_id}/resources", lambda: client.list_resources(project_id), rows)
        timed(f"GET /spend?projectId={project_id}", lambda: client.spend(project_id), rows)
    timed("GET /catalogue", client.list_catalogue, rows)
    if me:
        print(f"signed in as {me['username']} ({me['role']}; groups {me['groups']})")
    return _report(rows, cfg)


def _report(rows: list, cfg) -> int:
    print(f"API {cfg.api_url}")
    print(f"{'call':<44} {'seconds':>8}  result")
    failures = 0
    for label, seconds, status, detail in rows:
        if status == "FAIL" and not (label.startswith("GET /catalogue") and "REGISTRY_NOT_CONFIGURED" in detail):
            failures += 1
        print(f"{label:<44} {seconds:8.3f}  {status} {detail}")
    print(f"{len(rows)} calls, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
