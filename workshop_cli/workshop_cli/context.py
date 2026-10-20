"""Shared context of a `workshop` run: repository root, the record, the platform configuration, personas, the state file."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from platform_sdk import api, auth, config, names, state

from . import record as record_module


class WorkshopError(Exception):
    """A problem the learner can act on; printed without a traceback."""


def find_root(start: Path | None = None) -> Path:
    """The repository root: the first parent of the working directory that contains `scenario/`, else the SDK's checkout."""
    start = start or Path.cwd()
    for candidate in (start, *start.parents):
        if (candidate / "scenario").is_dir() and (candidate / "platform_sdk").is_dir():
            return candidate
    import platform_sdk

    root = Path(platform_sdk.__file__).resolve().parents[2]
    if (root / "scenario").is_dir():
        return root
    raise WorkshopError("run `workshop` from inside the repository (it reads scenario/ and design-record.yaml)")


def say(text: str = "") -> None:
    print(text, flush=True)


def head(text: str) -> None:
    print(f"\n== {text}", flush=True)


def look(where: str, what: str) -> None:
    """A 'where to look' line: the screen or console page and what to find there."""
    print(f"   look: {where}: {what}", flush=True)


def ok(text: str) -> None:
    print(f"OK   {text}", flush=True)


def warn(text: str) -> None:
    print(f"WARN {text}", flush=True)


def elapsed(started: float) -> str:
    return f"{time.monotonic() - started:.1f} s"


@dataclass
class Context:
    root: Path
    record_path: Path
    record: dict
    region_override: str | None = None
    _cfg: Any = field(default=None, repr=False)
    _state: Any = field(default=None, repr=False)
    _tokens: dict = field(default_factory=dict, repr=False)
    _clients: dict = field(default_factory=dict, repr=False)

    # ---- configuration, lazily (so `workshop record` works without AWS credentials) ----------------------------------
    @property
    def cfg(self):
        if self._cfg is None:
            try:
                self._cfg = config.load(region=self.region_override)
            except Exception as exc:  # no credentials, wrong account, foundation not deployed
                raise WorkshopError(
                    f"cannot read the platform configuration from SSM ({type(exc).__name__}: {exc}). Check your AWS "
                    "credentials and region (AWS_PROFILE, AWS_DEFAULT_REGION); the foundation stacks must be deployed."
                ) from exc
        return self._cfg

    @property
    def region(self) -> str:
        return self.cfg.region

    # ---- the record's identifiers ----------------------------------------------------------------------------------
    def inputs(self, module: int) -> dict:
        return record_module.inputs(self.record, module)

    @property
    def project_id(self) -> str:
        return str(self.inputs(3)["project"])

    @property
    def domain_id(self) -> str:
        return str(self.inputs(3)["domain"])

    @property
    def model_alias(self) -> str:
        return str(self.inputs(3)["model"])

    @property
    def finance(self) -> dict:
        return record_module.FINANCE

    def names(self, env: str, project_id: str | None = None) -> names.ProjectNames:
        return names.ProjectNames(project_id or self.project_id, env)

    # ---- the state file of this project (.workshop/state/<project>.json) -------------------------------------------
    @property
    def state(self) -> state.WorkshopState:
        if self._state is None:
            self._state = state.WorkshopState.for_project(self.project_id, root=self.root)
        return self._state

    # ---- personas --------------------------------------------------------------------------------------------------
    def tokens(self, persona: str) -> auth.Tokens:
        if persona not in self._tokens:
            try:
                self._tokens[persona] = auth.login(persona, cfg=self.cfg)
            except auth.AuthError as exc:
                raise WorkshopError(str(exc)) from exc
        return self._tokens[persona]

    def api(self, persona: str) -> api.PlatformClient:
        """A platform API client signed in as `admin`, `developer` or `approver`."""
        if persona not in self._clients:
            self._clients[persona] = api.PlatformClient(self.cfg, self.tokens(persona).id_token)
        return self._clients[persona]

    @property
    def admin(self) -> api.PlatformClient:
        return self.api("admin")

    @property
    def developer(self) -> api.PlatformClient:
        return self.api("developer")

    @property
    def approver(self) -> api.PlatformClient:
        return self.api("approver")

    # ---- paths -----------------------------------------------------------------------------------------------------
    @property
    def scenario_dir(self) -> Path:
        return self.root / "scenario"

    @property
    def blueprint_dir(self) -> Path:
        return self.scenario_dir / "blueprint"

    @property
    def dataset_path(self) -> Path:
        return self.scenario_dir / "evaluation" / "hr-assistant.json"

    @property
    def ui_url(self) -> str:
        return (self.cfg.get("ui/url") or "").rstrip("/") or "<platform UI URL>"

    def console(self, service_path: str) -> str:
        region = self.region
        return f"https://{region}.console.aws.amazon.com/{service_path}"

    def mark(self, key: str, **fields) -> None:
        """Record what a module did in the state file, under the `workshop.<key>` section."""
        self.state.set(f"workshop.{key}", {"at": datetime.now(UTC).isoformat(), **fields})


def build_context(record_path: str | None = None, region: str | None = None) -> Context:
    root = find_root()
    path = Path(record_path or os.environ.get("WORKSHOP_RECORD") or root / record_module.RECORD_FILE)
    try:
        document = record_module.load(path)
    except record_module.RecordError as exc:
        raise WorkshopError(str(exc)) from exc
    problems = record_module.validate(document)
    if problems:
        raise WorkshopError(f"{path} has problems:\n  - " + "\n  - ".join(problems))
    return Context(root=root, record_path=path, record=document, region_override=region)


def dump(value: Any) -> str:
    return json.dumps(value, indent=2, default=str)


def print_exception_hint(exc: BaseException) -> None:
    print(f"\nERROR {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
