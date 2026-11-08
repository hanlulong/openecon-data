from __future__ import annotations

import csv
import io
import json
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple
from urllib.parse import quote

import pandas as pd

from ..models import NormalizedData

# Date-label shapes the providers emit, each with its granularity. Labels are
# converted to datetimes for Stata only when every non-empty label matches one
# of these and all share one granularity; otherwise the original strings are
# kept, so a valid label never becomes a missing value.
_QUARTER_START_MONTH = {"1": 1, "2": 4, "3": 7, "4": 10}
_DATE_LABEL_SHAPES: Tuple[Tuple["re.Pattern[str]", str], ...] = (
    (re.compile(r"(\d{4})"), "year"),                                    # 2020
    (re.compile(r"(\d{4})-?Q([1-4])", re.IGNORECASE), "quarter"),        # 2020-Q1, 2020Q1
    (re.compile(r"(\d{4})-?M(\d{1,2})", re.IGNORECASE), "month"),       # 2020M01, 2020-M01
    (re.compile(r"(\d{4})-(\d{2})"), "month"),                          # 2020-01
    (re.compile(r"(\d{4})[-/](\d{2})[-/](\d{2})"), "day"),             # 2020-01-31, 2020/01/31
    (
        re.compile(
            r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(\.\d+)?)?"
            r"(Z|[+-]\d{2}:?\d{2})?",
            re.IGNORECASE,
        ),
        "datetime",                                                     # ISO timestamps
    ),
)


def _parse_date_label(label: str) -> Optional[Tuple[datetime, str]]:
    """Parse one provider date label to (naive UTC datetime, granularity)."""
    text = label.strip()
    for pattern, granularity in _DATE_LABEL_SHAPES:
        match = pattern.fullmatch(text)
        if not match:
            continue
        try:
            if granularity == "year":
                return datetime(int(match.group(1)), 1, 1), granularity
            if granularity == "quarter":
                return datetime(int(match.group(1)), _QUARTER_START_MONTH[match.group(2)], 1), granularity
            if granularity == "month":
                return datetime(int(match.group(1)), int(match.group(2)), 1), granularity
            if granularity == "day":
                return datetime(*(int(match.group(i)) for i in (1, 2, 3))), granularity
            year, month, day, hour, minute = (int(match.group(i)) for i in range(1, 6))
            fraction = (match.group(7) or ".")[1:]
            parsed = datetime(
                year, month, day, hour, minute, int(match.group(6) or 0),
                int((fraction + "000000")[:6]),
            )
            offset = match.group(8)
            if offset and offset.upper() != "Z":
                # Stata has no time zones: store the UTC wall time.
                digits = offset[1:].replace(":", "")
                delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
                parsed = parsed - delta if offset[0] == "+" else parsed + delta
            return parsed, granularity
        except ValueError:
            return None
    return None


def _stata_date_column(labels: List[object]):
    """Datetimes for the .dta date column, or None to keep the strings."""
    parsed = []
    granularities = set()
    for label in labels:
        text = "" if label is None else str(label)
        if not text.strip():
            parsed.append(None)
            continue
        result = _parse_date_label(text)
        if result is None:
            return None
        parsed.append(result[0])
        granularities.add(result[1])
    if len(granularities) != 1:
        return None
    return parsed


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
                        # Dates are provider-supplied strings — sanitize like
                        # every other text cell (formula-injection hygiene).
                        "date": self._csv_safe(point.date),
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
            column_names = self._unique_column_names(column_names)

            writer = csv.DictWriter(
                buffer,
                fieldnames=["date", *column_names],
                quoting=csv.QUOTE_ALL,
            )
            writer.writeheader()

            for date in all_dates:
                row = {"date": self._csv_safe(date)}
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
            # Multiple series - wide format with date as index. Column names
            # and a date -> value map are built once per series, so this is
            # O(dates x series) rather than a scan of every series per date.
            all_dates = sorted({dp.date for series in data for dp in series.data})
            column_names = []
            series_maps = []
            for series in data:
                parts = [
                    self._slug(series.metadata.seriesId),
                    self._slug(series.metadata.country),
                    self._slug(series.metadata.indicator),
                ]
                column_names.append("_".join(filter(None, parts)) or "series")
                series_maps.append({dp.date: dp.value for dp in series.data})
            # Stata variable names have max 32 chars
            column_names = self._unique_column_names(column_names, max_length=32)

            rows = []
            for date in all_dates:
                row = {"date": self._csv_safe(date)}
                for name, mapping in zip(column_names, series_maps):
                    row[name] = mapping.get(date)
                rows.append(row)

            df = pd.DataFrame(rows)

        # Convert the date column to datetimes for Stata only when every label
        # parses and all share one granularity (see _stata_date_column);
        # otherwise keep the provider's strings rather than writing missing
        # values. The old pd.to_datetime(errors="coerce") turned "2020M01" and
        # the "2020-06" in a mixed annual/monthly export into NaT.
        if "date" in df.columns:
            converted = _stata_date_column(df["date"].tolist())
            if converted is not None:
                df["date"] = pd.to_datetime(pd.Series(converted, dtype="object"))

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
    def _unique_column_names(bases: List[str], max_length: Optional[int] = None) -> List[str]:
        """Return one distinct column name per series, in order.

        Each name is its base (truncated to ``max_length``). Only a name that
        collides with an earlier one, or with the ``date`` column, gets a
        ``_2``, ``_3``, ... suffix, trimming the base so the result still fits
        ``max_length``. Without this, two series with the same slug (or the
        same first 32 characters in Stata) shared one dict key and the
        earlier series was silently dropped.
        """
        used = {"date"}
        names: List[str] = []
        for base in bases:
            name = base[:max_length] if max_length else base
            counter = 2
            while name in used:
                suffix = f"_{counter}"
                stem = base[: max_length - len(suffix)] if max_length else base
                name = f"{stem}{suffix}"
                counter += 1
            used.add(name)
            names.append(name)
        return names

    @staticmethod
    def _slug(value: Optional[str], limit: Optional[int] = None) -> str:
        if not value:
            return ""
        slug = "".join(ch if ch.isalnum() else "_" for ch in value).strip("_")
        if limit:
            return slug[:limit]
        return slug


_FILENAME_STEM_LIMIT = 150
_ASCII_UNSAFE_RUN = re.compile(r"[^A-Za-z0-9._-]+")
_EXTENSION = re.compile(r"[A-Za-z0-9]{1,10}")
_STEM_EDGE = re.compile(r"^[\s.]+|[\s.]+$")


def _is_header_unsafe_char(ch: str) -> bool:
    """Code points that must never reach an HTTP header value.

    Unicode category ``C*`` covers controls (CR/LF/TAB/NUL), format chars,
    surrogates and unassigned code points; ``Zl``/``Zp`` are U+2028/U+2029,
    which ``\\s`` matches and which Starlette's latin-1 header encoder rejects.
    """
    category = unicodedata.category(ch)
    return category.startswith("C") or category in ("Zl", "Zp")


def content_disposition(filename: Optional[str], file_format: str) -> str:
    """Build an ``attachment`` Content-Disposition value that is always latin-1 safe.

    Filenames come from provider metadata (CJK indicator names,
    ``name_en / name_zh`` labels, stray line separators) or from an
    unauthenticated request field, while Starlette encodes response headers
    as latin-1. The value therefore carries both RFC 6266 forms:

    * ``filename="..."`` -- a pure-ASCII fallback (NFKD-folded, anything
      outside ``[A-Za-z0-9._-]`` collapsed to ``_``) for legacy clients;
    * ``filename*=UTF-8''...`` -- the full Unicode name, percent-encoded per
      RFC 5987, which modern browsers prefer.

    Path components are stripped (no traversal) and header-unsafe code points
    are dropped before either form is built. Purely structural: it decides
    nothing about what the name means.
    """
    default_ext = _ASCII_UNSAFE_RUN.sub("", str(file_format or "")) or "bin"
    base = re.split(r"[\\/]", str(filename or ""))[-1]
    clean = "".join(ch for ch in base if not _is_header_unsafe_char(ch)).strip()

    stem, dot, ext = clean.rpartition(".")
    if not (dot and _EXTENSION.fullmatch(ext)):
        stem, ext = clean, default_ext
    # Leading/trailing dots and whitespace never belong to a stem ("name." would
    # otherwise become "name..csv"); re-trim after truncation.
    stem = _STEM_EDGE.sub("", _STEM_EDGE.sub("", stem)[:_FILENAME_STEM_LIMIT])

    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    ascii_stem = _ASCII_UNSAFE_RUN.sub("_", ascii_stem).strip("._-")
    if not re.search(r"[A-Za-z0-9]", ascii_stem):
        ascii_stem = "export"
    unicode_name = f"{stem or 'export'}.{ext}"

    return (
        f'attachment; filename="{ascii_stem}.{ext}"; '
        f"filename*=UTF-8''{quote(unicode_name, safe='')}"
    )


export_service = ExportService()
