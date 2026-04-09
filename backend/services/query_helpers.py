"""Self-contained helper functions extracted from query.py.

Phase 5 decomposition: extracts three larger helpers (~340 lines total)
that have minimal dependencies on QueryService state:

- extract_countries_from_query: thin wrapper over CountryResolver
- apply_country_overrides: query-based geography overrides on intent
- build_alternative_series: FTS5-based alternative indicator suggestions
- should_use_deep_agents: complexity-based routing decision

These functions are called from QueryService via one-line delegates.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional, TYPE_CHECKING

from ..models import NormalizedData, ParsedIntent, QueryResponse
from ..routing.country_resolver import CountryResolver
from ..services.query_complexity import QueryComplexityAnalyzer
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
    countries = CountryResolver.detect_all_countries_in_query(query)
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

    Performance optimizations:
    1. Skip entirely for catalog-resolved indicators (high confidence).
    2. Uses FTS5 full-text search instead of LIKE '%...%' scan.
    """
    from .indicator_database import IndicatorDatabase
    from ..models import AlternativeSeries

    try:
        if not data:
            return None

        # Skip alternatives for catalog-resolved indicators (already correct).
        if getattr(intent, "_catalog_resolved", False):
            logger.debug(
                "Skipping alternative series — catalog-resolved indicator: %s",
                (intent.parameters or {}).get("indicator", "?"),
            )
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

    if best_coverage.get("complete"):
        return best_data, None

    warning_message = svc._build_country_coverage_warning_message(best_coverage)
    return best_data, warning_message or None


def resolve_concept_for_fallback(
    svc: "QueryService",
    intent: ParsedIntent,
    primary_provider: str,
) -> Optional[str]:
    """Resolve a catalog concept name from the intent for cross-provider fallback.

    Checks (in order):
    1. Stored ``__catalog_concept`` parameter (set during catalog resolution)
    2. Reverse lookup from provider-specific indicator code via catalog
    3. Forward lookup from the original query text via ``find_concept_by_term``

    Returns:
        Catalog concept name (e.g., ``"exports_pct_gdp"``) or ``None``.
    """
    try:
        from .catalog_service import find_concept_by_term, find_concepts_by_code

        # 1. Check stored concept from prior catalog resolution
        stored_concept = (intent.parameters or {}).get("__catalog_concept")
        if stored_concept:
            logger.debug("Fallback concept from __catalog_concept: %s", stored_concept)
            return str(stored_concept)

        # 2. Reverse lookup: provider code -> concept
        for ind in (intent.indicators or []):
            ind_str = str(ind or "").strip()
            if ind_str and svc._looks_like_provider_indicator_code(primary_provider, ind_str):
                concepts = find_concepts_by_code(primary_provider, ind_str)
                if concepts:
                    logger.debug(
                        "Fallback concept via reverse code lookup (%s/%s): %s",
                        primary_provider, ind_str, concepts[0],
                    )
                    return concepts[0]

        # Also check the 'indicator' parameter which may hold the resolved code
        param_indicator = str((intent.parameters or {}).get("indicator", "")).strip()
        if param_indicator and svc._looks_like_provider_indicator_code(
            primary_provider, param_indicator
        ):
            concepts = find_concepts_by_code(primary_provider, param_indicator)
            if concepts:
                logger.debug(
                    "Fallback concept via param indicator reverse lookup (%s/%s): %s",
                    primary_provider, param_indicator, concepts[0],
                )
                return concepts[0]

        # 3. Forward lookup: original query text -> concept
        original_query = str(intent.originalQuery or "").strip()
        if original_query:
            concept = find_concept_by_term(original_query)
            if concept:
                logger.debug("Fallback concept via query text: %s", concept)
                return concept

            # Try distilled query
            distilled = svc._build_distilled_indicator_query(original_query)
            if distilled:
                concept = find_concept_by_term(distilled)
                if concept:
                    logger.debug("Fallback concept via distilled query: %s", concept)
                    return concept

    except Exception as exc:
        logger.debug("Concept resolution for fallback failed: %s", exc)

    return None
