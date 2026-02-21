#!/usr/bin/env python3
"""
WorldBank Metadata Extractor

Extracts all available indicators from WorldBank API and stores them
in a structured JSON format for fast local lookups.

API Documentation: https://datahelpdesk.worldbank.org/knowledgebase/articles/889392-about-the-indicators-api-documentation

Usage:
    python extract_worldbank_metadata.py [--output FILE]
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Set
import httpx

# Configuration
WORLDBANK_API_BASE = "https://api.worldbank.org/v2"
OUTPUT_DIR = Path(__file__).parent.parent.parent / "backend" / "data" / "metadata"
DEFAULT_OUTPUT_FILE = OUTPUT_DIR / "worldbank.json"
PER_PAGE = 10000  # Maximum allowed by API
REQUEST_TIMEOUT = 30.0
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2.0


def fetch_all_indicators() -> List[Dict]:
    """
    Fetch all indicators from WorldBank API with pagination.

    Returns:
        List of indicator dictionaries
    """
    all_indicators = []
    page = 1

    print(f"🌍 Fetching WorldBank indicators...")

    while True:
        url = f"{WORLDBANK_API_BASE}/indicator"
        params = {
            "format": "json",
            "per_page": PER_PAGE,
            "page": page
        }

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

        # WorldBank API returns [metadata, data]
        if len(data) < 2 or not data[1]:
            break

        metadata = data[0]
        indicators = data[1]

        total_pages = metadata.get("pages", 1)
        total_indicators = metadata.get("total", 0)

        print(f"   📄 Page {page}/{total_pages} ({len(indicators)} indicators)")

        all_indicators.extend(indicators)

        if page >= total_pages:
            break

        page += 1
        time.sleep(0.5)  # Rate limiting

    print(f"   ✅ Fetched {len(all_indicators)} indicators total")
    return all_indicators


def generate_aliases(indicator: Dict) -> List[str]:
    """
    Generate searchable aliases for an indicator.

    Args:
        indicator: Indicator dictionary from API

    Returns:
        List of alias strings
    """
    aliases = set()

    # Add ID variants
    indicator_id = indicator.get("id", "")
    if indicator_id:
        aliases.add(indicator_id)
        aliases.add(indicator_id.replace(".", "_"))
        aliases.add(indicator_id.replace(".", " "))

    # Add name variants
    name = indicator.get("name", "")
    if name:
        aliases.add(name.upper())
        aliases.add(name.upper().replace(" ", "_"))
        aliases.add(name.upper().replace(" ", ""))

        # Common abbreviations
        name_upper = name.upper()
        if "GROSS DOMESTIC PRODUCT" in name_upper:
            aliases.add("GDP")
            if "PER CAPITA" in name_upper:
                aliases.add("GDP_PER_CAPITA")
                aliases.add("GDP PER CAPITA")
        if "CONSUMER PRICE INDEX" in name_upper or "CPI" in name_upper:
            aliases.add("CPI")
            aliases.add("INFLATION")
        if "UNEMPLOYMENT" in name_upper:
            aliases.add("UNEMPLOYMENT")
            aliases.add("UNEMPLOYMENT_RATE")
        if "POPULATION" in name_upper:
            if "GROWTH" in name_upper:
                aliases.add("POPULATION_GROWTH")
                aliases.add("POPULATION_GROWTH_RATE")
            else:
                aliases.add("POPULATION")
        if "LIFE EXPECTANCY" in name_upper:
            aliases.add("LIFE_EXPECTANCY")
        if "POVERTY" in name_upper:
            aliases.add("POVERTY_RATE")
            aliases.add("POVERTY")
        if "INFANT MORTALITY" in name_upper:
            aliases.add("INFANT_MORTALITY")
            aliases.add("INFANT_MORTALITY_RATE")
        if "CO2" in name_upper or "CARBON" in name_upper:
            aliases.add("CO2_EMISSIONS")
            aliases.add("CARBON_EMISSIONS")
        if "RENEWABLE ENERGY" in name_upper:
            aliases.add("RENEWABLE_ENERGY")
            aliases.add("RENEWABLE_ENERGY_CONSUMPTION")
        if "FOREIGN DIRECT INVESTMENT" in name_upper or "FDI" in name_upper:
            aliases.add("FDI")
            aliases.add("FOREIGN_DIRECT_INVESTMENT")
        if "TRADE" in name_upper and "GDP" in name_upper:
            aliases.add("TRADE_GDP")
            aliases.add("TRADE")
        if "RESEARCH AND DEVELOPMENT" in name_upper or "R&D" in name_upper:
            aliases.add("R&D_EXPENDITURE")
            aliases.add("RD_EXPENDITURE")
            aliases.add("RESEARCH_EXPENDITURE")
        if "AGRICULTURE" in name_upper and "VALUE ADDED" in name_upper:
            aliases.add("AGRICULTURE_VALUE_ADDED")

    return list(aliases)


def build_search_index(indicators: List[Dict]) -> Dict[str, List[str]]:
    """
    Build search index mapping keywords to indicator IDs.

    Args:
        indicators: List of processed indicators

    Returns:
        Dictionary mapping lowercase keywords to indicator IDs
    """
    search_index = {}

    for indicator in indicators:
        indicator_id = indicator["id"]

        # Index all aliases
        for alias in indicator.get("aliases", []):
            keyword = alias.lower()
            if keyword not in search_index:
                search_index[keyword] = []
            if indicator_id not in search_index[keyword]:
                search_index[keyword].append(indicator_id)

        # Index words from name and description
        searchable_text = indicator.get("searchable_text", "")
        for word in searchable_text.split():
            if len(word) > 3:  # Skip short words
                keyword = word.lower()
                if keyword not in search_index:
                    search_index[keyword] = []
                if indicator_id not in search_index[keyword]:
                    search_index[keyword].append(indicator_id)

    return search_index


def process_indicators(raw_indicators: List[Dict]) -> List[Dict]:
    """
    Process raw indicators into structured format.

    Args:
        raw_indicators: Raw indicator data from API

    Returns:
        List of processed indicator dictionaries
    """
    processed = []
    seen_ids = set()  # Track indicator IDs to avoid duplicates

    print(f"\n📊 Processing indicators...")

    for raw in raw_indicators:
        # Extract fields
        indicator_id = raw.get("id", "")
        if not indicator_id:
            continue

        # Skip duplicates
        if indicator_id in seen_ids:
            continue
        seen_ids.add(indicator_id)

        name = raw.get("name", "")
        source_note = raw.get("sourceNote", "")
        source_org = raw.get("sourceOrganization", "")
        topics = [t.get("value", "") for t in raw.get("topics", []) if t.get("value")]

        # Generate aliases
        aliases = generate_aliases(raw)

        # Create searchable text
        searchable_parts = [
            name.lower(),
            source_note.lower() if source_note else "",
            " ".join(topics).lower(),
            " ".join(aliases).lower()
        ]
        searchable_text = " ".join(p for p in searchable_parts if p)

        # Build processed indicator
        processed_indicator = {
            "id": indicator_id,
            "code": indicator_id,
            "name": name,
            "description": source_note or "",
            "source": source_org or "World Bank",
            "topics": topics,
            "unit": "",  # WorldBank doesn't provide unit in metadata
            "periodicity": "",  # Not available in API
            "aliases": aliases,
            "searchable_text": searchable_text
        }

        processed.append(processed_indicator)

    print(f"   ✅ Processed {len(processed)} unique indicators")
    return processed


def organize_by_topic(indicators: List[Dict]) -> Dict[str, List[Dict]]:
    """
    Organize indicators by topic/category.

    Args:
        indicators: List of processed indicators

    Returns:
        Dictionary mapping topics to indicators
    """
    categories = {}

    for indicator in indicators:
        topics = indicator.get("topics", ["Uncategorized"])
        if not topics:
            topics = ["Uncategorized"]

        for topic in topics:
            if topic not in categories:
                categories[topic] = []
            categories[topic].append(indicator)

    return categories


def save_metadata(indicators: List[Dict], output_file: Path):
    """
    Save processed metadata to JSON file.

    Args:
        indicators: List of processed indicators
        output_file: Path to output JSON file
    """
    # Build search index
    search_index = build_search_index(indicators)

    # Organize by topic
    categories = organize_by_topic(indicators)

    # Build metadata structure
    metadata = {
        "provider": "WorldBank",
        "last_updated": datetime.utcnow().isoformat() + "Z",
        "total_indicators": len(indicators),
        "api_url": WORLDBANK_API_BASE,
        "categories": {
            topic: {
                "count": len(indicators_list),
                "indicators": [ind["id"] for ind in indicators_list]
            }
            for topic, indicators_list in categories.items()
        },
        "indicators": indicators,
        "search_index": {k: v for k, v in search_index.items() if len(v) <= 50}  # Limit index size
    }

    # Ensure output directory exists
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Save to file
    print(f"\n💾 Saving metadata to {output_file}...")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    # Print statistics
    file_size_mb = output_file.stat().st_size / (1024 * 1024)
    print(f"   ✅ Saved {len(indicators)} indicators ({file_size_mb:.2f} MB)")
    print(f"   📁 File: {output_file}")
    print(f"   🏷️  Categories: {len(categories)}")
    print(f"   🔍 Search keywords: {len(search_index)}")


def main():
    """Main extraction workflow"""
    print("=" * 70)
    print("WorldBank Metadata Extractor")
    print("=" * 70)
    print()

    try:
        # Step 1: Fetch all indicators
        raw_indicators = fetch_all_indicators()

        if not raw_indicators:
            print("❌ No indicators fetched. Exiting.")
            return 1

        # Step 2: Process indicators
        processed_indicators = process_indicators(raw_indicators)

        # Step 3: Save metadata
        save_metadata(processed_indicators, DEFAULT_OUTPUT_FILE)

        print()
        print("=" * 70)
        print("✅ WorldBank metadata extraction complete!")
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
