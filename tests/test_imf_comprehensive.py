"""IMF provider no-shortcut contract tests.

These tests intentionally avoid the retired natural-language indicator map.
Exact IMF codes remain mechanical provider-native inputs; natural-language
phrases must require provider metadata/LLM adjudication.
"""

import pytest

from backend.utils.retry import DataNotAvailableError
from backend.providers.imf import IMFProvider


class _MetadataSearch:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, str]] = []
        self.discovery_calls: list[tuple[str, str, list[dict[str, str]]]] = []

    async def search_with_sdmx_fallback(self, *, provider: str, indicator: str) -> list[dict[str, str]]:
        self.search_calls.append((provider, indicator))
        return [{"code": "NGDP_RPCH", "name": "Gross domestic product, constant prices"}]

    async def discover_indicator(
        self,
        *,
        provider: str,
        indicator_name: str,
        search_results: list[dict[str, str]],
    ) -> dict[str, str]:
        self.discovery_calls.append((provider, indicator_name, search_results))
        return {"code": "NGDP_RPCH", "name": "Gross domestic product, constant prices"}


def test_imf_country_mappings_remain_mechanical() -> None:
    provider = IMFProvider(metadata_search_service=None)

    assert provider._country_code("Greece") == "GRC"  # pylint: disable=protected-access
    assert provider._country_code("Netherlands") == "NLD"  # pylint: disable=protected-access
    assert provider._country_code("Czechia") == "CZE"  # pylint: disable=protected-access
    assert provider._country_code("South Korea") == "KOR"  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_imf_exact_indicator_codes_pass_through_catalog_validation() -> None:
    provider = IMFProvider(metadata_search_service=None)

    code, label = await provider._resolve_indicator_code("NGDPD")  # pylint: disable=protected-access

    assert code == "NGDPD"
    assert label is not None
    assert "gdp" in label.lower()


@pytest.mark.asyncio
async def test_imf_natural_language_requires_metadata_adjudication() -> None:
    provider = IMFProvider(metadata_search_service=None)

    with pytest.raises(DataNotAvailableError, match="Provide the official IMF code"):
        await provider._resolve_indicator_code("GDP growth rate")  # pylint: disable=protected-access


@pytest.mark.asyncio
async def test_imf_natural_language_uses_metadata_search_not_local_map() -> None:
    metadata = _MetadataSearch()
    provider = IMFProvider(metadata_search_service=metadata)

    code, label = await provider._resolve_indicator_code("GDP growth rate")  # pylint: disable=protected-access

    assert code == "NGDP_RPCH"
    assert label == "Gross domestic product, constant prices"
    assert metadata.search_calls == [("IMF", "GDP growth rate")]
    assert metadata.discovery_calls[0][0:2] == ("IMF", "GDP growth rate")
