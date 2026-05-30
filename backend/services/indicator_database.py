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
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
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
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_provider_lower_name "
            "ON indicators(provider, lower(trim(name)))"
        )

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

    # Common negation prefixes that change the meaning of economic terms.
    # e.g., "unemployment" is the antonym of "employment", "disinflation"
    # differs from "inflation", etc.  Used in post-query filtering to
    # avoid returning antonym indicators for a positive-form query.
    _NEGATION_PREFIXES = ("un", "dis", "de", "non", "in", "im", "ir", "il", "mis")

    @staticmethod
    def _word_appears_standalone(word: str, text: str) -> bool:
        """Check if *word* appears in *text* as a standalone word (not as a
        substring of a negated/prefixed form).

        For example, ``_word_appears_standalone("employment", "Unemployment Rate")``
        returns ``False`` because "employment" only appears inside "unemployment".
        ``_word_appears_standalone("employment", "Employment-Population Ratio")``
        returns ``True``.

        The check is case-insensitive and respects word boundaries.
        """
        # Pattern: word boundary + word (optionally followed by common suffixes)
        # but NOT preceded by a negation prefix.
        # We look for all occurrences of the word and verify none are prefixed.
        word_lower = word.lower()
        text_lower = text.lower()

        # Find every occurrence of the word in the text
        start = 0
        while True:
            idx = text_lower.find(word_lower, start)
            if idx == -1:
                return False  # word not found at all
            # Check what precedes this occurrence
            if idx == 0:
                return True  # at start of text — standalone
            preceding_char = text_lower[idx - 1]
            if not preceding_char.isalpha():
                return True  # preceded by non-alpha (space, hyphen, etc.) — standalone
            # preceded by letters — check if this is a negation prefix
            # find the full "word" that contains our match
            word_start = idx
            while word_start > 0 and text_lower[word_start - 1].isalpha():
                word_start -= 1
            prefix = text_lower[word_start:idx]
            # If the prefix is NOT a known negation prefix, treat as standalone
            # (handles compound words like "reemployment" which IS employment-related)
            if prefix not in IndicatorDatabase._NEGATION_PREFIXES:
                return True
            start = idx + 1

    def _apply_antonym_filter(
        self,
        results: List[Dict[str, Any]],
        query_words: List[str],
        limit: int,
    ) -> List[Dict[str, Any]]:
        """Demote results where query words appear only in negated form in the
        indicator name.

        For a query like "employment", an indicator named "Unemployment Rate"
        matched FTS because its description mentions "employment" in context.
        This filter demotes such results so genuine employment indicators
        rank higher.

        The demotion is applied as a large relevance penalty (+50.0 to the
        bm25 score, which is negative — higher = worse).  This keeps the
        results available but pushes them well below genuine matches.
        """
        # Only apply for single-word queries where antonym confusion is likely.
        # Multi-word queries (e.g., "employment rate") are already precise
        # enough via AND joining.
        if len(query_words) != 1:
            return results[:limit]

        word = query_words[0].lower()
        # Quick check: could this word even have negation-prefix issues?
        # If the word itself starts with a negation prefix, skip filtering
        # (user IS searching for the negated form, e.g., "unemployment").
        for prefix in self._NEGATION_PREFIXES:
            if word.startswith(prefix):
                return results[:limit]

        scored = []
        for r in results:
            name = r.get("name", "")
            if not self._word_appears_standalone(word, name):
                # The query word does NOT appear standalone in the name —
                # it only appears as part of a negated form (e.g.,
                # "unemployment") or not at all in the name (matched via
                # description/keywords).  Apply a demotion penalty.
                r = dict(r)  # copy to avoid mutating cached rows
                r["relevance"] = r.get("relevance", 0) + 50.0
            scored.append(r)

        # Re-sort by relevance (lower = better for bm25)
        scored.sort(key=lambda x: x.get("relevance", 0))
        return scored[:limit]

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
        # Escape special FTS5 characters and punctuation that causes provider-title
        # token mismatches in exact-title search flows: " ' ( ) * - : ^ % /
        safe_query = query
        for char in ['"', "'", '(', ')', '*', '-', ':', '^', '%', '/']:
            safe_query = safe_query.replace(char, ' ')

        # Split into words and filter empty strings
        words = [w.strip() for w in safe_query.split() if w.strip()]

        if not words:
            return []

        # Use AND for multi-word queries (reduces false positives by ~95%),
        # OR for single-word queries.  Prefix matching for partial words.
        joiner = " AND " if len(words) > 1 else " OR "
        fts_query = joiner.join([f'"{w}"*' for w in words])

        # Over-fetch when antonym filtering may apply (single-word queries)
        # so we have enough genuine results after demotion.
        fetch_limit = limit * 3 if len(words) == 1 else limit

        # Column weights: provider=0, code=3, name=10, description=1,
        # category=3, keywords=2, synonyms=2.  Name match is 10x more
        # important than description match — this is the single biggest
        # improvement for broad queries like "employment".
        sql = """
            SELECT
                i.*,
                (bm25(indicators_fts, 0, 3.0, 10.0, 1.0, 3.0, 2.0, 2.0)
                 - COALESCE(i.popularity, 0) * 0.05) as relevance
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

        sql += " ORDER BY relevance LIMIT ?"
        params.append(fetch_limit)

        try:
            cursor.execute(sql, params)
            results = []
            for row in cursor.fetchall():
                results.append(dict(row))
            return self._apply_antonym_filter(results, words, limit)
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
        # Canonical provider names as stored in indicators.db
        self._provider_aliases = {
            "FRED": "FRED",
            "WORLDBANK": "WorldBank",
            "WORLD BANK": "WorldBank",
            "IMF": "IMF",
            "EUROSTAT": "Eurostat",
            "OECD": "OECD",
            "STATSCAN": "StatsCan",
            "STATISTICSCANADA": "StatsCan",
            "STATISTICS CANADA": "StatsCan",
            "BIS": "BIS",
            "COMTRADE": "Comtrade",
            "COINGECKO": "CoinGecko",
            "COIN GECKO": "CoinGecko",
            "EXCHANGERATE": "ExchangeRate",
            "EXCHANGE RATE": "ExchangeRate",
            "EXCHANGE-RATE": "ExchangeRate",
        }

    def _normalize_provider(self, provider: Optional[str]) -> Optional[str]:
        """Normalize provider aliases to canonical database provider names."""
        if not provider:
            return None
        compact = provider.strip().replace("_", " ").replace("-", " ").upper()
        compact = " ".join(compact.split())
        no_space = compact.replace(" ", "")
        return self._provider_aliases.get(compact) or self._provider_aliases.get(no_space) or provider

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

        provider = self._normalize_provider(provider)

        # Over-fetch from DB to ensure quality scoring sees good candidates.
        # BM25 favours term repetition; _rank_results applies quality signals
        # (popularity, name density, aggregate boost) that need a broad pool.
        db_limit = max(limit * 10, 200)
        results = self.db.search(query, provider, category, db_limit)

        # Post-process and rank results
        ranked = self._rank_results(results, query)

        return ranked[:limit]

    def exact_name_matches(
        self,
        names: List[str],
        provider: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Return provider rows whose title exactly matches any supplied name.

        This supplements FTS lookup for provider-native pasted titles.  FTS is
        intentionally synonym-expanded for broad user search, but that can miss
        short exact titles such as "M1 for Republic of Korea" after query
        normalization injects extra terms ("money supply", "monetary").  Exact
        title matching must stay literal, provider-scoped, and side-effect free.
        """
        def _normalize_exact_title(value: str) -> str:
            return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

        raw_inputs = [
            str(name or "").strip()
            for name in names
            if str(name or "").strip()
        ]
        if not raw_inputs:
            return []

        # Preserve caller order while deduplicating.  Keep the raw spellings
        # for literal provider-title transport before falling back to SQLite
        # lower()/ASCII-style normalized recall.  SQLite lower() is
        # ASCII-oriented, so titles such as "Æ Coin" can otherwise collapse
        # into generic normalized labels like "coin" and shadow the exact
        # provider-native row.
        raw_deduped = list(dict.fromkeys(raw_inputs))
        raw_casefold_order: dict[str, int] = {}
        for order, raw_name in enumerate(raw_deduped):
            raw_casefold_order.setdefault(raw_name.casefold(), order)
        cleaned = [name.lower() for name in raw_deduped]
        deduped = list(dict.fromkeys(cleaned))
        normalized_provider = self._normalize_provider(provider)
        conn = self.db._get_connection()  # pylint: disable=protected-access
        cursor = conn.cursor()

        def _row_identity(row: Dict[str, Any]) -> tuple[str, str]:
            return (
                str(row.get("provider") or ""),
                str(row.get("code") or ""),
            )

        raw_exact_rows: list[tuple[int, int, str, Dict[str, Any]]] = []
        raw_seen: set[tuple[str, str]] = set()

        raw_placeholders = ",".join("?" for _ in raw_deduped)
        raw_sql = f"SELECT * FROM indicators WHERE trim(name) IN ({raw_placeholders})"
        raw_params: list[Any] = list(raw_deduped)
        if normalized_provider:
            raw_sql += " AND provider = ?"
            raw_params.append(normalized_provider)
        raw_sql += " ORDER BY COALESCE(popularity, 0) DESC, code LIMIT ?"
        raw_params.append(max(1, limit))
        cursor.execute(raw_sql, raw_params)
        for row in cursor.fetchall():
            candidate = dict(row)
            identity = _row_identity(candidate)
            if identity in raw_seen:
                continue
            raw_seen.add(identity)
            order = raw_casefold_order.get(
                str(candidate.get("name") or "").strip().casefold(),
                len(raw_casefold_order),
            )
            popularity = int(candidate.get("popularity") or 0)
            raw_exact_rows.append((order, -popularity, str(candidate.get("code") or ""), candidate))

        # For provider-scoped exact-title matching, add a bounded Python
        # casefold equality pass so Unicode titles still match when the user
        # uses lowercase Unicode text (for example "æ coin"). SQLite lower()
        # is ASCII-oriented, so this pass is only needed for non-ASCII user
        # input. Avoid scanning the full provider catalog for ordinary ASCII
        # exact-title probes such as "Canada GDP"; SQL exact/lower lookup and
        # the bounded normalized recall path below already cover those without
        # adding semantic shortcuts.
        needs_unicode_casefold_scan = any(
            not raw_name.isascii()
            for raw_name in raw_deduped
        )
        if normalized_provider and len(raw_exact_rows) < limit and needs_unicode_casefold_scan:
            cursor.execute(
                "SELECT * FROM indicators WHERE provider = ? "
                "ORDER BY COALESCE(popularity, 0) DESC, code",
                (normalized_provider,),
            )
            for row in cursor.fetchall():
                candidate = dict(row)
                identity = _row_identity(candidate)
                if identity in raw_seen:
                    continue
                order = raw_casefold_order.get(
                    str(candidate.get("name") or "").strip().casefold()
                )
                if order is None:
                    continue
                raw_seen.add(identity)
                popularity = int(candidate.get("popularity") or 0)
                raw_exact_rows.append((order, -popularity, str(candidate.get("code") or ""), candidate))

        if raw_exact_rows:
            raw_exact_rows.sort()
            return [candidate for _, _, _, candidate in raw_exact_rows][:limit]

        placeholders = ",".join("?" for _ in deduped)
        sql = f"SELECT * FROM indicators WHERE lower(trim(name)) IN ({placeholders})"
        params: list[Any] = list(deduped)
        if normalized_provider:
            sql += " AND provider = ?"
            params.append(normalized_provider)
        sql += " ORDER BY COALESCE(popularity, 0) DESC, code LIMIT ?"
        params.append(max(1, limit))

        cursor.execute(sql, params)
        exact_rows = [dict(row) for row in cursor.fetchall()]
        if len(exact_rows) >= limit:
            return exact_rows[:limit]

        normalized_inputs = {
            normalized: order
            for order, normalized in enumerate(_normalize_exact_title(name) for name in deduped)
            if normalized
        }
        first_tokens = sorted({normalized.split()[0] for normalized in normalized_inputs if normalized.split()})
        if not normalized_inputs or not first_tokens:
            return exact_rows[:limit]

        fallback_sql = "SELECT * FROM indicators WHERE "
        fallback_params: list[Any] = []
        conditions: list[str] = []
        if normalized_provider:
            conditions.append("provider = ?")
            fallback_params.append(normalized_provider)
        token_clauses = ["lower(name) LIKE ?" for _ in first_tokens]
        conditions.append("(" + " OR ".join(token_clauses) + ")")
        fallback_params.extend(f"{token}%" for token in first_tokens)
        fallback_sql += " AND ".join(conditions)
        fallback_sql += " ORDER BY COALESCE(popularity, 0) DESC, code"

        if not normalized_provider:
            # Provider-scoped exact-title lookups are the normal runtime path.
            # Keep unscoped fallback bounded so a generic title probe cannot scan
            # every catalog family on a hot request.
            fallback_sql += " LIMIT 5000"

        cursor.execute(fallback_sql, fallback_params)
        seen_codes = {str(row.get("code") or "") for row in exact_rows}
        normalized_rows: list[tuple[int, int, str, Dict[str, Any]]] = []
        for row in cursor.fetchall():
            candidate = dict(row)
            code = str(candidate.get("code") or "")
            if code in seen_codes:
                continue
            normalized_name = _normalize_exact_title(str(candidate.get("name") or ""))
            order = normalized_inputs.get(normalized_name)
            if order is None:
                continue
            seen_codes.add(code)
            popularity = int(candidate.get("popularity") or 0)
            normalized_rows.append((order, -popularity, code, candidate))

        normalized_rows.sort()

        # Provider titles are often comma-delimited provider-native labels whose
        # token order is not stable when pasted into natural-language queries
        # (for example "Exports Merchandise, Customs, Price, US$, seas. adj."
        # vs "Customs Price US$ seas. adj. Exports Merchandise").  The strict
        # exact-title resolver already verifies near-identical token bags before
        # accepting a row; this lookup fallback only broadens provider-scoped
        # candidate recall so that strict check can see the catalog row.  It is
        # not semantic final authority and does not introduce synonyms, keyword
        # maps, or code shortcuts.
        permutation_rows: list[tuple[int, int, int, int, int, str, Dict[str, Any]]] = []
        if normalized_provider == "WorldBank":
            for normalized, order in normalized_inputs.items():
                query_tokens = list(dict.fromkeys(normalized.split()))
                # Keep this path narrow: short generic titles should continue
                # through the literal/prefix exact-title paths or clarify.
                if len(query_tokens) < 5:
                    continue
                token_sql = "SELECT * FROM indicators WHERE provider = ?"
                token_params: list[Any] = [normalized_provider]
                for token in query_tokens:
                    token_sql += " AND lower(name) LIKE ?"
                    token_params.append(f"%{token}%")
                token_sql += " ORDER BY COALESCE(popularity, 0) DESC, code LIMIT ?"
                token_params.append(max(limit * 10, 200))

                cursor.execute(token_sql, token_params)
                query_token_set = set(query_tokens)
                for row in cursor.fetchall():
                    candidate = dict(row)
                    code = str(candidate.get("code") or "")
                    if code in seen_codes:
                        continue
                    normalized_name = _normalize_exact_title(str(candidate.get("name") or ""))
                    name_tokens = normalized_name.split()
                    if len(name_tokens) < 5:
                        continue
                    name_token_set = set(name_tokens)
                    shared_tokens = len(query_token_set & name_token_set)
                    token_delta = abs(len(name_tokens) - len(query_tokens))
                    unmatched_tokens = len(query_token_set ^ name_token_set)
                    if token_delta > 2 or unmatched_tokens > 1:
                        continue
                    popularity = int(candidate.get("popularity") or 0)
                    permutation_rows.append(
                        (
                            order,
                            unmatched_tokens,
                            token_delta,
                            -shared_tokens,
                            -popularity,
                            code,
                            candidate,
                        )
                    )
                    seen_codes.add(code)

        permutation_rows.sort()
        return (
            exact_rows
            + [candidate for _, _, _, candidate in normalized_rows]
            + [candidate for _, _, _, _, _, _, candidate in permutation_rows]
        )[:limit]

    def get(self, provider: str, code: str) -> Optional[Dict[str, Any]]:
        """
        Get a specific indicator by provider and code.

        Args:
            provider: Provider name (FRED, WorldBank, etc.)
            code: Indicator code

        Returns:
            Indicator metadata or None if not found
        """
        normalized_provider = self._normalize_provider(provider) or provider
        return self.db.get_by_code(normalized_provider, code)

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
        # Normalize common machine-style separators to plain tokens.
        # This makes parser outputs like EXPORT_TO_GDP_RATIO searchable.
        query = re.sub(r"[_/\-]+", " ", query)
        query = re.sub(r"\\s+", " ", query).strip()

        # Remove common noise words
        noise = ["the", "a", "an", "of", "for", "in", "to", "and", "or", "show", "get", "find"]
        words = query.split()
        words = [w for w in words if w not in noise]

        # Handle common variations and synonyms
        # These expansions help FTS5 find related terms that users might not type exactly
        replacements = {
            "gdp": "gdp gross domestic product",
            "growth": "growth annual percent change rate",
            "ppp": "ppp purchasing power parity international",
            "cpi": "cpi consumer price index",
            "ppi": "ppi producer price index",
            "unemployment": "unemployment rate total jobless labor force",
            "inflation": "inflation cpi price consumer",
            "interest": "interest rate",
            "real": "real adjusted inflation",
            "forex": "foreign exchange currency",
            "fx": "foreign exchange currency",
            # Lending synonyms
            "lending": "lending loan prime",
            "lend": "lending loan prime",
            # Treasury synonyms
            "treasury": "treasury yield bond",
            "yield": "yield treasury bond",
            # Trade synonyms
            "exports": "exports trade",
            "imports": "imports trade",
            # Money supply synonyms
            "m2": "m2 money supply monetary",
            "m1": "m1 money supply monetary",
            "m3": "m3 money supply monetary",
            # Commodity price synonyms — expand to match FRED series names
            "gold": "gold fixing price bullion",
            "silver": "silver fixing price",
            "oil": "oil crude petroleum wti brent",
            "wti": "wti west texas intermediate crude oil",
            "brent": "brent crude oil price",
            "copper": "copper price global",
            "natural": "natural gas henry hub",
            # Labor market synonyms — expand to match FRED series names
            "nonfarm": "nonfarm payrolls employment total private",
            "payrolls": "nonfarm payrolls employment",
            "claims": "claims initial unemployment insurance",
            "jobless": "jobless claims initial unemployment",
            "wages": "wages earnings average hourly",
            "earnings": "earnings average hourly wages",
            # Housing synonyms
            "housing": "housing starts units residential",
            "mortgage": "mortgage rate fixed 30 year",
            # Central bank rate synonyms
            "selic": "selic rate brazil central bank",
            "ecb": "ecb european central bank rate",
            "repo": "repo rate central bank policy",
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

            # Boost aggregate/total indicators for generic queries.
            # When users search "unemployment rate" or "GDP" without specifying
            # a demographic (youth, female, male), prefer the total/aggregate
            # indicator over specialized variants.
            demographic_terms = {"youth", "female", "male", "aged", "rural", "urban", "gender", "ratio of"}
            is_generic_query = not any(w in query_lower for w in demographic_terms)
            if is_generic_query:
                if ", total" in name_lower or "total (" in name_lower or name_lower.startswith("total "):
                    score += 8  # Strong boost for aggregate indicators
                if any(w in name_lower for w in ["youth", "female", "male", "ratio of female", "aged 15-24", "aged 25-64"]):
                    score -= 5  # Penalize demographic-specific variants

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

            # Boost/penalize based on query context (price vs holdings/reserves).
            # "Gold price" should rank gold commodity price higher than Federal
            # Reserve gold bullion holdings. "Oil price" should rank crude oil
            # price higher than petroleum reserves.
            if "price" in query_lower:
                if "price" in name_lower or "fixing" in name_lower or "spot" in name_lower:
                    score += 8  # Boost actual price series
                if any(t in name_lower for t in ("held", "reserves", "reserve bank", "deep storage",
                                                  "vault", "mint", "book value", "stock")):
                    score -= 15  # Penalize holdings/reserves/inventory series

            # Boost series matching the primary employment concept.
            # "Nonfarm payrolls" should rank total employment higher than
            # hours worked, discontinued series, or sub-sector breakdowns.
            if "payrolls" in query_lower or "employment" in query_lower:
                if "all employees" in name_lower or "total nonfarm" in name_lower:
                    score += 10
                if "discontinued" in name_lower:
                    score -= 8

            # Penalize projection/forecast indicators (framework-level fix).
            # Users asking for "GDP growth" want actual historical data, not FOMC
            # projections, IMF forecasts, or survey expectations.
            name_lower = (r.get("name") or "").lower()
            _proj_terms = ("fomc", "projection", "projections", "forecast",
                           "forecasts", "outlook", "central tendency",
                           "summary of economic projections", "longer run")
            query_wants_proj = any(t in query_lower for t in _proj_terms)
            if not query_wants_proj and any(t in name_lower for t in _proj_terms):
                score -= 15 if "fomc" in name_lower else 10

            # Penalize cross-domain mismatches.
            _domain_mismatches = {
                "industrial": ("fisheries", "aquaculture", "fish", "forestry"),
                "manufacturing": ("fisheries", "aquaculture", "fish"),
                "factory": ("fisheries", "aquaculture", "fish"),
                "health": ("research and development", "r&d", "education"),
                "healthcare": ("research and development", "r&d"),
                "hospital": ("research and development", "education"),
                "vaccination": ("inflation", "consumer price", "research"),
                "education": ("research and development", "r&d", "health"),
                "school": ("research and development", "health"),
                "electricity": ("agriculture", "education"),
                "solar": ("trade", "education"),
                "corn": ("industry", "manufacturing", "electricity"),
                "wheat": ("industry", "manufacturing", "electricity"),
                "rice": ("industry", "manufacturing", "electricity"),
                "cocoa": ("industry", "manufacturing", "electricity", "renewable"),
                "pension": ("research and development", "r&d", "trade", "electricity"),
                "elderly": ("research and development", "r&d", "electricity"),
                "mining": ("renewable", "education", "health"),
                "crime": ("research and development", "education", "electricity"),
            }
            for query_domain, wrong_domains in _domain_mismatches.items():
                if query_domain in query_lower and any(w in name_lower for w in wrong_domains):
                    score -= 12

            # WorldBank: prefer standard/base indicator codes over variant suffixes.
            # National estimates (code suffix .NE) have sparse coverage — many countries
            # don't report directly. Modeled estimates use regression models and have
            # data for virtually all countries. This is a framework-level preference
            # that helps ALL WorldBank indicator queries, not just specific ones.
            if provider == "WORLDBANK":
                code_upper = (r.get("code") or "").upper()
                if "national estimate" in name_lower or code_upper.endswith(".NE"):
                    score -= 15  # Penalize national-only estimates (sparse data)
                elif "modeled estimate" in name_lower:
                    score += 6  # Boost modeled estimates (broad coverage)

            r["_score"] = score
            ranked.append(r)

        # Sort by score descending
        ranked.sort(key=lambda x: x.get("_score", 0), reverse=True)

        return ranked


# Global IndicatorLookup instance
_indicator_lookup: Optional[IndicatorLookup] = None


def get_indicator_lookup() -> IndicatorLookup:
    """Get or create the global indicator lookup instance."""
    global _indicator_lookup
    if _indicator_lookup is None:
        _indicator_lookup = IndicatorLookup()
    return _indicator_lookup
