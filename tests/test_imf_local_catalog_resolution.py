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


def test_imf_resolve_indicator_does_not_finalize_from_local_catalog_variants() -> None:
    query = "US Current Account Services Credit Balance of Payments Goods and Services Royalties and License Fees from IMF"

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

    provider = IMFProvider(metadata_search_service=None)

    with patch("backend.services.indicator_database.get_indicator_lookup", return_value=lookup):
        try:
            run(provider._resolve_indicator_code(query))  # pylint: disable=protected-access
        except DataNotAvailableError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("Expected local catalog candidates to require metadata/LLM adjudication")


def test_imf_resolve_indicator_fails_closed_without_metadata_service() -> None:
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
        try:
            run(provider._resolve_indicator_code(query))  # pylint: disable=protected-access
        except DataNotAvailableError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("Expected local catalog candidates to require metadata adjudication")


def test_imf_resolve_indicator_rejects_generic_query_without_metadata() -> None:
    provider = IMFProvider(metadata_search_service=None)

    try:
        run(provider._resolve_indicator_code("GDP growth rate"))  # pylint: disable=protected-access
    except DataNotAvailableError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("Expected generic IMF phrases to fail without metadata adjudication")


def test_imf_local_catalog_real_variant_requires_metadata_adjudication() -> None:
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
        try:
            run(provider._resolve_indicator_code(query))  # pylint: disable=protected-access
        except DataNotAvailableError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("Expected local catalog ranking to require metadata adjudication")


def test_imf_local_catalog_ordering_requires_metadata_adjudication() -> None:
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
        try:
            run(provider._resolve_indicator_code(query))  # pylint: disable=protected-access
        except DataNotAvailableError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("Expected local catalog ordering to require metadata adjudication")


def test_imf_local_catalog_country_prefixed_candidates_require_metadata() -> None:
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
        try:
            run(provider._resolve_indicator_code(query))  # pylint: disable=protected-access
        except DataNotAvailableError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError("Expected country-prefixed local candidates to require metadata adjudication")


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


def test_imf_resolve_indicator_preserves_exact_short_weo_code() -> None:
    provider = IMFProvider(metadata_search_service=None)

    code, label = run(provider._resolve_indicator_code("BCA"))  # pylint: disable=protected-access

    assert code == "BCA"
    assert label is not None
    assert "current account" in label.lower()


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

    examples = [
        (
            "LER_ISIC31_C_PT",
            "Labor Markets, Employment, Employment Rate, By International Standard Industrial Classification of All Economic Activities (ISIC) Rev. 3.1, Mining and quarrying, Percent",
        ),
        (
            "LEW_ISIC4_G_USD",
            "Labor Markets, Employment, Wages, By International Standard Industrial Classification of All Economic Activities (ISIC) Rev. 4, Wholesale and retail trade; repair of motor vehicles and motorcycles, US Dollars",
        ),
        (
            "LE_PLP_RATE",
            "Labour Market, Employment to Population Ratio, Rate",
        ),
    ]

    for code, label in examples:
        hint = provider._likely_dataset_family_hint(  # pylint: disable=protected-access
            code,
            label,
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
