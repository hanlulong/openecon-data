"""Query classification for multi-round conversations.

Single LLM call that determines HOW a query should be handled,
separating classification from execution. This replaces the priority
chain in process_query where handlers competed in fixed order.

Query types:
- parameter_delta: Simple change to current query (country, time, indicator, dimension)
- pro_mode: Complex analysis requiring code execution (correlation, regression, etc.)
- new_query: Unrelated new topic
- clarification_answer: Response to a pending clarification
- informational: Question about the system, not a data fetch
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Literal, Optional, TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .conversation_state_v2 import ConversationState

logger = logging.getLogger(__name__)


class QueryClassification(BaseModel):
    """Result of classifying a follow-up query."""
    query_type: Literal[
        "parameter_delta",
        "pro_mode",
        "new_query",
        "clarification_answer",
        "informational",
    ]
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


# Structural patterns that don't need LLM classification
_NUMERIC_RE_PATTERN = r"^\s*\d+\s*$"


CLASSIFIER_SYSTEM_PROMPT = """You are a query classifier for an economic data system.

Given the user's conversation state and their new message, classify the message into ONE type:

- "parameter_delta": User wants to MODIFY the current query — change country, time range, indicator, add/remove countries, filter by dimension (sex, age, province, product subcategory), switch provider, change chart type, switch between related economic indicators. These are parameter changes that don't require code execution.

- "pro_mode": User wants COMPLEX ANALYSIS requiring Python code execution — correlations, regressions, scatter plots, custom calculations, statistical tests, forecasting, combining multiple datasets mathematically, trend comparison with statistics. Keywords: "correlate", "regression", "scatter plot", "calculate", "forecast", "predict", "ratio of X to Y", "standard deviation", "statistical".

- "new_query": User's message is a completely NEW topic unrelated to the prior conversation. No prior context should be preserved.

- "clarification_answer": User is answering a question the system asked — picking from options, confirming a choice, saying "yes", "the first one", or a number.

- "informational": User asks about the system itself — "what data sources do you have?", "how does this work?", "what countries are available?"

IMPORTANT DISTINCTIONS:
- "Show unemployment instead" → parameter_delta (indicator switch)
- "Correlate unemployment with GDP" → pro_mode (needs computation)
- "Compare US and Japan" → parameter_delta (add countries)
- "Compare trends statistically" → pro_mode (needs statistics)
- "Show me a scatter plot" → pro_mode (needs code to generate)
- "Show as bar chart" → parameter_delta (chart type change)

CURRENT STATE:
{state_text}

{pending_text}

Classify the user's message."""


def _build_state_text(state: Optional["ConversationState"]) -> str:
    """Build a concise text representation of the conversation state."""
    if state is None:
        return "  (no prior context — this is likely a first query)"

    lines = []
    if state.indicator:
        lines.append(f"  Indicator: {state.indicator}")
    if state.country:
        lines.append(f"  Country: {state.country}")
    if state.countries:
        lines.append(f"  Countries: {', '.join(state.countries)}")
    if state.provider or state.routed_provider:
        lines.append(f"  Provider: {state.provider or state.routed_provider}")
    if state.start_date:
        lines.append(f"  Time: {state.start_date} to {state.end_date or 'now'}")
    if state.dimensions:
        lines.append(f"  Dimensions: {state.dimensions}")
    if state.chart_type:
        lines.append(f"  Chart: {state.chart_type}")
    if state.coin_ids:
        lines.append(f"  Crypto: {state.coin_ids}")
    return "\n".join(lines) if lines else "  (empty state)"


async def classify_query(
    query: str,
    state: Optional["ConversationState"],
    pending_clarification: Optional[Dict[str, Any]],
    instructor_client: Any,
    instructor_model: str,
) -> QueryClassification:
    """Classify a query into the correct handling path.

    Parameters
    ----------
    query : str
        The user's raw query text.
    state : ConversationState or None
        Accumulated conversation state from prior turns.
    pending_clarification : dict or None
        If the system asked a clarification question, its details.
    instructor_client : Instructor client
        The Instructor-wrapped OpenAI client.
    instructor_model : str
        The model name to use.

    Returns
    -------
    QueryClassification
        The classification result with query_type and confidence.
    """
    import re

    query_text = query.strip()

    # === Structural fast paths (no LLM needed) ===

    # Numeric answer to pending choice
    if pending_clarification and re.match(_NUMERIC_RE_PATTERN, query_text):
        return QueryClassification(
            query_type="clarification_answer",
            confidence=0.99,
        )

    # No prior state → must be new query or informational
    if state is None:
        return QueryClassification(
            query_type="new_query",
            confidence=0.95,
        )

    # === LLM classification ===
    state_text = _build_state_text(state)
    pending_text = (
        f"Pending clarification: {pending_clarification.get('question', 'yes/no')}"
        if pending_clarification
        else "No pending clarification."
    )

    prompt = CLASSIFIER_SYSTEM_PROMPT.replace("{state_text}", state_text).replace("{pending_text}", pending_text)

    try:
        result = await instructor_client.chat.completions.create(
            model=instructor_model,
            response_model=QueryClassification,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": query_text},
            ],
            temperature=0.1,
            max_tokens=100,
            max_retries=2,
        )
        logger.info(
            "Query classified: type=%s, confidence=%.2f, query='%s'",
            result.query_type, result.confidence, query_text[:50],
        )
        return result
    except Exception as exc:
        logger.warning("Query classification failed: %s — defaulting to new_query (safe fallthrough)", exc)
        # Default to new_query — it falls through to the standard LLM parse
        # which handles everything. parameter_delta would be dangerous because
        # it would trap genuinely new queries in the delta extraction path.
        return QueryClassification(
            query_type="new_query",
            confidence=0.3,
        )
