# FRED Fixes - Quick Reference Guide

## US-Only Indicators (Always FRED, Never Ask Country)

These indicators ONLY exist for the United States:

| Indicator | FRED Series | Rule |
|-----------|-------------|------|
| Case-Shiller | CSUSHPINSA | US-only, never ask country |
| Federal funds rate | FEDFUNDS | US Federal Reserve only |
| PCE / Personal Consumption Expenditures | PCEPI | US-specific measure |
| Nonfarm payrolls | PAYEMS | US Bureau of Labor Statistics |
| Initial unemployment claims | ICSA | US weekly data |
| Consumer Sentiment (U of M) | UMCSENT | US survey only |
| S&P 500 | SP500 | US stock index |
| Prime lending rate | DPRIME | US bank prime loan rate |
| 30-year mortgage rate | MORTGAGE30US | US mortgage data |

**LLM Behavior:**
- apiProvider: "FRED"
- country: "US"
- clarificationNeeded: false (DO NOT ASK)

---

## Default to US When No Country Specified

If query mentions these indicators WITHOUT a country, default to US:

- GDP, unemployment, inflation, CPI
- Interest rates, housing, retail sales
- Wages, industrial production

**Examples:**
- "Show me GDP" → FRED, US ✅
- "Unemployment rate monthly" → FRED, US ✅
- "Consumer Price Index" → FRED, US ✅

**Exceptions:**
- "Canada GDP" → StatsCan ✅
- "Global GDP" → WorldBank ✅
- "Compare US and China" → WorldBank ✅

---

## FRED Series Mappings (Key Additions)

### GDP Growth Rate Fix
```python
"GDP_GROWTH": "A191RL1Q225SBEA"  # Percentage growth rate
"GDP_GROWTH_RATE": "A191RL1Q225SBEA"
"REAL_GDP_GROWTH": "A191RL1Q225SBEA"
"GDP": "GDP"  # Absolute values in billions
```

### Core CPI
```python
"CORE_CPI": "CPILFESL"  # Excluding food and energy
"CPI_EXCLUDING_FOOD_AND_ENERGY": "CPILFESL"
```

### Treasury Yields
```python
"2_YEAR_TREASURY": "DGS2"
"10_YEAR_TREASURY": "DGS10"
"30_YEAR_TREASURY": "DGS30"
```

### Housing
```python
"CASE_SHILLER": "CSUSHPINSA"
"MEDIAN_HOME_SALES_PRICE": "MSPUS"
"EXISTING_HOME_SALES": "EXHOSLUSM495S"
```

### Interest Rates
```python
"PRIME_LENDING_RATE": "DPRIME"
```

---

## Provider Routing Priority

### For US Queries (MANDATORY)
1. ✅ Use FRED for ALL US economic data
2. ❌ DO NOT route to WorldBank (unless explicit)
3. ❌ DO NOT route to BIS (unless explicit)
4. ❌ DO NOT route to StatsCan (that's Canada)

### Property/Housing Prices
- **US property prices** → FRED (includes Case-Shiller, median home prices, housing starts)
- **Non-US property prices** → BIS (international housing data)

---

## Multi-Indicator Queries

When user says "compare X and Y":
- Include BOTH indicators in the array
- Set clarificationNeeded: false

**Examples:**
```json
// "Compare 2-year and 10-year Treasury yields"
{
  "apiProvider": "FRED",
  "indicators": ["2_YEAR_TREASURY", "10_YEAR_TREASURY"],
  "clarificationNeeded": false
}

// "Compare nominal GDP and real GDP"
{
  "apiProvider": "FRED",
  "indicators": ["NOMINAL_GDP", "REAL_GDP"],
  "clarificationNeeded": false
}
```

---

## Default Time Periods

When time period is NOT specified:
- Historical queries → Last 5 years
- Current queries → Last 1 year or most recent
- ❌ DO NOT ask for clarification

**Examples:**
- "Core CPI excluding food and energy" → startDate: 5 years ago, endDate: today ✅
- "What is unemployment?" → Last 1 year ✅

---

## Test Queries to Verify Fixes

### Should NOT Ask for Country (8 queries)
```
1. "Show me nominal GDP and real GDP for 2023"
2. "Unemployment rate monthly from 2020 to 2024"
3. "Labor force participation rate from 2010 to 2024"
4. "Show me inflation rate for the last 3 years"
5. "Consumer Price Index monthly from 2020 to 2024"
6. "What was inflation in the 1970s?"
7. "Case-Shiller home price index"
8. "Consumer confidence quarterly from 2020 to 2024"
```
Expected: All use FRED with country: "US", clarificationNeeded: false

### Should Route to FRED (5 queries)
```
1. "US GDP per capita from 2015 to 2024"
2. "Prime lending rate historical data from 2000 to 2024"
3. "Show me median home sales price"
4. "Building permits issued annually from 2015 to 2024"
5. "Retail sales monthly for the past 2 years"
```
Expected: All use FRED (not WorldBank/BIS/StatsCan)

### Should Return Percentage Values (1 query)
```
1. "GDP growth rate for the United States quarterly from 2022 to 2024"
```
Expected: Values in range [-10, 10] percent (NOT billions like 25250)

### Should Parse Multi-Indicator (2 queries)
```
1. "Core CPI excluding food and energy"
2. "Compare 2-year and 10-year Treasury yields"
```
Expected: Both return FRED provider with proper indicators and default time periods

---

## Deployment Checklist

- [ ] Frontend built: `npm run build:frontend`
- [ ] Backend auto-reloaded (check logs)
- [ ] Test all 15 failed queries
- [ ] Verify no clarification questions for US-only indicators
- [ ] Verify FRED routing for US data
- [ ] Verify GDP growth rate returns percentage values
- [ ] Test on production: https://openecon.ai/chat
- [ ] Use chrome-devtools MCP for interactive testing
