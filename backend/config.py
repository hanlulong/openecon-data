import os
import sys
from functools import lru_cache
from typing import List

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .embedding_utils import is_openai_embedding_model


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    environment: str = Field(default="development", alias="NODE_ENV")
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    openai_org_id: str | None = Field(default=None, alias="OPENAI_ORG_ID")
    fred_api_key: str | None = Field(default=None, alias="FRED_API_KEY")
    comtrade_api_key: str | None = Field(default=None, alias="COMTRADE_API_KEY")
    coingecko_api_key: str | None = Field(default=None, alias="COINGECKO_API_KEY")
    coingecko_base_url: str = Field(default="https://api.coingecko.com/api/v3")
    worldbank_base_url: str = Field(default="https://api.worldbank.org/v2")
    fred_base_url: str = Field(default="https://api.stlouisfed.org/fred")
    comtrade_base_url: str = Field(default="https://comtradeapi.un.org/data/v1/get")
    statscan_base_url: str = Field(default="https://www150.statcan.gc.ca/t1/wds/rest")
    imf_base_url: str = Field(default="https://www.imf.org/external/datamapper/api/v1")
    exchangerate_base_url: str = Field(default="https://open.er-api.com/v6")
    exchangerate_api_key: str | None = Field(default=None, alias="EXCHANGERATE_API_KEY")
    bis_base_url: str = Field(default="https://stats.bis.org/api/v1")
    eurostat_base_url: str = Field(default="https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1")
    oecd_base_url: str = Field(default="https://sdmx.oecd.org/public/rest")
    # Supabase Configuration (optional, will use mock auth if not provided)
    supabase_url: str | None = Field(default=None, alias="SUPABASE_URL")
    supabase_anon_key: str | None = Field(default=None, alias="SUPABASE_ANON_KEY")
    supabase_service_key: str | None = Field(default=None, alias="SUPABASE_SERVICE_KEY")

    jwt_secret: str = Field(..., alias="JWT_SECRET")  # Required - no insecure default
    jwt_expiration_days: int = Field(default=7, alias="JWT_EXPIRES_DAYS")
    allowed_origins: List[str] = Field(
        default_factory=lambda: [],  # Changed from ["*"] to require explicit configuration
        alias="ALLOWED_ORIGINS"
    )
    app_url: str = Field(default="https://data.openecon.ai", alias="APP_URL")
    # Trusted reverse-proxy source IPs whose X-Forwarded-For header is honored.
    # Default: loopback only (Apache/nginx on same host). For CDN/LB termination,
    # set TRUSTED_PROXIES to the comma-separated source IPs (or CIDRs not supported here).
    trusted_proxies: List[str] = Field(
        default_factory=lambda: ["127.0.0.1", "::1"],
        alias="TRUSTED_PROXIES",
        description="IPs whose X-Forwarded-For header is trusted for client-IP attribution"
    )

    # LLM Configuration
    # LLM_PROVIDER options: openrouter, vllm, ollama, lm-studio
    # Match .env.example defaults so a stock local setup uses OpenRouter unless
    # the operator explicitly opts into a local model server.
    llm_provider: str = Field(default="openrouter", alias="LLM_PROVIDER")
    llm_model: str | None = Field(default="openai/gpt-4o-mini", alias="LLM_MODEL")
    llm_base_url: str | None = Field(default="http://localhost:8000", alias="LLM_BASE_URL")
    llm_timeout: int = Field(default=120, alias="LLM_TIMEOUT")  # Higher default for local models
    # vLLM-specific settings (for SSH-tunneled or local vLLM servers)
    vllm_api_key: str | None = Field(default=None, alias="VLLM_API_KEY")
    # Dedicated indicator-selector LLM (optional). When BOTH are set, the
    # IndicatorSelector adjudicates candidates against this endpoint FIRST
    # (e.g. an SSH-tunneled local vLLM serving a stronger model) and falls
    # back to the main LLM provider automatically if the endpoint is down.
    # Selection quality directly drives indicator accuracy across all 330K
    # indicators, so it is worth a stronger model than the parse path needs.
    selector_llm_model: str | None = Field(default=None, alias="SELECTOR_LLM_MODEL")
    selector_llm_base_url: str | None = Field(default=None, alias="SELECTOR_LLM_BASE_URL")
    # Hosted last-resort model (OpenRouter) used by BOTH the parse path and the
    # indicator selector when the configured provider fails at runtime (e.g.
    # the local-LLM tunnel drops). One knob so the whole fallback chain stays
    # coherent. Default per the 2026-06 selector bake-off: hosted gpt-oss-120b
    # beat gpt-4o-mini 10/12 vs 6/12 and costs ~3x less. It is a reasoning
    # model — callers must budget max_tokens for reasoning before the answer.
    llm_fallback_model: str = Field(
        default="openai/gpt-oss-120b", alias="LLM_FALLBACK_MODEL"
    )
    # Model-specific prompt configuration
    llm_strip_thinking: bool = Field(
        default=True,
        alias="LLM_STRIP_THINKING",
        description="Strip thinking tags from reasoning model outputs"
    )
    disable_mcp: bool = Field(default=False, alias="DISABLE_MCP")
    disable_background_jobs: bool = Field(default=False, alias="DISABLE_BACKGROUND_JOBS")
    use_langchain_orchestrator: bool = Field(
        default=True,  # Enabled by default for intelligent query routing
        alias="USE_LANGCHAIN_ORCHESTRATOR",
        description="Use LangChain orchestrator with LangGraph for intelligent query routing and state persistence"
    )
    embedding_model: str = Field(
        default="sentence-transformers/all-MiniLM-L6-v2",
        alias="EMBEDDING_MODEL",
        description="Embedding model for vector search. Options: all-MiniLM-L6-v2 (384d, default), BAAI/bge-base-en-v1.5 (768d, +6.6% accuracy). Rebuild FAISS index after changing."
    )
    embedding_dimensions: int | None = Field(
        default=None,
        alias="EMBEDDING_DIMENSIONS",
        description="Optional embedding vector dimension override"
    )
    use_indicator_hybrid_rerank: bool = Field(
        default=True,
        alias="USE_INDICATOR_HYBRID_RERANK",
        description="Enable hybrid indicator matching (FTS + vector RRF fusion + fuzzy scoring)"
    )
    use_outcome_decision_stage: bool = Field(
        default=False,
        alias="USE_OUTCOME_DECISION_STAGE",
        description="Enable the shared prefetch outcome-decision stage for direct-answer vs clarify vs unsupported control flow"
    )
    use_minimal_execution_plan: bool = Field(
        default=False,
        alias="USE_MINIMAL_EXECUTION_PLAN",
        description="Enable the minimal typed execution-plan skeleton used by Phase 2 verification"
    )
    use_post_fetch_semantic_judge: bool = Field(
        default=False,
        alias="USE_POST_FETCH_SEMANTIC_JUDGE",
        description="Enable the shared post-fetch semantic verification stage"
    )
    use_staged_state_commit: bool = Field(
        default=False,
        alias="USE_STAGED_STATE_COMMIT",
        description="Commit conversation state only after verification succeeds on the new path"
    )
    indicator_rrf_k: int = Field(
        default=60,
        alias="INDICATOR_RRF_K",
        description="RRF k-constant for blending lexical and semantic indicator candidate ranks"
    )
    indicator_vector_candidates: int = Field(
        default=40,
        alias="INDICATOR_VECTOR_CANDIDATES",
        description="Number of semantic candidates to retrieve for indicator hybrid ranking"
    )
    # Phase 2.2: switch between the legacy magic-constant fusion in
    # IndicatorSelector._effective_rank and canonical parameterless RRF
    # (k = INDICATOR_RRF_K = 60). Default flipped to "rrf" 2026-06-12 after
    # the legacy merge was shown to be broken by construction: its FTS-only
    # synthetic score (0.55) sits BELOW the embedding-score floor on real
    # queries, so the [:top_k] cut discarded 100% of lexical-only recall —
    # canonical series (M2SL, DGS10, UNRATE) never reached the LLM. RRF
    # interleaves both evidence sources rank-wise with one constant.
    indicator_fusion: str = Field(
        default="rrf",
        alias="INDICATOR_FUSION",
        description="Indicator-retrieval fusion strategy. 'rrf' = canonical Reciprocal Rank Fusion; 'legacy' = retired score-aware merge (rollback only)."
    )
    # Phase 2.1: per-stage telemetry baseline for IndicatorSelector. Default
    # disabled to avoid log volume on dev. Enable for shadow validation runs.
    indicator_telemetry_enabled: bool = Field(
        default=False,
        alias="INDICATOR_TELEMETRY_ENABLED",
        description="Emit structured per-query telemetry from IndicatorSelector covering FTS5/embedding/fused/final stages."
    )
    # Phase 1.6: per-call LLM token-usage telemetry. Default disabled; when
    # enabled, every LLM completion emits a one-line JSON record with
    # prompt/completion/total token counts and the originating call site.
    # Used to evaluate the include_provider_hints gating cost-benefit and
    # to monitor token spend across providers/models.
    llm_telemetry_enabled: bool = Field(
        default=False,
        alias="LLM_TELEMETRY_ENABLED",
        description="Emit structured token-usage telemetry on every LLM completion."
    )
    # Pro Mode configuration - cross-platform defaults
    promode_enabled: bool = Field(
        default=False,
        alias="PROMODE_ENABLED",
        description="Enable Pro Mode code execution (disabled by default for security)"
    )
    promode_public_dir: str | None = Field(default=None, alias="PROMODE_PUBLIC_DIR")
    promode_session_dir: str | None = Field(default=None, alias="PROMODE_SESSION_DIR")

    # Conversation TTL
    conversation_ttl_hours: int = Field(
        default=24,
        alias="CONVERSATION_TTL_HOURS",
        description="Conversation expiration time in hours (Redis TTL and in-memory max age)"
    )

    # Free prompts an anonymous (unregistered) visitor may run before the
    # registration wall. Registered users are exempt. 0 disables the gate.
    anon_query_limit: int = Field(
        default=20,
        alias="ANON_QUERY_LIMIT",
        description="Number of free queries for anonymous users before registration is required (0 = unlimited)."
    )

    # Anonymous-identity resolution mode for the free-query gate.
    #   legacy  = original behavior only (key on frontend sessionId); no token
    #             issuance, no shadow logging.
    #   shadow  = keep the legacy gating decision UNCHANGED, but additionally
    #             resolve+log the new token/IP-based identity and issue anon
    #             session tokens so the scheme can be measured before cutover.
    #   enforce = (NOT IMPLEMENTED) would gate on the resolved identity itself.
    anon_identity_mode: str = Field(
        default="shadow",
        alias="ANON_IDENTITY_MODE",
        description="Anonymous identity mode: 'legacy' (session-id only), 'shadow' (measure new scheme without changing gating), or 'enforce' (not implemented)."
    )

    email_confirm_redirect_url: str = Field(
        default="https://data.openecon.ai/chat?confirmed=1",
        alias="EMAIL_CONFIRM_REDIRECT_URL",
        description="Where Supabase redirects after a new user clicks the email-confirmation link. Must be in the Supabase Auth redirect allow list."
    )

    password_reset_redirect_url: str = Field(
        default="https://data.openecon.ai/reset-password",
        alias="PASSWORD_RESET_REDIRECT_URL",
        description="Where Supabase redirects after a user clicks the password-reset link. Must be in the Supabase Auth redirect allow list."
    )

    # Sync /api/query and /api/query/pro request-level deadline
    query_timeout_seconds: int = Field(
        default=120,
        alias="QUERY_TIMEOUT_SECONDS",
        description="Server-side deadline for non-streaming query endpoints. Beyond this, the request returns 504 with error='request_timeout'."
    )
    pro_query_timeout_seconds: int = Field(
        default=180,
        alias="PRO_QUERY_TIMEOUT_SECONDS",
        description="Server-side deadline for /api/query/pro (Grok code generation + sandboxed execution can be slower than standard queries)."
    )

    # Vector Search Configuration
    enable_metadata_loading: bool = Field(
        default=True,
        alias="ENABLE_METADATA_LOADING",
        description="Enable metadata loading on startup (enabled by default with FAISS)"
    )
    metadata_loading_timeout: int = Field(
        default=60,
        alias="METADATA_LOADING_TIMEOUT",
        description="Timeout for metadata loading in seconds"
    )
    use_faiss_instead_of_chroma: bool = Field(
        default=True,
        alias="USE_FAISS_INSTEAD_OF_CHROMA",
        description="Use FAISS for vector search instead of ChromaDB (default: True for performance)"
    )
    vector_search_cache_dir: str = Field(
        default="backend/data/faiss_index",
        alias="VECTOR_SEARCH_CACHE_DIR",
        description="Directory to store FAISS index files"
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,  # Treat empty strings as not set
    )

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, v):
        """Parse ALLOWED_ORIGINS from comma-separated string or list"""
        if isinstance(v, str):
            # Split by comma and strip whitespace
            return [origin.strip() for origin in v.split(",") if origin.strip()]
        return v or []

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def parse_trusted_proxies(cls, v):
        """Parse TRUSTED_PROXIES from comma-separated string or list"""
        if isinstance(v, str):
            return [ip.strip() for ip in v.split(",") if ip.strip()]
        return v or ["127.0.0.1", "::1"]

    @model_validator(mode="after")
    def validate_llm_provider_keys(self):
        """Validate that required API keys are set for the selected providers."""
        if self.llm_provider == "openrouter" and not self.openrouter_api_key:
            raise ValueError(
                "OPENROUTER_API_KEY is required when LLM_PROVIDER is 'openrouter'. "
                "Set LLM_PROVIDER to 'vllm', 'ollama', or 'lm-studio' for local models."
            )

        if is_openai_embedding_model(self.embedding_model) and not self.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY is required when EMBEDDING_MODEL uses an "
                "OpenAI embedding model."
            )
        return self

    @property
    def dev_mode(self) -> bool:
        """Check if running in development/test mode."""
        # Running in test mode if pytest is running or TEST environment is set
        in_test = "pytest" in sys.modules or os.getenv("TEST") == "true"
        # Running in development if environment is development
        in_dev = self.environment == "development"
        return in_test or in_dev

    @property
    def supabase_enabled(self) -> bool:
        """Check if Supabase is properly configured."""
        return bool(
            self.supabase_url
            and self.supabase_anon_key
            and self.supabase_service_key
        )

    @property
    def allow_mock_auth(self) -> bool:
        """Check if mock auth should be used (when Supabase is not configured)."""
        return not self.supabase_enabled

    @property
    def effective_cors_origins(self) -> List[str]:
        """Origins allowed for credentialed CORS.

        Explicit ALLOWED_ORIGINS always wins. Otherwise the fallback is
        environment-aware: development trusts the local Vite/CRA dev servers,
        but production must NEVER fall back to localhost — with
        allow_credentials=True that lets any page on a victim's own loopback
        make credentialed reads of /api/*. Production without ALLOWED_ORIGINS
        falls back to the public app URL (and its www host) only.
        """
        if self.allowed_origins:
            return self.allowed_origins
        if self.environment != "production":
            return [self.app_url, "http://localhost:5173", "http://localhost:3000"]
        origins = [self.app_url]
        if "://" in self.app_url and "://www." not in self.app_url:
            scheme, host = self.app_url.split("://", 1)
            origins.append(f"{scheme}://www.{host}")
        return origins


@lru_cache
def get_settings() -> Settings:
    return Settings()
