"""Indicator clarification logic for ambiguous or uncertain query results.

Extracted from query.py to reduce file size and isolate the indicator
disambiguation system into a testable, reusable module.

This module provides:
- Indicator option formatting, deduplication, and parsing
- Pending indicator-choice state management
- Structured and free-text option matching
- Uncertain/ambiguous result detection and clarification building
- Multi-concept query disambiguation
- Group-scope (region) clarification
- Provider-country compatibility checks for options
- Informational / metadata query handling
- Semantic discriminator verification
- Pre-fetch and post-fetch indicator choice flows
"""

from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ..models import (
    ClarificationOption,
    NormalizedData,
    ParsedIntent,
    QueryResponse,
)
from ..routing.country_resolver import CountryResolver
from ..services.conversation import conversation_manager
# Explicit candidate-evidence / decision scaffolding for Phase 1.
from ..services.candidate_evidence_builder import CandidateEvidenceBuilder, build_candidate_id
from ..services.parameter_validator import ParameterValidator
from ..services.relevance_scorer import (
    extract_indicator_cues as _extract_indicator_cues,
    has_directional_conflict as _has_directional_conflict,
    score_series_relevance as _score_series_relevance,
    series_text_for_relevance as _series_text_for_relevance,
    single_directional_cue as _single_directional_cue,
    specific_cues_compatible as _specific_cues_compatible,
)
from ..services.indicator_resolution import (
    is_comparison_query as _is_comparison_query_standalone,
    is_exact_match_locked as _is_exact_match_locked,
    is_placeholder_indicator_code as _is_placeholder_indicator_code,
    is_resolved_indicator_plausible as _is_resolved_indicator_plausible,
)
from ..utils.processing_steps import (
    ProcessingTracker,
    activate_processing_tracker,
    get_processing_tracker,
    reset_processing_tracker,
)
from ..utils.retry import retry_async
from .semantic_match_judge import decide_prefetch_outcome

if TYPE_CHECKING:
    from ..services.query_pipeline import ParseRouteResult, ValidationResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pre-compiled regex patterns (shared across functions)
# ---------------------------------------------------------------------------

_OPTION_PROVIDER_PREFIX_RE = re.compile(r"^\[[^\]]+\]\s*")
_OPTION_TRAILING_PARENS_RE = re.compile(r"\s*\([^()]+\)\s*$")
_OPTION_TRAILING_PARENS_ALT_RE = re.compile(r"\([^()]*\)\s*$")
_OPTION_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_provider_name(provider: str) -> str:
    """Normalize provider name to uppercase canonical form."""
    from ..utils.providers import normalize_provider_name as _npn
    return _npn(provider)


def _use_outcome_decision_stage(qs: Any) -> bool:
    """Return whether the Phase 1 prefetch outcome-decision stage is enabled."""
    settings = getattr(qs, "settings", None)
    return bool(getattr(settings, "use_outcome_decision_stage", False))


# ====================================================================
# Indicator option formatting / parsing
# ====================================================================

def format_indicator_option_name(
    qs: Any,
    provider: str,
    code: str,
    name: Optional[str],
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Build a user-facing indicator label for clarification options.

    Prefers human-readable names/descriptions over opaque provider codes.
    """
    provider_norm = normalize_provider_name(provider)
    code_text = str(code or "").strip()
    candidate_name = str(name or "").strip()
    metadata_dict = metadata or {}

    description = str(metadata_dict.get("description") or "").strip()
    if (
        (not candidate_name or looks_like_provider_indicator_code(provider_norm, candidate_name))
        and description
        and not looks_like_provider_indicator_code(provider_norm, description)
    ):
        candidate_name = description

    if provider_norm == "BIS" and code_text:
        flow_name, flow_description = qs.bis_provider._lookup_dataflow_info(code_text.upper())
        if flow_name and (not candidate_name or looks_like_provider_indicator_code(provider_norm, candidate_name)):
            candidate_name = str(flow_name).strip()
        elif flow_description and not candidate_name:
            candidate_name = str(flow_description).strip()

    if not candidate_name:
        candidate_name = code_text or "Unknown indicator"

    return re.sub(r"\s+", " ", candidate_name.replace("_", " ")).strip()


def dedupe_indicator_choice_options(qs: Any, options: List[str]) -> List[str]:
    """De-duplicate clarification options while preserving order.

    Removes:
    - options without parseable provider/code
    - placeholder/non-actionable codes (for example N/A, DYNAMIC)
    - near-duplicate entries from the same provider with equivalent label text
    """
    deduped: List[str] = []
    seen_by_code: set[tuple[str, str]] = set()
    seen_by_label: set[tuple[str, str]] = set()

    for option in options:
        option_text = str(option or "").strip()
        if not option_text:
            continue

        parsed = parse_indicator_option(option_text)
        if not parsed:
            continue

        provider, code = parsed
        code_upper = str(code).strip().upper()
        if _is_placeholder_indicator_code(code_upper):
            continue

        code_key = (provider, code_upper)
        if code_key in seen_by_code:
            continue

        option_body = _OPTION_PROVIDER_PREFIX_RE.sub("", option_text)
        option_label = _OPTION_TRAILING_PARENS_ALT_RE.sub("", option_body).strip().lower()
        option_label = _OPTION_NON_ALNUM_RE.sub(" ", option_label).strip()
        label_key = (provider, option_label)

        if option_label and label_key in seen_by_label:
            continue

        seen_by_code.add(code_key)
        if option_label:
            seen_by_label.add(label_key)
        deduped.append(option_text)

    return deduped


def parse_indicator_option(option: str) -> Optional[tuple[str, str]]:
    """Parse one option string like '[IMF] Indicator name (CODE)'."""
    text = str(option or "").strip()
    if not text:
        return None
    match = re.search(r"^\s*\[([^\]]+)\].*?\(([^()]+)\)\s*$", text)
    if not match:
        return None
    provider = normalize_provider_name(match.group(1))
    code = str(match.group(2) or "").strip()
    if not provider or not code:
        return None
    return provider, code


# ====================================================================
# Pending indicator state management
# ====================================================================

def store_pending_indicator_options(
    qs: Any,
    conversation_id: str,
    query: str,
    intent: ParsedIntent,
    options: List[str],
    question_lines: Optional[List[str]] = None,
) -> None:
    """Persist indicator-choice clarification options for follow-up turns."""
    if not conversation_id or not options:
        return

    clean_options = dedupe_indicator_choice_options(qs, options)
    if not clean_options:
        return

    payload = {
        "original_query": str(query or "").strip() or str(intent.originalQuery or "").strip(),
        "intent": intent.model_dump() if hasattr(intent, "model_dump") else None,
        "options": [str(option) for option in clean_options if str(option).strip()],
        "question_lines": [str(line) for line in (question_lines or []) if str(line).strip()],
    }
    try:
        conversation_manager.set_pending_indicator_options(conversation_id, payload)
    except Exception as exc:
        logger.debug("Failed to store pending indicator options: %s", exc)


def store_pending_semantic_clarification(
    conversation_id: str,
    payload: Dict[str, Any],
) -> None:
    """Persist semantic clarification state for a follow-up turn."""
    if not conversation_id or not payload:
        return
    try:
        conversation_manager.set_pending_semantic_clarification(conversation_id, payload)
    except Exception as exc:
        logger.debug("Failed to store pending semantic clarification: %s", exc)


# ====================================================================
# Clarification option building / matching
# ====================================================================

def build_clarification_options(
    qs: Any,
    options: Optional[List[str]],
) -> Optional[List[ClarificationOption]]:
    """Convert raw option strings into structured clarification choices."""
    clean_options = dedupe_indicator_choice_options(
        qs,
        [str(option) for option in (options or []) if str(option).strip()],
    )
    if not clean_options:
        return None

    structured_options: List[ClarificationOption] = []
    for idx, option_text in enumerate(clean_options, start=1):
        provider = None
        code = None
        label = option_text

        parsed = parse_indicator_option(option_text)
        if parsed:
            provider, code = parsed
            option_body = _OPTION_PROVIDER_PREFIX_RE.sub("", option_text).strip()
            label = _OPTION_TRAILING_PARENS_RE.sub("", option_body).strip() or option_body or option_text

        structured_options.append(
            ClarificationOption(
                id=str(idx),
                label=label,
                value=option_text,
                provider=provider,
                code=code,
            )
        )

    return structured_options


def indicator_option_label_key(option: str) -> Optional[str]:
    """Build a provider-agnostic label key for one indicator option."""
    option_text = str(option or "").strip()
    if not option_text:
        return None
    option_body = _OPTION_PROVIDER_PREFIX_RE.sub("", option_text).strip()
    label = _OPTION_TRAILING_PARENS_RE.sub("", option_body).strip() or option_body
    label_key = _OPTION_NON_ALNUM_RE.sub(" ", label.lower()).strip()
    return label_key or None


def has_materially_distinct_indicator_options(qs: Any, options: Optional[List[str]]) -> bool:
    """Return True when indicator-choice options differ by more than provider/code.

    Prefetch clarification should only interrupt execution when the user is
    choosing between genuinely different indicator meanings.
    """
    clean_options = dedupe_indicator_choice_options(
        qs,
        [str(option) for option in (options or []) if str(option).strip()],
    )
    label_keys = {
        label_key
        for option in clean_options
        if (label_key := indicator_option_label_key(option))
    }
    return len(label_keys) >= 2


def match_structured_clarification_option(
    user_query: str,
    options: List[ClarificationOption],
) -> Optional[ClarificationOption]:
    """Match a user reply against structured clarification options."""
    text = str(user_query or "").strip()
    if not text or not options:
        return None

    numeric_patterns = [
        r"^\s*(\d{1,2})\s*$",
        r"^\s*(?:option|choose|pick|select)\s*(\d{1,2})\s*$",
        r"^\s*#\s*(\d{1,2})\s*$",
    ]
    numeric = None
    for pattern in numeric_patterns:
        numeric = re.fullmatch(pattern, text.lower())
        if numeric:
            break
    if not numeric:
        ordinal_map = {
            "first": 1,
            "second": 2,
            "third": 3,
            "fourth": 4,
            "fifth": 5,
        }
        ordinal_value = ordinal_map.get(text.lower().strip())
        if ordinal_value is not None:
            numeric = re.match(r"(\d+)", str(ordinal_value))
    if numeric:
        idx = int(numeric.group(1)) - 1
        if 0 <= idx < len(options):
            return options[idx]
        return None

    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    for option in options:
        option_label = re.sub(r"\s+", " ", str(option.label or "").lower()).strip()
        option_value = re.sub(r"\s+", " ", str(option.value or "").lower()).strip()
        if normalized in {option_label, option_value}:
            return option
        if len(normalized) >= 4 and (
            normalized in option_label
            or normalized in option_value
        ):
            return option

    return None


def match_indicator_choice_option(user_query: str, options: List[str]) -> Optional[str]:
    """Match a user follow-up response against stored clarification options."""
    text = str(user_query or "").strip()
    if not text or not options:
        return None

    numeric_patterns = [
        r"^\s*(\d{1,2})\s*$",
        r"^\s*(?:option|choose|pick|select)\s*(\d{1,2})\s*$",
        r"^\s*#\s*(\d{1,2})\s*$",
    ]
    numeric = None
    for pattern in numeric_patterns:
        numeric = re.fullmatch(pattern, text.lower())
        if numeric:
            break
    if not numeric:
        ordinal_map = {
            "first": 1,
            "second": 2,
            "third": 3,
            "fourth": 4,
            "fifth": 5,
        }
        ordinal_value = ordinal_map.get(text.lower().strip())
        if ordinal_value is not None:
            numeric = re.match(r"(\d+)", str(ordinal_value))
    if numeric:
        idx = int(numeric.group(1)) - 1
        if 0 <= idx < len(options):
            return options[idx]
        return None

    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    for option in options:
        option_text = str(option or "").strip()
        if not option_text:
            continue

        option_lower = re.sub(r"\s+", " ", option_text.lower()).strip()
        option_body = _OPTION_PROVIDER_PREFIX_RE.sub("", option_text).strip()
        option_body_lower = re.sub(r"\s+", " ", option_body.lower()).strip()

        if normalized in {option_lower, option_body_lower}:
            return option_text

        parsed = parse_indicator_option(option_text)
        if parsed and normalized == parsed[1].lower():
            return option_text

        if len(normalized) >= 6 and normalized in option_body_lower:
            return option_text

    return None


# ====================================================================
# Pending indicator-choice resolution
# ====================================================================

async def try_resolve_pending_indicator_choice(
    qs: Any,
    query: str,
    conversation_id: str,
    tracker: Optional[ProcessingTracker] = None,
) -> Optional[QueryResponse]:
    """Apply a pending indicator-choice clarification when the user replies
    with an option number or indicator text.
    """
    pending = conversation_manager.get_pending_indicator_options(conversation_id)
    if not pending:
        return None

    raw_options = [str(option) for option in (pending.get("options") or []) if str(option).strip()]
    options = dedupe_indicator_choice_options(qs, raw_options)
    if not options:
        conversation_manager.clear_pending_indicator_options(conversation_id)
        return None

    selected_option = match_indicator_choice_option(query, options)
    if not selected_option:
        text = str(query or "").strip()
        if re.fullmatch(r"\d{1,2}", text):
            return QueryResponse(
                conversationId=conversation_id,
                clarificationNeeded=True,
                clarificationQuestions=pending.get("question_lines") or [],
                clarificationOptions=build_clarification_options(qs, options),
                message="Please choose one of the listed option numbers.",
                processingSteps=tracker.to_list() if tracker else None,
            )

        if len(text.split()) >= 3:
            conversation_manager.clear_pending_indicator_options(conversation_id)
        return None

    parsed = parse_indicator_option(selected_option)
    if not parsed:
        conversation_manager.clear_pending_indicator_options(conversation_id)
        return None

    selected_provider, selected_code = parsed
    original_query = str(pending.get("original_query") or "").strip()
    raw_intent = pending.get("intent")
    intent = qs._coerce_parsed_intent(raw_intent, original_query or query)
    if not intent:
        conversation_manager.clear_pending_indicator_options(conversation_id)
        return None

    conversation_id = conversation_manager.add_message_safe(conversation_id, "user", query)

    intent.apiProvider = selected_provider
    intent.indicators = [selected_code]
    intent.clarificationNeeded = False
    intent.clarificationQuestions = []
    if not intent.originalQuery:
        intent.originalQuery = original_query or query

    intent.isFollowUp = True
    intent.followUpType = "clarification_answer"
    intent.resolvedQuery = original_query or query

    params = dict(intent.parameters or {})
    params.pop("seriesId", None)
    params.pop("series_id", None)
    params.pop("code", None)
    params["indicator"] = selected_code
    intent.parameters = params

    try:
        if tracker:
            with tracker.track(
                "clarification_selection",
                "Applying your indicator selection...",
                {"provider": selected_provider, "indicator": selected_code},
            ):
                data = await retry_async(
                    lambda: qs._fetch_data(intent),
                    max_attempts=2,
                    initial_delay=0.3,
                )
        else:
            data = await retry_async(
                lambda: qs._fetch_data(intent),
                max_attempts=2,
                initial_delay=0.3,
            )
    except Exception as exc:
        return build_failed_indicator_choice_response(
            qs=qs,
            conversation_id=conversation_id,
            query=original_query or query,
            intent=intent,
            options=options,
            selected_option=selected_option,
            question_lines=pending.get("question_lines") or [],
            tracker=tracker,
            error=str(exc),
        )

    if not data:
        return build_failed_indicator_choice_response(
            qs=qs,
            conversation_id=conversation_id,
            query=original_query or query,
            intent=intent,
            options=options,
            selected_option=selected_option,
            question_lines=pending.get("question_lines") or [],
            tracker=tracker,
        )

    data = qs._rerank_data_by_query_relevance(intent.originalQuery or query, data)
    if qs._is_ranking_query(intent.originalQuery or query):
        data = qs._apply_ranking_projection(intent.originalQuery or query, data)

    recovered_data = await maybe_recover_from_uncertain_match(
        qs,
        intent.originalQuery or query,
        intent,
        data,
    )
    if recovered_data:
        data = recovered_data

    clarification_response = build_uncertain_result_clarification(
        qs=qs,
        conversation_id=conversation_id,
        query=intent.originalQuery or query,
        intent=intent,
        data=data,
        processing_steps=tracker.to_list() if tracker else None,
    )
    if clarification_response:
        return clarification_response

    conversation_id = conversation_manager.add_message_safe(
        conversation_id,
        "assistant",
        f"Retrieved data for selected indicator {selected_code}.",
        intent=intent,
    )
    conversation_manager.clear_pending_indicator_options(conversation_id)

    return QueryResponse(
        conversationId=conversation_id,
        intent=intent,
        data=data,
        clarificationNeeded=False,
        processingSteps=tracker.to_list() if tracker else None,
    )


# ====================================================================
# Uncertain match recovery
# ====================================================================

async def maybe_recover_from_uncertain_match(
    qs: Any,
    query: str,
    intent: Optional[ParsedIntent],
    data: List[NormalizedData],
) -> Optional[List[NormalizedData]]:
    """Try one automatic refetch when current top match looks uncertain.

    This is a framework-level recovery step before asking the user to pick
    from options.
    """
    if not intent or not data:
        return None
    if intent.indicators and len(intent.indicators) > 1:
        return None

    params = dict(intent.parameters or {})
    if params.get("_uncertain_recovery_attempted"):
        return None
    if _is_exact_match_locked(params):
        return None
    if not qs._needs_indicator_clarification(query, data, intent):
        return None

    top_series = data[0]
    current_score = _score_series_relevance(query, top_series)
    top_provider, top_code = qs._extract_series_provider_and_code(top_series)

    target_countries = qs._collect_target_countries(params)
    target_country = target_countries[0] if target_countries else None
    indicator_query = qs._select_indicator_query_for_resolution(intent) or query
    primary_provider = normalize_provider_name(intent.apiProvider or "")
    explicit_provider = normalize_provider_name(qs._detect_explicit_provider(intent.originalQuery or "") or "")

    candidate_keys: set[tuple[str, str]] = set()
    candidate_ordered: List[tuple[str, str]] = []

    def _add_candidate(provider_name: str, code: str) -> None:
        provider_norm = normalize_provider_name(provider_name or "")
        code_norm = str(code or "").strip()
        if not provider_norm or not code_norm:
            return
        key = (provider_norm, code_norm.upper())
        if key in candidate_keys:
            return
        candidate_keys.add(key)
        candidate_ordered.append((provider_norm, code_norm))

    for option in qs._collect_indicator_choice_options(query, intent, max_options=4):
        parsed = parse_indicator_option(option)
        if parsed:
            _add_candidate(parsed[0], parsed[1])

    best_data: Optional[List[NormalizedData]] = None
    best_score = current_score

    for provider_name, code in candidate_ordered:
        if top_provider and top_code and provider_name == top_provider and code.upper() == top_code.upper():
            continue
        if explicit_provider and provider_name != explicit_provider:
            continue

        attempt_intent = intent.model_copy(deep=True)
        attempt_intent.apiProvider = provider_name
        attempt_intent.indicators = [code]
        attempt_params = dict(attempt_intent.parameters or {})
        attempt_params["_uncertain_recovery_attempted"] = True
        attempt_params.pop("seriesId", None)
        attempt_params.pop("series_id", None)
        attempt_params.pop("code", None)
        attempt_params["indicator"] = code
        attempt_intent.parameters = attempt_params

        try:
            candidate_data = await retry_async(
                lambda i=attempt_intent: qs._fetch_data(i),
                max_attempts=2,
                initial_delay=0.4,
            )
        except Exception:
            continue
        if not candidate_data:
            continue

        candidate_data = qs._rerank_data_by_query_relevance(query, candidate_data)
        if qs._is_ranking_query(query):
            candidate_data = qs._apply_ranking_projection(query, candidate_data)
        if not candidate_data:
            continue
        if qs._has_implausible_top_series(query, candidate_data):
            continue

        candidate_score = _score_series_relevance(query, candidate_data[0])
        candidate_uncertain = qs._needs_indicator_clarification(query, candidate_data, attempt_intent)
        if (
            (not candidate_uncertain and candidate_score >= (best_score + 0.10))
            or candidate_score >= (best_score + 0.35)
        ):
            best_data = candidate_data
            best_score = candidate_score

    return best_data


# ====================================================================
# Provider country/scope compatibility
# ====================================================================

def provider_supports_country_for_options(provider: str, country_iso2: Optional[str]) -> bool:
    """Lightweight country-coverage filter for clarification options."""
    if not country_iso2:
        return True

    from ..providers.bis import BISProvider

    provider_upper = normalize_provider_name(provider)
    iso2 = country_iso2.upper()

    if provider_upper == "EUROSTAT":
        return CountryResolver.is_eu_member(iso2)
    if provider_upper in {"STATSCAN", "STATISTICS CANADA"}:
        return iso2 == "CA"
    if provider_upper == "FRED":
        return iso2 == "US"
    if provider_upper == "BIS":
        return iso2 in BISProvider.BIS_SUPPORTED_COUNTRIES
    return True


def provider_supports_requested_scope(
    provider: str,
    query: str,
    countries: Optional[List[str]],
) -> bool:
    """Filter options that are incompatible with the requested comparison scope.

    Delegates to :func:`provider_strategy.provider_supports_requested_scope`.
    """
    from .provider_strategy import provider_supports_requested_scope as _ps_fn

    return _ps_fn(provider, query, countries)


def apply_indicator_option_to_intent(intent: ParsedIntent, option_text: str) -> bool:
    """Apply one indicator-choice option directly onto an existing intent."""
    parsed = parse_indicator_option(option_text)
    if not parsed:
        return False

    provider_name, code = parsed
    intent.apiProvider = provider_name
    intent.indicators = [code]
    intent.clarificationNeeded = False
    intent.clarificationQuestions = []

    params = dict(intent.parameters or {})
    params.pop("seriesId", None)
    params.pop("series_id", None)
    params.pop("code", None)
    params["indicator"] = code
    intent.parameters = params
    return True


def provider_can_execute_indicator_option(
    qs: Any,
    provider: str,
    code: str,
    option_name: Optional[str] = None,
) -> bool:
    """Cheap provider-side executability guard for clarification options."""
    provider_upper = normalize_provider_name(provider)
    code_text = str(code or "").strip()
    option_label = str(option_name or "").strip()
    if not code_text:
        return False

    if provider_upper == "IMF":
        entry = qs.imf_provider._indicator_catalog_entry(code_text)
        if not entry:
            return False
        exact_code = str(entry.get("code") or code_text).strip().upper()
        has_namespace = (
            "_" in exact_code
            or "." in exact_code
            or any(ch.isdigit() for ch in exact_code)
        )
        is_weo_short_code = str(entry.get("category") or "").strip().upper() == "WEO"
        return bool(has_namespace or is_weo_short_code)

    return True


# ====================================================================
# Response builders
# ====================================================================

def build_no_reliable_indicator_match_response(
    conversation_id: str,
    intent: ParsedIntent,
    query: str,
    qs: Any,
    processing_steps: Optional[List[Any]] = None,
) -> QueryResponse:
    """Return a clarification instead of guessing when no reliable option remains."""
    query_text = str(query or intent.originalQuery or "").strip()
    target_countries = qs._collect_target_countries(intent.parameters)
    questions = [
        "I could not find a reliable indicator and provider combination for this request without guessing.",
    ]
    if target_countries and len(target_countries) > 8 and qs._is_comparison_query(query_text):
        questions.append(
            "Large cross-country comparisons are the hardest case here, and the current metric is not reliably available across the full scope."
        )
        questions.append(
            "Try a smaller country set, ask for one overall group value, or choose a different metric wording."
        )
    else:
        questions.append(
            "Please rephrase with a more specific metric, narrower geography, or different provider scope."
        )

    return QueryResponse(
        conversationId=conversation_id,
        intent=intent,
        clarificationNeeded=True,
        clarificationQuestions=questions,
        message="I stopped before fetching because the available matches were not reliable enough.",
        processingSteps=processing_steps,
    )


# ====================================================================
# Indicator option collection
# ====================================================================

def collect_indicator_choice_options(
    qs: Any,
    query: str,
    intent: ParsedIntent,
    max_options: int = 3,
) -> List[str]:
    """Build ranked indicator options across plausible providers for user clarification."""
    if not intent:
        return []

    from ..services.provider_fallback import (
        provider_covers_country_list as _provider_covers_country_list,
    )

    raw_query = str(query or "").strip()
    indicator_query = qs._select_indicator_query_for_resolution(intent) or raw_query
    if raw_query:
        raw_cues = _extract_indicator_cues(raw_query)
        indicator_cues = _extract_indicator_cues(indicator_query)
        high_signal_raw_cues = {
            cue for cue in raw_cues
            if cue not in {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
        }
        if (
            ("debt_gdp_ratio" in raw_cues and "debt_gdp_ratio" not in indicator_cues)
            or (high_signal_raw_cues and not (high_signal_raw_cues & indicator_cues))
        ):
            indicator_query = raw_query
    if not indicator_query:
        return []

    query_cues = _extract_indicator_cues(raw_query or indicator_query)
    high_signal_query_cues = {
        cue for cue in query_cues
        if cue not in {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
    }

    target_countries = qs._collect_target_countries(intent.parameters)
    if not target_countries and raw_query:
        target_countries = qs._extract_countries_from_query(raw_query)
    target_country = target_countries[0] if target_countries else None
    target_iso2 = [
        iso2
        for iso2 in (qs._normalize_country_to_iso2(country) for country in target_countries)
        if iso2
    ]

    primary_provider = normalize_provider_name(intent.apiProvider or "")
    fallback_candidates = qs._get_fallback_providers(
        primary_provider,
        indicator_query,
        country=target_country,
        countries=target_countries,
    )

    provider_candidates = []
    for provider_name in [
        primary_provider,
        *fallback_candidates,
        "IMF",
        "WORLDBANK",
        "BIS",
        "OECD",
        "EUROSTAT",
        "FRED",
    ]:
        normalized = normalize_provider_name(provider_name)
        if normalized and normalized not in provider_candidates:
            provider_candidates.append(normalized)

    from .indicator_selector import IndicatorSelector

    selector = IndicatorSelector()
    scored_options: List[tuple[float, str, str]] = []
    seen_codes: set[tuple[str, str]] = set()
    provider_labels = {
        "WORLDBANK": "WorldBank",
        "EUROSTAT": "Eurostat",
        "STATSCAN": "StatsCan",
        "COMTRADE": "Comtrade",
    }

    for provider_name in provider_candidates:
        if target_iso2 and not _provider_covers_country_list(provider_name, target_iso2):
            continue
        if target_iso2 and not all(
            provider_supports_country_for_options(provider_name, iso2)
            for iso2 in target_iso2
        ):
            continue
        if not provider_supports_requested_scope(
            provider_name,
            raw_query or indicator_query,
            target_iso2,
        ):
            continue

        try:
            candidates, scores = selector._get_candidates_with_scores(  # pylint: disable=protected-access
                indicator_query,
                provider_name,
                top_k=max(max_options * 3, 8),
            )
        except Exception as exc:
            logger.debug(
                "IndicatorSelector candidate retrieval failed for %s/%s: %s",
                provider_name,
                indicator_query,
                exc,
            )
            continue

        for idx, candidate in enumerate(candidates[: max(max_options * 2, 6)]):
            if len(candidate) < 2:
                continue
            code, name = candidate[0], candidate[1]
            code_text = str(code or "").strip()
            name_text = str(name or "").strip()
            if not code_text or _is_placeholder_indicator_code(code_text):
                continue
            if not _is_resolved_indicator_plausible(
                qs,
                provider_name,
                raw_query or indicator_query,
                code_text,
                name_text,
            ):
                continue
            if not provider_can_execute_indicator_option(
                qs=qs,
                provider=provider_name,
                code=code_text,
                option_name=name_text,
            ):
                continue

            code_key = (provider_name, code_text.upper())
            if code_key in seen_codes:
                continue
            seen_codes.add(code_key)

            synthetic_series = {
                "metadata": {
                    "indicator": name_text,
                    "seriesId": code_text,
                    "source": provider_name,
                }
            }
            relevance_score = _score_series_relevance(query, synthetic_series)
            if relevance_score < -0.5:
                continue

            option_cues = _extract_indicator_cues(f"{name_text} {code_text}")
            if high_signal_query_cues and not _specific_cues_compatible(
                high_signal_query_cues,
                option_cues,
            ):
                continue
            specific_query_cues = high_signal_query_cues & {
                "trade_openness",
                "gdp_deflator",
                "hicp",
                "debt_gdp_ratio",
                "public_debt",
                "trade_balance",
                "import",
                "export",
                "house_prices",
                "bond_yield",
            }
            if specific_query_cues and not _specific_cues_compatible(
                specific_query_cues,
                option_cues,
            ):
                continue
            if "trade_openness" in specific_query_cues and "trade_openness" not in option_cues:
                continue
            if "gdp_deflator" in specific_query_cues and "gdp_deflator" not in option_cues:
                continue
            if "hicp" in specific_query_cues and "hicp" not in option_cues:
                continue
            if "debt_gdp_ratio" in specific_query_cues and not (
                {"debt_gdp_ratio", "public_debt"} & option_cues
            ):
                continue

            retrieval_score = float(scores[idx]) if idx < len(scores) else 0.0
            combined_score = retrieval_score + (0.12 * relevance_score) - (0.005 * idx)
            provider_label = provider_labels.get(provider_name, provider_name)
            option_name = format_indicator_option_name(
                qs=qs,
                provider=provider_name,
                code=code_text,
                name=name_text,
                metadata=None,
            )
            option_text = f"[{provider_label}] {option_name} ({code_text})"
            scored_options.append((combined_score, option_text, provider_name))

    scored_options.sort(key=lambda item: item[0], reverse=True)
    raw_options = [option for _, option, _ in scored_options]
    deduped_options = dedupe_indicator_choice_options(qs, raw_options)
    return deduped_options[:max_options]


async def collect_statscan_selector_choice_options(
    query: str,
    max_options: int = 4,
) -> List[str]:
    """Ask the retrieval+LLM selector for StatsCan clarification options.

    This is deliberately not a keyword/product map.  It reuses the generic
    embed -> LLM selector, including its ability to reject the candidate set and
    retry with alternative search terms.  The prefetch gate can then clarify
    instead of either guessing or falling back to a provider shortcut.
    """
    selector_query = str(query or "").strip()
    if not selector_query:
        return []

    try:
        from .indicator_selector import IndicatorSelector

        selection = await IndicatorSelector().select(
            selector_query,
            "StatsCan",
            country="CA",
        )
    except Exception as exc:
        logger.debug("StatsCan selector choice fallback failed: %s", exc)
        return []

    options: List[str] = []
    if getattr(selection, "needs_user_choice", False):
        for option in getattr(selection, "options", []) or []:
            code = str(option.get("code") or "").strip()
            name = str(option.get("name") or "").strip()
            if code and name:
                options.append(f"[StatsCan] {name} ({code})")
    elif getattr(selection, "code", None):
        code = str(getattr(selection, "code") or "").strip()
        name = str(getattr(selection, "name", "") or selector_query).strip()
        if code:
            options.append(f"[StatsCan] {name or selector_query} ({code})")

        # A single LLM pick from a previously failed selection path is not enough
        # evidence to auto-fetch.  Add neighboring retrieval candidates so the
        # user can clarify rather than letting one uncertain pick become a
        # silent provider-code overwrite.
        try:
            candidates, _ = IndicatorSelector()._get_candidates_with_scores(  # pylint: disable=protected-access
                selector_query,
                "StatsCan",
                top_k=max(max_options, 4),
            )
            seen_codes = {code.upper()}
            for candidate_code, candidate_name in candidates:
                candidate_code_text = str(candidate_code or "").strip()
                candidate_name_text = str(candidate_name or "").strip()
                if (
                    not candidate_code_text
                    or not candidate_name_text
                    or candidate_code_text.upper() in seen_codes
                ):
                    continue
                seen_codes.add(candidate_code_text.upper())
                options.append(
                    f"[StatsCan] {candidate_name_text} ({candidate_code_text})"
                )
                if len(options) >= max_options:
                    break
        except Exception as exc:
            logger.debug("StatsCan selector neighbor candidates unavailable: %s", exc)

    return options[:max_options]


# ====================================================================
# Multi-concept / concept group detection
# ====================================================================

def infer_query_concept_groups(query: str) -> set[str]:
    """Infer high-level concept families from a query."""
    cues = _extract_indicator_cues(query)
    cues = {
        cue for cue in cues
        if cue not in {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
    }

    groups: set[str] = set()
    if cues & {"import", "export", "trade_balance", "trade_openness"}:
        groups.add("trade")
    if cues & {"unemployment", "employment_population"}:
        groups.add("labor")
    if cues & {"inflation", "hicp", "gdp_deflator", "producer_price", "house_prices"}:
        groups.add("prices")
    if cues & {"debt_gdp_ratio", "public_debt", "debt_service", "household_debt", "credit"}:
        groups.add("debt_credit")
    if cues & {"policy_rate", "bond_yield"}:
        groups.add("rates")
    if cues & {"exchange_rate", "real_effective_exchange_rate", "reserves", "current_account"}:
        groups.add("external")
    if cues & {"money_supply"}:
        groups.add("money")
    if cues & {"savings"}:
        groups.add("savings")
    return groups


def build_multi_concept_query_clarification(
    qs: Any,
    conversation_id: str,
    query: str,
    intent: Optional[ParsedIntent],
    is_multi_indicator: bool,
    processing_steps: Optional[List[Any]] = None,
) -> Optional[QueryResponse]:
    """Ask user to select focus when query spans multiple concept families."""
    if not intent:
        return None
    if is_multi_indicator:
        return None
    if intent.indicators and len(intent.indicators) > 1:
        return None

    params = intent.parameters or {}
    provider = normalize_provider_name(intent.apiProvider or "")
    if (
        provider == "COMTRADE"
        and params.get("__semantic_provider_locked")
        and params.get("__exact_indicator_title_match")
    ):
        # Exact UN Comtrade HS titles often contain tokens such as "m2" or
        # "chapter 52" that look like unrelated macro concepts.  Once the
        # exact-title fast path has locked a specific Comtrade commodity code,
        # do not ask a generic multi-concept question before fetching it.
        return None

    concept_groups = infer_query_concept_groups(query)
    if len(concept_groups) < 2:
        return None

    query_lower = str(query or "").lower()
    if (
        "trade openness" in query_lower
        or "exports plus imports" in query_lower
        or "export plus import" in query_lower
    ):
        return None

    group_labels = {
        "trade": "trade flows",
        "labor": "labor market",
        "prices": "prices/inflation",
        "debt_credit": "debt/credit",
        "rates": "interest rates/yields",
        "external": "external sector/FX",
        "money": "money supply",
        "savings": "savings",
    }
    detected_labels = [group_labels.get(group, group) for group in sorted(concept_groups)]
    options = qs._collect_indicator_choice_options(query, intent, max_options=4)

    clarification_questions = [
        "Your query mixes multiple indicator families, which can lead to partial or incorrect results in one fetch.",
        f"I detected: {', '.join(detected_labels)}.",
        "Please choose the first indicator focus to fetch accurately:",
    ]
    if options:
        clarification_questions.extend(
            f"{idx}. {option}" for idx, option in enumerate(options, start=1)
        )
        clarification_questions.append(
            "Reply with the option number (for example, 1) or the exact indicator you want first."
        )
        store_pending_indicator_options(
            qs=qs,
            conversation_id=conversation_id,
            query=query,
            intent=intent,
            options=options,
            question_lines=clarification_questions,
        )
    else:
        clarification_questions.append(
            "Reply with the exact indicator focus first (for example: unemployment rate, CPI inflation, or government debt-to-GDP)."
        )

    return QueryResponse(
        conversationId=conversation_id,
        intent=intent,
        clarificationNeeded=True,
        clarificationQuestions=clarification_questions,
        clarificationOptions=build_clarification_options(qs, options),
        processingSteps=processing_steps,
    )


# ====================================================================
# Informational / Metadata Query Handling
# ====================================================================

def is_simple_single_country_query(query: str) -> bool:
    """Detect simple single-country macro queries."""
    q = str(query or "").lower()
    countries = CountryResolver.detect_all_countries_in_query(query)
    if len(countries) != 1:
        return False
    macro_terms = {
        "gdp", "unemployment", "inflation", "cpi", "population",
        "labor force", "labour force", "employment rate",
        "interest rate", "government debt", "trade balance",
    }
    return any(term in q for term in macro_terms)


def looks_informational(query: str) -> bool:
    """Lightweight pre-LLM check for probable informational queries."""
    q = str(query or "").lower()
    question_words = {
        "what", "which", "list", "does", "browse", "search",
        "available", "show me", "have", "offer", "cover",
    }
    metadata_words = {
        "series", "indicators", "indicator", "datasets", "metrics",
        "providers", "data", "sources", "variables", "measures",
    }
    words = set(q.split())
    has_question = bool(words & question_words)
    has_metadata = bool(words & metadata_words)
    if has_question and has_metadata:
        fetch_signals = {"for", "in", "from", "last", "since", "between", "rate", "growth"}
        pure_metadata = words & metadata_words
        if pure_metadata == {"data"} and (words & fetch_signals):
            return False
        return True

    # ── Additional phrase-level patterns ───────────────────────────
    # Catch "how many indicators", "what data sources", etc. that the
    # simple word-intersection approach above might miss.
    informational_phrases = [
        "how many indicators", "how many series", "how many datasets",
        "how many variables", "data source", "data provider",
        "what databases", "which providers", "which sources",
        "where do you get", "where does the data",
    ]
    if any(p in q for p in informational_phrases):
        return True

    return False


def _classify_informational_subtype(query: str) -> str:
    """Classify an informational query into a sub-type for dispatch.

    Returns one of:
      - "data_sources"       : questions about available providers / data sources
      - "indicator_count"    : questions about how many indicators / series exist
      - "definition"         : "what is X?" style definitional / explanatory questions
      - "indicator_search"   : questions about what indicators exist for a topic
    """
    q = query.lower().strip()
    words = set(q.split())

    # ── data sources / providers ───────────────────────────────────
    source_keywords = {"sources", "providers", "databases", "APIs"}
    # Also match phrases like "where do you get data" or "what databases"
    source_phrases = [
        "data source", "data provider", "where do you get",
        "where does the data come from", "what databases",
        "which providers", "which sources", "what apis",
        "what services", "supported providers", "supported sources",
    ]
    if (words & {w.lower() for w in source_keywords}) or any(p in q for p in source_phrases):
        # Ensure it's asking ABOUT sources, not asking for data FROM a source
        fetch_signals = {"for", "in", "from", "last", "since", "between", "rate", "growth"}
        # If query is short or doesn't have fetch signals alongside a specific topic
        non_meta_words = words - {"what", "which", "do", "you", "have", "are",
                                   "the", "your", "data", "sources", "providers",
                                   "available", "databases", "list", "show", "me",
                                   "all", "supported", "can", "access", "offer",
                                   "does", "cover", "services", "apis", "a"}
        if len(non_meta_words) <= 2:
            return "data_sources"

    # ── indicator / series count ───────────────────────────────────
    count_phrases = [
        "how many indicators", "how many series", "how many datasets",
        "how many variables", "how many metrics", "total indicators",
        "total number of", "indicator count", "number of indicators",
        "number of series",
    ]
    if any(p in q for p in count_phrases):
        return "indicator_count"

    # ── definitions: "what is X?" style ────────────────────────────
    # Matches "what is GDP?", "what does CPI measure?", "define inflation",
    # "explain monetary policy", "tell me about purchasing power parity"
    definition_patterns = [
        r"^what\s+(?:is|are|does)\s+(?:the\s+)?(?!.*\bindicat)",  # "what is GDP" but not "what indicators..."
        r"^(?:define|explain|describe|tell\s+me\s+about)\s+",
        r"^what\s+do\s+you\s+(?:mean|know)\s+(?:by|about)\s+",
        r"^how\s+(?:is|are)\s+\w+\s+(?:calculated|measured|defined)",
    ]
    # Only classify as definition if NOT also asking about indicators/series
    indicator_words = {"indicators", "indicator", "series", "datasets", "metrics", "variables"}
    if not (words & indicator_words):
        for pat in definition_patterns:
            if re.search(pat, q):
                return "definition"

    # ── fallback: indicator search ─────────────────────────────────
    return "indicator_search"


def _handle_data_sources_query(
    query: str,
    conversation_id: str,
    tracker: Optional[ProcessingTracker] = None,
) -> QueryResponse:
    """Answer questions about available data sources / providers."""
    providers = [
        ("FRED", "US economic data — 800K+ time series from the Federal Reserve Bank of St. Louis"),
        ("World Bank", "Global development indicators — 16,000+ series covering 200+ countries"),
        ("Statistics Canada", "Canadian economic and social data — 40,000+ tables"),
        ("Eurostat", "European Union statistics — demographics, trade, economy across EU members"),
        ("IMF", "International financial data — balance of payments, exchange rates, fiscal indicators"),
        ("BIS", "Central bank statistics — credit, debt securities, property prices from the Bank for International Settlements"),
        ("OECD", "Cross-country economic data for OECD member nations"),
        ("UN Comtrade", "International trade data — bilateral import/export flows by commodity"),
        ("CoinGecko", "Cryptocurrency market data — prices, volumes, market caps"),
        ("ExchangeRate-API", "Foreign exchange rates — 160+ currencies with daily updates"),
    ]

    lines = [
        "I have access to **10 data sources** covering economic, financial, and trade data worldwide:",
        "",
    ]
    for name, desc in providers:
        lines.append(f"- **{name}** — {desc}")
    lines.append("")
    lines.append("Just ask for any economic indicator in natural language, for example:")
    lines.append('- *"Show me US GDP growth for the last 5 years"*')
    lines.append('- *"Compare inflation in Germany and France"*')
    lines.append('- *"What is the current Bitcoin price?"*')

    return QueryResponse(
        conversationId=conversation_id,
        clarificationNeeded=False,
        message="\n".join(lines),
        processingSteps=tracker.to_list() if tracker else None,
    )


def _handle_indicator_count_query(
    query: str,
    conversation_id: str,
    tracker: Optional[ProcessingTracker] = None,
) -> QueryResponse:
    """Answer questions about how many indicators are available."""
    from .indicator_database import IndicatorLookup
    lookup = IndicatorLookup()

    try:
        conn = lookup.db._get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM indicators")
        total = cursor.fetchone()[0]

        cursor.execute(
            "SELECT provider, COUNT(*) as cnt FROM indicators GROUP BY provider ORDER BY cnt DESC"
        )
        by_provider = [(row[0], row[1]) for row in cursor.fetchall()]
    except Exception:
        total = 330000
        by_provider = []

    lines = [f"The indicator database contains **{total:,}** indicators across all providers.", ""]
    if by_provider:
        lines.append("Breakdown by provider:")
        for prov, cnt in by_provider:
            lines.append(f"- **{prov}**: {cnt:,} indicators")
        lines.append("")
    lines.append("You can search for any indicator by topic, e.g.: *\"What GDP indicators does FRED have?\"*")

    return QueryResponse(
        conversationId=conversation_id,
        clarificationNeeded=False,
        message="\n".join(lines),
        processingSteps=tracker.to_list() if tracker else None,
    )


async def _handle_definition_query(
    qs: Any,
    query: str,
    conversation_id: str,
    tracker: Optional[ProcessingTracker] = None,
) -> Optional[QueryResponse]:
    """Use the LLM to answer definitional / explanatory questions."""
    llm = getattr(qs, "openrouter", None)
    provider = getattr(llm, "llm_provider", None) if llm else None
    if provider is None:
        return None  # No LLM available, fall through to default pipeline

    system_prompt = (
        "You are an expert economist. Answer the user's question clearly and concisely "
        "in 2-4 sentences. Focus on the economic definition, why it matters, and how it "
        "is commonly measured. Do not use markdown headers. Use **bold** for key terms."
    )
    try:
        result = await provider.generate(
            prompt=query,
            system_prompt=system_prompt,
            temperature=0.3,
            max_tokens=300,
        )
        text = ""
        if isinstance(result, dict) and "choices" in result:
            choices = result["choices"]
            if choices:
                msg = choices[0].get("message", {})
                text = msg.get("content", "") if isinstance(msg, dict) else str(choices[0].get("text", ""))
        text = text.strip()
        if not text:
            return None

        # Append a helpful follow-up suggestion
        text += "\n\nTo see actual data, try asking something like: *\"Show me [indicator] for [country]\"*"

        return QueryResponse(
            conversationId=conversation_id,
            clarificationNeeded=False,
            message=text,
            processingSteps=tracker.to_list() if tracker else None,
        )
    except Exception as e:
        logger.warning("Definition LLM call failed: %s", e)
        return None


def _extract_search_terms_from_query(query: str) -> str:
    """Extract meaningful search terms from a raw informational query.

    Strips common question words, metadata words, stop words, and provider
    names to produce terms suitable for indicator database search.
    """
    q = str(query or "").lower()
    # Remove punctuation
    q = re.sub(r"[?!.,;:\"'()]", " ", q)
    # Strip provider name phrases first (multi-word before single-word)
    for phrase in ("world bank", "worldbank", "statistics canada", "statscan",
                   "un comtrade", "coin gecko", "coingecko", "exchange rate",
                   "exchangerate"):
        q = q.replace(phrase, " ")
    # Strip common noise words and single-word provider names
    noise = {
        "what", "which", "list", "does", "do", "show", "me", "the", "a", "an",
        "are", "is", "have", "has", "you", "your", "available", "all", "any",
        "indicators", "indicator", "series", "datasets", "dataset", "metrics",
        "metric", "variables", "variable", "measures", "measure", "data",
        "sources", "source", "providers", "provider", "there", "exist",
        "for", "about", "of", "in", "that", "can", "i", "find", "get",
        "browse", "search", "offer", "cover", "tell", "please", "how",
        "many", "much", "some",
        # Single-word provider names
        "fred", "eurostat", "imf", "oecd", "bis", "comtrade",
    }
    words = [w for w in q.split() if w not in noise and len(w) > 1]
    return " ".join(words)


def _extract_provider_from_query(query: str) -> Optional[str]:
    """Extract a provider name from the raw query text if explicitly mentioned."""
    q = str(query or "").lower()
    provider_patterns = {
        "fred": "FRED",
        "world bank": "WorldBank",
        "worldbank": "WorldBank",
        "eurostat": "Eurostat",
        "imf": "IMF",
        "oecd": "OECD",
        "bis": "BIS",
        "statistics canada": "StatsCan",
        "statscan": "StatsCan",
        "comtrade": "Comtrade",
        "un comtrade": "Comtrade",
        "coingecko": "CoinGecko",
        "coin gecko": "CoinGecko",
        "exchangerate": "ExchangeRate",
        "exchange rate": "ExchangeRate",
    }
    for pattern, canonical in provider_patterns.items():
        if pattern in q:
            return canonical
    return None


def handle_informational_intent(
    qs: Any,
    query: str,
    intent: ParsedIntent,
    conversation_id: str,
    tracker: Optional[ProcessingTracker] = None,
) -> Optional[QueryResponse]:
    """Handle informational queries classified by the LLM (queryType=informational).

    Dispatches to sub-type handlers based on query classification:
      - data_sources   → static provider list
      - indicator_count → database count query
      - definition     → LLM-generated explanation
      - indicator_search → indicator database search (original behavior)
    """
    subtype = _classify_informational_subtype(query)
    logger.info("Informational sub-type: %s for query: %s", subtype, query[:60])

    # ── data sources ───────────────────────────────────────────────
    if subtype == "data_sources":
        return _handle_data_sources_query(query, conversation_id, tracker)

    # ── indicator count ────────────────────────────────────────────
    if subtype == "indicator_count":
        return _handle_indicator_count_query(query, conversation_id, tracker)

    # ── definition (async — need event loop) ───────────────────────
    if subtype == "definition":
        # Signal that this is a definition query — the async dispatch
        # in query.py will call _handle_definition_query.
        return _DefinitionSentinel(qs, query, conversation_id, tracker)

    # ── indicator search (original behavior) ───────────────────────
    provider = str(intent.apiProvider or "").strip()
    if provider.lower() in ("worldbank", "world bank", "none", ""):
        query_lower = str(query or "").lower()
        if "world bank" not in query_lower and "worldbank" not in query_lower:
            provider = None

    # Try extracting search terms from the parsed intent indicators first.
    # If the intent has only placeholder values (e.g., from pre-LLM dispatch),
    # fall back to extracting search terms from the raw query text.
    _placeholder_indicators = {"INFORMATIONAL", "NONE", "UNKNOWN", ""}
    raw_indicators = [
        str(ind) for ind in (intent.indicators or [])
        if str(ind).upper() not in _placeholder_indicators
    ]
    search_terms = " ".join(raw_indicators)
    search_terms = re.sub(
        r"\b(?:available|indicators?|series|datasets?|metrics?)\b",
        " ", search_terms, flags=re.IGNORECASE,
    )
    search_terms = " ".join(search_terms.split()).strip()

    # Fallback: extract search terms from the raw query by stripping
    # common question/metadata words.
    if not search_terms:
        search_terms = _extract_search_terms_from_query(query)

    # Try to detect provider from the query text if not already set
    if not provider:
        provider = _extract_provider_from_query(query)

    if not search_terms and not provider:
        return None

    from .indicator_database import IndicatorLookup
    lookup = IndicatorLookup()

    results = lookup.search(search_terms or "", provider=provider, limit=30)

    if not results and provider and search_terms:
        results = lookup.search(search_terms, provider=None, limit=20)

    if not results:
        provider_label = provider or "any provider"
        topic_label = search_terms or "that topic"
        message = (
            f"I couldn't find indicators matching \"{topic_label}\" "
            f"in {provider_label}. Try rephrasing or searching a different provider."
        )
        return QueryResponse(
            conversationId=conversation_id,
            clarificationNeeded=False,
            message=message,
            processingSteps=tracker.to_list() if tracker else None,
        )

    message = format_informational_results(results, provider, search_terms, query)

    return QueryResponse(
        conversationId=conversation_id,
        clarificationNeeded=False,
        message=message,
        processingSteps=tracker.to_list() if tracker else None,
    )


class _DefinitionSentinel:
    """Sentinel object returned when the definition sub-type needs async handling.

    The caller in query.py checks for this type and awaits the async handler.
    """
    def __init__(self, qs: Any, query: str, conversation_id: str,
                 tracker: Optional[ProcessingTracker] = None):
        self.qs = qs
        self.query = query
        self.conversation_id = conversation_id
        self.tracker = tracker

    async def resolve(self) -> Optional[QueryResponse]:
        return await _handle_definition_query(
            self.qs, self.query, self.conversation_id, self.tracker,
        )


def format_informational_results(
    results: List[Dict[str, Any]],
    provider_filter: Optional[str],
    topic: Optional[str],
    original_query: str,
) -> str:
    """Format indicator search results as a readable text response."""
    by_provider: Dict[str, List[Dict[str, Any]]] = {}
    for r in results:
        prov = str(r.get("provider") or r.get("source") or "Unknown")
        by_provider.setdefault(prov, []).append(r)

    total = len(results)
    topic_label = topic or "your search"
    if provider_filter:
        header = f"Found **{total}** indicators in **{provider_filter}** matching \"{topic_label}\":"
    else:
        header = f"Found **{total}** indicators matching \"{topic_label}\" across providers:"

    lines = [header, ""]

    for prov, indicators in by_provider.items():
        lines.append(f"**{prov}** ({len(indicators)} results):")
        for ind in indicators[:10]:
            code = ind.get("code") or ind.get("series_id") or "?"
            name = ind.get("name") or ind.get("title") or ind.get("description") or code
            if len(name) > 80:
                name = name[:77] + "..."
            lines.append(f"  - {name} (`{code}`)")
        if len(indicators) > 10:
            lines.append(f"  - ... and {len(indicators) - 10} more")
        lines.append("")

    lines.append("To fetch data for any indicator, just ask: *\"Show me [indicator name]\"*")
    return "\n".join(lines)


# ====================================================================
# Semantic discriminator verification
# ====================================================================

# Context terms that fundamentally change the requested measure. Kept local to
# verification so this module does not depend on retired shortcut modules just
# to read a private discriminator set.
SEMANTIC_DISCRIMINATORS: set[str] = {
    "growth", "real", "nominal", "level", "per capita",
    "constant", "current", "change", "rate",
    "ppp", "purchasing power",
}

def discriminator_present(disc: str, text: str) -> bool:
    """Check whether semantic discriminator *disc* is satisfied by *text*.

    This is the **single source of truth** for discriminator equivalence
    mappings.  Every call-site that needs to decide "does this indicator
    name / code / description match the query discriminator?" MUST use
    this function (or :func:`verify_semantic_discriminators` which wraps it).

    The equivalences are structural format mappings (e.g. "rate" ↔ "%"),
    NOT per-indicator patches.
    """
    if disc in text:
        return True
    if disc == "rate" and ("%" in text or "percent" in text or "ratio" in text
                           or "annual" in text):
        return True
    if disc == "growth" and ("annual %" in text or "percent change" in text):
        return True
    if disc == "ppp" and ("purchasing power" in text or "international $" in text):
        return True
    if disc == "purchasing power" and ("ppp" in text or "international $" in text):
        return True
    if disc == "per capita" and ("per person" in text or "per inhabitant" in text):
        return True
    return False


def find_missing_discriminators(
    query_discs: set[str],
    *texts: str,
) -> set[str]:
    """Return the subset of *query_discs* NOT satisfied by any of *texts*.

    Each text is checked via :func:`discriminator_present`.
    """
    return {
        d for d in query_discs
        if not any(discriminator_present(d, t) for t in texts)
    }


def verify_semantic_discriminators(
    original_query: str,
    code: str,
    series_name: str,
) -> bool:
    """Unified semantic verification -- single source of truth.

    Returns True if the code/name matches the query's semantic discriminators.
    """
    original_lower = str(original_query or "").lower()
    query_discs = {d for d in SEMANTIC_DISCRIMINATORS if d in original_lower}
    if not query_discs:
        return True

    name_lower = str(series_name or "").lower()
    code_lower = str(code or "").lower()

    missing = find_missing_discriminators(query_discs, name_lower, code_lower)
    if missing:
        logger.info(
            "Semantic verification: '%s' (%s) missing discriminators %s from '%s'",
            code, name_lower[:40], missing, original_lower[:40],
        )
        return False
    return True


# ====================================================================
# Region / group scope helpers
# ====================================================================

def humanize_region_name(region: str) -> str:
    """Convert normalized region keys into user-facing labels."""
    text = str(region or "").strip()
    if not text:
        return text

    replacements = {
        "BRICS_PLUS": "BRICS+",
        "EU": "European Union",
        "EUROZONE": "euro area",
        "LATAM": "Latin America",
        "MENA": "Middle East and North Africa",
        "SSA": "Sub-Saharan Africa",
        "EAST_ASIA": "East Asia",
        "SOUTH_ASIA": "South Asia",
        "SOUTHEAST_ASIA": "Southeast Asia",
    }
    if text in replacements:
        return replacements[text]
    if text.isupper():
        return text.replace("_", " ")
    return text.replace("_", " ").title()


def has_explicit_group_scope(qs: Any, query: str) -> bool:
    """Return True when the query already makes group scope explicit."""
    query_lower = str(query or "").lower()
    if qs._is_comparison_query(query_lower) or qs._is_ranking_query(query_lower):
        return True

    explicit_markers = [
        "member countries",
        "member country",
        "members",
        "countries",
        "nations",
        "nation",
        "economies",
        "each country",
        "by country",
        "country by country",
        "cross-country",
        "group average",
        "regional average",
        "average across",
        "mean across",
        "aggregate",
        "aggregated",
        "overall",
        "as a group",
        "as a whole",
        "group as a whole",
        "one value",
        "single value",
        "combined",
        "total for the group",
    ]
    if any(marker in query_lower for marker in explicit_markers):
        return True

    return False


def rewrite_group_scope_query(
    query: str,
    region: str,
    scope: str,
) -> str:
    """Rewrite an ambiguous group query into an explicit scope query."""
    query_text = str(query or "").strip()
    region_label = humanize_region_name(region)
    if not query_text or not region_label:
        return query_text

    region_patterns = [re.escape(region_label)]
    region_upper = str(region or "").strip()
    if region_upper and region_upper != region_label:
        region_patterns.append(re.escape(region_upper.replace("_", " ")))
    region_patterns.append(re.escape(region_upper))
    if region_upper == "EU":
        region_patterns.extend(
            re.escape(alias)
            for alias in (
                "Europe",
                "European",
                "European countries",
                "European economies",
            )
        )
    region_regex = "|".join(pattern for pattern in region_patterns if pattern)

    rewritten = query_text
    if scope == "compare_members":
        rewritten = re.sub(
            rf"\b(in|for|within|among)\s+(?:the\s+)?(?:{region_regex})\b",
            f"across {region_label} member countries",
            rewritten,
            count=1,
            flags=re.IGNORECASE,
        )
        if rewritten == query_text:
            rewritten = re.sub(
                rf"\b(?:{region_regex})\b",
                f"{region_label} member countries",
                rewritten,
                count=1,
                flags=re.IGNORECASE,
            )
        if not _is_comparison_query_standalone(rewritten):
            rewritten = f"compare {rewritten}"
        return rewritten

    rewritten = re.sub(
        rf"\b(in|for|within|among)\s+(?:the\s+)?(?:{region_regex})\b",
        f"for the {region_label} group as a whole",
        rewritten,
        count=1,
        flags=re.IGNORECASE,
    )
    if rewritten == query_text:
        rewritten = re.sub(
            rf"\b(?:{region_regex})\b",
            f"the {region_label} group as a whole",
            rewritten,
            count=1,
            flags=re.IGNORECASE,
        )
    if rewritten == query_text:
        rewritten = f"{query_text} for the {region_label} group as a whole"
    return rewritten


def build_group_scope_clarification(
    qs: Any,
    conversation_id: str,
    query: str,
    intent: Optional[ParsedIntent],
    is_multi_indicator: bool,
    processing_steps: Optional[List[Any]] = None,
) -> Optional[QueryResponse]:
    """Ask whether a group query means member-country comparison or one group value."""
    if is_multi_indicator:
        return None
    if intent and intent.indicators and len(intent.indicators) > 1:
        return None

    explicit_provider = qs._normalize_provider_alias(
        qs._detect_explicit_provider(query)
    )
    if explicit_provider:
        return None

    regions = CountryResolver.detect_regions_in_query(query)
    if len(regions) != 1:
        return None

    region = regions[0]
    expanded = CountryResolver.expand_region(region)
    if not expanded or len(expanded) < 2:
        return None
    if has_explicit_group_scope(qs, query):
        return None

    region_label = humanize_region_name(region)
    options = [
        ClarificationOption(
            id="1",
            label="compare member countries",
            value=rewrite_group_scope_query(query, region, "compare_members"),
        ),
        ClarificationOption(
            id="2",
            label="one overall group value (aggregate/average if available)",
            value=rewrite_group_scope_query(query, region, "group_value"),
        ),
    ]

    clarification_questions = [
        f"Your query mentions {region_label}, but the scope is still ambiguous.",
        f"Do you want to compare {region_label} member countries, or ask for one overall value for the group?",
    ]
    clarification_questions.extend(
        f"{option.id}. {option.label}" for option in options
    )
    clarification_questions.append(
        "Reply with the option number, or type a different scope."
    )

    payload = {
        "kind": "group_scope",
        "region": region,
        "original_query": str(query or "").strip(),
        "question_lines": clarification_questions,
        "options": [option.model_dump() for option in options],
    }
    store_pending_semantic_clarification(conversation_id, payload)

    return QueryResponse(
        conversationId=conversation_id,
        intent=intent,
        clarificationNeeded=True,
        clarificationQuestions=clarification_questions,
        clarificationOptions=options,
        processingSteps=processing_steps,
    )


def build_missing_decomposition_entities_clarification(
    conversation_id: str,
    query: str,
    intent: Optional[ParsedIntent],
    processing_steps: Optional[List[Any]] = None,
) -> Optional[QueryResponse]:
    """Ask for dimensions/categories when decomposition was requested without members.

    A parsed intent like "France inflation by category" can correctly detect a
    category breakdown request but omit the actual category members. Fetching a
    single overall series in that state is a wrong-confident answer; ask the
    user to specify the breakdown instead.
    """
    if not intent or not intent.needsDecomposition or intent.decompositionEntities:
        return None

    params = dict(intent.parameters or {})
    if (
        normalize_provider_name(intent.apiProvider or "") == "STATSCAN"
        and str(params.get("__statscan_decomposition_axis") or "").strip()
    ):
        # StatsCan axis decomposition members are provider-native metadata.
        # Do not ask the user to name members such as Men+/Women+; require the
        # downstream selector/exact-code path to establish product authority,
        # then enumerate non-aggregate members from the selected cube.
        return None

    decomp_type = str(intent.decompositionType or "").strip().lower()
    category_like_types = {
        "category",
        "categories",
        "dimension",
        "dimensions",
        "breakdown",
        "components",
        "component",
    }
    if decomp_type not in category_like_types:
        return None

    query_text = str(query or intent.originalQuery or "").strip()
    category_label = "category" if decomp_type in {"category", "categories"} else "breakdown member"
    category_plural = "categories" if category_label == "category" else "breakdown members"
    base_query = query_text or "this query"
    overall_value = re.sub(
        r"\s+\bby\s+(?:category|categories|dimension|dimensions|component|components|breakdown)\b",
        "",
        base_query,
        flags=re.IGNORECASE,
    ).strip(" ,;:-") or base_query
    specify_value = f"{base_query} for categories: <name the categories>"

    options = [
        ClarificationOption(
            id="1",
            label=f"specify {category_plural}",
            value=specify_value,
        ),
        ClarificationOption(
            id="2",
            label="use the main overall series",
            value=overall_value,
        ),
    ]
    clarification_questions = [
        f"Your query asks for a {category_label} breakdown, but no specific {category_plural} were named.",
        f"Which {category_plural} should I use?",
    ]
    clarification_questions.extend(
        f"{option.id}. {option.label}" for option in options
    )
    clarification_questions.append(
        f"Reply with the option number, or type the specific {category_plural} you want."
    )

    payload = {
        "kind": "missing_decomposition_entities",
        "decomposition_type": decomp_type,
        "original_query": base_query,
        "question_lines": clarification_questions,
        "options": [option.model_dump() for option in options],
    }
    store_pending_semantic_clarification(conversation_id, payload)

    return QueryResponse(
        conversationId=conversation_id,
        intent=intent,
        clarificationNeeded=True,
        clarificationQuestions=clarification_questions,
        clarificationOptions=options,
        processingSteps=processing_steps,
    )


def build_unspecified_geography_breakdown_clarification(
    conversation_id: str,
    query: str,
    intent: Optional[ParsedIntent] = None,
    processing_steps: Optional[List[Any]] = None,
) -> Optional[QueryResponse]:
    """Ask for explicit geography when a regional breakdown is requested without one."""
    query_text = str(query or "").strip()
    if not query_text:
        return None

    query_lower = query_text.lower()
    if not re.search(r"\bby\s+(?:province|state|region|city|county)\b", query_lower):
        return None
    if CountryResolver.detect_all_countries_in_query(query_text):
        return None
    if CountryResolver.detect_regions_in_query(query_text):
        return None

    clarification_questions = [
        "Your query asks for a geographic breakdown, but it does not name the geography to break down.",
        "If you want a subnational breakdown, name the country first (for example: 'Canada employment by province').",
        "If you want a multi-country group, name the region explicitly (for example: 'employment in South Asia').",
    ]

    return QueryResponse(
        conversationId=conversation_id,
        intent=intent,
        clarificationNeeded=True,
        clarificationQuestions=clarification_questions,
        clarificationOptions=None,
        processingSteps=processing_steps,
    )


def build_catalog_concept_clarification(
    qs: Any,
    conversation_id: str,
    query: str,
    intent: Optional[ParsedIntent],
    processing_steps: Optional[List[Any]] = None,
) -> Optional[QueryResponse]:
    """Disabled: broad concept clarification must come from the LLM, not code maps."""
    return None


def _semantic_metric_option_value(original_query: str, label: str) -> str:
    query_text = str(original_query or "").strip()
    label_text = str(label or "").strip()
    if not query_text or not label_text:
        return label_text or query_text

    for concept in ("employment", "trade data", "interest rate", "government debt"):
        replaced = re.sub(rf"\b{re.escape(concept)}\b", label_text, query_text, count=1, flags=re.IGNORECASE)
        if replaced != query_text:
            return replaced
    return f"{label_text} {query_text}".strip()


def _extract_semantic_metric_label(question: str) -> Optional[str]:
    text = str(question or "").strip().rstrip("?")
    if not text.lower().startswith("do you want"):
        return None

    text = re.sub(r"^Do you want\s+(?:the\s+)?", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s+for\s+.+$", "", text).strip()
    label = re.sub(r"\s*\([^)]*\)", "", text).strip()
    lowered = label.lower()
    if "employed person" in lowered or "number of employed" in lowered:
        return "number employed"
    return label or None


def build_structured_semantic_clarification(
    qs: Any,
    conversation_id: str,
    query: str,
    intent: Optional[ParsedIntent],
    processing_steps: Optional[List[Any]] = None,
) -> Optional[QueryResponse]:
    """Convert LLM clarification questions into structured semantic options when possible."""
    if not intent or not intent.clarificationNeeded:
        return None

    questions = [str(item).strip() for item in (intent.clarificationQuestions or []) if str(item).strip()]
    options: List[ClarificationOption] = []
    for idx, question in enumerate(questions, start=1):
        label = _extract_semantic_metric_label(question)
        if not label:
            continue
        options.append(
            ClarificationOption(
                id=str(idx),
                label=label,
                value=_semantic_metric_option_value(query, label),
            )
        )

    if len(options) < 2:
        return None

    payload = {
        "kind": "semantic_metric",
        "original_query": str(query or "").strip(),
        "question_lines": questions,
        "options": [option.model_dump() for option in options],
    }
    store_pending_semantic_clarification(conversation_id, payload)

    return QueryResponse(
        conversationId=conversation_id,
        intent=intent,
        clarificationNeeded=True,
        clarificationQuestions=questions,
        clarificationOptions=options,
        processingSteps=processing_steps,
    )


# ====================================================================
# Viable option filtering
# ====================================================================

async def filter_viable_indicator_choice_options(
    qs: Any,
    query: str,
    intent: ParsedIntent,
    options: List[str],
    max_options: int = 3,
) -> List[str]:
    """Keep only indicator-choice options that can plausibly fetch usable data."""
    clean_options = dedupe_indicator_choice_options(
        qs,
        [str(option) for option in (options or []) if str(option).strip()],
    )
    if len(clean_options) < 2:
        return clean_options[:max_options]

    viable_options: List[str] = []
    tracker_token = None
    if get_processing_tracker() is not None:
        tracker_token = activate_processing_tracker(None)  # type: ignore[arg-type]

    try:
        for option_text in clean_options:
            parsed = parse_indicator_option(option_text)
            if not parsed:
                continue

            provider_name, code = parsed
            attempt_intent = intent.model_copy(deep=True)
            attempt_intent.apiProvider = provider_name
            attempt_intent.indicators = [code]
            if not attempt_intent.originalQuery:
                attempt_intent.originalQuery = str(query or "").strip()

            attempt_params = dict(attempt_intent.parameters or {})
            attempt_params["_prefetch_option_validation"] = True
            attempt_params.pop("seriesId", None)
            attempt_params.pop("series_id", None)
            attempt_params.pop("code", None)
            attempt_params["indicator"] = code
            attempt_intent.parameters = attempt_params

            try:
                candidate_data = await retry_async(
                    lambda i=attempt_intent: qs._fetch_data(i),
                    max_attempts=1,
                    initial_delay=0.2,
                )
            except Exception:
                continue

            if not candidate_data:
                continue

            candidate_data = qs._rerank_data_by_query_relevance(query, candidate_data)
            if qs._is_ranking_query(query):
                candidate_data = qs._apply_ranking_projection(query, candidate_data)
            if not candidate_data:
                continue
            if qs._has_implausible_top_series(query, candidate_data):
                continue
            if qs._needs_indicator_clarification(query, candidate_data, attempt_intent):
                continue

            viable_options.append(option_text)
            if len(viable_options) >= max_options:
                break
    finally:
        if tracker_token is not None:
            reset_processing_tracker(tracker_token)

    return viable_options


def build_failed_indicator_choice_response(
    qs: Any,
    conversation_id: str,
    query: str,
    intent: ParsedIntent,
    options: List[str],
    selected_option: Optional[str],
    question_lines: Optional[List[str]],
    tracker: Optional[ProcessingTracker] = None,
    error: Optional[str] = None,
) -> QueryResponse:
    """Re-prompt after an indicator option fails and drop the dead option."""
    remaining_options = [
        option for option in dedupe_indicator_choice_options(qs, options)
        if option != selected_option
    ]
    if remaining_options:
        store_pending_indicator_options(
            qs=qs,
            conversation_id=conversation_id,
            query=query,
            intent=intent,
            options=remaining_options,
            question_lines=question_lines or [],
        )
    else:
        conversation_manager.clear_pending_indicator_options(conversation_id)

    return QueryResponse(
        conversationId=conversation_id,
        intent=intent,
        clarificationNeeded=bool(remaining_options),
        clarificationQuestions=question_lines or [],
        clarificationOptions=build_clarification_options(qs, remaining_options),
        error=error,
        message="That option did not return usable data. Please choose a different option.",
        processingSteps=tracker.to_list() if tracker else None,
    )


# ====================================================================
# Pre-fetch indicator choice clarification
# ====================================================================

async def build_prefetch_indicator_choice_clarification(
    qs: Any,
    conversation_id: str,
    query: str,
    intent: Optional[ParsedIntent],
    explicit_provider: Optional[str],
    is_multi_indicator: bool,
    processing_steps: Optional[List[Any]] = None,
) -> Optional[QueryResponse]:
    """Clarify indicator choice before fetch when the routed provider guess is weak."""
    if not intent:
        return None
    if explicit_provider:
        return None
    if is_multi_indicator:
        return None

    provider_upper = normalize_provider_name(intent.apiProvider or "")
    if provider_upper in ("COINGECKO", "EXCHANGERATE"):
        return None

    use_outcome_decision_stage = _use_outcome_decision_stage(qs)
    evidence_builder = CandidateEvidenceBuilder() if use_outcome_decision_stage else None

    if intent.indicators and len(intent.indicators) > 1:
        return None

    params = dict(intent.parameters or {})
    query_text = str(query or "").strip()
    indicator_query = qs._select_indicator_query_for_resolution(intent)
    if not indicator_query:
        indicator_query = str(intent.indicators[0] if intent.indicators else "").strip()
    if not indicator_query:
        indicator_query = query_text
    if not indicator_query:
        return None

    provider = normalize_provider_name(intent.apiProvider or "")
    current_indicator = str(params.get("indicator") or "").strip()
    current_indicator_is_provider_code = bool(
        current_indicator
        and looks_like_provider_indicator_code(provider, current_indicator)
    )
    current_indicator_name = str(
        params.get("__semantic_indicator_label")
        or (intent.indicators[0] if intent.indicators else "")
        or current_indicator
    )
    if (
        current_indicator_is_provider_code
        and _is_resolved_indicator_plausible(
            qs,
            provider,
            indicator_query,
            current_indicator,
            current_indicator_name,
        )
    ):
        return None
    if current_indicator and not current_indicator_is_provider_code:
        # Parsed/cached intents may carry a plain-English metric phrase in the
        # provider-code slot.  That text is useful semantic evidence, but it is
        # not final provider-native authority and must not prevent the generic
        # retrieval -> LLM selector from resolving a real executable code.
        params.setdefault("__semantic_indicator_label", current_indicator)
        params.pop("indicator", None)
        intent.parameters = params
        current_indicator = ""

    option_budget = (
        CandidateEvidenceBuilder.MAX_CLARIFICATION_LIMIT
        if use_outcome_decision_stage
        else 4
    )
    target_countries = qs._collect_target_countries(params)
    target_country = target_countries[0] if target_countries else None

    # Let the framework selector (retrieval -> LLM adjudication, including
    # REJECT/retry) make the only prefetch direct-answer decision. Candidate
    # options below may clarify, but must not silently become final authority.
    resolved = None
    primary_accepted = False
    primary_relevance = -999.0
    current_label = f"{provider or 'Unknown provider'} routing guess"
    if not current_indicator:
        try:
            from .indicator_selector import IndicatorSelector

            provider_for_selector = "StatsCan" if provider == "STATSCAN" else provider
            selection = await IndicatorSelector().select(
                indicator_query,
                provider_for_selector,
                country=target_country,
            )
        except Exception as exc:
            logger.debug("Prefetch selector unavailable for %s/%s: %s", provider, indicator_query, exc)
            selection = None

        if selection is not None and getattr(selection, "code", None):
            selection_code = str(getattr(selection, "code") or "").strip()
            selection_name = str(getattr(selection, "name", "") or "").strip()
            if _is_resolved_indicator_plausible(
                qs,
                provider,
                indicator_query,
                selection_code,
                selection_name,
            ):
                params["indicator"] = selection_code
                params["__semantic_indicator_label"] = indicator_query
                params["__semantic_authority"] = "llm_adjudication"
                params["__decision_source"] = str(getattr(selection, "source", "") or "llm_pick")
                intent.parameters = params
                resolved = SimpleNamespace(
                    code=selection_code,
                    name=selection_name or selection_code,
                    provider=provider,
                    metadata={},
                    source=str(getattr(selection, "source", "") or "llm_pick"),
                )
                primary_accepted = True
                primary_relevance = 1.0
                current_name = format_indicator_option_name(
                    qs=qs,
                    provider=provider,
                    code=selection_code,
                    name=selection_name,
                    metadata=None,
                )
                current_label = f"{current_name} from {provider}"
                return None

        if selection is not None and getattr(selection, "needs_user_choice", False):
            logger.debug(
                "Prefetch selector requested clarification for '%s'; "
                "continuing to candidate evidence path.",
                indicator_query,
            )

    options = qs._collect_indicator_choice_options(
        query_text or indicator_query,
        intent,
        max_options=option_budget,
    )
    if not options:
        if not primary_accepted:
            fallback_providers = qs._get_fallback_providers(provider)
            original_provider = intent.apiProvider
            for fb_provider in fallback_providers:
                fb_query = indicator_query or query_text or ""
                fb_intent = intent.model_copy(deep=True)
                fb_intent.apiProvider = fb_provider
                fb_options = qs._collect_indicator_choice_options(
                    fb_query, fb_intent, max_options=4,
                )
                if fb_options:
                    logger.info(
                        "Primary %s failed, found options via fallback %s",
                        original_provider, fb_provider,
                    )
                    options = fb_options
                    break
            intent.apiProvider = original_provider
            if not options and normalize_provider_name(provider) == "STATSCAN":
                options = await collect_statscan_selector_choice_options(
                    indicator_query or query_text,
                    max_options=option_budget,
                )
            if not options:
                return build_no_reliable_indicator_match_response(
                    conversation_id=conversation_id,
                    intent=intent,
                    query=query_text or indicator_query,
                    qs=qs,
                    processing_steps=processing_steps,
                )
        if not options:
            return None
    if len(options) >= 2 and not has_materially_distinct_indicator_options(qs, options):
        return None

    target_countries = qs._collect_target_countries(intent.parameters)
    should_skip_viability_prefetch = len(target_countries) > 8
    if len(options) >= 2 and not should_skip_viability_prefetch:
        viable_options = await qs._filter_viable_indicator_choice_options(
            query=query_text or indicator_query,
            intent=intent,
            options=options,
            max_options=option_budget,
        )
        if len(viable_options) >= 2:
            options = viable_options

    top_option = parse_indicator_option(options[0]) if options else None
    top_matches_primary = bool(
        resolved
        and top_option
        and top_option[0] == normalize_provider_name(getattr(resolved, "provider", provider))
        and str(top_option[1]).upper() == str(getattr(resolved, "code", "") or "").upper()
    )
    option_provider_names = {
        normalize_provider_name(parsed[0])
        for option in options
        for parsed in [parse_indicator_option(option)]
        if parsed
    }
    intent_indicator = str((intent.indicators or [None])[0] or "")
    has_provider_native_intent_indicator = bool(
        intent_indicator and looks_like_provider_indicator_code(provider, intent_indicator)
    )

    if len(options) == 1:
        if primary_accepted and top_matches_primary:
            return None
        return build_no_reliable_indicator_match_response(
            conversation_id=conversation_id,
            intent=intent,
            query=query_text or indicator_query,
            qs=qs,
            processing_steps=processing_steps,
        )

    if len(options) < 2:
        if not primary_accepted:
            return build_no_reliable_indicator_match_response(
                conversation_id=conversation_id,
                intent=intent,
                query=query_text or indicator_query,
                qs=qs,
                processing_steps=processing_steps,
            )
        return None

    if not use_outcome_decision_stage and primary_accepted and top_matches_primary:
        return None

    if (
        not use_outcome_decision_stage
        and primary_accepted
        and not top_matches_primary
        and primary_relevance >= 0.65
    ):
        return None

    if (
        primary_accepted
        and primary_relevance >= 0.65
        and option_provider_names
        and normalize_provider_name(provider) not in option_provider_names
    ):
        return None

    if (
        has_provider_native_intent_indicator
        and option_provider_names
        and normalize_provider_name(provider) not in option_provider_names
    ):
        return None

    if use_outcome_decision_stage and evidence_builder is not None:
        option_records: list[dict[str, Any]] = []
        option_index: dict[str, str] = {}
        for option_text in options:
            parsed = parse_indicator_option(option_text)
            if not parsed:
                continue
            option_provider, option_code = parsed
            candidate_id = build_candidate_id(option_provider, option_code)
            option_records.append(
                {
                    "provider": option_provider,
                    "code": option_code,
                    "label": option_text,
                    "title": option_text,
                    "source": "option",
                    "executable": True,
                }
            )
            option_index[candidate_id] = option_text

        primary_candidate_id = None
        if resolved and getattr(resolved, "code", None):
            primary_candidate_id = build_candidate_id(
                str(getattr(resolved, "provider", "") or provider),
                str(getattr(resolved, "code", "") or ""),
            )

        candidate_evidence = evidence_builder.build_prefetch_evidence(
            provider=provider,
            resolved=resolved,
            options=option_records,
            target_countries=target_countries,
            primary_accepted=primary_accepted,
        )
        decision = decide_prefetch_outcome(
            candidates=candidate_evidence,
            primary_accepted=primary_accepted,
            primary_candidate_id=primary_candidate_id,
        )

        if decision.outcome == "unsupported":
            return build_no_reliable_indicator_match_response(
                conversation_id=conversation_id,
                intent=intent,
                query=query_text or indicator_query,
                qs=qs,
                processing_steps=processing_steps,
            )

        if decision.outcome == "direct_answer":
            selected_option = option_index.get(str(decision.selected_candidate_id or ""))
            if selected_option and (not primary_accepted or not top_matches_primary):
                apply_indicator_option_to_intent(intent, selected_option)
            return None

        if decision.outcome == "clarify":
            max_options = max(
                2,
                min(
                    len(options),
                    decision.clarification_option_limit
                    or evidence_builder.clarification_option_limit(candidate_evidence),
                ),
            )
            options = options[:max_options]

    clarification_questions = [
        "I found multiple plausible indicator matches before fetching data.",
        f"Current routed match: {current_label}",
        "Please choose the indicator you intended:",
    ]
    clarification_questions.extend(
        f"{idx}. {option}" for idx, option in enumerate(options, start=1)
    )
    clarification_questions.append(
        "Reply with the option number (for example, 1) or the exact indicator text you want."
    )

    store_pending_indicator_options(
        qs=qs,
        conversation_id=conversation_id,
        query=query_text or indicator_query,
        intent=intent,
        options=options,
        question_lines=clarification_questions,
    )

    return QueryResponse(
        conversationId=conversation_id,
        intent=intent,
        clarificationNeeded=True,
        clarificationQuestions=clarification_questions,
        clarificationOptions=build_clarification_options(qs, options),
        processingSteps=processing_steps,
    )


# ====================================================================
# Post-parse clarification orchestration
# ====================================================================

async def build_post_parse_clarification(
    qs: Any,
    conversation_id: str,
    query: str,
    parse_result: "ParseRouteResult",
    validation: "ValidationResult",
    processing_steps: Optional[List[Any]] = None,
) -> Optional[QueryResponse]:
    """Apply the shared clarification guardrails after parse/validation."""
    intent = parse_result.intent
    if _is_exact_match_locked(intent.parameters if intent else None):
        return None

    if normalize_provider_name(intent.apiProvider or "") == "NOT_AVAILABLE":
        indicator_text = intent.indicators[0] if intent.indicators else query
        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            data=None,
            clarificationNeeded=False,
            message=(
                f"'{indicator_text}' is not currently available through any of our "
                f"data providers. This may be because the data series has been "
                f"discontinued, archived, or is only available through specialized sources."
            ),
        )

    multi_concept_clarification = build_multi_concept_query_clarification(
        qs=qs,
        conversation_id=conversation_id,
        query=query,
        intent=intent,
        is_multi_indicator=validation.is_multi_indicator,
        processing_steps=processing_steps,
    )
    if multi_concept_clarification:
        return multi_concept_clarification

    concept_clarification = build_catalog_concept_clarification(
        qs=qs,
        conversation_id=conversation_id,
        query=query,
        intent=intent,
        processing_steps=processing_steps,
    )
    if concept_clarification:
        return concept_clarification

    missing_decomposition_entities = build_missing_decomposition_entities_clarification(
        conversation_id=conversation_id,
        query=query,
        intent=intent,
        processing_steps=processing_steps,
    )
    if missing_decomposition_entities:
        return missing_decomposition_entities

    group_scope_clarification = build_group_scope_clarification(
        qs=qs,
        conversation_id=conversation_id,
        query=query,
        intent=intent,
        is_multi_indicator=validation.is_multi_indicator,
        processing_steps=processing_steps,
    )
    if group_scope_clarification:
        return group_scope_clarification

    return await build_prefetch_indicator_choice_clarification(
        qs=qs,
        conversation_id=conversation_id,
        query=query,
        intent=intent,
        explicit_provider=parse_result.explicit_provider,
        is_multi_indicator=validation.is_multi_indicator,
        processing_steps=processing_steps,
    )


# ====================================================================
# Validation failure responses
# ====================================================================

def build_invalid_intent_response(
    conversation_id: str,
    intent: ParsedIntent,
    validation_error: Optional[str],
    suggestions: Optional[Dict[str, Any]],
    processing_steps: Optional[List[Any]] = None,
) -> QueryResponse:
    """Build the standard validation-failure clarification response."""
    clarification_qs = ParameterValidator.suggest_clarification(intent, validation_error)
    message_parts = ["**Cannot Process Query**", str(validation_error or "The query is missing required details.")]
    if suggestions:
        if suggestions.get("suggestion"):
            message_parts.append(f"\n**Suggestion**: {suggestions['suggestion']}")
        if suggestions.get("common_indicators"):
            message_parts.append(f"\n**Common indicators**: {', '.join(suggestions['common_indicators'])}")
        if suggestions.get("example"):
            message_parts.append(f"\n**Example**: {suggestions['example']}")

    return QueryResponse(
        conversationId=conversation_id,
        intent=intent,
        clarificationNeeded=True,
        clarificationQuestions=clarification_qs,
        message="\n".join(message_parts),
        processingSteps=processing_steps,
    )


def build_low_confidence_intent_response(
    conversation_id: str,
    intent: ParsedIntent,
    confidence_reason: Optional[str],
    processing_steps: Optional[List[Any]] = None,
) -> QueryResponse:
    """Build the standard low-confidence clarification response."""
    reason = str(confidence_reason or "I could not confidently determine the requested data.")
    return QueryResponse(
        conversationId=conversation_id,
        intent=intent,
        clarificationNeeded=True,
        clarificationQuestions=[
            f"I'm not certain about this query: {reason}",
            "Could you rephrase with more specific details?",
            "Or would you like to use Pro Mode for a custom analysis?",
        ],
        message=f"**Uncertain Query**\n{reason}\n\nPlease provide more details or use Pro Mode for better results.",
        processingSteps=processing_steps,
    )


# ====================================================================
# Post-fetch indicator uncertainty detection
# ====================================================================

def needs_indicator_clarification(
    qs: Any,
    query: str,
    data: List[Any],
    intent: Optional[ParsedIntent] = None,
) -> bool:
    """Determine whether returned data looks semantically uncertain for the query."""
    if not data:
        return False
    if intent and intent.indicators and len(intent.indicators) > 1:
        return False

    if intent and intent.confidence and intent.confidence >= 0.90:
        indicator = (intent.parameters or {}).get("indicator", "")
        provider_upper = normalize_provider_name(intent.apiProvider or "")
        is_pre_resolved = False
        if indicator and "." in indicator and indicator[0].isalpha():
            is_pre_resolved = True
        elif indicator and provider_upper == "FRED" and re.fullmatch(r"[A-Z0-9]{3,}", indicator):
            is_pre_resolved = True
        if is_pre_resolved:
            logger.info(
                "Skipping uncertainty check -- high-confidence pre-resolved indicator: %s (conf=%.2f)",
                indicator, intent.confidence,
            )
            return False

    scored: List[tuple[float, Any]] = [
        (_score_series_relevance(query, series), series)
        for series in data
    ]
    scored.sort(key=lambda item: item[0], reverse=True)

    top_score, top_series = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else -999.0
    score_gap = top_score - second_score if len(scored) > 1 else 99.0
    top_meta = getattr(top_series, "metadata", None)
    query_cues = _extract_indicator_cues(query)
    top_series_cues = _extract_indicator_cues(_series_text_for_relevance(top_series))
    high_signal_query_cues = {
        cue for cue in query_cues
        if cue not in {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
    }
    strict_cues = {
        "import",
        "export",
        "trade_balance",
        "trade_openness",
        "debt_gdp_ratio",
        "gdp_deflator",
        "public_debt",
        "money_supply",
        "bond_yield",
        "tenor_2y",
        "tenor_10y",
        "tenor_30y",
        "policy_rate",
        "house_prices",
        "employment_rate",
        "hicp",
        "reserves",
        "employment_population",
        "producer_price",
        "exchange_rate",
    }

    normalized_keys: set[tuple[str, str]] = set()
    for _, series in scored:
        provider_name, provider_code = qs._extract_series_provider_and_code(series)
        if provider_name and provider_code:
            normalized_keys.add((provider_name, provider_code.upper()))
    has_consensus_code = len(normalized_keys) == 1 and len(scored) >= 2

    normalized_indicator_keys: set[tuple[str, str]] = set()
    for _, series in scored:
        meta = getattr(series, "metadata", None) if series is not None else None
        provider_name = normalize_provider_name(str(getattr(meta, "source", "") or "")) if meta else ""
        indicator_text = str(getattr(meta, "indicator", "") or getattr(meta, "seriesId", "") or "").strip().lower() if meta else ""
        indicator_text = re.sub(r"\s+", " ", indicator_text)
        if provider_name and indicator_text:
            normalized_indicator_keys.add((provider_name, indicator_text))
    has_consensus_indicator = len(normalized_indicator_keys) == 1 and len(scored) >= 2

    aligned_high_signal = (not high_signal_query_cues) or bool(high_signal_query_cues & top_series_cues)
    if top_score >= 1.0 and aligned_high_signal and (
        has_consensus_code
        or has_consensus_indicator
        or score_gap >= 0.45
        or second_score < 0.8
    ):
        return False

    top_provider = normalize_provider_name(str(getattr(top_meta, "source", "") or "")) if top_meta else ""
    explicit_provider = normalize_provider_name(qs._detect_explicit_provider(query) or "")
    if explicit_provider and top_provider == explicit_provider and top_score >= 0.8 and aligned_high_signal:
        return False

    if "hicp" in query_cues and "hicp" in top_series_cues and top_score >= 0.8:
        return False
    if "gdp_deflator" in query_cues and "gdp_deflator" in top_series_cues and top_score >= 0.8:
        return False
    if "trade_openness" in query_cues and "trade_openness" in top_series_cues and top_score >= 0.8:
        return False

    try:
        from .catalog_service import find_concept_by_term, find_concepts_by_code

        query_concept = find_concept_by_term(query)
        if not query_concept and intent and intent.originalQuery:
            query_concept = find_concept_by_term(str(intent.originalQuery))

        if query_concept:
            top_provider_name, top_code = qs._extract_series_provider_and_code(top_series)
            top_series_concepts = (
                find_concepts_by_code(top_provider_name, top_code)
                if top_provider_name and top_code
                else []
            )
            if top_series_concepts and query_concept not in top_series_concepts:
                return True

            if not top_series_concepts and top_meta:
                inferred_series_concept = find_concept_by_term(
                    str(getattr(top_meta, "indicator", "") or "")
                )
                if (
                    inferred_series_concept
                    and inferred_series_concept != query_concept
                    and (bool(high_signal_query_cues) or top_score < 1.5)
                ):
                    return True
    except Exception:
        pass


    if (
        qs._is_temporal_split_query(query)
        and (high_signal_query_cues & top_series_cues)
        and top_score >= 0.55
    ):
        return False

    if "debt_gdp_ratio" in query_cues and "debt_gdp_ratio" not in top_series_cues:
        return True
    if (
        "public_debt" in query_cues
        and "public_debt" not in top_series_cues
        and ("debt_service" in top_series_cues or "credit" in top_series_cues)
    ):
        return True
    if (high_signal_query_cues & strict_cues) and not (high_signal_query_cues & top_series_cues) and top_score < 0.8:
        return True
    if top_score < 0.25:
        return True

    if len(scored) > 1:
        if score_gap < 0.2 and top_score < 0.7:
            second_series_cues = _extract_indicator_cues(
                _series_text_for_relevance(scored[1][1])
            )
            if top_series_cues != second_series_cues:
                return True

    return False


# ====================================================================
# Post-fetch uncertain result clarification
# ====================================================================

def build_uncertain_result_clarification(
    qs: Any,
    conversation_id: str,
    query: str,
    intent: Optional[ParsedIntent],
    data: List[Any],
    processing_steps: Optional[List[Any]] = None,
) -> Optional[QueryResponse]:
    """Return a clarification response with options when series selection is uncertain."""
    if intent and _is_exact_match_locked(intent.parameters):
        return None
    if intent and intent.indicators and len(intent.indicators) > 1:
        return None
    if not intent or not qs._needs_indicator_clarification(query, data, intent):
        return None

    scored = sorted(
        (
            (_score_series_relevance(query, series), series)
            for series in (data or [])
        ),
        key=lambda item: item[0],
        reverse=True,
    )
    top_score, top_series = scored[0] if scored else (-1.0, None)
    top_meta = getattr(top_series, "metadata", None) if top_series else None
    top_provider = normalize_provider_name(getattr(top_meta, "source", "") or "") if top_meta else ""
    top_code = str(getattr(top_meta, "seriesId", "") or "").strip().upper() if top_meta else ""
    query_cues = _extract_indicator_cues(query)
    top_series_cues = _extract_indicator_cues(_series_text_for_relevance(top_series))
    high_signal_query_cues = {
        cue for cue in query_cues
        if cue not in {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
    }
    if top_meta and top_score >= 0.95 and (
        not high_signal_query_cues
        or bool(high_signal_query_cues & top_series_cues)
    ):
        return None

    options = dedupe_indicator_choice_options(
        qs,
        qs._collect_indicator_choice_options(query, intent),
    )

    if top_meta:
        current_provider = normalize_provider_name(getattr(top_meta, "source", "") or "")
        current_code = str(getattr(top_meta, "seriesId", "") or "").strip()
        current_name = format_indicator_option_name(
            qs=qs,
            provider=current_provider,
            code=current_code,
            name=getattr(top_meta, "indicator", "") or "",
            metadata={"description": str(getattr(top_meta, "description", "") or "")},
        )
        current_label = f"{current_name} from {getattr(top_meta, 'source', 'unknown source')}"
    else:
        current_label = "Unknown indicator"

    if options and top_meta and top_score >= 0.75:
        leading_option = options[0].upper()
        provider_token = f"[{top_provider}]"
        code_token = f"({top_code})" if top_code else ""
        if provider_token in leading_option and (not code_token or code_token in leading_option):
            return None

    if len(options) < 2 and top_meta:
        current_code = str(getattr(top_meta, "seriesId", "") or "").strip()
        current_provider = normalize_provider_name(getattr(top_meta, "source", "") or "")
        if not _is_placeholder_indicator_code(current_code):
            current_name = format_indicator_option_name(
                qs=qs,
                provider=current_provider,
                code=current_code,
                name=getattr(top_meta, "indicator", "") or "",
                metadata={"description": str(getattr(top_meta, "description", "") or "")},
            )
            current_option = f"[{current_provider}] {current_name} ({current_code})"
            options.insert(0, current_option)

    options = dedupe_indicator_choice_options(qs, options)
    mismatch_hint = build_indicator_mismatch_hint(query, top_series)
    directional_conflict = _has_directional_conflict(high_signal_query_cues, top_series_cues)
    severe_mismatch = bool(mismatch_hint) and (directional_conflict or top_score < 0.25)
    distinct_options: set[tuple[str, str]] = set()
    for option in options:
        parsed = parse_indicator_option(option)
        if not parsed:
            continue
        provider_name, indicator_code = parsed
        code_upper = str(indicator_code).strip().upper()
        if _is_placeholder_indicator_code(code_upper):
            continue
        distinct_options.add((provider_name, code_upper))

    if top_provider and top_code and not _is_placeholder_indicator_code(top_code):
        distinct_options.add((top_provider, top_code))

    if len(distinct_options) < 2 or len(options) < 2:
        if not severe_mismatch:
            return None
        clarification_questions = [
            "The current indicator match may be wrong for your request.",
            f"Current match: {current_label}",
            mismatch_hint,
            "Please reply with the exact indicator name or code you want (for example, NE.IMP.GNFS.ZS).",
        ]
        return QueryResponse(
            conversationId=conversation_id,
            intent=intent,
            clarificationNeeded=True,
            clarificationQuestions=clarification_questions,
            clarificationOptions=build_clarification_options(qs, options),
            processingSteps=processing_steps,
        )

    clarification_questions = [
        "I found multiple plausible indicators and the current match is uncertain.",
        f"Current match: {current_label}",
    ]
    if mismatch_hint:
        clarification_questions.append(mismatch_hint)
    clarification_questions.append("Please choose one option:")
    clarification_questions.extend(
        f"{idx}. {option}" for idx, option in enumerate(options, start=1)
    )
    clarification_questions.append(
        "Reply with the option number (for example, 1) or the exact indicator text you want."
    )

    store_pending_indicator_options(
        qs=qs,
        conversation_id=conversation_id,
        query=query,
        intent=intent,
        options=options,
        question_lines=clarification_questions,
    )

    return QueryResponse(
        conversationId=conversation_id,
        intent=intent,
        clarificationNeeded=True,
        clarificationQuestions=clarification_questions,
        clarificationOptions=build_clarification_options(qs, options),
        processingSteps=processing_steps,
    )


def build_indicator_mismatch_hint(query: str, top_series: Any) -> Optional[str]:
    """Generate a concise mismatch explanation for uncertain indicator matches."""
    if top_series is None:
        return None

    query_cues = _extract_indicator_cues(query)
    top_series_cues = _extract_indicator_cues(_series_text_for_relevance(top_series))
    high_signal_query_cues = {
        cue for cue in query_cues
        if cue not in {"gdp", "tenor_2y", "tenor_10y", "tenor_30y", "discontinued"}
    }

    query_direction = _single_directional_cue(high_signal_query_cues)
    if query_direction and _has_directional_conflict(high_signal_query_cues, top_series_cues):
        opposite = "exports" if query_direction == "import" else "imports"
        return (
            f"Potential mismatch: your query asks for {query_direction}s, "
            f"but the current match appears closer to {opposite}."
        )

    if high_signal_query_cues and not (high_signal_query_cues & top_series_cues):
        cue_labels = {
            "import": "imports",
            "export": "exports",
            "trade_balance": "trade balance",
            "trade_openness": "trade openness",
            "debt_gdp_ratio": "debt-to-GDP ratio",
            "public_debt": "public debt",
            "debt_service": "debt service",
            "gdp_deflator": "GDP deflator",
            "hicp": "HICP",
            "producer_price": "producer prices",
            "real_effective_exchange_rate": "real effective exchange rate",
            "bond_yield": "bond yields",
            "policy_rate": "policy rate",
            "money_supply": "money supply",
            "house_prices": "house prices",
            "current_account": "current account",
            "reserves": "foreign-exchange reserves",
        }
        readable = [
            cue_labels.get(cue, cue.replace("_", " "))
            for cue in sorted(high_signal_query_cues)
        ]
        if readable:
            return (
                "Potential mismatch: the current match does not clearly reflect "
                f"your requested concept ({', '.join(readable[:3])})."
            )

    return None


def build_no_data_indicator_clarification(
    qs: Any,
    conversation_id: str,
    query: str,
    intent: Optional[ParsedIntent],
    processing_steps: Optional[List[Any]] = None,
) -> Optional[QueryResponse]:
    """Offer indicator choices when no data is returned and intent may be ambiguous."""
    if not intent:
        return None
    if _is_exact_match_locked(intent.parameters):
        return None
    if intent.indicators and len(intent.indicators) > 1:
        return None

    options = dedupe_indicator_choice_options(
        qs,
        qs._collect_indicator_choice_options(query=query, intent=intent, max_options=4),
    )
    if len(options) < 2:
        for variant in _provider_native_query_variants(qs, query, intent)[1:]:
            options = dedupe_indicator_choice_options(
                qs,
                qs._collect_indicator_choice_options(query=variant, intent=intent, max_options=4),
            )
            if len(options) >= 2:
                logger.info(
                    "No-data clarification recovered options via simplified query variant: %s",
                    variant,
                )
                break
    if len(options) < 2:
        return None

    clarification_questions = [
        "I couldn't find data with the current indicator selection.",
        "Please choose the indicator you intended:",
    ]
    clarification_questions.extend(
        f"{idx}. {option}" for idx, option in enumerate(options, start=1)
    )
    clarification_questions.append(
        "Reply with the option number (for example, 1) or the exact indicator text you want."
    )

    store_pending_indicator_options(
        qs=qs,
        conversation_id=conversation_id,
        query=query,
        intent=intent,
        options=options,
        question_lines=clarification_questions,
    )

    return QueryResponse(
        conversationId=conversation_id,
        intent=intent,
        clarificationNeeded=True,
        clarificationQuestions=clarification_questions,
        clarificationOptions=build_clarification_options(qs, options),
        processingSteps=processing_steps,
    )


def _provider_native_query_variants(
    qs: Any,
    query: str,
    intent: Optional[ParsedIntent],
) -> list[str]:
    """Build simplified variants for explicit-provider long-tail direct queries.

    The goal is framework-level recovery for verbose provider-native titles that
    are too specific or metadata-heavy to execute directly but can still yield a
    useful clarification set when reduced to their semantic core.
    """
    original = str(query or "").strip()
    if not original:
        return [original]

    provider = normalize_provider_name((intent.apiProvider if intent else "") or "")
    stripped = re.sub(
        r"\bfrom\s+(world\s*bank|imf|eurostat|oecd|bis|statistics\s+canada|statscan|fred|coingecko|exchange ?rate)\b",
        " ",
        original,
        flags=re.IGNORECASE,
    )
    stripped = re.sub(r"\s+", " ", stripped).strip(" ,;:-")

    countries = []
    try:
        countries = qs._extract_countries_from_query(original)
    except Exception:
        countries = []
    country_prefix = ""
    if countries:
        country_prefix = f"{countries[0]} "

    variants = [original]
    if stripped and stripped != original:
        variants.append(stripped)

    if "," in stripped:
        segments = [segment.strip(" ,;:-") for segment in stripped.split(",") if segment.strip(" ,;:-")]
    else:
        segments = [segment.strip() for segment in re.split(r"\s+-\s+", stripped) if segment.strip()]

    noise_segments = {
        "IMF": {
            "the definition", "definition", "balance of payments", "goods and services", "services",
            "prices", "labor markets", "monthly", "persons", "number of", "all commodities",
            "national currency", "us dollars", "euros", "index",
        },
        "EUROSTAT": {
            "by analytical categories", "based on coicop 2018", "nominal and real expenditures",
        },
        "OECD": {
            "local areas", "other", "all countries", "harmonized definition",
        },
        "BIS": {
            "comparative tables type 1", "comparative tables type 2", "comparative tables type 3",
        },
    }.get(provider, set())

    simplified_segments: list[str] = []
    for segment in segments:
        lowered = re.sub(r"\[[^\]]+\]", "", segment.lower()).strip()
        lowered = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
        if not lowered:
            continue
        if lowered in noise_segments:
            continue
        if len(lowered.split()) <= 1 and lowered in {"the", "of", "and"}:
            continue
        simplified_segments.append(segment)

    if simplified_segments:
        tail = simplified_segments[-3:]
        compact = " ".join(part.strip() for part in tail if part.strip())
        compact = re.sub(r"\s+", " ", compact).strip(" ,;:-")
        if compact:
            candidate = f"{country_prefix}{compact}".strip()
            if candidate not in variants:
                variants.append(candidate)

    if len(simplified_segments) >= 2:
        longest = sorted(
            simplified_segments,
            key=lambda part: (len(re.sub(r"[^a-z0-9]+", " ", part.lower()).split()), len(part)),
            reverse=True,
        )[:2]
        candidate = f"{country_prefix}{' '.join(longest)}".strip()
        candidate = re.sub(r"\s+", " ", candidate).strip(" ,;:-")
        if candidate and candidate not in variants:
            variants.append(candidate)

    return variants


# ====================================================================
# Provider indicator code format detection
# ====================================================================

def looks_like_provider_indicator_code(provider: str, indicator: str) -> bool:
    """Heuristic check for provider-native indicator code formats.

    Returns True only when *indicator* looks like a provider-native series ID
    (e.g. ``UNRATE``, ``SP.POP.TOTL``, ``PCPIPCH``), NOT a plain-English
    economic term (e.g. ``inflation``, ``unemployment rate``).
    """
    if not indicator:
        return False

    indicator_text = str(indicator).strip()
    if not indicator_text:
        return False

    # Plain-English indicator names should never be treated as provider codes.

    # Contains a space → natural language, not a code
    if " " in indicator_text:
        return False

    # Distinguish real codes from English words using word-morphology patterns.
    # English economic terms have recognizable suffixes (inflation, unemployment,
    # population, manufacturing, etc.). Provider codes are abbreviations or
    # concatenated acronyms (CPIAUCSL, UNRATE, T10Y2Y, prc_hicp_manr) that
    # don't match these suffixes.
    _lower = indicator_text.lower()
    _ENGLISH_SUFFIXES = ("tion", "ment", "ness", "ity", "ing", "ure", "ism",
                         "ance", "ence", "ory", "ies", "ous", "ble", "ive",
                         "age", "ure", "dom")
    has_code_syntax = (
        any(ch in indicator_text for ch in "_@.-")
        or any(ch.isdigit() for ch in indicator_text)
    )
    if not has_code_syntax and any(_lower.endswith(s) for s in _ENGLISH_SUFFIXES):
        return False

    code_upper = indicator_text.upper()
    provider_upper = normalize_provider_name(provider)

    if provider_upper in {"WORLDBANK", "WORLD BANK"}:
        # WorldBank public REST IDs are broader than dotted WDI codes:
        # source-specific exact codes may be lower/mixed-case and may use
        # underscores or digits (for example ``CoHD_v_ss`` and
        # ``per_si_allsi.adq_pop_tot``).  Plain short acronyms such as ``TOT``
        # are accepted only by the catalog-backed explicit-code fast path, not
        # by this shape-only guard.
        return bool(
            re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\-]{1,127}", indicator_text)
            and ("." in indicator_text or "_" in indicator_text or any(ch.isdigit() for ch in indicator_text))
        )

    if provider_upper == "BIS":
        return bool(
            code_upper.startswith("WS_")
            or code_upper.startswith("BIS_WS_")
            or re.fullmatch(r"BIS\.[A-Z0-9_]{3,}", code_upper)
        )

    if provider_upper == "IMF":
        return bool(re.fullmatch(r"[A-Z0-9][A-Z0-9_\.]{2,}", code_upper))

    if provider_upper == "FRED":
        return bool(re.fullmatch(r"[A-Z0-9]{3,}", code_upper))

    if provider_upper == "EUROSTAT":
        return bool(re.fullmatch(r"[A-Z0-9_@\.]{4,}", code_upper))

    if provider_upper == "OECD":
        return bool(re.fullmatch(r"[A-Z0-9_@\.]{3,}", code_upper))

    if provider_upper == "COMTRADE":
        return bool(re.fullmatch(r"(?:HS)?[0-9]{2,6}", code_upper))

    if provider_upper in {"STATSCAN", "STATISTICS CANADA"}:
        return bool(re.fullmatch(r"[A-Z0-9_]{3,}", code_upper))

    return False
