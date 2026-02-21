import os
import sys
from functools import lru_cache
from typing import List

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    environment: str = Field(default="development", alias="NODE_ENV")
    openrouter_api_key: str | None = Field(default=None, alias="OPENROUTER_API_KEY")
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
    app_url: str = Field(default="https://openecon.ai", alias="APP_URL")

    # LLM Configuration
    # LLM_PROVIDER options: openrouter, vllm, ollama, lm-studio
    llm_provider: str = Field(default="vllm", alias="LLM_PROVIDER")
    llm_model: str | None = Field(default="gpt-oss-120b", alias="LLM_MODEL")
    llm_base_url: str | None = Field(default="http://localhost:8000", alias="LLM_BASE_URL")
    llm_timeout: int = Field(default=120, alias="LLM_TIMEOUT")  # Higher default for local models
    # vLLM-specific settings (for SSH-tunneled or local vLLM servers)
    vllm_api_key: str | None = Field(default=None, alias="VLLM_API_KEY")
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

    # Pro Mode configuration - cross-platform defaults
    promode_enabled: bool = Field(
        default=False,
        alias="PROMODE_ENABLED",
        description="Enable Pro Mode code execution (disabled by default for security)"
    )
    promode_public_dir: str | None = Field(default=None, alias="PROMODE_PUBLIC_DIR")
    promode_session_dir: str | None = Field(default=None, alias="PROMODE_SESSION_DIR")

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

    @model_validator(mode="after")
    def validate_llm_provider_keys(self):
        """Validate that required API keys are set for the selected LLM provider."""
        if self.llm_provider == "openrouter" and not self.openrouter_api_key:
            raise ValueError(
                "OPENROUTER_API_KEY is required when LLM_PROVIDER is 'openrouter'. "
                "Set LLM_PROVIDER to 'vllm', 'ollama', or 'lm-studio' for local models."
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
