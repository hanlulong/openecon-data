"""
Deep Agents Integration for econ-data-mcp

This module integrates LangChain Deep Agents for enhanced query processing:
1. Planning capability for complex multi-step queries
2. Subagent spawning for parallel data fetching across providers
3. Context management through file system tools
4. Todo tracking for complex query workflows
5. Progress tracking with real-time updates
6. Smart provider routing based on query analysis

Author: econ-data-mcp Development Team
Date: 2025-12-24
Updated: 2025-12-25 - Enhanced with progress tracking and smarter routing
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING, Callable
from dataclasses import dataclass, field
from enum import Enum

from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from ..config import get_settings
from ..models import NormalizedData, ParsedIntent, QueryResponse

if TYPE_CHECKING:
    from .query import QueryService

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    """Status of a planning task"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ProgressUpdate:
    """Progress update for long-running operations"""
    task_id: str
    status: TaskStatus
    message: str
    progress_pct: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class ProgressTracker:
    """Tracks progress of Deep Agent operations"""

    def __init__(self):
        self.updates: List[ProgressUpdate] = []
        self.listeners: List[Callable[[ProgressUpdate], None]] = []

    def add_listener(self, callback: Callable[[ProgressUpdate], None]) -> None:
        """Add a progress listener"""
        self.listeners.append(callback)

    def update(self, task_id: str, status: TaskStatus, message: str,
               progress_pct: float = 0.0, metadata: Dict[str, Any] = None) -> None:
        """Record and broadcast a progress update"""
        update = ProgressUpdate(
            task_id=task_id,
            status=status,
            message=message,
            progress_pct=progress_pct,
            metadata=metadata or {}
        )
        self.updates.append(update)
        logger.info(f"📊 Progress [{task_id}]: {message} ({progress_pct:.0f}%)")

        for listener in self.listeners:
            try:
                listener(update)
            except Exception as e:
                logger.warning(f"Progress listener error: {e}")

    def get_summary(self) -> Dict[str, Any]:
        """Get progress summary"""
        if not self.updates:
            return {"total_tasks": 0, "completed": 0, "failed": 0}

        completed = sum(1 for u in self.updates if u.status == TaskStatus.COMPLETED)
        failed = sum(1 for u in self.updates if u.status == TaskStatus.FAILED)

        return {
            "total_tasks": len(set(u.task_id for u in self.updates)),
            "completed": completed,
            "failed": failed,
            "updates": [
                {"task": u.task_id, "status": u.status.value, "message": u.message}
                for u in self.updates[-10:]  # Last 10 updates
            ]
        }

# Provider-specific descriptions and capabilities for smart routing
PROVIDER_CAPABILITIES = {
    "FRED": {
        "description": "Federal Reserve Economic Data - US economic indicators",
        "specialties": ["us", "gdp", "unemployment", "inflation", "interest rate", "housing", "monetary policy"],
        "countries": ["us", "usa", "united states", "america"],
        "indicators": ["gdp", "unemployment", "cpi", "pce", "federal funds", "housing starts", "retail sales"],
        "priority": 1,  # Higher = preferred
    },
    "WorldBank": {
        "description": "World Bank Development Indicators - Global development data",
        "specialties": ["global", "development", "poverty", "education", "health", "population"],
        "countries": ["all"],  # Global coverage
        "indicators": ["gdp", "population", "life expectancy", "literacy", "poverty", "gni"],
        "priority": 2,
    },
    "Eurostat": {
        "description": "European Union Statistics - EU member country data",
        "specialties": ["eu", "europe", "hicp", "eurozone"],
        "countries": ["eu", "europe", "germany", "france", "italy", "spain", "netherlands", "belgium", "austria", "greece", "portugal", "ireland"],
        "indicators": ["gdp", "unemployment", "inflation", "hicp", "population", "trade"],
        "priority": 1,
    },
    "IMF": {
        "description": "International Monetary Fund - International financial statistics",
        "specialties": ["debt", "fiscal", "balance of payments", "reserves", "exchange rate"],
        "countries": ["all"],
        "indicators": ["government debt", "fiscal balance", "current account", "reserves", "exchange rate"],
        "priority": 2,
    },
    "BIS": {
        "description": "Bank for International Settlements - Central bank data",
        "specialties": ["central bank", "policy rate", "credit", "property prices"],
        "countries": ["all"],
        "indicators": ["policy rate", "credit", "debt", "property prices"],
        "priority": 2,
    },
    "StatsCan": {
        "description": "Statistics Canada - Canadian data",
        "specialties": ["canada", "canadian"],
        "countries": ["canada", "ca"],
        "indicators": ["gdp", "unemployment", "cpi", "housing", "trade", "population"],
        "priority": 1,
    },
    "OECD": {
        "description": "OECD - Comparative data for member countries",
        "specialties": ["oecd", "comparative", "education", "environment"],
        "countries": ["oecd members"],
        "indicators": ["gdp", "unemployment", "education", "productivity"],
        "priority": 3,  # Lower priority due to rate limits
    },
    "Comtrade": {
        "description": "UN Comtrade - International trade flows",
        "specialties": ["trade", "imports", "exports", "bilateral"],
        "countries": ["all"],
        "indicators": ["exports", "imports", "trade balance", "commodities"],
        "priority": 1,
    },
    "ExchangeRate": {
        "description": "Currency exchange rates",
        "specialties": ["forex", "currency", "exchange"],
        "countries": ["all"],
        "indicators": ["exchange rate", "forex", "currency"],
        "priority": 1,
    },
    "CoinGecko": {
        "description": "Cryptocurrency data",
        "specialties": ["crypto", "bitcoin", "ethereum", "cryptocurrency"],
        "countries": ["global"],
        "indicators": ["bitcoin", "ethereum", "crypto", "market cap"],
        "priority": 1,
    },
}


def select_best_provider(indicator: str, country: str = None, is_multi_country: bool = False) -> str:
    """
    Intelligently select the best provider for a given indicator and country.

    Enhanced to handle multi-country queries - prefers global providers
    (WorldBank, IMF) over country-specific ones (FRED) for region queries.

    Args:
        indicator: The economic indicator requested
        country: The country/region requested (optional)
        is_multi_country: Whether this is part of a multi-country query (G7, BRICS, etc.)

    Returns:
        The best provider name for this query
    """
    indicator_lower = indicator.lower()
    country_lower = (country or "").lower()

    # For multi-country queries, prefer global providers
    if is_multi_country:
        # GDP, population, development indicators → WorldBank
        if any(kw in indicator_lower for kw in ["gdp", "population", "poverty", "literacy", "life expectancy", "growth"]):
            return "WorldBank"
        # Fiscal/debt/balance of payments → IMF
        if any(kw in indicator_lower for kw in ["debt", "deficit", "current account", "reserves", "fiscal", "balance"]):
            return "IMF"
        # Trade data → Comtrade
        if any(kw in indicator_lower for kw in ["trade", "export", "import"]):
            return "Comtrade"
        # EU-specific → Eurostat (if country is in EU)
        if any(kw in indicator_lower for kw in ["hicp", "harmonized"]):
            return "Eurostat"
        # Default for multi-country: WorldBank (global coverage)
        return "WorldBank"

    best_provider = None
    best_score = -1

    for provider, caps in PROVIDER_CAPABILITIES.items():
        score = 0

        # Check indicator match
        for ind in caps["indicators"]:
            if ind in indicator_lower or indicator_lower in ind:
                score += 10

        # Check specialty match
        for spec in caps["specialties"]:
            if spec in indicator_lower or spec in country_lower:
                score += 5

        # Check country match
        if country_lower:
            if "all" in caps["countries"]:
                score += 2
            else:
                for c in caps["countries"]:
                    if c in country_lower or country_lower in c:
                        score += 15  # Strong country match

        # Apply priority modifier
        score += caps["priority"]

        if score > best_score:
            best_score = score
            best_provider = provider

    return best_provider or "WorldBank"  # Default fallback


class DeepAgentConfig(BaseModel):
    """Configuration for Deep Agent orchestrator."""

    model: str = Field(default="openai/gpt-4o-mini", description="LLM model for the agent")
    enable_planning: bool = Field(default=True, description="Enable todo planning for complex queries")
    enable_subagents: bool = Field(default=True, description="Enable subagent spawning for parallel fetches")
    max_concurrent_subagents: int = Field(default=5, description="Maximum concurrent subagent calls")
    planning_threshold: int = Field(default=3, description="Number of providers/entities that trigger planning mode")
    enable_smart_routing: bool = Field(default=True, description="Enable intelligent provider selection")
    enable_progress_tracking: bool = Field(default=True, description="Enable detailed progress tracking")
    retry_failed_tasks: bool = Field(default=True, description="Retry failed tasks with alternate providers")


class DeepAgentOrchestrator:
    """
    Deep Agents-based orchestrator for complex economic data queries.

    Capabilities:
    1. Automatic planning for multi-step queries
    2. Parallel data fetching via subagents
    3. Context management for long conversations
    4. Intelligent provider routing
    5. Progress tracking with real-time updates
    6. Automatic retry with alternate providers
    """

    def __init__(
        self,
        query_service: 'QueryService',
        config: Optional[DeepAgentConfig] = None,
        progress_callback: Optional[Callable[[ProgressUpdate], None]] = None,
    ):
        """Initialize the Deep Agent orchestrator."""
        self.query_service = query_service
        self.config = config or DeepAgentConfig()
        self.settings = get_settings()

        # Initialize LLM
        self.llm = self._initialize_llm()

        # Track active tasks
        self.active_todos: List[Dict[str, Any]] = []

        # Progress tracking
        self.progress_tracker = ProgressTracker()
        if progress_callback:
            self.progress_tracker.add_listener(progress_callback)

        # Execution statistics
        self.stats = {
            "total_queries": 0,
            "successful_fetches": 0,
            "failed_fetches": 0,
            "retried_fetches": 0,
            "avg_parallel_tasks": 0.0,
        }

        logger.info("Deep Agent orchestrator initialized with planning, progress tracking, and smart routing")

    def _initialize_llm(self) -> ChatOpenAI:
        """Initialize the LLM for the Deep Agent."""
        return ChatOpenAI(
            model=self.config.model,
            openai_api_key=self.settings.openrouter_api_key,
            openai_api_base="https://openrouter.ai/api/v1",
            temperature=0.1,
            max_tokens=2000,
            default_headers={
                "HTTP-Referer": "https://openecon.ai",
                "X-Title": "econ-data-mcp Deep Agent"
            }
        )

    def _create_fetch_tool(self, provider: str) -> Callable:
        """Create a data fetching tool for a specific provider."""

        @tool
        async def fetch_data(
            indicator: str,
            country: Optional[str] = None,
            start_date: Optional[str] = None,
            end_date: Optional[str] = None,
        ) -> Dict[str, Any]:
            """
            Fetch economic data from the data provider.

            Args:
                indicator: The economic indicator to fetch (e.g., "GDP", "UNEMPLOYMENT")
                country: Country code or name (optional)
                start_date: Start date in YYYY-MM-DD format (optional)
                end_date: End date in YYYY-MM-DD format (optional)

            Returns:
                Dict with data and metadata
            """
            try:
                intent = ParsedIntent(
                    apiProvider=provider,
                    indicators=[indicator],
                    parameters={
                        "country": country,
                        "startDate": start_date,
                        "endDate": end_date,
                    },
                    clarificationNeeded=False
                )

                data = await self.query_service._fetch_data(intent)

                # CRITICAL FIX: Filter None values from data list
                valid_data = [d for d in data if d is not None] if data else []

                if valid_data:
                    # Safe access to first valid element
                    first_data = valid_data[0]
                    return {
                        "success": True,
                        "provider": provider,
                        "indicator": indicator,
                        "data": valid_data,  # Include filtered data
                        "data_points": len(first_data.data) if first_data.data else 0,
                        "metadata": first_data.metadata.model_dump() if first_data.metadata else None,
                    }
                else:
                    return {
                        "success": False,
                        "error": f"No data available for {indicator} from {provider}",
                    }

            except Exception as e:
                logger.error(f"Fetch tool error for {provider}: {e}")
                return {
                    "success": False,
                    "error": str(e),
                }

        fetch_data.__name__ = f"fetch_{provider.lower()}_data"
        fetch_data.__doc__ = f"""Fetch data from {provider}. {PROVIDER_CAPABILITIES.get(provider, {}).get('description', '')}"""

        return fetch_data

    def _analyze_query_complexity(self, query: str) -> Dict[str, Any]:
        """
        Analyze query to determine if planning mode is needed.

        Enhanced with region expansion support - detects G7, BRICS, EU, ASEAN, etc.
        and expands them to individual countries for parallel fetching.

        Returns:
            Dict with complexity analysis results
        """
        from ..routing.country_resolver import CountryResolver

        query_lower = query.lower()

        # Count entities that might need separate fetches
        countries_mentioned = []
        providers_mentioned = []
        indicators_mentioned = []
        regions_detected = []

        # STEP 1: Detect and expand region groups FIRST (G7, BRICS, EU, ASEAN, etc.)
        regions_detected = CountryResolver.detect_regions_in_query(query)
        if regions_detected:
            # Expand all detected regions to their member countries
            expanded_countries = CountryResolver.expand_regions_in_query(query)
            countries_mentioned.extend(expanded_countries)
            logger.info(f"Expanded regions {regions_detected} to {len(expanded_countries)} countries: {expanded_countries[:5]}...")

        # STEP 2: Also detect individual countries mentioned
        country_patterns = [
            "us", "usa", "united states", "america",
            "uk", "britain", "united kingdom",
            "germany", "france", "italy", "spain",
            "canada", "japan", "china", "india",
            "brazil", "mexico", "australia",
        ]
        for c in country_patterns:
            if c in query_lower:
                # Normalize to ISO code
                iso_code = CountryResolver.normalize(c)
                if iso_code and iso_code not in countries_mentioned:
                    countries_mentioned.append(iso_code)

        # Provider detection
        for provider in PROVIDER_CAPABILITIES.keys():
            if provider.lower() in query_lower:
                providers_mentioned.append(provider)

        # Indicator detection
        indicator_patterns = [
            "gdp", "unemployment", "inflation", "interest rate",
            "housing", "trade", "exports", "imports",
            "population", "debt", "deficit", "cpi",
        ]
        for ind in indicator_patterns:
            if ind in query_lower:
                indicators_mentioned.append(ind)

        # Determine complexity
        total_entities = len(countries_mentioned) + len(indicators_mentioned)
        is_comparison = any(word in query_lower for word in [
            "compare", "vs", "versus", "and", "both", "all"
        ])

        # Region queries always need planning (multiple countries)
        is_multi_country = len(countries_mentioned) > 1 or len(regions_detected) > 0

        needs_planning = (
            total_entities >= self.config.planning_threshold or
            len(providers_mentioned) > 1 or
            is_comparison or
            is_multi_country  # NEW: Region queries need planning
        )

        return {
            "needs_planning": needs_planning,
            "countries": countries_mentioned,
            "regions": regions_detected,  # NEW: Track detected regions
            "providers": providers_mentioned,
            "indicators": indicators_mentioned,
            "is_comparison": is_comparison,
            "is_multi_country": is_multi_country,  # NEW: Flag for multi-country
            "complexity_score": total_entities,
        }

    def _create_todos_from_analysis(
        self,
        query: str,
        analysis: Dict[str, Any]
    ) -> List[Dict[str, Any]]:
        """Create todo items from query analysis."""
        todos = []

        if analysis["is_comparison"]:
            # Create todos for each comparison element
            for i, indicator in enumerate(analysis["indicators"]):
                for country in analysis["countries"] or ["global"]:
                    todos.append({
                        "id": f"fetch_{i}",
                        "content": f"Fetch {indicator} data for {country}",
                        "status": "pending",
                        "priority": i + 1,
                    })

            # Add comparison todo
            todos.append({
                "id": "compare",
                "content": "Compare and visualize the fetched datasets",
                "status": "pending",
                "priority": len(todos) + 1,
            })
        else:
            # Single query todo
            todos.append({
                "id": "fetch_1",
                "content": f"Fetch data: {query}",
                "status": "pending",
                "priority": 1,
            })

        return todos

    async def execute(
        self,
        query: str,
        conversation_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Execute query using Deep Agent capabilities.

        Args:
            query: User's natural language query
            conversation_id: Optional conversation ID

        Returns:
            Dict with execution results
        """
        start_time = time.time()
        self.stats["total_queries"] += 1

        try:
            # Step 1: Analyze query complexity
            self.progress_tracker.update(
                "analysis", TaskStatus.IN_PROGRESS,
                "Analyzing query complexity...", 10
            )
            analysis = self._analyze_query_complexity(query)
            logger.info(f"Query complexity: {analysis}")

            self.progress_tracker.update(
                "analysis", TaskStatus.COMPLETED,
                f"Found {len(analysis['indicators'])} indicators, {len(analysis['countries'])} countries",
                20, {"analysis": analysis}
            )

            # Step 2: Create planning todos if needed
            if self.config.enable_planning and analysis["needs_planning"]:
                self.active_todos = self._create_todos_from_analysis(query, analysis)
                self.progress_tracker.update(
                    "planning", TaskStatus.COMPLETED,
                    f"Created {len(self.active_todos)} tasks", 30
                )
                logger.info(f"Created {len(self.active_todos)} todos for query planning")

            # Step 3: Execute with appropriate strategy
            if analysis["is_comparison"] or len(analysis["indicators"]) > 1 or len(analysis["countries"]) > 1:
                # Parallel fetch for multi-entity queries
                self.progress_tracker.update(
                    "execution", TaskStatus.IN_PROGRESS,
                    "Starting parallel data fetch...", 40
                )
                result = await self._execute_parallel_fetch(query, analysis, conversation_id)
            else:
                # Standard fetch (delegate to existing orchestrator)
                self.progress_tracker.update(
                    "execution", TaskStatus.IN_PROGRESS,
                    "Fetching data...", 40
                )
                result = await self._execute_standard_fetch(query, conversation_id)

            # Add execution stats
            result["execution_time"] = time.time() - start_time
            result["progress_summary"] = self.progress_tracker.get_summary()

            self.progress_tracker.update(
                "complete", TaskStatus.COMPLETED,
                f"Completed in {result['execution_time']:.2f}s", 100
            )

            return result

        except Exception as e:
            self.progress_tracker.update(
                "error", TaskStatus.FAILED,
                f"Error: {str(e)}", 0
            )
            logger.error(f"Deep Agent execution error: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "todos": self.active_todos,
                "execution_time": time.time() - start_time,
            }

    async def _execute_parallel_fetch(
        self,
        query: str,
        analysis: Dict[str, Any],
        conversation_id: Optional[str],
    ) -> Dict[str, Any]:
        """Execute parallel data fetching for comparison queries with smart routing."""
        tasks = []
        task_count = 0

        # Create fetch tasks for each indicator/country combination
        countries = analysis["countries"] or ["US"]
        indicators = analysis["indicators"]
        is_multi_country = analysis.get("is_multi_country", len(countries) > 1)
        regions = analysis.get("regions", [])
        total_tasks = len(indicators) * len(countries)

        self.progress_tracker.update(
            "parallel_setup", TaskStatus.IN_PROGRESS,
            f"Setting up {total_tasks} parallel fetch tasks for {len(countries)} countries...", 45
        )

        if regions:
            logger.info(f"Region query detected: {regions} expanded to {len(countries)} countries")

        for indicator in indicators:
            for country in countries:
                task_count += 1
                task_id = f"fetch_{task_count}"

                # Smart provider selection - pass is_multi_country flag
                if self.config.enable_smart_routing:
                    provider = select_best_provider(indicator, country, is_multi_country)
                    logger.info(f"Smart routing: {indicator}/{country} → {provider} (multi_country={is_multi_country})")
                else:
                    provider = None

                # Update todo status
                for todo in self.active_todos:
                    if indicator in todo["content"].lower() and country in todo["content"].lower():
                        todo["status"] = "in_progress"

                # Create async task with provider hint
                task = asyncio.create_task(
                    self._fetch_single_with_retry(indicator, country, provider, task_id)
                )
                tasks.append({
                    "task": task,
                    "task_id": task_id,
                    "indicator": indicator,
                    "country": country,
                    "provider": provider,
                })

        self.progress_tracker.update(
            "parallel_setup", TaskStatus.COMPLETED,
            f"Created {len(tasks)} fetch tasks", 50
        )

        # Execute with concurrency limit
        semaphore = asyncio.Semaphore(self.config.max_concurrent_subagents)
        completed_count = 0

        async def fetch_with_limit(task_info):
            nonlocal completed_count
            async with semaphore:
                self.progress_tracker.update(
                    task_info["task_id"], TaskStatus.IN_PROGRESS,
                    f"Fetching {task_info['indicator']} for {task_info['country']}...",
                    50 + (completed_count / len(tasks)) * 40
                )

                result = await task_info["task"]
                completed_count += 1

                status = TaskStatus.COMPLETED if result.get("success") else TaskStatus.FAILED
                self.progress_tracker.update(
                    task_info["task_id"], status,
                    f"{'✓' if result.get('success') else '✗'} {task_info['indicator']}/{task_info['country']}",
                    50 + (completed_count / len(tasks)) * 40
                )

                return {
                    "indicator": task_info["indicator"],
                    "country": task_info["country"],
                    "provider": task_info["provider"],
                    "result": result,
                }

        # Wait for all fetches
        completed = await asyncio.gather(
            *[fetch_with_limit(t) for t in tasks],
            return_exceptions=True
        )

        # Process results
        # CRITICAL FIX: Safely handle None results and missing keys
        data_results = []
        errors = []

        for result in completed:
            if isinstance(result, Exception):
                errors.append(str(result))
            elif result is None:
                # Skip None results from failed parallel tasks
                errors.append("Parallel task returned None")
            elif not isinstance(result, dict):
                errors.append(f"Invalid result type: {type(result)}")
            elif result.get("result") is None:
                errors.append("Result object is None")
            elif result.get("result", {}).get("success"):
                data_results.append(result)
                # Update todo status
                indicator = result.get("indicator", "")
                country = result.get("country", "")
                for todo in self.active_todos:
                    if (indicator and indicator.lower() in todo.get("content", "").lower() and
                        country and country.lower() in todo.get("content", "").lower()):
                        todo["status"] = "completed"
            else:
                errors.append(result.get("result", {}).get("error", "Unknown error"))

        return {
            "success": len(data_results) > 0,
            "data_count": len(data_results),
            "results": data_results,
            "errors": errors if errors else None,
            "todos": self.active_todos,
            "parallel_execution": True,
        }

    async def _fetch_single_with_retry(
        self,
        indicator: str,
        country: str,
        preferred_provider: Optional[str] = None,
        task_id: str = "fetch",
    ) -> Dict[str, Any]:
        """
        Fetch single indicator/country with retry and fallback providers.

        Args:
            indicator: Economic indicator to fetch
            country: Country/region
            preferred_provider: Preferred provider from smart routing
            task_id: Task ID for progress tracking

        Returns:
            Dict with success status and data
        """
        # Build query with provider hint if available
        if preferred_provider:
            query = f"{indicator} for {country} from {preferred_provider}"
        else:
            query = f"{indicator} for {country}"

        # Try primary fetch
        result = await self._fetch_single(indicator, country, query)

        # Retry with alternate provider if failed and retry is enabled
        if not result.get("success") and self.config.retry_failed_tasks:
            self.stats["retried_fetches"] += 1

            # Get alternate providers
            alternates = self._get_alternate_providers(indicator, country, preferred_provider)

            for alt_provider in alternates[:2]:  # Try up to 2 alternates
                self.progress_tracker.update(
                    task_id, TaskStatus.IN_PROGRESS,
                    f"Retrying {indicator}/{country} with {alt_provider}...",
                    progress_pct=0
                )

                alt_query = f"{indicator} for {country} from {alt_provider}"
                result = await self._fetch_single(indicator, country, alt_query)

                if result.get("success"):
                    result["retried"] = True
                    result["final_provider"] = alt_provider
                    break

        # Update stats
        if result.get("success"):
            self.stats["successful_fetches"] += 1
        else:
            self.stats["failed_fetches"] += 1

        return result

    def _get_alternate_providers(
        self,
        indicator: str,
        country: str,
        exclude_provider: Optional[str] = None
    ) -> List[str]:
        """Get alternate providers for retry, sorted by relevance."""
        alternates = []

        for provider, caps in PROVIDER_CAPABILITIES.items():
            if provider == exclude_provider:
                continue

            score = 0
            indicator_lower = indicator.lower()
            country_lower = country.lower()

            # Check indicator match
            for ind in caps["indicators"]:
                if ind in indicator_lower:
                    score += 5

            # Check country match
            if "all" in caps["countries"]:
                score += 2
            else:
                for c in caps["countries"]:
                    if c in country_lower:
                        score += 10

            if score > 0:
                alternates.append((provider, score))

        # Sort by score descending
        alternates.sort(key=lambda x: x[1], reverse=True)
        return [p[0] for p in alternates]

    async def _fetch_single(
        self,
        indicator: str,
        country: str,
        query: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch single indicator/country combination."""
        try:
            # Use existing query service
            if not query:
                query = f"{indicator} for {country}"
            result = await self.query_service.process_query(query)

            return {
                "success": result.data is not None and len(result.data) > 0,
                "data": result.data,
                "error": result.error,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
            }

    async def _execute_standard_fetch(
        self,
        query: str,
        conversation_id: Optional[str],
    ) -> Dict[str, Any]:
        """Execute standard query through existing service."""
        try:
            result = await self.query_service.process_query(
                query,
                conversation_id=conversation_id
            )

            # Update todos
            for todo in self.active_todos:
                todo["status"] = "completed" if result.data else "failed"

            return {
                "success": result.data is not None,
                "data": result.data,
                "intent": result.intent,
                "error": result.error,
                "todos": self.active_todos,
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "todos": self.active_todos,
            }

    def get_active_todos(self) -> List[Dict[str, Any]]:
        """Get current todo list."""
        return self.active_todos.copy()

    def update_todo(self, todo_id: str, status: str) -> bool:
        """Update a todo item's status."""
        for todo in self.active_todos:
            if todo["id"] == todo_id:
                todo["status"] = status
                return True
        return False


# Factory function
def create_deep_agent_orchestrator(
    query_service: 'QueryService',
    config: Optional[DeepAgentConfig] = None,
) -> DeepAgentOrchestrator:
    """Create a Deep Agent orchestrator instance."""
    return DeepAgentOrchestrator(
        query_service=query_service,
        config=config,
    )
