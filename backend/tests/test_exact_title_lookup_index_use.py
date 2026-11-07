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
* the bounded memo serves repeats without SQL and invalidates on writes/TTL.
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


def test_lookup_memo_expires_after_ttl(catalog, monkeypatch) -> None:
    lookup = IndicatorLookup(db=catalog)
    lookup.exact_name_matches(["Gross Domestic Product"], provider="FRED")
    state = lookup._catalog_state
    monkeypatch.setattr(
        indicator_database,
        "_CATALOG_CACHE_TTL_SECONDS",
        -1.0,
    )
    lookup.exact_name_matches(["Gross Domestic Product"], provider="FRED")
    assert lookup._catalog_state is not state


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
