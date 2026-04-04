"""Dry-run LLM delta extraction against 10 hard multi-round scenarios.

Tests the FollowUpDelta output WITHOUT any code changes to the backend.
Uses the existing Instructor client and vLLM to validate the approach.
"""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from openai import AsyncOpenAI
import instructor
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any


# FollowUpDelta model (mirrors conversation_state_v2.py)
class FollowUpDelta(BaseModel):
    changed_indicator: Optional[str] = None
    changed_country: Optional[str] = None
    changed_countries: Optional[List[str]] = None
    added_countries: Optional[List[str]] = None
    removed_countries: Optional[List[str]] = None
    changed_provider: Optional[str] = None
    changed_start_date: Optional[str] = None
    changed_end_date: Optional[str] = None
    added_dimensions: Optional[Dict[str, str]] = None
    removed_dimensions: Optional[List[str]] = None
    changed_chart_type: Optional[str] = None
    changed_trade_flow: Optional[str] = None
    changed_trade_reporter: Optional[str] = None
    changed_trade_partner: Optional[str] = None
    changed_trade_commodity: Optional[str] = None
    is_new_query: bool = False
    is_dimension_modifier_change: bool = False
    delta_type: Optional[str] = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


SYSTEM_PROMPT = """You are a delta extractor for an economic data query system.

Given the user's CURRENT conversation state and their NEW follow-up message,
determine ONLY what changed. Output a FollowUpDelta JSON where you populate
ONLY the fields that the user wants to modify. Leave everything else null.

RULES:
1. Only populate fields the user explicitly wants to change.
2. For countries:
   - changed_country: user wants to REPLACE the current country with a new one
   - changed_countries: user wants to REPLACE with multiple countries
   - added_countries: user says "add", "also", "compare with", "include" → ADDITIVE
   - removed_countries: user says "remove", "exclude", "without" → SUBTRACTIVE
3. For dimensions (sub-categories like sex, age group, province, product type):
   - added_dimensions: dict of {dimension_name: value} for filtering
   - is_dimension_modifier_change: true when changing dimensions
   - Common dimensions: sex (male/female), age_group (youth/15-24/25-54/55+/seniors), geography (province names), products (food/shelter/energy/transportation/clothing)
4. If the query is completely unrelated to prior context, set is_new_query=true.
5. Set delta_type to one of: country_change, additive_country, time_change, indicator_switch, provider_change, dimension_change, chart_change, new_query, compound_change
6. Set confidence (0-1) based on how certain you are about the interpretation.
7. For time changes: use ISO format dates (YYYY-MM-DD). "last N years" → start_date = current year minus N.
8. "Compare X and Y" or "Compare with Y" when X is already shown → added_countries (additive).

CURRENT STATE:
{state_text}

User's follow-up: "{query}"

Output ONLY the changed fields as JSON."""


def format_state(state: dict) -> str:
    lines = []
    for k, v in state.items():
        if v is not None:
            lines.append(f"  {k}: {v}")
    return "\n".join(lines) if lines else "  (empty)"


# Test scenarios: (state, query, expected_delta_description)
TESTS = [
    # Chain 1 R2: "break it down by sex" after employment rate
    {
        "name": "1. Employment → 'break it down by sex'",
        "state": {"indicator": "employment rate", "country": "Canada", "provider": "STATSCAN"},
        "query": "Can you break it down by sex?",
        "expect": "added_dimensions={'sex': 'male/female'} or similar dimension change",
    },
    # Chain 1 R3: "females aged 25 to 54"
    {
        "name": "2. Employment → 'females aged 25 to 54'",
        "state": {"indicator": "employment rate", "country": "Canada", "provider": "STATSCAN",
                  "dimensions": {"sex": "both"}},
        "query": "Now show me just females aged 25 to 54",
        "expect": "added_dimensions with sex=female AND age_group=25-54",
    },
    # Chain 2 R3: "Compare Ontario and Quebec"
    {
        "name": "3. Unemployment Ontario → 'Compare Ontario and Quebec'",
        "state": {"indicator": "UNEMPLOYMENT_RATE", "country": "Canada", "provider": "STATSCAN",
                  "dimensions": {"geography": "Ontario"}},
        "query": "Compare Ontario and Quebec",
        "expect": "additive: added_dimensions or added geography for Quebec (not replacement)",
    },
    # Chain 5 R3: "seniors 55+"
    {
        "name": "4. Unemployment youth → 'seniors 55+'",
        "state": {"indicator": "UNEMPLOYMENT_RATE", "country": "Canada", "provider": "STATSCAN",
                  "dimensions": {"age_group": "15 to 24 years"}},
        "query": "Now show for seniors (55+)",
        "expect": "added_dimensions with age_group=55+ or similar",
    },
    # Chain 7 R3: "Compare with Alberta"
    {
        "name": "5. Housing starts BC → 'Compare with Alberta'",
        "state": {"indicator": "HOUSING_STARTS", "country": "Canada", "provider": "STATSCAN",
                  "dimensions": {"geography": "British Columbia"}},
        "query": "Compare with Alberta",
        "expect": "ADDITIVE: added geography for Alberta, NOT replacement",
    },
    # Chain 9 R2: "trade balance" after GDP
    {
        "name": "6. Canada GDP → 'trade balance'",
        "state": {"indicator": "GDP", "country": "Canada", "provider": "STATSCAN"},
        "query": "What about the trade balance?",
        "expect": "changed_indicator='trade balance' (indicator switch, NOT dimension)",
    },
    # Multi-country time change
    {
        "name": "7. US/Japan inflation → 'from 2020 onwards'",
        "state": {"indicator": "inflation rate", "countries": ["US", "JP"], "provider": "WORLDBANK",
                  "start_date": "2019-01-01", "end_date": "2026-12-31"},
        "query": "Show from 2020 onwards",
        "expect": "changed_start_date='2020-01-01' only, preserve countries and indicator",
    },
    # Compound: country + time
    {
        "name": "8. Japan GDP → 'Show Germany last 5 years'",
        "state": {"indicator": "GDP", "country": "JP", "provider": "WORLDBANK"},
        "query": "Show Germany for the last 5 years",
        "expect": "changed_country=DE + changed_start_date (compound change)",
    },
    # Indicator switch preserving countries
    {
        "name": "9. GDP China/India/Brazil → 'unemployment rates instead'",
        "state": {"indicator": "GDP growth", "countries": ["CN", "IN", "BR"], "provider": "WORLDBANK"},
        "query": "What about their unemployment rates instead?",
        "expect": "changed_indicator='unemployment rate', countries preserved (null)",
    },
    # Chart type change
    {
        "name": "10. CPI line chart → 'show as bar chart'",
        "state": {"indicator": "CPI", "country": "Canada", "provider": "STATSCAN",
                  "chart_type": "line"},
        "query": "Show this as a bar chart",
        "expect": "changed_chart_type='bar' only",
    },
]


async def run_test(client, test_case: dict) -> dict:
    state_text = format_state(test_case["state"])
    prompt = SYSTEM_PROMPT.replace("{state_text}", state_text).replace("{query}", test_case["query"])

    try:
        delta = await client.chat.completions.create(
            model="gpt-oss-120b",
            response_model=FollowUpDelta,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": test_case["query"]},
            ],
            temperature=0.1,
            max_tokens=500,
        )
        # Collect non-None fields
        changed = {}
        for field_name, value in delta.model_dump().items():
            if value is not None and value is not False and value != 0.5:
                if field_name == "confidence" and value == 0.5:
                    continue
                changed[field_name] = value
        return {
            "name": test_case["name"],
            "query": test_case["query"],
            "expect": test_case["expect"],
            "delta": changed,
            "raw": delta.model_dump(exclude_none=True),
        }
    except Exception as e:
        return {
            "name": test_case["name"],
            "query": test_case["query"],
            "expect": test_case["expect"],
            "error": str(e),
        }


async def main():
    raw_client = AsyncOpenAI(
        api_key="not-needed",
        base_url="http://localhost:8000/v1",
        timeout=30.0,
    )
    client = instructor.from_openai(raw_client, mode=instructor.Mode.JSON)

    print("=" * 70)
    print("LLM Delta Extraction Dry-Run — 10 Hard Scenarios")
    print("=" * 70)

    results = []
    for test in TESTS:
        print(f"\n--- {test['name']} ---")
        print(f"  Query: \"{test['query']}\"")
        print(f"  Expect: {test['expect']}")
        result = await run_test(client, test)
        results.append(result)

        if "error" in result:
            print(f"  ERROR: {result['error']}")
        else:
            print(f"  Delta: {json.dumps(result['delta'], indent=2)}")

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    passed = 0
    for r in results:
        status = "ERROR" if "error" in r else "OK"
        if status == "OK":
            passed += 1
        print(f"  {status}: {r['name']}")
        if "delta" in r:
            # Show key fields
            d = r["delta"]
            key_fields = [f"{k}={v}" for k, v in d.items()
                          if k not in ("confidence", "delta_type", "is_new_query", "is_dimension_modifier_change")]
            print(f"       → {', '.join(key_fields) if key_fields else '(no changes detected)'}")
    print(f"\n  {passed}/{len(results)} completed successfully")


if __name__ == "__main__":
    asyncio.run(main())
