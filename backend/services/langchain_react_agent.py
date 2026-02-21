"""
LangChain ReAct Agent Orchestrator for econ-data-mcp

This module implements an intelligent ReAct (Reasoning + Acting) agent that:
- Analyzes user queries for complexity and intent
- Selects optimal data providers with confidence scoring
- Executes multi-step strategies with error recovery
- Provides transparent reasoning for debugging

Based on LANGCHAIN_ARCHITECTURE_DESIGN.md specification.

Author: econ-data-mcp Development Team
Date: 2025-11-26
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional, TYPE_CHECKING
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.output_parsers import JsonOutputParser
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from ..config import get_settings
from ..models import NormalizedData, ParsedIntent, QueryResponse
from ..utils.retry import retry_async, DataNotAvailableError
from .langchain_tools import create_provider_tools, get_tool_descriptions
from .parameter_mapper import ParameterMapper

if TYPE_CHECKING:
    from .query import QueryService

logger = logging.getLogger(__name__)


# ============================================================================
# Output Schemas for Structured Agent Responses
# ============================================================================

class QueryAnalysis(BaseModel):
    """Structured output from query analysis step."""
    intent: str = Field(description="What the user is asking for")
    complexity: str = Field(description="Query complexity: simple, moderate, or complex")
    complexity_factors: List[str] = Field(default_factory=list, description="Factors contributing to complexity")
    geographic_scope: str = Field(description="Geographic scope: us, canada, global, eu, specific_country")
    indicator_type: str = Field(description="Type of indicator: gdp, unemployment, inflation, trade, etc.")
    time_period: Optional[str] = Field(default=None, description="Requested time period if specified")
    requires_pro_mode: bool = Field(default=False, description="Whether Pro Mode is recommended")
    pro_mode_reason: Optional[str] = Field(default=None, description="Why Pro Mode is recommended")


class ProviderSelection(BaseModel):
    """Structured output from provider selection step."""
    primary_provider: str = Field(description="Primary data provider to use")
    primary_confidence: float = Field(description="Confidence score 0-1 for primary provider")
    primary_reasoning: str = Field(description="Why this provider was selected")
    fallback_providers: List[str] = Field(default_factory=list, description="Fallback providers if primary fails")
    strategy: str = Field(description="Execution strategy: direct, with_fallback, parallel, pro_mode")


class ExecutionResult(BaseModel):
    """Structured output from execution step."""
    success: bool = Field(description="Whether data was successfully retrieved")
    provider_used: str = Field(description="Provider that returned the data")
    data_summary: str = Field(description="Summary of retrieved data")
    error_message: Optional[str] = Field(default=None, description="Error message if failed")
    fallback_attempted: bool = Field(default=False, description="Whether fallback was attempted")
    reasoning_steps: List[str] = Field(default_factory=list, description="Step-by-step reasoning log")


# ============================================================================
# Agent Prompts
# ============================================================================

ORCHESTRATOR_SYSTEM_PROMPT = """You are an intelligent economic data orchestrator. Your job is to:

1. **Analyze** the user's query to understand their data needs
2. **Select** the optimal data provider based on query characteristics
3. **Execute** data retrieval with intelligent error recovery
4. **Explain** your reasoning transparently

## Available Data Providers

{tool_descriptions}

## Provider Selection Guidelines

**US-specific queries:**
- Economic indicators (GDP, unemployment, inflation) → FRED
- Housing data (housing starts, home prices) → FRED
- Interest rates (Fed funds, mortgages) → FRED

**Canada queries:**
- Any Canadian data → Statistics Canada (fetch_statscan)
- Provincial breakdowns → Statistics Canada
- Default to Canada if query mentions Canadian provinces

**European queries:**
- EU member countries → Eurostat
- OECD comparisons → OECD (may be rate-limited, fallback to IMF)

**Global/Multi-country queries:**
- Development indicators → World Bank
- Inflation comparisons → IMF
- Government debt/fiscal → IMF

**Central bank data:**
- Policy rates → BIS
- Credit statistics → BIS

**Trade data:**
- Imports/exports → UN Comtrade
- Trade balances → UN Comtrade

**Currency/Crypto:**
- Exchange rates → ExchangeRate-API
- Cryptocurrency → CoinGecko

## Error Recovery Strategies

1. **Timeout**: Reduce date range, try different provider
2. **No data**: Try alternative indicator code, check provider coverage
3. **Rate limit**: Switch to alternative provider (OECD → IMF, Eurostat → World Bank)
4. **Parsing error**: Simplify parameters, use default values

## Response Format

Always respond with valid JSON containing your analysis and action plan.
Think step-by-step and be transparent about your reasoning.
"""

QUERY_ANALYSIS_PROMPT = """Analyze this economic data query:

Query: "{query}"

Conversation History:
{history}

Provide a structured analysis with:
1. What is the user asking for?
2. What is the complexity level?
3. What is the geographic scope?
4. What type of indicator is requested?
5. Is Pro Mode required for this query?

Respond with JSON:
{{
  "intent": "description of what user wants",
  "complexity": "simple|moderate|complex",
  "complexity_factors": ["factor1", "factor2"],
  "geographic_scope": "us|canada|global|eu|specific_country",
  "indicator_type": "gdp|unemployment|inflation|trade|interest_rate|housing|crypto|other",
  "time_period": "last 5 years|2020-2023|null",
  "requires_pro_mode": false,
  "pro_mode_reason": null
}}
"""

PROVIDER_SELECTION_PROMPT = """Based on this query analysis, select the optimal data provider:

Query: "{query}"
Analysis:
- Intent: {intent}
- Complexity: {complexity}
- Geographic Scope: {geographic_scope}
- Indicator Type: {indicator_type}
- Time Period: {time_period}

Previous Errors (if any): {previous_errors}

Select the best provider and strategy. Consider:
1. Which provider is most appropriate for this query?
2. What is the confidence level?
3. What fallback options exist?
4. Should we use parallel fetching or sequential?

Respond with JSON:
{{
  "primary_provider": "FRED|WorldBank|StatsCan|IMF|BIS|Eurostat|OECD|Comtrade|ExchangeRate|CoinGecko",
  "primary_confidence": 0.85,
  "primary_reasoning": "Why this provider is best",
  "fallback_providers": ["Provider2", "Provider3"],
  "strategy": "direct|with_fallback|parallel|pro_mode"
}}
"""


# ============================================================================
# ReAct Agent Implementation
# ============================================================================

class LangChainReActAgent:
    """
    ReAct (Reasoning + Acting) Agent for intelligent query processing.

    Implements multi-step reasoning with:
    - Query analysis
    - Provider selection with confidence scoring
    - Adaptive execution with error recovery
    - Transparent reasoning logs
    """

    def __init__(
        self,
        query_service: 'QueryService',
        conversation_id: Optional[str] = None,
        settings: Optional[Any] = None
    ):
        """Initialize the ReAct agent."""
        self.query_service = query_service
        self.conversation_id = conversation_id
        self.settings = settings or get_settings()

        # Initialize LLM
        self.llm = self._initialize_llm()

        # Initialize tools
        self.tools = create_provider_tools(query_service)
        self.tool_map = {tool.name: tool for tool in self.tools}

        # Initialize parameter mapper
        self.parameter_mapper = ParameterMapper()

        # Reasoning log for transparency
        self.reasoning_log: List[str] = []

        # Error history for adaptive retry
        self.error_history: List[Dict[str, Any]] = []

        logger.info(f"LangChain ReAct agent initialized (conversation: {conversation_id or 'new'})")

    def _initialize_llm(self) -> ChatOpenAI:
        """Initialize LLM with OpenRouter backend."""
        return ChatOpenAI(
            model=self.settings.llm_model or "openai/gpt-4o-mini",
            openai_api_key=self.settings.openrouter_api_key,
            openai_api_base="https://openrouter.ai/api/v1",
            temperature=0.1,
            max_tokens=2000,
            default_headers={
                "HTTP-Referer": "https://openecon.ai",
                "X-Title": "econ-data-mcp ReAct Agent"
            }
        )

    def _log_reasoning(self, step: str, message: str):
        """Add a step to the reasoning log."""
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_entry = f"[{timestamp}] {step}: {message}"
        self.reasoning_log.append(log_entry)
        logger.info(f"🧠 {log_entry}")

    async def execute(
        self,
        query: str,
        chat_history: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        Execute query using ReAct reasoning pattern.

        Steps:
        1. Analyze query (Reasoning)
        2. Select provider (Planning)
        3. Execute with tools (Acting)
        4. Handle errors adaptively (Recovery)
        5. Return results (Output)

        Args:
            query: User's natural language query
            chat_history: Optional conversation history

        Returns:
            Dict with success status, output, and reasoning log
        """
        self.reasoning_log = []  # Reset log
        self._log_reasoning("START", f"Processing query: {query}")

        try:
            # Step 1: Analyze Query
            analysis = await self._analyze_query(query, chat_history)
            self._log_reasoning("ANALYSIS", f"Intent: {analysis['intent']}, Complexity: {analysis['complexity']}")

            # Step 2: Check if Pro Mode is required
            if analysis.get('requires_pro_mode'):
                self._log_reasoning("DECISION", f"Pro Mode required: {analysis.get('pro_mode_reason', 'Complex query')}")
                return await self._execute_pro_mode(query)

            # Step 3: Select Provider
            provider_selection = await self._select_provider(query, analysis)
            self._log_reasoning(
                "PROVIDER_SELECTION",
                f"Primary: {provider_selection['primary_provider']} (confidence: {provider_selection['primary_confidence']:.2f})"
            )

            # Step 4: Execute with Selected Provider
            result = await self._execute_with_provider(
                query,
                analysis,
                provider_selection
            )

            # Step 5: Format and Return Result
            if result['success']:
                self._log_reasoning("SUCCESS", f"Data retrieved from {result['provider_used']}")
                return {
                    "success": True,
                    "output": result['data_summary'],
                    "data": result.get('data'),
                    "provider": result['provider_used'],
                    "reasoning_log": self.reasoning_log,
                    "error": None
                }
            else:
                self._log_reasoning("FAILURE", f"All providers failed: {result['error_message']}")
                return {
                    "success": False,
                    "output": None,
                    "error": result['error_message'],
                    "reasoning_log": self.reasoning_log
                }

        except Exception as e:
            logger.exception("ReAct agent execution error")
            self._log_reasoning("ERROR", f"Execution failed: {str(e)}")
            return {
                "success": False,
                "output": None,
                "error": str(e),
                "reasoning_log": self.reasoning_log
            }

    async def _analyze_query(
        self,
        query: str,
        chat_history: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        Analyze query to understand intent and complexity.

        Uses LLM to extract:
        - Intent (what user wants)
        - Complexity (simple/moderate/complex)
        - Geographic scope
        - Indicator type
        - Whether Pro Mode is needed
        """
        # Format history
        history_text = ""
        if chat_history:
            for msg in chat_history[-5:]:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                history_text += f"{role}: {content}\n"

        # Create analysis prompt
        prompt = QUERY_ANALYSIS_PROMPT.format(
            query=query,
            history=history_text or "No previous context"
        )

        messages = [
            SystemMessage(content="You are a query analysis expert. Always respond with valid JSON."),
            HumanMessage(content=prompt)
        ]

        response = await self.llm.ainvoke(messages)
        response_text = response.content.strip()

        # Parse JSON response
        try:
            # Clean up response if wrapped in code blocks
            if response_text.startswith("```json"):
                response_text = response_text[7:]
            if response_text.startswith("```"):
                response_text = response_text[3:]
            if response_text.endswith("```"):
                response_text = response_text[:-3]

            analysis = json.loads(response_text.strip())
        except json.JSONDecodeError:
            logger.warning(f"Failed to parse analysis JSON: {response_text}")
            # Fallback analysis
            analysis = self._fallback_analyze(query)

        # Apply additional heuristics
        analysis = self._enhance_analysis(query, analysis)

        return analysis

    def _fallback_analyze(self, query: str) -> Dict[str, Any]:
        """Fallback analysis when LLM fails to parse."""
        query_lower = query.lower()

        # Detect complexity
        complexity = "simple"
        complexity_factors = []

        if "by province" in query_lower or "by state" in query_lower or "each" in query_lower:
            complexity = "complex"
            complexity_factors.append("regional_breakdown")

        if "compare" in query_lower or "vs" in query_lower:
            complexity = "moderate"
            complexity_factors.append("comparison")

        # Detect geographic scope
        geographic_scope = "us"  # Default
        if "canada" in query_lower or any(p in query_lower for p in ["ontario", "quebec", "british columbia", "alberta"]):
            geographic_scope = "canada"
        elif any(c in query_lower for c in ["germany", "france", "italy", "spain", "eu ", "europe"]):
            geographic_scope = "eu"
        elif "world" in query_lower or "global" in query_lower:
            geographic_scope = "global"

        # Detect indicator type
        indicator_type = "gdp"  # Default
        if "unemployment" in query_lower or "jobless" in query_lower:
            indicator_type = "unemployment"
        elif "inflation" in query_lower or "cpi" in query_lower:
            indicator_type = "inflation"
        elif "trade" in query_lower or "import" in query_lower or "export" in query_lower:
            indicator_type = "trade"
        elif "bitcoin" in query_lower or "crypto" in query_lower or "ethereum" in query_lower:
            indicator_type = "crypto"

        return {
            "intent": f"Fetch {indicator_type} data for {geographic_scope}",
            "complexity": complexity,
            "complexity_factors": complexity_factors,
            "geographic_scope": geographic_scope,
            "indicator_type": indicator_type,
            "time_period": None,
            "requires_pro_mode": complexity == "complex" and "regional_breakdown" in complexity_factors,
            "pro_mode_reason": "Regional breakdown requires Pro Mode" if complexity == "complex" else None
        }

    def _enhance_analysis(self, query: str, analysis: Dict[str, Any]) -> Dict[str, Any]:
        """Apply additional heuristics to enhance analysis."""
        query_lower = query.lower()

        # Override Pro Mode detection with more nuanced rules
        # NOT all regional queries need Pro Mode - only truly complex ones
        if "by province" in query_lower or "by state" in query_lower:
            # Check if it's a simple provincial query that StatsCan can handle
            if "canada" in query_lower or "canadian" in query_lower:
                # StatsCan can handle most provincial queries directly
                analysis["requires_pro_mode"] = False
                analysis["pro_mode_reason"] = None
                self._log_reasoning("ANALYSIS_OVERRIDE", "Provincial query - using StatsCan batch method instead of Pro Mode")

        # Detect explicit provider requests
        provider_keywords = {
            "from fred": "FRED",
            "from world bank": "WorldBank",
            "from statscan": "StatsCan",
            "from statistics canada": "StatsCan",
            "from imf": "IMF",
            "from bis": "BIS",
            "from eurostat": "Eurostat",
            "from oecd": "OECD",
        }
        for keyword, provider in provider_keywords.items():
            if keyword in query_lower:
                analysis["explicit_provider"] = provider
                self._log_reasoning("ANALYSIS", f"Explicit provider requested: {provider}")
                break

        return analysis

    async def _select_provider(
        self,
        query: str,
        analysis: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Select optimal provider based on query analysis.

        Uses LLM + rule-based logic for provider selection.
        """
        # Check for explicit provider request
        if analysis.get("explicit_provider"):
            provider = analysis["explicit_provider"]
            return {
                "primary_provider": provider,
                "primary_confidence": 0.95,
                "primary_reasoning": f"User explicitly requested {provider}",
                "fallback_providers": [],
                "strategy": "direct"
            }

        # Rule-based provider selection
        geographic_scope = analysis.get("geographic_scope", "us")
        indicator_type = analysis.get("indicator_type", "gdp")

        # Provider selection rules
        if geographic_scope == "canada":
            return {
                "primary_provider": "StatsCan",
                "primary_confidence": 0.90,
                "primary_reasoning": "Canadian data best served by Statistics Canada",
                "fallback_providers": ["WorldBank", "IMF"],
                "strategy": "with_fallback"
            }

        if geographic_scope == "us":
            return {
                "primary_provider": "FRED",
                "primary_confidence": 0.90,
                "primary_reasoning": "US economic data best served by FRED",
                "fallback_providers": ["WorldBank", "IMF"],
                "strategy": "with_fallback"
            }

        if geographic_scope == "eu":
            return {
                "primary_provider": "Eurostat",
                "primary_confidence": 0.85,
                "primary_reasoning": "EU country data best served by Eurostat",
                "fallback_providers": ["OECD", "IMF", "WorldBank"],
                "strategy": "with_fallback"
            }

        if indicator_type == "trade":
            return {
                "primary_provider": "Comtrade",
                "primary_confidence": 0.90,
                "primary_reasoning": "Trade data from UN Comtrade",
                "fallback_providers": ["WorldBank"],
                "strategy": "direct"
            }

        if indicator_type == "crypto":
            return {
                "primary_provider": "CoinGecko",
                "primary_confidence": 0.95,
                "primary_reasoning": "Cryptocurrency data from CoinGecko",
                "fallback_providers": [],
                "strategy": "direct"
            }

        if indicator_type == "interest_rate":
            return {
                "primary_provider": "BIS",
                "primary_confidence": 0.85,
                "primary_reasoning": "Central bank rates from BIS",
                "fallback_providers": ["FRED", "IMF"],
                "strategy": "with_fallback"
            }

        # Default to World Bank for global queries
        return {
            "primary_provider": "WorldBank",
            "primary_confidence": 0.75,
            "primary_reasoning": "Global indicator data from World Bank",
            "fallback_providers": ["IMF", "OECD"],
            "strategy": "with_fallback"
        }

    async def _execute_with_provider(
        self,
        query: str,
        analysis: Dict[str, Any],
        provider_selection: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute data retrieval with selected provider.

        Implements:
        - Primary provider execution
        - Fallback on failure
        - Error logging for adaptive retry
        """
        primary_provider = provider_selection["primary_provider"]
        fallback_providers = provider_selection.get("fallback_providers", [])

        # Try primary provider
        try:
            self._log_reasoning("EXECUTION", f"Trying primary provider: {primary_provider}")
            result = await self._fetch_from_provider(query, analysis, primary_provider)

            if result["success"]:
                return result

            # Primary failed, try fallbacks
            self._log_reasoning("FALLBACK", f"Primary provider failed, trying fallbacks: {fallback_providers}")

        except Exception as e:
            self._log_reasoning("ERROR", f"Primary provider error: {str(e)}")
            self.error_history.append({
                "provider": primary_provider,
                "error": str(e),
                "timestamp": datetime.now().isoformat()
            })

        # Try fallback providers
        for fallback in fallback_providers:
            try:
                self._log_reasoning("EXECUTION", f"Trying fallback provider: {fallback}")
                result = await self._fetch_from_provider(query, analysis, fallback)

                if result["success"]:
                    result["fallback_attempted"] = True
                    return result

            except Exception as e:
                self._log_reasoning("ERROR", f"Fallback {fallback} failed: {str(e)}")
                self.error_history.append({
                    "provider": fallback,
                    "error": str(e),
                    "timestamp": datetime.now().isoformat()
                })

        # All providers failed
        return {
            "success": False,
            "provider_used": None,
            "data_summary": None,
            "error_message": f"All providers failed. Last error: {self.error_history[-1]['error'] if self.error_history else 'Unknown'}",
            "fallback_attempted": True,
            "reasoning_steps": self.reasoning_log
        }

    async def _fetch_from_provider(
        self,
        query: str,
        analysis: Dict[str, Any],
        provider: str
    ) -> Dict[str, Any]:
        """
        Fetch data from a specific provider.

        Maps analysis to provider-specific parameters and calls the appropriate tool.
        """
        tool_name = f"fetch_{provider.lower()}"

        # Handle provider name variations
        provider_tool_map = {
            "statscan": "fetch_statscan",
            "statistics canada": "fetch_statscan",
            "worldbank": "fetch_worldbank",
            "world bank": "fetch_worldbank",
            "exchangerate": "fetch_exchangerate",
            "coingecko": "fetch_coingecko",
        }

        tool_name = provider_tool_map.get(provider.lower(), f"fetch_{provider.lower()}")

        if tool_name not in self.tool_map:
            raise DataNotAvailableError(f"Unknown provider: {provider}")

        tool = self.tool_map[tool_name]

        # Map analysis to tool parameters
        params = self._map_analysis_to_params(analysis, provider)

        self._log_reasoning("TOOL_CALL", f"Calling {tool_name} with params: {params}")

        # Execute tool
        try:
            result_text = await tool._arun(**params)

            # Parse result
            return {
                "success": True,
                "provider_used": provider,
                "data_summary": result_text,
                "error_message": None,
                "fallback_attempted": False,
                "reasoning_steps": self.reasoning_log
            }

        except Exception as e:
            raise DataNotAvailableError(f"{provider} error: {str(e)}")

    def _map_analysis_to_params(
        self,
        analysis: Dict[str, Any],
        provider: str
    ) -> Dict[str, Any]:
        """Map query analysis to provider-specific parameters."""
        indicator_type = analysis.get("indicator_type", "gdp")
        geographic_scope = analysis.get("geographic_scope", "us")

        # Basic parameter mapping
        params = {}

        # Map indicator
        indicator_mappings = {
            "gdp": {"FRED": "GDP", "WorldBank": "NY.GDP.MKTP.CD", "StatsCan": "GDP", "IMF": "NGDP_RPCH"},
            "unemployment": {"FRED": "UNRATE", "WorldBank": "SL.UEM.TOTL.ZS", "StatsCan": "UNEMPLOYMENT", "IMF": "LUR"},
            "inflation": {"FRED": "CPIAUCSL", "WorldBank": "FP.CPI.TOTL.ZG", "StatsCan": "INFLATION", "IMF": "PCPIPCH"},
            "housing": {"FRED": "HOUST", "StatsCan": "HOUSING_STARTS"},
            "interest_rate": {"FRED": "FEDFUNDS", "BIS": "POLICY_RATE"},
        }

        provider_upper = provider.upper()
        if indicator_type in indicator_mappings:
            params["indicator"] = indicator_mappings[indicator_type].get(
                provider_upper,
                indicator_type.upper()
            )
        else:
            params["indicator"] = indicator_type.upper()

        # Map geography
        country_mappings = {
            "us": {"FRED": None, "WorldBank": "USA", "IMF": "USA", "Eurostat": None, "OECD": "USA"},
            "canada": {"StatsCan": "Canada", "WorldBank": "CAN", "IMF": "CAN"},
            "eu": {"Eurostat": "EU27", "WorldBank": "EUU"},
        }

        if geographic_scope in country_mappings:
            country = country_mappings[geographic_scope].get(provider_upper)
            if country:
                if provider_upper == "STATSCAN":
                    params["geography"] = country
                else:
                    params["country"] = country

        return params

    async def _execute_pro_mode(self, query: str) -> Dict[str, Any]:
        """
        Execute query using Pro Mode (Grok code generation).

        Used for complex queries that require:
        - Multi-step calculations
        - Custom visualizations
        - Data transformations
        """
        self._log_reasoning("PRO_MODE", "Switching to Pro Mode for complex analysis")

        try:
            from .grok import get_grok_service
            from .code_executor import get_code_executor

            grok_service = get_grok_service()
            code_executor = get_code_executor()

            # Generate code
            self._log_reasoning("PRO_MODE", "Generating custom Python code...")
            generated_code = await grok_service.generate_code(
                query=query,
                conversation_history=[],
                available_data={},
                session_id=self.conversation_id[:8] if self.conversation_id else "react"
            )

            # Execute code
            self._log_reasoning("PRO_MODE", "Executing generated code...")
            result = await code_executor.execute_code(
                generated_code,
                session_id=self.conversation_id[:8] if self.conversation_id else "react"
            )

            if result.error:
                return {
                    "success": False,
                    "output": None,
                    "error": f"Pro Mode execution error: {result.error}",
                    "reasoning_log": self.reasoning_log
                }

            return {
                "success": True,
                "output": result.output,
                "code": generated_code,
                "files": result.files,
                "reasoning_log": self.reasoning_log,
                "error": None
            }

        except Exception as e:
            logger.exception("Pro Mode execution error")
            return {
                "success": False,
                "output": None,
                "error": f"Pro Mode failed: {str(e)}",
                "reasoning_log": self.reasoning_log
            }


# ============================================================================
# Factory Function
# ============================================================================

def create_react_agent(
    query_service: 'QueryService',
    conversation_id: Optional[str] = None,
    settings: Optional[Any] = None
) -> LangChainReActAgent:
    """
    Factory function to create ReAct agent.

    Args:
        query_service: QueryService instance
        conversation_id: Optional conversation ID
        settings: Optional settings override

    Returns:
        Configured LangChainReActAgent instance
    """
    return LangChainReActAgent(
        query_service=query_service,
        conversation_id=conversation_id,
        settings=settings
    )
