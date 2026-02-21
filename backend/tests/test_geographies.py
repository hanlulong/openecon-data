from backend.utils.geographies import (
    CANADIAN_TERRITORIES,
    normalize_canadian_region_list,
)


def test_normalize_adds_missing_territories_when_all_provinces_present():
    provinces_only = [
        "Ontario",
        "Quebec",
        "British Columbia",
        "Alberta",
        "Manitoba",
        "Saskatchewan",
        "Nova Scotia",
        "New Brunswick",
        "Newfoundland and Labrador",
        "Prince Edward Island",
    ]

    normalized = normalize_canadian_region_list(provinces_only, fill_missing_territories=True)
    for territory in CANADIAN_TERRITORIES:
        assert territory in normalized
    assert len(normalized) == 13


def test_normalize_accepts_abbreviations_and_returns_unique_list():
    inputs = ["ON", "Ontario", "nwt", "  pei  ", "PEI"]
    normalized = normalize_canadian_region_list(inputs)
    assert normalized == ["Ontario", "Northwest Territories", "Prince Edward Island"]
