"""Guard tests for two Comtrade framework fixes.

FIX 1 — region trade balance must SUM member series, not report member[0].
  A region reporter/partner is expanded to one series per member inside
  fetch_trade_data. The balance builder previously took exports_data[0]/
  imports_data[0], reporting a single member's balance under the region's
  name. It must now sum members per period, label genuine aggregates with the
  group name, attach a transparency note, and disclose partial coverage.

FIX 2 — the commodity resolver must fail closed on unrecognized commodities
  instead of the order-dependent substring tier (which mapped "vegetable oil"
  -> "OIL" -> HS 27) and the silent TOTAL default. An explicit total/all
  request still resolves to TOTAL; an absent commodity (None) still resolves to
  TOTAL.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.models import DataPoint, Metadata, NormalizedData
from backend.providers.comtrade import ComtradeProvider
from backend.utils.retry import DataNotAvailableError


def _series(country: str, indicator: str, points):
    return NormalizedData(
        metadata=Metadata(
            source="UN Comtrade",
            indicator=indicator,
            country=country,
            frequency="annual",
            unit="US Dollars",
            apiUrl="http://example/comtrade",
        ),
        data=[DataPoint(date=d, value=v) for d, v in points],
    )


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# FIX 1: region trade balance aggregation
# --------------------------------------------------------------------------
def test_region_trade_balance_sums_members_and_notes(monkeypatch):
    """3 EU members -> balance = sum of members, aggregation note attached."""
    provider = ComtradeProvider(api_key="demo")

    exports = [
        _series("United States", "Exports to France - Total Trade", [("2020", 10), ("2021", 11)]),
        _series("United States", "Exports to Germany - Total Trade", [("2020", 20), ("2021", 22)]),
        _series("United States", "Exports to Italy - Total Trade", [("2020", 5), ("2021", 6)]),
    ]
    imports = [
        _series("United States", "Imports from France - Total Trade", [("2020", 7), ("2021", 8)]),
        _series("United States", "Imports from Germany - Total Trade", [("2020", 15), ("2021", 16)]),
        _series("United States", "Imports from Italy - Total Trade", [("2020", 3), ("2021", 4)]),
    ]

    async def fake_fetch(reporter, partner, commodity, flow, start_year, end_year, frequency):
        return exports if "EXPORT" in flow else imports

    monkeypatch.setattr(provider, "fetch_trade_data", fake_fetch)
    monkeypatch.setattr(
        ComtradeProvider,
        "_region_expansion_count",
        staticmethod(lambda name: 3 if name == "EU" else None),
    )

    result = _run(provider.fetch_trade_balance(reporter="US", partner="EU"))

    # 2020: exports 10+20+5=35, imports 7+15+3=25 -> balance 10
    # 2021: exports 11+22+6=39, imports 8+16+4=28 -> balance 11
    assert [(p.date, p.value) for p in result.data] == [("2020", 10.0), ("2021", 11.0)]
    # Single reporter (US) across all series -> keep the resolved reporter name.
    assert result.metadata.country == "United States"
    assert result.metadata.indicator == "Trade Balance with EU"
    assert result.metadata.notes and "summed 3 export and 3 import" in result.metadata.notes[0]
    assert "'EU'" in result.metadata.notes[0]


def test_region_trade_balance_partial_coverage_disclosed(monkeypatch):
    """Only 2 of an expected 3 members returned -> partial-coverage note."""
    provider = ComtradeProvider(api_key="demo")

    exports = [
        _series("United States", "Exports to France", [("2020", 10)]),
        _series("United States", "Exports to Germany", [("2020", 20)]),
    ]
    imports = [
        _series("United States", "Imports from France", [("2020", 7)]),
        _series("United States", "Imports from Germany", [("2020", 15)]),
    ]

    async def fake_fetch(reporter, partner, commodity, flow, start_year, end_year, frequency):
        return exports if "EXPORT" in flow else imports

    monkeypatch.setattr(provider, "fetch_trade_data", fake_fetch)
    monkeypatch.setattr(
        ComtradeProvider,
        "_region_expansion_count",
        staticmethod(lambda name: 3 if name == "EU" else None),
    )

    result = _run(provider.fetch_trade_balance(reporter="US", partner="EU"))
    assert [(p.date, p.value) for p in result.data] == [("2020", 8.0)]
    assert any("Partial coverage: 2 of 3" in note for note in result.metadata.notes)


def test_group_reporter_labels_with_group_name(monkeypatch):
    """When the reporter side is the group, distinct member countries -> the
    result is labeled with the group name, not the first member's country."""
    provider = ComtradeProvider(api_key="demo")

    exports = [
        _series("France", "Exports - Total Trade", [("2020", 10)]),
        _series("Germany", "Exports - Total Trade", [("2020", 20)]),
    ]
    imports = [
        _series("France", "Imports - Total Trade", [("2020", 4)]),
        _series("Germany", "Imports - Total Trade", [("2020", 6)]),
    ]

    async def fake_fetch(reporter, partner, commodity, flow, start_year, end_year, frequency):
        return exports if "EXPORT" in flow else imports

    monkeypatch.setattr(provider, "fetch_trade_data", fake_fetch)
    monkeypatch.setattr(
        ComtradeProvider,
        "_region_expansion_count",
        staticmethod(lambda name: 2 if name == "EU" else None),
    )

    result = _run(provider.fetch_trade_balance(reporter="EU", partner=None))
    # Two distinct reporters -> label with the group name passed in.
    assert result.metadata.country == "EU"
    assert [(p.date, p.value) for p in result.data] == [("2020", 20.0)]  # (10+20)-(4+6)


def test_single_country_trade_balance_unchanged(monkeypatch):
    """A single reporter/partner pair keeps the original behavior: no
    aggregation note, resolved single country, balance = exports - imports."""
    provider = ComtradeProvider(api_key="demo")

    async def fake_fetch(reporter, partner, commodity, flow, start_year, end_year, frequency):
        if "EXPORT" in flow:
            return [_series("United States", "Exports to China - Total Trade", [("2020", 100), ("2021", 110)])]
        return [_series("United States", "Imports from China - Total Trade", [("2020", 60), ("2021", 70)])]

    monkeypatch.setattr(provider, "fetch_trade_data", fake_fetch)

    result = _run(provider.fetch_trade_balance(reporter="US", partner="China"))
    assert result.metadata.notes is None
    assert result.metadata.country == "United States"
    assert [(p.date, p.value) for p in result.data] == [("2020", 40.0), ("2021", 40.0)]


def test_aggregate_helper_sums_periods_present():
    """_aggregate_member_series sums only the members present per period."""
    series = [
        _series("A", "x", [("2020", 5), ("2021", 6)]),
        _series("B", "x", [("2020", 3)]),  # missing 2021
    ]
    summed, count = ComtradeProvider._aggregate_member_series(series)
    assert count == 2
    assert summed == {"2020": 8.0, "2021": 6.0}


# --------------------------------------------------------------------------
# FIX 2: commodity resolver fails closed instead of substring / silent TOTAL
# --------------------------------------------------------------------------
def test_vegetable_oil_does_not_resolve_to_petroleum():
    """The compound term must NOT match "OIL" -> HS 27; it fails closed."""
    with pytest.raises(DataNotAvailableError) as exc:
        ComtradeProvider._commodity_code("vegetable oil")
    assert "vegetable oil" in str(exc.value)
    # And explicitly is not the petroleum chapter.
    assert "27" not in str(exc.value).split("did not match")[0]


def test_unrecognized_commodity_fails_closed():
    with pytest.raises(DataNotAvailableError):
        ComtradeProvider._commodity_code("totally random unknownxyz thing")


def test_absent_commodity_still_total():
    assert ComtradeProvider._commodity_code(None) == "TOTAL"
    assert ComtradeProvider._commodity_code("") == "TOTAL"


@pytest.mark.parametrize(
    "phrase",
    ["total", "all", "all commodities", "total trade", "all goods", "all products"],
)
def test_explicit_total_requests_resolve_to_total(phrase):
    assert ComtradeProvider._commodity_code(phrase) == "TOTAL"


@pytest.mark.parametrize(
    "phrase,code",
    [("oil", "27"), ("wheat", "1001"), ("cars", "8703"), ("8703", "8703")],
)
def test_exact_commodity_matches_unchanged(phrase, code):
    assert ComtradeProvider._commodity_code(phrase) == code


def test_is_total_trade_request_predicate():
    assert ComtradeProvider._is_total_trade_request("all commodities") is True
    assert ComtradeProvider._is_total_trade_request("total") is True
    assert ComtradeProvider._is_total_trade_request("vegetable oil") is False
    assert ComtradeProvider._is_total_trade_request("oil") is False
    # Contract updated in review: bare all-merchandise nouns MEAN total trade
    # ("goods"/"merchandise"/"products" alone are not unresolved commodities
    # to fail closed on — they are the total-trade request itself).
    assert ComtradeProvider._is_total_trade_request("merchandise") is True
    assert ComtradeProvider._is_total_trade_request("goods") is True
    # A real commodity phrase containing such a noun is still NOT total trade.
    assert ComtradeProvider._is_total_trade_request("leather goods") is False


# --------------------------------------------------------------------------
# Per-period member coverage (members file on their own schedules)
# --------------------------------------------------------------------------
def _group_reporter_fetch(monkeypatch, provider, exports, imports, members=3):
    async def fake_fetch(reporter, partner, commodity, flow, start_year, end_year, frequency):
        return exports if "EXPORT" in flow else imports

    monkeypatch.setattr(provider, "fetch_trade_data", fake_fetch)
    monkeypatch.setattr(
        ComtradeProvider,
        "_region_expansion_count",
        staticmethod(lambda name: members if name == "ASEAN" else None),
    )


def test_trailing_year_filed_by_few_members_is_disclosed(monkeypatch):
    """Nov 2026, 'ASEAN trade balance': only one of three members has filed
    2025, so the 2025 total is a one-member sum — disclosed per period."""
    provider = ComtradeProvider(api_key="demo")
    exports = [
        _series("Thailand", "Exports", [("2023", 10), ("2024", 11), ("2025", 12)]),
        _series("Viet Nam", "Exports", [("2023", 20), ("2024", 21)]),
        _series("Malaysia", "Exports", [("2023", 5), ("2024", 6)]),
    ]
    imports = [
        _series("Thailand", "Imports", [("2023", 7), ("2024", 8), ("2025", 9)]),
        _series("Viet Nam", "Imports", [("2023", 15), ("2024", 16)]),
        _series("Malaysia", "Imports", [("2023", 3), ("2024", 4)]),
    ]
    _group_reporter_fetch(monkeypatch, provider, exports, imports)

    result = _run(provider.fetch_trade_balance(reporter="ASEAN", partner=None))
    assert [(p.date, p.value) for p in result.data] == [("2023", 10.0), ("2024", 10.0), ("2025", 3.0)]
    notes = result.metadata.notes or []
    period_notes = [note for note in notes if note.startswith("Partial member coverage")]
    assert len(period_notes) == 1
    assert "2025 (1 of 3)" in period_notes[0]
    assert "2023" not in period_notes[0] and "2024" not in period_notes[0]
    # Every member returned data, so the whole-member note does not fire.
    assert not any(note.startswith("Partial coverage:") for note in notes)


def test_fully_covered_group_gets_no_period_note(monkeypatch):
    provider = ComtradeProvider(api_key="demo")
    exports = [_series(c, "Exports", [("2023", 1), ("2024", 2)]) for c in ("A", "B", "C")]
    imports = [_series(c, "Imports", [("2023", 1), ("2024", 1)]) for c in ("A", "B", "C")]
    _group_reporter_fetch(monkeypatch, provider, exports, imports)

    result = _run(provider.fetch_trade_balance(reporter="ASEAN", partner=None))
    assert not any(note.startswith("Partial member coverage") for note in result.metadata.notes or [])


def test_period_with_one_flow_missing_counts_as_partial(monkeypatch):
    """A member that filed 2024 exports but not imports leaves 2024's balance
    over fewer complete members."""
    provider = ComtradeProvider(api_key="demo")
    exports = [_series(c, "Exports", [("2023", 1), ("2024", 2)]) for c in ("A", "B")]
    imports = [
        _series("A", "Imports", [("2023", 1), ("2024", 1)]),
        _series("B", "Imports", [("2023", 1)]),
    ]
    _group_reporter_fetch(monkeypatch, provider, exports, imports, members=2)

    result = _run(provider.fetch_trade_balance(reporter="ASEAN", partner=None))
    assert any("2024 (1 of 2)" in note for note in result.metadata.notes or [])


def test_member_disclosures_are_carried_into_the_group_balance(monkeypatch):
    """fetch_trade_data's consolidated trailing-year notes describe the group
    total too; the rebuilt Metadata must keep them (once each)."""
    provider = ComtradeProvider(api_key="demo")
    gap = ("UN Comtrade returned no annual rows for 2025 from Viet Nam, Malaysia "
           "(not yet reported, or not available for this selection); latest period returned: 2024.")
    exports = [
        _series("Thailand", "Exports", [("2024", 11), ("2025", 12)]),
        _series("Viet Nam", "Exports", [("2024", 21)]),
        _series("Malaysia", "Exports", [("2024", 6)]),
    ]
    imports = [
        _series("Thailand", "Imports", [("2024", 8), ("2025", 9)]),
        _series("Viet Nam", "Imports", [("2024", 16)]),
        _series("Malaysia", "Imports", [("2024", 4)]),
    ]
    for series in exports[1:] + imports[1:]:
        series.metadata.notes = [gap]
    _group_reporter_fetch(monkeypatch, provider, exports, imports)

    result = _run(provider.fetch_trade_balance(reporter="ASEAN", partner=None))
    notes = result.metadata.notes or []
    assert notes.count(gap) == 1
    assert notes[0].startswith("Trade balance aggregated over")
    assert notes.index(gap) > 0


def test_single_pair_balance_carries_its_inputs_notes(monkeypatch):
    provider = ComtradeProvider(api_key="demo")
    gap = ("UN Comtrade returned no annual rows for 2025 from Canada "
           "(not yet reported, or not available for this selection); latest period returned: 2024.")

    async def fake_fetch(reporter, partner, commodity, flow, start_year, end_year, frequency):
        series = _series("Canada", f"{flow} - Total Trade", [("2023", 100), ("2024", 110)])
        series.metadata.notes = [gap]
        return [series]

    monkeypatch.setattr(provider, "fetch_trade_data", fake_fetch)
    result = _run(provider.fetch_trade_balance(reporter="Canada", partner="US"))
    assert result.metadata.notes == [gap]


def test_member_period_coverage_ignores_missing_values():
    series = [
        _series("A", "x", [("2020", 5), ("2021", None)]),
        _series("B", "x", [("2020", 3), ("2021", 4)]),
    ]
    assert ComtradeProvider._member_period_coverage(series) == {"2020": 2, "2021": 1}


def test_partial_period_note_lists_quarters_and_bounds_the_list():
    coverage = {f"{y}-{m:02d}-01": 3 for y in (2023, 2024) for m in (1, 4, 7, 10)}
    coverage.update({f"2025-{m:02d}-01": 1 for m in (1, 4, 7, 10)})
    note = ComtradeProvider._partial_period_coverage_note(coverage, dict(coverage), "Q", max_listed=3)
    assert note and "2025-Q1 (1 of 3)" in note and "2025-Q3 (1 of 3)" in note
    assert "2025-Q4" not in note and "and 1 more period(s)" in note
    assert "2024" not in note
