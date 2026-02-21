#!/usr/bin/env python3
"""
Fetch and cache Statistics Canada metadata for common products

This script downloads metadata from Statistics Canada API and saves it locally
to avoid timeouts and improve performance.
"""
import json
import requests
import time
from pathlib import Path
from datetime import datetime

# Common products we want to cache
COMMON_PRODUCTS = {
    '14100287': 'Labour Force Survey - unemployment by demographics',
    '17100005': 'Population estimates by age and gender',
    '36100402': 'GDP by industry',
    '17100040': 'Permanent residents by category',
    '14100063': 'Average hourly and weekly earnings',
    # Housing data
    '34100156': 'Housing starts in centres 10,000+, provinces and CMAs (monthly)',
    '18100205': 'New housing price index (monthly)',
}

CACHE_FILE = Path(__file__).parent.parent / 'backend' / 'data' / 'statscan_metadata_cache.json'

def fetch_metadata(product_id: str) -> dict:
    """Fetch metadata for a single product"""
    url = "https://www150.statcan.gc.ca/t1/wds/rest/getCubeMetadata"

    print(f"  Fetching metadata for product {product_id}...")

    try:
        response = requests.post(
            url,
            json=[{"productId": int(product_id)}],
            headers={"Content-Type": "application/json"},
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            if data and data[0].get("status") == "SUCCESS":
                metadata = data[0]["object"]
                print(f"    ✅ Success: {metadata.get('cubeTitleEn', 'Unknown')}")
                return metadata
            else:
                print(f"    ❌ API returned status: {data[0].get('status')}")
                return None
        else:
            print(f"    ❌ HTTP error: {response.status_code}")
            return None

    except Exception as e:
        print(f"    ❌ Error: {e}")
        return None

def main():
    print("=" * 70)
    print("FETCHING STATISTICS CANADA METADATA")
    print("=" * 70)
    print(f"\nCache file: {CACHE_FILE}")
    print(f"Products to fetch: {len(COMMON_PRODUCTS)}\n")

    # Load existing cache
    if CACHE_FILE.exists():
        with open(CACHE_FILE, 'r') as f:
            cache = json.load(f)
        print(f"✅ Loaded existing cache with {len(cache.get('products', {}))} products\n")
    else:
        cache = {
            "metadata_version": "1.0",
            "last_updated": datetime.now().isoformat(),
            "products": {}
        }
        print("📝 Creating new cache\n")

    # Fetch metadata for each product
    success_count = 0
    for product_id, description in COMMON_PRODUCTS.items():
        print(f"{product_id}: {description}")

        metadata = fetch_metadata(product_id)
        if metadata:
            cache['products'][product_id] = metadata
            success_count += 1

        # Be nice to the API
        time.sleep(0.5)
        print()

    # Update last_updated timestamp
    cache['last_updated'] = datetime.now().isoformat()

    # Save cache
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_FILE, 'w') as f:
        json.dump(cache, f, indent=2)

    print("=" * 70)
    print(f"✅ Successfully cached {success_count}/{len(COMMON_PRODUCTS)} products")
    print(f"💾 Saved to: {CACHE_FILE}")
    print(f"📦 Cache size: {CACHE_FILE.stat().st_size / 1024:.1f} KB")
    print("=" * 70)

if __name__ == "__main__":
    main()
