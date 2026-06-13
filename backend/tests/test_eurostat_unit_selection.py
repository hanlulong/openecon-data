"""Multi-unit JSON-stat parsing: the unit choice must follow the dataset's
own headline label, not array order.

Regression for the TIPSLM80 class of bug: Eurostat publishes several units in
one cube (here a 3-year percentage-point CHANGE at unit index 0 and the actual
rate at index 1). Taking index 0 returned negative "youth unemployment rate"
values — wrong data under plausible metadata, the worst failure class.
"""
from __future__ import annotations

import pytest

from backend.providers.eurostat import EurostatProvider


def _tipslm80_payload() -> dict:
    """Minimal JSON-stat 2.0 cube mirroring tipslm80?geo=IT: dims
    freq(1) x age(1) x sex(1) x unit(2) x geo(1) x time(3); unit 0 is the
    change series (negative values), unit 1 the level series."""
    return {
        "label": "Youth unemployment rate - % of active population aged 15-24",
        "id": ["freq", "age", "sex", "unit", "geo", "time"],
        "size": [1, 1, 1, 2, 1, 3],
        "dimension": {
            "freq": {"category": {"index": {"A": 0}, "label": {"A": "Annual"}}},
            "age": {"category": {"index": {"Y15-24": 0}, "label": {"Y15-24": "From 15 to 24 years"}}},
            "sex": {"category": {"index": {"T": 0}, "label": {"T": "Total"}}},
            "unit": {
                "category": {
                    "index": {"PPCH_3Y": 0, "PC_ACT": 1},
                    "label": {
                        "PPCH_3Y": "Percentage point change (t-(t-3))",
                        "PC_ACT": "Percentage of population in the labour force",
                    },
                }
            },
            "geo": {"category": {"index": {"IT": 0}, "label": {"IT": "Italy"}}},
            "time": {
                "category": {
                    "index": {"2021": 0, "2022": 1, "2023": 2},
                    "label": {"2021": "2021", "2022": "2022", "2023": "2023"},
                }
            },
        },
        # Flattened values: positions 0-2 = PPCH_3Y (change, negative),
        # positions 3-5 = PC_ACT (the real rate).
        "value": {"0": -9.4, "1": -5.5, "2": -3.1, "3": 29.7, "4": 23.7, "5": 22.7},
    }


@pytest.fixture
def provider() -> EurostatProvider:
    return EurostatProvider()


def test_multi_unit_dataset_selects_headline_unit_by_label_affinity(provider):
    payload = _tipslm80_payload()
    unit_index, unit_label = provider._select_unit_choice(payload, "tipslm80")
    assert unit_index == 1
    assert unit_label == "Percentage of population in the labour force"


def test_parse_json_stat_returns_level_series_not_change_series(provider):
    points = provider._parse_json_stat(_tipslm80_payload(), "tipslm80")
    values = [p["value"] for p in points]
    assert values == [29.7, 23.7, 22.7], (
        "parser must return the rate series, not the percentage-point change"
    )
    assert all(v > 0 for v in values)


def test_extract_unit_label_matches_parsed_series(provider):
    unit = provider._extract_unit_from_payload(_tipslm80_payload(), "tipslm80")
    assert unit == "Percentage of population in the labour force"


def test_single_unit_dataset_unchanged(provider):
    payload = _tipslm80_payload()
    payload["dimension"]["unit"]["category"] = {
        "index": {"PC_ACT": 0},
        "label": {"PC_ACT": "Percentage of population in the labour force"},
    }
    payload["size"][3] = 1
    payload["value"] = {"0": 29.7, "1": 23.7, "2": 22.7}
    unit_index, unit_label = provider._select_unit_choice(payload, "tipslm80")
    assert unit_index == 0
    assert unit_label == "Percentage of population in the labour force"


def test_une_rt_a_special_case_preserved(provider):
    payload = _tipslm80_payload()
    payload["label"] = "Unemployment by sex and age - annual data"
    payload["dimension"]["unit"]["category"] = {
        "index": {"THS_PER": 0, "PC_ACT": 1},
        "label": {
            "THS_PER": "Thousand persons",
            "PC_ACT": "Percentage of population in the labour force",
        },
    }
    unit_index, unit_label = provider._select_unit_choice(payload, "une_rt_a")
    assert unit_index == 1
    assert unit_label == "Percentage of population in the labour force"


def test_no_affinity_signal_keeps_first_unit(provider):
    payload = _tipslm80_payload()
    payload["label"] = "Some dataset"
    payload["dimension"]["unit"]["category"] = {
        "index": {"U1": 0, "U2": 1},
        "label": {"U1": "Alpha beta", "U2": "Gamma delta"},
    }
    unit_index, _ = provider._select_unit_choice(payload, "whatever")
    assert unit_index == 0
