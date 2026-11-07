"""Live battery grading: outcome classification + intent-level geography."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "live_battery.py"
_spec = importlib.util.spec_from_file_location("live_battery", _SCRIPT)
battery = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(battery)


def _series(country, indicator="Inflation", value=2.1):
    return {"metadata": {"source": "Eurostat", "country": country, "indicator": indicator},
            "data": [{"date": "2026-10-01", "value": value}]}


@pytest.mark.parametrize("resp,status", [
    ({"data": [_series("EA")]}, "data"),
    ({"data": [], "clarificationNeeded": True, "clarificationQuestions": ["Which measure?"]}, "clarification"),
    ({"data": [], "error": "boom"}, "error"),
    ({"data": [], "message": "Not published for this period"}, "messaged_no_data"),
    ({"data": []}, "empty"),
    (None, "empty"),
])
def test_response_status_mirrors_backend_outcome_enum(resp, status):
    assert battery.response_status(resp) == status


def test_clarification_is_reported_as_clarification_not_silent_empty():
    resp = {"data": [], "clarificationNeeded": True, "clarificationQuestions": ["Headline or core HICP?"]}
    problems = battery.check(resp, {"nonempty": True})
    assert problems == ["unexpected clarification: 'Headline or core HICP?'"]
    assert battery.check(resp, {"nonempty": True, "clarification_ok": True}) == []


def test_true_silent_empty_is_still_flagged():
    assert battery.check({"data": []}, {"nonempty": True}) == ["empty data (empty)"]


@pytest.mark.parametrize("country", ["EA", "EA20", "EA21", "EMU", "EURO", "U2", "XM", "EZ19", "Euro area"])
def test_any_euro_area_entity_satisfies_euro_area_intent(country):
    exp = {"nonempty": True, "geo_any_of": battery.EURO_AREA_GEO}
    assert battery.check({"data": [_series(country)]}, exp) == []


@pytest.mark.parametrize("country", ["EU27_2020", "EU", "DE", "Estonia", "European Union"])
def test_wrong_aggregate_or_country_fails_euro_area_intent(country):
    exp = {"nonempty": True, "geo_any_of": battery.EURO_AREA_GEO}
    problems = battery.check({"data": [_series(country)]}, exp)
    assert len(problems) == 1 and problems[0].startswith("wrong geography")


def test_country_less_series_falls_back_to_label_evidence():
    exp = {"geo_any_of": battery.EURO_AREA_GEO}
    assert battery.check({"data": [_series(None, "HICP - Euro area (changing composition)")]}, exp) == []
    assert battery.check({"data": [_series(None, "HICP - European Union")]}, exp) != []


def test_euro_area_battery_cases_carry_geography_intent():
    names = {name: exp for name, _q, exp in battery.SINGLE}
    for name in ("eu-ea-hicp", "zh-ea-hicp", "zh-ea-unemp"):
        assert names[name].get("geo_any_of") == battery.EURO_AREA_GEO
    mr9 = dict(battery.MULTI)["mr9-eurostat-switch"]
    assert mr9[0][1].get("geo_any_of") == battery.EURO_AREA_GEO
