"""Region-coverage-aware cube/series selection.

When a query names a sub-national region (intent.subnationalRegion, e.g. "Ontario
GDP", "California unemployment"), selection must prefer a candidate that actually
covers that region rather than a national cube/series the subnational fail-closed
check would only discard. Two composable mechanisms plus a FRED analogue are
exercised here:

  (a) PRE-adjudication annotation — each StatsCan candidate cube is annotated
      with whether its Geography dimension contains the region (ground truth from
      cube metadata), and the adjudicator prompt is told to obey the annotation.
  (b) POST-pick guard — a picked cube that lacks the region's Geography member
      raises DataNotAvailableError EARLY (before coordinate probing) so the
      existing same-provider alternate-code retry re-adjudicates.

  FRED analogue — region-titled-series providers get the region as a preference
      in the adjudicator prompt (no coverage probe).

All network/LLM boundaries are mocked; there are no live-network tests here.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from backend.models import ParsedIntent
from backend.providers.statscan import StatsCanProvider
from backend.services.data_fetcher import _fetch_from_statscan
from backend.services.indicator_resolution import resolve_indicator_for_fetch
from backend.services.indicator_selector import (
    IndicatorSelector,
    SelectionResult,
    _SELECTION_CACHE,
)
from backend.utils.retry import DataNotAvailableError


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _geo_cube(*region_names: str) -> dict:
    """A cube whose Geography dimension has the given province/territory members."""
    return {
        "dimension": [
            {
                "dimensionNameEn": "Geography",
                "member": [
                    {"memberId": i + 1, "memberNameEn": name, "memberNameFr": name}
                    for i, name in enumerate(region_names)
                ],
            },
            {
                "dimensionNameEn": "Statistics",
                "member": [{"memberId": 1, "memberNameEn": "Estimate"}],
            },
        ]
    }


def _no_geo_cube() -> dict:
    return {
        "dimension": [
            {
                "dimensionNameEn": "Type of product",
                "member": [{"memberId": 1, "memberNameEn": "All products"}],
            }
        ]
    }


def _selector_settings() -> SimpleNamespace:
    return SimpleNamespace(
        indicator_telemetry_enabled=False,
        indicator_fusion="legacy",
        llm_provider="vllm",
        llm_base_url="http://localhost:8000",
        llm_model="test-model",
        selector_llm_base_url="",
        selector_llm_model="",
        openrouter_api_key="",
        llm_fallback_model="openai/gpt-oss-120b",
    )


def _enriched(*codes_names: tuple[str, str]):
    return [
        {
            "code": code,
            "name": name,
            "frequency": "",
            "unit": "",
            "end_date": "",
            "category": "",
            "description": "",
            "keywords": "",
            "discontinued": False,
        }
        for code, name in codes_names
    ]


class _CapturingResp:
    status_code = 200

    def __init__(self, content: str) -> None:
        self._content = content

    def raise_for_status(self):  # noqa: ANN201
        return None

    def json(self):  # noqa: ANN201
        return {"choices": [{"message": {"content": self._content}}]}


class _CapturingClient:
    """Fake async HTTP client that records the selector prompt and returns a PICK."""

    def __init__(self, sink: dict, pick_line: str = "PICK: 1") -> None:
        self._sink = sink
        self._pick_line = pick_line

    async def post(self, url, headers=None, json=None, timeout=None):  # noqa: ANN001, A002
        self._sink["prompt"] = json["messages"][0]["content"]
        return _CapturingResp(self._pick_line)


# ---------------------------------------------------------------------------
# (a-core) Provider region-coverage predicate + batch + assert
# ---------------------------------------------------------------------------

def test_geography_predicate_matches_member_including_abbrev_and_french() -> None:
    cube = _geo_cube("Canada", "Ontario", "Quebec", "Newfoundland and Labrador")
    assert StatsCanProvider.geography_dimension_covers_region(cube, "Ontario") is True
    assert StatsCanProvider.geography_dimension_covers_region(cube, "ON") is True  # abbrev
    assert StatsCanProvider.geography_dimension_covers_region(cube, "Newfoundland") is True
    # bracketed classification code on the member name still matches
    bracketed = _geo_cube("Canada", "Ontario [35]")
    assert StatsCanProvider.geography_dimension_covers_region(bracketed, "Ontario") is True


def test_geography_predicate_rejects_absent_and_coarser_regions() -> None:
    cube = _geo_cube("Canada", "Ontario", "Quebec")
    assert StatsCanProvider.geography_dimension_covers_region(cube, "California") is False
    # "Ontario" must not spuriously match a coarser "Northern Ontario" only cube
    partial = _geo_cube("Canada", "Northern Ontario")
    assert StatsCanProvider.geography_dimension_covers_region(partial, "Ontario") is False
    # a cube with no Geography dimension cannot serve any region
    assert StatsCanProvider.geography_dimension_covers_region(_no_geo_cube(), "Ontario") is False
    # empty region never matches
    assert StatsCanProvider.geography_dimension_covers_region(cube, "") is False


def test_region_coverage_from_cache_reports_true_false_unknown() -> None:
    prov = StatsCanProvider()
    prov._cube_metadata_cache["11111111"] = _geo_cube("Canada", "Ontario")
    prov._cube_metadata_cache["22222222"] = _geo_cube("Canada")  # national only
    cov = prov.region_coverage_from_cache(["11111111", "22222222", "99999999"], "Ontario")
    assert cov == {"11111111": True, "22222222": False, "99999999": None}
    # empty region → all unknown, no work
    assert prov.region_coverage_from_cache(["11111111"], "") == {"11111111": None}


def test_assert_region_supported_or_raise_early_supportability_error() -> None:
    prov = StatsCanProvider()
    prov._cube_metadata_cache["11111111"] = _geo_cube("Canada", "Ontario")
    prov._cube_metadata_cache["22222222"] = _geo_cube("Canada")

    async def run():
        # Covering cube → no raise.
        await prov.assert_region_supported_or_raise("11111111", "Ontario")
        # Empty region → no-op.
        await prov.assert_region_supported_or_raise("22222222", "")
        # Non-covering cube → machine-consumable early error.
        with pytest.raises(DataNotAvailableError) as exc:
            await prov.assert_region_supported_or_raise("22222222", "Ontario")
        assert "statscan_geography_not_covered" in str(exc.value)
        assert "Ontario" in str(exc.value)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# (a) Adjudicator prompt: coverage annotation + requirement (StatsCan)
# ---------------------------------------------------------------------------

def test_llm_pick_annotates_statscan_candidates_with_region_coverage(monkeypatch) -> None:
    selector = IndicatorSelector(settings=_selector_settings())
    monkeypatch.setattr(
        selector,
        "_enrich_candidates",
        lambda candidates, provider: _enriched(*candidates),
    )
    sink: dict = {}
    monkeypatch.setattr(
        "backend.services.http_pool.get_http_client",
        lambda: _CapturingClient(sink, pick_line="PICK: 1"),
    )

    async def probe(codes):
        return {"36100402": True, "36100104": False}

    result = asyncio.run(
        selector._llm_pick(  # pylint: disable=protected-access
            "GDP",
            [("36100402", "GDP, provincial and territorial"), ("36100104", "GDP, national")],
            "STATSCAN",
            region="Ontario",
            region_coverage_probe=probe,
        )
    )

    prompt = sink["prompt"]
    assert "[covers Ontario]" in prompt
    assert "[does NOT cover Ontario]" in prompt
    assert "REGION COVERAGE REQUIREMENT" in prompt
    assert result is not None and result.code == "36100402"


def test_llm_pick_marks_uncached_candidate_coverage_unknown(monkeypatch) -> None:
    selector = IndicatorSelector(settings=_selector_settings())
    monkeypatch.setattr(
        selector,
        "_enrich_candidates",
        lambda candidates, provider: _enriched(*candidates),
    )
    sink: dict = {}
    monkeypatch.setattr(
        "backend.services.http_pool.get_http_client",
        lambda: _CapturingClient(sink, pick_line="PICK: 1"),
    )

    async def probe(codes):
        return {"36100402": True}  # second candidate absent → None/unknown

    asyncio.run(
        selector._llm_pick(  # pylint: disable=protected-access
            "GDP",
            [("36100402", "GDP provincial"), ("99999999", "Some other cube")],
            "STATSCAN",
            region="Ontario",
            region_coverage_probe=probe,
        )
    )
    assert "[Ontario coverage unknown]" in sink["prompt"]


# ---------------------------------------------------------------------------
# FRED analogue: region requirement in prompt WITHOUT a coverage probe
# ---------------------------------------------------------------------------

def test_llm_pick_states_region_preference_for_fred_without_probe(monkeypatch) -> None:
    selector = IndicatorSelector(settings=_selector_settings())
    monkeypatch.setattr(
        selector,
        "_enrich_candidates",
        lambda candidates, provider: _enriched(*candidates),
    )
    sink: dict = {}
    monkeypatch.setattr(
        "backend.services.http_pool.get_http_client",
        lambda: _CapturingClient(sink, pick_line="PICK: 1"),
    )

    asyncio.run(
        selector._llm_pick(  # pylint: disable=protected-access
            "unemployment rate",
            [("CAUR", "Unemployment Rate in California"), ("UNRATE", "Unemployment Rate")],
            "FRED",
            region="California",
        )
    )
    prompt = sink["prompt"]
    assert "sub-national region California" in prompt
    assert "STRONGLY prefer" in prompt
    # No probe → no coverage annotations / requirement block.
    assert "REGION COVERAGE REQUIREMENT" not in prompt
    assert "[covers California]" not in prompt


def test_llm_pick_without_region_is_unchanged(monkeypatch) -> None:
    selector = IndicatorSelector(settings=_selector_settings())
    monkeypatch.setattr(
        selector,
        "_enrich_candidates",
        lambda candidates, provider: _enriched(*candidates),
    )
    sink: dict = {}
    monkeypatch.setattr(
        "backend.services.http_pool.get_http_client",
        lambda: _CapturingClient(sink, pick_line="PICK: 1"),
    )
    asyncio.run(
        selector._llm_pick(  # pylint: disable=protected-access
            "GDP", [("A", "GDP")], "STATSCAN",
        )
    )
    prompt = sink["prompt"]
    assert "REGION COVERAGE REQUIREMENT" not in prompt
    assert "sub-national region" not in prompt


# ---------------------------------------------------------------------------
# Cache identity: region is part of the selection-cache key
# ---------------------------------------------------------------------------

def test_selection_cache_key_includes_region(monkeypatch) -> None:
    _SELECTION_CACHE.clear()
    selector = IndicatorSelector(settings=_selector_settings())
    seen_regions: list = []

    async def fake_uncached(query, provider, **kwargs):  # noqa: ANN001, ARG001
        seen_regions.append(kwargs.get("region"))
        return SelectionResult(code="X", name="n", source="llm_pick")

    monkeypatch.setattr(selector, "_select_uncached", fake_uncached)

    async def run():
        await selector.select("gdp", "STATSCAN", region="Ontario")  # miss
        await selector.select("gdp", "STATSCAN", region="Ontario")  # cache HIT
        await selector.select("gdp", "STATSCAN", region="Quebec")   # miss (region differs)

    asyncio.run(run())
    # Ontario served from cache the 2nd time; Quebec is a distinct identity.
    assert seen_regions == ["Ontario", "Quebec"]
    _SELECTION_CACHE.clear()


# ---------------------------------------------------------------------------
# Resolution wiring: subnationalRegion → selector.select(region=…, probe)
# ---------------------------------------------------------------------------

def test_resolution_passes_region_and_coverage_probe_to_selector() -> None:
    class _CovProv:
        def region_coverage_from_cache(self, codes, region):
            return {c: (c == "36100402") for c in codes}

    svc = SimpleNamespace(
        statscan_provider=_CovProv(),
        _looks_like_provider_indicator_code=lambda _p, _i: False,
        _verify_semantic_discriminators=lambda *_a, **_k: True,
    )
    intent = ParsedIntent(
        apiProvider="STATSCAN",
        indicators=["GDP"],
        parameters={"country": "CA"},
        clarificationNeeded=False,
        originalQuery="Ontario GDP",
        subnationalRegion="Ontario",
    )

    select_mock = AsyncMock(
        return_value=SelectionResult(code="36100402", name="GDP provincial", source="llm_pick")
    )
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=select_mock):
        asyncio.run(
            resolve_indicator_for_fetch(svc, "STATSCAN", intent, dict(intent.parameters or {}))
        )

    select_mock.assert_awaited_once()
    kwargs = select_mock.await_args.kwargs
    assert kwargs.get("region") == "Ontario"
    probe = kwargs.get("region_coverage_probe")
    assert callable(probe)
    # The probe is cache-only and returns real coverage for the candidate cubes.
    cov = asyncio.run(probe(["36100402", "36100104"]))
    assert cov == {"36100402": True, "36100104": False}


def test_resolution_omits_probe_for_fred_but_still_passes_region() -> None:
    svc = SimpleNamespace(
        fred_provider=SimpleNamespace(),
        statscan_provider=SimpleNamespace(),
        _looks_like_provider_indicator_code=lambda _p, _i: False,
        _verify_semantic_discriminators=lambda *_a, **_k: True,
    )
    intent = ParsedIntent(
        apiProvider="FRED",
        indicators=["unemployment rate"],
        parameters={"country": "US"},
        clarificationNeeded=False,
        originalQuery="California unemployment rate",
        subnationalRegion="California",
    )
    select_mock = AsyncMock(
        return_value=SelectionResult(code="CAUR", name="Unemployment Rate in California", source="llm_pick")
    )
    with patch("backend.services.indicator_selector.IndicatorSelector.select", new=select_mock):
        asyncio.run(
            resolve_indicator_for_fetch(svc, "FRED", intent, dict(intent.parameters or {}))
        )
    kwargs = select_mock.await_args.kwargs
    assert kwargs.get("region") == "California"
    # FRED regional data lives in region-titled series → no coverage probe.
    assert kwargs.get("region_coverage_probe") is None


# ---------------------------------------------------------------------------
# (b) Post-pick guard wired into the StatsCan dispatch chokepoint
# ---------------------------------------------------------------------------

class _GuardStubProv:
    PRODUCT_ID_CACHE: dict = {}

    def __init__(self) -> None:
        self.calls: list = []

    @staticmethod
    def _normalize_metadata_product_id(pid):
        digits = "".join(c for c in str(pid) if c.isdigit())
        return digits[:8] if len(digits) >= 10 else digits

    async def assert_region_supported_or_raise(self, product_id, region):
        self.calls.append((product_id, region))
        raise DataNotAvailableError(
            f"statscan_geography_not_covered: product {product_id} '{region}'"
        )


def _statscan_intent(region: str) -> ParsedIntent:
    return ParsedIntent(
        apiProvider="STATSCAN",
        indicators=["GDP"],
        parameters={},
        clarificationNeeded=False,
        originalQuery=f"{region} GDP",
        subnationalRegion=region,
    )


def test_post_pick_guard_raises_early_for_llm_pick() -> None:
    prov = _GuardStubProv()
    svc = SimpleNamespace(statscan_provider=prov)
    intent = _statscan_intent("Ontario")
    params = {"__statscan_product_id": "36100402", "__decision_source": "llm_pick"}

    with pytest.raises(DataNotAvailableError) as exc:
        asyncio.run(_fetch_from_statscan(svc, intent, params))
    assert "statscan_geography_not_covered" in str(exc.value)
    # Guard ran before any dispatch, with the picked product + requested region.
    assert prov.calls == [("36100402", "Ontario")]


def test_post_pick_guard_skipped_for_non_llm_pick() -> None:
    prov = _GuardStubProv()
    svc = SimpleNamespace(statscan_provider=prov)
    intent = _statscan_intent("Ontario")
    # exact_title picks fail honestly through the normal path, not the retry.
    params = {"__statscan_product_id": "36100402", "__decision_source": "exact_title"}

    # Downstream dispatch will fail (stub lacks fetch methods); we only assert
    # the coverage guard itself was NOT invoked for a non-retriable pick.
    try:
        asyncio.run(_fetch_from_statscan(svc, intent, params))
    except Exception:  # noqa: BLE001 - dispatch fallthrough is expected here
        pass
    assert prov.calls == []


# ---------------------------------------------------------------------------
# build_region_selection_kwargs — the single construction point every select()
# call site (resolution, prefetch clarification, option collection) uses.
# The prefetch sites were region-unaware: an "Ontario ..." query shared a
# selection-cache entry (and a national pick) with the region-less query.
# ---------------------------------------------------------------------------

def test_build_region_selection_kwargs_empty_without_region() -> None:
    from backend.services.indicator_selector import build_region_selection_kwargs

    assert build_region_selection_kwargs(None, "STATSCAN", object()) == {}
    assert build_region_selection_kwargs("   ", "FRED", None) == {}


def test_build_region_selection_kwargs_fred_region_only() -> None:
    from backend.services.indicator_selector import build_region_selection_kwargs

    kw = build_region_selection_kwargs("California", "FRED", None)
    assert kw == {"region": "California"}


def test_build_region_selection_kwargs_statscan_attaches_cache_only_probe() -> None:
    from backend.services.indicator_selector import build_region_selection_kwargs

    class _Prov:
        def __init__(self):
            self.calls = []

        def region_coverage_from_cache(self, codes, region):
            self.calls.append((tuple(codes), region))
            return {c: True for c in codes}

    prov = _Prov()
    kw = build_region_selection_kwargs("Ontario", "StatsCan", prov)
    assert kw["region"] == "Ontario"
    probe = kw["region_coverage_probe"]
    result = asyncio.run(probe(["14100375"]))
    assert result == {"14100375": True}
    assert prov.calls == [(("14100375",), "Ontario")]


def test_collect_statscan_selector_choice_options_threads_region_kwargs() -> None:
    from backend.services import indicator_clarification as ic

    captured = {}

    class _FakeSelector:
        async def select(self, query, provider, country=None, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(needs_user_choice=False, options=[], code=None)

    class _FakeModule:
        IndicatorSelector = _FakeSelector

    real_import = ic.collect_statscan_selector_choice_options

    async def _run():
        import backend.services.indicator_selector as sel_mod
        orig = sel_mod.IndicatorSelector
        sel_mod.IndicatorSelector = _FakeSelector
        try:
            return await real_import(
                "Ontario unemployment rate",
                region_selection_kwargs={"region": "Ontario"},
            )
        finally:
            sel_mod.IndicatorSelector = orig

    asyncio.run(_run())
    assert captured.get("region") == "Ontario"


# ---------------------------------------------------------------------------
# FREQUENCY REQUIREMENT — when the user names a reporting frequency, the
# adjudicator must be told to prefer frequency-matching candidates (observed
# live: annual "Inflation, consumer prices for China" picked over the monthly
# CPI series for a "monthly, last 12 months" query — one useless data point).
# ---------------------------------------------------------------------------

def test_llm_pick_states_frequency_requirement_when_query_names_one(monkeypatch) -> None:
    selector = IndicatorSelector(settings=_selector_settings())
    monkeypatch.setattr(
        selector,
        "_enrich_candidates",
        lambda candidates, provider: _enriched(*candidates),
    )
    sink: dict = {}
    monkeypatch.setattr(
        "backend.services.http_pool.get_http_client",
        lambda: _CapturingClient(sink, pick_line="PICK: 1"),
    )

    asyncio.run(
        selector._llm_pick(  # pylint: disable=protected-access
            "China CPI monthly, last 12 months",
            [("CPALTT01CNM659N", "CPI: Total for China, growth rate same period previous year"),
             ("FPCPITOTLZGCHN", "Inflation, consumer prices for China")],
            "FRED",
        )
    )

    prompt = sink["prompt"]
    assert "FREQUENCY REQUIREMENT" in prompt
    assert "monthly" in prompt


def test_llm_pick_omits_frequency_requirement_without_frequency(monkeypatch) -> None:
    selector = IndicatorSelector(settings=_selector_settings())
    monkeypatch.setattr(
        selector,
        "_enrich_candidates",
        lambda candidates, provider: _enriched(*candidates),
    )
    sink: dict = {}
    monkeypatch.setattr(
        "backend.services.http_pool.get_http_client",
        lambda: _CapturingClient(sink, pick_line="PICK: 1"),
    )

    asyncio.run(
        selector._llm_pick(  # pylint: disable=protected-access
            "US GDP",
            [("GDPC1", "Real Gross Domestic Product"), ("GDPA", "Gross Domestic Product")],
            "FRED",
        )
    )

    assert "FREQUENCY REQUIREMENT" not in sink["prompt"]


def test_llm_pick_frequency_requirement_reads_fuller_constraint_query(monkeypatch) -> None:
    # The resolution layer strips structural qualifiers out of the selector
    # query ("China CPI monthly" -> "CPI ..."), so the frequency words live
    # only in the fuller metadata/constraint text — extraction must see it.
    selector = IndicatorSelector(settings=_selector_settings())
    monkeypatch.setattr(
        selector,
        "_enrich_candidates",
        lambda candidates, provider: _enriched(*candidates),
    )
    sink: dict = {}
    monkeypatch.setattr(
        "backend.services.http_pool.get_http_client",
        lambda: _CapturingClient(sink, pick_line="PICK: 1"),
    )

    asyncio.run(
        selector._llm_pick(  # pylint: disable=protected-access
            "consumer price index china",  # stripped: no frequency words
            [("CPALTT01CNM659N", "CPI: Total for China, monthly YoY"),
             ("FPCPITOTLZGCHN", "Inflation, consumer prices for China")],
            "FRED",
            constraint_query="China CPI monthly, last 12 months",
        )
    )

    prompt = sink["prompt"]
    assert "FREQUENCY REQUIREMENT" in prompt
    assert "monthly" in prompt


def test_selector_query_region_qualified_even_when_indicator_is_code_shaped() -> None:
    # Traced live ("California state GDP"): the parse emitted indicators=['GDP']
    # and GDP is a valid FRED series id, so the "looks like a provider code"
    # branch returned the distilled phrase UNQUALIFIED -> national-only
    # candidates -> hopeless pick. The wrapper now region-qualifies EVERY
    # branch's output for REGION_AS_SERIES_PROVIDERS.
    from types import SimpleNamespace

    from backend.models import ParsedIntent
    from backend.services.indicator_resolution import select_indicator_query_for_resolution

    svc = SimpleNamespace(
        _looks_like_provider_indicator_code=lambda p, t: t.isupper() and t.isalpha() and len(t) <= 6
    )
    intent = ParsedIntent(
        apiProvider="FRED",
        indicators=["GDP"],
        parameters={"country": "US"},
        clarificationNeeded=False,
        subnationalRegion="California",
        originalQuery="California state GDP",
    )
    chosen = select_indicator_query_for_resolution(svc, intent)
    assert "california" in chosen.lower(), chosen

    # StatsCan must stay unqualified (dimension-modeled; landmine).
    intent2 = ParsedIntent(
        apiProvider="StatsCan",
        indicators=["unemployment rate"],
        parameters={"country": "CA"},
        clarificationNeeded=False,
        subnationalRegion="Ontario",
        originalQuery="Ontario unemployment rate",
    )
    chosen2 = select_indicator_query_for_resolution(svc, intent2)
    assert "ontario" not in chosen2.lower(), chosen2


def test_exact_title_shortcut_declined_when_provider_cannot_cover_country() -> None:
    # "Canada unemployment rate by sex" exactly matches Eurostat's TEILM020
    # title; the shortcut provider-locked EUROSTAT for a non-EU country and
    # the fallback chain never reached StatsCan (battery mr8.t1). The builder
    # must decline so normal routing picks a covering provider.
    from unittest.mock import patch

    from backend.services.indicator_resolution import build_exact_indicator_title_intent

    fake_match = {"provider": "EUROSTAT", "code": "TEILM020", "name": "Unemployment rate by sex"}
    with patch(
        "backend.services.indicator_resolution.find_exact_provider_title_match",
        side_effect=lambda q, p: fake_match if p == "EUROSTAT" else None,
    ):
        declined = build_exact_indicator_title_intent(
            "Canada unemployment rate by sex",
            countries=["CA"],
            all_providers=["EUROSTAT", "STATSCAN", "FRED"],
        )
        assert declined is None, "Eurostat cannot cover CA — shortcut must decline"

        accepted = build_exact_indicator_title_intent(
            "Germany unemployment rate by sex",
            countries=["DE"],
            all_providers=["EUROSTAT", "STATSCAN", "FRED"],
        )
        assert accepted is not None and accepted.apiProvider == "EUROSTAT"


def test_llm_pick_marks_frequency_matches_per_candidate(monkeypatch) -> None:
    # Prose rules lose to title similarity; per-candidate markers (the
    # [covers X] pattern) are what the adjudicator reliably follows.
    selector = IndicatorSelector(settings=_selector_settings())

    def _enriched_freq(*codes_names):
        rows = _enriched(*codes_names)
        rows[0]["frequency"] = "Monthly"
        rows[1]["frequency"] = "Annual"
        return rows

    monkeypatch.setattr(
        selector, "_enrich_candidates",
        lambda candidates, provider: _enriched_freq(*candidates),
    )
    sink: dict = {}
    monkeypatch.setattr(
        "backend.services.http_pool.get_http_client",
        lambda: _CapturingClient(sink, pick_line="PICK: 1"),
    )

    asyncio.run(
        selector._llm_pick(  # pylint: disable=protected-access
            "CPI inflation",
            [("INDCPIALLMINMEI", "CPI Total for India"),
             ("FPCPITOTLZGIND", "Inflation, consumer prices for India")],
            "FRED",
            constraint_query="India CPI by month, past year",
        )
    )
    prompt = sink["prompt"]
    assert "MATCHES requested frequency" in prompt
    assert "does NOT match requested frequency" in prompt


def test_selector_query_country_qualified_for_non_home_fred() -> None:
    from types import SimpleNamespace

    from backend.models import ParsedIntent
    from backend.services.provider_strategy import country_qualified_indicator_text

    intent = ParsedIntent(
        apiProvider="FRED", indicators=["CPI"], parameters={"country": "India"},
        clarificationNeeded=False, originalQuery="India CPI by month",
    )
    assert country_qualified_indicator_text(intent, "FRED", "CPI inflation") == "CPI inflation India"
    # Home country untouched; non-region-as-series providers untouched.
    intent.parameters = {"country": "US"}
    assert country_qualified_indicator_text(intent, "FRED", "CPI inflation") == "CPI inflation"
    intent.parameters = {"country": "India"}
    assert country_qualified_indicator_text(intent, "WORLDBANK", "CPI inflation") == "CPI inflation"
