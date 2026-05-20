"""Provider-native BIS supportability classification for validation sampling.

The helpers in this module classify BIS catalog rows by their provider-native
SDMX dataflow code only. They are used by validation materializers to keep
known-unimplemented BIS dataflows in supportability inventory instead of using
them as ordinary user-answerability evidence. They do not route runtime queries,
map natural language to dataflows, or mark any row as a pass.
"""

from __future__ import annotations

from typing import Final

# BIS dataflows that the current public user-answerability route can execute
# from catalog-style natural language query text.  Keep this list aligned with
# backend.providers.bis.BISProvider.fetch_indicator, but do not include
# dataflows that only work through exact provider-code passthrough or the
# exact-dataflow fallback. Those remain supported runtime escape hatches, not
# ordinary lower-bound evidence for natural-language catalog coverage.
BIS_RUNTIME_SUPPORTED_DATAFLOWS: Final[frozenset[str]] = frozenset({
    "WS_CBPOL",
    "WS_CBTA",
    "WS_TC",
    "WS_SPP",
    "WS_CPP",
    "WS_DPP",
    "WS_DSR",
    "WS_GLI",
    "WS_CREDIT_GAP",
    "WS_DEBT_SEC2_PUB",
    "WS_LONG_CPI",
    "WS_XRU",
    "WS_EER",
    # Provider-native BIS dataflows proven executable through the normal
    # exact-title/selector/user-answerability route.  Keep this per-dataflow
    # and evidence-gated: do not broaden to whole CPMI/derivatives/securities
    # families unless each dataflow is replayed through the same path.
    "WS_CPMI_CASHLESS",
    "WS_CPMI_CT1",
    "WS_CPMI_CT2",
    "WS_CPMI_DEVICES",
    "WS_CPMI_INSTITUT",
    "WS_CPMI_MACRO",
    "WS_CPMI_PARTICIP",
    "WS_DER_OTC_TOV",
    "WS_NA_SEC_DSS",
})

_BIS_RELEASE_CALENDAR_CODES: Final[frozenset[str]] = frozenset({
    "BIS_REL_CAL",
    "REL_CAL",
    "BIS_RELEASE_CALENDAR",
})


def normalize_bis_dataflow_code(code: str | None) -> str:
    """Return a canonical BIS dataflow code from catalog/runtime aliases."""
    normalized = str(code or "").strip().upper().replace(".", "_")
    if not normalized:
        return ""
    if normalized.startswith("BIS_WS_"):
        return normalized.removeprefix("BIS_")
    if normalized.startswith("BIS_WS") and not normalized.startswith("BIS_WS_"):
        return normalized.removeprefix("BIS_")
    if normalized.startswith("BIS_REL_CAL"):
        return "BIS_REL_CAL"
    if normalized.startswith("BIS_") and normalized.removeprefix("BIS_").startswith("WS_"):
        return normalized.removeprefix("BIS_")
    return normalized


def bis_catalog_sampler_supportability_reason(
    code: str | None,
    name: str | None = None,
    category: str | None = None,
) -> str | None:
    """Return supportability reason for BIS rows outside runtime-supported dataflows.

    This is a metadata-only sampler prior. It may inspect only provider-native
    catalog fields (code/name/category), and is intentionally not a runtime
    provider-selection rule or answer adjudication rule.
    """
    normalized_code = normalize_bis_dataflow_code(code)
    name_upper = str(name or "").strip().upper()
    category_lower = str(category or "").strip().lower()

    if normalized_code in BIS_RUNTIME_SUPPORTED_DATAFLOWS:
        return None
    if normalized_code in _BIS_RELEASE_CALENDAR_CODES or name_upper == "BIS_RELEASE_CALENDAR":
        return "bis_release_calendar_not_runtime_supported"
    if normalized_code.startswith("WS_") or normalized_code.startswith("BIS_") or "bis" in category_lower:
        return "bis_runtime_dataflow_not_implemented"
    return None
