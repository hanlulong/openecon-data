"""Provider coverage is a CATALOG FACT, not a hardcoded assumption.

User rule (2026-07-19): correctness beats provider preference — a coverage
predicate must never discard a provider that would answer correctly. The old
`FRED -> US-only` shortcut forced multi-country monthly requests down to
annual-only providers (preference audit: "India and China CPI monthly" served
2 WorldBank annual points while FRED's monthly mirrors sat reachable).
`_fred_catalog_covers_country` answers from indicators.db instead.
"""
from __future__ import annotations

from backend.services.provider_fallback import (
    _FRED_COVERAGE_CACHE,
    _fred_catalog_covers_country,
    provider_covers_country_list,
)


def setup_function(_fn):
    _FRED_COVERAGE_CACHE.clear()


def test_fred_covers_us_unconditionally():
    assert _fred_catalog_covers_country("US") is True


def test_fred_covers_major_economies_from_catalog():
    # FRED's international mirrors are country-titled catalog rows — the
    # predicate must reflect them (these were all blocked by the US-only
    # shortcut before).
    for iso2 in ("IN", "CN", "BR", "JP", "DE"):
        assert _fred_catalog_covers_country(iso2) is True, iso2


def test_multi_country_list_no_longer_forces_downgrade():
    # The exact live defect case from the preference audit.
    assert provider_covers_country_list("FRED", ["IN", "CN"]) is True


def test_answers_are_cached():
    _fred_catalog_covers_country("IN")
    assert "IN" in _FRED_COVERAGE_CACHE


def test_unknown_iso2_errs_toward_covered():
    # No aliases resolve for a junk code -> names empty -> default covered
    # (keeps the routed provider; the fetch/fallback chain still guards).
    assert _fred_catalog_covers_country("ZZ") is True


def test_other_providers_unchanged():
    assert provider_covers_country_list("STATSCAN", ["CA"]) is True
    assert provider_covers_country_list("STATSCAN", ["US"]) is False
    assert provider_covers_country_list("CHINAMACRO", ["CN"]) is True
    assert provider_covers_country_list("CHINAMACRO", ["US"]) is False
