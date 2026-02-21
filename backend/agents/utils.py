"""Shared utilities for agent modules.

This module provides common functions used across multiple agents:
- DataReference creation from various sources
- Provider name mappings
- Common helper functions
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory.conversation_state import DataReference
    from ..models import NormalizedData, ParsedIntent, QueryResponse


# Standard provider name mappings used across agents
PROVIDER_NAMES = {
    # Standard names
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
    "exchangerate": "EXCHANGERATE",
    "exchange rate": "EXCHANGERATE",
    "coingecko": "COINGECKO",
}

# Provider patterns for query matching
PROVIDER_QUERY_PATTERNS = {
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
    "world bank data": "WORLDBANK",
    "imf data": "IMF",
}


def normalize_provider_name(provider: str) -> str:
    """
    Normalize a provider name to its standard uppercase form.

    Args:
        provider: Provider name in any case/format

    Returns:
        Standardized provider name (e.g., "FRED", "WORLDBANK")
    """
    if not provider:
        return "UNKNOWN"
    lower = provider.lower().strip()
    return PROVIDER_NAMES.get(lower, provider.upper())


def extract_provider_from_query(query: str) -> Optional[str]:
    """
    Extract provider name from query text if explicitly mentioned.

    Args:
        query: User query text

    Returns:
        Provider name if found, None otherwise
    """
    query_lower = query.lower()
    for pattern, provider in PROVIDER_QUERY_PATTERNS.items():
        if pattern in query_lower:
            return provider
    return None


def create_data_reference(
    query: str,
    data: Optional["NormalizedData"] = None,
    data_list: Optional[List["NormalizedData"]] = None,
    intent: Optional["ParsedIntent"] = None,
    chart_type: Optional[str] = None,
    include_data_points: bool = False,
) -> "DataReference":
    """
    Create a DataReference from various data sources.

    This is the standardized way to create DataReference objects across all agents.
    It handles single NormalizedData, list of NormalizedData, or empty data cases.

    Args:
        query: Original query that fetched the data
        data: Single NormalizedData object (takes precedence over data_list)
        data_list: List of NormalizedData objects (uses first item)
        intent: ParsedIntent for chart type information
        chart_type: Explicit chart type (overrides intent)
        include_data_points: Whether to include actual data points in reference

    Returns:
        DataReference with populated fields

    Example:
        >>> ref = create_data_reference(
        ...     query="US GDP growth",
        ...     data=normalized_data,
        ...     intent=parsed_intent
        ... )
    """
    from ..memory.conversation_state import DataReference

    # Determine which data to use
    source_data = data
    if source_data is None and data_list:
        source_data = data_list[0] if data_list else None

    # Handle empty data case
    if source_data is None:
        return DataReference(
            id=str(uuid.uuid4()),
            query=query,
            provider="UNKNOWN",
        )

    metadata = source_data.metadata

    # Determine chart type
    resolved_chart_type = chart_type
    if resolved_chart_type is None and intent:
        resolved_chart_type = intent.recommendedChartType
    if resolved_chart_type is None:
        resolved_chart_type = "line"

    # Build metadata dict
    meta_dict: Dict[str, Any] = {}
    if hasattr(metadata, "model_dump"):
        meta_dict = metadata.model_dump()
    elif hasattr(metadata, "dict"):
        meta_dict = metadata.dict()

    # Create the reference
    ref = DataReference(
        id=str(uuid.uuid4()),
        query=query,
        provider=normalize_provider_name(metadata.source) if metadata.source else "UNKNOWN",
        dataset_code=metadata.seriesId,
        indicator=metadata.indicator or "",
        country=metadata.country,
        time_range=(metadata.startDate, metadata.endDate) if metadata.startDate else None,
        unit=metadata.unit or "",
        frequency=metadata.frequency or "",
        metadata=meta_dict,
        chart_type=resolved_chart_type,
    )

    # Optionally include data points
    if include_data_points and source_data.data:
        ref.data = [{"date": d.date, "value": d.value} for d in source_data.data]

    return ref


def create_data_reference_from_response(
    query: str,
    response: "QueryResponse",
    include_data_points: bool = False,
) -> Optional["DataReference"]:
    """
    Create a DataReference from a QueryResponse.

    Args:
        query: Original query
        response: QueryResponse containing data
        include_data_points: Whether to include data points

    Returns:
        DataReference or None if response has no data
    """
    if not response.data:
        return None

    return create_data_reference(
        query=query,
        data_list=response.data,
        intent=response.intent,
        include_data_points=include_data_points,
    )


def create_variant_reference(
    base_reference: "DataReference",
    variant: str,
    variant_dataset_code: Optional[str] = None,
) -> "DataReference":
    """
    Create a DataReference for a comparison variant based on an existing reference.

    Args:
        base_reference: The original DataReference to base on
        variant: Variant name (e.g., country name, time period)
        variant_dataset_code: Optional different dataset code for the variant

    Returns:
        New DataReference for the variant
    """
    from ..memory.conversation_state import DataReference

    return DataReference(
        id=str(uuid.uuid4()),
        query=f"{base_reference.indicator} ({variant})",
        provider=base_reference.provider,
        dataset_code=variant_dataset_code or base_reference.dataset_code,
        indicator=f"{base_reference.indicator} ({variant})",
        country=base_reference.country,
        time_range=base_reference.time_range,
        unit=base_reference.unit,
        frequency=base_reference.frequency,
        metadata={
            **base_reference.metadata,
            "variant": variant,
            "original_dataset": base_reference.dataset_code,
        },
        variants=[variant],
    )
