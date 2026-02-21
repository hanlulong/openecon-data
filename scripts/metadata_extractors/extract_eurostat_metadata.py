#!/usr/bin/env python3
"""
Eurostat Metadata Extractor

Extracts indicators from Eurostat and stores them in a structured JSON format
for fast local lookups and semantic search.

Eurostat provides economic and social statistics for the European Union.

API Documentation:
- SDMX 2.1: https://ec.europa.eu/eurostat/web/main/data/web-services

UPDATED 2025-11-15: Switched from hardcoded list to API discovery
- Now fetches ALL dataflows from Eurostat SDMX API
- Discovers ~8,010 dataflows (vs. previous 118 hardcoded)
- Uses SDMX 2.1 XML format for complete metadata

Usage:
    python extract_eurostat_metadata.py [--output FILE]
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List
import xml.etree.ElementTree as ET

# Configuration
OUTPUT_DIR = Path(__file__).parent.parent.parent / "backend" / "data" / "metadata"
DEFAULT_OUTPUT_FILE = OUTPUT_DIR / "eurostat.json"

# SDMX API Configuration
EUROSTAT_SDMX_BASE_URL = "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1"
REQUEST_TIMEOUT = 180.0  # Increased timeout for very large XML responses (33 MB)

# SDMX 2.1 XML Namespaces
SDMX_NAMESPACES = {
    'm': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message',
    's': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure',
    'c': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common'
}


def fetch_all_eurostat_dataflows() -> List[Dict]:
    """
    Fetch all available dataflows from Eurostat SDMX API.

    Returns:
        List of dictionaries with dataflow metadata
    """
    try:
        import httpx
    except ImportError:
        print("❌ Error: httpx library not found. Install with: pip install httpx")
        raise

    print(f"\n🔍 Fetching all Eurostat dataflows from SDMX API...")
    print(f"   URL: {EUROSTAT_SDMX_BASE_URL}/dataflow/ESTAT")
    print(f"   ⚠️  Note: This is a large response (~33 MB), please be patient...")

    url = f"{EUROSTAT_SDMX_BASE_URL}/dataflow/ESTAT"
    headers = {
        "Accept": "application/vnd.sdmx.structure+xml; version=2.1"
    }

    try:
        # Fetch dataflow list
        response = httpx.get(url, headers=headers, timeout=REQUEST_TIMEOUT, follow_redirects=True)

        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}: {response.text[:500]}")

        print(f"   ✅ Received {len(response.content):,} bytes")

        # Parse XML
        print(f"   🔄 Parsing XML...")
        root = ET.fromstring(response.content)

        # Navigate to Structures -> Dataflows
        structures = root.find('.//m:Structures', SDMX_NAMESPACES)
        if structures is None:
            raise Exception("No Structures element found in SDMX response")

        # Find Dataflows element
        dataflows_elem = structures.find('.//s:Dataflows', SDMX_NAMESPACES)
        if dataflows_elem is None:
            raise Exception("No Dataflows element found in SDMX response")

        # Get all Dataflow elements
        dataflows = dataflows_elem.findall('.//s:Dataflow', SDMX_NAMESPACES)

        print(f"   ✅ Found {len(dataflows)} dataflows")
        print(f"   🔄 Extracting metadata...")

        # Extract metadata from each dataflow
        extracted_dataflows = []

        # Add xml namespace for lang attribute
        xml_ns = '{http://www.w3.org/XML/1998/namespace}'

        for i, df in enumerate(dataflows):
            if (i + 1) % 1000 == 0:
                print(f"      Progress: {i+1}/{len(dataflows)} dataflows processed...")

            df_id = df.get('id', 'N/A')
            agency_id = df.get('agencyID', 'N/A')
            version = df.get('version', 'N/A')

            # Get name - prefer English version
            name = df_id  # Default fallback
            name_elements = df.findall('.//c:Name', SDMX_NAMESPACES)
            for name_elem in name_elements:
                lang = name_elem.get(f'{xml_ns}lang', '')
                if lang == 'en':
                    name = name_elem.text
                    break
            # If no English, take first available
            if name == df_id and name_elements:
                name = name_elements[0].text or df_id

            # Get description - prefer English version
            description = ""
            desc_elements = df.findall('.//c:Description', SDMX_NAMESPACES)
            for desc_elem in desc_elements:
                lang = desc_elem.get(f'{xml_ns}lang', '')
                if lang == 'en':
                    description = desc_elem.text or ""
                    break
            # If no English, take first available
            if not description and desc_elements:
                description = desc_elements[0].text or ""

            # Determine category based on dataflow ID and name
            category = categorize_eurostat_dataflow(df_id, name)

            extracted_dataflows.append({
                "code": df_id,
                "agency": agency_id,
                "version": version,
                "name": name,
                "description": description,
                "category": category
            })

        print(f"   ✅ Extraction complete")
        return extracted_dataflows

    except Exception as e:
        print(f"   ❌ Error fetching dataflows: {e}")
        raise


def categorize_eurostat_dataflow(code: str, name: str) -> str:
    """
    Categorize a Eurostat dataflow based on code prefix and name.

    Eurostat uses systematic code prefixes for different statistical domains.

    Args:
        code: Dataflow code (e.g., "nama_10_gdp")
        name: Dataflow name

    Returns:
        Category string
    """
    code_upper = code.upper()
    name_upper = name.upper()

    # National Accounts
    if code.startswith(('nama_', 'nasq_', 'namq_', 'nasa_', 'naida_')):
        return "National Accounts"

    # Labor Force Survey / Employment
    if code.startswith(('lfst_', 'lfsa_', 'lfsq_', 'lfsm_', 'earn_', 'labour_')):
        return "Labor Market"

    # Prices and Inflation
    if code.startswith(('prc_', 'sts_in', 'ei_cphi')):
        return "Prices"

    # International Trade
    if code.startswith(('ext_', 'bop_', 'fdi_', 'comext_')):
        return "International Trade"

    # Government Finance
    if code.startswith(('gov_', 'tin_', 'tax_')):
        return "Government Finance"

    # Population and Demographics
    if code.startswith(('demo_', 'proj_', 'cens_', 'migr_')):
        return "Population"

    # Education and Training
    if code.startswith(('educ_', 'trng_', 'edat_')):
        return "Education"

    # Health
    if code.startswith(('hlth_', 'care_')):
        return "Health"

    # Social Protection
    if code.startswith(('spr_', 'ilc_', 'liv_')):
        return "Social Protection"

    # Industry and Construction
    if code.startswith(('sts_', 'ind_', 'con_')):
        return "Industry"

    # Agriculture and Fisheries
    if code.startswith(('agr_', 'apro_', 'fish_', 'for_')):
        return "Agriculture"

    # Energy
    if code.startswith(('nrg_', 'en_')):
        return "Energy"

    # Transport
    if code.startswith(('tran_', 'rail_', 'road_', 'avia_', 'mar_')):
        return "Transport"

    # Environment
    if code.startswith(('env_', 'air_', 'wat_', 'waste_', 'sdg_')):
        return "Environment"

    # Science and Technology
    if code.startswith(('rd_', 'inn_', 'pat_', 'hrst_', 'isoc_')):
        return "Science & Technology"

    # Regional Statistics
    if code.startswith(('reg_', 'urb_')):
        return "Regional Statistics"

    # Tourism
    if code.startswith(('tour_')):
        return "Tourism"

    # Business
    if code.startswith(('bd_', 'sbs_')):
        return "Business"

    # Fallback to name-based categorization
    if "GDP" in name_upper or "NATIONAL ACCOUNTS" in name_upper:
        return "National Accounts"
    elif "EMPLOYMENT" in name_upper or "UNEMPLOYMENT" in name_upper or "LABOUR" in name_upper:
        return "Labor Market"
    elif "PRICE" in name_upper or "INFLATION" in name_upper:
        return "Prices"
    elif "TRADE" in name_upper or "EXPORT" in name_upper or "IMPORT" in name_upper:
        return "International Trade"
    elif "GOVERNMENT" in name_upper or "TAX" in name_upper:
        return "Government Finance"
    elif "POPULATION" in name_upper or "DEMOGRAPHIC" in name_upper:
        return "Population"
    elif "EDUCATION" in name_upper:
        return "Education"
    elif "HEALTH" in name_upper:
        return "Health"

    return "Other"


def generate_aliases(code: str, name: str, category: str) -> List[str]:
    """Generate searchable aliases for a dataset."""
    aliases = set()

    name_upper = name.upper()
    category_upper = category.upper()

    # Add code variants
    aliases.add(code.upper())
    aliases.add(f"EUROSTAT_{code.upper()}")

    # Add name variants
    aliases.add(name_upper)
    aliases.add(name_upper.replace(" ", "_"))

    # Add category
    aliases.add(category_upper)

    # Common economic indicator abbreviations
    if "GDP" in name_upper or "NATIONAL ACCOUNTS" in name_upper:
        aliases.add("GDP")
        aliases.add("NATIONAL_ACCOUNTS")
    if "UNEMPLOYMENT" in name_upper:
        aliases.add("UNEMPLOYMENT")
        aliases.add("UNEMPLOYMENT_RATE")
    if "EMPLOYMENT" in name_upper:
        aliases.add("EMPLOYMENT")
    if "WAGE" in name_upper or "EARNINGS" in name_upper or "SALARY" in name_upper:
        aliases.add("WAGES")
        aliases.add("EARNINGS")
    if "PRICE" in name_upper or "CPI" in name_upper or "INFLATION" in name_upper or "HICP" in name_upper:
        aliases.add("CPI")
        aliases.add("HICP")
        aliases.add("INFLATION")
        aliases.add("PRICES")
    if "TRADE" in name_upper:
        aliases.add("TRADE")
        if "EXPORT" in name_upper:
            aliases.add("EXPORTS")
        if "IMPORT" in name_upper:
            aliases.add("IMPORTS")
    if "GOVERNMENT" in name_upper or "GOV_" in code:
        aliases.add("GOVERNMENT")
        if "REVENUE" in name_upper or "TAX" in name_upper:
            aliases.add("TAX")
            aliases.add("REVENUE")
    if "POPULATION" in name_upper or "DEMO_" in code:
        aliases.add("POPULATION")
        aliases.add("DEMOGRAPHICS")
    if "EDUCATION" in name_upper:
        aliases.add("EDUCATION")
    if "HEALTH" in name_upper:
        aliases.add("HEALTH")
    if "ENVIRONMENT" in name_upper or "ENV_" in code:
        aliases.add("ENVIRONMENT")
        if "EMISSION" in name_upper:
            aliases.add("EMISSIONS")

    return list(aliases)


def process_datasets(datasets: List[Dict]) -> List[Dict]:
    """Process datasets into structured indicator format."""
    processed = []

    print(f"\n📊 Processing Eurostat datasets...")

    for dataset in datasets:
        code = dataset.get("code", "")
        name = dataset.get("name", "")
        category = dataset.get("category", "")
        description = dataset.get("description", "")
        agency = dataset.get("agency", "")

        if not code:
            continue

        # Generate aliases
        aliases = generate_aliases(code, name, category)

        # Create searchable text
        searchable_text = " ".join([
            code.lower(),
            name.lower(),
            category.lower(),
            description.lower(),
            agency.lower(),
            " ".join(aliases).lower()
        ])

        processed_indicator = {
            "id": f"EUROSTAT_{code}",
            "code": code,
            "name": name,
            "description": description,
            "category": category,
            "agency": agency,
            "type": "eurostat_dataset",
            "source": "Eurostat",
            "aliases": aliases,
            "searchable_text": searchable_text
        }

        processed.append(processed_indicator)

    print(f"   ✅ Processed {len(processed)} Eurostat datasets")
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
        name = indicator.get("name", "")
        for word in name.split():
            if len(word) > 3:
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

    # Build metadata structure
    metadata = {
        "provider": "Eurostat",
        "last_updated": datetime.now().isoformat() + "Z",
        "total_indicators": len(indicators),
        "base_url": EUROSTAT_SDMX_BASE_URL,
        "categories": {
            category: {
                "count": len(indicator_ids),
                "indicators": indicator_ids
            }
            for category, indicator_ids in categories.items()
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
    print(f"   📁 File: {output_file}")
    print(f"   🏷️  Categories: {len(categories)}")
    print(f"   🔍 Search keywords: {len(search_index):,}")


def main():
    """Main extraction workflow"""
    print("=" * 70)
    print("Eurostat Metadata Extractor - API Discovery Mode")
    print("=" * 70)
    print()

    try:
        # Fetch all dataflows from SDMX API
        dataflows = fetch_all_eurostat_dataflows()

        if not dataflows:
            raise ValueError("No dataflows were fetched from Eurostat SDMX API.")

        print(f"\n📊 Processing {len(dataflows)} Eurostat dataflows...")

        # Process datasets
        all_indicators = process_datasets(dataflows)

        if not all_indicators:
            raise ValueError("No indicators were extracted.")

        # Save metadata
        save_metadata(all_indicators, DEFAULT_OUTPUT_FILE)

        print()
        print("=" * 70)
        print("✅ Eurostat metadata extraction complete!")
        print(f"   Extracted {len(all_indicators)} indicators (vs. previous 118)")
        print(f"   Improvement: {len(all_indicators) / 118:.1f}x more coverage")
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
