"""Guard tests for FIX 3 — Eurostat JSON-stat dimension member selection.

The primary parser previously defaulted every non-unit/non-time dimension to
index 0 ("Other dimensions default to 0"), silently slicing an arbitrary
member (e.g. a seasonally-adjusted variant or a specific age band) under the
dataset's headline name. The parser must now:

  (a) prefer the TOTAL/aggregate member of an unconstrained dimension and
      disclose the actual member chosen in the returned notes;
  (b) honor an explicit caller filter that the API left multi-valued when the
      member exists, and FAIL CLOSED when it does not;
  (c) leave single-member (size-1) dimensions and single-dimension datasets
      untouched.
"""
from __future__ import annotations

import pytest

from backend.providers.eurostat import EurostatProvider
from backend.utils.retry import DataNotAvailableError


@pytest.fixture
def provider() -> EurostatProvider:
    return EurostatProvider()


def _payload(s_adj_members, values):
    """dims: s_adj(N) x geo(1) x time(2), no unit dim.

    Row-major flat index = ((s_adj_idx * 1) + geo_idx) * 2 + time_idx.
    """
    return {
        "label": "Test indicator",
        "id": ["s_adj", "geo", "time"],
        "size": [len(s_adj_members), 1, 2],
        "dimension": {
            "s_adj": {
                "category": {
                    "index": {code: i for i, code in enumerate(s_adj_members)},
                    "label": {code: f"label-{code}" for code in s_adj_members},
                }
            },
            "geo": {"category": {"index": {"IT": 0}, "label": {"IT": "Italy"}}},
            "time": {
                "category": {
                    "index": {"2021": 0, "2022": 1},
                    "label": {"2021": "2021", "2022": "2022"},
                }
            },
        },
        "value": values,
    }


# members [SCA, NSA, TOTAL] at idx 0,1,2 -> flat positions:
#   SCA:   0,1     NSA:   2,3     TOTAL: 4,5
_MEMBERS = ["SCA", "NSA", "TOTAL"]
_VALUES = {"0": 1.0, "1": 2.0, "2": 3.0, "3": 4.0, "4": 100.0, "5": 200.0}


def test_unconstrained_dimension_prefers_total_and_notes(provider):
    notes: list = []
    points = provider._parse_json_stat(
        _payload(_MEMBERS, _VALUES), "ds", notes_sink=notes
    )
    assert [p["value"] for p in points] == [100.0, 200.0]  # TOTAL member
    assert any("TOTAL" in note for note in notes)
    assert any("s_adj" in note for note in notes)


def test_explicit_filter_honored_when_api_ignores_it(provider):
    """API returns s_adj multi-valued despite the NSA filter -> pick NSA."""
    notes: list = []
    points = provider._parse_json_stat(
        _payload(_MEMBERS, _VALUES),
        "ds",
        requested_members={"s_adj": "NSA"},
        strict_member_dims={"s_adj"},
        notes_sink=notes,
    )
    assert [p["value"] for p in points] == [3.0, 4.0]  # NSA member
    assert any("NSA" in note for note in notes)


def test_explicit_filter_member_absent_fails_closed(provider):
    with pytest.raises(DataNotAvailableError) as exc:
        provider._parse_json_stat(
            _payload(_MEMBERS, _VALUES),
            "ds",
            requested_members={"s_adj": "ZZZ"},
            strict_member_dims={"s_adj"},
            notes_sink=[],
        )
    assert "eurostat_filter_member_unavailable" in str(exc.value)
    assert "s_adj" in str(exc.value)


def test_nonstrict_default_member_absent_falls_back_to_total(provider):
    """A dataset mechanical default the API can't honor must NOT fail closed;
    it falls back to the aggregate member and discloses the substitution."""
    notes: list = []
    points = provider._parse_json_stat(
        _payload(_MEMBERS, _VALUES),
        "ds",
        requested_members={"s_adj": "ZZZ"},  # not in strict set
        strict_member_dims=set(),
        notes_sink=notes,
    )
    assert [p["value"] for p in points] == [100.0, 200.0]  # TOTAL fallback
    assert any("unavailable" in note for note in notes)


def test_single_member_dimension_not_disclosed(provider):
    """A dimension the API collapsed to one member needs no note or choice."""
    notes: list = []
    payload = _payload(["SCA"], {"0": 11.0, "1": 22.0})
    points = provider._parse_json_stat(
        payload,
        "ds",
        requested_members={"s_adj": "SCA"},
        strict_member_dims={"s_adj"},
        notes_sink=notes,
    )
    assert [p["value"] for p in points] == [11.0, 22.0]
    assert notes == []


def test_no_notes_sink_is_safe(provider):
    """Selection must work without a notes sink (callers may omit it)."""
    points = provider._parse_json_stat(_payload(_MEMBERS, _VALUES), "ds")
    assert [p["value"] for p in points] == [100.0, 200.0]
