"""Guard tests for three IMF provider framework fixes.

FIX 1 (T7): the SDMX exact-code path (``_fetch_sdmx_exact_indicator_family``)
returned as soon as the first country's candidate resolved, silently dropping
every other requested country from a multi-country comparison. It must now
accumulate the first success per country and return them all (partial coverage
is intentional — the pipeline discloses gaps).

FIX 2 (T13): ``_parse_sdmx_csv`` grouped rows into series by every column that
was not on a dead SDMX-standard exclusion list. The live IMF.STA CSV emits
STATUS/SCALE/DECIMALS_DISPLAYED (per-observation attributes), so a series whose
STATUS varied mid-history (provisional latest obs, rebases) was fragmented into
one series per STATUS value. Series identity must derive from the DSD's real
dimension ids; the fallback exclusion list must also cover the live names.

FIX 3: the DataMapper verification ``sourceUrl`` hardcoded ``@WEO`` for every
code, mislabeling GDD/FM/AFRREO/... codes. The dataset anchor must come from the
catalog entry (``category`` / raw ``dataset``); when not derivable, link the
dataset-agnostic indicator page rather than assert a wrong dataset.

These tests use synthetic fixtures and mocked fetches — they never touch the
live IMF API.
"""
from __future__ import annotations

from backend.providers.imf import IMFProvider
from backend.tests.utils import run


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class _CsvResp:
    """Minimal stand-in for the httpx response the SDMX path inspects."""

    def __init__(self, *, status_code: int = 200, text: str = "", content_type: str = "text/csv") -> None:
        self.status_code = status_code
        self.text = text
        self.headers = {"content-type": content_type}


class _JsonResp:
    """Minimal stand-in for the DataMapper JSON response."""

    def __init__(self, payload: dict, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


def _sdmx_csv_for(country: str) -> str:
    """A two-observation ITG series whose STATUS varies across the two rows.

    The STATUS variation doubles as a FIX 2 regression: even on the fallback
    grouping path (no structure metadata) the two rows must stay one series.
    """
    return (
        "DATAFLOW,COUNTRY,INDICATOR,TYPE_OF_TRANSFORMATION,FREQUENCY,"
        "TIME_PERIOD,OBS_VALUE,STATUS,SCALE,DECIMALS_DISPLAYED\n"
        f"IMF.STA:ITG(1.0),{country},XG,FOB,A,2020,100.0,A,Units,2\n"
        f"IMF.STA:ITG(1.0),{country},XG,FOB,A,2021,110.0,P,Units,2\n"
    )


def _trade_candidates(countries):
    return [
        {
            "flow": "ITG",
            "key": f"{c}.XG.FOB.A",
            "country": c,
            "frequency": "A",
            "unit": "FOB",
            "data_type": "Level",
        }
        for c in countries
    ]


def _install_sdmx_mocks(provider: IMFProvider, *, fail_countries=()):
    async def fake_get(client, url, *, headers=None, timeout=None, raise_on_status=True):
        url_s = str(url)
        for c in ("USA", "CAN", "GBR", "FRA"):
            if f"/{c}." in url_s:
                if c in fail_countries:
                    return _CsvResp(status_code=404, text="", content_type="text/plain")
                return _CsvResp(status_code=200, text=_sdmx_csv_for(c), content_type="text/csv")
        return _CsvResp(status_code=404, text="", content_type="text/plain")

    async def fake_structure(flow):
        # Force the conservative fallback grouping path in the CSV parser so this
        # FIX 1 test does not depend on a live structure fetch.
        return None

    provider._get_with_retry = fake_get
    provider._get_imf_dataflow_structure = fake_structure


# ---------------------------------------------------------------------------
# FIX 1 — multi-country accumulation on the SDMX exact-code path
# ---------------------------------------------------------------------------
def test_sdmx_returns_all_requested_countries() -> None:
    provider = IMFProvider()
    countries = ["USA", "CAN", "GBR"]
    _install_sdmx_mocks(provider)

    results = run(
        provider._fetch_sdmx_exact_indicator_family(
            indicator_code="TXG_FOB_USD",
            indicator_label="Exports",
            candidates=_trade_candidates(countries),
            start_year=None,
            end_year=None,
        )
    )

    # Regression: the old `if results: return results` returned only the first
    # country. Every requested country that resolves must now be present.
    assert len(results) == 3
    assert len({r.metadata.country for r in results}) == 3
    # Each country's two STATUS-varying rows stay one series (FIX 2 fallback).
    assert all(len(r.data) == 2 for r in results)


def test_sdmx_returns_partial_coverage_when_one_country_fails() -> None:
    provider = IMFProvider()
    countries = ["USA", "CAN", "GBR"]
    _install_sdmx_mocks(provider, fail_countries={"GBR"})

    results = run(
        provider._fetch_sdmx_exact_indicator_family(
            indicator_code="TXG_FOB_USD",
            indicator_label="Exports",
            candidates=_trade_candidates(countries),
            start_year=None,
            end_year=None,
        )
    )

    # Two of three resolve; the failed country is dropped, not the whole batch.
    assert len(results) == 2
    assert len({r.metadata.country for r in results}) == 2


def test_sdmx_multiple_candidates_per_country_take_first_success() -> None:
    """A country satisfied by an earlier candidate skips its later candidates,
    but a second country is still attempted (first-success-per-country)."""
    provider = IMFProvider()
    # USA has two candidates (annual then monthly); CAN has one. Only the first
    # USA candidate should be used, and CAN must still be fetched.
    candidates = [
        {"flow": "ITG", "key": "USA.XG.FOB.A", "country": "USA", "frequency": "A", "unit": "FOB", "data_type": "Level"},
        {"flow": "ITG", "key": "USA.XG.FOB.M", "country": "USA", "frequency": "M", "unit": "FOB", "data_type": "Level"},
        {"flow": "ITG", "key": "CAN.XG.FOB.A", "country": "CAN", "frequency": "A", "unit": "FOB", "data_type": "Level"},
    ]

    attempted_urls: list[str] = []

    async def fake_get(client, url, *, headers=None, timeout=None, raise_on_status=True):
        attempted_urls.append(str(url))
        for c in ("USA", "CAN"):
            if f"/{c}." in str(url):
                return _CsvResp(status_code=200, text=_sdmx_csv_for(c), content_type="text/csv")
        return _CsvResp(status_code=404, text="", content_type="text/plain")

    async def fake_structure(flow):
        return None

    provider._get_with_retry = fake_get
    provider._get_imf_dataflow_structure = fake_structure

    results = run(
        provider._fetch_sdmx_exact_indicator_family(
            indicator_code="TXG_FOB_USD",
            indicator_label="Exports",
            candidates=candidates,
            start_year=None,
            end_year=None,
        )
    )

    assert len(results) == 2
    # The monthly USA candidate must never be fetched (USA already satisfied).
    assert not any("USA.XG.FOB.M" in u for u in attempted_urls)
    assert any("CAN.XG.FOB.A" in u for u in attempted_urls)


# ---------------------------------------------------------------------------
# FIX 2 — CSV series identity from real dimensions, not attribute columns
# ---------------------------------------------------------------------------
_CPI_STATUS_VARIES_CSV = (
    "DATAFLOW,COUNTRY,INDICATOR,COICOP_1999,TYPE_OF_TRANSFORMATION,FREQUENCY,"
    "TIME_PERIOD,OBS_VALUE,STATUS,SCALE,DECIMALS_DISPLAYED\n"
    "IMF.STA:CPI(4.0.0),USA,CPI,_T,IX,A,2019,100.0,A,Units,2\n"
    "IMF.STA:CPI(4.0.0),USA,CPI,_T,IX,A,2020,101.5,P,Units,2\n"
)

_CPI_DIMENSION_IDS = ["COUNTRY", "INDICATOR", "COICOP_1999", "TYPE_OF_TRANSFORMATION", "FREQUENCY"]


def test_status_variation_does_not_fragment_series_with_structure() -> None:
    provider = IMFProvider()
    series = provider._parse_sdmx_csv(_CPI_STATUS_VARIES_CSV, _CPI_DIMENSION_IDS)
    # Rows are identical on every real dimension and differ only in STATUS
    # (per-observation attribute) — one series, two observations.
    assert len(series) == 1
    _dims, observations = series[0]
    assert len(observations) == 2


def test_status_variation_does_not_fragment_series_fallback() -> None:
    provider = IMFProvider()
    # No structure metadata -> conservative exclusion fallback, which must now
    # exclude the live STATUS/SCALE/DECIMALS_DISPLAYED names as well.
    series = provider._parse_sdmx_csv(_CPI_STATUS_VARIES_CSV)
    assert len(series) == 1
    _dims, observations = series[0]
    assert len(observations) == 2


def test_real_dimension_still_separates_series() -> None:
    """The allowlist must not over-merge: a differing real dimension
    (FREQUENCY) still yields distinct series."""
    provider = IMFProvider()
    csv_text = (
        "DATAFLOW,COUNTRY,INDICATOR,COICOP_1999,TYPE_OF_TRANSFORMATION,FREQUENCY,"
        "TIME_PERIOD,OBS_VALUE,STATUS,SCALE,DECIMALS_DISPLAYED\n"
        "IMF.STA:CPI(4.0.0),USA,CPI,_T,IX,A,2020,100.0,A,Units,2\n"
        "IMF.STA:CPI(4.0.0),USA,CPI,_T,IX,M,2020-01,100.2,A,Units,2\n"
    )
    series = provider._parse_sdmx_csv(csv_text, _CPI_DIMENSION_IDS)
    assert len(series) == 2


# ---------------------------------------------------------------------------
# FIX 3 — DataMapper sourceUrl dataset anchor from the catalog, not "@WEO"
# ---------------------------------------------------------------------------
def test_datamapper_sourceurl_uses_catalog_dataset_not_weo() -> None:
    provider = IMFProvider()
    code = "CG_DEBT_GDP"  # catalog category = GDD (a real non-WEO dataset)

    async def fake_get(client, url, *, raise_on_status=True, timeout=None, **kwargs):
        return _JsonResp({"values": {code: {"USA": {"2020": 100.0, "2021": 105.0}}}})

    provider._get_with_retry = fake_get

    results = run(provider.fetch_batch_indicator(indicator=code, countries=["USA"]))

    assert len(results) == 1
    source_url = results[0].metadata.sourceUrl
    assert "@GDD" in source_url
    assert "@WEO" not in source_url
    assert source_url == f"https://www.imf.org/external/datamapper/{code}@GDD/USA"


def test_datamapper_sourceurl_falls_back_to_generic_page_when_dataset_unknown(monkeypatch) -> None:
    provider = IMFProvider()
    fake_code = "ZZ_NOT_IN_CATALOG_123"

    async def fake_resolve(indicator):
        return (fake_code, "Fabricated indicator")

    async def fake_get(client, url, *, raise_on_status=True, timeout=None, **kwargs):
        return _JsonResp({"values": {fake_code: {"USA": {"2020": 1.0}}}})

    monkeypatch.setattr(provider, "_resolve_indicator_code", fake_resolve)
    provider._get_with_retry = fake_get

    results = run(provider.fetch_batch_indicator(indicator=fake_code, countries=["USA"]))

    assert len(results) == 1
    source_url = results[0].metadata.sourceUrl
    # No derivable dataset -> generic indicator page, never a wrong "@WEO".
    assert "@" not in source_url.rsplit("/", 1)[-1]
    assert "@WEO" not in source_url
    assert source_url == f"https://www.imf.org/external/datamapper/{fake_code}"
