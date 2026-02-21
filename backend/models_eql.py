"""
Economic Query Language (EQL) Models

Provider-agnostic intermediate representation for economic data queries.
"""
from typing import Optional, List
from pydantic import BaseModel, Field
from enum import Enum


class TaskType(str, Enum):
    """Type of economic data task"""
    TIME_SERIES = "time-series"      # Single indicator over time
    COMPARE = "compare"               # Compare across countries/indicators
    TRADE_FLOW = "trade-flow"         # Bilateral trade data
    CRYPTO = "crypto"                 # Cryptocurrency prices
    BLOCKCHAIN = "blockchain"         # Blockchain analytics


class Domain(str, Enum):
    """Economic domain classification"""
    PRICES = "prices"                 # CPI, PPI, inflation
    NATIONAL_ACCOUNTS = "national-accounts"  # GDP, GNI, consumption
    LABOUR = "labour"                 # Unemployment, employment, wages
    TRADE = "trade"                   # Exports, imports, balance
    MONEY = "money"                   # Interest rates, money supply
    FISCAL = "fiscal"                 # Government debt, deficit
    EXTERNAL = "external"             # FX rates, reserves, FDI
    POPULATION = "population"         # Demographics
    CRYPTO = "crypto"                 # Cryptocurrency
    OTHER = "other"


class Measure(str, Enum):
    """Type of measurement"""
    LEVEL = "level"                   # Absolute value
    YOY = "yoy"                       # Year-over-year change
    MOM = "mom"                       # Month-over-month change
    INDEX = "index"                   # Index (base year = 100)
    RATE = "rate"                     # Rate (%, ratio)


class Frequency(str, Enum):
    """Data frequency"""
    ANNUAL = "A"
    QUARTERLY = "Q"
    MONTHLY = "M"
    DAILY = "D"
    AUTO = "auto"                     # Let provider decide


class EQL(BaseModel):
    """
    Economic Query Language - Provider-agnostic query representation

    This intermediate representation separates "understanding economic intent"
    from "finding provider indicators."

    Examples:
        User: "Brazil inflation rate"
        EQL: {
            "task": "time-series",
            "domain": "prices",
            "concept": "prices.cpi.inflation",
            "raw_concept": "inflation rate",
            "measure": "yoy",
            "geo": ["BRA"],
            "freq": "A"
        }

        User: "US GDP vs China GDP"
        EQL: {
            "task": "compare",
            "domain": "national-accounts",
            "concept": "economy.gdp.real",
            "raw_concept": "GDP",
            "measure": "level",
            "geo": ["USA", "CHN"]
        }
    """

    # Core fields
    task: TaskType = Field(
        default=TaskType.TIME_SERIES,
        description="Type of query task"
    )

    domain: Optional[Domain] = Field(
        default=None,
        description="Economic domain (for task routing)"
    )

    concept: str = Field(
        ...,
        description="Canonical economic concept (e.g., 'prices.cpi.inflation', 'economy.gdp.real')",
        examples=["prices.cpi.inflation", "economy.gdp.real", "labour.unemployment.rate"]
    )

    raw_concept: str = Field(
        ...,
        description="Original user phrase before normalization (for debugging & features)",
        examples=["inflation rate", "GDP", "unemployment"]
    )

    measure: Measure = Field(
        default=Measure.LEVEL,
        description="Type of measurement"
    )

    geo: List[str] = Field(
        ...,
        description="ISO3 country codes or provider-specific geo codes",
        examples=[["USA"], ["BRA"], ["USA", "CHN", "DEU"]]
    )

    # Time parameters
    freq: Optional[Frequency] = Field(
        default=Frequency.ANNUAL,
        description="Desired frequency (provider may override)"
    )

    time_from: Optional[str] = Field(
        default=None,
        description="Start date (YYYY, YYYY-MM, or YYYY-MM-DD)",
        examples=["2020", "2020-01", "2020-01-15"]
    )

    time_to: Optional[str] = Field(
        default="latest",
        description="End date or 'latest'"
    )

    # Hints & constraints
    provider_hint: Optional[str] = Field(
        default=None,
        description="User-specified provider preference (e.g., 'worldbank', 'fred')"
    )

    unit_hint: Optional[str] = Field(
        default=None,
        description="Desired unit (%, index, millions, etc.)"
    )

    # Task-specific parameters (for trade, crypto, blockchain)
    trade_flow: Optional[str] = Field(
        default=None,
        description="For trade-flow task: 'import', 'export', or 'both'"
    )

    commodity_code: Optional[str] = Field(
        default=None,
        description="For trade-flow task: HS code or commodity name"
    )

    coin_ids: Optional[List[str]] = Field(
        default=None,
        description="For crypto task: CoinGecko coin IDs",
        examples=[["bitcoin", "ethereum"]]
    )

    wallet_address: Optional[str] = Field(
        default=None,
        description="For blockchain task: wallet address"
    )

    blockchain_chain: Optional[str] = Field(
        default=None,
        description="For blockchain task: chain name (ethereum, polygon, etc.)"
    )

    class Config:
        use_enum_values = True
        json_schema_extra = {
            "examples": [
                {
                    "task": "time-series",
                    "domain": "prices",
                    "concept": "prices.cpi.inflation",
                    "raw_concept": "inflation rate",
                    "measure": "yoy",
                    "geo": ["BRA"],
                    "freq": "A",
                    "time_from": "2014",
                    "time_to": "latest"
                },
                {
                    "task": "compare",
                    "domain": "national-accounts",
                    "concept": "economy.gdp.real",
                    "raw_concept": "GDP",
                    "measure": "level",
                    "geo": ["USA", "CHN"],
                    "freq": "A"
                },
                {
                    "task": "trade-flow",
                    "domain": "trade",
                    "concept": "trade.import",
                    "raw_concept": "imports",
                    "geo": ["USA"],
                    "trade_flow": "import",
                    "commodity_code": "1006",
                    "time_from": "2023",
                    "time_to": "2023"
                }
            ]
        }


class QueryLog(BaseModel):
    """
    Comprehensive logging for each query to track accuracy and performance

    Used for:
    - A/B testing EQL vs old RAG
    - Training per-provider LTR models
    - Debugging failures
    - Computing accuracy dashboards
    """

    # Input
    user_query: str
    timestamp: str
    user_id: Optional[str] = None

    # Parsing
    parsed_with_eql: bool = Field(
        ...,
        description="Whether EQL parser was used (vs old RAG)"
    )

    eql: Optional[EQL] = Field(
        default=None,
        description="Parsed EQL (if parsed_with_eql=True)"
    )

    # Resolution
    resolved_via: str = Field(
        ...,
        description="How indicators were resolved",
        examples=["kg", "rag", "provider-default", "validator-fallback"]
    )

    provider_selected: str = Field(
        ...,
        description="Final provider used"
    )

    # Phase 2 Enhanced Logging (Hotfix #5)
    eligible_providers_before_guard: Optional[List[str]] = Field(
        default=None,
        description="Providers eligible before guardrail filtering (Phase 2)"
    )

    eligible_providers_after_guard: Optional[List[str]] = Field(
        default=None,
        description="Providers eligible after guardrail filtering (Phase 2)"
    )

    rejection_reason: Optional[str] = Field(
        default=None,
        description="Reason for provider filtering/rejection",
        examples=["crypto_disallowed_for_macro", "provider_unavailable"]
    )

    indicators_tried: List[str] = Field(
        default_factory=list,
        description="Indicators attempted (for validation tracking)"
    )

    indicator_final: Optional[str] = Field(
        default=None,
        description="Final indicator that returned data"
    )

    # Performance
    latency_ms_total: float = Field(
        ...,
        description="Total query latency in milliseconds"
    )

    latency_ms_parse: Optional[float] = None
    latency_ms_resolve: Optional[float] = None
    latency_ms_fetch: Optional[float] = None

    # Outcome
    success: bool = Field(
        ...,
        description="Whether query returned data"
    )

    error_type: Optional[str] = Field(
        default=None,
        description="Error category if failed",
        examples=["data_not_available", "ambiguous", "provider_error", "timeout"]
    )

    data_points_returned: Optional[int] = Field(
        default=None,
        description="Number of data points in result"
    )
