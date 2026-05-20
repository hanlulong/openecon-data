"""Provider-native OECD supportability classification for validation sampling.

The helpers here are metadata-only sampler priors.  They keep OECD catalog
dataflow aliases that the current runtime does not execute as ordinary
natural-language prompts in supportability inventory.  They do not route user
queries, map prose to dataflows, or mark any result as passing.
"""

from __future__ import annotations

import json
import re
from typing import Any, Final

OECD_RUNTIME_DATAFLOW_ALIAS_UNSUPPORTED_REASON: Final[str] = (
    "oecd_runtime_dataflow_alias_not_supported"
)

_RUNTIME_SHAPED_DATAFLOW_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:[A-Z0-9_.-]+,)?(?:OECD_)?DSD_[A-Z0-9_]+@[A-Z0-9_]+(?:,[A-Z0-9_.-]+)?$",
    flags=re.IGNORECASE,
)


def _parse_raw_metadata(raw_metadata: Any) -> dict[str, Any]:
    if isinstance(raw_metadata, dict):
        return raw_metadata
    if isinstance(raw_metadata, str) and raw_metadata.strip():
        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _looks_like_oecd_dataflow_catalog_row(
    code: str,
    category: str,
    raw_metadata: Any,
) -> bool:
    category_lower = str(category or "").strip().lower()
    metadata = _parse_raw_metadata(raw_metadata)
    metadata_type = str(metadata.get("type") or "").strip().lower()
    normalized_code = str(code or "").strip().upper()
    return (
        category_lower == "oecd dataflow"
        or metadata_type == "oecd_dataset"
        or normalized_code.startswith("OECD_DF_")
        or normalized_code.startswith("OECD_SEEA_")
    )


def oecd_catalog_sampler_supportability_reason(
    code: str | None,
    name: str | None = None,
    category: str | None = None,
    raw_metadata: Any = None,
) -> str | None:
    """Return supportability reason for stale OECD dataflow aliases.

    Current OECD runtime exact provider-code execution expects SDMX
    structure/dataflow tuple shapes such as ``DSD_AEA@DF_AEA`` or
    ``OECD.SDD.NAD,DSD_NASEC10@DF_QNA``.  Short catalog aliases such as
    ``OECD_DF_SDG_GLC`` are valid catalog inventory but not executable through
    the same production path, so validation must inventory them separately.
    """
    del name  # Name prose is not supportability authority.
    normalized_code = str(code or "").strip().upper()
    if not normalized_code:
        return None
    if _RUNTIME_SHAPED_DATAFLOW_RE.fullmatch(normalized_code):
        return None
    if _looks_like_oecd_dataflow_catalog_row(normalized_code, str(category or ""), raw_metadata):
        return OECD_RUNTIME_DATAFLOW_ALIAS_UNSUPPORTED_REASON
    return None
