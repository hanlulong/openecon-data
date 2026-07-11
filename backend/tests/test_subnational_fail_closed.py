"""Guard tests for the subnational fail-closed enforcement (Proposal B).

When the user names a sub-country region but the served data is national-level
(the provider did not decompose to it), the response is replaced with an
explicit explanation instead of returning national data silently mislabeled as
the region. StatsCan+CA and FRED+US are exempt (their own mechanisms serve
genuine subnational data), and every non-triggering case passes through.
"""
from __future__ import annotations

from backend.models import Metadata, NormalizedData, ParsedIntent, QueryResponse
from backend.services.query import QueryService


def _svc() -> QueryService:
    return QueryService.__new__(QueryService)


def _series(indicator="GDP", country="China", source="WorldBank", notes=None) -> NormalizedData:
    return NormalizedData(
        metadata=Metadata(
            source=source, indicator=indicator, country=country,
            frequency="annual", unit="USD", notes=notes,
        ),
        data=[{"date": "2020-01-01", "value": 100.0}],
    )


def _resp(intent, data) -> QueryResponse:
    return QueryResponse(conversationId="c", intent=intent, data=data, clarificationNeeded=False)


def _intent(provider="WorldBank", region="Beijing", country="China", language=None, **params):
    p = {"country": country}
    p.update(params)
    intent = ParsedIntent(
        apiProvider=provider, indicators=["GDP"], parameters=p,
        clarificationNeeded=False, subnationalRegion=region,
    )
    intent.language = language
    return intent


def test_national_worldbank_for_region_becomes_explanation():
    out = _svc()._enforce_subnational_fail_closed(_resp(_intent(), [_series()]))
    assert out.data is None
    assert out.error == "subnational_data_unavailable"
    assert "Beijing" in out.message and "China" in out.message
    assert "not published there" in out.message


def test_statscan_canada_province_untouched():
    intent = _intent(provider="StatsCan", region="Ontario", country="CA")
    data = [_series(indicator="Unemployment rate", country="Ontario", source="StatsCan")]
    out = _svc()._enforce_subnational_fail_closed(_resp(intent, data))
    assert out.data == data and out.error is None


def test_statscan_national_data_for_region_now_fails_closed():
    # Contract updated 2026-07-10: the blanket StatsCan exemption hid the
    # exact failure this check exists for — a national-only cube selected for
    # "Ontario GDP" served NATIONAL data silently. Nationally-labeled StatsCan
    # data for a region request now fails closed like every other provider.
    intent = _intent(provider="STATSCAN", region="Ontario", country="Canada")
    data = [_series(indicator="Unemployment rate", country="Canada", source="StatsCan")]
    out = _svc()._enforce_subnational_fail_closed(_resp(intent, data))
    assert not out.data
    assert out.error == "subnational_data_unavailable"


def test_statscan_genuine_provincial_series_passes():
    # Genuine provincial results NAME the region (verified live:
    # "Canadian Unemployment Rate - Ontario") — the reference check passes them.
    intent = _intent(provider="STATSCAN", region="Ontario", country="Canada")
    data = [
        _series(
            indicator="Canadian Unemployment Rate - Ontario",
            country="Canada",
            source="StatsCan",
        )
    ]
    out = _svc()._enforce_subnational_fail_closed(_resp(intent, data))
    assert out.data == data and out.error is None


def test_fred_us_state_untouched():
    intent = _intent(provider="FRED", region="California", country="US")
    data = [_series(indicator="California Real GDP", country="US", source="FRED")]
    out = _svc()._enforce_subnational_fail_closed(_resp(intent, data))
    assert out.data == data and out.error is None


def test_no_region_untouched():
    intent = _intent(region=None)
    data = [_series()]
    out = _svc()._enforce_subnational_fail_closed(_resp(intent, data))
    assert out.data == data and out.error is None


def test_served_data_referencing_region_passes_through():
    # A non-exempt provider that DID decompose to the region (its metadata names
    # it) is genuine subnational data — must not fire.
    intent = _intent(provider="WorldBank", region="Beijing", country="China")
    data = [_series(indicator="GDP for Beijing Municipality")]
    out = _svc()._enforce_subnational_fail_closed(_resp(intent, data))
    assert out.data == data and out.error is None


def test_region_matched_in_notes_passes_through():
    intent = _intent(provider="WorldBank", region="Zhejiang", country="China")
    data = [_series(indicator="GDP", notes=["Subnational series: Zhejiang province"])]
    out = _svc()._enforce_subnational_fail_closed(_resp(intent, data))
    assert out.data == data and out.error is None


def test_empty_data_untouched_by_enforcement():
    # Empty data is finalization's job, not enforcement's.
    out = _svc()._enforce_subnational_fail_closed(_resp(_intent(), []))
    assert out.error is None


def test_existing_error_untouched():
    resp = QueryResponse(
        conversationId="c", intent=_intent(), data=[_series()],
        clarificationNeeded=False, error="verification_failed", message="x",
    )
    out = _svc()._enforce_subnational_fail_closed(resp)
    assert out.error == "verification_failed"


def test_zh_language_renders_chinese_explanation():
    intent = _intent(language="zh")
    out = _svc()._enforce_subnational_fail_closed(_resp(intent, [_series()]))
    assert out.error == "subnational_data_unavailable"
    assert "级别的数据" in out.message   # native zh framing
    assert "Beijing" in out.message      # region name interpolated


def test_served_data_references_region_helper():
    ref = QueryService._served_data_references_region
    hit = [_series(indicator="Unemployment rate, Ontario")]
    miss = [_series(indicator="Unemployment rate", country="Canada")]
    assert ref(hit, "Ontario") is True
    assert ref(miss, "Ontario") is False
    assert ref(miss, "") is False  # empty region never matches
