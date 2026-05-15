from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services.indicator_selector import (
    LLM_SELECTION_PROMPT,
    IndicatorSelector,
    SelectionResult,
)
from backend.tests.fixtures.indicator_selector_llm_fixtures import LLM_SELECTOR_FIXTURES


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


@pytest.mark.parametrize(
    ("fixture_name", "expected_source", "expected_code"),
    [
        ("pick", "llm_pick", "CODE2"),
        ("ask", "user_choice", None),
        ("reject_search", "llm_reject", None),
        ("undecided", None, None),
    ],
)
def test_deterministic_llm_selector_fixtures(
    fixture_name: str,
    expected_source: str | None,
    expected_code: str | None,
) -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())

    result = selector._parse_llm_response(  # pylint: disable=protected-access
        LLM_SELECTOR_FIXTURES[fixture_name],
        [("CODE1", "First"), ("CODE2", "Second"), ("CODE3", "Third")],
        "STATSCAN",
        "requested measure",
    )

    if expected_source is None:
        assert result is None
        return

    assert result is not None
    assert result.source == expected_source
    assert result.code == expected_code


def test_score_ambiguity_requires_ordered_score_evidence() -> None:
    assert IndicatorSelector._scores_are_ambiguous([0.88, 0.87, 0.86])
    assert not IndicatorSelector._scores_are_ambiguous([0.55, 0.88, 0.87])
    assert not IndicatorSelector._scores_are_ambiguous([0.88, 0.82, 0.80])



def test_get_candidates_uses_catalog_provider_alias_for_statscan(monkeypatch) -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())
    seen: list[tuple[str, str]] = []

    class _FakeEmbeddingRetrieval:
        def search(self, query: str, provider: str, top_k: int):  # noqa: ANN001
            seen.append(("embedding", provider))
            return []

    monkeypatch.setattr(
        "backend.services.embedding_retrieval.get_embedding_retrieval",
        lambda: _FakeEmbeddingRetrieval(),
    )

    def fake_fts(query: str, provider: str, top_k: int):  # noqa: ANN001
        seen.append(("fts", provider))
        return []

    monkeypatch.setattr(selector, "_get_candidates_fts5", fake_fts)

    candidates, scores = selector._get_candidates_with_scores(  # pylint: disable=protected-access
        "employment",
        "STATSCAN",
    )

    assert candidates == []
    assert scores == []
    assert seen == [("embedding", "StatsCan"), ("fts", "StatsCan")]


def test_hybrid_candidate_merge_keeps_embedding_backed_matches_ahead_of_fts_recall(monkeypatch) -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())

    class _FakeEmbeddingRetrieval:
        def search(self, query: str, provider: str, top_k: int):  # noqa: ANN001, ARG002
            assert query == "unemployment rate"
            assert provider == "FRED"
            return [
                {"code": "GENERIC_SA", "name": "Unemployment Rate", "score": 0.91},
                {"code": "GENERIC_NSA", "name": "Unemployment Rate", "score": 0.90},
                {"code": "LEVEL", "name": "Unemployment Level", "score": 0.76},
            ]

    monkeypatch.setattr(
        "backend.services.embedding_retrieval.get_embedding_retrieval",
        lambda: _FakeEmbeddingRetrieval(),
    )
    monkeypatch.setattr(
        selector,
        "_get_candidates_fts5",
        lambda query, provider, top_k: [  # noqa: ARG005
            ("STATE_CA", "Unemployment Rate in California"),
            ("STATE_TX", "Unemployment Rate in Texas"),
            ("GENERIC_SA", "Unemployment Rate"),
        ],
    )

    candidates, scores = selector._get_candidates_with_scores(  # pylint: disable=protected-access
        "unemployment rate",
        "FRED",
    )

    assert [code for code, _name in candidates[:2]] == ["GENERIC_SA", "GENERIC_NSA"]
    assert {code for code, _name in candidates[:5]} >= {"STATE_CA", "STATE_TX"}
    assert scores[0] > scores[2]
    assert not IndicatorSelector._scores_are_ambiguous(scores[:3])  # pylint: disable=protected-access


def test_hybrid_candidate_merge_keeps_fts_only_recall_when_embeddings_miss(monkeypatch) -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())

    class _FakeEmbeddingRetrieval:
        def search(self, query: str, provider: str, top_k: int):  # noqa: ANN001, ARG002
            return []

    monkeypatch.setattr(
        "backend.services.embedding_retrieval.get_embedding_retrieval",
        lambda: _FakeEmbeddingRetrieval(),
    )
    monkeypatch.setattr(
        selector,
        "_get_candidates_fts5",
        lambda query, provider, top_k: [  # noqa: ARG005
            ("LEXICAL_CODE", "Lexical acronym match"),
            ("SECOND_CODE", "Second lexical match"),
        ],
    )

    candidates, scores = selector._get_candidates_with_scores(  # pylint: disable=protected-access
        "lexical acronym",
        "FRED",
    )

    assert candidates == [
        ("LEXICAL_CODE", "Lexical acronym match"),
        ("SECOND_CODE", "Second lexical match"),
    ]
    assert scores[0] > scores[1]


def test_imf_candidate_order_prefers_public_datamapper_surface(monkeypatch) -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())

    class _Lookup:
        def get(self, provider: str, code: str):
            assert provider == "IMF"
            return {
                "GDP": {"category": "CF"},
                "BFD_GDP": {"category": "CF"},
                "NGDPD": {"category": "WEO"},
                "TTT": {"category": "AFRREO"},
            }.get(code)

    monkeypatch.setattr(
        "backend.services.indicator_database.get_indicator_lookup",
        lambda: _Lookup(),
    )

    candidates, scores = selector._prioritize_candidates_by_provider_surface(  # pylint: disable=protected-access
        [
            ("GDP", "Nominal GDP"),
            ("BFD_GDP", "Net foreign direct investment (% of GDP)"),
            ("NGDPD", "GDP, current prices"),
            ("TTT", "Terms of trade"),
        ],
        [0.55, 0.55, 0.69, 0.60],
        "IMF",
    )

    assert [code for code, _name in candidates] == ["NGDPD", "TTT", "BFD_GDP", "GDP"]
    assert scores == [0.69, 0.60, 0.55, 0.55]


def test_query_metadata_ordering_promotes_requested_frequency_and_unit(monkeypatch) -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())

    def fake_enrich(candidates, provider):  # noqa: ANN001, ARG001
        metadata = {
            "TOTBKCR": {
                "frequency": "Weekly, Ending Wednesday",
                "unit": "Billions of U.S. Dollars",
                "end_date": "",
                "category": "",
                "description": "",
                "keywords": "",
                "discontinued": False,
            },
            "H8B1001NCBCMG": {
                "frequency": "Monthly",
                "unit": "Percent Change at Annual Rate",
                "end_date": "",
                "category": "",
                "description": "",
                "keywords": "",
                "discontinued": False,
            },
            "LOANINV": {
                "frequency": "Monthly",
                "unit": "Billions of U.S. Dollars",
                "end_date": "",
                "category": "",
                "description": "",
                "keywords": "",
                "discontinued": False,
            },
        }
        return [
            {"code": code, "name": name, **metadata[code]}
            for code, name in candidates
        ]

    monkeypatch.setattr(selector, "_enrich_candidates", fake_enrich)

    candidates, scores = selector._prioritize_candidates_by_query_metadata(  # pylint: disable=protected-access
        "US Bank Credit All Commercial Banks in Billions of U.S. Dollars (Monthly) from FRED",
        [
            ("TOTBKCR", "Bank Credit, All Commercial Banks"),
            ("H8B1001NCBCMG", "Bank Credit, All Commercial Banks"),
            ("LOANINV", "Bank Credit, All Commercial Banks"),
        ],
        [0.90, 0.88, 0.86],
        "FRED",
    )

    assert candidates[0][0] == "LOANINV"
    assert scores[0] == 0.86


@pytest.mark.asyncio
async def test_select_retries_when_llm_pick_conflicts_with_requested_frequency(monkeypatch) -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())
    all_candidates = [
        ("TOTBKCR", "Bank Credit, All Commercial Banks"),
        ("LOANINV", "Bank Credit, All Commercial Banks"),
    ]

    def fake_candidates(query: str, provider: str):  # noqa: ARG001
        return all_candidates, [0.90, 0.86]

    def fake_enrich(candidates, provider):  # noqa: ANN001, ARG001
        metadata = {
            "TOTBKCR": {"frequency": "Weekly", "unit": "Billions of U.S. Dollars"},
            "LOANINV": {"frequency": "Monthly", "unit": "Billions of U.S. Dollars"},
        }
        return [
            {
                "code": code,
                "name": name,
                "end_date": "",
                "category": "",
                "description": "",
                "keywords": "",
                "discontinued": False,
                **metadata[code],
            }
            for code, name in candidates
        ]

    seen_candidate_sets: list[list[str]] = []

    async def fake_llm_pick(query, candidates, provider, prefer_ask=False):  # noqa: ANN001, ARG001
        seen_candidate_sets.append([code for code, _name in candidates])
        if seen_candidate_sets[-1] == ["TOTBKCR", "LOANINV"]:
            return SelectionResult(code="TOTBKCR", name="Bank Credit, All Commercial Banks", source="llm_pick")
        return SelectionResult(code="LOANINV", name="Bank Credit, All Commercial Banks", source="llm_pick")

    monkeypatch.setattr(selector, "_get_candidates_with_scores", fake_candidates)
    monkeypatch.setattr(selector, "_enrich_candidates", fake_enrich)
    monkeypatch.setattr(selector, "_llm_pick", fake_llm_pick)

    result = await selector.select(
        "US Bank Credit All Commercial Banks in Billions of U.S. Dollars (Monthly) from FRED",
        "FRED",
    )

    assert result.code == "LOANINV"
    assert seen_candidate_sets == [["TOTBKCR", "LOANINV"], ["LOANINV"]]


@pytest.mark.asyncio
async def test_select_uses_metadata_query_for_lost_frequency_constraints(monkeypatch) -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())
    all_candidates = [
        ("BOGZ1FL155035066A", "Households; Owners' Equity in Real Estate as a Percentage of Household Real Estate, Level"),
        ("HOEREPHRE", "Households; Owners' Equity in Real Estate as a Percentage of Household Real Estate, Level"),
    ]

    def fake_candidates(query: str, provider: str):  # noqa: ARG001
        return all_candidates, [0.90, 0.88]

    def fake_enrich(candidates, provider):  # noqa: ANN001, ARG001
        metadata = {
            "BOGZ1FL155035066A": {"frequency": "Annual", "unit": "Percent"},
            "HOEREPHRE": {"frequency": "Quarterly, End of Period", "unit": "Percent"},
        }
        return [
            {
                "code": code,
                "name": name,
                "end_date": "",
                "category": "",
                "description": "",
                "keywords": "",
                "discontinued": False,
                **metadata[code],
            }
            for code, name in candidates
        ]

    seen_candidate_sets: list[list[str]] = []

    async def fake_llm_pick(query, candidates, provider, prefer_ask=False):  # noqa: ANN001, ARG001
        seen_candidate_sets.append([code for code, _name in candidates])
        if seen_candidate_sets[-1] == ["BOGZ1FL155035066A", "HOEREPHRE"]:
            return SelectionResult(
                code="BOGZ1FL155035066A",
                name="Households; Owners' Equity in Real Estate as a Percentage of Household Real Estate, Level",
                source="llm_pick",
            )
        return SelectionResult(
            code="HOEREPHRE",
            name="Households; Owners' Equity in Real Estate as a Percentage of Household Real Estate, Level",
            source="llm_pick",
        )

    monkeypatch.setattr(selector, "_get_candidates_with_scores", fake_candidates)
    monkeypatch.setattr(selector, "_enrich_candidates", fake_enrich)
    monkeypatch.setattr(selector, "_llm_pick", fake_llm_pick)

    result = await selector.select(
        "Owners' Equity in Real Estate as a Percentage of Household Real Estate",
        "FRED",
        metadata_query=(
            "US Level Households; Owners' Equity in Real Estate as a Percentage "
            "of Household Real Estate (Quarterly, End of Period) from FRED"
        ),
    )

    assert result.code == "HOEREPHRE"
    assert seen_candidate_sets == [["BOGZ1FL155035066A", "HOEREPHRE"], ["HOEREPHRE"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query,provider,wrong_code,wrong_title,correct_code,correct_title",
    [
        (
            "number of households in Canada",
            "STATSCAN",
            "42100012",
            "Number of children in Canada",
            "17100159",
            "Estimates of the number of private households by size on July 1st",
        ),
        (
            "number of children in Canada",
            "STATSCAN",
            "17100159",
            "Estimates of the number of private households by size on July 1st",
            "42100012",
            "Number of children in Canada",
        ),
        (
            "total unemployment rate in Canada",
            "STATSCAN",
            "YOUTH_UNEMP",
            "Youth unemployment rate, 15 to 24 years",
            "TOTAL_UNEMP",
            "Unemployment rate, total labour force",
        ),
        (
            "youth unemployment rate in Canada",
            "STATSCAN",
            "TOTAL_UNEMP",
            "Unemployment rate, total labour force",
            "YOUTH_UNEMP",
            "Youth unemployment rate, 15 to 24 years",
        ),
        (
            "GDP growth rate in Germany",
            "WORLDBANK",
            "NY.GDP.MKTP.CD",
            "GDP (current US$)",
            "NY.GDP.MKTP.KD.ZG",
            "GDP growth (annual %)",
        ),
        (
            "GDP per capita in Japan",
            "WORLDBANK",
            "NY.GDP.MKTP.CD",
            "GDP (current US$)",
            "NY.GDP.PCAP.CD",
            "GDP per capita (current US$)",
        ),
        (
            "inflation rate in France",
            "WORLDBANK",
            "FP.CPI.TOTL",
            "Consumer price index (2010 = 100)",
            "FP.CPI.TOTL.ZG",
            "Inflation, consumer prices (annual %)",
        ),
        (
            "imports as percent of GDP for China",
            "WORLDBANK",
            "NE.EXP.GNFS.ZS",
            "Exports of goods and services (% of GDP)",
            "NE.IMP.GNFS.ZS",
            "Imports of goods and services (% of GDP)",
        ),
        (
            "current account balance Germany",
            "IMF",
            "TRADE_BAL",
            "Trade balance of goods and services",
            "BCA",
            "Current account balance",
        ),
        (
            "bitcoin price history",
            "COINGECKO",
            "GDP",
            "Gross Domestic Product",
            "bitcoin",
            "Bitcoin price",
        ),
    ],
)
async def test_selector_semantic_confusion_oracles_choose_correct_candidate_without_shortcut_rules(
    monkeypatch,
    query: str,
    provider: str,
    wrong_code: str,
    wrong_title: str,
    correct_code: str,
    correct_title: str,
) -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())

    def fake_candidates(candidate_query: str, candidate_provider: str):
        assert candidate_query == query
        assert candidate_provider == provider
        return [
            (wrong_code, wrong_title),
            (correct_code, correct_title),
        ], [0.93, 0.79]

    async def fake_llm_pick(candidate_query, candidates, candidate_provider, prefer_ask=False):  # noqa: ANN001, ARG001
        titles = {title: code for code, title in candidates}
        assert wrong_title in titles
        assert correct_title in titles
        return SelectionResult(
            code=titles[correct_title],
            name=correct_title,
            source="llm_pick",
        )

    monkeypatch.setattr(selector, "_get_candidates_with_scores", fake_candidates)
    monkeypatch.setattr(selector, "_llm_pick", fake_llm_pick)

    result = await selector.select(query, provider)

    assert result.code == correct_code
    assert result.name == correct_title
    assert result.source == "llm_pick"


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


@pytest.mark.asyncio
async def test_select_refuses_top_candidate_when_llm_is_undecided(monkeypatch) -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())

    def fake_candidates(query: str, provider: str):  # noqa: ARG001
        return [
            ("42100012", "Number of children in Canada"),
            ("36100126", "Property income of households, Canada"),
        ], [0.76, 0.70]

    async def undecided_llm(*_args, **_kwargs):
        return None

    monkeypatch.setattr(selector, "_get_candidates_with_scores", fake_candidates)
    monkeypatch.setattr(selector, "_llm_pick", undecided_llm)

    result = await selector.select("number of households", "STATSCAN")

    assert result.code is None
    assert result.source == "no_decision"


@pytest.mark.asyncio
async def test_select_single_candidate_still_requires_llm_authority(monkeypatch) -> None:
    selector = IndicatorSelector(settings=SimpleNamespace())

    def fake_candidates(query: str, provider: str):  # noqa: ARG001
        return [("17100159", "Estimates of the number of private households by size")], [0.91]

    async def undecided_llm(*_args, **_kwargs):
        return None

    monkeypatch.setattr(selector, "_get_candidates_with_scores", fake_candidates)
    monkeypatch.setattr(selector, "_llm_pick", undecided_llm)

    result = await selector.select("number of households", "STATSCAN")

    assert result.code is None
    assert result.source == "no_decision"
