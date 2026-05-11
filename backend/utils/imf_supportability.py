"""Conservative IMF public-surface supportability guards.

These helpers protect runtime and certification from treating detailed IMF
catalog slices as ordinary legacy DataMapper requests.  They deliberately do
not map detailed rows to broad proxies; they only identify query shapes that
need true IMF dataset-family routing before they can be claim-grade successes.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON = "imf_non_weo_public_surface_unsupported"

_PUBLIC_SDMX_CODE_RE = re.compile(
    r"\b(?:"
    r"(?:T[MX]G?_(?:FOB|CIF)_USD)"
    r"|(?:P{1,2}PIA?_IX)"
    r"|(?:PCPI_(?:X?CP)_?\d{2}(?:_BY\d{4}(?:M\d{2})?)?_IX)"
    r"|(?:(?:[A-Z]{3}_)?(?:BOP_)?B[A-Z0-9_]*(?:_BP6)?(?:_FY)?_(?:USD|EUR|XDC|XDR))"
    r")\b",
    flags=re.IGNORECASE,
)

_DETAIL_MARKERS = {
    "activity",
    "activities",
    "agricultural",
    "agriculture",
    "animal",
    "barrels",
    "base year",
    "beverage",
    "budgetary",
    "capital city",
    "cash",
    "central government",
    "clothing",
    "coicop",
    "compensation of employees",
    "construction",
    "current activity",
    "definition",
    "fabrics",
    "fiscal year",
    "food",
    "fruit",
    "fruits",
    "general government",
    "government and public sector finance",
    "industry",
    "isic",
    "kathmandu valley",
    "local government",
    "manufactur",
    "mining",
    "nace",
    "non-alcoholic beverages",
    "oil production",
    "publishing",
    "quarry",
    "regional government",
    "sector",
    "service activities",
    "terms of trade",
    "total debt",
    "vegetables",
    "veterinary",
    "wages and salaries",
}

_CONSUMER_PRICE_DETAIL_MARKERS = {
    "all items special indexes",
    "base year previous period",
    "capital city",
    "clothing",
    "coicop",
    "definition",
    "fabrics",
    "food",
    "food and beverage",
    "food and non-alcoholic beverages",
    "food at home",
    "fruits and vegetables",
    "harmonized consumer prices",
    "housing gas and other fuels",
    "services housing",
    "kathmandu valley",
    "non-alcoholic beverages",
}

_FISCAL_DETAIL_MARKERS = {
    "budgetary central government",
    "cash",
    "central government",
    "compensation of employees",
    "expense",
    "fiscal year",
    "government and public sector finance",
    "local government",
    "regional government",
    "revenue",
    "social contributions",
    "tax",
    "taxes",
    "total expenditure",
    "domestic public debt",
    "public debt",
    "bridge loans",
    "total debt",
    "wages and salaries",
}

_NATIONAL_ACCOUNTS_DETAIL_MARKERS = {
    "collective consumption expenditure",
    "domestic output",
    "external balance of goods and services",
    "financial intermediation nominal services",
    "gross domestic expenditure",
    "gross real national income",
    "gross real saving",
    "gross value added",
    "memorandum items",
    "nace2",
    "net exports crude oil",
    "public final consumption expenditure",
    "real chained",
    "subsidies on products",
}

_NATIONAL_ACCOUNTS_DEFLATOR_RE = re.compile(
    r"\bdeflator\b.*\b(?:gross value added|subsidies on products)\b"
    r"|\b(?:gross value added|subsidies on products)\b.*\bdeflator\b",
    flags=re.IGNORECASE,
)

_SOCIAL_DEMOGRAPHIC_DETAIL_MARKERS = {
    "mortality rate",
    "poverty",
    "social indicators",
    "socio demographic",
    "socio-demographic",
}

_COMPLEX_FINANCE_DETAIL_MARKERS = {
    "assets loans sectoral",
    "financial auxiliaries",
    "financial corporations",
    "financial soudness",
    "financial soundness",
    "monetary net foreign assets",
    "sectoral accounts",
}

_SPECIAL_PUBLIC_ENTITY_MARKERS = {
    "federation income and distribution",
    "panama canal authority",
    "state oil fund",
}


def _normalize_text(parts: Iterable[Any]) -> str:
    text = " ".join(str(part or "") for part in parts if part is not None)
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip().lower()


def _has_any(text: str, markers: Iterable[str]) -> bool:
    return any(marker in text for marker in markers)


def imf_query_only_public_surface_reason(
    query: str = "",
    indicators: Iterable[Any] | None = None,
    params: Mapping[str, Any] | None = None,
) -> str | None:
    """Return a supportability reason for unsupported natural IMF detail rows.

    The guard is intentionally conservative.  It leaves verified public SDMX
    code requests and broad aggregate DataMapper/SDMX concepts alone, while
    flagging detailed natural-language IMF catalog titles that currently cause
    slow fuzzy-resolution/fallback loops or require dataset-family routing not
    implemented in production.
    """
    params = params or {}
    indicator_parts = [str(indicator or "") for indicator in (indicators or [])]
    text = _normalize_text([query, *indicator_parts, params.get("__semantic_indicator_label")])
    if not text:
        return None

    explicit_indicator = str(params.get("indicator") or "").strip()
    if explicit_indicator and _PUBLIC_SDMX_CODE_RE.fullmatch(explicit_indicator):
        return None
    if _PUBLIC_SDMX_CODE_RE.search(text):
        return None

    # Broad aggregate concepts that are currently executable should remain on
    # the runtime path unless the query includes a detailed IMF catalog slice.
    detailed = _has_any(text, _DETAIL_MARKERS)

    if "terms of trade" in text:
        return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON

    if _has_any(text, _SPECIAL_PUBLIC_ENTITY_MARKERS):
        return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON

    if _has_any(text, {"mineral production", "quarried stone"}):
        return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON

    if _has_any(text, _SOCIAL_DEMOGRAPHIC_DETAIL_MARKERS):
        return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON

    if "population" in text and _has_any(
        text,
        {
            "by sex",
            "definition",
            "north west",
            "of which foreign resident",
            "socio demographic",
            "socio-demographic",
        },
    ):
        return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON

    if _has_any(text, _COMPLEX_FINANCE_DETAIL_MARKERS):
        return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON

    if _has_any(text, _NATIONAL_ACCOUNTS_DETAIL_MARKERS) or _NATIONAL_ACCOUNTS_DEFLATOR_RE.search(text):
        return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON

    if "external sector" in text and "external balance" in text:
        return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON

    if re.search(r"\b(?:import|export) price index\b", text) and detailed:
        return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON

    if "producer price index" in text and detailed:
        if "publishing" in text:
            return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON
        # Do not block the broad aggregate PPI query; only detailed PPI slices.
        broad_only = re.fullmatch(
            r"(?:[a-z .'-]+ )?producer price index(?: from imf)?",
            text,
        )
        if not broad_only:
            return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON
    elif "producer price index" in text and "publishing" in text:
        return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON

    if (
        ("consumer price" in text or "consumer prices" in text or "price index" in text)
        and (
            _has_any(text, _CONSUMER_PRICE_DETAIL_MARKERS)
            or re.search(r"\ball items\s+by\d{4}\b", text)
        )
    ):
        return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON

    if (
        ("fiscal" in text or "government and public sector finance" in text)
        and _has_any(text, _FISCAL_DETAIL_MARKERS)
        and not re.search(r"\bgeneral government (?:debt|net lending) fiscal from imf\b", text)
    ):
        return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON

    if "industrial production" in text and _has_any(
        text,
        {"current activity", "definition", "mining", "quarry", "manufactur", "construction", "economic activity", "base year"},
    ):
        return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON

    if "oil production" in text and _has_any(text, {"barrels", "definition", "not specified", "economic activity"}):
        return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON

    if "gross domestic product" in text and _has_any(
        text,
        {"industry base year", "base year-", "by industry", "activity", "production approach", "nominal gdp by industry"},
    ):
        return UNSUPPORTED_IMF_PUBLIC_SURFACE_REASON

    return None
