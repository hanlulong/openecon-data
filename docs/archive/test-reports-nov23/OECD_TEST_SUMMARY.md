# OECD Provider Production Test - Executive Summary

**Date:** 2025-11-22
**Test Type:** Production API testing against https://openecon.ai
**Total Queries:** 30
**Success Rate:** 0/30 (0%)

---

## Critical Finding

🚨 **The OECD provider is completely non-functional due to LLM routing failures.**

Despite having a functional OECD provider implementation (`backend/providers/oecd.py`), **zero queries were routed to OECD**, even when users explicitly requested "from OECD".

---

## Root Cause Identified

### Issue 1: Negative Instruction in System Prompt

**File:** `/home/hanlulong/econ-data-mcp/backend/services/openrouter.py` (line 75)

```python
OECD: OECD member countries economic data (38 members) - Use only when World Bank or IMF don't have the data, as OECD has rate limiting issues
```

**Impact:** This tells the LLM to AVOID using OECD as much as possible, which causes:
- LLM to default to World Bank/IMF for all queries
- Explicit "from OECD" requests to be ignored
- OECD to be treated as last-resort provider

### Issue 2: Competing "PREFERRED PROVIDER" Instructions

**Lines 68 and 71:**
```python
WorldBank: **PREFERRED PROVIDER** - Global development indicators...
IMF: **PREFERRED PROVIDER** - Cross-country economic comparisons...
```

**Impact:** The LLM sees these providers as preferred, further reducing OECD priority.

### Issue 3: Explicit Source Override Not Enforced

**Lines 102-110** claim that explicit source mentions (like "from OECD") MUST override automatic selection, but this is clearly not working in practice.

---

## Test Results Breakdown

| Category | Queries | Correct Provider | Wrong Provider | % Failed |
|----------|---------|------------------|----------------|----------|
| Explicit "from OECD" | 8 | 0 | 8 | 100% |
| OECD Average/Total | 3 | 0 | 3 | 100% |
| OECD member queries | 19 | 0 | 19 | 100% |
| **TOTAL** | **30** | **0** | **30** | **100%** |

### Providers Used Instead of OECD:
- World Bank: 15 queries (50%)
- Eurostat: 4 queries (13%)
- IMF: 3 queries (10%)
- Statistics Canada: 1 query (3%)
- FRED: 1 query (3%)
- Other/Timeout: 6 queries (20%)

---

## Impact on Users

1. **Wrong data source attribution** - Users requesting OECD data get World Bank/IMF data instead
2. **Missing OECD-specific indicators** - Productivity, wages, R&D expenditure queries fail
3. **No OECD comparative data** - "OECD average" queries don't work
4. **Incorrect comparisons** - Multi-country OECD queries use inconsistent sources
5. **Broken trust** - Explicit "from OECD" requests are ignored

---

## Example Failures

### Query: "Show me GDP for United States from OECD"
- **Expected:** OECD provider
- **Actual:** World Bank provider
- **Result:** Error - "No data found"

### Query: "Show unemployment rate for Canada from OECD"
- **Expected:** OECD provider
- **Actual:** World Bank provider
- **Result:** World Bank data returned (wrong source)

### Query: "Compare GDP growth for USA, Germany, and Japan from OECD"
- **Expected:** OECD provider with consistent methodology
- **Actual:** World Bank provider
- **Result:** World Bank data (not OECD comparative data)

### Query: "Show me OECD average GDP growth"
- **Expected:** OECD provider with aggregate data
- **Actual:** World Bank provider
- **Result:** Clarification needed (OECD average not understood)

---

## Recommended Fixes

### Fix 1: Update System Prompt (HIGH PRIORITY)

**Change line 75 from:**
```
OECD: OECD member countries economic data (38 members) - Use only when World Bank or IMF don't have the data, as OECD has rate limiting issues
```

**To:**
```
OECD: OECD member countries economic data (38 members) - Preferred for OECD comparative statistics, labor market data, productivity, wages, tax statistics, and when explicitly requested
```

**Rationale:** Remove negative instruction, add positive use cases

### Fix 2: Add OECD-Specific Indicators

Add to the prompt:
```
🎯 OECD Specializations:
- Labor productivity and unit labor costs
- Average wages and earnings
- Tax revenue as % of GDP
- R&D expenditure
- OECD averages and aggregates
- Multi-country OECD comparisons
When these indicators are requested, prefer OECD over World Bank/IMF
```

### Fix 3: Enforce Explicit Source Requests

Strengthen the explicit source override section (lines 102-110):
```
🚨 USER-SPECIFIED DATA SOURCE (ABSOLUTE PRIORITY - NO EXCEPTIONS):
- If the user includes "from OECD", "using OECD", "according to OECD", or "OECD data"
- You MUST set apiProvider: "OECD"
- This is MANDATORY and overrides all other provider selection logic
- Do NOT use WorldBank, IMF, or other providers when OECD is explicitly requested
```

### Fix 4: Add OECD Metadata to Search

Ensure OECD indicators are indexed in the metadata search service so the LLM knows what data OECD has.

---

## Verification Steps

After implementing fixes:

1. **Re-run this exact test suite** (`scripts/test_oecd_production.py`)
2. **Expected success rate:** >90% (27+ out of 30 queries)
3. **Key tests:**
   - All explicit "from OECD" requests must route to OECD (8/8)
   - OECD average/total queries must route to OECD (3/3)
   - OECD-specific indicators (wages, productivity) must route to OECD

---

## Files to Modify

1. `/home/hanlulong/econ-data-mcp/backend/services/openrouter.py`
   - Update OECD description (line 75)
   - Strengthen explicit source override rules (lines 102-110)
   - Add OECD specialization section

2. `/home/hanlulong/econ-data-mcp/backend/services/metadata_search.py` (if exists)
   - Add OECD metadata indexing

---

## Detailed Test Results

Full results saved to:
- `/home/hanlulong/econ-data-mcp/scripts/oecd_test_results_20251122_231929.json`
- `/home/hanlulong/econ-data-mcp/OECD_PRODUCTION_TEST_REPORT.md`

---

## Conclusion

The OECD provider is **implemented but unusable** due to incorrect LLM prompt configuration. The system prompt actively discourages OECD usage and fails to enforce explicit source requests.

**Severity:** CRITICAL
**User Impact:** HIGH (users cannot access OECD data)
**Fix Complexity:** LOW (prompt changes only)
**Fix Time Estimate:** 30 minutes + testing

**Status:** Awaiting prompt updates and re-testing
