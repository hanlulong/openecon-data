"""
Parameter validation service
Validates query parameters before fetching data to prevent wrong answers
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from ..models import ParsedIntent

logger = logging.getLogger(__name__)


class ParameterValidator:
    """Validates parameters and checks if data is likely to exist"""

    # Default time periods by provider (to reduce clarification requests)
    DEFAULT_LOOKBACK_YEARS = {
        "FRED": 5,
        "STATSCAN": 5,
        "STATISTICS CANADA": 5,
        "WORLDBANK": 10,
        "WORLD BANK": 10,
        "IMF": 5,
        "OECD": 5,
        "BIS": 5,
        "EUROSTAT": 5,
        "COMTRADE": 5,
        "EXCHANGERATE": 1,  # Recent exchange rates
        "EXCHANGE_RATE": 1,
        "FX": 1,
        "COINGECKO": 1,  # Recent crypto prices
        "COIN GECKO": 1,
    }

    # Example indicator names for error messages (actual resolution via selector/provider metadata)
    FRED_EXAMPLE_INDICATORS = ['GDP', 'Unemployment', 'Inflation', 'Retail Sales', 'Housing Starts']
    STATSCAN_EXAMPLE_INDICATORS = ['GDP', 'Unemployment', 'Inflation', 'CPI', 'Population']

    @staticmethod
    def apply_default_time_periods(intent: ParsedIntent) -> None:
        """
        Apply default time periods to intent parameters BEFORE validation.

        This eliminates many clarification requests by providing sensible defaults
        for queries like "US GDP" or "Canada inflation" without explicit time periods.

        Args:
            intent: The ParsedIntent to enrich with default time periods
        """
        if not intent.parameters:
            intent.parameters = {}

        # Only apply defaults if neither startDate nor endDate is specified
        if not intent.parameters.get("startDate") and not intent.parameters.get("endDate"):
            provider = intent.apiProvider.upper()

            # Provider-native exact title/code requests already identify a
            # concrete public series/table.  Applying a generic "last N years"
            # window can erase valid historical/survey-only tables (for example
            # Statistics Canada 2002 school-health products) even though the user
            # supplied the exact public title.  Let the provider return its
            # native latest/available observations unless the user explicitly
            # requested a date range.
            if (
                intent.parameters.get("__exact_indicator_title_match")
                or intent.parameters.get("__exact_provider_code_match")
            ):
                logger.debug(
                    "Skipping default time period for exact provider-native %s request",
                    provider,
                )
                return

            # IMPORTANT: Do NOT apply default dates to providers that only support current data:
            # - ExchangeRate: Free tier only provides current exchange rates
            # - CoinGecko: Requires explicit days parameter for historical data
            if provider in {"EXCHANGERATE", "EXCHANGE_RATE", "FX", "COINGECKO", "COIN GECKO"}:
                logger.debug(
                    "Skipping default time period for %s (current data only provider)",
                    provider
                )
                return

            # Get default lookback years for this provider
            lookback_years = ParameterValidator.DEFAULT_LOOKBACK_YEARS.get(provider, 5)

            # Calculate start and end dates
            today = datetime.now(timezone.utc).date()
            start_date = today - timedelta(days=365 * lookback_years)

            # Set default time period
            intent.parameters["startDate"] = start_date.isoformat()
            intent.parameters["endDate"] = today.isoformat()

            logger.debug(
                "Applied default time period (%d years) to %s query: %s to %s",
                lookback_years,
                provider,
                intent.parameters["startDate"],
                intent.parameters["endDate"]
            )

    @staticmethod
    def validate_intent(intent: ParsedIntent) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """
        Validate parsed intent before fetching data

        Returns:
            (is_valid, error_message, suggestions)
        """
        provider = intent.apiProvider.upper()
        params = intent.parameters or {}

        # Validate based on provider
        if provider == "FRED":
            return ParameterValidator._validate_fred(intent, params)
        elif provider in {"STATSCAN", "STATISTICS CANADA"}:
            return ParameterValidator._validate_statscan(intent, params)
        elif provider == "COMTRADE":
            return ParameterValidator._validate_comtrade(intent, params)
        elif provider in {"WORLDBANK", "WORLD BANK"}:
            return ParameterValidator._validate_worldbank(intent, params)
        elif provider in {"IMF", "OECD", "BIS", "EUROSTAT"}:
            # These providers are flexible and can discover indicators via metadata search
            # Only basic validation: must have indicator
            if not intent.indicators:
                return False, "Please specify an economic indicator (GDP, inflation, unemployment, etc.)", {
                    'suggestion': 'Try: "GDP for Canada", "inflation in France", "unemployment in Germany"'
                }
            return True, None, None
        else:
            # Other providers - assume valid (ExchangeRate, CoinGecko, etc.)
            return True, None, None

    @staticmethod
    def _validate_fred(intent: ParsedIntent, params: Dict) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """Validate FRED query parameters.

        NOTE: This is now more lenient. FRED provider can:
        - Resolve indicators via retrieval + LLM adjudication/provider metadata
        - Try unknown indicators as-is (may work if valid FRED code)
        - Dynamic FRED API search as fallback

        Only reject if we have absolutely no indicator or series.
        """
        series_id = params.get('seriesId')
        indicator = params.get('indicator') or (intent.indicators[0] if intent.indicators else None)

        # If we have neither series ID nor indicator, we can't proceed
        if not series_id and not indicator:
            return False, "FRED query requires a series ID or indicator name", {
                'suggestion': 'Try specifying a common indicator (GDP, unemployment, inflation)',
                'common_indicators': ParameterValidator.FRED_EXAMPLE_INDICATORS
            }

        # If we have an indicator, we can proceed - FRED provider will map it or try as-is
        if indicator:
            return True, None, None

        # If we have series ID, validate it
        # Allow any series ID - FRED API will validate when we call it
        return True, None, None

    @staticmethod
    def _validate_statscan(intent: ParsedIntent, params: Dict) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """Validate StatsCan query parameters.

        NOTE: More lenient validation. StatsCan provider can:
        - Map indicator names to vector IDs via _SELECTED_VECTOR_CODES
        - Discover unknown indicators via metadata search (SDMX-first)
        - Try unknown vectors as-is (API will validate)

        Only reject if we have absolutely no vector ID or indicator.
        """
        vector_id = params.get('vectorId')
        indicator = params.get('indicator')

        # Check intent.indicators as fallback if indicator not in params
        if not indicator and intent.indicators:
            indicator = intent.indicators[0]

        # Need either vector ID or indicator
        if not vector_id and not indicator:
            return False, "StatsCan query requires a vector ID or indicator name", {
                'suggestion': 'Try a common indicator (GDP, UNEMPLOYMENT, HOUSING_STARTS)',
                'common_indicators': ParameterValidator.STATSCAN_EXAMPLE_INDICATORS
            }

        # If we have anything (indicator or vector), we can proceed
        # StatsCan provider and metadata search will handle discovery
        return True, None, None

    @staticmethod
    def _validate_comtrade(intent: ParsedIntent, params: Dict) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """Validate Comtrade query parameters"""
        # Support both single reporter and multiple reporters
        reporter = params.get('reporter') or params.get('country')
        reporters = params.get('reporters') or params.get('countries')

        if not reporter and not reporters:
            return False, "Comtrade query requires a reporter country or countries", {
                'suggestion': 'Specify which country/countries you want trade data for',
                'example': 'Try: "China exports to US" or "US and China imports from 2020 to 2024"'
            }

        # Check for trade balance queries - these often fail
        indicators = [ind.lower() for ind in intent.indicators]
        if any('balance' in ind for ind in indicators):
            # Trade balance has known issues - warn user
            return True, None, {
                'warning': 'Trade balance queries may not work for all country pairs',
                'suggestion': 'If this fails, query exports and imports separately or use Pro Mode',
                'fallback': 'Pro Mode can fetch both and calculate the balance'
            }

        return True, None, None

    @staticmethod
    def _validate_worldbank(intent: ParsedIntent, params: Dict) -> Tuple[bool, Optional[str], Optional[Dict]]:
        """Validate World Bank query parameters"""
        country = params.get('country')
        countries = params.get('countries')

        if not country and not countries:
            if params.get("__exact_indicator_title_match") or params.get("__exact_provider_code_match"):
                # Provider-native exact-title/code queries can be valid without
                # a user-specified country.  The WorldBank provider already
                # knows how to fetch the cross-country "all" surface; do not
                # fail validation before it gets a chance to return data.
                params['country'] = 'all'
                params['__worldbank_defaulted_country_all'] = True
                intent.parameters = params
                return True, None, {
                    'note': 'Defaulted exact WorldBank title/code query to all countries',
                }

            # Allow global/commodity indicators that don't need a country.
            # WorldBank has global aggregates using country code "1W" (World).
            # Indicators like PE.USG.LNDN (gold price) are global by nature.
            indicator = params.get('indicator', '')
            is_global_indicator = (
                indicator.startswith('PE.')  # Commodity prices (PE.USG.LNDN, etc.)
                or indicator.startswith('CM.')  # Commodity market codes
                or 'commodity' in str(intent.originalQuery or '').lower()
                or 'price' in str(intent.originalQuery or '').lower()
            )
            if is_global_indicator:
                # Default to World aggregate
                params['country'] = '1W'
                intent.parameters = params
                return True, None, None

            # Phase 2.5: deleted the FRED-USA word-overlap override (was H6 in
            # the deep review). Previously this block searched FRED via FTS5
            # and silently re-routed WorldBank → FRED + USA when query word
            # overlap >= 0.3 or the FRED series code appeared in the query.
            # That was a hardcoded-heuristic override of the LLM's provider
            # choice — exactly the Rule-1 violation
            # docs/DEEP_REVIEW_2026-05-30.md §4 flagged. Removing it means a
            # countryless WorldBank query now fails with the clear validation
            # message below, and the LLM/UnifiedRouter is the single source
            # of provider truth.
            return False, "World Bank query requires a country or list of countries", {
                'suggestion': 'Specify which country/countries you want data for',
                'example': 'Try: "China GDP" or "Compare GDP between US and UK"'
            }

        return True, None, None

    @staticmethod
    def check_confidence(intent: ParsedIntent) -> Tuple[bool, Optional[str]]:
        """
        Check if we have enough confidence in the parsed intent.

        NOTE: This is more lenient than previous versions. The providers
        can handle missing parameters better than validation allows, and
        the metadata search can fill in gaps. Only reject when truly essential
        data is missing.

        Returns:
            (is_confident, reason)
        """
        # If LLM provided confidence score, use it ONLY if very low
        # Previous threshold of 0.7 was too strict - LLM often returns 0.0 for valid queries
        if intent.confidence is not None and intent.confidence < 0.3:
            return False, f"Very low confidence ({intent.confidence:.0%}) in query parsing - please rephrase"

        # Check if critical fields are present
        if not intent.apiProvider:
            return False, "Could not determine data source"

        if not intent.indicators:
            return False, "Could not identify what data to fetch"

        # NOTE: Removed strict seriesId/indicator checks for FRED and StatsCan
        # The providers have:
        # - FRED: Indicator to seriesId mappings
        # - StatsCan: Indicator to vector ID mappings
        # - Metadata search: Can discover missing IDs
        #
        # Only reject if we have ZERO clue about what to fetch.

        return True, None

    @staticmethod
    def suggest_clarification(intent: ParsedIntent, validation_error: str) -> List[str]:
        """Generate clarification questions based on validation failure"""
        questions = []

        if "series id" in validation_error.lower():
            questions.append("Which specific economic indicator would you like? (e.g., GDP, unemployment, inflation)")
            questions.append("Would you like me to search for the correct data series in Pro Mode?")

        elif "indicator" in validation_error.lower():
            questions.append("Could you rephrase using a common indicator name? (GDP, unemployment, CPI, etc.)")
            questions.append("Or should I use Pro Mode to search for this data?")

        elif "country" in validation_error.lower():
            questions.append("Which country or countries would you like data for?")

        else:
            questions.append("Could you rephrase your query with more specific details?")
            questions.append("Would you like to use Pro Mode for a custom analysis instead?")

        return questions
