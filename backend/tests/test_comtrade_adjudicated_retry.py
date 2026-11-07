"""Comtrade dispatch: commodity text first, adjudicated HS code only as a
bound, one-shot retry when the text does not resolve.

Commodity text always beat a numeric selector pick, so when the provider's
lexical HS catalog could not resolve the text (comtrade_commodity_unresolved /
comtrade_hs_subheading_unresolved / comtrade_hs_subheading_ambiguous) the
request failed even though the indicator selector had already adjudicated a
concrete HS code. The retry uses that code only when it is a 2-6 digit HS code
(optionally "HS"-prefixed; bare digits are dispatched), adjudicated by THIS request's
resolver, and bound to the same commodity surface — never TOTAL, never a code
carried from a previous turn.
"""

import asyncio

import pytest

from backend.models import DataPoint, ExecutionPlan, Metadata, NormalizedData, ParsedIntent
from backend.services.data_fetcher import (
    _comtrade_adjudicated_retry_code,
    _comtrade_dispatch_commodity,
    fetch_from_provider_dispatch,
)
from backend.utils.retry import DataNotAvailableError

_UNRESOLVED = DataNotAvailableError("comtrade_commodity_unresolved: 'x' did not match any UN Comtrade HS commodity.")
_AMBIGUOUS = DataNotAvailableError("comtrade_hs_subheading_ambiguous: provider catalog contains multiple ...")
_SUBHEADING_UNRESOLVED = DataNotAvailableError(
    "comtrade_hs_subheading_unresolved: specific HS heading text did not resolve to a provider catalog code"
)


def _params(**kw):
    base = {
        "reporter": "India",
        "partner": "Russia",
        "flow": "IMPORT",
        "commodity": "petroleum oils crude",
        "indicator": "2709",
        "__semantic_authority": "llm_adjudication",
        "__semantic_indicator_label": "petroleum oils crude imports",
    }
    base.update(kw)
    return base


# --- pure decision ----------------------------------------------------------

@pytest.mark.parametrize("error", [_UNRESOLVED, _AMBIGUOUS, _SUBHEADING_UNRESOLVED])
def test_retry_on_every_text_resolution_failure(error):
    # All three provider markers are raised while resolving the TEXT, before
    # any network request; each is a case a bound adjudicated code can rescue.
    assert _comtrade_adjudicated_retry_code(_params(), "petroleum oils crude", error) == "2709"


@pytest.mark.parametrize(
    "indicator,expected",
    [
        ("TOTAL", None),
        ("crude oil", None),
        ("27091", "27091"),
        ("1", None),
        ("1234567", None),
        ("", None),
        (None, None),
        # Provider-legal HS-prefixed shape (indicators.db chapter codes HS01..HS97)
        ("HS27", "27"),
        ("hs 2709", "2709"),
        ("HS", None),
        ("HS1234567", None),
        ("HSX27", None),
        ("27 crude", None),
    ],
)
def test_only_hs_code_shapes_retry_and_dispatch_bare_digits(indicator, expected):
    params = _params(indicator=indicator)
    assert _comtrade_adjudicated_retry_code(params, "petroleum oils crude", _UNRESOLVED) == expected


@pytest.mark.parametrize("authority", ["verified_conversation_state", "", None, "prefetch_guess"])
def test_unauthoritative_or_carried_codes_never_retry(authority):
    params = _params(__semantic_authority=authority)
    assert _comtrade_adjudicated_retry_code(params, "petroleum oils crude", _UNRESOLVED) is None


def test_stale_code_from_previous_commodity_never_retries():
    # Turn 1 adjudicated crude oil -> 2709; turn 2 says "now wheat".
    params = _params(commodity="wheat", __semantic_indicator_label="crude oil imports")
    assert _comtrade_adjudicated_retry_code(params, "wheat", _UNRESOLVED) is None


def test_non_resolution_errors_never_retry():
    err = DataNotAvailableError("Comtrade API rate limit exceeded")
    assert _comtrade_adjudicated_retry_code(_params(), "petroleum oils crude", err) is None


def test_dispatch_still_prefers_commodity_text():
    assert _comtrade_dispatch_commodity(_params()) == "petroleum oils crude"
    assert _comtrade_dispatch_commodity(_params(indicator="TOTAL", commodity="crude oil")) == "crude oil"
    assert _comtrade_dispatch_commodity({"indicator": "2709"}) == "2709"


# --- through the real dispatch chokepoint -----------------------------------

class _FakeComtrade:
    def __init__(self, resolvable):
        self.resolvable = resolvable
        self.calls = []

    async def fetch_trade_data(self, commodity=None, **kwargs):
        self.calls.append(commodity)
        if commodity not in self.resolvable:
            raise DataNotAvailableError(
                f"comtrade_commodity_unresolved: '{commodity}' did not match any UN Comtrade HS commodity."
            )
        return [
            NormalizedData(
                metadata=Metadata(
                    source="UN Comtrade",
                    indicator=f"Imports of {commodity}",
                    country="India",
                    frequency="annual",
                    unit="US Dollars",
                    seriesId=str(commodity),
                ),
                data=[DataPoint(date="2023-01-01", value=1.0)],
            )
        ]


class _FakeSvc:
    def __init__(self, resolvable):
        self.comtrade_provider = _FakeComtrade(resolvable)

    def _normalize_country_to_iso2(self, value):
        return {"india": "IN", "russia": "RU"}.get(str(value).lower(), value)


def _dispatch(svc, params):
    intent = ParsedIntent(
        apiProvider="COMTRADE",
        indicators=["imports"],
        parameters=params,
        clarificationNeeded=False,
        originalQuery="India imports from Russia",
    )
    plan = ExecutionPlan(
        provider="COMTRADE", candidate_id="t", fetch_strategy="provider_dispatch", params=params
    )
    return asyncio.run(fetch_from_provider_dispatch(svc, intent, plan))


def test_unresolved_text_served_with_adjudicated_code_and_note():
    svc = _FakeSvc(resolvable={"2709"})
    result = _dispatch(svc, _params())
    assert svc.comtrade_provider.calls == ["petroleum oils crude", "2709"]
    assert result[0].metadata.seriesId == "2709"
    assert any("served HS 2709" in n for n in result[0].metadata.notes)


def test_resolvable_text_wins_over_different_code():
    svc = _FakeSvc(resolvable={"petroleum oils crude", "27"})
    result = _dispatch(svc, _params(indicator="27"))
    assert svc.comtrade_provider.calls == ["petroleum oils crude"]
    assert result[0].metadata.seriesId == "petroleum oils crude"


def test_total_indicator_never_replaces_text():
    svc = _FakeSvc(resolvable={"crude oil", "TOTAL"})
    result = _dispatch(svc, _params(indicator="TOTAL", commodity="crude oil",
                                    __semantic_indicator_label="crude oil"))
    assert svc.comtrade_provider.calls == ["crude oil"]
    assert result[0].metadata.seriesId == "crude oil"


def test_text_only_unresolved_still_fails_closed():
    svc = _FakeSvc(resolvable=set())
    params = _params(indicator=None, __semantic_authority=None)
    with pytest.raises(DataNotAvailableError, match="comtrade_commodity_unresolved"):
        _dispatch(svc, params)
    assert svc.comtrade_provider.calls == ["petroleum oils crude"]


def test_stale_code_switch_fails_closed_instead_of_serving_old_commodity():
    svc = _FakeSvc(resolvable={"2709"})
    params = _params(commodity="wheat", __semantic_indicator_label="crude oil imports")
    with pytest.raises(DataNotAvailableError):
        _dispatch(svc, params)
    assert svc.comtrade_provider.calls == ["wheat"]


def test_hs_prefixed_adjudicated_chapter_code_dispatched_as_digits():
    svc = _FakeSvc(resolvable={"27"})
    result = _dispatch(svc, _params(indicator="HS27"))
    assert svc.comtrade_provider.calls == ["petroleum oils crude", "27"]
    assert result[0].metadata.seriesId == "27"
    assert any("served HS 27 " in n for n in result[0].metadata.notes)
