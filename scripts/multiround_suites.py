"""Named 10x10 multiround benchmark suites for conversation accuracy testing."""

from __future__ import annotations

from datetime import datetime


DEFAULT_SUITE_NAME = "baseline"


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


def _baseline_suite(now: datetime | None = None) -> dict[str, list[str]]:
    rotation_index = _rotation_index(now)

    return {
        "Test 1: GDP Deep Dive": [
            "US GDP",
            "Add China GDP",
            "Add Germany GDP",
            "Switch to per capita GDP",
            "Remove China",
            "Switch to GDP growth rate",
            "Show from IMF instead",
            "Change time range to 2015-2024",
            "Add Japan",
            _BASELINE_ROTATIONS["test1_r10"][rotation_index],
        ],
        "Test 2: Inflation Multi-Provider": [
            "Germany inflation rate",
            "Switch to France inflation",
            "Add Italy inflation",
            "Switch to US inflation from FRED",
            "Add UK inflation",
            "Change to 2020-2025",
            "Switch to World Bank data",
            "Show only US and UK",
            "Switch to core inflation",
            _BASELINE_ROTATIONS["test2_r10"][rotation_index],
        ],
        "Test 3: Crypto Cycling": [
            "Bitcoin price",
            "Switch to Ethereum price",
            "Switch to Solana price",
            "Back to Bitcoin price last 90 days",
            "Add Ethereum for comparison",
            "Switch to Cardano price",
            "Switch to Dogecoin price",
            "Back to Bitcoin price",
            "Change to last 30 days",
            _BASELINE_ROTATIONS["test3_r10"][rotation_index],
        ],
        "Test 4: Canada StatsCan Dimensions": [
            "Canada unemployment rate",
            "Show by province",
            "Just Ontario unemployment",
            "Switch to Alberta unemployment",
            "Switch to employment rate",
            "Show by age group",
            "Show 15-24 age group only",
            "Switch back to unemployment rate",
            "Show all provinces",
            "Change to 2020-2025",
        ],
        "Test 5: Trade Data Complex": [
            "US exports to China",
            "Switch to US imports from China",
            "Change partner to Japan",
            "Switch to trade balance US and Japan",
            "Change to Germany exports to China",
            "Add France exports to China",
            "Switch to 2020-2024",
            "Switch back to US exports",
            "Change partner to Canada",
            "Show total trade US and Canada",
        ],
        "Test 6: Exchange Rate Switching": [
            "USD to EUR exchange rate",
            "Switch to USD to GBP",
            "Switch to USD to JPY",
            "Switch to EUR to GBP",
            "Back to USD to EUR",
            "Change to last 30 days",
            "Switch to USD to CAD",
            "Switch to USD to CHF",
            "Back to USD to EUR",
            "Change to last year",
        ],
        "Test 7: BIS + IMF Financial": [
            "BIS credit to GDP ratio",
            "Narrow to US credit to GDP",
            "Add China credit to GDP",
            "Switch to IMF GDP growth rate",
            "Add Germany GDP growth",
            "Switch to current account balance",
            "Change to 2018-2024",
            "Remove Germany",
            "Switch to government debt to GDP",
            "Add Japan government debt",
        ],
        "Test 8: Eurostat Deep Dive": [
            "France unemployment rate from Eurostat",
            "Switch to Germany unemployment",
            "Add Spain unemployment",
            "Switch to inflation rate",
            "Switch to GDP growth rate",
            "Remove Spain",
            "Add Italy",
            "Switch to government debt to GDP",
            "Change to 2015-2024",
            "Switch back to unemployment rate",
        ],
        "Test 9: Mixed Provider Stress": [
            "US GDP from FRED",
            "Japan GDP from World Bank",
            "Germany GDP from Eurostat",
            "China GDP from IMF",
            "Canada GDP from Statistics Canada",
            "Switch all to GDP growth rate",
            "Change to 2020-2025",
            "Show only US and China",
            "Switch to per capita GDP",
            "Add Germany back",
        ],
        "Test 10: Indicator Variant Cycling": [
            "US real GDP",
            "Switch to nominal GDP",
            "Switch to GDP per capita",
            "Switch to GDP growth rate",
            "Switch to GDP deflator",
            "Back to real GDP",
            "Add UK real GDP",
            "Switch to PPP GDP",
            "Change to 2018-2024",
            "Switch to constant prices GDP",
        ],
    }


def _alternative_suite(_: datetime | None = None) -> dict[str, list[str]]:
    return {
        "Alt 1: GDP Provider Boundary Ladder": [
            "US GDP growth rate",
            "Add Canada GDP growth rate",
            "Switch to IMF",
            "Show only Canada",
            "Add Japan GDP growth rate",
            "Change to 2016-2024",
            "Switch to GDP per capita",
            "Switch back to GDP growth rate",
            "Show only Japan and United States",
            "Add Germany GDP growth rate",
        ],
        "Alt 2: Inflation Country Rotation": [
            "US inflation rate",
            "Add Canada inflation rate",
            "Add United Kingdom inflation rate",
            "Show only Canada and United Kingdom",
            "Switch to France inflation rate",
            "Add Germany inflation rate",
            "Change to 2019-2024",
            "Switch to World Bank data",
            "Show only France and Germany",
            "Switch back to United States inflation rate",
        ],
        "Alt 3: Trade Share Scope Control": [
            "Exports share of GDP in Japan",
            "Add South Korea exports share of GDP",
            "Add China exports share of GDP",
            "Show only China and Japan",
            "Switch to imports share of GDP",
            "Show only South Korea",
            "Add United States imports share of GDP",
            "Change to 2015-2024",
            "Add Germany imports share of GDP",
            "Show only United States and Germany",
        ],
        "Alt 4: StatsCan Province and Age": [
            "Canada employment rate",
            "Show by province",
            "Show only Ontario",
            "Switch to Alberta",
            "Show all provinces",
            "Show by age group",
            "Show only 25-54",
            "Switch back to employment rate",
            "Change to 2020-2024",
            "Show only Ontario again",
        ],
        "Alt 5: Exchange Rate Base Quote Stress": [
            "USD to EUR exchange rate",
            "Switch to USD to CAD",
            "Switch to EUR to GBP",
            "Show only the last 90 days",
            "Switch to USD to JPY",
            "Change to last year",
            "Switch to USD to CHF",
            "Switch back to USD to EUR",
            "Show only the last 30 days",
            "Switch to EUR to CAD",
        ],
        "Alt 6: Debt and External Balance Rotation": [
            "Japan government debt to GDP",
            "Add United States government debt to GDP",
            "Add Italy government debt to GDP",
            "Show only Japan and Italy",
            "Switch to current account balance",
            "Show only United States",
            "Add Germany current account balance",
            "Change to 2018-2024",
            "Switch back to government debt to GDP",
            "Add Canada government debt to GDP",
        ],
        "Alt 7: Eurostat Labour and Debt": [
            "Germany unemployment rate from Eurostat",
            "Add France unemployment rate",
            "Add Spain unemployment rate",
            "Show only Germany and Spain",
            "Switch to government debt to GDP",
            "Add Italy government debt to GDP",
            "Change to 2016-2024",
            "Switch back to unemployment rate",
            "Show only France",
            "Add Italy unemployment rate",
        ],
        "Alt 8: Crypto Asset Rotation": [
            "Bitcoin price",
            "Add Ethereum price",
            "Show only Bitcoin",
            "Switch to Solana price",
            "Add Dogecoin price",
            "Change to last 60 days",
            "Show only Ethereum and Dogecoin",
            "Switch back to Bitcoin price",
            "Change to last 30 days",
            "Add Ethereum again",
        ],
        "Alt 9: Bilateral Trade Direction": [
            "US exports to Canada",
            "Switch to US imports from Canada",
            "Change partner to Mexico",
            "Switch to trade balance US and Mexico",
            "Change partner to China",
            "Switch back to exports",
            "Change reporter to Germany",
            "Change partner to United States",
            "Switch to imports",
            "Show total trade Germany and United States",
        ],
        "Alt 10: Indicator Variant Ladder": [
            "US real GDP",
            "Add Canada real GDP",
            "Switch to nominal GDP",
            "Show only United States",
            "Add Germany nominal GDP",
            "Switch to GDP per capita",
            "Change to 2018-2024",
            "Switch to GDP growth rate",
            "Add Japan GDP growth rate",
            "Show only Germany and Japan",
        ],
    }


_SUITES: dict[str, dict[str, object]] = {
    "baseline": {
        "description": "Canonical Phase 6 multiround benchmark with rotating tail rounds.",
        "builder": _baseline_suite,
    },
    "alternative": {
        "description": (
            "Alternative 10x10 benchmark stressing provider persistence, scope narrowing, "
            "trade direction changes, decomposition carryover, and broader context rewrites."
        ),
        "builder": _alternative_suite,
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


def load_suite(name: str = DEFAULT_SUITE_NAME, now: datetime | None = None) -> dict[str, list[str]]:
    if name not in _SUITES:
        available = ", ".join(list_suite_names())
        raise KeyError(f"Unknown multiround suite {name!r}. Available suites: {available}")
    builder = _SUITES[name]["builder"]
    return builder(now)
