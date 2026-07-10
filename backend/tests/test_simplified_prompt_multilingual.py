"""Guard tests for the parse-prompt additions (Proposals A.1, B, C).

The parser must instruct the LLM to (A.1) emit English canonical indicator
names, (B) extract a subnationalRegion while keeping the parent country, and
(C) label the query's language — and expose those keys in the output schema.
"""
from __future__ import annotations

from backend.services.simplified_prompt import SimplifiedPrompt


def _prompt() -> str:
    return SimplifiedPrompt.generate()


def test_english_canonical_indicators_rule_present():
    p = _prompt()
    assert "indicators array MUST use English canonical metric names" in p
    assert "English-language search key" in p
    # The non-English -> English examples from the design.
    assert "失业率" in p and "unemployment rate" in p


def test_subnational_region_rule_and_examples():
    p = _prompt()
    assert "subnationalRegion" in p
    # Country stays the parent; region is separate — incl. a Chinese example.
    assert "北京GDP" in p
    assert "Ontario" in p


def test_language_rule_present():
    p = _prompt()
    assert "ISO 639-1" in p
    assert '"zh"' in p


def test_output_schema_exposes_new_keys():
    p = _prompt()
    assert '"subnationalRegion":' in p
    assert '"language":' in p


def test_region_never_treated_as_country():
    p = _prompt()
    assert "a region is never a country" in p
