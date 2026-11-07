"""Behavioral snapshot of every provider-identity view BEFORE the registry
consolidation.

The golden values below were recorded from the hand-maintained maps as they
existed on main (d955e99) — they are NOT generated from the registry, so the
consolidation is provably behavior-preserving.  Any intended behavior change
must be listed explicitly in an ``_INTENDED_*`` allowlist; every other drift
fails.

Covered views:
- ``detect_explicit_provider_match`` (router's top authority; 24 call sites)
  over an English + Chinese corpus including multi-provider order cases and
  known false-positive traps.
- ``normalize_provider_name`` over every label providers emit plus aliases.
- ``IndicatorLookup._normalize_provider`` (indicators.db provider namespace,
  gates every FTS provider filter).
- ``add_provider_transparency`` note / no-note outcomes.
- The informational "which data sources" answer (lines and display order).
- User-visible provider labels (clarification option brackets, progress step).
- ``exact_title_search_inputs`` (provider-scoped exact-title variants).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.routing.unified_router import detect_explicit_provider_match
from backend.services.indicator_database import IndicatorLookup
from backend.utils.providers import normalize_provider_name


# ---------------------------------------------------------------------------
# detect_explicit_provider_match
# ---------------------------------------------------------------------------

GOLDEN_DETECTION = {
    # every directive x provider (English)
    'US GDP from FRED': ('FRED', 'from fred'),
    'unemployment using fred': ('FRED', 'using fred'),
    'CPI via fred': ('FRED', 'via fred'),
    'rates according to fred': ('FRED', 'according to fred'),
    'data from federal reserve': ('FRED', 'from federal reserve'),
    'series from st. louis fed': ('FRED', 'from st. louis fed'),
    'data using stlouisfed': ('FRED', 'using stlouisfed'),
    'China GDP from World Bank': ('WorldBank', 'from world bank'),
    'population using worldbank': ('WorldBank', 'using worldbank'),
    'GDP via world bank': ('WorldBank', 'via world bank'),
    'exports from comtrade': ('Comtrade', 'from comtrade'),
    'trade using un comtrade': ('Comtrade', 'using un comtrade'),
    'flows from united nations comtrade': ('Comtrade', 'from united nations comtrade'),
    'Canada CPI from statscan': ('StatsCan', 'from statscan'),
    'GDP from Statistics Canada': ('StatsCan', 'from statistics canada'),
    'jobs using stats canada': ('StatsCan', 'using stats canada'),
    'Canada CPI from statcan': None,
    'Canada CPI using statcan': None,
    'China GDP from IMF': ('IMF', 'from imf'),
    'debt from the imf': ('IMF', 'from the imf'),
    'growth according to the imf': ('IMF', 'according to the imf'),
    'reserves using international monetary fund': ('IMF', 'using international monetary fund'),
    'credit gap from BIS': ('BIS', 'from bis'),
    'property prices using bis': ('BIS', 'using bis'),
    'data from bank for international settlements': ('BIS', 'from bank for international settlements'),
    'Germany inflation from Eurostat': ('Eurostat', 'from eurostat'),
    'HICP according to eurostat': ('Eurostat', 'according to eurostat'),
    'unemployment via eurostat': ('Eurostat', 'via eurostat'),
    'GDP from OECD': ('OECD', 'from oecd'),
    'CLI using oecd': ('OECD', 'using oecd'),
    'data according to oecd': ('OECD', 'according to oecd'),
    'EUR rate from exchangerate': ('ExchangeRate', 'from exchangerate'),
    'USD to JPY using exchange rate api': ('ExchangeRate', 'using exchange rate api'),
    'rate from exchangerate-api': None,
    'data from exchangerate-api': None,
    'rates via exchange rate-api': ('ExchangeRate', 'via exchange rate-api'),
    'bitcoin from coingecko': ('CoinGecko', 'from coingecko'),
    'eth price using coin gecko': ('CoinGecko', 'using coin gecko'),
    'China PMI from ChinaMacro': None,
    'China M2 using china macro': None,
    'PMI via china-macro': None,
    'China CPI via ChinaMacro': None,
    'China PMI according to chinamacro': None,
    'PMI from China Macro': None,
    # articles
    'credit from the BIS': None,
    'GDP from the OECD': None,
    'inflation from the eurostat': None,
    'GDP from the world bank': ('WorldBank', 'world bank'),
    # bare tier
    'fred unemployment': ('FRED', 'fred'),
    'world bank data on India': ('WorldBank', 'world bank'),
    'wb data for Brazil': ('WorldBank', 'wb data'),
    'comtrade exports of China': ('Comtrade', 'comtrade'),
    'statscan housing starts': ('StatsCan', 'statscan'),
    'imf data on Japan': ('IMF', 'IMF (at start)'),
    'bis data credit': ('BIS', 'BIS (at start)'),
    'eurostat data on Spain': ('Eurostat', 'Eurostat (at start)'),
    'coingecko bitcoin': ('CoinGecko', 'coingecko'),
    'crypto prices today': ('CoinGecko', 'crypto prices'),
    'eu statistics on unemployment': ('Eurostat', 'eu statistics'),
    'european statistics GDP': ('Eurostat', 'european statistics'),
    'what did the fed do with rates': ('FRED', 'the fed'),
    'exchangerate for EUR': ('ExchangeRate', 'exchangerate'),
    'chinamacro PMI': None,
    'ChinaMacro China PMI': None,
    'China PMI (ChinaMacro)': None,
    # start-of-query
    'OECD GDP growth': ('OECD', 'OECD (at start)'),
    'IMF GDP forecast Japan': ('IMF', 'IMF (at start)'),
    'BIS credit to GDP': ('BIS', 'BIS (at start)'),
    'Eurostat unemployment Spain': ('Eurostat', 'Eurostat (at start)'),
    'OECD countries average unemployment': None,
    'IMF member countries debt': None,
    'OECD average inflation': None,
    # multi-provider / order (bare tier is first-match in registry order)
    'world bank data vs fred': ('FRED', 'fred'),
    'bitcoin from coingecko and fred': ('CoinGecko', 'from coingecko'),
    'fredenergy from CoinGecko': ('CoinGecko', 'from coingecko'),
    'FRED Energy price from CoinGecko': ('CoinGecko', 'from coingecko'),
    'federal reserve world bank': ('FRED', 'federal reserve'),
    'GDP from IMF then from World Bank': ('WorldBank', 'from world bank'),
    'statistics canada and world bank': ('WorldBank', 'world bank'),
    'comtrade or world bank exports': ('WorldBank', 'world bank'),
    # traps / negatives (must never become an absolute explicit route)
    'data from UN': None,
    'population data from UN': None,
    'exchange rate in China': None,
    'GDP using exchange rate of 2020': None,
    'GDP converted using exchange rate': None,
    'China macro outlook': None,
    'show me China macro indicators': None,
    'china macro data overview': None,
    'China macroeconomic outlook': None,
    'macro data for China': None,
    'US GDP': None,
    'imf': None,
    'bisque production': None,
    'fredrick county': None,
    'unemployment in Canada': None,
    # Chinese
    '中国GDP 来自IMF': None,
    '中国GDP 使用IMF数据': None,
    '德国通胀 来自Eurostat': None,
    '中国信贷缺口 来自BIS': None,
    '加拿大CPI 来自statcan': None,
    '用世界银行数据 中国GDP': None,
    '国际货币基金组织 中国GDP': None,
    '中国GDP 世界银行': None,
    '美国GDP 来自FRED': ('FRED', 'fred'),
    '中国PMI 来自ChinaMacro': None,
    '从chinamacro获取中国PMI': None,
    '经合组织国家平均失业率': None,
    '中国GDP数据IMF': None,
    '据IMF统计中国GDP': None,
    '根据国际清算银行 中国信贷': None,
    '数据来源：欧盟统计局 德国通胀': None,
    '加拿大CPI 来自加拿大统计局': None,
    '中国GDP 来自经合组织': None,
    '美联储利率': None,
    '中国宏观数据': None,
    '中国信用 数据': None,
}

# Intended behavior changes introduced by the ProviderSpec registry.  Every
# entry here is a deliberate fix; any other drift from GOLDEN_DETECTION fails.
_INTENDED_DETECTION_CHANGES: dict = {
    # ChinaMacro registered (directive tier: chinamacro / china macro /
    # china-macro; bare tier: the coined token 'chinamacro' only).
    'China PMI from ChinaMacro': ('ChinaMacro', 'from chinamacro'),
    'China M2 using china macro': ('ChinaMacro', 'using china macro'),
    'PMI via china-macro': ('ChinaMacro', 'via china-macro'),
    'China CPI via ChinaMacro': ('ChinaMacro', 'via chinamacro'),
    'China PMI according to chinamacro': ('ChinaMacro', 'according to chinamacro'),
    'PMI from China Macro': ('ChinaMacro', 'from china macro'),
    'chinamacro PMI': ('ChinaMacro', 'chinamacro'),
    'ChinaMacro China PMI': ('ChinaMacro', 'chinamacro'),
    'China PMI (ChinaMacro)': ('ChinaMacro', 'chinamacro'),
    '中国PMI 来自ChinaMacro': ('ChinaMacro', '来自chinamacro'),
    '从chinamacro获取中国PMI': ('ChinaMacro', '从chinamacro'),
    # Chinese source markers are directive syntax; official Chinese names
    # are identity (经合组织 / 美联储 directive-only).
    '中国GDP 来自IMF': ('IMF', '来自imf'),
    '中国GDP 使用IMF数据': ('IMF', '使用imf'),
    '德国通胀 来自Eurostat': ('Eurostat', '来自eurostat'),
    '中国信贷缺口 来自BIS': ('BIS', '来自bis'),
    '加拿大CPI 来自statcan': ('StatsCan', '来自statcan'),
    '用世界银行数据 中国GDP': ('WorldBank', '用世界银行'),
    '国际货币基金组织 中国GDP': ('IMF', '国际货币基金组织'),
    '中国GDP 世界银行': ('WorldBank', '世界银行'),
    '美国GDP 来自FRED': ('FRED', '来自fred'),  # was bare 'fred'; same provider
    '据IMF统计中国GDP': ('IMF', '据imf'),
    '根据国际清算银行 中国信贷': ('BIS', '根据国际清算银行'),
    '数据来源：欧盟统计局 德国通胀': ('Eurostat', '数据来源：欧盟统计局'),
    '加拿大CPI 来自加拿大统计局': ('StatsCan', '来自加拿大统计局'),
    '中国GDP 来自经合组织': ('OECD', '来自经合组织'),
    # Directive aliases normalize_provider_name already knew.
    'Canada CPI from statcan': ('StatsCan', 'from statcan'),
    'Canada CPI using statcan': ('StatsCan', 'using statcan'),
    'rate from exchangerate-api': ('ExchangeRate', 'from exchangerate-api'),
    'data from exchangerate-api': ('ExchangeRate', 'from exchangerate-api'),
    # Optional article in the directive grammar (structural).
    'credit from the BIS': ('BIS', 'from the bis'),
    'GDP from the OECD': ('OECD', 'from the oecd'),
    'inflation from the eurostat': ('Eurostat', 'from the eurostat'),
    'GDP from the world bank': ('WorldBank', 'from the world bank'),  # same provider
}


@pytest.mark.parametrize("query", list(GOLDEN_DETECTION))
def test_explicit_detection_matches_golden_except_intended(query: str) -> None:
    expected = _INTENDED_DETECTION_CHANGES.get(query, GOLDEN_DETECTION[query])
    assert detect_explicit_provider_match(query) == expected


# ---------------------------------------------------------------------------
# normalize_provider_name
# ---------------------------------------------------------------------------

GOLDEN_NORMALIZE = {
    'FRED': 'FRED',
    'fred': 'FRED',
    'FRED (Federal Reserve)': 'FRED',
    'Federal Reserve': 'FRED',
    'World Bank': 'WORLDBANK',
    'WorldBank': 'WORLDBANK',
    'WORLDBANK': 'WORLDBANK',
    'wb': 'WORLDBANK',
    'Statistics Canada': 'STATSCAN',
    'StatsCan': 'STATSCAN',
    'STATSCAN': 'STATSCAN',
    'statcan': 'STATSCAN',
    'Stats Canada': 'STATSCAN',
    'IMF': 'IMF',
    'International Monetary Fund': 'IMF',
    'BIS': 'BIS',
    'Bank for International Settlements': 'BIS',
    'Eurostat': 'EUROSTAT',
    'EUROSTAT': 'EUROSTAT',
    'OECD': 'OECD',
    'UN Comtrade': 'COMTRADE',
    'Comtrade': 'COMTRADE',
    'UN_COMTRADE': 'COMTRADE',
    'un': 'COMTRADE',
    'CoinGecko': 'COINGECKO',
    'Coin Gecko': 'COINGECKO',
    'ExchangeRate-API': 'EXCHANGERATE',
    'ExchangeRate': 'EXCHANGERATE',
    'exchange rate': 'EXCHANGERATE',
    'EXCHANGE_RATE': 'EXCHANGE_RATE',
    'ChinaMacro': 'CHINAMACRO',
    'China Macro': 'CHINAMACRO',
    'china_macro': 'CHINAMACRO',
    'ChinaMacro (NBS via EastMoney datacenter)': 'CHINAMACRO',
    'ChinaMacro (MOFCOM)': 'CHINAMACRO',
    'World Bank (WDI)': 'WORLDBANK',
    'not_available': 'NOT_AVAILABLE',
    'NOT_AVAILABLE': 'NOT_AVAILABLE',
    'Catalog': 'CATALOG',
    '': '',
    'AlphaVantage': 'ALPHAVANTAGE',
    'stlouisfed': 'STLOUISFED',
    'st. louis fed': 'ST. LOUIS FED',
    'united nations comtrade': 'UNITED NATIONS COMTRADE',
    'the imf': 'THE IMF',
    'china-macro': 'CHINA-MACRO',
    'exchange rate-api': 'EXCHANGE RATE-API',
}

_INTENDED_NORMALIZE_CHANGES: dict = {
    # Every explicit-request alias is now also a normalize alias (invariant:
    # explicit tiers ⊆ normalize tier).
    'stlouisfed': 'FRED',
    'st. louis fed': 'FRED',
    'united nations comtrade': 'COMTRADE',
    'the imf': 'IMF',
    'china-macro': 'CHINAMACRO',
    'exchange rate-api': 'EXCHANGERATE',
}


@pytest.mark.parametrize("label", list(GOLDEN_NORMALIZE))
def test_normalize_provider_name_matches_golden_except_intended(label: str) -> None:
    expected = _INTENDED_NORMALIZE_CHANGES.get(label, GOLDEN_NORMALIZE[label])
    assert normalize_provider_name(label) == expected


# ---------------------------------------------------------------------------
# IndicatorLookup._normalize_provider (indicators.db namespace)
# ---------------------------------------------------------------------------

GOLDEN_DB_NORMALIZE = {
    'FRED': 'FRED',
    'fred': 'FRED',
    'WorldBank': 'WorldBank',
    'WORLDBANK': 'WorldBank',
    'world bank': 'WorldBank',
    'World_Bank': 'WorldBank',
    'IMF': 'IMF',
    'imf': 'IMF',
    'Eurostat': 'Eurostat',
    'OECD': 'OECD',
    'StatsCan': 'StatsCan',
    'STATSCAN': 'StatsCan',
    'statistics canada': 'StatsCan',
    'Statistics-Canada': 'StatsCan',
    'BIS': 'BIS',
    'Comtrade': 'Comtrade',
    'COMTRADE': 'Comtrade',
    'CoinGecko': 'CoinGecko',
    'coin gecko': 'CoinGecko',
    'ExchangeRate': 'ExchangeRate',
    'exchange rate': 'ExchangeRate',
    'exchange-rate': 'ExchangeRate',
    'EXCHANGE_RATE': 'ExchangeRate',
    'ChinaMacro': 'ChinaMacro',
    'CHINAMACRO': 'ChinaMacro',
    'china macro': 'ChinaMacro',
    'china_macro': 'ChinaMacro',
    'Unknown': 'Unknown',
    'UN Comtrade': 'UN Comtrade',
    'un': 'un',
}

_INTENDED_DB_NORMALIZE_CHANGES: dict = {
    # Previously passed through unchanged and matched no indicators.db rows;
    # the derived lookup maps every registered provider label to its db name.
    'UN Comtrade': 'Comtrade',
    'un': 'Comtrade',
}


@pytest.mark.parametrize("label", list(GOLDEN_DB_NORMALIZE))
def test_db_provider_normalization_matches_golden_except_intended(label: str) -> None:
    lookup = IndicatorLookup(db=object())
    expected = _INTENDED_DB_NORMALIZE_CHANGES.get(label, GOLDEN_DB_NORMALIZE[label])
    assert lookup._normalize_provider(label) == expected


# ---------------------------------------------------------------------------
# add_provider_transparency
# ---------------------------------------------------------------------------

def _transparency(explicit: str, sources: list) -> str:
    from backend.services.query_helpers import add_provider_transparency

    svc = SimpleNamespace(_detect_explicit_provider=lambda _q: explicit)
    data = [
        SimpleNamespace(metadata=SimpleNamespace(source=source))
        for source in sources
    ]
    result = SimpleNamespace(data=data, intent=object(), message=None)
    add_provider_transparency(svc, result, "query")
    return result.message or ""


@pytest.mark.parametrize(
    ("explicit", "sources", "note_expected"),
    [
        ("BIS", ["World Bank"], True),
        ("IMF", ["World Bank"], True),
        ("OECD", ["Eurostat"], True),
        ("WorldBank", ["World Bank", "IMF"], False),
        ("StatsCan", ["Statistics Canada"], False),
        ("Comtrade", ["UN Comtrade"], False),
        ("ExchangeRate", ["ExchangeRate-API"], False),
        ("CoinGecko", ["CoinGecko"], False),
        ("Eurostat", ["Eurostat"], False),
        ("FRED", ["FRED"], False),
        ("IMF", ["IMF"], False),
    ],
)
def test_transparency_note_outcomes_preserved(explicit, sources, note_expected) -> None:
    message = _transparency(explicit, sources)
    assert ("did not have this data" in message) is note_expected
    if note_expected:
        for source in sources:
            assert source in message  # raw served labels stay in the note


# ---------------------------------------------------------------------------
# Informational data-sources answer
# ---------------------------------------------------------------------------

GOLDEN_DATA_SOURCE_LINES = [
    "- **World Bank** — Global development indicators — 16,000+ series covering 200+ countries",
    "- **Statistics Canada** — Canadian economic and social data — 40,000+ tables",
    "- **Eurostat** — European Union statistics — demographics, trade, economy across EU members",
    "- **IMF** — International financial data — balance of payments, exchange rates, fiscal indicators",
    "- **BIS** — Central bank statistics — credit, debt securities, property prices from the Bank for International Settlements",
    "- **OECD** — Cross-country economic data for OECD member nations",
    "- **UN Comtrade** — International trade data — bilateral import/export flows by commodity",
    "- **CoinGecko** — Cryptocurrency market data — prices, volumes, market caps",
    "- **ExchangeRate-API** — Foreign exchange rates — 160+ currencies with daily updates",
]


def test_data_sources_answer_keeps_existing_provider_lines() -> None:
    from backend.services.indicator_clarification import _handle_data_sources_query

    message = _handle_data_sources_query("what data sources do you have", "c1").message
    lines = message.splitlines()
    for line in GOLDEN_DATA_SOURCE_LINES:
        assert line in lines
    assert any(line.startswith("- **FRED** — US economic data") for line in lines)


# ---------------------------------------------------------------------------
# Data-sources answer ORDER (presentation must not follow routing order)
# ---------------------------------------------------------------------------

GOLDEN_DATA_SOURCE_ORDER = [
    "FRED", "World Bank", "Statistics Canada", "Eurostat", "IMF", "BIS", "OECD",
    "UN Comtrade", "CoinGecko", "ExchangeRate-API",
]
# ChinaMacro was missing from the answer; it is appended, nothing reorders.
_INTENDED_DATA_SOURCE_ORDER = [*GOLDEN_DATA_SOURCE_ORDER, "ChinaMacro"]


def test_data_sources_answer_keeps_existing_display_order() -> None:
    import re

    from backend.services.indicator_clarification import _handle_data_sources_query

    message = _handle_data_sources_query("what data sources do you have", "c1").message
    listed = re.findall(r"^- \*\*(.+?)\*\* — ", message, flags=re.MULTILINE)
    assert listed == _INTENDED_DATA_SOURCE_ORDER


# ---------------------------------------------------------------------------
# User-visible provider labels (clarification option brackets, progress step)
# ---------------------------------------------------------------------------

# Effective labels on main: the private maps plus their `.get(p, p)` fallback.
GOLDEN_OPTION_LABELS = {
    "OECD": "OECD", "FRED": "FRED", "WORLDBANK": "WorldBank", "COMTRADE": "Comtrade",
    "STATSCAN": "StatsCan", "IMF": "IMF", "BIS": "BIS", "EUROSTAT": "Eurostat",
    "EXCHANGERATE": "EXCHANGERATE", "COINGECKO": "COINGECKO", "CHINAMACRO": "ChinaMacro",
}
_INTENDED_OPTION_LABEL_CHANGES = {
    # Registry name form, like WorldBank/StatsCan; parse_indicator_option
    # normalizes either spelling back to the canonical provider.
    "EXCHANGERATE": "ExchangeRate",
    "COINGECKO": "CoinGecko",
}

GOLDEN_PROGRESS_LABELS = {
    "OECD": "OECD", "FRED": "Federal Reserve", "WORLDBANK": "World Bank",
    "COMTRADE": "UN Comtrade", "STATSCAN": "Statistics Canada", "IMF": "IMF",
    "BIS": "Bank for International Settlements", "EUROSTAT": "Eurostat",
    "EXCHANGERATE": "EXCHANGERATE", "COINGECKO": "CoinGecko", "CHINAMACRO": "CHINAMACRO",
}
_INTENDED_PROGRESS_LABEL_CHANGES = {
    # Previously fell through to the raw uppercase namespace.
    "EXCHANGERATE": "ExchangeRate-API",
    "CHINAMACRO": "ChinaMacro",
}


def test_option_labels_match_golden_except_intended() -> None:
    from backend.services.indicator_clarification import _PROVIDER_OPTION_LABELS

    expected = {**GOLDEN_OPTION_LABELS, **_INTENDED_OPTION_LABEL_CHANGES}
    assert {key: _PROVIDER_OPTION_LABELS.get(key, key) for key in expected} == expected


def test_progress_labels_match_golden_except_intended() -> None:
    from backend.utils.providers import provider_progress_labels

    labels = provider_progress_labels()
    expected = {**GOLDEN_PROGRESS_LABELS, **_INTENDED_PROGRESS_LABEL_CHANGES}
    assert {key: labels.get(key, key) for key in expected} == expected


# ---------------------------------------------------------------------------
# exact_title_search_inputs (provider-scoped exact-title variants)
# ---------------------------------------------------------------------------

# Recorded by running main's (d955e99) indicator_resolution.exact_title_search_inputs
# — loaded from `git show d955e99:backend/services/indicator_resolution.py` —
# NOT generated from the registry.
GOLDEN_TITLE_SEARCH_INPUTS = {
    ('US GDP from FRED', 'FRED'): [
        'US GDP from FRED',
        'GDP from FRED',
        'US GDP',
        'GDP',
    ],
    ('FRED Real GDP', 'FRED'): [
        'FRED Real GDP',
        'Real GDP',
        'GDP',
    ],
    ('[FRED] Unemployment Rate', 'FRED'): [
        '[FRED] Unemployment Rate',
        'FRED Unemployment Rate',
        'Unemployment Rate',
    ],
    ('Fredericksburg unemployment rate', 'FRED'): [
        'Fredericksburg unemployment rate',
        'ericksburg unemployment rate',
    ],
    ('Federal Reserve Bank of Philadelphia manufacturing index', 'FRED'): [
        'Federal Reserve Bank of Philadelphia manufacturing index',
    ],
    ('Unemployment Rate from federal reserve', 'FRED'): [
        'Unemployment Rate from federal reserve',
    ],
    ('GDP growth (annual %) from World Bank', 'WorldBank'): [
        'GDP growth (annual %) from World Bank',
        'GDP growth annual from World Bank',
        'GDP growth (annual %)',
        'growth (annual %) from World Bank, GDP',
        'GDP growth annual',
        'growth annual from World Bank, GDP',
        'GDP growth',
        'growth annual from World Bank GDP',
    ],
    ('[WorldBank] GDP growth (annual %)', 'WorldBank'): [
        '[WorldBank] GDP growth (annual %)',
        'WorldBank GDP growth annual',
        '[WorldBank] GDP growth',
        'GDP growth (annual %)',
        'GDP growth annual',
        'WorldBank GDP growth',
        'GDP growth',
    ],
    ('World Bank GDP per capita', 'WorldBank'): [
        'World Bank GDP per capita',
        'Bank GDP per capita',
        'GDP per capita',
    ],
    ('wbc index', 'WorldBank'): [
        'wbc index',
    ],
    ('WBL index score', 'WorldBank'): [
        'WBL index score',
    ],
    ('Labour force characteristics from Statistics Canada', 'StatsCan'): [
        'Labour force characteristics from Statistics Canada',
        'Labour force characteristics',
    ],
    ('[StatsCan] Consumer Price Index, monthly', 'StatsCan'): [
        '[StatsCan] Consumer Price Index, monthly',
        'StatsCan Consumer Price Index monthly',
        'Consumer Price Index, monthly',
        'Consumer Price Index monthly',
    ],
    ('Consumer Price Index from statcan', 'StatsCan'): [
        'Consumer Price Index from statcan',
    ],
    ('Unwrought aluminium exports', 'Comtrade'): [
        'Unwrought aluminium exports',
    ],
    ('Unmanufactured tobacco', 'Comtrade'): [
        'Unmanufactured tobacco',
    ],
    ('United States imports of crude oil', 'Comtrade'): [
        'United States imports of crude oil',
        'imports of crude oil',
        'crude oil',
    ],
    ('UN Comtrade crude oil exports', 'Comtrade'): [
        'UN Comtrade crude oil exports',
        'crude oil exports',
        'Comtrade crude oil exports, UN',
        'Comtrade crude oil exports UN',
        'crude oil exports, UN',
        'crude oil exports UN',
    ],
    ('Crude oil exports from comtrade', 'Comtrade'): [
        'Crude oil exports from comtrade',
        'Crude oil exports',
    ],
    ('Real GDP growth from IMF', 'IMF'): [
        'Real GDP growth from IMF',
        'GDP growth from IMF',
        'Real GDP growth',
        'GDP growth',
    ],
    ('IMF Real GDP growth', 'IMF'): [
        'IMF Real GDP growth',
        'Real GDP growth',
        'GDP growth',
    ],
    ('Real GDP growth from the IMF', 'IMF'): [
        'Real GDP growth from the IMF',
        'GDP growth from the IMF',
    ],
    ('Credit-to-GDP gaps from BIS', 'BIS'): [
        'Credit-to-GDP gaps from BIS',
        'Credit to GDP gaps from BIS',
        'Credit-to-GDP gaps',
        'Credit to GDP gaps',
    ],
    ('Bismuth exports', 'BIS'): [
        'Bismuth exports',
        'muth exports',
    ],
    ('HICP all items from eurostat', 'Eurostat'): [
        'HICP all items from eurostat',
        'HICP all items',
        'all items from eurostat, HICP',
        'all items from eurostat HICP',
    ],
    ('Eurostat HICP all items', 'Eurostat'): [
        'Eurostat HICP all items',
        'HICP all items',
    ],
    ('OECD composite leading indicator', 'OECD'): [
        'OECD composite leading indicator',
        'composite leading indicator',
    ],
    ('Composite leading indicator from oecd', 'OECD'): [
        'Composite leading indicator from oecd',
        'Composite leading indicator',
    ],
    ('exchange rate Canadian Dollar', 'ExchangeRate'): [
        'exchange rate Canadian Dollar',
        'Canadian Dollar',
    ],
    ('Canadian Dollar from exchangerate-api', 'ExchangeRate'): [
        'Canadian Dollar from exchangerate-api',
        'Canadian Dollar from exchangerate api',
        'Canadian Dollar',
    ],
    ('Exchange rate-API Euro', 'ExchangeRate'): [
        'Exchange rate-API Euro',
        'Exchange rate API Euro',
        '-API Euro',
        'Euro',
        'API Euro',
    ],
    ('Bitcoin from coingecko', 'CoinGecko'): [
        'Bitcoin from coingecko',
        'Bitcoin',
    ],
    ('coin gecko ethereum price', 'CoinGecko'): [
        'coin gecko ethereum price',
        'ethereum price',
        'coin gecko ethereum',
        'ethereum',
    ],
    ('China Manufacturing PMI from ChinaMacro', 'ChinaMacro'): [
        'China Manufacturing PMI from ChinaMacro',
        'Manufacturing PMI from ChinaMacro',
        'China Manufacturing PMI',
        'Manufacturing PMI',
    ],
    ('China macroeconomic climate index', 'ChinaMacro'): [
        'China macroeconomic climate index',
        'macroeconomic climate index',
    ],
    ('ChinaMacro China CPI YoY', 'ChinaMacro'): [
        'ChinaMacro China CPI YoY',
        'China CPI YoY',
        'CPI YoY',
    ],
}

# (added, removed) variants vs main.  Everything else — including the
# relative order of shared variants — must be unchanged.
_INTENDED_TITLE_INPUT_CHANGES: dict = {
    # ASCII aliases now need a token boundary in the prefix strip: a provider
    # name no longer chops the head off an ordinary word.
    ("Fredericksburg unemployment rate", "FRED"): ((), ("ericksburg unemployment rate",)),
    ("Bismuth exports", "BIS"): ((), ("muth exports",)),
    # Directive-tier identity forms now also unwrap a pasted title.
    ("Unemployment Rate from federal reserve", "FRED"): (("Unemployment Rate",), ()),
    ("Consumer Price Index from statcan", "StatsCan"): (("Consumer Price Index",), ()),
    ("Real GDP growth from the IMF", "IMF"): (("Real GDP growth", "GDP growth"), ()),
    # Additive only (the original input stays first and is searched as-is);
    # no FRED catalog title equals the stripped remainder.
    ("Federal Reserve Bank of Philadelphia manufacturing index", "FRED"): (
        ("Bank of Philadelphia manufacturing index",), (),
    ),
}


@pytest.mark.parametrize(("text", "provider"), list(GOLDEN_TITLE_SEARCH_INPUTS))
def test_exact_title_search_inputs_match_golden_except_intended(text, provider) -> None:
    from backend.services.indicator_resolution import exact_title_search_inputs

    golden = GOLDEN_TITLE_SEARCH_INPUTS[(text, provider)]
    added, removed = _INTENDED_TITLE_INPUT_CHANGES.get((text, provider), ((), ()))
    current = exact_title_search_inputs(text, provider)
    assert current[0] == text
    assert set(current) == (set(golden) - set(removed)) | set(added)
    shared_now = [variant for variant in current if variant in golden]
    shared_before = [variant for variant in golden if variant in current]
    assert shared_now == shared_before
