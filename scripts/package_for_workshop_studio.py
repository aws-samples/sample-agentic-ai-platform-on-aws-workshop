"""Package the repository for AWS Workshop Studio.

Produces build/workshop-studio/repo.zip (the repository without environments, build output and internal notes) and copies
workshop-studio/deployer.yaml next to it. Upload both to the event's assets bucket and register deployer.yaml as the event's
CloudFormation template; it deploys the foundation with CodeBuild (see workshop-studio/README.md).

Usage: uv run python scripts/package_for_workshop_studio.py [--out build/workshop-studio] [--skip-ui-check]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    "build",
    "cdk.out",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".claude",
    ".mypy_cache",
    "dist",
    "test-results",
    "playwright-report",
    "coverage",
    ".vite",
}
KEEP_DIST = {ROOT / "frontend" / "dist"}  # the built UI is shipped; every other dist/ directory is excluded
EXCLUDE_FILES = {"cdk.context.json", ".DS_Store"}
EXCLUDE_SUFFIXES = {".pyc", ".log"}
STATE_DIRS = {ROOT / ".workshop", ROOT / "exports"}  # the state of the `workshop` command and `workshop export` output never ship


def included(path: Path) -> bool:
    rel_parts = path.relative_to(ROOT).parts
    for i, part in enumerate(rel_parts[:-1]):
        parent = ROOT.joinpath(*rel_parts[: i + 1])
        if part in EXCLUDE_DIRS and parent not in KEEP_DIST:
            return False
        if parent in STATE_DIRS:
            return False
    if path.name in EXCLUDE_FILES or path.suffix in EXCLUDE_SUFFIXES:
        return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(ROOT / "build" / "workshop-studio"))
    ap.add_argument("--skip-ui-check", action="store_true", help="do not insist on a built UI (frontend/dist/index.html)")
    args = ap.parse_args()
    out = Path(args.out)
    if not args.skip_ui_check and not (ROOT / "frontend" / "dist" / "index.html").exists():
        print(
            "frontend/dist/index.html is missing: build the UI first (cd frontend && npm ci && npm run build) or pass --skip-ui-check "
            "(the deployer then builds it inside CodeBuild).",
            file=sys.stderr,
        )
        return 2
    out.mkdir(parents=True, exist_ok=True)
    archive = out / "repo.zip"
    count = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(ROOT.rglob("*")):
            if path.is_file() and included(path):
                zf.write(path, path.relative_to(ROOT).as_posix())
                count += 1
    shutil.copy(ROOT / "workshop-studio" / "deployer.yaml", out / "deployer.yaml")
    size_mb = archive.stat().st_size / 1_000_000
    print(f"wrote {archive} ({count} files, {size_mb:.1f} MB) and {out / 'deployer.yaml'}")
    print("Next: upload both to the event assets bucket, for example")
    print(
        f"  aws s3 cp {archive} s3://<assets-bucket>/<prefix>repo.zip && aws s3 cp {out / 'deployer.yaml'} s3://<assets-bucket>/<prefix>deployer.yaml"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
