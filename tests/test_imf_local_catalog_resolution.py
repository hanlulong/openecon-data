from __future__ import annotations

import sys
import types
from unittest.mock import patch

from backend.tests.utils import run


if "pybreaker" not in sys.modules:
    fake_pybreaker = types.ModuleType("pybreaker")

    class _CircuitBreaker:  # pragma: no cover - test shim only
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    fake_pybreaker.CircuitBreaker = _CircuitBreaker
    sys.modules["pybreaker"] = fake_pybreaker

from backend.providers.imf import IMFProvider
from backend.utils.retry import DataNotAvailableError


class _Lookup:
    def __init__(self, mapping: dict[str, list[dict]]):
        self.mapping = {
            " ".join(str(key).lower().split()): value
            for key, value in mapping.items()
        }

    def search(self, text, provider=None, limit=5):
        normalized = " ".join(str(text).lower().split())
        if provider != "IMF":
            return []
        return self.mapping.get(normalized, [])


def test_imf_resolve_indicator_uses_local_catalog_variants_when_metadata_search_is_empty() -> None:
    query = "US Current Account Services Credit Balance of Payments Goods and Services Royalties and License Fees from IMF"

    class _Metadata:
        async def discover_indicator(self, provider: str, indicator_name: str, search_results):
            self.provider = provider
            self.indicator_name = indicator_name
            self.search_results = search_results
            return {
                "code": search_results[0]["code"],
                "name": search_results[0]["name"],
                "confidence": 0.92,
            }

    lookup = _Lookup(
        {
            "current account services credit balance of payments goods and services royalties and license fees": [
                {
                    "code": "BXSRL_USD",
                    "name": "Balance of Payments, Current Account, Goods and Services, Services, Royalties and License Fees, Credit, US Dollars",
                    "description": "Royalties and license fees credit, US dollars",
                }
            ]
        }
    )

    metadata = _Metadata()
    provider = IMFProvider(metadata_search_service=metadata)

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        code, label = run(provider._resolve_indicator_code(query))  # pylint: disable=protected-access

    assert code == "BXSRL_USD"
    assert label is not None
    assert metadata.provider == "IMF"
    assert metadata.indicator_name == query
    assert metadata.search_results[0]["code"] == "BXSRL_USD"


def test_imf_resolve_indicator_uses_local_catalog_without_metadata_service() -> None:
    query = "Germany Employment Employment Rate Percent Labor Markets Mining and quarrying from IMF"
    provider = IMFProvider(metadata_search_service=None)

    lookup = _Lookup(
        {
            "employment employment rate percent labor markets mining and quarrying": [
                {
                    "code": "LER_ISIC31_C_PT",
                    "name": "Labor Markets, Employment, Employment Rate, By International Standard Industrial Classification of All Economic Activities (ISIC) Rev. 3.1, Mining and quarrying, Percent",
                    "description": "Employment rate mining and quarrying percent",
                }
            ]
        }
    )

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        code, label = run(provider._resolve_indicator_code(query))  # pylint: disable=protected-access

    assert code == "LER_ISIC31_C_PT"
    assert label is not None
    assert "employment" in label.lower()


def test_imf_resolve_indicator_keeps_generic_queries_on_translator_path() -> None:
    provider = IMFProvider(metadata_search_service=None)

    code, label = run(provider._resolve_indicator_code("GDP growth rate"))  # pylint: disable=protected-access

    assert code == "NGDP_RPCH"
    assert label is not None
    assert "gdp" in label.lower()


def test_imf_local_catalog_prefers_real_variant_for_real_query() -> None:
    query = "Germany Real Gross Value Added Miscellaneous machinery and equipment manufacturing from IMF"
    provider = IMFProvider(metadata_search_service=None)

    lookup = _Lookup(
        {
            "gross value added miscellaneous machinery and equipment manufacturing": [
                {
                    "code": "NGDPVA_ISIC4_C28_XDC",
                    "name": "National Accounts, Gross Value Added, Nominal, By International Standard Industrial Classification of All Economic Activities (ISIC) Rev. 4, Miscellaneous machinery and equipment manufacturing, National Currency",
                    "description": "Nominal gross value added",
                },
                {
                    "code": "NGDPVA_R_ISIC4_C28_XDC",
                    "name": "National Accounts, Gross Value Added, Real, By International Standard Industrial Classification of All Economic Activities (ISIC) Rev. 4, Miscellaneous machinery and equipment manufacturing, National Currency",
                    "description": "Real gross value added",
                },
            ]
        }
    )

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        code, label = run(provider._resolve_indicator_code(query))  # pylint: disable=protected-access

    assert code == "NGDPVA_R_ISIC4_C28_XDC"
    assert label is not None
    assert "real" in label.lower()


def test_imf_local_catalog_keeps_earlier_candidate_when_no_stronger_signal_exists() -> None:
    query = "US Current Account Services Credit Balance of Payments Goods and Services Royalties and License Fees from IMF"
    provider = IMFProvider(metadata_search_service=None)

    lookup = _Lookup(
        {
            "current account services credit balance of payments goods and services royalties and license fees": [
                {
                    "code": "BXSRL_USD",
                    "name": "Balance of Payments, Current Account, Goods and Services, Services, Royalties and License Fees, Credit, US Dollars",
                    "description": "Royalties and license fees credit, US dollars",
                },
                {
                    "code": "BXSRL_EUR",
                    "name": "Balance of Payments, Current Account, Goods and Services, Services, Royalties and License Fees, Credit, Euros",
                    "description": "Royalties and license fees credit, euros",
                },
            ]
        }
    )

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        code, label = run(provider._resolve_indicator_code(query))  # pylint: disable=protected-access

    assert code == "BXSRL_USD"
    assert label is not None


def test_imf_local_catalog_penalizes_wrong_country_prefixed_candidates() -> None:
    query = "US Current Account Services Balance of Payments Goods and Services Insurance and pension services from IMF"
    provider = IMFProvider(metadata_search_service=None)

    lookup = _Lookup(
        {
            "current account services balance of payments goods and services insurance and pension services": [
                {
                    "code": "TLS_BP_BCASMIN_USD",
                    "name": "Timor-Leste Definition, Balance of Payments, Balance of Payments Country Presentation, Current Account Exclude other primary income, Current Account, Goods and Services, Services, Imports, Insurance & pension services, US Dollars",
                    "description": "Timor-Leste imports insurance and pension services",
                },
                {
                    "code": "BMS_BP6_USD",
                    "name": "Balance of Payments, Current Account, Goods and Services, Services, Insurance and pension services, Debit [BPM6], US Dollars",
                    "description": "Insurance and pension services debit, US dollars",
                },
            ]
        }
    )

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        code, label = run(provider._resolve_indicator_code(query))  # pylint: disable=protected-access

    assert code == "BMS_BP6_USD"
    assert label is not None


def test_imf_execution_family_classifier_marks_indicator_codes_non_datamapper() -> None:
    provider = IMFProvider(metadata_search_service=None)

    with patch.object(provider, "_indicator_catalog_entry", return_value={"category": "INDICATOR"}):
        family = provider._classify_execution_family("BXSRLO_USD")  # pylint: disable=protected-access

    assert family == "NON_DATAMAPPER_INDICATOR"


def test_imf_execution_family_classifier_keeps_weo_codes_on_datamapper_path() -> None:
    provider = IMFProvider(metadata_search_service=None)

    with patch.object(provider, "_indicator_catalog_entry", return_value={"category": "WEO"}):
        family = provider._classify_execution_family("NGDP_RPCH")  # pylint: disable=protected-access

    assert family == "DATAMAPPER_WEO"


def test_imf_fetch_fails_explicitly_for_non_datamapper_indicator_family() -> None:
    provider = IMFProvider(metadata_search_service=None)

    with patch.object(
        provider,
        "_resolve_indicator_code",
        return_value=(
            "LER_ISIC31_C_PT",
            "Labor Markets, Employment, Employment Rate, By International Standard Industrial Classification of All Economic Activities (ISIC) Rev. 3.1, Mining and quarrying, Percent",
        ),
    ), patch.object(provider, "_indicator_catalog_entry", return_value={"category": "INDICATOR"}):
        try:
            run(provider.fetch_indicator("ignored", country="USA"))  # pylint: disable=protected-access
        except DataNotAvailableError as exc:
            message = str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("Expected DataNotAvailableError")

    assert "non-DataMapper IMF family" in message
    assert "LER_ISIC31_C_PT" in message


def test_imf_dataset_family_hint_maps_bop_like_codes() -> None:
    provider = IMFProvider(metadata_search_service=None)

    hint = provider._likely_dataset_family_hint(  # pylint: disable=protected-access
        "BXSRLO_USD",
        "Balance of Payments, Current Account, Goods and Services, Services, Royalties and License Fees, Other Royalties and License Fees, Credit, US Dollars",
    )

    assert hint == "IMF.STA:BOP"


def test_imf_dataset_family_hint_maps_labor_market_codes() -> None:
    provider = IMFProvider(metadata_search_service=None)

    hint = provider._likely_dataset_family_hint(  # pylint: disable=protected-access
        "LER_ISIC31_C_PT",
        "Labor Markets, Employment, Employment Rate, By International Standard Industrial Classification of All Economic Activities (ISIC) Rev. 3.1, Mining and quarrying, Percent",
    )

    assert hint == "IMF.STA:LS"


def test_imf_dataset_family_hint_maps_national_accounts_codes() -> None:
    provider = IMFProvider(metadata_search_service=None)

    hint = provider._likely_dataset_family_hint(  # pylint: disable=protected-access
        "NGDPVA_R_ISIC4_C28_XDC",
        "National Accounts, Gross Value Added, Real, By International Standard Industrial Classification of All Economic Activities (ISIC) Rev. 4, Miscellaneous machinery and equipment manufacturing, National Currency",
    )

    assert hint == "IMF.STA:NA_MAIN"


def test_imf_non_datamapper_failure_includes_dataset_hint() -> None:
    provider = IMFProvider(metadata_search_service=None)

    with patch.object(
        provider,
        "_resolve_indicator_code",
        return_value=(
            "NGDPVA_R_ISIC4_C28_XDC",
            "National Accounts, Gross Value Added, Real, By International Standard Industrial Classification of All Economic Activities (ISIC) Rev. 4, Miscellaneous machinery and equipment manufacturing, National Currency",
        ),
    ), patch.object(provider, "_indicator_catalog_entry", return_value={"category": "INDICATOR"}):
        try:
            run(provider.fetch_indicator("ignored", country="USA"))  # pylint: disable=protected-access
        except DataNotAvailableError as exc:
            message = str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("Expected DataNotAvailableError")

    assert "Likely next dataset family: IMF.STA:NA_MAIN" in message
