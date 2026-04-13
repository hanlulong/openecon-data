"""Query text parsing helpers extracted from QueryService.

This module contains pure functions for cleaning and transforming
query text before it is passed to routing or indicator resolution.
"""

from __future__ import annotations

import re
from typing import List

from .relevance_scorer import extract_indicator_cues


def infer_multi_concept_indicators_from_query(query: str) -> List[str]:
    """Infer explicit indicator list for comparison queries spanning concept families.

    Pure function equivalent of ``QueryService._infer_multi_concept_indicators_from_query``.

    Args:
        query: The raw user query.

    Returns:
        Ordered, deduplicated list of inferred indicator descriptions.
    """
    query_lower = str(query or "").lower()
    cues = extract_indicator_cues(query_lower)
    inferred: List[str] = []

    if "employment_population" in cues:
        inferred.append("employment to population ratio")
    elif "employment_rate" in cues:
        inferred.append("employment rate")
    elif "unemployment" in cues:
        inferred.append("unemployment rate")

    if "gdp" in cues:
        if "per capita" in query_lower:
            inferred.append("GDP per capita")
        elif "ppp" in query_lower:
            inferred.append("PPP GDP")
        elif "deflator" in query_lower:
            inferred.append("GDP deflator")
        elif "constant price" in query_lower or "constant prices" in query_lower:
            inferred.append("constant prices GDP")
        elif "real" in query_lower:
            inferred.append("real GDP")
        elif "nominal" in query_lower or "current us$" in query_lower:
            inferred.append("nominal GDP")
        elif "growth" in query_lower:
            inferred.append("GDP growth rate")
        else:
            inferred.append("GDP")

    if "producer_price" in cues:
        inferred.append("producer price inflation")
    elif "inflation" in cues:
        inferred.append("HICP inflation" if "hicp" in query_lower else "inflation rate")

    if "debt_service" in cues:
        inferred.append("debt service ratio")
    elif "debt_gdp_ratio" in cues or "public_debt" in cues:
        inferred.append("government debt (% of GDP)")
    elif "credit" in cues:
        inferred.append("private sector credit to GDP")

    if "policy_rate" in cues:
        inferred.append("policy rate")
    elif "bond_yield" in cues:
        inferred.append("long-term interest rate")

    if "money_supply" in cues:
        inferred.append("money supply")

    if "reserves" in cues:
        inferred.append("foreign exchange reserves")
    elif "current_account" in cues:
        inferred.append("current account balance (% of GDP)")
    elif "real_effective_exchange_rate" in cues:
        inferred.append("real effective exchange rate")
    elif "exchange_rate" in cues:
        inferred.append("real effective exchange rate" if "reer" in query_lower else "exchange rate")

    if "trade_balance" in cues:
        inferred.append("trade balance (% of GDP)" if "gdp" in query_lower else "trade balance")
    elif "import" in cues:
        inferred.append("imports as % of GDP" if "gdp" in query_lower else "imports")
    elif "export" in cues:
        inferred.append("exports as % of GDP" if "gdp" in query_lower else "exports")

    # Preserve order and uniqueness.
    return list(dict.fromkeys([item for item in inferred if item]))


def extract_indicator_text_from_refined_query(refined_query: str) -> str:
    """Strip scope suffixes (e.g. 'across X', 'for Y') from a clarification-refined query.

    Args:
        refined_query: The query text, potentially containing trailing scope info.

    Returns:
        Cleaned indicator text with scope suffixes removed.
    """
    text = str(refined_query or "").strip()
    if not text:
        return ""

    cleaned = re.sub(
        r"\s+(?:across|for)\s+.+$",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    return cleaned or text
