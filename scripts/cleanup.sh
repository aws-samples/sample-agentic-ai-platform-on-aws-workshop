#!/usr/bin/env bash
# Sweep what `cdk destroy --all` leaves behind (secrets in their recovery window, orphan log groups and buckets).
# Dry run by default; pass --yes to apply. Credentials from the environment (e.g. AWS_PROFILE=workshop).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec uv run python scripts/cleanup.py "$@"
