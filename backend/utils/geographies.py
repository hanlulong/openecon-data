from __future__ import annotations

import re
from typing import Any, Iterable, List, NamedTuple, Optional

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


# ---------------------------------------------------------------------------
# Aggregate-geography code grammar (EA / EU families)
# ---------------------------------------------------------------------------
# Statistical publishers (Eurostat, ECB) name an aggregate by a FAMILY prefix
# (EA = euro area, EU = European Union), an optional composition member count
# and an optional composition-vintage year: bare "EA"/"EU" is the changing
# composition, "EA21" / "EU27_2020" / "EU28" are fixed compositions. Which
# compositions a dataset publishes drifts with membership (Bulgaria joined the
# euro area on 2026-01-01; une_rt_m then dropped EA20 and publishes only EA21),
# so every layer that must recognise "this is the euro area / the EU" reads the
# code grammar instead of a fixed code set. Structural code parsing only: it
# never decides which composition a request means.
_AGGREGATE_GEO_CODE_RE = re.compile(r"(EA|EU)(\d{1,2})?(?:_(\d{4}))?")

# Family prefix -> the logical aggregate entity key used by provider_strategy
# (PROVIDER_GROUP_AGGREGATES), plus a display name. Uniform reference table.
AGGREGATE_GEO_FAMILY_REGIONS = {"EA": "EUROZONE", "EU": "EU"}
AGGREGATE_GEO_FAMILY_NAMES = {"EA": "euro area", "EU": "European Union"}


class AggregateGeoCode(NamedTuple):
    family: str
    members: int
    vintage: int

    @property
    def is_changing_composition(self) -> bool:
        """Bare family code ("EA", "EU"): membership changes over time."""
        return self.members == 0 and self.vintage == 0


def aggregate_geo_family(code: Any) -> Optional[AggregateGeoCode]:
    """Parse an aggregate geo code into (family, members, vintage).

    Returns None for anything that is not an EA*/EU* aggregate code, so
    individual countries ("DE", "EE", "EL") are never treated as aggregates.
    """
    match = _AGGREGATE_GEO_CODE_RE.fullmatch(str(code or "").strip().upper())
    if not match:
        return None
    return AggregateGeoCode(match.group(1), int(match.group(2) or 0), int(match.group(3) or 0))
