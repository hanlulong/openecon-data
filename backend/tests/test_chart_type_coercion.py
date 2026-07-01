"""A stray recommendedChartType must not fail the whole intent parse.

The field used a hard regex pattern, so an LLM value like "pie" or "area"
raised ValidationError, burned all parse retries, and dropped an otherwise
perfect intent. Unknown values now coerce to None (frontend infers the type).
"""

import pytest

from backend.models import ParsedIntent


def _intent(chart_type):
    return ParsedIntent(
        apiProvider="FRED",
        indicators=["GDP"],
        clarificationNeeded=False,
        recommendedChartType=chart_type,
    )


@pytest.mark.parametrize("valid", ["line", "bar", "scatter", "table"])
def test_valid_chart_types_preserved(valid):
    assert _intent(valid).recommendedChartType == valid


@pytest.mark.parametrize("bogus", ["pie", "area", "Histogram", "", "donut"])
def test_unknown_chart_type_coerced_to_none_not_rejected(bogus):
    # Must not raise; must not keep the unrenderable value.
    assert _intent(bogus).recommendedChartType is None
