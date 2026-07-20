"""Sole-survivor serve: a one-option 'menu' is not a choice — serve it.

User principle (2026-07-19): clarification is for choosing among materially
distinct options; never ask when there is no real choice, and never let a
runner-up win because a better candidate transiently flaked ("guess and get
wrong is not good"). These tests pin the contract of
filter_viable_indicator_choice_options (+ its validation helper) and the
consumer's sole-survivor branch:

  * exactly ONE fetch-validated-CONFIDENT option, all competitors dropped
    SUBSTANTIVELY  -> apply + serve (builder returns None, intent mutated);
  * >=2 viable                       -> menu unchanged (genuine choice kept);
  * a transient-dropped competitor gets ONE retry before any sole-survivor
    decision; recovering -> menu, failing again -> serve;
  * a national series never survives validation for a subnational query
    (mirrors the maybe_recover hard predicate — the OECD-over-Ontario class).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from backend.models import Metadata, NormalizedData, ParsedIntent
from backend.services.indicator_clarification import (
    _validate_indicator_choice_option,
    filter_viable_indicator_choice_options,
)


def _series(indicator: str = "thing", source: str = "FRED") -> NormalizedData:
    return NormalizedData(
        metadata=Metadata(
            source=source, indicator=indicator, country="US",
            frequency="monthly", unit="idx",
        ),
        data=[{"date": "2026-01-01", "value": 1.0}],
    )


class _FakeQS:
    """Minimal QueryService stand-in for the validation loop's seams."""

    def __init__(
        self,
        fetch_results: Dict[str, Any],
        gate_uncertain: Optional[set] = None,
        region_hits: Optional[set] = None,
    ) -> None:
        # fetch_results[code] is: list -> data; Exception -> raise; a LIST of
        # outcomes -> consumed one per call (models per-attempt flakiness).
        self._fetch_results = fetch_results
        self._gate_uncertain = gate_uncertain or set()
        self._region_hits = region_hits if region_hits is not None else None
        self.fetch_calls: List[str] = []

    async def _fetch_data(self, intent: ParsedIntent):
        code = str((intent.parameters or {}).get("indicator") or "")
        self.fetch_calls.append(code)
        outcome = self._fetch_results.get(code)
        if isinstance(outcome, list) and outcome and isinstance(
            outcome[0], (list, Exception)
        ):
            outcome = outcome.pop(0) if len(outcome) > 1 else outcome[0]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def _rerank_data_by_query_relevance(self, query, data):
        return data

    def _is_ranking_query(self, query):
        return False

    def _apply_ranking_projection(self, query, data):
        return data

    def _has_implausible_top_series(self, query, data):
        return False

    def _served_data_references_region(self, data, region):
        if self._region_hits is None:
            return True
        code = str(getattr(data[0].metadata, "indicator", "") or "")
        return code in self._region_hits

    def _needs_indicator_clarification(self, query, data, intent, caller=""):
        code = str((intent.parameters or {}).get("indicator") or "")
        return code in self._gate_uncertain

    async def _filter_viable_indicator_choice_options(self, **kwargs):
        return await filter_viable_indicator_choice_options(self, **kwargs)


def _intent(region: Optional[str] = None) -> ParsedIntent:
    return ParsedIntent(
        apiProvider="FRED",
        indicators=["thing"],
        parameters={},
        clarificationNeeded=False,
        subnationalRegion=region,
    )


OPT_A = "[FRED] Thing A (AAA)"
OPT_B = "[WorldBank] Thing B (BBB)"


@pytest.mark.asyncio
async def test_sole_survivor_with_substantive_drops_is_viable_and_collected():
    qs = _FakeQS(
        fetch_results={"AAA": [_series("A")], "BBB": [_series("B")]},
        gate_uncertain={"BBB"},  # substantive drop
    )
    collector: Dict[str, Any] = {}
    viable = await filter_viable_indicator_choice_options(
        qs, "q", _intent(), [OPT_A, OPT_B], collector=collector
    )
    assert viable == [OPT_A]
    assert collector["validated"][OPT_A]
    assert collector["dropped"][OPT_B] == "substantive:gate_uncertain"


@pytest.mark.asyncio
async def test_two_viable_options_keep_the_menu_shape():
    qs = _FakeQS(fetch_results={"AAA": [_series("A")], "BBB": [_series("B")]})
    collector: Dict[str, Any] = {}
    viable = await filter_viable_indicator_choice_options(
        qs, "q", _intent(), [OPT_A, OPT_B], collector=collector
    )
    assert sorted(viable) == sorted([OPT_A, OPT_B])
    assert len(collector["validated"]) == 2


@pytest.mark.asyncio
async def test_transient_competitor_recovering_on_retry_restores_the_menu():
    flaky = [RuntimeError("boom"), [_series("B")]]  # attempt1 raises, retry OK
    qs = _FakeQS(fetch_results={"AAA": [_series("A")], "BBB": flaky})
    collector: Dict[str, Any] = {}
    viable = await filter_viable_indicator_choice_options(
        qs, "q", _intent(), [OPT_A, OPT_B], collector=collector
    )
    assert sorted(viable) == sorted([OPT_A, OPT_B]), (
        "a transiently-flaked competitor that recovers must restore the menu"
    )


@pytest.mark.asyncio
async def test_transient_competitor_failing_retry_leaves_sole_survivor():
    qs = _FakeQS(
        fetch_results={"AAA": [_series("A")], "BBB": RuntimeError("down")},
    )
    collector: Dict[str, Any] = {}
    viable = await filter_viable_indicator_choice_options(
        qs, "q", _intent(), [OPT_A, OPT_B], collector=collector
    )
    assert viable == [OPT_A]
    assert "(retried)" in collector["dropped"][OPT_B]
    # BBB got exactly one extra attempt
    assert qs.fetch_calls.count("BBB") == 2


@pytest.mark.asyncio
async def test_national_series_never_survives_subnational_validation():
    qs = _FakeQS(
        fetch_results={"AAA": [_series("Ontario thing")], "BBB": [_series("national thing")]},
        region_hits={"Ontario thing"},
    )
    data, reason = await _validate_indicator_choice_option(
        qs, "q", _intent(region="Ontario"), OPT_B
    )
    assert data is None and reason == "substantive:region_not_covered"
    data, reason = await _validate_indicator_choice_option(
        qs, "q", _intent(region="Ontario"), OPT_A
    )
    assert data is not None and reason == "viable"


@pytest.mark.asyncio
async def test_collector_is_optional_and_return_shape_unchanged():
    qs = _FakeQS(fetch_results={"AAA": [_series("A")], "BBB": [_series("B")]})
    viable = await filter_viable_indicator_choice_options(
        qs, "q", _intent(), [OPT_A, OPT_B]
    )
    assert isinstance(viable, list) and all(isinstance(o, str) for o in viable)
