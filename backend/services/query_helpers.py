"""Self-contained helper functions extracted from query.py.

Phases 5-7 decomposition: extracts larger helpers that have minimal
dependencies on QueryService state.  Each method accepts ``svc`` as
first parameter when it needs to delegate back to QueryService.
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional, TYPE_CHECKING

from ..models import ClarificationOption, NormalizedData, ParsedIntent, QueryResponse
from ..routing.country_resolver import CountryResolver
from ..services.query_complexity import QueryComplexityAnalyzer
from ..services.provider_fallback import normalize_country_to_iso2
from ..utils.providers import normalize_provider_name
from ..utils.retry import retry_async, DataNotAvailableError

if TYPE_CHECKING:
    from .query import QueryService

logger = logging.getLogger("openecon")


def extract_countries_from_query(query: str) -> List[str]:
    """Extract all country codes from query in appearance order.

    Returns:
        List of ISO Alpha-2 country codes.
    """
    sanitized_query = query
    try:
        from ..routing.unified_router import detect_explicit_provider_match

        explicit_match = detect_explicit_provider_match(query)
        if explicit_match:
            provider, matched_keyword = explicit_match
            query_lower = query.lower()
            if matched_keyword.endswith("(at start)"):
                provider_lower = provider.lower()
                sanitized_query = re.sub(
                    rf"^\s*{re.escape(provider_lower)}\b",
                    " ",
                    query_lower,
                    flags=re.IGNORECASE,
                )
            else:
                sanitized_query = re.sub(
                    re.escape(matched_keyword),
                    " ",
                    query,
                    flags=re.IGNORECASE,
                )
    except Exception:
        sanitized_query = query

    countries = CountryResolver.detect_all_countries_in_query(sanitized_query)
    if countries:
        logger.info("🌍 Fallback country extraction found countries: %s", countries)
    return countries


def apply_country_overrides(
    svc: "QueryService", intent: ParsedIntent, query: str
) -> None:
    """Apply geography overrides when query text clearly specifies country
    context but LLM output defaults to US/no country.

    Rules:
    - If query names 1 non-US country and intent defaults to US/no country -> set `country`.
    - If query names multiple countries and intent defaults to US/no country -> set `countries`.
    """
    if intent.parameters is None:
        intent.parameters = {}

    extracted_countries = extract_countries_from_query(query)
    expanded_region_countries = CountryResolver.expand_regions_in_query(query)
    explicit_provider_requested = normalize_provider_name(
        svc._detect_explicit_provider(query) or ""
    )
    if not extracted_countries and not expanded_region_countries:
        return

    current_country = str(intent.parameters.get("country", "") or "")
    current_countries_raw = intent.parameters.get("countries")
    current_countries = []
    if isinstance(current_countries_raw, list):
        current_countries = [str(c) for c in current_countries_raw if c is not None]

    def _is_us(value: str) -> bool:
        return value.strip().lower() in {"us", "usa", "united states", "america"}

    defaulted_to_us_or_empty = (
        (not current_country and not current_countries)
        or (_is_us(current_country) and not current_countries)
        or (len(current_countries) == 1 and _is_us(current_countries[0]))
    )

    # Region-based multi-country override: when query mentions a known
    # country group (G7, G20, BRICS, EU, ASEAN, etc.), always expand to
    # the full member list regardless of comparative language.
    if len(expanded_region_countries) > 1:
        # Provider names like "from OECD" or "from Eurostat" can look like
        # region/group cues, but they should not override an explicitly named
        # concrete country in the same query.
        if explicit_provider_requested and extracted_countries:
            logger.info(
                "🌍 Region Override skipped: explicit provider '%s' with concrete country %s",
                explicit_provider_requested,
                extracted_countries,
            )
            expanded_region_countries = []

    if len(expanded_region_countries) > 1:
        current_geo = current_countries[:] if current_countries else (
            [current_country] if current_country else []
        )
        normalized_current = [
            svc._normalize_country_to_iso2(country) or str(country).upper()
            for country in current_geo
            if country
        ]
        normalized_target = [
            svc._normalize_country_to_iso2(country) or str(country).upper()
            for country in expanded_region_countries
        ]
        if normalized_current != normalized_target:
            previous = current_country or (
                ",".join(current_countries) if current_countries else ""
            )
            intent.parameters.pop("country", None)
            intent.parameters["countries"] = expanded_region_countries
            logger.info(
                "🌍 Region Override: '%s' -> %s (query specifies a country group)",
                previous,
                expanded_region_countries,
            )
            return

    # Multi-country override should apply whenever query explicitly names
    # multiple countries, even if parser already selected one non-US country.
    if len(extracted_countries) > 1:
        normalized_current = [
            svc._normalize_country_to_iso2(country) or str(country).upper()
            for country in current_countries
            if country
        ]
        if current_country:
            normalized_current.append(
                svc._normalize_country_to_iso2(current_country) or str(current_country).upper()
            )
        normalized_current = list(dict.fromkeys(normalized_current))

        normalized_extracted = [
            svc._normalize_country_to_iso2(country) or str(country).upper()
            for country in extracted_countries
        ]
        normalized_extracted = list(dict.fromkeys(normalized_extracted))

        if normalized_current != normalized_extracted:
            previous = current_country or (
                ",".join(current_countries) if current_countries else ""
            )
            intent.parameters.pop("country", None)
            intent.parameters["countries"] = extracted_countries
            logger.info(
                "🌍 Country Override (multi): '%s' -> %s (query explicitly names multiple countries)",
                previous,
                extracted_countries,
            )
        return

    if not defaulted_to_us_or_empty:
        return

    # Single-country override
    if not extracted_countries:
        return
    extracted_country = extracted_countries[0]
    if extracted_country.upper() != "US":
        previous = current_country or (current_countries[0] if current_countries else "")
        intent.parameters["country"] = extracted_country
        intent.parameters.pop("countries", None)
        logger.info(
            "🌍 Country Override: '%s' -> '%s' (query explicitly mentions non-US country)",
            previous,
            extracted_country,
        )


def build_alternative_series(intent: ParsedIntent, data: Any) -> Optional[list]:
    """Generate alternative indicator suggestions based on the returned data.

    Shows related indicators the user might also want to explore.
    E.g., after GDP (current US$), suggest GDP growth, GDP per capita, GDP PPP.

    Performance optimization: uses FTS5 full-text search instead of
    LIKE '%...%' scan.
    """
    from .indicator_database import IndicatorDatabase
    from ..models import AlternativeSeries

    try:
        if not data:
            return None

        # Get the indicator code from returned data
        first_data = data[0] if isinstance(data, list) else data
        meta = (
            getattr(first_data, "metadata", None)
            if not isinstance(first_data, dict)
            else first_data.get("metadata")
        )
        if not meta:
            return None
        series_id = str(getattr(meta, "seriesId", "") or "")
        provider = str(getattr(meta, "source", "") or "")
        indicator_name = str(getattr(meta, "indicator", "") or "")

        if not series_id or not provider:
            return None

        # Get the concept family — indicators with similar name prefix
        core = indicator_name.split(",")[0].split("(")[0].strip().lower()
        if len(core) < 2:
            return None

        normalized_provider = normalize_provider_name(provider)

        # Use FTS5 search (indexed, <50ms vs 2-6s for LIKE on 330K rows).
        db = IndicatorDatabase()
        conn = db._get_connection()
        cur = conn.cursor()

        fts_words = [w.strip() for w in core.split() if w.strip() and len(w.strip()) > 2]
        if not fts_words:
            return None

        fts_query = " AND ".join([f'"{w}"' for w in fts_words[:4]])

        try:
            cur.execute(
                """SELECT i.code, i.name FROM indicators_fts f
                JOIN indicators i ON f.rowid = i.id
                WHERE indicators_fts MATCH ? AND i.provider = ? AND i.code != ?
                ORDER BY bm25(indicators_fts) LIMIT 5""",
                (fts_query, normalized_provider.upper(), series_id),
            )
            rows = cur.fetchall()
        except Exception as _fts_exc:
            logger.debug("FTS5 AND query failed, trying OR fallback: %s", _fts_exc)
            simple_fts = " OR ".join([f'"{w}"' for w in fts_words[:3]])
            try:
                cur.execute(
                    """SELECT i.code, i.name FROM indicators_fts f
                    JOIN indicators i ON f.rowid = i.id
                    WHERE indicators_fts MATCH ? AND i.provider = ? AND i.code != ?
                    ORDER BY bm25(indicators_fts) LIMIT 5""",
                    (simple_fts, normalized_provider.upper(), series_id),
                )
                rows = cur.fetchall()
            except Exception as _fts_or_exc:
                logger.debug("FTS5 OR fallback also failed: %s", _fts_or_exc)
                rows = []

        if not rows:
            return None

        alternatives = []
        for code, name in rows:
            alternatives.append(
                AlternativeSeries(
                    code=code,
                    name=name,
                    provider=normalized_provider,
                )
            )

        return alternatives if alternatives else None
    except Exception as _alt_exc:
        logger.debug("Alternative series lookup failed: %s", _alt_exc)
        return None


def should_use_deep_agents(svc: "QueryService", query: str) -> bool:
    """Determine if a query should use Deep Agents for parallel processing.

    Uses QueryComplexityAnalyzer for comprehensive pattern detection.

    Deep Agents are used only for truly complex analytical queries
    (correlation, regression, forecasting, etc.).  Multi-country and
    multi-indicator comparisons are handled by the standard pipeline.
    """
    query_lower = query.lower()

    # Framework guardrail: keep single-metric retrieval queries on the
    # deterministic path.
    ratio_patterns = [
        "% of gdp", "as % of gdp", "as percent of gdp", "as percentage of gdp",
        "share of gdp", "to gdp ratio", "ratio to gdp", "as share of gdp",
    ]
    analysis_keywords = [
        "correlation", "regression", "causal", "simulate", "scenario",
        "what if", "decompose", "optimize", "compute", "calculate", "derive",
    ]
    has_ratio_query = any(pattern in query_lower for pattern in ratio_patterns)
    has_analysis_keyword = any(term in query_lower for term in analysis_keywords)
    query_cues = svc._extract_indicator_cues(query_lower)
    high_signal_query_cues = {
        cue for cue in query_cues
        if cue not in {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
    }
    concept_groups = svc._infer_query_concept_groups(query)

    if has_ratio_query and not has_analysis_keyword:
        logger.info("⏭️ Deep Agents skipped for single-metric ratio retrieval query")
        return False

    # Single-concept retrieval queries (even when ranking/comparison phrasing
    # is present) are better served by deterministic fetching + ranking.
    if (
        (svc._is_ranking_query(query) or svc._is_comparison_query(query))
        and len(concept_groups) <= 1
        and len(high_signal_query_cues) <= 2
        and not has_analysis_keyword
    ):
        logger.info(
            "⏭️ Deep Agents skipped for single-concept retrieval query (concepts=%s, cues=%s)",
            sorted(concept_groups),
            sorted(high_signal_query_cues),
        )
        return False

    if ("trade" in query_lower or "import" in query_lower or "export" in query_lower) and not has_analysis_keyword:
        if not any(term in query_lower for term in ["correlation", "versus and", "decompose", "optimize"]):
            logger.info("⏭️ Deep Agents skipped for direct trade retrieval query")
            return False

    if any(term in query_lower for term in ["rank", "ranking", "top ", "highest", "lowest"]):
        if len(query_cues) <= 2 and not has_analysis_keyword:
            logger.info("⏭️ Deep Agents skipped for single-indicator ranking query")
            return False

    # Use QueryComplexityAnalyzer for comprehensive detection
    complexity = QueryComplexityAnalyzer.detect_complexity(query)

    # Standard multi-country/multi-indicator: handled efficiently by
    # standard pipeline (batch providers, parallel fetches).  Deep Agents
    # would decompose into N*M individual calls — much slower.
    is_multi_country = complexity.get('is_multi_country', False)
    is_multi_indicator = complexity.get('is_multi_indicator', False)
    is_ranking = complexity.get('is_ranking', False)

    deep_analysis_keywords = [
        "correlation", "correlate", "regression", "decompose",
        "optimize", "forecast", "predict", "simulate", "model",
        "causal", "elasticity", "sensitivity",
    ]
    needs_analysis = any(kw in query_lower for kw in deep_analysis_keywords)

    if (is_multi_country or is_multi_indicator or is_ranking) and not needs_analysis:
        logger.info(
            "⏭️ Deep Agents skipped: multi-country/indicator comparison handled "
            "by standard pipeline (multi_country=%s, multi_indicator=%s, ranking=%s)",
            is_multi_country, is_multi_indicator, is_ranking,
        )
        return False

    # Deep Agents only for truly complex analytical queries
    is_complex = False
    trigger_reason = []

    if needs_analysis:
        trigger_reason.append("analysis")
        is_complex = True

    if is_complex:
        logger.info(f"🧠 Deep Agents triggered: {', '.join(trigger_reason)}")

    return is_complex


async def maybe_recover_from_empty_data(
    svc: "QueryService",
    query: str,
    intent: Optional[ParsedIntent],
) -> Optional[List[NormalizedData]]:
    """Attempt semantic/ranking recovery when a primary fetch returns empty data.

    Recovery actions:
    - Distill noisy ranking/comparison phrasing to a stable metric phrase.
    - Expand region/group queries to explicit country lists.
    - Re-route provider for the recovered intent and retry once.
    """
    if not intent:
        return None

    params = dict(intent.parameters or {})
    if params.get("_semantic_recovery_attempted"):
        return None
    if params.get("__exact_provider_code_match"):
        return None

    ranking_or_comparison = svc._is_ranking_query(query) or svc._is_comparison_query(query)
    distilled_indicator = svc._build_distilled_indicator_query(query)
    if not ranking_or_comparison and not distilled_indicator:
        return None

    recovered_intent = intent.model_copy(deep=True)
    recovered_params = dict(recovered_intent.parameters or {})
    recovered_params["_semantic_recovery_attempted"] = True

    if distilled_indicator:
        recovered_intent.indicators = [distilled_indicator]
        recovered_params.pop("indicator", None)
        recovered_params.pop("seriesId", None)
        recovered_params.pop("series_id", None)
        recovered_params.pop("code", None)
        recovered_params["indicator"] = distilled_indicator

    if ranking_or_comparison:
        target_countries = svc._collect_target_countries(recovered_params)
        if len(target_countries) < 2:
            expanded_regions = CountryResolver.expand_regions_in_query(query)
            explicit_countries = svc._extract_countries_from_query(query)
            target_countries = explicit_countries or expanded_regions or target_countries
        # Structural safety net: comparison queries need multiple countries.
        if len(target_countries) < 2:
            target_countries = sorted(CountryResolver.G20_MEMBERS)
        if target_countries:
            recovered_params.pop("country", None)
            recovered_params["countries"] = list(
                dict.fromkeys([str(c) for c in target_countries if c])
            )

    recovered_intent.parameters = recovered_params

    try:
        rerouted_provider = await svc._select_routed_provider(
            recovered_intent, distilled_indicator or query
        )
        recovered_intent.apiProvider = rerouted_provider
    except Exception as exc:
        logger.warning(
            "Semantic recovery routing failed, keeping existing provider: %s", exc
        )

    try:
        recovered_data = await retry_async(
            lambda: svc._fetch_data(recovered_intent),
            max_attempts=2,
            initial_delay=0.5,
        )
    except Exception as exc:
        logger.info("Semantic recovery fetch failed: %s", exc)
        return None

    if not recovered_data:
        return None

    recovered_data = svc._rerank_data_by_query_relevance(query, recovered_data)
    if ranking_or_comparison:
        recovered_data = svc._apply_ranking_projection(query, recovered_data)
    return recovered_data


async def maybe_improve_country_coverage(
    svc: "QueryService",
    query: str,
    intent: Optional[ParsedIntent],
    data: Optional[List[NormalizedData]],
) -> tuple[List[NormalizedData], Optional[str]]:
    """Try to improve multi-country coverage via fallback providers, then
    return data plus optional warning when coverage remains partial.
    """
    current_data = list(data or [])
    if not intent or not current_data:
        return current_data, None

    provider = normalize_provider_name(intent.apiProvider or "")
    params = intent.parameters or {}
    if provider == "COMTRADE":
        bilateral_trade_query = False
        router = getattr(svc, "unified_router", None)
        if router is not None:
            try:
                bilateral_trade_query = bool(
                    router._is_bilateral_trade_query(query.lower(), query)
                )
            except Exception:
                bilateral_trade_query = False
        if bilateral_trade_query or params.get("reporter") or params.get("partner"):
            return current_data, None

    initial_coverage = svc._assess_country_coverage(intent, current_data)
    if not initial_coverage or initial_coverage.get("complete"):
        return current_data, None

    logger.warning(
        "Partial country coverage detected for query '%s': covered=%s/%s missing=%s",
        query,
        initial_coverage.get("covered_count"),
        initial_coverage.get("requested_count"),
        initial_coverage.get("missing_display"),
    )

    best_data = current_data
    best_coverage = initial_coverage

    missing_display = [str(item) for item in (initial_coverage.get("missing_display") or []) if item]
    missing_fetch_targets = [
        normalize_country_to_iso2(item) or str(item)
        for item in (initial_coverage.get("missing_iso2") or initial_coverage.get("missing_display") or [])
        if item
    ]
    if provider and missing_fetch_targets:
        supplemental_data: List[NormalizedData] = []
        for missing_country in missing_fetch_targets:
            child_intent = intent.model_copy(deep=True)
            child_params = dict(params)
            child_params.pop("countries", None)
            child_params["country"] = missing_country
            child_intent.parameters = child_params
            child_intent.apiProvider = provider
            try:
                fetched = await retry_async(
                    lambda intent=child_intent: svc._fetch_data(intent),
                    max_attempts=2,
                    initial_delay=0.3,
                )
            except Exception as exc:
                logger.info(
                    "Coverage same-provider backfill failed for %s via %s: %s",
                    missing_country,
                    provider,
                    exc,
                )
                continue
            if fetched:
                supplemental_data.extend(list(fetched))

        if supplemental_data:
            merged_data: List[NormalizedData] = []
            seen_keys: set[tuple[str, str, str, str]] = set()
            for series in list(current_data) + supplemental_data:
                metadata = getattr(series, "metadata", None) if series is not None else None
                key = (
                    str(getattr(metadata, "source", "") or "").strip().upper(),
                    str(getattr(metadata, "seriesId", "") or "").strip().upper(),
                    str(getattr(metadata, "country", "") or "").strip().upper(),
                    str(getattr(metadata, "indicator", "") or "").strip().lower(),
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                merged_data.append(series)

            merged_coverage = svc._assess_country_coverage(intent, merged_data)
            if merged_coverage:
                merged_score = (
                    float(merged_coverage.get("coverage_ratio", 0.0)),
                    int(merged_coverage.get("covered_count", 0)),
                )
                best_score = (
                    float(best_coverage.get("coverage_ratio", 0.0)),
                    int(best_coverage.get("covered_count", 0)),
                )
                if merged_score > best_score:
                    best_data = merged_data
                    best_coverage = merged_coverage
                    logger.info(
                        "Same-provider coverage backfill improved country coverage to %s/%s",
                        best_coverage.get("covered_count"),
                        best_coverage.get("requested_count"),
                    )

    if best_coverage.get("complete"):
        return best_data, None

    try:
        fallback_data = await svc._try_with_fallback(
            intent,
            DataNotAvailableError("Partial multi-country coverage from primary provider"),
        )
    except Exception as exc:
        logger.info("Coverage fallback attempt failed: %s", exc)
        fallback_data = None

    if fallback_data:
        fallback_data = svc._rerank_data_by_query_relevance(query, fallback_data)
        fallback_data = svc._apply_ranking_projection(query, fallback_data)
        fallback_coverage = svc._assess_country_coverage(intent, fallback_data)
        if fallback_coverage:
            fallback_score = (
                float(fallback_coverage.get("coverage_ratio", 0.0)),
                int(fallback_coverage.get("covered_count", 0)),
            )
            best_score = (
                float(best_coverage.get("coverage_ratio", 0.0)),
                int(best_coverage.get("covered_count", 0)),
            )
            if fallback_score > best_score:
                best_data = fallback_data
                best_coverage = fallback_coverage
                logger.info(
                    "Coverage fallback improved country coverage to %s/%s",
                    best_coverage.get("covered_count"),
                    best_coverage.get("requested_count"),
                )
        elif fallback_data:
            logger.debug(
                "Coverage fallback returned data without country labels; keeping primary result"
            )

    warning_message = svc._build_country_coverage_warning_message(best_coverage)
    return best_data, warning_message or None


def add_provider_transparency(
    svc: "QueryService",
    result: QueryResponse,
    original_query: str,
) -> QueryResponse:
    """Add transparency message when data comes from a different provider
    than requested.  Runs on ALL query responses.

    When the user explicitly asked for a specific provider (e.g., "from BIS")
    but the data came from a different one (e.g., WorldBank fallback), the
    response gets a message explaining the substitution.
    """
    if not result.data or not result.intent:
        return result

    explicit_provider = svc._detect_explicit_provider(original_query)
    if not explicit_provider:
        return result

    actual_sources = set()
    for series in result.data:
        if series.metadata and series.metadata.source:
            actual_sources.add(series.metadata.source)

    explicit_norm = normalize_provider_name(explicit_provider)
    source_map = {
        "FRED": "FRED", "World Bank": "WORLDBANK", "Statistics Canada": "STATSCAN",
        "IMF": "IMF", "BIS": "BIS", "Eurostat": "EUROSTAT", "OECD": "OECD",
        "UN Comtrade": "COMTRADE", "CoinGecko": "COINGECKO", "ExchangeRate-API": "EXCHANGERATE",
    }
    actual_norm = {source_map.get(s, s.upper()) for s in actual_sources}

    if explicit_norm not in actual_norm:
        actual_display = ", ".join(actual_sources)
        note = (
            f"**Note:** {explicit_provider} did not have this data. "
            f"Showing results from {actual_display} instead."
        )
        if result.message:
            result.message = f"{note}\n\n{result.message}"
        else:
            result.message = note
        logger.info(
            "Provider transparency: requested=%s, actual=%s",
            explicit_provider, actual_sources,
        )

    return result


def looks_like_country_follow_up(
    query: str,
    target_countries: List[str],
) -> bool:
    """Detect short geography-only follow-ups such as "show only US" or "Japan".

    These should reuse the last intent instead of being reparsed as a brand
    new query with no indicator context.
    """
    query_text = str(query or "").strip()
    if not query_text or not target_countries:
        return False

    query_lower = query_text.lower()
    tokens = re.findall(r"[a-zA-Z]+", query_lower)
    if not tokens:
        return False

    allowed_tokens = {
        "show", "only", "just", "keep", "filter", "now", "instead",
        "use", "plot", "display", "me", "the", "for", "in", "to",
        "add", "also", "include", "plus", "and", "with", "compare",
        "what", "about", "how", "same", "but", "too", "well", "as",
    }
    geography_tokens = {country.lower() for country in target_countries}
    for country in target_countries:
        if country.upper() == "US":
            geography_tokens.update({"united", "states", "usa", "us", "america"})
        iso3 = CountryResolver.to_iso3(country)
        if iso3:
            geography_tokens.add(iso3.lower())
        # Add common country name tokens so "Add Germany" matches ["DE"]
        for alias, code in CountryResolver.COUNTRY_ALIASES.items():
            if code == country.upper():
                for token in alias.split():
                    geography_tokens.add(token.lower())

    non_geography_tokens = [
        token for token in tokens
        if token not in allowed_tokens and token not in geography_tokens
    ]
    return len(non_geography_tokens) == 0


def maybe_expand_multi_concept_intent(
    svc: "QueryService",
    query: str,
    intent: ParsedIntent,
) -> bool:
    """Auto-expand clearly comparative multi-concept queries into
    multi-indicator intent.

    This reduces unnecessary clarification loops for queries like
    "compare unemployment and inflation for G7 countries".
    """
    if not intent:
        return False
    if intent.indicators and len(intent.indicators) > 1:
        return False
    if not (svc._is_comparison_query(query) or svc._is_ranking_query(query)):
        return False

    inferred_indicators = svc._infer_multi_concept_indicators_from_query(query)
    if len(inferred_indicators) < 2:
        return False

    target_countries = svc._collect_target_countries(intent.parameters)
    if len(target_countries) < 2:
        extracted = svc._extract_countries_from_query(query)
        expanded = CountryResolver.expand_regions_in_query(query)
        target_countries = extracted or expanded or target_countries
    if len(target_countries) < 2:
        return False

    params = dict(intent.parameters or {})
    params.pop("country", None)
    params["countries"] = list(
        dict.fromkeys([str(c) for c in target_countries if c])
    )
    params.pop("indicator", None)
    params.pop("seriesId", None)
    params.pop("series_id", None)
    params.pop("code", None)

    intent.parameters = params
    intent.indicators = inferred_indicators
    intent.clarificationNeeded = False
    intent.clarificationQuestions = []

    logger.info(
        "🧩 Auto-expanded multi-concept comparison query into indicators=%s countries=%s",
        inferred_indicators,
        params.get("countries"),
    )
    return True


def build_intent_from_semantic_clarification(
    svc: "QueryService",
    pending: dict,
    selected_option: ClarificationOption,
    refined_query: str,
) -> Optional[ParsedIntent]:
    """Build a deterministic intent for clarification follow-ups when possible.

    This avoids sending an already-disambiguated reply back through the full
    LLM parse path.
    """
    kind = str(pending.get("kind") or "").strip()
    option_label = str(selected_option.label or "").strip()
    query_text = str(refined_query or "").strip()
    if not query_text:
        return None

    # "group as a whole" still requires more nuanced aggregate handling.
    if kind == "group_scope" and "compare member countries" not in option_label.lower():
        return None

    extracted_countries = svc._extract_countries_from_query(query_text)
    expanded_region_countries = CountryResolver.expand_regions_in_query(query_text)
    params: dict = {}

    if expanded_region_countries and (
        "member countries" in query_text.lower()
        or svc._is_comparison_query(query_text)
    ):
        params["countries"] = expanded_region_countries
    elif len(extracted_countries) == 1:
        params["country"] = extracted_countries[0]
    elif len(extracted_countries) > 1:
        params["countries"] = extracted_countries

    indicator_text = (
        option_label
        if kind != "group_scope"
        else svc._extract_indicator_text_from_refined_query(query_text)
    )
    indicator_text = (
        str(indicator_text or "").strip()
        or svc._extract_indicator_text_from_refined_query(query_text)
    )
    if not indicator_text:
        return None

    routing_decision = svc.unified_router.route(
        query=query_text,
        indicators=[indicator_text],
        country=params.get("country"),
        countries=params.get("countries"),
        llm_provider=None,
    )
    api_provider = normalize_provider_name(selected_option.provider or routing_decision.provider)
    if selected_option.code:
        params["indicator"] = str(selected_option.code)
    if selected_option.provider:
        params["__semantic_provider_locked"] = True
    if (
        params.get("countries")
        and len(params["countries"]) > 1
        and not svc._provider_covers_country_list(api_provider, params["countries"])
    ):
        api_provider = "WORLDBANK"

    intent = ParsedIntent(
        apiProvider=api_provider,
        indicators=[indicator_text],
        parameters=params,
        clarificationNeeded=False,
        confidence=0.95,
        recommendedChartType="line",
        originalQuery=query_text,
    )
    svc._apply_country_overrides(intent, query_text)
    return intent
