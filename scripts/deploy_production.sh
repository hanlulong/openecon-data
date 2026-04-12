#!/bin/bash
# Authoritative production deploy entrypoint for the canonical OpenEcon repo.
# Usage: ./scripts/deploy_production.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "$PROJECT_ROOT"

echo "== deploy_production.sh =="
echo "PROJECT_ROOT=$PROJECT_ROOT"
echo "TARGET_BRANCH=main"

git checkout main
git pull --ff-only origin main

DEPLOY_COMMIT_SHA="$(git rev-parse HEAD)"
echo "DEPLOY_COMMIT_SHA=$DEPLOY_COMMIT_SHA"

npm run build:frontend
"$SCRIPT_DIR/start_backend.sh" production

curl -fsS https://data.openecon.ai/api/health

echo "DEPLOY_HEALTH_OK"
echo "DEPLOY_COMPLETE_SHA=$DEPLOY_COMMIT_SHA"
