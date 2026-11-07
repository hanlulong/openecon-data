"""Historical FX via FRED: never relabel a USD leg as a cross rate, and never
serve a USD leg in the wrong quote direction.

Every FRED FX series is USD-based, so fetch_historical_exchange_from_fred can
only serve pairs with exactly one USD leg. The old code, when neither leg was
USD, returned a single USD leg (e.g. DEXUSUK = USD/GBP) stamped with the
requested cross-rate label ("EUR to GBP") — wrong data shown as success. It now
fails closed for cross rates.

FRED also mixes quote conventions ("U.S. Dollars to One Euro" vs "Swiss Francs
to One U.S. Dollar"). The served direction is derived from the fetched series'
own units text; a series quoted the other way is inverted (1/x) and disclosed.
"""

import asyncio

import pytest

from backend.models import DataPoint, Metadata, NormalizedData, ParsedIntent
from backend.services.data_fetcher import (
    fetch_historical_exchange_from_fred,
    orient_fx_series,
    parse_fx_quote_orientation,
)

# Test fixture copied from backend/data/indicators.db (FRED units text), with
# an INDEPENDENT ground-truth direction flag and a plausible USD price of one
# unit of the foreign currency (2020-2024 order of magnitude).
_FRED_FX = {
    # ccy: (series_id, native units text, native_is_usd_per_foreign, usd_per_1_foreign)
    "EUR": ("DEXUSEU", "U.S. Dollars to One Euro", True, 1.10),
    "GBP": ("DEXUSUK", "U.S. Dollars to One U.K. Pound Sterling", True, 1.27),
    "JPY": ("DEXJPUS", "Japanese Yen to One U.S. Dollar", False, 1 / 150.0),
    "CAD": ("DEXCAUS", "Canadian Dollars to One U.S. Dollar", False, 1 / 1.36),
    "CHF": ("DEXSZUS", "Swiss Francs to One U.S. Dollar", False, 1 / 0.823),
    "AUD": ("DEXUSAL", "U.S. Dollars to One Australian Dollar", True, 0.66),
    "CNY": ("DEXCHUS", "Chinese Yuan Renminbi to One U.S. Dollar", False, 1 / 7.1),
    "MXN": ("DEXMXUS", "Mexican Pesos to One U.S. Dollar", False, 1 / 17.5),
    "INR": ("DEXINUS", "Indian Rupees to One U.S. Dollar", False, 1 / 83.0),
    "BRL": ("DEXBZUS", "Brazilian Reals to One U.S. Dollar", False, 1 / 5.0),
    "KRW": ("DEXKOUS", "South Korean Won to One U.S. Dollar", False, 1 / 1300.0),
    "SEK": ("DEXSDUS", "Swedish Kronor to One U.S. Dollar", False, 1 / 10.5),
    "NOK": ("DEXNOUS", "Norwegian Kroner to One U.S. Dollar", False, 1 / 10.6),
    "DKK": ("DEXDNUS", "Danish Kroner to One U.S. Dollar", False, 1 / 6.9),
    "SGD": ("DEXSIUS", "Singapore Dollars to One U.S. Dollar", False, 1 / 1.35),
    "HKD": ("DEXHKUS", "Hong Kong Dollars to One U.S. Dollar", False, 1 / 7.8),
    "NZD": ("DEXUSNZ", "U.S. Dollars to One New Zealand Dollar", True, 0.61),
    "ZAR": ("DEXSFUS", "South African Rand to One U.S. Dollar", False, 1 / 18.5),
    "THB": ("DEXTHUS", "Thai Baht to One U.S. Dollar", False, 1 / 35.0),
    "MYR": ("DEXMAUS", "Malaysian Ringgit to One U.S. Dollar", False, 1 / 4.6),
    "TWD": ("DEXTAUS", "Taiwan Dollars to One U.S. Dollar", False, 1 / 31.5),
}
_BY_ID = {sid: (ccy, units, usd_first, px) for ccy, (sid, units, usd_first, px) in _FRED_FX.items()}


def _native_series(series_id, unit=None, values=None):
    ccy, units, usd_first, usd_px = _BY_ID[series_id]
    native_value = usd_px if usd_first else 1.0 / usd_px
    if values is None:
        values = [native_value, None, native_value]
    return NormalizedData(
        metadata=Metadata(
            source="FRED",
            indicator=f"native title {series_id}",
            country="United States",
            frequency="daily",
            unit=units if unit is None else unit,
            seriesId=series_id,
        ),
        data=[DataPoint(date=f"2020-01-0{i + 1}", value=v) for i, v in enumerate(values)],
    )


class _FakeFred:
    def __init__(self, unit_override=None):
        self.unit_override = unit_override
        self.calls = []

    async def fetch_series(self, params):
        self.calls.append(params["seriesId"])
        return _native_series(params["seriesId"], unit=self.unit_override)


class _FakeSvc:
    def __init__(self, unit_override=None):
        self.fred_provider = _FakeFred(unit_override)


def _intent(base, tgt, query=None):
    return ParsedIntent(
        apiProvider="ExchangeRate",
        indicators=["exchange rate"],
        parameters={},
        clarificationNeeded=False,
        originalQuery=query or f"{base} to {tgt} 2020 to 2022",
    )


def _run(base, tgt, svc=None, query=None):
    params = {
        "baseCurrency": base,
        "targetCurrency": tgt,
        "startDate": "2020-01-01",
        "endDate": "2022-01-01",
    }
    return asyncio.run(
        fetch_historical_exchange_from_fred(svc or _FakeSvc(), _intent(base, tgt, query), params)
    )


def test_cross_rate_fails_closed():
    assert _run("EUR", "GBP") is None
    assert _run("EUR", "JPY") is None


def test_usd_legs_still_served():
    for base, tgt in (("USD", "GBP"), ("GBP", "USD")):
        result = _run(base, tgt)
        assert result is not None and len(result) == 1
        assert result[0].metadata.indicator == f"{base} to {tgt} Exchange Rate"


@pytest.mark.parametrize("ccy", sorted(_FRED_FX))
@pytest.mark.parametrize("direction", ["usd_to_foreign", "foreign_to_usd"])
def test_every_table_currency_served_in_requested_direction(ccy, direction):
    usd_px = _FRED_FX[ccy][3]
    if direction == "usd_to_foreign":
        base, tgt, expected = "USD", ccy, 1.0 / usd_px  # foreign units per 1 USD
    else:
        base, tgt, expected = ccy, "USD", usd_px  # USD per 1 foreign unit
    (series,) = _run(base, tgt)
    meta = series.metadata
    assert meta.indicator == f"{base} to {tgt} Exchange Rate"
    assert meta.seriesId == _FRED_FX[ccy][0]  # provenance stays the true FRED id
    values = [p.value for p in series.data]
    assert values[1] is None  # None skipped, date kept
    assert values[0] == pytest.approx(expected, rel=1e-9)
    assert values[2] == pytest.approx(expected, rel=1e-9)
    inverted = parse_fx_quote_orientation(_FRED_FX[ccy][1]) != (
        "foreign_per_usd" if base == "USD" else "usd_per_foreign"
    )
    if inverted:
        assert meta.unit == f"{tgt} per 1 {base}"
        assert any(f"FRED {_FRED_FX[ccy][0]}" in n for n in (meta.notes or []))
    else:
        assert meta.unit == _FRED_FX[ccy][1]


@pytest.mark.parametrize(
    "base,tgt,lo,hi",
    [
        ("USD", "EUR", 0.85, 0.95),
        ("EUR", "USD", 1.05, 1.2),
        ("CHF", "USD", 1.15, 1.25),
        ("USD", "CHF", 0.8, 0.85),
        ("USD", "JPY", 140, 160),
        ("JPY", "USD", 1 / 160, 1 / 140),
        ("USD", "GBP", 0.7, 0.85),
        ("NZD", "USD", 0.55, 0.65),
    ],
)
def test_spot_values_plausible(base, tgt, lo, hi):
    (series,) = _run(base, tgt)
    assert lo <= series.data[0].value <= hi


def test_cny_to_usd_from_chinese_query():
    # "人民币兑美元" arrives as base=CNY target=USD; FRED DEXCHUS is yuan per USD.
    (series,) = _run("CNY", "USD", query="2020年以来人民币兑美元汇率")
    assert series.metadata.indicator == "CNY to USD Exchange Rate"
    assert series.metadata.unit == "USD per 1 CNY"
    assert 0.12 <= series.data[0].value <= 0.16


@pytest.mark.parametrize(
    "units,expected",
    [
        ("U.S. Dollars to One Euro", "usd_per_foreign"),
        ("Hong Kong Dollars to One U.S. Dollar", "foreign_per_usd"),
        ("Canadian Dollars to One U.S. Dollar", "foreign_per_usd"),
        ("U.S. Dollars to One Australian Dollar", "usd_per_foreign"),
        ("U.S. Dollars to One New Zealand Dollar", "usd_per_foreign"),
        ("Taiwan Dollars to One U.S. Dollar", "foreign_per_usd"),
        ("Index Jan 2006=100", None),
        ("", None),
        (None, None),
        ("Hong Kong Dollars to One Singapore Dollar", None),
    ],
)
def test_units_parser_is_exact_on_usd_leg(units, expected):
    assert parse_fx_quote_orientation(units) == expected


def test_unparseable_units_keep_native_label_and_values():
    (series,) = _run("CHF", "USD", svc=_FakeSvc(unit_override="Index"))
    meta = series.metadata
    assert meta.indicator == "native title DEXSZUS"  # no unverified relabel
    assert meta.unit == "Index"
    assert series.data[0].value == pytest.approx(0.823)  # not inverted
    assert any("could not be verified" in n for n in meta.notes)


def test_orientation_never_mutates_the_fetched_object():
    original = _native_series("DEXSZUS")
    oriented = orient_fx_series(original, "CHF", "USD")
    assert oriented is not original
    assert original.data[0].value == pytest.approx(0.823)
    assert original.metadata.unit == "Swiss Francs to One U.S. Dollar"
    assert original.metadata.notes is None
    assert oriented.data[0].value == pytest.approx(1 / 0.823)


def test_zero_value_becomes_none_when_inverting():
    original = _native_series("DEXSZUS", values=[0.0, 0.9])
    oriented = orient_fx_series(original, "CHF", "USD")
    assert oriented.data[0].value is None
    assert oriented.data[1].value == pytest.approx(1 / 0.9)
