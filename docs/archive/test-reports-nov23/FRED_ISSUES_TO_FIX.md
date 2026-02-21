# FRED Provider: Issues to Fix

**Priority:** HIGH
**Impact:** Affects 50% of test queries (15/30 failures)

---

## Issue 1: Country Disambiguation - 8 Failures (26.7%)

**Problem:** LLM asks "Which country?" even for obvious US queries or US-only indicators.

### Affected Queries
1. "Show me nominal GDP and real GDP for 2023"
2. "Unemployment rate monthly from 2020 to 2024"
3. "Labor force participation rate from 2010 to 2024"
4. "Show me inflation rate for the last 3 years"
5. "Consumer Price Index monthly from 2020 to 2024"
6. "What was inflation in the 1970s?"
7. **"Case-Shiller home price index"** ← US-only indicator!
8. "Consumer confidence index quarterly from 2020 to 2024"

### Root Cause
File: `backend/services/openrouter.py` (LLM prompt)

The LLM is not configured to:
- Default to US when no country specified
- Recognize US-only indicators (Case-Shiller, FRED-specific series)

### Proposed Fix

**Location:** `backend/services/openrouter.py` - System prompt for query parsing

Add to the prompt:
```
COUNTRY DISAMBIGUATION RULES:
1. If the query mentions US-specific indicators (Case-Shiller, Federal Funds Rate,
   Prime Rate, etc.), assume country=US and do NOT ask for clarification.
2. If no country is specified and the user is querying general economic data
   (GDP, unemployment, inflation, CPI), default to US.
3. Only ask for country clarification if:
   - Query explicitly mentions multiple countries
   - Query asks for international comparison
   - Query asks for a specific non-US country

US-ONLY INDICATORS (never ask for country):
- Case-Shiller Home Price Index
- Federal Funds Rate
- Prime Rate (US)
- S&P 500
- Most FRED series (unless explicitly international)
```

### Verification
Test queries that should NOT ask for clarification:
- "Case-Shiller home price index" → Should return FRED data for US
- "Consumer Price Index monthly from 2020 to 2024" → Should default to US CPI
- "Show me inflation rate for the last 3 years" → Should default to US

---

## Issue 2: GDP Growth Rate Returns Wrong Series (Critical Data Bug)

**Problem:** Query "GDP growth rate" returns absolute GDP values instead of percentage growth.

### Current Behavior
```
Query: "GDP growth rate for the United States quarterly from 2022 to 2024"
Returns: GDP series (absolute values in billions: 25250, 25861, 26336...)
Expected: Growth rate series (percentage: 2.1%, 3.2%, 1.8%...)
```

### Root Cause
File: `backend/providers/fred.py` or `backend/services/openrouter.py`

The indicator mapping does not distinguish between:
- "GDP" → Series ID: GDP (absolute values)
- "GDP growth rate" → Series ID: A191RL1Q225SBEA (percentage change)

### Proposed Fix

**Location:** `backend/services/openrouter.py` or add indicator mapping logic

Option 1: Update LLM prompt with explicit mappings:
```
INDICATOR MAPPINGS FOR FRED:
- "GDP" → Series: GDP (absolute values)
- "GDP growth rate" → Series: A191RL1Q225SBEA (percent change from preceding period)
- "Real GDP" → Series: GDPC1 (chained 2017 dollars)
- "Nominal GDP" → Series: GDP (current dollars)

When user asks for "growth rate" or "change" of any indicator,
search for the corresponding percentage change series, NOT the level series.
```

Option 2: Add post-processing in `fred.py`:
```python
# In fred.py provider
def map_indicator_to_series(indicator: str, intent: ParsedIntent) -> str:
    """Map natural language indicators to FRED series IDs"""

    # Check if query asks for growth/change
    if "growth" in indicator.lower() or "change" in indicator.lower():
        if "gdp" in indicator.lower():
            return "A191RL1Q225SBEA"  # GDP growth rate
        if "employment" in indicator.lower():
            return "PAYEMS"  # Employment change

    # Default series mappings
    indicator_map = {
        "gdp": "GDP",
        "unemployment": "UNRATE",
        # ... etc
    }
    return indicator_map.get(indicator.lower(), indicator)
```

### Verification Test
```bash
curl -X POST https://openecon.ai/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "GDP growth rate for US quarterly from 2022 to 2024"}'
```

Expected result:
- Series ID: A191RL1Q225SBEA
- Unit: Percent
- Sample values: Small percentages (e.g., 2.1, 3.2, 1.8)

---

## Issue 3: Provider Routing Errors - 5 Failures (16.7%)

**Problem:** Queries for US data are routed to WorldBank, BIS, or StatsCan instead of FRED.

### Affected Queries

| Query | Current Provider | Should Be |
|-------|------------------|-----------|
| US GDP per capita from 2015 to 2024 | WorldBank | FRED |
| Prime lending rate historical data from 2000 to 2024 | BIS | FRED |
| Show me median home sales price | BIS | FRED |
| Building permits issued annually from 2015 to 2024 | StatsCan | FRED |
| Retail sales monthly for the past 2 years | StatsCan | FRED |

### Root Cause
File: `backend/services/openrouter.py` (provider selection prompt)

The LLM does not have strong enough preference rules for FRED when:
- Query explicitly mentions "US" or "United States"
- Query asks for US-specific economic indicators

### Proposed Fix

**Location:** `backend/services/openrouter.py` - Provider selection rules

Update the prompt:
```
PROVIDER SELECTION PRIORITY:

For United States economic data, prioritize in this order:
1. FRED - Federal Reserve Economic Data (preferred for ALL US economic indicators)
2. WorldBank - Only if FRED does not have the data
3. Other providers - Only for non-US data

FRED is the PRIMARY source for:
- US GDP, GNP, GDP per capita
- US unemployment, employment, labor statistics
- US inflation, CPI, PCE
- US interest rates (federal funds, Treasury yields, mortgage rates, prime rate)
- US housing data (housing starts, home sales, building permits, home prices)
- US retail sales, industrial production, manufacturing
- US financial markets (S&P 500, stock indices)

Rules:
- If query contains "US", "United States", or "American" → Use FRED
- If query asks for US city/state data → Use FRED
- Only use other providers if FRED explicitly does not have the indicator
```

### Verification
Test queries that should route to FRED:
- "US GDP per capita from 2015 to 2024" → FRED (not WorldBank)
- "Prime lending rate historical data from 2000 to 2024" → FRED (not BIS)
- "Building permits issued annually from 2015 to 2024" → FRED (not StatsCan)

---

## Issue 4: Multi-Series Comparison Not Handled - 1 Failure

**Problem:** Query "Compare 2-year and 10-year Treasury yields" returns provider: None

### Current Behavior
```
Query: "Compare 2-year and 10-year Treasury yields"
Result: apiProvider = None (parsing failed)
```

### Root Cause
The LLM does not understand how to parse comparison queries into multiple indicators.

### Proposed Fix

**Location:** `backend/services/openrouter.py` - Parsing prompt

Add to the prompt:
```
MULTI-SERIES QUERIES:
When user asks to "compare X and Y" or "show me X and Y":
1. Parse as TWO separate indicators in the indicators array
2. Set apiProvider to the provider that has both series
3. Example:
   Query: "Compare 2-year and 10-year Treasury yields"
   Result: {
     "apiProvider": "FRED",
     "indicators": ["DGS2", "DGS10"],
     "parameters": {...}
   }
```

### Verification
```bash
curl -X POST https://openecon.ai/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Compare 2-year and 10-year Treasury yields"}'
```

Expected:
- apiProvider: FRED
- indicators: ["DGS2", "DGS10"] or similar
- data: Array with 2 series

---

## Issue 5: Missing Time Period Handling - 1 Failure

**Problem:** Query "Core CPI excluding food and energy" returns provider: None

### Current Behavior
```
Query: "Core CPI excluding food and energy"
Result: apiProvider = None
```

### Root Cause
Missing time period causes parsing failure.

### Proposed Fix

**Location:** `backend/services/openrouter.py` - Default parameters

Add to the prompt:
```
DEFAULT TIME PERIODS:
If no time period is specified, use these defaults:
- For "current" or "latest" queries → Last available data point
- For time series queries → Last 5 years
- For historical queries without dates → Last 10 years

Never fail to parse due to missing time period. Always provide a sensible default.
```

### Verification
```bash
curl -X POST https://openecon.ai/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Core CPI excluding food and energy"}'
```

Expected:
- apiProvider: FRED
- indicators: ["CPILFESL"]
- Default date range applied

---

## Summary of Required Changes

### File: `backend/services/openrouter.py`

Required prompt updates:
1. ✅ Add country disambiguation rules (default to US)
2. ✅ Add US-only indicator list
3. ✅ Add GDP growth rate vs GDP level mapping
4. ✅ Strengthen FRED provider priority for US queries
5. ✅ Add multi-series comparison parsing
6. ✅ Add default time period rules

### Expected Impact

If all fixes are implemented:
- **Theoretical success rate: 93%** (28/30 tests passing)
- Only 2 remaining edge cases that may still need refinement

### Testing Plan

After implementing fixes, re-run:
```bash
python3 scripts/test_fred_production.py
```

Target: 28/30 tests passing (93% success rate)
