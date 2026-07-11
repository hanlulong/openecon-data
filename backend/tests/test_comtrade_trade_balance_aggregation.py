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
