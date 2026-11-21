"""Country/subdivision identities from ISO registry data, not query rules."""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any


REGISTRY_PATH = Path(__file__).resolve().parent.parent / "data" / "geographic_name_collisions.json"


def normalize_geographic_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return re.sub(r"\s+", " ", "".join(char for char in text if not unicodedata.combining(char))).strip()


@lru_cache(maxsize=1)
def geographic_registry() -> dict[str, Any]:
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    if not isinstance(registry.get("countries"), dict) or not isinstance(registry.get("collisions"), dict):
        raise ValueError("Invalid geographic identity registry")
    return registry


def geographic_name_collisions(query: str) -> list[dict[str, Any]]:
    """Find named identity candidates; this never decides the intended place."""
    text = normalize_geographic_name(query)
    matches = []
    for name, entities in geographic_registry()["collisions"].items():
        identities = distinct_geographic_entities(entities)
        match = re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", text)
        if match and len(identities) > 1:
            matches.append((match.start(), {"name": name, "entities": identities}))
    return [item for _, item in sorted(matches, key=lambda item: item[0])]


def has_geographic_collision(query: str) -> bool:
    return bool(geographic_name_collisions(query))


def ambiguous_country_alias(alias: str, iso2: str | None = None) -> bool:
    entities = distinct_geographic_entities(
        geographic_registry()["collisions"].get(normalize_geographic_name(alias), [])
    )
    return len({entity["country"] for entity in entities}) > 1 and any(
        entity["kind"] == "country" and (iso2 is None or entity["country"] == iso2.upper())
        for entity in entities
    )


def country_evidence_text(text: str) -> str:
    """Remove bare aliases that identify places in different parent countries.

    A same-country city or province does not contradict country coverage.
    Explicit country qualification still establishes the country identity;
    otherwise a colliding name supplies no country evidence by itself.
    """
    original = str(text or "")
    for collision in geographic_name_collisions(original):
        name = collision["name"]
        if not ambiguous_country_alias(name):
            continue
        escaped = re.escape(name)
        if re.search(rf"\bcountry\s+(?:of\s+)?{escaped}(?![a-z0-9])", original, re.IGNORECASE) or re.search(
            rf"(?<![a-z0-9]){escaped}\s*\(?\s*country\b", original, re.IGNORECASE
        ):
            continue
        original = re.sub(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", " ", original, flags=re.IGNORECASE)
    return original


def distinct_geographic_entities(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Avoid asking about two ISO classifications of the same territory.

    A cross-parent subdivision whose suffix references the separate country
    identifier links that country's territorial classification to its parent.
    Same-parent subdivisions remain distinct (a country is not its canton).
    Parallel classifications of the linked name within that parent are aliases.
    """
    country = next((entity for entity in entities if entity["kind"] == "country"), None)
    if country is None:
        return entities
    territorial_parents = {
        entity["country"] for entity in entities
        if entity["kind"] == "subdivision"
        and entity["country"] != country["country"]
        and entity.get("code", "").split("-", 1)[-1] == country["country"]
    }
    return [entity for entity in entities
            if entity["kind"] == "country" or entity["country"] not in territorial_parents]


def country_display_name(iso2: str) -> str:
    return str(geographic_registry()["countries"].get(iso2.upper(), iso2))
