"""
Query complexity analyzer and handler
Detects complex queries and provides strategies to handle them
"""
from typing import Dict, List, Optional, Tuple
import re
import logging

from ..models import ParsedIntent

logger = logging.getLogger(__name__)


class QueryComplexityAnalyzer:
    """Analyzes query complexity and suggests handling strategies"""

    # Patterns that indicate complexity
    MULTI_COUNTRY_PATTERNS = [
        r'\b(?:compare|comparison|versus|vs)\b',
        r'\band\b.*\band\b',  # "US and UK and Germany"
        r'\b(?:all|multiple|several)\s+(?:countries|nations)\b',
        r'\ball\s+(?:oecd|g7|g20|eu|european)\s+(?:countries|nations|members?)\b',
        r'\b(?:oecd|g7|g20)\s+(?:countries|nations|members?)\b',
        r'\bacross\s+(?:all\s+)?(?:oecd|g7|g20|eu)\b',
    ]

    # Calculation patterns - these indicate the user wants us to PERFORM a calculation
    # NOT just retrieve data that has "rate" or "growth" in the name
    #
    # IMPORTANT: "GDP growth rate" is an indicator NAME, not a calculation request
    # "inflation rate" is an indicator NAME, not a calculation request
    # "unemployment rate" is an indicator NAME, not a calculation request
    #
    # Actual calculation requests look like:
    # - "calculate the growth rate from the GDP data"
    # - "compute year-over-year change"
    # - "find the correlation between X and Y"
    CALCULATION_PATTERNS = [
        # Explicit calculation verbs (these truly indicate calculation requests)
        r'\b(?:calculate|compute|derive|determine|find)\s+(?:the\s+)?(?:growth|change|difference|rate|trend|correlation|ratio)\b',
        r'\b(?:calculate|compute|derive|determine)\b.*\b(?:from|between|using)\b',
        # Correlation/comparison analysis
        r'\bcorrelation\s+between\b',
        r'\bcompare\s+(?:the\s+)?(?:growth|change|trend)\b',
        # Statistical aggregations (but not "average hours" which is an indicator)
        r'\b(?:average|mean|median|total|sum)\s+(?:of|across|for)\s+(?:all|multiple|the)\b',
        # Year-over-year, quarter-over-quarter explicit calculations
        r'\byear-over-year\s+(?:change|growth|comparison)\b',
        r'\bquarter-over-quarter\s+(?:change|growth|comparison)\b',
        # Ratio calculations
        r'\bratio\s+(?:of|between)\b',
    ]

    # Common indicator name patterns that should NOT trigger Pro Mode
    # These are pre-calculated indicators available from data providers
    INDICATOR_NAME_PATTERNS = [
        r'\bgdp\s+growth\s+rate\b',
        r'\binflation\s+rate\b',
        r'\bunemployment\s+rate\b',
        r'\binterest\s+rate\b',
        r'\bexchange\s+rate\b',
        r'\bgrowth\s+rate\b',  # Generic "growth rate" is usually an indicator
        r'\bpopulation\s+growth\b',
        r'\beconomic\s+growth\b',
    ]

    MULTI_INDICATOR_PATTERNS = [
        r'\band\b.*\band\b.*\b(?:data|indicator|metric)',
        r'\b(?:multiple|several|various)\s+(?:indicators|metrics|variables)\b',
    ]

    REGIONAL_PATTERNS = [
        r'\bby\s+(?:province|state|region|city|county)\b',
        r'\b(?:provincial|regional|state-level)\b',
        r'\beach\s+(?:province|state|region)\b',
    ]

    CATEGORICAL_PATTERNS = [
        # Only trigger for explicit breakdown REQUESTS (user asking for custom breakdown)
        # NOT for pre-computed indicators like "employment by sector"
        r'\b(?:breakdown|broken\s+down|break\s+down)\s+by\s+(?:age|gender|education|occupation|industry|sector)\b',
        r'\bfor\s+(?:each|all|different)\s+(?:age\s+groups?|demographic|gender|category|categories)\b',
        r'\bacross\s+(?:all|different)\s+(?:age\s+groups?|demographics|categories)\b',
        # Specific multi-dimension requests
        r'\bfor\s+all\s+(?:provinces|states|regions)\b',
        r'\bby\s+(?:province|state|region)\s+and\s+(?:age|gender|sector)\b',
    ]

    # Standard breakdown indicators that are PRE-COMPUTED by providers
    # These should NOT trigger Pro Mode - they use standard decomposition path
    STANDARD_BREAKDOWN_INDICATORS = [
        # StatsCan pre-computed tables (with optional country prefix)
        r'\b(?:canada\s+)?employment\s+(?:rate\s+)?by\s+(?:province|sector|industry)\b',  # Table 14-10-0202-01
        r'\b(?:canada\s+)?gdp\s+by\s+(?:industry|sector|province|provinces)\b',           # Table 36-10-0434-01
        r'\b(?:canada\s+)?unemployment\s+(?:rate\s+)?by\s+province\b',                    # Available aggregated
        r'\b(?:canada\s+)?(?:unemployment|employment)\s+(?:rate\s+)?(?:by|across)\s+(?:canadian\s+)?provinces?\b',  # Flexible
        r'\bpopulation\s+by\s+(?:age|province)\b',                                        # Table 17-10-0005-01
        r'\binflation\s+by\s+(?:component|category)\b',                                   # CPI components
        # WorldBank/IMF pre-computed indicators
        r'\bgdp\s+(?:growth\s+)?by\s+sector\b',
        r'\bexports?\s+by\s+(?:category|product|commodity)\b',
        r'\bimports?\s+by\s+(?:category|product|commodity)\b',
        # Generic pre-computed breakdown indicators
        r'\b(?:trade|employment|gdp)\s+by\s+(?:sector|industry)\b',
        # Decomposition-friendly patterns (country + indicator + by province)
        r'\b(?:canada|canadian)\b.*\b(?:unemployment|employment|labor|labour)\b.*\bby\s+province\b',
    ]

    RANKING_PATTERNS = [
        r'\b(?:rank|ranking|ranked|rankings)\b',
        r'\b(?:top|highest|lowest|best|worst)\s+\d*\s*(?:countries?|nations?|states?)\b',
        r'\bwhich\s+(?:country|countries|nation|nations|state|states).*(?:highest|lowest|best|worst)\b',
        r'\bwhich\s+(?:g7|g20|eu|brics|asean).*(?:highest|lowest|best|worst)\b',
        r'\b(?:compare|comparison)\s+all\b',
        r'\ball\s+(?:eu|european|g7|g20|brics|asean)\s+(?:country|countries)\b',
    ]

    KNOWN_LIMITATIONS = {
        'multi_province': {
            'description': 'Data by province/region requires multiple API calls',
            'suggestion': 'Use Pro Mode to fetch and combine data from all provinces',
            'example': 'Try: "Use Pro Mode to show employment by province"'
        },
        'trade_balance': {
            'description': 'Some trade balance queries may not be supported by the API',
            'suggestion': 'Query exports and imports separately, or use Pro Mode to calculate',
            'example': 'Try: "Show China exports to US" and "Show China imports from US"'
        },
        'complex_calculation': {
            'description': 'Complex calculations require custom code',
            'suggestion': 'Use Pro Mode to generate code for the calculation',
            'example': 'Try Pro Mode: "Calculate the correlation between GDP and unemployment"'
        },
        'multi_country_comparison': {
            'description': 'Comparing many countries requires multiple API calls',
            'suggestion': 'Use Pro Mode to fetch all countries and create comparison charts',
            'example': 'Try Pro Mode: "Compare GDP across G7 countries"'
        },
        'missing_indicator': {
            'description': 'This specific indicator may not be mapped in our system',
            'suggestion': 'Try rephrasing with a common indicator name, or use Pro Mode',
            'example': 'Common indicators: GDP, unemployment, inflation, CPI, housing starts'
        }
    }

    @staticmethod
    def detect_complexity(query: str, intent: Optional[ParsedIntent] = None) -> Dict:
        """
        Detect query complexity and return analysis

        Returns:
            {
                'is_complex': bool,
                'complexity_factors': List[str],
                'suggested_strategy': str,
                'pro_mode_recommended': bool,
                'pro_mode_required': bool,  # NEW: Must use Pro Mode, standard mode will fail
                'breakdown_possible': bool,
                'limitation_type': Optional[str]
            }
        """
        query_lower = query.lower()
        factors = []
        limitation_type = None
        pro_mode_required = False

        # Check for ranking queries (MUST use Pro Mode)
        has_ranking = any(re.search(pattern, query_lower) for pattern in QueryComplexityAnalyzer.RANKING_PATTERNS)
        if has_ranking:
            factors.append('ranking')
            limitation_type = 'multi_country_comparison'
            pro_mode_required = True  # Rankings REQUIRE Pro Mode

        # Check for multi-country
        multi_country = any(re.search(pattern, query_lower) for pattern in QueryComplexityAnalyzer.MULTI_COUNTRY_PATTERNS)
        if multi_country or (intent and intent.parameters.get('countries') and len(intent.parameters['countries']) > 3):
            factors.append('multi_country')
            if not limitation_type:
                limitation_type = 'multi_country_comparison'

        # Check for calculations - but first exclude common indicator names
        # "GDP growth rate" is an indicator NAME, not a calculation request
        is_indicator_name = any(re.search(pattern, query_lower) for pattern in QueryComplexityAnalyzer.INDICATOR_NAME_PATTERNS)

        # Only check for calculation patterns if this is NOT a known indicator name
        has_calculation = False
        if not is_indicator_name:
            has_calculation = any(re.search(pattern, query_lower) for pattern in QueryComplexityAnalyzer.CALCULATION_PATTERNS)

        if has_calculation:
            factors.append('calculation')
            if not limitation_type:
                limitation_type = 'complex_calculation'

        # Check for multi-indicator
        multi_indicator = any(re.search(pattern, query_lower) for pattern in QueryComplexityAnalyzer.MULTI_INDICATOR_PATTERNS)
        if multi_indicator or (intent and len(intent.indicators) > 2):
            factors.append('multi_indicator')
            # Multi-indicator queries can now be handled in standard mode by fetching each indicator separately
            # Only require Pro Mode if combined with other complexity factors (ranking, calculation, etc.)

        # Check for categorical breakdown (age groups, gender, education, etc.)
        # BUT FIRST check if this is a standard pre-computed indicator
        is_standard_breakdown = any(
            re.search(pattern, query_lower)
            for pattern in QueryComplexityAnalyzer.STANDARD_BREAKDOWN_INDICATORS
        )

        categorical = any(re.search(pattern, query_lower) for pattern in QueryComplexityAnalyzer.CATEGORICAL_PATTERNS)
        if categorical and not is_standard_breakdown:
            # Only require Pro Mode if it's NOT a standard breakdown indicator
            factors.append('categorical_breakdown')
            if not limitation_type:
                limitation_type = 'multi_province'  # Reuse this limitation type
            pro_mode_required = True  # Categorical queries REQUIRE Pro Mode
        elif is_standard_breakdown:
            # Standard breakdown - use regular data fetch, no Pro Mode needed
            logger.debug(f"Query matches standard breakdown indicator, not triggering Pro Mode")

        # Check for regional breakdown
        # BUT don't force Pro Mode if it's a standard breakdown (handled by decomposition)
        regional = any(re.search(pattern, query_lower) for pattern in QueryComplexityAnalyzer.REGIONAL_PATTERNS)
        if regional and not is_standard_breakdown:
            factors.append('regional_breakdown')
            if not limitation_type:
                limitation_type = 'multi_province'
            # Force Pro Mode for regional queries that aren't standard breakdowns
            # Statistics Canada batch methods are too slow (180+ seconds)
            # Pro Mode can handle this with proper code generation
            pro_mode_required = True
        elif regional and is_standard_breakdown:
            # Standard breakdown detected - use decomposition instead of Pro Mode
            logger.debug(f"Query is a regional query but matches standard breakdown - using decomposition")
            factors.append('regional_breakdown')

        # Check for trade balance (known limitation)
        if 'trade balance' in query_lower and intent and intent.apiProvider == 'Comtrade':
            factors.append('trade_balance')
            if not limitation_type:
                limitation_type = 'trade_balance'

        is_complex = len(factors) > 0
        pro_mode_recommended = is_complex and (
            'calculation' in factors or
            'regional_breakdown' in factors or
            'ranking' in factors or
            len(factors) > 1
        )

        breakdown_possible = (
            'multi_country' in factors and
            'calculation' not in factors and
            'ranking' not in factors and
            not pro_mode_required
        )

        # Determine strategy
        if pro_mode_required:
            strategy = 'pro_mode_required'
        elif pro_mode_recommended:
            strategy = 'pro_mode'
        elif breakdown_possible:
            strategy = 'breakdown'
        else:
            strategy = 'standard'

        return {
            'is_complex': is_complex,
            'complexity_factors': factors,
            'suggested_strategy': strategy,
            'pro_mode_recommended': pro_mode_recommended,
            'pro_mode_required': pro_mode_required,
            'breakdown_possible': breakdown_possible,
            'limitation_type': limitation_type
        }

    @staticmethod
    def get_limitation_explanation(limitation_type: str) -> Optional[Dict]:
        """Get detailed explanation for a known limitation"""
        return QueryComplexityAnalyzer.KNOWN_LIMITATIONS.get(limitation_type)

    @staticmethod
    def format_error_message(
        error: str,
        query: str,
        intent: Optional[ParsedIntent] = None,
        fallback_attempts: Optional[List[str]] = None,
        original_provider: Optional[str] = None,
    ) -> str:
        """
        Format error message with helpful suggestions and fallback transparency.

        Args:
            error: The error message
            query: The original query
            intent: Parsed intent if available
            fallback_attempts: List of providers that were tried as fallbacks
            original_provider: The originally selected provider

        Returns a user-friendly error message with context and suggestions
        """
        # Analyze query complexity
        analysis = QueryComplexityAnalyzer.detect_complexity(query, intent)

        # Start with base error
        message_parts = []
        limitation = None

        # Identify error type and provide context
        error_lower = error.lower()

        # === CATEGORIZED ERROR HANDLING ===

        # 1. Timeout/Rate Limit Errors
        if 'timeout' in error_lower or 'timed out' in error_lower:
            message_parts.append("⏱️ **Request Timed Out**")
            message_parts.append("The data provider took too long to respond.")
            message_parts.append("")
            message_parts.append("**💡 What you can do:**")
            message_parts.append("  • Try again in a few moments")
            message_parts.append("  • Simplify your query (fewer countries or shorter time range)")
            message_parts.append("  • Try a different data provider")

        elif 'rate limit' in error_lower or '429' in error_lower or 'too many requests' in error_lower:
            message_parts.append("🚦 **Rate Limit Reached**")
            message_parts.append("Too many requests were made to the data provider.")
            message_parts.append("")
            message_parts.append("**💡 Please wait a moment before trying again.**")

        # 2. Indicator Not Found
        elif 'vector id' in error_lower or 'series id' in error_lower or 'indicator is required' in error_lower:
            message_parts.append("🔍 **Indicator Not Found**")
            message_parts.append("The specific indicator in your query could not be found.")
            limitation = QueryComplexityAnalyzer.get_limitation_explanation('missing_indicator')

        elif 'indicator' in error_lower and 'not found' in error_lower:
            message_parts.append("🔍 **Indicator Not Available**")
            indicator_match = re.search(r'indicator\s+(\w+)\s+not found', error_lower)
            if indicator_match:
                message_parts.append(f"The indicator code '{indicator_match.group(1).upper()}' was not found.")
            else:
                message_parts.append("The requested indicator is not available from this provider.")
            message_parts.append("")
            message_parts.append("**💡 Suggestions:**")
            message_parts.append("  • Check the indicator name spelling")
            message_parts.append("  • Try a different provider (World Bank, IMF, or Eurostat)")
            message_parts.append("  • Use more general terms like 'GDP' or 'unemployment'")

        # 3. Country/Region Not Supported
        elif 'not a valid country' in error_lower or 'country' in error_lower and 'not' in error_lower:
            message_parts.append("🌍 **Country/Region Not Recognized**")
            message_parts.append(f"{error}")
            message_parts.append("")
            message_parts.append("**💡 Suggestions:**")
            message_parts.append("  • Use standard country names (e.g., 'United States' not 'USA')")
            message_parts.append("  • For regions, specify individual countries")
            message_parts.append("  • Example: Instead of 'Asia', try 'China, Japan, South Korea'")

        # 4. No Data Found
        elif 'no data found' in error_lower or 'no data available' in error_lower or 'norecordsfound' in error_lower:
            message_parts.append("📭 **No Data Found**")
            message_parts.append("The requested data is not available from this source.")
            message_parts.append("")
            # Add specific reason if detectable
            if 'country' in error_lower:
                message_parts.append("**Reason:** This country may not be covered by the selected provider.")
            elif 'indicator' in error_lower:
                message_parts.append("**Reason:** This indicator may not be tracked by the provider.")
            else:
                message_parts.append("**Possible reasons:**")
                message_parts.append("  • The indicator is not tracked for this country")
                message_parts.append("  • The data hasn't been published yet")
                message_parts.append("  • The time period is outside available range")

        # 5. Multiple Datasets Match
        elif 'matches multiple datasets' in error_lower or 'multiple datasets' in error_lower:
            message_parts.append("🔀 **Multiple Matches Found**")
            message_parts.append(f"{error}")
            message_parts.append("")
            message_parts.append("**💡 Please be more specific about which indicator you need.**")

        # 6. Trade Data Issues
        elif 'missing import or export' in error_lower or 'trade balance' in error_lower:
            message_parts.append("⚖️ **Trade Balance Limitation**")
            message_parts.append("This trade balance query cannot be processed directly.")
            limitation = QueryComplexityAnalyzer.get_limitation_explanation('trade_balance')

        # 7. Invalid Parameters
        elif 'invalid partner' in error_lower or 'invalid parameter' in error_lower or 'partnercode is invalid' in error_lower:
            message_parts.append("⚠️ **Invalid Parameters**")
            message_parts.append("The API rejected this query due to invalid country or partner codes.")
            if analysis['limitation_type']:
                limitation = QueryComplexityAnalyzer.get_limitation_explanation(analysis['limitation_type'])

        # 8. Historical Data Limitations
        elif 'historical' in error_lower and ('not available' in error_lower or 'api' in error_lower):
            message_parts.append("📅 **Historical Data Not Available**")
            message_parts.append(f"{error}")

        # 9. General Error
        else:
            message_parts.append("❌ **Query Could Not Be Completed**")
            # Clean up technical error messages
            cleaned_error = error
            if len(error) > 200:
                cleaned_error = error[:200] + "..."
            message_parts.append(f"Error: {cleaned_error}")

        # === ADD FALLBACK INFORMATION ===
        if fallback_attempts and len(fallback_attempts) > 0:
            message_parts.append("")
            message_parts.append("**📋 What we tried:**")
            if original_provider:
                message_parts.append(f"  1. {original_provider} (primary)")
            for i, provider in enumerate(fallback_attempts, start=2 if original_provider else 1):
                message_parts.append(f"  {i}. {provider} (fallback)")
            message_parts.append("")
            message_parts.append("All available data sources were checked without success.")

        # === ADD LIMITATION EXPLANATION ===
        if limitation:
            message_parts.append("")
            message_parts.append(f"**Known Limitation**: {limitation['description']}")
            message_parts.append("")
            message_parts.append(f"**💡 Suggestion**: {limitation['suggestion']}")
            message_parts.append("")
            message_parts.append(f"**Example**: {limitation['example']}")

        # === ADD PRO MODE RECOMMENDATION ===
        if analysis['pro_mode_recommended'] and not limitation:
            message_parts.append("")
            message_parts.append("**💡 This query appears complex.**")
            message_parts.append("Try using **Pro Mode** for:")
            message_parts.append("  • Custom calculations and analysis")
            message_parts.append("  • Multi-region or multi-country data")
            message_parts.append("  • Data transformations and visualizations")

        return "\n".join(message_parts)

    @staticmethod
    def suggest_pro_mode_prompt(query: str, analysis: Dict) -> str:
        """Generate a suggested Pro Mode prompt based on the original query"""
        factors = analysis['complexity_factors']

        if 'regional_breakdown' in factors:
            return f"Fetch and display {query} using multiple API calls for each region, then create a comparison chart."

        elif 'calculation' in factors:
            return f"Fetch the necessary data and calculate: {query}"

        elif 'multi_country' in factors:
            return f"Fetch data for all countries mentioned and create a comparison visualization for: {query}"

        else:
            return f"Analyze and visualize: {query}"
