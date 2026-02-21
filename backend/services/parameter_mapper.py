"""
Parameter Mapper for LangChain Orchestrator

Maps natural language terms and LangChain LLM outputs to provider-specific parameters.
Handles indicator codes, country codes, time periods, and special parameters for each provider.

Author: econ-data-mcp Development Team
Date: 2025-11-21
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


class ParameterMapper:
    """Maps natural language and LangChain outputs to provider-specific parameters."""

    def __init__(self):
        """Initialize parameter mapper with comprehensive mappings."""

        # Indicator mappings for each provider
        self.indicator_mappings = {
            "FRED": {
                # GDP and Growth
                "gdp": ["GDP", "GDPC1"],
                "gross domestic product": ["GDP", "GDPC1"],
                "real gdp": ["GDPC1"],
                "nominal gdp": ["GDP"],
                "gdp growth": ["A191RL1Q225SBEA"],
                "gdp growth rate": ["A191RL1Q225SBEA"],
                "gdp per capita": ["A939RX0Q048SBEA"],

                # Labor Market
                "unemployment": ["UNRATE"],
                "unemployment rate": ["UNRATE"],
                "jobless rate": ["UNRATE"],
                "employment": ["PAYEMS"],
                "total employment": ["PAYEMS"],
                "nonfarm payroll": ["PAYEMS"],
                "nonfarm payrolls": ["PAYEMS"],
                "jobs": ["PAYEMS"],
                "labor force": ["CLF16OV"],
                "labor force participation": ["CIVPART"],
                "labor force participation rate": ["CIVPART"],

                # Prices and Inflation
                "inflation": ["CPIAUCSL"],
                "inflation rate": ["CPIAUCSL"],
                "cpi": ["CPIAUCSL"],
                "consumer price index": ["CPIAUCSL"],
                "consumer prices": ["CPIAUCSL"],
                "price index": ["CPIAUCSL"],
                "ppi": ["PPIACO"],
                "producer price index": ["PPIACO"],
                "producer prices": ["PPIACO"],

                # Interest Rates
                "interest rate": ["FEDFUNDS"],
                "interest rates": ["FEDFUNDS"],
                "fed funds": ["FEDFUNDS"],
                "fed funds rate": ["FEDFUNDS"],
                "federal funds": ["FEDFUNDS"],
                "federal funds rate": ["FEDFUNDS"],
                "policy rate": ["FEDFUNDS"],
                "mortgage rate": ["MORTGAGE30US"],
                "mortgage rates": ["MORTGAGE30US"],
                "30 year mortgage": ["MORTGAGE30US"],
                "prime rate": ["DPRIME"],
                "prime bank rate": ["DPRIME"],

                # Housing
                "housing starts": ["HOUST"],
                "housing start": ["HOUST"],
                "new housing": ["HOUST"],
                "housing prices": ["CSUSHPINSA"],
                "housing price": ["CSUSHPINSA"],
                "home prices": ["CSUSHPINSA"],
                "home price": ["CSUSHPINSA"],
                "house prices": ["CSUSHPINSA"],
                "house price index": ["CSUSHPINSA"],
                "building permits": ["PERMIT"],

                # Consumption
                "retail sales": ["RSXFS"],
                "consumer spending": ["PCE"],
                "consumer expenditures": ["PCE"],
                "personal consumption": ["PCE"],
                "personal consumption expenditures": ["PCE"],

                # Sentiment
                "consumer confidence": ["UMCSENT"],
                "consumer sentiment": ["UMCSENT"],

                # Savings
                "savings rate": ["PSAVERT"],
                "personal savings rate": ["PSAVERT"],
                "disposable income": ["DPI"],
                "disposable personal income": ["DPI"],

                # Industrial
                "industrial production": ["INDPRO"],
                "capacity utilization": ["TCU"],
                "capacity utilization rate": ["TCU"],

                # Trade
                "imports": ["IMPGS"],
                "exports": ["EXPGS"],
                "trade deficit": ["BOPGSTB"],
                "trade balance": ["BOPGSTB"],

                # Money Supply
                "m1": ["M1SL"],
                "m2": ["M2SL"],
                "money supply": ["M2SL"],
            },

            "WORLDBANK": {
                # GDP
                "gdp": ["NY.GDP.MKTP.CD"],
                "gross domestic product": ["NY.GDP.MKTP.CD"],
                "nominal gdp": ["NY.GDP.MKTP.CD"],
                "real gdp": ["NY.GDP.MKTP.KD"],
                "gdp growth": ["NY.GDP.MKTP.KD.ZG"],
                "gdp growth rate": ["NY.GDP.MKTP.KD.ZG"],
                "gdp per capita": ["NY.GDP.PCAP.CD"],

                # Labor
                "unemployment": ["SL.UEM.TOTL.ZS"],
                "unemployment rate": ["SL.UEM.TOTL.ZS"],
                "youth unemployment": ["SL.UEM.1524.ZS"],
                "labor force participation": ["SL.TLF.CACT.ZS"],

                # Prices
                "inflation": ["FP.CPI.TOTL.ZG"],
                "inflation rate": ["FP.CPI.TOTL.ZG"],
                "cpi": ["FP.CPI.TOTL.ZG"],

                # Population
                "population": ["SP.POP.TOTL"],
                "population growth": ["SP.POP.GROW"],

                # Health
                "life expectancy": ["SP.DYN.LE00.IN"],
                "infant mortality": ["SP.DYN.IMRT.IN"],

                # Environment
                "co2 emissions": ["EN.GHG.CO2.PC.CE.AR5"],
                "carbon emissions": ["EN.GHG.CO2.PC.CE.AR5"],
                "emissions": ["EN.GHG.CO2.PC.CE.AR5"],
                "renewable energy": ["EG.FEC.RNEW.ZS"],

                # Poverty
                "poverty": ["SI.POV.DDAY"],
                "poverty rate": ["SI.POV.DDAY"],
                "extreme poverty": ["SI.POV.DDAY"],

                # Trade
                "trade": ["NE.TRD.GNFS.ZS"],
                "exports": ["NE.EXP.GNFS.ZS"],
                "imports": ["NE.IMP.GNFS.ZS"],
                "fdi": ["BX.KLT.DINV.WD.GD.ZS"],
                "foreign direct investment": ["BX.KLT.DINV.WD.GD.ZS"],
            },

            "STATSCAN": {
                # Core indicators (vector IDs)
                "gdp": ["65201210"],
                "gross domestic product": ["65201210"],
                "unemployment": ["2062815"],
                "unemployment rate": ["2062815"],
                "inflation": ["41690973"],
                "inflation rate": ["41690973"],
                "cpi": ["41690914"],
                "consumer price index": ["41690914"],
                "population": ["1"],
                "housing starts": ["52300157"],
                "housing start": ["52300157"],
            },

            "IMF": {
                # GDP
                "gdp": ["NGDP_RPCH"],
                "gdp growth": ["NGDP_RPCH"],
                "real gdp growth": ["NGDP_RPCH"],

                # Unemployment
                "unemployment": ["LUR"],
                "unemployment rate": ["LUR"],

                # Inflation
                "inflation": ["PCPIPCH"],
                "inflation rate": ["PCPIPCH"],
                "cpi": ["PCPIPCH"],

                # Debt
                "government debt": ["GGXWDG_NGDP"],
                "public debt": ["GGXWDG_NGDP"],
                "debt to gdp": ["GGXWDG_NGDP"],
                "debt to gdp ratio": ["GGXWDG_NGDP"],
                "fiscal deficit": ["GGXCNL_NGDP"],
                "budget deficit": ["GGXCNL_NGDP"],

                # Others
                "current account": ["BCA_NGDPD"],
                "population": ["LP"],
                "exchange rate": ["EREER"],
            },

            "BIS": {
                "policy rate": ["WS_CBPOL"],
                "interest rate": ["WS_CBPOL"],
                "central bank rate": ["WS_CBPOL"],
                "central bank policy rate": ["WS_CBPOL"],
                "credit": ["WS_TC"],
                "total credit": ["WS_TC"],
                "credit to gdp": ["WS_TC"],
                "credit gap": ["WS_TC"],
                "household credit": ["WS_TC"],
                "household debt": ["WS_TC"],
                "consumer debt": ["WS_TC"],
                "corporate credit": ["WS_TC"],
                "corporate debt": ["WS_TC"],
                "business debt": ["WS_TC"],
                "property prices": ["WS_SPP"],
                "house prices": ["WS_SPP"],
                "housing prices": ["WS_SPP"],
                "real estate prices": ["WS_SPP"],
                "exchange rate": ["WS_XRU"],
                "effective exchange rate": ["WS_XRU"],
                "inflation": ["WS_LONG_CPI"],
                "cpi": ["WS_LONG_CPI"],
                "consumer prices": ["WS_LONG_CPI"],
                "debt service": ["WS_DSR"],
                "debt service ratio": ["WS_DSR"],
                "global liquidity": ["WS_GLI"],
            },

            "EUROSTAT": {
                "gdp": ["nama_10_gdp"],
                "gross domestic product": ["nama_10_gdp"],
                "real gdp": ["nama_10_gdp"],
                "nominal gdp": ["nama_10_gdp"],
                "gdp growth": ["nama_10_gdp"],
                "gdp per capita": ["nama_10_pc"],
                "unemployment": ["une_rt_a"],
                "unemployment rate": ["une_rt_a"],
                "jobless rate": ["une_rt_a"],
                "employment": ["lfsi_emp_a"],
                "inflation": ["prc_hicp_aind"],
                "cpi": ["prc_hicp_aind"],
                "hicp": ["prc_hicp_aind"],
                "consumer prices": ["prc_hicp_aind"],
                "population": ["demo_pjan"],
                "house prices": ["prc_hpi_a"],
                "housing prices": ["prc_hpi_a"],
                "property prices": ["prc_hpi_a"],
                "trade": ["ext_lt_maineu"],
                "trade balance": ["tet00034"],
                "exports": ["ext_lt_maineu"],
                "imports": ["ext_lt_maineu"],
                "government debt": ["gov_10q_ggdebt"],
                "debt": ["gov_10q_ggdebt"],
                "deficit": ["gov_10dd_edpt1"],
            },

            "OECD": {
                "gdp": ["GDP"],
                "unemployment": ["UNE_RT"],
                "unemployment rate": ["UNE_RT"],
                "inflation": ["CPI"],
                "cpi": ["CPI"],
                "interest rate": ["IR"],
            },

            "COMTRADE": {
                "trade": ["TRADE"],
                "imports": ["IMPORT"],
                "exports": ["EXPORT"],
                "bilateral trade": ["TRADE"],
            },

            "EXCHANGERATE": {
                "exchange rate": ["rates"],
                "forex": ["rates"],
                "currency": ["rates"],
            },

            "COINGECKO": {
                "bitcoin": ["bitcoin"],
                "ethereum": ["ethereum"],
                "crypto": ["bitcoin"],
                "cryptocurrency": ["bitcoin"],
            },
        }

        # Country code mappings (ISO 3166-1 alpha-2 and alpha-3)
        self.country_mappings = {
            # Major Countries
            "united states": ["US", "USA", "United States"],
            "us": ["US", "USA", "United States"],
            "usa": ["US", "USA", "United States"],
            "america": ["US", "USA", "United States"],

            "canada": ["CA", "CAN", "Canada"],
            "ca": ["CA", "CAN", "Canada"],
            "can": ["CA", "CAN", "Canada"],

            "united kingdom": ["GB", "GBR", "United Kingdom"],
            "uk": ["GB", "GBR", "United Kingdom"],
            "britain": ["GB", "GBR", "United Kingdom"],
            "great britain": ["GB", "GBR", "United Kingdom"],

            "china": ["CN", "CHN", "China"],
            "cn": ["CN", "CHN", "China"],
            "prc": ["CN", "CHN", "China"],

            "japan": ["JP", "JPN", "Japan"],
            "jp": ["JP", "JPN", "Japan"],

            "germany": ["DE", "DEU", "Germany"],
            "de": ["DE", "DEU", "Germany"],

            "france": ["FR", "FRA", "France"],
            "fr": ["FR", "FRA", "France"],

            "india": ["IN", "IND", "India"],
            "in": ["IN", "IND", "India"],

            "italy": ["IT", "ITA", "Italy"],
            "it": ["IT", "ITA", "Italy"],

            "brazil": ["BR", "BRA", "Brazil"],
            "br": ["BR", "BRA", "Brazil"],

            "australia": ["AU", "AUS", "Australia"],
            "au": ["AU", "AUS", "Australia"],

            "spain": ["ES", "ESP", "Spain"],
            "es": ["ES", "ESP", "Spain"],

            "mexico": ["MX", "MEX", "Mexico"],
            "mx": ["MX", "MEX", "Mexico"],

            "south korea": ["KR", "KOR", "South Korea"],
            "korea": ["KR", "KOR", "South Korea"],
            "kr": ["KR", "KOR", "South Korea"],

            "russia": ["RU", "RUS", "Russia"],
            "ru": ["RU", "RUS", "Russia"],

            # European Countries
            "netherlands": ["NL", "NLD", "Netherlands"],
            "holland": ["NL", "NLD", "Netherlands"],
            "belgium": ["BE", "BEL", "Belgium"],
            "switzerland": ["CH", "CHE", "Switzerland"],
            "sweden": ["SE", "SWE", "Sweden"],
            "norway": ["NO", "NOR", "Norway"],
            "denmark": ["DK", "DNK", "Denmark"],
            "finland": ["FI", "FIN", "Finland"],
            "austria": ["AT", "AUT", "Austria"],
            "poland": ["PL", "POL", "Poland"],
            "portugal": ["PT", "PRT", "Portugal"],
            "greece": ["GR", "GRC", "Greece"],
            "ireland": ["IE", "IRL", "Ireland"],

            # Central/Eastern European Countries (EU members)
            "czech republic": ["CZ", "CZE", "Czechia"],
            "czechia": ["CZ", "CZE", "Czechia"],
            "cz": ["CZ", "CZE", "Czechia"],
            "hungary": ["HU", "HUN", "Hungary"],
            "hu": ["HU", "HUN", "Hungary"],
            "bulgaria": ["BG", "BGR", "Bulgaria"],
            "bg": ["BG", "BGR", "Bulgaria"],
            "romania": ["RO", "ROU", "Romania"],
            "ro": ["RO", "ROU", "Romania"],
            "croatia": ["HR", "HRV", "Croatia"],
            "hr": ["HR", "HRV", "Croatia"],
            "slovakia": ["SK", "SVK", "Slovakia"],
            "sk": ["SK", "SVK", "Slovakia"],
            "slovenia": ["SI", "SVN", "Slovenia"],
            "si": ["SI", "SVN", "Slovenia"],

            # Baltic States (EU members)
            "lithuania": ["LT", "LTU", "Lithuania"],
            "lt": ["LT", "LTU", "Lithuania"],
            "latvia": ["LV", "LVA", "Latvia"],
            "lv": ["LV", "LVA", "Latvia"],
            "estonia": ["EE", "EST", "Estonia"],
            "ee": ["EE", "EST", "Estonia"],

            # Small EU Countries
            "luxembourg": ["LU", "LUX", "Luxembourg"],
            "lu": ["LU", "LUX", "Luxembourg"],
            "malta": ["MT", "MLT", "Malta"],
            "mt": ["MT", "MLT", "Malta"],
            "cyprus": ["CY", "CYP", "Cyprus"],
            "cy": ["CY", "CYP", "Cyprus"],

            # Asian Countries
            "indonesia": ["ID", "IDN", "Indonesia"],
            "thailand": ["TH", "THA", "Thailand"],
            "singapore": ["SG", "SGP", "Singapore"],
            "malaysia": ["MY", "MYS", "Malaysia"],
            "philippines": ["PH", "PHL", "Philippines"],
            "vietnam": ["VN", "VNM", "Vietnam"],
            "pakistan": ["PK", "PAK", "Pakistan"],
            "bangladesh": ["BD", "BGD", "Bangladesh"],

            # Middle East
            "saudi arabia": ["SA", "SAU", "Saudi Arabia"],
            "uae": ["AE", "ARE", "United Arab Emirates"],
            "united arab emirates": ["AE", "ARE", "United Arab Emirates"],
            "israel": ["IL", "ISR", "Israel"],
            "turkey": ["TR", "TUR", "Turkey"],

            # Africa
            "south africa": ["ZA", "ZAF", "South Africa"],
            "nigeria": ["NG", "NGA", "Nigeria"],
            "egypt": ["EG", "EGY", "Egypt"],

            # Americas
            "argentina": ["AR", "ARG", "Argentina"],
            "chile": ["CL", "CHL", "Chile"],
            "colombia": ["CO", "COL", "Colombia"],

            # European Union Aggregates (Eurostat-specific)
            "eu": ["EU27_2020", "EU", "European Union"],
            "european union": ["EU27_2020", "EU", "European Union"],
            "eurozone": ["EA19", "EA", "Euro Area"],
            "euro area": ["EA19", "EA", "Euro Area"],
            "ea": ["EA19", "EA", "Euro Area"],

            # Canadian Provinces
            "ontario": ["Ontario", "ON"],
            "quebec": ["Quebec", "QC"],
            "british columbia": ["British Columbia", "BC"],
            "bc": ["British Columbia", "BC"],
            "alberta": ["Alberta", "AB"],
            "manitoba": ["Manitoba", "MB"],
            "saskatchewan": ["Saskatchewan", "SK"],
            "nova scotia": ["Nova Scotia", "NS"],
            "new brunswick": ["New Brunswick", "NB"],
            "newfoundland": ["Newfoundland and Labrador", "NL"],
            "newfoundland and labrador": ["Newfoundland and Labrador", "NL"],
            "prince edward island": ["Prince Edward Island", "PE"],
            "pei": ["Prince Edward Island", "PE"],
        }

        # Default parameters for each provider
        self.default_parameters = {
            "FRED": {
                "frequency": None,  # Auto-detect from series
                "aggregation_method": "avg",
            },
            "WORLDBANK": {
                "frequency": "annual",
                "per_page": 1000,
            },
            "STATSCAN": {
                "latest": 10,  # Last 10 observations by default
            },
            "IMF": {
                "frequency": "annual",
            },
            "BIS": {
                "frequency": "quarterly",
            },
            "EUROSTAT": {
                "frequency": "annual",
            },
            "OECD": {
                "frequency": "annual",
            },
            "COMTRADE": {
                "classification": "HS",  # Harmonized System
                "frequency": "annual",
            },
            "EXCHANGERATE": {
                "base_currency": "USD",
            },
            "COINGECKO": {
                "vs_currency": "usd",
                "days": 30,
            },
        }

    def map_intent_to_parameters(
        self,
        provider: str,
        intent: Dict[str, Any],
        query: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Convert LangChain/LLM intent to provider-specific parameters.

        Args:
            provider: Provider name (FRED, WorldBank, etc.)
            intent: Intent dict from LLM with indicator, country, etc.
            query: Optional original query for context

        Returns:
            Dict of provider-specific parameters
        """
        provider_upper = provider.upper()
        params = self.get_default_parameters(provider_upper).copy()

        # Map indicator
        if "indicator" in intent:
            indicator = intent["indicator"]
            normalized_indicator = self.normalize_indicator(
                provider_upper,
                indicator
            )

            # Provider-specific parameter names
            if provider_upper == "FRED":
                if normalized_indicator:
                    params["seriesId"] = normalized_indicator
                else:
                    params["indicator"] = indicator
            elif provider_upper == "STATSCAN":
                if normalized_indicator and normalized_indicator.isdigit():
                    params["vectorId"] = int(normalized_indicator)
                else:
                    params["indicator"] = indicator
            else:
                params["indicator"] = normalized_indicator or indicator

        # Map country
        if "country" in intent and intent["country"]:
            country_codes = self.normalize_country(
                provider_upper,
                intent["country"]
            )
            if country_codes:
                # Use appropriate format for provider
                if provider_upper in ["WORLDBANK", "IMF"]:
                    params["country"] = country_codes[1]  # Use 3-letter code
                elif provider_upper == "STATSCAN":
                    params["geography"] = country_codes[0]  # Full name
                else:
                    params["country"] = country_codes[0]  # 2-letter code

        # Map time period parameters
        if "startDate" in intent:
            params["startDate"] = intent["startDate"]
        if "endDate" in intent:
            params["endDate"] = intent["endDate"]
        if "start_date" in intent:
            params["startDate"] = intent["start_date"]
        if "end_date" in intent:
            params["endDate"] = intent["end_date"]

        # Map frequency
        if "frequency" in intent:
            params["frequency"] = intent["frequency"].lower()

        # Map any other parameters directly
        for key, value in intent.items():
            if key not in ["indicator", "country", "provider", "reasoning",
                          "needs_clarification", "clarification_questions"]:
                if key not in params:
                    params[key] = value

        logger.info(f"Mapped intent to parameters for {provider_upper}: {params}")
        return params

    def normalize_indicator(
        self,
        provider: str,
        indicator: str
    ) -> Optional[str]:
        """
        Find best matching indicator code for provider.

        Args:
            provider: Provider name
            indicator: Indicator name or synonym

        Returns:
            Provider-specific indicator code or None if not found
        """
        provider_upper = provider.upper()

        if provider_upper not in self.indicator_mappings:
            logger.warning(f"No indicator mappings for provider: {provider_upper}")
            return None

        provider_indicators = self.indicator_mappings[provider_upper]
        indicator_lower = indicator.lower().strip()

        # Direct match
        if indicator_lower in provider_indicators:
            codes = provider_indicators[indicator_lower]
            return codes[0] if codes else None

        # Fuzzy match
        best_match = None
        best_score = 0.0

        for key, codes in provider_indicators.items():
            score = SequenceMatcher(None, indicator_lower, key).ratio()
            if score > best_score and score >= 0.7:  # 70% similarity threshold
                best_score = score
                best_match = codes[0] if codes else None

        if best_match:
            logger.info(f"Fuzzy matched '{indicator}' to '{best_match}' for {provider_upper} (score: {best_score:.2f})")
            return best_match

        # Special handling for CoinGecko - try to extract coin name from indicator text
        if provider_upper == "COINGECKO":
            coin_name = self._extract_coingecko_coin(indicator_lower)
            if coin_name:
                logger.info(f"Extracted CoinGecko coin '{coin_name}' from '{indicator}'")
                return coin_name
            # Default to bitcoin if no coin found
            logger.info(f"Defaulting to 'bitcoin' for CoinGecko query: '{indicator}'")
            return "bitcoin"

        logger.warning(f"No match found for indicator '{indicator}' in {provider_upper}")
        return best_match

    def _extract_coingecko_coin(self, text: str) -> Optional[str]:
        """Extract cryptocurrency name from text for CoinGecko."""
        # Common cryptocurrency names and their CoinGecko IDs
        crypto_mapping = {
            "bitcoin": "bitcoin",
            "btc": "bitcoin",
            "ethereum": "ethereum",
            "eth": "ethereum",
            "solana": "solana",
            "sol": "solana",
            "cardano": "cardano",
            "ada": "cardano",
            "dogecoin": "dogecoin",
            "doge": "dogecoin",
            "ripple": "ripple",
            "xrp": "ripple",
            "polkadot": "polkadot",
            "dot": "polkadot",
            "litecoin": "litecoin",
            "ltc": "litecoin",
            "chainlink": "chainlink",
            "link": "chainlink",
            "polygon": "matic-network",
            "matic": "matic-network",
            "avalanche": "avalanche-2",
            "avax": "avalanche-2",
            "cosmos": "cosmos",
            "atom": "cosmos",
            "uniswap": "uniswap",
            "uni": "uniswap",
            "binance coin": "binancecoin",
            "bnb": "binancecoin",
        }

        text_lower = text.lower()
        for keyword, coin_id in crypto_mapping.items():
            if keyword in text_lower:
                return coin_id
        return None

    def normalize_country(
        self,
        provider: str,
        country: str
    ) -> Optional[List[str]]:
        """
        Normalize country name to codes for provider.

        Args:
            provider: Provider name
            country: Country name or code

        Returns:
            List of [2-letter, 3-letter, full name] or None
        """
        country_lower = country.lower().strip()
        country_upper = country.upper().strip()

        # 1. Direct key match (country name)
        if country_lower in self.country_mappings:
            return self.country_mappings[country_lower]

        # 2. Reverse lookup: check if input is an ISO code (2-letter or 3-letter)
        # or the full name inside the values list
        for key, codes in self.country_mappings.items():
            # codes = ["ZA", "ZAF", "South Africa"] for example
            if len(codes) >= 3:
                iso2, iso3, full_name = codes[0], codes[1], codes[2]
                # Check if input matches any of the codes
                if country_upper == iso2 or country_upper == iso3:
                    logger.info(f"Reverse lookup: ISO code '{country}' → {codes}")
                    return codes
                if country_lower == full_name.lower():
                    logger.info(f"Reverse lookup: full name '{country}' → {codes}")
                    return codes

        # 3. Fuzzy match country names (keys)
        best_match = None
        best_score = 0.0

        for key, codes in self.country_mappings.items():
            score = SequenceMatcher(None, country_lower, key).ratio()
            if score > best_score and score >= 0.8:  # 80% similarity for countries
                best_score = score
                best_match = codes

        if best_match:
            logger.info(f"Fuzzy matched country '{country}' to {best_match} (score: {best_score:.2f})")
        else:
            logger.warning(f"No match found for country '{country}'")

        return best_match

    def get_default_parameters(self, provider: str) -> Dict[str, Any]:
        """
        Get default parameters for provider.

        Args:
            provider: Provider name

        Returns:
            Dict of default parameters
        """
        provider_upper = provider.upper()
        return self.default_parameters.get(provider_upper, {}).copy()

    def add_indicator_mapping(
        self,
        provider: str,
        indicator_name: str,
        indicator_code: str
    ) -> None:
        """
        Add a new indicator mapping dynamically (for runtime learning).

        Args:
            provider: Provider name
            indicator_name: Natural language indicator name
            indicator_code: Provider-specific code
        """
        provider_upper = provider.upper()
        indicator_lower = indicator_name.lower().strip()

        if provider_upper not in self.indicator_mappings:
            self.indicator_mappings[provider_upper] = {}

        if indicator_lower not in self.indicator_mappings[provider_upper]:
            self.indicator_mappings[provider_upper][indicator_lower] = []

        if indicator_code not in self.indicator_mappings[provider_upper][indicator_lower]:
            self.indicator_mappings[provider_upper][indicator_lower].insert(0, indicator_code)
            logger.info(f"Added mapping: {provider_upper} '{indicator_name}' -> '{indicator_code}'")

    def get_indicator_synonyms(
        self,
        provider: str,
        indicator: str
    ) -> List[str]:
        """
        Get all synonyms for an indicator.

        Args:
            provider: Provider name
            indicator: Indicator name

        Returns:
            List of indicator names that map to the same code
        """
        provider_upper = provider.upper()

        if provider_upper not in self.indicator_mappings:
            return []

        # Find the code for this indicator
        code = self.normalize_indicator(provider_upper, indicator)
        if not code:
            return []

        # Find all names that map to this code
        synonyms = []
        for name, codes in self.indicator_mappings[provider_upper].items():
            if code in codes:
                synonyms.append(name)

        return synonyms
