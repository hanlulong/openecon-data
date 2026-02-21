# Comtrade Provider Root Cause Analysis

## Summary

Comtrade provider has a 70% failure rate (7 out of 10 test queries failing). However, **these are primarily DATA AVAILABILITY issues, not code bugs**.

## Root Causes Identified

### 1. Taiwan Data Unavailability (1 query)

**Query**: "Show total semiconductor exports from Taiwan to all countries in 2023"

**Root Cause**: UN Comtrade does not publish Taiwan trade statistics for political reasons.

**Evidence**:
- Reporter code 158 (Taiwan) returns zero data for all years (2020, 2021, 2022, 2023)
- API response: `{"count": 0, "data": []}`
- Source: [UN Comtrade Taiwan Data Documentation](https://unstats.un.org/wiki/display/comtrade/Taiwan,+Province+of+China+Trade+data)

**Quote from UN documentation**:
> "For political reasons, the UN is not allowed to show trade statistics referring to Taiwan, Province of China. However, Taiwan, Province of China, is included under 'Other Asia, not elsewhere specified' (code 490) in the partner breakdown."

**Impact**: Any query asking for Taiwan as reporter will fail.

**Current Handling**: Returns empty data (correct behavior).

**Recommended Fix**: Improve LLM prompt to avoid parsing Taiwan as reporter. Suggest using Taiwan as partner instead.

---

### 2. Invalid Regional Codes (4 queries)

**Queries**:
1. "Compare oil imports between EU and China from Middle East countries"
2. "What is the total value of agricultural exports from Brazil to Asia?"
3. "Show Japan's technology exports to Southeast Asian nations in 2023"
4. "Calculate total pharmaceutical trade between India and Africa"

**Root Cause**: UN Comtrade does not support broad regional aggregates like "Asia", "Africa", "Middle East", or "Southeast Asia" as partner codes.

**Evidence**:
- Code explicitly documents: `# Note: "Middle East", "Asia", "Africa" are NOT valid Comtrade codes` (comtrade_metadata.py:124)
- `_country_code()` returns `None` for these regions
- Provider correctly raises `DataNotAvailableError` with helpful message

**Current Error Message**:
```
'Middle East' is not a valid country or recognized region in UN Comtrade. Please specify individual countries or use supported regions like 'EU'. For regions like 'Middle East', please specify individual countries: UAE, Saudi Arabia, Qatar, Kuwait, Oman, Iraq, Iran, Israel, etc.
```

**Impact**: Any query with unsupported regions as partner will fail.

**Current Handling**: **Correctly implemented** - raises `DataNotAvailableError` with helpful suggestions.

**Recommended Fix**:
- Improve LLM prompt to decompose regional queries into individual countries
- Add Pro Mode capability to aggregate multiple country queries
- Document supported regions clearly (EU is supported, others are not)

---

### 3. Missing Commodity Mappings (2 queries)

**Queries**:
1. "Show rare earth elements exports from China to US and EU since 2020"
2. "Compare textile imports of US from Bangladesh, Vietnam, and India"

**Investigation Results**:

**Rare Earth Elements**:
- Current mapping: `rare earth elements` → `TOTAL` (fallback)
- HS code exists: Chapter 28 (Inorganic chemicals including rare earth compounds)
- But specific rare earth HS codes not in mapping
- **Result**: Query succeeds but returns ALL commodities instead of specific rare earth data

**Textiles**:
- Current mapping: `textiles` → `50` (correct)
- Query succeeds but hits **rate limiting** (HTTP 429)
- Multiple simultaneous requests (Bangladesh, Vietnam, India) trigger rate limits
- **Result**: Partial success - some countries fail due to rate limits

**Root Cause**:
- Rare earth: Incomplete HS code mappings (maps to TOTAL instead of specific codes)
- Textiles: Rate limiting from API (not a code bug, but operational constraint)

**Current Handling**:
- Rare earth: Returns incorrect data (TOTAL instead of specific)
- Textiles: Retry logic handles rate limits but may still fail under heavy load

**Recommended Fixes**:
1. Add specific HS codes for rare earth elements (2805, 2846)
2. Improve rate limiting handling with longer delays between requests
3. Consider implementing request throttling/queueing

---

## Summary Statistics

| Category | Count | Percentage |
|----------|-------|------------|
| **Taiwan unavailability** | 1 | 14% |
| **Invalid regions** | 4 | 57% |
| **Commodity mapping issues** | 2 | 29% |
| **Total failures** | 7 | 70% |
| **Total queries** | 10 | 100% |

## Passing Queries (3/10)

1. "What are the top 5 importers of Chinese electric vehicles in 2023?" - **Pass** (actually used WorldBank, not Comtrade)
2. "Show bilateral trade balance between US and Mexico for automotive sector" - **Pass**
3. "What are Germany's machinery exports to Eastern European countries?" - **Pass** (production), Timeout (local)

## Conclusions

1. **70% of failures are NOT code bugs** - they are data availability limitations (Taiwan, regions)
2. **Code is correctly handling invalid inputs** - raises appropriate errors with helpful messages
3. **Real issues to fix**:
   - Improve commodity mappings (add rare earth HS codes)
   - Better rate limit handling for multi-country queries
   - LLM prompt engineering to avoid unsupported queries

## Action Items

### High Priority
1. ✅ Improve error messages (already good)
2. Add rare earth element HS codes to commodity mappings
3. Improve LLM system prompt to avoid Taiwan as reporter
4. Improve LLM system prompt to decompose regional queries

### Medium Priority
1. Implement better rate limiting/throttling for multi-country queries
2. Add Pro Mode capability to handle regional aggregation
3. Document supported vs unsupported regions in provider

### Low Priority
1. Consider caching to reduce API calls
2. Add monitoring for rate limit errors

## Sources

- [UN Comtrade Taiwan Data](https://unstats.un.org/wiki/display/comtrade/Taiwan,+Province+of+China+Trade+data)
- [UN Comtrade API Documentation](https://comtradedeveloper.un.org/)
- [UN Comtrade Country Codes](https://unstats.un.org/wiki/display/comtrade/Comtrade+Country+Code+and+Name)
