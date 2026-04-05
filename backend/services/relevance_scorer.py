"""Relevance scoring for series/indicator results against user queries.

Extracted from query.py to reduce file size and isolate the scoring logic
into a testable, reusable module.

This module provides:
- Semantic cue extraction (import/export, debt, inflation, etc.)
- Directional conflict detection (import vs export queries)
- Specialization mismatch penalties (generic vs subgroup queries)
- Series relevance scoring against query text
- Re-ranking and ranking projection utilities
"""

from __future__ import annotations

import re
from typing import Any, List, Optional

from ..routing.country_resolver import CountryResolver


# ---------------------------------------------------------------------------
# Semantic cue map — high-signal tokens/phrases for intent consistency checks
# ---------------------------------------------------------------------------

_CUE_MAP = {
    "import": {"import", "imports"},
    "export": {"export", "exports"},
    "trade_balance": {
        "trade balance",
        "trade surplus",
        "trade deficit",
        "net trade balance",
        "external balance on goods and services",
        "net exports",
    },
    "current_account": {
        "current account",
        "current account balance",
        "balance of payments current account",
        "bca_ngdpd",
        "bn.cab",
    },
    "trade_openness": {
        "trade openness",
        "trade (% of gdp)",
        "exports plus imports",
        "imports plus exports",
        "exports and imports as % of gdp",
    },
    "debt": {"debt", "liability", "liabilities"},
    "debt_service": {"debt service", "debt service ratio", "dsr"},
    "debt_gdp_ratio": {
        "debt to gdp",
        "debt-to-gdp",
        "debt as % of gdp",
        "debt as percentage of gdp",
        "debt (% of gdp)",
        "debt, total (% of gdp)",
        "% of gdp debt",
        "gdp to debt ratio",
        "gdp/debt ratio",
    },
    "gdp_deflator": {
        "gdp deflator",
        "gross domestic product deflator",
    },
    "public_debt": {
        "government debt",
        "public debt",
        "sovereign debt",
        "national debt",
        "central government debt",
        "general government debt",
    },
    "household_debt": {"household debt"},
    "unemployment": {"unemployment", "jobless"},
    "inflation": {"inflation", "consumer price", "cpi"},
    "hicp": {"hicp", "harmonized index", "harmonised index", "harmonized consumer price"},
    "producer_price": {"producer price", "ppi", "wholesale price"},
    "policy_rate": {"policy rate", "repo rate", "fed funds", "federal funds", "benchmark rate", "cash rate"},
    "bond_yield": {
        "bond yield",
        "treasury yield",
        "government bond",
        "sovereign yield",
        "10-year yield",
        "10 year yield",
        "2-year yield",
        "2 year yield",
        "long-term interest rate",
        "long term interest rate",
    },
    "tenor_2y": {"2-year", "2 year", "2yr", "2 yr"},
    "tenor_10y": {"10-year", "10 year", "10yr", "10 yr", "over 10 years", "maturity over 10 years"},
    "tenor_30y": {"30-year", "30 year", "30yr", "30 yr"},
    "money_supply": {
        "money supply",
        "money stock",
        "monetary aggregate",
        "broad money",
        "narrow money",
        "m1",
        "m2",
        "m3",
    },
    "reserves": {"foreign exchange reserves", "fx reserves", "reserve assets", "international reserves", "reserves"},
    "house_prices": {"house price", "house prices", "housing prices", "property prices", "residential property"},
    "employment_rate": {"employment rate"},
    "employment_population": {"employment to population", "employment-population", "employment population ratio"},
    "discontinued": {"discontinued", "deprecated", "legacy"},
    "savings": {"saving", "savings"},
    "credit": {"credit", "lending", "loan"},
    "exchange_rate": {"exchange rate", "forex", "fx", "currency pair", "us dollar exchange"},
    "real_effective_exchange_rate": {
        "reer",
        "real effective exchange rate",
        "real effective fx",
        "trade weighted real exchange rate",
        "ereer",
    },
    "gdp": {"gdp", "gross domestic product"},
}

_ENERGY_GROUP_PATTERNS = (
    "energy importers",
    "energy importing countries",
    "net energy importers",
    "oil importers",
    "energy exporters",
    "energy exporting countries",
    "net energy exporters",
    "oil exporters",
)

# Pre-built set of geographic tokens for stop-word filtering in tokenization.
_GEO_TOKENS: set[str] | None = None


def _get_geo_tokens() -> set[str]:
    """Lazily build the set of geographic tokens from CountryResolver aliases."""
    global _GEO_TOKENS
    if _GEO_TOKENS is None:
        tokens: set[str] = set()
        for alias in CountryResolver.COUNTRY_ALIASES.keys():
            for token in re.findall(r"[a-z0-9]+", str(alias or "").lower()):
                if len(token) >= 2:
                    tokens.add(token)
        _GEO_TOKENS = tokens
    return _GEO_TOKENS


# ---------------------------------------------------------------------------
# Stop words for indicator tokenization
# ---------------------------------------------------------------------------

_INDICATOR_STOP_WORDS = {
    "the", "a", "an", "of", "for", "in", "to", "and", "or",
    "show", "get", "find", "data", "series", "indicator",
    "country", "countries", "from", "with", "by", "on", "at",
    "current", "constant", "annual", "monthly", "quarterly",
    "percent", "percentage", "ratio", "share", "rate", "index",
    "gdp", "value", "values",
}


# ---------------------------------------------------------------------------
# Public functions — usable without a class instance
# ---------------------------------------------------------------------------


def tokenize_indicator_terms(text: str) -> set[str]:
    """Tokenize indicator text into comparable semantic terms."""
    if not text:
        return set()

    geo_terms = _get_geo_tokens()

    raw_terms = set(re.findall(r"[a-z0-9]+", text.lower().replace("_", " ")))
    terms: set[str] = set()
    for term in raw_terms:
        if len(term) <= 2 or term in _INDICATOR_STOP_WORDS or term in geo_terms:
            continue
        terms.add(term)
        if term.endswith("ies") and len(term) > 4:
            terms.add(term[:-3] + "y")
        elif term.endswith("s") and len(term) > 3:
            terms.add(term[:-1])
    return terms


def extract_indicator_cues(text: str) -> set[str]:
    """Extract high-signal semantic cues for intent/indicator consistency checks."""
    if not text:
        return set()

    text_lower = str(text).lower()
    normalized_text = re.sub(r"[_/]+", " ", text_lower).replace("-", " ")
    search_text = f"{text_lower} {normalized_text}"

    cues: set[str] = set()
    for cue, phrases in _CUE_MAP.items():
        for phrase in phrases:
            # Use word-boundary matching for ALL phrases to avoid
            # "employment rate" matching inside "unemployment rate".
            # Allow trailing 's' for plurals (e.g., "policy rates" matches "policy rate").
            if re.search(rf"(?<![a-z]){re.escape(phrase)}s?(?![a-z])", search_text):
                cues.add(cue)
                break

    # "Energy importers/exporters" is a country-group qualifier, not a
    # directional trade-flow request. Keep current-account semantics primary.
    if any(pattern in search_text for pattern in _ENERGY_GROUP_PATTERNS):
        cues.add("energy_group")
        if "current_account" in cues:
            cues.discard("import")
            cues.discard("export")
            cues.discard("trade_balance")

    return cues


def single_directional_cue(cues: set[str]) -> str:
    """Return the single directional cue requested by a query, if any.

    Empty string means no strict single-direction intent (for example, trade openness).
    """
    has_import = "import" in cues
    has_export = "export" in cues
    if has_import and not has_export:
        return "import"
    if has_export and not has_import:
        return "export"
    return ""


def has_directional_conflict(
    query_cues: set[str],
    candidate_cues: set[str],
) -> bool:
    """Detect import/export direction conflicts between query and candidate."""
    query_direction = single_directional_cue(query_cues)
    if not query_direction:
        return False
    opposite = "export" if query_direction == "import" else "import"
    return opposite in candidate_cues and query_direction not in candidate_cues


def specific_cues_compatible(
    query_cues: set[str],
    candidate_cues: set[str],
) -> bool:
    """Determine whether two cue sets are semantically compatible.

    Exact cue overlap is preferred, but closely related cue families are accepted
    to avoid discarding valid matches due wording differences.
    """
    if not query_cues:
        return True
    if has_directional_conflict(query_cues, candidate_cues):
        return False
    query_direction = single_directional_cue(query_cues)
    if query_direction and "trade_balance" in candidate_cues and query_direction not in candidate_cues:
        return False
    if query_cues & candidate_cues:
        return True

    compatible_families = [
        {"debt_gdp_ratio", "public_debt"},
        {"trade_openness", "import", "export", "trade_balance"},
        {"gdp_deflator", "inflation", "producer_price"},
        {"bond_yield", "policy_rate", "tenor_2y", "tenor_10y", "tenor_30y"},
        {"exchange_rate", "real_effective_exchange_rate", "reserves"},
        {"current_account", "trade_balance"},
    ]
    for family in compatible_families:
        if (query_cues & family) and (candidate_cues & family):
            return True

    return False


def series_text_for_relevance(series: Any) -> str:
    """Build a comparable text blob from a series metadata payload."""
    metadata = None
    if series is not None and hasattr(series, "metadata"):
        metadata = getattr(series, "metadata", None)
    elif isinstance(series, dict):
        metadata = series.get("metadata")

    if metadata is None:
        return ""

    if hasattr(metadata, "model_dump"):
        meta_dict = metadata.model_dump()
    elif isinstance(metadata, dict):
        meta_dict = metadata
    else:
        meta_dict = {}

    return " ".join(
        str(meta_dict.get(key) or "")
        for key in ("indicator", "seriesId", "description", "source", "country", "unit")
    ).strip()


def labor_rate_specificity_penalty(query_text: str, candidate_text: str) -> float:
    """Penalize near-miss labour-rate candidates for generic rate queries."""
    query_lower = str(query_text or "").lower()
    candidate_lower = str(candidate_text or "").lower().replace("_", " ")
    if not query_lower or not candidate_lower:
        return 0.0

    requested_metric = ""
    if re.search(r"\bemployment\s+rate\b", query_lower):
        requested_metric = "employment"
    elif re.search(r"\bunemployment\s+rate\b", query_lower):
        requested_metric = "unemployment"
    elif re.search(r"\b(?:labou?r\s+force\s+)?participation\s+rate\b", query_lower):
        requested_metric = "participation"

    if not requested_metric:
        return 0.0

    def has_metric_rate(metric: str) -> bool:
        metric_pattern = {
            "employment": r"\bemployment(?:\s+\w+){0,2}\s+rates?\b",
            "unemployment": r"\bunemployment(?:\s+\w+){0,2}\s+rates?\b",
            "participation": r"\b(?:labou?r\s+force\s+)?participation(?:\s+\w+){0,2}\s+rates?\b",
        }[metric]
        if re.search(metric_pattern, candidate_lower):
            return True
        # Recognize percentage-of-labor-force indicators as equivalent to "rate"
        # e.g., "unemployment, total (% of total labor force)" IS the unemployment rate
        if metric in candidate_lower and re.search(r"\(%\s+of\s+(?:total\s+)?labo[u]?r\s+force\)", candidate_lower):
            return True
        return False

    penalty = 0.0
    if not has_metric_rate(requested_metric):
        penalty += 2.0

    for sibling_metric in ("employment", "unemployment", "participation"):
        if sibling_metric == requested_metric:
            continue
        if has_metric_rate(sibling_metric):
            penalty += 0.45

    return penalty


def specialization_mismatch_penalty(query_text: str, candidate_text: str) -> float:
    """Penalize subgroup-specific series when the query asked for a generic metric."""
    query_lower = str(query_text or "").lower()
    candidate_lower = str(candidate_text or "").lower()
    if not candidate_lower:
        return 0.0

    penalty = 0.0

    def query_mentions(*terms: str) -> bool:
        return any(term in query_lower for term in terms)

    def candidate_mentions(*terms: str) -> bool:
        return any(term in candidate_lower for term in terms)

    penalty += labor_rate_specificity_penalty(query_lower, candidate_lower)

    if not query_mentions("student", "students", "school months", "summer months") and candidate_mentions(
        "student",
        "students",
        "school months",
        "summer months",
    ):
        penalty += 3.4

    if not query_mentions("education", "educational attainment", "school") and candidate_mentions(
        "educational attainment",
        "education",
    ):
        penalty += 2.4

    if not query_mentions("fiscal") and candidate_mentions("fiscal"):
        penalty += 2.1

    if not query_mentions("insurance", "employment insurance", "beneficiaries", "claimants") and candidate_mentions(
        "employment insurance",
        "insurance program",
        "insurance region",
        "beneficiaries",
        "claimants",
    ):
        penalty += 2.2

    if not query_mentions("indigenous") and candidate_mentions("indigenous"):
        penalty += 2.0

    if not query_mentions("gender", "sex", "women", "woman", "men", "man", "female", "male") and candidate_mentions(
        "gender",
        "sex",
        "women",
        "woman",
        "men",
        "man",
        "female",
        "male",
    ):
        penalty += 1.6

    if not query_mentions("urban", "rural", "metropolitan", "population centre", "health region") and candidate_mentions(
        "urban",
        "rural",
        "metropolitan",
        "population centre",
        "health region",
    ):
        penalty += 1.2

    if not query_mentions("industry", "industries", "sector", "sectors") and candidate_mentions(
        "industry",
        "industries",
        "sector",
        "sectors",
    ):
        penalty += 1.7

    if not query_mentions("firm size", "business size", "company size", "enterprise size") and candidate_mentions(
        "firm size",
        "business size",
        "enterprise size",
    ):
        penalty += 1.7

    if not query_mentions("province", "provinces", "territory", "territories", "regional", "region", "regions") and candidate_mentions(
        "province",
        "provinces",
        "territory",
        "territories",
        "regional",
        "regions",
    ):
        penalty += 1.3

    return penalty


def score_series_relevance(query: str, series: Any) -> float:
    """Score semantic relevance of one returned series to the original query."""
    query_text = str(query or "").lower()
    series_text = series_text_for_relevance(series).lower()
    if not series_text:
        return -1.0

    score = 0.0
    query_cues = extract_indicator_cues(query_text)
    series_cues = extract_indicator_cues(series_text)
    query_direction = single_directional_cue(query_cues)

    if query_cues:
        cue_overlap = query_cues & series_cues
        score += float(len(cue_overlap)) * 2.5
        if not cue_overlap:
            score -= 2.0
    if has_directional_conflict(query_cues, series_cues):
        score -= 3.4

    query_terms = tokenize_indicator_terms(query_text)
    series_terms = tokenize_indicator_terms(series_text)
    if query_terms and series_terms:
        lexical_overlap = len(query_terms & series_terms)
        score += min(2.5, lexical_overlap * 0.35)

    ratio_patterns = [
        "% of gdp",
        "as % of gdp",
        "as percent of gdp",
        "as percentage of gdp",
        "share of gdp",
        "to gdp ratio",
        "ratio to gdp",
        "as share of gdp",
    ]
    has_ratio_query = any(pattern in query_text for pattern in ratio_patterns)
    has_ratio_series = any(pattern in series_text for pattern in ratio_patterns)
    if has_ratio_query:
        if has_ratio_series:
            score += 2.5
        else:
            score -= 1.8

    # Penalize directional mismatches.
    if "import" in query_cues and "import" not in series_cues:
        score -= 2.2
        if "trade_balance" in series_cues:
            score -= 1.0
    if "export" in query_cues and "export" not in series_cues:
        score -= 2.2
        if "trade_balance" in series_cues:
            score -= 1.0
    if "trade_balance" in query_cues and "trade_balance" not in series_cues:
        score -= 2.2
    if "current_account" in query_cues and "current_account" not in series_cues:
        score -= 2.6
    if "trade_openness" in query_cues:
        if "trade_openness" in series_cues:
            score += 2.5
        else:
            score -= 2.6
    if "debt_service" in query_cues and "debt_service" not in series_cues:
        score -= 2.2
    if "debt_gdp_ratio" in query_cues:
        if "debt_gdp_ratio" in series_cues:
            score += 2.5
        else:
            score -= 2.8
        if "debt_service" in series_cues:
            score -= 3.0
    if "gdp_deflator" in query_cues and "gdp_deflator" not in series_cues:
        score -= 2.8
    if "hicp" in query_cues and "hicp" not in series_cues:
        score -= 2.6
    if "public_debt" in query_cues and "public_debt" not in series_cues:
        if ("household_debt" in series_cues) or ("credit" in series_cues) or ("debt_service" in series_cues):
            score -= 2.4
    if "credit" in query_cues and "credit" not in series_cues:
        score -= 1.8
    if "exchange_rate" in query_cues and "exchange_rate" not in series_cues:
        score -= 1.8
    if "real_effective_exchange_rate" in query_cues and "real_effective_exchange_rate" not in series_cues:
        score -= 2.8
    if "money_supply" in query_cues and "money_supply" not in series_cues:
        score -= 2.2
    if "bond_yield" in query_cues and "bond_yield" not in series_cues:
        score -= 2.2
    if "tenor_2y" in query_cues and "tenor_2y" not in series_cues:
        score -= 2.6
    if "tenor_10y" in query_cues and "tenor_10y" not in series_cues:
        score -= 2.6
    if "tenor_30y" in query_cues and "tenor_30y" not in series_cues:
        score -= 2.6
    if "policy_rate" in query_cues and "policy_rate" not in series_cues:
        score -= 2.0
    if "house_prices" in query_cues and "house_prices" not in series_cues:
        score -= 2.2
    if "reserves" in query_cues and "reserves" not in series_cues:
        score -= 2.0
    if "employment_population" in query_cues and "employment_population" not in series_cues:
        score -= 2.2
    if "producer_price" in query_cues and "producer_price" not in series_cues:
        score -= 2.0
    if "discontinued" in series_cues and "discontinued" not in query_cues:
        score -= 3.0

    score -= specialization_mismatch_penalty(query_text, series_text)

    # Generic GDP series should not dominate directional/ratio trade queries.
    if "gdp (current us$)" in series_text and ({"import", "export", "trade_balance"} & query_cues):
        score -= 3.0

    # Trade flow totals are usually not ratio indicators.
    if has_ratio_query and "total trade" in series_text:
        score -= 1.5
    if has_ratio_query and ({"import", "export"} & query_cues):
        ratio_signals = (
            "% of gdp",
            "percent of gdp",
            "percentage of gdp",
            "share of gdp",
            "ne.imp.gnfs.zs",
            "ne.exp.gnfs.zs",
            "_ngdp",
        )
        has_ratio_signal = any(signal in series_text for signal in ratio_signals)
        directional_tokens = ()
        if query_direction == "import":
            directional_tokens = ("import", "imports", ".imp.", "_imp", "imp_")
        elif query_direction == "export":
            directional_tokens = ("export", "exports", ".exp.", "_exp", "exp_")
        has_directional_signal = (
            any(token in series_text for token in directional_tokens)
            if directional_tokens else True
        )
        has_directional_ratio_signal = has_ratio_signal and has_directional_signal
        if has_directional_ratio_signal:
            score += 1.2
        else:
            score -= 2.6
            if has_ratio_signal and directional_tokens:
                score -= 1.4
            if any(token in series_text for token in ("current us$", "constant us$", "million", "billion")):
                score -= 1.2

    return score


def rerank_data_by_query_relevance(
    query: str,
    data: List[Any],
    *,
    is_multi_indicator: bool = False,
) -> List[Any]:
    """Reorder (and lightly filter) returned series by semantic relevance to query.

    This is a framework-level guardrail against agent over-decomposition where
    unrelated series can be returned before the intended concept.

    When *is_multi_indicator* is True (explicit multi-indicator fetch),
    the function reorders but never drops series, since each series
    intentionally represents a different requested concept.
    """
    if not data:
        return data

    scored: List[tuple[float, int, Any]] = []
    for idx, series in enumerate(data):
        scored.append((score_series_relevance(query, series), idx, series))

    scored.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    reranked = [item[2] for item in scored]

    # Multi-indicator queries intentionally fetch different concepts;
    # never filter any series out — only reorder by relevance.
    if is_multi_indicator:
        return reranked

    top_score = scored[0][0] if scored else 0.0
    if top_score < 0.8:
        return reranked

    # Keep all strong matches; discard clearly irrelevant tail when we have good matches.
    filtered = [series for score, _, series in scored if score >= max(0.0, top_score - 3.0)]
    return filtered or reranked


def extract_ranking_value(
    series: Any,
    target_year: Optional[int],
) -> tuple[Optional[float], Any]:
    """Extract comparable ranking value from one series."""
    points = list(series.data or [])
    if not points:
        return None, None

    selected_point = None
    if target_year:
        year_prefix = f"{target_year:04d}"
        year_points = [
            point for point in points
            if str(point.date).startswith(year_prefix) and point.value is not None
        ]
        if year_points:
            selected_point = sorted(year_points, key=lambda point: str(point.date))[-1]

    if selected_point is None:
        valid_points = [point for point in points if point.value is not None]
        if valid_points:
            selected_point = sorted(valid_points, key=lambda point: str(point.date))[-1]

    if selected_point is None:
        return None, None
    return float(selected_point.value), selected_point
