#!/usr/bin/env python3
"""
Framework-level 100-query sweep for OpenEcon.

Goals:
- Stress the end-to-end query framework with complex macro/trade/finance prompts
- Detect systemic failures (routing, clarification, semantic mismatch, no-data, timeout)
- Keep a tracked TODO markdown that is updated every run

Usage:
  python scripts/framework_sweep_100.py --local
  python scripts/framework_sweep_100.py --local --concurrency 3 --timeout 45
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import httpx


LOCAL_URL = "http://localhost:3001/api/query"
PROD_URL = "https://openecon.ai/api/query"

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "docs" / "testing" / "reports"
TODO_PATH = ROOT / "docs" / "testing" / "FRAMEWORK_SWEEP_100_TODO.md"
LATEST_JSON = REPORT_DIR / "framework_sweep_100_latest.json"


def _norm_provider(source: Optional[str]) -> str:
    if not source:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9]+", "", source).upper()
    aliases = {
        "UNCOMTRADE": "COMTRADE",
        "UNCOMTRADECOMTRADE": "COMTRADE",
        "FREDFEDERALRESERVE": "FRED",
        "WORLDBANKGROUP": "WORLDBANK",
        "STATISTICSCANADA": "STATSCAN",
        "STATISTICSCANADASTATSCAN": "STATSCAN",
    }
    return aliases.get(cleaned, cleaned)


def _text_tokens(text: str) -> Set[str]:
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def infer_query_cues(query: str) -> Set[str]:
    text = (query or "").lower()
    cues: Set[str] = set()
    cue_map = {
        "imports": [" import ", "imports", "imported", "import share", "import ratio"],
        "exports": [" export ", "exports", "exported", "export share", "export ratio"],
        "trade_balance": ["trade balance", "trade deficit", "trade surplus", "net exports"],
        "gdp": [" gdp", "gross domestic product", "output"],
        "unemployment": ["unemployment", "jobless"],
        "inflation": ["inflation", "cpi", "consumer prices", "hicp", "ppi"],
        "debt": ["debt", "liabilities", "fiscal deficit", "public deficit"],
        "interest_rate": ["policy rate", "interest rate", "fed funds", "refinancing rate", "yield"],
        "exchange_rate": ["exchange rate", "fx", "forex", "currency"],
        "money_supply": ["money supply", "m1", "m2", "m3", "monetary aggregate"],
        "reserves": ["foreign reserves", "fx reserves", "reserve assets"],
        "house_prices": ["house prices", "property prices", "housing prices", "real estate"],
        "crypto": ["bitcoin", "ethereum", "crypto", "cryptocurrency", "market cap"],
        "wages": ["wage", "earnings", "salary"],
        "credit": ["credit", "bank lending", "private sector credit"],
    }
    for cue, patterns in cue_map.items():
        if any(p in text for p in patterns):
            cues.add(cue)
    return cues


def infer_series_cues(metadata: Dict[str, Any]) -> Set[str]:
    indicator = str(metadata.get("indicator") or "")
    series_id = str(metadata.get("seriesId") or "")
    text = f"{indicator} {series_id}".lower()
    cues: Set[str] = set()

    if re.search(r"\bimport(s)?\b|\.imp\.|_imp_|^imp", text):
        cues.add("imports")
    if re.search(r"\bexport(s)?\b|\.exp\.|_exp_|^exp", text):
        cues.add("exports")
    if re.search(r"trade\s*\(% of gdp\)|trade openness|openness", text):
        cues.add("trade_open")
    if re.search(r"trade balance|external balance|surplus|deficit|net export|bopgstb|ne\.rsb", text):
        cues.add("trade_balance")
    if re.search(r"\bgdp\b|gross domestic product|ngdp|ny\.gdp", text):
        cues.add("gdp")
    if re.search(r"unemployment|jobless|une_rt|lur|unrate", text):
        cues.add("unemployment")
    if re.search(r"inflation|cpi|consumer price|hicp|ppi|pcpipch|fp\.cpi", text):
        cues.add("inflation")
    if re.search(r"debt|deficit|liabilit|ggxwdg|gc\.dod", text):
        cues.add("debt")
    if re.search(r"interest|policy rate|yield|fedfunds|ws_cbpol|ei_mfir", text):
        cues.add("interest_rate")
    if re.search(r"exchange|forex|currency|dex|xru|reer", text):
        cues.add("exchange_rate")
    if re.search(r"money supply|m1|m2|m3|monetary", text):
        cues.add("money_supply")
    if re.search(r"reserve", text):
        cues.add("reserves")
    if re.search(r"house|housing|property|real estate|hpi|ws_spp", text):
        cues.add("house_prices")
    if re.search(r"bitcoin|ethereum|crypto|coin", text):
        cues.add("crypto")
    if re.search(r"wage|earning|salary", text):
        cues.add("wages")
    if re.search(r"credit|lending|loan", text):
        cues.add("credit")
    return cues


def semantic_mismatch(query: str, metadata: Dict[str, Any]) -> Optional[str]:
    q_cues = infer_query_cues(query)
    if not q_cues:
        return None

    s_cues = infer_series_cues(metadata)
    if not s_cues:
        return None

    directional = {"imports", "exports", "trade_balance"}
    q_dir = q_cues & directional
    if q_dir and not (q_dir & s_cues):
        if q_dir == {"imports", "exports"} and "trade_open" in s_cues:
            return None
        return f"directional_mismatch query={sorted(q_dir)} series={sorted(s_cues & directional)}"

    # For strongly signaled economic concepts, require overlap.
    strong = {
        "unemployment",
        "inflation",
        "debt",
        "interest_rate",
        "exchange_rate",
        "money_supply",
        "reserves",
        "house_prices",
        "crypto",
        "wages",
    }
    q_strong = q_cues & strong
    if q_strong and not (q_strong & s_cues):
        return f"concept_mismatch query={sorted(q_strong)} series={sorted(s_cues)}"
    return None


@dataclass
class QueryCase:
    id: int
    category: str
    query: str
    expected_provider: Optional[str] = None


@dataclass
class SweepResult:
    id: int
    category: str
    query: str
    status: str  # pass|warn|fail|error|timeout
    failure_type: Optional[str]
    expected_provider: Optional[str]
    provider_returned: Optional[str]
    indicator_returned: Optional[str]
    country_returned: Optional[str]
    response_time_ms: float
    series_count: int
    non_null_points: int
    error_message: Optional[str]
    notes: Optional[str]


def build_query_suite() -> List[QueryCase]:
    """Create 100 complex framework stress queries."""
    raw: List[Dict[str, Any]] = [
        # Macro growth / labor / prices / fiscal (30)
        {"category": "macro_growth", "query": "Real GDP growth for China, India, and Indonesia from 2015 to 2024", "expected_provider": "WORLDBANK"},
        {"category": "macro_growth", "query": "Quarterly real GDP for the United States since 2018", "expected_provider": "FRED"},
        {"category": "macro_growth", "query": "GDP per capita trend in Brazil and Mexico over the past decade", "expected_provider": "WORLDBANK"},
        {"category": "macro_growth", "query": "Nominal GDP in Japan and South Korea from 2010 to 2023", "expected_provider": "WORLDBANK"},
        {"category": "macro_growth", "query": "GDP deflator inflation in Germany between 2012 and 2024", "expected_provider": "WORLDBANK"},
        {"category": "macro_growth", "query": "Potential output gap proxy for the US using real GDP growth", "expected_provider": "FRED"},
        {"category": "labor", "query": "Unemployment rate in Spain, Italy, and Greece from 2010 to 2024", "expected_provider": "EUROSTAT"},
        {"category": "labor", "query": "US unemployment rate monthly during 2020 to 2024", "expected_provider": "FRED"},
        {"category": "labor", "query": "Youth unemployment in France and Portugal since 2015", "expected_provider": "EUROSTAT"},
        {"category": "labor", "query": "Labor force participation in Canada from 2014 to 2024", "expected_provider": "STATSCAN"},
        {"category": "labor", "query": "Employment to population ratio in South Africa and Nigeria", "expected_provider": "WORLDBANK"},
        {"category": "labor", "query": "Average wages and earnings trend in the US since 2016", "expected_provider": "FRED"},
        {"category": "inflation", "query": "CPI inflation in Turkey, Argentina, and Egypt from 2018 to 2024", "expected_provider": "WORLDBANK"},
        {"category": "inflation", "query": "HICP inflation in euro area countries 2019 to 2024", "expected_provider": "EUROSTAT"},
        {"category": "inflation", "query": "US core CPI and headline CPI since 2015", "expected_provider": "FRED"},
        {"category": "inflation", "query": "Producer price inflation trend in the US and Germany", "expected_provider": "FRED"},
        {"category": "inflation", "query": "Inflation forecast style series for advanced economies", "expected_provider": "IMF"},
        {"category": "fiscal", "query": "Government debt to GDP ratio for G7 countries from 2005 to 2023", "expected_provider": "IMF"},
        {"category": "fiscal", "query": "General government fiscal balance for euro area members since 2010", "expected_provider": "IMF"},
        {"category": "fiscal", "query": "Public debt trend in Japan, Italy, and the US from 2000 to 2023", "expected_provider": "IMF"},
        {"category": "fiscal", "query": "Government deficit as share of GDP in India and Brazil", "expected_provider": "IMF"},
        {"category": "external_sector", "query": "Current account balance to GDP in Korea and Thailand since 2010", "expected_provider": "IMF"},
        {"category": "external_sector", "query": "Foreign exchange reserves for China, India, and Saudi Arabia", "expected_provider": "IMF"},
        {"category": "external_sector", "query": "FDI net inflows for Vietnam and Malaysia from 2012 to 2023", "expected_provider": "WORLDBANK"},
        {"category": "external_sector", "query": "Gross national savings share of GDP in ASEAN countries", "expected_provider": "WORLDBANK"},
        {"category": "credit", "query": "Private sector credit to GDP for Chile, Colombia, and Peru", "expected_provider": "WORLDBANK"},
        {"category": "credit", "query": "Bank lending growth in the United States since 2019", "expected_provider": "FRED"},
        {"category": "housing", "query": "Residential property prices in Canada, Australia, and Sweden since 2015", "expected_provider": "BIS"},
        {"category": "housing", "query": "House price index in Germany and Netherlands from 2012 to 2024", "expected_provider": "BIS"},
        {"category": "housing", "query": "US housing starts and building permits trend since 2016", "expected_provider": "FRED"},

        # Trade flows and trade ratios (40)
        {"category": "trade_ratio", "query": "Exports to GDP ratio in China and the UK since 2000", "expected_provider": "WORLDBANK"},
        {"category": "trade_ratio", "query": "Import share of GDP in China and the US since 2000", "expected_provider": "WORLDBANK"},
        {"category": "trade_ratio", "query": "Imports of goods and services as percent of GDP for India and Indonesia", "expected_provider": "WORLDBANK"},
        {"category": "trade_ratio", "query": "Exports as percentage of GDP in Germany, France, and Italy", "expected_provider": "WORLDBANK"},
        {"category": "trade_ratio", "query": "Net trade balance as share of GDP in Japan and Korea", "expected_provider": "WORLDBANK"},
        {"category": "trade_ratio", "query": "Merchandise exports as share of GDP in Vietnam and Bangladesh", "expected_provider": "WORLDBANK"},
        {"category": "trade_ratio", "query": "Merchandise imports as share of GDP in Mexico and Brazil", "expected_provider": "WORLDBANK"},
        {"category": "trade_ratio", "query": "Service exports share of GDP in Singapore and Ireland", "expected_provider": "WORLDBANK"},
        {"category": "trade_ratio", "query": "Service imports share of GDP in UAE and Qatar", "expected_provider": "WORLDBANK"},
        {"category": "trade_ratio", "query": "Trade openness ratio (exports plus imports to GDP) in small open economies", "expected_provider": "WORLDBANK"},
        {"category": "bilateral_trade", "query": "US exports to China from 2018 to 2024", "expected_provider": "COMTRADE"},
        {"category": "bilateral_trade", "query": "US imports from China from 2018 to 2024", "expected_provider": "COMTRADE"},
        {"category": "bilateral_trade", "query": "Germany exports to France and Italy in 2023", "expected_provider": "COMTRADE"},
        {"category": "bilateral_trade", "query": "Japan imports from South Korea and Taiwan since 2019", "expected_provider": "COMTRADE"},
        {"category": "bilateral_trade", "query": "India exports to UAE and Saudi Arabia from 2016 to 2024", "expected_provider": "COMTRADE"},
        {"category": "bilateral_trade", "query": "Brazil imports from Argentina and Chile in the last 5 years", "expected_provider": "COMTRADE"},
        {"category": "bilateral_trade", "query": "Canada exports to the United States from 2010 to 2024", "expected_provider": "COMTRADE"},
        {"category": "bilateral_trade", "query": "Mexico imports from the US by year since 2012", "expected_provider": "COMTRADE"},
        {"category": "bilateral_trade", "query": "UK exports to Germany and Netherlands after 2019", "expected_provider": "COMTRADE"},
        {"category": "bilateral_trade", "query": "China exports to ASEAN members from 2015 onward", "expected_provider": "COMTRADE"},
        {"category": "commodity_trade", "query": "Global crude oil exports by Saudi Arabia and Russia since 2018", "expected_provider": "COMTRADE"},
        {"category": "commodity_trade", "query": "US soybean exports by year from 2016 to 2024", "expected_provider": "COMTRADE"},
        {"category": "commodity_trade", "query": "Australia iron ore exports to China over the past decade", "expected_provider": "COMTRADE"},
        {"category": "commodity_trade", "query": "Japan semiconductor equipment exports in 2020 to 2024", "expected_provider": "COMTRADE"},
        {"category": "commodity_trade", "query": "India pharmaceutical exports trend since 2015", "expected_provider": "COMTRADE"},
        {"category": "commodity_trade", "query": "Germany automobile exports trend since 2014", "expected_provider": "COMTRADE"},
        {"category": "commodity_trade", "query": "France aircraft exports to the world from 2012 to 2024", "expected_provider": "COMTRADE"},
        {"category": "commodity_trade", "query": "Indonesia coal exports yearly from 2010 to 2024", "expected_provider": "COMTRADE"},
        {"category": "commodity_trade", "query": "Chile copper exports to China from 2013 to 2024", "expected_provider": "COMTRADE"},
        {"category": "commodity_trade", "query": "Qatar LNG related exports trend over the last decade", "expected_provider": "COMTRADE"},
        {"category": "hs_trade", "query": "US exports of HS 1001 wheat from 2018 to 2024", "expected_provider": "COMTRADE"},
        {"category": "hs_trade", "query": "China imports of HS 8542 integrated circuits since 2017", "expected_provider": "COMTRADE"},
        {"category": "hs_trade", "query": "Germany exports of HS 8703 motor cars from 2015 to 2024", "expected_provider": "COMTRADE"},
        {"category": "hs_trade", "query": "India imports of HS 2710 petroleum oils by year", "expected_provider": "COMTRADE"},
        {"category": "hs_trade", "query": "Japan exports of HS 8471 computers since 2010", "expected_provider": "COMTRADE"},
        {"category": "hs_trade", "query": "Brazil exports of HS 1201 soybeans from 2012 to 2024", "expected_provider": "COMTRADE"},
        {"category": "hs_trade", "query": "Mexico exports of HS 8708 auto parts since 2014", "expected_provider": "COMTRADE"},
        {"category": "hs_trade", "query": "France exports of HS 2204 wine over the last decade", "expected_provider": "COMTRADE"},
        {"category": "hs_trade", "query": "Korea exports of HS 2711 petroleum gases from 2016 onward", "expected_provider": "COMTRADE"},
        {"category": "hs_trade", "query": "Vietnam exports of HS 6404 footwear from 2015 to 2024", "expected_provider": "COMTRADE"},

        # Monetary / rates / FX / crypto (20)
        {"category": "policy_rate", "query": "Federal funds target rate history since 2005", "expected_provider": "FRED"},
        {"category": "policy_rate", "query": "ECB policy rate trend from 2010 to 2024", "expected_provider": "BIS"},
        {"category": "policy_rate", "query": "Bank of England base rate over the last 20 years", "expected_provider": "BIS"},
        {"category": "policy_rate", "query": "Bank of Japan policy rate since 1999", "expected_provider": "BIS"},
        {"category": "bond_yield", "query": "US 10-year government bond yield from 2000 to 2024", "expected_provider": "FRED"},
        {"category": "bond_yield", "query": "US 2-year and 10-year yield spread since 2010", "expected_provider": "FRED"},
        {"category": "bond_yield", "query": "Long-term interest rate comparison for OECD economies", "expected_provider": "OECD"},
        {"category": "money_supply", "query": "US M2 money supply growth from 2005 to 2024", "expected_provider": "FRED"},
        {"category": "money_supply", "query": "US M1 money stock trend since 2010", "expected_provider": "FRED"},
        {"category": "money_supply", "query": "Broad money growth in China and India over the last decade", "expected_provider": "WORLDBANK"},
        {"category": "fx", "query": "USD to EUR exchange rate over the last 12 months", "expected_provider": "EXCHANGERATE"},
        {"category": "fx", "query": "USD to JPY exchange rate from 2020 to 2024", "expected_provider": "EXCHANGERATE"},
        {"category": "fx", "query": "GBP to USD historical exchange rate for the last 5 years", "expected_provider": "EXCHANGERATE"},
        {"category": "fx", "query": "Real effective exchange rate for Japan and Korea since 2010", "expected_provider": "IMF"},
        {"category": "fx", "query": "REER trend for China and India from 2012 to 2024", "expected_provider": "IMF"},
        {"category": "crypto", "query": "Bitcoin price in USD for the last 365 days", "expected_provider": "COINGECKO"},
        {"category": "crypto", "query": "Ethereum market capitalization trend over the last year", "expected_provider": "COINGECKO"},
        {"category": "crypto", "query": "Top 10 cryptocurrencies by market cap right now", "expected_provider": "COINGECKO"},
        {"category": "crypto", "query": "Solana trading volume over the last 90 days", "expected_provider": "COINGECKO"},
        {"category": "crypto", "query": "XRP price performance over the last 6 months", "expected_provider": "COINGECKO"},

        # Multi-country and complex comparisons (10)
        {"category": "complex_comparison", "query": "Compare unemployment and inflation for G7 countries from 2010 to 2024", "expected_provider": None},
        {"category": "complex_comparison", "query": "Compare export to GDP ratios across BRICS in the last decade", "expected_provider": None},
        {"category": "complex_comparison", "query": "Which ASEAN country has the highest import share of GDP since 2015", "expected_provider": None},
        {"category": "complex_comparison", "query": "Show debt to GDP ranking for euro area countries in the latest year", "expected_provider": None},
        {"category": "complex_comparison", "query": "Compare current account balances for energy importers versus exporters", "expected_provider": None},
        {"category": "complex_comparison", "query": "Contrast US and China trade balances before and after 2018", "expected_provider": None},
        {"category": "complex_comparison", "query": "Compare house price growth across Canada, Australia, and the UK since 2015", "expected_provider": None},
        {"category": "complex_comparison", "query": "Compare policy rates and inflation for US, UK, and euro area since 2010", "expected_provider": None},
        {"category": "complex_comparison", "query": "How did import share of GDP evolve in India versus Vietnam after 2010", "expected_provider": None},
        {"category": "complex_comparison", "query": "Rank top 10 economies by GDP growth in 2023", "expected_provider": None},
    ]

    if len(raw) != 100:
        raise ValueError(f"Expected 100 queries, found {len(raw)}")
    return [
        QueryCase(
            id=idx,
            category=item["category"],
            query=item["query"],
            expected_provider=item.get("expected_provider"),
        )
        for idx, item in enumerate(raw, start=1)
    ]


async def run_single(
    client: httpx.AsyncClient,
    case: QueryCase,
    base_url: str,
    timeout_seconds: int,
) -> SweepResult:
    start = time.time()
    try:
        resp = await client.post(
            f"{base_url}/api/query",
            json={"query": case.query},
            timeout=timeout_seconds,
        )
        elapsed_ms = (time.time() - start) * 1000
    except httpx.TimeoutException:
        return SweepResult(
            id=case.id,
            category=case.category,
            query=case.query,
            status="timeout",
            failure_type="timeout",
            expected_provider=case.expected_provider,
            provider_returned=None,
            indicator_returned=None,
            country_returned=None,
            response_time_ms=(time.time() - start) * 1000,
            series_count=0,
            non_null_points=0,
            error_message=f"timeout after {timeout_seconds}s",
            notes=None,
        )
    except Exception as exc:
        return SweepResult(
            id=case.id,
            category=case.category,
            query=case.query,
            status="error",
            failure_type="exception",
            expected_provider=case.expected_provider,
            provider_returned=None,
            indicator_returned=None,
            country_returned=None,
            response_time_ms=(time.time() - start) * 1000,
            series_count=0,
            non_null_points=0,
            error_message=str(exc),
            notes=None,
        )

    if resp.status_code != 200:
        return SweepResult(
            id=case.id,
            category=case.category,
            query=case.query,
            status="error",
            failure_type="http_error",
            expected_provider=case.expected_provider,
            provider_returned=None,
            indicator_returned=None,
            country_returned=None,
            response_time_ms=elapsed_ms,
            series_count=0,
            non_null_points=0,
            error_message=f"HTTP {resp.status_code}",
            notes=resp.text[:300],
        )

    payload = resp.json()
    if payload.get("error"):
        return SweepResult(
            id=case.id,
            category=case.category,
            query=case.query,
            status="error",
            failure_type="api_error",
            expected_provider=case.expected_provider,
            provider_returned=None,
            indicator_returned=None,
            country_returned=None,
            response_time_ms=elapsed_ms,
            series_count=0,
            non_null_points=0,
            error_message=str(payload.get("error")),
            notes=str(payload.get("message") or "")[:400],
        )

    if payload.get("clarificationNeeded"):
        return SweepResult(
            id=case.id,
            category=case.category,
            query=case.query,
            status="clarify",
            failure_type=None,
            expected_provider=case.expected_provider,
            provider_returned=_norm_provider((payload.get("intent") or {}).get("apiProvider")),
            indicator_returned=None,
            country_returned=None,
            response_time_ms=elapsed_ms,
            series_count=0,
            non_null_points=0,
            error_message=None,
            notes=str(payload.get("clarificationQuestions") or "")[:400],
        )

    datasets = payload.get("data") or []
    if not isinstance(datasets, list):
        datasets = []

    if not datasets:
        return SweepResult(
            id=case.id,
            category=case.category,
            query=case.query,
            status="fail",
            failure_type="no_data",
            expected_provider=case.expected_provider,
            provider_returned=None,
            indicator_returned=None,
            country_returned=None,
            response_time_ms=elapsed_ms,
            series_count=0,
            non_null_points=0,
            error_message="empty data array",
            notes=str(payload.get("message") or "")[:400],
        )

    first_meta: Dict[str, Any] = {}
    non_null_points = 0
    for ds in datasets:
        if not isinstance(ds, dict):
            continue
        meta = ds.get("metadata") or {}
        if not first_meta and isinstance(meta, dict):
            first_meta = meta
        points = ds.get("data") or []
        if not isinstance(points, list):
            continue
        non_null_points += sum(1 for p in points if isinstance(p, dict) and p.get("value") is not None)

    provider_returned = _norm_provider(first_meta.get("source"))
    indicator_returned = str(first_meta.get("indicator") or "")
    country_returned = str(first_meta.get("country") or "")

    if non_null_points == 0:
        return SweepResult(
            id=case.id,
            category=case.category,
            query=case.query,
            status="fail",
            failure_type="null_data",
            expected_provider=case.expected_provider,
            provider_returned=provider_returned or None,
            indicator_returned=indicator_returned or None,
            country_returned=country_returned or None,
            response_time_ms=elapsed_ms,
            series_count=len(datasets),
            non_null_points=0,
            error_message="all returned values are null",
            notes=None,
        )

    mismatch = semantic_mismatch(case.query, first_meta)
    if mismatch:
        return SweepResult(
            id=case.id,
            category=case.category,
            query=case.query,
            status="fail",
            failure_type="semantic_mismatch",
            expected_provider=case.expected_provider,
            provider_returned=provider_returned or None,
            indicator_returned=indicator_returned or None,
            country_returned=country_returned or None,
            response_time_ms=elapsed_ms,
            series_count=len(datasets),
            non_null_points=non_null_points,
            error_message="semantic mismatch",
            notes=mismatch,
        )

    if case.expected_provider and provider_returned and _norm_provider(case.expected_provider) != provider_returned:
        return SweepResult(
            id=case.id,
            category=case.category,
            query=case.query,
            status="warn",
            failure_type="provider_mismatch",
            expected_provider=case.expected_provider,
            provider_returned=provider_returned,
            indicator_returned=indicator_returned or None,
            country_returned=country_returned or None,
            response_time_ms=elapsed_ms,
            series_count=len(datasets),
            non_null_points=non_null_points,
            error_message=None,
            notes=f"expected={_norm_provider(case.expected_provider)} actual={provider_returned}",
        )

    return SweepResult(
        id=case.id,
        category=case.category,
        query=case.query,
        status="pass",
        failure_type=None,
        expected_provider=case.expected_provider,
        provider_returned=provider_returned or None,
        indicator_returned=indicator_returned or None,
        country_returned=country_returned or None,
        response_time_ms=elapsed_ms,
        series_count=len(datasets),
        non_null_points=non_null_points,
        error_message=None,
        notes=None,
    )


async def run_sweep(
    cases: List[QueryCase],
    base_url: str,
    concurrency: int,
    timeout_seconds: int,
) -> List[SweepResult]:
    semaphore = asyncio.Semaphore(concurrency)
    results: List[SweepResult] = []

    async with httpx.AsyncClient() as client:
        async def _bounded(case: QueryCase) -> SweepResult:
            async with semaphore:
                return await run_single(client, case, base_url, timeout_seconds)

        tasks = [_bounded(case) for case in cases]
        total = len(tasks)
        for idx, done in enumerate(asyncio.as_completed(tasks), start=1):
            result = await done
            results.append(result)
            emoji = {"pass": "✅", "warn": "⚠️", "clarify": "🔵", "fail": "❌", "error": "🛑", "timeout": "⏱️"}.get(result.status, "•")
            print(
                f"[{idx:03d}/{total:03d}] {emoji} Q{result.id:03d} {result.category} "
                f"({result.response_time_ms:.0f}ms) {result.query[:88]}"
            )
    return sorted(results, key=lambda r: r.id)


def summarize(results: List[SweepResult]) -> Dict[str, Any]:
    statuses = ["pass", "warn", "clarify", "fail", "error", "timeout"]
    by_status: Dict[str, int] = {k: 0 for k in statuses}
    by_failure: Dict[str, int] = {}
    by_category: Dict[str, Dict[str, int]] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        if r.failure_type:
            by_failure[r.failure_type] = by_failure.get(r.failure_type, 0) + 1
        if r.category not in by_category:
            by_category[r.category] = {k: 0 for k in statuses}
        by_category[r.category][r.status] += 1

    total = len(results)
    effective_pass = by_status["pass"] + by_status["warn"] + by_status["clarify"]
    return {
        "total": total,
        "by_status": by_status,
        "by_failure_type": by_failure,
        "by_category": by_category,
        "effective_pass_rate": (effective_pass / total * 100.0) if total else 0.0,
        "strict_pass_rate": (by_status["pass"] / total * 100.0) if total else 0.0,
        "avg_response_time_ms": (
            sum(r.response_time_ms for r in results) / total if total else 0.0
        ),
    }


def write_todo_md(
    path: Path,
    base_url: str,
    results: List[SweepResult],
    summary: Dict[str, Any],
    run_timestamp: str,
    report_path: Path,
) -> None:
    lines: List[str] = []
    lines.append("# Framework Sweep 100 TODO")
    lines.append("")
    lines.append(f"- Last Run (UTC): `{run_timestamp}`")
    lines.append(f"- Target: `{base_url}`")
    lines.append(f"- Raw Report: `{report_path.relative_to(ROOT)}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total: **{summary['total']}**")
    lines.append(f"- Pass: **{summary['by_status'].get('pass', 0)}**")
    lines.append(f"- Warn: **{summary['by_status'].get('warn', 0)}**")
    lines.append(f"- Clarify: **{summary['by_status'].get('clarify', 0)}** (valid — system asked user for details)")
    lines.append(f"- Fail: **{summary['by_status'].get('fail', 0)}**")
    lines.append(f"- Error: **{summary['by_status'].get('error', 0)}**")
    lines.append(f"- Timeout: **{summary['by_status'].get('timeout', 0)}**")
    lines.append(f"- Strict Pass Rate: **{summary['strict_pass_rate']:.1f}%**")
    lines.append(f"- Effective Pass Rate (pass+warn+clarify): **{summary['effective_pass_rate']:.1f}%**")
    lines.append(f"- Average Response Time: **{summary['avg_response_time_ms']:.0f} ms**")
    lines.append("")

    if summary["by_failure_type"]:
        lines.append("## Framework Issues To Fix")
        lines.append("")
        for issue, count in sorted(summary["by_failure_type"].items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"- [ ] `{issue}` ({count})")
        lines.append("")

    lines.append("## Query Checklist")
    lines.append("")
    for r in results:
        checked = "x" if r.status in {"pass", "warn", "clarify"} else " "
        detail_parts: List[str] = [f"`{r.status.upper()}`", f"`{r.category}`"]
        if r.provider_returned:
            detail_parts.append(f"`{r.provider_returned}`")
        if r.indicator_returned:
            detail_parts.append(f"`{r.indicator_returned[:90]}`")
        if r.failure_type:
            detail_parts.append(f"`{r.failure_type}`")
        if r.error_message:
            detail_parts.append(f"`{r.error_message[:140]}`")

        lines.append(f"- [{checked}] Q{r.id:03d} {r.query}")
        lines.append(f"  - {' | '.join(detail_parts)}")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def save_report(base_url: str, cases: List[QueryCase], results: List[SweepResult], summary: Dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = REPORT_DIR / f"framework_sweep_100_{timestamp}.json"
    payload = {
        "run_timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "target": base_url,
        "query_count": len(cases),
        "summary": summary,
        "queries": [asdict(c) for c in cases],
        "results": [asdict(r) for r in results],
    }
    report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    LATEST_JSON.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return report_path


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run framework-level 100-query sweep")
    parser.add_argument("--local", action="store_true", help="Use localhost target")
    parser.add_argument("--base-url", type=str, default=None, help="Override base URL (without /api/query)")
    parser.add_argument("--concurrency", type=int, default=3, help="Concurrent query calls")
    parser.add_argument("--timeout", type=int, default=45, help="Per-query timeout in seconds")
    args = parser.parse_args()

    base_url = args.base_url or (LOCAL_URL.rsplit("/api/query", 1)[0] if args.local else PROD_URL.rsplit("/api/query", 1)[0])
    cases = build_query_suite()

    print(f"Running framework sweep against: {base_url}")
    print(f"Queries: {len(cases)} | Concurrency: {args.concurrency} | Timeout: {args.timeout}s")
    print("-" * 100)

    results = await run_sweep(cases, base_url, max(1, args.concurrency), max(10, args.timeout))
    summary = summarize(results)
    report_path = save_report(base_url, cases, results, summary)
    run_timestamp = datetime.now(timezone.utc).isoformat()
    write_todo_md(TODO_PATH, base_url, results, summary, run_timestamp, report_path)

    print("\nSummary:")
    print(json.dumps(summary, indent=2))
    print(f"\nReport: {report_path}")
    print(f"TODO:   {TODO_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
