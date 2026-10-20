#!/usr/bin/env bash
# Health check of the deployed foundation (SSM parameters, API, model gateway, Cognito, scenario assets).
# Credentials and region from the environment (e.g. AWS_PROFILE=workshop AWS_DEFAULT_REGION=us-east-1); exit code 1 on any failure.
# Pass --show-passwords to print the persona passwords (otherwise only the users secret ARN is shown).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec uv run python scripts/doctor.py "$@"
