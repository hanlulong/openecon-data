"""Provider-native IMF public-surface supportability helpers.

These helpers deliberately avoid judging arbitrary natural-language query text.
They only fail closed when an exact IMF provider code can be checked against
local/provider catalog metadata and the current runtime support matrix.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON = "imf_non_weo_public_surface_unsupported"
UNSUPPORTED_IMF_DATAMAPPER_CATEGORY_REASON = "imf_public_datamapper_v1_category_not_served"
UNSUPPORTED_IMF_DATAFLOW_DIRECT_SERIES_REASON = "imf_catalog_dataflow_not_single_series"

# Evidence: Ralph168 provider-contract inventory found that the sampled
# ALT_FISCAL catalog rows, plus the first 20 ALT_FISCAL catalog controls, return
# public DataMapper v1 API echoes with no ``values`` payload.  This is a
# sampler prior only: it is not a runtime blocker and it is deliberately not a
# broad "all non-WEO" rule because AIPI/FPP/CF/SPRLU controls have public
# DataMapper values.
_PUBLIC_DATAMAPPER_V1_UNSERVED_CATEGORIES = frozenset({"ALT_FISCAL"})

_EXACT_CODE_TOKEN_RE = re.compile(
    r"\b[A-Z0-9][A-Z0-9_\.]{1,40}\b",
    flags=re.IGNORECASE,
)


def _normalize_code(value: Any) -> str:
    return str(value or "").strip().upper()


def _looks_like_imf_provider_code(value: Any) -> bool:
    """Return True for provider-native code tokens, not prose labels."""
    token = _normalize_code(value)
    if not token:
        return False
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_\.]{1,40}", token):
        return False
    return (
        "_" in token
        or "." in token
        or any(ch.isdigit() for ch in token)
        or token in {"NGDP", "NGDPD", "NGDPRPC", "GG_DEBT_GDP", "TTT"}
    )


def _strip_imf_iso3_country_prefix(code: str) -> str:
    value = _normalize_code(code)
    if "_" not in value:
        return value
    prefix = value.split("_", 1)[0]
    if len(prefix) != 3 or not prefix.isalpha():
        return value
    try:
        from ..routing.country_resolver import CountryResolver

        if CountryResolver.to_iso2(prefix):
            return value[len(prefix) + 1 :]
    except Exception:
        pass
    return value


def _is_imf_aggregate_trade_code(code: str) -> bool:
    bare_code = _strip_imf_iso3_country_prefix(code)
    if any(fragment in bare_code for fragment in ("_H5_", "_HS", "_SITC", "_CPC", "_BEC")):
        return False
    return bool(
        re.fullmatch(r"(?:T?[XM]G?|[XM]G)_(?:FOB|CIF)_(?:USD|XDC)", bare_code)
        or re.fullmatch(r"(?:T?[XM]G?|[XM]G)_(?:FOB|CIF)_(?:USD|XDC)_IX", bare_code)
    )


def _is_imf_aggregate_cpi_code(code: str, name: str = "") -> bool:
    bare_code = _strip_imf_iso3_country_prefix(code)
    text = str(name or "").lower()
    if "weight" in text:
        return False
    if bare_code == "PCPI_IX":
        return True
    if re.fullmatch(r"PCPI_CP_?\d{2}(?:_BY\d{4}|_BY\d{4}M\d{2})?_IX", bare_code):
        return True
    return False


def _is_imf_aggregate_ppi_code(code: str, name: str = "") -> bool:
    bare_code = _strip_imf_iso3_country_prefix(code)
    text = str(name or "").lower()
    if any(fragment in bare_code for fragment in ("ISIC", "NACE")):
        return False
    if any(term in text for term in ["by activity", "commodities by activity", "manufacture of", "mining of"]):
        return False
    return bare_code in {"PPPI_IX", "PPI_IX", "WPI_IX"} or bare_code == "PPPIA_IX"


def _is_imf_bop_public_sdmx_code(code: str, name: str = "") -> bool:
    bare_code = _strip_imf_iso3_country_prefix(code)
    if bare_code.startswith("BOP_"):
        bare_code = bare_code[len("BOP_") :]
    if re.search(r"(?:^|_)\d+[A-Z]?(?:_|$)", bare_code):
        return False
    text = f"{name or ''} {code or ''}".lower()
    if not (
        "balance of payments" in text
        or "bpm6" in text
        or "_bp6_" in f"_{bare_code.lower()}_"
    ):
        return False
    return bool(re.fullmatch(r"B[A-Z0-9_]*(?:_BP6)?(?:_FY)?_(?:USD|EUR|XDC|XDR)", bare_code))


def imf_public_sdmx_runtime_family(code: str, name: str = "", category: str = "") -> str | None:
    """Return the exact public IMF.STA SDMX family currently executable.

    This is a provider support matrix, not a semantic match.  It only accepts
    exact provider-native code shapes that map mechanically to implemented
    public IMF.STA dataset keys.
    """
    if str(category or "").strip().lower() == "weo":
        return None
    if _is_imf_aggregate_trade_code(code):
        return "itg_aggregate"
    if _is_imf_aggregate_cpi_code(code, name):
        return "cpi_aggregate"
    if _is_imf_aggregate_ppi_code(code, name):
        return "ppi_aggregate"
    return None


def _catalog_entry_for_code(code: str) -> Mapping[str, Any]:
    exact_code = _normalize_code(code)
    if not exact_code:
        return {}
    try:
        from ..services.indicator_database import get_indicator_lookup

        entry = get_indicator_lookup().get("IMF", exact_code)
        if isinstance(entry, Mapping):
            return entry
    except Exception:
        return {}
    return {}


def imf_catalog_surface_supportability_reason(
    code: str = "",
    name: str = "",
    category: str = "",
) -> str | None:
    """Fail closed only for exact IMF catalog codes outside runtime support.

    Natural-language wording is intentionally ignored.  A supportability reason
    is returned only when provider-native metadata identifies the code as an IMF
    ``INDICATOR`` row and no implemented exact public-SDMX family can execute it.
    """
    exact_code = _normalize_code(code)
    if not _looks_like_imf_provider_code(exact_code):
        return None

    entry = _catalog_entry_for_code(exact_code)
    label = str(name or entry.get("name") or "")
    category_value = str(category or entry.get("category") or "").strip().upper()

    if imf_public_sdmx_runtime_family(exact_code, label, category_value):
        return None
    if category_value and category_value != "INDICATOR":
        return None
    if category_value == "INDICATOR":
        return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON
    return None


def imf_catalog_sampler_supportability_reason(
    code: str = "",
    name: str = "",
    category: str = "",
) -> str | None:
    """Return sampler-only IMF supportability provenance.

    This extends the runtime exact-code support matrix with evidence-backed
    validation-sampler priors.  It must not be used by production query
    execution because these reasons are candidate-selection provenance, not
    final supportability blockers.
    """
    runtime_reason = imf_catalog_surface_supportability_reason(code, name, category)
    if runtime_reason:
        return runtime_reason

    exact_code = _normalize_code(code)
    entry = _catalog_entry_for_code(exact_code)
    category_value = str(category or entry.get("category") or "").strip().upper()
    if category_value == "DATAFLOW" or str(code or "").strip().upper().startswith("DF:"):
        return UNSUPPORTED_IMF_DATAFLOW_DIRECT_SERIES_REASON

    if not _looks_like_imf_provider_code(exact_code):
        return None

    if category_value in _PUBLIC_DATAMAPPER_V1_UNSERVED_CATEGORIES:
        return UNSUPPORTED_IMF_DATAMAPPER_CATEGORY_REASON
    return None


def _exact_code_candidates(
    query: str,
    indicators: Iterable[Any] | None,
    params: Mapping[str, Any],
) -> list[str]:
    candidates: list[str] = []
    for value in (
        params.get("indicator"),
        params.get("__semantic_indicator_code"),
        params.get("source_indicator_code"),
    ):
        if _looks_like_imf_provider_code(value):
            candidates.append(_normalize_code(value))
    for indicator in indicators or []:
        if _looks_like_imf_provider_code(indicator):
            candidates.append(_normalize_code(indicator))
    for match in _EXACT_CODE_TOKEN_RE.findall(str(query or "")):
        if _looks_like_imf_provider_code(match):
            candidates.append(_normalize_code(match))
    return list(dict.fromkeys(candidates))


def imf_exact_provider_surface_supportability_reason(
    query: str = "",
    indicators: Iterable[Any] | None = None,
    params: Mapping[str, Any] | None = None,
) -> str | None:
    """Return an IMF supportability reason from exact provider-code evidence only.

    Query text only contributes literal provider-code tokens; prose never
    becomes final supportability authority.
    """
    params = params or {}
    label = str(params.get("__semantic_indicator_label") or params.get("indicator_label") or "")
    category = str(params.get("__semantic_indicator_category") or params.get("category") or "")
    for code in _exact_code_candidates(query, indicators, params):
        reason = imf_catalog_surface_supportability_reason(code, label, category)
        if reason:
            return reason
    return None


def imf_query_only_public_surface_reason(
    query: str = "",
    indicators: Iterable[Any] | None = None,
    params: Mapping[str, Any] | None = None,
) -> str | None:
    """Compatibility wrapper for the old query-text guard name."""
    return imf_exact_provider_surface_supportability_reason(query, indicators, params)
