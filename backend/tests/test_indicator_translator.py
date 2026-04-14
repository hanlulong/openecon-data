from __future__ import annotations

from backend.services.indicator_translator import IndicatorTranslator


def test_translate_labor_force_participation_to_worldbank():
    translator = IndicatorTranslator()

    code, concept = translator.translate_indicator(
        "labor force participation rate",
        target_provider="WorldBank",
    )

    assert concept == "labor_force_participation"
    assert code == "SL.TLF.CACT.ZS"


def test_translate_labor_force_participation_to_fred():
    translator = IndicatorTranslator()

    code, concept = translator.translate_indicator(
        "workforce participation",
        target_provider="FRED",
    )

    assert concept == "labor_force_participation"
    assert code == "CIVPART"


def test_translate_forex_reserves_to_worldbank():
    translator = IndicatorTranslator()

    code, concept = translator.translate_indicator(
        "fx reserves",
        target_provider="WorldBank",
    )

    assert concept == "foreign_exchange_reserves"
    assert code == "FI.RES.TOTL.CD"


def test_translate_government_spending_to_worldbank():
    translator = IndicatorTranslator()

    code, concept = translator.translate_indicator(
        "government spending",
        target_provider="WorldBank",
    )

    assert concept == "government_expenditure"
    assert code == "NE.CON.GOVT.ZS"


def test_translate_renewable_energy_share_to_worldbank():
    translator = IndicatorTranslator()

    code, concept = translator.translate_indicator(
        "renewable energy share",
        target_provider="WorldBank",
    )

    assert concept == "renewable_energy"
    assert code == "EG.FEC.RNEW.ZS"


def test_translate_retail_sales_to_fred():
    translator = IndicatorTranslator()

    code, concept = translator.translate_indicator(
        "retail sales",
        target_provider="FRED",
    )

    assert concept == "retail_sales"
    assert code == "RSAFS"


def test_translate_industrial_production_to_fred():
    translator = IndicatorTranslator()

    code, concept = translator.translate_indicator(
        "industrial production",
        target_provider="FRED",
    )

    assert concept == "industrial_production"
    assert code == "INDPRO"


def test_translate_housing_starts_to_fred():
    translator = IndicatorTranslator()

    code, concept = translator.translate_indicator(
        "housing starts",
        target_provider="FRED",
    )

    assert concept == "housing_starts"
    assert code == "HOUST"


def test_translate_consumer_confidence_to_fred():
    translator = IndicatorTranslator()

    code, concept = translator.translate_indicator(
        "consumer confidence",
        target_provider="FRED",
    )

    assert concept == "consumer_confidence"
    assert code == "UMCSENT"


def test_translate_pmi_to_fred():
    translator = IndicatorTranslator()

    code, concept = translator.translate_indicator(
        "manufacturing pmi",
        target_provider="FRED",
    )

    assert concept == "pmi"
    assert code == "NAPM"


def test_translate_debt_service_ratio_to_bis():
    translator = IndicatorTranslator()

    code, concept = translator.translate_indicator(
        "debt service ratio",
        target_provider="BIS",
    )

    assert concept == "debt_service_ratio"
    assert code == "WS_DSR"


def test_translate_long_context_ppi_query_to_oecd():
    translator = IndicatorTranslator()

    code, concept = translator.translate_indicator(
        "producer price inflation trend in the us and germany",
        target_provider="OECD",
    )

    assert concept == "producer_price_inflation"
    assert code == "PPI"


def test_translate_trade_openness_query_to_worldbank():
    translator = IndicatorTranslator()

    code, concept = translator.translate_indicator(
        "trade openness ratio (exports plus imports to gdp) in small open economies",
        target_provider="WorldBank",
    )

    assert concept == "trade_openness"
    assert code == "NE.TRD.GNFS.ZS"


def test_translate_gdp_to_imf_uses_level_not_growth_code():
    translator = IndicatorTranslator()

    code, concept = translator.translate_indicator(
        "GDP",
        target_provider="IMF",
    )

    assert concept == "gdp"
    assert code == "NGDPD"


def test_translate_reer_query_to_worldbank_series_code():
    translator = IndicatorTranslator()

    code, concept = translator.translate_indicator(
        "reer trend for china and india from 2012 to 2024",
        target_provider="WorldBank",
    )

    assert concept == "real_effective_exchange_rate"
    assert code == "PX.REX.REER"


def test_translate_indicator_does_not_fuzzy_match_employment_to_unemployment():
    translator = IndicatorTranslator()

    code, concept = translator.translate_indicator(
        "employment rate",
        target_provider="IMF",
    )

    assert code is None
    assert concept is None
