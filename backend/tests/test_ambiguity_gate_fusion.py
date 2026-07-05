"""The retrieval-ambiguity gate must be scale-invariant under RRF fusion.

_scores_are_ambiguous used an absolute 0.03 gap tuned for cosine scores
(~0.5-0.9). Production fuses via RRF, whose scores are reciprocal-rank sums
(max ~2/61 ≈ 0.033), so 0.03 was satisfied on almost every query — the
ambiguity signal degenerated to constant-true and biased the LLM to ASK
everywhere. RRF now uses a relative gap; the legacy path is unchanged.
"""

from backend.services.indicator_selector import IndicatorSelector

amb = IndicatorSelector._scores_are_ambiguous


def test_rrf_clear_winner_not_ambiguous():
    # The bug: under the old absolute 0.03, this returned True (over-ask).
    assert amb([0.0328, 0.025, 0.020], "rrf") is False


def test_rrf_genuine_tie_is_ambiguous():
    assert amb([0.0328, 0.0325, 0.0322], "rrf") is True


def test_rrf_gate_not_constant_true():
    # A spread of typical RRF winners must NOT all read as ambiguous.
    winners = [
        [0.0328, 0.026, 0.021],
        [0.033, 0.022, 0.018],
        [0.0325, 0.024, 0.019],
    ]
    assert not all(amb(s, "rrf") for s in winners)


def test_legacy_cosine_unchanged():
    assert amb([0.85, 0.84, 0.83], "legacy") is True   # gap .02 < .03
    assert amb([0.85, 0.80, 0.78], "legacy") is False  # gap .07


def test_default_mode_is_legacy():
    # Default arg keeps the legacy (absolute) behaviour for callers that omit it.
    assert amb([0.85, 0.84, 0.83]) is True


def test_unordered_or_short_scores_safe():
    assert amb([0.03, 0.05, 0.02], "rrf") is False  # not descending
    assert amb([0.03], "rrf") is False               # too few
