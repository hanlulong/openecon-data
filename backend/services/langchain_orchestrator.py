"""
LangChain Orchestrator for Intelligent Query Routing

This module provides intelligent query routing using LangChain with tool calling
to select the most appropriate data provider based on query content.

Enhanced with multi-agent architecture for:
- Query type classification (research, data, comparison, follow-up)
- Entity reference resolution
- Proper conversation context management

Compatible with LangChain 1.0+ API.

Author: econ-data-mcp Development Team
Date: 2025-11-21
Updated: 2025-11-29 - Added multi-agent architecture
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from ..config import get_settings
from ..models import NormalizedData, ParsedIntent
from ..utils.retry import retry_async, DataNotAvailableError
from .parameter_mapper import ParameterMapper

# Import new agent architecture
from ..agents.router_agent import RouterAgent, RoutingResult
from ..agents.research_agent import ResearchAgent
from ..agents.comparison_agent import ComparisonAgent
from ..memory.conversation_state import QueryType, DataReference
from ..memory.state_manager import ConversationStateManager

if TYPE_CHECKING:
    from ..services.query import QueryService

logger = logging.getLogger(__name__)

# Use the global singleton from state_manager module
def get_state_manager() -> ConversationStateManager:
    """Get the global state manager singleton from the memory module."""
    from ..memory.state_manager import conversation_state_manager
    return conversation_state_manager


def apply_default_time_range(
    provider: str,
    routing: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Apply smart default time ranges based on provider when user doesn't specify time period.

    Rules:
    - If user specified dates, always respect their request (don't override)
    - Exchange Rate / CoinGecko: Default to last 3 months (high-frequency data)
    - UN Comtrade: Default to last 10 years (annual trade data)
    - Other providers: No default (return all available data)

    Special handling:
    - For Comtrade: Override LLM-generated 5-year defaults with 10-year default

    Args:
        provider: The data provider name (uppercase)
        routing: The routing dictionary containing startDate and endDate

    Returns:
        Updated routing dictionary with default dates if applicable
    """
    from datetime import datetime, timedelta

    provider_upper = provider.upper()
    today = datetime.now()

    # Check if user already specified dates
    start_date = routing.get("startDate")
    end_date = routing.get("endDate")

    # Special handling for Comtrade: detect and override LLM-generated defaults
    # LLMs often default to "last 5 years" even when told not to
    if provider_upper == "COMTRADE":
        should_apply_default = False

        if not start_date and not end_date:
            # No dates specified - apply default
            should_apply_default = True
        elif start_date:
            # Check if this looks like an LLM-generated default (~5 years)
            try:
                start_year = int(start_date[:4])
                years_ago = today.year - start_year
                # If it's approximately 5 years (4-6 years range), it's likely an LLM default
                if 4 <= years_ago <= 6:
                    logger.info(f"Detected LLM-generated {years_ago}-year default for Comtrade, overriding with 10-year default")
                    should_apply_default = True
            except (ValueError, TypeError):
                pass

        if should_apply_default:
            # Trade data: default to last 10 years
            start_year_int = today.year - 10
            end_year_int = today.year
            routing["startDate"] = f"{start_year_int}-01-01"
            routing["endDate"] = f"{end_year_int}-12-31"
            routing["start_year"] = start_year_int  # Integer format for Comtrade API
            routing["end_year"] = end_year_int      # Integer format for Comtrade API
            logger.info(f"Applied default 10-year range for Comtrade: {start_year_int} to {end_year_int}")
            return routing

        # User specified a specific date range (not ~5 years), respect it
        return routing

    # For non-Comtrade providers, check if dates are specified
    if start_date or end_date:
        # User specified a time range, respect their request
        return routing

    # Apply provider-specific defaults for other providers
    if provider_upper == "COINGECKO":
        # Crypto: default to last 30 days for historical chart
        start = today - timedelta(days=30)
        routing["startDate"] = start.strftime("%Y-%m-%d")
        routing["endDate"] = today.strftime("%Y-%m-%d")
        logger.info(f"Applied default 30-day range for CoinGecko: {routing['startDate']} to {routing['endDate']}")

    # ExchangeRate: DO NOT apply default date range
    # The ExchangeRate-API free tier only supports CURRENT rates
    # If user wants historical exchange rates, they should explicitly ask for dates
    # Without dates, ExchangeRate-API returns current rates directly
    # Historical queries will automatically fallback to FRED when dates are specified
    if provider_upper == "EXCHANGERATE":
        # Don't apply default - let ExchangeRate-API return current rates
        # This ensures "What is USD to EUR exchange rate?" uses ExchangeRate-API
        logger.info(f"ExchangeRate: No default date range applied (current rates only)")

    # For other providers (FRED, World Bank, IMF, OECD, Eurostat, BIS, StatsCan):
    # No default - return all available data

    return routing


def filter_data_by_date_range(
    data: List[NormalizedData],
    start_date: Optional[str],
    end_date: Optional[str]
) -> List[NormalizedData]:
    """
    Filter NormalizedData list to only include data points within the specified date range.

    Args:
        data: List of NormalizedData objects
        start_date: Start date in YYYY-MM-DD format (or None for no start limit)
        end_date: End date in YYYY-MM-DD format (or None for no end limit)

    Returns:
        Filtered list of NormalizedData with updated data points
    """
    if not data or (not start_date and not end_date):
        return data

    # Extract year from date strings for comparison
    def get_year_from_date(date_str: str) -> int:
        """Extract year from various date formats."""
        if not date_str:
            return 0
        # Handle formats like "2019-01-01", "2019", "2019-Q1", etc.
        try:
            return int(date_str[:4])
        except (ValueError, TypeError):
            return 0

    start_year = get_year_from_date(start_date) if start_date else None
    end_year = get_year_from_date(end_date) if end_date else None

    filtered_data = []
    for series in data:
        filtered_points = []
        for point in series.data:
            point_year = get_year_from_date(point.date)
            if point_year == 0:
                continue  # Skip invalid dates

            # Apply filters
            if start_year and point_year < start_year:
                continue
            if end_year and point_year > end_year:
                continue
            filtered_points.append(point)

        if filtered_points:
            # Create new NormalizedData with filtered points
            from ..models import NormalizedData as ND, Metadata, DataPoint
            filtered_series = ND(
                metadata=series.metadata,
                data=filtered_points
            )
            filtered_data.append(filtered_series)

    return filtered_data if filtered_data else data  # Return original if filter removed all data


class LangChainOrchestrator:
    """
    Enhanced LangChain orchestrator for intelligent query routing.

    Features:
    - Multi-agent architecture with specialized agents
    - Query type classification (research, data, comparison, follow-up)
    - Entity reference resolution for follow-up queries
    - Structured conversation state with data references

    Uses direct LLM function calling instead of agent framework for better
    compatibility with LangChain 1.0+.
    """

    def __init__(
        self,
        query_service: 'QueryService',
        conversation_id: Optional[str] = None,
        settings: Optional[Any] = None
    ):
        """Initialize the orchestrator."""
        self.query_service = query_service
        self.conversation_id = conversation_id
        self.settings = settings or get_settings()

        # Initialize parameter mapper
        self.parameter_mapper = ParameterMapper()

        # Initialize LLM with function calling
        self.llm = self._initialize_llm()

        # Chat history for context
        self.chat_history = []

        # Use global state manager singleton (persists across orchestrator instances)
        self.state_manager = get_state_manager()
        self.router_agent = RouterAgent()
        self.research_agent = ResearchAgent(
            metadata_search=getattr(query_service, 'metadata_search', None)
        )
        self.comparison_agent = ComparisonAgent()

        logger.info(f"LangChain orchestrator initialized with multi-agent support (conversation: {conversation_id or 'new'})")

    def _initialize_llm(self) -> ChatOpenAI:
        """Initialize LLM with OpenRouter backend."""
        return ChatOpenAI(
            model=self.settings.llm_model or "openai/gpt-4o-mini",
            openai_api_key=self.settings.openrouter_api_key,
            openai_api_base="https://openrouter.ai/api/v1",
            temperature=0.1,
            max_tokens=1000,
            default_headers={
                "HTTP-Referer": "https://openecon.ai",
                "X-Title": "econ-data-mcp"
            }
        )

    def _get_system_prompt(self) -> str:
        """Get system prompt for intelligent routing."""
        return """You are an intelligent economic data routing assistant. Your job is to:

1. Analyze the user's query to understand:
   - Geographic scope (US, Canada, global, specific country)
   - Economic indicator (GDP, unemployment, inflation, trade, etc.)
   - Time period (if specified)

2. Select the most appropriate data provider:
   - FRED: US-specific data (GDP, unemployment, inflation, interest rates, housing)
   - Statistics Canada: Canadian data (all indicators for Canada)
   - World Bank: Global/multi-country development indicators
   - IMF: International finance, inflation rates (preferred for cross-country comparisons)
   - BIS: Central bank policy rates, financial stability
   - Eurostat: European Union statistics
   - OECD: OECD member countries (38 countries)
   - UN Comtrade: International trade flows, imports/exports
   - ExchangeRate: Currency exchange rates
   - CoinGecko: Cryptocurrency prices

3. Use conversation context:
   - If country was mentioned previously, use that for follow-up queries
   - If the query says "show me unemployment" without country, check chat history
   - Default to US if country is ambiguous and no context exists

4. Provider selection rules:
   - US query → FRED
   - Canada query → Statistics Canada
   - EU country (NOT debt/fiscal) → Eurostat
   - Trade/imports/exports → UN Comtrade
   - Debt/fiscal/government finance → IMF (MANDATORY)
     * public debt, government debt, national debt, sovereign debt
     * fiscal balance, budget deficit, fiscal deficit
     * government spending, government expenditure, government revenue
     * current account balance, debt to GDP ratio
   - Inflation comparison → IMF
   - Currency/forex → ExchangeRate
   - Crypto → CoinGecko
   - Multi-country comparison → World Bank or IMF

Format your response as JSON:
{
  "provider": "FRED | WorldBank | StatsCan | IMF | BIS | Eurostat | OECD | Comtrade | ExchangeRate | CoinGecko",
  "reasoning": "Why you selected this provider",
  "indicator": "GDP | UNEMPLOYMENT | INFLATION | etc",
  "country": "US | CA | etc (if applicable)",
  "startDate": "YYYY-01-01 or null if not specified",
  "endDate": "YYYY-12-31 or null if not specified",
  "needs_clarification": false,
  "clarification_questions": []
}

IMPORTANT for date extraction:
- If user says "2019-2023", set startDate to "2019-01-01" and endDate to "2023-12-31"
- If user says "last 5 years", calculate dates relative to current year
- If user says "since 2020", set startDate to "2020-01-01", endDate to null
- **CRITICAL: If no time period specified, set BOTH startDate AND endDate to null**
- **DO NOT assume any default date range** - the system will apply provider-specific defaults
- For Comtrade trade queries without dates: set startDate and endDate to null (system defaults to 10 years)
- For ExchangeRate/CoinGecko queries without dates: set to null (system defaults to 3 months)

IMPORTANT: If the user explicitly specifies a provider (e.g., "from BIS", "from OECD", "from FRED"), you MUST:
1. Use that provider (do not suggest alternatives)
2. Set needs_clarification to FALSE (never ask for clarification when provider is explicit)
3. Select the most appropriate indicator from that provider

For BIS specifically (use these EXACT codes):
- Property prices: WS_SPP (Selected Residential Property Prices) - PRIMARY for general property/house price queries
- Detailed property: WS_DPP (Detailed Residential Property Prices) - more granular breakdowns
- Commercial property: WS_CPP (Commercial Property Prices) - only if specifically asked for commercial
- Policy rates: WS_CBPOL (Central Bank Policy Rates)
- Consumer prices: WS_LONG_CPI (Consumer Price Index)
- Credit/Debt: WS_TC (Total Credit), WS_DSR (Debt Service Ratios)

For CoinGecko specifically:
- The indicator MUST be the cryptocurrency ID (lowercase): bitcoin, ethereum, solana, dogecoin, etc.
- Do NOT use generic terms like "price" or "price history" - always use the actual coin ID
- Example: For "Bitcoin price history" → indicator: "bitcoin"

For OECD specifically:
- GDP growth: Use "GDP" or "B1GQ" (GDP and main aggregates)
- Unemployment: Use "UNE_RATE" or "unemployment_rate"
- Inflation: Use "CPI" or "consumer_prices"

For Eurostat specifically (use these EXACT dataset codes):
- Unemployment: Use "une_rt_a" (annual) or "une_rt_m" (monthly) - NOT "UNE_RATE"
- GDP: Use "namq_10_gdp" (quarterly GDP) or "nama_10_gdp" (annual GDP)
- Population: Use "demo_pjan" (population on 1 January)
- Inflation/HICP: Use "prc_hicp_aind" (annual) or "prc_hicp_midx" (monthly)

Only set needs_clarification to true if the query is truly ambiguous AND no provider is specified."""

    async def execute(
        self,
        query: str,
        chat_history: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        Execute query using intelligent routing with multi-agent architecture.

        Args:
            query: User's natural language query
            chat_history: Optional conversation history

        Returns:
            Dict with success status, output, and optional error
        """
        try:
            # Get or create conversation state
            conv_id = self.state_manager.get_or_create(self.conversation_id)
            state = self.state_manager.get(conv_id)

            logger.info(f"[DEBUG] Orchestrator state for conv {conv_id[:8]}: "
                       f"datasets={len(state.entity_context.current_datasets) if state else 'N/A'}, "
                       f"messages={len(state.messages) if state else 'N/A'}")

            # STEP 1: Use RouterAgent to classify query and resolve references
            routing_result = self.router_agent.classify(query, state)
            logger.info(f"Query classified as: {routing_result.query_type.value}")

            # STEP 2: Handle based on query type
            if routing_result.query_type == QueryType.RESEARCH:
                # Handle research questions (e.g., "does Eurostat have X?")
                return await self._handle_research_query(query, routing_result, state, conv_id)

            if routing_result.query_type == QueryType.COMPARISON:
                # Handle comparison requests (e.g., "consolidated and unconsolidated on same graph")
                return await self._handle_comparison_query(query, routing_result, state, conv_id)

            if routing_result.query_type == QueryType.FOLLOW_UP:
                # Handle follow-up queries (e.g., "plot that", "show it monthly")
                return await self._handle_follow_up_query(query, routing_result, state, conv_id)

            if routing_result.query_type == QueryType.ANALYSIS:
                # Handle analysis queries (e.g., "what's the correlation?", "calculate growth rate")
                return await self._handle_analysis_query(query, routing_result, state, conv_id)

            # STEP 3: For standard data queries, use LLM-based routing
            # Build messages for LLM
            messages = [SystemMessage(content=self._get_system_prompt())]

            # Add chat history for context (last 5 messages)
            if chat_history:
                for msg in chat_history[-5:]:
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    if role == "user":
                        messages.append(HumanMessage(content=content))
                    elif role == "assistant":
                        messages.append(AIMessage(content=content))

            # Add current query
            messages.append(HumanMessage(content=query))

            logger.info(f"Routing query with LLM: {query}")

            # Get routing decision from LLM
            response = await self.llm.ainvoke(messages)
            routing_text = response.content

            logger.info(f"LLM routing response: {routing_text}")

            # Parse routing decision
            try:
                import json
                # Extract JSON from response (may have markdown code blocks)
                routing_text_cleaned = routing_text.strip()
                if routing_text_cleaned.startswith("```json"):
                    routing_text_cleaned = routing_text_cleaned[7:]
                if routing_text_cleaned.startswith("```"):
                    routing_text_cleaned = routing_text_cleaned[3:]
                if routing_text_cleaned.endswith("```"):
                    routing_text_cleaned = routing_text_cleaned[:-3]

                routing = json.loads(routing_text_cleaned.strip())
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse routing JSON, using fallback parsing")
                # Fallback: extract key information using string matching
                routing = self._fallback_parse_routing(routing_text)

            # Check if clarification is needed
            if routing.get("needs_clarification"):
                questions = routing.get("clarification_questions", [])
                return {
                    "success": True,
                    "output": f"I need some clarification:\n\n" + "\n".join(f"- {q}" for q in questions),
                    "error": None
                }

            # Route to appropriate provider
            provider = routing.get("provider", "").upper()
            indicator = routing.get("indicator", "GDP")
            country = routing.get("country")
            reasoning = routing.get("reasoning", "")

            # CRITICAL FIX: Enforce explicit provider detection at code level
            # This ensures user's explicit "from Eurostat", "from BIS", etc. is ALWAYS respected
            # even if LLM incorrectly selects a different provider
            from ..services.provider_router import ProviderRouter
            explicit_provider = ProviderRouter.detect_explicit_provider(query)
            if explicit_provider and explicit_provider.upper() != provider:
                logger.info(f"🔄 EXPLICIT PROVIDER OVERRIDE: '{provider}' → '{explicit_provider.upper()}' (user explicitly requested '{explicit_provider}')")
                provider = explicit_provider.upper()
                reasoning = f"User explicitly requested data from {explicit_provider}"

            # FALLBACK: Extract country from query and override LLM if it defaults to US/USA
            # This prevents defaulting to USA when a specific country was mentioned
            extracted_country = self._extract_country_from_query(query)
            if extracted_country:
                is_default_us = not country or country.lower() in ["us", "usa", "united states", "america"]
                if is_default_us and extracted_country.lower() not in ["us", "usa", "united states", "america"]:
                    logger.info(f"🌍 Country Override: '{country}' → '{extracted_country}' (query explicitly mentions {extracted_country})")
                    country = extracted_country

            logger.info(f"Routing to {provider} for {indicator} ({country or 'no country'}): {reasoning}")

            # Apply smart default time ranges based on provider
            routing = apply_default_time_range(provider, routing)

            # Use parameter mapper to convert LLM output to provider parameters
            # IMPORTANT: Remove country from routing to prevent overriding our corrected value
            routing_without_country = {k: v for k, v in routing.items() if k != "country"}
            mapped_params = self.parameter_mapper.map_intent_to_parameters(
                provider=provider,
                intent={
                    **routing_without_country,  # Include other parameters from LLM first
                    "indicator": indicator,
                    "country": country,  # Use our corrected country (override LLM's country)
                },
                query=query
            )

            logger.info(f"Mapped parameters: {mapped_params}")

            # Build intent with mapped parameters
            # Extract indicator code if mapped
            if "seriesId" in mapped_params:
                # FRED uses seriesId
                indicators = [mapped_params.pop("seriesId")]
            elif "vectorId" in mapped_params:
                # StatsCanada uses vectorId
                indicators = [str(mapped_params.pop("vectorId"))]
            elif "indicator" in mapped_params:
                # Other providers use indicator code
                indicators = [mapped_params.pop("indicator")]
            else:
                # Fallback to original indicator
                indicators = [indicator]

            intent = ParsedIntent(
                apiProvider=provider,
                indicators=indicators,
                parameters=mapped_params,
                clarificationNeeded=False
            )

            # Fetch data
            data = await retry_async(
                lambda: self.query_service._fetch_data(intent),
                max_attempts=2,
                initial_delay=0.5
            )

            # Apply date range filtering if dates were specified in the query
            start_date = routing.get("startDate")
            end_date = routing.get("endDate")
            if data and (start_date or end_date):
                original_count = sum(len(series.data) for series in data)
                data = filter_data_by_date_range(data, start_date, end_date)
                filtered_count = sum(len(series.data) for series in data) if data else 0
                logger.info(f"Date range filter applied: {start_date} to {end_date}, points: {original_count} -> {filtered_count}")

            # CRITICAL: Store data reference in state for follow-up queries
            self.state_manager.add_user_message(conv_id, query, QueryType.DATA_FETCH)

            if data:
                # Create data reference from fetched data
                import uuid
                first = data[0]
                metadata = first.metadata

                data_ref = DataReference(
                    id=str(uuid.uuid4()),
                    query=query,
                    provider=metadata.source or provider,
                    dataset_code=metadata.seriesId,
                    indicator=metadata.indicator or indicator,
                    country=metadata.country or country,
                    time_range=(metadata.startDate, metadata.endDate) if metadata.startDate else None,
                    unit=metadata.unit or "",
                    frequency=metadata.frequency or "",
                    metadata=metadata.model_dump() if hasattr(metadata, "model_dump") else {},
                )

                # Store in conversation state
                self.state_manager.add_assistant_message(
                    conv_id,
                    f"Retrieved {len(data)} series from {provider}",
                    data_reference=data_ref
                )

                # Update entity context
                state = self.state_manager.get(conv_id)
                if state:
                    if metadata.country and metadata.country not in state.entity_context.current_countries:
                        state.entity_context.current_countries.append(metadata.country)
                    if metadata.indicator and metadata.indicator not in state.entity_context.current_indicators:
                        state.entity_context.current_indicators.append(metadata.indicator)
                    if metadata.source:
                        state.entity_context.current_provider = metadata.source

                logger.info(f"Stored data reference for follow-ups: {data_ref.indicator} ({data_ref.provider})")

            # Format response
            output = self._format_data_response(data, provider, reasoning)

            return {
                "success": True,
                "output": output,
                "data": data,
                "error": None
            }

        except Exception as e:
            logger.error(f"Orchestrator execution failed: {e}", exc_info=True)
            return {
                "success": False,
                "output": None,
                "error": str(e)
            }

    def _extract_country_from_query(self, query: str) -> Optional[str]:
        """
        Extract country name from query using pattern matching.
        This serves as a fallback when LLM fails to extract the country.

        Returns the country name if found, None otherwise.
        """
        query_lower = query.lower()

        # Common country names that might be missed by LLM
        # Ordered by specificity (compound names first to avoid partial matches)
        country_patterns = [
            # Compound names (must be checked first)
            ("south africa", "South Africa"),
            ("south african", "South Africa"),
            ("south korea", "South Korea"),
            ("south korean", "South Korea"),
            ("north korea", "North Korea"),
            ("north korean", "North Korea"),
            ("new zealand", "New Zealand"),
            ("costa rica", "Costa Rica"),
            ("saudi arabia", "Saudi Arabia"),
            ("united arab emirates", "UAE"),
            ("uae", "UAE"),
            ("united kingdom", "UK"),
            ("united states", "US"),
            ("czech republic", "Czech Republic"),
            ("dominican republic", "Dominican Republic"),
            # Single word countries (common ones that might be missed)
            ("indonesia", "Indonesia"),
            ("indonesian", "Indonesia"),
            ("nigeria", "Nigeria"),
            ("nigerian", "Nigeria"),
            ("brazil", "Brazil"),
            ("brazilian", "Brazil"),
            ("mexico", "Mexico"),
            ("mexican", "Mexico"),
            ("russia", "Russia"),
            ("russian", "Russia"),
            ("india", "India"),
            ("indian", "India"),
            ("japan", "Japan"),
            ("japanese", "Japan"),
            ("germany", "Germany"),
            ("german", "Germany"),
            ("france", "France"),
            ("french", "France"),
            ("italy", "Italy"),
            ("italian", "Italy"),
            ("spain", "Spain"),
            ("spanish", "Spain"),
            ("australia", "Australia"),
            ("australian", "Australia"),
            ("argentina", "Argentina"),
            ("argentine", "Argentina"),
            ("thailand", "Thailand"),
            ("thai", "Thailand"),
            ("vietnam", "Vietnam"),
            ("vietnamese", "Vietnam"),
            ("philippines", "Philippines"),
            ("filipino", "Philippines"),
            ("malaysia", "Malaysia"),
            ("malaysian", "Malaysia"),
            ("singapore", "Singapore"),
            ("turkey", "Turkey"),
            ("turkish", "Turkey"),
            ("egypt", "Egypt"),
            ("egyptian", "Egypt"),
            ("kenya", "Kenya"),
            ("kenyan", "Kenya"),
            ("pakistan", "Pakistan"),
            ("pakistani", "Pakistan"),
            ("bangladesh", "Bangladesh"),
            ("colombian", "Colombia"),
            ("colombia", "Colombia"),
            ("chile", "Chile"),
            ("chilean", "Chile"),
            ("peru", "Peru"),
            ("peruvian", "Peru"),
            ("china", "China"),
            ("chinese", "China"),
            ("canada", "Canada"),
            ("canadian", "Canada"),
            ("uk", "UK"),
            ("britain", "UK"),
            ("british", "UK"),
        ]

        for pattern, country in country_patterns:
            if pattern in query_lower:
                logger.info(f"🌍 Fallback country extraction: found '{country}' in query")
                return country
        return None

    def _fallback_parse_routing(self, routing_text: str) -> Dict[str, Any]:
        """Fallback parsing when JSON parsing fails."""
        routing = {
            "provider": "FRED",  # Default
            "indicator": "GDP",
            "country": None,
            "needs_clarification": False,
            "reasoning": "Fallback routing"
        }

        text_lower = routing_text.lower()

        # Detect provider
        if "fred" in text_lower or "federal reserve" in text_lower:
            routing["provider"] = "FRED"
        elif "world bank" in text_lower or "worldbank" in text_lower:
            routing["provider"] = "WorldBank"
        elif "statistics canada" in text_lower or "statscan" in text_lower:
            routing["provider"] = "StatsCan"
        elif "imf" in text_lower:
            routing["provider"] = "IMF"
        elif "bis" in text_lower:
            routing["provider"] = "BIS"
        elif "eurostat" in text_lower:
            routing["provider"] = "Eurostat"
        elif "oecd" in text_lower:
            routing["provider"] = "OECD"
        elif "comtrade" in text_lower:
            routing["provider"] = "Comtrade"
        elif "coingecko" in text_lower or "crypto" in text_lower:
            routing["provider"] = "CoinGecko"

        # Detect indicator
        if "unemployment" in text_lower:
            routing["indicator"] = "UNEMPLOYMENT"
        elif "inflation" in text_lower:
            routing["indicator"] = "INFLATION"
        elif "gdp" in text_lower:
            routing["indicator"] = "GDP"
        elif "trade" in text_lower or "import" in text_lower or "export" in text_lower:
            routing["indicator"] = "TRADE"

        # Detect country
        if "us" in text_lower or "united states" in text_lower or "america" in text_lower:
            routing["country"] = "US"
        elif "canada" in text_lower or "canadian" in text_lower:
            routing["country"] = "CA"
        elif "uk" in text_lower or "united kingdom" in text_lower:
            routing["country"] = "GB"

        return routing

    async def _handle_research_query(
        self,
        query: str,
        routing: RoutingResult,
        state: Any,
        conv_id: str
    ) -> Dict[str, Any]:
        """
        Handle research questions about data availability.

        Example: "Does Eurostat have EU nonfinancial corporation total assets?"

        Returns informative answer without fetching data.
        """
        try:
            result = await self.research_agent.process(query, routing.context)

            # Update conversation state
            self.state_manager.add_user_message(conv_id, query, QueryType.RESEARCH)
            self.state_manager.add_assistant_message(conv_id, result.message)

            return {
                "success": True,
                "output": result.message,
                "error": None,
                "query_type": "research",
            }

        except Exception as e:
            logger.error(f"Research query failed: {e}")
            return {
                "success": False,
                "output": None,
                "error": str(e),
            }

    async def _handle_comparison_query(
        self,
        query: str,
        routing: RoutingResult,
        state: Any,
        conv_id: str
    ) -> Dict[str, Any]:
        """
        Handle comparison requests for multiple series on same graph.

        Example: "Plot consolidated and unconsolidated total financial liabilities
                  for EU nonfinancial corporations on one graph"

        Fetches all variants and returns them for combined visualization.
        """
        try:
            # Set up data fetcher if not already configured
            if not self.comparison_agent.data_fetcher:
                async def fetch_variant(params):
                    # Build intent from params
                    provider = params.get("provider", "EUROSTAT")
                    indicator = params.get("indicator", "")
                    country = params.get("country", "EU")

                    intent = ParsedIntent(
                        apiProvider=provider,
                        indicators=[indicator],
                        parameters={
                            "country": country,
                            "dimensions": params.get("dimensions", {}),
                        },
                        clarificationNeeded=False
                    )

                    result = await self.query_service._fetch_data(intent)
                    return result[0] if result else None

                self.comparison_agent.data_fetcher = fetch_variant

            result = await self.comparison_agent.process(query, routing.context, state)

            if not result.success:
                return {
                    "success": False,
                    "output": None,
                    "error": result.error,
                }

            # Update conversation state with data references
            self.state_manager.add_user_message(conv_id, query, QueryType.COMPARISON)
            for ref in result.data_references:
                self.state_manager.add_assistant_message(
                    conv_id,
                    f"Retrieved {ref.indicator}",
                    data_reference=ref
                )

            # Format response
            output = f"**Comparison Query Results**\n\n"
            output += f"Retrieved {len(result.datasets)} series for comparison:\n"
            for i, label in enumerate(result.legend_labels, 1):
                output += f"{i}. {label}\n"

            if result.datasets:
                output += f"\nChart type: {result.chart_type}"
                output += f"\nMerge series: {'Yes' if result.merge_series else 'No'}"

            return {
                "success": True,
                "output": output,
                "data": result.datasets,
                "chart_type": result.chart_type,
                "merge_series": result.merge_series,
                "legend_labels": result.legend_labels,
                "error": None,
                "query_type": "comparison",
            }

        except Exception as e:
            logger.error(f"Comparison query failed: {e}")
            return {
                "success": False,
                "output": None,
                "error": str(e),
            }

    async def _handle_follow_up_query(
        self,
        query: str,
        routing: RoutingResult,
        state: Any,
        conv_id: str
    ) -> Dict[str, Any]:
        """
        Handle follow-up queries that reference previous data.

        Example: "Plot unconsolidated data on the same graph"
                 (refers to previous query about financial liabilities)

        Uses entity context to resolve references.
        """
        try:
            context = routing.context
            base_dataset = context.get("base_dataset")

            if not base_dataset:
                return {
                    "success": False,
                    "output": "No previous data found to reference. Please query the data first.",
                    "error": "no_context",
                }

            # Check for variant request (e.g., "unconsolidated")
            variant = context.get("dataset_variant")
            if variant:
                # Fetch the variant
                logger.info(f"Fetching variant '{variant}' of {base_dataset.indicator}")

                # Build modified parameters for the variant
                params = {
                    "country": base_dataset.country or "EU",
                    "startDate": base_dataset.time_range[0] if base_dataset.time_range else None,
                    "endDate": base_dataset.time_range[1] if base_dataset.time_range else None,
                }

                # Add variant-specific dimension filters
                if base_dataset.provider == "EUROSTAT":
                    if variant == "unconsolidated":
                        params["dimensions"] = {"conso": "N"}
                    elif variant == "consolidated":
                        params["dimensions"] = {"conso": "S"}

                intent = ParsedIntent(
                    apiProvider=base_dataset.provider,
                    indicators=[base_dataset.indicator],
                    parameters=params,
                    clarificationNeeded=False
                )

                data = await retry_async(
                    lambda: self.query_service._fetch_data(intent),
                    max_attempts=2,
                    initial_delay=0.5
                )

                if data:
                    # Create data reference
                    import uuid
                    ref = DataReference(
                        id=str(uuid.uuid4()),
                        query=query,
                        provider=base_dataset.provider,
                        dataset_code=base_dataset.dataset_code,
                        indicator=f"{base_dataset.indicator} ({variant})",
                        country=base_dataset.country,
                        time_range=base_dataset.time_range,
                        unit=base_dataset.unit,
                        variants=[variant],
                    )

                    # Update state
                    self.state_manager.add_user_message(conv_id, query, QueryType.FOLLOW_UP)
                    self.state_manager.add_assistant_message(
                        conv_id,
                        f"Retrieved {ref.indicator}",
                        data_reference=ref
                    )

                    output = self._format_data_response(
                        data,
                        base_dataset.provider,
                        f"Follow-up: {variant} variant of {base_dataset.indicator}"
                    )

                    return {
                        "success": True,
                        "output": output,
                        "data": data,
                        "merge_with_previous": routing.merge_with_previous,
                        "error": None,
                        "query_type": "follow_up",
                    }

            # For other follow-ups (frequency change, etc.)
            return {
                "success": False,
                "output": "Follow-up type not yet supported. Please be more specific.",
                "error": "unsupported_follow_up",
            }

        except Exception as e:
            logger.error(f"Follow-up query failed: {e}")
            return {
                "success": False,
                "output": None,
                "error": str(e),
            }

    async def _handle_analysis_query(
        self,
        query: str,
        routing: RoutingResult,
        state: Any,
        conv_id: str
    ) -> Dict[str, Any]:
        """
        Handle analysis queries that require calculations on existing data.

        Example: "What's the correlation between these two?", "Calculate growth rate"

        These queries require Pro Mode for actual calculations.
        """
        try:
            context = routing.context
            available_data = context.get("available_data", {})

            # Update conversation state
            self.state_manager.add_user_message(conv_id, query, QueryType.ANALYSIS)

            # Check if we have data to analyze
            if state and state.entity_context.current_datasets:
                datasets = state.entity_context.current_datasets
                dataset_info = []
                for ds in datasets[-3:]:  # Last 3 datasets
                    dataset_info.append(f"  - {ds.indicator} ({ds.provider})")

                message = (
                    f"**Analysis Request Detected**\n\n"
                    f"Your query: \"{query}\"\n\n"
                    f"I found the following data in our conversation:\n"
                    + "\n".join(dataset_info) + "\n\n"
                    f"**To perform this analysis**, please use **Pro Mode** which can:\n"
                    f"- Calculate correlations between datasets\n"
                    f"- Compute growth rates and percentage changes\n"
                    f"- Perform trend analysis and regressions\n"
                    f"- Create custom visualizations\n\n"
                    f"*Tip: Try asking 'Use Pro Mode to calculate the correlation between GDP and inflation'*"
                )
            else:
                message = (
                    f"**Analysis Request Detected**\n\n"
                    f"Your query: \"{query}\"\n\n"
                    f"I don't have any data in our current conversation to analyze.\n\n"
                    f"**Please first fetch the data** you want to analyze, then ask me to perform calculations on it.\n\n"
                    f"For example:\n"
                    f"1. 'Show me US GDP and inflation for the last 10 years'\n"
                    f"2. 'What's the correlation between these two?'"
                )

            self.state_manager.add_assistant_message(conv_id, message)

            return {
                "success": True,
                "output": message,
                "error": None,
                "query_type": "analysis",
            }

        except Exception as e:
            logger.error(f"Analysis query failed: {e}")
            return {
                "success": False,
                "output": None,
                "error": str(e),
            }

    def _format_data_response(
        self,
        data: List[NormalizedData],
        provider: str,
        reasoning: str
    ) -> str:
        """Format normalized data response."""
        if not data:
            return f"No data returned from {provider}."

        # Start directly with data summary (no routing details for end users)
        response_parts = [
            f"**Retrieved {len(data)} dataset(s)**:\n"
        ]

        for i, dataset in enumerate(data[:3], 1):
            metadata = dataset.metadata
            data_points = dataset.data[:5]

            response_parts.append(f"\n{i}. **{metadata.indicator}** ({metadata.country or 'Global'})")
            response_parts.append(f"   - Source: {metadata.source}")
            response_parts.append(f"   - Frequency: {metadata.frequency}")
            response_parts.append(f"   - Unit: {metadata.unit}")
            response_parts.append(f"   - Data points: {len(dataset.data)}")

            if data_points:
                response_parts.append("   - Sample data:")
                for dp in data_points:
                    value_str = f"{dp.value:.2f}" if dp.value is not None else "N/A"
                    response_parts.append(f"     * {dp.date}: {value_str}")

        if len(data) > 3:
            response_parts.append(f"\n... and {len(data) - 3} more dataset(s)")

        return "\n".join(response_parts)


def create_langchain_orchestrator(
    query_service: 'QueryService',
    conversation_id: Optional[str] = None,
    settings: Optional[Any] = None
) -> LangChainOrchestrator:
    """Factory function to create orchestrator."""
    return LangChainOrchestrator(
        query_service=query_service,
        conversation_id=conversation_id,
        settings=settings
    )
