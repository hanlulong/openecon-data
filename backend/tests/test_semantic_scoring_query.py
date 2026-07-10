"""Guard tests: non-English queries score with the canonical-English key.

The lexical relevance scorer/cue extractor operate on English series text, so
scoring a raw non-English query zero-scores every series and the uncertainty
gate discards correct data. semantic_scoring_query substitutes the parse LLM's
canonical-English indicators (plus geography) for non-English queries only.
"""

from backend.models import ParsedIntent
from backend.services.indicator_clarification import semantic_scoring_query


def _intent(**kwargs) -> ParsedIntent:
    base = dict(
        apiProvider="STATSCAN",
        indicators=["unemployment rate"],
        parameters={"country": "Canada"},
        clarificationNeeded=False,
        originalQuery="加拿大失业率",
    )
    base.update(kwargs)
    return ParsedIntent(**base)


def test_non_english_uses_canonical_terms_plus_country():
    intent = _intent(language="zh")
    assert semantic_scoring_query("加拿大失业率", intent) == "unemployment rate Canada"


def test_english_query_untouched():
    intent = _intent(language="en", originalQuery="Canada unemployment rate")
    assert semantic_scoring_query("Canada unemployment rate", intent) == "Canada unemployment rate"


def test_missing_language_untouched():
    intent = _intent(language=None)
    assert semantic_scoring_query("加拿大失业率", intent) == "加拿大失业率"


def test_non_english_without_indicators_falls_back_to_raw():
    intent = _intent(language="zh", indicators=[" "])
    assert semantic_scoring_query("加拿大失业率", intent) == "加拿大失业率"


def test_country_not_duplicated_when_already_in_terms():
    intent = _intent(language="zh", indicators=["Canada unemployment rate"])
    assert semantic_scoring_query("x", intent) == "Canada unemployment rate"


def test_no_intent_untouched():
    assert semantic_scoring_query("加拿大失业率", None) == "加拿大失业率"
