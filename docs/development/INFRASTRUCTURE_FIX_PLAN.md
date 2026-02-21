# Infrastructure Fix Plan - December 26, 2025

## Executive Summary

Based on deep analysis of test failures and online documentation research, this plan addresses three critical infrastructure issues affecting query accuracy.

---

## Issue 1: Indicator Translation Fuzzy Matching (HIGH PRIORITY)

### Problem
"M2 Growth" is incorrectly fuzzy-matched to "gdp_growth" (73.7% similarity) because there's no money_supply concept defined.

### Root Cause
- `indicator_translator.py:386-407` uses SequenceMatcher with 0.7 threshold
- "m2 growth" vs "gdp growth" = 0.737 similarity (exceeds threshold)
- No competing money_supply concept exists to match against

### Fix Strategy

**Fix 1A: Add money_supply concept to catalog**
- Create `/backend/catalog/concepts/money_supply.yaml`
- Add M1, M2, M3 as aliases
- Provider mappings:
  - FRED: M2SL, M1SL, M3SL
  - World Bank: FM.LBL.MQMY.GD.ZS (M2 as % of GDP)

**Fix 1B: Increase fuzzy threshold for short queries**
- Location: `indicator_translator.py:386`
- Change: Use 0.85 threshold for inputs < 15 chars

**Fix 1C: Add explicit exclusions to gdp_growth**
- Location: `/backend/catalog/concepts/gdp_growth.yaml`
- Add: m1 growth, m2 growth, m3 growth, monetary growth

### Implementation
```python
# indicator_translator.py line 386
def _fuzzy_match_concept(self, indicator: str, threshold: float = 0.7) -> Optional[str]:
    indicator_lower = indicator.lower().replace("_", " ")

    # Direct match first
    if indicator_lower in self._alias_to_concept:
        return self._alias_to_concept[indicator_lower]

    # INFRASTRUCTURE FIX: Higher threshold for short queries
    effective_threshold = 0.85 if len(indicator_lower) < 15 else threshold
    # ... rest of method
```

---

## Issue 2: Metadata Search Disambiguation (MEDIUM PRIORITY)

### Problem
"China money supply M2" returns 38 World Bank options and asks for clarification instead of selecting the best match.

### Root Cause
- `metadata_search.py:339` triggers ambiguity when results >= 3 AND diverse
- `metadata_search.py:448` uses 0.3 Jaccard similarity threshold
- M1, M2, M3 indicators are marked as "diverse" (< 50% term overlap)

### Fix Strategy

**Fix 2A: Lower diversity threshold for monetary aggregates**
- Location: `metadata_search.py:448`
- Change: Increase similarity threshold from 0.3 to 0.4 for related indicators

**Fix 2B: Add M2-specific selection in LLM prompt**
- Location: `metadata_search.py:609-652`
- Add explicit guidance: "For queries mentioning M2/M1/M3 specifically, prioritize exact match"

**Fix 2C: Pre-filter results using query context**
- Before diversity check, filter results that match specific M2/M1/M3 pattern
- If query contains "M2", prioritize indicators with "M2" in code/name

### Implementation
```python
# metadata_search.py - add before diversity check
def _prefilter_monetary_results(self, results: List[Dict], query: str) -> List[Dict]:
    """Pre-filter monetary aggregate results based on specific M1/M2/M3 mention."""
    query_lower = query.lower()

    # Check for specific monetary aggregate mention
    for m_type in ['m3', 'm2', 'm1']:  # Priority order
        if m_type in query_lower:
            filtered = [r for r in results if m_type in r.get('code', '').lower()
                       or m_type in r.get('name', '').lower()]
            if filtered:
                return filtered

    return results
```

---

## Issue 3: BIS Provider Fallback (MEDIUM PRIORITY)

### Problem
BIS has 66-country coverage. Unsupported countries silently return empty results instead of triggering fallback.

### Root Cause
- `bis.py:387-410` silently continues when country has no data
- Returns empty list instead of raising `DataNotAvailableError`
- Query orchestrator never triggers fallback chain

### Fix Strategy

**Fix 3A: Add explicit country coverage check**
- Define BIS_SUPPORTED_COUNTRIES constant
- Check before API call, raise error for unsupported countries

**Fix 3B: Raise DataNotAvailableError for empty results**
- After looping through countries, check if results empty
- Raise error with helpful message suggesting alternatives

**Fix 3C: Add indicator-specific fallback logic**
- For policy rates: FRED (US only), then warn no global alternative
- For interest rates: World Bank FR.INR.DPST, FR.INR.LEND as proxies

### Implementation
```python
# bis.py - add at top
BIS_SUPPORTED_COUNTRIES = frozenset({
    "AE", "AR", "AT", "AU", "BE", "BG", "BR", "CA", "CH", "CL", "CN", "CO",
    "CZ", "DE", "DK", "EE", "EG", "ES", "FI", "FR", "GB", "GR", "HK", "HR",
    "HU", "ID", "IE", "IL", "IN", "IT", "JP", "KR", "LT", "LV", "MX", "MY",
    "NL", "NO", "NZ", "PH", "PL", "PT", "RO", "RU", "SA", "SE", "SG", "SK",
    "TH", "TR", "TW", "US", "VN", "ZA", "XM"
})

# In fetch_indicator method, after country resolution
unsupported = [c for c in country_codes if c not in BIS_SUPPORTED_COUNTRIES]
if unsupported and len(unsupported) == len(country_codes):
    raise DataNotAvailableError(
        f"BIS doesn't have data for {', '.join(unsupported)}. "
        f"For US policy rates, try FRED. For global interest rates, try World Bank."
    )
```

---

## Implementation Order

| Priority | Fix | Estimated Impact | Files |
|----------|-----|------------------|-------|
| 1 | 1A: money_supply.yaml | HIGH - Fixes M2 translation | New file |
| 2 | 1B: Fuzzy threshold | HIGH - Prevents false matches | indicator_translator.py |
| 3 | 2C: Pre-filter monetary | MEDIUM - Better M2 selection | metadata_search.py |
| 4 | 3A+3B: BIS coverage | MEDIUM - Better errors | bis.py |
| 5 | 1C: gdp_growth exclusions | LOW - Defense in depth | gdp_growth.yaml |
| 6 | 2A: Diversity threshold | LOW - Reduces ambiguity | metadata_search.py |

---

## Verification Plan

### 5-Query Test for Each Fix

**Fix 1 (Indicator Translation):**
1. "M2 Growth China" → Should NOT match gdp_growth
2. "M1 money supply Japan" → Should find monetary indicator
3. "US M3 monetary aggregate" → Should route to FRED M3
4. "Money supply growth rate India" → Should find monetary indicator
5. "M2 as percentage of GDP Brazil" → Should find FM.LBL.MQMY.GD.ZS

**Fix 2 (Metadata Search):**
1. "China money supply M2" → Should select M2 indicator, not ask clarification
2. "Indonesia M2 growth" → Should select M2 indicator
3. "South Korea M1" → Should select M1 indicator
4. "Broad money supply Germany" → Should select broad money indicator
5. "Monetary base Japan" → Should select monetary base indicator

**Fix 3 (BIS Fallback):**
1. "Pakistan central bank rate" → Should gracefully error with alternatives
2. "Kenya policy rate" → Should gracefully error with alternatives
3. "US federal funds rate" → Should work (BIS or fallback to FRED)
4. "ECB refinancing rate" → Should work (BIS has EU)
5. "Vietnam interest rates" → Should work (BIS has VN)

---

## Sources Referenced

- [World Bank M2 Indicator FM.LBL.MQMY.GD.ZS](https://data.worldbank.org/indicator/FM.LBL.MQMY.GD.ZS)
- [IMF DataMapper API Help](https://www.imf.org/external/datamapper/api/help)
- [RapidFuzz Library](https://github.com/rapidfuzz/RapidFuzz)
- [Python Fuzzy Matching Best Practices](https://coderivers.org/blog/python-fuzzy-matching/)
