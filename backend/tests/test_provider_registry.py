"""Mechanism tests for the ProviderSpec identity registry.

These test the MECHANISM (registry -> derived views), not single queries:
- every dispatched provider has a ProviderSpec, and vice versa;
- every directive alias of every provider is detectable after every
  directive verb (so a new provider cannot be half-registered again);
- ordinary-language phrases never become an absolute explicit route;
- alias tiers stay disjoint and inside the normalize tier;
- every label a provider stamps on metadata.source normalizes back to it;
- the substitution note is honest for parenthetical labels and sentinels.
"""

from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.routing.unified_router import (
    _LEGACY_TOPIC_CUES,
    _START_OF_QUERY_PROVIDERS,
    detect_explicit_provider_match,
)
from backend.utils.providers import (
    NON_PROVIDER_SOURCES,
    PROVIDER_SPECS,
    PROVIDER_SPECS_BY_CANONICAL,
    db_provider_lookup,
    normalize_provider_name,
    provider_option_labels,
    provider_progress_labels,
    provider_title_aliases,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _dispatch_providers() -> set[str]:
    """Canonical providers handled by fetch_from_provider_dispatch branches."""
    from backend.services import data_fetcher

    source = inspect.getsource(data_fetcher.fetch_from_provider_dispatch)
    func = ast.parse(source.lstrip()).body[0]
    found: set[str] = set()
    for stmt in func.body:
        if not isinstance(stmt, ast.If) or not isinstance(stmt.test, ast.Compare):
            continue
        test = stmt.test
        if not (isinstance(test.left, ast.Name) and test.left.id == "provider"):
            continue
        literals: list[str] = []
        for comparator in test.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(comparator.value, str):
                literals.append(comparator.value)
            elif isinstance(comparator, (ast.Set, ast.Tuple, ast.List)):
                literals.extend(
                    elt.value for elt in comparator.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                )
        registered = {lit for lit in literals if lit in PROVIDER_SPECS_BY_CANONICAL}
        assert len(registered) == 1, f"dispatch branch {literals} has no unique ProviderSpec"
        found |= registered
    return found


def test_registry_covers_exactly_the_dispatched_providers() -> None:
    assert _dispatch_providers() == set(PROVIDER_SPECS_BY_CANONICAL)


_DIRECTIVES = ("from {}", "using {}", "via {}", "according to {}", "use {}")
_DIRECTIVE_CASES = [
    (spec.name, alias, template)
    for spec in PROVIDER_SPECS
    for alias in spec.directive_aliases
    for template in _DIRECTIVES
]


@pytest.mark.parametrize(("provider", "alias", "template"), _DIRECTIVE_CASES)
def test_every_directive_alias_is_detectable(provider, alias, template) -> None:
    query = f"show me the latest data {template.format(alias)}"
    match = detect_explicit_provider_match(query)
    assert match is not None and match[0] == provider


@pytest.mark.parametrize("spec", PROVIDER_SPECS, ids=lambda s: s.canonical)
def test_every_provider_display_name_is_detectable_after_from(spec) -> None:
    match = detect_explicit_provider_match(f"GDP from {spec.display_name}")
    assert match is not None and match[0] == spec.name


@pytest.mark.parametrize(
    "query",
    [
        # ordinary language that merely contains a loose provider-ish word
        "population data from UN",
        "data from UN",
        "GDP using exchange rate conversion",
        "GDP in USD using exchange rate of 2020",
        "exchange rate in China",
        "show me China macro indicators",
        "china macro data overview",
        "China macroeconomic outlook",
        "macro data for China",
        "US GDP",
        "bisque production in France",
        "unemployment in Canada",
        "from the beginning of 2020",
    ],
)
def test_ordinary_language_is_never_an_explicit_provider(query) -> None:
    assert detect_explicit_provider_match(query) is None


def test_bare_tier_order_is_registry_order() -> None:
    # First-match order is behavior consumed by 24 call sites.
    assert detect_explicit_provider_match("world bank data vs fred") == ("FRED", "fred")
    assert _START_OF_QUERY_PROVIDERS == ["OECD", "IMF", "BIS", "Eurostat"]


def _strip_data_suffix(alias: str) -> str:
    return alias[: -len(" data")] if alias.endswith(" data") else alias


def test_explicit_alias_tiers_stay_inside_the_normalize_tier() -> None:
    for spec in PROVIDER_SPECS:
        normalize = {alias.lower() for alias in spec.normalize_aliases}
        for alias in (*spec.bare_aliases, *spec.directive_aliases):
            assert _strip_data_suffix(alias) in normalize, (spec.canonical, alias)
            assert normalize_provider_name(_strip_data_suffix(alias)) == spec.canonical


def test_no_alias_is_claimed_by_two_providers() -> None:
    owner: dict[str, str] = {}
    for spec in PROVIDER_SPECS:
        for alias in (*spec.normalize_aliases, *spec.bare_aliases, *spec.directive_aliases):
            key = alias.lower()
            assert owner.setdefault(key, spec.canonical) == spec.canonical, key


def test_legacy_topic_cues_are_quarantined_not_registered() -> None:
    registered = {
        alias
        for spec in PROVIDER_SPECS
        for alias in (*spec.normalize_aliases, *spec.bare_aliases, *spec.directive_aliases)
    }
    cues = {cue for cues in _LEGACY_TOPIC_CUES.values() for cue in cues}
    assert cues.isdisjoint(registered)
    # Frozen: the quarantine may shrink, never grow.
    assert cues <= {"the fed", "eu statistics", "european statistics", "crypto prices"}


def test_loose_normalize_aliases_never_reach_user_text_tiers() -> None:
    for spec in PROVIDER_SPECS:
        explicit = set(spec.bare_aliases) | set(spec.directive_aliases)
        for loose in ("un", "wb", "exchange rate"):
            assert loose not in explicit


def test_title_wrapper_view_uses_only_user_text_tiers() -> None:
    # exact_title_search_inputs strips these from USER text, so the lenient
    # normalize-only tier must never leak in ("un" chopped "Unwrought" into
    # "wrought"; "wb" chopped "WBL index").
    for spec in PROVIDER_SPECS:
        title_aliases = provider_title_aliases(spec.canonical) or []
        allowed = set(spec.directive_aliases) | set(spec.title_wrapper_aliases)
        assert set(title_aliases) == allowed, spec.canonical
        normalize_only = set(spec.normalize_aliases) - allowed
        assert normalize_only.isdisjoint(title_aliases), spec.canonical
        for loose in ("un", "wb"):
            assert loose not in title_aliases
        # Title-wrapper extras are identity forms (inside the normalize tier)
        # but are never explicit-request aliases.
        for alias in spec.title_wrapper_aliases:
            assert alias in spec.normalize_aliases
            assert alias not in spec.bare_aliases and alias not in spec.directive_aliases


def test_display_order_is_a_total_order_separate_from_routing_order() -> None:
    from backend.utils.providers import provider_display_specs

    orders = [spec.display_order for spec in PROVIDER_SPECS]
    assert len(set(orders)) == len(orders)
    assert set(provider_display_specs()) == set(PROVIDER_SPECS)


def test_derived_identity_views_cover_every_registered_provider() -> None:
    from backend.services.indicator_clarification import _handle_data_sources_query

    db_lookup = db_provider_lookup()
    labels = provider_option_labels()
    progress = provider_progress_labels()
    message = _handle_data_sources_query("which data sources", "c1").message
    assert f"**{len(PROVIDER_SPECS)} data sources**" in message
    for spec in PROVIDER_SPECS:
        assert db_lookup[spec.canonical] == spec.name
        assert labels[spec.canonical] == spec.name
        assert normalize_provider_name(labels[spec.canonical]) == spec.canonical
        assert progress[spec.canonical]
        assert provider_title_aliases(spec.name)
        assert f"- **{spec.display_name}** — {spec.description}" in message
        assert normalize_provider_name(spec.display_name) == spec.canonical
        assert normalize_provider_name(spec.name) == spec.canonical


def _emitted_source_label_prefixes() -> list[tuple[str, str]]:
    """(provider module, label prefix) for every source= literal providers stamp."""
    pattern = re.compile(r"""source\s*=\s*f?["']([^"'{(]+)""")
    found = []
    for path in sorted((_REPO_ROOT / "backend/providers").glob("*.py")):
        for label in pattern.findall(path.read_text()):
            label = label.strip()
            if label:
                found.append((path.stem, label))
    return found


@pytest.mark.parametrize(("module", "label"), _emitted_source_label_prefixes())
def test_every_emitted_source_label_normalizes_to_a_registered_provider(module, label) -> None:
    assert normalize_provider_name(label) in PROVIDER_SPECS_BY_CANONICAL, (module, label)


# ---------------------------------------------------------------------------
# Substitution note
# ---------------------------------------------------------------------------

def _note(explicit: str, sources: list) -> str:
    from backend.services.query_helpers import add_provider_transparency

    svc = SimpleNamespace(_detect_explicit_provider=lambda _q: explicit)
    data = [SimpleNamespace(metadata=SimpleNamespace(source=s)) for s in sources]
    result = SimpleNamespace(data=data, intent=object(), message=None)
    add_provider_transparency(svc, result, "query")
    return result.message or ""


@pytest.mark.parametrize(
    ("explicit", "sources", "note_expected"),
    [
        ("ChinaMacro", ["ChinaMacro (NBS via EastMoney datacenter)"], False),
        ("ChinaMacro", ["ChinaMacro (MOFCOM)"], False),
        ("FRED", ["FRED (Federal Reserve)"], False),
        ("WorldBank", ["World Bank", "IMF"], False),
        ("BIS", ["World Bank"], True),
        ("ChinaMacro", ["FRED"], True),  # e.g. "US GDP from ChinaMacro" fell back
        ("IMF", [None], False),  # no served label -> no "Showing results from  instead."
        ("BIS", ["Catalog"], False),  # not-available sentinel is not a provider
    ],
)
def test_substitution_note_is_honest(explicit, sources, note_expected) -> None:
    message = _note(explicit, sources)
    assert ("did not have this data" in message) is note_expected
    if note_expected:
        assert f"**Note:** {explicit} did not have this data." in message
        assert "Catalog" not in message


def test_detected_chinamacro_request_served_by_chinamacro_has_no_note() -> None:
    match = detect_explicit_provider_match("China PMI from ChinaMacro")
    assert match is not None
    assert _note(match[0], ["ChinaMacro (NBS via EastMoney datacenter)"]) == ""


def test_non_provider_sentinels_are_not_registered() -> None:
    assert NON_PROVIDER_SOURCES.isdisjoint(PROVIDER_SPECS_BY_CANONICAL)


def test_country_extraction_keeps_country_outside_the_provider_name() -> None:
    from backend.services.query_helpers import extract_countries_from_query

    assert extract_countries_from_query("China PMI from ChinaMacro") == ["CN"]
    # Provider-name tokens are identity, not geography — same contract as
    # "GDP from Statistics Canada" (country left to the LLM parse).
    assert extract_countries_from_query("PMI from China Macro") == []
    assert extract_countries_from_query("GDP from Statistics Canada") == []

