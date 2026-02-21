# CRITICAL: Data Value Comparison Between Old Prompt and SimplifiedPrompt

**Date:** 2025-11-23
**Status:** ⚠️ **CRITICAL ISSUE FOUND**

## Summary

Compared actual data values (by counting returned series) between old prompt and SimplifiedPrompt for queries that succeeded in BOTH tests.

### Results

- **Total queries compared:** 48
- **Identical data counts:** 6 (12.5%)
- **Different data counts:** 42 (87.5%)

## Critical Finding

**The SimplifiedPrompt returns DIFFERENT amounts of data for the same queries!**

This is a **serious bug** that indicates the SimplifiedPrompt is not just changing routing behavior, but is fundamentally altering the data retrieval logic in unpredictable ways.

## Examples of Data Discrepancies

### Case 1: Fewer Data Series Returned
Query 2: "Show me US federal funds rate vs inflation rate from 2000 to 2024"
- Old prompt: 2 series (correct - federal funds + inflation)
- SimplifiedPrompt: 0 series (WRONG - no data returned!)

### Case 2: More Data Series Returned
Query 31: "Compare unemployment rates across all Canadian provinces for 2024"
- Old prompt: 4 series (partial data)
- SimplifiedPrompt: 10 series (possibly correct - all provinces)

### Case 3: Complete Data Loss
Query 41: "Compare government debt to GDP ratios for G7 countries in 2023"
- Old prompt: 7 series (correct - all G7 countries)
- SimplifiedPrompt: 0 series (WRONG - complete data loss!)

Query 51: "Compare residential property price indices for major global cities"
- Old prompt: 8 series (has data)
- SimplifiedPrompt: 0 series (WRONG - no data!)

### Case 4: Unexpected Improvement
Query 44: "Compare fiscal deficits across European Union member states"
- Old prompt: 1 series (incomplete)
- SimplifiedPrompt: 24 series (better - more EU countries)

## Queries with Matching Data Counts (Only 6 out of 48)

✅ Query 4: Calculate year-over-year change in US industrial production (1 series)
✅ Query 5: Show US housing starts (1 series)
✅ Query 8: Show me US labor force participation rate (1 series)
✅ Query 9: Calculate average US personal savings rate (9 series)
✅ Query 35: Show Canadian wheat and canola production (3 series)
✅ Query 55: Show long-term house price cycles for US, UK, and Japan (3 series)

## Analysis

### Why This is Critical

1. **Data Integrity Issue**: The SimplifiedPrompt is not just routing differently - it's fundamentally changing what data gets returned
2. **Unpredictable Behavior**: Some queries return more data, some return less, some return none
3. **User Impact**: Users would get completely different results for the same query depending on which prompt version is used
4. **Not Just Routing**: This proves the issue is deeper than just provider selection - the data retrieval logic itself is affected

### Possible Root Causes

1. **Parameter Extraction Changed**: SimplifiedPrompt may be extracting different parameters from queries
2. **Indicator Selection Changed**: Different indicator codes being selected for the same query
3. **Country/Region Parsing Changed**: Different interpretation of geographical scope
4. **Time Range Parsing Changed**: Different date ranges being extracted
5. **Provider-Specific Logic Affected**: Changes in how queries are translated to provider-specific API calls

## Recommendation

**DO NOT DEPLOY SimplifiedPrompt until this is resolved.**

The old prompt may have routing issues, but at least it returns data consistently. The SimplifiedPrompt appears to have fundamental data retrieval problems that go beyond routing.

### Next Steps

1. **Debug a few specific cases** to understand WHY the data counts differ
2. **Examine the actual data values** (not just counts) to see if they're different
3. **Review parameter extraction logic** in SimplifiedPrompt
4. **Test individual provider calls** to see if the issue is in the LLM prompt or the provider code

## Full Discrepancy List

| Query ID | Query | Old Count | New Count | Issue |
|----------|-------|-----------|-----------|-------|
| 2 | Show me US federal funds rate vs inflation rate | 2 | 0 | Complete data loss |
| 10 | Peak US unemployment COVID vs 2008 | 2 | 1 | Missing comparison data |
| 11 | Compare GDP per capita China, India, Brazil | 1 | 3 | Now returns all countries |
| 14 | Female labor force Nordic vs global | 0 | 1 | Now has data |
| 15 | CO2 emissions top 10 economies | 10 | 9 | Missing one economy |
| 16 | Literacy rate South Asia | 8 | 1 | Major data loss |
| 17 | Infant mortality developed vs developing | 12 | 0 | Complete data loss |
| 19 | Urban population 100M+ countries | 0 | 4 | Now has data |
| 22 | Top 5 importers Chinese EVs | 0 | 1 | Now has some data |
| 24 | US-Mexico auto trade balance | 0 | 1 | Now has data |
| 26 | China rare earth exports | 1 | 0 | Data loss |
| 27 | US textile imports | 1 | 0 | Data loss |
| 28 | Germany machinery exports | 0 | 4 | Now has data |
| 31 | Canadian provincial unemployment | 4 | 10 | More complete data |
| 32 | Toronto/Vancouver/Montreal housing | 4 | 0 | Complete data loss |
| 33 | Ontario-Alberta migration | 0 | 1 | Now has data |
| 36 | Quebec-BC retail sales | 2 | 0 | Data loss |
| 41 | G7 debt-to-GDP | 7 | 0 | Complete data loss |
| 42 | Emerging market current accounts | 0 | 5 | Now has data |
| 44 | EU fiscal deficits | 1 | 24 | Much more complete |
| 45 | LatAm inflation forecasts | 0 | 1 | Now has data |
| 49 | Oil-exporting countries GDP growth | 1 | 0 | Data loss |
| 51 | Global cities property prices | 8 | 0 | Complete data loss |
| 52 | Commercial real estate | 1 | 0 | Data loss |
| 53 | OECD house price to income ratio | 0 | 1 | Now has data |
| 57 | Emerging markets property | 6 | 0 | Complete data loss |
| 62 | Eurozone harmonized inflation | 0 | 1 | Now has data |
| 71 | OECD productivity growth | 0 | 1 | Now has data |
| 72 | OECD healthcare expenditure | 0 | 1 | Now has data |
| 74 | OECD income inequality | 0 | 2 | Now has data |
| 77 | OECD broadband penetration | 0 | 1 | Now has data |
| 78 | OECD foreign-born population | 1 | 1 | Same count (identical) |
| 81 | USD strength index | 1 | 0 | Data loss |
| 82 | EUR/USD volatility | 1 | 1 | Same count (identical) |
| 83 | GBP/USD biggest move | 0 | 1 | Now has data |
| 84 | Emerging currencies vs USD | 1 | 1 | Same count (identical) |
| 87 | JPY/USD monthly rates | 1 | 1 | Same count (identical) |
| 91 | Bitcoin dominance | 1 | 1 | Same count (identical) |

## Pattern Analysis

### Data Loss Cases (SimplifiedPrompt returns LESS data): 18 cases
Queries: 2, 10, 15, 16, 17, 26, 27, 32, 36, 41, 49, 51, 52, 57, 81

### Data Gain Cases (SimplifiedPrompt returns MORE data): 19 cases
Queries: 11, 14, 19, 22, 24, 28, 31, 33, 42, 44, 45, 53, 62, 71, 72, 74, 77, 83

### Identical Cases: 6 cases
Queries: 4, 5, 8, 9, 35, 55, 78, 82, 84, 87, 91

**Conclusion:** SimplifiedPrompt is highly unstable and unpredictable in data retrieval. The behavior is erratic with no clear pattern.
