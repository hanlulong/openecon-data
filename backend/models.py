import math
from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


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
    description: Optional[str] = None  # Full description of the series
    notes: Optional[List[str]] = None  # Additional notes or footnotes
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


class ParsedIntent(BaseModel):
    apiProvider: str
    indicators: List[str]
    parameters: dict[str, Any] = Field(default_factory=dict)
    clarificationNeeded: bool
    clarificationQuestions: Optional[List[str]] = None
    confidence: Optional[float] = None
    recommendedChartType: Optional[str] = Field(default=None, pattern="^(line|bar|scatter|table)$")

    # Query type classification — determines routing path
    # data_fetch: standard data retrieval (default)
    # informational: questions about available data/indicators
    # analysis: complex analysis requiring Pro Mode
    # comparison: structured comparisons across entities
    queryType: Optional[str] = "data_fetch"

    # Original query text for downstream processing (e.g., time period extraction)
    originalQuery: Optional[str] = None

    # Follow-up detection fields (populated by LLM when conversation context is provided)
    isFollowUp: bool = False
    followUpType: Optional[str] = None  # "country_change", "indicator_switch", "time_change", "provider_change", "pronoun_reuse", "clarification_answer"
    resolvedQuery: Optional[str] = None  # The explicit rewritten query if follow-up

    # Query decomposition for "all provinces", "each state", etc.
    needsDecomposition: Optional[bool] = False
    decompositionType: Optional[str] = None  # "provinces", "states", "regions", "countries"
    decompositionEntities: Optional[List[str]] = None  # e.g., ["Ontario", "Quebec", "BC", ...]
    useProMode: Optional[bool] = False  # Auto-switch to Pro Mode for complex aggregations

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
    processingTimeMs: Optional[float] = None  # End-to-end query processing time in milliseconds
    # Internal flag: delta path already saved conversation state — skip
    # the guaranteed save in main.py to avoid overwriting the merged state.
    delta_state_saved: bool = Field(default=False, exclude=True)


class StreamEvent(BaseModel):
    """Event sent during streaming query processing"""
    event: str  # "step", "data", "error", "done"
    data: dict[str, Any]  # Event-specific data


class ExportRequest(BaseModel):
    data: List[NormalizedData]
    format: str
    filename: Optional[str] = None


class User(BaseModel):
    id: str
    email: str
    passwordHash: str
    name: str
    createdAt: datetime
    lastLogin: Optional[datetime] = None


class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str


class LoginRequest(BaseModel):
    email: str
    password: str


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


class FeedbackSessionInfo(BaseModel):
    """Session information collected with feedback"""
    url: str
    userAgent: str
    timestamp: str
    screenSize: str
    language: str
    timezone: str
    referrer: str


class FeedbackConversationMessage(BaseModel):
    """Simplified message info for feedback"""
    role: str
    content: str
    timestamp: str
    hasData: bool
    dataCount: int
    isProMode: Optional[bool] = None


class FeedbackConversation(BaseModel):
    """Conversation data included with feedback"""
    messages: str  # Formatted string of messages
    messageCount: int
    conversationId: Optional[str] = None
    rawMessages: Optional[List[FeedbackConversationMessage]] = None


class FeedbackRequest(BaseModel):
    """User feedback request model"""
    type: str = Field(..., pattern="^(bug|feature|other)$", description="Type of feedback")
    message: Optional[str] = Field(None, max_length=10000, description="User's feedback message")
    email: Optional[str] = Field(None, max_length=254, description="User's email for follow-up")
    sessionInfo: Optional[FeedbackSessionInfo] = None
    conversation: Optional[FeedbackConversation] = None
    userId: Optional[str] = None
    userName: Optional[str] = None


class FeedbackResponse(BaseModel):
    """Response after submitting feedback"""
    success: bool
    message: str
    feedbackId: Optional[str] = None
