# Statistics Canada Test Queries - Quick Reference

## ✅ PASSED QUERIES (23/30)

### National Indicators (5/8)
1. ✅ "Show me Canada's GDP for the last 5 years" - 60 monthly points, 2.5s
2. ✅ "What is Canada's CPI inflation rate?" - 60 monthly points, 2.3s
3. ✅ "Get Canada's unemployment rate for 2023" - 12 monthly points, 2.9s
4. ✅ "Canada population growth over time" - 60 quarterly points, 2.4s
5. ✅ "Show me Canada's retail sales data" - 105 monthly points, 10.1s
6. ❌ "Canada's international trade balance last 3 years" - Wrong provider (Comtrade)
7. ✅ "Canadian housing starts nationwide" - 240 monthly points, 3.2s
8. ✅ "Get Canada's manufacturing sales data" - 60 monthly points, 4.5s

### Provincial Data (8/8) - 100% SUCCESS
1. ✅ "Ontario GDP last 5 years" - 60 monthly points, 3.3s
2. ✅ "What is the unemployment rate in Quebec?" - 240 monthly points, 2.6s
3. ✅ "Alberta population growth" - 240 quarterly points, 3.4s
4. ✅ "British Columbia housing starts" - 60 monthly points, 2.6s
5. ✅ "Saskatchewan retail sales data" - 60 monthly points, 5.6s
6. ✅ "Manitoba CPI inflation" - 240 monthly points, 2.9s
7. ✅ "Nova Scotia GDP data" - 60 monthly points, 23.6s
8. ✅ "New Brunswick unemployment rate" - 240 monthly points, 2.7s

### Sector-Specific (5/9)
1. ✅ "Canada housing price index last 3 years" - 36 monthly points, 5.0s
2. ❌ "Agricultural production in Canada" - Clarification needed
3. ✅ "Energy production statistics Canada" - 30 annual points, 6.0s
4. ❌ "Canadian construction spending" - Runtime error (NoneType)
5. ❌ "Tourism revenue in Canada" - Runtime error (NoneType)
6. ❌ "Canadian automotive sales data" - Clarification needed
7. ✅ "Wholesale trade in Canada" - 60 monthly points, 5.4s
8. ❌ "Canadian employment by industry" - Runtime error (NoneType)

### Time Period Variations (3/3) - 100% SUCCESS
1. ✅ "Canada GDP quarterly data for 2024" - 4 monthly points, 2.8s
2. ✅ "Monthly CPI data for Canada in 2023" - 12 monthly points, 2.9s
3. ✅ "Annual unemployment rate Canada 2010-2024" - 120 monthly points, 2.9s

### Search-Based (2/3)
1. ✅ "Find housing starts data for Toronto" - 60 monthly points, 3.3s
2. ❌ "Get retail sales for all provinces in Canada" - Timeout (60s)
3. ✅ "Consumer price index for food in Canada" - 240 monthly points, 3.0s

---

## ❌ FAILED QUERIES (7/30)

### Critical Issues - Runtime Errors (3)
These queries caused backend exceptions and need investigation:

1. **"Canadian construction spending"**
   - Error: `'NoneType' object has no attribute 'get'`
   - Response time: 6,078ms
   - Likely cause: Pro Mode activation or backend error

2. **"Tourism revenue in Canada"**
   - Error: `'NoneType' object has no attribute 'get'`
   - Response time: 4,871ms
   - Likely cause: Pro Mode activation or backend error

3. **"Canadian employment by industry"**
   - Error: `'NoneType' object has no attribute 'get'`
   - Response time: 22,074ms
   - Likely cause: Pro Mode activation or backend error

### Provider Routing Issue (1)

4. **"Canada's international trade balance last 3 years"**
   - Issue: Routed to Comtrade instead of StatsCan
   - Response time: 5,377ms
   - Data: 3 annual points from UN Comtrade
   - Fix needed: Update LLM prompt to prefer StatsCan for Canadian trade

### Reasonable Clarification Requests (2)

5. **"Agricultural production in Canada"**
   - Clarification: "What specific aspect of agricultural production?"
   - Clarification: "What time period would you like to see?"
   - Assessment: Reasonable - query too vague

6. **"Canadian automotive sales data"**
   - Clarification: "What time period are you interested in?"
   - Assessment: Reasonable - missing time period

### Performance Issue (1)

7. **"Get retail sales for all provinces in Canada"**
   - Issue: Timeout after 60 seconds
   - Likely cause: Multiple API calls for all provinces
   - Fix needed: Batch fetching or Pro Mode decomposition

---

## QUERY PATTERNS THAT WORK WELL

### High Success Patterns:
- ✅ "Show me [indicator] for [province]"
- ✅ "[Province] [indicator] data"
- ✅ "What is [indicator] in [province]?"
- ✅ "Canada [indicator] [time period]"
- ✅ "Get [indicator] for Canada"
- ✅ "[Indicator] growth over time"
- ✅ "Find [indicator] data for [location]"

### Patterns Needing Improvement:
- ⚠️ "[Vague sector] in Canada" (e.g., "agricultural production")
- ⚠️ "Get [data] for all provinces" (timeout risk)
- ⚠️ "[Indicator] data" without time period

---

## RECOMMENDATIONS FOR USERS

### For Best Results:

1. **Be Specific About Time Periods:**
   - Good: "Canada GDP for the last 5 years"
   - Bad: "Canada GDP" (may require clarification)

2. **Specify Province When Relevant:**
   - Good: "Ontario unemployment rate"
   - Good: "What is the unemployment rate in Quebec?"

3. **Use Common Indicator Names:**
   - "GDP", "CPI", "unemployment", "population", "housing starts"
   - "retail sales", "manufacturing", "wholesale trade"

4. **Avoid Multi-Entity Queries:**
   - Risky: "Get retail sales for all provinces"
   - Better: "Canada retail sales" or "Ontario retail sales"

5. **For Sector Data, Be Specific:**
   - Vague: "Agricultural production in Canada"
   - Better: "Canada crop production last 5 years"
   - Better: "Canadian farm income 2020-2024"

---

## DATA QUALITY OBSERVATIONS

### Units Returned:
- GDP: billions CAD
- Retail Sales: thousands CAD
- Manufacturing: thousands CAD
- Housing Starts: thousands of units
- CPI: index values (2002=100 or other base)
- Unemployment: percentage
- Population: absolute count

### Frequencies Available:
- Monthly (most common)
- Quarterly (population data)
- Annual (energy production)

### Typical Data Coverage:
- Recent data: 60 months (5 years)
- Long-term data: 240 months (20 years)
- Short-term: 12 months (1 year)

---

## TEST METRICS SUMMARY

| Metric | Value |
|--------|-------|
| Total Queries | 30 |
| Passed | 23 (76.67%) |
| Failed | 7 (23.33%) |
| Avg Response Time | 5,095 ms |
| Fastest Query | 2,257 ms (CPI inflation) |
| Slowest Query | 23,560 ms (Nova Scotia GDP) |
| Most Data Points | 240 (Quebec unemployment) |
| Least Data Points | 3 (trade balance) |

