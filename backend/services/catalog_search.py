#!/usr/bin/env python3
"""
Catalog Search Service - Fast full-text search over indexed indicators

This service provides sub-5ms full-text search over the catalog database
using SQLite FTS5.
"""

import sqlite3
import os
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """Catalog search result"""

    provider: str
    code: str
    name: str
    description: str
    unit: str
    frequency: str
    geo_coverage: str
    start_date: str
    end_date: str
    category: str
    popularity_score: float
    data_quality_score: float
    relevance_score: float  # FTS5 rank score


class CatalogSearchService:
    """Service for searching the catalog database"""

    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize the catalog search service

        Args:
            db_path: Path to the SQLite database (defaults to backend/data/catalog.db)
        """
        if db_path is None:
            # Default to backend/data/catalog.db
            db_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "data", "catalog.db"
            )

        self.db_path = db_path

    def search(
        self,
        query: str,
        providers: Optional[List[str]] = None,
        categories: Optional[List[str]] = None,
        geo_coverage: Optional[str] = None,
        limit: int = 20,
    ) -> List[SearchResult]:
        """
        Search the catalog using full-text search

        Args:
            query: Search query (natural language or keywords)
            providers: Optional list of providers to filter by (e.g., ["FRED", "WORLDBANK"])
            categories: Optional list of categories to filter by
            geo_coverage: Optional geographic coverage filter
            limit: Maximum number of results to return (default: 20)

        Returns:
            List of SearchResult objects, sorted by relevance
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Build WHERE clause for filters
            filters = []
            params = [query]

            if providers:
                placeholders = ",".join(["?"] * len(providers))
                filters.append(f"i.provider IN ({placeholders})")
                params.extend(providers)

            if categories:
                placeholders = ",".join(["?"] * len(categories))
                filters.append(f"i.category IN ({placeholders})")
                params.extend(categories)

            if geo_coverage:
                filters.append("i.geo_coverage LIKE ?")
                params.append(f"%{geo_coverage}%")

            where_clause = " AND " + " AND ".join(filters) if filters else ""

            # Escape FTS5 special characters in query
            # FTS5 special chars: " - ( ) { } [ ] * : ~ AND OR NOT
            fts_query = query.replace('"', '').replace("'", "").replace("(", "").replace(")", "")
            fts_query = fts_query.replace("{", "").replace("}", "").replace("[", "").replace("]", "")
            fts_query = ' '.join(fts_query.split())  # Normalize whitespace

            # Convert to OR query for broader matching
            # This matches ANY of the words instead of requiring all words as a phrase
            words = fts_query.split()
            if len(words) > 1:
                # Use OR to match any word, but boost exact phrase matches
                fts_query = ' OR '.join(words)
            # Single word queries stay as-is

            # FTS5 query with rank
            # Note: rank is negative (closer to 0 = more relevant)
            sql = f"""
                SELECT
                    i.provider,
                    i.code,
                    i.name,
                    i.description,
                    i.unit,
                    i.frequency,
                    i.geo_coverage,
                    i.start_date,
                    i.end_date,
                    i.category,
                    i.popularity_score,
                    i.data_quality_score,
                    bm25(indicators_fts) as relevance
                FROM indicators_fts fts
                JOIN indicators i ON fts.rowid = i.id
                WHERE indicators_fts MATCH ?{where_clause}
                ORDER BY relevance ASC, i.popularity_score DESC
                LIMIT ?
            """

            params[0] = fts_query  # Replace original query with escaped version
            params.append(limit)
            cursor.execute(sql, params)
            rows = cursor.fetchall()

            results = []
            for row in rows:
                results.append(
                    SearchResult(
                        provider=row[0],
                        code=row[1],
                        name=row[2],
                        description=row[3] or "",
                        unit=row[4] or "",
                        frequency=row[5] or "",
                        geo_coverage=row[6] or "",
                        start_date=row[7] or "",
                        end_date=row[8] or "",
                        category=row[9] or "",
                        popularity_score=row[10] or 0.0,
                        data_quality_score=row[11] or 0.0,
                        relevance_score=row[12],
                    )
                )

            conn.close()

            logger.info(
                f"Search query '{query}' returned {len(results)} results"
                + (f" (filtered by {filters})" if filters else "")
            )

            return results

        except sqlite3.Error as e:
            logger.error(f"Database error during search: {e}", exc_info=True)
            return []
        except Exception as e:
            logger.error(f"Error during search: {e}", exc_info=True)
            return []

    def search_by_provider(
        self, query: str, provider: str, limit: int = 20
    ) -> List[SearchResult]:
        """
        Search within a specific provider

        Args:
            query: Search query
            provider: Provider name (e.g., "FRED", "WORLDBANK")
            limit: Maximum number of results

        Returns:
            List of SearchResult objects
        """
        return self.search(query, providers=[provider], limit=limit)

    def get_provider_stats(self) -> Dict[str, Any]:
        """
        Get statistics about indexed providers

        Returns:
            Dictionary with provider statistics
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            # Total indicators
            cursor.execute("SELECT COUNT(*) FROM indicators")
            total = cursor.fetchone()[0]

            # Breakdown by provider
            cursor.execute("""
                SELECT provider, COUNT(*) as count
                FROM indicators
                GROUP BY provider
                ORDER BY count DESC
            """)
            providers = {row[0]: row[1] for row in cursor.fetchall()}

            # Last update times
            cursor.execute("""
                SELECT provider, last_indexed, status
                FROM index_metadata
                ORDER BY last_indexed DESC
            """)
            last_updates = {
                row[0]: {"last_indexed": row[1], "status": row[2]}
                for row in cursor.fetchall()
            }

            conn.close()

            return {
                "total_indicators": total,
                "providers": providers,
                "last_updates": last_updates,
            }

        except Exception as e:
            logger.error(f"Error getting provider stats: {e}")
            return {"total_indicators": 0, "providers": {}, "last_updates": {}}

    def get_categories(self, provider: Optional[str] = None) -> List[str]:
        """
        Get list of available categories

        Args:
            provider: Optional provider to filter by

        Returns:
            List of category names
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            if provider:
                cursor.execute(
                    """
                    SELECT DISTINCT category
                    FROM indicators
                    WHERE provider = ? AND category != ''
                    ORDER BY category
                    """,
                    (provider,),
                )
            else:
                cursor.execute("""
                    SELECT DISTINCT category
                    FROM indicators
                    WHERE category != ''
                    ORDER BY category
                """)

            categories = [row[0] for row in cursor.fetchall()]
            conn.close()

            return categories

        except Exception as e:
            logger.error(f"Error getting categories: {e}")
            return []

    def get_indicator(self, provider: str, code: str) -> Optional[SearchResult]:
        """
        Get a specific indicator by provider and code

        Args:
            provider: Provider name
            code: Indicator code

        Returns:
            SearchResult or None if not found
        """
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT
                    provider, code, name, description, unit, frequency,
                    geo_coverage, start_date, end_date, category,
                    popularity_score, data_quality_score
                FROM indicators
                WHERE provider = ? AND code = ?
                """,
                (provider, code),
            )

            row = cursor.fetchone()
            conn.close()

            if row:
                return SearchResult(
                    provider=row[0],
                    code=row[1],
                    name=row[2],
                    description=row[3] or "",
                    unit=row[4] or "",
                    frequency=row[5] or "",
                    geo_coverage=row[6] or "",
                    start_date=row[7] or "",
                    end_date=row[8] or "",
                    category=row[9] or "",
                    popularity_score=row[10] or 0.0,
                    data_quality_score=row[11] or 0.0,
                    relevance_score=0.0,  # Not applicable for direct lookup
                )
            return None

        except Exception as e:
            logger.error(f"Error getting indicator {provider}:{code}: {e}")
            return None


def format_results_for_llm(results: List[SearchResult], max_results: int = 10) -> str:
    """
    Format search results for LLM consumption

    Args:
        results: List of SearchResult objects
        max_results: Maximum number of results to include

    Returns:
        Formatted string for LLM prompt
    """
    if not results:
        return "No indicators found matching your query."

    output = f"Found {len(results)} indicators. Showing top {min(len(results), max_results)}:\n\n"

    for i, result in enumerate(results[:max_results], 1):
        output += f"{i}. **{result.name}** ({result.provider}:{result.code})\n"
        output += f"   - Provider: {result.provider}\n"
        output += f"   - Code: {result.code}\n"

        if result.description:
            # Truncate long descriptions
            desc = result.description[:200] + "..." if len(result.description) > 200 else result.description
            output += f"   - Description: {desc}\n"

        if result.unit:
            output += f"   - Unit: {result.unit}\n"

        if result.frequency:
            output += f"   - Frequency: {result.frequency}\n"

        if result.geo_coverage:
            output += f"   - Coverage: {result.geo_coverage}\n"

        if result.start_date and result.end_date:
            output += f"   - Date Range: {result.start_date} to {result.end_date}\n"

        output += "\n"

    return output
