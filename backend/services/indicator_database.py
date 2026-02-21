"""
Comprehensive Indicator Database Service

This module provides a SQLite-based indicator database that:
1. Stores ALL indicators from ALL providers
2. Provides full-text search for fast lookups
3. Includes variations, synonyms, and alternative names
4. Supports incremental updates

Coverage Goals:
- FRED: 800,000+ series (fetch popular ones, ~50,000)
- World Bank: 29,323 indicators (100%)
- IMF: DataMapper + IFS + BOP databases
- Eurostat: 8,118 datasets (100%)
- OECD: All dataflows
- StatsCan: 8,058 tables (100%)
- BIS: 30 dataflows (100%)
- CoinGecko: 19,000+ cryptocurrencies
- ExchangeRate: 160+ currencies
- Comtrade: HS product codes
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Database path
DB_PATH = Path(__file__).parent.parent / "data" / "indicators.db"


@dataclass
class Indicator:
    """Represents a single indicator from any provider."""
    provider: str
    code: str
    name: str
    description: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    unit: Optional[str] = None
    frequency: Optional[str] = None
    coverage: Optional[str] = None  # Countries/regions covered
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    keywords: Optional[str] = None  # Space-separated keywords for search
    synonyms: Optional[str] = None  # Alternative names
    popularity: Optional[int] = None  # For ranking results
    last_updated: Optional[str] = None
    raw_metadata: Optional[str] = None  # JSON string of full metadata


class IndicatorDatabase:
    """
    SQLite-based indicator database with full-text search.

    Provides fast indicator lookups across all providers.
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._initialized = False
        self._write_lock = threading.Lock()  # Thread safety for write operations

    def _get_connection(self) -> sqlite3.Connection:
        """Get or create database connection."""
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            # Enable FTS5 if not initialized
            if not self._initialized:
                self._initialize_db()
                self._initialized = True
        return self._conn

    def _initialize_db(self) -> None:
        """Initialize database schema with FTS5 for full-text search."""
        conn = self._conn
        cursor = conn.cursor()

        # Main indicators table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS indicators (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT,
                category TEXT,
                subcategory TEXT,
                unit TEXT,
                frequency TEXT,
                coverage TEXT,
                start_date TEXT,
                end_date TEXT,
                keywords TEXT,
                synonyms TEXT,
                popularity INTEGER DEFAULT 0,
                last_updated TEXT,
                raw_metadata TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(provider, code)
            )
        """)

        # Create indexes for fast lookups
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_provider ON indicators(provider)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_code ON indicators(code)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_category ON indicators(category)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_popularity ON indicators(popularity DESC)")

        # FTS5 virtual table for full-text search
        cursor.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS indicators_fts USING fts5(
                provider,
                code,
                name,
                description,
                category,
                keywords,
                synonyms,
                content='indicators',
                content_rowid='id'
            )
        """)

        # Triggers to keep FTS in sync
        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS indicators_ai AFTER INSERT ON indicators BEGIN
                INSERT INTO indicators_fts(rowid, provider, code, name, description, category, keywords, synonyms)
                VALUES (new.id, new.provider, new.code, new.name, new.description, new.category, new.keywords, new.synonyms);
            END
        """)

        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS indicators_ad AFTER DELETE ON indicators BEGIN
                INSERT INTO indicators_fts(indicators_fts, rowid, provider, code, name, description, category, keywords, synonyms)
                VALUES ('delete', old.id, old.provider, old.code, old.name, old.description, old.category, old.keywords, old.synonyms);
            END
        """)

        cursor.execute("""
            CREATE TRIGGER IF NOT EXISTS indicators_au AFTER UPDATE ON indicators BEGIN
                INSERT INTO indicators_fts(indicators_fts, rowid, provider, code, name, description, category, keywords, synonyms)
                VALUES ('delete', old.id, old.provider, old.code, old.name, old.description, old.category, old.keywords, old.synonyms);
                INSERT INTO indicators_fts(rowid, provider, code, name, description, category, keywords, synonyms)
                VALUES (new.id, new.provider, new.code, new.name, new.description, new.category, new.keywords, new.synonyms);
            END
        """)

        # Provider metadata table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS provider_stats (
                provider TEXT PRIMARY KEY,
                total_indicators INTEGER DEFAULT 0,
                last_full_fetch TEXT,
                last_incremental_fetch TEXT,
                fetch_duration_seconds REAL,
                notes TEXT
            )
        """)

        conn.commit()
        logger.info(f"Initialized indicator database at {self.db_path}")

    def insert_indicator(self, indicator: Indicator) -> bool:
        """Insert or update a single indicator."""
        conn = self._get_connection()

        with self._write_lock:
            cursor = conn.cursor()
            try:
                cursor.execute("""
                    INSERT INTO indicators (
                        provider, code, name, description, category, subcategory,
                        unit, frequency, coverage, start_date, end_date,
                        keywords, synonyms, popularity, last_updated, raw_metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, code) DO UPDATE SET
                        name = excluded.name,
                        description = excluded.description,
                        category = excluded.category,
                        subcategory = excluded.subcategory,
                        unit = excluded.unit,
                        frequency = excluded.frequency,
                        coverage = excluded.coverage,
                        start_date = excluded.start_date,
                        end_date = excluded.end_date,
                        keywords = excluded.keywords,
                        synonyms = excluded.synonyms,
                        popularity = excluded.popularity,
                        last_updated = excluded.last_updated,
                        raw_metadata = excluded.raw_metadata
                """, (
                    indicator.provider, indicator.code, indicator.name,
                    indicator.description, indicator.category, indicator.subcategory,
                    indicator.unit, indicator.frequency, indicator.coverage,
                    indicator.start_date, indicator.end_date,
                    indicator.keywords, indicator.synonyms, indicator.popularity,
                    indicator.last_updated, indicator.raw_metadata
                ))
                conn.commit()
                return True
            except Exception as e:
                logger.error(f"Error inserting indicator {indicator.provider}:{indicator.code}: {e}")
                return False

    def insert_batch(self, indicators: List[Indicator], batch_size: int = 1000) -> int:
        """Insert indicators in batches for better performance."""
        conn = self._get_connection()

        # Filter out invalid indicators (must have provider, code, and name)
        valid_indicators = [
            ind for ind in indicators
            if ind.provider and ind.code and ind.name
        ]
        if len(valid_indicators) < len(indicators):
            logger.warning(f"Filtered out {len(indicators) - len(valid_indicators)} invalid indicators (missing required fields)")

        inserted = 0
        with self._write_lock:
            cursor = conn.cursor()
            for i in range(0, len(valid_indicators), batch_size):
                batch = valid_indicators[i:i + batch_size]
                try:
                    cursor.executemany("""
                    INSERT INTO indicators (
                        provider, code, name, description, category, subcategory,
                        unit, frequency, coverage, start_date, end_date,
                        keywords, synonyms, popularity, last_updated, raw_metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, code) DO UPDATE SET
                        name = excluded.name,
                        description = excluded.description,
                        category = excluded.category,
                        subcategory = excluded.subcategory,
                        unit = excluded.unit,
                        frequency = excluded.frequency,
                        coverage = excluded.coverage,
                        start_date = excluded.start_date,
                        end_date = excluded.end_date,
                        keywords = excluded.keywords,
                        synonyms = excluded.synonyms,
                        popularity = excluded.popularity,
                        last_updated = excluded.last_updated,
                        raw_metadata = excluded.raw_metadata
                    """, [
                        (
                            ind.provider, ind.code, ind.name,
                            ind.description, ind.category, ind.subcategory,
                            ind.unit, ind.frequency, ind.coverage,
                            ind.start_date, ind.end_date,
                            ind.keywords, ind.synonyms, ind.popularity,
                            ind.last_updated, ind.raw_metadata
                        ) for ind in batch
                    ])
                    conn.commit()
                    inserted += len(batch)
                    logger.info(f"Inserted batch {i//batch_size + 1}: {len(batch)} indicators")
                except Exception as e:
                    logger.error(f"Error inserting batch: {e}")

        return inserted

    def search(
        self,
        query: str,
        provider: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """
        Search indicators using full-text search.

        Args:
            query: Search query (supports FTS5 syntax)
            provider: Filter by provider
            category: Filter by category
            limit: Maximum results to return

        Returns:
            List of matching indicators with relevance scores
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Build FTS query with proper escaping
        # Escape special FTS5 characters: " ' ( ) * - : ^
        safe_query = query
        for char in ['"', "'", '(', ')', '*', '-', ':', '^']:
            safe_query = safe_query.replace(char, ' ')

        # Split into words and filter empty strings
        words = [w.strip() for w in safe_query.split() if w.strip()]

        if not words:
            return []

        # Use prefix matching for partial words, wrapped in quotes for safety
        fts_query = " OR ".join([f'"{w}"*' for w in words])

        sql = """
            SELECT
                i.*,
                bm25(indicators_fts) as relevance
            FROM indicators_fts f
            JOIN indicators i ON f.rowid = i.id
            WHERE indicators_fts MATCH ?
        """
        params = [fts_query]

        if provider:
            sql += " AND i.provider = ?"
            params.append(provider)

        if category:
            sql += " AND i.category = ?"
            params.append(category)

        sql += " ORDER BY relevance, i.popularity DESC LIMIT ?"
        params.append(limit)

        try:
            cursor.execute(sql, params)
            results = []
            for row in cursor.fetchall():
                results.append(dict(row))
            return results
        except Exception as e:
            logger.error(f"Search error: {e}")
            return []

    def get_by_code(self, provider: str, code: str) -> Optional[Dict[str, Any]]:
        """Get indicator by provider and code."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "SELECT * FROM indicators WHERE provider = ? AND code = ?",
            (provider, code)
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_provider_stats(self) -> Dict[str, Dict[str, Any]]:
        """Get statistics for all providers."""
        conn = self._get_connection()
        cursor = conn.cursor()

        # Get counts per provider
        cursor.execute("""
            SELECT provider, COUNT(*) as count
            FROM indicators
            GROUP BY provider
            ORDER BY count DESC
        """)

        stats = {}
        for row in cursor.fetchall():
            stats[row['provider']] = {
                'count': row['count'],
                'last_fetch': None,
            }

        # Get last fetch times
        cursor.execute("SELECT * FROM provider_stats")
        for row in cursor.fetchall():
            provider = row['provider']
            if provider in stats:
                stats[provider]['last_full_fetch'] = row['last_full_fetch']
                stats[provider]['last_incremental_fetch'] = row['last_incremental_fetch']
                stats[provider]['total_available'] = row['total_indicators']

        return stats

    def update_provider_stats(
        self,
        provider: str,
        total_indicators: int,
        fetch_type: str = "full",
        duration: float = 0,
        notes: str = ""
    ) -> None:
        """Update provider fetch statistics."""
        conn = self._get_connection()
        cursor = conn.cursor()

        now = datetime.now(timezone.utc).isoformat()

        if fetch_type == "full":
            cursor.execute("""
                INSERT INTO provider_stats (provider, total_indicators, last_full_fetch, fetch_duration_seconds, notes)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    total_indicators = excluded.total_indicators,
                    last_full_fetch = excluded.last_full_fetch,
                    fetch_duration_seconds = excluded.fetch_duration_seconds,
                    notes = excluded.notes
            """, (provider, total_indicators, now, duration, notes))
        else:
            cursor.execute("""
                INSERT INTO provider_stats (provider, total_indicators, last_incremental_fetch, fetch_duration_seconds, notes)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    total_indicators = excluded.total_indicators,
                    last_incremental_fetch = excluded.last_incremental_fetch,
                    fetch_duration_seconds = excluded.fetch_duration_seconds,
                    notes = excluded.notes
            """, (provider, total_indicators, now, duration, notes))

        conn.commit()

    def close(self) -> None:
        """Close database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None


# Global instance
_indicator_db: Optional[IndicatorDatabase] = None


def get_indicator_database() -> IndicatorDatabase:
    """Get or create the global indicator database instance."""
    global _indicator_db
    if _indicator_db is None:
        _indicator_db = IndicatorDatabase()
    return _indicator_db
