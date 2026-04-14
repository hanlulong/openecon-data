#!/usr/bin/env bash
set -euo pipefail

EXPECTED_SHA="${1:-}"
REPO_ROOT="/home/hanlulong/OpenEcon"
PROD_HEALTH_URL="https://data.openecon.ai/api/health"
PROD_QUERY_URL="https://data.openecon.ai/api/query"
LOG_PATH="/tmp/openecon_after_hours_push_and_verify.log"
SMOKE_JSON="/tmp/openecon_after_hours_push_and_verify_smoke.json"

exec > >(tee -a "$LOG_PATH") 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z %z')] starting after-hours push/verify"
cd "$REPO_ROOT"

CURRENT_SHA="$('/usr/bin/git' rev-parse HEAD)"
echo "current HEAD: $CURRENT_SHA"
if [[ -n "$EXPECTED_SHA" && "$CURRENT_SHA" != "$EXPECTED_SHA" ]]; then
  echo "expected HEAD $EXPECTED_SHA but found $CURRENT_SHA; aborting"
  exit 1
fi

/usr/bin/git push origin main

echo "push complete; polling production health"
for i in $(seq 1 30); do
  if /usr/bin/curl -sf --max-time 10 "$PROD_HEALTH_URL" > /tmp/openecon_after_hours_prod_health.json; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S %Z %z')] production health OK"
    cat /tmp/openecon_after_hours_prod_health.json
    break
  fi
  echo "[$(date '+%Y-%m-%d %H:%M:%S %Z %z')] waiting for production health ($i/30)"
  sleep 20
  if [[ "$i" == "30" ]]; then
    echo "production health did not become ready in allotted time"
    exit 1
  fi
done

backend/.venv/bin/python - <<'PY'
import json
import requests
import sys
from datetime import datetime, timezone

PROD_QUERY_URL = "https://data.openecon.ai/api/query"
SMOKE_JSON = "/tmp/openecon_after_hours_push_and_verify_smoke.json"

def run_sequence(name, steps):
    conv = None
    results = []
    for step in steps:
        payload = {"query": step["query"]}
        if conv:
            payload["conversationId"] = conv
        response = requests.post(PROD_QUERY_URL, json=payload, timeout=120)
        data = response.json()
        conv = data.get("conversationId") or data.get("conversation_id") or conv
        series = data.get("data") or []
        providers = sorted({(item.get("metadata") or {}).get("source", "") for item in series})
        row = {
            "query": step["query"],
            "status_code": response.status_code,
            "error": data.get("error"),
            "series_count": len(series),
            "providers": providers,
        }
        if row["status_code"] != 200:
            raise SystemExit(f"{name}: non-200 for {step['query']}: {row}")
        if row["error"]:
            raise SystemExit(f"{name}: API error for {step['query']}: {row}")
        if row["series_count"] < step.get("min_series_count", 1):
            raise SystemExit(f"{name}: too few series for {step['query']}: {row}")
        required_provider = step.get("required_provider")
        if required_provider and required_provider not in providers:
            raise SystemExit(f"{name}: required provider {required_provider} missing for {step['query']}: {row}")
        results.append(row)
    return results

report = {
    "timestamp": datetime.now(timezone.utc).isoformat(),
    "sequences": {
        "gdp_variant_smoke": run_sequence("gdp_variant_smoke", [
            {"query": "US real GDP", "min_series_count": 1},
            {"query": "Switch to GDP per capita", "min_series_count": 1},
            {"query": "Switch to GDP growth rate", "min_series_count": 1},
            {"query": "Switch to GDP deflator", "min_series_count": 1},
        ]),
        "imf_current_account_smoke": run_sequence("imf_current_account_smoke", [
            {"query": "BIS credit to GDP ratio", "min_series_count": 1},
            {"query": "Narrow to US credit to GDP", "min_series_count": 1},
            {"query": "Add China credit to GDP", "min_series_count": 2},
            {"query": "Switch to IMF GDP growth rate", "min_series_count": 2, "required_provider": "IMF"},
            {"query": "Add Germany GDP growth", "min_series_count": 3, "required_provider": "IMF"},
            {"query": "Switch to current account balance", "min_series_count": 3, "required_provider": "IMF"},
            {"query": "Change to 2018-2024", "min_series_count": 3, "required_provider": "IMF"},
            {"query": "Remove Germany", "min_series_count": 2, "required_provider": "IMF"},
        ]),
    },
}

with open(SMOKE_JSON, "w", encoding="utf-8") as f:
    json.dump(report, f, indent=2)

print(json.dumps(report, indent=2))
PY

echo "[$(date '+%Y-%m-%d %H:%M:%S %Z %z')] after-hours push/verify complete"
echo "smoke report: $SMOKE_JSON"
