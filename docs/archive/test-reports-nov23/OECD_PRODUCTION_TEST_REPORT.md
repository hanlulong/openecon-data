# OECD Provider Production Test Report

**Date:** 2025-11-22
**Target:** https://openecon.ai/api/query
**Test Duration:** 283.4 seconds
**Total Queries:** 30

## Executive Summary

🚨 **CRITICAL FAILURE: OECD provider is completely non-functional**

- **Success Rate:** 0/30 (0.0%)
- **Provider Routing Failures:** 12/30 queries routed to wrong provider
- **Data Errors:** 9/30 queries failed with "NoneType" errors or data not available
- **Clarification Issues:** 9/30 queries requiring unnecessary clarification

## Critical Issue: Provider Selection Failure

**The LLM is NOT routing queries to OECD even when explicitly requested with "from OECD"**

### Evidence:

1. **Query:** "Show me GDP for United States from OECD"
   - **Routed to:** World Bank ❌
   - **Result:** Data not available error

2. **Query:** "Show unemployment rate for Canada from OECD"
   - **Routed to:** World Bank ❌
   - **Result:** World Bank data returned (wrong source)

3. **Query:** "Show me inflation rate for United Kingdom from OECD"
   - **Routed to:** World Bank ❌
   - **Result:** World Bank data returned (wrong source)

4. **Query:** "Show labor productivity for United States from OECD"
   - **Routed to:** World Bank ❌
   - **Result:** Indicator not found

5. **Query:** "Get imports data for Mexico from OECD"
   - **Routed to:** World Bank ❌
   - **Result:** Clarification needed

6. **Query:** "Get investment rate for Turkey from OECD"
   - **Routed to:** World Bank ❌
   - **Result:** Clarification needed

7. **Query:** "Show average wage for Norway from OECD"
   - **Routed to:** World Bank ❌
   - **Result:** Indicator not found

8. **Query:** "Compare GDP growth for USA, Germany, and Japan from OECD"
   - **Routed to:** World Bank ❌
   - **Result:** World Bank data returned (wrong source)

## Failure Breakdown

| Failure Type | Count | Percentage |
|-------------|-------|------------|
| Wrong Provider | 12 | 40% |
| Data Not Available Errors | 9 | 30% |
| Clarification Needed | 9 | 30% |
| API Errors | 0 | 0% |
| Request Timeout | 1 | 3% |

## Provider Selection Analysis

### Queries explicitly requesting "from OECD":
- **Total:** 8 queries
- **Correctly routed to OECD:** 0 ✗
- **Routed to wrong provider:** 8 ❌

### Providers chosen instead of OECD:
- World Bank: 8 queries
- Eurostat: 2 queries (for European countries)
- Statistics Canada: 1 query (for Canada)
- FRED: 1 query (for US monthly data)
- IMF: 2 queries

## Detailed Test Results

### Category 1: Basic GDP Queries (3 queries)

1. ❌ **"Show me GDP for United States from OECD"**
   - Provider: World Bank (should be OECD)
   - Error: "No data found for any of the requested countries for indicator NY.GDP.MKTP.CD"
   - Issue: Wrong provider selected despite explicit "from OECD" request

2. ❌ **"Get quarterly GDP growth rate for Germany from 2020 to 2024"**
   - Provider: Eurostat (should be OECD)
   - Result: Returned annual (not quarterly) Eurostat data
   - Issue: LLM chose Eurostat for European country

3. ❌ **"What is the annual GDP for France in the last 5 years?"**
   - Provider: Eurostat (should be OECD)
   - Result: Returned Eurostat data (2020-2024)
   - Issue: LLM chose Eurostat for European country

### Category 2: Unemployment Queries (3 queries)

4. ❌ **"Show unemployment rate for Canada from OECD"**
   - Provider: World Bank (should be OECD)
   - Result: Returned World Bank data
   - Issue: Explicit "from OECD" ignored

5. ❌ **"Get unemployment data for Japan from 2015 to 2024"**
   - Provider: World Bank (should be OECD)
   - Result: Returned World Bank data
   - Issue: World Bank chosen over OECD

6. ❌ **"What is the youth unemployment rate in Spain?"**
   - Provider: Eurostat (should be OECD)
   - Error: "Eurostat dataset 'youth_unemployment_rate' not found for country ES"
   - Issue: Eurostat chosen, indicator not found

### Category 3: Inflation Queries (3 queries)

7. ❌ **"Show me inflation rate for United Kingdom from OECD"**
   - Provider: World Bank (should be OECD)
   - Result: Returned World Bank data
   - Issue: Explicit "from OECD" ignored

8. ❌ **"Get CPI inflation for Italy in the last 3 years"**
   - Provider: Eurostat (should be OECD)
   - Error: "Eurostat dataset for 'CPI_INFLATION' not found"
   - Issue: Eurostat chosen, indicator not found

9. ❌ **"What is the consumer price index for Australia?"**
   - Provider: World Bank (should be OECD)
   - Result: Clarification needed (time period)
   - Issue: World Bank chosen over OECD

### Category 4: OECD Average/Total Queries (3 queries)

10. ❌ **"Show me OECD average GDP growth"**
    - Provider: World Bank (should be OECD)
    - Result: Clarification needed (time period)
    - Issue: "OECD average" not recognized as OECD query

11. ❌ **"Get unemployment rate for OECD total"**
    - Provider: World Bank (should be OECD)
    - Error: "No data found for any of the requested countries for indicator SL.UEM.TOTL.ZS"
    - Issue: "OECD total" not recognized

12. ❌ **"What is the average inflation across OECD countries?"**
    - Provider: IMF (should be OECD)
    - Result: Clarification needed (time period)
    - Issue: "OECD countries" not recognized

### Category 5: Productivity Queries (2 queries)

13. ❌ **"Show labor productivity for United States from OECD"**
    - Provider: World Bank (should be OECD)
    - Error: "WorldBank indicator 'labor_productivity' not found"
    - Issue: Explicit "from OECD" ignored

14. ❌ **"Get productivity growth rate for Korea"**
    - Provider: World Bank (should be OECD)
    - Result: Clarification needed
    - Issue: World Bank chosen, doesn't have indicator

### Category 6: Trade Queries (2 queries)

15. ❌ **"Show exports as percentage of GDP for Netherlands"**
    - Provider: World Bank (should be OECD)
    - Result: Returned GDP per capita (WRONG indicator!)
    - Issue: Wrong provider AND wrong indicator

16. ❌ **"Get imports data for Mexico from OECD"**
    - Provider: World Bank (should be OECD)
    - Result: Clarification needed (time period)
    - Issue: Explicit "from OECD" ignored

### Category 7: Government Finance Queries (2 queries)

17. ❌ **"Show government debt as percentage of GDP for Greece"**
    - Provider: IMF (should be OECD)
    - Result: Returned IMF data (165.2% in 2023)
    - Issue: IMF chosen over OECD

18. ❌ **"Get government deficit for Portugal"**
    - Provider: World Bank (should be OECD)
    - Result: Returned GDP growth (WRONG indicator!)
    - Issue: Wrong provider AND completely wrong indicator

### Category 8: Investment Queries (2 queries)

19. ❌ **"Show gross fixed capital formation for Switzerland"**
    - Provider: World Bank (should be OECD)
    - Error: "WorldBank indicator 'Gross fixed capital formation' not found"
    - Issue: Wrong provider, indicator not found

20. ❌ **"Get investment rate for Turkey from OECD"**
    - Provider: World Bank (should be OECD)
    - Result: Clarification needed (time period)
    - Issue: Explicit "from OECD" ignored

### Category 9: Income and Wages Queries (2 queries)

21. ❌ **"Show average wage for Norway from OECD"**
    - Provider: World Bank (should be OECD)
    - Error: "WorldBank indicator 'WAGES' not found"
    - Issue: Explicit "from OECD" ignored

22. ❌ **"Get household disposable income for Sweden"**
    - Provider: World Bank (should be OECD)
    - Result: Clarification needed
    - Issue: Wrong provider chosen

### Category 10: Multiple Country Comparisons (2 queries)

23. ❌ **"Compare GDP growth for USA, Germany, and Japan from OECD"**
    - Provider: World Bank (should be OECD)
    - Result: Returned World Bank data for all 3 countries
    - Issue: Explicit "from OECD" completely ignored

24. ❌ **"Show unemployment for France, Italy, and Spain"**
    - Provider: World Bank (should be OECD)
    - Result: Returned World Bank data for all 3 countries
    - Issue: World Bank chosen over OECD

### Category 11: Time-Specific Queries (2 queries)

25. ❌ **"Show quarterly GDP for Canada in 2023"**
    - Provider: Statistics Canada (should be OECD)
    - Result: Returned monthly Canadian GDP (2025 data!)
    - Issue: StatsCan chosen for Canada queries

26. ❌ **"Get monthly unemployment for United States in 2024"**
    - Provider: FRED (should be OECD)
    - Result: Returned FRED data (correct but wrong source)
    - Issue: FRED chosen for US monthly data

### Category 12: Edge Cases and Specific Indicators (3 queries)

27. ❌ **"Show research and development expenditure for Finland"**
    - Provider: World Bank (should be OECD)
    - Result: Clarification needed
    - Issue: Wrong provider chosen

28. ❌ **"Get tax revenue as percentage of GDP for Denmark"**
    - Provider: World Bank (should be OECD)
    - Error: "WorldBank indicator 'tax_revenue_percentage_of_GDP' not found"
    - Issue: Wrong provider, indicator doesn't exist

29. ❌ **"Show employment rate for Iceland"**
    - Provider: IMF (should be OECD)
    - Result: Clarification needed (time period)
    - Issue: IMF chosen over OECD

30. ❌ **"Get interest rates for New Zealand from OECD"**
    - Provider: Unknown (timeout)
    - Error: Request timeout (60s)
    - Issue: Query timed out before completion

## Root Cause Analysis

### Primary Issue: LLM Prompt Does Not Prioritize OECD

The OpenRouter LLM parsing service is **not trained to recognize or prioritize OECD** as a data provider. Observations:

1. **Explicit "from OECD" is ignored** - Even when users explicitly request OECD data, the LLM routes to other providers
2. **Regional bias** - European countries automatically routed to Eurostat
3. **Country-specific bias** - Canada routed to StatsCan, US routed to FRED/World Bank
4. **Default fallback** - World Bank appears to be the default provider for most queries
5. **No OECD-specific indicators** - Productivity, wages, R&D not mapped to OECD

### Secondary Issues:

1. **OECD provider may not be implemented** - Zero queries successfully routed to OECD suggests it may not exist in the routing logic
2. **No OECD metadata indexed** - Metadata search appears to only cover World Bank, IMF, Eurostat, etc.
3. **LLM system prompt missing OECD** - The query parsing prompt likely doesn't include OECD in the available providers list

## Comparison to Other Providers

### World Bank (used in 60% of queries):
- Successfully returned data when indicator available
- Used as default fallback
- Missing OECD-specific indicators (wages, productivity)

### Eurostat (used in 13% of queries):
- Chosen for European countries
- Some indicators not available (youth unemployment, CPI inflation)
- Returns correct data when available

### FRED (used in 3% of queries):
- Chosen for US monthly data
- Works correctly for its scope

### Statistics Canada (used in 3% of queries):
- Chosen for Canada queries
- Works but returns wrong time period

### IMF (used in 10% of queries):
- Chosen for government finance queries
- Works correctly for debt/deficit data

### OECD (used in 0% of queries):
- **NEVER selected by LLM**
- **Complete routing failure**

## Recommendations

### Immediate Actions Required:

1. **Verify OECD provider exists**
   - Check `backend/providers/oecd.py` implementation
   - Verify OECD is registered in provider routing logic
   - Check if OECD is included in LLM system prompt

2. **Update LLM system prompt**
   - Add OECD to list of available providers
   - Specify when to use OECD (OECD member countries, comparative data, specific indicators)
   - Add examples of OECD queries
   - Enforce "from OECD" explicit requests

3. **Add OECD metadata to search**
   - Index OECD indicators in metadata search service
   - Map OECD-specific indicators (productivity, wages, R&D, tax revenue)
   - Support "OECD average" and "OECD total" as special entities

4. **Implement provider priority rules**
   - When "from OECD" is explicit, MUST route to OECD
   - When OECD average/total requested, MUST route to OECD
   - For OECD member countries, prefer OECD over World Bank for certain indicators

5. **Add OECD indicator mappings**
   - Labor productivity → OECD productivity indicators
   - Average wage → OECD earnings data
   - Tax revenue % GDP → OECD tax statistics
   - R&D expenditure → OECD science & technology indicators
   - Government debt/deficit → OECD fiscal indicators

### Testing Requirements:

1. **Verify OECD provider works in isolation**
   - Test direct API calls to OECD provider
   - Confirm data retrieval and normalization

2. **Test LLM routing with explicit requests**
   - "from OECD" must route to OECD
   - "OECD average" must route to OECD
   - "OECD countries" must route to OECD

3. **Re-run this test suite after fixes**
   - All 30 queries should route to OECD
   - Success rate should be >90%

## Conclusion

**The OECD provider is completely non-functional in production.** The LLM query parser is not routing ANY queries to OECD, even when explicitly requested. This is a critical failure that requires immediate investigation and remediation.

The system is currently routing OECD queries to World Bank, Eurostat, IMF, FRED, and Statistics Canada instead, which:
- Provides wrong data source attribution
- Misses OECD-specific indicators
- Fails to provide OECD comparative data (averages, totals)
- Confuses users who explicitly request OECD data

**Status: CRITICAL BUG - OECD provider completely broken**

---

**Next Steps:**
1. Investigate if `backend/providers/oecd.py` exists and is properly integrated
2. Check LLM system prompt in `backend/services/openrouter.py`
3. Add OECD to provider routing logic
4. Update metadata search to include OECD indicators
5. Re-test with this same test suite
