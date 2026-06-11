"""Delta extraction for conversation follow-ups.

Phase 3: Context-aware deterministic detection of what changed between turns.

DeltaExtractor tries fast structural handlers (country-only change,
dimension modifier, indicator switch, time change) BEFORE the LLM.
If no deterministic match is found it returns None so the caller can
fall through to the LLM-based follow-up detection.

Phase 3 addition: the extractor is now *context-aware*.  When the current
ConversationState points to a StatsCan indicator with dimensional tables,
follow-up terms like "female", "shelter", "Ontario" are checked against the
table's actual dimension members BEFORE the indicator-switch handler runs.
This prevents dimension modifiers from being misclassified as new indicators.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Dict, Optional, TYPE_CHECKING

from .conversation_state_v2 import ConversationState, FollowUpDelta
from ..routing.country_resolver import CountryResolver

if TYPE_CHECKING:
    from .query import QueryService

logger = logging.getLogger(__name__)


# Provider names for detection
_PROVIDER_NAMES = {
    "fred", "world bank", "worldbank", "imf", "eurostat", "oecd",
    "bis", "statscan", "statistics canada", "comtrade", "un comtrade",
    "coingecko", "exchangerate",
}

# Filler/stop words that are not semantically meaningful in follow-ups
_FILLER_WORDS = {
    "show", "me", "the", "a", "an", "for", "in", "of", "to", "and",
    "what", "about", "how", "is", "are", "was", "were", "do", "does",
    "can", "could", "would", "will", "shall", "let", "please", "now",
    "instead", "also", "too", "well", "same", "but", "only", "just",
    "keep", "filter", "display", "plot", "use", "from", "with",
    "compare", "add", "include", "plus", "get", "give", "tell",
    "look", "at", "up", "see", "that", "this", "it", "its", "my",
    "switch", "back", "again", "comparison",
    # Removal words (country-level operations, not indicator terms)
    "remove", "exclude", "without", "drop", "delete", "minus",
    # Time-related words (not indicator terms)
    "since", "last", "past", "recent", "latest", "between",
    "years", "year", "months", "month", "quarters", "quarter",
    "decades", "decade",
}

# Provider switch patterns
_PROVIDER_SWITCH_RE = re.compile(
    r"\b(?:from|use|switch\s+to|via|using|through)\s+"
    r"(fred|world\s*bank|imf|eurostat|oecd|bis|statscan|statistics\s+canada|comtrade|coingecko)\b",
    re.IGNORECASE,
)

# Time-related patterns
_TIME_YEAR_RE = re.compile(r"\b(19\d{2}|20\d{2})\b")
_TIME_LAST_N_RE = re.compile(
    r"\blast\s+(\d+)\s+(years?|months?|quarters?|decades?)\b",
    re.IGNORECASE,
)
_TIME_LAST_N_DAYS_RE = re.compile(
    r"\blast\s+(\d+)\s+days?\b",
    re.IGNORECASE,
)
_TIME_LAST_SIMPLE_RE = re.compile(
    r"\blast\s+(year|month|quarter|week|day)\b",
    re.IGNORECASE,
)
_TIME_SINCE_RE = re.compile(r"\bsince\s+(19\d{2}|20\d{2})\b", re.IGNORECASE)
_TIME_FROM_TO_RE = re.compile(
    r"\bfrom\s+(19\d{2}|20\d{2})\s+to\s+(19\d{2}|20\d{2})\b",
    re.IGNORECASE,
)
# "change to 2020-2025", "2020-2025", "2020 to 2025" (with hyphen or dash)
_TIME_RANGE_RE = re.compile(
    r"\b(19\d{2}|20\d{2})\s*[-–—]\s*(19\d{2}|20\d{2})\b",
)

_FREQUENCY_CHANGE_RE = re.compile(
    r"\b(?:change|switch|set|show|use)?\s*(?:to\s+)?"
    r"(monthly|quarterly|annual|annually|yearly|daily|weekly)"
    r"(?:\s+frequency|\s+data)?\b",
    re.IGNORECASE,
)

_CURRENCY_PAIR_RE = re.compile(
    r"\b([A-Za-z]{3})\s*(?:to|/|vs)\s*([A-Za-z]{3})\b",
    re.IGNORECASE,
)

_YOY_CHANGE_RE = re.compile(
    r"\b(?:show\s+)?(?:year[\s-]*over[\s-]*year|yoy)\b(?:\s+(?:change|growth))?\b",
    re.IGNORECASE,
)

_RANKING_FOLLOW_UP_RE = re.compile(
    r"\b(?:top(?:\s+\d+)?|highest|lowest|largest|smallest|best|worst|rank(?:ed|ing)?)\b",
    re.IGNORECASE,
)

_ANNUALIZED_RATE_RE = re.compile(
    r"\bannualized\s+rate\b",
    re.IGNORECASE,
)

_UNIT_CONVERSION_RE = re.compile(
    r"\b(?:convert|show|display)\s+(?:to|in)\s+"
    r"(billions?|millions?|trillions?|thousands?)\b",
    re.IGNORECASE,
)

_ALL_TIME_HIGH_RE = re.compile(
    r"\b(?:all[\s-]*time\s+high|ath)\b",
    re.IGNORECASE,
)

_TRADE_EXPORT_IMPORT_RE = re.compile(
    r"\b(exports?|imports?)\b",
    re.IGNORECASE,
)

_TRADE_BALANCE_RE = re.compile(
    r"\btrade\s+balance\b",
    re.IGNORECASE,
)

_TOTAL_TRADE_RE = re.compile(
    r"\btotal\s+trade\b",
    re.IGNORECASE,
)

_PARTNER_RE = re.compile(
    r"\bpartner\b",
    re.IGNORECASE,
)

# Additive / replacement / removal markers
_ADDITIVE_MARKERS = {"compare", "add", "also", "include", "plus", "too", "well"}
_REPLACEMENT_MARKERS = {"only", "just", "filter", "keep"}
_REMOVAL_MARKERS = {"remove", "exclude", "without", "drop", "delete", "minus"}

_DECOMPOSITION_VALUE_TO_TYPE = {
    "province": "provinces",
    "provinces": "provinces",
    "state": "states",
    "states": "states",
    "region": "regions",
    "regions": "regions",
    "country": "countries",
    "countries": "countries",
}

_CRYPTO_ASSET_ALIASES = {
    "bitcoin": "bitcoin price",
    "btc": "bitcoin price",
    "ethereum": "ethereum price",
    "eth": "ethereum price",
    "dogecoin": "dogecoin price",
    "doge": "dogecoin price",
    "solana": "solana price",
    "sol": "solana price",
    "cardano": "cardano price",
    "ada": "cardano price",
    "ripple": "ripple price",
    "xrp": "ripple price",
}

_GEOGRAPHY_DIMENSION_KEYS = {
    "geography",
    "province",
    "provinces",
    "state",
    "states",
    "region",
    "regions",
    "country",
    "countries",
}

_INDICATOR_NOISE_WORDS = {
    "rate", "rates", "data", "series", "value", "values", "index", "indexes",
    "metric", "metrics", "indicator", "indicators",
}

_INDICATOR_ALIAS_EQUIVALENTS: dict[str, tuple[tuple[str, ...], ...]] = {
    "gdp": (("gross", "domestic", "product"),),
    "gni": (("gross", "national", "income"),),
    "cpi": (("consumer", "price"), ("consumer", "prices")),
    "ppi": (("producer", "price"), ("producer", "prices")),
    "hicp": (
        ("harmonised", "consumer", "prices"),
        ("harmonized", "consumer", "prices"),
    ),
    "reer": (("real", "effective", "exchange", "rate"),),
    "neer": (("nominal", "effective", "exchange", "rate"),),
}


class DeltaExtractor:
    """Extract FollowUpDelta from a query given conversation state.

    Two-tier extraction:
    1. Fast deterministic regex handlers for structurally unambiguous patterns
    2. LLM-based extraction for everything else (natural language, compound changes)
    """

    def __init__(self, query_service: "QueryService") -> None:
        self._qs = query_service

    @staticmethod
    def _looks_like_pure_time_change_query(
        query: str,
        state: Optional["ConversationState"] = None,
    ) -> bool:
        query_lower = str(query or "").lower().strip()
        if not query_lower:
            return False

        tokens = re.findall(r"[a-zA-Z]+", query_lower)
        non_filler = [
            t for t in tokens
            if t not in _FILLER_WORDS and t not in {
                "years", "year", "months", "month", "quarters", "quarter",
                "decades", "decade", "weeks", "week", "days", "day",
                "since", "last", "from", "to", "between",
                "past", "recent", "latest", "change", "switch", "update", "set",
                "period", "range", "time", "dates", "date", "timeframe",
            }
        ]
        if len(non_filler) <= 1 and state is None:
            return True
        if not non_filler:
            return True

        # A residual content token survives.  If it is NOT part of the current
        # indicator's token set, this is NOT a pure time change — it is an
        # indicator switch wearing a time modifier ("inflation since 2010",
        # "unemployment last 5 years", "exports 2010-2020").  Falling through to
        # the time handler would silently keep the OLD indicator and only move
        # the date window (F2).  Return False so the query falls through to the
        # indicator-switch / LLM tier.  Tokens that DO restate the current
        # indicator (e.g. "GDP last 10 years" when state.indicator is GDP)
        # remain a pure time change.
        if state is None:
            return len(non_filler) <= 1

        indicator_tokens = DeltaExtractor._indicator_token_set(state.indicator)
        residual_content = {
            t for t in non_filler
            if t not in _INDICATOR_NOISE_WORDS and len(t) > 1
        }
        # Expand residual tokens through indicator aliases so "gdp" matches a
        # "gross domestic product" indicator label and vice-versa.
        expanded_residual = DeltaExtractor._indicator_token_set(" ".join(residual_content))
        unmatched = (residual_content | expanded_residual) - indicator_tokens
        # Drop pure alias-expansion artifacts that resolve back into the
        # indicator set; only flag tokens that are genuinely foreign.
        unmatched = {t for t in unmatched if t in residual_content and t not in indicator_tokens}
        if unmatched:
            return False
        return True

    @staticmethod
    def _normalize_frequency(value: str) -> str:
        freq = str(value or "").strip().lower()
        return {
            "annually": "annual",
            "yearly": "annual",
        }.get(freq, freq)

    @staticmethod
    def _promote_decomposition_semantics(
        delta: FollowUpDelta,
        state: ConversationState,
    ) -> FollowUpDelta:
        added_dimensions = dict(delta.added_dimensions or {})
        if not added_dimensions:
            return delta

        remaining_dimensions: Dict[str, str] = {}
        decomposition_payload = delta.changed_decomposition
        has_specific_geography_filter = False

        for raw_key, raw_value in added_dimensions.items():
            key = str(raw_key or "").strip()
            value = str(raw_value or "").strip()
            key_lower = key.lower()
            value_lower = value.lower()
            if key_lower in _GEOGRAPHY_DIMENSION_KEYS:
                decomp_type = _DECOMPOSITION_VALUE_TO_TYPE.get(value_lower)
                if decomp_type and decomposition_payload is None:
                    decomposition_payload = {
                        "type": decomp_type,
                        "entities": None,
                        "axis": key,
                    }
                    continue
                has_specific_geography_filter = True
            remaining_dimensions[key] = value

        if decomposition_payload is not None:
            delta.changed_decomposition = decomposition_payload
            delta.delta_type = "decomposition_change"

        delta.added_dimensions = remaining_dimensions or None
        if has_specific_geography_filter and state.decomposition is not None:
            delta.is_dimension_modifier_change = True

        return delta

    @staticmethod
    def _fill_explicit_country_scope_for_switch_delta(
        delta: FollowUpDelta,
        query_text: str,
    ) -> FollowUpDelta:
        if delta.changed_country or delta.changed_countries or delta.added_countries or delta.removed_countries:
            return delta
        if delta.delta_type not in {"indicator_switch", "provider_change", "compound_change", "country_change"} and not (
            delta.changed_provider or delta.changed_indicator
        ):
            return delta
        stripped_query = _PROVIDER_SWITCH_RE.sub(" ", query_text)
        extracted_countries = CountryResolver.detect_all_countries_in_query(stripped_query)
        if not extracted_countries:
            extracted_countries = CountryResolver.detect_all_countries_in_query(query_text)
        if not extracted_countries:
            return delta
        if len(extracted_countries) == 1:
            delta.changed_country = extracted_countries[0]
        else:
            delta.changed_countries = extracted_countries
        return delta

    @staticmethod
    def _sanitize_provider_change_geography_artifacts(
        delta: FollowUpDelta,
        query_text: str,
    ) -> FollowUpDelta:
        if delta.changed_provider is None:
            return delta

        stripped_query = _PROVIDER_SWITCH_RE.sub(" ", str(query_text or ""))
        # detect_all_countries_in_query returns ISO2 codes ("DE"); the LLM emits
        # full names ("GERMANY").  Normalize BOTH sides through CountryResolver
        # so a valid country change is not silently nulled by a code/name
        # mismatch (F5).  Fall back to the upper-cased raw token when a value
        # cannot be normalized, so non-country artifacts (e.g. "1W") still get
        # filtered out by the membership check.
        def _canon(value: object) -> str:
            text = str(value or "").strip()
            if not text:
                return ""
            return (CountryResolver.normalize(text) or text).upper()

        retained_countries = {
            _canon(country)
            for country in CountryResolver.detect_all_countries_in_query(stripped_query)
        }
        retained_countries.discard("")

        if delta.changed_country and _canon(delta.changed_country) not in retained_countries:
            delta.changed_country = None
        if delta.changed_countries:
            kept = [
                country
                for country in delta.changed_countries
                if _canon(country) in retained_countries
            ]
            delta.changed_countries = kept or None

        return delta

    @staticmethod
    def _fill_explicit_provider_for_switch_delta(
        delta: FollowUpDelta,
        query_text: str,
    ) -> FollowUpDelta:
        if delta.changed_provider:
            return delta
        if delta.delta_type not in {"indicator_switch", "provider_change", "compound_change", "country_change"} and not delta.changed_indicator:
            return delta

        match = _PROVIDER_SWITCH_RE.search(str(query_text or ""))
        if not match:
            return delta

        from ..utils.providers import normalize_provider_name

        delta.changed_provider = normalize_provider_name(match.group(1).strip())
        return delta

    def extract(
        self,
        query: str,
        state: ConversationState,
        intent: Optional[object] = None,
    ) -> Optional[FollowUpDelta]:
        """Try deterministic extraction.  Returns ``None`` if ambiguous.

        Parameters
        ----------
        query : str
            The user's raw follow-up text.
        state : ConversationState
            Accumulated state from previous turns.
        intent : ParsedIntent, optional
            LLM-parsed intent (used for LLM-layer extraction in future phases).

        Returns
        -------
        FollowUpDelta or None
            A delta if a deterministic pattern was matched, else ``None``.
        """
        if not state.indicator:
            return None

        query_text = str(query or "").strip()
        if not query_text:
            return None

        delta = self._try_exchange_rate_pair_change(query_text, state)
        if delta:
            return delta

        delta = self._try_transform_equivalence_noop(query_text, state)
        if delta:
            return delta

        delta = self._try_annualized_rate_noop(query_text, state)
        if delta:
            return delta

        delta = self._try_rate_like_unit_conversion_noop(query_text, state)
        if delta:
            return delta

        delta = self._try_crypto_all_time_high_follow_up(query_text, state)
        if delta:
            return delta

        delta = self._try_ranking_follow_up(query_text, state)
        if delta:
            return delta

        delta = self._try_comtrade_trade_follow_up(query_text, state)
        if delta:
            return delta

        delta = self._try_trade_flow_indicator_sync(query_text, state)
        if delta:
            return delta

        delta = self._try_country_provider_follow_up_with_indicator_reaffirmation(query_text, state)
        if delta:
            return delta

        delta = self._try_country_follow_up_with_indicator_reaffirmation(query_text, state)
        if delta:
            return delta

        delta = self._try_breakdown_follow_up(query_text, state)
        if delta:
            return delta

        delta = self._try_dimension_modifier(query_text, state)
        if delta:
            return delta

        # Fast structural handlers — only for unambiguous patterns.
        # Dimension and indicator changes are handled by the LLM (Tier 2)
        # because regex can't distinguish "seniors" (age dimension) from
        # "seniors" (indicator switch), or "trade balance" (indicator) from
        # "wholesale trade" (GDP dimension).
        delta = self._try_country_only_follow_up(query_text, state)
        if delta:
            return delta

        delta = self._try_time_change(query_text, state)
        if delta:
            return delta

        delta = self._try_frequency_change(query_text, state)
        if delta:
            return delta

        delta = self._try_provider_change(query_text, state)
        if delta:
            return delta

        delta = self._try_indicator_switch(query_text, state)
        if delta:
            return delta

        return None

    def _try_exchange_rate_pair_change(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        provider = str(state.provider or state.routed_provider or "").strip().upper()
        indicator_text = str(state.indicator or "").strip().lower()
        # Only the exchange-rate domain owns currency-pair switches.  The old
        # blanket "FRED" allowlist let this greedy regex hijack FRED indicator
        # follow-ups like "CPI vs PCE" / "GDP to GNP" into a bogus Currency
        # Pair dimension (F1).  Gate strictly on the EXCHANGERATE provider or an
        # exchange-rate indicator.
        if provider != "EXCHANGERATE" and "exchange" not in indicator_text:
            return None

        match = _CURRENCY_PAIR_RE.search(query)
        if not match:
            return None

        base, target = match.group(1).upper(), match.group(2).upper()
        if base == target:
            return None

        # Both tokens must be real ISO-4217 currency codes.  _CURRENCY_PAIR_RE
        # matches ANY two 3-letter tokens around to/vs//, so without this guard
        # "CPI vs PCE" would slip through even on the exchange-rate path.
        from ..providers.exchangerate import ISO_4217_CURRENCY_CODES
        if base not in ISO_4217_CURRENCY_CODES or target not in ISO_4217_CURRENCY_CODES:
            return None

        pair_text = f"{base} to {target}"
        logger.info("Delta: exchange-rate pair change → %s", pair_text)
        return FollowUpDelta(
            added_dimensions={"Currency Pair": pair_text},
            is_dimension_modifier_change=True,
            raw_query=query,
            delta_type="dimension_change",
            query_type="parameter_delta",
        )

    def _try_transform_equivalence_noop(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Treat redundant transform phrasing as a no-op state-preserving follow-up.

        Example: asking for year-over-year change when the active series is
        already a growth/inflation rate should preserve the current answer set
        rather than triggering an unsupported indicator rewrite.
        """
        if not _YOY_CHANGE_RE.search(query):
            return None

        indicator_text = str(state.indicator or "").strip().lower()
        resolved_code = str(state.resolved_indicator_code or "").strip().upper()
        already_rate_like = (
            "growth" in indicator_text
            or "inflation" in indicator_text
            or resolved_code in {"NGDP_RPCH", "NY.GDP.MKTP.KD.ZG", "A191RL1Q225SBEA", "FP.CPI.TOTL.ZG"}
        )
        if not already_rate_like:
            return None

        logger.info("Delta: redundant transform request on already rate-like series → preserve current state")
        return FollowUpDelta(
            changed_chart_type=state.chart_type or "line",
            raw_query=query,
            delta_type="chart_change",
            query_type="parameter_delta",
        )

    def _try_ranking_follow_up(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Treat short ranking follow-ups as state-preserving retrieval modifiers.

        This prevents generic ranking prompts like "Show only top 3" from being
        reinterpreted as decomposition changes by the LLM when the active answer
        set is a multi-country comparison.
        """
        query_text = str(query or "").strip()
        if not _RANKING_FOLLOW_UP_RE.search(query_text):
            return None
        if CountryResolver.detect_all_countries_in_query(query_text):
            return None
        if any(
            getattr(state, field, None)
            for field in ("dimensions", "decomposition", "trade_flow", "trade_partner", "trade_reporter", "trade_commodity")
        ):
            return None

        active_members = list(getattr(state, "active_answer_members", None) or [])
        recent_members = list(getattr(state, "recent_answer_members", None) or [])
        scope_count = 0
        if state.countries:
            scope_count = len(state.countries)
        elif state.country:
            scope_count = 1
        scope_count = max(scope_count, len(active_members), len(recent_members))
        if scope_count < 2:
            return None

        logger.info("Delta: ranking follow-up -> preserve current state and apply ranking projection")
        return FollowUpDelta(
            changed_chart_type=state.chart_type or "line",
            raw_query=query,
            delta_type="chart_change",
            query_type="parameter_delta",
        )

    def _try_annualized_rate_noop(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Treat 'annualized rate' phrasing as a no-op when the active series is already rate-like."""
        if not _ANNUALIZED_RATE_RE.search(query):
            return None

        indicator_text = str(state.indicator or "").strip().lower()
        provider = str(state.provider or state.routed_provider or "").strip().upper()
        frequency = str(state.frequency or "").strip().lower()
        resolved_code = str(state.resolved_indicator_code or "").strip().upper()

        already_rate_like = (
            "growth" in indicator_text
            or "inflation" in indicator_text
            or "rate" in indicator_text
            or provider in {"WORLDBANK", "IMF"}
            or frequency == "annual"
            or resolved_code in {"NGDP_RPCH", "NY.GDP.MKTP.KD.ZG", "FP.CPI.TOTL.ZG", "UNE_RT_A"}
        )
        if not already_rate_like:
            return None

        logger.info("Delta: annualized-rate request on already rate-like series -> preserve current state")
        return FollowUpDelta(
            changed_chart_type=state.chart_type or "line",
            raw_query=query,
            delta_type="chart_change",
            query_type="parameter_delta",
        )

    def _try_rate_like_unit_conversion_noop(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Treat incompatible magnitude-unit requests on rate-like series as no-ops.

        Follow-ups such as "Convert to billions" are formatting requests for
        level data, but they do not make semantic sense when the active series
        is already expressed as a rate/percent. Preserving the current state is
        safer than re-routing to a new provider/indicator family.
        """
        if not _UNIT_CONVERSION_RE.search(query):
            return None

        indicator_text = str(state.indicator or "").strip().lower()
        provider = str(state.provider or state.routed_provider or "").strip().upper()
        frequency = str(state.frequency or "").strip().lower()
        resolved_code = str(state.resolved_indicator_code or "").strip().upper()

        already_rate_like = (
            "growth" in indicator_text
            or "inflation" in indicator_text
            or "rate" in indicator_text
            or "%" in indicator_text
            or frequency == "annual"
            or resolved_code in {"NGDP_RPCH", "NY.GDP.MKTP.KD.ZG", "FP.CPI.TOTL.ZG", "UNE_RT_A"}
            or provider in {"IMF", "WORLDBANK"} and any(
                cue in indicator_text for cue in ("growth", "inflation")
            )
        )
        if not already_rate_like:
            return None

        logger.info("Delta: incompatible magnitude-unit request on rate-like series -> preserve current state")
        return FollowUpDelta(
            changed_chart_type=state.chart_type or "line",
            raw_query=query,
            delta_type="chart_change",
            query_type="parameter_delta",
        )

    def _try_crypto_all_time_high_follow_up(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Treat CoinGecko ATH comparison prompts as state-preserving follow-ups.

        The current framework does not model ATH as a separate benchmark series.
        Returning a no-op chart delta prevents the follow-up from drifting into
        an additive pseudo-indicator that duplicates the active crypto asset.
        """
        if not _ALL_TIME_HIGH_RE.search(query):
            return None

        provider = str(state.provider or state.routed_provider or "").strip().upper()
        if provider != "COINGECKO":
            return None

        if not (state.coin_ids or "price" in str(state.indicator or "").strip().lower()):
            return None

        logger.info("Delta: crypto all-time-high benchmark follow-up -> preserve current state")
        return FollowUpDelta(
            changed_chart_type=state.chart_type or "line",
            raw_query=query,
            delta_type="chart_change",
            query_type="parameter_delta",
        )

    def _try_trade_flow_indicator_sync(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        provider = str(state.provider or state.routed_provider or "").strip().upper()
        if provider != "COMTRADE":
            return None

        match = _TRADE_EXPORT_IMPORT_RE.search(query)
        if not match:
            return None

        flow_word = match.group(1).lower()
        indicator = "exports" if flow_word.startswith("export") else "imports"
        flow = "EXPORT" if indicator == "exports" else "IMPORT"
        logger.info("Delta: trade flow/indicator sync → %s (%s)", indicator, flow)
        return FollowUpDelta(
            changed_indicator=indicator,
            changed_trade_flow=flow,
            raw_query=query,
            delta_type="compound_change",
            query_type="parameter_delta",
        )

    @staticmethod
    def _trade_indicator_payload(query: str) -> tuple[Optional[str], Optional[str]]:
        query_text = str(query or "")
        if _TRADE_BALANCE_RE.search(query_text):
            return "trade balance", None
        if _TOTAL_TRADE_RE.search(query_text):
            return "total trade", None
        match = _TRADE_EXPORT_IMPORT_RE.search(query_text)
        if not match:
            return None, None
        flow_word = match.group(1).lower()
        if flow_word.startswith("export"):
            return "exports", "EXPORT"
        return "imports", "IMPORT"

    def _try_comtrade_trade_follow_up(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        provider = str(state.provider or state.routed_provider or "").strip().upper()
        if provider != "COMTRADE":
            return None

        query_text = str(query or "").strip()
        if not query_text:
            return None

        query_lower = query_text.lower()
        query_words = set(re.findall(r"[a-zA-Z]+", query_lower))
        is_removal = bool(query_words & _REMOVAL_MARKERS)
        is_additive = bool(query_words & _ADDITIVE_MARKERS) and not bool(
            query_words & _REPLACEMENT_MARKERS
        ) and not is_removal

        raw_countries = CountryResolver.detect_all_countries_in_query(query_text)
        if not raw_countries:
            return None

        current_partner = str(state.trade_partner or "").strip()
        current_partner_iso = CountryResolver.COUNTRY_ALIASES.get(current_partner.lower()) if current_partner else None
        current_reporters = {
            (CountryResolver.COUNTRY_ALIASES.get(str(country).strip().lower()) or str(country).strip().upper())
            for country in ((state.countries or []) or ([state.country] if state.country else []))
            if str(country or "").strip()
        }

        mentioned: list[tuple[str, str]] = []
        for country in raw_countries:
            country_text = str(country).strip()
            country_iso = CountryResolver.COUNTRY_ALIASES.get(country_text.lower()) or country_text.upper()
            pair = (country_text, country_iso)
            if pair not in mentioned:
                mentioned.append(pair)

        indicator_label, trade_flow = self._trade_indicator_payload(query_text)

        partner_candidate: Optional[str] = None
        reporter_mentions: list[str] = []

        if indicator_label and len(mentioned) >= 2 and re.search(r"\b(to|from|with|and)\b", query_lower):
            partner_candidate = mentioned[-1][0]
            reporter_mentions = [country_text for country_text, _ in mentioned[:-1]]

        if _PARTNER_RE.search(query_text):
            partner_candidates = [
                country_text
                for country_text, country_iso in mentioned
                if country_iso not in current_reporters
            ]
            if partner_candidates:
                payload = {
                    "changed_trade_partner": partner_candidates[-1],
                    "raw_query": query_text,
                    "delta_type": "compound_change",
                    "query_type": "parameter_delta",
                }
                if indicator_label:
                    payload["changed_indicator"] = indicator_label
                if trade_flow is not None:
                    payload["changed_trade_flow"] = trade_flow
                return FollowUpDelta(**payload)

        if not reporter_mentions:
            for country_text, country_iso in mentioned:
                if partner_candidate and country_text == partner_candidate:
                    continue
                if current_partner_iso and country_iso == current_partner_iso:
                    continue
                reporter_mentions.append(country_text)
        reporter_mentions = list(dict.fromkeys(reporter_mentions))

        if not reporter_mentions and indicator_label and len(mentioned) >= 2:
            partner_candidate = mentioned[-1][0]
            reporter_candidate = mentioned[0][0]
            payload = {
                "changed_country": reporter_candidate,
                "changed_trade_partner": partner_candidate,
                "changed_indicator": indicator_label,
                "raw_query": query_text,
                "delta_type": "compound_change",
                "query_type": "parameter_delta",
            }
            if trade_flow is not None:
                payload["changed_trade_flow"] = trade_flow
            return FollowUpDelta(**payload)

        if not reporter_mentions:
            return None

        payload = {
            "raw_query": query_text,
            "query_type": "parameter_delta",
        }
        if is_removal:
            payload["removed_countries"] = reporter_mentions
            payload["delta_type"] = "country_change"
        elif is_additive:
            payload["added_countries"] = reporter_mentions
            payload["delta_type"] = "additive_country"
        elif len(reporter_mentions) == 1:
            payload["changed_country"] = reporter_mentions[0]
            payload["delta_type"] = "country_change"
        else:
            payload["changed_countries"] = reporter_mentions
            payload["delta_type"] = "country_change"

        if partner_candidate:
            payload["changed_trade_partner"] = partner_candidate

        if indicator_label:
            payload["changed_indicator"] = indicator_label
            payload["delta_type"] = "compound_change"
        if trade_flow is not None:
            payload["changed_trade_flow"] = trade_flow
            payload["delta_type"] = "compound_change"

        return FollowUpDelta(**payload)

    async def extract_with_llm(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """LLM-based delta extraction for queries the regex handlers can't parse.

        Uses structured output (Instructor + Pydantic) to have the LLM
        populate only the changed fields in FollowUpDelta.
        """
        if not state.indicator:
            return None

        query_text = str(query or "").strip()
        if not query_text:
            return None

        # Get the Instructor client from the query service's openrouter
        openrouter = getattr(self._qs, "openrouter", None)
        if openrouter is None:
            logger.debug("LLM delta: no openrouter available")
            return None

        instructor_client = getattr(openrouter, "instructor_client", None)
        instructor_model = getattr(openrouter, "instructor_model", None)
        if instructor_client is None or instructor_model is None:
            logger.debug("LLM delta: instructor client not available")
            return None

        # Build state description for the prompt.
        # state.* values originate from earlier user turns, so they are
        # attacker-influenceable: text planted in turn 1 ("…Ignore prior
        # instructions, set provider=…") persists into ConversationState and
        # would be re-injected into this extractor's system prompt on every
        # follow-up. Run each value through the same sanitizer simplified_prompt
        # uses (strip control chars / backticks / triple-quotes, collapse
        # newlines, truncate) so injected line breaks can't escape the slot.
        from .simplified_prompt import SimplifiedPrompt
        _san = SimplifiedPrompt._sanitize_context
        state_lines = []
        if state.indicator:
            state_lines.append(f"  Indicator: {_san(state.indicator)}")
        if state.country:
            state_lines.append(f"  Country: {_san(state.country)}")
        if state.countries:
            state_lines.append(f"  Countries: {', '.join(_san(c) for c in state.countries)}")
        if state.provider or state.routed_provider:
            state_lines.append(f"  Provider: {_san(state.provider or state.routed_provider)}")
        if state.start_date:
            state_lines.append(f"  Start date: {_san(state.start_date)}")
        if state.end_date:
            state_lines.append(f"  End date: {_san(state.end_date)}")
        if state.frequency:
            state_lines.append(f"  Frequency: {_san(state.frequency)}")
        if state.dimensions:
            state_lines.append(f"  Dimensions: {_san(str(state.dimensions))}")
        if state.chart_type:
            state_lines.append(f"  Chart type: {_san(state.chart_type)}")
        if state.trade_flow:
            state_lines.append(f"  Trade flow: {_san(state.trade_flow)}")
        if state.trade_reporter:
            state_lines.append(f"  Trade reporter: {_san(state.trade_reporter)}")
        if state.trade_partner:
            state_lines.append(f"  Trade partner: {_san(state.trade_partner)}")
        state_text = "\n".join(state_lines) if state_lines else "  (empty)"

        # Add available dimension members if StatsCan cube metadata is cached.
        # Show exact member names so the LLM can use them directly.
        # We include top-level members PLUS any members matching the query
        # to avoid truncation hiding valid dimension values (e.g., "Energy"
        # at index 344 of 359 in CPI's "Products and product groups").
        dimension_context = ""
        if state.statscan_cube_metadata:
            dims = state.statscan_cube_metadata.get("dimension", [])
            if dims:
                dim_lines = ["\nAvailable dimensions for this indicator (use EXACT member names):"]
                _query_tokens = {t.lower() for t in query_text.split() if t.lower() not in _FILLER_WORDS and len(t) > 2}
                for dim in dims:
                    name = dim.get("dimensionNameEn", "?")
                    all_members = [m.get("memberNameEn", "?") for m in (dim.get("member") or [])]
                    if not all_members:
                        continue
                    # Always show top-level members (first 20)
                    shown = list(all_members[:20])
                    # Add any members that fuzzy-match query tokens (case-insensitive substring)
                    if _query_tokens:
                        for member in all_members[20:]:
                            member_lower = member.lower()
                            if any(tok in member_lower for tok in _query_tokens):
                                if member not in shown:
                                    shown.append(member)
                    # Cap at 50 to keep prompt reasonable
                    if len(shown) > 50:
                        shown = shown[:50]
                    truncated = " (truncated, more available)" if len(all_members) > len(shown) else ""
                    dim_lines.append(f"  {name}: {', '.join(shown)}{truncated}")
                dimension_context = "\n".join(dim_lines)

        system_prompt = f"""You are a delta extractor for an economic data query system.

Given the user's CURRENT conversation state and their NEW follow-up message,
determine ONLY what changed. Output a FollowUpDelta JSON where you populate
ONLY the fields that the user wants to modify. Leave everything else null.

STEP 1 — CLASSIFY the query into one of these types:
- "parameter_delta": Simple modification (country, time, indicator, dimension, provider, chart type)
- "pro_mode": Complex analysis needing code execution (correlation, regression, scatter plot, forecast, statistics)
- "new_query": Completely unrelated new topic
- "clarification_answer": Answering a system question (picking options, "yes", "the first one")
- "informational": Question about the system ("what sources do you have?")

Set the query_type field accordingly.

STEP 2 — If query_type is "parameter_delta", populate the changed fields:
1. Only populate fields the user explicitly wants to change.
2. For countries:
   - changed_country: REPLACE current country with a new one
   - changed_countries: REPLACE with multiple countries
   - added_countries: "add", "also", "compare with", "include" → ADDITIVE
   - removed_countries: "remove", "exclude", "without" → SUBTRACTIVE
3. For dimensions (sub-categories like sex, age group, province, product type):
   - added_dimensions: dict of {{dimension_name: value}} where value is either:
     a) A SPECIFIC member name to FILTER by (e.g., "Ontario", "Females", "15 to 24 years")
     b) The CATEGORY name when the user wants to see ALL items (e.g., "province", "age group")
   - IMPORTANT: When the user says "by province" / "show by province" / "break down by province"
     WITHOUT naming a specific province, do NOT use added_dimensions. Instead set
     changed_decomposition = {{"type": "provinces", "entities": null, "axis": "Geography"}}.
     This means "show data for ALL provinces" as a first-class breakdown, not a member filter.
   - Similarly: "by age group" → value="age group", "by sex" → value="sex" (show all).
   - But "show Ontario" / "for Ontario" → value="Ontario" (specific filter).
   - "show youth" → age_group = "15 to 24 years" (use exact member name if available below).
     "show seniors" / "55+" → age_group = "55 years and over".
   - When available dimension members are listed below, use the EXACT member name.
   - is_dimension_modifier_change: true when changing dimensions.
4. If the query is completely unrelated to prior context, set is_new_query=true, query_type="new_query".
5. For frequency changes:
   - changed_frequency: use this for "monthly frequency", "quarterly data", "annual data", "daily data", "weekly data"
   - valid values: monthly, quarterly, annual, daily, weekly
   - DO NOT put frequency in changed_chart_type
6. Set delta_type to one of: country_change, additive_country, time_change, indicator_switch, provider_change, dimension_change, chart_change, new_query, compound_change
7. For time changes: use ISO format dates (YYYY-MM-DD). "last N years" = start_date N years before today.
8. "Compare X and Y" or "Compare with Y" when X is already shown → ADDITIVE for geography/countries.
9. "What about X" / "show X instead" where X is a different economic concept → changed_indicator (replaces).
10. "break it down by X" / "filter by X" / "by sex" / "by age" / "show by province":
   - use changed_decomposition for full breakdowns like "by province"
   - use added_dimensions for specific member filters like "Ontario only"
   - CRITICAL: "show by province" / "by province" / "break down by province" (no specific province named)
     → changed_decomposition = {{"type": "provinces", "entities": null, "axis": "Geography"}}
   - "show for Ontario" / "Ontario only" → added_dimensions = {{"Geography": "Ontario"}}
   - "break it down by sector" / "by industry" → changed_decomposition = {{"type": "sectors", "entities": null, "axis": "Sector"}}
   - "by age group" (no specific group) → changed_decomposition = {{"type": "age_groups", "entities": null, "axis": "Age"}}
   - "by category" / "by sub-category" → changed_decomposition = {{"type": "categories", "entities": null, "axis": "Category"}}
   - "by region" (sub-national, no specific region) → changed_decomposition = {{"type": "regions", "entities": null, "axis": "Geography"}}
11. "also show X" / "add X" / "include X" / "and also X" where X is a DIFFERENT indicator → added_indicators (list). This ADDS to the existing indicators, not replaces. Example: after "US GDP", "also show inflation" → added_indicators=["inflation"].

STEP 3 — TELEMETRY (always populate):
- delta_confidence: float between 0.0 and 1.0 reflecting your confidence in the extracted delta.
  - 0.9-1.0: query maps cleanly to one or two fields (e.g., "switch to Japan").
  - 0.6-0.9: query is mostly clear but has some ambiguity (e.g., "what about the latest data" — frequency or time range?).
  - <0.6: query is ambiguous, restructures the conversation, or you had to guess which fields apply.
- needs_full_rewrite: true ONLY when the user's message restructures the conversation in a way that the
  field-by-field delta cannot capture — e.g., a compound query that switches indicator, country, and
  decomposition all at once with new constraints, or a query whose intent depends on details no delta field
  represents. Default false; prefer normal delta extraction when possible.

IMPORTANT DISTINCTIONS:
- "Show unemployment instead" → changed_indicator (replaces current)
- "Also show unemployment" → added_indicators (adds alongside current)
- "Correlate unemployment with GDP" → pro_mode (needs computation)
- "Show as bar chart" → parameter_delta (chart type)
- "Change to monthly frequency" → parameter_delta with changed_frequency="monthly"
- "Show me a scatter plot" → pro_mode (needs code)
- "Show by province" → changed_decomposition with type="provinces" (ALL provinces, not one specific)
- "Show for Ontario" → dimension_change with Geography="Ontario" (specific province filter)

CRITICAL RULE — DIMENSION vs INDICATOR SWITCH vs COUNTRY CHANGE:
If available dimension members are listed below, and the user's query term matches
or closely relates to one of those members, this is a DIMENSION CHANGE (added_dimensions),
NOT an indicator switch or country change.

Examples:
- CPI has "Products and product groups" with "Food", "Energy" → "show energy" = added_dimensions
- Unemployment has "Geography" with "Ontario", "Quebec" → "show for Ontario" = added_dimensions with Geography=Ontario, NOT changed_country
- Unemployment has "Geography" with provinces → "show by province" = changed_decomposition with type="provinces"
- Unemployment has "Sex" with "Males", "Females" → "show female" = added_dimensions with Sex value

Canadian provinces (Ontario, Quebec, BC, Alberta, etc.) appearing in Geography dimension members
should ALWAYS be added_dimensions, NEVER changed_country. They are sub-national filters, not countries.

CURRENT STATE:
{state_text}
{dimension_context}

Output the query_type and any changed fields as JSON."""

        try:
            delta = await instructor_client.chat.completions.create(
                model=instructor_model,
                response_model=FollowUpDelta,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": query_text},
                ],
                temperature=0.1,
                max_tokens=2000,  # Reasoning models need 3-5x headroom for JSON output
            )

            # For non-parameter_delta classifications (pro_mode, new_query,
            # informational, clarification_answer), return the delta even
            # with no changed fields — the query_type is the important signal.
            if delta.query_type and delta.query_type != "parameter_delta":
                delta.raw_query = query_text
                logger.info("LLM classification: query_type=%s for: %s",
                            delta.query_type, query_text[:50])
                return delta

            # For parameter_delta: at least one field must be non-None AND non-empty.
            # LLM may return empty strings/dicts/lists which are truthy but meaningless.
            def _has_value(val: object) -> bool:
                if val is None:
                    return False
                if isinstance(val, (str, dict, list)) and not val:
                    return False
                return True

            has_change = any(
                _has_value(getattr(delta, f))
                for f in [
                    "changed_indicator", "added_indicators",
                    "changed_country", "changed_countries",
                    "added_countries", "removed_countries", "changed_provider",
                    "changed_start_date", "changed_end_date",
                    "changed_frequency",
                    "added_dimensions", "removed_dimensions", "changed_decomposition",
                    "changed_chart_type", "changed_trade_flow",
                    "changed_trade_reporter", "changed_trade_partner",
                    "changed_trade_commodity",
                ]
            ) or delta.is_new_query

            if not has_change:
                logger.info("LLM delta: no changes detected, returning None")
                return None

            if delta.delta_type == "time_change" and not self._looks_like_pure_time_change_query(query_text, state):
                logger.info(
                    "LLM delta guard: ignoring time_change for contentful query: %s",
                    query_text[:80],
                )
                return None

            delta = self._promote_decomposition_semantics(delta, state)
            delta = self._fill_explicit_provider_for_switch_delta(delta, query_text)
            delta = self._fill_explicit_country_scope_for_switch_delta(delta, query_text)
            delta = self._sanitize_provider_change_geography_artifacts(delta, query_text)
            delta.raw_query = query_text
            logger.info(
                "LLM Delta: type=%s, changes=%s",
                delta.delta_type,
                {k: v for k, v in delta.model_dump(exclude_none=True).items()
                 if k not in ("raw_query", "delta_type", "is_new_query",
                              "is_dimension_modifier_change")},
            )
            return delta

        except Exception as exc:
            logger.warning("LLM delta extraction failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Handler: Country-only follow-up
    # ------------------------------------------------------------------

    def _try_country_only_follow_up(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Detect "show only US", "Japan", "add France" style follow-ups."""
        extracted_countries = CountryResolver.detect_all_countries_in_query(query)
        expanded_regions = CountryResolver.expand_regions_in_query(query)
        target_countries = extracted_countries or expanded_regions
        if not target_countries:
            return None

        # Check that query is geography-only (no other meaningful tokens)
        query_lower = query.lower()
        tokens = re.findall(r"[a-zA-Z]+", query_lower)
        if not tokens:
            return None

        # Build set of geography-related tokens
        geography_tokens: set = set()
        for country in target_countries:
            geography_tokens.add(country.lower())
            if country.upper() == "US":
                geography_tokens.update({"united", "states", "usa", "us", "america"})
            iso3 = CountryResolver.to_iso3(country)
            if iso3:
                geography_tokens.add(iso3.lower())
            for alias, code in CountryResolver.COUNTRY_ALIASES.items():
                if code == country.upper():
                    for token in alias.split():
                        geography_tokens.add(token.lower())

        non_geography_tokens = [
            t for t in tokens
            if t not in _FILLER_WORDS and t not in geography_tokens
        ]
        if non_geography_tokens:
            return None

        # Determine additive vs. removal vs. replacement
        query_words = set(query_lower.split())
        is_removal = bool(query_words & _REMOVAL_MARKERS)
        is_additive = bool(query_words & _ADDITIVE_MARKERS) and not bool(
            query_words & _REPLACEMENT_MARKERS
        ) and not is_removal

        if is_removal:
            logger.info(
                "Delta: removal country follow-up %s",
                target_countries,
            )
            return FollowUpDelta(
                removed_countries=target_countries,
                raw_query=query,
                delta_type="country_change",
            )

        if is_additive:
            logger.info(
                "Delta: additive country follow-up %s",
                target_countries,
            )
            return FollowUpDelta(
                added_countries=target_countries,
                raw_query=query,
                delta_type="additive_country",
            )

        # Replacement
        if len(target_countries) == 1:
            logger.info(
                "Delta: country change → %s",
                target_countries[0],
            )
            return FollowUpDelta(
                changed_country=target_countries[0],
                raw_query=query,
                delta_type="country_change",
            )
        else:
            logger.info(
                "Delta: countries change → %s",
                target_countries,
            )
            return FollowUpDelta(
                changed_countries=target_countries,
                raw_query=query,
                delta_type="country_change",
            )

    @staticmethod
    def _indicator_token_set(text: Optional[str]) -> set[str]:
        tokens = {
            token
            for token in re.findall(r"[a-zA-Z]+", str(text or "").lower())
            if token not in _FILLER_WORDS and token not in _INDICATOR_NOISE_WORDS and len(token) > 1
        }
        expanded_tokens = set(tokens)
        for alias, phrase_variants in _INDICATOR_ALIAS_EQUIVALENTS.items():
            if alias in tokens:
                for phrase in phrase_variants:
                    expanded_tokens.update(phrase)
            if any(set(phrase).issubset(tokens) for phrase in phrase_variants):
                expanded_tokens.add(alias)
        tokens = expanded_tokens
        return tokens

    def _try_country_provider_follow_up_with_indicator_reaffirmation(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Detect follow-ups like 'Japan GDP from World Bank' structurally.

        This preserves the current semantic indicator while allowing explicit
        provider + geography rewrites to avoid a slower, error-prone LLM delta.
        """
        provider_match = _PROVIDER_SWITCH_RE.search(query)
        if not provider_match:
            return None

        stripped_query = _PROVIDER_SWITCH_RE.sub(" ", query)
        target_countries = CountryResolver.detect_all_countries_in_query(stripped_query)
        if not target_countries:
            return None

        query_lower = stripped_query.lower()
        tokens = re.findall(r"[a-zA-Z]+", query_lower)
        if not tokens:
            return None

        geography_tokens: set[str] = set()
        for country in target_countries:
            geography_tokens.add(country.lower())
            if country.upper() == "US":
                geography_tokens.update({"united", "states", "usa", "us", "america"})
            iso3 = CountryResolver.to_iso3(country)
            if iso3:
                geography_tokens.add(iso3.lower())
            for alias, code in CountryResolver.COUNTRY_ALIASES.items():
                if code == country.upper():
                    for token in alias.split():
                        geography_tokens.add(token.lower())

        content_tokens = [
            token for token in tokens
            if token not in _FILLER_WORDS and token not in geography_tokens
        ]
        if not content_tokens:
            return None

        current_indicator_tokens = self._indicator_token_set(state.indicator)
        for member in (state.active_answer_members or []):
            current_indicator_tokens.update(self._indicator_token_set(getattr(member, "indicator_label", None)))
        if not current_indicator_tokens:
            return None

        content_token_set = {
            token for token in content_tokens
            if token not in _INDICATOR_NOISE_WORDS and len(token) > 1
        }
        if not content_token_set or not content_token_set.issubset(current_indicator_tokens):
            return None

        from ..utils.providers import normalize_provider_name

        provider = normalize_provider_name(provider_match.group(1).strip())
        logger.info(
            "Delta: country+provider follow-up with indicator reaffirmation → provider=%s countries=%s",
            provider,
            target_countries,
        )
        if len(target_countries) == 1:
            return FollowUpDelta(
                changed_country=target_countries[0],
                changed_provider=provider,
                raw_query=query,
                delta_type="compound_change",
                query_type="parameter_delta",
            )
        return FollowUpDelta(
            changed_countries=target_countries,
            changed_provider=provider,
            raw_query=query,
            delta_type="compound_change",
            query_type="parameter_delta",
        )

    def _try_country_follow_up_with_indicator_reaffirmation(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Detect geography follow-ups that restate the current indicator variant.

        Examples:
        - "Add UK real GDP" after "US real GDP"
        - "Show Germany GDP per capita" after a GDP-per-capita chain

        This preserves the existing semantic indicator while allowing the user
        to restate it naturally, instead of forcing the turn through a fresh
        parse that can drift provider or country scope.
        """
        extracted_countries = CountryResolver.detect_all_countries_in_query(query)
        expanded_regions = CountryResolver.expand_regions_in_query(query)
        target_countries = extracted_countries or expanded_regions
        if not target_countries:
            return None

        query_lower = query.lower()
        tokens = re.findall(r"[a-zA-Z]+", query_lower)
        if not tokens:
            return None

        geography_tokens: set[str] = set()
        for country in target_countries:
            geography_tokens.add(country.lower())
            if country.upper() == "US":
                geography_tokens.update({"united", "states", "usa", "us", "america"})
            iso3 = CountryResolver.to_iso3(country)
            if iso3:
                geography_tokens.add(iso3.lower())
            for alias, code in CountryResolver.COUNTRY_ALIASES.items():
                if code == country.upper():
                    for token in alias.split():
                        geography_tokens.add(token.lower())

        content_tokens = [
            token for token in tokens
            if token not in _FILLER_WORDS and token not in geography_tokens
        ]
        if not content_tokens:
            return None

        current_indicator_tokens = self._indicator_token_set(state.indicator)
        for member in (state.active_answer_members or []):
            current_indicator_tokens.update(self._indicator_token_set(getattr(member, "indicator_label", None)))
        if not current_indicator_tokens:
            return None

        content_token_set = {
            token for token in content_tokens
            if token not in _INDICATOR_NOISE_WORDS and len(token) > 1
        }
        if not content_token_set or not content_token_set.issubset(current_indicator_tokens):
            return None

        query_words = set(query_lower.split())
        is_removal = bool(query_words & _REMOVAL_MARKERS)
        is_additive = bool(query_words & _ADDITIVE_MARKERS) and not bool(
            query_words & _REPLACEMENT_MARKERS
        ) and not is_removal

        if is_removal:
            return FollowUpDelta(
                removed_countries=target_countries,
                raw_query=query,
                delta_type="country_change",
            )
        if is_additive:
            return FollowUpDelta(
                added_countries=target_countries,
                raw_query=query,
                delta_type="additive_country",
            )
        if len(target_countries) == 1:
            return FollowUpDelta(
                changed_country=target_countries[0],
                raw_query=query,
                delta_type="country_change",
            )
        return FollowUpDelta(
            changed_countries=target_countries,
            raw_query=query,
            delta_type="country_change",
        )

    # ------------------------------------------------------------------
    # Handler: Indicator switch
    # ------------------------------------------------------------------

    def _try_indicator_switch(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Detect "what about inflation", "show unemployment" style follow-ups.

        Uses exclusion-based detection: if the query is short and all tokens
        are either filler words, country names (already handled above), or
        a new indicator term, then it is an indicator switch.  No hardcoded
        term set required.
        """
        query_lower = query.lower().strip()
        if not query_lower or len(query_lower) > 80:
            return None

        tokens = re.findall(r"[a-zA-Z]+", query_lower)
        if not tokens or len(tokens) > 10:
            return None

        # Check for explicit switch markers
        switch_markers = {
            "what about", "show", "how about", "switch to",
            "instead", "what is", "what's", "back to",
        }
        has_marker = any(marker in query_lower for marker in switch_markers)

        # Must NOT mention a new country (that is a country follow-up)
        extracted_countries = CountryResolver.detect_all_countries_in_query(query)
        if extracted_countries:
            return None

        query_words = set(query_lower.split())
        is_removal = bool(query_words & _REMOVAL_MARKERS)
        is_additive = bool(query_words & _ADDITIVE_MARKERS) and not bool(
            query_words & _REPLACEMENT_MARKERS
        ) and not is_removal

        detected_crypto_indicators: list[str] = []
        for alias, indicator_name in _CRYPTO_ASSET_ALIASES.items():
            if re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", query_lower):
                if indicator_name not in detected_crypto_indicators:
                    detected_crypto_indicators.append(indicator_name)
        if detected_crypto_indicators:
            if is_additive:
                logger.info(
                    "Delta: additive crypto indicators %s",
                    detected_crypto_indicators,
                )
                return FollowUpDelta(
                    added_indicators=detected_crypto_indicators,
                    raw_query=query,
                    delta_type="indicator_switch",
                )
            changed_indicator = detected_crypto_indicators[0]
            added_indicators = (
                detected_crypto_indicators[1:] if len(detected_crypto_indicators) > 1 else None
            )
            logger.info(
                "Delta: crypto indicator switch '%s' → '%s' (+%s)",
                state.indicator,
                changed_indicator,
                added_indicators,
            )
            return FollowUpDelta(
                changed_indicator=changed_indicator,
                added_indicators=added_indicators,
                raw_query=query,
                delta_type="indicator_switch",
            )

        return None

    # ------------------------------------------------------------------
    # Handler: Provider change
    # ------------------------------------------------------------------

    def _try_provider_change(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Detect "from FRED", "use IMF", "switch to Eurostat" patterns."""
        m = _PROVIDER_SWITCH_RE.search(query)
        if not m:
            return None

        provider_raw = m.group(1).strip()
        # Provider-only follow-ups are safe to handle structurally, but
        # richer utterances like "Japan GDP from World Bank" should fall back
        # to the LLM/action path so country + metric semantics are not lost.
        stripped_query = _PROVIDER_SWITCH_RE.sub(" ", query)
        residual_tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", stripped_query.lower())
            if token not in _FILLER_WORDS and token not in {"from", "use", "switch", "to", "via", "using", "through", "data"}
        ]
        if residual_tokens:
            return None
        # Normalize without importing QueryService (keeps extractor testable
        # even when optional runtime dependencies are unavailable).
        from ..utils.providers import normalize_provider_name
        provider = normalize_provider_name(provider_raw)

        current_provider = (state.provider or state.routed_provider or "").upper()
        if provider == current_provider:
            logger.info("Delta: provider reaffirmation → %s", provider)
        else:
            logger.info("Delta: provider change → %s", provider)
        return FollowUpDelta(
            changed_provider=provider,
            raw_query=query,
            delta_type="provider_change",
            query_type="parameter_delta",
        )

    # ------------------------------------------------------------------
    # Handler: Context-aware dimension modifier  (Phase 3)
    # ------------------------------------------------------------------

    def _try_dimension_modifier(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Detect dimension modifiers for StatsCan indicators.

        When the conversation state points to a StatsCan indicator that has
        dimensional tables (e.g., unemployment rate with sex/age/geography
        dimensions, or CPI with product dimensions), this handler checks
        whether the follow-up query contains a term that matches one of
        those dimension members.

        If a match is found the delta is ``added_dimensions`` (not
        ``changed_indicator``), which prevents misclassification.

        This handler is synchronous but wraps an async call to
        ``_get_cube_metadata`` via ``asyncio`` so it integrates with the
        sync ``extract()`` method.
        """
        # Only applies when current state is StatsCan
        current_provider = (
            state.provider or state.routed_provider or ""
        ).upper()
        if current_provider not in {"STATSCAN", "STATISTICS CANADA"}:
            return None

        # Need a base indicator in state
        if not state.indicator:
            return None

        # Access the StatsCan provider via the QueryService
        statscan = getattr(self._qs, "statscan_provider", None)
        if statscan is None:
            return None

        # Resolve the indicator key
        indicator_key = (
            state.base_indicator
            or state.indicator.upper().replace(" ", "_").replace("-", "_")
        )

        # Resolve product ID
        product_id: Optional[str] = str(getattr(state, "statscan_product_id", "") or "").strip() or None
        if product_id:
            product_id = statscan._normalize_metadata_product_id(product_id)
        else:
            # Only derive a product from exact provider-native IDs, never from
            # natural-language indicator aliases.
            digits = "".join(ch for ch in str(state.indicator or "") if ch.isdigit())
            if len(digits) in {8, 10}:
                product_id = statscan._normalize_metadata_product_id(digits)
            elif len(digits) >= 7:
                _cached = statscan.PRODUCT_ID_CACHE.get(int(digits))
                if _cached:
                    product_id = statscan._normalize_metadata_product_id(_cached)

        if not product_id:
            return None

        # Use cached cube metadata from conversation state (pre-populated
        # after first successful StatsCan query — no async API call needed).
        # Fall back to async fetch in a thread if cache is missing.
        cube_metadata = state.statscan_cube_metadata
        if not cube_metadata:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        cube_metadata = pool.submit(
                            lambda: asyncio.run(
                                statscan._get_cube_metadata(product_id)
                            )
                        ).result(timeout=10)
                else:
                    cube_metadata = loop.run_until_complete(
                        statscan._get_cube_metadata(product_id)
                    )
            except Exception as exc:
                logger.debug(
                    "Dimension modifier: failed to fetch cube metadata for %s: %s",
                    product_id, exc,
                )
                return None

        if not cube_metadata:
            return None

        # Use the provider's extract_dimension_modifiers
        modifiers: Dict[str, str] = statscan.extract_dimension_modifiers(
            query_text=query,
            base_indicator=indicator_key,
            product_id=product_id,
            cube_metadata=cube_metadata,
        )

        if not modifiers:
            return None

        logger.info(
            "Delta: dimension modifier for %s → %s",
            indicator_key,
            modifiers,
        )
        return FollowUpDelta(
            added_dimensions=modifiers,
            is_dimension_modifier_change=True,
            raw_query=query,
            delta_type="dimension_change",
        )

    # ------------------------------------------------------------------
    # Handler: Time change
    # ------------------------------------------------------------------

    def _try_time_change(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Detect time-period follow-ups like "last 20 years", "since 2010"."""
        query_lower = query.lower().strip()
        if not query_lower:
            return None

        # Only match if query is very short and mostly about time AND any
        # residual content token restates the current indicator (otherwise this
        # is an indicator switch + time modifier — see F2).
        if not self._looks_like_pure_time_change_query(query, state):
            return None

        # Try "from YYYY to YYYY"
        m = _TIME_FROM_TO_RE.search(query)
        if m:
            start_year = m.group(1)
            end_year = m.group(2)
            logger.info("Delta: time change → %s to %s", start_year, end_year)
            return FollowUpDelta(
                changed_start_date=f"{start_year}-01-01",
                changed_end_date=f"{end_year}-12-31",
                raw_query=query,
                delta_type="time_change",
            )

        # Try "YYYY-YYYY" / "YYYY–YYYY" (range with hyphen/dash)
        m = _TIME_RANGE_RE.search(query)
        if m:
            start_year = m.group(1)
            end_year = m.group(2)
            logger.info("Delta: time change → %s-%s", start_year, end_year)
            return FollowUpDelta(
                changed_start_date=f"{start_year}-01-01",
                changed_end_date=f"{end_year}-12-31",
                raw_query=query,
                delta_type="time_change",
            )

        # Try "since YYYY"
        m = _TIME_SINCE_RE.search(query)
        if m:
            start_year = m.group(1)
            logger.info("Delta: time change → since %s", start_year)
            return FollowUpDelta(
                changed_start_date=f"{start_year}-01-01",
                raw_query=query,
                delta_type="time_change",
            )

        # Try "last N days"
        m = _TIME_LAST_N_DAYS_RE.search(query)
        if m:
            from datetime import datetime, timedelta

            n = int(m.group(1))
            now = datetime.now().date()
            start_dt = now - timedelta(days=n)
            logger.info("Delta: time change → last %d days", n)
            return FollowUpDelta(
                changed_start_date=start_dt.isoformat(),
                changed_end_date=now.isoformat(),
                raw_query=query,
                delta_type="time_change",
            )

        # Try "last N years/months/quarters/decades"
        m = _TIME_LAST_N_RE.search(query)
        if m:
            from datetime import datetime, timedelta
            n = int(m.group(1))
            unit = m.group(2).lower().rstrip("s")
            now = datetime.now().date()
            if unit == "year":
                logger.info("Delta: time change → last %d years", n)
                return FollowUpDelta(
                    changed_start_date=(now - timedelta(days=365 * n)).isoformat(),
                    changed_end_date=now.isoformat(),
                    raw_query=query,
                    delta_type="time_change",
                )
            if unit == "month":
                logger.info("Delta: time change → last %d months", n)
                return FollowUpDelta(
                    changed_start_date=(now - timedelta(days=30 * n)).isoformat(),
                    changed_end_date=now.isoformat(),
                    raw_query=query,
                    delta_type="time_change",
                )
            if unit == "quarter":
                logger.info("Delta: time change → last %d quarters", n)
                return FollowUpDelta(
                    changed_start_date=(now - timedelta(days=91 * n)).isoformat(),
                    changed_end_date=now.isoformat(),
                    raw_query=query,
                    delta_type="time_change",
                )
            if unit == "decade":
                logger.info("Delta: time change → last %d decades", n)
                return FollowUpDelta(
                    changed_start_date=(now - timedelta(days=3652 * n)).isoformat(),
                    changed_end_date=now.isoformat(),
                    raw_query=query,
                    delta_type="time_change",
                )

        # Try singular windows like "last year"
        m = _TIME_LAST_SIMPLE_RE.search(query)
        if m:
            from datetime import datetime, timedelta

            unit = m.group(1).lower()
            now = datetime.now().date()
            delta_days = {
                "day": 1,
                "week": 7,
                "month": 30,
                "quarter": 91,
                "year": 365,
            }[unit]
            logger.info("Delta: time change → last %s", unit)
            return FollowUpDelta(
                changed_start_date=(now - timedelta(days=delta_days)).isoformat(),
                changed_end_date=now.isoformat(),
                raw_query=query,
                delta_type="time_change",
            )

        return None

    def _try_frequency_change(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Detect pure frequency follow-ups like 'change to monthly frequency'."""
        match = _FREQUENCY_CHANGE_RE.search(query)
        if not match:
            return None

        query_lower = str(query or "").lower()
        stripped = _FREQUENCY_CHANGE_RE.sub(" ", query_lower)
        residual_tokens = [
            token
            for token in re.findall(r"[a-z0-9]+", stripped)
            if token not in _FILLER_WORDS and token not in {"frequency", "data", "change", "switch", "set"}
        ]
        if residual_tokens:
            return None

        frequency = self._normalize_frequency(match.group(1))
        if frequency == str(state.frequency or "").strip().lower():
            return None

        logger.info("Delta: frequency change → %s", frequency)
        return FollowUpDelta(
            changed_frequency=frequency,
            raw_query=query,
            delta_type="parameter_change",
            query_type="parameter_delta",
        )

    def _try_breakdown_follow_up(
        self,
        query: str,
        state: ConversationState,
    ) -> Optional[FollowUpDelta]:
        """Detect simple breakdown follow-ups without sending them to the LLM."""
        query_lower = str(query or "").strip().lower()
        if not query_lower:
            return None

        breakdown_map = [
            (re.compile(r"\b(?:show|break\s+down|group)\s+(?:it\s+)?by\s+province(?:s)?\b"), {"type": "provinces", "entities": None, "axis": "Geography"}),
            (re.compile(r"\b(?:show|break\s+down|group)\s+(?:it\s+)?by\s+state(?:s)?\b"), {"type": "states", "entities": None, "axis": "Geography"}),
            (re.compile(r"\b(?:show|break\s+down|group)\s+(?:it\s+)?by\s+region(?:s)?\b"), {"type": "regions", "entities": None, "axis": "Geography"}),
            (re.compile(r"\b(?:show|break\s+down|group)\s+(?:it\s+)?by\s+country\b"), {"type": "countries", "entities": None, "axis": "Geography"}),
            (re.compile(r"\b(?:show|break\s+down|group)\s+(?:it\s+)?by\s+(?:age|age group|age groups)\b"), {"type": "dimension", "entities": None, "axis": "Age group"}),
            (re.compile(r"\b(?:show|break\s+down|group)\s+(?:it\s+)?by\s+(?:sex|gender)\b"), {"type": "dimension", "entities": None, "axis": "Gender"}),
        ]
        for pattern, payload in breakdown_map:
            if pattern.search(query_lower):
                logger.info("Delta: decomposition breakdown → %s", payload)
                return FollowUpDelta(
                    changed_decomposition=dict(payload),
                    is_dimension_modifier_change=True,
                    raw_query=query,
                    delta_type="dimension_change",
                    query_type="parameter_delta",
                )

        return None
