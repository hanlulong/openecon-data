from __future__ import annotations

from typing import Dict, List, Optional, TYPE_CHECKING
import logging

import httpx

from ..config import get_settings
from ..models import Metadata, NormalizedData
from ..utils.retry import DataNotAvailableError
from ..services.http_pool import get_http_client
from .base import BaseProvider

if TYPE_CHECKING:
    from ..services.metadata_search import MetadataSearchService

logger = logging.getLogger(__name__)


class WorldBankProvider(BaseProvider):
    """World Bank data provider.

    PHASE D: Now inherits from BaseProvider for:
    - Unified provider_name property
    - Standardized HTTP retry logic
    - Common error handling patterns
    """
    # WorldBank region and aggregate codes (from https://api.worldbank.org/v2/region)
    # These can be used in place of country codes to query aggregate data
    # NOTE: These codes are VALID and work correctly with the WorldBank API
    # Previous testing showed SSA returning 400 errors, but this was due to external issues,
    # not invalid codes. Direct API tests confirm all these codes work.
    VALID_REGIONS = {
        # Major regions
        "AFE", "AFR", "AFW",  # Africa regions
        "EAS", "ECS", "LCN",  # Asia, Europe, Latin America
        "MEA", "NAC", "SAS",  # Middle East, North America, South Asia
        "SSA", "SSF",         # Sub-Saharan Africa (with/without high income)
        "WLD",                # World
        # Income levels (from https://api.worldbank.org/v2/incomelevel)
        "HIC", "LIC", "LMC", "LMY", "MIC", "UMC", "INX",
    }

    # Fallback mappings for income-based aggregates that often lack data
    # When LMY/MIC/LIC fail, we fetch multiple geographic regions that overlap
    # This provides comprehensive coverage for "developing countries" queries
    INCOME_AGGREGATE_FALLBACKS = {
        # Low & Middle Income (LMY) → fetch major developing region aggregates
        "LMY": ["SAS", "SSF", "EAS", "LCN", "MEA"],  # South Asia, Sub-Saharan Africa, East Asia, Latin America, Middle East
        # Middle Income (MIC) → similar regions
        "MIC": ["EAS", "LCN", "MEA", "ECS"],  # East Asia, Latin America, Middle East, Europe (includes some MIC)
        # Low Income (LIC) → focus on poorest regions
        "LIC": ["SSF", "SAS"],  # Sub-Saharan Africa, South Asia (most LIC countries)
    }

    # Regional term mappings for natural language queries
    # Maps common regional terms to WorldBank region codes
    # This prevents system from decomposing regional queries into individual country queries
    REGIONAL_TERM_MAPPINGS = {
        # Geographic regions
        "SOUTH ASIA": "SAS",
        "SOUTH ASIAN": "SAS",
        "EAST ASIA": "EAS",
        "EAST ASIAN": "EAS",
        "MIDDLE EAST": "MEA",
        "LATIN AMERICA": "LCN",
        "LATIN AMERICAN": "LCN",
        "NORTH AMERICA": "NAC",
        "NORTH AMERICAN": "NAC",
        "SUB-SAHARAN AFRICA": "SSF",  # Use SSF (excl. high income) - has data for poverty indicators
        "SUB SAHARAN AFRICA": "SSF",
        "AFRICA": "AFR",
        "AFRICAN": "AFR",
        "AFRICAN COUNTRIES": "AFR",
        "EUROPEAN": "ECS",
        "EUROPE": "ECS",
        "EUROPEAN UNION": "ECS",
        "EU": "ECS",
        "WORLD": "WLD",
        "GLOBAL": "WLD",
        "GLOBALLY": "WLD",

        # Regional groups
        # Note: ASEAN expanded to individual countries via COUNTRY_GROUP_EXPANSIONS
        "SOUTH AMERICA": "LCN",  # Latin America & Caribbean includes South America
        "SOUTH AMERICAN": "LCN",
        "SOUTH AMERICAN COUNTRIES": "LCN",

        # Income/development levels
        "DEVELOPING COUNTRIES": "LMY",  # Low & middle income
        "DEVELOPING NATIONS": "LMY",
        "DEVELOPING ECONOMIES": "LMY",  # Added for "developing economies inflation" queries
        "DEVELOPED COUNTRIES": "HIC",  # High income
        "DEVELOPED NATIONS": "HIC",
        "DEVELOPED ECONOMIES": "HIC",  # Added for consistency
        "EMERGING MARKETS": "LMY",
        "EMERGING ECONOMIES": "LMY",
        "LOW-INCOME COUNTRIES": "LIC",
        "LOW INCOME COUNTRIES": "LIC",
        "MIDDLE-INCOME COUNTRIES": "MIC",
        "MIDDLE INCOME COUNTRIES": "MIC",
        "HIGH-INCOME COUNTRIES": "HIC",
        "HIGH INCOME COUNTRIES": "HIC",

        # Special groupings
        "LEAST DEVELOPED COUNTRIES": "LIC",
        "LEAST DEVELOPED NATIONS": "LIC",
    }

    INDICATOR_MAPPINGS: Dict[str, str] = {
        # GDP Indicators
        "GDP": "NY.GDP.MKTP.CD",
        "GDP_GROWTH": "NY.GDP.MKTP.KD.ZG",
        "GDP_GROWTH_RATE": "NY.GDP.MKTP.KD.ZG",  # Same as GDP_GROWTH
        "GDP_PER_CAPITA": "NY.GDP.PCAP.CD",
        "GDP_GROWTH_PER_CAPITA": "NY.GDP.PCAP.KD.ZG",
        "GDP_PER_CAPITA_PPP": "NY.GDP.PCAP.PP.CD",  # GDP per capita, PPP (current international $)

        # Labor Market
        "UNEMPLOYMENT": "SL.UEM.TOTL.ZS",
        "UNEMPLOYMENT_RATE": "SL.UEM.TOTL.ZS",
        "YOUTH_UNEMPLOYMENT": "SL.UEM.1524.ZS",
        "YOUTH_UNEMPLOYMENT_RATE": "SL.UEM.1524.ZS",
        "LABOR_FORCE_PARTICIPATION": "SL.TLF.CACT.ZS",
        "FEMALE_LABOR_FORCE_PARTICIPATION": "SL.TLF.CACT.FE.ZS",
        "FEMALE_LABOR_FORCE_PARTICIPATION_RATE": "SL.TLF.CACT.FE.ZS",

        # Prices and Inflation
        "INFLATION": "FP.CPI.TOTL.ZG",
        "CPI": "FP.CPI.TOTL.ZG",

        # Population
        "POPULATION": "SP.POP.TOTL",
        "POPULATION_GROWTH": "SP.POP.GROW",
        "POPULATION_GROWTH_RATE": "SP.POP.GROW",
        "URBAN_POPULATION": "SP.URB.TOTL.IN.ZS",
        "URBAN_POPULATION_PERCENTAGE": "SP.URB.TOTL.IN.ZS",
        "URBAN_POPULATION_GROWTH": "SP.URB.GROW",  # Urban population growth (annual %)
        "URBAN_POPULATION_GROWTH_RATE": "SP.URB.GROW",

        # Health
        "LIFE_EXPECTANCY": "SP.DYN.LE00.IN",
        "LIFE_EXPECTANCY_AT_BIRTH": "SP.DYN.LE00.IN",
        "INFANT_MORTALITY": "SP.DYN.IMRT.IN",
        "INFANT_MORTALITY_RATE": "SP.DYN.IMRT.IN",
        "MORTALITY_UNDER_5": "SH.DYN.MORT",  # Mortality rate, under-5 (per 1,000 live births)
        "UNDER_5_MORTALITY": "SH.DYN.MORT",
        "HEALTH_EXPENDITURE": "SH.XPD.CHEX.GD.ZS",  # Current health expenditure (% of GDP)
        "HEALTH_EXPENDITURE_GDP": "SH.XPD.CHEX.GD.ZS",
        "HEALTH_EXPENDITURE_PERCENTAGE_GDP": "SH.XPD.CHEX.GD.ZS",
        "HEALTH_EXPENDITURE_AS_PERCENTAGE_OF_GDP": "SH.XPD.CHEX.GD.ZS",
        "HEALTH_EXPENDITURE_PER_CAPITA": "SH.XPD.CHEX.PC.CD",  # Current health expenditure per capita (current US$)
        "CURRENT_HEALTH_EXPENDITURE": "SH.XPD.CHEX.PC.CD",

        # Environment
        "CO2_EMISSIONS": "EN.GHG.CO2.PC.CE.AR5",  # Updated 2024: CO2 emissions per capita (AR5)
        "CO2_EMISSIONS_PER_CAPITA": "EN.GHG.CO2.PC.CE.AR5",
        "EMISSIONS": "EN.GHG.CO2.PC.CE.AR5",  # Default to CO2
        "EMISSIONS_PER_CAPITA": "EN.GHG.CO2.PC.CE.AR5",
        "CARBON_EMISSIONS": "EN.GHG.CO2.PC.CE.AR5",
        "CARBON_EMISSIONS_PER_CAPITA": "EN.GHG.CO2.PC.CE.AR5",
        "RENEWABLE_ENERGY": "EG.FEC.RNEW.ZS",
        "RENEWABLE_ENERGY_CONSUMPTION": "EG.FEC.RNEW.ZS",
        "RENEWABLE_ENERGY_CONSUMPTION_SHARE": "EG.FEC.RNEW.ZS",
        "RENEWABLE_ENERGY_SHARE": "EG.FEC.RNEW.ZS",
        "AGRICULTURAL_LAND": "AG.LND.AGRI.ZS",  # Agricultural land (% of land area)
        "AGRICULTURAL_LAND_PERCENTAGE": "AG.LND.AGRI.ZS",

        # Poverty and Development
        # NOTE: WorldBank changed poverty indicators in 2024
        # SI.POV.DDAY now means $3.00/day (2021 PPP), not $2.15/day
        "POVERTY_RATE": "SI.POV.DDAY",  # Poverty headcount ratio at $3.00 a day (2021 PPP)
        "POVERTY_HEADCOUNT": "SI.POV.DDAY",
        "EXTREME_POVERTY": "SI.POV.DDAY",
        "POVERTY_HEADCOUNT_RATIO": "SI.POV.DDAY",
        "POVERTY_RATES": "SI.POV.DDAY",  # Plural form

        # Inequality
        "GINI": "SI.POV.GINI",  # Gini index
        "GINI_INDEX": "SI.POV.GINI",
        "GINI_COEFFICIENT": "SI.POV.GINI",
        "INCOME_INEQUALITY": "SI.POV.GINI",
        "INEQUALITY": "SI.POV.GINI",

        # Water & Sanitation
        "CLEAN_WATER": "SH.H2O.SMDW.ZS",  # Safely managed drinking water (% of population)
        "CLEAN_WATER_ACCESS": "SH.H2O.SMDW.ZS",
        "ACCESS_TO_CLEAN_WATER": "SH.H2O.SMDW.ZS",
        "WATER_ACCESS": "SH.H2O.SMDW.ZS",
        "SAFE_WATER": "SH.H2O.SMDW.ZS",
        "DRINKING_WATER": "SH.H2O.SMDW.ZS",
        "BASIC_WATER": "SH.H2O.BASW.ZS",  # Basic drinking water (% of population)

        # Food Security
        "FOOD_SECURITY": "SN.ITK.DEFC.ZS",  # Prevalence of undernourishment (% of population)
        "FOOD_INSECURITY": "SN.ITK.DEFC.ZS",
        "UNDERNOURISHMENT": "SN.ITK.DEFC.ZS",
        "MALNUTRITION": "SN.ITK.DEFC.ZS",

        # Gender Equality
        "GENDER_EQUALITY": "SG.GEN.PARL.ZS",  # Proportion of seats held by women in parliament
        "GENDER_PARITY": "SG.GEN.PARL.ZS",
        "WOMEN_PARLIAMENT": "SG.GEN.PARL.ZS",
        "FEMALE_LABOR_FORCE": "SL.TLF.CACT.FE.ZS",  # Female labor force participation

        # Education
        "EDUCATION_EXPENDITURE": "SE.XPD.TOTL.GD.ZS",  # Government expenditure on education, total (% of GDP)
        "GOVERNMENT_EXPENDITURE_EDUCATION": "SE.XPD.TOTL.GD.ZS",
        "GOVERNMENT_EXPENDITURE_ON_EDUCATION": "SE.XPD.TOTL.GD.ZS",
        "EDUCATION_SPENDING": "SE.XPD.TOTL.GD.ZS",  # Common phrasing
        "EDUCATION_SPENDING_GDP": "SE.XPD.TOTL.GD.ZS",
        "EDUCATION_SPENDING_PERCENT_GDP": "SE.XPD.TOTL.GD.ZS",
        "EDUCATION_AS_PERCENT_OF_GDP": "SE.XPD.TOTL.GD.ZS",

        # School Enrollment - Primary (CRITICAL: Use SE.PRM.ENRR for rates, not HD.HCI.EYRS)
        "SCHOOL_ENROLLMENT_PRIMARY": "SE.PRM.NENR",
        "PRIMARY_SCHOOL_ENROLLMENT": "SE.PRM.NENR",
        "PRIMARY_SCHOOL_ENROLLMENT_RATE": "SE.PRM.ENRR",  # Gross enrollment ratio, primary
        "PRIMARY_SCHOOL_ENROLLMENT_RATES": "SE.PRM.ENRR",
        "PRIMARY_ENROLLMENT_RATE": "SE.PRM.ENRR",
        "PRIMARY_ENROLLMENT_RATES": "SE.PRM.ENRR",
        "PRIMARY_ENROLLMENT": "SE.PRM.ENRR",
        "PRIMARY_SCHOOL_ENROLMENT": "SE.PRM.ENRR",  # British spelling
        "PRIMARY_SCHOOL_ENROLMENT_RATE": "SE.PRM.ENRR",
        "PRIMARY_SCHOOL_ENROLMENT_RATES": "SE.PRM.ENRR",
        "ELEMENTARY_SCHOOL_ENROLLMENT": "SE.PRM.ENRR",
        "ELEMENTARY_ENROLLMENT": "SE.PRM.ENRR",

        # School Enrollment - Secondary
        "SCHOOL_ENROLLMENT_SECONDARY": "SE.SEC.NENR",
        "SECONDARY_SCHOOL_ENROLLMENT": "SE.SEC.NENR",
        "SECONDARY_SCHOOL_ENROLLMENT_RATE": "SE.SEC.ENRR",  # Gross enrollment ratio, secondary
        "SECONDARY_SCHOOL_ENROLLMENT_RATES": "SE.SEC.ENRR",
        "SECONDARY_ENROLLMENT_RATE": "SE.SEC.ENRR",
        "SECONDARY_ENROLLMENT_RATES": "SE.SEC.ENRR",
        "SECONDARY_ENROLLMENT": "SE.SEC.ENRR",
        "HIGH_SCHOOL_ENROLLMENT": "SE.SEC.ENRR",

        # Tertiary Education Enrollment
        "TERTIARY_ENROLLMENT": "SE.TER.ENRR",
        "TERTIARY_ENROLLMENT_RATE": "SE.TER.ENRR",
        "UNIVERSITY_ENROLLMENT": "SE.TER.ENRR",
        "HIGHER_EDUCATION_ENROLLMENT": "SE.TER.ENRR",

        # Literacy
        "LITERACY_RATE": "SE.ADT.LITR.ZS",
        "LITERACY": "SE.ADT.LITR.ZS",
        "ADULT_LITERACY_RATE": "SE.ADT.LITR.ZS",

        # Technology & Internet
        "INTERNET_USERS": "IT.NET.USER.ZS",  # Individuals using the Internet (% of population)
        "INTERNET_USERS_PERCENTAGE": "IT.NET.USER.ZS",
        "INTERNET_USERS_PERCENT_POPULATION": "IT.NET.USER.ZS",

        # Electricity Access
        "ACCESS_TO_ELECTRICITY": "EG.ELC.ACCS.ZS",  # Access to electricity (% of population)
        "ELECTRICITY_ACCESS": "EG.ELC.ACCS.ZS",
        "ACCESS_ELECTRICITY": "EG.ELC.ACCS.ZS",
        "ELECTRICITY_ACCESS_TOTAL": "EG.ELC.ACCS.ZS",
        "ELECTRICITY_ACCESS_RURAL": "EG.ELC.ACCS.RU.ZS",  # Access to electricity, rural (% of rural population)
        "ELECTRICITY_ACCESS_URBAN": "EG.ELC.ACCS.UR.ZS",  # Access to electricity, urban (% of urban population)
        "RURAL_ELECTRICITY_ACCESS": "EG.ELC.ACCS.RU.ZS",
        "URBAN_ELECTRICITY_ACCESS": "EG.ELC.ACCS.UR.ZS",

        # Trade and Investment
        "TRADE_GDP": "NE.TRD.GNFS.ZS",  # Trade (% of GDP)
        "TRADE": "NE.TRD.GNFS.ZS",
        "TRADE_BALANCE": "BN.GSR.GNFS.CD",  # External balance on goods and services (current US$)
        "TRADE_SURPLUS": "BN.GSR.GNFS.CD",
        "TRADE_DEFICIT": "BN.GSR.GNFS.CD",
        "NET_EXPORTS": "BN.GSR.GNFS.CD",
        "FDI": "BX.KLT.DINV.CD.WD",  # FDI, net inflows (BoP, current US$)
        "FDI_INFLOWS": "BX.KLT.DINV.CD.WD",
        "FDI_NET_INFLOWS": "BX.KLT.DINV.CD.WD",
        "FOREIGN_DIRECT_INVESTMENT": "BX.KLT.DINV.CD.WD",
        "FOREIGN_DIRECT_INVESTMENT_NET_INFLOWS": "BX.KLT.DINV.CD.WD",
        "FDI_FLOWS": "BX.KLT.DINV.CD.WD",  # Common phrasing
        "FOREIGN_DIRECT_INVESTMENT_FLOWS": "BX.KLT.DINV.CD.WD",
        "FOREIGN_INVESTMENT_FLOWS": "BX.KLT.DINV.CD.WD",
        "EXPORTS": "NE.EXP.GNFS.ZS",  # Exports (% of GDP)
        "IMPORTS": "NE.IMP.GNFS.ZS",  # Imports (% of GDP)
        "MERCHANDISE_EXPORTS": "TX.VAL.MRCH.CD.WT",  # Merchandise exports (current US$)
        "MERCHANDISE_EXPORTS_GDP": "TM.VAL.MRCH.CD.WT",  # Merchandise exports (% of GDP)

        # Foreign Exchange Reserves
        "FOREIGN_EXCHANGE_RESERVES": "FI.RES.TOTL.CD",  # Total reserves (includes gold, current US$)
        "FX_RESERVES": "FI.RES.TOTL.CD",
        "RESERVES": "FI.RES.TOTL.CD",
        "TOTAL_RESERVES": "FI.RES.TOTL.CD",
        "CURRENCY_RESERVES": "FI.RES.TOTL.CD",
        "INTERNATIONAL_RESERVES": "FI.RES.TOTL.CD",
        "FOREX_RESERVES": "FI.RES.TOTL.CD",

        # Agriculture and Industry
        "AGRICULTURE_VALUE_ADDED": "NV.AGR.TOTL.ZS",  # Agriculture, forestry, and fishing (% of GDP)
        "INDUSTRY_VALUE_ADDED": "NV.IND.TOTL.ZS",
        "SERVICES_VALUE_ADDED": "NV.SRV.TOTL.ZS",

        # Research and Development
        "R&D_EXPENDITURE": "GB.XPD.RSDV.GD.ZS",  # R&D expenditure (% of GDP)
        "RD_EXPENDITURE": "GB.XPD.RSDV.GD.ZS",
        "RESEARCH_EXPENDITURE": "GB.XPD.RSDV.GD.ZS",

        # Government Finance
        "GOVERNMENT_EXPENDITURE": "NE.CON.GOVT.ZS",  # General government final consumption (% of GDP)
        "TAX_REVENUE": "GC.TAX.TOTL.GD.ZS",
        "GOVERNMENT_DEBT": "GC.DOD.TOTL.GD.ZS",  # Central government debt, total (% of GDP)
        "GOVERNMENT_DEBT_TO_GDP": "GC.DOD.TOTL.GD.ZS",
        "GOVERNMENT_DEBT_TO_GDP_RATIO": "GC.DOD.TOTL.GD.ZS",
        "GOVERNMENT_DEBT_GDP_RATIO": "GC.DOD.TOTL.GD.ZS",
        "GOVERNMENT_DEBT_GDP": "GC.DOD.TOTL.GD.ZS",
        "DEBT_TO_GDP": "GC.DOD.TOTL.GD.ZS",
        "DEBT_TO_GDP_RATIO": "GC.DOD.TOTL.GD.ZS",
        "DEBT_GDP_RATIO": "GC.DOD.TOTL.GD.ZS",
        "CENTRAL_GOVERNMENT_DEBT": "GC.DOD.TOTL.GD.ZS",
        "GOVERNMENT_DEBT_(%_OF_GDP)": "GC.DOD.TOTL.GD.ZS",
        "GOVERNMENT_DEBT_(PERCENT_OF_GDP)": "GC.DOD.TOTL.GD.ZS",

        # Economic Structure
        "GROSS_CAPITAL_FORMATION": "NE.GDI.TOTL.ZS",  # Gross capital formation (% of GDP)
        "GROSS_CAPITAL_FORMATION_PERCENTAGE": "NE.GDI.TOTL.ZS",
        "GROSS_CAPITAL_FORMATION_GDP": "NE.GDI.TOTL.ZS",
        "HOUSEHOLD_CONSUMPTION": "NE.CON.PRVT.ZS",  # Household final consumption (% of GDP)
        "SAVINGS_RATE": "NY.GNS.ICTR.ZS",  # Gross savings (% of GDP)

        # Balance of Payments
        "CURRENT_ACCOUNT_BALANCE": "BN.CAB.XOKA.GD.ZS",  # Current account balance (% of GDP)
        "CURRENT_ACCOUNT": "BN.CAB.XOKA.GD.ZS",
        "EXTERNAL_DEBT": "DT.DOD.DECT.GN.ZS",  # External debt stocks (% of GNI)

        # Labor Productivity (GDP per person employed)
        # These are critical mappings to avoid false positives from metadata search
        "PRODUCTIVITY": "SL.GDP.PCAP.EM.KD",  # GDP per person employed (constant 2021 PPP $)
        "LABOR_PRODUCTIVITY": "SL.GDP.PCAP.EM.KD",
        "LABOUR_PRODUCTIVITY": "SL.GDP.PCAP.EM.KD",  # UK spelling
        "GDP_PER_WORKER": "SL.GDP.PCAP.EM.KD",
        "GDP_PER_PERSON_EMPLOYED": "SL.GDP.PCAP.EM.KD",
        "WORKER_PRODUCTIVITY": "SL.GDP.PCAP.EM.KD",
        "OUTPUT_PER_WORKER": "SL.GDP.PCAP.EM.KD",
        "ECONOMIC_PRODUCTIVITY": "SL.GDP.PCAP.EM.KD",

        # Sector-specific productivity (value added per worker)
        "AGRICULTURAL_PRODUCTIVITY": "NV.AGR.EMPL.KD",  # Agriculture value added per worker
        "AGRICULTURE_PRODUCTIVITY": "NV.AGR.EMPL.KD",
        "FARM_PRODUCTIVITY": "NV.AGR.EMPL.KD",
        "INDUSTRY_PRODUCTIVITY": "NV.IND.EMPL.KD",  # Industry value added per worker
        "INDUSTRIAL_PRODUCTIVITY": "NV.IND.EMPL.KD",
        "MANUFACTURING_PRODUCTIVITY": "NV.IND.EMPL.KD",
        "SERVICES_PRODUCTIVITY": "NV.SRV.EMPL.KD",  # Services value added per worker
        "SERVICE_SECTOR_PRODUCTIVITY": "NV.SRV.EMPL.KD",

        # Labor productivity growth
        "PRODUCTIVITY_GROWTH": "SL.GDP.PCAP.EM.KD.ZG",
        "LABOR_PRODUCTIVITY_GROWTH": "SL.GDP.PCAP.EM.KD.ZG",
        "LABOUR_PRODUCTIVITY_GROWTH": "SL.GDP.PCAP.EM.KD.ZG",  # UK spelling

        # Common ambiguous terms - explicit mappings to prevent false positives
        "GROWTH": "NY.GDP.MKTP.KD.ZG",  # Default to GDP growth (most common meaning)
        "ECONOMIC_GROWTH": "NY.GDP.MKTP.KD.ZG",
        "OUTPUT": "NY.GDP.MKTP.CD",  # Default to GDP (most common meaning)
        "ECONOMIC_OUTPUT": "NY.GDP.MKTP.CD",
        "EFFICIENCY": "SL.GDP.PCAP.EM.KD",  # Usually means productivity
        "ECONOMIC_EFFICIENCY": "SL.GDP.PCAP.EM.KD",
        "INCOME": "NY.GNP.PCAP.CD",  # GNI per capita (most common meaning)
        "NATIONAL_INCOME": "NY.GNP.MKTP.CD",  # GNI total
        "PER_CAPITA_INCOME": "NY.GNP.PCAP.CD",  # GNI per capita
        "INVESTMENT": "NE.GDI.TOTL.ZS",  # Gross capital formation (most common meaning)
        "CAPITAL_INVESTMENT": "NE.GDI.TOTL.ZS",
        "FOREIGN_INVESTMENT": "BX.KLT.DINV.CD.WD",  # FDI inflows
    }

    # Country group expansions - maps group names to lists of country codes
    # This enables queries like "G7 countries", "Nordic countries", etc.
    COUNTRY_GROUP_EXPANSIONS: Dict[str, List[str]] = {
        # G7 (7 major advanced economies)
        "G7": ["USA", "GBR", "FRA", "DEU", "ITA", "CAN", "JPN"],
        "G7_COUNTRIES": ["USA", "GBR", "FRA", "DEU", "ITA", "CAN", "JPN"],
        "G7 COUNTRIES": ["USA", "GBR", "FRA", "DEU", "ITA", "CAN", "JPN"],
        "GROUP_OF_SEVEN": ["USA", "GBR", "FRA", "DEU", "ITA", "CAN", "JPN"],
        "GROUP OF SEVEN": ["USA", "GBR", "FRA", "DEU", "ITA", "CAN", "JPN"],

        # Nordic countries
        "NORDIC": ["SWE", "NOR", "DNK", "FIN", "ISL"],
        "NORDIC_COUNTRIES": ["SWE", "NOR", "DNK", "FIN", "ISL"],
        "NORDIC COUNTRIES": ["SWE", "NOR", "DNK", "FIN", "ISL"],
        "SCANDINAVIA": ["SWE", "NOR", "DNK"],
        "SCANDINAVIAN_COUNTRIES": ["SWE", "NOR", "DNK"],
        "SCANDINAVIAN COUNTRIES": ["SWE", "NOR", "DNK"],

        # African countries (major economies)
        "AFRICAN": ["ZAF", "NGA", "EGY", "KEN", "ETH", "GHA", "MAR", "TZA", "DZA", "AGO"],
        "AFRICAN_COUNTRIES": ["ZAF", "NGA", "EGY", "KEN", "ETH", "GHA", "MAR", "TZA", "DZA", "AGO"],
        "AFRICAN COUNTRIES": ["ZAF", "NGA", "EGY", "KEN", "ETH", "GHA", "MAR", "TZA", "DZA", "AGO"],

        # East Asian economies
        "EAST_ASIAN": ["CHN", "JPN", "KOR", "TWN", "HKG", "SGP"],
        "EAST_ASIAN_ECONOMIES": ["CHN", "JPN", "KOR", "TWN", "HKG", "SGP"],
        "EAST ASIAN ECONOMIES": ["CHN", "JPN", "KOR", "TWN", "HKG", "SGP"],
        "EAST_ASIAN_COUNTRIES": ["CHN", "JPN", "KOR", "TWN", "HKG", "SGP"],
        "EAST ASIAN COUNTRIES": ["CHN", "JPN", "KOR", "TWN", "HKG", "SGP"],
        "EAST_ASIA": ["CHN", "JPN", "KOR", "TWN", "HKG", "SGP"],

        # BRICS
        "BRICS": ["BRA", "RUS", "IND", "CHN", "ZAF"],
        "BRICS_COUNTRIES": ["BRA", "RUS", "IND", "CHN", "ZAF"],
        "BRICS COUNTRIES": ["BRA", "RUS", "IND", "CHN", "ZAF"],

        # BRICS+ (2024 expansion - includes Egypt, Ethiopia, Iran, UAE)
        "BRICS_PLUS": ["BRA", "RUS", "IND", "CHN", "ZAF", "EGY", "ETH", "IRN", "ARE"],
        "BRICS+": ["BRA", "RUS", "IND", "CHN", "ZAF", "EGY", "ETH", "IRN", "ARE"],
        "BRICS PLUS": ["BRA", "RUS", "IND", "CHN", "ZAF", "EGY", "ETH", "IRN", "ARE"],

        # ASEAN (10 member countries)
        "ASEAN": ["IDN", "THA", "MYS", "SGP", "PHL", "VNM", "MMR", "KHM", "LAO", "BRN"],
        "ASEAN_COUNTRIES": ["IDN", "THA", "MYS", "SGP", "PHL", "VNM", "MMR", "KHM", "LAO", "BRN"],
        "ASEAN COUNTRIES": ["IDN", "THA", "MYS", "SGP", "PHL", "VNM", "MMR", "KHM", "LAO", "BRN"],
        "SOUTHEAST_ASIAN": ["IDN", "THA", "MYS", "SGP", "PHL", "VNM", "MMR", "KHM", "LAO", "BRN"],
        "SOUTHEAST ASIAN": ["IDN", "THA", "MYS", "SGP", "PHL", "VNM", "MMR", "KHM", "LAO", "BRN"],

        # Top 10 CO2 emitters (approximate, based on recent data)
        "TOP_10_EMITTERS": ["CHN", "USA", "IND", "RUS", "JPN", "DEU", "IRN", "KOR", "SAU", "IDN"],
        "TOP_EMITTERS": ["CHN", "USA", "IND", "RUS", "JPN", "DEU", "IRN", "KOR", "SAU", "IDN"],
        "TOP 10 EMITTERS": ["CHN", "USA", "IND", "RUS", "JPN", "DEU", "IRN", "KOR", "SAU", "IDN"],

        # European Union (major members)
        "EU": ["DEU", "FRA", "ITA", "ESP", "NLD", "POL", "BEL", "SWE", "AUT", "GRC",
               "PRT", "CZE", "ROU", "HUN", "DNK", "FIN", "IRL"],
        "EUROPEAN_UNION": ["DEU", "FRA", "ITA", "ESP", "NLD", "POL", "BEL", "SWE", "AUT", "GRC",
                          "PRT", "CZE", "ROU", "HUN", "DNK", "FIN", "IRL"],
        "EUROPEAN UNION": ["DEU", "FRA", "ITA", "ESP", "NLD", "POL", "BEL", "SWE", "AUT", "GRC",
                          "PRT", "CZE", "ROU", "HUN", "DNK", "FIN", "IRL"],
        "EUROPEAN_COUNTRIES": ["DEU", "FRA", "ITA", "ESP", "GBR", "NLD", "POL", "BEL", "SWE", "AUT",
                               "GRC", "PRT", "CHE", "NOR", "DNK", "FIN", "IRL"],  # Includes non-EU GBR, CHE, NOR
        "EUROPEAN COUNTRIES": ["DEU", "FRA", "ITA", "ESP", "GBR", "NLD", "POL", "BEL", "SWE", "AUT",
                               "GRC", "PRT", "CHE", "NOR", "DNK", "FIN", "IRL"],

        # G20 (major economies)
        "G20": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN",
                "KOR", "RUS", "AUS", "ESP", "MEX", "IDN", "TUR", "SAU", "ARG", "ZAF"],
        "G20_COUNTRIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN",
                         "KOR", "RUS", "AUS", "ESP", "MEX", "IDN", "TUR", "SAU", "ARG", "ZAF"],
        "G20 COUNTRIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN",
                         "KOR", "RUS", "AUS", "ESP", "MEX", "IDN", "TUR", "SAU", "ARG", "ZAF"],

        # Major/Top economies (by GDP)
        "MAJOR_ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],
        "MAJOR ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],
        "TOP_ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],
        "TOP ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],
        "TOP_10_ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],
        "TOP 10 ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],
        "LARGEST_ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],
        "LARGEST ECONOMIES": ["USA", "CHN", "JPN", "DEU", "IND", "GBR", "FRA", "ITA", "BRA", "CAN"],

        # Baltic states
        "BALTIC": ["EST", "LVA", "LTU"],
        "BALTIC_STATES": ["EST", "LVA", "LTU"],
        "BALTIC STATES": ["EST", "LVA", "LTU"],

        # OECD countries (major subset - 38 members total, showing main ones)
        "OECD": ["USA", "CAN", "MEX", "GBR", "DEU", "FRA", "ITA", "ESP", "NLD", "BEL",
                 "AUT", "CHE", "SWE", "NOR", "DNK", "FIN", "ISL", "IRL", "PRT", "GRC",
                 "POL", "CZE", "HUN", "SVK", "SVN", "EST", "LVA", "LTU",
                 "JPN", "KOR", "AUS", "NZL", "TUR", "ISR", "CHL", "CRI", "COL"],
        "OECD_COUNTRIES": ["USA", "CAN", "MEX", "GBR", "DEU", "FRA", "ITA", "ESP", "NLD", "BEL",
                          "AUT", "CHE", "SWE", "NOR", "DNK", "FIN", "ISL", "IRL", "PRT", "GRC",
                          "POL", "CZE", "HUN", "SVK", "SVN", "EST", "LVA", "LTU",
                          "JPN", "KOR", "AUS", "NZL", "TUR", "ISR", "CHL", "CRI", "COL"],
        "OECD COUNTRIES": ["USA", "CAN", "MEX", "GBR", "DEU", "FRA", "ITA", "ESP", "NLD", "BEL",
                          "AUT", "CHE", "SWE", "NOR", "DNK", "FIN", "ISL", "IRL", "PRT", "GRC",
                          "POL", "CZE", "HUN", "SVK", "SVN", "EST", "LVA", "LTU",
                          "JPN", "KOR", "AUS", "NZL", "TUR", "ISR", "CHL", "CRI", "COL"],

        # Oil exporting countries (OPEC+ major members)
        "OIL_EXPORTING": ["SAU", "RUS", "USA", "IRQ", "ARE", "CAN", "IRN", "KWT", "NGA", "QAT"],
        "OIL_EXPORTING_COUNTRIES": ["SAU", "RUS", "USA", "IRQ", "ARE", "CAN", "IRN", "KWT", "NGA", "QAT"],
        "OIL EXPORTING COUNTRIES": ["SAU", "RUS", "USA", "IRQ", "ARE", "CAN", "IRN", "KWT", "NGA", "QAT"],
        "OPEC": ["SAU", "IRQ", "ARE", "IRN", "KWT", "NGA", "VEN", "DZA", "AGO", "LBY", "ECU", "GAB", "GNQ"],
        "OPEC_COUNTRIES": ["SAU", "IRQ", "ARE", "IRN", "KWT", "NGA", "VEN", "DZA", "AGO", "LBY", "ECU", "GAB", "GNQ"],
        "OPEC COUNTRIES": ["SAU", "IRQ", "ARE", "IRN", "KWT", "NGA", "VEN", "DZA", "AGO", "LBY", "ECU", "GAB", "GNQ"],
    }

    COUNTRY_MAPPINGS: Dict[str, str] = {
        # Common abbreviations
        "US": "USA",
        "USA": "USA",
        "UK": "GBR",
        "GB": "GBR",
        "UAE": "ARE",

        # ISO2 codes (World Bank API accepts both ISO2 and ISO3)
        "DE": "DE", "FR": "FR", "JP": "JP", "CN": "CN", "IN": "IN",
        "CA": "CA", "BR": "BR", "RU": "RU", "AU": "AU", "ES": "ES",
        "PT": "PT", "SE": "SE", "HR": "HR", "ID": "ID", "MX": "MX",
        "ZA": "ZA", "VN": "VN", "PH": "PH", "TR": "TR", "PL": "PL",
        "EG": "EG", "BD": "BD", "KR": "KR", "NO": "NO", "DK": "DK",
        "FI": "FI", "IE": "IE", "NG": "NG", "TH": "TH", "AR": "AR",
        "IT": "IT", "NL": "NL", "BE": "BE", "AT": "AT", "GR": "GR",
        "CH": "CH", "SG": "SG", "MY": "MY", "PK": "PK", "CL": "CL",
        "CO": "CO", "PE": "PE", "VE": "VE", "CZ": "CZ", "HU": "HU",
        "RO": "RO", "UA": "UA", "IL": "IL", "SA": "SA", "NZ": "NZ",

        # Comprehensive country names to ISO2 codes
        # Americas
        "UNITED_STATES": "USA", "AMERICA": "USA", "UNITED_STATES_OF_AMERICA": "USA",
        "CANADA": "CA", "MEXICO": "MX", "BRAZIL": "BR", "ARGENTINA": "AR",
        "CHILE": "CL", "COLOMBIA": "CO", "PERU": "PE", "VENEZUELA": "VE",
        "ECUADOR": "EC", "BOLIVIA": "BO", "PARAGUAY": "PY", "URUGUAY": "UY",
        "COSTA_RICA": "CR", "PANAMA": "PA", "CUBA": "CU", "DOMINICAN_REPUBLIC": "DO",
        "PUERTO_RICO": "PR", "GUATEMALA": "GT", "HONDURAS": "HN", "EL_SALVADOR": "SV",
        "NICARAGUA": "NI", "JAMAICA": "JM", "TRINIDAD_AND_TOBAGO": "TT", "HAITI": "HT",

        # Europe
        "GERMANY": "DE", "FRANCE": "FR", "UNITED_KINGDOM": "GBR", "BRITAIN": "GBR",
        "ITALY": "IT", "SPAIN": "ES", "NETHERLANDS": "NL", "HOLLAND": "NL",
        "BELGIUM": "BE", "AUSTRIA": "AT", "SWITZERLAND": "CH", "SWEDEN": "SE",
        "NORWAY": "NO", "DENMARK": "DK", "FINLAND": "FI", "IRELAND": "IE",
        "PORTUGAL": "PT", "GREECE": "GR", "POLAND": "PL", "CZECH_REPUBLIC": "CZ",
        "CZECHIA": "CZ", "HUNGARY": "HU", "ROMANIA": "RO", "UKRAINE": "UA",
        "CROATIA": "HR", "SLOVAKIA": "SK", "SLOVENIA": "SI", "BULGARIA": "BG",
        "SERBIA": "RS", "BOSNIA": "BA", "ALBANIA": "AL", "NORTH_MACEDONIA": "MK",
        "MACEDONIA": "MK", "MONTENEGRO": "ME", "KOSOVO": "XK", "LATVIA": "LV",
        "LITHUANIA": "LT", "ESTONIA": "EE", "ICELAND": "IS", "LUXEMBOURG": "LU",
        "MALTA": "MT", "CYPRUS": "CY", "MOLDOVA": "MD", "BELARUS": "BY",

        # Asia
        "CHINA": "CN", "JAPAN": "JP", "SOUTH_KOREA": "KR", "KOREA": "KR",
        "NORTH_KOREA": "KP", "INDIA": "IN", "INDONESIA": "ID", "PAKISTAN": "PK",
        "BANGLADESH": "BD", "VIETNAM": "VN", "THAILAND": "TH", "PHILIPPINES": "PH",
        "MALAYSIA": "MY", "SINGAPORE": "SG", "MYANMAR": "MM", "BURMA": "MM",
        "CAMBODIA": "KH", "LAOS": "LA", "SRI_LANKA": "LK", "NEPAL": "NP",
        "TAIWAN": "TW", "HONG_KONG": "HK", "MONGOLIA": "MN", "BRUNEI": "BN",
        "TIMOR_LESTE": "TL", "MALDIVES": "MV", "BHUTAN": "BT", "AFGHANISTAN": "AF",

        # Middle East
        "TURKEY": "TR", "TURKIYE": "TR", "IRAN": "IR", "IRAQ": "IQ",
        "SAUDI_ARABIA": "SA", "ISRAEL": "IL", "UNITED_ARAB_EMIRATES": "ARE",
        "QATAR": "QA", "KUWAIT": "KW", "OMAN": "OM", "BAHRAIN": "BH",
        "JORDAN": "JO", "LEBANON": "LB", "SYRIA": "SY", "YEMEN": "YE",
        "PALESTINE": "PS",

        # Africa
        "NIGERIA": "NG", "SOUTH_AFRICA": "ZA", "EGYPT": "EG", "KENYA": "KE",
        "ETHIOPIA": "ET", "GHANA": "GH", "TANZANIA": "TZ", "MOROCCO": "MA",
        "ALGERIA": "DZ", "TUNISIA": "TN", "LIBYA": "LY", "SUDAN": "SD",
        "UGANDA": "UG", "ANGOLA": "AO", "MOZAMBIQUE": "MZ", "ZIMBABWE": "ZW",
        "ZAMBIA": "ZM", "BOTSWANA": "BW", "NAMIBIA": "NA", "SENEGAL": "SN",
        "IVORY_COAST": "CI", "COTE_D_IVOIRE": "CI", "CAMEROON": "CM",
        "DEMOCRATIC_REPUBLIC_OF_CONGO": "CD", "DRC": "CD", "CONGO": "CG",
        "RWANDA": "RW", "MAURITIUS": "MU", "MADAGASCAR": "MG",

        # Oceania
        "AUSTRALIA": "AU", "NEW_ZEALAND": "NZ", "PAPUA_NEW_GUINEA": "PG",
        "FIJI": "FJ",

        # Russia/Central Asia
        "RUSSIA": "RU", "RUSSIAN_FEDERATION": "RU", "KAZAKHSTAN": "KZ",
        "UZBEKISTAN": "UZ", "TURKMENISTAN": "TM", "TAJIKISTAN": "TJ",
        "KYRGYZSTAN": "KG", "AZERBAIJAN": "AZ", "GEORGIA": "GE", "ARMENIA": "AM",
    }

    @property
    def provider_name(self) -> str:
        """Return canonical provider name for logging and routing."""
        return "WorldBank"

    def __init__(self, metadata_search_service: Optional["MetadataSearchService"] = None, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)  # Initialize BaseProvider
        settings = get_settings()
        self.base_url = settings.worldbank_base_url.rstrip("/")
        self.metadata_search = metadata_search_service

    async def _fetch_data(self, **params) -> NormalizedData | list[NormalizedData]:
        """Implementation of BaseProvider's abstract method.

        Routes to fetch_indicator with appropriate parameters.
        """
        indicator = params.get("indicator", "GDP")
        country = params.get("country") or params.get("region")
        countries = params.get("countries")
        start_date = params.get("start_date") or params.get("startDate")
        end_date = params.get("end_date") or params.get("endDate")

        return await self.fetch_indicator(
            indicator=indicator,
            country=country,
            countries=countries,
            start_date=start_date,
            end_date=end_date,
        )

    def _indicator_code(self, indicator: str) -> Optional[str]:
        key = indicator.upper().replace(" ", "_")
        return self.INDICATOR_MAPPINGS.get(key)

    def _map_regional_term(self, term: str) -> Optional[str]:
        """
        Map regional terms to WorldBank region codes.

        Args:
            term: Regional term (e.g., "South Asia", "developing countries")

        Returns:
            WorldBank region code if term is regional, None otherwise
        """
        term_upper = term.upper().strip()

        # CRITICAL: First check if this is a known country name or country CODE
        # Countries like "South Africa", "South Korea" should NOT be treated as regions
        # Also ISO codes like "DEU" should not match "EU" partial term
        term_key = term_upper.replace(" ", "_")
        if term_key in self.COUNTRY_MAPPINGS:
            logger.debug(f"'{term}' is a country (code: {self.COUNTRY_MAPPINGS[term_key]}), not a region")
            return None

        # Check if it's an ISO country code (2 or 3 letters)
        # ISO codes should NEVER be treated as regional terms
        if len(term_upper) <= 3 and term_upper.isalpha():
            # This looks like a country code (e.g., "DEU", "USA", "GB")
            # Don't try to match partial regional terms within it
            logger.debug(f"'{term}' looks like an ISO country code, not treating as region")
            return None

        # Direct lookup for exact regional term matches
        if term_upper in self.REGIONAL_TERM_MAPPINGS:
            region_code = self.REGIONAL_TERM_MAPPINGS[term_upper]
            logger.info(f"🌍 Mapped regional term '{term}' → WorldBank region code '{region_code}'")
            return region_code

        # Partial match (e.g., "countries in South Asia" → "SAS")
        # But only if the term is clearly about a region, not a country
        # AND the term is longer than a typical country code (> 3 chars)
        if len(term_upper) > 3:
            for regional_term, region_code in self.REGIONAL_TERM_MAPPINGS.items():
                if regional_term in term_upper:
                    # Additional safety: don't match partial region names within country names
                    # e.g., don't match "AFRICA" in "SOUTH AFRICA" or "ASIA" in "SOUTH KOREA"
                    # Check if the term starts with a known country prefix
                    known_country_prefixes = ["SOUTH AFRICA", "SOUTH KOREA", "NORTH KOREA",
                                              "CENTRAL AFRICAN", "WEST BANK"]
                    is_country = any(term_upper.startswith(prefix) or term_upper == prefix
                                    for prefix in known_country_prefixes)
                    if not is_country:
                        logger.info(f"🌍 Matched regional term '{regional_term}' in '{term}' → WorldBank region code '{region_code}'")
                        return region_code
                    else:
                        logger.debug(f"Skipping regional match for country: '{term}'")

        return None

    def _expand_country_group(self, country: str) -> Optional[List[str]]:
        """
        Check if the country string represents a country group and expand it.

        Uses CountryResolver as the single source of truth for region definitions.
        Falls back to WorldBank-specific mappings only for groups not in CountryResolver.

        Args:
            country: Country string (e.g., "G7", "Nordic countries")

        Returns:
            List of ISO3 country codes if it's a group, None otherwise
        """
        from ..routing.country_resolver import CountryResolver

        key = country.upper().replace(" ", "_")

        # Guardrail: if this already resolves to a concrete country code (e.g., US/USA),
        # do NOT attempt fuzzy group expansion.
        if CountryResolver.normalize(country):
            return None

        # First, try CountryResolver (single source of truth)
        expanded = CountryResolver.get_region_expansion(key, format="iso3")
        if expanded:
            logger.info(f"🌍 Expanded country group '{country}' via CountryResolver → {len(expanded)} countries: {', '.join(expanded)}")
            return expanded

        # Try partial match variants
        for variant in [key, key.replace("_COUNTRIES", ""), key.replace("_NATIONS", "")]:
            expanded = CountryResolver.get_region_expansion(variant, format="iso3")
            if expanded:
                logger.info(f"🌍 Matched country group '{variant}' via CountryResolver → {len(expanded)} countries: {', '.join(expanded)}")
                return expanded

        # Fall back to WorldBank-specific group expansions (for non-standard groups)
        if key in self.COUNTRY_GROUP_EXPANSIONS:
            countries = self.COUNTRY_GROUP_EXPANSIONS[key]
            logger.info(f"🌍 Expanded country group '{country}' via WorldBank mappings → {len(countries)} countries: {', '.join(countries)}")
            return countries

        # Check for partial matches in WorldBank-specific groups.
        # Only allow this for longer tokens to avoid false positives
        # like "US" matching "BRICS_PLUS".
        if len(key) < 4:
            return None

        for group_key, countries in self.COUNTRY_GROUP_EXPANSIONS.items():
            if group_key in key or key in group_key:
                logger.info(f"🌍 Matched country group '{group_key}' in '{country}' → {len(countries)} countries: {', '.join(countries)}")
                return countries

        return None

    def _country_code(self, country: str) -> str:
        """
        Convert country name/code to WorldBank API format.

        CENTRALIZED COUNTRY HANDLING: Uses CountryResolver as primary source,
        with fallback to WorldBank-specific regional/aggregate codes.

        Accepts:
        - ISO2/ISO3 country codes (e.g., "US", "USA", "CN")
        - Region codes (e.g., "SSA", "EAS", "WLD")
        - Income level codes (e.g., "HIC", "LMC")
        - Country names (e.g., "United States", "Germany")
        - Regional terms (e.g., "South Asia", "developing countries")

        Returns:
        - Uppercase country/region/aggregate code for API
        """
        # First, try to map regional terms (WorldBank-specific aggregates)
        region_code = self._map_regional_term(country)
        if region_code:
            return region_code

        country_upper = country.upper()

        # Check if it's a valid WorldBank region/aggregate code
        if country_upper in self.VALID_REGIONS:
            logger.debug(f"Using WorldBank region/aggregate code: {country_upper}")
            return country_upper

        # CENTRALIZED: Use CountryResolver for individual country normalization
        try:
            from ..routing.country_resolver import CountryResolver
            iso_code = CountryResolver.normalize(country)
            if iso_code:
                logger.debug(f"CountryResolver: '{country}' → '{iso_code}'")
                return iso_code
        except Exception as e:
            logger.debug(f"CountryResolver failed: {e}")

        # Fallback to local mappings
        key = country_upper.replace(" ", "_")
        mapped = self.COUNTRY_MAPPINGS.get(key)
        if mapped:
            return mapped

        # Default: return uppercase (might be ISO2/ISO3 code)
        logger.debug(f"Using country code as-is: {country_upper}")
        return country_upper

    async def _get_alternative_indicators(
        self, indicator: str, primary_code: str, limit: int = 5
    ) -> List[str]:
        """
        Get alternative indicator codes from the database for fallback.

        INFRASTRUCTURE FIX: When an indicator is archived or unavailable,
        this provides alternatives to try. This is a GENERAL mechanism that
        helps ALL queries hitting unavailable indicators.
        """
        try:
            from ..services.indicator_lookup import get_indicator_lookup
            lookup = get_indicator_lookup()
            results = lookup.search(indicator, provider='WorldBank', limit=limit + 1)

            # Return alternative codes, excluding the primary one we already tried
            alternatives = []
            for r in results:
                code = r.get('code')
                if code and code != primary_code and code not in alternatives:
                    alternatives.append(code)
                    if len(alternatives) >= limit:
                        break
            return alternatives
        except Exception as e:
            logger.debug(f"Could not get alternative indicators: {e}")
            return []

    async def fetch_indicator(
        self,
        indicator: str,
        country: Optional[str] = None,
        countries: Optional[List[str]] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        _skip_alternatives: bool = False,  # Internal flag to prevent recursion
    ) -> List[NormalizedData]:
        indic = await self._resolve_indicator_code(indicator)
        country_list = countries or [country or "USA"]

        # Expand country groups (e.g., "G7" → ["USA", "GBR", "FRA", ...])
        # ALWAYS expand groups to individual countries - region codes often fail
        expanded_countries: List[str] = []
        for country_item in country_list:
            # First, try explicit country group expansion
            group_expansion = self._expand_country_group(country_item)
            if group_expansion:
                expanded_countries.extend(group_expansion)
                continue

            # Check if this is a regional term that should map to a region code
            region_code = self._map_regional_term(country_item)
            if region_code:
                # Region codes often fail for many indicators (e.g., AFR doesn't work for population)
                # So we try region code first, but fall back to expanding it if possible
                # For now, use the region code - individual queries will handle failures
                expanded_countries.append(region_code)
                continue

            # Otherwise, use country as-is (might be ISO code or country name)
            expanded_countries.append(country_item)

        country_list = expanded_countries
        results: List[NormalizedData] = []

        # Add proper headers to avoid rate limiting and blocking
        headers = {
            "User-Agent": "econ-data-mcp/1.0 (https://openecon.ai; economic-data-aggregator)",
            "Accept": "application/json",
        }

        # Use shared HTTP client pool for better performance
        client = get_http_client()
        for country_code_raw in country_list:
            try:
                country_code = self._country_code(country_code_raw)
                url = f"{self.base_url}/country/{country_code}/indicator/{indic}"

                date_param = None
                if start_date and end_date:
                    date_param = f"{start_date[:4]}:{end_date[:4]}"

                params = {"format": "json", "per_page": 1000}
                if date_param:
                    params["date"] = date_param

                response = await client.get(url, params=params, headers=headers, timeout=30.0)
                response.raise_for_status()
                payload = response.json()

                # Check for error messages from World Bank API
                if isinstance(payload, list) and len(payload) > 0:
                    if isinstance(payload[0], dict) and "message" in payload[0]:
                        error_msg = payload[0]["message"]
                        if isinstance(error_msg, list) and len(error_msg) > 0:
                            error_detail = error_msg[0].get("value", "Unknown error")
                            logger.warning(
                                f"World Bank API error for country {country_code_raw} ({country_code}) "
                                f"indicator {indic}: {error_detail}. Skipping this country."
                            )
                            continue

                if len(payload) < 2 or not payload[1]:
                    logger.debug(f"No data for {country_code_raw} ({country_code}) indicator {indic}")
                    continue
            except httpx.HTTPError as e:
                error_msg = str(e)
                # Provide helpful error messages for common issues
                if "400" in error_msg:
                    logger.warning(
                        f"Bad Request (400) for {country_code_raw} ({country_code}). "
                        f"This may indicate an invalid country/region code or indicator combination. "
                        f"Skipping this country."
                    )
                elif "404" in error_msg:
                    logger.warning(
                        f"Not Found (404) for {country_code_raw} ({country_code}). "
                        f"Data may not be available for this country/region. Skipping."
                    )
                else:
                    logger.warning(
                        f"HTTP error fetching data for {country_code_raw} ({country_code}): {e}. "
                        f"Skipping this country."
                    )
                continue
            except Exception as e:
                logger.warning(
                    f"Error processing {country_code_raw}: {e}. Skipping this country."
                )
                continue

            records = payload[1]
            # Validate records array is non-empty before accessing
            if not records or len(records) == 0:
                logger.warning(f"Empty records for {country_code_raw}/{indic}. Skipping.")
                continue
            first_record = records[0]
            if not first_record or not isinstance(first_record, dict):
                logger.warning(f"Invalid first record for {country_code_raw}/{indic}. Skipping.")
                continue
            indicator_name = first_record.get("indicator", {}).get("value", indic)
            country_name = first_record.get("country", {}).get("value", country_code_raw)

            api_url = f"{url}?format=json&per_page=1000"
            if date_param:
                api_url += f"&date={date_param}"

            # Extract unit from indicator name (e.g., "GDP per capita, PPP (current international $)" → "current international $")
            unit = ""
            if "(" in indicator_name and ")" in indicator_name:
                unit = indicator_name[indicator_name.rfind("(")+1:indicator_name.rfind(")")]
            # Fallback: if no parentheses, check for common unit patterns
            elif "%" in indicator_name or "percent" in indicator_name.lower():
                unit = "%"
            elif "$" in indicator_name or "dollars" in indicator_name.lower():
                unit = "USD"

            # Human-readable URL for data verification on World Bank website
            source_url = f"https://data.worldbank.org/indicator/{indic}?locations={country_code}"

            # Determine data type from indicator name
            data_type = None
            indicator_lower = indicator_name.lower()
            if "growth" in indicator_lower or "% change" in indicator_lower:
                data_type = "Percent Change"
            elif "%" in indicator_name or "percent" in indicator_lower or "ratio" in indicator_lower:
                data_type = "Rate"
            elif "index" in indicator_lower:
                data_type = "Index"
            else:
                data_type = "Level"

            # Determine price type from indicator name
            price_type = None
            if "constant" in indicator_lower or "real" in indicator_lower:
                price_type = "Real (constant prices)"
            elif "current" in indicator_lower or "nominal" in indicator_lower:
                price_type = "Nominal (current prices)"
            elif "ppp" in indicator_lower:
                price_type = "PPP (purchasing power parity)"

            # Extract data range from records (safe access pattern)
            data_list = [
                {"date": f"{entry.get('date', 'unknown')}-01-01", "value": entry.get("value")}
                for entry in reversed(records)
                if isinstance(entry, dict) and entry.get("value") is not None and entry.get("date")
            ]

            # Skip countries/regions with no actual data values
            if not data_list:
                logger.debug(f"No data values for {country_code_raw} ({country_code}) indicator {indic} - all values null")
                continue

            # These are safe now due to the guard clause above
            start_date_val = data_list[0]["date"]
            end_date_val = data_list[-1]["date"]

            normalized = NormalizedData(
                metadata=Metadata(
                    source="World Bank",
                    indicator=indicator_name,
                    country=country_name,
                    frequency="annual",
                    unit=unit,
                    lastUpdated=response.headers.get("Date", ""),
                    seriesId=indic,  # Add seriesId with indicator code
                    apiUrl=api_url,
                    sourceUrl=source_url,
                    # Enhanced metadata fields
                    seasonalAdjustment=None,  # World Bank data is typically not seasonally adjusted (annual)
                    dataType=data_type,
                    priceType=price_type,
                    description=indicator_name,
                    notes=None,
                    startDate=start_date_val,
                    endDate=end_date_val,
                ),
                data=data_list,
            )
            results.append(normalized)

        # If no results found, try income aggregate fallback
        if not results:
            # Check if any of the original countries were income aggregates with fallbacks
            income_aggregates_tried = [c for c in country_list if c in self.INCOME_AGGREGATE_FALLBACKS]

            if income_aggregates_tried:
                # Try geographic region fallbacks for income aggregates
                logger.info(f"⚠️ Income aggregate(s) {income_aggregates_tried} returned no data for {indic}. Trying geographic region fallbacks...")

                fallback_regions = set()
                for agg in income_aggregates_tried:
                    fallback_regions.update(self.INCOME_AGGREGATE_FALLBACKS[agg])

                # Recursively fetch from fallback regions (avoid infinite recursion by not using income aggregates)
                fallback_results = []
                for region in fallback_regions:
                    try:
                        region_data = await self.fetch_indicator(
                            indicator=indic,  # Use resolved indicator code directly
                            country=region,
                            start_date=start_date,
                            end_date=end_date,
                        )
                        fallback_results.extend(region_data)
                    except DataNotAvailableError:
                        logger.debug(f"Fallback region {region} also has no data for {indic}")
                        continue
                    except Exception as e:
                        logger.debug(f"Error fetching fallback region {region}: {e}")
                        continue

                if fallback_results:
                    logger.info(f"✅ Income aggregate fallback succeeded: got data from {len(fallback_results)} geographic regions")
                    return fallback_results

            # INFRASTRUCTURE FIX: Try alternative indicators before giving up
            # This handles archived/unavailable indicators by trying similar ones
            if not _skip_alternatives:
                alternatives = await self._get_alternative_indicators(indicator, indic, limit=3)
                if alternatives:
                    logger.info(f"⚠️ Primary indicator {indic} failed. Trying {len(alternatives)} alternatives: {alternatives}")
                    for alt_code in alternatives:
                        try:
                            alt_results = await self.fetch_indicator(
                                indicator=alt_code,  # Use alternative code directly
                                countries=country_list,
                                start_date=start_date,
                                end_date=end_date,
                                _skip_alternatives=True,  # Prevent infinite recursion
                            )
                            if alt_results:
                                logger.info(f"✅ Alternative indicator succeeded: {alt_code}")
                                return alt_results
                        except DataNotAvailableError:
                            logger.debug(f"Alternative indicator {alt_code} also has no data")
                            continue
                        except Exception as e:
                            logger.debug(f"Error with alternative indicator {alt_code}: {e}")
                            continue

            # No fallback available or fallback also failed
            raise DataNotAvailableError(
                f"No data found for any of the requested countries for indicator {indic}. "
                f"The data may not be available for the specified countries or indicator."
            )

        return results

    async def _resolve_indicator_code(self, indicator: str) -> str:
        """Resolve WorldBank indicator code through IndicatorResolver (unified), hardcoded mappings, or metadata search."""
        # PHASE B: Use IndicatorResolver as the unified first attempt
        # This consolidates FTS5 search, translator, and catalog into one service
        try:
            from ..services.indicator_resolver import get_indicator_resolver
            resolver = get_indicator_resolver()
            resolved = resolver.resolve(indicator, provider="WorldBank")
            if resolved and resolved.confidence >= 0.7:
                logger.info(f"🔍 IndicatorResolver: WorldBank '{indicator}' → '{resolved.code}' (confidence: {resolved.confidence:.2f}, source: {resolved.source})")
                return resolved.code
        except Exception as e:
            logger.debug(f"IndicatorResolver failed, falling back: {e}")

        # Fallback: hardcoded mappings
        mapped = self._indicator_code(indicator)
        if mapped:
            return mapped

        # Allow users to supply raw WorldBank indicator codes directly
        if indicator and "." in indicator:
            return indicator

        # INFRASTRUCTURE FIX: Check indicator database FIRST
        # The database has 330K+ indicators with FTS5 search and subject/reference detection
        # This is more reliable than the World Bank REST API search for abbreviations like M2
        try:
            from ..services.indicator_lookup import get_indicator_lookup
            lookup = get_indicator_lookup()
            results = lookup.search(indicator, provider='WorldBank', limit=5)

            if results:
                # Use the top-ranked result - it already applies subject/reference scoring
                best = results[0]
                code = best.get('code')
                if code and best.get('_score', 0) > 20:  # Only use if score is confident
                    logger.info(f"✅ Indicator database resolved WorldBank '{indicator}' → {code}")
                    return code
        except Exception as e:
            logger.debug(f"Indicator database lookup failed: {e}")

        if not self.metadata_search:
            raise DataNotAvailableError(
                f"WorldBank indicator '{indicator}' not recognized. Provide the official indicator code (e.g., NY.GDP.MKTP.CD) or enable metadata discovery."
            )

        # Use hierarchical search: SDMX first, then WorldBank REST API
        search_results = await self.metadata_search.search_with_sdmx_fallback(
            provider="WorldBank",
            indicator=indicator,
        )
        if not search_results:
            raise DataNotAvailableError(
                f"WorldBank indicator '{indicator}' not found. Try another description or provide the official indicator code."
            )

        discovery = await self.metadata_search.discover_indicator(
            provider="WorldBank",
            indicator_name=indicator,
            search_results=search_results,
        )

        # Check if discovery returned ambiguity flag (multiple diverse options)
        if discovery and discovery.get("ambiguous"):
            options = discovery.get("options", [])
            options_text = "\n".join([
                f"  • {opt['name']}" for opt in options[:5]
            ])
            raise DataNotAvailableError(
                f"Your query '{indicator}' matches multiple datasets. Please be more specific:\n{options_text}\n\n"
                f"Try specifying the exact metric you need."
            )

        if discovery and discovery.get("code"):
            code = discovery["code"]
            key = indicator.upper().replace(" ", "_")
            self.INDICATOR_MAPPINGS[key] = code
            return code

        raise DataNotAvailableError(
            f"WorldBank indicator '{indicator}' not found. Try refining your query or use a known indicator name like GDP or Unemployment."
        )
