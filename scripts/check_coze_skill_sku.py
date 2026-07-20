#!/usr/bin/env python3
"""Tripwire: is the Coze skill "OpenEcon 开源经济数据" still FREE?

Background (2026-07-20): an individual Coze creator ("saka",
user5104375419) wrapped our public API into a Coze skill with ~9.8K users —
it drives ~80% of our real traffic. The skill is currently a FREE SKU. If it
ever flips to PAID, someone else is charging for our free service — the
agreed tripwire for escalating the monetization plan
(memory: project_monetization_plan).

The Coze product API requires ByteDance's browser-side request signing
(msToken/a_bogus), so this check drives headless Chrome to render the
product page's API call and inspects the result. Run monthly (maintenance
loop) or on demand:

    python3 scripts/check_coze_skill_sku.py

Exit 0 = still free. Exit 2 = PAID DETECTED (act!). Exit 1 = check failed
(page shape changed / network) — re-verify manually at
https://www.coze.cn/skill-store?industry=7641591949430112266 ("OpenEcon
开源经济数据" card should say 免费).
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile

PRODUCT_ID = "7641536143707914259"
LISTING_URL = "https://www.coze.cn/skill-store?industry=7641591949430112266"
CHROME = "/usr/bin/google-chrome"

# Render the listing page (public, no login) and dump the DOM after JS
# settles; the card text carries 免费/paid pricing and the 人在用 counter.
CMD = [
    CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
    "--virtual-time-budget=15000", "--window-size=1400,2000",
    "--dump-dom", LISTING_URL,
]


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            out = subprocess.run(
                CMD + [f"--user-data-dir={tmp}"],
                capture_output=True, text=True, timeout=90,
            ).stdout
    except Exception as exc:  # noqa: BLE001
        print(f"CHECK FAILED (chrome): {exc}")
        return 1

    idx = out.find("OpenEcon")
    if idx < 0:
        print("CHECK FAILED: skill card not found on the listing page "
              "(page shape changed, or the skill was unlisted — verify "
              f"manually: {LISTING_URL})")
        return 1

    window = out[idx : idx + 1200]
    is_free = "免费" in window
    # usage counter e.g. 9.8K人在用 / 1.2w人在用
    import re
    m = re.search(r"([\d.]+[KkWw万]?)\s*人在用", window)
    users = m.group(1) if m else "?"

    result = {"skill": "OpenEcon 开源经济数据", "product_id": PRODUCT_ID,
              "free": is_free, "users": users}
    print(json.dumps(result, ensure_ascii=False))
    if not is_free:
        print("⚠️ TRIPWIRE: the skill no longer shows 免费 — someone may be "
              "charging for our data. Escalate per the monetization plan.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
