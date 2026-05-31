#!/bin/bash
# Authoritative production deploy entrypoint for the canonical OpenEcon repo.
# Usage: ./scripts/deploy_production.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "$PROJECT_ROOT"

wait_for_url() {
  local label="$1"
  local url="$2"
  local max_wait="${DEPLOY_HEALTH_MAX_WAIT_SECONDS:-240}"
  local poll_seconds="${DEPLOY_HEALTH_POLL_SECONDS:-5}"
  local start_time="$SECONDS"

  echo "Waiting for ${label}: ${url}"
  while true; do
    if curl -fsS --max-time 10 "$url"; then
      echo
      echo "${label} OK"
      return 0
    fi

    if (( SECONDS - start_time >= max_wait )); then
      echo "Timed out waiting ${max_wait}s for ${label}: ${url}" >&2
      return 1
    fi

    sleep "$poll_seconds"
  done
}

service_exists() {
  systemctl cat "$1" >/dev/null 2>&1
}

restart_service() {
  local service_name="$1"
  echo "Restarting ${service_name}"
  sudo -n systemctl restart "$service_name"
}

echo "== deploy_production.sh =="
echo "PROJECT_ROOT=$PROJECT_ROOT"
echo "TARGET_BRANCH=main"

git checkout main
git pull --ff-only origin main

DEPLOY_COMMIT_SHA="$(git rev-parse HEAD)"
echo "DEPLOY_COMMIT_SHA=$DEPLOY_COMMIT_SHA"

npm run build:frontend
mkdir -p "${PROJECT_ROOT}/packages/frontend/dist-data"
rsync -a --delete "${PROJECT_ROOT}/packages/frontend/dist/" "${PROJECT_ROOT}/packages/frontend/dist-data/"

if service_exists openecon-backend.service; then
  echo "Restarting backend via systemd"
  sudo -n systemctl daemon-reload
  restart_service openecon-backend.service
  if service_exists openecon-mcp.service; then
    restart_service openecon-mcp.service
  fi
else
  "$SCRIPT_DIR/start_backend.sh" production
fi

wait_for_url "local backend health" "http://localhost:3001/api/health"
if service_exists openecon-mcp.service; then
  wait_for_url "local MCP service health" "http://localhost:3002/api/health"
fi
wait_for_url "public backend health" "https://data.openecon.ai/api/health"

echo "DEPLOY_HEALTH_OK"
echo "DEPLOY_COMPLETE_SHA=$DEPLOY_COMMIT_SHA"
