"""
Research Agent for Data Availability Questions

This agent answers questions like:
- "Does Eurostat have EU nonfinancial corporation total assets?"
- "What data is available for housing prices?"
- "Which provider has trade balance data?"

It does NOT fetch data - it provides information about data availability.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..services.metadata_search import MetadataSearchService

logger = logging.getLogger(__name__)


@dataclass
class DatasetInfo:
    """Information about an available dataset"""
    code: str
    name: str
    description: Optional[str] = None
    provider: str = ""
    frequency: Optional[str] = None
    unit: Optional[str] = None
    coverage: Optional[str] = None  # e.g., "EU countries, 2000-2023"


@dataclass
class ResearchResponse:
    """Response from research agent"""
    available: bool
    message: str
    datasets: List[DatasetInfo] = field(default_factory=list)
    requires_calculation: bool = False
    calculation_components: List[str] = field(default_factory=list)
    alternative_sources: List[str] = field(default_factory=list)


class ResearchAgent:
    """
    Answers research questions about data availability.

    Key responsibilities:
    1. Search provider metadata for matching datasets
    2. Determine if data is directly available or needs calculation
    3. Suggest alternative sources if not available
    4. Provide clear, informative responses
    """

    # Known indicators that require calculation (not directly available)
    CALCULATED_INDICATORS = {
        "total assets": {
            "components": ["financial assets", "real assets (various categories)"],
            "note": "Total assets = Financial assets + Real assets",
        },
        "net worth": {
            "components": ["total assets", "total liabilities"],
            "note": "Net worth = Total assets - Total liabilities",
        },
        "real gdp growth": {
            "components": ["real gdp"],
            "note": "Calculate year-over-year percentage change",
        },
    }

    # Provider capabilities summary
    PROVIDER_CAPABILITIES = {
        "EUROSTAT": {
            "specialties": [
                "EU member state statistics",
                "National accounts",
                "Labor market",
                "Prices and inflation",
                "Government finance",
                "Sectoral accounts (financial/non-financial corporations)",
            ],
            "sectors": ["non-financial corporations", "financial corporations", "households", "government"],
            "note": "Best source for EU economic data. Includes consolidated and unconsolidated accounts.",
        },
        "FRED": {
            "specialties": [
                "US economic data",
                "Federal Reserve data",
                "Interest rates",
                "Employment",
                "Housing",
            ],
            "note": "Best source for US economic indicators.",
        },
        "WORLDBANK": {
            "specialties": [
                "Global development indicators",
                "Cross-country comparisons",
                "Developing countries",
            ],
            "note": "Best for global comparisons and development metrics.",
        },
        "IMF": {
            "specialties": [
                "International financial statistics",
                "Government debt and fiscal data",
                "Balance of payments",
                "Exchange rates",
            ],
            "note": "Best for fiscal/debt data and international finance.",
        },
        "BIS": {
            "specialties": [
                "Credit indicators",
                "Property prices",
                "Debt service ratios",
                "Financial stability",
            ],
            "note": "Best for financial stability and credit data.",
        },
        "OECD": {
            "specialties": [
                "OECD member country statistics",
                "Economic outlook",
                "Education and health",
            ],
            "note": "Best for OECD member comparisons.",
        },
    }

    def __init__(self, metadata_search: Optional[MetadataSearchService] = None):
        self.metadata_search = metadata_search

    async def process(
        self,
        query: str,
        context: Dict[str, Any]
    ) -> ResearchResponse:
        """
        Answer a research question about data availability.

        Args:
            query: Original user query
            context: Context from router (provider, indicator)

        Returns:
            ResearchResponse with availability information
        """
        provider = context.get("provider", "").upper()
        indicator = context.get("indicator", "").lower().strip()

        logger.info(f"Research query: provider={provider}, indicator={indicator}")

        # If no provider specified, search across all
        if not provider:
            return await self._search_all_providers(indicator)

        # Check if this requires calculation
        calc_info = self._check_requires_calculation(indicator)
        if calc_info:
            return ResearchResponse(
                available=False,
                requires_calculation=True,
                calculation_components=calc_info["components"],
                message=self._format_calculation_message(provider, indicator, calc_info),
            )

        # Search the specified provider
        if self.metadata_search:
            return await self._search_provider(provider, indicator)
        else:
            return self._provide_capability_info(provider, indicator)

    async def _search_provider(
        self,
        provider: str,
        indicator: str
    ) -> ResearchResponse:
        """Search a specific provider's metadata"""
        try:
            # Use metadata search service
            results = await self.metadata_search.search(
                provider=provider,
                query=indicator,
                limit=10
            )

            if results and len(results) > 0:
                # Found matching datasets
                datasets = [
                    DatasetInfo(
                        code=r.get("code", ""),
                        name=r.get("name", ""),
                        description=r.get("description"),
                        provider=provider,
                    )
                    for r in results[:5]
                ]

                return ResearchResponse(
                    available=True,
                    datasets=datasets,
                    message=self._format_available_message(provider, indicator, datasets),
                )
            else:
                # No direct match
                alternatives = self._suggest_alternatives(indicator)
                return ResearchResponse(
                    available=False,
                    message=self._format_not_available_message(provider, indicator),
                    alternative_sources=alternatives,
                )

        except Exception as e:
            logger.warning(f"Metadata search failed: {e}")
            return self._provide_capability_info(provider, indicator)

    async def _search_all_providers(self, indicator: str) -> ResearchResponse:
        """Search across all providers for an indicator"""
        all_providers = ["EUROSTAT", "FRED", "WORLDBANK", "IMF", "BIS", "OECD"]

        found_in = []
        all_datasets = []

        for provider in all_providers:
            if self.metadata_search:
                try:
                    results = await self.metadata_search.search(
                        provider=provider,
                        query=indicator,
                        limit=3
                    )
                    if results:
                        found_in.append(provider)
                        for r in results[:2]:
                            all_datasets.append(DatasetInfo(
                                code=r.get("code", ""),
                                name=r.get("name", ""),
                                provider=provider,
                            ))
                except Exception as e:
                    logger.debug(f"Search failed for {provider}: {e}")
                    continue

        if found_in:
            return ResearchResponse(
                available=True,
                datasets=all_datasets,
                message=f"'{indicator}' is available from: {', '.join(found_in)}.\n\n"
                        f"Datasets found:\n" +
                        "\n".join([f"- [{d.provider}] {d.name} ({d.code})" for d in all_datasets[:5]]),
            )
        else:
            calc_info = self._check_requires_calculation(indicator)
            if calc_info:
                return ResearchResponse(
                    available=False,
                    requires_calculation=True,
                    calculation_components=calc_info["components"],
                    message=f"'{indicator}' is not directly available as a single series. "
                            f"It typically needs to be calculated from:\n"
                            f"- " + "\n- ".join(calc_info["components"]) + "\n\n"
                            f"{calc_info.get('note', '')}",
                )
            else:
                return ResearchResponse(
                    available=False,
                    message=f"Could not find '{indicator}' in any of the available data providers. "
                            f"Try being more specific or using different keywords.",
                    alternative_sources=self._suggest_alternatives(indicator),
                )

    def _check_requires_calculation(self, indicator: str) -> Optional[Dict[str, Any]]:
        """Check if indicator requires calculation from components"""
        indicator_lower = indicator.lower()

        for calc_ind, info in self.CALCULATED_INDICATORS.items():
            if calc_ind in indicator_lower:
                return info

        return None

    def _provide_capability_info(
        self,
        provider: str,
        indicator: str
    ) -> ResearchResponse:
        """Provide capability information when metadata search unavailable"""
        if provider in self.PROVIDER_CAPABILITIES:
            caps = self.PROVIDER_CAPABILITIES[provider]
            specialties = caps.get("specialties", [])
            note = caps.get("note", "")

            # Check if indicator might match capabilities
            indicator_lower = indicator.lower()
            likely_available = any(
                any(keyword in indicator_lower for keyword in specialty.lower().split())
                for specialty in specialties
            )

            if likely_available:
                return ResearchResponse(
                    available=True,  # Likely but not confirmed
                    message=f"Based on {provider}'s capabilities, '{indicator}' is likely available.\n\n"
                            f"{provider} specializes in:\n"
                            f"- " + "\n- ".join(specialties[:5]) + "\n\n"
                            f"{note}\n\n"
                            f"Try querying directly to get the specific data.",
                )
            else:
                return ResearchResponse(
                    available=False,
                    message=f"'{indicator}' may not be directly available from {provider}.\n\n"
                            f"{provider} specializes in:\n"
                            f"- " + "\n- ".join(specialties[:5]) + "\n\n"
                            f"Consider trying: {', '.join(self._suggest_alternatives(indicator))}",
                    alternative_sources=self._suggest_alternatives(indicator),
                )
        else:
            return ResearchResponse(
                available=False,
                message=f"Unknown provider: {provider}. Available providers: "
                        f"{', '.join(self.PROVIDER_CAPABILITIES.keys())}",
            )

    def _suggest_alternatives(self, indicator: str) -> List[str]:
        """Suggest alternative providers based on indicator keywords"""
        indicator_lower = indicator.lower()
        suggestions = []

        if any(k in indicator_lower for k in ["trade", "export", "import"]):
            suggestions.append("UN Comtrade")
        if any(k in indicator_lower for k in ["gdp", "growth", "economic"]):
            suggestions.extend(["World Bank", "IMF", "OECD"])
        if any(k in indicator_lower for k in ["credit", "debt", "property", "housing price"]):
            suggestions.append("BIS")
        if any(k in indicator_lower for k in ["eu", "europe", "euro"]):
            suggestions.append("Eurostat")
        if any(k in indicator_lower for k in ["us ", "usa", "america", "federal"]):
            suggestions.append("FRED")

        return list(set(suggestions))[:3]

    def _format_available_message(
        self,
        provider: str,
        indicator: str,
        datasets: List[DatasetInfo]
    ) -> str:
        """Format message when data is available"""
        msg = f"Yes, {provider} has data for '{indicator}'.\n\n"
        msg += "Available datasets:\n"
        for d in datasets[:5]:
            msg += f"- {d.name}"
            if d.code:
                msg += f" ({d.code})"
            msg += "\n"

        msg += f"\nYou can query this data by asking: 'Show me {indicator} from {provider}'"
        return msg

    def _format_not_available_message(
        self,
        provider: str,
        indicator: str
    ) -> str:
        """Format message when data is not available"""
        alternatives = self._suggest_alternatives(indicator)
        msg = f"'{indicator}' does not appear to be directly available from {provider}.\n\n"

        if alternatives:
            msg += f"You might find this data in: {', '.join(alternatives)}"

        return msg

    def _format_calculation_message(
        self,
        provider: str,
        indicator: str,
        calc_info: Dict[str, Any]
    ) -> str:
        """Format message when indicator requires calculation"""
        msg = f"'{indicator}' is not available as a single series in {provider}.\n\n"
        msg += "This indicator typically needs to be calculated from:\n"
        for comp in calc_info["components"]:
            msg += f"- {comp}\n"

        if calc_info.get("note"):
            msg += f"\n{calc_info['note']}\n"

        msg += "\nWould you like me to calculate this for you? "
        msg += "(This will use Pro Mode to fetch and combine the component data.)"

        return msg


# Singleton instance
research_agent = ResearchAgent()
