#!/usr/bin/env python3
"""
Catalog Indexer Service - Downloads and indexes metadata from all providers

This service downloads indicator catalogs from all data providers and stores
them in a local SQLite database with full-text search support.
"""

import sqlite3
import os
import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from datetime import datetime
import httpx

logger = logging.getLogger(__name__)


class CatalogIndexer(ABC):
    """Base class for catalog indexing"""

    def __init__(self, db_path: str):
        """
        Initialize the catalog indexer

        Args:
            db_path: Path to the SQLite database
        """
        self.db_path = db_path
        self.provider_name = self.__class__.__name__.replace("Indexer", "").upper()

    @abstractmethod
    async def fetch_indicators(self) -> List[Dict[str, Any]]:
        """
        Fetch indicators from the provider API

        Returns:
            List of indicator dictionaries with keys:
            - code: Unique indicator code
            - name: Human-readable name
            - description: Detailed description
            - unit: Unit of measurement
            - frequency: Data frequency (annual, quarterly, monthly, etc.)
            - geo_coverage: Geographic coverage (country, region, global)
            - start_date: Start date (ISO format)
            - end_date: End date (ISO format)
            - keywords: Space-separated keywords for search
            - category: Indicator category
            - metadata_json: JSON string with additional metadata
        """
        pass

    def _extract_keywords(self, name: str, description: str, category: str) -> str:
        """
        Extract searchable keywords from indicator metadata

        Args:
            name: Indicator name
            description: Indicator description
            category: Indicator category

        Returns:
            Space-separated keywords
        """
        # Simple keyword extraction - can be improved with NLP
        keywords = []

        # Add words from name
        if name:
            keywords.extend(name.lower().split())

        # Add category
        if category:
            keywords.extend(category.lower().split())

        # Remove duplicates and common stop words
        stop_words = {"the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or"}
        keywords = [k for k in set(keywords) if k not in stop_words]

        return " ".join(keywords)

    async def index(self) -> Dict[str, Any]:
        """
        Run the indexing process

        Returns:
            Dictionary with indexing results:
            - success: bool
            - indicators_indexed: int
            - error: str (if failed)
        """
        logger.info(f"Starting indexing for {self.provider_name}")

        try:
            # Fetch indicators from API
            indicators = await self.fetch_indicators()
            logger.info(f"Fetched {len(indicators)} indicators from {self.provider_name}")

            # Insert into database
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Clear old data for this provider
            cursor.execute("DELETE FROM indicators WHERE provider = ?", (self.provider_name,))

            # Insert new data
            inserted = 0
            for indicator in indicators:
                try:
                    cursor.execute(
                        """
                        INSERT INTO indicators (
                            provider, code, name, description, unit, frequency,
                            geo_coverage, start_date, end_date, keywords, category,
                            popularity_score, data_quality_score, metadata_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            self.provider_name,
                            indicator["code"],
                            indicator["name"],
                            indicator.get("description", ""),
                            indicator.get("unit", ""),
                            indicator.get("frequency", ""),
                            indicator.get("geo_coverage", ""),
                            indicator.get("start_date", ""),
                            indicator.get("end_date", ""),
                            indicator.get("keywords", ""),
                            indicator.get("category", ""),
                            indicator.get("popularity_score", 0.0),
                            indicator.get("data_quality_score", 0.0),
                            indicator.get("metadata_json", "{}"),
                        ),
                    )
                    inserted += 1
                except sqlite3.IntegrityError:
                    logger.warning(
                        f"Duplicate indicator: {self.provider_name}:{indicator['code']}"
                    )

            # Update metadata table
            cursor.execute(
                """
                INSERT OR REPLACE INTO index_metadata (provider, last_indexed, indicator_count, status)
                VALUES (?, ?, ?, ?)
                """,
                (self.provider_name, datetime.utcnow().isoformat(), inserted, "success"),
            )

            conn.commit()
            conn.close()

            logger.info(f"Successfully indexed {inserted} indicators for {self.provider_name}")

            return {
                "success": True,
                "indicators_indexed": inserted,
                "provider": self.provider_name,
            }

        except Exception as e:
            logger.error(f"Error indexing {self.provider_name}: {e}", exc_info=True)

            # Record error in metadata
            try:
                conn = sqlite3.connect(self.db_path)
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO index_metadata (provider, last_indexed, indicator_count, status, error_message)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        self.provider_name,
                        datetime.utcnow().isoformat(),
                        0,
                        "error",
                        str(e),
                    ),
                )
                conn.commit()
                conn.close()
            except Exception as db_error:
                logger.error(f"Error recording failure: {db_error}")

            return {"success": False, "error": str(e), "provider": self.provider_name}


class FREDIndexer(CatalogIndexer):
    """FRED (Federal Reserve Economic Data) catalog indexer"""

    def __init__(self, db_path: str, api_key: Optional[str] = None):
        super().__init__(db_path)
        self.api_key = api_key or os.getenv("FRED_API_KEY")

    async def fetch_indicators(self) -> List[Dict[str, Any]]:
        """Fetch FRED series catalog"""

        if not self.api_key:
            logger.warning("FRED API key not found - indexing will be limited")
            return []

        indicators = []

        # FRED has thousands of series - we'll fetch the most popular categories
        # To get ALL series, we'd need to paginate through all results
        categories = ["gdp", "inflation", "unemployment", "interest", "money", "trade"]

        async with httpx.AsyncClient(timeout=30.0) as client:
            for category in categories:
                try:
                    url = "https://api.stlouisfed.org/fred/series/search"
                    params = {
                        "search_text": category,
                        "api_key": self.api_key,
                        "file_type": "json",
                        "limit": 1000,  # Max per request
                    }

                    response = await client.get(url, params=params)
                    response.raise_for_status()
                    data = response.json()

                    for series in data.get("seriess", []):
                        indicators.append({
                            "code": series["id"],
                            "name": series.get("title", ""),
                            "description": series.get("notes", ""),
                            "unit": series.get("units", ""),
                            "frequency": series.get("frequency", ""),
                            "geo_coverage": "United States",
                            "start_date": series.get("observation_start", ""),
                            "end_date": series.get("observation_end", ""),
                            "keywords": self._extract_keywords(
                                series.get("title", ""),
                                series.get("notes", ""),
                                category,
                            ),
                            "category": category,
                            "popularity_score": float(series.get("popularity", 0)),
                            "metadata_json": "{}",
                        })

                    logger.info(f"Fetched {len(data.get('seriess', []))} FRED series for category: {category}")

                except Exception as e:
                    logger.error(f"Error fetching FRED category {category}: {e}")

        return indicators


class WorldBankIndexer(CatalogIndexer):
    """World Bank catalog indexer"""

    async def fetch_indicators(self) -> List[Dict[str, Any]]:
        """Fetch World Bank indicators catalog"""

        indicators = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            # World Bank API supports pagination
            page = 1
            per_page = 1000

            while True:
                try:
                    url = f"https://api.worldbank.org/v2/indicator"
                    params = {
                        "format": "json",
                        "per_page": per_page,
                        "page": page,
                    }

                    response = await client.get(url, params=params)
                    response.raise_for_status()
                    data = response.json()

                    if len(data) < 2 or not data[1]:
                        break

                    for indicator in data[1]:
                        indicators.append({
                            "code": indicator["id"],
                            "name": indicator.get("name", ""),
                            "description": indicator.get("sourceNote", ""),
                            "unit": indicator.get("unit", ""),
                            "frequency": "annual",  # World Bank is mostly annual
                            "geo_coverage": "global",
                            "start_date": "",
                            "end_date": "",
                            "keywords": self._extract_keywords(
                                indicator.get("name", ""),
                                indicator.get("sourceNote", ""),
                                indicator.get("topics", [{}])[0].get("value", "") if indicator.get("topics") else "",
                            ),
                            "category": indicator.get("topics", [{}])[0].get("value", "") if indicator.get("topics") else "",
                            "metadata_json": "{}",
                        })

                    logger.info(f"Fetched page {page} with {len(data[1])} World Bank indicators")
                    page += 1

                    # Check if we got fewer results than requested (last page)
                    if len(data[1]) < per_page:
                        break

                except Exception as e:
                    logger.error(f"Error fetching World Bank indicators page {page}: {e}")
                    break

        return indicators


class StatsCanIndexer(CatalogIndexer):
    """Statistics Canada catalog indexer"""

    async def fetch_indicators(self) -> List[Dict[str, Any]]:
        """Fetch Statistics Canada data cubes catalog"""

        indicators = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                url = "https://www150.statcan.gc.ca/t1/wds/rest/getAllCubesListLite"
                params = [("lang", "en")]

                response = await client.get(url, params=params)
                response.raise_for_status()
                data = response.json()

                for cube in data:
                    indicators.append({
                        "code": str(cube["productId"]),
                        "name": cube.get("cubeTitleEn", ""),
                        "description": cube.get("cubeStartDate", "") + " to " + cube.get("cubeEndDate", ""),
                        "unit": "",
                        "frequency": "varies",
                        "geo_coverage": "Canada",
                        "start_date": cube.get("cubeStartDate", ""),
                        "end_date": cube.get("cubeEndDate", ""),
                        "keywords": self._extract_keywords(
                            cube.get("cubeTitleEn", ""),
                            "",
                            "",
                        ),
                        "category": "",
                        "metadata_json": "{}",
                    })

                logger.info(f"Fetched {len(data)} Statistics Canada data cubes")

            except Exception as e:
                logger.error(f"Error fetching Statistics Canada catalog: {e}")

        return indicators


class IMFIndexer(CatalogIndexer):
    """IMF (International Monetary Fund) catalog indexer"""

    async def fetch_indicators(self) -> List[Dict[str, Any]]:
        """Fetch IMF indicators catalog"""

        indicators = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                # IMF Metadata API
                url = "https://www.imf.org/external/datamapper/api/v1/indicators"

                response = await client.get(url)
                response.raise_for_status()
                data = response.json()

                indicators_data = data.get("indicators", {})

                for code, info in indicators_data.items():
                    indicators.append({
                        "code": code,
                        "name": info.get("label", ""),
                        "description": info.get("description", ""),
                        "unit": info.get("unit", ""),
                        "frequency": "annual",
                        "geo_coverage": "global",
                        "start_date": "",
                        "end_date": "",
                        "keywords": self._extract_keywords(
                            info.get("label", ""),
                            info.get("description", ""),
                            "",
                        ),
                        "category": "",
                        "metadata_json": "{}",
                    })

                logger.info(f"Fetched {len(indicators)} IMF indicators")

            except Exception as e:
                logger.error(f"Error fetching IMF catalog: {e}")

        return indicators


class BISIndexer(CatalogIndexer):
    """BIS (Bank for International Settlements) catalog indexer"""

    async def fetch_indicators(self) -> List[Dict[str, Any]]:
        """Fetch BIS dataflows catalog"""

        indicators = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                url = "https://stats.bis.org/api/v1/dataflow/BIS"

                response = await client.get(url)
                response.raise_for_status()

                # BIS returns XML, we need to parse it
                # For simplicity, we'll use a basic approach
                # In production, use xml.etree.ElementTree or lxml

                text = response.text

                # Very basic XML parsing (can be improved)
                import re

                dataflow_pattern = r'<Dataflow id="([^"]+)"[^>]*>.*?<Name[^>]*>([^<]+)</Name>'
                matches = re.findall(dataflow_pattern, text, re.DOTALL)

                for code, name in matches:
                    indicators.append({
                        "code": code,
                        "name": name.strip(),
                        "description": "",
                        "unit": "",
                        "frequency": "varies",
                        "geo_coverage": "global",
                        "start_date": "",
                        "end_date": "",
                        "keywords": self._extract_keywords(name.strip(), "", ""),
                        "category": "financial",
                        "metadata_json": "{}",
                    })

                logger.info(f"Fetched {len(indicators)} BIS dataflows")

            except Exception as e:
                logger.error(f"Error fetching BIS catalog: {e}")

        return indicators


class EurostatIndexer(CatalogIndexer):
    """Eurostat catalog indexer"""

    async def fetch_indicators(self) -> List[Dict[str, Any]]:
        """Fetch Eurostat datasets catalog"""

        indicators = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                url = "https://ec.europa.eu/eurostat/api/dissemination/catalogue/toc/txt?lang=en"

                response = await client.get(url)
                response.raise_for_status()

                # Eurostat returns tab-separated text
                lines = response.text.strip().split("\n")

                for line in lines[1:]:  # Skip header
                    parts = line.split("\t")
                    if len(parts) >= 3:
                        code = parts[0]
                        name = parts[1]
                        data_type = parts[2]

                        indicators.append({
                            "code": code,
                            "name": name,
                            "description": "",
                            "unit": "",
                            "frequency": "varies",
                            "geo_coverage": "European Union",
                            "start_date": "",
                            "end_date": "",
                            "keywords": self._extract_keywords(name, "", data_type),
                            "category": data_type,
                            "metadata_json": "{}",
                        })

                logger.info(f"Fetched {len(indicators)} Eurostat datasets")

            except Exception as e:
                logger.error(f"Error fetching Eurostat catalog: {e}")

        return indicators


class CoinGeckoIndexer(CatalogIndexer):
    """CoinGecko cryptocurrency catalog indexer"""

    async def fetch_indicators(self) -> List[Dict[str, Any]]:
        """Fetch CoinGecko coins list"""

        indicators = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                url = "https://api.coingecko.com/api/v3/coins/list"

                response = await client.get(url)
                response.raise_for_status()
                data = response.json()

                for coin in data:
                    indicators.append({
                        "code": coin["id"],
                        "name": coin.get("name", ""),
                        "description": f"{coin.get('name', '')} ({coin.get('symbol', '').upper()})",
                        "unit": "USD",
                        "frequency": "daily",
                        "geo_coverage": "global",
                        "start_date": "",
                        "end_date": "",
                        "keywords": self._extract_keywords(
                            coin.get("name", ""),
                            coin.get("symbol", ""),
                            "cryptocurrency",
                        ),
                        "category": "cryptocurrency",
                        "metadata_json": "{}",
                    })

                logger.info(f"Fetched {len(indicators)} CoinGecko coins")

            except Exception as e:
                logger.error(f"Error fetching CoinGecko catalog: {e}")

        return indicators


class ComtradeIndexer(CatalogIndexer):
    """UN Comtrade catalog indexer"""

    async def fetch_indicators(self) -> List[Dict[str, Any]]:
        """Fetch Comtrade HS classification codes"""

        indicators = []

        # Comtrade uses HS codes - we'll provide a basic list
        # Full list available at: https://unstats.un.org/unsd/classifications/Econ/Download/In%20Text/HS_english.txt

        hs_codes = [
            ("AG2", "2-digit HS", "Agricultural products, live animals, vegetable products, foodstuffs, beverages, tobacco"),
            ("AG4", "4-digit HS", "Detailed agricultural commodities"),
            ("AG6", "6-digit HS", "Full detailed agricultural classification"),
            ("01", "Live animals", "Live horses, cattle, swine, sheep, goats, etc."),
            ("02", "Meat and edible meat offal", "Beef, pork, poultry, etc."),
            ("03", "Fish and crustaceans", "Live fish, fresh fish, frozen fish, etc."),
            ("04", "Dairy produce; eggs; honey", "Milk, cream, butter, cheese, eggs, etc."),
            ("05", "Products of animal origin", "Animal hair, skins, feathers, etc."),
            ("06", "Live trees and plants", "Bulbs, flowers, plants for planting"),
            ("07", "Edible vegetables", "Potatoes, tomatoes, onions, etc."),
            ("08", "Edible fruit and nuts", "Coconuts, bananas, citrus fruit, etc."),
            ("09", "Coffee, tea, spices", "Coffee, tea, pepper, ginger, etc."),
            ("10", "Cereals", "Wheat, corn, rice, barley, oats, etc."),
            ("11", "Milling products", "Flour, starch, malt, etc."),
            ("12", "Oil seeds", "Soya beans, groundnuts, sunflower seeds, etc."),
            ("15", "Animal/vegetable fats and oils", "Lard, tallow, butter oil, etc."),
            ("27", "Mineral fuels, oils", "Coal, petroleum, natural gas, etc."),
            ("28", "Inorganic chemicals", "Chemical elements, acids, etc."),
            ("29", "Organic chemicals", "Hydrocarbons, alcohols, etc."),
            ("30", "Pharmaceutical products", "Medicaments, vaccines, etc."),
            ("71", "Pearls, precious stones", "Diamonds, precious metals, jewelry"),
            ("72", "Iron and steel", "Iron ore, steel products, etc."),
            ("84", "Machinery and equipment", "Engines, pumps, computers, etc."),
            ("85", "Electrical machinery", "Batteries, phones, circuits, etc."),
            ("87", "Vehicles", "Cars, trucks, motorcycles, etc."),
            ("88", "Aircraft, spacecraft", "Airplanes, helicopters, spacecraft"),
            ("90", "Optical instruments", "Cameras, microscopes, etc."),
            ("99", "Commodities n.e.s.", "Miscellaneous manufactured articles"),
            ("TOTAL", "Total trade", "Total imports or exports"),
        ]

        for code, name, description in hs_codes:
            indicators.append({
                "code": code,
                "name": name,
                "description": description,
                "unit": "USD",
                "frequency": "annual",
                "geo_coverage": "global",
                "start_date": "2000",
                "end_date": "2024",
                "keywords": self._extract_keywords(name, description, "trade"),
                "category": "international trade",
                "metadata_json": "{}",
            })

        logger.info(f"Fetched {len(indicators)} Comtrade HS codes")

        return indicators


class DuneIndexer(CatalogIndexer):
    """Dune Analytics blockchain data catalog indexer"""

    async def fetch_indicators(self) -> List[Dict[str, Any]]:
        """Fetch Dune query types (manually curated list)"""

        indicators = []

        # Dune doesn't have a public catalog API - we provide a curated list
        dune_queries = [
            ("ethereum_gas", "Ethereum Gas Prices", "Average gas prices on Ethereum network", "gwei"),
            ("ethereum_tx_volume", "Ethereum Transaction Volume", "Daily transaction count on Ethereum", "count"),
            ("uniswap_volume", "Uniswap Trading Volume", "Trading volume on Uniswap DEX", "USD"),
            ("defi_tvl", "DeFi Total Value Locked", "Total value locked in DeFi protocols", "USD"),
            ("nft_sales", "NFT Sales Volume", "NFT marketplace sales volume", "USD"),
            ("stablecoin_supply", "Stablecoin Supply", "Circulating supply of major stablecoins", "USD"),
        ]

        for code, name, description, unit in dune_queries:
            indicators.append({
                "code": code,
                "name": name,
                "description": description,
                "unit": unit,
                "frequency": "daily",
                "geo_coverage": "global",
                "start_date": "2020-01-01",
                "end_date": datetime.utcnow().strftime("%Y-%m-%d"),
                "keywords": self._extract_keywords(name, description, "blockchain"),
                "category": "blockchain",
                "metadata_json": "{}",
            })

        logger.info(f"Fetched {len(indicators)} Dune query types")

        return indicators


async def index_all_providers(db_path: str) -> Dict[str, Any]:
    """
    Index all providers

    Args:
        db_path: Path to the SQLite database

    Returns:
        Dictionary with results for all providers
    """
    results = {}

    indexers = [
        FREDIndexer(db_path),
        WorldBankIndexer(db_path),
        StatsCanIndexer(db_path),
        IMFIndexer(db_path),
        BISIndexer(db_path),
        EurostatIndexer(db_path),
        CoinGeckoIndexer(db_path),
        ComtradeIndexer(db_path),
        DuneIndexer(db_path),
    ]

    for indexer in indexers:
        result = await indexer.index()
        results[indexer.provider_name] = result

    return results
