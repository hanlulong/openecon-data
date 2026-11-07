"""Eurostat TOC freshness backfill: structural code matching, safe apply."""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backfill_eurostat_toc_freshness.py"
_spec = importlib.util.spec_from_file_location("backfill_eurostat_toc_freshness", _SCRIPT)
backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(backfill)

TOC = (
    '"title"\t"code"\t"type"\t"last update of data"\t"last table structure change"\t"data start"\t"data end"\t"values"\n'
    '"Database by themes"\t"data"\t"folder"\t" "\t" "\t" "\t" "\t\n'
    '"HICP - monthly data (annual rate of change) (1997-2025)"\t"prc_hicp_manr"\t"dataset"\t"06.02.2026"\t"07.01.2026"\t"1997-01"\t"2025-12"\t3511040\n'
    '"HICP - ECOICOP ver.2"\t"prc_hicp_minr"\t"dataset"\t"04.11.2026"\t"04.11.2026"\t"1996-01"\t"2026-10"\t22481679\n'
    '"Quarterly government debt"\t"gov_10q_ggdebt"\t"dataset"\t"21.10.2026"\t"21.10.2026"\t"1994-Q1"\t"2026-Q2"\t610308\n'
    '"GDP annual"\t"nama_10_gdp"\t"dataset"\t"05.11.2026"\t"20.10.2026"\t"1975"\t"2025"\t1087944\n'
    '"GDP annual (second folder)"\t"nama_10_gdp"\t"dataset"\t"05.11.2026"\t"20.10.2026"\t"1975"\t"2025"\t1087944\n'
)


def test_parse_toc_keeps_native_periods_and_iso_update_dates():
    toc = backfill.parse_toc(TOC)
    assert toc["PRC_HICP_MANR"] == ("2025-12", "2026-02-06")
    assert toc["GOV_10Q_GGDEBT"] == ("2026-Q2", "2026-10-21")
    assert toc["NAMA_10_GDP"] == ("2025", "2026-11-05")
    assert "DATA" not in toc  # folders carry no freshness


def test_plan_matches_exact_and_derived_view_codes_only():
    toc = backfill.parse_toc(TOC)
    rows = [
        (1, "PRC_HICP_MANR", None, None),
        (2, "PRC_HICP_MANR$DV_3123", None, None),
        (3, "NAMA_10_GDP", "2025", "2026-11-05"),  # already current
        (4, "AEI_EF_IR", None, None),  # not in the TOC any more
    ]
    updates, summary = backfill.plan_updates(rows, toc)
    assert updates == [("2025-12", "2026-02-06", 1), ("2025-12", "2026-02-06", 2)]
    assert summary == {
        "rows": 4, "matched_exact": 2, "matched_derived_view": 1,
        "unmatched": 1, "unchanged": 1, "to_update": 2,
    }


def _db(path: Path) -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE indicators (id INTEGER PRIMARY KEY, provider TEXT, code TEXT, "
        "name TEXT, end_date TEXT, last_updated TEXT)"
    )
    conn.executemany(
        "INSERT INTO indicators (provider, code, name, end_date, last_updated) VALUES (?, ?, ?, ?, ?)",
        [
            ("Eurostat", "PRC_HICP_MANR", "HICP", None, None),
            ("Eurostat", "GOV_10Q_GGDEBT", "Debt", None, None),
            ("FRED", "PRC_HICP_MANR", "same code, other provider", None, None),
        ],
    )
    conn.commit()
    conn.close()
    return path


def _rows(path: Path):
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT provider, code, end_date, last_updated FROM indicators ORDER BY id").fetchall()
    finally:
        conn.close()


def test_dry_run_is_default_and_writes_nothing(tmp_path):
    db = _db(tmp_path / "ind.db")
    toc = tmp_path / "toc.txt"
    toc.write_text(TOC, encoding="utf-8")
    before = _rows(db)
    assert backfill.main(["--db", str(db), "--toc-file", str(toc)]) == 0
    assert _rows(db) == before


def test_apply_refuses_without_backup(tmp_path):
    db = _db(tmp_path / "ind.db")
    toc = tmp_path / "toc.txt"
    toc.write_text(TOC, encoding="utf-8")
    assert backfill.main(["--apply", "--db", str(db), "--toc-file", str(toc)]) == 2
    assert all(row[2] is None for row in _rows(db))


def test_apply_backs_up_then_stamps_only_eurostat_rows(tmp_path):
    db = _db(tmp_path / "ind.db")
    toc = tmp_path / "toc.txt"
    toc.write_text(TOC, encoding="utf-8")
    backup = tmp_path / "bak" / "ind.db.bak"

    assert backfill.main(["--apply", "--backup", str(backup), "--db", str(db), "--toc-file", str(toc)]) == 0

    assert _rows(db) == [
        ("Eurostat", "PRC_HICP_MANR", "2025-12", "2026-02-06"),
        ("Eurostat", "GOV_10Q_GGDEBT", "2026-Q2", "2026-10-21"),
        ("FRED", "PRC_HICP_MANR", None, None),
    ]
    assert all(row[2] is None for row in _rows(backup))  # pre-change copy


@pytest.mark.parametrize("value", ["2025-13x", "n/a", " "])
def test_unparseable_periods_are_ignored(value):
    assert backfill._period(value) is None
