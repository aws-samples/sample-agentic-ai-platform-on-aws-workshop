#!/usr/bin/env python3
"""Assemble build/backend/: the Lambda bundle for the platform API and worker.

Contents: backend/src/* + platform_sdk/platform_sdk/ (copied) + the dependencies (pyyaml, requests, boto3 + botocore, and
bedrock-agentcore with pydantic for the evaluate job's dataset runner) installed with `uv pip install --target` for
x86_64 / Python 3.13 (about 50 MB unzipped). boto3 is vendored on purpose: the Lambda runtime's own boto3 predates the Agent
Registry (`agent-registry-control`) and the newest AgentCore operations, which gave `UnknownServiceError` on GET /catalogue
(8 Oct 2026); the bundle's copy (first on sys.path) is the version the repo pins. No Docker. `foundation/app.py` calls
`ensure_bundle()` at synth time; the dependency install is skipped when every marker package exists unless `--force` is given.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "backend" / "src"
SDK = ROOT / "platform_sdk" / "platform_sdk"
OUT = ROOT / "build" / "backend"
DEPS = ["pyyaml", "requests", "boto3", "bedrock-agentcore"]
MARKERS = (
    "yaml",
    "requests",
    "boto3",
    "botocore",
    "bedrock_agentcore",
    "pydantic",
)  # one directory per installed dependency
IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", "tests")


def _copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=IGNORE)


def _constraints() -> Path:
    """The repo's resolution (uv.lock) as a constraints file, so the bundle gets the pinned bedrock-agentcore, boto3 and
    pydantic versions the tests ran against instead of whatever PyPI serves at synth time."""
    path = OUT.parent / "backend-constraints.txt"
    cmd = ["uv", "export", "--frozen", "--no-dev", "--no-hashes", "--no-emit-workspace", "-o", str(path)]
    result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"build_backend: {' '.join(cmd)} failed:\n{result.stderr.strip()}")
    return path


def _install_deps(target: Path) -> None:
    base = ["uv", "pip", "install", "--target", str(target), "--quiet", "--constraints", str(_constraints())]
    platform_flags = [
        "--python-platform",
        "x86_64-manylinux_2_17",
        "--python-version",
        "3.13",
        "--only-binary",
        ":all:",
    ]
    for flags in (platform_flags, []):
        cmd = base + flags + DEPS
        result = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if result.returncode == 0:
            return
        print(f"build_backend: {' '.join(cmd)} failed:\n{result.stderr.strip()}", file=sys.stderr)
    raise SystemExit("build_backend: could not install the backend dependencies with uv")


def ensure_bundle(force: bool = False) -> Path:
    """Build (or refresh) build/backend and return its path."""
    OUT.mkdir(parents=True, exist_ok=True)
    for item in SRC.iterdir():
        if item.name in ("__pycache__",) or item.suffix == ".pyc":
            continue
        if item.is_dir():
            _copy_tree(item, OUT / item.name)
        else:
            shutil.copy2(item, OUT / item.name)
    _copy_tree(SDK, OUT / "platform_sdk")
    if force or not all((OUT / marker).exists() for marker in MARKERS):
        _install_deps(OUT)
    return OUT


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="reinstall the dependencies even if present")
    args = parser.parse_args()
    out = ensure_bundle(force=args.force)
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"build_backend: {out} ready ({size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
