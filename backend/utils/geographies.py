from __future__ import annotations

from typing import Iterable, List, Optional

# Canonical region lists
CANADIAN_PROVINCES: List[str] = [
    "Newfoundland and Labrador",
    "Prince Edward Island",
    "Nova Scotia",
    "New Brunswick",
    "Quebec",
    "Ontario",
    "Manitoba",
    "Saskatchewan",
    "Alberta",
    "British Columbia",
]

CANADIAN_TERRITORIES: List[str] = ["Yukon", "Northwest Territories", "Nunavut"]

ALL_CANADIAN_REGIONS: List[str] = CANADIAN_PROVINCES + CANADIAN_TERRITORIES

# Alternate spellings/abbreviations
_PROVINCE_NORMALIZATION_MAP = {
    "NEWFOUNDLAND AND LABRADOR": "Newfoundland and Labrador",
    "NEWFOUNDLAND": "Newfoundland and Labrador",
    "NL": "Newfoundland and Labrador",
    "PRINCE EDWARD ISLAND": "Prince Edward Island",
    "PEI": "Prince Edward Island",
    "PE": "Prince Edward Island",
    "NOVA SCOTIA": "Nova Scotia",
    "NS": "Nova Scotia",
    "NEW BRUNSWICK": "New Brunswick",
    "NB": "New Brunswick",
    "QUEBEC": "Quebec",
    "QC": "Quebec",
    "ONTARIO": "Ontario",
    "ON": "Ontario",
    "MANITOBA": "Manitoba",
    "MB": "Manitoba",
    "SASKATCHEWAN": "Saskatchewan",
    "SK": "Saskatchewan",
    "ALBERTA": "Alberta",
    "AB": "Alberta",
    "BRITISH COLUMBIA": "British Columbia",
    "BRITISHCOLUMBIA": "British Columbia",
    "BC": "British Columbia",
    "YUKON": "Yukon",
    "YT": "Yukon",
    "YUKON TERRITORY": "Yukon",
    "NORTHWEST TERRITORIES": "Northwest Territories",
    "NORTH WEST TERRITORIES": "Northwest Territories",
    "NWT": "Northwest Territories",
    "NUNAVUT": "Nunavut",
    "NU": "Nunavut",
}


def canonicalize_canadian_region(name: Optional[str]) -> Optional[str]:
    """Map abbreviations/synonyms to canonical province/territory names."""
    if not name:
        return None
    cleaned = name.strip()
    if not cleaned:
        return None
    key = cleaned.upper().replace(".", "")
    return _PROVINCE_NORMALIZATION_MAP.get(key, cleaned)


def normalize_canadian_region_list(
    regions: Optional[Iterable[str]],
    fill_missing_territories: bool = False
) -> List[str]:
    """
    Normalize a list of provinces/territories, optionally auto-adding territories
    when the user clearly requested all provinces.
    """
    if not regions:
        # Caller provided no explicit list → assume all regions
        canonical = ALL_CANADIAN_REGIONS.copy()
    else:
        seen = set()
        canonical = []
        for region in regions:
            normalized = canonicalize_canadian_region(region)
            if not normalized or normalized in seen:
                continue
            if normalized not in ALL_CANADIAN_REGIONS:
                continue
            canonical.append(normalized)
            seen.add(normalized)

        if not canonical:
            canonical = ALL_CANADIAN_REGIONS.copy()
            seen = set(canonical)

    if fill_missing_territories:
        region_set = set(canonical)
        if set(CANADIAN_PROVINCES).issubset(region_set):
            for territory in CANADIAN_TERRITORIES:
                if territory not in region_set:
                    canonical.append(territory)

    return canonical
