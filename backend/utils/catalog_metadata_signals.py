"""Structural catalog-metadata signals decoded from official provider codesets.

The indicator catalog (``indicators.db``) stores some provider metadata in the
provider's RAW official encoding.  Selection, exact-title resolution and
clarification menus then compare that raw value against what the user asked
for and silently get it wrong.  This module is the one place those raw values
are decoded, from committed copies of the provider's own published codesets
(``backend/data/metadata/reference/``; regenerate offline with
``scripts/refresh_catalog_reference_codesets.py`` -- nothing here touches the
network).

Two signals, both STRUCTURAL (official codelists / published dimension lists;
no concept, keyword, country or indicator-code knowledge):

* Frequency.  Statistics Canada rows carry the numeric WDS ``frequencyCode``
  ("6", "12", ...).  :func:`canonical_catalog_frequency` decodes it through the
  official ``getCodeSets`` frequency codeset into a closed canonical token
  vocabulary.  Non-core official frequencies get single tokens that can never
  alias a core one: "Semi-annual" -> ``semiannual`` (never "semi annual", which
  would tokenize to "annual"), "Every N years" -> ``multiyear``, "Occasional*"
  -> ``irregular``.  Values that are not a known provider code pass through
  byte-identical, so FRED/ChinaMacro text frequencies are unchanged.

* Vintage-archive source.  World Bank rows carry ``raw_metadata.source``.  A
  source whose PUBLISHED dimension list includes a ``Version`` concept is a
  vintage archive: every series is split by release vintage and the
  current-data endpoint answers "indicator not found / archived".  Keyed on the
  dimension list, not on source names or ids, so servable historical sources
  (e.g. ones merely *named* "...Archive" or "Doing Business") are untouched.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, FrozenSet, Mapping, MutableMapping, Optional

from .providers import normalize_provider_name

logger = logging.getLogger(__name__)

REFERENCE_DIR = Path(__file__).resolve().parent.parent / "data" / "metadata" / "reference"
STATSCAN_FREQUENCY_CODESET_FILE = REFERENCE_DIR / "statscan_frequency_codeset.json"
WORLDBANK_SOURCE_CONCEPTS_FILE = REFERENCE_DIR / "worldbank_source_concepts.json"

# Closed canonical frequency vocabulary.  The core tokens are the ones the
# runtime (selector/resolution frequency matchers, provider metadata) already
# speaks; the non-core tokens are deliberately single words that no frequency
# alias table can match against a core request.
CORE_FREQUENCY_TOKENS: FrozenSet[str] = frozenset(
    {"daily", "weekly", "biweekly", "monthly", "bimonthly", "quarterly", "semiannual", "annual"}
)
MULTIYEAR_FREQUENCY = "multiyear"
IRREGULAR_FREQUENCY = "irregular"

# The dimension a vintage-archive source publishes (World Bank API concept id).
_VINTAGE_DIMENSION = "version"


def canonical_frequency_from_official_label(label: str) -> str:
    """Map an official English frequency label onto the canonical vocabulary.

    Purely lexical normalization of the provider's own label text
    ("Semi-annual" -> "semiannual", "Every 5 years" -> "multiyear",
    "Occasional Monthly" -> "irregular").  An unrecognised label is returned
    compacted (letters/digits only), which matches no core alias.
    """
    compact = re.sub(r"[^a-z0-9]+", "", str(label or "").lower())
    if not compact:
        return ""
    if compact.startswith("occasional"):
        return IRREGULAR_FREQUENCY
    if compact.startswith("every") and "year" in compact:
        return MULTIYEAR_FREQUENCY
    return compact


@lru_cache(maxsize=1)
def statscan_frequency_codeset() -> Dict[int, str]:
    """Official StatsCan ``frequencyCode -> frequencyDescEn`` (committed copy)."""
    try:
        payload = json.loads(STATSCAN_FREQUENCY_CODESET_FILE.read_text(encoding="utf-8"))
        return {
            int(row["frequencyCode"]): str(row.get("frequencyDescEn") or "")
            for row in payload.get("frequency") or []
            if row.get("frequencyCode") is not None
        }
    except Exception as exc:  # fail open: raw codes pass through unchanged
        logger.warning("StatsCan frequency codeset unavailable (%s); codes stay raw", exc)
        return {}


@lru_cache(maxsize=1)
def statscan_canonical_frequency_by_code() -> Dict[int, str]:
    """StatsCan ``frequencyCode -> canonical frequency token``."""
    return {
        code: canonical_frequency_from_official_label(label)
        for code, label in statscan_frequency_codeset().items()
    }


def statscan_frequency_code_for(canonical: str) -> Optional[int]:
    """Inverse lookup: the official StatsCan code for a canonical token."""
    for code, token in sorted(statscan_canonical_frequency_by_code().items()):
        if token == canonical:
            return code
    return None


def _as_int_code(raw: Any) -> Optional[int]:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    text = str(raw or "").strip()
    return int(text) if text.isdigit() else None


def canonical_catalog_frequency(provider: str, raw: Any) -> Any:
    """Decode a catalog ``frequency`` value into the canonical vocabulary.

    Only a raw value that is a known code in the provider's official codeset
    is decoded; anything else (FRED's "Monthly, End of Period", NULL, unknown
    codes) is returned unchanged.
    """
    code = _as_int_code(raw)
    if code is None:
        return raw
    if normalize_provider_name(str(provider or "")) != "STATSCAN":
        return raw
    return statscan_canonical_frequency_by_code().get(code, raw)


def normalize_catalog_row_frequency(row: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Decode ``row['frequency']`` in place; keep the raw code as ``frequency_code``.

    Cheap on the hot path: a non-numeric frequency (every non-StatsCan row)
    returns after one ``isdigit`` check, with no JSON parsing.
    """
    raw = row.get("frequency")
    if raw is None or _as_int_code(raw) is None:
        return row
    decoded = canonical_catalog_frequency(str(row.get("provider") or ""), raw)
    if decoded != raw:
        row.setdefault("frequency_code", raw)
        row["frequency"] = decoded
    return row


# ---------------------------------------------------------------------------
# Vintage-archive sources (World Bank)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def worldbank_vintage_source_ids() -> FrozenSet[str]:
    """WB source ids whose published dimension list includes ``Version``."""
    try:
        payload = json.loads(WORLDBANK_SOURCE_CONCEPTS_FILE.read_text(encoding="utf-8"))
        return frozenset(
            str(source_id).strip()
            for source_id, entry in (payload.get("sources") or {}).items()
            if any(
                str(concept or "").strip().lower() == _VINTAGE_DIMENSION
                for concept in (entry or {}).get("concepts") or []
            )
        )
    except Exception as exc:  # fail open: nothing is treated as vintage
        logger.warning("World Bank source-concept reference unavailable (%s)", exc)
        return frozenset()


def provider_has_vintage_source_reference(provider: str) -> bool:
    """True when the provider publishes source dimension lists we decode."""
    return normalize_provider_name(str(provider or "")) == "WORLDBANK" and bool(
        worldbank_vintage_source_ids()
    )


def catalog_source_id(raw_metadata: Any) -> str:
    """``raw_metadata.source.id`` from a catalog row's raw metadata (str or dict)."""
    raw = raw_metadata
    if isinstance(raw, str):
        if not raw.strip():
            return ""
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return ""
    if not isinstance(raw, Mapping):
        return ""
    source = raw.get("source")
    if isinstance(source, Mapping):
        return str(source.get("id") or "").strip()
    return ""


def catalog_row_is_vintage_archive(provider: str, raw_metadata: Any) -> bool:
    """True when the row's source is a vintage archive the current API cannot serve.

    Fails open (False) for other providers, absent/unparseable metadata, or a
    missing reference table.
    """
    if not provider_has_vintage_source_reference(provider):
        return False
    source_id = catalog_source_id(raw_metadata)
    return bool(source_id) and source_id in worldbank_vintage_source_ids()
