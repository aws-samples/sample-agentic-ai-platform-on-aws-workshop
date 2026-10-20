"""The CDK deployments the learners run: preflight (Node, the pinned CDK CLI, credentials, bootstrap), then a streamed `cdk deploy`.

Two chains, deployed by whoever the learning path says:
    landing zone  Network -> SharedServices -> ModelGateway   module 1, `workshop apply 1` (about 20 minutes, the model gateway dominates)
    pre-day       Web -> Auth -> Platform, plus Scenario        before the day: the Workshop Studio deployer, `scripts/deploy.sh --pre-day`
                                                               or `workshop deploy pre-day`
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from .context import WorkshopError, head, ok, say, warn

LANDING_ZONE = ["AgentPlatform-Network", "AgentPlatform-SharedServices", "AgentPlatform-ModelGateway"]
PRE_DAY = ["AgentPlatform-Web", "AgentPlatform-Auth", "AgentPlatform-Platform", "AgentPlatform-Scenario"]
IDP = ["AgentPlatform-Idp"]  # module 2: the corporate IdP (Keycloak) and the HR API behind API Gateway
# `all` is the platform (what the Workshop Studio deployer and scripts/deploy.sh build before the day); the IdP stack is deployed by
# `workshop apply 2` (module 2), so it stays out of `all` and costs nothing until that module runs. EVERYTHING = status and destroy.
SCOPES = {"landing-zone": LANDING_ZONE, "pre-day": PRE_DAY, "idp": IDP, "all": LANDING_ZONE + PRE_DAY}
EVERYTHING = LANDING_ZONE + PRE_DAY + IDP
CHAIN = {
    **{s: "landing zone" for s in LANDING_ZONE},
    **{s: "control plane" for s in PRE_DAY[:3]},
    PRE_DAY[3]: "sample data",
    IDP[0]: "corporate IdP (module 2)",
}
MIN_NODE_MAJOR = 20
BOOTSTRAP_STACK = "CDKToolkit"
HEALTHY = ("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE", "IMPORT_COMPLETE")


def node_version() -> str:
    node = shutil.which("node")
    if not node:
        raise WorkshopError(
            "Node.js 20 or later is needed for `cdk deploy` and is not on your PATH (https://nodejs.org)"
        )
    out = subprocess.run([node, "--version"], capture_output=True, text=True, check=False).stdout.strip()
    match = re.match(r"v?(\d+)", out)
    if not match or int(match.group(1)) < MIN_NODE_MAJOR:
        raise WorkshopError(f"Node.js {MIN_NODE_MAJOR} or later is needed for `cdk deploy`; found {out or 'nothing'}")
    return out


def cdk_command(root: Path) -> list[str]:
    """`node <aws-cdk/bin/cdk>`: the pinned CLI that `npm install` put into node_modules, started through Node so that it works
    on Windows too (there, `node_modules/.bin/cdk` is a POSIX shell shim and only `cdk.cmd` would run)."""
    node = shutil.which("node")
    if not node:
        raise WorkshopError(
            "Node.js 20 or later is needed for `cdk deploy` and is not on your PATH (https://nodejs.org)"
        )
    entry = root / "node_modules" / "aws-cdk" / "bin" / "cdk"
    if not entry.exists():
        raise WorkshopError(
            "the pinned CDK CLI is not installed: run `npm install` at the repository root (package.json pins aws-cdk)"
        )
    return [node, str(entry)]


def identity(region: str | None) -> tuple[str, str]:
    """(account id, region) of the current credentials; the region falls back to us-east-1 like the SDK does."""
    session = boto3.Session(region_name=region)
    region = session.region_name or "us-east-1"
    try:
        account = session.client("sts", region_name=region).get_caller_identity()["Account"]
    except Exception as exc:  # NoCredentialsError, ExpiredToken, ...
        raise WorkshopError(
            f"no usable AWS credentials ({type(exc).__name__}: {exc}); set AWS_PROFILE or paste the event credentials"
        ) from exc
    return account, region


def app_env(account: str, region: str) -> dict:
    """The environment for the CDK CLI and the scripts: the account and region pinned, a foreign VIRTUAL_ENV dropped (the CDK app
    runs `uv run --project ..`, which otherwise only warns about it)."""
    env = {**os.environ, "CDK_DEFAULT_ACCOUNT": account, "CDK_DEFAULT_REGION": region, "AWS_DEFAULT_REGION": region}
    env.pop("VIRTUAL_ENV", None)
    return env


def stack_statuses(region: str, names: list[str]) -> dict[str, str | None]:
    """CloudFormation status per stack name (None when the stack does not exist)."""
    cfn = boto3.client("cloudformation", region_name=region)
    statuses: dict[str, str | None] = {}
    for name in names:
        try:
            statuses[name] = cfn.describe_stacks(StackName=name)["Stacks"][0]["StackStatus"]
        except ClientError as exc:
            if "does not exist" in str(exc):
                statuses[name] = None
            else:
                raise
    return statuses


def print_statuses(region: str, names: list[str] | None = None) -> dict[str, str | None]:
    statuses = stack_statuses(region, names or EVERYTHING)
    for name, status in statuses.items():
        say(f"  {name:<32} {status or 'not deployed':<26} {CHAIN.get(name, '')}")
    return statuses


def ensure_bootstrap(cdk: list[str], root: Path, account: str, region: str, env: dict) -> None:
    if stack_statuses(region, [BOOTSTRAP_STACK])[BOOTSTRAP_STACK] in HEALTHY:
        return
    say(
        f"  CDK bootstrap stack {BOOTSTRAP_STACK} missing: running `cdk bootstrap aws://{account}/{region}` first (about 2 minutes)"
    )
    completed = subprocess.run(
        [*cdk, "bootstrap", f"aws://{account}/{region}", "--tags", "Project=workshop-agent-platform"],
        cwd=str(root / "foundation"),
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        raise WorkshopError(f"cdk bootstrap exited {completed.returncode}")


FRONTEND_INPUTS = (
    "src",
    "public",
    "index.html",
    "package.json",
    "package-lock.json",
    "vite.config.ts",
    "tsconfig.json",
)


def frontend_dist_stale(root: Path) -> str | None:
    """Why frontend/dist must be (re)built: missing, or older than a source file. None when it is current.

    A stale dist is how an old UI (lab-era wording) kept being deployed: the Web stack ships whatever frontend/dist holds."""
    frontend = root / "frontend"
    index = frontend / "dist" / "index.html"
    if not index.exists():
        return "frontend/dist is missing"
    built = index.stat().st_mtime
    for name in FRONTEND_INPUTS:
        path = frontend / name
        candidates = [p for p in path.rglob("*") if p.is_file()] if path.is_dir() else ([path] if path.exists() else [])
        newest = max((p for p in candidates), key=lambda p: p.stat().st_mtime, default=None)
        if newest is not None and newest.stat().st_mtime > built:
            return f"frontend/dist is older than {newest.relative_to(root)}"
    return None


def ensure_frontend_build(root: Path) -> None:
    """The Web stack ships frontend/dist; build it when missing or stale (needs npm), otherwise the placeholder page or an old UI
    is deployed."""
    reason = frontend_dist_stale(root)
    if reason is None:
        return
    npm = shutil.which("npm")
    if not npm:
        raise WorkshopError(
            f"{reason} and npm is not on your PATH: build the UI first (cd frontend && npm ci && npm run build)"
        )
    frontend = root / "frontend"
    steps = [["run", "build"]]
    # npm writes node_modules/.package-lock.json on every install: reinstall when it is missing or older than the lock file
    # (a pulled commit that bumps a dependency would otherwise fail the build with a missing module).
    installed = frontend / "node_modules" / ".package-lock.json"
    lock = frontend / "package-lock.json"
    if not installed.exists() or (lock.exists() and lock.stat().st_mtime > installed.stat().st_mtime):
        steps.insert(0, ["ci", "--no-audit", "--no-fund"])
    say(f"  {reason}: building the web UI ({' && '.join('npm ' + ' '.join(s) for s in steps)}, about two minutes)")
    for args in steps:
        completed = subprocess.run([npm, *args], cwd=str(root / "frontend"), check=False)
        if completed.returncode != 0:
            raise WorkshopError(f"npm {' '.join(args)} exited {completed.returncode}")


def deploy(root: Path, scope: str, region: str | None = None, concurrency: int = 4) -> float:
    """`cdk deploy` of one scope with streamed CloudFormation events; returns the wall time in seconds. Idempotent: CDK skips
    stacks without changes, so re-running after a failure resumes with the stacks that are not finished."""
    if scope not in SCOPES:
        raise WorkshopError(f"unknown scope {scope!r}: {', '.join(SCOPES)}")
    stacks = SCOPES[scope]
    head(f"cdk deploy: {scope} ({', '.join(s.removeprefix('AgentPlatform-') for s in stacks)})")
    node = node_version()
    cdk = cdk_command(root)
    account, region = identity(region)
    say(f"  account {account}, region {region}; node {node}; cdk {Path(cdk[1]).relative_to(root)}")
    env = app_env(account, region)
    say("  stacks before:")
    before = print_statuses(region, stacks)
    if all(status in HEALTHY for status in before.values()):
        say(
            "  every stack of this scope exists: cdk deploy will only apply template changes (usually none, about a minute)"
        )
    if scope not in ("landing-zone", "idp"):
        ensure_frontend_build(root)
    ensure_bootstrap(cdk, root, account, region, env)
    command = [
        *cdk,
        "deploy",
        *stacks,
        "--require-approval",
        "never",
        "--concurrency",
        str(concurrency),
        "--progress",
        "events",
    ]
    say(f"  $ (cd foundation && npx cdk {' '.join(command[2:])})")
    started = time.monotonic()
    completed = subprocess.run(command, cwd=str(root / "foundation"), env=env, check=False)
    seconds = time.monotonic() - started
    if completed.returncode != 0:
        print_statuses(region, stacks)
        raise WorkshopError(
            f"cdk deploy exited {completed.returncode} after {seconds / 60:.1f} min; read the CloudFormation events above. "
            "Finished stacks are kept: fix the cause and run the same command again"
        )
    say("  stacks after:")
    after = print_statuses(region, stacks)
    unhealthy = [n for n, s in after.items() if s not in HEALTHY]
    if unhealthy:
        warn(f"not healthy: {unhealthy}")
    else:
        ok(f"{scope}: {len(stacks)} stacks deployed in {seconds / 60:.1f} min")
    return seconds
