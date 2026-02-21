#!/usr/bin/env python3
"""
Comprehensive Indicator Fetcher

Fetches ALL indicators from ALL providers and stores them in the SQLite database.

Usage:
    python scripts/fetch_all_indicators.py                    # Fetch all providers
    python scripts/fetch_all_indicators.py --provider FRED    # Fetch specific provider
    python scripts/fetch_all_indicators.py --stats            # Show current stats
    python scripts/fetch_all_indicators.py --verify           # Verify completeness

Coverage Goals:
    - FRED: Popular series (50,000+)
    - World Bank: 29,323 indicators (100%)
    - IMF: All DataMapper + IFS databases
    - Eurostat: 8,118 datasets (100%)
    - OECD: All dataflows
    - StatsCan: 8,058 tables (100%)
    - BIS: 30 dataflows (100%)
    - CoinGecko: 19,000+ cryptocurrencies
    - ExchangeRate: 160+ currencies
    - Comtrade: HS product codes
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.services.indicator_database import (
    Indicator,
    IndicatorDatabase,
    get_indicator_database,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Load environment
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().split("\n"):
        if "=" in line and not line.startswith("#"):
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class IndicatorFetcher:
    """Fetches indicators from all providers."""

    def __init__(self, db: IndicatorDatabase):
        self.db = db
        self.client = httpx.AsyncClient(timeout=60.0)

    async def close(self):
        await self.client.aclose()

    # =========================================================================
    # FRED - Federal Reserve Economic Data
    # =========================================================================
    async def fetch_fred(self) -> int:
        """
        Fetch FRED series by exploring ALL major categories.
        FRED has 800,000+ series - we fetch from all major categories.
        """
        api_key = os.getenv("FRED_API_KEY")
        if not api_key:
            logger.warning("FRED_API_KEY not set, skipping FRED")
            return 0

        logger.info("Fetching FRED indicators...")
        start_time = time.time()
        indicators = []

        # ALL major FRED categories with series counts (discovered via API exploration)
        # Format: (category_id, category_name, max_to_fetch)
        categories = [
            # Largest categories
            (97, "Housing", 50000),  # 53,187 series
            (32251, "Flow of Funds", 40000),  # 43,865 series
            (33441, "Services", 8000),  # 7,962 series
            (33500, "Education", 6500),  # 6,445 series
            (33959, "Patents", 5000),  # 4,708 series
            (1, "Production & Business Activity", 3500),  # 3,274 series
            (3, "Industrial Production & Capacity Utilization", 3000),  # 2,672 series
            (33731, "Tax Data", 2500),  # 2,095 series
            (32217, "Commodities", 2000),  # 1,969 series
            (33509, "Labor Market Conditions", 1000),  # 914 series
            (32261, "House Price Indexes", 800),  # 726 series
            (33201, "Economic Policy Uncertainty", 700),  # 677 series
            (33001, "Income Distribution", 600),  # 515 series
            (32429, "Manufacturing", 500),  # 455 series
            (6, "Retail Trade", 500),  # 438 series
            (4, "Employment Cost Index", 500),  # 417 series
            (32262, "Business Cycle Expansions & Contractions", 300),  # 295 series
            (33203, "Wholesale Trade", 300),  # 236 series
            (33490, "Finance Companies", 250),  # 222 series
            (32436, "Construction", 200),  # 144 series
            (23, "Banking", 200),  # 142 series
            (33891, "Historical Federal Reserve Data", 150),  # 124 series
            (9, "Consumer Price Indexes (CPI and PCE)", 150),  # 111 series
            (32250, "ADP Employment", 100),  # 93 series
            (33940, "Emissions", 100),  # 60 series
            (104, "Population", 100),  # 58 series
            (33202, "Transportation", 100),  # 57 series
            (33717, "Health Care Indexes", 100),  # 57 series
            (33831, "Minimum Wage", 100),  # 51 series
            (46, "Financial Indicators", 50),  # 16 series
            (31, "Producer Price Indexes (PPI)", 50),  # 15 series
            (12, "Current Population Survey (Household Survey)", 50),  # 41 series
            (32241, "Job Openings and Labor Turnover (JOLTS)", 50),  # 6 series
            (32145, "Foreign Exchange Intervention", 50),  # 21 series
            (5, "Federal Government Debt", 50),  # 41 series
            (33936, "Business Surveys", 50),  # 39 series
        ]

        for cat_id, cat_name, max_fetch in categories:
            logger.info(f"  Fetching FRED category: {cat_name} ({cat_id}), max {max_fetch}")
            try:
                offset = 0
                limit = 1000
                fetched_in_cat = 0
                while fetched_in_cat < max_fetch:
                    resp = await self.client.get(
                        "https://api.stlouisfed.org/fred/category/series",
                        params={
                            "category_id": cat_id,
                            "api_key": api_key,
                            "file_type": "json",
                            "limit": limit,
                            "offset": offset,
                            "order_by": "popularity",
                            "sort_order": "desc",
                        }
                    )
                    if resp.status_code != 200:
                        break

                    data = resp.json()
                    series_list = data.get("seriess", [])
                    if not series_list:
                        break

                    for s in series_list:
                        indicators.append(Indicator(
                            provider="FRED",
                            code=s.get("id", ""),
                            name=s.get("title", ""),
                            description=s.get("notes", ""),
                            category=cat_name,
                            unit=s.get("units", ""),
                            frequency=s.get("frequency", ""),
                            start_date=s.get("observation_start"),
                            end_date=s.get("observation_end"),
                            popularity=s.get("popularity", 0),
                            keywords=f"{s.get('title', '')} {s.get('id', '')}".lower(),
                            last_updated=s.get("last_updated"),
                            raw_metadata=json.dumps(s),
                        ))

                    fetched_in_cat += len(series_list)
                    offset += limit

                    # Check if we've fetched all available
                    total_available = data.get("count", 0)
                    if offset >= total_available:
                        break

                    await asyncio.sleep(0.15)  # Rate limit

                logger.info(f"    -> Fetched {fetched_in_cat} from {cat_name}")

            except Exception as e:
                logger.error(f"Error fetching FRED category {cat_id}: {e}")

        # Also fetch via search for additional coverage
        logger.info("  Fetching FRED popular series via search...")
        search_terms = [
            "GDP", "unemployment", "inflation", "CPI", "interest rate",
            "housing", "employment", "trade", "manufacturing", "retail",
            "wages", "income", "debt", "deficit", "exports", "imports",
            "consumer", "business", "investment", "savings", "productivity",
            "oil price", "gas price", "commodity", "gold", "exchange rate",
            "federal funds", "treasury", "mortgage", "credit", "loans",
            "bank", "financial", "stock", "market", "index",
        ]

        try:
            for search_term in search_terms:
                resp = await self.client.get(
                    "https://api.stlouisfed.org/fred/series/search",
                    params={
                        "search_text": search_term,
                        "api_key": api_key,
                        "file_type": "json",
                        "limit": 1000,
                        "order_by": "popularity",
                        "sort_order": "desc",
                    }
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for s in data.get("seriess", []):
                        indicators.append(Indicator(
                            provider="FRED",
                            code=s.get("id", ""),
                            name=s.get("title", ""),
                            description=s.get("notes", ""),
                            category="Search: " + search_term,
                            unit=s.get("units", ""),
                            frequency=s.get("frequency", ""),
                            start_date=s.get("observation_start"),
                            end_date=s.get("observation_end"),
                            popularity=s.get("popularity", 0),
                            keywords=f"{s.get('title', '')} {search_term}".lower(),
                            last_updated=s.get("last_updated"),
                            raw_metadata=json.dumps(s),
                        ))
                await asyncio.sleep(0.2)
        except Exception as e:
            logger.error(f"Error searching FRED: {e}")

        # Deduplicate by code
        seen = set()
        unique_indicators = []
        for ind in indicators:
            if ind.code not in seen:
                seen.add(ind.code)
                unique_indicators.append(ind)

        # Insert into database
        count = self.db.insert_batch(unique_indicators)
        duration = time.time() - start_time
        self.db.update_provider_stats("FRED", count, "full", duration,
                                      f"Fetched from {len(categories)} categories + {len(search_terms)} search terms")

        logger.info(f"  FRED: Inserted {count} series in {duration:.1f}s")
        return count

    # =========================================================================
    # World Bank
    # =========================================================================
    async def fetch_worldbank(self) -> int:
        """Fetch ALL World Bank indicators (29,323+)."""
        logger.info("Fetching World Bank indicators...")
        start_time = time.time()
        indicators = []

        page = 1
        per_page = 500
        total_pages = 1

        while page <= total_pages:
            try:
                resp = await self.client.get(
                    "https://api.worldbank.org/v2/indicator",
                    params={
                        "format": "json",
                        "per_page": per_page,
                        "page": page,
                    }
                )
                if resp.status_code != 200:
                    break

                data = resp.json()
                if not data or len(data) < 2:
                    break

                meta = data[0]
                total_pages = meta.get("pages", 1)
                items = data[1] if len(data) > 1 else []

                for item in items:
                    source = item.get("source", {})
                    topics = item.get("topics", [])
                    topic_names = " ".join([t.get("value", "") for t in topics if t])

                    indicators.append(Indicator(
                        provider="WorldBank",
                        code=item.get("id", ""),
                        name=item.get("name", ""),
                        description=item.get("sourceNote", ""),
                        category=source.get("value", "") if source else "",
                        subcategory=topic_names,
                        unit=item.get("unit", ""),
                        keywords=f"{item.get('name', '')} {item.get('id', '')} {topic_names}".lower(),
                        raw_metadata=json.dumps(item),
                    ))

                logger.info(f"  World Bank: Page {page}/{total_pages} ({len(items)} indicators)")
                page += 1
                await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"Error fetching World Bank page {page}: {e}")
                break

        count = self.db.insert_batch(indicators)
        duration = time.time() - start_time
        self.db.update_provider_stats("WorldBank", count, "full", duration)

        logger.info(f"  World Bank: Inserted {count} indicators in {duration:.1f}s")
        return count

    # =========================================================================
    # IMF
    # =========================================================================
    async def fetch_imf(self) -> int:
        """
        Fetch IMF indicators from:
        1. SDMX Central codelists (113,631+ indicator codes)
        2. DataMapper API (133 indicators with descriptions)
        3. Dataflows (210 datasets)
        """
        logger.info("Fetching IMF indicators...")
        start_time = time.time()
        indicators = []

        # We need to use sync httpx for large XML parsing
        import httpx as sync_httpx
        import xml.etree.ElementTree as ET

        sync_client = sync_httpx.Client(timeout=300.0)

        ns = {
            'message': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message',
            'structure': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure',
            'common': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common'
        }

        # 1. Fetch ALL codelists from SDMX Central (66MB, contains 113K+ indicator codes)
        logger.info("  Fetching IMF SDMX Central codelists (this may take a minute)...")
        try:
            resp = sync_client.get('https://sdmxcentral.imf.org/ws/public/sdmxapi/rest/codelist')
            if resp.status_code == 200:
                logger.info(f"  Downloaded {len(resp.text) / 1024 / 1024:.1f} MB, parsing XML...")
                root = ET.fromstring(resp.text)

                # Find indicator-related codelists
                codelists = root.findall('.//structure:Codelist', ns)
                logger.info(f"  Found {len(codelists)} codelists")

                for cl in codelists:
                    cl_id = cl.get('id', 'unknown')

                    # Only process indicator-related codelists
                    if 'INDICATOR' in cl_id.upper():
                        codes = cl.findall('.//structure:Code', ns)

                        for code in codes:
                            code_id = code.get('id', '')
                            if not code_id:
                                continue

                            # Get name/description
                            name_elem = code.find('.//common:Name', ns)
                            name = name_elem.text if name_elem is not None else code_id

                            desc_elem = code.find('.//common:Description', ns)
                            desc = desc_elem.text if desc_elem is not None else ""

                            indicators.append(Indicator(
                                provider="IMF",
                                code=code_id,
                                name=name or code_id,
                                description=desc or name,
                                category=cl_id.replace('CL_', '').replace('_INDICATOR', ''),
                                keywords=f"{name} {code_id} imf".lower(),
                            ))

                        logger.info(f"    {cl_id}: {len(codes)} codes")

                logger.info(f"  IMF SDMX indicators: {len(indicators)} total")
        except Exception as e:
            logger.error(f"Error fetching IMF SDMX codelists: {e}")

        # 2. Fetch dataflows to add as datasets
        logger.info("  Fetching IMF dataflows...")
        try:
            resp = sync_client.get('https://sdmxcentral.imf.org/ws/public/sdmxapi/rest/dataflow')
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                dataflows = root.findall('.//structure:Dataflow', ns)

                existing_codes = {ind.code for ind in indicators}
                df_added = 0
                for df in dataflows:
                    df_id = df.get('id', 'unknown')
                    if df_id in existing_codes:
                        continue

                    name_elem = df.find('.//common:Name', ns)
                    df_name = name_elem.text if name_elem is not None else df_id

                    indicators.append(Indicator(
                        provider="IMF",
                        code=f"DF:{df_id}",
                        name=f"Dataset: {df_name}",
                        description=f"IMF Dataflow: {df_name}",
                        category="Dataflow",
                        keywords=f"{df_name} {df_id} imf dataset dataflow".lower(),
                    ))
                    df_added += 1

                logger.info(f"  IMF dataflows added: {df_added}")
        except Exception as e:
            logger.error(f"Error fetching IMF dataflows: {e}")

        # 3. Also fetch DataMapper API for better descriptions on common indicators
        logger.info("  Fetching IMF DataMapper API...")
        try:
            resp = await self.client.get(
                "https://www.imf.org/external/datamapper/api/v1/indicators",
                timeout=60.0
            )
            if resp.status_code == 200:
                data = resp.json()
                existing_codes = {ind.code for ind in indicators}
                dm_added = 0

                for code, info in data.get("indicators", {}).items():
                    if code not in existing_codes:
                        indicators.append(Indicator(
                            provider="IMF",
                            code=code,
                            name=info.get("label", code),
                            description=info.get("description", ""),
                            category=info.get("dataset", "DataMapper"),
                            unit=info.get("unit", ""),
                            keywords=f"{info.get('label', '')} {code} imf datamapper".lower(),
                            raw_metadata=json.dumps(info),
                        ))
                        dm_added += 1
                    else:
                        # Update existing with better description if available
                        for ind in indicators:
                            if ind.code == code and info.get("description"):
                                ind.description = info.get("description")
                                if info.get("unit"):
                                    ind.unit = info.get("unit")
                                break

                logger.info(f"  IMF DataMapper: {dm_added} new + updated existing")
        except Exception as e:
            logger.error(f"Error fetching IMF DataMapper: {e}")

        sync_client.close()

        # Deduplicate by code
        seen = set()
        unique_indicators = []
        for ind in indicators:
            if ind.code and ind.code not in seen:
                seen.add(ind.code)
                unique_indicators.append(ind)

        count = self.db.insert_batch(unique_indicators)
        duration = time.time() - start_time
        self.db.update_provider_stats("IMF", count, "full", duration,
                                      "SDMX Central codelists + DataMapper")

        logger.info(f"  IMF: Inserted {count} indicators in {duration:.1f}s")
        return count

    # =========================================================================
    # Eurostat
    # =========================================================================
    async def fetch_eurostat(self) -> int:
        """Fetch ALL Eurostat datasets (8,118+)."""
        logger.info("Fetching Eurostat datasets...")
        start_time = time.time()
        indicators = []

        try:
            resp = await self.client.get(
                "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/dataflow/ESTAT",
                params={"format": "json"},
                timeout=120.0
            )
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("link", {}).get("item", [])

                for item in items:
                    ext = item.get("extension", {})
                    indicators.append(Indicator(
                        provider="Eurostat",
                        code=ext.get("id", ""),
                        name=item.get("label", ""),
                        description=item.get("label", ""),
                        category="Eurostat Dataset",
                        coverage="EU",
                        keywords=f"{item.get('label', '')} {ext.get('id', '')}".lower(),
                        raw_metadata=json.dumps(item),
                    ))

                logger.info(f"  Eurostat: Found {len(items)} datasets")
        except Exception as e:
            logger.error(f"Error fetching Eurostat: {e}")

        count = self.db.insert_batch(indicators)
        duration = time.time() - start_time
        self.db.update_provider_stats("Eurostat", count, "full", duration)

        logger.info(f"  Eurostat: Inserted {count} datasets in {duration:.1f}s")
        return count

    # =========================================================================
    # Statistics Canada
    # =========================================================================
    async def fetch_statscan(self) -> int:
        """Fetch ALL Statistics Canada tables (8,058+)."""
        logger.info("Fetching Statistics Canada tables...")
        start_time = time.time()
        indicators = []

        try:
            resp = await self.client.get(
                "https://www150.statcan.gc.ca/t1/wds/rest/getAllCubesListLite",
                timeout=120.0
            )
            if resp.status_code == 200:
                data = resp.json()
                for item in data:
                    # Product ID is the main identifier
                    product_id = str(item.get("productId", ""))
                    cube_id = str(item.get("cansimId", product_id))

                    indicators.append(Indicator(
                        provider="StatsCan",
                        code=product_id,
                        name=item.get("cubeTitleEn", ""),
                        description=item.get("cubeTitleEn", ""),
                        category=item.get("subjectCodeEn", ""),
                        subcategory=item.get("surveyEn", ""),
                        frequency=item.get("frequencyCode", ""),
                        coverage="Canada",
                        start_date=item.get("cubeStartDate"),
                        end_date=item.get("cubeEndDate"),
                        keywords=f"{item.get('cubeTitleEn', '')} {product_id} {cube_id}".lower(),
                        synonyms=cube_id if cube_id != product_id else None,
                        raw_metadata=json.dumps(item),
                    ))

                logger.info(f"  StatsCan: Found {len(data)} tables")
        except Exception as e:
            logger.error(f"Error fetching StatsCan: {e}")

        count = self.db.insert_batch(indicators)
        duration = time.time() - start_time
        self.db.update_provider_stats("StatsCan", count, "full", duration)

        logger.info(f"  StatsCan: Inserted {count} tables in {duration:.1f}s")
        return count

    # =========================================================================
    # OECD
    # =========================================================================
    async def fetch_oecd(self) -> int:
        """Fetch ALL OECD dataflows from SDMX API (1,463+ dataflows)."""
        logger.info("Fetching OECD dataflows from SDMX API...")
        start_time = time.time()
        indicators = []

        # OECD uses ALL agencies - fetch from the main dataflow endpoint
        try:
            resp = await self.client.get(
                "https://sdmx.oecd.org/public/rest/dataflow",
                headers={"Accept": "application/vnd.sdmx.structure+json;version=1.0"},
                timeout=120.0
            )
            if resp.status_code == 200:
                data = resp.json()
                dataflows = data.get("data", {}).get("dataflows", [])

                for df in dataflows:
                    df_id = df.get("id", "")
                    df_name = df.get("name", "")

                    # Handle name being a dict with language codes
                    if isinstance(df_name, dict):
                        df_name = df_name.get("en", str(df_name))

                    df_desc = df.get("description", "")
                    if isinstance(df_desc, dict):
                        df_desc = df_desc.get("en", str(df_desc))

                    indicators.append(Indicator(
                        provider="OECD",
                        code=df_id,
                        name=df_name or df_id,
                        description=df_desc or df_name,
                        category="OECD Dataflow",
                        keywords=f"{df_name} {df_id} oecd".lower(),
                        raw_metadata=json.dumps(df),
                    ))

                logger.info(f"  OECD: Found {len(dataflows)} dataflows from API")
        except Exception as e:
            logger.error(f"Error fetching OECD API: {e}")

        # Fallback to local cache if API failed
        if not indicators:
            logger.warning("  OECD: API failed, trying local cache")
            sdmx_path = Path(__file__).parent.parent / "backend/data/metadata/sdmx/oecd_dataflows.json"
            if sdmx_path.exists():
                try:
                    with open(sdmx_path) as f:
                        data = json.load(f)
                        dataflows = data.get("dataflows", [])
                        for df in dataflows:
                            indicators.append(Indicator(
                                provider="OECD",
                                code=df.get("id", ""),
                                name=df.get("name", ""),
                                description=df.get("description", df.get("name", "")),
                                category="OECD Dataflow",
                                keywords=f"{df.get('name', '')} {df.get('id', '')}".lower(),
                                raw_metadata=json.dumps(df),
                            ))
                        logger.info(f"  OECD: Loaded {len(dataflows)} dataflows from cache")
                except Exception as e:
                    logger.error(f"Error loading OECD cache: {e}")

        count = self.db.insert_batch(indicators)
        duration = time.time() - start_time
        self.db.update_provider_stats("OECD", count, "full", duration)

        logger.info(f"  OECD: Inserted {count} dataflows in {duration:.1f}s")
        return count

    # =========================================================================
    # BIS
    # =========================================================================
    async def fetch_bis(self) -> int:
        """Fetch ALL BIS dataflows from SDMX API (32 dataflows)."""
        logger.info("Fetching BIS dataflows from SDMX API...")
        start_time = time.time()
        indicators = []

        import httpx as sync_httpx
        import xml.etree.ElementTree as ET

        sync_client = sync_httpx.Client(timeout=60.0)

        ns = {
            'message': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message',
            'structure': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure',
            'common': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common'
        }

        # Fetch from BIS SDMX API
        try:
            resp = sync_client.get('https://stats.bis.org/api/v1/dataflow')
            if resp.status_code == 200:
                root = ET.fromstring(resp.text)
                dataflows = root.findall('.//structure:Dataflow', ns)

                for df in dataflows:
                    df_id = df.get('id', 'unknown')
                    names = df.findall('.//common:Name', ns)
                    df_name = names[0].text if names else df_id

                    indicators.append(Indicator(
                        provider="BIS",
                        code=df_id,
                        name=df_name,
                        description=f"BIS Dataflow: {df_name}",
                        category="BIS Statistics",
                        keywords=f"{df_name} {df_id} bis banking central bank".lower(),
                    ))

                logger.info(f"  BIS: Found {len(dataflows)} dataflows from API")
        except Exception as e:
            logger.error(f"Error fetching BIS dataflows: {e}")

        sync_client.close()

        # Fallback to local file if API failed
        if len(indicators) == 0:
            bis_path = Path(__file__).parent.parent / "backend/data/metadata/bis.json"
            if bis_path.exists():
                try:
                    with open(bis_path) as f:
                        data = json.load(f)
                        for df in data.get("dataflows", data.get("indicators", [])):
                            indicators.append(Indicator(
                                provider="BIS",
                                code=df.get("id", df.get("code", "")),
                                name=df.get("name", df.get("title", "")),
                                description=df.get("description", ""),
                                category="BIS Dataflow",
                                keywords=f"{df.get('name', '')} {df.get('id', '')}".lower(),
                                raw_metadata=json.dumps(df),
                            ))
                except Exception as e:
                    logger.error(f"Error loading BIS fallback: {e}")

        count = self.db.insert_batch(indicators)
        duration = time.time() - start_time
        self.db.update_provider_stats("BIS", count, "full", duration)

        logger.info(f"  BIS: Inserted {count} dataflows in {duration:.1f}s")
        return count

    # =========================================================================
    # CoinGecko
    # =========================================================================
    async def fetch_coingecko(self) -> int:
        """Fetch all CoinGecko cryptocurrencies."""
        logger.info("Fetching CoinGecko cryptocurrencies...")
        start_time = time.time()
        indicators = []

        try:
            resp = await self.client.get(
                "https://api.coingecko.com/api/v3/coins/list",
                timeout=60.0
            )
            if resp.status_code == 200:
                coins = resp.json()
                for coin in coins:
                    indicators.append(Indicator(
                        provider="CoinGecko",
                        code=coin.get("id", ""),
                        name=coin.get("name", ""),
                        description=f"Cryptocurrency: {coin.get('name', '')}",
                        category="Cryptocurrency",
                        unit="USD",
                        keywords=f"{coin.get('name', '')} {coin.get('symbol', '')} {coin.get('id', '')} crypto cryptocurrency".lower(),
                        synonyms=coin.get("symbol", ""),
                        raw_metadata=json.dumps(coin),
                    ))
                logger.info(f"  CoinGecko: Found {len(coins)} cryptocurrencies")
        except Exception as e:
            logger.error(f"Error fetching CoinGecko: {e}")

        count = self.db.insert_batch(indicators)
        duration = time.time() - start_time
        self.db.update_provider_stats("CoinGecko", count, "full", duration)

        logger.info(f"  CoinGecko: Inserted {count} cryptocurrencies in {duration:.1f}s")
        return count

    # =========================================================================
    # ExchangeRate currencies
    # =========================================================================
    async def fetch_exchangerate(self) -> int:
        """Fetch all supported currencies dynamically from API."""
        logger.info("Fetching ExchangeRate currencies from API...")
        start_time = time.time()

        # Currency name mappings for common currencies
        currency_names = {
            "USD": "United States Dollar", "EUR": "Euro", "GBP": "British Pound Sterling",
            "JPY": "Japanese Yen", "CHF": "Swiss Franc", "CAD": "Canadian Dollar",
            "AUD": "Australian Dollar", "NZD": "New Zealand Dollar", "CNY": "Chinese Yuan",
            "HKD": "Hong Kong Dollar", "SGD": "Singapore Dollar", "KRW": "South Korean Won",
            "INR": "Indian Rupee", "MXN": "Mexican Peso", "BRL": "Brazilian Real",
            "ZAR": "South African Rand", "RUB": "Russian Ruble", "TRY": "Turkish Lira",
            "PLN": "Polish Zloty", "SEK": "Swedish Krona", "NOK": "Norwegian Krone",
            "DKK": "Danish Krone", "THB": "Thai Baht", "MYR": "Malaysian Ringgit",
            "IDR": "Indonesian Rupiah", "PHP": "Philippine Peso", "VND": "Vietnamese Dong",
            "TWD": "Taiwan Dollar", "AED": "UAE Dirham", "SAR": "Saudi Riyal",
            "ILS": "Israeli Shekel", "EGP": "Egyptian Pound", "PKR": "Pakistani Rupee",
            "BDT": "Bangladeshi Taka", "NGN": "Nigerian Naira", "KES": "Kenyan Shilling",
            "GHS": "Ghanaian Cedi", "CZK": "Czech Koruna", "HUF": "Hungarian Forint",
            "RON": "Romanian Leu", "BGN": "Bulgarian Lev", "HRK": "Croatian Kuna",
            "ISK": "Icelandic Krona", "CLP": "Chilean Peso", "COP": "Colombian Peso",
            "PEN": "Peruvian Sol", "ARS": "Argentine Peso", "UAH": "Ukrainian Hryvnia",
            "KZT": "Kazakhstani Tenge", "QAR": "Qatari Riyal", "KWD": "Kuwaiti Dinar",
            "BHD": "Bahraini Dinar", "OMR": "Omani Rial", "JOD": "Jordanian Dinar",
            "LBP": "Lebanese Pound", "MAD": "Moroccan Dirham", "TND": "Tunisian Dinar",
            "DZD": "Algerian Dinar", "LYD": "Libyan Dinar", "SDG": "Sudanese Pound",
            "ETB": "Ethiopian Birr", "TZS": "Tanzanian Shilling", "UGX": "Ugandan Shilling",
            "ZMW": "Zambian Kwacha", "BWP": "Botswanan Pula", "MUR": "Mauritian Rupee",
            "SCR": "Seychellois Rupee", "XOF": "West African CFA Franc",
            "XAF": "Central African CFA Franc", "XCD": "East Caribbean Dollar",
            "BBD": "Barbadian Dollar", "BSD": "Bahamian Dollar", "JMD": "Jamaican Dollar",
            "TTD": "Trinidad Dollar", "HTG": "Haitian Gourde", "DOP": "Dominican Peso",
            "CRC": "Costa Rican Colon", "GTQ": "Guatemalan Quetzal", "HNL": "Honduran Lempira",
            "NIO": "Nicaraguan Cordoba", "PAB": "Panamanian Balboa", "PYG": "Paraguayan Guarani",
            "UYU": "Uruguayan Peso", "BOB": "Bolivian Boliviano", "VES": "Venezuelan Bolivar",
            "SRD": "Surinamese Dollar", "GYD": "Guyanese Dollar", "FJD": "Fijian Dollar",
            "PGK": "Papua New Guinean Kina", "WST": "Samoan Tala", "TOP": "Tongan Paanga",
            "VUV": "Vanuatu Vatu", "SBD": "Solomon Islands Dollar", "LKR": "Sri Lankan Rupee",
            "NPR": "Nepalese Rupee", "MMK": "Myanmar Kyat", "KHR": "Cambodian Riel",
            "LAK": "Lao Kip", "BND": "Brunei Dollar", "MOP": "Macanese Pataca",
            "MNT": "Mongolian Tugrik", "AFN": "Afghan Afghani", "IRR": "Iranian Rial",
            "IQD": "Iraqi Dinar", "SYP": "Syrian Pound", "YER": "Yemeni Rial",
            "GEL": "Georgian Lari", "AMD": "Armenian Dram", "AZN": "Azerbaijani Manat",
            "BYN": "Belarusian Ruble", "MDL": "Moldovan Leu", "ALL": "Albanian Lek",
            "MKD": "Macedonian Denar", "RSD": "Serbian Dinar", "BAM": "Bosnia Convertible Mark",
        }

        indicators = []

        # Fetch live currencies from API to get the full list
        import httpx as sync_httpx
        try:
            sync_client = sync_httpx.Client(timeout=30.0)
            resp = sync_client.get('https://open.er-api.com/v6/latest/USD')
            if resp.status_code == 200:
                data = resp.json()
                rates = data.get('rates', {})
                logger.info(f"  Found {len(rates)} currencies from API")

                for code in rates.keys():
                    # Get name from mapping or generate a default
                    name = currency_names.get(code, f"{code} Currency")
                    indicators.append(Indicator(
                        provider="ExchangeRate",
                        code=code,
                        name=name,
                        description=f"Exchange rate for {name}",
                        category="Currency",
                        unit="Rate",
                        keywords=f"{name} {code} currency exchange rate forex".lower(),
                        synonyms=code,
                    ))
            sync_client.close()
        except Exception as e:
            logger.error(f"Failed to fetch currencies from API: {e}")
            # Fallback to basic currencies if API fails
            for code, name in list(currency_names.items())[:50]:
                indicators.append(Indicator(
                    provider="ExchangeRate",
                    code=code,
                    name=name,
                    description=f"Exchange rate for {name}",
                    category="Currency",
                    unit="Rate",
                    keywords=f"{name} {code} currency exchange rate forex".lower(),
                    synonyms=code,
                ))

        count = self.db.insert_batch(indicators)
        duration = time.time() - start_time
        self.db.update_provider_stats("ExchangeRate", count, "full", duration)

        logger.info(f"  ExchangeRate: Inserted {count} currencies in {duration:.1f}s")
        return count

    # =========================================================================
    # Comtrade HS Codes
    # =========================================================================
    async def fetch_comtrade(self) -> int:
        """Fetch ALL Comtrade HS product codes (8,000+ codes)."""
        logger.info("Fetching Comtrade HS codes from API...")
        start_time = time.time()
        indicators = []

        # Use sync client for the fetch
        import httpx as sync_httpx
        sync_client = sync_httpx.Client(timeout=120.0)

        # Fetch from UN Comtrade classification API
        try:
            resp = sync_client.get('https://comtrade.un.org/data/cache/classificationHS.json')
            if resp.status_code == 200:
                data = resp.json()
                hs_codes = data.get('results', [])
                logger.info(f"  Found {len(hs_codes)} HS codes from Comtrade API")

                for item in hs_codes:
                    code = item.get('id', '')
                    text = item.get('text', '')

                    if not code or not text:
                        continue

                    # Determine category based on code length
                    if len(code) == 2:
                        category = "HS Chapter"
                    elif len(code) == 4:
                        category = "HS Heading"
                    elif len(code) == 6:
                        category = "HS Subheading"
                    else:
                        category = "HS Code"

                    indicators.append(Indicator(
                        provider="Comtrade",
                        code=code,
                        name=text,
                        description=f"HS Code {code}: {text}",
                        category=category,
                        subcategory="Trade Classification",
                        keywords=f"{text} {code} hs trade export import".lower(),
                    ))

        except Exception as e:
            logger.error(f"Error fetching Comtrade HS codes from API: {e}")

        sync_client.close()

        # Fallback to hardcoded chapters if API failed
        if len(indicators) < 50:
            logger.warning("  API failed, using hardcoded HS chapters as fallback")
            # Add basic HS chapters (97 total)
            for i in range(1, 98):
                code = f"{i:02d}"
                indicators.append(Indicator(
                    provider="Comtrade",
                    code=code,
                    name=f"HS Chapter {code}",
                    description=f"HS Chapter {code}",
                    category="HS Chapter",
                    subcategory="Trade Classification",
                    keywords=f"hs chapter {code} trade export import".lower(),
                ))

        count = self.db.insert_batch(indicators)
        duration = time.time() - start_time
        self.db.update_provider_stats("Comtrade", count, "full", duration,
                                      f"Fetched {count} HS codes from API")

        logger.info(f"  Comtrade: Inserted {count} HS codes in {duration:.1f}s")
        return count

    # =========================================================================
    # Main fetch method
    # =========================================================================
    async def fetch_all(self, providers: Optional[List[str]] = None) -> Dict[str, int]:
        """Fetch indicators from all or specified providers."""
        all_providers = {
            "FRED": self.fetch_fred,
            "WorldBank": self.fetch_worldbank,
            "IMF": self.fetch_imf,
            "Eurostat": self.fetch_eurostat,
            "StatsCan": self.fetch_statscan,
            "OECD": self.fetch_oecd,
            "BIS": self.fetch_bis,
            "CoinGecko": self.fetch_coingecko,
            "ExchangeRate": self.fetch_exchangerate,
            "Comtrade": self.fetch_comtrade,
        }

        providers_to_fetch = providers or list(all_providers.keys())
        results = {}

        for provider in providers_to_fetch:
            if provider in all_providers:
                try:
                    count = await all_providers[provider]()
                    results[provider] = count
                except Exception as e:
                    logger.error(f"Error fetching {provider}: {e}")
                    results[provider] = 0
            else:
                logger.warning(f"Unknown provider: {provider}")

        return results


async def main():
    parser = argparse.ArgumentParser(description="Fetch all indicators from providers")
    parser.add_argument("--provider", "-p", type=str, help="Specific provider to fetch")
    parser.add_argument("--stats", "-s", action="store_true", help="Show current stats")
    parser.add_argument("--verify", "-v", action="store_true", help="Verify completeness")
    args = parser.parse_args()

    db = get_indicator_database()

    if args.stats:
        print("\n=== Indicator Database Statistics ===\n")
        stats = db.get_provider_stats()
        total = 0
        for provider, info in sorted(stats.items(), key=lambda x: x[1].get('count', 0), reverse=True):
            count = info.get('count', 0)
            total += count
            last_fetch = info.get('last_full_fetch', 'Never')
            print(f"  {provider:15} {count:>10,} indicators  (last fetch: {last_fetch or 'Never'})")
        print(f"\n  {'TOTAL':15} {total:>10,} indicators")
        return

    if args.verify:
        print("\n=== Verifying Indicator Database Completeness ===\n")
        stats = db.get_provider_stats()

        expected = {
            "FRED": 130000,  # Target for all major categories
            "WorldBank": 29323,
            "IMF": 116000,  # SDMX Central has 116K+ indicator codes
            "Eurostat": 8118,
            "StatsCan": 8058,
            "OECD": 1463,  # All OECD dataflows
            "BIS": 32,  # All BIS dataflows
            "CoinGecko": 19079,  # All cryptocurrencies
            "ExchangeRate": 50,
            "Comtrade": 8267,  # All HS codes from API
        }

        for provider, target in expected.items():
            current = stats.get(provider, {}).get('count', 0)
            pct = (current / target * 100) if target > 0 else 0
            status = "✅" if pct >= 90 else "⚠️" if pct >= 50 else "❌"
            print(f"  {status} {provider:15} {current:>10,} / {target:>10,} ({pct:5.1f}%)")
        return

    # Fetch indicators
    fetcher = IndicatorFetcher(db)
    try:
        providers = [args.provider] if args.provider else None
        print(f"\n=== Fetching Indicators {'for ' + args.provider if args.provider else 'from All Providers'} ===\n")

        results = await fetcher.fetch_all(providers)

        print("\n=== Fetch Results ===\n")
        total = 0
        for provider, count in sorted(results.items(), key=lambda x: x[1], reverse=True):
            print(f"  {provider:15} {count:>10,} indicators")
            total += count
        print(f"\n  {'TOTAL':15} {total:>10,} indicators")

    finally:
        await fetcher.close()


if __name__ == "__main__":
    asyncio.run(main())
