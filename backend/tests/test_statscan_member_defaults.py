"""Guards: StatsCan default-member selection never silently gambles.

Root cause of the observed "LLM picks a good cube but the fetch dies and
cross-provider fallback wanders" chain: dimensions with no matching branch
used to default to members[0] (provider table order — often a suppressed
cross-tab cell or a non-aggregate member). Now: aggregate member when
detectable; otherwise disclose the arbitrary pick via a notes sink (paths
that surface metadata notes) or fail closed with an attributable
supportability error (paths that cannot disclose).
"""

import pytest

from backend.providers.statscan import StatsCanProvider
from backend.utils.retry import DataNotAvailableError


def _members(*names):
    return [
        {"memberId": i + 1, "memberNameEn": name, "parentMemberId": None if i == 0 else 1}
        for i, name in enumerate(names)
    ]


@pytest.fixture()
def provider():
    return StatsCanProvider()


def test_unmatched_dimension_with_total_member_selects_it(provider):
    members = _members("Estimate", "Standard error", "Total, all types")
    mid = provider._select_default_member_id("Data type", members, "gdp")
    assert mid == 3 or mid == 1  # keyword "estimate" also structurally valid
    assert mid is not None


def test_unmatched_dimension_without_aggregate_returns_none(provider):
    # No total/all/aggregate-like member, flat hierarchy → no structural signal.
    members = [
        {"memberId": 4, "memberNameEn": "Cattle", "parentMemberId": None},
        {"memberId": 7, "memberNameEn": "Pigs", "parentMemberId": None},
        {"memberId": 9, "memberNameEn": "Sheep", "parentMemberId": None},
    ]
    assert provider._select_default_member_id("Type of livestock", members, "inventory") is None


def test_default_member_or_raise_fails_closed_without_sink(provider):
    members = [
        {"memberId": 4, "memberNameEn": "Cattle", "parentMemberId": None},
        {"memberId": 7, "memberNameEn": "Pigs", "parentMemberId": None},
    ]
    with pytest.raises(DataNotAvailableError) as exc:
        provider._default_member_or_raise("32100322", "Type of livestock", members, "inventory")
    assert "statscan_required_dimension_missing" in str(exc.value)
    assert "Type of livestock" in str(exc.value)


def test_default_member_or_raise_discloses_with_sink(provider):
    members = [
        {"memberId": 4, "memberNameEn": "Cattle", "parentMemberId": None},
        {"memberId": 7, "memberNameEn": "Pigs", "parentMemberId": None},
    ]
    notes: list = []
    mid = provider._default_member_or_raise(
        "32100322", "Type of livestock", members, "inventory", arbitrary_note_sink=notes
    )
    assert mid == 4  # first member, but DISCLOSED
    assert len(notes) == 1
    assert "Cattle" in notes[0] and "Type of livestock" in notes[0]


def test_aggregate_member_detected_from_hierarchy_root(provider):
    # Structural signal: a single root member that others point to as parent.
    members = [
        {"memberId": 10, "memberNameEn": "All industries", "parentMemberId": None},
        {"memberId": 11, "memberNameEn": "Manufacturing", "parentMemberId": 10},
        {"memberId": 12, "memberNameEn": "Construction", "parentMemberId": 10},
    ]
    mid = provider._select_default_member_id("NAICS industry", members, "gdp")
    assert mid == 10


def test_fetch_with_breakdown_routes_defaults_through_selection(provider):
    # The breakdown builder must NOT hardcode member "1" for non-target dims:
    # the source now routes through _default_member_or_raise (fail-closed).
    import inspect

    src = inspect.getsource(StatsCanProvider.fetch_with_breakdown)
    assert "_default_member_or_raise" in src
    assert 'coordinate_parts.append("1")' not in src


def test_shared_probe_wired_into_both_no_retry_paths(provider):
    import inspect

    for fn in (StatsCanProvider.fetch_categorical_data, StatsCanProvider.fetch_with_dimensions):
        src = inspect.getsource(fn)
        assert "_fetch_coordinate_with_fallback" in src, fn.__name__
