from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.indicator_selector import (
    LLM_SELECTION_PROMPT,
    IndicatorSelector,
    SelectionResult,
)


def test_selector_prompt_prefers_direct_counts_over_breakdowns() -> None:
    prompt = LLM_SELECTION_PROMPT.lower()

    assert "direct count/number/total requests" in prompt
    assert "measures the" in prompt
    assert "requested entity count directly" in prompt
    assert "distribution" in prompt
    assert "ratio" in prompt
    assert "unless the user explicitly asks" in prompt


def test_parse_llm_pick_ignores_explanatory_numbers() -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())

    result = selector._parse_llm_response(  # pylint: disable=protected-access
        "PICK: 2\nReason: option 2 has data through 2021 and provider code X123.",
        [("CODE1", "Wrong measure"), ("CODE2", "Right measure")],
        "STATSCAN",
        "requested measure",
    )

    assert result is not None
    assert result.code == "CODE2"
    assert result.source == "llm_pick"


def test_parse_llm_ask_uses_only_control_line_numbers() -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())

    result = selector._parse_llm_response(  # pylint: disable=protected-access
        "ASK: 1, 3\nReason: candidate 2 is a 2021 subset, not the total.",
        [("CODE1", "First"), ("CODE2", "Second"), ("CODE3", "Third")],
        "STATSCAN",
        "requested measure",
    )

    assert result is not None
    assert result.needs_user_choice
    assert result.options == [
        {"code": "CODE1", "name": "First"},
        {"code": "CODE3", "name": "Third"},
    ]


def test_parse_llm_reject_extracts_retry_search_without_pick_confusion() -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())

    result = selector._parse_llm_response(  # pylint: disable=protected-access
        "REJECT: I cannot pick any provided candidate.\nSEARCH: direct total count measure",
        [("CODE1", "Wrong measure")],
        "STATSCAN",
        "requested measure",
    )

    assert result is not None
    assert result.rejected_candidates
    assert "cannot pick" in result.rejection_reason
    assert result.retry_query == "direct total count measure"


def test_parse_llm_response_does_not_select_from_arbitrary_digits() -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())

    result = selector._parse_llm_response(  # pylint: disable=protected-access
        "The series has observations in 2021, but I am not sure.",
        [("CODE1", "First"), ("CODE2", "Second")],
        "STATSCAN",
        "requested measure",
    )

    assert result is None


def test_parse_llm_response_accepts_explicit_choice_fallback() -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())

    result = selector._parse_llm_response(  # pylint: disable=protected-access
        "I would choose #2.",
        [("CODE1", "First"), ("CODE2", "Second")],
        "STATSCAN",
        "requested measure",
    )

    assert result is not None
    assert result.code == "CODE2"


def test_score_ambiguity_requires_ordered_score_evidence() -> None:
    assert IndicatorSelector._scores_are_ambiguous([0.88, 0.87, 0.86])
    assert not IndicatorSelector._scores_are_ambiguous([0.55, 0.88, 0.87])
    assert not IndicatorSelector._scores_are_ambiguous([0.88, 0.82, 0.80])


@pytest.mark.asyncio
async def test_select_researches_with_llm_retry_query_when_candidates_are_rejected(monkeypatch) -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())
    first_candidates = [
        ("42100012", "Number of children in Canada"),
        ("36100126", "Property income of households, Canada"),
        ("98100138", "Household type including multigenerational households"),
    ]
    second_candidates = [
        ("17100159", "Estimates of the number of private households by size on July 1st"),
        ("17100075", "Historical statistics, number of persons per household and family"),
    ]
    seen_queries: list[str] = []

    def fake_candidates(query: str, provider: str):
        seen_queries.append(query)
        if query == "number of private households total households household size":
            return second_candidates, [0.81, 0.73]
        return first_candidates, [0.76, 0.70, 0.64]

    rejected = SimpleNamespace(
        code=None,
        name=None,
        source="llm_reject",
        needs_user_choice=False,
        rejection_reason="Candidates describe children, income, or household type, not a total household count.",
        retry_query="number of private households total households household size",
    )

    async def fake_llm_pick(query, candidates, provider, prefer_ask=False):  # noqa: ANN001
        if query == "number of households":
            return rejected
        return SelectionResult(
            code="17100159",
            name="Estimates of the number of private households by size on July 1st",
            source="llm_pick",
        )

    monkeypatch.setattr(selector, "_get_candidates_with_scores", fake_candidates)
    monkeypatch.setattr(selector, "_llm_pick", fake_llm_pick)

    result = await selector.select("number of households", "STATSCAN")

    assert result.code == "17100159"
    assert seen_queries == [
        "number of households",
        "number of private households total households household size",
    ]
