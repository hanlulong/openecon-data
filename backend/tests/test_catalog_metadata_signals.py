"""Structural catalog-metadata signals reach selection, resolution and menus.

Two mechanisms, both keyed on OFFICIAL provider metadata (never on a code,
title, country or concept):

1. Frequency.  The catalog stores StatsCan's raw WDS ``frequencyCode``
   ("6", "12", ...).  Every catalog read now decodes it through the committed
   copy of the official ``getCodeSets`` frequency codeset, so the selector's
   annotation/rerank/conflict checks and indicator_resolution's exact-title
   frequency filter compare canonical tokens instead of failing on "6".

2. Vintage-archive source.  A World Bank source whose published dimension list
   carries a ``Version`` concept cannot be served by the current-data endpoint.
   The selector drops such candidates when servable ones remain, the WB
   alternate-code retry never retries into them, and clarification menus never
   offer them.

Assertions are on PROPERTIES (frequency token, servability), never on an
expected series id.  Catalog-backed tests derive their inputs from the catalog
itself and skip when the gitignored indicators.db is absent.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.providers.statscan import StatsCanProvider
from backend.services.indicator_resolution import _candidate_frequency_matches
from backend.services.indicator_selector import (
    IndicatorSelector,
    _extract_requested_frequencies,
    _frequency_matches,
)
from backend.utils.catalog_metadata_signals import (
    CORE_FREQUENCY_TOKENS,
    IRREGULAR_FREQUENCY,
    MULTIYEAR_FREQUENCY,
    STATSCAN_FREQUENCY_CODESET_FILE,
    canonical_catalog_frequency,
    canonical_frequency_from_official_label,
    catalog_row_is_vintage_archive,
    normalize_catalog_row_frequency,
    statscan_canonical_frequency_by_code,
    statscan_frequency_codeset,
    worldbank_vintage_source_ids,
)
from backend.utils.option_supportability import option_supportability_reason

_DB = Path(__file__).resolve().parent.parent / "data" / "indicators.db"
needs_catalog = pytest.mark.skipif(not _DB.exists(), reason="indicators.db not present")


@pytest.fixture(autouse=True)
def _selector_fixture_replay_off(monkeypatch):
    """Tests that stub the LLM transport assert on the rendered prompt."""
    monkeypatch.setenv("OPENECON_SELECTOR_FIXTURES", "")


def _selector() -> IndicatorSelector:
    return IndicatorSelector(
        settings=SimpleNamespace(
            indicator_telemetry_enabled=False,
            indicator_fusion="legacy",
            llm_provider="vllm",
            llm_base_url="http://localhost:8000",
            llm_model="test-model",
            selector_llm_base_url="",
            selector_llm_model="",
            openrouter_api_key="",
            llm_fallback_model="openai/gpt-oss-120b",
        )
    )


class _Resp:
    status_code = 200

    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self):  # noqa: ANN201
        return None

    def json(self):  # noqa: ANN201
        return {"choices": [{"message": {"content": self._content}}]}


class _CapturingClient:
    def __init__(self, sink: dict, line: str = "PICK: 1") -> None:
        self._sink = sink
        self._line = line

    async def post(self, url, headers=None, json=None, timeout=None):  # noqa: ANN001, A002
        self._sink["prompt"] = json["messages"][0]["content"]
        return _Resp(self._line)


def _lookup():
    from backend.services.indicator_database import get_indicator_lookup

    return get_indicator_lookup()


# ---------------------------------------------------------------------------
# 1. Frequency decoding (pure; no catalog needed)
# ---------------------------------------------------------------------------


def test_reference_codeset_is_the_official_snapshot_with_provenance() -> None:
    payload = json.loads(STATSCAN_FREQUENCY_CODESET_FILE.read_text(encoding="utf-8"))
    assert payload["source_url"].endswith("/getCodeSets")
    assert payload["retrieved_at"]
    assert statscan_frequency_codeset(), "codeset must load"


def test_every_official_code_decodes_into_the_closed_vocabulary() -> None:
    allowed = CORE_FREQUENCY_TOKENS | {MULTIYEAR_FREQUENCY, IRREGULAR_FREQUENCY}
    decoded = statscan_canonical_frequency_by_code()
    assert set(decoded) == set(statscan_frequency_codeset())
    assert set(decoded.values()) <= allowed
    # The core frequencies are all present in the official codeset.
    assert {"daily", "weekly", "monthly", "quarterly", "annual"} <= set(decoded.values())


@pytest.mark.parametrize(
    "label, expected",
    [
        ("Semi-annual", "semiannual"),
        ("Every 2 years", MULTIYEAR_FREQUENCY),
        ("Every 10 years", MULTIYEAR_FREQUENCY),
        ("Occasional", IRREGULAR_FREQUENCY),
        ("Occasional Monthly", IRREGULAR_FREQUENCY),
        ("Monthly", "monthly"),
    ],
)
def test_official_label_normalization(label: str, expected: str) -> None:
    assert canonical_frequency_from_official_label(label) == expected


@pytest.mark.parametrize("request_freq", ["annual", "monthly", "quarterly", "daily", "weekly"])
def test_non_core_official_frequencies_never_alias_a_core_request(request_freq: str) -> None:
    """Semi-annual / every-N-years / occasional must not MATCH a core request in
    either the selector's or the exact-title resolver's frequency matcher."""
    non_core = {
        token
        for token in statscan_canonical_frequency_by_code().values()
        if token != request_freq and token not in {"daily", "weekly", "monthly", "quarterly", "annual"}
    }
    assert non_core
    for token in non_core:
        assert not _frequency_matches({request_freq}, token), token
        assert not _candidate_frequency_matches({request_freq}, token), token


def test_decoder_accepts_str_and_int_and_is_provider_scoped() -> None:
    by_code = statscan_canonical_frequency_by_code()
    monthly_code = next(code for code, token in by_code.items() if token == "monthly")
    assert canonical_catalog_frequency("StatsCan", str(monthly_code)) == "monthly"
    assert canonical_catalog_frequency("Statistics Canada", monthly_code) == "monthly"
    # Other providers' raw values pass through byte-identical.
    for raw in ("Monthly, End of Period", "Weekly, Ending Saturday", "5 Year", None, ""):
        assert canonical_catalog_frequency("FRED", raw) == raw
    assert canonical_catalog_frequency("FRED", str(monthly_code)) == str(monthly_code)
    # Unknown StatsCan code: unchanged, never guessed.
    assert canonical_catalog_frequency("StatsCan", "999") == "999"


def test_row_normalizer_keeps_raw_code_and_leaves_text_rows_alone() -> None:
    by_code = statscan_canonical_frequency_by_code()
    annual_code = next(code for code, token in by_code.items() if token == "annual")
    row = {"provider": "StatsCan", "code": "X", "frequency": str(annual_code)}
    normalize_catalog_row_frequency(row)
    assert row["frequency"] == "annual" and row["frequency_code"] == str(annual_code)
    fred = {"provider": "FRED", "code": "Y", "frequency": "Monthly"}
    assert normalize_catalog_row_frequency(dict(fred)) == fred


def test_statscan_provider_frequency_map_derives_from_the_official_codeset() -> None:
    by_code = statscan_canonical_frequency_by_code()
    # Emitted vocabulary unchanged (downstream TTL/period formatting/deltas).
    assert set(StatsCanProvider.FREQUENCY_MAP.values()) <= {"daily", "weekly", "monthly", "quarterly", "annual"}
    for code, token in StatsCanProvider.FREQUENCY_MAP.items():
        assert by_code[code] == token
    weekly_code = next(code for code, token in by_code.items() if token == "weekly")
    assert StatsCanProvider.FREQUENCY_MAP[weekly_code] == "weekly"
    assert StatsCanProvider._frequency_code_from_text("Weekly") == weekly_code
    for text, token in (("Monthly", "monthly"), ("Quarterly", "quarterly"), ("Daily", "daily"), ("", "annual")):
        assert by_code[StatsCanProvider._frequency_code_from_text(text)] == token
    # Codes absent from the official codeset are not invented.
    assert set(StatsCanProvider.FREQUENCY_MAP) <= set(by_code)


# ---------------------------------------------------------------------------
# 1b. Frequency at the catalog read boundary + selector/resolution consumers
# ---------------------------------------------------------------------------


def _statscan_rows_by_frequency(per_freq: int = 2) -> dict[str, list[dict]]:
    """Real StatsCan catalog rows for each core frequency, read through the
    catalog chokepoint (derived from the catalog, not pinned ids)."""
    lookup = _lookup()
    rows: dict[str, list[dict]] = {}
    for phrase in ("unemployment rate", "consumer price index", "gross domestic product", "retail trade"):
        for row in lookup.search(phrase, provider="StatsCan", limit=40):
            freq = str(row.get("frequency") or "")
            if freq in {"monthly", "quarterly", "annual"} and len(rows.setdefault(freq, [])) < per_freq:
                if all(r["code"] != row["code"] for r in rows[freq]):
                    rows[freq].append(row)
    return rows


@needs_catalog
def test_catalog_reads_return_canonical_statscan_frequency() -> None:
    lookup = _lookup()
    rows = _statscan_rows_by_frequency()
    assert {"monthly", "quarterly", "annual"} <= set(rows), rows.keys()
    for freq, items in rows.items():
        for row in items:
            assert row["frequency_code"].isdigit()
            again = lookup.get("StatsCan", row["code"])
            assert again["frequency"] == freq
            exact = lookup.exact_name_matches([row["name"]], provider="StatsCan", limit=5)
            assert any(r["code"] == row["code"] and r["frequency"] == freq for r in exact)
    # FRED rows carry provider text and are untouched.
    fred = lookup.search("unemployment rate", provider="FRED", limit=5)
    assert fred and all("frequency_code" not in r for r in fred)


@needs_catalog
@pytest.mark.parametrize(
    "query",
    [
        "Canada unemployment rate monthly from Statistics Canada since 2025",
        "Canada CPI quarterly",
        "Canada GDP annual",
        "Canadian retail sales monthly",
        "加拿大失业率 monthly",
    ],
)
def test_selector_annotations_and_rerank_follow_decoded_frequency(monkeypatch, query: str) -> None:
    rows = _statscan_rows_by_frequency()
    requested = _extract_requested_frequencies(query)
    assert len(requested) == 1
    (want,) = requested
    candidates = [
        (row["code"], row["name"])
        for freq in ("annual", "quarterly", "monthly")
        for row in rows.get(freq, [])
    ]
    selector = _selector()

    # Rerank: requested-frequency candidates move ahead (previously a no-op
    # for StatsCan because "6" matched nothing).
    ordered, _scores = selector._prioritize_candidates_by_query_metadata(
        query, candidates, [1.0] * len(candidates), "StatsCan"
    )
    meta = {row["code"]: row["frequency"] for items in rows.values() for row in items}
    n_match = sum(1 for code, _ in candidates if meta[code] == want)
    assert n_match and all(meta[code] == want for code, _ in ordered[:n_match])

    # Annotation: decoded token displayed; MATCHES only on the right rows.
    sink: dict = {}
    monkeypatch.setattr("backend.services.http_pool.get_http_client", lambda: _CapturingClient(sink))
    asyncio.run(selector._llm_pick(query, candidates, "StatsCan"))
    lines = [line for line in sink["prompt"].splitlines() if line[:1].isdigit() and "[" in line]
    assert len(lines) == len(candidates)
    for line, (code, _name) in zip(lines, candidates):
        assert f"({meta[code]}," in line
        if meta[code] == want:
            assert "MATCHES requested frequency" in line
        else:
            assert "does NOT match requested frequency" in line


@needs_catalog
def test_exact_title_frequency_filter_no_longer_skips_statscan_rows() -> None:
    rows = _statscan_rows_by_frequency(per_freq=1)
    for freq, items in rows.items():
        for row in items:
            assert _candidate_frequency_matches({freq}, row["frequency"])
            others = {"monthly", "quarterly", "annual"} - {freq}
            assert not any(_candidate_frequency_matches({o}, row["frequency"]) for o in others)


# ---------------------------------------------------------------------------
# 2. Vintage-archive source signal (World Bank)
# ---------------------------------------------------------------------------

# Phrases from production archive hits, plus a Chinese phrasing. Inputs only:
# the codes under test are whatever the catalog retrieves for them.
_ARCHIVE_HIT_PHRASES = (
    "household final consumption expenditure annual growth",
    "household final consumption expenditure % of GDP",
    "domestic financing total",
    "external borrowing financing from abroad",
    "liabilities",
    "London gold price",
)


def _is_vintage(row: dict) -> bool:
    return catalog_row_is_vintage_archive("WorldBank", row.get("raw_metadata"))


def _wb_candidates(phrase: str, limit: int = 40) -> list[dict]:
    return _lookup().db.search(phrase, provider="WorldBank", limit=limit)


def test_vintage_predicate_keys_on_published_dimensions_not_names() -> None:
    ids = worldbank_vintage_source_ids()
    assert ids, "reference table must declare at least one vintage source"
    vintage_id = sorted(ids)[0]
    assert catalog_row_is_vintage_archive("WorldBank", {"source": {"id": vintage_id, "value": "anything"}})
    # A source NAMED like an archive but without a Version dimension is servable.
    assert not catalog_row_is_vintage_archive(
        "WorldBank", json.dumps({"source": {"id": "__no_such__", "value": "Some Datahub Archive"}})
    )
    # Fail open: other providers / absent / garbage metadata.
    assert not catalog_row_is_vintage_archive("FRED", {"source": {"id": vintage_id}})
    assert not catalog_row_is_vintage_archive("WorldBank", None)
    assert not catalog_row_is_vintage_archive("WorldBank", "{not json")


@needs_catalog
@pytest.mark.parametrize("phrase", _ARCHIVE_HIT_PHRASES + ("中国居民消费支出增长",))
def test_selector_drops_vintage_candidates_when_servable_ones_remain(phrase: str) -> None:
    selector = _selector()
    pool = _wb_candidates(phrase) or _wb_candidates("household final consumption expenditure")
    vintage = [r for r in pool if _is_vintage(r)]
    live = [r for r in pool if not _is_vintage(r)]
    if not vintage or not live:
        pytest.skip("catalog retrieval for this phrase is not mixed")
    candidates = [(r["code"], r["name"]) for r in pool]
    kept, scores = asyncio.run(
        selector._drop_vintage_archive_candidates(phrase, candidates, [1.0] * len(candidates), "WorldBank")
    )
    vintage_codes = {r["code"] for r in vintage}
    assert kept and not any(code in vintage_codes for code, _ in kept)
    assert len(kept) == len(scores) == len(live)


@needs_catalog
def test_selector_keeps_archive_only_sets_and_explicit_codes_and_other_providers() -> None:
    selector = _selector()
    vintage = [r for p in _ARCHIVE_HIT_PHRASES for r in _wb_candidates(p) if _is_vintage(r)]
    live = [r for p in _ARCHIVE_HIT_PHRASES for r in _wb_candidates(p) if not _is_vintage(r)]
    assert vintage and live
    only_archive = [(r["code"], r["name"]) for r in vintage[:4]]
    kept, _ = asyncio.run(
        selector._drop_vintage_archive_candidates("q", only_archive, [1.0] * 4, "WorldBank")
    )
    assert kept == only_archive  # adjudicator still sees them (and may REJECT)

    named = vintage[0]["code"]
    mixed = [(named, vintage[0]["name"]), (live[0]["code"], live[0]["name"])]
    kept, _ = asyncio.run(
        selector._drop_vintage_archive_candidates(f"show {named} for China", mixed, [1.0, 1.0], "WorldBank")
    )
    assert [c for c, _ in kept] == [named, live[0]["code"]]

    kept, _ = asyncio.run(selector._drop_vintage_archive_candidates("q", mixed, [1.0, 1.0], "FRED"))
    assert kept == mixed


@needs_catalog
def test_archive_only_prompt_marks_candidates_unservable_and_keeps_reject(monkeypatch) -> None:
    selector = _selector()
    vintage = [r for p in _ARCHIVE_HIT_PHRASES for r in _wb_candidates(p) if _is_vintage(r)][:3]
    sink: dict = {}
    monkeypatch.setattr(
        "backend.services.http_pool.get_http_client",
        lambda: _CapturingClient(sink, line="REJECT: no candidate is servable"),
    )
    result = asyncio.run(
        selector._llm_pick("China household consumption growth", [(r["code"], r["name"]) for r in vintage], "WorldBank")
    )
    assert sink["prompt"].count("ARCHIVED vintage source: not servable for current data") == len(vintage)
    assert result is None or not result.code


@needs_catalog
@pytest.mark.parametrize("phrase", _ARCHIVE_HIT_PHRASES)
def test_wb_alternatives_never_retry_into_vintage_siblings(phrase: str) -> None:
    from backend.providers.worldbank import WorldBankProvider

    pool = _wb_candidates(phrase)
    primary = next((r for r in pool if _is_vintage(r)), None)
    if primary is None:
        pytest.skip("no vintage row retrieved for this phrase")
    provider = WorldBankProvider.__new__(WorldBankProvider)
    alternatives = asyncio.run(provider._get_alternative_indicators(phrase, primary["code"], limit=3))
    lookup = _lookup()
    for code in alternatives:
        assert not _is_vintage(lookup.get("WorldBank", code) or {}), code


@needs_catalog
def test_menu_options_never_offer_vintage_sources_and_keep_servable_ones() -> None:
    from backend.services.indicator_clarification import dedupe_indicator_choice_options

    vintage = [r for p in _ARCHIVE_HIT_PHRASES for r in _wb_candidates(p) if _is_vintage(r)][:3]
    live = [r for p in _ARCHIVE_HIT_PHRASES for r in _wb_candidates(p) if not _is_vintage(r)][:3]
    options = [f"[WorldBank] {r['name']} ({r['code']})" for r in vintage + live]
    kept = dedupe_indicator_choice_options(None, options)
    kept_text = "\n".join(kept)
    assert not any(f"({r['code']})" in kept_text for r in vintage)
    assert all(f"({r['code']})" in kept_text for r in live)
    for r in vintage:
        assert option_supportability_reason("WorldBank", r["code"])
    # Servable historical sources (no Version dimension) stay offerable,
    # whatever their source is called.
    conn = _lookup().db._get_connection()
    other_sources = conn.execute(
        "SELECT code, raw_metadata FROM indicators WHERE provider='WorldBank' "
        "AND json_extract(raw_metadata,'$.source.value') LIKE '%rchive%' LIMIT 200"
    ).fetchall()
    servable_named_archive = [row[0] for row in other_sources if not catalog_row_is_vintage_archive("WorldBank", row[1])]
    for code in servable_named_archive[:5]:
        assert option_supportability_reason("WorldBank", code) is None
    # Non-WB providers and unknown codes are unaffected.
    assert option_supportability_reason("FRED", "UNRATE") is None
    assert option_supportability_reason("WorldBank", "NO.SUCH.CODE.XYZ") is None
