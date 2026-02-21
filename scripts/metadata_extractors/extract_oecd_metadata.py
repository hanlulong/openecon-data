#!/usr/bin/env python3
"""
OECD Metadata Extractor

Extracts indicators from OECD and stores them in a structured JSON format
for fast local lookups and semantic search.

OECD provides economic statistics for member countries and key partners.

API Documentation:
- SDMX JSON: https://www.oecd.org/en/data/insights/data-explainers/2024/09/api.html

UPDATED 2025-11-15: Switched from hardcoded list to API discovery
- Now fetches ALL dataflows from OECD SDMX API
- Discovers ~1,436 dataflows (vs. previous 110 hardcoded)
- Uses SDMX 2.1 XML format for complete metadata

Usage:
    python extract_oecd_metadata.py [--output FILE]
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List
import xml.etree.ElementTree as ET

# Configuration
OUTPUT_DIR = Path(__file__).parent.parent.parent / "backend" / "data" / "metadata"
DEFAULT_OUTPUT_FILE = OUTPUT_DIR / "oecd.json"

# SDMX API Configuration
OECD_SDMX_BASE_URL = "https://sdmx.oecd.org/public/rest"
REQUEST_TIMEOUT = 120.0  # Increased timeout for large XML responses

# SDMX 2.1 XML Namespaces
SDMX_NAMESPACES = {
    'mes': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/message',
    'str': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/structure',
    'com': 'http://www.sdmx.org/resources/sdmxml/schemas/v2_1/common'
}


def fetch_all_oecd_dataflows() -> List[Dict]:
    """
    Fetch all available dataflows from OECD SDMX API.

    Returns:
        List of dictionaries with dataflow metadata
    """
    try:
        import httpx
    except ImportError:
        print("❌ Error: httpx library not found. Install with: pip install httpx")
        raise

    print(f"\n🔍 Fetching all OECD dataflows from SDMX API...")
    print(f"   URL: {OECD_SDMX_BASE_URL}/dataflow")

    url = f"{OECD_SDMX_BASE_URL}/dataflow"
    headers = {
        "Accept": "application/vnd.sdmx.structure+xml; version=2.1"
    }

    try:
        # Fetch dataflow list
        response = httpx.get(url, headers=headers, timeout=REQUEST_TIMEOUT)

        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}: {response.text[:500]}")

        print(f"   ✅ Received {len(response.content):,} bytes")

        # Parse XML
        root = ET.fromstring(response.content)

        # Navigate to Structures -> Dataflows
        structures = root.find('.//mes:Structures', SDMX_NAMESPACES)
        if structures is None:
            raise Exception("No Structures element found in SDMX response")

        # Find Dataflows element
        dataflows_elem = structures.find('.//str:Dataflows', SDMX_NAMESPACES)
        if dataflows_elem is None:
            raise Exception("No Dataflows element found in SDMX response")

        # Get all Dataflow elements
        dataflows = dataflows_elem.findall('.//str:Dataflow', SDMX_NAMESPACES)

        print(f"   ✅ Found {len(dataflows)} dataflows")

        # Extract metadata from each dataflow
        extracted_dataflows = []

        # Add xml namespace for lang attribute
        xml_ns = '{http://www.w3.org/XML/1998/namespace}'

        for df in dataflows:
            df_id = df.get('id', 'N/A')
            agency_id = df.get('agencyID', 'N/A')
            version = df.get('version', 'N/A')

            # Get name - prefer English version
            name = df_id  # Default fallback
            name_elements = df.findall('.//com:Name', SDMX_NAMESPACES)
            for name_elem in name_elements:
                lang = name_elem.get(f'{xml_ns}lang', '')
                if lang == 'en':
                    name = name_elem.text
                    break
            # If no English, take first available
            if name == df_id and name_elements:
                name = name_elements[0].text or df_id

            # Get description - prefer English version
            description = ""
            desc_elements = df.findall('.//com:Description', SDMX_NAMESPACES)
            for desc_elem in desc_elements:
                lang = desc_elem.get(f'{xml_ns}lang', '')
                if lang == 'en':
                    description = desc_elem.text or ""
                    break
            # If no English, take first available
            if not description and desc_elements:
                description = desc_elements[0].text or ""

            # Determine category based on agency or name
            category = categorize_dataflow(agency_id, name, df_id)

            extracted_dataflows.append({
                "code": df_id,
                "agency": agency_id,
                "version": version,
                "name": name,
                "description": description,
                "category": category
            })

        return extracted_dataflows

    except Exception as e:
        print(f"   ❌ Error fetching dataflows: {e}")
        raise


def categorize_dataflow(agency: str, name: str, code: str) -> str:
    """
    Categorize a dataflow based on agency, name, and code.

    Args:
        agency: Agency ID (e.g., "OECD.SDD.NAD")
        name: Dataflow name
        code: Dataflow code

    Returns:
        Category string
    """
    name_upper = name.upper()
    code_upper = code.upper()

    # Agency-based categorization
    if "ESTAT" in agency:
        return "Eurostat"
    elif "IAEG-SDGs" in agency or "SDG" in code_upper:
        return "UN SDG"
    elif "OECD" in agency:
        # OECD sub-agencies
        if ".NAD" in agency or "GDP" in name_upper or "NATIONAL ACCOUNTS" in name_upper:
            return "National Accounts"
        elif ".TPS" in agency:
            if "UNEMPLOYMENT" in name_upper or "EMPLOYMENT" in name_upper or "LABOUR" in name_upper or "WAGE" in name_upper:
                return "Labor Market"
            elif "PRICE" in name_upper or "CPI" in name_upper or "INFLATION" in name_upper or "PPP" in name_upper:
                return "Prices"
            elif "TRADE" in name_upper or "EXPORT" in name_upper or "IMPORT" in name_upper:
                return "International Trade"
            elif "MEI" in code_upper or "CLI" in code_upper or "BCI" in code_upper or "INDUSTRIAL" in name_upper:
                return "Business"
            else:
                return "Economic Indicators"
        elif ".GOV" in agency or ".CTP" in agency:
            return "Government"
        elif ".ELS" in agency:
            if "HEALTH" in name_upper:
                return "Health"
            elif "EDUCATION" in name_upper:
                return "Education"
            else:
                return "Social"
        elif ".ENV" in agency:
            if "ENERGY" in name_upper:
                return "Energy"
            else:
                return "Environment"
        elif ".STI" in agency:
            return "Innovation"
        elif ".DAF" in agency:
            if "FDI" in name_upper or "INVESTMENT" in name_upper:
                return "International Trade"
            else:
                return "Finance"
        elif ".EDU" in agency:
            return "Education"

    # Fallback to name-based categorization
    if "TRADE" in name_upper or "EXPORT" in name_upper or "IMPORT" in name_upper:
        return "International Trade"
    elif "GDP" in name_upper or "NATIONAL ACCOUNTS" in name_upper:
        return "National Accounts"
    elif "PRICE" in name_upper or "INFLATION" in name_upper or "CPI" in name_upper:
        return "Prices"
    elif "EMPLOYMENT" in name_upper or "UNEMPLOYMENT" in name_upper or "LABOUR" in name_upper:
        return "Labor Market"
    elif "GOVERNMENT" in name_upper or "TAX" in name_upper or "FISCAL" in name_upper:
        return "Government"
    elif "HEALTH" in name_upper:
        return "Health"
    elif "EDUCATION" in name_upper:
        return "Education"
    elif "ENVIRONMENT" in name_upper or "EMISSION" in name_upper:
        return "Environment"
    elif "ENERGY" in name_upper:
        return "Energy"

    return "Other"


# ============================================================================
# DEPRECATED: Hardcoded list (replaced by API discovery)
# ============================================================================
# The following hardcoded list is kept for reference but is NO LONGER USED.
# The extractor now fetches all dataflows dynamically from the SDMX API.
#
# Previous count: 110 manually selected dataflows
# New count: ~1,436 dataflows discovered via API
# ============================================================================

_OLD_KEY_DATASETS = [
    # ===== National Accounts (12 datasets) =====
    {"code": "DF_NAAG_I", "agency": "OECD.SDD.NAD", "name": "National Accounts at a Glance", "category": "National Accounts", "description": "GDP and main aggregates"},
    {"code": "DF_QNA", "agency": "OECD.SDD.NAD", "name": "Quarterly National Accounts", "category": "National Accounts", "description": "Quarterly GDP and components"},
    {"code": "DF_SNA_TABLE1", "agency": "OECD.SDD.NAD", "name": "GDP Output approach", "category": "National Accounts", "description": "GDP by production approach"},
    {"code": "DF_SNA_TABLE2", "agency": "OECD.SDD.NAD", "name": "GDP Income approach", "category": "National Accounts", "description": "GDP by income approach"},
    {"code": "DF_SNA_TABLE3", "agency": "OECD.SDD.NAD", "name": "GDP Expenditure approach", "category": "National Accounts", "description": "GDP by expenditure approach"},
    {"code": "DF_SNA_TABLE6", "agency": "OECD.SDD.NAD", "name": "Final consumption expenditure", "category": "National Accounts", "description": "Household and government consumption"},
    {"code": "DF_SNA_TABLE8", "agency": "OECD.SDD.NAD", "name": "Gross fixed capital formation", "category": "National Accounts", "description": "Investment by asset type"},
    {"code": "DF_NAAG_GDP_PC", "agency": "OECD.SDD.NAD", "name": "GDP per capita", "category": "National Accounts", "description": "GDP per capita in USD PPP"},
    {"code": "DF_GDP_ANNPCT", "agency": "OECD.SDD.NAD", "name": "GDP growth rates", "category": "National Accounts", "description": "Annual GDP growth percentage"},
    {"code": "DF_REALGDP", "agency": "OECD.SDD.NAD", "name": "Real GDP", "category": "National Accounts", "description": "GDP at constant prices"},
    {"code": "DF_FINPOS", "agency": "OECD.SDD.NAD", "name": "Financial positions", "category": "National Accounts", "description": "Net lending/net borrowing by sector"},
    {"code": "DF_HOUSEHOLD_DEBT", "agency": "OECD.SDD.NAD", "name": "Household debt", "category": "National Accounts", "description": "Household debt as % of net disposable income"},

    # ===== Labor Market (18 datasets) =====
    {"code": "DF_IALFS_UNE_M", "agency": "OECD.SDD.TPS", "name": "Unemployment rates monthly", "category": "Labor Market", "description": "Monthly unemployment rate statistics"},
    {"code": "DF_IALFS_UNE_Q", "agency": "OECD.SDD.TPS", "name": "Unemployment rates quarterly", "category": "Labor Market", "description": "Quarterly unemployment statistics"},
    {"code": "DF_ALFS_EMP", "agency": "OECD.SDD.TPS", "name": "Employment", "category": "Labor Market", "description": "Employment statistics by demographic groups"},
    {"code": "DF_ALFS_POP_LABOUR", "agency": "OECD.SDD.TPS", "name": "Labour force statistics", "category": "Labor Market", "description": "Labour force participation rates"},
    {"code": "DF_AV_AN_WAGE", "agency": "OECD.SDD.TPS", "name": "Average wages", "category": "Labor Market", "description": "Average annual wages in USD PPP"},
    {"code": "DF_MIN_WAGE", "agency": "OECD.SDD.TPS", "name": "Minimum wages", "category": "Labor Market", "description": "Statutory minimum wages"},
    {"code": "DF_ALFS_EMP_FT_PT", "agency": "OECD.SDD.TPS", "name": "Full-time part-time employment", "category": "Labor Market", "description": "Employment by working time"},
    {"code": "DF_ANHRS", "agency": "OECD.SDD.TPS", "name": "Hours worked", "category": "Labor Market", "description": "Average annual hours worked per worker"},
    {"code": "DF_LFS_SEXAGE_I_R", "agency": "OECD.SDD.TPS", "name": "Employment by age and sex", "category": "Labor Market", "description": "Employment rates by demographic"},
    {"code": "DF_LFS_D_I", "agency": "OECD.SDD.TPS", "name": "Duration of unemployment", "category": "Labor Market", "description": "Long-term unemployment statistics"},
    {"code": "DF_EAR", "agency": "OECD.SDD.TPS", "name": "Earnings distribution", "category": "Labor Market", "description": "Distribution of gross earnings"},
    {"code": "DF_EARNINGS_INDUSTRY", "agency": "OECD.SDD.TPS", "name": "Earnings by industry", "category": "Labor Market", "description": "Average earnings by economic activity"},
    {"code": "DF_JOB_TENURE", "agency": "OECD.SDD.TPS", "name": "Job tenure", "category": "Labor Market", "description": "Average tenure in current job"},
    {"code": "DF_TEMP_EMPL", "agency": "OECD.SDD.TPS", "name": "Temporary employment", "category": "Labor Market", "description": "Share of temporary employees"},
    {"code": "DF_SELF_EMPL", "agency": "OECD.SDD.TPS", "name": "Self-employment", "category": "Labor Market", "description": "Self-employment rates"},
    {"code": "DF_YOUTH_UNE", "agency": "OECD.SDD.TPS", "name": "Youth unemployment", "category": "Labor Market", "description": "Unemployment rates ages 15-24"},
    {"code": "DF_GENDER_WAGE_GAP", "agency": "OECD.SDD.TPS", "name": "Gender wage gap", "category": "Labor Market", "description": "Gender pay gap in median earnings"},
    {"code": "DF_EMPLOYMENT_RATE", "agency": "OECD.SDD.TPS", "name": "Employment rate", "category": "Labor Market", "description": "Employment to population ratio"},

    # ===== Prices and Inflation (10 datasets) =====
    {"code": "DF_PRICES_ALL", "agency": "OECD.SDD.TPS", "name": "Consumer prices", "category": "Prices", "description": "CPI and inflation rates"},
    {"code": "DF_PRICES_CPI", "agency": "OECD.SDD.TPS", "name": "CPI all items", "category": "Prices", "description": "Consumer price index"},
    {"code": "DF_PRICES_FOOD", "agency": "OECD.SDD.TPS", "name": "Food prices", "category": "Prices", "description": "Food and non-alcoholic beverages CPI"},
    {"code": "DF_PRICES_ENERGY", "agency": "OECD.SDD.TPS", "name": "Energy prices", "category": "Prices", "description": "Energy component of CPI"},
    {"code": "DF_PPP", "agency": "OECD.SDD.TPS", "name": "Purchasing Power Parities", "category": "Prices", "description": "PPP conversion rates and price level indices"},
    {"code": "DF_PPI", "agency": "OECD.SDD.TPS", "name": "Producer price index", "category": "Prices", "description": "Producer prices in industry"},
    {"code": "DF_HOUSE_PRICES", "agency": "OECD.SDD.TPS", "name": "House prices", "category": "Prices", "description": "Residential property prices"},
    {"code": "DF_PRICE_LEVEL_INDICES", "agency": "OECD.SDD.TPS", "name": "Price level indices", "category": "Prices", "description": "Comparative price levels"},
    {"code": "DF_ULC", "agency": "OECD.SDD.TPS", "name": "Unit labour costs", "category": "Prices", "description": "ULC and components"},
    {"code": "DF_IMPORT_EXPORT_PRICES", "agency": "OECD.SDD.TPS", "name": "Import and export prices", "category": "Prices", "description": "Trade price indices"},

    # ===== International Trade (12 datasets) =====
    {"code": "DF_TRD", "agency": "OECD.SDD.TPS", "name": "International trade", "category": "International Trade", "description": "Merchandise trade statistics"},
    {"code": "DF_BTDIXE", "agency": "OECD.SDD.TPS", "name": "Trade in goods and services", "category": "International Trade", "description": "Balance of trade in national accounts"},
    {"code": "DF_TRADE_SERV", "agency": "OECD.SDD.TPS", "name": "Trade in services", "category": "International Trade", "description": "Services trade by category"},
    {"code": "DF_TIVA_2021", "agency": "OECD.SDD.TPS", "name": "Trade in value added", "category": "International Trade", "description": "Global value chains"},
    {"code": "DF_FDI_FLOW", "agency": "OECD.DAF.INV", "name": "FDI flows", "category": "International Trade", "description": "Foreign direct investment flows"},
    {"code": "DF_FDI_POS", "agency": "OECD.DAF.INV", "name": "FDI positions", "category": "International Trade", "description": "FDI stocks by partner country"},
    {"code": "DF_BERD_INDUSTRY", "agency": "OECD.SDD.TPS", "name": "Trade by industry", "category": "International Trade", "description": "Bilateral trade flows by industry"},
    {"code": "DF_REGION_TRADE", "agency": "OECD.SDD.TPS", "name": "Regional trade agreements", "category": "International Trade", "description": "Trade within regional blocs"},
    {"code": "DF_EXPORT_SHARE", "agency": "OECD.SDD.TPS", "name": "Export market shares", "category": "International Trade", "description": "Share of world exports"},
    {"code": "DF_TRADE_BALANCE", "agency": "OECD.SDD.TPS", "name": "Trade balance", "category": "International Trade", "description": "Exports minus imports"},
    {"code": "DF_CURRENT_ACCOUNT", "agency": "OECD.SDD.TPS", "name": "Current account balance", "category": "International Trade", "description": "Current account as % of GDP"},
    {"code": "DF_TERMS_OF_TRADE", "agency": "OECD.SDD.TPS", "name": "Terms of trade", "category": "International Trade", "description": "Export/import price ratio"},

    # ===== Government Finance (10 datasets) =====
    {"code": "DF_GOV", "agency": "OECD.GOV.PGE", "name": "Government at a Glance", "category": "Government", "description": "Government finance statistics"},
    {"code": "DF_REV", "agency": "OECD.CTP.TPSD", "name": "Revenue Statistics", "category": "Government", "description": "Tax revenue by type"},
    {"code": "DF_GOV_DEBT", "agency": "OECD.GOV.PGE", "name": "General government debt", "category": "Government", "description": "Government debt as % of GDP"},
    {"code": "DF_GOV_DEFICIT", "agency": "OECD.GOV.PGE", "name": "Government deficit", "category": "Government", "description": "Net lending/borrowing"},
    {"code": "DF_TAX_WEDGE", "agency": "OECD.CTP.TPSD", "name": "Tax wedge", "category": "Government", "description": "Tax burden on labour income"},
    {"code": "DF_SOCIAL_SPENDING", "agency": "OECD.ELS.SPD", "name": "Social expenditure", "category": "Government", "description": "Social spending by category"},
    {"code": "DF_GOV_REVENUE", "agency": "OECD.GOV.PGE", "name": "Government revenue", "category": "Government", "description": "Total government receipts"},
    {"code": "DF_GOV_SPENDING", "agency": "OECD.GOV.PGE", "name": "Government expenditure", "category": "Government", "description": "Total government spending"},
    {"code": "DF_TAX_GDP", "agency": "OECD.CTP.TPSD", "name": "Tax to GDP ratio", "category": "Government", "description": "Total tax revenue as % of GDP"},
    {"code": "DF_FISCAL_BALANCE", "agency": "OECD.GOV.PGE", "name": "Fiscal balance", "category": "Government", "description": "Underlying government fiscal balance"},

    # ===== Productivity and Innovation (8 datasets) =====
    {"code": "DF_PDBI", "agency": "OECD.SDD.NAD", "name": "Productivity", "category": "Productivity", "description": "Labour productivity indicators"},
    {"code": "DF_MULTIFACTOR_PRODUCTIVITY", "agency": "OECD.SDD.NAD", "name": "Multifactor productivity", "category": "Productivity", "description": "Total factor productivity growth"},
    {"code": "DF_RD_SPENDING", "agency": "OECD.STI.STP", "name": "R&D expenditure", "category": "Innovation", "description": "Research and development spending"},
    {"code": "DF_RD_PERSONNEL", "agency": "OECD.STI.STP", "name": "R&D personnel", "category": "Innovation", "description": "Researchers and R&D staff"},
    {"code": "DF_PATENTS", "agency": "OECD.STI.STP", "name": "Patents", "category": "Innovation", "description": "Patent applications and grants"},
    {"code": "DF_ICT_INVESTMENT", "agency": "OECD.STI.STP", "name": "ICT investment", "category": "Innovation", "description": "Investment in ICT equipment and software"},
    {"code": "DF_INNOVATION_OUTPUT", "agency": "OECD.STI.STP", "name": "Innovation outputs", "category": "Innovation", "description": "Product and process innovation"},
    {"code": "DF_STARTUP_RATES", "agency": "OECD.STI.STP", "name": "Business dynamics", "category": "Innovation", "description": "Firm entry and exit rates"},

    # ===== Business and Industry (10 datasets) =====
    {"code": "DF_MEI", "agency": "OECD.SDD.TPS", "name": "Main Economic Indicators", "category": "Business", "description": "Key short-term economic indicators"},
    {"code": "DF_CLI", "agency": "OECD.SDD.TPS", "name": "Composite Leading Indicators", "category": "Business", "description": "Leading indicators of economic activity"},
    {"code": "DF_BCI", "agency": "OECD.SDD.TPS", "name": "Business confidence index", "category": "Business", "description": "Business confidence surveys"},
    {"code": "DF_CCI", "agency": "OECD.SDD.TPS", "name": "Consumer confidence index", "category": "Business", "description": "Consumer confidence surveys"},
    {"code": "DF_INDUSTRIAL_PROD", "agency": "OECD.SDD.TPS", "name": "Industrial production", "category": "Business", "description": "Industrial production index"},
    {"code": "DF_RETAIL_TRADE", "agency": "OECD.SDD.TPS", "name": "Retail trade", "category": "Business", "description": "Volume of retail sales"},
    {"code": "DF_CONSTRUCTION", "agency": "OECD.SDD.TPS", "name": "Construction activity", "category": "Business", "description": "Construction output"},
    {"code": "DF_MANUFACTURING_PMI", "agency": "OECD.SDD.TPS", "name": "Manufacturing PMI", "category": "Business", "description": "Purchasing managers index"},
    {"code": "DF_CAPACITY_UTILIZATION", "agency": "OECD.SDD.TPS", "name": "Capacity utilization", "category": "Business", "description": "Industrial capacity utilization rates"},
    {"code": "DF_BUSINESS_INVESTMENT", "agency": "OECD.SDD.NAD", "name": "Business investment", "category": "Business", "description": "Private sector fixed capital formation"},

    # ===== Financial Markets (6 datasets) =====
    {"code": "DF_INTEREST_RATES", "agency": "OECD.SDD.TPS", "name": "Interest rates", "category": "Finance", "description": "Short and long-term interest rates"},
    {"code": "DF_EXCHANGE_RATES", "agency": "OECD.SDD.TPS", "name": "Exchange rates", "category": "Finance", "description": "Bilateral exchange rates"},
    {"code": "DF_STOCK_MARKET", "agency": "OECD.SDD.TPS", "name": "Share prices", "category": "Finance", "description": "Stock market indices"},
    {"code": "DF_MONEY_SUPPLY", "agency": "OECD.SDD.TPS", "name": "Monetary aggregates", "category": "Finance", "description": "Money supply indicators"},
    {"code": "DF_CREDIT_MARKETS", "agency": "OECD.SDD.TPS", "name": "Credit to private sector", "category": "Finance", "description": "Bank lending and credit growth"},
    {"code": "DF_FINANCIAL_BALANCE_SHEETS", "agency": "OECD.SDD.NAD", "name": "Financial balance sheets", "category": "Finance", "description": "Financial assets and liabilities by sector"},

    # ===== Education (6 datasets) =====
    {"code": "DF_EAG", "agency": "OECD.EDU.EAG", "name": "Education at a Glance", "category": "Education", "description": "Education indicators"},
    {"code": "DF_EDU_ATTAINMENT", "agency": "OECD.EDU.EAG", "name": "Educational attainment", "category": "Education", "description": "Population by level of education"},
    {"code": "DF_EDU_EXPENDITURE", "agency": "OECD.EDU.EAG", "name": "Education expenditure", "category": "Education", "description": "Spending on education"},
    {"code": "DF_STUDENT_PERFORMANCE", "agency": "OECD.EDU.EAG", "name": "Student performance", "category": "Education", "description": "PISA test scores"},
    {"code": "DF_TEACHERS", "agency": "OECD.EDU.EAG", "name": "Teachers", "category": "Education", "description": "Teacher salaries and student-teacher ratios"},
    {"code": "DF_TERTIARY_ENROLLMENT", "agency": "OECD.EDU.EAG", "name": "Tertiary enrollment", "category": "Education", "description": "University enrollment rates"},

    # ===== Health (5 datasets) =====
    {"code": "DF_SHA", "agency": "OECD.ELS.HD", "name": "Health Statistics", "category": "Health", "description": "Health expenditure and outcomes"},
    {"code": "DF_HEALTH_SPENDING", "agency": "OECD.ELS.HD", "name": "Health expenditure", "category": "Health", "description": "Health spending as % of GDP"},
    {"code": "DF_LIFE_EXPECTANCY", "agency": "OECD.ELS.HD", "name": "Life expectancy", "category": "Health", "description": "Life expectancy at birth"},
    {"code": "DF_HEALTH_RESOURCES", "agency": "OECD.ELS.HD", "name": "Health resources", "category": "Health", "description": "Doctors, nurses, hospital beds"},
    {"code": "DF_HEALTH_STATUS", "agency": "OECD.ELS.HD", "name": "Health status", "category": "Health", "description": "Mortality and morbidity indicators"},

    # ===== Social Indicators (6 datasets) =====
    {"code": "DF_INEQUALITY", "agency": "OECD.ELS.SPD", "name": "Income inequality", "category": "Social", "description": "Gini coefficient and income distribution"},
    {"code": "DF_POVERTY", "agency": "OECD.ELS.SPD", "name": "Poverty rates", "category": "Social", "description": "Relative poverty indicators"},
    {"code": "DF_PENSION_SPENDING", "agency": "OECD.ELS.SPD", "name": "Pension expenditure", "category": "Social", "description": "Public pension spending"},
    {"code": "DF_FAMILY_BENEFITS", "agency": "OECD.ELS.SPD", "name": "Family benefits", "category": "Social", "description": "Family cash benefits and services"},
    {"code": "DF_HOUSING_AFFORDABILITY", "agency": "OECD.ELS.SPD", "name": "Housing affordability", "category": "Social", "description": "Housing costs relative to income"},
    {"code": "DF_LIFE_SATISFACTION", "agency": "OECD.ELS.SPD", "name": "Life satisfaction", "category": "Social", "description": "Subjective well-being indicators"},

    # ===== Environment and Energy (7 datasets) =====
    {"code": "DF_AIR_GHG", "agency": "OECD.ENV.EPI", "name": "Greenhouse gas emissions", "category": "Environment", "description": "GHG emissions by source"},
    {"code": "DF_AIR_EMISSIONS", "agency": "OECD.ENV.EPI", "name": "Air emissions", "category": "Environment", "description": "SOx, NOx, particulates"},
    {"code": "DF_RENEWABLE_ENERGY", "agency": "OECD.ENV.EPI", "name": "Renewable energy", "category": "Energy", "description": "Share of renewables in energy supply"},
    {"code": "DF_ENERGY_SUPPLY", "agency": "OECD.ENV.EPI", "name": "Energy supply", "category": "Energy", "description": "Total primary energy supply"},
    {"code": "DF_ENERGY_USE", "agency": "OECD.ENV.EPI", "name": "Energy use", "category": "Energy", "description": "Final energy consumption"},
    {"code": "DF_CARBON_INTENSITY", "agency": "OECD.ENV.EPI", "name": "Carbon intensity", "category": "Environment", "description": "CO2 emissions per unit of GDP"},
    {"code": "DF_WASTE_GENERATION", "agency": "OECD.ENV.EPI", "name": "Waste generation", "category": "Environment", "description": "Municipal waste per capita"},
]


def generate_aliases(code: str, name: str, category: str) -> List[str]:
    """Generate searchable aliases for a dataset."""
    aliases = set()

    name_upper = name.upper()
    category_upper = category.upper()

    # Add code variants
    aliases.add(code.upper())
    aliases.add(f"OECD_{code.upper()}")

    # Add name variants
    aliases.add(name_upper)
    aliases.add(name_upper.replace(" ", "_"))

    # Add category
    aliases.add(category_upper)

    # Common economic indicator abbreviations
    if "GDP" in name_upper or "NATIONAL ACCOUNTS" in name_upper:
        aliases.add("GDP")
        aliases.add("NATIONAL_ACCOUNTS")
    if "UNEMPLOYMENT" in name_upper:
        aliases.add("UNEMPLOYMENT")
        aliases.add("UNEMPLOYMENT_RATE")
    if "EMPLOYMENT" in name_upper:
        aliases.add("EMPLOYMENT")
    if "WAGE" in name_upper or "EARNINGS" in name_upper:
        aliases.add("WAGES")
        aliases.add("EARNINGS")
    if "PRICE" in name_upper or "CPI" in name_upper or "INFLATION" in name_upper:
        aliases.add("CPI")
        aliases.add("INFLATION")
        aliases.add("PRICES")
    if "PPP" in name_upper or "PURCHASING POWER" in name_upper:
        aliases.add("PPP")
    if "TRADE" in name_upper:
        aliases.add("TRADE")
        if "EXPORT" in name_upper:
            aliases.add("EXPORTS")
        if "IMPORT" in name_upper:
            aliases.add("IMPORTS")
    if "GOVERNMENT" in name_upper or "GOV" in code:
        aliases.add("GOVERNMENT")
        if "REVENUE" in name_upper or "TAX" in name_upper:
            aliases.add("TAX")
            aliases.add("REVENUE")
    if "PRODUCTIVITY" in name_upper:
        aliases.add("PRODUCTIVITY")
        aliases.add("LABOUR_PRODUCTIVITY")
    if "ECONOMIC INDICATORS" in name_upper or "MEI" in code:
        aliases.add("MEI")
        aliases.add("ECONOMIC_INDICATORS")
    if "LEADING" in name_upper and "INDICATOR" in name_upper:
        aliases.add("CLI")
        aliases.add("LEADING_INDICATORS")
    if "EDUCATION" in name_upper:
        aliases.add("EDUCATION")
    if "HEALTH" in name_upper:
        aliases.add("HEALTH")
    if "ENVIRONMENT" in name_upper or "EMISSION" in name_upper:
        aliases.add("ENVIRONMENT")
        if "GHG" in name_upper or "GREENHOUSE" in name_upper:
            aliases.add("GHG")
            aliases.add("EMISSIONS")

    return list(aliases)


def process_datasets(datasets: List[Dict]) -> List[Dict]:
    """Process datasets into structured indicator format."""
    processed = []

    print(f"\n📊 Processing OECD datasets...")

    for dataset in datasets:
        code = dataset.get("code", "")
        name = dataset.get("name", "")
        category = dataset.get("category", "")
        description = dataset.get("description", "")
        agency = dataset.get("agency", "")

        if not code:
            continue

        # Generate aliases
        aliases = generate_aliases(code, name, category)

        # Create searchable text
        searchable_text = " ".join([
            code.lower(),
            name.lower(),
            category.lower(),
            description.lower(),
            agency.lower(),
            " ".join(aliases).lower()
        ])

        processed_indicator = {
            "id": f"OECD_{code}",
            "code": code,
            "name": name,
            "description": description,
            "category": category,
            "agency": agency,
            "type": "oecd_dataset",
            "source": "OECD",
            "aliases": aliases,
            "searchable_text": searchable_text
        }

        processed.append(processed_indicator)

    print(f"   ✅ Processed {len(processed)} OECD datasets")
    return processed


def build_search_index(indicators: List[Dict]) -> Dict[str, List[str]]:
    """Build search index mapping keywords to indicator IDs."""
    search_index = {}

    for indicator in indicators:
        indicator_id = indicator["id"]

        # Index all aliases
        for alias in indicator.get("aliases", []):
            keyword = alias.lower()
            if keyword not in search_index:
                search_index[keyword] = []
            if indicator_id not in search_index[keyword]:
                search_index[keyword].append(indicator_id)

        # Index words from name
        name = indicator.get("name", "")
        for word in name.split():
            if len(word) > 3:
                keyword = word.lower()
                if keyword not in search_index:
                    search_index[keyword] = []
                if indicator_id not in search_index[keyword]:
                    search_index[keyword].append(indicator_id)

    return search_index


def organize_by_category(indicators: List[Dict]) -> Dict[str, List[str]]:
    """Organize indicators by category."""
    categories = {}

    for indicator in indicators:
        category = indicator.get("category", "Other")
        if category not in categories:
            categories[category] = []
        categories[category].append(indicator["id"])

    return categories


def save_metadata(indicators: List[Dict], output_file: Path):
    """Save processed metadata to JSON file."""
    # Build search index
    search_index = build_search_index(indicators)

    # Organize by category
    categories = organize_by_category(indicators)

    # Build metadata structure
    metadata = {
        "provider": "OECD",
        "last_updated": datetime.now().isoformat() + "Z",
        "total_indicators": len(indicators),
        "base_url": "https://sdmx.oecd.org/public/rest",
        "categories": {
            category: {
                "count": len(indicator_ids),
                "indicators": indicator_ids
            }
            for category, indicator_ids in categories.items()
        },
        "indicators": indicators,
        "search_index": {k: v for k, v in search_index.items() if len(v) <= 50}
    }

    # Ensure output directory exists
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Save to file
    print(f"\n💾 Saving metadata to {output_file}...")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    # Print statistics
    file_size_mb = output_file.stat().st_size / (1024 * 1024)
    print(f"   ✅ Saved {len(indicators)} indicators ({file_size_mb:.2f} MB)")
    print(f"   📁 File: {output_file}")
    print(f"   🏷️  Categories: {len(categories)}")
    print(f"   🔍 Search keywords: {len(search_index):,}")


def main():
    """Main extraction workflow"""
    print("=" * 70)
    print("OECD Metadata Extractor - API Discovery Mode")
    print("=" * 70)
    print()

    try:
        # Fetch all dataflows from SDMX API
        dataflows = fetch_all_oecd_dataflows()

        if not dataflows:
            raise ValueError("No dataflows were fetched from OECD SDMX API.")

        print(f"\n📊 Processing {len(dataflows)} OECD dataflows...")

        # Process datasets
        all_indicators = process_datasets(dataflows)

        if not all_indicators:
            raise ValueError("No indicators were extracted.")

        # Save metadata
        save_metadata(all_indicators, DEFAULT_OUTPUT_FILE)

        print()
        print("=" * 70)
        print("✅ OECD metadata extraction complete!")
        print(f"   Extracted {len(all_indicators)} indicators (vs. previous 110)")
        print(f"   Improvement: {len(all_indicators) / 110:.1f}x more coverage")
        print("=" * 70)
        return 0

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
