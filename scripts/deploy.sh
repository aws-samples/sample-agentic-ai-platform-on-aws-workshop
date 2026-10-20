#!/usr/bin/env bash
# One-command deployment of the workshop foundation into the current AWS account/region.
# Prerequisites: uv, Node 20+, AWS credentials (e.g. AWS_PROFILE=workshop), CDK bootstrap done once (npx cdk bootstrap).
#   scripts/deploy.sh                 the seven platform stacks at once (the IdP stack of module 2 is separate: --idp)
#   scripts/deploy.sh --pre-day       the control plane and the sample data (Web, Auth, Platform, Scenario): what the learning
#                                     path expects before the day; the learners deploy the landing zone with `workshop apply 1`
#   scripts/deploy.sh --landing-zone  Network, SharedServices, ModelGateway (what `workshop apply 1` deploys)
#   scripts/deploy.sh --idp           Idp: Keycloak and the HR API for the on-behalf-of modules (what `workshop apply 2` deploys)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PLATFORM="AgentPlatform-Network AgentPlatform-SharedServices AgentPlatform-ModelGateway AgentPlatform-Web AgentPlatform-Auth AgentPlatform-Platform AgentPlatform-Scenario"
STACKS="$PLATFORM"  # the seven platform stacks; the IdP stack of module 2 is deployed by `workshop apply 2` (or --idp)
case "${1:-}" in
  --pre-day) STACKS="AgentPlatform-Web AgentPlatform-Auth AgentPlatform-Platform AgentPlatform-Scenario"; shift ;;
  --landing-zone) STACKS="AgentPlatform-Network AgentPlatform-SharedServices AgentPlatform-ModelGateway"; shift ;;
  --idp) STACKS="AgentPlatform-Idp"; shift ;;
esac

echo "== uv sync"
uv sync --quiet
echo "== npm install (CDK CLI)"
npm install --silent --no-audit --no-fund
echo "== frontend (frontend/dist is git-ignored; the Web stack ships whatever it holds, so a stale build is rebuilt)"
# Rebuild when dist is missing or older than any source, config or lock file: the lab-era UI kept being deployed from a dist
# built before the sources changed. node_modules is reinstalled when the lock file is newer than the last install.
frontend_stale() {
  [ -f frontend/dist/index.html ] || return 0
  [ -n "$(cd frontend && find src public index.html package.json package-lock.json vite.config.ts tsconfig.json -newer dist/index.html -print -quit 2>/dev/null)" ]
}
if frontend_stale; then
  if [ ! -f frontend/node_modules/.package-lock.json ] || [ frontend/package-lock.json -nt frontend/node_modules/.package-lock.json ]; then
    (cd frontend && npm ci --no-audit --no-fund)
  fi
  echo "frontend/dist missing or older than the sources: npm run build"
  (cd frontend && npm run build)
else
  echo "frontend/dist is current"
fi
echo "== backend bundle"
uv run python scripts/build_backend.py
echo "== cdk deploy $STACKS"
# --concurrency: the CDK CLI deploys independent stacks in parallel (Web/Auth and Scenario alongside ModelGateway);
# without it the stacks deploy one at a time and a fresh account takes ~27 min instead of ~16.
# Asset uploads to the bootstrap bucket run in parallel too. On laptops behind a VPN or on a slow network the CDK CLI can
# fail with "@smithy/node-http-handler - the request socket did not establish a connection ... within 10000 ms" while
# publishing the 19 MB backend bundle. Stacks that already deployed are untouched, so the second attempt below resumes
# with one stack and one asset at a time. Set DEPLOY_CONCURRENCY=1 to start that way.
# shellcheck disable=SC2086  # STACKS is a word list on purpose
deploy() { (cd foundation && npx cdk deploy $STACKS --require-approval never "$@"); }
if ! deploy --concurrency "${DEPLOY_CONCURRENCY:-4}" "$@"; then
  echo "== cdk deploy failed (usually a transient network error while publishing assets); retrying with --concurrency 1 --no-asset-parallelism"
  deploy --concurrency 1 --no-asset-parallelism "$@"
fi
if [ "$STACKS" = "$PLATFORM" ]; then
  echo "== personas (module 2 of the learning path does this with workshop apply 2; the all-at-once deploy creates them here)"
  uv run python scripts/create_personas.py
fi
echo "== doctor"
uv run python scripts/doctor.py
