"""Deterministic LLM-control fixtures for IndicatorSelector tests."""

from __future__ import annotations

LLM_SELECTOR_FIXTURES = {
    "pick": "PICK: 2\nReason: candidate 2 directly measures the requested concept.",
    "ask": "ASK: 1, 3\nReason: both options are valid but represent different measures.",
    "reject_search": (
        "REJECT: none of the candidates measure the requested concept.\n"
        "SEARCH: direct total count measure"
    ),
    "undecided": "The candidates contain useful data, but I cannot decide.",
}

