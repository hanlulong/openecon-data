"""Exercise Comtrade authentication through the real HTTP/retry boundary."""

import asyncio

import httpx
import pytest

from backend.providers.comtrade import ComtradeProvider
from backend.utils.retry import DataNotAvailableError


KEY = "test-comtrade-subscription-not-real"
PAYLOAD = {
    "data": [{
        "period": "2023",
        "flowDesc": "Exports",
        "cmdCode": "TOTAL",
        "reporterDesc": "Canada",
        "primaryValue": 1234567890,
    }],
}


@pytest.fixture(autouse=True)
def no_rate_limit_wait(monkeypatch):
    async def ready(_provider):
        pass

    monkeypatch.setattr("backend.providers.base.wait_for_provider", ready)


async def fetch(provider, client):
    return await provider._fetch_single_reporter_data(
        client, "Canada", "0", "TOTAL", "X", "2023", "A",
    )


@pytest.mark.parametrize("api_key", [KEY, None])
def test_auth_is_request_local_and_urls_are_credential_free(api_key):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=PAYLOAD)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=True,
            headers={"X-Shared-Header": "preserved"},
        ) as client:
            before = dict(client.headers)
            result = await fetch(ComtradeProvider(api_key), client)
            assert dict(client.headers) == before
            await client.get("https://unrelated.example/data")
            return result

    result = asyncio.run(run())
    assert len(requests) == 2
    assert requests[0].headers.get("Ocp-Apim-Subscription-Key") == api_key
    assert "Ocp-Apim-Subscription-Key" not in requests[1].headers
    assert requests[0].url.params["reporterCode"] == "124"
    assert requests[0].headers["X-Shared-Header"] == "preserved"
    assert len(result) == 1
    assert [(point.date, point.value) for point in result[0].data] == [
        ("2023-01-01", 1234567890),
    ]
    assert result[0].metadata.source == "UN Comtrade"
    for url in [str(requests[0].url), result[0].metadata.apiUrl]:
        assert KEY not in url
        assert "subscription-key" not in url.lower()


@pytest.mark.parametrize("status", [302, 401])
def test_redirects_and_invalid_credentials_fail_without_forwarding(status):
    requests = []

    def respond(request):
        requests.append(request)
        # Even a data-shaped body must not turn a redirect/error into success.
        return httpx.Response(
            status, json=PAYLOAD,
            headers={"Location": "https://unrelated.example/collect"},
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=True,
        ) as client:
            with pytest.raises(DataNotAvailableError, match=f"HTTP {status}") as error:
                await fetch(ComtradeProvider(KEY), client)
            assert KEY not in str(error.value)

    asyncio.run(run())
    assert len(requests) == 1
    assert requests[0].url.host == "comtradeapi.un.org"
    assert KEY not in str(requests[0].url)
