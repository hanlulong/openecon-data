"""
Comparison Agent for Multi-Series Handling

This agent handles queries that involve:
- Multiple variants (consolidated vs unconsolidated)
- Side-by-side comparisons
- Plotting multiple series on the same graph
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..memory.conversation_state import ConversationState, DataReference
from ..models import NormalizedData

logger = logging.getLogger(__name__)


@dataclass
class ComparisonResponse:
    """Response from comparison agent"""
    success: bool
    datasets: List[NormalizedData] = field(default_factory=list)
    data_references: List[DataReference] = field(default_factory=list)
    chart_type: str = "line"
    merge_series: bool = True
    legend_labels: List[str] = field(default_factory=list)
    message: Optional[str] = None
    error: Optional[str] = None


class ComparisonAgent:
    """
    Handles comparison and multi-series requests.

    Key responsibilities:
    1. Extract all variants/series to compare
    2. Fetch each variant from the appropriate provider
    3. Combine into a single visualization context
    """

    # Eurostat dataset variant mappings
    # Maps base dataset codes to their consolidated/unconsolidated equivalents
    EUROSTAT_VARIANT_MAPPINGS = {
        # Non-financial corporations financial accounts (annual)
        "nasa_10_nf_tr": {
            "consolidated": "nasa_10_nf_tr",
            "unconsolidated": "nasq_10_nf_tr",
        },
        # Financial corporations
        "nasa_10_f_tr": {
            "consolidated": "nasa_10_f_tr",
            "unconsolidated": "nasq_10_f_tr",
        },
        # Government accounts
        "nasa_10_gov_tr": {
            "consolidated": "nasa_10_gov_tr",
            "unconsolidated": "gov_10q_ggfa",
        },
        # Household accounts
        "nasa_10_hh_tr": {
            "consolidated": "nasa_10_hh_tr",
            "unconsolidated": "nasq_10_hh_tr",
        },
        # Annual sector accounts
        "nasa_10_f_bs": {
            "consolidated": "nasa_10_f_bs",
            "unconsolidated": "nasq_10_f_bs",
        },
        # National accounts main aggregates
        "nama_10_gdp": {
            "consolidated": "nama_10_gdp",
            "unconsolidated": "namq_10_gdp",  # Quarterly
        },
    }

    # Variant dimension mappings for different providers (API-level filtering)
    EUROSTAT_VARIANT_DIMENSIONS = {
        "consolidated": {"conso": "S"},    # S = Consolidated
        "unconsolidated": {"conso": "N"},  # N = Non-consolidated / Unconsolidated
    }

    # Generic pattern for dataset code transformation
    EUROSTAT_VARIANT_PATTERNS = {
        # Annual → Quarterly pattern (common Eurostat naming)
        "consolidated": lambda code: code,  # Keep original
        "unconsolidated": lambda code: code.replace("nasa_", "nasq_") if code.startswith("nasa_") else code,
    }

    def __init__(self, data_fetcher=None):
        """
        Initialize with optional data fetcher.

        Args:
            data_fetcher: Function to fetch data from providers
        """
        self.data_fetcher = data_fetcher

    async def process(
        self,
        query: str,
        context: Dict[str, Any],
        state: Optional[ConversationState] = None
    ) -> ComparisonResponse:
        """
        Process a comparison query.

        Args:
            query: Original user query
            context: Context from router (variants, base_dataset, etc.)
            state: Current conversation state

        Returns:
            ComparisonResponse with multiple datasets for comparison
        """
        variants = context.get("variants", [])
        base_dataset = context.get("base_dataset")
        merge_series = context.get("merge_series", True)

        logger.info(f"Comparison query: variants={variants}, base_dataset={base_dataset}")

        if not variants:
            # Try to extract variants from query
            variants = self._extract_variants_from_query(query)

        if not variants:
            return ComparisonResponse(
                success=False,
                error="Could not determine what to compare. Please specify the variants "
                      "(e.g., 'consolidated and unconsolidated').",
            )

        if not base_dataset and not self._can_extract_indicator(query):
            return ComparisonResponse(
                success=False,
                error="No previous data context found. Please first query the base data, "
                      "then ask to compare variants.",
            )

        # Fetch each variant
        datasets = []
        data_refs = []
        legend_labels = []

        for variant in variants:
            try:
                if base_dataset:
                    # Fetch variant of existing dataset
                    data, ref = await self._fetch_variant(base_dataset, variant)
                else:
                    # Extract indicator from query and fetch
                    data, ref = await self._fetch_from_query(query, variant)

                if data:
                    datasets.append(data)
                    data_refs.append(ref)
                    label = f"{ref.indicator} ({variant})"
                    legend_labels.append(label)

            except Exception as e:
                logger.warning(f"Failed to fetch variant {variant}: {e}")
                continue

        if not datasets:
            return ComparisonResponse(
                success=False,
                error=f"Could not fetch any of the requested variants: {variants}",
            )

        # Determine chart type
        chart_type = self._determine_chart_type(datasets)

        return ComparisonResponse(
            success=True,
            datasets=datasets,
            data_references=data_refs,
            chart_type=chart_type,
            merge_series=merge_series,
            legend_labels=legend_labels,
            message=f"Comparison of {len(datasets)} series: {', '.join(legend_labels)}",
        )

    def _extract_variants_from_query(self, query: str) -> List[str]:
        """Extract variant names from query text"""
        variants = []
        query_lower = query.lower()

        if "consolidated" in query_lower:
            variants.append("consolidated")
        if "unconsolidated" in query_lower:
            variants.append("unconsolidated")
        if "seasonally adjusted" in query_lower or " sa " in query_lower:
            variants.append("seasonally_adjusted")
        if "not seasonally adjusted" in query_lower or " nsa " in query_lower:
            variants.append("not_seasonally_adjusted")
        if "nominal" in query_lower:
            variants.append("nominal")
        if "real" in query_lower:
            variants.append("real")

        return variants

    def _extract_asset_comparison(self, query: str) -> List[str]:
        """Extract assets/indicators to compare from query text"""
        import re
        query_lower = query.lower()

        # Common asset comparison patterns
        # "Compare X and Y" or "Compare X with Y" or "X vs Y"
        compare_patterns = [
            r"compare\s+([a-z]+)\s+(?:and|with|to|vs\.?)\s+([a-z]+)",
            r"([a-z]+)\s+(?:vs\.?|versus)\s+([a-z]+)",
            r"([a-z]+)\s+and\s+([a-z]+)\s+(?:prices?|data|comparison)",
        ]

        for pattern in compare_patterns:
            match = re.search(pattern, query_lower)
            if match:
                return [match.group(1).strip(), match.group(2).strip()]

        return []

    def is_asset_comparison(self, query: str) -> bool:
        """Check if query is comparing different assets (not variants)"""
        assets = self._extract_asset_comparison(query)
        return len(assets) >= 2

    def _can_extract_indicator(self, query: str) -> bool:
        """Check if we can extract an indicator from the query"""
        # Look for common indicator patterns
        indicator_patterns = [
            "financial liabilities",
            "financial assets",
            "total liabilities",
            "total assets",
            "gdp",
            "unemployment",
            "inflation",
            "debt",
        ]
        query_lower = query.lower()
        return any(p in query_lower for p in indicator_patterns)

    async def _fetch_variant(
        self,
        base_dataset: DataReference,
        variant: str
    ) -> tuple[Optional[NormalizedData], Optional[DataReference]]:
        """
        Fetch a variant of an existing dataset.

        Args:
            base_dataset: The base dataset reference
            variant: Variant name (e.g., "unconsolidated")

        Returns:
            (NormalizedData, DataReference) tuple
        """
        if not self.data_fetcher:
            logger.warning("No data fetcher available")
            return None, None

        # Get variant-specific parameters including transformed dataset code
        variant_params = self._get_variant_params(
            base_dataset.provider,
            variant,
            base_dataset.dataset_code  # Pass base dataset code for transformation
        )

        # Determine the variant dataset code
        variant_dataset_code = variant_params.get("dataset_code", base_dataset.dataset_code)

        logger.info(f"Fetching {variant} variant: {base_dataset.dataset_code} -> {variant_dataset_code}")

        # Build fetch parameters
        params = {
            "provider": base_dataset.provider,
            "indicator": base_dataset.indicator,
            "country": base_dataset.country,
            "startDate": base_dataset.time_range[0] if base_dataset.time_range else None,
            "endDate": base_dataset.time_range[1] if base_dataset.time_range else None,
            "dataset_code": variant_dataset_code,
            "series_id": variant_dataset_code,
        }

        # Add dimension filters if available
        if "dimensions" in variant_params:
            params["dimensions"] = variant_params["dimensions"]

        try:
            # Use data fetcher to get the variant
            data = await self.data_fetcher(params)

            if data:
                # Create data reference for the variant
                ref = DataReference(
                    id=str(uuid.uuid4()),
                    query=f"{base_dataset.indicator} ({variant})",
                    provider=base_dataset.provider,
                    dataset_code=variant_dataset_code,  # Use the transformed code
                    indicator=f"{base_dataset.indicator} ({variant})",
                    country=base_dataset.country,
                    time_range=base_dataset.time_range,
                    unit=base_dataset.unit,
                    metadata={
                        **base_dataset.metadata,
                        "variant": variant,
                        "original_dataset": base_dataset.dataset_code,
                    },
                    variants=[variant],
                )
                return data, ref

        except Exception as e:
            logger.warning(f"Failed to fetch variant {variant}: {e}")

        return None, None

    async def _fetch_from_query(
        self,
        query: str,
        variant: str
    ) -> tuple[Optional[NormalizedData], Optional[DataReference]]:
        """
        Fetch data based on query text for a specific variant.

        This is used when there's no base dataset in context.
        """
        if not self.data_fetcher:
            logger.warning("No data fetcher available")
            return None, None

        # Extract indicator and provider from query
        # This is a simplified extraction - the real implementation would use the LLM
        indicator = self._extract_indicator(query)
        provider = self._extract_provider(query)
        country = self._extract_country(query)

        if not indicator:
            return None, None

        variant_params = self._get_variant_params(provider, variant) if provider else {}

        params = {
            "provider": provider or "EUROSTAT",  # Default to Eurostat for EU data
            "indicator": indicator,
            "country": country or "EU",
            **variant_params,
        }

        try:
            data = await self.data_fetcher(params)

            if data:
                ref = DataReference(
                    id=str(uuid.uuid4()),
                    query=query,
                    provider=params["provider"],
                    indicator=f"{indicator} ({variant})",
                    country=params.get("country"),
                    variants=[variant],
                )
                return data, ref

        except Exception as e:
            logger.warning(f"Failed to fetch from query: {e}")

        return None, None

    def _get_variant_params(self, provider: str, variant: str, base_dataset_code: Optional[str] = None) -> Dict[str, Any]:
        """
        Get provider-specific parameters for a variant.

        Args:
            provider: Provider name (e.g., "EUROSTAT")
            variant: Variant name (e.g., "unconsolidated")
            base_dataset_code: Original dataset code to transform

        Returns:
            Dict with variant-specific parameters including transformed dataset code
        """
        params = {}

        if provider == "EUROSTAT":
            # First, try exact mapping
            if base_dataset_code and base_dataset_code in self.EUROSTAT_VARIANT_MAPPINGS:
                variant_code = self.EUROSTAT_VARIANT_MAPPINGS[base_dataset_code].get(variant)
                if variant_code:
                    params["dataset_code"] = variant_code
                    params["series_id"] = variant_code

            # If no exact mapping, try pattern transformation
            elif base_dataset_code and variant in self.EUROSTAT_VARIANT_PATTERNS:
                transform_fn = self.EUROSTAT_VARIANT_PATTERNS[variant]
                transformed_code = transform_fn(base_dataset_code)
                if transformed_code != base_dataset_code:
                    params["dataset_code"] = transformed_code
                    params["series_id"] = transformed_code

            # Also add dimension filter for API-level variant selection
            if variant in self.EUROSTAT_VARIANT_DIMENSIONS:
                params["dimensions"] = self.EUROSTAT_VARIANT_DIMENSIONS[variant]

        # Add other provider-specific variant mappings as needed
        # Example: FRED seasonally adjusted vs not adjusted
        elif provider == "FRED":
            if variant == "seasonally_adjusted":
                params["frequency_adjustment"] = "sa"
            elif variant == "not_seasonally_adjusted":
                params["frequency_adjustment"] = "nsa"

        return params

    def _transform_dataset_code_for_variant(self, provider: str, base_code: str, variant: str) -> str:
        """
        Transform a dataset code to get a specific variant.

        Args:
            provider: Provider name
            base_code: Original dataset code
            variant: Target variant name

        Returns:
            Transformed dataset code
        """
        if provider != "EUROSTAT":
            return base_code

        # Try exact mapping first
        if base_code in self.EUROSTAT_VARIANT_MAPPINGS:
            mapping = self.EUROSTAT_VARIANT_MAPPINGS[base_code]
            if variant in mapping:
                return mapping[variant]

        # Try pattern-based transformation
        if variant in self.EUROSTAT_VARIANT_PATTERNS:
            transform_fn = self.EUROSTAT_VARIANT_PATTERNS[variant]
            return transform_fn(base_code)

        return base_code

    def _extract_indicator(self, query: str) -> Optional[str]:
        """Extract indicator name from query"""
        query_lower = query.lower()

        # Common indicator patterns
        indicators = {
            "financial liabilities": "total financial liabilities",
            "financial assets": "total financial assets",
            "total liabilities": "total liabilities",
            "total assets": "total assets",
            "nonfinancial corporation": "nonfinancial corporations",
        }

        for pattern, indicator in indicators.items():
            if pattern in query_lower:
                return indicator

        return None

    def _extract_provider(self, query: str) -> Optional[str]:
        """Extract provider from query"""
        query_lower = query.lower()
        providers = {
            "eurostat": "EUROSTAT",
            "fred": "FRED",
            "worldbank": "WORLDBANK",
            "imf": "IMF",
            "oecd": "OECD",
            "bis": "BIS",
        }

        for pattern, provider in providers.items():
            if pattern in query_lower:
                return provider

        # Default based on content
        if "eu " in query_lower or "european" in query_lower:
            return "EUROSTAT"

        return None

    def _extract_country(self, query: str) -> Optional[str]:
        """Extract country from query"""
        query_lower = query.lower()

        if "eu " in query_lower or "european union" in query_lower:
            return "EU"
        if "eurozone" in query_lower or "euro area" in query_lower:
            return "EA"
        # Add more country patterns as needed

        return None

    def _determine_chart_type(self, datasets: List[NormalizedData]) -> str:
        """Determine appropriate chart type for comparison"""
        if not datasets:
            return "line"

        # Check data frequency
        if datasets[0].metadata.frequency in ["quarterly", "monthly"]:
            return "line"
        elif datasets[0].metadata.frequency == "annual":
            # For annual data, bar might be better for few points
            if len(datasets[0].data) <= 10:
                return "bar"
            return "line"

        return "line"


# Singleton instance
comparison_agent = ComparisonAgent()
