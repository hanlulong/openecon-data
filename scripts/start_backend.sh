#!/bin/bash
# Proper backend startup script for OpenEcon
# Usage: ./scripts/start_backend.sh [production|development]

set -euo pipefail

MODE=${1:-production}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
LOG_DIR="${PROJECT_ROOT}/.omx/logs"
LOCAL_VENV="${PROJECT_ROOT}/backend/.venv"
SHARED_VENV="/home/hanlulong/OpenEcon/backend/.venv"

if [ -d "$LOCAL_VENV" ]; then
  VENV_PATH="$LOCAL_VENV"
else
  VENV_PATH="$SHARED_VENV"
fi

mkdir -p "$LOG_DIR"
cd "$PROJECT_ROOT" || exit 1

echo "🧹 Cleaning up existing processes..."
lsof -ti:3001 2>/dev/null | xargs kill -9 2>/dev/null || true
sleep 2

source "${VENV_PATH}/bin/activate"

LOG_PATH="${LOG_DIR}/backend-${MODE}.log"
CMD=(uvicorn backend.main:app --host 0.0.0.0 --port 3001)
HEALTH_POLL_SECONDS="${HEALTH_POLL_SECONDS:-2}"
HEALTH_MAX_WAIT_SECONDS="${HEALTH_MAX_WAIT_SECONDS:-180}"
MAX_ATTEMPTS=$(( HEALTH_MAX_WAIT_SECONDS / HEALTH_POLL_SECONDS ))

if [ "$MODE" = "production" ]; then
  echo "🚀 Starting backend in PRODUCTION mode (no auto-reload)..."
elif [ "$MODE" = "development" ]; then
  echo "🔧 Starting backend in DEVELOPMENT mode (with auto-reload)..."
  CMD+=(--reload --reload-dir backend)
else
  echo "❌ Invalid mode: $MODE"
  echo "Usage: $0 [production|development]"
  exit 1
fi

nohup "${CMD[@]}" > "$LOG_PATH" 2>&1 &
BACKEND_PID=$!

HEALTH_OK=0
for _attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  if curl -s http://localhost:3001/api/health > /dev/null 2>&1; then
    HEALTH_OK=1
    break
  fi
  sleep "$HEALTH_POLL_SECONDS"
done

if [ "$HEALTH_OK" -eq 1 ]; then
  echo "✅ Backend started successfully"
  echo "   PID: $BACKEND_PID"
  echo "   Mode: $MODE"
  echo "   Logs: $LOG_PATH"
  echo ""
  echo "Monitor with: ps aux | grep uvicorn"
else
  echo "❌ Backend failed to start"
  echo "   Waited up to ${HEALTH_MAX_WAIT_SECONDS}s for /api/health"
  echo "Check logs: tail -50 $LOG_PATH"
  exit 1
fi
