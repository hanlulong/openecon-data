"""Dry-run query classifier against 20 hard scenarios.

Tests whether a single LLM call can correctly classify follow-up queries
into the right handling path BEFORE execution.
"""
import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from openai import AsyncOpenAI
import instructor
from pydantic import BaseModel, Field
from typing import Optional, Literal


class QueryClassification(BaseModel):
    """Classification of a follow-up query into the correct handling path."""
    query_type: Literal[
        "parameter_delta",       # Simple change: country, time, indicator, dimension, provider
        "pro_mode",              # Complex analysis: correlation, regression, scatter plot, custom calculation
        "new_query",             # Completely new topic, unrelated to prior context
        "clarification_answer",  # Answer to a pending clarification question
        "informational",         # Metadata question ("what providers do you have?")
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=300)


SYSTEM_PROMPT = """You are a query classifier for an economic data system.

Given the user's conversation state and their new message, classify the message into ONE type:

- "parameter_delta": User wants to MODIFY the current query — change country, time range, indicator, add/remove countries, filter by dimension (sex, age, province), switch provider, change chart type. These are simple parameter changes that don't need code execution.

- "pro_mode": User wants COMPLEX ANALYSIS that requires Python code execution — correlations, regressions, scatter plots, custom calculations, statistical tests, forecasting, combining multiple datasets mathematically. Keywords: "correlate", "regression", "scatter plot", "calculate", "forecast", "predict", "ratio of X to Y", "compare trends statistically".

- "new_query": User's message is a completely NEW topic unrelated to the prior conversation. No prior context should be preserved.

- "clarification_answer": User is answering a question the system asked — picking from options, confirming a choice, saying "yes" or "the first one".

- "informational": User asks about the system itself — "what data sources do you have?", "how does this work?", "what countries are available?"

IMPORTANT: "parameter_delta" is the most common type for follow-ups. Only classify as "pro_mode" if the user explicitly requests computation/analysis beyond simple data retrieval.

CURRENT STATE:
{state_text}

{pending_text}

Classify the user's message."""


def format_state(state: dict) -> str:
    lines = []
    for k, v in state.items():
        if v is not None:
            lines.append(f"  {k}: {v}")
    return "\n".join(lines) if lines else "  (no prior context)"


TESTS = [
    # === PARAMETER_DELTA cases ===
    {"name": "1. Country add", "state": {"indicator": "GDP", "countries": ["US", "China"]}, "pending": None,
     "query": "Add India and Brazil", "expect": "parameter_delta"},

    {"name": "2. Time change", "state": {"indicator": "unemployment rate", "country": "Canada"}, "pending": None,
     "query": "Show me the last 20 years", "expect": "parameter_delta"},

    {"name": "3. Dimension: show female", "state": {"indicator": "unemployment rate", "country": "Canada", "provider": "StatsCan"}, "pending": None,
     "query": "show female", "expect": "parameter_delta"},

    {"name": "4. Dimension: break down by sex", "state": {"indicator": "employment rate", "country": "Canada"}, "pending": None,
     "query": "Can you break it down by sex?", "expect": "parameter_delta"},

    {"name": "5. Indicator switch", "state": {"indicator": "GDP", "country": "Japan"}, "pending": None,
     "query": "What about unemployment instead?", "expect": "parameter_delta"},

    {"name": "6. CPI subcategory", "state": {"indicator": "CPI", "country": "Canada"}, "pending": None,
     "query": "Show me shelter costs", "expect": "parameter_delta"},

    {"name": "7. Province filter", "state": {"indicator": "unemployment rate", "country": "Canada"}, "pending": None,
     "query": "Show for Ontario", "expect": "parameter_delta"},

    {"name": "8. Chart type change", "state": {"indicator": "GDP", "country": "US"}, "pending": None,
     "query": "Show this as a bar chart", "expect": "parameter_delta"},

    {"name": "9. Crypto switch", "state": {"indicator": "Bitcoin", "provider": "CoinGecko"}, "pending": None,
     "query": "Show ethereum instead", "expect": "parameter_delta"},

    {"name": "10. Provider switch", "state": {"indicator": "unemployment rate", "country": "Canada", "provider": "StatsCan"}, "pending": None,
     "query": "Use FRED instead", "expect": "parameter_delta"},

    # === PRO_MODE cases ===
    {"name": "11. Correlation request", "state": {"indicator": "GDP growth", "country": "US"}, "pending": None,
     "query": "Now correlate this with unemployment rate", "expect": "pro_mode"},

    {"name": "12. Scatter plot", "state": {"indicator": "inflation", "countries": ["US", "Japan", "Germany"]}, "pending": None,
     "query": "Show me a scatter plot of inflation vs GDP growth", "expect": "pro_mode"},

    {"name": "13. Regression", "state": {"indicator": "GDP", "country": "China"}, "pending": None,
     "query": "Run a linear regression to forecast next 5 years", "expect": "pro_mode"},

    {"name": "14. Statistical comparison", "state": {"indicator": "unemployment", "countries": ["US", "Canada"]}, "pending": None,
     "query": "Calculate the standard deviation and compare trends statistically", "expect": "pro_mode"},

    {"name": "15. Complex ratio", "state": {"indicator": "exports", "country": "Japan"}, "pending": None,
     "query": "Calculate exports as a ratio of GDP and plot the trend", "expect": "pro_mode"},

    # === NEW_QUERY cases ===
    {"name": "16. Unrelated topic", "state": {"indicator": "GDP", "country": "US"}, "pending": None,
     "query": "What is the bitcoin price today?", "expect": "new_query"},

    {"name": "17. Completely different", "state": {"indicator": "CPI", "country": "Canada"}, "pending": None,
     "query": "Show me poverty rates in Sub-Saharan Africa", "expect": "new_query"},

    # === CLARIFICATION_ANSWER cases ===
    {"name": "18. Numeric choice", "state": {"indicator": "employment", "country": "Canada"},
     "pending": "System asked: Did you mean 1) Employment rate 2) Employment count?",
     "query": "1", "expect": "clarification_answer"},

    {"name": "19. Yes/confirm", "state": {"indicator": "GDP", "country": "US"},
     "pending": "System asked: Did you mean GDP in current US dollars?",
     "query": "Yes, that one", "expect": "clarification_answer"},

    # === INFORMATIONAL cases ===
    {"name": "20. System question", "state": {"indicator": "GDP", "country": "US"}, "pending": None,
     "query": "What data sources do you support?", "expect": "informational"},
]


async def run_test(client, test: dict) -> dict:
    state_text = format_state(test["state"])
    pending_text = f"Pending clarification: {test['pending']}" if test["pending"] else "No pending clarification."

    prompt = SYSTEM_PROMPT.replace("{state_text}", state_text).replace("{pending_text}", pending_text)

    try:
        result = await client.chat.completions.create(
            model="gpt-oss-120b",
            response_model=QueryClassification,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": test["query"]},
            ],
            temperature=0.1,
            max_tokens=200,
        )
        correct = result.query_type == test["expect"]
        return {
            "name": test["name"],
            "query": test["query"],
            "expect": test["expect"],
            "got": result.query_type,
            "confidence": result.confidence,
            "reasoning": result.reasoning,
            "correct": correct,
        }
    except Exception as e:
        return {
            "name": test["name"],
            "query": test["query"],
            "expect": test["expect"],
            "got": "ERROR",
            "error": str(e),
            "correct": False,
        }


async def main():
    raw_client = AsyncOpenAI(
        api_key="not-needed",
        base_url="http://localhost:8000/v1",
        timeout=30.0,
    )
    client = instructor.from_openai(raw_client, mode=instructor.Mode.JSON)

    print("=" * 70)
    print("Query Classifier Dry-Run — 20 Hard Scenarios")
    print("=" * 70)

    results = []
    for test in TESTS:
        result = await run_test(client, test)
        results.append(result)
        status = "✅" if result["correct"] else "❌"
        print(f"{status} {result['name']}")
        print(f"   Query: \"{result['query']}\"")
        print(f"   Expected: {result['expect']} | Got: {result['got']} ({result.get('confidence', '?')})")
        if not result["correct"]:
            print(f"   Reasoning: {result.get('reasoning', result.get('error', '?'))}")
        print()

    correct = sum(1 for r in results if r["correct"])
    print("=" * 70)
    print(f"TOTAL: {correct}/{len(results)} correct")
    print("=" * 70)

    # Show failures
    failures = [r for r in results if not r["correct"]]
    if failures:
        print(f"\nFAILURES ({len(failures)}):")
        for f in failures:
            print(f"  {f['name']}: expected={f['expect']}, got={f['got']}")
            print(f"    Reasoning: {f.get('reasoning', f.get('error', '?'))}")


if __name__ == "__main__":
    asyncio.run(main())
