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
