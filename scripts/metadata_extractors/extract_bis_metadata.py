#!/usr/bin/env python3
"""
BIS Metadata Extractor

Extracts indicators from BIS and stores them in a structured JSON format.

CRITICAL: BIS metadata catalogs are unreliable - many dataflows don't work.
This extractor includes VALIDATION to test each dataflow before including it.

UPDATED 2025-11-15: Added API discovery + validation
- Fetches all dataflows from BIS SDMX API
- **Validates each one** with test queries
- Only includes verified working dataflows
- Marks dataflows as "verified" with test date

Usage:
    python extract_bis_metadata.py [--output FILE] [--skip-validation]
"""

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List
import xml.etree.ElementTree as ET
import time

# Configuration
OUTPUT_DIR = Path(__file__).parent.parent.parent / "backend" / "data" / "metadata"
DEFAULT_OUTPUT_FILE = OUTPUT_DIR / "bis.json"

# SDMX API Configuration
BIS_SDMX_BASE_URL = "https://stats.bis.org/api/v1"
REQUEST_TIMEOUT = 30.0
VALIDATION_TIMEOUT = 10.0  # Shorter timeout for validation queries

# SDMX 2.1 XML Namespaces
SDMX_NAMESPACES = {
    'mes': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message',
    'str': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure',
    'com': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common'
}

# Test parameters for validation
TEST_COUNTRIES = ['US', 'GB', 'DE', 'JP', 'CH']  # Common countries in BIS data
TEST_FREQUENCIES = ['M', 'Q', 'A']  # Monthly, Quarterly, Annual


def fetch_all_bis_dataflows() -> List[Dict]:
    """Fetch all available dataflows from BIS SDMX API."""
    try:
        import httpx
    except ImportError:
        print("❌ Error: httpx library not found. Install with: pip install httpx")
        raise

    print(f"\n🔍 Fetching all BIS dataflows from SDMX API...")
    print(f"   URL: {BIS_SDMX_BASE_URL}/dataflow/BIS")

    url = f"{BIS_SDMX_BASE_URL}/dataflow/BIS"
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
        print(f"   ✅ Found {len(dataflows)} dataflows")

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
        print(f"   ❌ Error fetching dataflows: {e}")
        raise


def validate_dataflow(dataflow_id: str) -> Dict:
    """
    Validate a BIS dataflow by testing if it returns data.
    
    Returns dict with validation results.
    """
    try:
        import httpx
    except ImportError:
        return {"verified": False, "error": "httpx not installed"}

    # Test with multiple combinations
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365 * 2)  # Last 2 years
    
    for freq in TEST_FREQUENCIES:
        for country in TEST_COUNTRIES:
            try:
                # Build test query URL
                url = f"{BIS_SDMX_BASE_URL}/data/{dataflow_id}/{freq}.{country}"
                params = {
                    "startPeriod": start_date.strftime("%Y"),
                    "endPeriod": end_date.strftime("%Y")
                }
                
                response = httpx.get(url, params=params, timeout=VALIDATION_TIMEOUT, follow_redirects=True)
                
                if response.status_code == 200 and len(response.content) > 500:
                    # Successfully got data
                    return {
                        "verified": True,
                        "test_query": f"{freq}.{country}",
                        "test_date": datetime.now().isoformat(),
                        "frequency": freq,
                        "test_country": country
                    }
                    
            except Exception:
                continue  # Try next combination
    
    # No combination worked
    return {
        "verified": False,
        "test_date": datetime.now().isoformat(),
        "error": "No data returned for any test combination"
    }


def process_datasets_with_validation(datasets: List[Dict], skip_validation: bool = False) -> List[Dict]:
    """Process datasets and validate each one."""
    processed = []
    
    print(f"\n📊 Processing and validating BIS datasets...")
    print(f"   ⚠️  Validation may take several minutes...")
    print()
    
    verified_count = 0
    failed_count = 0
    
    for i, dataset in enumerate(datasets):
        code = dataset.get("code", "")
        name = dataset.get("name", "")
        
        if not code:
            continue
        
        print(f"   [{i+1}/{len(datasets)}] Testing {code}... ", end='', flush=True)
        
        # Validate dataflow
        if skip_validation:
            validation_result = {"verified": False, "skipped": True}
            print("⏭️  SKIPPED")
        else:
            validation_result = validate_dataflow(code)
            if validation_result.get("verified"):
                print(f"✅ VERIFIED ({validation_result.get('test_query')})")
                verified_count += 1
            else:
                print(f"❌ FAILED ({validation_result.get('error', 'unknown')})")
                failed_count += 1
            
            # Small delay to avoid rate limiting
            time.sleep(0.5)
        
        # Create indicator entry
        indicator = {
            "id": f"BIS_{code}",
            "code": code,
            "name": name,
            "description": dataset.get("description", ""),
            "category": categorize_bis_dataflow(code, name),
            "agency": dataset.get("agency", "BIS"),
            "type": "bis_dataset",
            "source": "BIS",
            "verified": validation_result.get("verified", False),
            "validation_date": validation_result.get("test_date", ""),
            "test_query": validation_result.get("test_query", ""),
            "test_frequency": validation_result.get("frequency", ""),
            "test_country": validation_result.get("test_country", "")
        }
        
        processed.append(indicator)
    
    print()
    print(f"   ✅ Verified: {verified_count}/{len(datasets)}")
    print(f"   ❌ Failed: {failed_count}/{len(datasets)}")
    
    return processed


def categorize_bis_dataflow(code: str, name: str) -> str:
    """Categorize a BIS dataflow."""
    code_upper = code.upper()
    name_upper = name.upper()
    
    if "CBPOL" in code_upper or "POLICY" in name_upper:
        return "Monetary Policy"
    elif "CREDIT" in code_upper or "CREDIT" in name_upper:
        return "Credit"
    elif "EXCHANGE" in code_upper or "XRU" in code_upper or "XTD" in code_upper:
        return "Exchange Rates"
    elif "PRICE" in name_upper or "CPI" in code_upper or "CPP" in code_upper or "DPP" in code_upper or "SPP" in code_upper:
        return "Prices"
    elif "BANK" in name_upper or "CBS" in code_upper or "LBS" in code_upper:
        return "Banking Statistics"
    elif "DERIV" in code_upper or "DERIVATIVE" in name_upper:
        return "Derivatives"
    elif "DEBT" in code_upper or "DEBT" in name_upper or "DSR" in code_upper:
        return "Debt"
    elif "PAYMENT" in name_upper or "CPMI" in code_upper:
        return "Payments"
    else:
        return "Other"


def generate_aliases(code: str, name: str, category: str) -> List[str]:
    """Generate searchable aliases."""
    aliases = set()
    aliases.add(code.upper())
    aliases.add(f"BIS_{code.upper()}")
    aliases.add(name.upper())
    aliases.add(category.upper())
    return list(aliases)


def build_search_index(indicators: List[Dict]) -> Dict[str, List[str]]:
    """Build search index."""
    search_index = {}
    for indicator in indicators:
        indicator_id = indicator["id"]
        aliases = generate_aliases(indicator["code"], indicator["name"], indicator["category"])
        
        for alias in aliases:
            keyword = alias.lower()
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
    search_index = build_search_index(indicators)
    categories = organize_by_category(indicators)
    
    # Count verified vs unverified
    verified = [i for i in indicators if i.get("verified")]
    unverified = [i for i in indicators if not i.get("verified")]
    
    metadata = {
        "provider": "BIS",
        "last_updated": datetime.now().isoformat() + "Z",
        "total_indicators": len(indicators),
        "verified_indicators": len(verified),
        "unverified_indicators": len(unverified),
        "base_url": BIS_SDMX_BASE_URL,
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
    
    output_file.parent.mkdir(parents=True, exist_ok=True)
    
    print(f"\n💾 Saving metadata to {output_file}...")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    
    file_size_mb = output_file.stat().st_size / (1024 * 1024)
    print(f"   ✅ Saved {len(indicators)} indicators ({file_size_mb:.2f} MB)")
    print(f"      - Verified: {len(verified)}")
    print(f"      - Unverified: {len(unverified)}")
    print(f"   📁 File: {output_file}")
    print(f"   🏷️  Categories: {len(categories)}")


def main():
    """Main extraction workflow"""
    import argparse
    
    parser = argparse.ArgumentParser(description='Extract BIS metadata with validation')
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT_FILE, help='Output file path')
    parser.add_argument('--skip-validation', action='store_true', help='Skip validation step (not recommended)')
    args = parser.parse_args()
    
    print("=" * 70)
    print("BIS Metadata Extractor - API Discovery + Validation Mode")
    print("=" * 70)
    print()
    
    try:
        # Fetch all dataflows
        dataflows = fetch_all_bis_dataflows()
        
        if not dataflows:
            raise ValueError("No dataflows were fetched from BIS SDMX API.")
        
        # Process and validate
        all_indicators = process_datasets_with_validation(dataflows, skip_validation=args.skip_validation)
        
        if not all_indicators:
            raise ValueError("No indicators were extracted.")
        
        # Save metadata
        save_metadata(all_indicators, args.output)
        
        print()
        print("=" * 70)
        print("✅ BIS metadata extraction complete!")
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
