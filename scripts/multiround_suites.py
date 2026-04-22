"""Named multiround benchmark suites with round-level semantic oracles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


DEFAULT_SUITE_NAME = "baseline"
SUITES_VERSION = 4


@dataclass(frozen=True)
class RoundOracle:
    """Expected semantic outcome for a single multiround turn.

    The oracle is intentionally semantic rather than provider-rigid by default.
    Providers can be constrained where the prompt explicitly asks for one or when
    the suite is intentionally stress-testing provider persistence.
    """

    accepted_providers: tuple[str, ...] = ()
    required_countries: tuple[str, ...] = ()
    forbidden_countries: tuple[str, ...] = ()
    accepted_series_ids: tuple[str, ...] = ()
    required_indicator_cues: tuple[str, ...] = ()
    forbidden_indicator_cues: tuple[str, ...] = ()
    accepted_frequencies: tuple[str, ...] = ()
    min_series_count: int = 1
    exact_series_count: int | None = None
    min_points_per_series: int = 0
    earliest_year_at_most: int | None = None
    latest_year_at_least: int | None = None
    expect_clarification: bool = False
    required_option_cues: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class RoundCase:
    query: str
    oracle: RoundOracle


_BASELINE_ROTATIONS = {
    "test1_r10": [
        "Switch to bar chart",
        "Show as scatter plot",
        "Show only top 3",
        "Convert to billions",
        "Show year-over-year change",
    ],
    "test2_r10": [
        "Change to monthly frequency",
        "Show core inflation only",
        "Convert to annualized rate",
        "Show 12-month moving average",
        "Compare to ECB target of 2%",
    ],
    "test3_r10": [
        "Add Ethereum again",
        "Show market cap instead",
        "Show 24h volume",
        "Compare price to all-time high",
        "Show in EUR",
    ],
}


def _rotation_index(now: datetime | None = None) -> int:
    timestamp = now or datetime.now()
    return timestamp.timetuple().tm_yday % 5


def _case(
    query: str,
    *,
    providers: tuple[str, ...] = (),
    countries: tuple[str, ...] = (),
    forbidden_countries: tuple[str, ...] = (),
    series_ids: tuple[str, ...] = (),
    cues: tuple[str, ...] = (),
    forbidden_cues: tuple[str, ...] = (),
    frequencies: tuple[str, ...] = (),
    min_series_count: int = 1,
    exact_series_count: int | None = None,
    min_points_per_series: int = 0,
    earliest_year_at_most: int | None = None,
    latest_year_at_least: int | None = None,
    expect_clarification: bool = False,
    option_cues: tuple[str, ...] = (),
    note: str = "",
) -> RoundCase:
    return RoundCase(
        query=query,
        oracle=RoundOracle(
            accepted_providers=providers,
            required_countries=countries,
            forbidden_countries=forbidden_countries,
            accepted_series_ids=series_ids,
            required_indicator_cues=cues,
            forbidden_indicator_cues=forbidden_cues,
            accepted_frequencies=frequencies,
            min_series_count=min_series_count,
            exact_series_count=exact_series_count,
            min_points_per_series=min_points_per_series,
            earliest_year_at_most=earliest_year_at_most,
            latest_year_at_least=latest_year_at_least,
            expect_clarification=expect_clarification,
            required_option_cues=option_cues,
            note=note,
        ),
    )


def _baseline_test1_round10(rotation_index: int) -> RoundCase:
    query = _BASELINE_ROTATIONS["test1_r10"][rotation_index]
    cues = ("gdp", "growth")
    exact_series_count = 3
    if query == "Convert to billions":
        cues = ("gdp",)
    return _case(
        query,
        providers=("IMF",),
        countries=("US", "DE", "JP"),
        cues=cues,
        exact_series_count=exact_series_count,
        note="Visualization / formatting follow-up should preserve the IMF GDP context and top-3 country scope.",
    )


def _baseline_test2_round10(rotation_index: int) -> RoundCase:
    query = _BASELINE_ROTATIONS["test2_r10"][rotation_index]
    cues = ("inflation",)
    if query in {"Show core inflation only", "Convert to annualized rate"}:
        cues = ("inflation",)
    elif query == "Compare to ECB target of 2%":
        cues = ("inflation",)
    return _case(
        query,
        providers=("WORLDBANK",),
        countries=("US", "GB"),
        cues=cues,
        exact_series_count=2,
        note="Formatting/benchmark follow-up should retain the active World Bank inflation comparison for the current US/UK pair.",
    )


def _baseline_test3_round10(rotation_index: int) -> RoundCase:
    query = _BASELINE_ROTATIONS["test3_r10"][rotation_index]
    exact = 1
    cues = ("bitcoin",)
    if query == "Add Ethereum again":
        exact = 2
    elif query == "Show market cap instead":
        cues = ("bitcoin", "market")
    elif query == "Show 24h volume":
        cues = ("bitcoin", "volume")
    elif query == "Compare price to all-time high":
        cues = ("bitcoin", "price")
    elif query == "Show in EUR":
        cues = ("bitcoin",)
    return _case(
        query,
        providers=("COINGECKO",),
        cues=cues,
        exact_series_count=exact,
        note="Rotation should preserve the active crypto context instead of drifting to unrelated assets or providers.",
    )


def _baseline_suite(now: datetime | None = None) -> dict[str, list[RoundCase]]:
    rotation_index = _rotation_index(now)
    return {
        "Test 1: GDP Deep Dive": [
            _case("US GDP", countries=("US",), cues=("gdp", "gross domestic"), exact_series_count=1),
            _case("Add China GDP", countries=("US", "CN"), cues=("gdp",), exact_series_count=2),
            _case("Add Germany GDP", countries=("US", "CN", "DE"), cues=("gdp",), exact_series_count=3),
            _case("Switch to per capita GDP", countries=("US", "CN", "DE"), cues=("gdp", "per capita"), exact_series_count=3),
            _case("Remove China", countries=("US", "DE"), forbidden_countries=("CN",), cues=("gdp", "per capita"), exact_series_count=2),
            _case("Switch to GDP growth rate", countries=("US", "DE"), cues=("gdp", "growth"), exact_series_count=2),
            _case("Show from IMF instead", providers=("IMF",), countries=("US", "DE"), cues=("gdp", "growth"), exact_series_count=2),
            _case("Change time range to 2015-2024", providers=("IMF",), countries=("US", "DE"), cues=("gdp", "growth"), exact_series_count=2),
            _case("Add Japan", providers=("IMF",), countries=("US", "DE", "JP"), cues=("gdp", "growth"), exact_series_count=3),
            _baseline_test1_round10(rotation_index),
        ],
        "Test 2: Inflation Multi-Provider": [
            _case("Germany inflation rate", countries=("DE",), cues=("inflation",), exact_series_count=1),
            _case("Switch to France inflation", countries=("FR",), cues=("inflation",), exact_series_count=1),
            _case("Add Italy inflation", countries=("FR", "IT"), cues=("inflation",), exact_series_count=2),
            _case("Switch to US inflation from FRED", providers=("FRED",), countries=("US",), cues=("inflation", "cpi"), exact_series_count=1),
            _case("Add UK inflation", countries=("US", "GB"), cues=("inflation",), exact_series_count=2),
            _case("Change to 2020-2025", countries=("US", "GB"), cues=("inflation",), exact_series_count=2),
            _case("Switch to World Bank data", providers=("WORLDBANK",), countries=("US", "GB"), cues=("inflation",), exact_series_count=2),
            _case("Show only US and UK", providers=("WORLDBANK",), countries=("US", "GB"), cues=("inflation",), exact_series_count=2),
            _case("Switch to core inflation", cues=("inflation",), min_series_count=1, note="Core inflation should remain an inflation-family result rather than drifting to unrelated price indicators."),
            _baseline_test2_round10(rotation_index),
        ],
        "Test 3: Crypto Cycling": [
            _case("Bitcoin price", providers=("COINGECKO",), cues=("bitcoin",), exact_series_count=1),
            _case("Switch to Ethereum price", providers=("COINGECKO",), cues=("ethereum",), exact_series_count=1),
            _case("Switch to Solana price", providers=("COINGECKO",), cues=("solana",), exact_series_count=1),
            _case("Back to Bitcoin price last 90 days", providers=("COINGECKO",), cues=("bitcoin",), exact_series_count=1),
            _case("Add Ethereum for comparison", providers=("COINGECKO",), exact_series_count=2, cues=("bitcoin", "ethereum")),
            _case("Switch to Cardano price", providers=("COINGECKO",), cues=("cardano",), exact_series_count=1),
            _case("Switch to Dogecoin price", providers=("COINGECKO",), cues=("dogecoin",), exact_series_count=1),
            _case("Back to Bitcoin price", providers=("COINGECKO",), cues=("bitcoin",), exact_series_count=1),
            _case("Change to last 30 days", providers=("COINGECKO",), cues=("bitcoin",), exact_series_count=1),
            _baseline_test3_round10(rotation_index),
        ],
        "Test 4: Canada StatsCan Dimensions": [
            _case("Canada unemployment rate", providers=("STATSCAN",), countries=("CA",), cues=("unemployment",), exact_series_count=1),
            _case("Show by province", providers=("STATSCAN",), countries=("CA",), cues=("unemployment",), min_series_count=2),
            _case("Just Ontario unemployment", providers=("STATSCAN",), countries=("CA",), cues=("unemployment", "ontario"), exact_series_count=1),
            _case("Switch to Alberta unemployment", providers=("STATSCAN",), countries=("CA",), cues=("unemployment", "alberta"), exact_series_count=1),
            _case("Switch to employment rate", providers=("STATSCAN",), countries=("CA",), cues=("employment",), exact_series_count=1),
            _case("Show by age group", providers=("STATSCAN",), countries=("CA",), cues=("employment",), min_series_count=2),
            _case("Show 15-24 age group only", providers=("STATSCAN",), countries=("CA",), cues=("employment", "15-24"), exact_series_count=1),
            _case("Switch back to unemployment rate", providers=("STATSCAN",), countries=("CA",), cues=("unemployment",), exact_series_count=1),
            _case("Show all provinces", providers=("STATSCAN",), countries=("CA",), cues=("unemployment",), min_series_count=2),
            _case("Change to 2020-2025", providers=("STATSCAN",), countries=("CA",), cues=("unemployment",), min_series_count=2),
        ],
        "Test 5: Trade Data Complex": [
            _case("US exports to China", providers=("COMTRADE",), cues=("export",), exact_series_count=1),
            _case("Switch to US imports from China", providers=("COMTRADE",), cues=("import",), exact_series_count=1),
            _case("Change partner to Japan", providers=("COMTRADE",), cues=("import",), exact_series_count=1),
            _case("Switch to trade balance US and Japan", providers=("COMTRADE",), cues=("trade balance",), exact_series_count=1),
            _case("Change to Germany exports to China", providers=("COMTRADE",), cues=("export",), exact_series_count=1),
            _case("Add France exports to China", providers=("COMTRADE",), cues=("export",), exact_series_count=2),
            _case("Switch to 2020-2024", providers=("COMTRADE",), cues=("export",), exact_series_count=2),
            _case("Switch back to US exports", providers=("COMTRADE",), cues=("export",), exact_series_count=1),
            _case("Change partner to Canada", providers=("COMTRADE",), cues=("export",), exact_series_count=1),
            _case("Show total trade US and Canada", providers=("COMTRADE",), cues=("trade",), exact_series_count=1),
        ],
        "Test 6: Exchange Rate Switching": [
            _case("USD to EUR exchange rate", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "eur"), exact_series_count=1),
            _case("Switch to USD to GBP", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "gbp"), exact_series_count=1),
            _case("Switch to USD to JPY", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "jpy"), exact_series_count=1),
            _case("Switch to EUR to GBP", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "gbp", "eur"), exact_series_count=1),
            _case("Back to USD to EUR", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "eur"), exact_series_count=1),
            _case("Change to last 30 days", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "eur"), exact_series_count=1),
            _case("Switch to USD to CAD", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "cad"), exact_series_count=1),
            _case("Switch to USD to CHF", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "chf"), exact_series_count=1),
            _case("Back to USD to EUR", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "eur"), exact_series_count=1),
            _case("Change to last year", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "eur"), exact_series_count=1),
        ],
        "Test 7: BIS + IMF Financial": [
            _case("BIS credit to GDP ratio", providers=("BIS",), cues=("credit", "gdp"), min_series_count=1),
            _case("Narrow to US credit to GDP", providers=("BIS",), countries=("US",), cues=("credit", "gdp"), exact_series_count=1),
            _case("Add China credit to GDP", providers=("BIS",), countries=("US", "CN"), cues=("credit", "gdp"), exact_series_count=2),
            _case("Switch to IMF GDP growth rate", providers=("IMF",), cues=("gdp", "growth"), min_series_count=1),
            _case("Add Germany GDP growth", providers=("IMF",), cues=("gdp", "growth"), min_series_count=2),
            _case("Switch to current account balance", providers=("IMF",), cues=("current account",), min_series_count=1),
            _case("Change to 2018-2024", providers=("IMF",), cues=("current account",), min_series_count=1),
            _case("Remove Germany", providers=("IMF",), cues=("current account",), min_series_count=1),
            _case("Switch to government debt to GDP", providers=("IMF", "WORLDBANK"), cues=("debt", "gdp"), min_series_count=1),
            _case("Add Japan government debt", providers=("IMF", "WORLDBANK"), cues=("debt", "gdp"), min_series_count=2),
        ],
        "Test 8: Eurostat Deep Dive": [
            _case("France unemployment rate from Eurostat", providers=("EUROSTAT",), countries=("FR",), cues=("unemployment",), exact_series_count=1),
            _case("Switch to Germany unemployment", providers=("EUROSTAT",), countries=("DE",), cues=("unemployment",), exact_series_count=1),
            _case("Add Spain unemployment", providers=("EUROSTAT",), countries=("DE", "ES"), cues=("unemployment",), exact_series_count=2),
            _case("Switch to inflation rate", providers=("EUROSTAT",), cues=("inflation",), min_series_count=1),
            _case("Switch to GDP growth rate", providers=("EUROSTAT",), cues=("gdp", "growth"), min_series_count=1),
            _case("Remove Spain", providers=("EUROSTAT",), cues=("gdp", "growth"), min_series_count=1),
            _case("Add Italy", providers=("EUROSTAT",), cues=("gdp", "growth"), min_series_count=2),
            _case("Switch to government debt to GDP", providers=("EUROSTAT",), cues=("debt", "gdp"), min_series_count=1),
            _case("Change to 2015-2024", providers=("EUROSTAT",), cues=("debt", "gdp"), min_series_count=1),
            _case("Switch back to unemployment rate", providers=("EUROSTAT",), cues=("unemployment",), min_series_count=1),
        ],
        "Test 9: Mixed Provider Stress": [
            _case("US GDP from FRED", providers=("FRED",), countries=("US",), cues=("gdp",), exact_series_count=1),
            _case("Japan GDP from World Bank", providers=("WORLDBANK",), countries=("JP",), cues=("gdp",), exact_series_count=1),
            _case("Germany GDP from Eurostat", providers=("EUROSTAT",), countries=("DE",), cues=("gdp",), exact_series_count=1),
            _case("China GDP from IMF", providers=("IMF",), countries=("CN",), cues=("gdp",), exact_series_count=1),
            _case("Canada GDP from Statistics Canada", providers=("STATSCAN",), countries=("CA",), cues=("gdp",), exact_series_count=1),
            _case("Switch all to GDP growth rate", countries=("US", "JP", "DE", "CN", "CA"), cues=("gdp", "growth"), exact_series_count=5),
            _case("Change to 2020-2025", countries=("US", "JP", "DE", "CN", "CA"), cues=("gdp", "growth"), exact_series_count=5),
            _case("Show only US and China", countries=("US", "CN"), forbidden_countries=("JP", "DE", "CA"), cues=("gdp", "growth"), exact_series_count=2),
            _case("Switch to per capita GDP", countries=("US", "CN"), cues=("gdp", "per capita"), exact_series_count=2),
            _case("Add Germany back", countries=("US", "CN", "DE"), cues=("gdp", "per capita"), exact_series_count=3),
        ],
        "Test 10: Indicator Variant Cycling": [
            _case("US real GDP", countries=("US",), cues=("gdp", "real"), exact_series_count=1),
            _case("Switch to nominal GDP", countries=("US",), cues=("gdp", "nominal"), exact_series_count=1),
            _case("Switch to GDP per capita", countries=("US",), cues=("gdp", "per capita"), exact_series_count=1),
            _case("Switch to GDP growth rate", countries=("US",), cues=("gdp", "growth"), exact_series_count=1),
            _case("Switch to GDP deflator", countries=("US",), cues=("gdp", "deflator"), exact_series_count=1),
            _case("Back to real GDP", countries=("US",), cues=("gdp", "real"), exact_series_count=1),
            _case("Add UK real GDP", countries=("US", "GB"), cues=("gdp", "real"), exact_series_count=2),
            _case("Switch to PPP GDP", countries=("US", "GB"), cues=("gdp", "ppp"), exact_series_count=2),
            _case("Change to 2018-2024", countries=("US", "GB"), cues=("gdp", "ppp"), exact_series_count=2),
            _case("Switch to constant prices GDP", countries=("US", "GB"), cues=("gdp", "constant"), exact_series_count=2),
        ],
    }


def _alternative_suite(_: datetime | None = None) -> dict[str, list[RoundCase]]:
    return {
        "Alt 1: GDP Provider Boundary Ladder": [
            _case("US GDP growth rate", countries=("US",), cues=("gdp", "growth"), exact_series_count=1),
            _case("Add Canada GDP growth rate", countries=("US", "CA"), cues=("gdp", "growth"), exact_series_count=2),
            _case("Switch to IMF", providers=("IMF",), countries=("US", "CA"), cues=("gdp", "growth"), exact_series_count=2),
            _case("Show only Canada", providers=("IMF",), countries=("CA",), forbidden_countries=("US",), cues=("gdp", "growth"), exact_series_count=1),
            _case("Add Japan GDP growth rate", providers=("IMF",), countries=("CA", "JP"), cues=("gdp", "growth"), exact_series_count=2),
            _case("Change to 2016-2024", providers=("IMF",), countries=("CA", "JP"), cues=("gdp", "growth"), exact_series_count=2),
            _case("Switch to GDP per capita", countries=("CA", "JP"), cues=("gdp", "per capita"), exact_series_count=2, note="Equivalent provider substitution is acceptable if IMF per-capita coverage is unavailable."),
            _case("Switch back to GDP growth rate", countries=("CA", "JP"), cues=("gdp", "growth"), exact_series_count=2, note="Equivalent provider substitution remains acceptable after the prior provider boundary."),
            _case("Show only Japan and United States", countries=("JP", "US"), forbidden_countries=("CA",), cues=("gdp", "growth"), exact_series_count=2, note="Preserve transformed GDP-growth semantics even if the provider is no longer IMF."),
            _case("Add Germany GDP growth rate", countries=("JP", "US", "DE"), cues=("gdp", "growth"), exact_series_count=3, note="Equivalent provider substitution remains acceptable while preserving growth semantics and country scope."),
        ],
        "Alt 2: Inflation Country Rotation": [
            _case("US inflation rate", countries=("US",), cues=("inflation",), exact_series_count=1),
            _case("Add Canada inflation rate", countries=("US", "CA"), cues=("inflation",), exact_series_count=2),
            _case("Add United Kingdom inflation rate", countries=("US", "CA", "GB"), cues=("inflation",), exact_series_count=3),
            _case("Show only Canada and United Kingdom", countries=("CA", "GB"), forbidden_countries=("US",), cues=("inflation",), exact_series_count=2),
            _case("Switch to France inflation rate", countries=("FR",), cues=("inflation",), exact_series_count=1),
            _case("Add Germany inflation rate", countries=("FR", "DE"), cues=("inflation",), exact_series_count=2),
            _case("Change to 2019-2024", countries=("FR", "DE"), cues=("inflation",), exact_series_count=2),
            _case("Switch to World Bank data", providers=("WORLDBANK",), countries=("FR", "DE"), cues=("inflation",), exact_series_count=2),
            _case("Show only France and Germany", providers=("WORLDBANK",), countries=("FR", "DE"), cues=("inflation",), exact_series_count=2),
            _case("Switch back to United States inflation rate", providers=("WORLDBANK",), countries=("US",), cues=("inflation",), exact_series_count=1),
        ],
        "Alt 3: Trade Share Scope Control": [
            _case("Exports share of GDP in Japan", cues=("export", "gdp"), countries=("JP",), exact_series_count=1),
            _case("Add South Korea exports share of GDP", cues=("export", "gdp"), countries=("JP", "KR"), exact_series_count=2),
            _case("Add China exports share of GDP", cues=("export", "gdp"), countries=("JP", "KR", "CN"), exact_series_count=3),
            _case("Show only China and Japan", cues=("export", "gdp"), countries=("CN", "JP"), forbidden_countries=("KR",), exact_series_count=2),
            _case("Switch to imports share of GDP", cues=("import", "gdp"), countries=("CN", "JP"), exact_series_count=2),
            _case("Show only South Korea", cues=("import", "gdp"), countries=("KR",), exact_series_count=1),
            _case("Add United States imports share of GDP", cues=("import", "gdp"), countries=("KR", "US"), exact_series_count=2),
            _case("Change to 2015-2024", cues=("import", "gdp"), countries=("KR", "US"), exact_series_count=2),
            _case("Add Germany imports share of GDP", cues=("import", "gdp"), countries=("KR", "US", "DE"), exact_series_count=3),
            _case("Show only United States and Germany", cues=("import", "gdp"), countries=("US", "DE"), forbidden_countries=("KR",), exact_series_count=2),
        ],
        "Alt 4: StatsCan Province and Age": [
            _case("Canada employment rate", countries=("CA",), cues=("employment",), exact_series_count=1, note="Equivalent provider substitution is acceptable for the initial national employment-rate turn."),
            _case("Show by province", providers=("STATSCAN",), countries=("CA",), cues=("employment",), min_series_count=2),
            _case("Show only Ontario", providers=("STATSCAN",), countries=("CA",), cues=("employment", "ontario"), exact_series_count=1),
            _case("Switch to Alberta", providers=("STATSCAN",), countries=("CA",), cues=("employment", "alberta"), exact_series_count=1),
            _case("Show all provinces", providers=("STATSCAN",), countries=("CA",), cues=("employment",), min_series_count=2),
            _case("Show by age group", providers=("STATSCAN",), countries=("CA",), cues=("employment",), min_series_count=2),
            _case("Show only 25-54", providers=("STATSCAN",), countries=("CA",), cues=("employment", "25-54"), exact_series_count=1),
            _case("Switch back to employment rate", providers=("STATSCAN",), countries=("CA",), cues=("employment",), exact_series_count=1),
            _case("Change to 2020-2024", providers=("STATSCAN",), countries=("CA",), cues=("employment",), exact_series_count=1),
            _case("Show only Ontario again", providers=("STATSCAN",), countries=("CA",), cues=("employment", "ontario"), exact_series_count=1),
        ],
        "Alt 5: Exchange Rate Base Quote Stress": [
            _case("USD to EUR exchange rate", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "eur"), exact_series_count=1),
            _case("Switch to USD to CAD", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "cad"), exact_series_count=1),
            _case("Switch to EUR to GBP", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "eur", "gbp"), exact_series_count=1),
            _case("Show only the last 90 days", providers=("EXCHANGERATE", "FRED"), cues=("exchange",), exact_series_count=1),
            _case("Switch to USD to JPY", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "jpy"), exact_series_count=1),
            _case("Change to last year", providers=("EXCHANGERATE", "FRED"), cues=("exchange",), exact_series_count=1),
            _case("Switch to USD to CHF", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "chf"), exact_series_count=1),
            _case("Switch back to USD to EUR", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "eur"), exact_series_count=1),
            _case("Show only the last 30 days", providers=("EXCHANGERATE", "FRED"), cues=("exchange",), exact_series_count=1),
            _case("Switch to EUR to CAD", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "eur", "cad"), exact_series_count=1),
        ],
        "Alt 6: Debt and External Balance Rotation": [
            _case("Japan government debt to GDP", cues=("debt", "gdp"), countries=("JP",), exact_series_count=1),
            _case("Add United States government debt to GDP", cues=("debt", "gdp"), countries=("JP", "US"), exact_series_count=2),
            _case("Add Italy government debt to GDP", cues=("debt", "gdp"), countries=("JP", "US", "IT"), exact_series_count=3),
            _case("Show only Japan and Italy", cues=("debt", "gdp"), countries=("JP", "IT"), forbidden_countries=("US",), exact_series_count=2),
            _case("Switch to current account balance", cues=("current account",), countries=("JP", "IT"), exact_series_count=2),
            _case("Show only United States", cues=("current account",), countries=("US",), exact_series_count=1),
            _case("Add Germany current account balance", cues=("current account",), countries=("US", "DE"), exact_series_count=2),
            _case("Change to 2018-2024", cues=("current account",), countries=("US", "DE"), exact_series_count=2),
            _case("Switch back to government debt to GDP", cues=("debt", "gdp"), countries=("US", "DE"), exact_series_count=2),
            _case("Add Canada government debt to GDP", cues=("debt", "gdp"), countries=("US", "DE", "CA"), exact_series_count=3),
        ],
        "Alt 7: Eurostat Labour and Debt": [
            _case("Germany unemployment rate from Eurostat", providers=("EUROSTAT",), countries=("DE",), cues=("unemployment",), exact_series_count=1),
            _case("Add France unemployment rate", providers=("EUROSTAT",), countries=("DE", "FR"), cues=("unemployment",), exact_series_count=2),
            _case("Add Spain unemployment rate", providers=("EUROSTAT",), countries=("DE", "FR", "ES"), cues=("unemployment",), exact_series_count=3),
            _case("Show only Germany and Spain", providers=("EUROSTAT",), countries=("DE", "ES"), forbidden_countries=("FR",), cues=("unemployment",), exact_series_count=2),
            _case("Switch to government debt to GDP", providers=("EUROSTAT",), cues=("debt", "gdp"), countries=("DE", "ES"), exact_series_count=2),
            _case("Add Italy government debt to GDP", providers=("EUROSTAT",), countries=("DE", "ES", "IT"), cues=("debt", "gdp"), exact_series_count=3),
            _case("Change to 2016-2024", providers=("EUROSTAT",), cues=("debt", "gdp"), countries=("DE", "ES", "IT"), exact_series_count=3),
            _case("Switch back to unemployment rate", providers=("EUROSTAT",), cues=("unemployment",), countries=("DE", "ES", "IT"), exact_series_count=3),
            _case("Show only France", providers=("EUROSTAT",), countries=("FR",), cues=("unemployment",), exact_series_count=1),
            _case("Add Italy unemployment rate", providers=("EUROSTAT",), countries=("FR", "IT"), cues=("unemployment",), exact_series_count=2),
        ],
        "Alt 8: Crypto Asset Rotation": [
            _case("Bitcoin price", providers=("COINGECKO",), cues=("bitcoin",), exact_series_count=1),
            _case("Add Ethereum price", providers=("COINGECKO",), cues=("bitcoin", "ethereum"), exact_series_count=2),
            _case("Show only Bitcoin", providers=("COINGECKO",), cues=("bitcoin",), exact_series_count=1),
            _case("Switch to Solana price", providers=("COINGECKO",), cues=("solana",), exact_series_count=1),
            _case("Add Dogecoin price", providers=("COINGECKO",), cues=("solana", "dogecoin"), exact_series_count=2),
            _case("Change to last 60 days", providers=("COINGECKO",), min_series_count=2),
            _case("Show only Ethereum and Dogecoin", providers=("COINGECKO",), cues=("ethereum", "dogecoin"), exact_series_count=2),
            _case("Switch back to Bitcoin price", providers=("COINGECKO",), cues=("bitcoin",), exact_series_count=1),
            _case("Change to last 30 days", providers=("COINGECKO",), cues=("bitcoin",), exact_series_count=1),
            _case("Add Ethereum again", providers=("COINGECKO",), cues=("bitcoin", "ethereum"), exact_series_count=2),
        ],
        "Alt 9: Bilateral Trade Direction": [
            _case("US exports to Canada", providers=("COMTRADE",), cues=("export",), exact_series_count=1),
            _case("Switch to US imports from Canada", providers=("COMTRADE",), cues=("import",), exact_series_count=1),
            _case("Change partner to Mexico", providers=("COMTRADE",), cues=("import",), exact_series_count=1),
            _case("Switch to trade balance US and Mexico", providers=("COMTRADE",), cues=("trade balance",), exact_series_count=1),
            _case("Change partner to China", providers=("COMTRADE",), cues=("trade balance",), exact_series_count=1),
            _case("Switch back to exports", providers=("COMTRADE",), cues=("export",), exact_series_count=1),
            _case("Change reporter to Germany", providers=("COMTRADE",), cues=("export",), exact_series_count=1),
            _case("Change partner to United States", providers=("COMTRADE",), cues=("export",), exact_series_count=1),
            _case("Switch to imports", providers=("COMTRADE",), cues=("import",), exact_series_count=1),
            _case("Show total trade Germany and United States", providers=("COMTRADE",), cues=("trade",), exact_series_count=1),
        ],
        "Alt 10: Indicator Variant Ladder": [
            _case("US real GDP", countries=("US",), cues=("gdp", "real"), exact_series_count=1),
            _case("Add Canada real GDP", countries=("US", "CA"), cues=("gdp", "real"), exact_series_count=2),
            _case("Switch to nominal GDP", countries=("US", "CA"), cues=("gdp", "nominal"), exact_series_count=2),
            _case("Show only United States", countries=("US",), forbidden_countries=("CA",), cues=("gdp", "nominal"), exact_series_count=1),
            _case("Add Germany nominal GDP", countries=("US", "DE"), cues=("gdp", "nominal"), exact_series_count=2),
            _case("Switch to GDP per capita", countries=("US", "DE"), cues=("gdp", "per capita"), exact_series_count=2),
            _case("Change to 2018-2024", countries=("US", "DE"), cues=("gdp", "per capita"), exact_series_count=2),
            _case("Switch to GDP growth rate", countries=("US", "DE"), cues=("gdp", "growth"), exact_series_count=2),
            _case("Add Japan GDP growth rate", countries=("US", "DE", "JP"), cues=("gdp", "growth"), exact_series_count=3),
            _case("Show only Germany and Japan", countries=("DE", "JP"), forbidden_countries=("US",), cues=("gdp", "growth"), exact_series_count=2),
        ],
    }


def _regression_suite(now: datetime | None = None) -> dict[str, list[RoundCase]]:
    timestamp = now or datetime.now()
    earliest_year_for_last_20 = timestamp.year - 19
    latest_year_for_recent_data = timestamp.year - 1

    return {
        "Reg 1: StatsCan Sex Follow-up Horizon": [
            _case(
                "unemployment in Canada by sex",
                providers=("STATSCAN",),
                countries=("CA",),
                cues=("unemployment", "male", "female"),
                frequencies=("monthly",),
                exact_series_count=2,
                min_points_per_series=24,
            ),
            _case(
                "last 20 years",
                providers=("STATSCAN",),
                countries=("CA",),
                cues=("male", "female"),
                frequencies=("monthly",),
                exact_series_count=2,
                min_points_per_series=200,
                earliest_year_at_most=earliest_year_for_last_20,
                latest_year_at_least=latest_year_for_recent_data,
                note="This regression must prove that the follow-up actually expands the time horizon, not just preserves the sex split.",
            ),
            _case(
                "show only females",
                providers=("STATSCAN",),
                countries=("CA",),
                cues=("females",),
                forbidden_cues=("male",),
                frequencies=("monthly",),
                exact_series_count=1,
                min_points_per_series=200,
                earliest_year_at_most=earliest_year_for_last_20,
                latest_year_at_least=latest_year_for_recent_data,
            ),
        ],
        "Reg 2: StatsCan Sex Single-turn Horizon": [
            _case(
                "unemployment in Canada by sex in last 20 years",
                providers=("STATSCAN",),
                countries=("CA",),
                cues=("unemployment", "male", "female"),
                frequencies=("monthly",),
                exact_series_count=2,
                min_points_per_series=200,
                earliest_year_at_most=earliest_year_for_last_20,
                latest_year_at_least=latest_year_for_recent_data,
                note="The fused single-turn variant should be equivalent to the multiround follow-up chain.",
            ),
            _case(
                "show only males",
                providers=("STATSCAN",),
                countries=("CA",),
                cues=("males",),
                forbidden_cues=("female",),
                frequencies=("monthly",),
                exact_series_count=1,
                min_points_per_series=200,
                earliest_year_at_most=earliest_year_for_last_20,
                latest_year_at_least=latest_year_for_recent_data,
            ),
        ],
        "Reg 3: StatsCan Province Single-turn Horizon": [
            _case(
                "give me unemployment by province in Canada in last 20 years",
                providers=("STATSCAN",),
                countries=("CA",),
                cues=("unemployment",),
                frequencies=("monthly",),
                exact_series_count=10,
                min_points_per_series=200,
                earliest_year_at_most=earliest_year_for_last_20,
                latest_year_at_least=latest_year_for_recent_data,
                note="Province decomposition must keep the full requested time horizon instead of collapsing to the latest 20 monthly points.",
            ),
        ],
    }


def _hardening_suite(now: datetime | None = None) -> dict[str, list[RoundCase]]:
    timestamp = now or datetime.now()
    latest_recent_year = timestamp.year - 1

    return {
        "Hard 1: IMF Chart Persistence": [
            _case("US GDP", countries=("US",), cues=("gdp",), exact_series_count=1),
            _case("Add Germany GDP", countries=("US", "DE"), cues=("gdp",), exact_series_count=2),
            _case("Switch to GDP growth rate", countries=("US", "DE"), cues=("gdp", "growth"), exact_series_count=2),
            _case("Show from IMF instead", providers=("IMF",), countries=("US", "DE"), cues=("gdp", "growth"), exact_series_count=2),
            _case("Add Japan", providers=("IMF",), countries=("US", "DE", "JP"), cues=("gdp", "growth"), exact_series_count=3),
            _case(
                "Convert to billions",
                providers=("IMF",),
                countries=("US", "DE", "JP"),
                cues=("gdp",),
                exact_series_count=3,
                min_points_per_series=5,
                note="Rate-like formatting follow-up must preserve the active IMF GDP-growth chain without rerouting.",
            ),
        ],
        "Hard 2: Crypto ATH Stability": [
            _case("Bitcoin price", providers=("COINGECKO",), cues=("bitcoin",), exact_series_count=1),
            _case("Change to last 30 days", providers=("COINGECKO",), cues=("bitcoin",), exact_series_count=1, min_points_per_series=20),
            _case(
                "Compare price to all-time high",
                providers=("COINGECKO",),
                cues=("bitcoin", "price"),
                exact_series_count=1,
                min_points_per_series=20,
                latest_year_at_least=latest_recent_year,
                note="ATH comparison must not duplicate the active BTC series or drift providers.",
            ),
        ],
        "Hard 3: Trade Scope Backfill": [
            _case("Exports share of GDP in Japan", cues=("export", "gdp"), countries=("JP",), exact_series_count=1, min_points_per_series=5),
            _case("Add South Korea exports share of GDP", cues=("export", "gdp"), countries=("JP", "KR"), exact_series_count=2, min_points_per_series=5),
            _case("Show only South Korea", cues=("export", "gdp"), countries=("KR",), exact_series_count=1, min_points_per_series=5),
            _case("Switch to imports share of GDP", cues=("import", "gdp"), countries=("KR",), exact_series_count=1, min_points_per_series=5),
            _case("Add United States imports share of GDP", cues=("import", "gdp"), countries=("KR", "US"), exact_series_count=2, min_points_per_series=5),
            _case("Change to 2015-2024", cues=("import", "gdp"), countries=("KR", "US"), exact_series_count=2, min_points_per_series=5),
            _case("Add Germany imports share of GDP", cues=("import", "gdp"), countries=("KR", "US", "DE"), exact_series_count=3, min_points_per_series=5),
        ],
        "Hard 4: Canada Decomposition Lock": [
            _case(
                "Canada employment rate",
                countries=("CA",),
                cues=("employment",),
                exact_series_count=1,
                note="The initial national turn may use an equivalent provider, but decomposition follow-ups must lock to StatsCan.",
            ),
            _case("Show by province", providers=("STATSCAN",), countries=("CA",), cues=("employment",), min_series_count=2, min_points_per_series=12),
            _case("Show only Ontario", providers=("STATSCAN",), countries=("CA",), cues=("employment", "ontario"), exact_series_count=1, min_points_per_series=12),
            _case("Switch to Alberta", providers=("STATSCAN",), countries=("CA",), cues=("employment", "alberta"), exact_series_count=1, min_points_per_series=12),
            _case("Show all provinces", providers=("STATSCAN",), countries=("CA",), cues=("employment",), min_series_count=2, min_points_per_series=12),
        ],
        "Hard 5: Exchange Historical Persistence": [
            _case("USD to EUR exchange rate", providers=("EXCHANGERATE",), cues=("exchange", "eur"), exact_series_count=1),
            _case("Switch to USD to CAD", providers=("EXCHANGERATE",), cues=("exchange", "cad"), exact_series_count=1),
            _case("Switch to EUR to GBP", providers=("EXCHANGERATE",), cues=("exchange", "eur", "gbp"), exact_series_count=1),
            _case(
                "Show only the last 90 days",
                providers=("FRED",),
                cues=("exchange", "eur", "gbp"),
                exact_series_count=1,
                frequencies=("daily",),
                min_points_per_series=40,
                latest_year_at_least=latest_recent_year,
                note="Historical follow-up after pair switching must yield a dense daily series rather than clarifying or returning current-only data.",
            ),
            _case("Switch to USD to JPY", providers=("EXCHANGERATE", "FRED"), cues=("exchange", "jpy"), exact_series_count=1),
            _case(
                "Change to last year",
                providers=("FRED",),
                cues=("exchange", "jpy"),
                exact_series_count=1,
                frequencies=("daily",),
                min_points_per_series=150,
                latest_year_at_least=latest_recent_year,
            ),
        ],
    }


_SUITES: dict[str, dict[str, object]] = {
    "baseline": {
        "description": "Canonical Phase 6 multiround benchmark with round-level semantic oracles.",
        "builder": _baseline_suite,
    },
    "alternative": {
        "description": (
            "Alternative 10x10 benchmark stressing provider persistence, scope narrowing, "
            "trade direction changes, decomposition carryover, and broader context rewrites."
        ),
        "builder": _alternative_suite,
    },
    "regression": {
        "description": (
            "Targeted multiround regressions for previously broken follow-up behaviors, "
            "including StatsCan decomposition + timeframe retention."
        ),
        "builder": _regression_suite,
    },
    "hardening": {
        "description": (
            "Stricter certification suite for framework-level persistence fixes, with denser "
            "historical/time-range expectations and targeted provider-lock regressions."
        ),
        "builder": _hardening_suite,
    },
}


def list_suite_names() -> list[str]:
    return list(_SUITES.keys())


def list_suite_descriptions() -> dict[str, str]:
    return {name: str(meta["description"]) for name, meta in _SUITES.items()}


def get_suite_description(name: str) -> str:
    if name not in _SUITES:
        available = ", ".join(list_suite_names())
        raise KeyError(f"Unknown multiround suite {name!r}. Available suites: {available}")
    return str(_SUITES[name]["description"])


def load_suite(name: str = DEFAULT_SUITE_NAME, now: datetime | None = None) -> dict[str, list[RoundCase]]:
    if name not in _SUITES:
        available = ", ".join(list_suite_names())
        raise KeyError(f"Unknown multiround suite {name!r}. Available suites: {available}")
    builder = _SUITES[name]["builder"]
    return builder(now)
