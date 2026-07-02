"""WB region substitution must not collapse an explicit country comparison.

The old test keyed on min(len(region), len(set)) * 0.7, a tautology for any
subset of a larger region: 5 African countries ⊂ Sub-Saharan Africa's 22
collapsed into the single SSF aggregate, discarding the comparison. Region
substitution now requires near-total region coverage.
"""

from backend.providers.worldbank import WorldBankProvider


def _ssf_members():
    return list(WorldBankProvider._REGION_COUNTRY_SETS["SSF"])


def test_small_subset_not_collapsed():
    # 5 countries that happen to be in SSF is a comparison, not "the region".
    assert WorldBankProvider._region_aggregate_for_country_set(_ssf_members()[:5]) is None


def test_full_region_collapses():
    assert WorldBankProvider._region_aggregate_for_country_set(_ssf_members()) == "SSF"


def test_near_total_coverage_collapses():
    members = _ssf_members()
    # 20 of 22 (~91%) still counts as the region (a couple members may be
    # dropped for data availability during expansion).
    assert WorldBankProvider._region_aggregate_for_country_set(members[:20]) == "SSF"


def test_non_region_set_not_collapsed():
    assert (
        WorldBankProvider._region_aggregate_for_country_set(
            ["USA", "JPN", "DEU", "BRA", "CHN", "IND"]
        )
        is None
    )


def test_too_few_countries_ignored():
    assert WorldBankProvider._region_aggregate_for_country_set(["US", "JP"]) is None
