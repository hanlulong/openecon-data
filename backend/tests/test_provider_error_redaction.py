"""Provider credentials never reach error text, user messages or logs.

httpx embeds the full request URL in HTTPStatusError text. FRED and CoinGecko
send their key as a query parameter and ExchangeRate-API puts it in the URL
path, so these tests drive each provider through the real retry boundary with
an httpx.MockTransport and a fake key.
"""

import asyncio
import logging

import httpx
import pytest

from backend.providers.coingecko import CoinGeckoProvider
from backend.providers.exchangerate import ExchangeRateProvider
from backend.providers.fred import FREDProvider
from backend.services.query_complexity import QueryComplexityAnalyzer
from backend.utils.redaction import redact_secrets
from backend.utils.retry import DataNotAvailableError


FRED_KEY = "fakefredkey0123456789abcdef0123"
EXCHANGE_KEY = "fake-exchangerate-key-0123"
COINGECKO_DEMO_KEY = "CG-fakeDemoKey0123456789"
COINGECKO_PRO_KEY = "fake-coingecko-pro-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def no_rate_limit_wait(monkeypatch):
    async def ready(_provider):
        pass

    monkeypatch.setattr("backend.providers.base.acquire_provider", ready)


@pytest.fixture
def app_logs(caplog):
    """Capture every log record at DEBUG, with httpx/httpcore capped at WARNING
    as backend/main.py caps them (their INFO request lines carry full URLs)."""
    for name in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection"):
        caplog.set_level(logging.WARNING, logger=name)
    return caplog


def _run_with_status(monkeypatch, module_path, status, call, body=None):
    """Run ``call(client)`` with every request answered by ``status``.

    Returns (exception, requests).
    """
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, json=body or {"error": "bad request"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            monkeypatch.setattr(f"{module_path}.get_http_client", lambda: client)
            with pytest.raises(DataNotAvailableError) as excinfo:
                await call()
            return excinfo.value

    return asyncio.run(run()), requests


def _assert_clean(exc, key, logs, *, provider, status):
    text = str(exc)
    assert key not in text
    assert provider in text
    assert str(status) in text
    user_message = QueryComplexityAnalyzer.format_error_message(text, "test query")
    assert key not in user_message
    for record in logs.records:
        assert key not in record.getMessage()


def test_fred_series_error_masks_api_key(monkeypatch, app_logs):
    provider = FREDProvider(FRED_KEY)
    exc, requests = _run_with_status(
        monkeypatch,
        "backend.providers.fred",
        400,
        lambda: provider.fetch_series({"seriesId": "UNRATE", "indicator": "UNRATE"}),
    )
    # The key really was sent, so its absence from the error is the redaction.
    assert requests and requests[0].url.params["api_key"] == FRED_KEY
    _assert_clean(exc, FRED_KEY, app_logs, provider="FRED", status=400)
    assert "api_key=***" in str(exc)


def test_fred_search_failure_log_masks_api_key(monkeypatch, app_logs):
    provider = FREDProvider(FRED_KEY)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(400, json={"error_message": "Bad Request."})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            monkeypatch.setattr("backend.providers.fred.get_http_client", lambda: client)
            return await provider._search_series_dynamic("gold price")

    assert asyncio.run(run()) == []
    assert requests and requests[0].url.params["api_key"] == FRED_KEY
    messages = [record.getMessage() for record in app_logs.records]
    assert any("FRED series search failed" in message for message in messages)
    assert all(FRED_KEY not in message for message in messages)


@pytest.mark.parametrize("status", [400, 401])
@pytest.mark.parametrize("history", [False, True])
def test_exchangerate_path_key_is_masked(monkeypatch, app_logs, status, history):
    provider = ExchangeRateProvider(api_key=EXCHANGE_KEY)
    if history:
        call = lambda: provider.fetch_historical_rate("USD", "EUR", 2024, 1, 2)
    else:
        call = lambda: provider.fetch_exchange_rate("USD", "EUR")
    exc, requests = _run_with_status(monkeypatch, "backend.providers.exchangerate", status, call)
    assert requests and EXCHANGE_KEY in requests[0].url.path
    _assert_clean(exc, EXCHANGE_KEY, app_logs, provider="ExchangeRate", status=status)
    assert "/v6/***/" in str(exc)


@pytest.mark.parametrize(
    ("key", "param"),
    [(COINGECKO_DEMO_KEY, "x_cg_demo_api_key"), (COINGECKO_PRO_KEY, "x_cg_pro_api_key")],
)
def test_coingecko_keys_stay_out_of_errors(monkeypatch, app_logs, key, param):
    provider = CoinGeckoProvider(api_key=key)
    exc, requests = _run_with_status(
        monkeypatch,
        "backend.providers.coingecko",
        400,
        lambda: provider.get_simple_price(["bitcoin"], "usd"),
    )
    assert requests and requests[0].url.params[param] == key
    _assert_clean(exc, key, app_logs, provider="CoinGecko", status=400)


def test_param_names_are_masked_and_imf_series_keys_are_not():
    url = (
        "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin"
        "&x_cg_demo_api_key=CG-abc123&x_cg_pro_api_key=pro456"
        "&API_KEY=upper&apikey=a&api-key=b&access_token=t"
        "&Subscription-Key=s&registrationkey=r"
    )
    redacted = redact_secrets(url)
    for value in ["CG-abc123", "pro456", "upper", "=a&", "=b&", "=t&", "=s&", "registrationkey=r"]:
        assert value not in redacted
    assert "ids=bitcoin" in redacted
    assert redacted.count("=***") == 8

    imf = "https://api.imf.org/external/sdmx/2.1/data/IFS?key=M.US.PCPI_IX&startPeriod=2020"
    assert redact_secrets(imf) == imf
    assert redact_secrets("monkey=1&keys=2&tokens=3") == "monkey=1&keys=2&tokens=3"


def test_configured_secret_values_are_masked_raw_and_encoded():
    secret = "s3cret/with+chars"
    text = f"https://example.org/v6/{secret}/latest and https://example.org/?x=s3cret%2Fwith%2Bchars"
    redacted = redact_secrets(text, [secret, None, "short"])
    assert "s3cret" not in redacted
    assert redacted.count("***") == 2
    # Values shorter than 8 characters are never treated as secrets.
    assert redact_secrets("short text", ["short"]) == "short text"


def test_user_facing_error_text_masks_credential_params():
    raw = "Client error '400 Bad Request' for url 'https://api.example.org/x?api_key=leakedvalue123&a=1'"
    message = QueryComplexityAnalyzer.format_error_message(raw, "test query")
    assert "leakedvalue123" not in message
