"""Provider-agnostic supportability filter for offered clarification options.

A clarification menu must never offer an exact provider code that dispatch would
then refuse to execute.  When it does, the user picks the option and the fetch
fail-closes -- a dead-end menu choice.  This module is the single seam that
clarification builders consult before offering a concrete ``(provider, code)``
candidate.  It delegates to the same per-provider, exact-code supportability
predicates that fire at dispatch (IMF's catalog-surface predicate; World Bank's
vintage-archive source predicate, the same one the indicator selector and the
WB alternate-code retry use), so an *offered* option and a *dispatched* option
are judged by identical logic.

Contract (mirrors ``imf_supportability.py``): only exact provider codes plus
catalog metadata are ever judged.  Natural-language query text is never consulted
here -- there is deliberately no query parameter.  New per-provider predicates
plug in by adding a branch keyed on the normalized provider name; every
clarification builder then inherits the filter with no further change.
"""

from __future__ import annotations

import logging

from .catalog_metadata_signals import (
    catalog_row_is_vintage_archive,
    provider_has_vintage_source_reference,
)
from .imf_supportability import imf_catalog_surface_supportability_reason
from .providers import normalize_provider_name

logger = logging.getLogger(__name__)


def _vintage_archive_source_reason(provider: str, code: str) -> str | None:
    """Reason string when the option's catalog row is a vintage-archive source.

    Structural: the row's ``raw_metadata.source`` is a source whose published
    dimension list carries a release ``Version`` -- the current-data fetch
    path answers "indicator not found / archived" for it, so offering it is a
    dead-end menu choice.  Fails open (``None``) when the provider has no
    source-dimension reference, the row is absent, or the lookup errors.
    """
    if not provider_has_vintage_source_reference(provider):
        return None
    try:
        # Lazy import: utils must not import services at module load.
        from ..services.indicator_database import get_indicator_lookup

        row = get_indicator_lookup().get(provider, code)
    except Exception as exc:
        logger.debug("Vintage-archive option check skipped for %s/%s: %s", provider, code, exc)
        return None
    if not row or not catalog_row_is_vintage_archive(provider, row.get("raw_metadata")):
        return None
    return (
        f"{code} belongs to a vintage-archive source (release-versioned database); "
        "the current-data endpoint cannot serve it"
    )


def option_supportability_reason(
    provider: str,
    code: str,
    name: str = "",
    category: str = "",
) -> str | None:
    """Return a dispatch-consistent supportability reason for an offered option.

    Returns a non-empty reason string when the exact ``(provider, code)``
    candidate would be rejected at dispatch and therefore must not be offered as
    a clarification choice.  Returns ``None`` when the option is safe to offer,
    or when no provider-native supportability predicate applies.

    Only the exact provider code and catalog metadata are judged.  ``name`` is
    provider/catalog metadata (an option label), never the user's query text;
    the per-provider predicate falls back to the catalog name when it is empty,
    exactly as dispatch does.
    """
    provider_norm = normalize_provider_name(provider)
    code_text = str(code or "").strip()
    if not code_text:
        return None

    if provider_norm == "IMF":
        return imf_catalog_surface_supportability_reason(code_text, name, category)

    if provider_norm == "WORLDBANK":
        return _vintage_archive_source_reason("WorldBank", code_text)

    # Other providers have no exact-code supportability predicate yet.  When one
    # is added, delegate to it here keyed on ``provider_norm`` so every
    # clarification builder inherits the filter without change.
    return None
