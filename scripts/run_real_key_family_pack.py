#!/usr/bin/env python3
"""Run the broader manual family pack with real runtime keys and resumable output.

This avoids losing progress when long verification runs are interrupted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / ".omx" / "reports" / "phase1-manual-10-chain-inprocess-real-keys.json"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _ensure_runtime_python() -> None:
    venv_python = ROOT / "backend" / ".venv" / "bin" / "python"
    if not venv_python.exists():
        return
    if Path(sys.executable).resolve() == venv_python.resolve():
        return
    os.execv(str(venv_python), [str(venv_python), __file__, *sys.argv[1:]])


_ensure_runtime_python()


def _load_query_service():
    from backend.config import get_settings
    from backend.services.cache import cache_service
    from backend.services.query import QueryService

    settings = get_settings()
    cache_service.clear()
    return QueryService(
        openrouter_key=settings.openrouter_api_key,
        fred_key=settings.fred_api_key or "",
        comtrade_key=settings.comtrade_api_key or "",
    )


def _run_query(service: Any, query: str, conversation_id: str | None):
    from backend.tests.utils import run

    return run(service.process_query(query, conversation_id=conversation_id))


def _chain_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "StatsCan employment Ontario age chain",
            "queries": [
                "Canada employment rate",
                "Show by province",
                "Show only Ontario",
                "Show by age group",
                "Show only 25-54",
            ],
            "check": lambda rs: (
                rs[-1].intent.apiProvider == "STATSCAN"
                and not rs[-1].clarificationNeeded
                and len(rs[-1].data or []) == 1
                and "25 to 54" in ((rs[-1].data or [])[0].metadata.indicator or "")
            ),
        },
        {
            "name": "Mixed provider collective chain",
            "queries": [
                "US GDP from FRED",
                "Japan GDP from World Bank",
                "Germany GDP from Eurostat",
                "China GDP from IMF",
                "Canada GDP from Statistics Canada",
                "Switch all to GDP growth rate",
                "Change to 2020-2025",
                "Show only US and China",
                "Switch to per capita GDP",
                "Add Germany back",
            ],
            "check": lambda rs: (
                rs[5].intent.apiProvider == "MULTI"
                and len(rs[5].data or []) >= 5
                and rs[6].intent.apiProvider == "MULTI"
                and len(rs[6].data or []) >= 5
                and rs[7].intent.apiProvider == "MULTI"
                and len(rs[7].data or []) == 2
                and rs[8].intent.apiProvider == "MULTI"
                and len(rs[8].data or []) >= 2
                and rs[9].intent.apiProvider == "MULTI"
                and len(rs[9].data or []) >= 3
            ),
        },
        {
            "name": "IMF provider lock chain",
            "queries": [
                "US GDP growth rate",
                "Add Canada GDP growth rate",
                "Switch to IMF",
                "Show only Canada",
                "Add Japan GDP growth rate",
            ],
            "check": lambda rs: all(r.intent.apiProvider == "IMF" for r in rs[2:])
            and len(rs[-1].data or []) >= 2,
        },
        {
            "name": "Inflation provider swap chain",
            "queries": [
                "US inflation rate",
                "Add Canada inflation rate",
                "Add United Kingdom inflation rate",
                "Show only Canada and United Kingdom",
                "Switch to World Bank data",
                "Show only France and Germany",
            ],
            "check": lambda rs: (not rs[-1].clarificationNeeded) and (len(rs[-1].data or []) == 2),
        },
        {
            "name": "GDP deep dive chain",
            "queries": [
                "US GDP",
                "Add China GDP",
                "Add Germany GDP",
                "Switch to per capita GDP",
                "Remove China",
                "Switch to GDP growth rate",
                "Show from IMF instead",
                "Change time range to 2015-2024",
                "Add Japan",
            ],
            "check": lambda rs: rs[-1].intent.apiProvider == "IMF"
            and len(rs[-1].data or []) == 3,
        },
        {
            "name": "Unemployment Ontario age chain",
            "queries": [
                "Canada unemployment rate",
                "Show by province",
                "Show only Ontario",
                "Show by age group",
                "Show only 25-54",
            ],
            "check": lambda rs: rs[-1].intent.apiProvider == "STATSCAN"
            and not rs[-1].clarificationNeeded
            and len(rs[-1].data or []) == 1,
        },
        {
            "name": "Trade bilateral chain",
            "queries": [
                "US exports to Canada",
                "Switch to trade balance US and Mexico",
                "Change partner to China",
                "Switch to imports",
            ],
            "check": lambda rs: (not rs[-1].clarificationNeeded) and (len(rs[-1].data or []) >= 1),
        },
        {
            "name": "Eurostat labour-debt chain",
            "queries": [
                "Germany employment rate from Eurostat",
                "Add Spain employment rate",
                "Switch to government debt to GDP",
                "Remove Spain",
                "Add Italy",
            ],
            "check": lambda rs: (not rs[-1].clarificationNeeded) and (len(rs[-1].data or []) == 2),
        },
        {
            "name": "Crypto comparison chain",
            "queries": [
                "Bitcoin price",
                "Add Ethereum price",
                "Show only Bitcoin",
                "Add Solana price",
                "Show only Bitcoin and Solana",
            ],
            "check": lambda rs: (not rs[-1].clarificationNeeded)
            and len({(s.metadata.source, s.metadata.seriesId) for s in (rs[-1].data or [])}) == 2,
        },
        {
            "name": "Exchange rate chain",
            "queries": [
                "USD to JPY exchange rate",
                "Switch to EUR/USD",
                "Show last 5 years",
            ],
            "check": lambda rs: (not rs[-1].clarificationNeeded)
            and (rs[-1].error is None)
            and len(rs[-1].data or []) >= 1,
        },
    ]


def _load_existing() -> dict[str, Any]:
    if REPORT_PATH.exists():
        return json.loads(REPORT_PATH.read_text())
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "mode": "in_process_manual_10_chain_real_keys",
        "results": [],
    }


def _save(report: dict[str, Any]) -> None:
    report["passed_chains"] = sum(1 for r in report["results"] if r.get("passed"))
    report["total_chains"] = len(report["results"])
    report["pass_rate"] = round(
        report["passed_chains"] / report["total_chains"], 3
    ) if report["total_chains"] else 0.0
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2) + "\n")


def _round_payload(response: Any, query: str, elapsed: float) -> dict[str, Any]:
    intent = response.intent
    return {
        "query": query,
        "provider": getattr(intent, "apiProvider", None) if intent else None,
        "indicators": getattr(intent, "indicators", None) if intent else None,
        "needsDecomposition": getattr(intent, "needsDecomposition", None) if intent else None,
        "decompositionType": getattr(intent, "decompositionType", None) if intent else None,
        "clarificationNeeded": response.clarificationNeeded,
        "error": response.error,
        "series_count": len(response.data or []),
        "series_preview": [
            {
                "source": s.metadata.source,
                "indicator": s.metadata.indicator,
                "country": s.metadata.country,
                "seriesId": s.metadata.seriesId,
            }
            for s in (response.data or [])[:5]
        ],
        "elapsed_s": round(elapsed, 2),
    }


def run_family(service: Any, family: dict[str, Any]) -> dict[str, Any]:
    conversation_id = None
    responses = []
    rounds = []
    for query in family["queries"]:
        started = time.time()
        response = _run_query(service, query, conversation_id)
        conversation_id = response.conversationId
        responses.append(response)
        rounds.append(_round_payload(response, query, time.time() - started))
    try:
        passed = bool(family["check"](responses))
    except Exception:
        passed = False
    return {
        "name": family["name"],
        "passed": passed,
        "rounds": rounds,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--family",
        action="append",
        help="Run only selected family name(s). Can be passed multiple times.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip families already present in the report.",
    )
    args = parser.parse_args()

    families = _chain_definitions()
    if args.family:
        requested = set(args.family)
        families = [family for family in families if family["name"] in requested]

    report = _load_existing()
    existing_by_name = {result["name"]: result for result in report.get("results", [])}
    service = _load_query_service()

    for family in families:
        if args.resume and family["name"] in existing_by_name:
            continue
        result = run_family(service, family)
        existing_by_name[family["name"]] = result
        report["results"] = [
            existing_by_name[name]
            for name in sorted(existing_by_name.keys())
        ]
        _save(report)

    print(json.dumps({
        "report": str(REPORT_PATH),
        "passed_chains": report.get("passed_chains"),
        "total_chains": report.get("total_chains"),
        "pass_rate": report.get("pass_rate"),
        "failed": [r["name"] for r in report["results"] if not r.get("passed")],
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
