"""
Router Agent for Query Classification and Reference Resolution

This agent is responsible for:
1. Classifying query types (research, data_fetch, follow_up, comparison, analysis)
2. Resolving entity references ("it", "that", "unconsolidated")
3. Building context for downstream agents
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ..memory.conversation_state import (
    ConversationState,
    DataReference,
    QueryType,
)

logger = logging.getLogger(__name__)


@dataclass
class RoutingResult:
    """Result of query routing"""
    query_type: QueryType
    context: Dict[str, Any]
    resolved_references: List[DataReference]
    merge_with_previous: bool = False
    variants_requested: List[str] = None

    def __post_init__(self):
        if self.variants_requested is None:
            self.variants_requested = []


class RouterAgent:
    """
    Routes queries to appropriate specialist agents.

    Pattern-based classification with entity resolution from conversation state.
    """

    # Research patterns - questions about data availability
    RESEARCH_PATTERNS = [
        r"\bdoes\s+(\w+)\s+have\b",
        r"\bis\s+.+\s+available\b",
        r"\bwhat\s+data\s+(is|are)\b",
        r"\bcan\s+(I|you|we)\s+get\b",
        r"\bhow\s+(do|can)\s+(I|you|we)\s+(find|get)\b",
        r"\bwhich\s+provider\s+has\b",
        r"\bwhere\s+(can|do)\s+(I|you|we)\s+find\b",
        r"\bis\s+there\s+(any\s+)?data\b",
        r"\bdo\s+you\s+have\b",
    ]

    # Follow-up patterns - references to previous queries
    FOLLOW_UP_PATTERNS = [
        r"^(plot|show|display)\s+(it|that|this|the\s+same)\b",
        r"\bon\s+the\s+same\s+(graph|chart|plot)\b",
        r"^(same|identical)\s+(but|with)\b",
        r"^(now|also)\s+(show|plot|add|display)\b",
        r"^(what\s+about|how\s+about)\b",
        r"^add\s+(it|that|this|the\s+data)\b",
        r"\b(show|plot)\s+(it|that|this)\s+(as|in)\b",
        r"^(and|also)\s+(the|show|plot)\b",
        r"\bthe\s+same\s+(data|thing|series)\b",
    ]

    # Comparison patterns - for VARIANT comparisons (consolidated vs unconsolidated)
    # NOT for different asset comparisons (Bitcoin vs gold) - those go through DATA_FETCH
    COMPARISON_PATTERNS = [
        r"\b(consolidated|unconsolidated)\s+and\s+(consolidated|unconsolidated)\b",
        r"\b(both|multiple|all)\s+.*(on\s+(one|the\s+same)\s+(graph|chart))",
        r"\btogether\s+on\s+(one|the\s+same)\b",
        r"\bplot\s+.+\s+(consolidated|unconsolidated).+\s+(on|together)\b",
        r"\b(side\s+by\s+side|overlay)\b.*(consolidated|unconsolidated)",
    ]

    # Asset comparison patterns - these should go to DATA_FETCH, not COMPARISON
    # COMPARISON is for variants of the same indicator, not different assets
    ASSET_COMPARISON_PATTERNS = [
        r"\bcompare\s+\w+\s+(?:and|with|to|vs\.?)\s+\w+\s*(?:prices?|data)?",
        r"\b\w+\s+vs\.?\s+\w+\s+(?:prices?|data)",
    ]

    # Analysis patterns - require Pro Mode
    # IMPORTANT: These patterns should indicate that the user wants us to PERFORM
    # calculations, not just fetch data that happens to have "rate" or "growth" in the name.
    #
    # For example:
    # - "calculate the growth rate from GDP data" -> ANALYSIS (calculation request)
    # - "US GDP growth rate" -> DATA_FETCH (just fetching a pre-calculated indicator)
    ANALYSIS_PATTERNS = [
        r"\bcalculate\b",
        r"\bcompute\b",
        r"\bderive\b",
        r"\bdetermine\b",
        # Removed: r"\bgrowth\s+rate\b" - "GDP growth rate" is an indicator NAME, not analysis
        r"\bpercentage\s+change\b",
        r"\bcorrelation\b",
        r"\btrend\s+analysis\b",
        r"\bforecast\b",
        r"\bproject(ion)?\b",
        r"\bregression\b",
        r"\bstatistical\b",
        r"\baverage\s+of\b",
        r"\bsum\s+of\b",
        r"\bratio\s+of\b",
        # More specific patterns that truly indicate calculation requests
        r"\bcalculate\s+(?:the\s+)?(?:growth|change|rate)\b",
        r"\bcompute\s+(?:the\s+)?(?:growth|change|rate)\b",
        r"\bcorrelation\s+between\b",
        r"\byear-over-year\s+change\b",
    ]

    # Common indicator name patterns that should NOT be classified as analysis
    # These are pre-calculated indicators available directly from data providers
    INDICATOR_NAME_PATTERNS = [
        r"\bgdp\s+growth(?:\s+rate)?\b",
        r"\binflation\s+rate\b",
        r"\bunemployment\s+rate\b",
        r"\binterest\s+rate\b",
        r"\bexchange\s+rate\b",
        r"\bpopulation\s+growth\b",
        r"\beconomic\s+growth\b",
        r"\bcpi\b",
        r"\bhousing\s+starts\b",
        r"\bworking\s+hours\b",
        r"\blabor\s+productivity\b",
        r"\bproductivity\s+growth\b",
    ]

    # Variant keywords for data (consolidated/unconsolidated, etc.)
    VARIANT_KEYWORDS = {
        "consolidated": ["consolidated", "cons", "combined"],
        "unconsolidated": ["unconsolidated", "uncons", "unconsolidated"],
        "seasonally_adjusted": ["sa", "seasonally adjusted", "seas adj"],
        "not_seasonally_adjusted": ["nsa", "not seasonally adjusted", "unadjusted"],
        "nominal": ["nominal", "current prices"],
        "real": ["real", "constant prices", "inflation adjusted"],
    }

    def classify(
        self,
        query: str,
        state: Optional[ConversationState]
    ) -> RoutingResult:
        """
        Classify query and resolve references.

        Args:
            query: User's query text
            state: Current conversation state (may be None for first query)

        Returns:
            RoutingResult with query type, context, and resolved references
        """
        query_lower = query.lower().strip()
        context: Dict[str, Any] = {}
        resolved_refs: List[DataReference] = []
        merge_with_previous = False
        variants: List[str] = []

        # 1. Check research patterns first (highest priority)
        if self._matches_patterns(query_lower, self.RESEARCH_PATTERNS):
            context = self._extract_research_context(query)
            logger.info(f"Classified as RESEARCH: {query[:50]}...")
            return RoutingResult(
                query_type=QueryType.RESEARCH,
                context=context,
                resolved_references=resolved_refs,
            )

        # 2. Check comparison patterns
        comp_match, variants = self._check_comparison(query_lower)
        if comp_match:
            context = self._resolve_comparison_context(query, state, variants)
            if state and state.entity_context.current_datasets:
                resolved_refs = [state.entity_context.get_last_dataset()]
            merge_with_previous = "same graph" in query_lower or "one graph" in query_lower
            logger.info(f"Classified as COMPARISON with variants {variants}: {query[:50]}...")
            return RoutingResult(
                query_type=QueryType.COMPARISON,
                context=context,
                resolved_references=resolved_refs,
                merge_with_previous=merge_with_previous,
                variants_requested=variants,
            )

        # 3. Check follow-up patterns
        if self._matches_patterns(query_lower, self.FOLLOW_UP_PATTERNS):
            if state and state.entity_context.current_datasets:
                context = self._resolve_follow_up_context(query, state)
                resolved_refs = [state.entity_context.get_last_dataset()]
                merge_with_previous = "same graph" in query_lower or "same chart" in query_lower
                logger.info(f"Classified as FOLLOW_UP: {query[:50]}...")
                return RoutingResult(
                    query_type=QueryType.FOLLOW_UP,
                    context=context,
                    resolved_references=resolved_refs,
                    merge_with_previous=merge_with_previous,
                )

        # 4. Check analysis patterns (but NOT for known indicator names)
        # "GDP growth rate" is an indicator name, NOT an analysis request
        is_indicator_name = self._matches_patterns(query_lower, self.INDICATOR_NAME_PATTERNS)
        if not is_indicator_name and self._matches_patterns(query_lower, self.ANALYSIS_PATTERNS):
            context = self._extract_analysis_context(query, state)
            if state:
                resolved_refs = state.get_all_data_references()
            logger.info(f"Classified as ANALYSIS: {query[:50]}...")
            return RoutingResult(
                query_type=QueryType.ANALYSIS,
                context=context,
                resolved_references=resolved_refs,
            )

        # 5. Default to data fetch
        context = self._extract_data_context(query)
        logger.info(f"Classified as DATA_FETCH: {query[:50]}...")
        return RoutingResult(
            query_type=QueryType.DATA_FETCH,
            context=context,
            resolved_references=resolved_refs,
        )

    def _matches_patterns(self, query: str, patterns: List[str]) -> bool:
        """Check if query matches any of the patterns"""
        for pattern in patterns:
            if re.search(pattern, query, re.IGNORECASE):
                return True
        return False

    def _check_comparison(self, query: str) -> Tuple[bool, List[str]]:
        """Check if query is a comparison and extract variants"""
        variants = []

        # Check for explicit variant mentions
        for variant_name, keywords in self.VARIANT_KEYWORDS.items():
            for keyword in keywords:
                if keyword.lower() in query:
                    variants.append(variant_name)
                    break

        # Check comparison patterns
        if len(variants) >= 2:
            return True, variants

        # Check for "and" pattern with variants
        for pattern in self.COMPARISON_PATTERNS:
            if re.search(pattern, query, re.IGNORECASE):
                return True, variants

        return False, variants

    def _extract_research_context(self, query: str) -> Dict[str, Any]:
        """Extract context for research query"""
        context = {"research_mode": True}

        # Extract provider name
        providers = {
            "eurostat": "EUROSTAT",
            "fred": "FRED",
            "worldbank": "WORLDBANK",
            "world bank": "WORLDBANK",
            "imf": "IMF",
            "oecd": "OECD",
            "bis": "BIS",
            "statscan": "STATSCAN",
            "statistics canada": "STATSCAN",
            "comtrade": "COMTRADE",
            "un comtrade": "COMTRADE",
        }

        query_lower = query.lower()
        for provider_text, provider_code in providers.items():
            if provider_text in query_lower:
                context["provider"] = provider_code
                break

        # Extract what they're looking for
        # Pattern: "does X have Y?" or "is Y available in X?"
        have_match = re.search(r"have\s+(.+?)(?:\?|$)", query, re.IGNORECASE)
        if have_match:
            context["indicator"] = have_match.group(1).strip().rstrip("?")

        available_match = re.search(r"(.+?)\s+available", query, re.IGNORECASE)
        if available_match and "indicator" not in context:
            context["indicator"] = available_match.group(1).strip()

        return context

    def _resolve_follow_up_context(
        self,
        query: str,
        state: ConversationState
    ) -> Dict[str, Any]:
        """Resolve references in follow-up query"""
        context = {"follow_up_mode": True}
        query_lower = query.lower()

        # Get last discussed dataset
        last_dataset = state.entity_context.get_last_dataset()
        if last_dataset:
            context["base_dataset"] = last_dataset
            context["provider"] = last_dataset.provider
            context["indicator"] = last_dataset.indicator
            context["country"] = last_dataset.country
            context["time_range"] = last_dataset.time_range

        # Check for variant requests
        if "unconsolidated" in query_lower:
            context["dataset_variant"] = "unconsolidated"
        elif "consolidated" in query_lower:
            context["dataset_variant"] = "consolidated"

        # Check for frequency change requests
        if "monthly" in query_lower:
            context["frequency"] = "monthly"
        elif "quarterly" in query_lower:
            context["frequency"] = "quarterly"
        elif "annual" in query_lower or "yearly" in query_lower:
            context["frequency"] = "annual"

        # Check for "same graph" requests
        if any(p in query_lower for p in ["same graph", "same chart", "one graph", "one chart"]):
            context["merge_with_previous"] = True
            context["chart_type"] = state.entity_context.current_chart_type

        return context

    def _resolve_comparison_context(
        self,
        query: str,
        state: Optional[ConversationState],
        variants: List[str]
    ) -> Dict[str, Any]:
        """Resolve comparison context"""
        context = {
            "comparison_mode": True,
            "variants": variants,
        }

        if state:
            last_dataset = state.entity_context.get_last_dataset()
            if last_dataset:
                context["base_dataset"] = last_dataset
                context["provider"] = last_dataset.provider
                context["indicator"] = last_dataset.indicator
                context["country"] = last_dataset.country

        # Check for merge request
        query_lower = query.lower()
        if any(p in query_lower for p in ["one graph", "same graph", "together", "overlay"]):
            context["merge_series"] = True

        return context

    def _extract_analysis_context(
        self,
        query: str,
        state: Optional[ConversationState]
    ) -> Dict[str, Any]:
        """Extract context for analysis query"""
        context = {"analysis_mode": True}

        # Include all available data from conversation
        if state:
            context["available_data"] = {
                ref.id: {
                    "indicator": ref.indicator,
                    "provider": ref.provider,
                    "country": ref.country,
                    "has_data": ref.data is not None,
                }
                for ref in state.get_all_data_references()
            }

        return context

    def _extract_data_context(self, query: str) -> Dict[str, Any]:
        """Extract context for standard data fetch query"""
        context = {"data_fetch_mode": True}

        # Extract any explicit provider mention
        query_lower = query.lower()
        providers = {
            "from eurostat": "EUROSTAT",
            "from fred": "FRED",
            "from worldbank": "WORLDBANK",
            "from world bank": "WORLDBANK",
            "from imf": "IMF",
            "from oecd": "OECD",
            "from bis": "BIS",
            "from statscan": "STATSCAN",
            "from statistics canada": "STATSCAN",
            "from comtrade": "COMTRADE",
            "eurostat data": "EUROSTAT",
            "fred data": "FRED",
        }

        for pattern, provider in providers.items():
            if pattern in query_lower:
                context["explicit_provider"] = provider
                break

        return context


# Singleton instance
router_agent = RouterAgent()
