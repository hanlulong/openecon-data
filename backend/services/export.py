from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import List, Optional

import pandas as pd

from ..models import NormalizedData


class ExportService:
    def generate_csv(self, data: List[NormalizedData]) -> str:
        if not data:
            return ""

        buffer = io.StringIO()

        # Write metadata as comments
        first = data[0]
        buffer.write(f"# Source: {self._csv_safe(first.metadata.source)}\n")
        buffer.write(f"# Retrieved: {datetime.now(timezone.utc).isoformat()}\n")
        buffer.write(f"# Indicator: {self._csv_safe(first.metadata.indicator)}\n")
        if first.metadata.seriesId:
            buffer.write(f"# Series ID: {self._csv_safe(first.metadata.seriesId)}\n")
        buffer.write(f"# Unit: {self._csv_safe(first.metadata.unit)}\n")
        buffer.write(f"# Frequency: {self._csv_safe(first.metadata.frequency)}\n")
        if first.metadata.apiUrl:
            buffer.write(f"# API URL: {self._csv_safe(first.metadata.apiUrl)}\n")
        buffer.write("#\n")

        if len(data) == 1:
            series = data[0]
            writer = csv.DictWriter(
                buffer,
                fieldnames=["date", "value", "indicator", "country", "unit"],
                quoting=csv.QUOTE_ALL,
            )
            writer.writeheader()
            for point in series.data:
                writer.writerow(
                    {
                        "date": point.date,
                        "value": "" if point.value is None else point.value,
                        "indicator": self._csv_safe(series.metadata.indicator),
                        "country": self._csv_safe(series.metadata.country or ""),
                        "unit": self._csv_safe(series.metadata.unit),
                    }
                )
        else:
            all_dates = sorted({dp.date for series in data for dp in series.data})
            series_maps = []
            column_names = []

            for series in data:
                mapping = {dp.date: dp.value for dp in series.data}
                series_maps.append(mapping)

                parts = [
                    self._slug(series.metadata.seriesId),
                    self._slug(series.metadata.country),
                    self._slug(series.metadata.indicator),
                ]
                column_names.append("_".join(filter(None, parts)) or "series")

            writer = csv.DictWriter(
                buffer,
                fieldnames=["date", *column_names],
                quoting=csv.QUOTE_ALL,
            )
            writer.writeheader()

            for date in all_dates:
                row = {"date": date}
                for name, mapping in zip(column_names, series_maps):
                    value = mapping.get(date)
                    row[name] = "" if value is None else value
                writer.writerow(row)

        return buffer.getvalue()

    def generate_json(self, data: List[NormalizedData]) -> str:
        payload = {
            "metadata": {
                "exportDate": datetime.now(timezone.utc).isoformat(),
                "seriesCount": len(data),
            },
            "series": [item.model_dump() for item in data],
        }
        return json.dumps(payload, indent=2)

    def generate_dta(self, data: List[NormalizedData]) -> bytes:
        """Generate Stata .dta file from normalized data."""
        if not data:
            return b""

        if len(data) == 1:
            # Single series - simple format
            series = data[0]
            df = pd.DataFrame([
                {
                    "date": point.date,
                    "value": point.value,
                    "indicator": series.metadata.indicator,
                    "country": series.metadata.country or "",
                    "unit": series.metadata.unit,
                }
                for point in series.data
            ])
        else:
            # Multiple series - wide format with date as index
            all_dates = sorted({dp.date for series in data for dp in series.data})

            # Build column data
            rows = []
            for date in all_dates:
                row = {"date": date}
                for series in data:
                    # Create column name from series metadata
                    parts = [
                        self._slug(series.metadata.seriesId),
                        self._slug(series.metadata.country),
                        self._slug(series.metadata.indicator),
                    ]
                    col_name = "_".join(filter(None, parts)) or "series"
                    # Stata variable names have max 32 chars
                    col_name = col_name[:32]

                    # Find value for this date
                    value = None
                    for dp in series.data:
                        if dp.date == date:
                            value = dp.value
                            break
                    row[col_name] = value
                rows.append(row)

            df = pd.DataFrame(rows)

        # Convert date column to datetime for better Stata compatibility
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")

        # Write to bytes buffer
        buffer = io.BytesIO()
        df.to_stata(buffer, write_index=False, version=118)
        return buffer.getvalue()

    def generate_filename(self, data: List[NormalizedData], file_format: str) -> str:
        if not data:
            return f"export_{int(datetime.now(timezone.utc).timestamp())}.{file_format}"

        series = data[0]
        indicator = self._slug(series.metadata.indicator, limit=30)
        series_id = self._slug(series.metadata.seriesId, limit=30)
        country = self._slug(series.metadata.country, limit=20)

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        parts = [part for part in (series_id, indicator) if part]
        if country:
            parts.append(country)
        parts.append(timestamp)
        if not any(parts[:-1]):
            parts.insert(0, "export")
        return f"{'_'.join(parts)}.{file_format}"

    @staticmethod
    def _csv_safe(value: Optional[str]) -> str:
        """Neutralize spreadsheet formula injection in string cells.

        Excel/Sheets evaluate a cell whose first character is one of = + - @
        (or a leading TAB/CR before such a trigger). Provider-supplied strings
        (indicator/country/unit/source/apiUrl) flow into the CSV verbatim, so a
        crafted value like ``=HYPERLINK("http://evil",...)`` would execute on
        open. Prefixing a single quote forces the cell to render as text. This
        is structural (first-character guard), not a semantic denylist.
        """
        if value is None:
            return ""
        text = str(value)
        if text and text.lstrip("\t\r")[:1] in ("=", "+", "-", "@"):
            return "'" + text
        return text

    @staticmethod
    def _slug(value: Optional[str], limit: Optional[int] = None) -> str:
        if not value:
            return ""
        slug = "".join(ch if ch.isalnum() else "_" for ch in value).strip("_")
        if limit:
            return slug[:limit]
        return slug


export_service = ExportService()
