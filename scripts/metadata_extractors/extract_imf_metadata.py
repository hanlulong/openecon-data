#!/usr/bin/env python3
"""
IMF Metadata Extractor - Comprehensive Edition

Extracts indicators from TWO IMF data sources:
1. IMF SDMX API - 101 dataflows across multiple databases (BOP, DOT, FSI, GFS, etc.)
2. IMF DataMapper API - ~132 summary economic indicators

This provides complete coverage of IMF data accessible via natural language queries.

UPDATED 2025-11-15: Added SDMX dataflow discovery alongside DataMapper indicators

Usage:
    python extract_imf_metadata.py [--output FILE]
"""

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List
import xml.etree.ElementTree as ET

# Configuration
OUTPUT_DIR = Path(__file__).parent.parent.parent / "backend" / "data" / "metadata"
DEFAULT_OUTPUT_FILE = OUTPUT_DIR / "imf.json"

# IMF API URLs
IMF_DATAMAPPER_BASE = "https://www.imf.org/external/datamapper/api/v1"
IMF_SDMX_BASE = "https://sdmxcentral.imf.org/ws/public/sdmxapi/rest"

REQUEST_TIMEOUT = 30.0
RETRY_ATTEMPTS = 3
RETRY_DELAY = 2.0

# SDMX 2.1 XML Namespaces
SDMX_NAMESPACES = {
    'mes': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message',
    'str': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure',
    'com': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common'
}


def fetch_datamapper_indicators() -> Dict:
    """Fetch indicators from IMF DataMapper API.

    Returns ~132 summary economic indicators.
    """
    try:
        import httpx
    except ImportError:
        print("❌ Error: httpx library not found. Install with: pip install httpx")
        raise

    print(f"\n🌍 Fetching IMF DataMapper indicators...")
    url = f"{IMF_DATAMAPPER_BASE}/indicators"

    for attempt in range(RETRY_ATTEMPTS):
        try:
            response = httpx.get(url, timeout=REQUEST_TIMEOUT)
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

    indicators = data.get("indicators", {})
    print(f"   ✅ Fetched {len(indicators)} DataMapper indicators")
    return indicators


def fetch_sdmx_dataflows() -> List[Dict]:
    """Fetch all dataflows from IMF SDMX API.

    Returns 101 dataflows across databases like BOP, DOT, FSI, GFS, etc.
    """
    try:
        import httpx
    except ImportError:
        print("❌ Error: httpx library not found. Install with: pip install httpx")
        raise

    print(f"\n🔍 Fetching IMF SDMX dataflows...")
    print(f"   URL: {IMF_SDMX_BASE}/dataflow/IMF")

    url = f"{IMF_SDMX_BASE}/dataflow/IMF"
    headers = {"Accept": "application/vnd.sdmx.structure+xml; version=2.1"}

    try:
        response = httpx.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}: {response.text[:500]}")

        print(f"   ✅ Received {len(response.content):,} bytes")

        # Parse XML
        root = ET.fromstring(response.content)

        structures = root.find('.//mes:Structures', SDMX_NAMESPACES)
        if structures is None:
            raise Exception("No Structures element found")

        dataflows_elem = structures.find('.//str:Dataflows', SDMX_NAMESPACES)
        if dataflows_elem is None:
            raise Exception("No Dataflows element found")

        dataflows = dataflows_elem.findall('.//str:Dataflow', SDMX_NAMESPACES)
        print(f"   ✅ Found {len(dataflows)} SDMX dataflows")

        # Extract metadata
        extracted_dataflows = []
        xml_ns = '{http://www.w3.org/XML/1998/namespace}'

        for df in dataflows:
            df_id = df.get('id', 'N/A')
            agency_id = df.get('agencyID', 'N/A')
            version = df.get('version', 'N/A')

            # Get name
            name = df_id
            name_elements = df.findall('.//com:Name', SDMX_NAMESPACES)
            for name_elem in name_elements:
                lang = name_elem.get(f'{xml_ns}lang', '')
                if lang == 'en':
                    name = name_elem.text
                    break
            if name == df_id and name_elements:
                name = name_elements[0].text or df_id

            # Get description
            description = ""
            desc_elements = df.findall('.//com:Description', SDMX_NAMESPACES)
            for desc_elem in desc_elements:
                lang = desc_elem.get(f'{xml_ns}lang', '')
                if lang == 'en':
                    description = desc_elem.text or ""
                    break
            if not description and desc_elements:
                description = desc_elements[0].text or ""

            extracted_dataflows.append({
                "code": df_id,
                "agency": agency_id,
                "version": version,
                "name": name,
                "description": description
            })

        return extracted_dataflows

    except Exception as e:
        print(f"   ❌ Error fetching SDMX dataflows: {e}")
        raise


def categorize_sdmx_dataflow(code: str, name: str) -> str:
    """Categorize an SDMX dataflow by database."""
    name_upper = name.upper()

    if "INTERNATIONAL FINANCIAL STATISTICS" in name_upper or "IFS" in name_upper:
        return "IFS - International Financial Statistics"
    elif "BALANCE OF PAYMENTS" in name_upper or "BOP" in name_upper or "BPM" in name_upper:
        return "BOP - Balance of Payments"
    elif "DIRECTION OF TRADE" in name_upper or "DOTS" in name_upper:
        return "DOT - Direction of Trade"
    elif "FINANCIAL SOUNDNESS" in name_upper or "FSI" in name_upper:
        return "FSI - Financial Soundness Indicators"
    elif "WORLD ECONOMIC OUTLOOK" in name_upper or "WEO" in name_upper:
        return "WEO - World Economic Outlook"
    elif "GOVERNMENT FINANCE" in name_upper or "GFS" in name_upper:
        return "GFS - Government Finance Statistics"
    elif "COORDINATED PORTFOLIO" in name_upper or "CPIS" in name_upper:
        return "CPIS - Coordinated Portfolio Investment Survey"
    elif "COORDINATED DIRECT" in name_upper or "CDIS" in name_upper:
        return "CDIS - Coordinated Direct Investment Survey"
    elif "EXCHANGE RATE" in name_upper or "RESERVES" in name_upper or code == "01R":
        return "Exchange Rates & Reserves"
    elif "MONETARY" in name_upper or "M&B" in name_upper:
        return "Monetary & Banking Statistics"
    elif "DEBT" in name_upper:
        return "Debt Statistics"
    else:
        return "Other Statistical Databases"


def categorize_datamapper_indicator(code: str, label: str) -> str:
    """Categorize a DataMapper indicator."""
    label_upper = label.upper()

    if "GDP" in label_upper or "GROWTH" in label_upper or "OUTPUT" in label_upper:
        return "GDP & Economic Growth"
    elif "INFLATION" in label_upper or "PRICE" in label_upper or "CPI" in label_upper:
        return "Prices & Inflation"
    elif "UNEMPLOYMENT" in label_upper or "LABOR" in label_upper or "EMPLOYMENT" in label_upper:
        return "Labor Market"
    elif "CURRENT ACCOUNT" in label_upper or "BALANCE OF PAYMENTS" in label_upper or "TRADE" in label_upper:
        return "External Sector"
    elif "DEBT" in label_upper or "FISCAL" in label_upper or "GOVERNMENT" in label_upper:
        return "Fiscal & Debt"
    elif "EXCHANGE RATE" in label_upper or "CURRENCY" in label_upper:
        return "Exchange Rates"
    elif "POPULATION" in label_upper or "DEMOGRAPHIC" in label_upper:
        return "Demographics"
    else:
        return "Other Economic Indicators"


def process_sdmx_dataflows(dataflows: List[Dict]) -> List[Dict]:
    """Process SDMX dataflows into indicator format."""
    processed = []

    print(f"\n📊 Processing SDMX dataflows...")

    for dataflow in dataflows:
        code = dataflow.get("code", "")
        name = dataflow.get("name", "")

        if not code or not name:
            continue

        category = categorize_sdmx_dataflow(code, name)

        # Generate aliases
        aliases = set()
        aliases.add(code.upper())
        aliases.add(f"IMF_{code.upper()}")
        aliases.add(name.upper())
        aliases.add(category.upper())

        # Create searchable text
        searchable_text = " ".join([
            code.lower(),
            name.lower(),
            dataflow.get("description", "").lower(),
            category.lower()
        ])

        indicator = {
            "id": f"IMF_SDMX_{code}",
            "code": code,
            "name": name,
            "description": dataflow.get("description", name),
            "category": category,
            "type": "sdmx_dataflow",
            "source": "IMF SDMX",
            "agency": dataflow.get("agency", "IMF"),
            "version": dataflow.get("version", ""),
            "aliases": list(aliases),
            "searchable_text": searchable_text
        }

        processed.append(indicator)

    print(f"   ✅ Processed {len(processed)} SDMX dataflows")
    return processed


def process_datamapper_indicators(raw_indicators: Dict) -> List[Dict]:
    """Process DataMapper indicators into standard format."""
    processed = []
    skipped = 0

    print(f"\n📊 Processing DataMapper indicators...")

    for code, data in raw_indicators.items():
        # Skip indicators without valid code
        if not code or not isinstance(code, str) or not code.strip():
            skipped += 1
            continue

        # Extract fields
        label = data.get("label", "")

        # Skip indicators without valid label/name
        if not label or not isinstance(label, str) or not label.strip():
            skipped += 1
            continue

        description = data.get("description", label)
        unit = data.get("unit", "")
        dataset = data.get("dataset", "")

        category = categorize_datamapper_indicator(code, label)

        # Generate aliases (simplified - removed complex logic for consistency)
        aliases = set()
        aliases.add(code.upper())
        aliases.add(f"IMF_{code.upper()}")
        aliases.add(label.upper())
        aliases.add(category.upper())

        # Create searchable text
        searchable_text = " ".join([
            code.lower(),
            label.lower(),
            description.lower() if description else "",
            unit.lower() if unit else "",
            category.lower()
        ])

        # Build processed indicator
        processed_indicator = {
            "id": f"IMF_DM_{code}",
            "code": code,
            "name": label,
            "description": description,
            "unit": unit,
            "dataset": dataset,
            "category": category,
            "type": "datamapper_indicator",
            "source": "IMF DataMapper",
            "aliases": list(aliases),
            "searchable_text": searchable_text
        }

        processed.append(processed_indicator)

    print(f"   ✅ Processed {len(processed)} DataMapper indicators")
    if skipped > 0:
        print(f"   ⚠️ Skipped {skipped} malformed indicator(s)")
    return processed


def build_search_index(indicators: List[Dict]) -> Dict[str, List[str]]:
    """Build search index mapping keywords to indicator IDs."""
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

        # Index words from name
        name = indicator.get("name", "") or ""
        for word in name.split():
            if len(word) > 3:  # Skip short words
                keyword = word.lower()
                if keyword not in search_index:
                    search_index[keyword] = []
                if indicator_id not in search_index[keyword]:
                    search_index[keyword].append(indicator_id)

    return search_index


def organize_by_category(indicators: List[Dict]) -> Dict[str, List[str]]:
    """Organize indicators by category."""
    categories = {}

    for indicator in indicators:
        category = indicator.get("category", "Other")
        if category not in categories:
            categories[category] = []
        categories[category].append(indicator["id"])

    return categories


def save_metadata(indicators: List[Dict], output_file: Path):
    """Save processed metadata to JSON file."""
    # Build search index
    search_index = build_search_index(indicators)

    # Organize by category
    categories = organize_by_category(indicators)

    # Count by source type
    sdmx_count = sum(1 for i in indicators if i.get("type") == "sdmx_dataflow")
    datamapper_count = sum(1 for i in indicators if i.get("type") == "datamapper_indicator")

    # Build metadata structure
    metadata = {
        "provider": "IMF",
        "last_updated": datetime.now().isoformat() + "Z",
        "total_indicators": len(indicators),
        "sdmx_dataflows": sdmx_count,
        "datamapper_indicators": datamapper_count,
        "sdmx_api_url": IMF_SDMX_BASE,
        "datamapper_api_url": IMF_DATAMAPPER_BASE,
        "categories": {
            category: {
                "count": len(codes),
                "indicators": codes
            }
            for category, codes in categories.items()
        },
        "indicators": indicators,
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
    print(f"   ✅ Saved {len(indicators)} indicators ({file_size_mb:.2f} MB)")
    print(f"      - SDMX dataflows: {sdmx_count}")
    print(f"      - DataMapper indicators: {datamapper_count}")
    print(f"   📁 File: {output_file}")
    print(f"   🏷️  Categories: {len(categories)}")
    print(f"   🔍 Search keywords: {len(search_index)}")


def main():
    """Main extraction workflow"""
    print("=" * 70)
    print("IMF Metadata Extractor - Comprehensive Edition")
    print("=" * 70)
    print()

    try:
        # Step 1: Fetch SDMX dataflows
        sdmx_dataflows = fetch_sdmx_dataflows()

        # Step 2: Fetch DataMapper indicators
        datamapper_indicators = fetch_datamapper_indicators()

        if not sdmx_dataflows and not datamapper_indicators:
            print("❌ No indicators fetched from either source. Exiting.")
            return 1

        # Step 3: Process both sources
        processed_sdmx = process_sdmx_dataflows(sdmx_dataflows)
        processed_datamapper = process_datamapper_indicators(datamapper_indicators)

        # Step 4: Combine all indicators
        all_indicators = processed_sdmx + processed_datamapper

        # Step 5: Save metadata
        save_metadata(all_indicators, DEFAULT_OUTPUT_FILE)

        print()
        print("=" * 70)
        print("✅ IMF metadata extraction complete!")
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
