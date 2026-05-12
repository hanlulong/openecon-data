from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from backend.models import NormalizedData, ParsedIntent
from backend.services.data_fetcher import (
    _fetch_from_statscan,
    _restore_semantic_indicator_label_for_generic_metadata,
)
from backend.services.parameter_validator import ParameterValidator
from backend.tests.utils import run


def _sample_statscan_series() -> NormalizedData:
    return NormalizedData.model_validate(
        {
            "metadata": {
                "source": "Statistics Canada",
                "indicator": "Estimates of the number of private households by size on July 1st",
                "country": "Canada",
                "frequency": "annual",
                "unit": "Number",
                "lastUpdated": "2026-01-01",
                "seriesId": "17100159:1.1.1.0.0.0.0.0.0.0",
                "apiUrl": "https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=1710015901",
            },
            "data": [{"date": "2017-01-01", "value": 14587316.0}],
        }
    )


class StatsCanDateDispatchTests(unittest.TestCase):
    def test_exact_statscan_title_request_does_not_get_recent_default_dates(self) -> None:
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=[
                "Health behaviour in school-aged children 2002, student response to question: "
                "In the last 12 months, how many times did you travel away on holiday with your family?"
            ],
            parameters={
                "indicator": "13100290",
                "__semantic_indicator_label": "Health behaviour in school-aged children 2002",
                "__exact_indicator_title_match": True,
            },
            clarificationNeeded=False,
            originalQuery=(
                "Canada Health behaviour in school-aged children 2002, student response to question: "
                "In the last 12 months, how many times did you travel away on holiday with your family? "
                "from Statistics Canada"
            ),
        )

        ParameterValidator.apply_default_time_periods(intent)

        self.assertNotIn("startDate", intent.parameters)
        self.assertNotIn("endDate", intent.parameters)

    def test_dynamic_dispatch_preserves_requested_start_and_end_dates(self) -> None:
        statscan_provider = SimpleNamespace(
            PRODUCT_ID_CACHE={},
            fetch_dynamic_data=AsyncMock(return_value=_sample_statscan_series()),
            fetch_series=AsyncMock(side_effect=AssertionError("dynamic path should not use vector fetch")),
        )
        svc = SimpleNamespace(statscan_provider=statscan_provider)
        intent = ParsedIntent(
            apiProvider="STATSCAN",
            indicators=["number of households"],
            parameters={
                "country": "CA",
                "indicator": "17100159",
                "__semantic_indicator_label": "number of households",
                "startDate": "2017-01-01",
                "endDate": "2017-12-31",
                "periods": 240,
            },
            clarificationNeeded=False,
            originalQuery="number of households in Canada in 2017",
        )

        result = run(_fetch_from_statscan(svc, intent, dict(intent.parameters or {})))

        self.assertEqual(len(result), 1)
        statscan_provider.fetch_dynamic_data.assert_awaited_once()
        dynamic_params = statscan_provider.fetch_dynamic_data.await_args.args[0]
        self.assertEqual(dynamic_params["indicator"], "17100159")
        self.assertEqual(dynamic_params["indicatorLabel"], "number of households")
        self.assertEqual(dynamic_params["startDate"], "2017-01-01")
        self.assertEqual(dynamic_params["endDate"], "2017-12-31")

    def test_generic_vector_metadata_uses_semantic_label_for_display(self) -> None:
        series = _sample_statscan_series()
        series.metadata.indicator = "Vector 41690914"

        _restore_semantic_indicator_label_for_generic_metadata(
            [series],
            {
                "indicator": "18100004",
                "__semantic_indicator_label": "consumer price index",
            },
        )

        self.assertEqual(series.metadata.indicator, "consumer price index")


if __name__ == "__main__":
    unittest.main()
