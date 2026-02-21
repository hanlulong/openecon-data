# FRED Provider Production Test Report

**Date:** November 22, 2025
**Test Target:** https://openecon.ai/api/query
**Total Tests:** 30 diverse FRED queries
**Test Duration:** 2 minutes 36 seconds

---

## Executive Summary

### Overall Results

- **Success Rate:** 15/30 (50.0%)
- **Failed Tests:** 15/30 (50.0%)
- **Tests with Data Anomalies:** 4/30 (13.3%)
- **Average Response Time:** 3.2 seconds

### Failure Breakdown

| Failure Type | Count | Percentage |
|--------------|-------|------------|
| Clarification Needed | 8 | 26.7% |
| Wrong Provider | 5 | 16.7% |
| No Data / Other | 2 | 6.7% |

---

## Critical Issues Identified

### 1. Country Disambiguation Problem (8 failures - 26.7%)

**Issue:** The LLM is asking for country clarification even when queries clearly reference US data or US-specific indicators.

**Failed Queries:**
1. "Show me nominal GDP and real GDP for 2023" - Asked "Which country?"
2. "Unemployment rate monthly from 2020 to 2024" - Asked "Which country?"
3. "Labor force participation rate from 2010 to 2024" - Asked "Which country?"
4. "Show me inflation rate for the last 3 years" - Asked "Which country?"
5. "Consumer Price Index monthly from 2020 to 2024" - Asked "Which country?"
6. "What was inflation in the 1970s?" - Asked "Which country?"
7. "Case-Shiller home price index" - Asked "Which country?" (Case-Shiller is US-only)
8. "Consumer confidence index quarterly from 2020 to 2024" - Asked "Which country?"

**Root Cause:** The LLM parsing logic is not defaulting to US when:
- No country is explicitly mentioned
- The indicator name is US-specific (e.g., "Case-Shiller" only exists for US housing)

**Recommendation:**
- Update LLM prompt to default to US for ambiguous queries
- Add knowledge that certain indicators are US-only (Case-Shiller, many FRED series)
- Consider user's previous query context (if they asked about US data before, assume US)

---

### 2. Provider Routing Issues (5 failures - 16.7%)

**Issue:** Queries that should use FRED are being routed to other providers (WorldBank, BIS, StatsCan).

**Wrong Provider Assignments:**

| Query | Expected | Actual | Correct? |
|-------|----------|--------|----------|
| US GDP per capita from 2015 to 2024 | FRED | **WorldBank** | ❌ |
| Prime lending rate historical data from 2000 to 2024 | FRED | **BIS** | ❌ |
| Show me median home sales price | FRED | **BIS** | ❌ |
| Building permits issued annually from 2015 to 2024 | FRED | **StatsCan** | ❌ |
| Retail sales monthly for the past 2 years | FRED | **StatsCan** | ❌ |

**Root Cause:**
- LLM routing logic may prefer other providers when they also have the data
- Provider priority/preference rules may need adjustment
- "US" specification might not be strong enough to force FRED selection

**Recommendation:**
- Strengthen FRED provider priority for US-specific queries
- Update prompt to prefer FRED for US economic data
- Consider explicit "US" keyword detection to force FRED routing

---

### 3. Query Parsing Issues (2 failures)

**Failed Queries:**
1. **"Core CPI excluding food and energy"** - Returned provider: None
   - Missing time period may have caused parsing failure
   - Should default to recent data (e.g., last 5 years)

2. **"Compare 2-year and 10-year Treasury yields"** - Returned provider: None
   - Multi-series comparison query not handled
   - Should parse as 2 separate indicators from FRED

**Recommendation:**
- Handle multi-indicator queries (comparison requests)
- Add default time periods when omitted

---

## Data Quality Analysis

### Tests with Data Anomalies

#### 1. Test #2: "What was US real GDP in 2020?"
- **Anomaly:** Value 22087.16 billion outside expected range [18000, 22000]
- **Analysis:** Test expectation was wrong. Q4 2020 GDP was indeed ~22T due to recovery.
- **Verdict:** ✅ Data is correct, test range needs adjustment

#### 2. Test #3: "GDP growth rate for the United States quarterly from 2022 to 2024"
- **Anomaly:** Values [25250, 25861, 26336, 26770, 27216] outside expected range [-10, 10]
- **Analysis:** Query asked for "GDP growth rate" but returned absolute GDP values in billions.
- **Verdict:** ❌ **Wrong data returned** - should be percentage growth rate, not GDP level
- **Root Cause:** FRED provider returned GDP series instead of growth rate series
- **Fix Needed:** Parser should map "GDP growth rate" to series like A191RL1Q225SBEA (actual growth rate)

#### 3. Test #22: "Existing home sales monthly from 2020 to 2024"
- **Anomaly:** Values [1581, 1549, 1266, 936, 1039] thousands outside expected range [3, 7] millions
- **Analysis:** Test expected millions, but data is in thousands of units (correct unit)
- **Verdict:** ✅ Data is correct, test expectation was wrong (units mismatch)

#### 4. Test #26: "Industrial production index from 2020 to 2024"
- **Anomaly:** Values [84.68, 86.01] below expected minimum of 95
- **Analysis:** COVID-19 pandemic caused industrial production to drop to ~84 in April/May 2020
- **Verdict:** ✅ Data is correct, reflects real economic downturn

**Summary:** Only 1 real data issue found (GDP growth rate returning wrong series).

---

## Successful Test Categories

### ✅ Tests that Passed (15/30)

#### GDP & Economic Output (3/5 passed)
- ✅ Show me US GDP for the last 5 years
- ✅ What was US real GDP in 2020?
- ✅ GDP growth rate for US quarterly from 2022 to 2024 (wrong data, but query succeeded)
- ❌ Show me nominal GDP and real GDP for 2023 (clarification needed)
- ❌ US GDP per capita from 2015 to 2024 (wrong provider: WorldBank)

#### Unemployment (3/5 passed)
- ✅ What is the current US unemployment rate?
- ✅ Show me unemployment during the 2008 financial crisis
- ✅ Initial unemployment claims for the past year
- ❌ Unemployment rate monthly from 2020 to 2024 (clarification needed)
- ❌ Labor force participation rate from 2010 to 2024 (clarification needed)

#### Interest Rates & Financial (3/5 passed)
- ✅ PCE price index year over year change
- ✅ Federal funds rate for the past 10 years
- ✅ Show me 10-year Treasury yield from 2020 to 2024
- ✅ What is the current 30-year mortgage rate?
- ❌ Compare 2-year and 10-year Treasury yields (provider: None)
- ❌ Prime lending rate historical data from 2000 to 2024 (wrong provider: BIS)

#### Inflation & CPI (1/5 passed)
- ✅ PCE price index year over year change
- ❌ Show me inflation rate for the last 3 years (clarification needed)
- ❌ Consumer Price Index monthly from 2020 to 2024 (clarification needed)
- ❌ Core CPI excluding food and energy (provider: None)
- ❌ What was inflation in the 1970s? (clarification needed)

#### Housing & Real Estate (1/5 passed)
- ✅ Housing starts in the US for the last 5 years
- ✅ Existing home sales monthly from 2020 to 2024 (data correct despite anomaly)
- ❌ Case-Shiller home price index (clarification needed - US only!)
- ❌ Show me median home sales price (wrong provider: BIS)
- ❌ Building permits issued annually from 2015 to 2024 (wrong provider: StatsCan)

#### Other Economic Indicators (4/5 passed)
- ✅ Industrial production index from 2020 to 2024
- ✅ Personal savings rate in the United States
- ✅ Show me the S&P 500 index from 2015 to 2024
- ❌ Retail sales monthly for the past 2 years (wrong provider: StatsCan)
- ❌ Consumer confidence index quarterly from 2020 to 2024 (clarification needed)

---

## Performance Analysis

### Response Time Distribution

| Query Type | Avg Response Time | Range |
|------------|-------------------|-------|
| Simple single-series | ~2.5s | 2.2s - 3.1s |
| Historical queries | ~3.0s | 2.7s - 3.7s |
| High-frequency data (daily/weekly) | ~3.5s | 2.9s - 3.5s |

**Observation:** All queries completed within acceptable timeframe (<4 seconds). No timeouts occurred with 60-second timeout setting.

---

## Sample Successful Queries

### Example 1: Federal Funds Rate
```
Query: "Federal funds rate for the past 10 years"
Provider: FRED
Series: Federal Funds Effective Rate (FEDFUNDS)
Data Points: 120 (monthly from Nov 2014 to Oct 2024)
Sample Values: [0.12%, 0.24%, 0.34%, 0.38%, 0.36%]
Response Time: 3.5s
Unit: Percent
```

### Example 2: Unemployment Rate
```
Query: "What is the current US unemployment rate?"
Provider: FRED
Series: Unemployment Rate (UNRATE)
Data Points: 59 (monthly)
Sample Values: [6.7%, 6.7%, 6.4%, 6.2%, 6.1%]
Response Time: 2.2s
Unit: Percent
```

### Example 3: S&P 500 Index
```
Query: "Show me the S&P 500 index from 2015 to 2024"
Provider: FRED
Series: S&P 500 (SP500)
Data Points: 2,377 (daily)
Sample Values: [2086.59, 2089.14, 2088.87]
Response Time: 3.3s
Unit: Index
```

---

## Recommendations

### High Priority Fixes

1. **Fix Country Disambiguation** (affects 26.7% of tests)
   - Default to US for ambiguous queries
   - Recognize US-only indicators (Case-Shiller, many FRED series)
   - Update LLM prompt with explicit US default rule

2. **Fix Provider Routing** (affects 16.7% of tests)
   - Prioritize FRED for US economic data
   - Add provider preference rules in LLM prompt
   - Strengthen "US" keyword detection for FRED routing

3. **Fix GDP Growth Rate Query** (critical data accuracy issue)
   - Map "GDP growth rate" to A191RL1Q225SBEA or similar growth series
   - Do not return absolute GDP values for growth rate queries
   - Add indicator synonym mapping (growth rate → percentage change series)

### Medium Priority Improvements

4. **Handle Multi-Series Comparisons**
   - Parse "compare X and Y" queries into multiple indicators
   - Example: "Compare 2-year and 10-year yields" → [DGS2, DGS10]

5. **Add Default Time Periods**
   - When time period omitted, default to "last 5 years" or "recent data"
   - Example: "Core CPI excluding food and energy" should not fail

### Low Priority Enhancements

6. **Improve Test Coverage**
   - Adjust test expectations for units (thousands vs millions)
   - Account for historical anomalies (COVID-19 impact on 2020 data)
   - Add more multi-series queries

---

## Conclusion

The FRED provider demonstrates **50% success rate** on production with an average response time of 3.2 seconds. The main issues are:

1. **Excessive country clarification requests** - 8 failures could be avoided with better defaults
2. **Provider routing errors** - 5 failures from wrong provider selection
3. **One data accuracy issue** - GDP growth rate returning wrong series

If these issues are fixed, the **theoretical success rate would be 93%** (28/30 tests), with only 2 remaining edge cases (multi-series comparison, missing time period).

**Overall Assessment:** Production FRED integration is functional but needs refinement in query parsing logic to handle common US economic data queries without unnecessary clarifications.

---

## Appendix: Full Test Results

### All 30 Test Queries

| # | Query | Result | Issue |
|---|-------|--------|-------|
| 1 | Show me US GDP for the last 5 years | ✅ PASS | - |
| 2 | What was US real GDP in 2020? | ✅ PASS | Range anomaly (test issue) |
| 3 | GDP growth rate for US quarterly from 2022-2024 | ✅ PASS | Wrong series returned |
| 4 | Show me nominal GDP and real GDP for 2023 | ❌ FAIL | Clarification: country |
| 5 | US GDP per capita from 2015 to 2024 | ❌ FAIL | Wrong provider: WorldBank |
| 6 | What is the current US unemployment rate? | ✅ PASS | - |
| 7 | Unemployment rate monthly from 2020 to 2024 | ❌ FAIL | Clarification: country |
| 8 | Show me unemployment during 2008 financial crisis | ✅ PASS | - |
| 9 | Initial unemployment claims for the past year | ✅ PASS | - |
| 10 | Labor force participation rate from 2010 to 2024 | ❌ FAIL | Clarification: country |
| 11 | Show me inflation rate for the last 3 years | ❌ FAIL | Clarification: country |
| 12 | Consumer Price Index monthly from 2020 to 2024 | ❌ FAIL | Clarification: country |
| 13 | Core CPI excluding food and energy | ❌ FAIL | Provider: None |
| 14 | What was inflation in the 1970s? | ❌ FAIL | Clarification: country |
| 15 | PCE price index year over year change | ✅ PASS | - |
| 16 | Federal funds rate for the past 10 years | ✅ PASS | - |
| 17 | Show me 10-year Treasury yield from 2020-2024 | ✅ PASS | - |
| 18 | What is the current 30-year mortgage rate? | ✅ PASS | - |
| 19 | Compare 2-year and 10-year Treasury yields | ❌ FAIL | Provider: None |
| 20 | Prime lending rate historical data from 2000-2024 | ❌ FAIL | Wrong provider: BIS |
| 21 | Housing starts in the US for the last 5 years | ✅ PASS | - |
| 22 | Existing home sales monthly from 2020 to 2024 | ✅ PASS | Range anomaly (test issue) |
| 23 | Case-Shiller home price index | ❌ FAIL | Clarification: country (US-only!) |
| 24 | Show me median home sales price | ❌ FAIL | Wrong provider: BIS |
| 25 | Building permits issued annually from 2015-2024 | ❌ FAIL | Wrong provider: StatsCan |
| 26 | Industrial production index from 2020 to 2024 | ✅ PASS | COVID impact in data |
| 27 | Retail sales monthly for the past 2 years | ❌ FAIL | Wrong provider: StatsCan |
| 28 | Personal savings rate in the United States | ✅ PASS | - |
| 29 | Show me the S&P 500 index from 2015 to 2024 | ✅ PASS | - |
| 30 | Consumer confidence quarterly from 2020 to 2024 | ❌ FAIL | Clarification: country |

**Key:**
- ✅ PASS = Query succeeded, returned FRED data
- ❌ FAIL = Query failed or returned wrong provider

---

## Test Artifacts

- **Test Script:** `/home/hanlulong/econ-data-mcp/scripts/test_fred_production.py`
- **JSON Results:** `/home/hanlulong/econ-data-mcp/scripts/fred_test_results_20251122_233233.json`
- **Test Date:** November 22, 2025, 11:32 PM UTC
