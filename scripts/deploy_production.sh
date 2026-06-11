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

# Pro Mode sandbox Layer-2 provisioning (idempotent). Creates the dedicated
# 'promode' uid, shared group, scoped sudoers, and uid-scoped egress allowlist.
# Non-fatal: if it fails, Pro Mode falls back to Layer-1 mount isolation (which
# already hides all secrets); we only lose the SSRF/loopback egress restriction.
if [ -x "${SCRIPT_DIR}/setup_promode_sandbox.sh" ]; then
  echo "Provisioning Pro Mode sandbox (Layer 2)"
  PROMODE_PUBLIC_DIR="${PROMODE_PUBLIC_DIR:-${PROJECT_ROOT}/public_media/promode}" \
  PROMODE_SESSION_DIR="${PROMODE_SESSION_DIR:-/tmp/promode_sessions}" \
  BACKEND_USER="${BACKEND_USER:-hanlulong}" \
    sudo -n -E bash "${SCRIPT_DIR}/setup_promode_sandbox.sh" \
    || echo "WARNING: Pro Mode Layer-2 provisioning failed; continuing with Layer-1-only." >&2
fi

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

wait_for_url "local backend health" "http://127.0.0.1:3001/api/health"
if service_exists openecon-mcp.service; then
  wait_for_url "local MCP service health" "http://127.0.0.1:3002/api/health"
fi
wait_for_url "public backend health" "https://data.openecon.ai/api/health"

echo "DEPLOY_HEALTH_OK"
echo "DEPLOY_COMPLETE_SHA=$DEPLOY_COMMIT_SHA"
