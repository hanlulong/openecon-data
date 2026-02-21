#!/bin/bash
# Proper backend startup script for econ-data-mcp
# Usage: ./scripts/start_backend.sh [production|development]

MODE=${1:-production}
PROJECT_ROOT="/home/hanlulong/econ-data-mcp"

cd "$PROJECT_ROOT" || exit 1

# Kill any existing uvicorn processes
echo "🧹 Cleaning up existing processes..."
lsof -ti:3001 2>/dev/null | xargs kill -9 2>/dev/null
sleep 2

# Activate virtual environment
source backend/.venv/bin/activate

if [ "$MODE" = "production" ]; then
    echo "🚀 Starting backend in PRODUCTION mode (no auto-reload)..."
    nohup uvicorn backend.main:app \
        --host 0.0.0.0 \
        --port 3001 \
        > /tmp/backend-production.log 2>&1 &

elif [ "$MODE" = "development" ]; then
    echo "🔧 Starting backend in DEVELOPMENT mode (with auto-reload)..."
    nohup uvicorn backend.main:app \
        --host 0.0.0.0 \
        --port 3001 \
        --reload \
        --reload-dir backend \
        > /tmp/backend-development.log 2>&1 &
else
    echo "❌ Invalid mode: $MODE"
    echo "Usage: $0 [production|development]"
    exit 1
fi

BACKEND_PID=$!
sleep 5

# Verify startup
if curl -s http://localhost:3001/api/health > /dev/null 2>&1; then
    echo "✅ Backend started successfully"
    echo "   PID: $BACKEND_PID"
    echo "   Mode: $MODE"
    echo "   Logs: /tmp/backend-$MODE.log"
    echo ""
    echo "Monitor with: ps aux | grep uvicorn"
else
    echo "❌ Backend failed to start"
    echo "Check logs: tail -50 /tmp/backend-$MODE.log"
    exit 1
fi
