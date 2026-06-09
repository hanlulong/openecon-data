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

stop_stale_3001_guards() {
  # Certification/replay jobs may leave a temporary guard that kills anything
  # binding production port 3001.  Production deploys must remove that guard
  # before starting uvicorn, otherwise the fresh backend can be SIGKILLed during
  # startup and Apache will return 503.
  local guard_name="kill_stray_3001_guard.py"
  local pid_file="${LOG_DIR}/kill_stray_3001_guard.pid"
  local candidate

  if [ -f "$pid_file" ]; then
    candidate="$(cat "$pid_file" 2>/dev/null || true)"
    if [ -n "$candidate" ] && [ -r "/proc/${candidate}/cmdline" ]; then
      if tr '\0' ' ' < "/proc/${candidate}/cmdline" | grep -q "$guard_name"; then
        echo "🛑 Stopping stale 3001 guard (PID: $candidate)..."
        kill "$candidate" 2>/dev/null || true
      fi
    fi
  fi

  for candidate in /proc/[0-9]*; do
    [ -r "${candidate}/cmdline" ] || continue
    if tr '\0' ' ' < "${candidate}/cmdline" | grep -q "${PROJECT_ROOT}/.omx/logs/${guard_name}"; then
      candidate="${candidate#/proc/}"
      if [ "$candidate" != "$$" ]; then
        echo "🛑 Stopping stale 3001 guard (PID: $candidate)..."
        kill "$candidate" 2>/dev/null || true
      fi
    fi
  done
}

stop_stale_3001_guards

echo "🧹 Cleaning up existing processes..."
lsof -ti:3001 2>/dev/null | xargs kill -9 2>/dev/null || true
sleep 2

source "${VENV_PATH}/bin/activate"

LOG_PATH="${LOG_DIR}/backend-${MODE}.log"
CMD=(uvicorn backend.main:app --host 127.0.0.1 --port 3001)
HEALTH_POLL_SECONDS="${HEALTH_POLL_SECONDS:-2}"
HEALTH_MAX_WAIT_SECONDS="${HEALTH_MAX_WAIT_SECONDS:-180}"
MAX_ATTEMPTS=$(( HEALTH_MAX_WAIT_SECONDS / HEALTH_POLL_SECONDS ))

if [ "$MODE" = "production" ]; then
  echo "🚀 Starting backend in PRODUCTION mode (no auto-reload)..."
  UVICORN_WORKERS="${UVICORN_WORKERS:-2}"
  echo "   Workers: $UVICORN_WORKERS"
  CMD+=(--workers "$UVICORN_WORKERS")
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
  if curl -s http://127.0.0.1:3001/api/health > /dev/null 2>&1; then
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
