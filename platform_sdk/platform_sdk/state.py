"""Small JSON state store for the `workshop` command: the ids and ARNs the modules create (build contract section 9).

Default location `<repo>/.workshop/state/<project>.json` (git-ignored). Every `workshop apply` can therefore be re-run and
still find what an earlier module created.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_PROJECT = "hr-assistant"
STATE_SUBDIR = (".workshop", "state")


def _repo_root(start: Path) -> Path | None:
    """The first of `start` and its parents that holds both `scenario/` and `platform_sdk/` (the rule of `workshop_cli.context.find_root`)."""
    for candidate in (start, *start.parents):
        if (candidate / "scenario").is_dir() and (candidate / "platform_sdk").is_dir():
            return candidate
    return None


def state_dir(root: str | os.PathLike | None = None) -> Path:
    """`PLATFORM_STATE_DIR` if set, else `<root>/.workshop/state` when the repository root is given, else `<repo>/.workshop/state`
    with the repository root found from the working directory (falling back to `<cwd>/.workshop/state` outside the repository)."""
    override = os.environ.get("PLATFORM_STATE_DIR")
    if override:
        return Path(override)
    if root is not None:
        return Path(root).joinpath(*STATE_SUBDIR)
    cwd = Path.cwd()
    return (_repo_root(cwd) or cwd).joinpath(*STATE_SUBDIR)


def default_path(project_id: str = DEFAULT_PROJECT, root: str | os.PathLike | None = None) -> Path:
    return state_dir(root) / f"{project_id}.json"


class WorkshopState:
    """Dict-like JSON file with dotted keys, for example `state.set("gateway.dev.arn", arn)`."""

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path else default_path()
        self.data: dict[str, Any] = {}
        if self.path.exists():
            self.data = json.loads(self.path.read_text() or "{}")

    @classmethod
    def for_project(cls, project_id: str = DEFAULT_PROJECT, root: str | os.PathLike | None = None) -> WorkshopState:
        """The state file of a project; pass `root` (the repository) so the location never depends on the working directory."""
        return cls(default_path(project_id, root))

    def get(self, key: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key: str, value: Any) -> Any:
        """Store a JSON copy of `value` (the caller's objects are never aliased: a later `set` of a nested key cannot mutate them)."""
        value = json.loads(json.dumps(value, default=str))
        parts = key.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise KeyError(f"{key}: {part!r} is not a section")
        node[parts[-1]] = value
        self.save()
        return value

    def delete(self, key: str) -> None:
        parts = key.split(".")
        node = self.data
        for part in parts[:-1]:
            node = node.get(part) if isinstance(node, dict) else None
            if node is None:
                return
        if isinstance(node, dict):
            node.pop(parts[-1], None)
            self.save()

    def require(self, key: str, module: str | None = None) -> Any:
        """The value of `key`, or a KeyError naming the module (`workshop apply <module>`) that creates it."""
        value = self.get(key)
        if value is None:
            hint = f" (created by `workshop apply {module}`)" if module else ""
            raise KeyError(f"{key!r} is not in {self.path}{hint}; run the earlier module first")
        return value

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True, default=str) + "\n")

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def __repr__(self) -> str:
        return f"WorkshopState({str(self.path)!r}, keys={sorted(self.data)})"
