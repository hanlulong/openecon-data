"""
Fast Indicator Lookup Service

Provides fast indicator lookups using the pre-populated SQLite database.
This replaces expensive runtime API calls with instant database lookups.

Usage:
    from backend.services.indicator_lookup import get_indicator_lookup

    lookup = get_indicator_lookup()

    # Search for indicators
    results = lookup.search("GDP growth", provider="FRED", limit=10)

    # Get specific indicator
    indicator = lookup.get("FRED", "GDP")

    # Find best provider for a concept
    provider, code = lookup.find_best_provider("unemployment rate")
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from .indicator_database import IndicatorDatabase, get_indicator_database

logger = logging.getLogger(__name__)


class IndicatorLookup:
    """
    Fast indicator lookup service using pre-populated SQLite database.

    Features:
    - Full-text search across all providers
    - Provider-specific lookups
    - Synonym and keyword matching
    - Relevance-based ranking
    """

    def __init__(self, db: Optional[IndicatorDatabase] = None):
        self.db = db or get_indicator_database()

    def search(
        self,
        query: str,
        provider: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Search for indicators matching the query.

        Args:
            query: Natural language search query
            provider: Filter by provider (FRED, WorldBank, IMF, etc.)
            category: Filter by category
            limit: Maximum results to return

        Returns:
            List of matching indicators with metadata
        """
        # Clean and normalize query
        query = self._normalize_query(query)

        if not query:
            return []

        # Use database FTS search
        results = self.db.search(query, provider, category, limit)

        # Post-process and rank results
        ranked = self._rank_results(results, query)

        return ranked[:limit]

    def get(self, provider: str, code: str) -> Optional[Dict[str, Any]]:
        """
        Get a specific indicator by provider and code.

        Args:
            provider: Provider name (FRED, WorldBank, etc.)
            code: Indicator code

        Returns:
            Indicator metadata or None if not found
        """
        return self.db.get_by_code(provider, code)

    def find_best_provider(
        self,
        query: str,
        country: Optional[str] = None,
        preferred_providers: Optional[List[str]] = None,
    ) -> Optional[Tuple[str, str, str]]:
        """
        Find the best provider and indicator code for a query.

        Args:
            query: Natural language query (e.g., "GDP growth")
            country: Optional country context
            preferred_providers: Optional list of preferred providers

        Returns:
            Tuple of (provider, code, name) or None if no match
        """
        # Search across all providers
        results = self.search(query, limit=50)

        if not results:
            return None

        # Score results based on preferences
        scored = []
        for r in results:
            score = r.get("_score", 0)

            # Boost preferred providers
            if preferred_providers and r["provider"] in preferred_providers:
                score += 10

            # Boost based on country coverage
            if country:
                coverage = r.get("coverage", "") or ""
                if coverage and country.upper() in coverage.upper():
                    score += 5

            # Boost by popularity
            popularity = r.get("popularity", 0) or 0
            score += min(popularity / 100, 5)

            scored.append((score, r))

        # Sort by score
        scored.sort(key=lambda x: x[0], reverse=True)

        if scored:
            best = scored[0][1]
            return (best["provider"], best["code"], best["name"])

        return None

    def get_providers_for_indicator(self, query: str) -> List[Dict[str, Any]]:
        """
        Get all providers that have data for an indicator type.

        Args:
            query: Indicator query (e.g., "GDP", "unemployment")

        Returns:
            List of providers with their indicator codes
        """
        results = self.search(query, limit=100)

        # Group by provider
        by_provider = {}
        for r in results:
            provider = r["provider"]
            if provider not in by_provider:
                by_provider[provider] = {
                    "provider": provider,
                    "indicators": [],
                }
            by_provider[provider]["indicators"].append({
                "code": r["code"],
                "name": r["name"],
                "score": r.get("_score", 0),
            })

        return list(by_provider.values())

    def get_stats(self) -> Dict[str, Any]:
        """Get database statistics."""
        return self.db.get_provider_stats()

    def _normalize_query(self, query: str) -> str:
        """Normalize query for search."""
        # Lowercase and strip
        query = query.lower().strip()

        # Remove common noise words
        noise = ["the", "a", "an", "of", "for", "in", "to", "and", "or", "show", "get", "find"]
        words = query.split()
        words = [w for w in words if w not in noise]

        # Handle common variations and synonyms
        # These expansions help FTS5 find related terms that users might not type exactly
        replacements = {
            "gdp": "gdp gross domestic product",
            "cpi": "cpi consumer price index",
            "ppi": "ppi producer price index",
            "unemployment": "unemployment rate jobless",
            "inflation": "inflation cpi price",
            "interest": "interest rate",
            "forex": "foreign exchange currency",
            "fx": "foreign exchange currency",
            # Lending synonyms - users searching for "lending rates" often want prime rate
            "lending": "lending loan prime",
            "lend": "lending loan prime",
            # Treasury synonyms
            "treasury": "treasury yield bond",
            "yield": "yield treasury bond",
            # Trade synonyms
            "exports": "exports trade",
            "imports": "imports trade",
            # Money supply synonyms - infrastructure fix to disambiguate from education codes
            "m2": "m2 money supply monetary",
            "m1": "m1 money supply monetary",
            "m3": "m3 money supply monetary",
        }

        expanded = []
        for word in words:
            if word in replacements:
                expanded.append(replacements[word])
            else:
                expanded.append(word)

        return " ".join(expanded)

    def _rank_results(
        self,
        results: List[Dict[str, Any]],
        query: str,
    ) -> List[Dict[str, Any]]:
        """Rank results by relevance to query.

        Key ranking factors:
        1. FTS5 BM25 relevance score
        2. Exact word matches in name/code
        3. Popularity boost
        4. Country preference for FRED (US data source) - penalize non-US series
           when query doesn't explicitly request another country
        5. Data freshness (prefer series with recent end dates)
        """
        query_lower = query.lower()
        query_words = set(query_lower.split())

        # Check if query explicitly mentions a non-US country
        non_us_countries = {
            "canada", "canadian", "china", "chinese", "japan", "japanese",
            "germany", "german", "uk", "britain", "british", "france", "french",
            "india", "indian", "brazil", "brazilian", "mexico", "mexican",
            "australia", "australian", "korea", "korean", "italy", "italian"
        }
        mentions_non_us = bool(query_words & non_us_countries)

        ranked = []
        for r in results:
            # FTS5 BM25 returns negative scores (more negative = less relevant)
            # Negate to get positive scores where higher = more relevant
            score = -1 * r.get("relevance", 0)

            # Boost exact matches (handle None values safely)
            name_lower = (r.get("name") or "").lower()
            code_lower = (r.get("code") or "").lower()
            provider = (r.get("provider") or "").upper()

            for word in query_words:
                if word in name_lower:
                    score += 2

                    # INFRASTRUCTURE FIX: Distinguish SUBJECT vs REFERENCE in indicator names
                    # Example: "Money and quasi money (M2) as % of GDP" - M2 is SUBJECT (boost)
                    # Example: "Claims on governments (as % of M2)" - M2 is REFERENCE (penalize)
                    # This fixes World Bank M2 queries selecting wrong indicator FM.AST.GOVT.ZG.M2

                    # Boost: Term appears at start of name or in parentheses (indicating it's the subject)
                    if name_lower.startswith(word) or f"({word})" in name_lower or f" {word} " in f" {name_lower} ":
                        score += 5  # Strong boost for subject indicators

                    # Penalize: Term appears after "% of" or "as % of" (indicating it's just a reference)
                    if f"% of {word}" in name_lower or f"of {word})" in name_lower:
                        score -= 10  # Strong penalty for reference-only indicators

                # Use prefix matching for code to avoid false positives
                # e.g., "m2" should match "m2sl" but NOT "cm2" (education indicator)
                # This is an infrastructure fix for all short indicator code queries
                code_clean = code_lower.replace('_', '').replace('-', '')
                if code_lower.startswith(word) or code_clean.startswith(word) or code_lower == word:
                    score += 3

            # Boost popular indicators
            popularity = r.get("popularity", 0) or 0
            score += min(popularity / 100, 3)

            # FRED-specific: Prefer US-based series when query doesn't mention another country
            # FRED is primarily a US data source, so international data is often less relevant
            if provider == "FRED" and not mentions_non_us:
                # Penalize series that are clearly for other countries
                country_names_in_title = [
                    "canada", "china", "japan", "germany", "uk", "france",
                    "india", "brazil", "mexico", "australia", "korea", "italy",
                    "spain", "netherlands", "switzerland", "sweden", "norway",
                    "euro area", "vietnam", "viet nam", "thailand", "indonesia",
                    "russia", "turkey", "south africa", "argentina", "chile",
                    "colombia", "poland", "portugal", "greece", "ireland"
                ]
                for country in country_names_in_title:
                    if country in name_lower:
                        score -= 15  # Strong penalty for wrong country
                        break

                # Boost series that are explicitly for United States
                if "united states" in name_lower or "u.s." in name_lower:
                    score += 5

            # Prefer series with recent data (not discontinued)
            end_date = r.get("end_date") or ""
            if end_date:
                try:
                    # Check if series has data in the last 2 years
                    from datetime import datetime
                    end_year = int(end_date[:4]) if len(end_date) >= 4 else 0
                    current_year = datetime.now().year
                    if end_year >= current_year - 1:
                        score += 3  # Boost for current data
                    elif end_year < current_year - 5:
                        score -= 5  # Penalty for very old data
                except (ValueError, TypeError):
                    pass

            r["_score"] = score
            ranked.append(r)

        # Sort by score descending
        ranked.sort(key=lambda x: x.get("_score", 0), reverse=True)

        return ranked


# Global instance
_indicator_lookup: Optional[IndicatorLookup] = None


def get_indicator_lookup() -> IndicatorLookup:
    """Get or create the global indicator lookup instance."""
    global _indicator_lookup
    if _indicator_lookup is None:
        _indicator_lookup = IndicatorLookup()
    return _indicator_lookup
