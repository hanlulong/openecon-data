"""
Cross-Provider Indicator Translator

A general-purpose indicator translation layer that handles:
1. IMF-style codes (NGDP, LUR, PCPIPCH) that LLMs commonly return
2. Fuzzy matching for similar indicator names
3. Universal concept mapping across providers

This replaces hardcoded provider-specific aliases with a general solution.

Author: econ-data-mcp Development Team
Date: 2025-11-29
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


class IndicatorTranslator:
    """
    Translates indicator codes/names between providers using universal concepts.

    This provides a general solution for cross-provider indicator resolution:
    - Recognizes IMF-style codes and translates them to universal concepts
    - Uses fuzzy matching to find similar indicators
    - Maps universal concepts to provider-specific codes
    """

    # Universal indicator concepts mapped to provider-specific codes
    # Each concept has a canonical name and maps to codes for each provider
    UNIVERSAL_CONCEPTS: Dict[str, Dict[str, List[str]]] = {
        # GDP and Growth
        "gdp": {
            "aliases": ["gdp", "gross domestic product", "gross_domestic_product", "national output"],
            "imf_codes": ["NGDP", "NGDP_R", "NGDPD"],
            "providers": {
                "FRED": ["GDP", "GDPC1"],
                "WORLDBANK": ["NY.GDP.MKTP.CD", "NY.GDP.MKTP.KD"],
                "IMF": ["NGDP_RPCH"],
                "EUROSTAT": ["nama_10_gdp"],
                "OECD": ["GDP"],
                "BIS": [],  # BIS doesn't have GDP
                "STATSCAN": ["65201210"],
            }
        },
        "gdp_growth": {
            "aliases": ["gdp growth", "gdp_growth", "gdp growth rate", "real gdp growth",
                       "economic growth", "growth rate"],
            "imf_codes": ["NGDP_RPCH", "NGDP_R_PCH"],
            "providers": {
                "FRED": ["A191RL1Q225SBEA"],
                "WORLDBANK": ["NY.GDP.MKTP.KD.ZG"],
                "IMF": ["NGDP_RPCH"],
                "EUROSTAT": ["nama_10_gdp"],
                "OECD": ["GDP"],
                "BIS": [],
                "STATSCAN": [],
            }
        },
        "gdp_per_capita": {
            "aliases": ["gdp per capita", "gdp_per_capita", "per capita gdp", "income per capita"],
            "imf_codes": ["NGDPDPC", "NGDPPC"],
            "providers": {
                "FRED": ["A939RX0Q048SBEA"],
                "WORLDBANK": ["NY.GDP.PCAP.CD"],
                "IMF": ["NGDPDPC"],
                "EUROSTAT": ["nama_10_pc"],
                "OECD": [],
                "BIS": [],
                "STATSCAN": [],
            }
        },

        # Unemployment
        "unemployment": {
            "aliases": ["unemployment", "unemployment rate", "jobless rate", "labor market"],
            "imf_codes": ["LUR", "LPROD"],
            "providers": {
                "FRED": ["UNRATE"],
                "WORLDBANK": ["SL.UEM.TOTL.ZS"],
                "IMF": ["LUR"],
                "EUROSTAT": ["une_rt_a"],
                "OECD": ["UNE_RT"],
                "BIS": [],
                "STATSCAN": ["2062815"],
            }
        },

        # Inflation and Prices
        "inflation": {
            "aliases": ["inflation", "inflation rate", "price level", "consumer prices"],
            "imf_codes": ["PCPIPCH", "PCPI", "PCPIEPCH"],
            "providers": {
                "FRED": ["CPIAUCSL"],
                "WORLDBANK": ["FP.CPI.TOTL.ZG"],
                "IMF": ["PCPIPCH"],
                "EUROSTAT": ["prc_hicp_aind"],
                "OECD": ["CPI"],
                "BIS": ["WS_LONG_CPI"],
                "STATSCAN": ["41690973"],
            }
        },
        "cpi": {
            "aliases": ["cpi", "consumer price index", "cost of living"],
            "imf_codes": ["PCPI", "PCPIPCH"],
            "providers": {
                "FRED": ["CPIAUCSL"],
                "WORLDBANK": ["FP.CPI.TOTL"],
                "IMF": ["PCPIPCH"],
                "EUROSTAT": ["prc_hicp_aind"],
                "OECD": ["CPI"],
                "BIS": ["WS_LONG_CPI"],
                "STATSCAN": ["41690914"],
            }
        },

        # Debt and Credit
        "government_debt": {
            "aliases": ["government debt", "public debt", "sovereign debt", "national debt",
                       "debt to gdp", "debt ratio"],
            "imf_codes": ["GGXWDG_NGDP", "GGXWDG"],
            "providers": {
                "FRED": ["GFDEGDQ188S"],
                "WORLDBANK": ["GC.DOD.TOTL.GD.ZS"],
                "IMF": ["GGXWDG_NGDP"],
                "EUROSTAT": ["gov_10q_ggdebt"],
                "OECD": [],
                "BIS": [],
                "STATSCAN": [],
            }
        },
        "household_debt": {
            "aliases": ["household debt", "household credit", "personal debt",
                       "household debt to gdp", "household debt ratio",
                       "household debt to income", "household debt to disposable income",
                       "debt to income ratio", "household debt service"],
            "imf_codes": [],  # Let IndicatorResolver find the correct codes dynamically
            "providers": {
                "FRED": ["HDTGPDUSQ163N"],
                "WORLDBANK": [],
                "IMF": [],  # IndicatorResolver will find FS_HH_IDG_FSHDDI_XDC dynamically
                "EUROSTAT": [],
                "OECD": [],  # IndicatorResolver will find OECD codes dynamically
                "BIS": ["WS_TC"],  # Total credit dataset has household breakdown
                "STATSCAN": [],
            }
        },
        # INFRASTRUCTURE FIX: Consumer credit is different from household debt
        # Consumer credit = unsecured credit (credit cards, personal loans)
        # Household debt = all household liabilities (includes mortgages)
        "consumer_credit": {
            "aliases": ["consumer credit", "consumer credit outstanding",
                       "total consumer credit", "consumer loans", "consumer lending",
                       "credit card debt", "revolving credit", "consumer debt"],
            "imf_codes": [],
            "providers": {
                "FRED": ["TOTALSL", "REVOLSL"],  # Total consumer credit, Revolving credit
                "WORLDBANK": [],
                "IMF": [],
                "EUROSTAT": [],
                "OECD": [],
                "BIS": [],
                "STATSCAN": [],
            }
        },
        "corporate_debt": {
            "aliases": ["corporate debt", "business debt", "corporate credit",
                       "nonfinancial corporate debt", "business credit"],
            "imf_codes": [],
            "providers": {
                "FRED": ["BCNSDODNS"],
                "WORLDBANK": [],
                "IMF": [],
                "EUROSTAT": [],
                "OECD": [],
                "BIS": ["WS_TC"],  # Total credit dataset has corporate breakdown
                "STATSCAN": [],
            }
        },
        "total_credit": {
            "aliases": ["total credit", "credit", "private credit", "credit to gdp",
                       "credit to private sector", "private sector credit"],
            "imf_codes": [],
            "providers": {
                "FRED": [],
                "WORLDBANK": ["FS.AST.PRVT.GD.ZS"],
                "IMF": [],
                "EUROSTAT": [],
                "OECD": [],
                "BIS": ["WS_TC"],
                "STATSCAN": [],
            }
        },

        # Interest Rates / Policy Rates
        "interest_rate": {
            "aliases": ["interest rate", "policy rate", "central bank rate",
                       "fed funds rate", "base rate", "cash rate", "deposit facility rate",
                       "repo rate", "official rate", "key rate", "discount rate",
                       "monetary policy rate", "bank rate", "lending rate",
                       "ecb rate", "boe rate", "rba rate", "overnight rate",
                       "real interest rate", "nominal interest rate",
                       "government bond yield", "long term interest rate"],
            "imf_codes": [],
            "providers": {
                "FRED": ["FEDFUNDS", "DFEDTARU"],
                "WORLDBANK": ["FR.INR.RINR"],
                "IMF": [],
                "EUROSTAT": ["EI_MFIR_M"],  # INFRASTRUCTURE FIX: Add Eurostat interest rates
                "OECD": ["IR"],
                "BIS": ["WS_CBPOL"],
                "STATSCAN": [],
            }
        },

        # Trade
        "trade_balance": {
            "aliases": ["trade balance", "trade deficit", "net exports", "external balance"],
            "imf_codes": ["BCA", "BCA_NGDPD"],
            "providers": {
                "FRED": ["BOPGSTB"],
                "WORLDBANK": ["NE.RSB.GNFS.ZS"],
                "IMF": ["BCA_NGDPD"],
                "EUROSTAT": ["tet00034"],
                "OECD": [],
                "BIS": [],
                "STATSCAN": [],
            }
        },
        "exports": {
            "aliases": ["exports", "export", "goods exports", "merchandise exports"],
            "imf_codes": ["BX_GDP"],
            "providers": {
                "FRED": ["EXPGS"],
                "WORLDBANK": ["NE.EXP.GNFS.ZS"],
                "IMF": ["BX_GDP"],
                "EUROSTAT": ["ext_lt_maineu"],
                "OECD": [],
                "BIS": [],
                "COMTRADE": ["EXPORT"],
                "STATSCAN": [],
            }
        },
        "imports": {
            "aliases": ["imports", "import", "goods imports", "merchandise imports"],
            "imf_codes": ["BM_GDP"],
            "providers": {
                "FRED": ["IMPGS"],
                "WORLDBANK": ["NE.IMP.GNFS.ZS"],
                "IMF": ["BM_GDP"],
                "EUROSTAT": ["ext_lt_maineu"],
                "OECD": [],
                "BIS": [],
                "COMTRADE": ["IMPORT"],
                "STATSCAN": [],
            }
        },

        # Housing
        "house_prices": {
            "aliases": ["house prices", "housing prices", "property prices",
                       "real estate prices", "home prices"],
            "imf_codes": [],
            "providers": {
                "FRED": ["CSUSHPINSA"],
                "WORLDBANK": [],
                "IMF": [],
                "EUROSTAT": ["prc_hpi_a"],
                "OECD": [],
                "BIS": ["WS_SPP"],
                "STATSCAN": [],
            }
        },

        # Population
        "population": {
            "aliases": ["population", "total population", "pop"],
            "imf_codes": ["LP"],
            "providers": {
                "FRED": ["POPTHM"],
                "WORLDBANK": ["SP.POP.TOTL"],
                "IMF": ["LP"],
                "EUROSTAT": ["demo_pjan"],
                "OECD": [],
                "BIS": [],
                "STATSCAN": ["1"],
            }
        },

        # Exchange Rates
        "exchange_rate": {
            "aliases": ["exchange rate", "forex", "currency", "fx rate",
                       "effective exchange rate"],
            "imf_codes": ["EREER"],
            "providers": {
                "FRED": ["DEXUSEU"],
                "WORLDBANK": ["PA.NUS.FCRF"],
                "IMF": ["EREER"],
                "EUROSTAT": [],
                "OECD": [],
                "BIS": ["WS_XRU"],
                "EXCHANGERATE": ["rates"],
                "STATSCAN": [],
            }
        },
    }

    # Known IMF codes that should be translated to concepts
    IMF_CODE_TO_CONCEPT: Dict[str, str] = {}  # Built dynamically from UNIVERSAL_CONCEPTS

    def __init__(self):
        """Initialize the translator and build lookup tables."""
        self._build_imf_code_lookup()
        self._build_alias_lookup()

    def _build_imf_code_lookup(self):
        """Build IMF code to concept mapping from UNIVERSAL_CONCEPTS."""
        self.IMF_CODE_TO_CONCEPT = {}
        for concept_name, concept_data in self.UNIVERSAL_CONCEPTS.items():
            for imf_code in concept_data.get("imf_codes", []):
                self.IMF_CODE_TO_CONCEPT[imf_code.upper()] = concept_name

    def _build_alias_lookup(self):
        """Build alias to concept mapping for fuzzy matching."""
        self._alias_to_concept: Dict[str, str] = {}
        for concept_name, concept_data in self.UNIVERSAL_CONCEPTS.items():
            for alias in concept_data.get("aliases", []):
                self._alias_to_concept[alias.lower()] = concept_name

    def is_imf_code(self, indicator: str) -> bool:
        """Check if the indicator looks like an IMF code."""
        if not indicator:
            return False
        upper = indicator.upper().replace(" ", "_")
        # IMF codes are typically uppercase with underscores
        return upper in self.IMF_CODE_TO_CONCEPT

    def translate_imf_code(self, imf_code: str) -> Optional[str]:
        """Translate an IMF code to a universal concept name."""
        upper = imf_code.upper().replace(" ", "_")
        return self.IMF_CODE_TO_CONCEPT.get(upper)

    def get_provider_code(self, concept: str, provider: str) -> Optional[str]:
        """Get provider-specific code for a universal concept."""
        concept_lower = concept.lower()
        if concept_lower not in self.UNIVERSAL_CONCEPTS:
            return None

        concept_data = self.UNIVERSAL_CONCEPTS[concept_lower]
        provider_codes = concept_data.get("providers", {}).get(provider.upper(), [])

        if provider_codes:
            return provider_codes[0]  # Return first (primary) code
        return None

    def translate_indicator(
        self,
        indicator: str,
        target_provider: str,
        source_provider: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[str]]:
        """
        Translate an indicator to a target provider's code.

        Returns:
            Tuple of (translated_code, concept_name) or (None, None) if no translation found
        """
        if not indicator:
            return None, None

        indicator_clean = indicator.strip()
        target_upper = target_provider.upper()

        # Step 1: Check if it's already a valid code for the target provider
        # (This is handled by the provider's own mapping, not here)

        # Step 2: Check if it's an IMF code
        concept = self.translate_imf_code(indicator_clean)
        if concept:
            provider_code = self.get_provider_code(concept, target_upper)
            if provider_code:
                logger.info(f"Translated IMF code '{indicator}' -> concept '{concept}' -> {target_upper} code '{provider_code}'")
                return provider_code, concept

        # Step 3: Try fuzzy matching against aliases
        concept = self._fuzzy_match_concept(indicator_clean)
        if concept:
            provider_code = self.get_provider_code(concept, target_upper)
            if provider_code:
                logger.info(f"Fuzzy matched '{indicator}' -> concept '{concept}' -> {target_upper} code '{provider_code}'")
                return provider_code, concept

        # Step 4: Try fuzzy matching against known IMF codes
        best_imf_match = self._fuzzy_match_imf_code(indicator_clean)
        if best_imf_match:
            concept = self.translate_imf_code(best_imf_match)
            if concept:
                provider_code = self.get_provider_code(concept, target_upper)
                if provider_code:
                    logger.info(f"Fuzzy matched '{indicator}' to IMF code '{best_imf_match}' -> {target_upper} code '{provider_code}'")
                    return provider_code, concept

        logger.debug(f"No translation found for indicator '{indicator}' to provider '{target_upper}'")
        return None, None

    def _fuzzy_match_concept(self, indicator: str, threshold: float = 0.7) -> Optional[str]:
        """Find the best matching concept using fuzzy string matching.

        Infrastructure fix: Uses higher threshold for short queries to prevent
        false positives like "M2 Growth" matching "GDP Growth" (73.7% similarity).
        Short queries need stricter matching because small character differences
        have disproportionate impact on similarity scores.
        """
        indicator_lower = indicator.lower().replace("_", " ")

        # Direct match first
        if indicator_lower in self._alias_to_concept:
            return self._alias_to_concept[indicator_lower]

        # INFRASTRUCTURE FIX: Higher threshold for short queries
        # "m2 growth" (9 chars) vs "gdp growth" (10 chars) = 0.737 similarity
        # This exceeds 0.7 threshold but is clearly wrong (monetary vs output)
        # Short queries need 0.85+ threshold to prevent such false positives
        effective_threshold = 0.85 if len(indicator_lower) < 15 else threshold

        # Fuzzy match
        best_match = None
        best_score = 0.0

        for alias, concept in self._alias_to_concept.items():
            score = SequenceMatcher(None, indicator_lower, alias).ratio()
            if score > best_score and score >= effective_threshold:
                best_score = score
                best_match = concept

        if best_match:
            logger.debug(f"Fuzzy matched '{indicator}' to concept '{best_match}' (score: {best_score:.2f}, threshold: {effective_threshold:.2f})")

        return best_match

    def _fuzzy_match_imf_code(self, indicator: str, threshold: float = 0.8) -> Optional[str]:
        """Try to fuzzy match against known IMF codes."""
        indicator_upper = indicator.upper().replace(" ", "_")

        best_match = None
        best_score = 0.0

        for imf_code in self.IMF_CODE_TO_CONCEPT.keys():
            score = SequenceMatcher(None, indicator_upper, imf_code).ratio()
            if score > best_score and score >= threshold:
                best_score = score
                best_match = imf_code

        return best_match

    def get_all_aliases_for_provider(self, provider: str) -> Dict[str, str]:
        """
        Get all indicator aliases that can be translated to this provider.

        Returns:
            Dict mapping alias -> provider code
        """
        result = {}
        provider_upper = provider.upper()

        for concept_name, concept_data in self.UNIVERSAL_CONCEPTS.items():
            provider_codes = concept_data.get("providers", {}).get(provider_upper, [])
            if provider_codes:
                primary_code = provider_codes[0]
                # Add all aliases
                for alias in concept_data.get("aliases", []):
                    result[alias.lower()] = primary_code
                # Add IMF codes
                for imf_code in concept_data.get("imf_codes", []):
                    result[imf_code.upper()] = primary_code

        return result


# Singleton instance
_translator_instance: Optional[IndicatorTranslator] = None


def get_indicator_translator() -> IndicatorTranslator:
    """Get the singleton indicator translator instance."""
    global _translator_instance
    if _translator_instance is None:
        _translator_instance = IndicatorTranslator()
    return _translator_instance
