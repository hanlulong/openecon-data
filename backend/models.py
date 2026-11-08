import logging
import math
from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)

#
# Core data structures shared across the backend
#


class DataPoint(BaseModel):
    """A single data point with date and value.

    Values are sanitized to ensure NaN and infinity are converted to None
    for proper JSON serialization.
    """
    date: str
    value: float | None

    @field_validator('value', mode='before')
    @classmethod
    def sanitize_float_value(cls, v):
        """Sanitize float values - convert NaN/Infinity to None.

        This is a critical infrastructure fix that prevents JSON serialization
        errors like 'Out of range float values are not JSON compliant: nan'.
        """
        if v is None:
            return None
        try:
            # Handle string representations of special values
            if isinstance(v, str):
                v_lower = v.lower().strip()
                if v_lower in ('nan', 'null', 'none', '.', '', 'n/a', 'na', '-'):
                    return None
                if v_lower in ('inf', 'infinity', '-inf', '-infinity'):
                    return None
                v = float(v)
            # Check for NaN and infinity
            if isinstance(v, float):
                if math.isnan(v) or math.isinf(v):
                    return None
            return v
        except (ValueError, TypeError):
            return None


class Metadata(BaseModel):
    source: str
    indicator: str
    country: Optional[str] = None
    frequency: str
    unit: str
    lastUpdated: str = ""
    seriesId: Optional[str] = None
    apiUrl: Optional[str] = None
    sourceUrl: Optional[str] = None  # Human-readable URL for data verification

    # Enhanced metadata fields for detailed series information
    seasonalAdjustment: Optional[str] = None  # e.g., "Seasonally adjusted", "Not seasonally adjusted"
    dataType: Optional[str] = None  # e.g., "Level", "Change", "Percent Change", "Index"
    priceType: Optional[str] = None  # e.g., "Chained (2017) dollars", "Current prices"
    description: Optional[str] = None  # Short description; runtime relevance/fallback/scope checks read it
    notes: Optional[List[str]] = None  # Transparency disclosures (coverage gaps, substitutions), always shown
    # DISPLAY-ONLY long-form provider prose (definition, methodology, sources),
    # rendered collapsed as "About this series". No selection, validation or
    # scope check reads it — keep it that way, so the length of a provider's
    # prose can never shift relevance scores or fail-closed guards.
    about: Optional[str] = None
    # "default" when the provider dispatch chose this geography because the
    # request named none (disclosure provenance, mirrors __time_scope_authority).
    geoScopeAuthority: Optional[str] = None
    scaleFactor: Optional[str] = None  # e.g., "millions", "billions", "thousands"
    startDate: Optional[str] = None  # First available data date
    endDate: Optional[str] = None  # Last available data date

    @field_validator("lastUpdated", mode="before")
    @classmethod
    def sanitize_last_updated(cls, v):
        if v is None:
            return ""
        return str(v)


class NormalizedData(BaseModel):
    metadata: Metadata
    data: List[DataPoint]


# Contract enum for ParsedIntent.queryType (see SimplifiedPrompt output schema).
_QUERY_TYPES = frozenset({"data_fetch", "informational", "analysis", "comparison"})
# Contract enum for ParsedIntent.informationalSubtype (which text answer an
# informational ask gets). Chosen by the LLM; absent means the handler's
# structural default. Both enums are stated in SimplifiedPrompt and the delta
# extractor prompt (test_informational_llm_authority checks they stay in sync).
_INFORMATIONAL_SUBTYPES = frozenset({"indicator_search", "data_sources", "indicator_count", "definition"})


def _normalize_contract_label(field: str, value: Any, allowed: frozenset, default: Optional[str]) -> Optional[str]:
    """Structural enum normalization for an LLM-emitted label.

    Case/whitespace are normalized; a missing value or one outside the
    contract enum becomes ``default``. A NON-EMPTY off-contract value is
    logged so label drift from the parse model stays measurable.
    """
    normalized = str(value or "").strip().lower()
    if normalized in allowed:
        return normalized
    if normalized:
        logger.info("Off-contract %s %r from the LLM coerced to %r", field, value, default)
    return default


class ParsedIntent(BaseModel):
    apiProvider: str
    indicators: List[str]
    parameters: dict[str, Any] = Field(default_factory=dict)
    clarificationNeeded: bool
    clarificationQuestions: Optional[List[str]] = None
    confidence: Optional[float] = None
    recommendedChartType: Optional[str] = Field(default=None)

    # Query type classification — determines routing path
    # data_fetch: standard data retrieval (default)
    # informational: questions about available data/indicators
    # analysis: complex analysis requiring Pro Mode
    # comparison: structured comparisons across entities
    queryType: Optional[str] = "data_fetch"
    # For queryType="informational": which text answer fits — indicator_search,
    # data_sources, indicator_count, or definition. Set by the parse LLM in the
    # same call; None means the handler's structural default.
    informationalSubtype: Optional[str] = None

    # Original query text for downstream processing (e.g., time period extraction)
    originalQuery: Optional[str] = None

    # Named sub-country region when the user asks about one (e.g. "Beijing" for
    # "北京GDP", "Ontario" for "Ontario unemployment"), else None. The country
    # stays the parent country — this is an ANNOTATION used at the result stage
    # to fail closed when a provider served national data for a sub-region it
    # cannot decompose to (Proposal B). It does NOT reroute or rewrite country.
    subnationalRegion: Optional[str] = None

    # ISO 639-1 language of the user's query ("en", "zh", "es", …) as detected
    # semantically by the parse LLM (never by regex/charset rules). Used to
    # render user-facing response strings in the user's language via the
    # user_messages catalog (Proposal C). Carried across follow-up turns.
    language: Optional[str] = None

    # Follow-up detection fields (populated by LLM when conversation context is provided)
    isFollowUp: bool = False
    followUpType: Optional[str] = None  # "country_change", "indicator_switch", "time_change", "provider_change", "pronoun_reuse", "clarification_answer"
    resolvedQuery: Optional[str] = None  # The explicit rewritten query if follow-up

    # Query decomposition for "all provinces", "each state", etc.
    needsDecomposition: Optional[bool] = False
    decompositionType: Optional[str] = None  # "provinces", "states", "regions", "countries"
    decompositionEntities: Optional[List[str]] = None  # e.g., ["Ontario", "Quebec", "BC", ...]
    useProMode: Optional[bool] = False  # Auto-switch to Pro Mode for complex aggregations

    @field_validator('queryType', mode='before')
    @classmethod
    def normalize_query_type(cls, v):
        # Structural normalization of the LLM's routing label. queryType is
        # parsed in Instructor JSON mode (no decode-time enum), so a strict
        # Literal would turn a stray value (mixed case, "ranking", null) into
        # retries or a failed parse on a DATA query. Case/whitespace are
        # normalized; anything missing or outside the contract enum takes the
        # data path. No keyword guess ever substitutes for the label.
        return _normalize_contract_label("queryType", v, _QUERY_TYPES, "data_fetch")

    @field_validator('informationalSubtype', mode='before')
    @classmethod
    def normalize_informational_subtype(cls, v):
        # Structural: tolerate case/whitespace; an off-contract value becomes
        # None (the handler's default) instead of failing the whole parse.
        return _normalize_contract_label("informationalSubtype", v, _INFORMATIONAL_SUBTYPES, None)

    @field_validator('recommendedChartType', mode='before')
    @classmethod
    def coerce_chart_type(cls, v):
        # A hard pattern here rejected any stray LLM value ("pie", "area",
        # "histogram") and failed the WHOLE parse — burning all retries and
        # dropping an otherwise-perfect intent (right provider/indicators/dates).
        # Coerce anything outside the renderable set to None so the frontend's
        # structural chart-type inference decides instead of the query aborting.
        if v is None:
            return None
        return v if str(v).strip().lower() in {"line", "bar", "scatter", "table"} else None

    @field_validator('apiProvider')
    @classmethod
    def api_provider_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('apiProvider must not be empty')
        return v

    @field_validator('indicators')
    @classmethod
    def indicators_not_empty(cls, v: List[str]) -> List[str]:
        if not v:
            raise ValueError('indicators must contain at least one item')
        return v

    @model_validator(mode='after')
    def clarification_consistency(self) -> 'ParsedIntent':
        if self.clarificationNeeded:
            if not self.clarificationQuestions:
                raise ValueError(
                    'clarificationQuestions must be non-empty when clarificationNeeded=true'
                )
        return self


class ClarificationOption(BaseModel):
    """One structured clarification choice shown to the user."""

    id: str
    label: str
    value: str
    provider: Optional[str] = None
    code: Optional[str] = None
    # Self-contained re-query string for STATELESS callers (API/agent clients
    # that never send conversationId back, so a numeric reply cannot be
    # matched to this menu). Present only when the string was verified at
    # build time to resolve, through the exact provider-code fast path, to this
    # option's provider + code + country scope without re-clarifying; absent
    # (None) whenever the scope cannot be expressed that way. Optional and
    # additive: existing clients that pick by id/value are unaffected.
    query: Optional[str] = None


class UnservedIndicator(BaseModel):
    """One requested indicator a served multi-indicator response could not include."""

    indicator: str
    # Structural failure class from the fetch outcome (exception type / empty
    # result), never parsed from message text: "no_data" (the provider
    # answered with nothing), "unavailable" (the provider could not be reached
    # — no verdict), "timeout", "error".
    reason: str


class ExecutionPlan(BaseModel):
    """Minimal typed execution contract for runtime verification.

    Phase 2 starts with a small typed plan that can grow into the fuller
    planner/provider boundary in later phases.
    """

    provider: str
    candidate_id: str
    fetch_strategy: str
    params: dict[str, Any] = Field(default_factory=dict)
    expected_shape: dict[str, Any] = Field(default_factory=dict)
    verification_checks: List[str] = Field(default_factory=list)
    provider_request: dict[str, Any] = Field(default_factory=dict)
    cache_identity: dict[str, Any] = Field(default_factory=dict)


class GeneratedFile(BaseModel):
    """Represents a file generated by Pro Mode code execution"""
    url: str  # URL path to access the file (e.g., /static/promode/file.png)
    name: str  # File name
    type: str  # File type: 'image', 'data', 'html', 'file'


class CodeExecutionResult(BaseModel):
    code: str
    output: str
    error: Optional[str] = None
    executionTime: Optional[float] = None
    files: Optional[List[GeneratedFile]] = None  # List of generated files


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=5000, description="Natural language query (max 5000 chars)")
    conversationId: Optional[str] = Field(None, max_length=100, description="Conversation ID for follow-ups")
    sessionId: Optional[str] = Field(None, max_length=100, description="Session ID for anonymous user tracking")


class ProcessingStep(BaseModel):
    """Represents a step in query processing for user feedback"""
    step: str  # e.g., "parsing_query", "searching_metadata", "fetching_data"
    description: str  # Human-readable description
    status: str = "completed"  # "pending", "in-progress", "completed", "error"
    duration_ms: Optional[float] = None  # How long this step took (only for completed)
    metadata: Optional[dict[str, Any]] = None  # Additional info about the step


class AlternativeSeries(BaseModel):
    """A related indicator the user might also want to explore."""
    code: str
    name: str
    provider: str
    description: Optional[str] = None
    apiUrl: Optional[str] = None


class QueryResponse(BaseModel):
    conversationId: str
    # Attribution travels in EVERY response (monetization plan step 1,
    # 2026-07-20): integrators relaying our answers surface who produced
    # them; the free tier's terms require keeping it intact.
    attribution: str = "Data by OpenEcon — https://data.openecon.ai"
    intent: Optional[ParsedIntent] = None
    data: Optional[List[NormalizedData]] = None
    clarificationNeeded: bool
    clarificationQuestions: Optional[List[str]] = None
    clarificationOptions: Optional[List[ClarificationOption]] = None
    error: Optional[str] = None
    message: Optional[str] = None
    codeExecution: Optional[CodeExecutionResult] = None
    isProMode: Optional[bool] = None
    processingSteps: Optional[List[ProcessingStep]] = None
    alternativeSeries: Optional[List[AlternativeSeries]] = None  # Related indicators user might want
    # Requested indicators a PARTIAL multi-indicator answer could not serve
    # (structured twin of the disclosure appended to ``message``). None when
    # every requested indicator was served or the query asked for only one.
    unservedIndicators: Optional[List[UnservedIndicator]] = None
    processingTimeMs: Optional[float] = None  # End-to-end query processing time in milliseconds
    # Anonymous free-query gate: how many of the free prompts this anonymous
    # session has used and the limit, so the UI can show "N free queries left".
    # registrationRequired=true means the limit was reached and the query was
    # NOT processed — the UI shows the registration wall. (All None/false for
    # registered users, who are exempt.)
    anonymousQueriesUsed: Optional[int] = None
    anonymousQueryLimit: Optional[int] = None
    registrationRequired: Optional[bool] = None
    # Internal flag: delta path already saved conversation state — skip
    # the guaranteed save in main.py to avoid overwriting the merged state.
    delta_state_saved: bool = Field(default=False, exclude=True)
    # Internal: FETCHED coverage fact (data_fetcher.WindowCoverage) behind a
    # no_data_in_requested_window response, so result-stage checks (the
    # subnational fail-closed) can test the served series' real metadata.
    # Never serialized.
    windowCoverage: Optional[Any] = Field(default=None, exclude=True)

    @field_validator("error", mode="before")
    @classmethod
    def _normalize_blank_error(cls, v):
        """The error field must be a meaningful code/message or None — never
        whitespace.

        Several construction paths pass ``error`` a runtime value that can be
        blank — most notably clarification builders doing ``error=str(exc)``
        for an exception with an empty message, or ``error=code_exec.get(
        "error")``. A whitespace-only error carries no information yet is
        truthy to some consumers and renders as a silent failure in the UI
        (``error=" "`` was observed live producing a blank result), while
        polluting telemetry. Normalizing any whitespace-only value to None at
        the model boundary guarantees no such value can escape from ANY setter,
        empty-data or not; real errors pass through untouched.
        """
        if isinstance(v, str) and not v.strip():
            return None
        return v


class StreamEvent(BaseModel):
    """Event sent during streaming query processing"""
    event: str  # "step", "data", "error", "done"
    data: dict[str, Any]  # Event-specific data


class ExportRequest(BaseModel):
    # Bounded: the endpoint is unauthenticated and fully re-materializes the
    # payload (Stata .dta is O(dates × series)); an unbounded list was a
    # single-request memory-exhaustion vector against the whole backend.
    data: List[NormalizedData] = Field(..., min_length=1, max_length=100)
    format: str
    filename: Optional[str] = None

    @field_validator("data")
    @classmethod
    def _bound_total_points(cls, v: List[NormalizedData]) -> List[NormalizedData]:
        total_points = sum(len(series.data or []) for series in v)
        if total_points > 200_000:
            raise ValueError(
                f"export too large: {total_points} data points exceeds the "
                "200,000-point limit; narrow the date range or series count"
            )
        return v


class User(BaseModel):
    id: str
    email: str
    passwordHash: str
    name: str
    createdAt: datetime
    lastLogin: Optional[datetime] = None


class RegisterRequest(BaseModel):
    email: str = Field(..., max_length=254)
    # Server-side floor so empty/trivial passwords never reach Supabase. The
    # frontend enforces a stronger policy (12+ chars, mixed case, digit); this
    # is the backstop and matches ResetPasswordRequest.
    password: str = Field(..., min_length=8, max_length=200)
    name: str = Field(..., max_length=200)
    # Optional institution/company (powers future "trusted by" social proof).
    institution: Optional[str] = Field(None, max_length=200)
    # Anonymous session to migrate on signup, so the user keeps the history they
    # built before hitting the registration wall.
    sessionId: Optional[str] = Field(None, max_length=100)


class LoginRequest(BaseModel):
    # Bounded like RegisterRequest: unbounded strings reach the auth backend
    # verbatim (cheap resource-abuse surface on an unauthenticated endpoint).
    email: str = Field(..., max_length=254)
    password: str = Field(..., max_length=256)


class ForgotPasswordRequest(BaseModel):
    """Request a password-reset email."""
    email: str = Field(..., max_length=254)


class ResetPasswordRequest(BaseModel):
    """Complete a password reset using the recovery token from the emailed link."""
    accessToken: str = Field(..., min_length=10, max_length=8192)
    password: str = Field(..., min_length=8, max_length=200)


class AuthUser(BaseModel):
    id: str
    email: str
    name: str
    createdAt: Optional[datetime] = None
    lastLogin: Optional[datetime] = None


class AuthResponse(BaseModel):
    success: bool
    token: Optional[str] = None
    user: Optional[AuthUser] = None
    error: Optional[str] = None
    # True when registration succeeded but the account must confirm their email
    # before they can log in (Supabase "Confirm email" is on). The UI shows a
    # "check your inbox" message instead of auto-logging the user in.
    emailVerificationRequired: Optional[bool] = None


class UserQueryHistory(BaseModel):
    id: str
    userId: str
    query: str
    conversationId: str
    intent: Optional[ParsedIntent] = None
    data: Optional[List[NormalizedData]] = None
    timestamp: datetime


class HealthCacheStats(BaseModel):
    keys: int
    hits: int
    misses: int
    ksize: int
    vsize: int


class HealthUserStats(BaseModel):
    totalUsers: int
    totalQueries: int


class HealthResponse(BaseModel):
    status: str
    timestamp: str
    environment: str
    services: dict[str, bool]
    cache: HealthCacheStats
    users: HealthUserStats
    promodeEnabled: bool = False


# --- Feedback context bounds -------------------------------------------------
# Session/conversation context is auto-collected by the client and is useful
# but not essential, so oversized context is TRUNCATED server-side (never a
# 422 that would lose the user's report). The request body as a whole is also
# capped at the ASGI layer (RequestBodyLimitMiddleware on /api/feedback).
from typing import Annotated  # noqa: E402  (kept local to the feedback models)

from pydantic import BeforeValidator  # noqa: E402

FEEDBACK_TRUNCATION_MARKER = "...[truncated]"
FEEDBACK_MAX_CONTEXT_FIELD_CHARS = 2_000     # each sessionInfo / short message field
FEEDBACK_MAX_MESSAGES_CHARS = 50_000         # formatted conversation transcript
FEEDBACK_MAX_RAW_MESSAGES = 100              # most recent rawMessages kept
FEEDBACK_MAX_RAW_MESSAGE_CHARS = 5_000       # each rawMessages[].content


def _feedback_clip(max_chars: int, keep: str = "head") -> BeforeValidator:
    """Truncate an over-long string to max_chars (marker included).

    keep="tail" keeps the END (most recent turns of a transcript)."""
    room = max_chars - len(FEEDBACK_TRUNCATION_MARKER)

    def _clip(value: Any) -> Any:
        if isinstance(value, str) and len(value) > max_chars:
            if keep == "tail":
                return FEEDBACK_TRUNCATION_MARKER + value[-room:]
            return value[:room] + FEEDBACK_TRUNCATION_MARKER
        return value

    return BeforeValidator(_clip)


_FeedbackContextStr = Annotated[str, _feedback_clip(FEEDBACK_MAX_CONTEXT_FIELD_CHARS)]


class FeedbackSessionInfo(BaseModel):
    """Session information collected with feedback"""
    url: _FeedbackContextStr
    userAgent: _FeedbackContextStr
    timestamp: _FeedbackContextStr
    screenSize: _FeedbackContextStr
    language: _FeedbackContextStr
    timezone: _FeedbackContextStr
    referrer: _FeedbackContextStr


class FeedbackConversationMessage(BaseModel):
    """Simplified message info for feedback"""
    role: _FeedbackContextStr
    content: Annotated[str, _feedback_clip(FEEDBACK_MAX_RAW_MESSAGE_CHARS)]
    timestamp: _FeedbackContextStr
    hasData: bool
    dataCount: int
    isProMode: Optional[bool] = None


class FeedbackConversation(BaseModel):
    """Conversation data included with feedback"""
    # Formatted string of messages; keep the most recent turns when clipped.
    messages: Annotated[str, _feedback_clip(FEEDBACK_MAX_MESSAGES_CHARS, keep="tail")]
    messageCount: int
    conversationId: Optional[_FeedbackContextStr] = None
    rawMessages: Optional[List[FeedbackConversationMessage]] = None

    @field_validator("rawMessages", mode="before")
    @classmethod
    def _keep_recent_raw_messages(cls, value: Any) -> Any:
        if isinstance(value, list) and len(value) > FEEDBACK_MAX_RAW_MESSAGES:
            return value[-FEEDBACK_MAX_RAW_MESSAGES:]
        return value


class FeedbackRequest(BaseModel):
    """User feedback request model"""
    type: str = Field(..., pattern="^(bug|feature|other)$", description="Type of feedback")
    message: Optional[str] = Field(None, max_length=10000, description="User's feedback message")
    email: Optional[str] = Field(None, max_length=254, description="User's email for follow-up")
    sessionInfo: Optional[FeedbackSessionInfo] = None
    conversation: Optional[FeedbackConversation] = None
    # Client CLAIMS only (the endpoint is anonymous-capable): the verified
    # identity comes from the bearer token, never from these fields.
    userId: Optional[str] = Field(None, max_length=100, description="Client-claimed user id (unverified)")
    userName: Optional[str] = Field(None, max_length=200, description="Client-claimed user name (unverified)")


class FeedbackResponse(BaseModel):
    """Response after submitting feedback"""
    success: bool
    message: str
    feedbackId: Optional[str] = None
