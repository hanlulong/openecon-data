#!/usr/bin/env python3
"""
FRED Metadata Extractor

Extracts top economic series from FRED API and stores them
in a structured JSON format for fast local lookups.

Strategy: FRED has 820,000+ series. We use a two-tier approach:
1. Offline extraction (this script):
   - Extract top 50,000 most popular series
   - Augment with all key economic indicators via search
   - Store in local JSON for instant lookups

2. On-demand lookup (runtime):
   - If series not in cache, query FRED metadata API
   - Cache the result for future queries
   - Ensures comprehensive coverage of all 820K+ series

This provides:
- FAST: Most queries hit local cache
- COMPREHENSIVE: Rare series looked up on-demand
- SCALABLE: Don't need to store all 820K series locally

API Documentation: https://fred.stlouisfed.org/docs/api/fred/

Usage:
    python extract_fred_metadata.py [--output FILE] [--limit N]
"""

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set, Optional
import httpx

# Configuration
FRED_API_BASE = "https://api.stlouisfed.org/fred"
OUTPUT_DIR = Path(__file__).parent.parent.parent / "backend" / "data" / "metadata"
DEFAULT_OUTPUT_FILE = OUTPUT_DIR / "fred.json"
REQUEST_TIMEOUT = 30.0
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2.0

# Extract top N most popular series (FRED has 820,000+ total)
# Increased to 50,000 for more comprehensive coverage
MAX_SERIES = 50000

# Priority categories to ensure coverage of key economic indicators
PRIORITY_CATEGORIES = {
    "National Accounts",
    "GDP",
    "Income & Expenditure",
    "Production & Business Activity",
    "Prices",
    "Inflation",
    "Employment & Unemployment",
    "Interest Rates",
    "Money & Banking",
    "International Trade",
    "Housing",
    "Consumer Spending",
}

# Key search terms to ensure we get important series
KEY_SEARCH_TERMS = [
    "GDP", "unemployment", "inflation", "CPI", "PPI",
    "employment", "interest rate", "housing", "retail sales",
    "industrial production", "consumer price", "producer price",
    "nonfarm payroll", "federal funds", "mortgage", "home prices",
    "consumer spending", "personal income", "capacity utilization",
    "trade balance", "exports", "imports", "manufacturing",
    "construction spending", "building permits", "housing starts",
    "consumer sentiment", "savings rate", "productivity",
    "wages", "M2", "money supply", "corporate profits",
]


def get_api_key() -> str:
    """Get FRED API key from environment."""
    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        raise ValueError(
            "FRED_API_KEY environment variable not set. "
            "Get your API key from https://fred.stlouisfed.org/docs/api/api_key.html"
        )
    return api_key


def fetch_series_by_popularity(api_key: str, limit: int = MAX_SERIES) -> List[Dict]:
    """
    Fetch most popular FRED series using search with popularity sorting.

    Args:
        api_key: FRED API key
        limit: Maximum number of series to fetch

    Returns:
        List of series dictionaries
    """
    all_series = []
    offset = 0
    per_page = 1000  # Max allowed by FRED API

    print(f"📊 Fetching top {limit} FRED series by popularity...")

    while len(all_series) < limit:
        params = {
            "api_key": api_key,
            "file_type": "json",
            "limit": per_page,
            "offset": offset,
            "order_by": "popularity",
            "sort_order": "desc",
        }

        # Use series/search with wildcard to get all series
        url = f"{FRED_API_BASE}/series/search"
        params["search_text"] = "*"

        # Retry logic
        for attempt in range(RETRY_ATTEMPTS):
            try:
                response = httpx.get(url, params=params, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                data = response.json()
                break
            except Exception as e:
                if attempt < RETRY_ATTEMPTS - 1:
                    print(f"   ⚠️ Attempt {attempt + 1} failed: {e}. Retrying...")
                    time.sleep(RETRY_DELAY)
                else:
                    print(f"   ❌ Failed after {RETRY_ATTEMPTS} attempts")
                    raise

        series = data.get("seriess", [])
        if not series:
            break

        all_series.extend(series)
        print(f"   📄 Fetched {len(all_series):,}/{limit:,} series")

        offset += per_page
        time.sleep(0.5)  # Rate limiting

        if len(series) < per_page:
            # No more results
            break

    # Trim to limit
    result = all_series[:limit]
    print(f"   ✅ Fetched {len(result):,} series total")
    return result


def fetch_series_by_category(api_key: str, category_id: int) -> List[Dict]:
    """
    Fetch all series in a specific category.

    Args:
        api_key: FRED API key
        category_id: FRED category ID

    Returns:
        List of series dictionaries
    """
    all_series = []
    offset = 0
    per_page = 1000

    while True:
        params = {
            "api_key": api_key,
            "file_type": "json",
            "category_id": category_id,
            "limit": per_page,
            "offset": offset,
        }

        url = f"{FRED_API_BASE}/category/series"

        try:
            response = httpx.get(url, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            print(f"   ⚠️ Error fetching category {category_id}: {e}")
            break

        series = data.get("seriess", [])
        if not series:
            break

        all_series.extend(series)
        offset += per_page
        time.sleep(0.5)

        if len(series) < per_page:
            break

    return all_series


def fetch_series_by_search(api_key: str, search_term: str) -> List[Dict]:
    """
    Fetch series matching a search term.

    Args:
        api_key: FRED API key
        search_term: Search query

    Returns:
        List of series dictionaries
    """
    params = {
        "api_key": api_key,
        "file_type": "json",
        "search_text": search_term,
        "limit": 100,  # Get top 100 for each search term
        "order_by": "popularity",
        "sort_order": "desc",
    }

    url = f"{FRED_API_BASE}/series/search"

    try:
        response = httpx.get(url, params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        data = response.json()
        return data.get("seriess", [])
    except Exception as e:
        print(f"   ⚠️ Error searching '{search_term}': {e}")
        return []


def generate_aliases(series: Dict) -> List[str]:
    """
    Generate searchable aliases for a series.

    Args:
        series: Series dictionary from API

    Returns:
        List of alias strings
    """
    aliases = set()

    # Add series ID variants
    series_id = series.get("id", "")
    if series_id:
        aliases.add(series_id)
        aliases.add(series_id.upper())
        aliases.add(series_id.lower())

    # Add title variants
    title = series.get("title", "")
    if title:
        aliases.add(title.upper())
        aliases.add(title.upper().replace(" ", "_"))
        aliases.add(title.upper().replace(" ", ""))

        # Common abbreviations
        title_upper = title.upper()
        if "GROSS DOMESTIC PRODUCT" in title_upper:
            aliases.add("GDP")
            if "PER CAPITA" in title_upper:
                aliases.add("GDP_PER_CAPITA")
        if "CONSUMER PRICE INDEX" in title_upper or "CPI" in title_upper:
            aliases.add("CPI")
            aliases.add("INFLATION")
        if "PRODUCER PRICE INDEX" in title_upper or "PPI" in title_upper:
            aliases.add("PPI")
        if "UNEMPLOYMENT" in title_upper:
            aliases.add("UNEMPLOYMENT")
            aliases.add("UNEMPLOYMENT_RATE")
        if "FEDERAL FUNDS" in title_upper:
            aliases.add("FED_FUNDS")
            aliases.add("FEDERAL_FUNDS_RATE")

    return list(aliases)


def build_search_index(series_list: List[Dict]) -> Dict[str, List[str]]:
    """
    Build search index mapping keywords to series IDs.

    Args:
        series_list: List of processed series

    Returns:
        Dictionary mapping lowercase keywords to series IDs
    """
    search_index = {}

    for series in series_list:
        series_id = series["id"]

        # Index all aliases
        for alias in series.get("aliases", []):
            keyword = alias.lower()
            if keyword not in search_index:
                search_index[keyword] = []
            if series_id not in search_index[keyword]:
                search_index[keyword].append(series_id)

        # Index words from title
        title = series.get("title", "")
        for word in title.split():
            if len(word) > 3:  # Skip short words
                keyword = word.lower()
                if keyword not in search_index:
                    search_index[keyword] = []
                if series_id not in search_index[keyword]:
                    search_index[keyword].append(series_id)

    return search_index


def process_series(raw_series_list: List[Dict]) -> List[Dict]:
    """
    Process raw series into structured format.

    Args:
        raw_series_list: Raw series data from API

    Returns:
        List of processed series dictionaries
    """
    processed = []
    seen_ids = set()

    print(f"\n📊 Processing series...")

    for raw in raw_series_list:
        series_id = raw.get("id", "")

        # Skip duplicates
        if series_id in seen_ids:
            continue
        seen_ids.add(series_id)

        # Extract fields
        title = raw.get("title", "")
        notes = raw.get("notes", "")
        frequency = raw.get("frequency", "")
        units = raw.get("units", "")
        seasonal_adjustment = raw.get("seasonal_adjustment", "")

        # Generate aliases
        aliases = generate_aliases(raw)

        # Create searchable text
        searchable_parts = [
            series_id.lower(),
            title.lower(),
            notes.lower() if notes else "",
            " ".join(aliases).lower()
        ]
        searchable_text = " ".join(p for p in searchable_parts if p)

        # Build processed series
        processed_series = {
            "id": series_id,
            "code": series_id,
            "name": title,
            "description": notes or "",
            "source": "FRED",
            "frequency": frequency,
            "unit": units,
            "seasonal_adjustment": seasonal_adjustment,
            "observation_start": raw.get("observation_start", ""),
            "observation_end": raw.get("observation_end", ""),
            "popularity": raw.get("popularity", 0),
            "aliases": aliases,
            "searchable_text": searchable_text
        }

        processed.append(processed_series)

    print(f"   ✅ Processed {len(processed):,} unique series")
    return processed


def organize_by_category(series_list: List[Dict]) -> Dict[str, List[str]]:
    """
    Organize series by frequency (as proxy for category).

    Args:
        series_list: List of processed series

    Returns:
        Dictionary mapping frequencies to series IDs
    """
    categories = {}

    for series in series_list:
        freq = series.get("frequency", "Unknown")
        if freq not in categories:
            categories[freq] = []
        categories[freq].append(series["id"])

    return categories


def save_metadata(series_list: List[Dict], output_file: Path):
    """
    Save processed metadata to JSON file.

    Args:
        series_list: List of processed series
        output_file: Path to output JSON file
    """
    # Build search index
    search_index = build_search_index(series_list)

    # Organize by category
    categories = organize_by_category(series_list)

    # Build metadata structure
    metadata = {
        "provider": "FRED",
        "last_updated": datetime.now().isoformat() + "Z",
        "total_indicators": len(series_list),
        "api_url": FRED_API_BASE,
        "categories": {
            freq: {
                "count": len(series_ids),
                "indicators": series_ids
            }
            for freq, series_ids in categories.items()
        },
        "indicators": series_list,
        "search_index": {k: v for k, v in search_index.items() if len(v) <= 50}
    }

    # Ensure output directory exists
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Save to file
    print(f"\n💾 Saving metadata to {output_file}...")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    # Print statistics
    file_size_mb = output_file.stat().st_size / (1024 * 1024)
    print(f"   ✅ Saved {len(series_list):,} series ({file_size_mb:.2f} MB)")
    print(f"   📁 File: {output_file}")
    print(f"   🏷️  Categories: {len(categories)}")
    print(f"   🔍 Search keywords: {len(search_index):,}")


def main():
    """Main extraction workflow"""
    print("=" * 70)
    print("FRED Metadata Extractor")
    print("=" * 70)
    print()

    try:
        # Get API key
        api_key = get_api_key()

        # Step 1: Fetch top series by popularity
        top_series = fetch_series_by_popularity(api_key, limit=MAX_SERIES)

        # Step 2: Augment with key search terms (to ensure important series are included)
        print(f"\n🔍 Augmenting with key economic indicators...")
        all_series = list(top_series)  # Start with top series
        seen_ids = {s["id"] for s in all_series}

        for search_term in KEY_SEARCH_TERMS:
            search_results = fetch_series_by_search(api_key, search_term)
            for series in search_results:
                if series["id"] not in seen_ids:
                    all_series.append(series)
                    seen_ids.add(series["id"])
            time.sleep(0.5)

        print(f"   ✅ Total series after augmentation: {len(all_series):,}")

        if not all_series:
            print("❌ No series fetched. Exiting.")
            return 1

        # Step 3: Process series
        processed_series = process_series(all_series)

        # Step 4: Save metadata
        save_metadata(processed_series, DEFAULT_OUTPUT_FILE)

        print()
        print("=" * 70)
        print("✅ FRED metadata extraction complete!")
        print("=" * 70)
        return 0

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
