"""`workshop export`: the Dev harness as a code skeleton, Strands Agents or LangGraph (module 6), written to exports/."""

from __future__ import annotations

import os
import py_compile
import subprocess
import sys
import time
from datetime import UTC, datetime

from platform_sdk import agentcore, export, litellm, names

from .context import Context, WorkshopError, head, look, ok, say, warn


def run(ctx: Context, flavour: str | None = None, run: bool = False, out_dir: str = "exports") -> int:
    flavour = flavour or str(ctx.inputs(6).get("export") or "strands")
    cfg = ctx.cfg
    hr_dev = ctx.names("dev")
    rows = {(r["type"], r["name"]): r for r in ctx.developer.list_resources(ctx.project_id, env="dev")}
    harness_row = rows.get(("harness", hr_dev.harness))
    if harness_row is None:
        raise WorkshopError("the Dev agent is not deployed yet: run `workshop apply 6` first")
    harness = agentcore.control_client(cfg.region).get_harness(harnessId=harness_row["id"])["harness"]

    head(f"export the Dev harness {harness['harnessName']} (version {harness['harnessVersion']}) to {flavour}")
    summary = export.summarize(harness, cfg)
    say(f"  model {summary['model']['alias']} via {summary['model']['apiBase']}")
    say(f"  gateway {summary['gateway']['url'] if summary['gateway'] else '-'}  allowedTools {summary['allowedTools']}")
    say(f"  agent role {summary['executionRoleArn']}")
    source = export.to_langgraph(harness, cfg) if flavour == "langgraph" else export.to_strands(harness, cfg)
    export_dir = ctx.root / out_dir
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"{hr_dev.harness}_{flavour}_agent.py"
    path.write_text(source)
    py_compile.compile(str(path), doraise=True)
    ok(f"{path.relative_to(ctx.root)}: {len(source.splitlines())} lines, compiles")
    ctx.state.set(
        f"export.{flavour}",
        {
            "path": str(path.relative_to(ctx.root)),
            "lines": len(source.splitlines()),
            "at": datetime.now(UTC).isoformat(),
        },
    )
    say("  the skeleton keeps the model alias, the gateway tools and the agent role: the same policies and caps apply.")
    say("  memory, tracing, the managed runtime and the release gate are yours to add (see the comments in the file).")
    if flavour == "langgraph":
        say(
            "  needs: uv sync --group langgraph  (langchain 1.x, langgraph, langchain-openai, langchain-core; a default group of this repository)"
        )

    if run:
        head("run it once with a temporary key of the project's team")
        from .apply_5 import proxy_admin

        proxy = proxy_admin(ctx)
        alias = f"{ctx.project_id}-dev-export"
        stale = proxy.find_key(alias)
        if stale:
            proxy.delete_key(stale.get("token") or stale.get("token_id"))
        created = proxy.create_service_account_key(
            hr_dev.team,
            alias,
            [ctx.model_alias],
            0.2,
            "7d",
            {"project": ctx.project_id, "agent": "export", "env": "dev", "model": ctx.model_alias},
        )
        env = {**os.environ, export.DEFAULT_API_KEY_ENV: created["key"], "AWS_DEFAULT_REGION": cfg.region}
        relay_proxy = None
        if proxy.via_relay:
            # the model gateway is private: the exported module speaks plain HTTP, so a localhost server carries its calls
            # through the relay Lambda; the generated code reads MODEL_GATEWAY_BASE and otherwise keeps the harness's apiBase
            relay_proxy = litellm.LocalRelayProxy(proxy.session).start()
            env[export.BASE_URL_ENV] = relay_proxy.base_url + "/v1"
            say(
                f"  the model gateway is private: a localhost relay proxy at {relay_proxy.base_url} forwards the module's calls "
                f"through {proxy.transport}; {export.BASE_URL_ENV}={env[export.BASE_URL_ENV]}"
            )
        prompt = "What is my leave balance? My employee id is E1001."
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [sys.executable, str(path), prompt],
                env=env,
                capture_output=True,
                text=True,
                timeout=600,
                cwd=str(ctx.root),
            )
            say(
                f"  $ python {path.relative_to(ctx.root)} {prompt!r} -> exit {completed.returncode} after {time.monotonic() - started:.0f} s"
            )
            say("    > " + " ".join(completed.stdout.split())[:500])
            if completed.returncode != 0:
                warn(completed.stderr[-1500:])
        finally:
            if relay_proxy is not None:
                relay_proxy.stop()
            proxy.delete_key(created["token"])
            say(f"  temporary key {alias} deleted")
    look("the file", str(path))
    say(f"  shared names: gateway {names.ProjectNames(ctx.project_id, 'dev').gateway}")
    return 0
