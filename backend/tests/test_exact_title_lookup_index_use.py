"""Exact-title lookup must stay index-served AND match-set identical.

The exact-title shortcut probes every provider on every request.  Its SQL used
``provider = ? COLLATE NOCASE`` (2026-07-19 casing fix) and unindexed
``trim(name)`` / ``lower(name) LIKE`` predicates, which defeated the
BINARY-collated (provider, lower(trim(name))) index and full-scanned the
330K-row catalog several times per provider (~5s per request in prod).

These tests pin the MECHANISM:
* the rewritten statements return exactly what the original NOCASE/LIKE
  statements returned (``legacy_exact_name_matches`` below is the frozen
  pre-fix implementation, used as an oracle over a synthetic catalog built
  with the production schema);
* mixed-case stored providers still match any caller spelling;
* every per-request statement is served by an index (EXPLAIN QUERY PLAN);
* the bounded memo serves repeats without SQL and invalidates on in-process
  writes, on out-of-process writes (file/WAL signature) and on its age
  backstop -- never on plain reads -- and never files pre-change rows under a
  post-change snapshot;
* the FRED substring probe (``name_contains_matches``) returns exactly what
  its original ``lower(name) LIKE '%..%'`` statement returned.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from backend.services import indicator_database
from backend.services.indicator_database import Indicator, IndicatorDatabase, IndicatorLookup


# ---------------------------------------------------------------------------
# Frozen oracle: IndicatorLookup.exact_name_matches as of d955e99 (pre-fix).
# ---------------------------------------------------------------------------

def legacy_exact_name_matches(
    self: Any,
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
        raw_sql += " AND provider = ? COLLATE NOCASE"
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
            "SELECT * FROM indicators WHERE provider = ? COLLATE NOCASE "
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
        sql += " AND provider = ? COLLATE NOCASE"
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
        conditions.append("provider = ? COLLATE NOCASE")
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
            token_sql = "SELECT * FROM indicators WHERE provider = ? COLLATE NOCASE"
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


# ---------------------------------------------------------------------------
# Synthetic catalog built with the PRODUCTION schema (IndicatorDatabase), so
# the (provider, lower(trim(name))) expression index exists exactly as in
# backend/data/indicators.db.
# ---------------------------------------------------------------------------

_ROWS: list[tuple[str, str, str, int]] = [
    ("FRED", "MYAGM1KRM189S", "M1 for Republic of Korea", 50),
    ("FRED", "MANMM101KRM189S", "M1 for Republic of Korea", 10),
    ("FRED", "GDPC1", "Real Gross Domestic Product", 99),
    ("FRED", "GDP", "Gross Domestic Product", 100),
    ("FRED", "GDPA", "Gross domestic product", 20),
    ("FRED", "UNRATE", "Unemployment Rate", 98),
    ("FRED", "PADDED", " Gross Domestic Product ", 5),  # SQLite trim() target
    ("FRED", "TABBED", "Gross Domestic Product\t", 4),  # Python-only strip target
    ("FRED", "M1SL", "M1", 90),
    ("FRED", "CPIAUCSL", "Consumer Price Index for All Urban Consumers: All Items in U.S. City Average", 97),
    ("WorldBank", "NY.GDP.MKTP.CD", "GDP (current US$)", 100),
    ("WorldBank", "SP.POP.TOTL", "Population, total", 90),
    ("WorldBank", "TOT", "Terms of Trade", 30),
    ("WorldBank", "DXGSRMRCHSAXD", "Exports Merchandise, Customs, Price, US$, seas. adj.", 10),
    ("WorldBank", "LEAD", " GDP (current US$)", 1),
    ("IMF", "NGDP_RPCH", "Real GDP growth", 80),
    ("IMF", "CN_GDP", "中国GDP", 3),
    ("Eurostat", "PRC_HICP_MANR", "HICP - monthly data (annual rate of change)", 40),
    ("Eurostat", "UNE_RT_M", "Unemployment by sex and age – monthly data", 60),
    ("StatsCan", "18100004", "Consumer Price Index, monthly, not seasonally adjusted", 70),
    ("CoinGecko", "bitcoin", "Bitcoin", 100),
    ("CoinGecko", "ae-coin", "Æ Coin", 2),
    ("CoinGecko", "strasse", "Straße Token", 1),
    ("Comtrade", "01", "Live animals", 10),
    ("ExchangeRate", "USD", "US Dollar", 10),
    ("ChinaMacro", "CN_PMI_MFG", "Manufacturing PMI", 10),
    ("ChinaMacro", "CN_M2", "M2 货币供应", 9),
    ("OECD", "UNR", "Unemployment rate", 5),
    ("BIS", "BIS_WS_CREDIT_GAP", "Credit-to-GDP gaps", 30),
]

# (names, provider) probes: >= 15 across providers, caller spellings in every
# casing, Unicode/casefold inputs, padded titles, prefix-fallback inputs and
# misses.
_PROBES: list[tuple[list[str], Any]] = [
    (["M1 for Republic of Korea"], "FRED"),
    (["m1 for republic of korea"], "fred"),
    (["Gross Domestic Product"], "FRED"),
    (["gross domestic product"], "FRED"),
    (["Gross  Domestic, Product"], "FRED"),
    (["M1"], "FRED"),
    (["Real GDP growth"], "IMF"),
    (["real gdp growth"], "imf"),
    (["中国GDP"], "IMF"),
    (["中国gdp", "GDP"], "IMF"),
    (["GDP (current US$)"], "WORLDBANK"),
    (["gdp current us"], "WorldBank"),
    (["Population, total"], "worldbank"),
    (["Customs Price US$ seas. adj. Exports Merchandise"], "WORLDBANK"),
    (["Terms of Trade"], "World Bank"),
    (["Unemployment by sex and age – monthly data"], "EUROSTAT"),
    (["hicp monthly data annual rate of change"], "eurostat"),
    (["Consumer Price Index, monthly, not seasonally adjusted"], "STATSCAN"),
    (["consumer price index monthly not seasonally adjusted"], "Statistics Canada"),
    (["Bitcoin"], "COINGECKO"),
    (["æ coin"], "coingecko"),
    (["STRASSE TOKEN"], "CoinGecko"),
    (["straße token"], "COINGECKO"),
    (["Live animals"], "COMTRADE"),
    (["us dollar"], "EXCHANGERATE"),
    (["Manufacturing PMI"], "CHINAMACRO"),
    (["m2 货币供应"], "chinamacro"),
    (["Unemployment rate"], "OECD"),
    (["credit to gdp gaps"], "BIS"),
    (["nonexistent title zzz"], "FRED"),
    (["Gross Domestic Product"], "NoSuchProvider"),
    (["Gross Domestic Product"], None),
    (["gdp (current us$)"], None),
]

_MIXED_CASE_PROVIDERS = {
    "WorldBank": "Population, total",
    "Eurostat": "Unemployment by sex and age – monthly data",
    "StatsCan": "Consumer Price Index, monthly, not seasonally adjusted",
    "CoinGecko": "Bitcoin",
    "Comtrade": "Live animals",
    "ExchangeRate": "US Dollar",
    "ChinaMacro": "Manufacturing PMI",
}


def _summary(rows: list[dict[str, Any]]) -> list[tuple[Any, Any, Any]]:
    return [(row.get("provider"), row.get("code"), row.get("name")) for row in rows]


@pytest.fixture()
def catalog(tmp_path: Path):
    db = IndicatorDatabase(db_path=tmp_path / "indicators.db")
    db.insert_batch(
        [
            Indicator(provider=provider, code=code, name=name, popularity=popularity)
            for provider, code, name, popularity in _ROWS
        ]
    )
    yield db
    db.close()


def test_exact_name_matches_identical_to_legacy_nocase_scan(catalog) -> None:
    lookup = IndicatorLookup(db=catalog)
    for names, provider in _PROBES:
        expected = legacy_exact_name_matches(lookup, names, provider=provider, limit=20)
        actual = lookup.exact_name_matches(names, provider=provider, limit=20)
        assert _summary(actual) == _summary(expected), (names, provider)
    # The probe table must actually exercise matches, not only misses.
    assert sum(
        1
        for names, provider in _PROBES
        if legacy_exact_name_matches(lookup, names, provider=provider, limit=20)
    ) >= 15


def test_exact_name_matches_identical_under_small_limits(catalog) -> None:
    lookup = IndicatorLookup(db=catalog)
    for names, provider in _PROBES:
        for limit in (1, 2):
            expected = legacy_exact_name_matches(lookup, names, provider=provider, limit=limit)
            actual = lookup.exact_name_matches(names, provider=provider, limit=limit)
            assert _summary(actual) == _summary(expected), (names, provider, limit)


@pytest.mark.parametrize("stored", sorted(_MIXED_CASE_PROVIDERS))
def test_mixed_case_providers_match_any_caller_spelling(catalog, stored: str) -> None:
    lookup = IndicatorLookup(db=catalog)
    title = _MIXED_CASE_PROVIDERS[stored]
    for spelling in (stored, stored.upper(), stored.lower()):
        rows = lookup.exact_name_matches([title], provider=spelling)
        assert rows, (stored, spelling)
        assert {row["provider"] for row in rows} == {stored}


def test_provider_filter_binds_stored_spelling_without_nocase(catalog) -> None:
    lookup = IndicatorLookup(db=catalog)
    assert lookup._provider_filter("WORLDBANK") == ("provider = ?", ["WorldBank"])
    assert lookup._provider_filter("statscan") == ("provider = ?", ["StatsCan"])
    # No stored provider folds to this name: NOCASE would match nothing.
    assert lookup._provider_filter("NoSuchProvider") == ("0", [])


def test_provider_filter_falls_back_to_nocase_when_spellings_unavailable() -> None:
    class _BrokenDB:
        def _get_connection(self):
            raise sqlite3.OperationalError("unavailable")

    lookup = IndicatorLookup(db=_BrokenDB())
    assert lookup._provider_filter("WorldBank") == (
        "provider = ? COLLATE NOCASE",
        ["WorldBank"],
    )


def _traced_statements(db: IndicatorDatabase) -> list[str]:
    statements: list[str] = []
    db._get_connection().set_trace_callback(statements.append)
    return statements


@pytest.mark.parametrize(
    "names, provider",
    [
        (["Gross Domestic Product"], "FRED"),  # literal pass hit
        (["gross domestic product"], "FRED"),  # lowered pass
        (["Gross  Domestic, Product"], "FRED"),  # prefix-range fallback
        (["GDP (current US$)"], "WORLDBANK"),
        (["nonexistent title zzz"], "StatsCan"),
        (["中国gdp", "GDP"], "IMF"),  # Unicode casefold pass
    ],
)
def test_exact_title_statements_are_index_served(catalog, names, provider) -> None:
    lookup = IndicatorLookup(db=catalog)
    # Build the cached provider spellings / irregular-title index first: those
    # are one-off per catalog generation, not per request.
    lookup.exact_name_matches(["warm up"], provider=provider)
    lookup.exact_name_matches(["warm-up ünïcode"], provider=provider)
    conn = catalog._get_connection()
    statements = _traced_statements(catalog)
    lookup.exact_name_matches(names, provider=provider)
    conn.set_trace_callback(None)
    selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert selects, "exact-title lookup issued no SQL"
    for sql in selects:
        assert "COLLATE NOCASE" not in sql, sql
        plan = " ; ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql))
        assert (
            "USING INDEX" in plan
            or "USING COVERING INDEX" in plan
            or "USING INTEGER PRIMARY KEY" in plan
        ), (sql, plan)
        for step in plan.split(" ; "):
            if step.startswith("SCAN indicators"):
                assert "USING" in step, (sql, plan)


def test_exact_name_matches_memo_hit_skips_sql_and_returns_copies(catalog) -> None:
    lookup = IndicatorLookup(db=catalog)
    first = lookup.exact_name_matches(["Gross Domestic Product"], provider="FRED")
    assert first
    first[0]["name"] = "mutated by caller"
    statements = _traced_statements(catalog)
    second = lookup.exact_name_matches(["Gross Domestic Product"], provider="FRED")
    catalog._get_connection().set_trace_callback(None)
    assert statements == []
    assert second[0]["name"] == "Gross Domestic Product"


def test_lookup_memo_invalidates_on_catalog_write(catalog) -> None:
    lookup = IndicatorLookup(db=catalog)
    assert lookup.exact_name_matches(["Brand New Title"], provider="FRED") == []
    catalog.insert_indicator(
        Indicator(provider="FRED", code="NEW1", name="Brand New Title", popularity=1)
    )
    rows = lookup.exact_name_matches(["Brand New Title"], provider="FRED")
    assert [row["code"] for row in rows] == ["NEW1"]


def test_file_tracked_cache_uses_long_age_backstop_only(catalog, monkeypatch) -> None:
    lookup = IndicatorLookup(db=catalog)
    lookup.exact_name_matches(["Gross Domestic Product"], provider="FRED")
    state = lookup._catalog_state
    # The file signature observes out-of-process writes, so the short TTL
    # (meant for unobservable catalogs) must not churn the derived indexes.
    monkeypatch.setattr(indicator_database, "_CATALOG_CACHE_TTL_SECONDS", -1.0)
    lookup.exact_name_matches(["Gross Domestic Product"], provider="FRED")
    assert lookup._catalog_state is state
    monkeypatch.setattr(indicator_database, "_CATALOG_CACHE_MAX_AGE_SECONDS", -1.0)
    lookup.exact_name_matches(["Gross Domestic Product"], provider="FRED")
    assert lookup._catalog_state is not state


def test_untracked_catalog_falls_back_to_short_ttl(monkeypatch) -> None:
    class _DB:
        catalog_generation = 0

        def search(self, query, provider=None, category=None, limit=20):
            return []

    lookup = IndicatorLookup(db=_DB())
    assert lookup.catalog_version() is None
    lookup.search("gdp")
    state = lookup._catalog_state
    lookup.search("gdp")
    assert lookup._catalog_state is state
    monkeypatch.setattr(indicator_database, "_CATALOG_CACHE_TTL_SECONDS", -1.0)
    lookup.search("gdp")
    assert lookup._catalog_state is not state


def _external_insert(db_path: Path, code: str, name: str) -> None:
    """Commit a row through a separate connection, as an offline refresh would.

    The large description grows the database file (rollback journal) or
    appends WAL frames, so the change is visible to a stat-based signature
    even within one filesystem timestamp tick.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO indicators (provider, code, name, description, popularity) "
            "VALUES ('FRED', ?, ?, ?, 1)",
            (code, name, "x" * 65536),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_out_of_process_write_invalidates_without_restart(catalog, journal_mode) -> None:
    conn = catalog._get_connection()
    assert conn.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0].lower() == journal_mode
    lookup = IndicatorLookup(db=catalog)
    assert lookup.exact_name_matches(["Offline Refresh Title"], provider="FRED") == []
    assert lookup.name_contains_matches("offline refresh", provider="FRED") == []
    version_before = lookup.catalog_version()
    assert version_before is not None

    _external_insert(catalog.db_path, "OFFLINE1", "Offline Refresh Title")

    assert lookup.catalog_version() != version_before
    assert [row["code"] for row in lookup.exact_name_matches(["Offline Refresh Title"], provider="FRED")] == ["OFFLINE1"]
    assert [row["code"] for row in lookup.name_contains_matches("offline refresh", provider="FRED")] == ["OFFLINE1"]


@pytest.mark.parametrize("journal_mode", ["delete", "wal"])
def test_reads_never_invalidate_the_snapshot(catalog, journal_mode) -> None:
    catalog._get_connection().execute(f"PRAGMA journal_mode={journal_mode}")
    lookup = IndicatorLookup(db=catalog)
    lookup.exact_name_matches(["Gross Domestic Product"], provider="FRED")
    state = lookup._catalog_state
    version = lookup.catalog_version()
    for names, provider in _PROBES:
        lookup.exact_name_matches(names, provider=provider)
        lookup.search(names[0], provider=provider)
    lookup.name_contains_matches("domestic product", provider="FRED")
    assert lookup._catalog_state is state
    assert lookup.catalog_version() == version


def test_catalog_version_tracks_in_process_writes(catalog) -> None:
    lookup = IndicatorLookup(db=catalog)
    version = lookup.catalog_version()
    assert isinstance(version, str)
    assert lookup.catalog_version() == version
    catalog.insert_indicator(Indicator(provider="FRED", code="NEW2", name="Another Title", popularity=1))
    assert lookup.catalog_version() != version


def test_write_during_lookup_never_files_stale_rows_under_new_snapshot(catalog, monkeypatch) -> None:
    lookup = IndicatorLookup(db=catalog)
    real_uncached = lookup._exact_name_matches_uncached

    def _uncached_then_concurrent_write(names, provider=None, limit=20):
        rows = real_uncached(names, provider, limit)  # computed pre-write
        catalog.insert_indicator(
            Indicator(provider="FRED", code="RACE1", name="Race Title", popularity=1)
        )
        return rows

    monkeypatch.setattr(lookup, "_exact_name_matches_uncached", _uncached_then_concurrent_write)
    assert lookup.exact_name_matches(["Race Title"], provider="FRED") == []
    monkeypatch.setattr(lookup, "_exact_name_matches_uncached", real_uncached)
    assert [row["code"] for row in lookup.exact_name_matches(["Race Title"], provider="FRED")] == ["RACE1"]


def test_failed_search_is_not_memoized() -> None:
    calls: list[str] = []

    class _FlakyDB:
        catalog_generation = 0
        search_failures = 0

        def search(self, query, provider=None, category=None, limit=20):
            calls.append(query)
            if len(calls) == 1:
                # IndicatorDatabase.search() swallows errors into [] and
                # counts them; a transient failure must cost one request only.
                self.search_failures += 1
                return []
            return [{"provider": "FRED", "code": "GDP", "name": "Gross Domestic Product", "relevance": -5.0}]

    lookup = IndicatorLookup(db=_FlakyDB())
    assert lookup.search("gross domestic product", provider="FRED") == []
    assert [row["code"] for row in lookup.search("gross domestic product", provider="FRED")] == ["GDP"]
    assert [row["code"] for row in lookup.search("gross domestic product", provider="FRED")] == ["GDP"]
    assert len(calls) == 2


def test_database_search_counts_swallowed_errors(catalog) -> None:
    before = catalog.search_failures
    catalog._get_connection().execute("DROP TABLE indicators_fts")
    assert catalog.search("gross domestic product", provider="FRED") == []
    assert catalog.search_failures == before + 1


def test_memo_is_bounded_by_payload_bytes(monkeypatch) -> None:
    class _DB:
        catalog_generation = 0

        def search(self, query, provider=None, category=None, limit=20):
            return [{"provider": "FRED", "code": query.upper(), "name": query, "description": "d" * 1000}]

    probe = IndicatorLookup(db=_DB())
    probe.search("q00")
    entry_size = next(iter(probe._catalog_state["memo"].values()))[0]
    # Ten same-sized entries fit; each is far below the 1/8 single-entry cap.
    monkeypatch.setattr(indicator_database, "_LOOKUP_MEMO_MAX_BYTES", entry_size * 10)

    lookup = IndicatorLookup(db=_DB())
    for index in range(12):
        lookup.search(f"q{index:02d}")
    state = lookup._catalog_state
    assert len(state["memo"]) == 10
    assert state["memo_bytes"] == entry_size * 10
    assert [key[1] for key in state["memo"]][:2] == ["q02", "q03"]  # LRU evicted q00, q01

    # A single result too large to share the budget is served, not memoized.
    big_query = "q" + "x" * (entry_size * 2)
    assert lookup.search(big_query)
    assert all(key[1] != big_query for key in state["memo"])
    assert state["memo_bytes"] <= indicator_database._LOOKUP_MEMO_MAX_BYTES


# ---------------------------------------------------------------------------
# FRED substring probe (find_exact_provider_title_match FTS-miss fallback)
# ---------------------------------------------------------------------------

def legacy_name_contains(conn: sqlite3.Connection, provider: str, needle: str) -> list[tuple[Any, ...]]:
    """The statement indicator_resolution ran before name_contains_matches."""
    return [
        (row["provider"], row["code"], row["name"])
        for row in conn.execute(
            """
            SELECT *
            FROM indicators
            WHERE provider = ?
              AND lower(name) LIKE ?
            ORDER BY COALESCE(popularity, 0) DESC, code
            LIMIT 20
            """,
            (provider, f"%{needle}%"),
        )
    ]


_SUBSTRING_NEEDLES = [
    "gross domestic product",
    "domestic product",
    "m1",
    "m1 for republic of korea",
    "rate",
    "consumer price index for all urban consumers",
    "product\t",
    "gross  domestic",
    "no such words",
    # LIKE wildcards / edge spaces: must keep the original lower(name) form.
    " gross domestic",
    "gross domestic product ",
    "gross_domestic",
    "m1%korea",
    "%",
    "_",
]


@pytest.fixture()
def substring_catalog(catalog):
    catalog.insert_batch(
        [
            Indicator(provider="FRED", code="LEADSP", name="  gross domestic product, leading", popularity=7),
            Indicator(provider="FRED", code="UNDER", name="Gross_Domestic index", popularity=6),
            Indicator(provider="FRED", code="PCT", name="M1 100% for Korea", popularity=6),
            Indicator(provider="FRED", code="TRAIL", name="Real gross domestic product  ", popularity=8),
        ]
    )
    return catalog


def test_name_contains_matches_identical_to_legacy_like_scan(substring_catalog) -> None:
    lookup = IndicatorLookup(db=substring_catalog)
    conn = substring_catalog._get_connection()
    nonempty = 0
    for needle in _SUBSTRING_NEEDLES:
        for spelling in ("FRED", "fred"):
            expected = legacy_name_contains(conn, "FRED", needle)
            actual = _summary(lookup.name_contains_matches(needle, provider=spelling))
            assert actual == expected, (needle, spelling)
        nonempty += bool(expected)
    assert nonempty >= 10
    assert lookup.name_contains_matches("gross domestic product", provider="NoSuchProvider") == []
    assert lookup.name_contains_matches("", provider="FRED") == []


def test_name_contains_matches_reads_indexed_expression_and_memoizes(substring_catalog) -> None:
    lookup = IndicatorLookup(db=substring_catalog)
    lookup.name_contains_matches("warm up", provider="FRED")  # provider spellings
    conn = substring_catalog._get_connection()
    statements = _traced_statements(substring_catalog)
    rows = lookup.name_contains_matches("domestic product", provider="FRED")
    conn.set_trace_callback(None)
    selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1 and "lower(trim(name)) LIKE" in selects[0], selects
    plan = " ; ".join(row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + selects[0]))
    assert "USING INDEX idx_provider_lower_name" in plan or "USING COVERING INDEX idx_provider_lower_name" in plan, plan
    rows[0]["code"] = "MUTATED"
    statements = _traced_statements(substring_catalog)
    again = lookup.name_contains_matches("domestic product", provider="FRED")
    conn.set_trace_callback(None)
    assert statements == []
    assert again[0]["code"] != "MUTATED"


def test_search_memo_hit_skips_fts_and_returns_copies() -> None:
    calls: list[tuple[Any, ...]] = []

    class _DB:
        catalog_generation = 0

        def search(self, query, provider=None, category=None, limit=20):
            calls.append((query, provider, category, limit))
            return [{"provider": "FRED", "code": "GDP", "name": "Gross Domestic Product", "relevance": -5.0}]

    lookup = IndicatorLookup(db=_DB())
    first = lookup.search("gross domestic product", provider="FRED")
    first[0]["code"] = "MUTATED"
    second = lookup.search("gross domestic product", provider="fred")
    assert len(calls) == 1
    assert second[0]["code"] == "GDP"
    lookup.search("gross domestic product", provider="WORLDBANK")
    assert len(calls) == 2
