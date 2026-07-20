#!/usr/bin/env python3
"""Record REAL upstream payload samples for the ChinaMacro provider tests.

Fetches a small number of rows from every live endpoint the provider uses
(EastMoney datacenter reports, the EastMoney treasury-yield report, and the
MOFCOM social-financing query) and writes them verbatim to
backend/tests/fixtures/chinamacro_live_samples.json.

Project rule: offline tests replay RECORDED real responses — never invented
mocks. Re-run this script to refresh the fixtures when upstream drifts:

    python3 scripts/record_chinamacro_fixtures.py
"""
from __future__ import annotations

import json
import ssl
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "backend" / "tests" / "fixtures" / "chinamacro_live_samples.json"

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) econ-data-mcp/1.0"}

EASTMONEY_V1 = "https://datacenter-web.eastmoney.com/api/data/v1/get"
EASTMONEY_OLD = "https://datacenter.eastmoney.com/api/data/get"
MOFCOM_SHRZGM = "https://data.mofcom.gov.cn/datamofcom/front/gnmy/shrzgmQuery"

# reportName -> sample size. Small samples keep the fixture readable while
# covering every field the parsers consume.
EASTMONEY_REPORTS = {
    "RPT_ECONOMY_PMI": 4,
    "RPT_ECONOMY_CURRENCY_SUPPLY": 4,
    "RPT_ECONOMY_RMB_LOAN": 4,
    "RPT_ECONOMY_CPI": 4,
    "RPT_ECONOMY_PPI": 4,
    "RPT_ECONOMY_TOTAL_RETAIL": 4,
    "RPT_ECONOMY_INDUS_GROW": 4,
    "RPT_ECONOMY_ASSET_INVEST": 4,
    "RPT_ECONOMY_GDP": 4,
    "RPT_ECONOMY_GOLD_CURRENCY": 4,
    "RPT_ECONOMY_CUSTOMS": 4,
    "RPT_ECONOMY_FAITH_INDEX": 4,
    # (report, sort_column) tuples for non-REPORT_DATE reports
    "RPTA_WEB_RATE": 4,
}
REPORT_SORT_OVERRIDES = {"RPTA_WEB_RATE": "TRADE_DATE"}
TREASURY_TOKEN = "894050c76af8597a853f5b408b759f5d"  # fixed public token (vendored)


def _get(url: str, timeout: float = 20.0) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _post(url: str, timeout: float = 20.0) -> str:
    # MOFCOM's server negotiates only legacy TLS ciphers (old Tomcat); modern
    # OpenSSL defaults (SECLEVEL=2) refuse the handshake. Lower the cipher
    # security level for THIS request only — certificate verification stays on.
    ctx = ssl.create_default_context()
    ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    req = urllib.request.Request(url, data=b"", headers=UA, method="POST")
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read().decode("utf-8")


def main() -> int:
    samples: dict[str, object] = {}

    for report, n in EASTMONEY_REPORTS.items():
        qs = urllib.parse.urlencode({
            "reportName": report,
            "columns": "ALL",
            "pageSize": n,
            "sortColumns": REPORT_SORT_OVERRIDES.get(report, "REPORT_DATE"),
            "sortTypes": "-1",
        })
        raw = _get(f"{EASTMONEY_V1}?{qs}")
        samples[report] = json.loads(raw)
        print(f"recorded {report}: {len(samples[report]['result']['data'])} rows")

    qs = urllib.parse.urlencode({
        "type": "RPTA_WEB_TREASURYYIELD", "sty": "ALL", "st": "SOLAR_DATE",
        "sr": "-1", "token": TREASURY_TOKEN, "p": 1, "ps": 4,
        "pageNo": 1, "pageNum": 1,
    })
    samples["RPTA_WEB_TREASURYYIELD"] = json.loads(_get(f"{EASTMONEY_OLD}?{qs}"))
    print("recorded RPTA_WEB_TREASURYYIELD")

    samples["MOFCOM_SHRZGM"] = json.loads(_post(MOFCOM_SHRZGM))[:6]
    print(f"recorded MOFCOM_SHRZGM: {len(samples['MOFCOM_SHRZGM'])} rows")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(samples, ensure_ascii=False, indent=1, sort_keys=True))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
