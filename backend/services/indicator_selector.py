"""
Indicator Selector — retrieval + LLM adjudication for ALL 330K indicators.

Architecture (decided 2026-04-01):
  Step 1: FTS5 + embedding retrieval → find candidate indicators
  Step 2: LLM picks, asks, or rejects the candidate set

No catalog injection or provider-code shortcut maps. Retrieval supplies the
candidate evidence; the LLM adjudicates the user's requested measure.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from ..config import Settings
from ..routing.country_resolver import CountryResolver
from ..utils.providers import normalize_provider_name


_BILATERAL_TITLE_MARKER_RE = re.compile(r"\b(?:from|to|with|vis-a-vis|versus|vs\.?)\b", re.IGNORECASE)


@lru_cache(maxsize=4096)
def _iso3_suffix_family_exists(code: str) -> bool:
    """Data-driven check that a code's trailing ISO3 really is a country-family
    suffix: true only when sibling codes sharing the prefix but carrying a
    DIFFERENT ISO3 suffix exist in the indicator database (e.g. FPCPITOTLZGUSA
    has siblings ...ZGIND/...ZGFRA). Prevents codes that merely *end* in an
    ISO3 string (DEXCAUS, codes ending PER/AND/...) from being mis-tagged.
    Fails neutral (False) on any error.
    """
    prefix = str(code or "")[:-3]
    if len(prefix) < 3:
        return False
    try:
        conn = sqlite3.connect(str(_INDICATORS_DB))
        try:
            # substr/length equality instead of LIKE: codes routinely contain
            # '_' (IMF GGXWDG_NGDP, Eurostat NAMA_10_GDP), which LIKE treats
            # as a single-char wildcard — false sibling families would make
            # the country constraint drop correct candidates.
            rows = conn.execute(
                "SELECT DISTINCT substr(code, -3) FROM indicators "
                "WHERE length(code) = ? AND substr(code, 1, ?) = ? "
                "AND code != ? LIMIT 200",
                (len(prefix) + 3, len(prefix), prefix, code),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return False
    own_suffix = str(code or "").strip().upper()[-3:]
    return any(
        CountryResolver.ISO3_TO_ISO2.get(str(row[0] or "").strip().upper())
        and str(row[0] or "").strip().upper() != own_suffix
        for row in rows
    )


def _derive_candidate_country(code: str, name: str) -> Optional[str]:
    """Best-effort ISO2 country a candidate series is *specifically about*, or
    None when it is country-agnostic / global / US-domestic-without-marker.

    Purely data-driven — NO per-indicator mapping:
      1. an explicit country named in the series title ("... for Canada"),
      2. trailing ISO3 in the provider code (FRED ``FPCPITOTLZG**USA**``
         family), verified against the catalog: trusted only when sibling
         codes with other ISO3 suffixes exist for the same prefix.

    Used to enforce that a resolved series matches the requested country, so
    country-suffixed families can't be picked for the wrong country by text
    similarity alone. Returns None for codes/titles with no country evidence
    (e.g. ``CPIAUCSL``, ``GDP``, ``bitcoin``) — those are treated as neutral and
    never filtered out. Neutral is always the safe direction: neutral
    candidates are never dropped, they just get no country-rank preference.
    """
    title = str(name or "")
    titled = CountryResolver.detect_all_countries_in_query(title)
    if len(titled) > 1:
        # Bilateral/cross-country series (FX pairs, "US exports to China"):
        # the series is about a relationship, not a single country.
        return None
    if len(titled) == 1:
        # A partner preposition next to the only detected country means the
        # detected country is likely the PARTNER, not the subject ("Imports of
        # Goods from China" queried for the US). Structural guard only — when
        # in doubt we stay neutral, never mis-tag.
        if _BILATERAL_TITLE_MARKER_RE.search(title):
            return None
        return titled[0]
    code_raw = str(code or "").strip()
    code_s = code_raw.upper()
    if len(code_s) >= 6:
        iso2 = CountryResolver.ISO3_TO_ISO2.get(code_s[-3:])
        if iso2 and _iso3_suffix_family_exists(code_raw):
            return iso2
    return None

# Indicators database path
_INDICATORS_DB = Path(__file__).parent.parent / "data" / "indicators.db"

logger = logging.getLogger(__name__)

_settings: Optional[Settings] = None


def _get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


_FREQUENCY_ALIASES = {
    "daily": {"daily", "day", "d"},
    "weekly": {"weekly", "week", "w"},
    "monthly": {"monthly", "month", "m"},
    "quarterly": {"quarterly", "quarter", "q"},
    "annual": {"annual", "annually", "yearly", "year", "a"},
}

_UNIT_CUE_RE = re.compile(
    r"\b(?:dollars?|u\.?s\.?|usd|percent(?:age)?|index|capita|ppp|"
    r"currency|millions?|billions?|thousands?|trillions?|units?)\b|"
    r"\b\d{4}\s*=\s*100\b",
    flags=re.IGNORECASE,
)

_MEASUREMENT_QUALIFIER_ALIASES = {
    "real": {"real", "inflation adjusted", "inflation-adjusted", "deflated", "cpi", "constant"},
    "nominal": {"nominal", "current"},
}


def _normalize_metadata_text(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def _extract_requested_frequencies(text: str) -> set[str]:
    normalized = _normalize_metadata_text(text)
    # Recency WINDOWS are time ranges, not frequency requests: "past year",
    # "last 3 months", "this quarter" must not fire annual/monthly/quarterly
    # (observed: "India CPI by month, past year" extracted {monthly, annual},
    # so the annual WB-mirror ALSO 'matched the requested frequency').
    normalized = re.sub(
        r"\b(?:past|last|this|next|previous|recent)\s+(?:\d+\s+)?"
        r"(?:years?|months?|quarters?|weeks?|days?)\b",
        " ",
        normalized,
    )
    tokens = set(normalized.split())
    found: set[str] = set()
    for canonical, aliases in _FREQUENCY_ALIASES.items():
        alias_tokens = {_normalize_metadata_text(alias) for alias in aliases}
        long_aliases = {alias for alias in alias_tokens if len(alias) > 1}
        one_letter_aliases = {alias for alias in alias_tokens if len(alias) == 1}
        if canonical == "annual":
            long_aliases = {
                alias
                for alias in long_aliases
                if not re.search(rf"\b\d+\s*{re.escape(alias)}\b", normalized)
            }
        if tokens & long_aliases:
            found.add(canonical)
            continue
        if any(re.search(rf"\(\s*{re.escape(alias)}\s*\)", normalized) for alias in one_letter_aliases):
            found.add(canonical)
    return found


def _frequency_matches(requested: set[str], candidate_frequency: str) -> bool:
    if not requested:
        return True
    normalized = _normalize_metadata_text(candidate_frequency)
    tokens = set(normalized.split())
    if not normalized:
        return False
    for canonical in requested:
        aliases = {
            _normalize_metadata_text(alias)
            for alias in _FREQUENCY_ALIASES.get(canonical, {canonical})
        }
        if canonical in tokens or tokens & aliases or normalized.startswith(canonical):
            return True
    return False


def _extract_requested_unit_tokens(text: str) -> set[str]:
    query = re.sub(r"\b(?:from|via|use)\s+[a-z][a-z0-9 ._-]*$", "", str(text or ""), flags=re.IGNORECASE)
    query = re.sub(
        r"\s+\((?:daily|weekly(?:,\s*ending\s+[a-z]+)?|monthly|quarterly|annual|yearly)\)\s*$",
        "",
        query,
        flags=re.IGNORECASE,
    ).strip()
    match = re.search(r"\s+in\s+(?P<unit>[^,;:]+)$", query, flags=re.IGNORECASE)
    if not match:
        return set()
    unit_text = match.group("unit")
    if not _UNIT_CUE_RE.search(unit_text):
        return set()
    return {
        token
        for token in _normalize_metadata_text(unit_text).split()
        if len(token) > 1 and token not in {"and", "the", "per", "of", "at", "in"}
    }


def _unit_matches(requested_unit_tokens: set[str], candidate_unit: str) -> bool:
    if not requested_unit_tokens:
        return True
    candidate_tokens = {
        token
        for token in _normalize_metadata_text(candidate_unit).split()
        if len(token) > 1 and token not in {"and", "the", "per", "of", "at", "in"}
    }
    return bool(candidate_tokens and requested_unit_tokens <= candidate_tokens)


def _extract_requested_measurement_qualifiers(text: str) -> set[str]:
    tokens = _normalize_metadata_text(text).split()
    found: set[str] = set()
    for idx, token in enumerate(tokens):
        next_token = tokens[idx + 1] if idx + 1 < len(tokens) else ""
        if token == "real" and next_token != "estate":
            found.add("real")
        elif token == "nominal":
            found.add("nominal")
    normalized = _normalize_metadata_text(text)
    if "inflation adjusted" in normalized:
        found.add("real")
    return found


def _measurement_matches(requested: set[str], candidate_text: str) -> bool:
    if not requested:
        return True
    normalized = _normalize_metadata_text(candidate_text)
    return all(
        any(_normalize_metadata_text(alias) in normalized for alias in _MEASUREMENT_QUALIFIER_ALIASES.get(term, {term}))
        for term in requested
    )


LLM_SELECTION_PROMPT = """You are selecting the best economic indicator for a user's data query.

User query: "{query}"
Data provider: {provider}

Available indicators (numbered):
{options}

INSTRUCTIONS:

A query like "unemployment" clearly means the GENERAL/TOTAL unemployment rate.
Variants like "youth unemployment", "female unemployment" are SUBSETS — they are
NOT what the user meant unless they explicitly asked for them.

Only consider the query AMBIGUOUS when there are genuinely DIFFERENT MEASURES of
the same concept that the user might want. For example:
- "health spending" is ambiguous: % of GDP vs per capita vs absolute dollars
- "GDP" is ambiguous: current US$ vs constant dollars vs PPP
- "unemployment" is NOT ambiguous: it means the total rate

DECISION:
- Try to PICK one indicator when the candidate list contains a semantically valid answer.
- Use REJECT when NONE of the provided candidates answer the requested measure.
- Only use ASK when the user's query EXPLICITLY asks about something that has
  fundamentally different measurement approaches (e.g., "health spending" could
  be % of GDP OR per capita — genuinely different numbers).
- Single-word or broad queries like "GDP", "trade", "energy" should PICK the
  most standard version, NOT ask the user.
- Do NOT REJECT just because the query is broad. REJECT only when all candidates
  are clearly about different concepts, populations, geographies, frequencies,
  or units than the user's requested measure.

Reply with one of:
- "PICK: <number>" when a candidate answers the query.
- "ASK: <number>,<number>,..." when the user must choose between genuinely
  different measurement approaches.
- "REJECT: <short reason>" and "SEARCH: <alternative search terms>" when no
  candidate answers the requested measure.

Defaults for broad queries:
- "GDP" → GDP current US$ (most standard)
- "trade" → trade (% of GDP)
- "energy" → energy use per capita or total
- "emissions" → CO2 emissions total
- "education" → school enrollment primary
- "agriculture" → agriculture value added (% of GDP)
- "manufacturing" → manufacturing value added (% of GDP)

CRITICAL RULE — Match the MEASURE TYPE the user asked for:
- "inflation rate", "growth rate", "year-over-year change", "% change" → the
  candidate must itself BE a rate/percent-change series (unit like "Percent",
  "Annual %", "% change"). An index LEVEL (unit like "Index 1982-84=100") or a
  currency LEVEL is NOT the requested measure — never PICK a level series when
  the user asked for a rate, even if its name mentions the same concept.
- Conversely, "CPI", "price index", "GDP" with no rate wording → a level/index
  series is the correct answer.
- Use each candidate's unit/metadata shown in parentheses to decide. If no
  candidate carries the requested measure type, REJECT and put the measure in
  the SEARCH terms (e.g. "consumer price inflation annual percent change").

CRITICAL RULE — Match specificity of answer to specificity of question:
- "unemployment rate" → NATIONAL/AGGREGATE (never county or MSA level)
- "unemployment rate Florida" → STATE level Florida
- "unemployment rate Sarasota County" → COUNTY level (user was specific)
- If the user did NOT mention a state/county/city, NEVER pick a geographic sub-unit.
- If the user did NOT mention "female", "youth", "male", ALWAYS pick "total".
- If the user did NOT mention "seasonally adjusted" or "NSA", prefer seasonally adjusted.
- The answer should NEVER be more specific than what the user asked for.

CRITICAL RULE — Frequency matching:
- If the user query contains "monthly" / "month" → MUST pick a series with
  frequency=monthly (e.g., une_rt_m, not une_rt_a).
- If the user query contains "quarterly" / "quarter" → MUST pick frequency=quarterly.
- If the user query contains "annual" / "annually" / "yearly" → prefer frequency=annual.
- If the user query contains "daily" → prefer frequency=daily.
- If the user query contains "weekly" → prefer frequency=weekly.
- A monthly variant is BETTER than annual when monthly is requested, even if the
  annual variant has a slightly more popular code.  Frequency match is a HARD
  constraint, not a preference.
- If no frequency-matching candidate exists, fall back to the closest available
  (e.g., monthly if no daily exists), and note "frequency unavailable" in reasoning.

Selection rules:
- NEVER pick a DISCONTINUED series when active alternatives exist
- Prefer ACTIVE (recent data) over DISCONTINUED/OBSOLETE series
- When one candidate is marked "MOST-USED series for this concept", pick it
  for plain-concept asks unless the user explicitly requested a variant
  (a sub-population, a different adjustment, a specific source).
- NEVER pick an experimental/research/model-based index (e.g. names containing
  "index", "experimental", "tracker", "nowcast", or a research-team brand) over
  the OFFICIAL headline measure of the same concept when both are candidates —
  the user asking for a plain concept wants the official statistic.
- Prefer NATIONAL/AGGREGATE over state/county/MSA/regional variants
- Prefer TOTAL over demographic subsets (female, male, youth, elderly)
- For direct count/number/total requests, prefer an indicator that measures the
  requested entity count directly. Do not pick distribution, ratio, subset,
  account-allocation, or breakdown tables unless the user explicitly asks for
  that narrower measure.
- Prefer SEASONALLY ADJUSTED over not adjusted (NSA)
- Prefer modeled ILO estimates (SL.*) over Jobs Indicators (JI.*)
- Prefer % of GDP over absolute values for cross-country comparison
- Prefer gross enrollment over net unless user specifies "net"
- Prefer SHORTER/SIMPLER indicator codes when concepts are identical
- Prefer the most GENERAL version — never pick a more specific variant than asked

FRED-specific rules for near-identical series:
- Flow of Funds codes (BOGZ1F...): prefer L (Level) over R (Revaluation),
  A (Transactions), U (Volume changes). Users want LEVELS unless specified.
- When two codes differ only by trailing letters (Q vs A, SA vs NSA, MM vs YY):
  prefer the STANDARD version (SA, Annual, or the shorter code)
- "Total Private" is more general than "Construction" or other industry subsets
- Prefer US Dollar denomination over other currencies unless user specifies

When in doubt between valid variants, PICK the most general — the user can
always ask for a variant. When none are valid, REJECT and provide better SEARCH
terms."""


def build_canonical_arm_kwargs(
    intent: Any,
    selector_query: str,
    country: Optional[str] = None,
) -> Dict[str, Any]:
    """Optional english_terms kwarg for ``IndicatorSelector.select``.

    intent.indicators carries the parse LLM's canonical-English metric name.
    Two cases need it fused as an extra RRF retrieval arm:
    - non-English queries (the original Proposal A arm), and
    - ENGLISH colloquialisms where the chooser's low-overlap guard fell back
      to the raw text ("jobs numbers" vs canonical "nonfarm payrolls" share
      zero tokens BY DESIGN of parse normalization; raw-text retrieval then
      misses the canonical series entirely — live: Indeed-postings junk while
      PAYEMS ranks #1-#2 at 2x score under the canonical term).
    Single construction point so every select() call site threads it
    identically (the region/constraint kwargs each got added at one seam and
    missed the other — this ends that class). Returns {} when the arm would
    be a no-op.
    """
    terms = " ".join(
        str(t).strip() for t in (getattr(intent, "indicators", None) or []) if str(t).strip()
    ).strip()
    if not terms:
        return {}
    country_s = str(country or "").strip()
    if country_s and country_s.lower() not in terms.lower():
        terms = f"{terms} {country_s}"
    sel_q = str(selector_query or "").strip().lower()
    if not sel_q or terms.lower() == sel_q:
        return {}
    language = str(getattr(intent, "language", "") or "").strip().lower()
    if language and language != "en":
        return {"english_terms": terms}
    # English path: fuse only on ZERO content-token overlap (the colloquial
    # case). Any shared content token means the raw text already carries the
    # canonical vocabulary and the dominant path suffices.
    _stop = {"us", "the", "for", "in", "of", "a", "an", "to", "united", "states"}
    sel_tokens = set(re.findall(r"[a-z0-9]+", sel_q)) - _stop
    canon_tokens = set(re.findall(r"[a-z0-9]+", terms.lower())) - _stop
    if canon_tokens and sel_tokens and not (canon_tokens & sel_tokens):
        return {"english_terms": terms}
    return {}


def build_continuity_kwargs(params: Any) -> Dict[str, Any]:
    """Optional continuity kwarg for ``IndicatorSelector.select``.

    conversation_state_v2 stamps params['__continuity_series'] ("phrase
    [CODE]") when a frequency/country change invalidates the resolved code:
    the re-resolution must return the SAME measure with one aspect changed.
    Near-tie adjudication without this flips variants or asks (battery
    mr1/mr3). Single construction point, opt-in like every steering kwarg.
    """
    value = ""
    try:
        value = str((params or {}).get("__continuity_series") or "").strip()
    except AttributeError:
        value = ""
    return {"continuity_series": value} if value else {}


def build_region_selection_kwargs(
    region: Optional[str],
    provider: Optional[str],
    statscan_provider: Any = None,
) -> Dict[str, Any]:
    """Optional region kwargs for ``IndicatorSelector.select``.

    Single construction point for region steering so every select() call site
    (main resolution, prefetch clarification, option collection) threads the
    region identically: the region string joins the selection-cache key and the
    adjudicator prompt; for StatsCan a cache-only Geography coverage probe is
    attached so candidates are annotated with real membership. The region is
    NEVER placed into retrieval/selector query text (nationally-titled cubes
    that contain the province as a member would be wrongly rejected). Returns
    {} when no region is set, leaving non-region call sites untouched.
    """
    region_s = str(region or "").strip()
    if not region_s:
        return {}
    kwargs: Dict[str, Any] = {"region": region_s}
    if (
        normalize_provider_name(provider or "") == "STATSCAN"
        and statscan_provider is not None
        and hasattr(statscan_provider, "region_coverage_from_cache")
    ):
        async def _region_coverage_probe(
            codes: List[str],
            _prov=statscan_provider,
            _reg=region_s,
        ) -> Dict[str, Optional[bool]]:
            # Cache-only; no network on the adjudication hot path.
            return _prov.region_coverage_from_cache(codes, _reg)

        kwargs["region_coverage_probe"] = _region_coverage_probe
    return kwargs


class IndicatorSelector:
    """Hybrid retrieval plus LLM indicator selection."""

    def __init__(self, settings: Optional[Settings] = None):
        self._settings = settings or _get_settings()

    async def select(
        self,
        query: str,
        provider: str,
        country: Optional[str] = None,
        metadata_query: Optional[str] = None,
        exclude_codes: Optional[set] = None,
        english_terms: Optional[str] = None,
        region: Optional[str] = None,
        region_coverage_probe: Optional[
            Callable[[List[str]], Awaitable[Dict[str, Optional[bool]]]]
        ] = None,
        continuity_series: Optional[str] = None,
    ) -> SelectionResult:
        """Cached wrapper around the full selection pipeline.

        Confident llm_pick results for identical resolution inputs are served
        from a per-worker TTL cache (the FTS + embedding + LLM pipeline is the
        dominant per-query latency and the same concept recurs across users).
        Ambiguous outcomes, rejections, and alternate-retry calls
        (exclude_codes) always run the full pipeline.

        ``region`` (intent.subnationalRegion) and ``region_coverage_probe`` steer
        selection toward a candidate that actually covers the requested
        sub-national region (see ``_llm_pick``). ``region`` is part of the cache
        identity because it changes the adjudicated pick — without it a cached
        "Ontario" pick could be served for a "Quebec" query.
        """
        cache_key = None
        if not exclude_codes:
            cache_key = (
                str(provider or "").strip().upper(),
                str(query or "").strip().lower(),
                str(country or "").strip().upper(),
                str(metadata_query or "").strip().lower(),
                str(english_terms or "").strip().lower(),
                str(region or "").strip().lower(),
                str(continuity_series or "").strip().lower(),
            )
            cached = _selection_cache_get(cache_key)
            if cached is not None:
                logger.info(
                    "⚡ Selection cache HIT: %r/%s -> %s",
                    cache_key[1][:60], cache_key[0], cached.get("code"),
                )
                return SelectionResult(
                    code=cached.get("code"),
                    name=cached.get("name"),
                    source=cached.get("source", "llm_pick"),
                    # Carry the confidence signal across the cache so a repeat
                    # query keeps (or is denied) request-level authority exactly
                    # as its first, freshly-adjudicated resolution did — repeats
                    # are precisely where the flip-flop shows up.
                    made_under_prefer_ask=bool(cached.get("made_under_prefer_ask", False)),
                )

        result = await self._select_uncached(
            query,
            provider,
            country=country,
            metadata_query=metadata_query,
            exclude_codes=exclude_codes,
            english_terms=english_terms,
            region=region,
            region_coverage_probe=region_coverage_probe,
            continuity_series=continuity_series,
        )

        if (
            cache_key is not None
            and result is not None
            and result.code
            and result.source == "llm_pick"
            and not result.needs_user_choice
        ):
            _selection_cache_put(
                cache_key,
                {
                    "code": result.code,
                    "name": result.name,
                    "source": result.source,
                    "made_under_prefer_ask": bool(
                        getattr(result, "made_under_prefer_ask", False)
                    ),
                },
            )
        return result

    async def _select_uncached(
        self,
        query: str,
        provider: str,
        country: Optional[str] = None,
        metadata_query: Optional[str] = None,
        exclude_codes: Optional[set] = None,
        english_terms: Optional[str] = None,
        region: Optional[str] = None,
        region_coverage_probe: Optional[
            Callable[[List[str]], Awaitable[Dict[str, Optional[bool]]]]
        ] = None,
        continuity_series: Optional[str] = None,
    ) -> SelectionResult:
        """
        Select the best indicator for a query.

        Step 1: Find 20 nearest indicators using OpenAI embeddings
        Step 2: LLM picks (or asks user if ambiguous)

        Args:
            query: Natural language query (e.g., "female youth unemployment")
            provider: Data provider (e.g., "WorldBank", "FRED")
            country: Optional country context
            metadata_query: Optional fuller query text used only for explicit
                frequency/unit/measurement metadata constraints.
            exclude_codes: Provider codes to drop from the candidate set before
                adjudication. Used by the same-provider alternate-retry path:
                when an LLM-picked code returns no data, re-adjudicating with it
                excluded lets the LLM choose the next-best EXECUTABLE code while
                preserving its semantic judgement (no blind next-in-rank pick).
            english_terms: Optional English canonical metric name(s) from the
                parse LLM (Proposal A). When non-empty and different from
                ``query`` (case-insensitive), the retrieval runs an ADDITIONAL
                FTS5 + embedding arm on it and fuses those ranked lists into the
                same RRF, giving non-English queries lexical recall against the
                English catalog. A no-op when empty or identical to ``query``.

        Returns:
            SelectionResult with selected code or options for user choice
        """
        _excluded_upper = {
            str(code).strip().upper() for code in (exclude_codes or set()) if str(code).strip()
        }

        def _drop_excluded(
            cands: List[tuple[str, str]], scs: List[float]
        ) -> tuple[List[tuple[str, str]], List[float]]:
            if not _excluded_upper:
                return cands, scs
            kept = [
                (c, s) for c, s in zip(cands, scs)
                if str(c[0]).strip().upper() not in _excluded_upper
            ]
            return [c for c, _s in kept], [s for _c, s in kept]

        # Region steering (Proposal: region-coverage-aware selection). Threaded
        # to _llm_pick ONLY when a sub-national region is actually set, so that
        # every existing _llm_pick override/fake with the original signature
        # keeps working unchanged (mirrors the english_query opt-in above).
        _continuity_kw: Dict[str, Any] = (
            {"continuity_series": str(continuity_series).strip()}
            if str(continuity_series or "").strip()
            else {}
        )
        _region_norm = str(region or "").strip()
        _region_kw: Dict[str, Any] = {}
        if _region_norm:
            _region_kw["region"] = _region_norm
            if region_coverage_probe is not None:
                _region_kw["region_coverage_probe"] = region_coverage_probe

        telemetry_enabled = bool(getattr(self._settings, "indicator_telemetry_enabled", False))
        fusion_mode = str(getattr(self._settings, "indicator_fusion", "legacy") or "legacy").lower()

        # Step 1: Find candidates via embedding similarity (with scores).
        # Offloaded to a worker thread: this does a blocking embedding HTTP call,
        # a ~125ms numpy matmul over the 330K×1536 index, and a sqlite FTS5 query
        # — running it inline would park the event loop for ~400ms and serialize
        # every concurrent request behind it. (Made thread-safe by the locked
        # lazy-init in embedding_retrieval/indicator_database and the per-call
        # sqlite connection in _get_candidates_fts5.)
        # Only thread english_query when it is actually present, so retrieval
        # overrides/fakes with the original signature keep working and the extra
        # arm stays a strict opt-in.
        _english_kw = {"english_query": english_terms} if english_terms else {}
        # Fuller user text (frequency/unit qualifiers) steers the metadata
        # prioritizer inside retrieval — opt-in like every steering kwarg.
        _retrieval_constraint_kw = (
            {"constraint_query": metadata_query}
            if str(metadata_query or "").strip()
            and str(metadata_query or "").strip().lower() != str(query or "").strip().lower()
            else {}
        )
        candidates, scores = await asyncio.to_thread(
            self._get_candidates_with_scores, query, provider,
            **_english_kw, **_retrieval_constraint_kw,
        )
        candidates, scores = _drop_excluded(candidates, scores)

        if not candidates:
            return SelectionResult(code=None, source="no_candidates")

        # Step 1.1: Country constraint. When the query targets a specific country,
        # never let a country-suffixed series for a DIFFERENT country win on text
        # similarity (e.g. "US CPI inflation" must not resolve to FRED's India
        # variant FPCPITOTLZGIND). Data-driven; a strict no-op for country-agnostic
        # queries/providers. If only cross-country candidates exist, refuse so the
        # resolver can fall back to a country-agnostic provider or clarify.
        #
        # has_country_match is True when the constraint found at least one
        # candidate whose series is specifically about the requested country
        # (e.g. "Constant GDP per capita for Canada"). In that case we surface
        # the country to the LLM picker so it prefers the country-matching
        # series over a neutral US-domestic-without-marker series that text
        # similarity alone would otherwise win. The reranking is not enough on
        # its own: the LLM ignores candidate order and picks by title, so a
        # country-only follow-up ("US GDP per capita" -> "for Canada") would
        # otherwise re-pick the generic US series.
        has_country_match = False
        if country:
            candidates, scores, all_conflict, has_country_match = self._apply_country_constraint(country, candidates, scores)
            if all_conflict:
                logger.info(
                    "🌍 No %s candidate for '%s' among country-tagged options — refusing wrong-country pick",
                    country, query[:80],
                )
                return SelectionResult(code=None, source="country_mismatch")

        # Step 1.5: If top candidates have very similar scores, retrieval can't
        # confidently distinguish them. Tell the LLM to ASK the user instead of
        # guessing. This reduces overconfident wrong picks.
        # Threshold: if top 3+ candidates are within 0.03 cosine similarity,
        # they're too similar for automated selection.
        candidates_are_ambiguous = self._scores_are_ambiguous(scores, fusion_mode)
        if candidates_are_ambiguous:
            logger.info(
                "🔍 Top candidates very similar (spread=%.4f, fusion=%s) — will prefer ASK",
                scores[0] - scores[2],
                fusion_mode,
            )

        # Step 2: LLM picks from top 20 candidates (embedding retrieves 50 for better recall)
        llm_candidates = candidates[:20]
        metadata_constraint_query = metadata_query or query
        # Only surface the country to the picker when a country-specific series
        # actually exists in the candidate set. For country-agnostic concepts
        # (single neutral series fetched per-country downstream) the country is
        # irrelevant to series choice and must not bias the LLM.
        llm_country = country if has_country_match else None
        # Opt-in kwarg (same pattern as _region_kw): only passed when the
        # fuller text differs, so test fakes with the old signature keep working.
        _constraint_kw: Dict[str, Any] = (
            {"constraint_query": metadata_constraint_query}
            if metadata_constraint_query and metadata_constraint_query != query
            else {}
        )
        result = await self._llm_pick(query, llm_candidates, provider, prefer_ask=candidates_are_ambiguous, country=llm_country, **_constraint_kw, **_continuity_kw, **_region_kw)
        result = await self._retry_if_metadata_conflict(metadata_constraint_query, result, llm_candidates, provider, **_region_kw)

        # Step 2.5: If the LLM says the whole candidate set is off-target,
        # honor its alternative search terms with one bounded research retry.
        if self._is_llm_rejection(result):
            retry_query = str(getattr(result, "retry_query", "") or "").strip()
            if retry_query and retry_query.lower() != query.lower():
                logger.info(
                    "🔎 LLM rejected candidate set for '%s'; retrying selector search with '%s'",
                    query[:80],
                    retry_query[:80],
                )
                retry_candidates, retry_scores = await asyncio.to_thread(
                    self._get_candidates_with_scores, retry_query, provider
                )
                retry_candidates, retry_scores = _drop_excluded(retry_candidates, retry_scores)
                retry_has_country_match = False
                if retry_candidates and country:
                    retry_candidates, retry_scores, retry_conflict, retry_has_country_match = self._apply_country_constraint(
                        country, retry_candidates, retry_scores
                    )
                    if retry_conflict:
                        return SelectionResult(code=None, source="country_mismatch")
                if retry_candidates:
                    retry_ambiguous = self._scores_are_ambiguous(retry_scores, fusion_mode)
                    retry_result = await self._llm_pick(
                        retry_query,
                        retry_candidates[:20],
                        provider,
                        prefer_ask=retry_ambiguous,
                        country=country if retry_has_country_match else None,
                        **_region_kw,
                    )
                    if retry_result and (retry_result.code or retry_result.needs_user_choice):
                        return self._mark_prefer_ask(
                            await self._retry_if_metadata_conflict(
                                metadata_constraint_query,
                                retry_result,
                                retry_candidates[:20],
                                provider,
                                **_region_kw,
                            ),
                            retry_ambiguous,
                        )
                    if self._is_llm_rejection(retry_result):
                        return retry_result
            return result

        # Step 3: If LLM couldn't decide, try with fewer/different candidates
        if not result or (not result.code and not result.needs_user_choice):
            # Retry with top 5 only (simpler for LLM)
            result = await self._llm_pick(query, candidates[:5], provider, country=llm_country, **_constraint_kw, **_continuity_kw, **_region_kw)
            result = await self._retry_if_metadata_conflict(metadata_constraint_query, result, candidates[:5], provider, **_region_kw)

        if self._is_llm_rejection(result):
            return result

        if result and (result.code or result.needs_user_choice):
            # A confident pick is authoritative only if the retrieval that fed it
            # was NOT ambiguous (see _mark_prefer_ask / SelectionResult.authoritative).
            self._mark_prefer_ask(result, candidates_are_ambiguous)
            self._emit_telemetry(
                telemetry_enabled,
                fusion_mode,
                query,
                provider,
                candidates,
                result,
            )
            return result

        logger.info(
            "🔵 IndicatorSelector made no final selection for '%s'; refusing top-candidate fallback",
            query[:80],
        )
        final = SelectionResult(code=None, source="no_decision")
        self._emit_telemetry(telemetry_enabled, fusion_mode, query, provider, candidates, final)
        return final

    @staticmethod
    def _emit_telemetry(
        enabled: bool,
        fusion_mode: str,
        query: str,
        provider: str,
        fused_candidates: List[tuple[str, str]],
        result: SelectionResult,
    ) -> None:
        """Phase 2.1 baseline telemetry — structured per-query record.

        Logs the fused candidate codes + the LLM's final pick + the
        selection source. Used as a baseline to compare RRF vs legacy
        fusion during the Phase 2.2 shadow window before any default flip.
        Gated by INDICATOR_TELEMETRY_ENABLED so dev/test traffic doesn't
        flood logs.
        """
        if not enabled:
            return
        import json as _json
        try:
            top_fused = [code for code, _name in (fused_candidates or [])[:10]]
            payload = {
                "fusion": fusion_mode,
                "provider": str(provider or ""),
                "query": str(query or "")[:200],
                "fused_top10": top_fused,
                "final_code": getattr(result, "code", None),
                "final_source": getattr(result, "source", None),
                "needs_user_choice": bool(getattr(result, "needs_user_choice", False)),
            }
            logger.info("indicator_selector_telemetry %s", _json.dumps(payload, ensure_ascii=False))
        except Exception as exc:  # never raise from telemetry path
            logger.debug("telemetry emit failed: %s", exc)

    @staticmethod
    def _apply_country_constraint(
        country: str,
        candidates: List[tuple],
        scores: List[float],
    ) -> tuple[List[tuple], List[float], bool, bool]:
        """Drop candidates whose series is explicitly about a DIFFERENT country
        than requested, and rank country-matching candidates ahead of neutral
        (country-agnostic) ones. Data-driven via :func:`_derive_candidate_country`
        — no hardcoded indicator→code mappings.

        Returns ``(candidates, scores, all_conflict, has_match)``.
        ``all_conflict`` is True only when EVERY candidate names a different
        country (nothing matching and nothing neutral) — the caller should then
        refuse rather than return a wrong-country series. ``has_match`` is True
        when at least one kept candidate's series is specifically about the
        requested country, so the caller can tell the LLM picker to prefer it
        over a neutral series. A no-op when ``country`` doesn't resolve or no
        candidate carries a derivable country (e.g. crypto/FX/index queries).
        """
        target = CountryResolver.normalize(country) if country else None
        if not target:
            return candidates, scores, False, False

        matching: List[tuple] = []
        matching_scores: List[float] = []
        neutral: List[tuple] = []
        neutral_scores: List[float] = []
        conflicting = 0
        for cand, score in zip(candidates, scores):
            cand_country = _derive_candidate_country(cand[0], cand[1] if len(cand) > 1 else "")
            if cand_country is None:
                neutral.append(cand)
                neutral_scores.append(score)
            elif cand_country == target:
                matching.append(cand)
                matching_scores.append(score)
            else:
                conflicting += 1  # explicitly a different country → drop

        kept = matching + neutral
        kept_scores = matching_scores + neutral_scores
        if not kept:
            # Every candidate is tagged for some other country and none matches.
            return candidates, scores, True, False
        if conflicting:
            logger.info(
                "🌍 Country constraint (%s): kept %d matching + %d neutral, dropped %d cross-country candidate(s)",
                target, len(matching), len(neutral), conflicting,
            )
        return kept, kept_scores, False, bool(matching)

    @staticmethod
    def _scores_are_ambiguous(scores: List[float], fusion_mode: str = "legacy") -> bool:
        if len(scores) >= 3 and all(score > 0 for score in scores[:3]):
            first, second, third = scores[:3]
            # FTS5 candidates and embedding candidates are merged by evidence
            # source, so score order is not guaranteed. Only use the score-gap
            # ambiguity signal when the first three scores are actually ordered.
            if first >= second >= third:
                if fusion_mode == "rrf":
                    # RRF scores are reciprocal-rank sums (Σ 1/(k+rank), max
                    # ~2/61 ≈ 0.033), so the absolute 0.03 cosine threshold is
                    # satisfied on almost every query — the ambiguity signal
                    # degenerates to constant-true and biases the LLM to ASK
                    # everywhere. Use a SCALE-INVARIANT relative gap instead:
                    # ambiguous only when the 1st and 3rd candidates are within
                    # 5% of the top score of each other (a genuine near-tie).
                    return (first - third) < first * 0.05
                # legacy/cosine merge: keep the original absolute threshold
                # (this path is the rollback-only strategy; behaviour unchanged).
                return (first - third) < 0.03
        return False

    @staticmethod
    def _is_llm_rejection(result: Optional["SelectionResult"]) -> bool:
        return bool(result and getattr(result, "source", "") == "llm_reject")

    @staticmethod
    def _mark_prefer_ask(
        result: Optional["SelectionResult"], ambiguous: bool
    ) -> Optional["SelectionResult"]:
        """Record whether a PICK was produced from an AMBIGUOUS candidate field.

        The confidence signal the request-level authority contract reads
        (SelectionResult.authoritative). Only ever set on a genuine llm_pick; a
        pick made while retrieval could not separate the candidates (prefer_ask
        engaged) is deliberately denied authority — the contract trusts only a
        pick from a clearly-separated field.
        """
        if result is not None and getattr(result, "code", None) and getattr(result, "source", "") == "llm_pick":
            result.made_under_prefer_ask = bool(ambiguous)
        return result

    def _get_candidates_with_scores(
        self, query: str, provider: str, top_k: int = 50,
        english_query: Optional[str] = None,
        constraint_query: Optional[str] = None,
    ) -> tuple[List[tuple[str, str]], List[float]]:
        """Step 1: Find nearest indicators using hybrid FTS5 + embedding retrieval.

        Cycle 29 fix: Audit found that embedding-only retrieval has only ~60%
        accuracy on FRED variants because:
        - Embeddings use only `name` field (no aliases or descriptions)
        - Verbose BLS/Census titles ("Sticky Price CPI less Food and Energy")
          swamp canonical series ("Core CPI" / CPILFESL)
        - FTS5 nails lexical/acronym matches that embeddings miss

        Solution: merge BOTH retrieval methods. FTS5 gets the canonical
        codes that match query vocabulary; embeddings get semantic
        paraphrases.  The union (deduped) is passed to the LLM for selection.

        Semantic matching comes from retrieval plus LLM adjudication, not forced
        provider-code shortcuts.
        """

        # 1. Run BOTH retrievals in parallel-ish (sequential but cheap)
        # Normalize runtime aliases to the provider names stored in the 330K
        # catalog/embedding metadata.  Without this, canonical runtime names such
        # as STATSCAN miss the StatsCan embedding partition and silently degrade
        # to lexical-only retrieval.
        retrieval_provider = self._catalog_provider_name(provider)

        def _retrieve(text: str) -> tuple[List[tuple[str, str]], List[float], List[tuple[str, str]]]:
            """Run the embedding + FTS5 arms for one query string."""
            emb_c: List[tuple[str, str]] = []
            emb_s: List[float] = []
            try:
                from .embedding_retrieval import get_embedding_retrieval
                er = get_embedding_retrieval()
                results = er.search(text, provider=retrieval_provider, top_k=top_k)
                if results:
                    emb_c = [(r["code"], r["name"]) for r in results]
                    emb_s = [r.get("score", 0.0) for r in results]
            except Exception as e:
                logger.warning("Embedding retrieval failed: %s", e)
            fts_c: List[tuple[str, str]] = []
            try:
                fts_c = self._get_candidates_fts5(text, retrieval_provider, top_k=20)
            except Exception as e:
                logger.debug("FTS5 retrieval failed: %s", e)
            return emb_c, emb_s, fts_c

        embedding_candidates, embedding_scores, fts5_candidates = _retrieve(query)

        # English canonical retrieval arm (Proposal A). When the query was
        # written in another language, the parse LLM also produced an English
        # canonical metric name; retrieving on it too and fusing the extra
        # ranked lists into the SAME fusion gives non-English queries lexical
        # recall against the English catalog. Skipped (strict no-op) when the
        # english terms are empty or case-insensitively identical to the primary
        # query, so English queries keep their exact current arms and ranking.
        english_text = str(english_query or "").strip()
        use_english_arm = bool(english_text) and english_text.lower() != str(query or "").strip().lower()
        if use_english_arm:
            eng_embedding_candidates, eng_embedding_scores, eng_fts5_candidates = _retrieve(english_text)
        else:
            eng_embedding_candidates, eng_embedding_scores, eng_fts5_candidates = [], [], []

        # 2. Merge with score-aware hybrid ordering.  FTS5 is excellent recall
        # evidence for lexical/provider-title surfaces, but it must not occupy the
        # whole front of the LLM prompt ahead of much stronger embedding matches.
        # Keep both evidence sources, dedupe by provider code, and let the final
        # LLM selector adjudicate semantics.
        if embedding_candidates or fts5_candidates or eng_embedding_candidates or eng_fts5_candidates:
            merged_by_code: Dict[str, Dict[str, Any]] = {}

            def _ensure(code: Any, name: Any) -> Dict[str, Any]:
                return merged_by_code.setdefault(
                    str(code or "").strip(),
                    {
                        "candidate": (code, name),
                        "embedding_score": None,
                        "embedding_rank": None,
                        "fts_rank": None,
                        # English-arm ranks (Proposal A); always None when the
                        # english arm is off, so fusion reduces to the primary.
                        "english_embedding_score": None,
                        "english_embedding_rank": None,
                        "english_fts_rank": None,
                    },
                )

            for rank, (code, name) in enumerate(fts5_candidates[:20]):
                if not str(code or "").strip():
                    continue
                entry = _ensure(code, name)
                if entry["fts_rank"] is None:
                    entry["fts_rank"] = rank

            for rank, (code, name) in enumerate(embedding_candidates):
                if not str(code or "").strip():
                    continue
                score = embedding_scores[rank] if rank < len(embedding_scores) else 0.0
                entry = _ensure(code, name)
                # Prefer the embedding-sourced display name when this source has
                # stronger numeric evidence; FTS-only candidates still remain as
                # recall candidates below embedding-backed matches.
                if entry["embedding_score"] is None or score > float(entry["embedding_score"]):
                    entry["candidate"] = (code, name)
                    entry["embedding_score"] = score
                    entry["embedding_rank"] = rank

            # English-arm ranked lists fold into the same merged_by_code. They
            # only ever contribute rank fields (never overwrite a primary-arm
            # display name); a candidate the primary query missed keeps the name
            # from whichever english source first inserted it.
            for rank, (code, name) in enumerate(eng_fts5_candidates[:20]):
                if not str(code or "").strip():
                    continue
                entry = _ensure(code, name)
                if entry["english_fts_rank"] is None:
                    entry["english_fts_rank"] = rank

            for rank, (code, name) in enumerate(eng_embedding_candidates):
                if not str(code or "").strip():
                    continue
                score = eng_embedding_scores[rank] if rank < len(eng_embedding_scores) else 0.0
                entry = _ensure(code, name)
                if entry["english_embedding_score"] is None or score > float(entry["english_embedding_score"]):
                    entry["english_embedding_score"] = score
                    entry["english_embedding_rank"] = rank

            # Two fusion strategies behind INDICATOR_FUSION:
            # - "legacy" (default): the score-aware merge with magic constants
            #   (0.02, 0.55, 0.10, 0.005). Kept as the rollback path during the
            #   shadow-mode validation period for Phase 2.2.
            # - "rrf": canonical parameterless Reciprocal Rank Fusion,
            #   score(c) = Σ 1/(k + rank_i(c)). One constant (k), one citation
            #   (Cormack et al., SIGIR 2009), encoder-independent. Replaces
            #   the magic constants entirely. Default off until shadow shows
            #   parity per docs/DEEP_REVIEW_2026-05-30.md §6 invariant #8.
            rrf_k = max(1, int(getattr(self._settings, "indicator_rrf_k", 60) or 60))
            fusion_mode = str(getattr(self._settings, "indicator_fusion", "legacy") or "legacy").lower()

            def _effective_rank(item: tuple[str, Dict[str, Any]]) -> tuple[float, int, int, str]:
                code, entry = item
                embedding_score = entry["embedding_score"]
                fts_rank = entry["fts_rank"]
                embedding_rank = entry["embedding_rank"]
                eng_embedding_score = entry["english_embedding_score"]
                eng_fts_rank = entry["english_fts_rank"]
                eng_embedding_rank = entry["english_embedding_rank"]
                if embedding_score is not None:
                    lexical_boost = 0.02 / (int(fts_rank) + 1) if fts_rank is not None else 0.0
                    # English arm adds an extra lexical boost only when present;
                    # all english_* fields are None when the arm is off, so this
                    # reduces to the exact legacy formula for English queries.
                    if eng_fts_rank is not None:
                        lexical_boost += 0.02 / (int(eng_fts_rank) + 1)
                    effective_score = float(embedding_score) + lexical_boost
                elif eng_embedding_score is not None:
                    # English-arm embedding evidence for a candidate the primary
                    # query missed — treat it like primary embedding evidence so
                    # genuine recall survives the [:top_k] cut.
                    lexical_boost = 0.02 / (int(eng_fts_rank) + 1) if eng_fts_rank is not None else 0.0
                    effective_score = float(eng_embedding_score) + lexical_boost
                else:
                    # FTS-only rows (primary or english) are useful recall
                    # candidates, but their synthetic score must stay below real
                    # embedding evidence.
                    best_fts_rank = min(
                        [r for r in (fts_rank, eng_fts_rank) if r is not None],
                        default=0,
                    )
                    effective_score = 0.55 - min(0.10, 0.005 * int(best_fts_rank))
                tie_embedding_rank = (
                    int(embedding_rank) if embedding_rank is not None
                    else int(eng_embedding_rank) if eng_embedding_rank is not None
                    else top_k + int(fts_rank if fts_rank is not None else (eng_fts_rank or 0))
                )
                tie_fts_rank = (
                    int(fts_rank) if fts_rank is not None
                    else int(eng_fts_rank) if eng_fts_rank is not None
                    else top_k
                )
                return (-effective_score, tie_embedding_rank, tie_fts_rank, code)

            def _rrf_rank(item: tuple[str, Dict[str, Any]]) -> tuple[float, str]:
                """Reciprocal Rank Fusion: score(c) = Σ 1/(k + rank_i(c)).

                The english canonical arm (Proposal A) contributes two more
                ranked lists to the same sum; its ranks are None (no
                contribution) whenever the arm is off, so English queries score
                identically to before.
                """
                _code, entry = item
                score = 0.0
                for rank_field in (
                    "fts_rank",
                    "embedding_rank",
                    "english_fts_rank",
                    "english_embedding_rank",
                ):
                    rank_value = entry[rank_field]
                    if rank_value is not None:
                        score += 1.0 / (rrf_k + int(rank_value) + 1)
                return (-score, _code)

            if fusion_mode == "rrf":
                ranked_entries = sorted(merged_by_code.items(), key=_rrf_rank)[:top_k]
                merged_candidates = [entry["candidate"] for _code, entry in ranked_entries]
                merged_scores = [-_rrf_rank((code, entry))[0] for code, entry in ranked_entries]
            else:
                ranked_entries = sorted(merged_by_code.items(), key=_effective_rank)[:top_k]
                merged_candidates = [entry["candidate"] for _code, entry in ranked_entries]
                merged_scores = [
                    -_effective_rank((code, entry))[0]
                    for code, entry in ranked_entries
                ]

            prioritized_candidates, prioritized_scores = self._prioritize_candidates_by_provider_surface(
                merged_candidates,
                merged_scores,
                retrieval_provider,
            )
            return self._prioritize_candidates_by_query_metadata(
                # Structural qualifiers (frequency/unit/price-basis) live in
                # the USER's fuller text; the stripped retrieval query has
                # none, so this purpose-built prioritizer never fired on
                # follow-up/colloquial paths ("India CPI by month" adjudicated
                # over an order with the monthly series buried).
                constraint_query or query,
                prioritized_candidates,
                prioritized_scores,
                retrieval_provider,
            )

        return [], []

    @staticmethod
    def _catalog_provider_name(provider: str) -> str:
        """Return the provider spelling used by indicators.db/embeddings."""
        canonical = normalize_provider_name(provider or "")
        return {
            "STATSCAN": "StatsCan",
        }.get(canonical, provider)

    def _prioritize_candidates_by_query_metadata(
        self,
        query: str,
        candidates: List[tuple[str, str]],
        scores: List[float],
        provider: str,
    ) -> tuple[List[tuple[str, str]], List[float]]:
        """Order candidates by explicit metadata constraints in the query.

        This is not a semantic shortcut: it never creates candidates or maps a
        concept to a code.  It only moves already-retrieved provider catalog
        candidates whose metadata satisfies explicit user constraints
        (frequency, unit, or price-basis qualifier) ahead of near-neighbors
        that contradict those constraints.
        """

        if not candidates:
            return candidates, scores

        requested_frequencies = _extract_requested_frequencies(query)
        requested_unit_tokens = _extract_requested_unit_tokens(query)
        requested_measurements = _extract_requested_measurement_qualifiers(query)
        if not (requested_frequencies or requested_unit_tokens or requested_measurements):
            return candidates, scores

        enriched = self._enrich_candidates(candidates, provider)
        has_frequency_match = requested_frequencies and any(
            _frequency_matches(requested_frequencies, str(item.get("frequency") or ""))
            for item in enriched
        )
        has_unit_match = requested_unit_tokens and any(
            _unit_matches(requested_unit_tokens, str(item.get("unit") or ""))
            for item in enriched
        )
        has_measurement_match = requested_measurements and any(
            _measurement_matches(requested_measurements, self._candidate_metadata_text(item))
            for item in enriched
        )
        if not (has_frequency_match or has_unit_match or has_measurement_match):
            return candidates, scores

        paired = list(zip(range(len(candidates)), candidates, scores, enriched))

        def _rank(item: tuple[int, tuple[str, str], float, Dict[str, Any]]) -> tuple[int, int, int, int]:
            index, _candidate, _score, meta = item
            frequency_penalty = (
                0
                if not has_frequency_match
                or _frequency_matches(requested_frequencies, str(meta.get("frequency") or ""))
                else 1
            )
            unit_penalty = (
                0
                if not has_unit_match
                or _unit_matches(requested_unit_tokens, str(meta.get("unit") or ""))
                else 1
            )
            measurement_penalty = (
                0
                if not has_measurement_match
                or _measurement_matches(requested_measurements, self._candidate_metadata_text(meta))
                else 1
            )
            return frequency_penalty, unit_penalty, measurement_penalty, index

        paired.sort(key=_rank)
        return [candidate for _idx, candidate, _score, _meta in paired], [
            score for _idx, _candidate, score, _meta in paired
        ]

    async def _retry_if_metadata_conflict(
        self,
        query: str,
        result: Optional["SelectionResult"],
        candidates: List[tuple[str, str]],
        provider: str,
        region: Optional[str] = None,
        region_coverage_probe: Optional[
            Callable[[List[str]], Awaitable[Dict[str, Optional[bool]]]]
        ] = None,
    ) -> Optional["SelectionResult"]:
        """Retry LLM selection if a PICK contradicts explicit metadata constraints."""

        if not result or not result.code or not candidates:
            return result

        compatible = await asyncio.to_thread(
            self._metadata_compatible_subset, query, candidates, provider
        )
        if not compatible:
            return result

        normalized_selected = self._normalize_code(str(result.code or ""), provider)
        compatible_codes = {
            self._normalize_code(str(code or ""), provider)
            for code, _name in compatible
        }
        if normalized_selected in compatible_codes:
            return result

        _region_kw: Dict[str, Any] = {}
        if str(region or "").strip():
            _region_kw["region"] = str(region).strip()
            if region_coverage_probe is not None:
                _region_kw["region_coverage_probe"] = region_coverage_probe
        retry_result = await self._llm_pick(query, compatible[:20], provider, prefer_ask=False, **_region_kw)
        if retry_result and (retry_result.code or retry_result.needs_user_choice):
            return retry_result

        return SelectionResult(
            code=None,
            source="metadata_conflict",
            rejection_reason="LLM pick contradicted explicit frequency/unit/measurement metadata.",
        )

    def _metadata_compatible_subset(
        self,
        query: str,
        candidates: List[tuple[str, str]],
        provider: str,
    ) -> List[tuple[str, str]]:
        requested_frequencies = _extract_requested_frequencies(query)
        requested_unit_tokens = _extract_requested_unit_tokens(query)
        requested_measurements = _extract_requested_measurement_qualifiers(query)
        if not (requested_frequencies or requested_unit_tokens or requested_measurements):
            return []

        enriched = self._enrich_candidates(candidates, provider)
        paired = list(zip(candidates, enriched))
        filtered = paired
        if requested_frequencies and any(
            _frequency_matches(requested_frequencies, str(meta.get("frequency") or ""))
            for _candidate, meta in paired
        ):
            filtered = [
                (candidate, meta)
                for candidate, meta in filtered
                if _frequency_matches(requested_frequencies, str(meta.get("frequency") or ""))
            ]
        if requested_unit_tokens and any(
            _unit_matches(requested_unit_tokens, str(meta.get("unit") or ""))
            for _candidate, meta in paired
        ):
            filtered = [
                (candidate, meta)
                for candidate, meta in filtered
                if _unit_matches(requested_unit_tokens, str(meta.get("unit") or ""))
            ]
        if requested_measurements and any(
            _measurement_matches(requested_measurements, self._candidate_metadata_text(meta))
            for _candidate, meta in paired
        ):
            filtered = [
                (candidate, meta)
                for candidate, meta in filtered
                if _measurement_matches(requested_measurements, self._candidate_metadata_text(meta))
            ]

        if len(filtered) >= len(paired):
            return []
        return [candidate for candidate, _meta in filtered]

    @staticmethod
    def _candidate_metadata_text(item: Dict[str, Any]) -> str:
        return " ".join(
            str(item.get(key) or "")
            for key in ("name", "description", "keywords", "category", "unit")
        )

    def _prioritize_candidates_by_provider_surface(
        self,
        candidates: List[tuple[str, str]],
        scores: List[float],
        provider: str,
    ) -> tuple[List[tuple[str, str]], List[float]]:
        """Prefer candidates from public executable provider surfaces.

        This is candidate ordering only; the LLM still adjudicates the final
        indicator.  For IMF, the local catalog includes legacy/auxiliary rows
        whose short codes can look like good natural-language answers but are
        not the public DataMapper WEO/regional surfaces used by the runtime.
        Exact user-supplied codes still pass through the provider separately.
        """
        if normalize_provider_name(provider or "") != "IMF" or not candidates:
            return candidates, scores

        try:
            from .indicator_database import get_indicator_lookup

            lookup = get_indicator_lookup()
        except Exception as exc:
            logger.debug("IMF candidate prioritization skipped: %s", exc)
            return candidates, scores

        def _rank(item: tuple[int, tuple[str, str], float]) -> tuple[int, int]:
            index, (code, _name), _score = item
            try:
                metadata = lookup.get("IMF", code)
            except Exception:
                metadata = None
            category = str((metadata or {}).get("category") or "").strip().upper()
            code_text = str(code or "").strip().upper()
            has_namespace = (
                "_" in code_text
                or "." in code_text
                or any(ch.isdigit() for ch in code_text)
            )
            if category == "WEO":
                surface_rank = 0
            elif category.endswith("REO"):
                surface_rank = 1
            elif has_namespace:
                surface_rank = 2
            else:
                surface_rank = 3
            return surface_rank, index

        paired = list(zip(range(len(candidates)), candidates, scores))
        paired.sort(key=_rank)
        return [candidate for _idx, candidate, _score in paired], [
            score for _idx, _candidate, score in paired
        ]

    def _get_candidates_fts5(
        self, query: str, provider: str, top_k: int = 20,
    ) -> List[tuple[str, str]]:
        """FTS5 fallback when embeddings unavailable.

        Opens its OWN sqlite connection per call (not the shared singleton), so
        it is safe to run from the thread the selector now offloads retrieval to.
        The connection is always closed in `finally` to avoid leaking one per
        query under concurrent load.
        """
        conn = None
        try:
            from .indicator_database import IndicatorDatabase
            db = IndicatorDatabase()
            conn = db._get_connection()
            cur = conn.cursor()

            safe_query = query
            for char in ['"', "'", '(', ')', '*', '-', ':', '^', ',']:
                safe_query = safe_query.replace(char, ' ')
            # Keep 2-char tokens: monetary aggregates (M1/M2/M3), country
            # codes (US/EU), and tenors (2y/5y) are short AND load-bearing.
            words = [w.strip() for w in safe_query.split() if len(w.strip()) >= 2]
            if not words:
                return []

            def _run(fts_query: str) -> list:
                # Popularity joins the ORDER at the CUT itself: synonym
                # enrichment of mid-popularity rows made many series share the
                # same user vocabulary ("federal nonfarm payrolls" et al.), and
                # bm25 alone then dropped the FLAGSHIPS out of the top-k before
                # any downstream boost could act (live: PAYNSA/PAYEMS vanished
                # from 'nonfarm payrolls' retrieval after a 922-row enrichment
                # pass). bm25 is smaller-is-better; popularity (0-100 catalog
                # data) subtracts at a scale that reorders near-ties without
                # letting popularity beat clearly-better text matches.
                cur.execute(
                    """SELECT i.code, i.name FROM indicators_fts f
                    JOIN indicators i ON f.rowid = i.id
                    WHERE indicators_fts MATCH ? AND i.provider = ?
                    ORDER BY bm25(indicators_fts, 0, 3.0, 10.0, 1.0, 3.0, 2.0, 2.0)
                          - (COALESCE(i.popularity, 0) * 0.2)
                    LIMIT ?""",
                    (fts_query, provider, top_k),
                )
                return cur.fetchall()

            # Precision first: rows matching ALL query words (name/keywords/
            # synonyms) are near-certain lexical hits and must not be diluted
            # by the thousands of single-word matches an OR query returns.
            # Fall back to OR for recall when the AND set is too small.
            rows: list = []
            if len(words) > 1:
                rows = _run(" AND ".join(f'"{w}"*' for w in words))
            if len(rows) < top_k:
                seen = {r[0] for r in rows}
                rows += [
                    r for r in _run(" OR ".join(f'"{w}"*' for w in words))
                    if r[0] not in seen
                ][: top_k - len(rows)]
            return rows
        except Exception as e:
            logger.warning("FTS5 fallback failed: %s", e)
            return []
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    def _enrich_candidates(
        self,
        candidates: List[tuple[str, str]],
        provider: str,
    ) -> List[Dict[str, Any]]:
        """Enrich candidates with metadata from indicators.db.

        Returns dicts with frequency/unit/activity plus compact evidence fields.
        This gives the LLM visibility into whether a series is active or obsolete.
        """
        enriched = []
        meta_map: Dict[str, Dict[str, Any]] = {}
        conn = None
        try:
            conn = sqlite3.connect(str(_INDICATORS_DB))
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()

            codes = [c[0] for c in candidates]
            # Build a lookup map: (provider, code) -> metadata row
            placeholders = ",".join(["?"] * len(codes))
            cur.execute(
                f"SELECT code, frequency, unit, end_date, category, description, keywords, popularity FROM indicators "
                f"WHERE provider = ? AND code IN ({placeholders})",
                [self._catalog_provider_name(provider)] + codes,
            )
            for row in cur.fetchall():
                meta_map[row["code"]] = {
                    "frequency": row["frequency"] or "",
                    "unit": row["unit"] or "",
                    "end_date": row["end_date"] or "",
                    "category": row["category"] or "",
                    "description": row["description"] or "",
                    "keywords": row["keywords"] or "",
                    "popularity": row["popularity"] or 0,
                }
        except Exception as e:
            logger.warning("Failed to enrich candidates from DB: %s", e)
            meta_map = {}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        for code, name in candidates:
            meta = meta_map.get(code, {})
            end_date = meta.get("end_date", "")
            # Mark as discontinued if last observation is older than 5 years.
            # Use a sliding 5-year window relative to today rather than a
            # hardcoded year, so 2020-2025 series aren't incorrectly flagged
            # as dead in 2026+.
            discontinued = False
            if end_date:
                try:
                    from datetime import datetime as _dt
                    year = int(end_date[:4])
                    if year < (_dt.now().year - 5):
                        discontinued = True
                except (ValueError, IndexError):
                    pass

            enriched.append({
                "code": code,
                "name": name,
                "frequency": meta.get("frequency", ""),
                "unit": meta.get("unit", ""),
                "end_date": end_date,
                "category": meta.get("category", ""),
                "description": meta.get("description", ""),
                "keywords": meta.get("keywords", ""),
                "popularity": meta.get("popularity", 0),
                "discontinued": discontinued,
            })

        return enriched

    async def _llm_pick(
        self,
        query: str,
        candidates: List[tuple[str, str]],
        provider: str,
        prefer_ask: bool = False,
        country: Optional[str] = None,
        region: Optional[str] = None,
        region_coverage_probe: Optional[
            Callable[[List[str]], Awaitable[Dict[str, Optional[bool]]]]
        ] = None,
        constraint_query: Optional[str] = None,
        continuity_series: Optional[str] = None,
    ) -> Optional[SelectionResult]:
        """Step 2: LLM picks the best indicator from candidates.

        ``constraint_query`` is the FULLER user text (metadata_query) when the
        resolution layer stripped structural qualifiers like frequency words
        out of ``query`` — frequency extraction must see the user's wording.

        ``country`` is supplied only when the candidate set contains a series
        specifically about that country (see ``_apply_country_constraint``);
        surfacing it steers the LLM toward the country-matching series instead
        of a generic / different-country series that text similarity would win.

        ``region`` (a sub-national region the user named) plus an optional
        ``region_coverage_probe`` make the adjudicator region-coverage-aware.
        When a probe is supplied (providers whose regional data lives as a
        dimension MEMBER, e.g. StatsCan cube Geography), each candidate is
        annotated with whether its geography actually contains ``region`` — this
        is what lets the adjudicator keep a nationally-TITLED cube that in fact
        contains the province, and reject a cube that does not. Without a probe
        (providers whose regional data lives in a region-TITLED series, e.g.
        FRED "…in Texas"), the region is stated as a preference so the
        region-specific series wins over the national one.
        """
        # Enrich candidates with metadata so the LLM can see frequency,
        # unit, and whether a series is discontinued/obsolete. The sqlite
        # lookup is blocking — keep it off the event loop.
        enriched = await asyncio.to_thread(self._enrich_candidates, candidates, provider)

        region_label = str(region or "").strip()
        # Ground-truth region coverage per candidate (dimension-member providers
        # only): {code: True/False/None}. None = coverage unknown (metadata not
        # cached); the post-pick guard is the live backstop for those.
        coverage: Dict[str, Optional[bool]] = {}
        if region_label and region_coverage_probe is not None:
            try:
                coverage = await region_coverage_probe(
                    [str(item["code"]) for item in enriched]
                ) or {}
            except Exception as exc:  # never let annotation break selection
                logger.debug("Region-coverage probe failed (%s); proceeding unannotated", exc)
                coverage = {}

        _requested_freqs_for_marks = _extract_requested_frequencies(constraint_query or query)
        # OFFICIAL-HEADLINE marker: catalog popularity is the demand signal
        # for which series a plain-concept ask means. Prose rules ("prefer the
        # standard version") lose to title similarity on noisy enriched sets
        # (ADP/federal payroll rotations); per-candidate markers are what the
        # adjudicator reliably follows (the [covers X] pattern). Data-driven:
        # argmax popularity in THIS candidate list, ties included, no lists.
        try:
            _max_pop = max(int(item.get("popularity") or 0) for item in enriched)
        except ValueError:
            _max_pop = 0
        option_lines = []
        for i, item in enumerate(enriched):
            parts = [f"{i + 1}. [{item['code']}] {item['name']}"]
            meta_parts = []
            if item["frequency"]:
                meta_parts.append(item["frequency"])
                # Explicit match markers (the [covers X] pattern): the
                # adjudicator follows per-candidate annotations far more
                # reliably than prose rules — the FREQUENCY REQUIREMENT alone
                # still lost to title similarity (India monthly vs the annual
                # WB-mirror).
                if _requested_freqs_for_marks:
                    if _frequency_matches(_requested_freqs_for_marks, str(item["frequency"])):
                        meta_parts.append("MATCHES requested frequency")
                    else:
                        meta_parts.append("does NOT match requested frequency")
            if item["unit"]:
                meta_parts.append(item["unit"])
            if item.get("category"):
                meta_parts.append(f"category: {item['category']}")
            evidence_text = " ".join(
                str(item.get(key) or "").strip()
                for key in ("keywords", "description")
            ).strip()
            if evidence_text:
                evidence_text = re.sub(r"\s+", " ", evidence_text)[:180]
                meta_parts.append(f"evidence: {evidence_text}")
            if item["end_date"]:
                meta_parts.append(f"last data: {item['end_date'][:10]}")
            if _max_pop >= 40 and int(item.get("popularity") or 0) == _max_pop:
                meta_parts.append("MOST-USED series for this concept")
            if item["discontinued"]:
                meta_parts.append("DISCONTINUED")
            if meta_parts:
                parts.append(f"  ({', '.join(meta_parts)})")
            if coverage:
                covered = coverage.get(str(item["code"]))
                if covered is True:
                    parts.append(f"  [covers {region_label}]")
                elif covered is False:
                    parts.append(f"  [does NOT cover {region_label}]")
                else:
                    parts.append(f"  [{region_label} coverage unknown]")
            option_lines.append("".join(parts))

        options = "\n".join(option_lines)

        prompt = LLM_SELECTION_PROMPT.format(
            query=query, provider=provider, options=options,
        )

        # Country steering: when a country-specific series exists among the
        # candidates, name the target country so the LLM prefers it over a
        # generic / US-domestic series that title similarity alone would pick
        # (e.g. "GDP per capita" for Canada must resolve to the "for Canada"
        # series, not the generic US BEA series). Gated upstream on an actual
        # country-matching candidate, so country-agnostic concepts are unaffected.
        country_label = str(country or "").strip()
        if country_label:
            country_iso2 = CountryResolver.normalize(country_label) or ""
            country_hint = country_label
            if country_iso2 and country_iso2.upper() != country_label.upper():
                country_hint = f"{country_label} ({country_iso2})"
            prompt += (
                f"\n\nThe user is asking specifically about this country: {country_hint}. "
                "When a candidate's series is specifically about that country, "
                "STRONGLY prefer it over a generic, global, or different-country "
                "series — even if the generic series has a shorter or more standard "
                "code. Do not pick a series that names a DIFFERENT country."
            )

        # Region steering. Two shapes, chosen by whether coverage annotations
        # were produced (i.e. whether the caller supplied a probe):
        #  - annotated (dimension-member providers): the [covers …] markers are
        #    ground truth about the cube's geography dimension, so instruct the
        #    adjudicator to obey them and NOT to reject a cube because its title
        #    is national — the marker, not the title, decides coverage.
        #  - unannotated (region-titled-series providers): a plain preference for
        #    the region-specific series over the national one.
        if region_label and coverage:
            prompt += (
                f"\n\nREGION COVERAGE REQUIREMENT: The user needs data specifically for "
                f"{region_label}. Each candidate is annotated with whether its geography "
                f"actually includes {region_label}. You MUST pick a candidate marked "
                f"\"[covers {region_label}]\". NEVER pick one marked "
                f"\"[does NOT cover {region_label}]\". If none is marked "
                f"\"[covers {region_label}]\", prefer one marked "
                f"\"[{region_label} coverage unknown]\" over one marked "
                f"\"[does NOT cover {region_label}]\". The annotation is ground truth "
                f"about the candidate's geography dimension — a candidate can cover "
                f"{region_label} even when its title names only the country, so do not "
                f"reject it on the title alone."
            )
        elif region_label:
            prompt += (
                f"\n\nThe user is asking specifically about the sub-national region "
                f"{region_label}. STRONGLY prefer a candidate whose series is specifically "
                f"about {region_label} (its title or coverage names {region_label}) over a "
                f"national or aggregate series. Do not pick a series for a DIFFERENT region."
            )

        # Continuity steering: a follow-up changed ONE aspect (frequency /
        # country / time) of an already-served series. Without this the
        # adjudicator near-ties variants of the same concept and flips or asks.
        if str(continuity_series or "").strip():
            prompt += (
                f"\n\nCONTINUITY: the previous turn served {continuity_series}. "
                f"This follow-up changes ONE aspect (frequency, country, or time "
                f"range). Pick the candidate that is the SAME measure with that "
                f"one change — keep real-vs-nominal, seasonal adjustment, and "
                f"index-vs-level identical to the previous series. Do not switch "
                f"variants; do not ASK."
            )

        # Frequency steering. The user's query names a reporting frequency
        # ("monthly", "quarterly", "last N months" implies sub-annual…) —
        # extracted STRUCTURALLY by _extract_requested_frequencies; candidates
        # already display their catalog frequency in the option line. Without
        # this instruction the adjudicator picks the best TITLE match even at
        # the wrong frequency (observed live: annual "Inflation, consumer
        # prices for China" chosen over the monthly CPI series for a
        # "monthly, last 12 months" query — one useless data point).
        requested_freqs = _extract_requested_frequencies(constraint_query or query)
        if requested_freqs:
            freq_label = "/".join(sorted(requested_freqs))
            prompt += (
                f"\n\nFREQUENCY REQUIREMENT: The user asked for {freq_label} data. "
                f"Each candidate's reporting frequency is shown in its metadata. "
                f"STRONGLY prefer a candidate whose frequency matches {freq_label}; "
                f"pick a different-frequency candidate ONLY when no candidate "
                f"matches. Never trade a matching frequency for a marginally "
                f"better title."
            )

        # When candidates are very similar (embedding scores within 0.03),
        # tell the LLM to prefer ASK over PICK to avoid overconfident wrong picks.
        if prefer_ask:
            prompt += (
                "\n\nIMPORTANT: The available indicators are VERY similar to each other. "
                "Unless you are HIGHLY confident one is clearly the best match, "
                "use ASK to let the user choose from the top 3-5 most relevant options. "
                "Do not ASK merely because retrieval scores are close when one option is the "
                "general/direct count or total and the alternatives are breakdowns, distributions, "
                "sub-populations, or account tables that the user did not request. "
                "Likewise do not ASK when the candidates are variants of the SAME "
                "concept along structural axes the selection rules above already "
                "resolve — frequency (annual/quarterly/monthly), seasonally "
                "adjusted vs NSA, real vs nominal, level vs growth rate: apply "
                "those rules and PICK the standard variant. Asking a user to "
                "choose among such variants is noise; ASK is for genuinely "
                "DIFFERENT concepts or scopes."
            )

        settings = self._settings
        if settings.llm_provider == "openrouter":
            main_url = "https://openrouter.ai/api/v1/chat/completions"
            main_headers = {
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
            }
        else:
            main_url = (settings.llm_base_url or "http://localhost:8000").rstrip("/") + "/v1/chat/completions"
            main_headers = {"Content-Type": "application/json"}

        # Attempt chain: a dedicated selector endpoint (when configured) is
        # tried FIRST — selection quality drives indicator accuracy, so a
        # stronger local model is worth it — with the main LLM provider as an
        # automatic fallback so a dropped SSH tunnel degrades quality, never
        # availability. Reasoning models need extra token headroom because
        # vLLM streams their thinking before the final control line.
        attempts: List[tuple[str, str, Dict[str, str], Optional[str], int]] = []
        selector_base = (getattr(settings, "selector_llm_base_url", None) or "").strip()
        selector_model = (getattr(settings, "selector_llm_model", None) or "").strip()
        if selector_base and selector_model:
            attempts.append(
                (
                    "selector-endpoint",
                    selector_base.rstrip("/") + "/v1/chat/completions",
                    {"Content-Type": "application/json"},
                    selector_model,
                    1500,
                )
            )
        if not attempts or attempts[0][1] != main_url or attempts[0][3] != settings.llm_model:
            # Skip the main-LLM attempt when it IS the selector endpoint
            # (same URL + model) — retrying the same dead tunnel wastes 30s.
            attempts.append(("main-llm", main_url, main_headers, settings.llm_model, 500))
        if (
            settings.llm_provider != "openrouter"
            and getattr(settings, "openrouter_api_key", None)
            and all("openrouter.ai" not in a[1] for a in attempts)
        ):
            # When the main provider is itself a local endpoint (possibly the
            # same tunnel as the selector endpoint), keep a hosted last resort
            # so a dropped tunnel degrades cost, not availability or quality:
            # the hosted twin of the local model scored 10/12 on the selector
            # eval at $0.18/1k selections (gpt-4o-mini: 6/12 with 5 wrong).
            # LLM_FALLBACK_MODEL is the shared knob with the parse path.
            attempts.append(
                (
                    "openrouter-fallback",
                    "https://openrouter.ai/api/v1/chat/completions",
                    {
                        "Authorization": f"Bearer {settings.openrouter_api_key}",
                        "Content-Type": "application/json",
                    },
                    getattr(settings, "llm_fallback_model", None) or "openai/gpt-oss-120b",
                    1500,
                )
            )

        # Offline test determinism (OPENECON_SELECTOR_FIXTURES): replay recorded
        # adjudication responses so the selector seam never touches the network.
        # Iterate the SAME attempt chain the live path would, keyed per
        # (model, prompt), so a fallback endpoint's recording satisfies the call
        # in exactly the order the live fallback would try it. A hard miss (no
        # recording for ANY attempt's model) raises KeyError naming the query and
        # the re-record command — never a silent default that would mask the gap.
        from . import selector_llm_fixtures as _sel_fx

        _fixture_mode = _sel_fx.mode()
        if _fixture_mode == "replay":
            _fixtures = _sel_fx.load_fixtures()
            _found_recording = False
            for _label, _url, _headers, _model, _max_tokens in attempts:
                _recorded = _sel_fx.get_recorded_response(_fixtures, _model, prompt)
                if _recorded is None:
                    continue  # not recorded for THIS model — try the next attempt
                _found_recording = True
                _content = (_recorded or "").strip()
                if not _content:
                    continue  # recorded empty → mirror live "try next endpoint"
                _parsed = self._parse_llm_response(_content, candidates, provider, query)
                if _parsed is not None:
                    return _parsed
                # recorded but no control line → mirror live "try next endpoint"
            if not _found_recording:
                raise _sel_fx.missing_fixture_error(
                    query, provider, [a[3] for a in attempts]
                )
            return None

        from .http_pool import get_http_client

        client = get_http_client()
        for attempt_label, url, headers, model, max_tokens in attempts:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0,
            }
            try:
                resp = await client.post(url, headers=headers, json=payload, timeout=30)
                if resp.status_code == 429 or resp.status_code >= 500:
                    # One bounded retry for transient LLM-gateway errors so an
                    # infra blip doesn't silently become a "no decision" refusal.
                    await asyncio.sleep(1.0)
                    resp = await client.post(url, headers=headers, json=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json()
                content = (data["choices"][0]["message"].get("content") or "").strip()

                if not content:
                    logger.warning(
                        "LLM selection via %s returned empty content (%s/%s); trying next endpoint",
                        attempt_label, provider, query[:40],
                    )
                    continue

                # Record mode: persist the real response this endpoint returned,
                # keyed by (this attempt's model, prompt), so replay can reproduce
                # the exact live fallback order. Recorded verbatim — even when it
                # has no control line — so replay mirrors the "try next endpoint"
                # path too. See selector_llm_fixtures for the contract.
                if _fixture_mode == "record":
                    _sel_fx.record_response(model, prompt, content)

                parsed = self._parse_llm_response(content, candidates, provider, query)
                if parsed is not None:
                    return parsed
                # Non-empty but unparseable (no control line — e.g. truncated
                # reasoning prose). A quality failure on one endpoint must not
                # short-circuit the remaining healthy endpoints: that is the
                # "tunnel degrades cost, never availability" contract.
                logger.warning(
                    "LLM selection via %s returned no control line (%s/%s); trying next endpoint",
                    attempt_label, provider, query[:40],
                )

            except Exception as e:
                logger.warning(
                    "LLM selection via %s failed (%s/%s): %s",
                    attempt_label, provider, query[:40], e,
                )

        return None

    def _parse_llm_response(
        self,
        content: str,
        candidates: List[tuple[str, str]],
        provider: str,
        query: str = "",
    ) -> Optional["SelectionResult"]:
        """Parse the selector LLM's control response.

        The parser is intentionally mechanical: it extracts option numbers only
        from explicit PICK/ASK/REJECT control lines so years, codes, or other
        explanatory numbers cannot silently change the selected candidate.
        """
        lines = [line.strip() for line in str(content or "").splitlines() if line.strip()]
        if not lines:
            return None

        # Control lines must be ANCHORED at line start: reasoning prose like
        # "I should not PICK 2 — it is a level, not a rate." must never be
        # read as a decision. When several control lines appear (a model
        # thinking out loud before committing), the LAST one is the decision —
        # reasoning models emit their conclusion at the end.
        control_kind = ""
        control_index = -1
        for idx, line in enumerate(lines):
            ctl = re.match(r"^(PICK|ASK|REJECT)\b", line, re.IGNORECASE)
            if ctl:
                control_kind = ctl.group(1).upper()
                control_index = idx

        reject_line = lines[control_index] if control_kind == "REJECT" else ""
        if reject_line:
            search_line = next((line for line in lines if re.match(r"^SEARCH\b", line, re.IGNORECASE)), "")
            if not search_line:
                # The LLM often emits "REJECT: ... SEARCH: ..." on one line;
                # honor the alternative search terms wherever they appear.
                inline = re.search(r"\bSEARCH\s*[:\-]\s*(.+)$", reject_line, flags=re.IGNORECASE)
                if inline:
                    search_line = f"SEARCH: {inline.group(1)}"
                    reject_line = reject_line[: inline.start()].strip()
            rejection_reason = re.sub(r"^REJECT\s*[:\-]?\s*", "", reject_line, flags=re.IGNORECASE).strip()
            retry_query = re.sub(r"^SEARCH\s*[:\-]?\s*", "", search_line, flags=re.IGNORECASE).strip()
            # SEARCH content is typically a quoted, comma-separated term list.
            # Use the terms verbatim (joined) as the retry retrieval query, but
            # drop surrounding quotes so the embedding query reads naturally.
            retry_query = " ".join(
                term for term in re.findall(r'"([^"]+)"', retry_query)
            ) or retry_query.strip('"’ ')
            logger.info(
                "🚫 LLM rejected all candidates for '%s': %s",
                query[:40],
                rejection_reason[:120],
            )
            return SelectionResult(
                code=None,
                source="llm_reject",
                rejection_reason=rejection_reason,
                retry_query=retry_query,
            )

        pick_line = lines[control_index] if control_kind == "PICK" else ""
        if pick_line:
            match = re.match(r"^PICK\b\s*[:#\-]?\s*(\d{1,3})\b", pick_line, re.IGNORECASE)
            if match:
                num = int(match.group(1)) - 1
                if 0 <= num < len(candidates):
                    code, name = candidates[num]
                    code = self._normalize_code(code, provider)
                    logger.info("🎯 LLM picked: '%s' → %s (%s)", query[:40], code, name[:40])
                    return SelectionResult(code=code, name=name, source="llm_pick")

        ask_line = lines[control_index] if control_kind == "ASK" else ""
        if ask_line:
            match = re.match(r"^ASK\b\s*[:#\-]?\s*([0-9,\s]+)", ask_line, re.IGNORECASE)
            number_text = match.group(1) if match else ""
            options_list = []
            for digits in re.findall(r"\d{1,3}", number_text):
                idx = int(digits) - 1
                if 0 <= idx < len(candidates):
                    code, name = candidates[idx]
                    code = self._normalize_code(code, provider)
                    if not any(o["code"] == code for o in options_list):
                        options_list.append({"code": code, "name": name})
            if options_list:
                logger.info("🔵 LLM asks user: '%s' → %d options", query[:40], len(options_list))
                return SelectionResult(code=None, source="user_choice", options=options_list[:10])

        # Conservative fallback for non-compliant but explicit responses such as
        # "choose option 2". Do not extract arbitrary digits from explanations.
        fallback = re.search(
            r"(?:\b(?:choose|choice|option|indicator|number)\b|#)\s*[:#\-]?\s*(\d{1,3})\b",
            content,
            re.IGNORECASE,
        )
        if fallback:
            num = int(fallback.group(1)) - 1
            if 0 <= num < len(candidates):
                code, name = candidates[num]
                code = self._normalize_code(code, provider)
                return SelectionResult(code=code, name=name, source="llm_pick")

        return None

    @staticmethod
    def _normalize_code(code: str, provider: str) -> str:
        """Normalize provider-specific code prefixes."""
        if provider.upper() == "BIS" and code.startswith("BIS_"):
            return code[4:]
        return code


# ---------------------------------------------------------------------------
# Selection cache: the (FTS + embedding + LLM adjudication) pipeline is the
# dominant per-query latency (3-10s) and the SAME concept resolves repeatedly
# across users ("unemployment rate"/FRED). Confident llm_pick results are
# cached per-worker with a TTL; anything ambiguous (user-choice, rejections)
# or an alternate-retry call (exclude_codes set) bypasses the cache, and the
# post-fetch verification/alternate-retry safety nets still run per query.
# Cleared on restart (deploys naturally invalidate).
# ---------------------------------------------------------------------------
_SELECTION_CACHE: "dict[tuple, tuple[float, dict]]" = {}
_SELECTION_CACHE_TTL_S = 6 * 3600.0
_SELECTION_CACHE_MAX = 500


def _selection_cache_get(key: tuple):
    import time as _time

    entry = _SELECTION_CACHE.get(key)
    if not entry:
        return None
    ts, payload = entry
    if _time.monotonic() - ts > _SELECTION_CACHE_TTL_S:
        _SELECTION_CACHE.pop(key, None)
        return None
    return payload


def invalidate_selection_cache_entry(provider: str, code: str) -> int:
    """Evict cached selections that picked *code* under *provider*.

    Used when a response-level hard check (subnational fail-closed) discards
    served data: the confident pick that produced it would otherwise be
    replayed from this cache for up to 6h, fast-failing every repeat of the
    query (observed live: 'California GDP' fast-failed ~1.6s on a cached
    national pick). Returns the number of entries removed.
    """
    provider_norm = str(provider or "").strip().upper()
    code_norm = str(code or "").strip().upper()
    if not provider_norm or not code_norm:
        return 0
    doomed = [
        key
        for key, (_ts, payload) in list(_SELECTION_CACHE.items())
        if key[0] == provider_norm
        and str(payload.get("code") or "").strip().upper() == code_norm
    ]
    for key in doomed:
        _SELECTION_CACHE.pop(key, None)
    return len(doomed)


def _selection_cache_put(key: tuple, payload: dict) -> None:
    import time as _time

    if len(_SELECTION_CACHE) >= _SELECTION_CACHE_MAX:
        # Evict the oldest ~10% (insertion-ordered dict).
        for old_key in list(_SELECTION_CACHE.keys())[: _SELECTION_CACHE_MAX // 10 or 1]:
            _SELECTION_CACHE.pop(old_key, None)
    _SELECTION_CACHE[key] = (_time.monotonic(), payload)


class SelectionResult:
    """Result of indicator selection."""

    def __init__(
        self,
        code: Optional[str] = None,
        name: Optional[str] = None,
        source: str = "unknown",
        options: Optional[List[Dict[str, str]]] = None,
        rejection_reason: str = "",
        retry_query: str = "",
        made_under_prefer_ask: bool = False,
    ):
        self.code = code
        self.name = name
        self.source = source
        self.options = options
        self.rejection_reason = rejection_reason
        self.retry_query = retry_query
        # True when the PICK was produced while the score-ambiguity ASK bias
        # (prefer_ask) was set — the retrieval could not clearly separate the
        # candidates. Such a pick is deliberately NOT authoritative (see the
        # `authoritative` property): the request-level authority contract only
        # trusts a pick made from a clearly-separated candidate field.
        self.made_under_prefer_ask = bool(made_under_prefer_ask)

    @property
    def needs_user_choice(self) -> bool:
        return self.source == "user_choice" and bool(self.options)

    @property
    def authoritative(self) -> bool:
        """Confidence signal consumed by the request-level authority contract.

        A confident, self-sufficient LLM PICK: a real provider-native code, an
        ``llm_pick`` source (never ASK/reject/uncertain), and NOT made under the
        ``prefer_ask`` score-ambiguity bias. Only such a pick may later suppress
        a redundant near-tie ask-gate (indicator_clarification), and only when
        its OWN served data is exactly this pick and passes the hard predicates.
        It never suppresses refusal of wrong data — that is enforced structurally
        at the consumption seam.
        """
        return bool(self.code) and self.source == "llm_pick" and not self.made_under_prefer_ask

    @property
    def rejected_candidates(self) -> bool:
        return self.source == "llm_reject"

    def __repr__(self) -> str:
        if self.needs_user_choice:
            return f"SelectionResult(user_choice, {len(self.options)} options)"
        if self.rejected_candidates:
            return f"SelectionResult(llm_reject, retry_query={self.retry_query!r})"
        return f"SelectionResult(code={self.code}, source={self.source})"
