#!/usr/bin/env bash
# heartbeat.sh - Quick health check for OpenEcon services
# Usage: ./scripts/heartbeat.sh [--json]
#
# Checks:
#   1. Backend API health endpoint
#   2. Frontend accessibility
#   3. Backend process running
#   4. Disk space
#   5. Recent error count in logs

set -euo pipefail

JSON_OUTPUT=false
[[ "${1:-}" == "--json" ]] && JSON_OUTPUT=true

BACKEND_URL="${BACKEND_URL:-http://localhost:3001}"
FRONTEND_URL="${FRONTEND_URL:-http://localhost:5173}"
PROD_URL="${PROD_URL:-https://data.openecon.ai}"

# Colors (disabled for JSON mode)
if $JSON_OUTPUT; then
  RED="" GREEN="" YELLOW="" NC=""
else
  RED='\033[0;31m' GREEN='\033[0;32m' YELLOW='\033[1;33m' NC='\033[0m'
fi

STATUS_BACKEND="unknown"
STATUS_FRONTEND="unknown"
STATUS_PROD="unknown"
STATUS_DISK="unknown"
BACKEND_DETAILS=""
ERROR_COUNT=0
WARNINGS=()

# 1. Backend health
if response=$(curl -sf --max-time 5 "${BACKEND_URL}/api/health" 2>/dev/null); then
  STATUS_BACKEND="healthy"
  BACKEND_DETAILS="$response"
else
  STATUS_BACKEND="down"
  WARNINGS+=("Backend is not responding at ${BACKEND_URL}")
fi

# 2. Frontend (dev)
if curl -sf --max-time 5 "${FRONTEND_URL}" >/dev/null 2>&1; then
  STATUS_FRONTEND="healthy"
else
  STATUS_FRONTEND="down"
  WARNINGS+=("Frontend dev server is not responding at ${FRONTEND_URL}")
fi

# 3. Production site
if curl -sf --max-time 10 "${PROD_URL}/api/health" >/dev/null 2>&1; then
  STATUS_PROD="healthy"
else
  STATUS_PROD="down"
  WARNINGS+=("Production site is not responding at ${PROD_URL}")
fi

# 4. Disk space
DISK_USAGE=$(df -h / | awk 'NR==2 {print $5}' | tr -d '%')
if (( DISK_USAGE > 90 )); then
  STATUS_DISK="critical"
  WARNINGS+=("Disk usage is at ${DISK_USAGE}%")
elif (( DISK_USAGE > 80 )); then
  STATUS_DISK="warning"
  WARNINGS+=("Disk usage is at ${DISK_USAGE}%")
else
  STATUS_DISK="healthy"
fi

# 5. Recent errors in backend log
if [[ -f /tmp/backend-dev.log ]]; then
  ERROR_COUNT=$(grep -ci "error\|exception\|traceback" /tmp/backend-dev.log 2>/dev/null | tail -1 || echo 0)
fi

# Output
if $JSON_OUTPUT; then
  cat <<EOF
{
  "timestamp": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "backend": "$STATUS_BACKEND",
  "frontend": "$STATUS_FRONTEND",
  "production": "$STATUS_PROD",
  "disk": "$STATUS_DISK",
  "disk_usage_pct": $DISK_USAGE,
  "log_error_count": $ERROR_COUNT,
  "warning_count": ${#WARNINGS[@]},
  "warnings": [$(printf '"%s",' "${WARNINGS[@]:-}" | sed 's/,$//' )]
}
EOF
else
  echo ""
  echo "=== OpenEcon Heartbeat ==="
  echo "Time: $(date)"
  echo ""
  [[ "$STATUS_BACKEND" == "healthy" ]] && echo -e "${GREEN}[OK]${NC} Backend" || echo -e "${RED}[FAIL]${NC} Backend"
  [[ "$STATUS_FRONTEND" == "healthy" ]] && echo -e "${GREEN}[OK]${NC} Frontend (dev)" || echo -e "${YELLOW}[DOWN]${NC} Frontend (dev)"
  [[ "$STATUS_PROD" == "healthy" ]] && echo -e "${GREEN}[OK]${NC} Production" || echo -e "${RED}[FAIL]${NC} Production"
  [[ "$STATUS_DISK" == "healthy" ]] && echo -e "${GREEN}[OK]${NC} Disk (${DISK_USAGE}%)" || echo -e "${YELLOW}[WARN]${NC} Disk (${DISK_USAGE}%)"
  echo "Log errors: $ERROR_COUNT"
  if (( ${#WARNINGS[@]} > 0 )); then
    echo ""
    echo "Warnings:"
    for w in "${WARNINGS[@]}"; do
      echo "  - $w"
    done
  fi
  echo ""
fi
