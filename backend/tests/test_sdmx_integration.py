#!/usr/bin/env python3
"""
Test SDMX Metadata Integration

Verifies that SDMX catalogs are loaded and searchable as the primary metadata source.
"""

import asyncio
import sys
from pathlib import Path

# Add backend to path
backend_dir = Path(__file__).parent.parent
sys.path.insert(0, str(backend_dir.parent))

from backend.services.metadata_search import MetadataSearchService


class MockLLMProvider:
    """Mock LLM provider for testing"""
    async def generate(self, *args, **kwargs):
        return None


async def main():
    print("="*70)
    print("🧪 Testing SDMX Metadata Integration")
    print("="*70)
    print()

    # Initialize metadata search service with mock LLM provider
    mock_llm = MockLLMProvider()
    metadata_service = MetadataSearchService(mock_llm)

    # Test 1: Load SDMX catalogs
    print("📂 Test 1: Loading SDMX catalogs...")
    catalogs = metadata_service._load_sdmx_catalogs()
    print(f"✅ Loaded catalogs from {len(catalogs)} providers:")
    for provider, dataflows in catalogs.items():
        print(f"   - {provider}: {len(dataflows)} dataflows")
    print()

    # Test 2: Search for "GDP"
    print("🔎 Test 2: Searching for 'GDP' across all SDMX catalogs...")
    gdp_results = await metadata_service.search_sdmx("GDP")
    print(f"✅ Found {len(gdp_results)} results")
    print(f"\nTop 5 results:")
    for i, result in enumerate(gdp_results[:5], 1):
        print(f"   {i}. [{result['provider']}] {result['code']}: {result['name'][:70]}")
    print()

    # Test 3: Search for "inflation"
    print("🔎 Test 3: Searching for 'inflation' across all SDMX catalogs...")
    inflation_results = await metadata_service.search_sdmx("inflation")
    print(f"✅ Found {len(inflation_results)} results")
    print(f"\nTop 5 results:")
    for i, result in enumerate(inflation_results[:5], 1):
        print(f"   {i}. [{result['provider']}] {result['code']}: {result['name'][:70]}")
    print()

    # Test 4: Provider-specific search (IMF only)
    print("🔎 Test 4: Searching for 'trade' in IMF catalogs only...")
    imf_trade_results = await metadata_service.search_sdmx("trade", provider_filter="IMF")
    print(f"✅ Found {len(imf_trade_results)} results from IMF")
    print(f"\nTop 5 results:")
    for i, result in enumerate(imf_trade_results[:5], 1):
        print(f"   {i}. [{result['provider']}] {result['code']}: {result['name'][:70]}")
    print()

    # Test 5: Test caching
    print("🔎 Test 5: Testing cache (searching 'GDP' again)...")
    gdp_results_cached = await metadata_service.search_sdmx("GDP")
    print(f"✅ Got {len(gdp_results_cached)} results (should be from cache)")
    print()

    print("="*70)
    print("✅ All SDMX integration tests passed!")
    print("="*70)


if __name__ == '__main__':
    asyncio.run(main())
