"""Shared default time-range helpers for provider routing/fetching."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict

logger = logging.getLogger(__name__)


def apply_default_time_range(provider: str, routing: Dict[str, Any]) -> Dict[str, Any]:
    """
    Apply smart default time ranges when user does not provide explicit dates.

    Rules:
    - Respect explicit user dates.
    - Comtrade uses last 10 years for absent or explicitly framework-default dates.
    - CoinGecko defaults to last 30 days.
    - ExchangeRate keeps no date default (current rate behavior).
    """
    provider_upper = (provider or "").upper()
    today = datetime.now()

    start_date = routing.get("startDate")
    end_date = routing.get("endDate")

    if provider_upper == "COMTRADE":
        should_apply_default = (
            (not start_date and not end_date)
            or routing.get("__time_scope_authority") == "default"
        )

        if should_apply_default:
            start_year_int = today.year - 10
            end_year_int = today.year
            routing["startDate"] = f"{start_year_int}-01-01"
            routing["endDate"] = f"{end_year_int}-12-31"
            routing["start_year"] = start_year_int
            routing["end_year"] = end_year_int
            logger.info(
                "Applied default 10-year range for Comtrade: %s to %s",
                start_year_int,
                end_year_int,
            )
            return routing

        return routing

    if start_date or end_date:
        return routing

    if provider_upper == "COINGECKO":
        start = today - timedelta(days=30)
        routing["startDate"] = start.strftime("%Y-%m-%d")
        routing["endDate"] = today.strftime("%Y-%m-%d")
        routing["__default_time_range_applied"] = "coingecko_30d"
        logger.info(
            "Applied default 30-day range for CoinGecko: %s to %s",
            routing["startDate"],
            routing["endDate"],
        )

    # ExchangeRate intentionally has no default date range.
    return routing


# ---------------------------------------------------------------------------
# Explicit time-scope detection (shared by data_fetcher and providers)
# ---------------------------------------------------------------------------

_TIME_SCOPE_SINGLE_YEAR_RE = re.compile(
    r"\b(?:since|between|from|through|until|before|after|during|in|for)\s+"
    r"(?:19\d{2}|20\d{2})\b",
    flags=re.IGNORECASE,
)
_TIME_SCOPE_YEAR_RANGE_RE = re.compile(
    r"\b(?:19\d{2}|20\d{2})\s*(?:-|–|—|to|through|until)\s*"
    r"(?:19\d{2}|20\d{2})\b",
    flags=re.IGNORECASE,
)
_RECENCY_CUE_RE = re.compile(
    r"\b(?:latest|most recent|current|currently|today|yesterday|now)\b",
    flags=re.IGNORECASE,
)
_CURRENT_MEASUREMENT_CUE_RE = re.compile(
    r"\bcurrent\s+(?:"
    r"prices?|"
    r"us\$|u\.s\.\$|u\.s\. dollars?|us dollars?|dollars?|"
    r"lcu|local currency|national currency|"
    r"account|activity|assets?|liabilities?|"
    r"expenditure|expenditures|revenues?|costs?"
    r")(?=\W|$)",
    flags=re.IGNORECASE,
)
_DURATION_MEASUREMENT_CUE_RE = re.compile(
    r"\bwithin\s+\d{1,3}\s+quarters?\b|"
    r"\b\d{1,3}[-\s]+month\s+treasury\s+bill\b",
    flags=re.IGNORECASE,
)
# A constant-price BASE YEAR ("in 2015 dollars", "at 2010 prices",
# "chained 2012 dollars") denominates the values — it is NOT a request to
# filter the time window to that year. The d8264dc prompt teaches the LLM this,
# but the text time-scope heuristics still saw the bare year and marked the
# window user-set, suppressing the default-window strip and Eurostat sparse
# retry → false "no data". Strip the phrase before the single-year/range tests.
_CONSTANT_PRICE_BASE_YEAR_RE = re.compile(
    r"\b(?:constant|chained)\s+(?:19\d{2}|20\d{2})\b"
    r"|\b(?:19\d{2}|20\d{2})\s+(?:u\.?s\.?\s+)?(?:constant\s+|chained\s+)?(?:dollars?|prices?)\b",
    flags=re.IGNORECASE,
)


def _user_set_time_scope(params: dict, original_query: str) -> bool:
    """Whether the active time window was set by the USER (vs framework default).

    Prefers the recorded provenance stamp (__time_scope_authority, written by
    apply_default_time_periods and carried through conversation state) over
    re-inferring from the current turn's text — text inference breaks on
    follow-ups whose raw text ("compare with France") carries no time words
    even though the dates in params were user-set turns ago. Falls back to
    the text heuristic when no stamp is present (legacy intents, caches).
    """
    authority = str((params or {}).get("__time_scope_authority") or "")
    if authority == "user":
        return True
    if authority == "default":
        return False
    return _query_has_explicit_time_scope(original_query or "")


def _query_has_explicit_time_scope(query: str) -> bool:
    """Detect whether the user explicitly constrained the requested time window."""
    query_text = str(query or "").strip()
    if not query_text:
        return False
    recency_query_text = _CURRENT_MEASUREMENT_CUE_RE.sub(" ", query_text)
    if _RECENCY_CUE_RE.search(recency_query_text):
        return True
    # Neutralize constant-price base years ("in 2015 dollars") before the year
    # tests so they don't count as an explicit time filter.
    query_text = _CONSTANT_PRICE_BASE_YEAR_RE.sub(" ", query_text)
    if _TIME_SCOPE_SINGLE_YEAR_RE.search(query_text):
        return True
    for match in _TIME_SCOPE_YEAR_RANGE_RE.finditer(query_text):
        # Exact catalog titles sometimes contain coverage years, e.g.
        # "(1998-2001) from Eurostat"; those title years should not make a
        # framework-applied recent default window strict.  Treat non-title
        # ranges such as "from OECD 2021-2024" or "2015 to 2019" as explicit.
        parenthesized_title_range = (
            match.start() > 0
            and query_text[match.start() - 1] == "("
            and match.end() < len(query_text)
            and query_text[match.end()] == ")"
        )
        if not parenthesized_title_range:
            return True
    duration_query_text = _DURATION_MEASUREMENT_CUE_RE.sub(" ", recency_query_text)
    if re.search(
        r"\b\d+\s+(?:day|days|week|weeks|month|months|year|years|quarter|quarters)\b",
        duration_query_text,
        flags=re.IGNORECASE,
    ):
        return True
    return False
