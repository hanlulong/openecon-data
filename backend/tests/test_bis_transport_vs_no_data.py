"""BIS must not convert an outage into a "BIS has no data" verdict.

Before: every per-candidate fetch/parse sat under ``except Exception:
continue``, so a 503 / timeout / open breaker looked exactly like a genuine
empty result and the provider raised ``BIS has no ... Try World Bank or IMF``
-- a false no-data claim that told the user the series does not exist. Now a
country whose candidates never answered has NO verdict: the aggregate re-raises
the provider helper's transport error text, while genuine 404/empty answers
keep the honest no-data error.
"""
from __future__ import annotations

import httpx
import pytest

from backend.providers import base as base_module
from backend.providers.bis import BISProvider
from backend.tests.utils import MockAsyncResponse, run
from backend.utils.retry import DataNotAvailableError, is_provider_unavailable

_PAYLOAD = {
    "data": {
        "dataSets": [{"series": {"0:0": {"observations": {"0": ["5.25"], "1": ["5.5"]}}}}],
        "structure": {
            "dimensions": {
                "series": [
                    {"id": "FREQ", "values": [{"id": "M", "name": "Monthly"}]},
                    {"id": "REF_AREA", "values": [{"id": "US", "name": "United States"}]},
                ],
                "observation": [{"id": "TIME_PERIOD", "values": [{"id": "2024-01"}, {"id": "2024-02"}]}],
            }
        },
    }
}


@pytest.fixture(autouse=True)
def _isolated_breaker():
    base_module._provider_breakers.pop("BIS", None)
    yield
    base_module._provider_breakers.pop("BIS", None)


def _payload(period_ids):
    """A one-series monthly BIS payload whose observations are ``period_ids``."""
    return {
        "data": {
            "dataSets": [{"series": {"0:0": {"observations": {
                str(idx): [str(5.0 + idx / 10)] for idx in range(len(period_ids))
            }}}}],
            "structure": {
                "dimensions": {
                    "series": [
                        {"id": "FREQ", "values": [{"id": "M", "name": "Monthly"}]},
                        {"id": "REF_AREA", "values": [{"id": "US", "name": "United States"}]},
                    ],
                    "observation": [{"id": "TIME_PERIOD", "values": [{"id": p} for p in period_ids]}],
                }
            },
        }
    }


def _recent_months(count=3):
    """Monthly period ids ending last month (a current monthly series)."""
    from datetime import date

    today = date.today()
    year, month = today.year, today.month
    out = []
    for _ in range(count):
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        out.append(f"{year}-{month:02d}")
    return list(reversed(out))


class _Client:
    """Route each REF_AREA (last key segment) to a scripted behaviour."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = []

    async def get(self, url, *, params=None, **_kwargs):
        area = str(url).rsplit(".", 1)[-1]
        self.calls.append(area)
        action = self.behaviour.get(area, "404")
        if action == "timeout":
            raise httpx.ReadTimeout("timed out")
        if action == "connect":
            raise httpx.ConnectError("connection refused")
        if action == "503":
            return MockAsyncResponse({"errors": [{"code": 503}]}, status_code=503)
        if action == "404":
            return MockAsyncResponse({"errors": [{"code": 404}]}, status_code=404)
        if action == "empty":
            return MockAsyncResponse({"data": {"dataSets": []}})
        if action == "garbled":
            return MockAsyncResponse({"data": {"dataSets": [{"series": {"0": {}}}], "structure": {}}})
        if action == "current":
            return MockAsyncResponse(_payload(_recent_months()))
        if isinstance(action, tuple) and action[0] == "periods":
            return MockAsyncResponse(_payload(list(action[1])))
        return MockAsyncResponse(_PAYLOAD)


def _provider():
    provider = BISProvider(metadata_search_service=None)
    provider.MAX_RETRIES = 1  # keep tenacity from sleeping; retry policy is base.py's concern
    return provider


def _fetch(monkeypatch, behaviour, **kwargs):
    client = _Client(behaviour)
    monkeypatch.setattr("backend.providers.bis.get_http_client", lambda: client)
    kwargs.setdefault("indicator", "WS_CBPOL")
    return run(_provider().fetch_indicator(**kwargs)), client


@pytest.mark.parametrize("failure", ["503", "timeout", "connect"])
def test_transport_failure_is_not_reported_as_no_data(monkeypatch, failure):
    with pytest.raises(DataNotAvailableError) as info:
        _fetch(monkeypatch, {"US": failure}, country="US")
    message = str(info.value)
    assert "transport failure, not a no-data verdict" in message
    assert "BIS has no" not in message and "Try World Bank" not in message
    assert isinstance(info.value.__cause__, DataNotAvailableError)  # helper's own transport error
    # Typed, so no consumer (no-data menu exclusion, multi-indicator reason)
    # reads the outage as a definitive empty.
    assert is_provider_unavailable(info.value)
    assert is_provider_unavailable(info.value.__cause__)


def test_timeout_text_survives_for_the_user_facing_formatter(monkeypatch):
    with pytest.raises(DataNotAvailableError) as info:
        _fetch(monkeypatch, {"US": "timeout"}, country="US")
    assert "timed out" in str(info.value).lower()


@pytest.mark.parametrize("answer", ["404", "empty", "garbled"])
def test_genuine_no_data_keeps_honest_no_data_error(monkeypatch, answer):
    with pytest.raises(DataNotAvailableError) as info:
        _fetch(monkeypatch, {"US": answer}, country="US")
    assert "BIS has no" in str(info.value)
    assert "transport failure" not in str(info.value)
    assert not is_provider_unavailable(info.value)


def test_mixed_countries_one_unreachable_is_not_a_no_data_verdict(monkeypatch):
    with pytest.raises(DataNotAvailableError) as info:
        _fetch(monkeypatch, {"US": "404", "GB": "503"}, countries=["US", "GB"])
    assert "transport failure" in str(info.value)
    assert "GB" in str(info.value)
    assert is_provider_unavailable(info.value)


def test_partial_outage_still_serves_reachable_countries(monkeypatch):
    series, _client = _fetch(monkeypatch, {"US": "ok", "GB": "timeout"}, countries=["US", "GB"])
    assert len(series) == 1
    assert [p.value for p in series[0].data] == [5.25, 5.5]


def test_eurozone_member_with_unreachable_aggregate_has_no_verdict(monkeypatch):
    # The national key answers 404 but the XM aggregate that would serve the
    # policy rate is unreachable: claiming "no data" would be false.
    with pytest.raises(DataNotAvailableError) as info:
        _fetch(monkeypatch, {"DE": "404", "XM": "503"}, country="DE")
    assert "transport failure" in str(info.value)


def test_eurozone_member_served_by_aggregate_despite_national_404(monkeypatch):
    series, client = _fetch(monkeypatch, {"DE": "404", "XM": "ok"}, country="DE")
    assert series and series[0].data
    assert "XM" in client.calls


def test_cooldown_class_and_retry_after_survive_the_aggregate(monkeypatch):
    from backend.utils.retry import ProviderCoolingDown

    async def cooling(self, *_args, **_kwargs):
        raise ProviderCoolingDown("Rate limited (429); upstream cooldown is active", retry_after_seconds=42.0)

    monkeypatch.setattr(BISProvider, "_get_with_retry", cooling)
    with pytest.raises(ProviderCoolingDown) as info:
        _fetch(monkeypatch, {}, country="US")
    assert info.value.retry_after_seconds == 42.0
    assert "transport failure, not a no-data verdict" in str(info.value)
    assert "429" in str(info.value)


def test_served_candidate_discloses_an_unreachable_fresher_candidate(monkeypatch):
    # The national key answers (possibly a discontinued series) while the XM
    # aggregate freshness-wins would compare it against timed out: the answer
    # must not read as the confirmed latest BIS series.
    series, client = _fetch(monkeypatch, {"DE": "ok", "XM": "timeout"}, country="DE")
    assert "XM" in client.calls
    notes = series[0].metadata.notes or []
    assert any("could not be reached for candidate series XM" in note for note in notes)


def test_all_candidates_answering_adds_no_reachability_note(monkeypatch):
    series, _client = _fetch(monkeypatch, {"DE": "ok", "XM": "ok"}, country="DE")
    assert not any("could not be reached" in note for note in (series[0].metadata.notes or []))


def test_current_served_candidate_gets_no_reachability_note(monkeypatch):
    # The served national/aggregate series is already current (last month):
    # no unreached sibling can be fresher, so "may not show the latest
    # available data" would be a false disclosure.
    series, client = _fetch(monkeypatch, {"DE": "current", "XM": "timeout"}, country="DE")
    assert "XM" in client.calls
    assert not any("could not be reached" in note for note in (series[0].metadata.notes or []))


def test_stale_served_candidate_inside_a_past_window_is_still_disclosed(monkeypatch):
    # A national key that stopped in 1998 vs a 1990-2024 window: stale for
    # the request, and the unreached aggregate could have served later years.
    series, _client = _fetch(
        monkeypatch, {"DE": ("periods", ("1998-11", "1998-12")), "XM": "503"},
        country="DE", start_year=1990, end_year=2024,
    )
    assert any("could not be reached for candidate series XM" in note for note in series[0].metadata.notes or [])


def test_candidate_reaching_the_end_of_a_past_window_is_current(monkeypatch):
    series, _client = _fetch(
        monkeypatch, {"DE": ("periods", ("2024-11", "2024-12")), "XM": "timeout"},
        country="DE", start_year=2020, end_year=2024,
    )
    assert not any("could not be reached" in note for note in (series[0].metadata.notes or []))
