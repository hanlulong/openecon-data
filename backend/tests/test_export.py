from __future__ import annotations

import unittest

from backend.models import NormalizedData
from backend.services.export import export_service


def build_series() -> NormalizedData:
    return NormalizedData.model_validate(
        {
            "metadata": {
                "source": "Eurostat",
                "indicator": "Gross Domestic Product",
                "country": "DE",
                "frequency": "annual",
                "unit": "Million euro",
                "lastUpdated": "2024-01-01",
                "seriesId": "nama_10_gdp",
                "apiUrl": "https://example.com/api/data/nama_10_gdp?geo=DE",
            },
            "data": [
                {"date": "2020-01-01", "value": 1000},
                {"date": "2021-01-01", "value": 1100},
            ],
        }
    )


class ExportServiceTests(unittest.TestCase):
    def test_generate_csv_includes_metadata(self) -> None:
        csv_output = export_service.generate_csv([build_series()])
        self.assertIn("# Source: Eurostat", csv_output)
        self.assertIn("# Series ID: nama_10_gdp", csv_output)
        self.assertIn("# API URL: https://example.com/api/data/nama_10_gdp?geo=DE", csv_output)

    def test_generate_filename_uses_series_id(self) -> None:
        filename = export_service.generate_filename([build_series()], "csv")
        self.assertTrue(filename.startswith("nama_10_gdp"))
        self.assertTrue(filename.endswith(".csv"))


if __name__ == "__main__":
    unittest.main()
