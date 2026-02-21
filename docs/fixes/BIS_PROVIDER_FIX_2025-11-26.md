# BIS Provider Fix - November 26, 2025

## Problem Statement

BIS (Bank for International Settlements) provider had 87.5% failure rate (7 out of 8 queries failed).
All queries returned "data_not_available" errors.

## Root Cause Analysis

1. **Missing Indicator Mappings**: BIS provider lacked mappings for several common indicator names
   - Debt service ratio
   - Global liquidity indicators
   - International debt securities
   - Residential property price (exact phrasing)
   - Credit to non-financial sector (exact phrasing)

2. **Routing Issues**: Provider router didn't have BIS-specific keywords for financial stability indicators

3. **Special Dataflow Handling**: Some BIS dataflows (WS_GLI, WS_DEBT_SEC2_PUB) required special handling due to complex multi-dimensional structures

## Solutions Implemented

### 1. Added Indicator Mappings (`backend/providers/bis.py`)

Added missing mappings in `INDICATOR_MAPPINGS` dict:

```python
"DEBT_SERVICE_RATIO": "WS_DSR",
"DSR": "WS_DSR",
"DEBT_SERVICE": "WS_DSR",
"DEBT_SERVICE_RATIOS": "WS_DSR",

"GLOBAL_LIQUIDITY": "WS_GLI",
"LIQUIDITY": "WS_GLI",
"LIQUIDITY_INDICATORS": "WS_GLI",
"GLI": "WS_GLI",

"DEBT_SECURITIES": "WS_DEBT_SEC2_PUB",
"INTERNATIONAL_DEBT_SECURITIES": "WS_DEBT_SEC2_PUB",
"INTERNATIONAL_DEBT": "WS_DEBT_SEC2_PUB",
"DEBT_SEC": "WS_DEBT_SEC2_PUB",

"RESIDENTIAL_PROPERTY_PRICE": "WS_SPP",
"RESIDENTIAL_PROPERTY_PRICES": "WS_SPP",

"CREDIT_TO_NON-FINANCIAL_SECTOR": "WS_TC",
"CREDIT_TO_NON-FINANCIAL_SECTOR_AS_PERCENTAGE_OF_GDP": "WS_TC",
```

### 2. Special Handling for WS_GLI (`backend/providers/bis.py`)

- Created `_fetch_gli_data()` method for Global Liquidity Indicators
- GLI has multi-dimensional structure (currency, borrower country, sectors, lenders)
- Fetches aggregate measures in USD
- Returns "Global" as country (not country-specific)

### 3. Updated Series Selection (`backend/providers/bis.py`)

Added preferences in `_select_best_series()` for:
- WS_GLI: USD denomination, all countries, all sectors
- WS_DEBT_SEC2_PUB: All issuers, USD denomination

### 4. Enhanced Provider Routing (`backend/services/provider_router.py`)

Added BIS-specific keywords to `PROVIDER_KEYWORDS_PRIORITY`:

```python
"BIS": [
    # Existing keywords
    "house price to income", "property valuation",
    "housing valuation", "real estate valuation",
    "property market", "housing market valuation",
    # NEW KEYWORDS
    "credit to non-financial", "credit to gdp", "credit gap",
    "credit-to-gdp", "credit to gdp gap", "credit-to-gdp gap",
    "debt service ratio", "debt service",
    "residential property price", "property price index",
    "effective exchange rate", "exchange rate index",
    "global liquidity", "liquidity indicator",
    "international debt securities",
    "policy rate", "central bank policy rate",
    "commercial property price"
],
```

## Test Results

### Before Fix: 1/8 (12.5% success rate)
- Only 1 query (policy rates) worked
- 7 queries failed with "data_not_available"

### After Fix: 8/8 (100% success rate)

| Query | Indicator | Status |
|-------|-----------|--------|
| Query 66 | Credit to non-financial sector as % of GDP | ✅ SUCCESS |
| Query 67 | Residential property price index | ✅ SUCCESS |
| Query 68 | Debt service ratio for households | ✅ SUCCESS |
| Query 69 | Effective exchange rates | ✅ SUCCESS |
| Query 70 | Global liquidity indicators | ✅ SUCCESS |
| Query 71 | Credit-to-GDP gap | ✅ SUCCESS |
| Query 72 | International debt securities | ✅ SUCCESS |
| Query 73 | Central bank policy rates | ✅ SUCCESS |

### Example Queries

```bash
# Query 66
curl -X POST http://localhost:3001/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Show me credit to GDP ratio for the US from 2020 to 2023"}'

# Query 70
curl -X POST http://localhost:3001/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Show me global liquidity for the US from 2020 to 2023"}'

# Query 71
curl -X POST http://localhost:3001/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Show me credit-to-GDP gap for the US from 2020 to 2023"}'
```

## Files Modified

1. `/home/hanlulong/econ-data-mcp/backend/providers/bis.py`
   - Added 15+ new indicator mappings
   - Created `_fetch_gli_data()` method
   - Updated `_select_best_series()` with GLI and DEBT_SEC preferences
   - Set quarterly frequency for GLI and DEBT_SEC dataflows

2. `/home/hanlulong/econ-data-mcp/backend/services/provider_router.py`
   - Added 12 new BIS-specific keywords
   - Improved routing accuracy for financial stability indicators

## Impact

- **Success Rate**: 87.5% improvement (12.5% → 100%)
- **Fixed Queries**: 7 additional queries now work
- **Data Points Returned**: Average 16-22 data points per query
- **Provider Coverage**: BIS now handles all major financial stability indicators

## Notes

- BIS API uses SDMX format with complex multi-dimensional structures
- Some indicators (GLI, DEBT_SEC) don't follow standard country-based querying
- Quarterly frequency is enforced for WS_TC, WS_SPP, WS_DSR, WS_GLI, WS_DEBT_SEC2_PUB
- All data is fetched from https://stats.bis.org/api/v1/

## Future Improvements

1. Add more BIS dataflows (currently using 6 core dataflows)
2. Implement better series selection for multi-dimensional data
3. Add metadata search integration for BIS
4. Support more currency denominations for GLI (currently USD only)

## Verification Commands

```bash
# Test BIS provider directly
python3 << 'EOF'
import asyncio
import sys
sys.path.insert(0, '/home/hanlulong/econ-data-mcp')
from backend.providers.bis import BISProvider

async def test():
    provider = BISProvider()
    result = await provider.fetch_indicator('debt service ratio', country='US', start_year=2020)
    print(f"Data points: {len(result[0].data)}")

asyncio.run(test())
EOF

# Test via API
curl -X POST http://localhost:3001/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Show me debt service ratio for the US from 2020"}'
```

## Deployment

Changes are backward compatible and can be deployed immediately:

```bash
# Build frontend
npm run build:frontend

# Backend auto-reloads on file changes (--reload flag)
# No manual restart needed

# Verify
curl https://openecon.ai/api/health
```
