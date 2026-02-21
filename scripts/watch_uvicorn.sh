#!/bin/bash
# Continuous monitoring for uvicorn processes
# Usage: ./watch_uvicorn.sh [interval_seconds]

INTERVAL=${1:-3}  # Default: check every 3 seconds
MAX_CHECKS=${2:-20}  # Default: 20 checks (1 minute)

echo "🔍 Watching uvicorn processes every ${INTERVAL}s for ${MAX_CHECKS} iterations..."
echo "Press Ctrl+C to stop"
echo ""

for i in $(seq 1 $MAX_CHECKS); do
    clear
    echo "==================================================================="
    echo "Check #${i}/${MAX_CHECKS} - $(date '+%H:%M:%S')"
    echo "==================================================================="

    # Count processes
    MAIN_COUNT=$(pgrep -f "uvicorn backend.main:app --host.*--reload" | wc -l)
    WORKER_COUNT=$(pgrep -f "multiprocessing.spawn" | wc -l)
    STANDALONE_COUNT=$(pgrep -f "uvicorn backend.main:app" | grep -v -f <(pgrep -f "reload") | wc -l)

    echo "📊 Process Counts:"
    echo "   Main servers (with --reload): $MAIN_COUNT"
    echo "   Reload workers: $WORKER_COUNT"
    echo "   Standalone processes (no --reload): $STANDALONE_COUNT"
    echo ""

    # Show all processes
    echo "🔍 Active Processes:"
    ps aux | grep -E "(uvicorn|multiprocessing)" | grep -v grep | grep -v "watch_uvicorn" | \
        awk '{printf "  PID %-7s CPU %5s%% MEM %5s%% UPTIME %-8s CMD: %s...\n",
              $2, $3, $4, $10, substr($0, index($0,$11), 80)}'

    echo ""
    echo "⚠️  ALERTS:"
    if [ "$MAIN_COUNT" -gt 1 ]; then
        echo "   🚨 WARNING: Multiple main servers detected! ($MAIN_COUNT)"
    fi
    if [ "$STANDALONE_COUNT" -gt 0 ]; then
        echo "   🚨 WARNING: Standalone processes without --reload detected!"
    fi
    if [ "$WORKER_COUNT" -gt 2 ]; then
        echo "   ⚠️  High worker count: $WORKER_COUNT (reload in progress?)"
    fi
    if [ "$MAIN_COUNT" -eq 0 ]; then
        echo "   ❌ ERROR: No main server running!"
    fi

    # Port status
    echo ""
    echo "🌐 Port 3001:"
    PORT_COUNT=$(lsof -i :3001 2>/dev/null | grep LISTEN | wc -l)
    echo "   Listeners: $PORT_COUNT"

    if [ $i -lt $MAX_CHECKS ]; then
        sleep $INTERVAL
    fi
done

echo ""
echo "==================================================================="
echo "✅ Monitoring complete"
echo "==================================================================="
