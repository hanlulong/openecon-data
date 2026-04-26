from __future__ import annotations

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services.data_fetcher import _fetch_from_imf
from backend.tests.utils import run


class _FakeIMFProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _resolve_countries(self, country: str) -> list[str]:
        return [country]

    async def fetch_indicator(self, *, indicator: str, country: str, start_year=None, end_year=None):
        self.calls.append(
            {
                "indicator": indicator,
                "country": country,
                "start_year": start_year,
                "end_year": end_year,
            }
        )
        return NormalizedData(
            metadata=Metadata(
                source="IMF",
                indicator=indicator,
                country=country,
                frequency="annual",
                unit="",
                seriesId=indicator,
            ),
            data=[{"date": f"{start_year}-01-01", "value": 1.0}],
        )


class _FakeService:
    def __init__(self) -> None:
        self.imf_provider = _FakeIMFProvider()


def test_fetch_from_imf_derives_years_from_default_date_params() -> None:
    svc = _FakeService()
    intent = ParsedIntent(
        apiProvider="IMF",
        indicators=["TXG_FOB_USD"],
        parameters={
            "indicator": "TXG_FOB_USD",
            "country": "USA",
            "startDate": "2021-04-27",
            "endDate": "2026-04-26",
        },
        clarificationNeeded=False,
    )

    result = run(_fetch_from_imf(svc, intent, intent.parameters))

    assert len(result) == 1
    assert svc.imf_provider.calls == [
        {
            "indicator": "TXG_FOB_USD",
            "country": "USA",
            "start_year": 2021,
            "end_year": 2026,
        }
    ]
