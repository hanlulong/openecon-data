"""Provider-agnostic supportability filter for offered clarification options.

A clarification menu must never offer an exact provider code that dispatch would
then refuse to execute.  When it does, the user picks the option and the fetch
fail-closes -- a dead-end menu choice.  This module is the single seam that
clarification builders consult before offering a concrete ``(provider, code)``
candidate.  It delegates to the same per-provider, exact-code supportability
predicates that fire at dispatch (today only IMF has one), so an *offered*
option and a *dispatched* option are judged by identical logic.

Contract (mirrors ``imf_supportability.py``): only exact provider codes plus
catalog metadata are ever judged.  Natural-language query text is never consulted
here -- there is deliberately no query parameter.  New per-provider predicates
plug in by adding a branch keyed on the normalized provider name; every
clarification builder then inherits the filter with no further change.
"""

from __future__ import annotations

from .imf_supportability import imf_catalog_surface_supportability_reason
from .providers import normalize_provider_name


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

    # Other providers have no exact-code supportability predicate yet.  When one
    # is added, delegate to it here keyed on ``provider_norm`` so every
    # clarification builder inherits the filter without change.
    return None
