#!/bin/bash
# Monitor uvicorn processes

echo "==================================================================="
echo "econ-data-mcp Backend Process Monitor"
echo "==================================================================="
echo ""

echo "📊 Current Time: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

echo "🔍 All Uvicorn-related Processes:"
echo "-------------------------------------------------------------------"
ps aux | grep -E "(uvicorn|backend)" | grep -v grep | \
  awk '{printf "PID: %-8s CPU: %5s%% MEM: %5s%% TIME: %-10s CMD: %s\n", $2, $3, $4, $10, substr($0, index($0,$11))}'
echo ""

echo "🌳 Process Tree (if uvicorn is running):"
echo "-------------------------------------------------------------------"
MAIN_PID=$(pgrep -f "uvicorn backend.main:app" | head -1)
if [ -n "$MAIN_PID" ]; then
    pstree -p "$MAIN_PID" 2>/dev/null || echo "pstree not available"
    echo ""
    echo "Main PID: $MAIN_PID"
    echo "Uptime: $(ps -p "$MAIN_PID" -o etime= 2>/dev/null | xargs)"
else
    echo "❌ No uvicorn process found!"
fi
echo ""

echo "🌐 Port 3001 Status:"
echo "-------------------------------------------------------------------"
PORT_INFO=$(ss -tlnp 2>/dev/null | grep :3001)
if [ -n "$PORT_INFO" ]; then
    echo "$PORT_INFO"
    echo ""
    echo "Processes using port 3001:"
    lsof -i :3001 2>/dev/null | grep -v COMMAND | \
      awk '{printf "  %-10s PID: %-8s User: %-10s Type: %s\n", $1, $2, $3, $8}'
else
    echo "❌ Nothing listening on port 3001!"
fi
echo ""

echo "📝 Recent Backend Logs (last 5 lines):"
echo "-------------------------------------------------------------------"
if [ -f /tmp/backend-oecd-fix.log ]; then
    tail -5 /tmp/backend-oecd-fix.log | grep -E "(INFO|ERROR|WARNING)" | tail -5
else
    echo "❌ Log file not found: /tmp/backend-oecd-fix.log"
fi
echo ""

echo "💾 Memory Usage Summary:"
echo "-------------------------------------------------------------------"
ps aux | grep -E "(uvicorn|python3.*backend)" | grep -v grep | \
  awk '{mem+=$6} END {printf "Total RSS: %.2f MB\n", mem/1024}'
echo ""

echo "==================================================================="
