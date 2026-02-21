# Codebase Region/Country Handling Audit

**Date:** December 26, 2025
**Purpose:** Comprehensive audit of multi-country/region handling across the entire codebase
**Status:** ✅ **FIXES APPLIED** - All critical issues resolved

---

## Executive Summary

~~The codebase has **significant inconsistencies** in how regions and countries are handled:~~

**UPDATE (Dec 26, 2025):** All critical issues have been fixed with **CENTRALIZED** approach:

### Centralization Complete
All providers now use `CountryResolver` as the **single source of truth**:
- `CountryResolver.get_region_expansion(region, format="iso2|iso3|un_numeric")`
- Providers fall back to their own mappings only for provider-specific groups

### Changes Applied:
1. ✅ **ISO Code Conversion Utilities**: Added `to_iso3()`, `to_iso2()`, `to_un_numeric()` to CountryResolver
2. ✅ **Universal Expansion Method**: Added `get_region_expansion(region, format)` supporting all formats
3. ✅ **Query Service Fixed**: Now uses CountryResolver.expand_region() instead of hardcoded mappings
4. ✅ **WorldBank Centralized**: `_expand_country_group()` now uses CountryResolver first
5. ✅ **IMF Centralized**: `_resolve_countries()` now uses CountryResolver first
6. ✅ **OECD Centralized**: `expand_countries()` now uses CountryResolver first
7. ✅ **BIS Centralized**: `_expand_region()` now uses CountryResolver first
8. ✅ **Comtrade Centralized**: Region expansion now uses CountryResolver first
9. ✅ **LLM Prompt Updated**: Accurate provider-specific region support documentation

---

## Critical Issues Summary - FIXED & CENTRALIZED

| Priority | Issue | Files Affected | Status |
|----------|-------|---------------|--------|
| **HIGH** | Query service has hardcoded REGION_MAPPINGS | `query.py:1720-1752` | ✅ **Uses CountryResolver** |
| **HIGH** | WorldBank has own region expansion logic | `worldbank.py:618-660` | ✅ **Uses CountryResolver first** |
| **HIGH** | IMF uses ISO3, CountryResolver uses ISO2 | `imf.py:582-619` | ✅ **Uses CountryResolver first** |
| **HIGH** | BIS missing G7, BRICS, ASEAN regions | `bis.py:283-318` | ✅ **Uses CountryResolver first** |
| **MEDIUM** | OECD missing ASEAN, BRICS+ regions | `oecd.py:323-369` | ✅ **Uses CountryResolver first** |
| **MEDIUM** | Eurostat has NO multi-country method | `query.py` | ✅ **Uses CountryResolver** |
| **MEDIUM** | LLM prompt misleading about expansion | `simplified_prompt.py` | ✅ **Updated** |
| **LOW** | Comtrade only supports EU27 expansion | `comtrade.py:559-590` | ✅ **Uses CountryResolver first** |

---

## Detailed Findings by Component

### 1. CountryResolver (Central Authority) - SINGLE SOURCE OF TRUTH

**File:** `backend/routing/country_resolver.py`
**Status:** ✅ ENHANCED - All providers now use this as their primary source

**Regions Defined (FrozenSets):**
- G7_MEMBERS (7 countries) - ISO2 codes
- G20_MEMBERS (19 countries) - ISO2 codes
- BRICS_MEMBERS (5 countries) - ISO2 codes
- BRICS_PLUS_MEMBERS (9 countries) - ISO2 codes
- EU_MEMBERS (27 countries) - ISO2 codes
- EUROZONE_MEMBERS (20 countries) - ISO2 codes
- ASEAN_MEMBERS (10 countries) - ISO2 codes
- NORDIC_MEMBERS (5 countries) - ISO2 codes
- OECD_MEMBERS (38 countries) - ISO2 codes

**Methods:**
- `expand_region(region)` - Returns list of ISO2 codes
- `expand_region_iso3(region)` - Returns list of ISO3 codes
- `expand_region_un_numeric(region)` - Returns list of UN numeric codes
- **`get_region_expansion(region, format)`** - Universal method supporting all formats:
  - `format="iso2"` → ISO Alpha-2 codes (US, DE, JP)
  - `format="iso3"` → ISO Alpha-3 codes (USA, DEU, JPN)
  - `format="un_numeric"` → UN Comtrade numeric codes (842, 276, 392)
- `to_iso3(iso2_code)` - Convert ISO2 → ISO3
- `to_iso2(iso3_code)` - Convert ISO3 → ISO2
- `to_un_numeric(iso2_code)` - Convert ISO2 → UN numeric
- `is_region(text)` - Checks if text is a region name
- `detect_regions_in_query(query)` - Finds regions in query text
- `expand_regions_in_query(query)` - Expands all regions in query

---

### 2. WorldBank Provider

**File:** `backend/providers/worldbank.py`
**Status:** ✅ UPDATED - Now uses CountryResolver first

**Current Approach:**
1. `COUNTRY_GROUP_EXPANSIONS` (lines 367-462) - 50+ entries using ISO3 codes
2. `REGIONAL_TERM_MAPPINGS` (lines 51-101) - WorldBank region codes
3. `_expand_country_group()` (lines 607-631) - Custom expansion method
4. `_map_regional_term()` (lines 552-605) - Maps regions to WB codes

**Code Format:** Uses ISO3 (USA, DEU, FRA)

**Recommendations:**
- Keep `REGIONAL_TERM_MAPPINGS` for WorldBank-specific aggregates (SAS, MEA, SSA)
- Replace `COUNTRY_GROUP_EXPANSIONS` with CountryResolver.expand_region()
- Add ISO2 to ISO3 conversion utility

---

### 3. IMF Provider

**File:** `backend/providers/imf.py`
**Status:** ❌ NEEDS UPDATE - Duplicates CountryResolver

**Issues:**
1. `REGION_MAPPINGS` (lines 149-314) - 165 lines defining regions in ISO3
2. `COUNTRY_MAPPINGS` (lines 316-447) - Aliases to ISO3 codes
3. `_resolve_countries()` (lines 577-594) - Custom expansion method
4. Missing BRICS+ (only has original BRICS 5)

**Code Format:** Uses ISO3 (USA, DEU, FRA)

**Recommendations:**
- Add BRICS_PLUS region
- Consider using CountryResolver with ISO2 to ISO3 conversion
- Keep IMF-specific regions (DEVELOPED_ECONOMIES, EMERGING_MARKETS, etc.)

---

### 4. OECD Provider

**File:** `backend/providers/oecd.py`
**Status:** ⚠️ PARTIAL - Good structure but missing regions

**Current REGION_EXPANSIONS (lines 192-240):**
- ✅ G7, G20, Nordic, EU, Eurozone, Eastern Europe, Anglosphere
- ❌ MISSING: ASEAN, BRICS, BRICS+

**Code Format:** Uses ISO3 (USA, DEU, FRA)

**Recommendations:**
- Add ASEAN expansion (for non-OECD completeness)
- Add BRICS note (limited - only Chile, Colombia, Costa Rica overlap with OECD)
- Consider using CountryResolver with conversion

---

### 5. Eurostat Provider

**File:** `backend/providers/eurostat.py`
**Status:** ❌ NEEDS MAJOR UPDATE - No multi-country support

**Issues:**
1. NO `fetch_multi_country()` method
2. Only supports aggregate codes (EU27_2020, EA20)
3. Cannot fetch individual Nordic, G7, etc. countries

**Code Format:** Uses ISO2 (DE, FR, IT) - Compatible with CountryResolver

**Recommendations:**
- Implement `fetch_multi_country()` method
- Add region expansion using CountryResolver
- Support Nordic, G7, OECD subsets for EU countries

---

### 6. BIS Provider

**File:** `backend/providers/bis.py`
**Status:** ⚠️ PARTIAL - Limited region support

**Current REGION_MAPPINGS (lines 234-240):**
- ✅ EU, EUROZONE, EURO_AREA, EUROPE
- ❌ MISSING: G7, BRICS, ASEAN, Nordic, OECD

**Code Format:** Uses ISO2 (US, DE, FR) - Compatible with CountryResolver

**Recommendations:**
- Add G7, Nordic, ASEAN to REGION_MAPPINGS
- Use CountryResolver.expand_region() instead of custom _expand_region()

---

### 7. Comtrade Provider

**File:** `backend/providers/comtrade.py`
**Status:** ⚠️ PARTIAL - Only EU27 expansion

**Issues:**
1. Only expands EU27_2020 (lines 604-679)
2. No support for G7, BRICS, ASEAN, Nordic
3. Uses UN numeric codes, not ISO codes

**Code Format:** UN numeric codes (840 for USA)

**Recommendations:**
- Add G7, BRICS, ASEAN, Nordic expansion
- Create conversion from ISO2 to UN numeric codes

---

### 8. Query Service

**File:** `backend/services/query.py`
**Status:** ❌ NEEDS UPDATE - Hardcoded region mappings

**Issues:**
Lines 1720-1742 have HARDCODED REGION_MAPPINGS:
```python
EU_AGGREGATES = {"EU", "EU27", "EU27_2020", ...}
REGION_MAPPINGS = {
    "NORDIC": ["SE", "NO", "DK", "FI", "IS"],
    "BENELUX": ["BE", "NL", "LU"],
    ...
}
```

**Recommendations:**
- Replace with CountryResolver.expand_region()
- Keep EU_AGGREGATES for aggregate detection only

---

### 9. LLM Prompt (simplified_prompt.py)

**File:** `backend/services/simplified_prompt.py`
**Status:** ⚠️ MISLEADING - Incomplete documentation

**Issues:**
1. Line 256-257: Claims "Backend will automatically expand regions" - partially true
2. Lines 94-98 vs 399: Contradictory routing rules for G7/G20
3. Lines 1025-1026: BRICS for "emerging markets" - doesn't work with OECD
4. Missing ASEAN documentation

**Recommendations:**
- Add provider-specific region support table
- Document which providers support which regions
- Add warnings about OECD rate limits for regional queries

---

## Region Support Matrix

| Region | CountryResolver | WorldBank | IMF | OECD | Eurostat | BIS | Comtrade |
|--------|-----------------|-----------|-----|------|----------|-----|----------|
| **G7** | ✅ (7) | ✅ (ISO3) | ✅ (ISO3) | ✅ (ISO3) | ❌ | ❌ | ❌ |
| **G20** | ✅ (19) | ✅ | ✅ | ⚠️ (15) | ❌ | ❌ | ❌ |
| **BRICS** | ✅ (5) | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **BRICS+** | ✅ (9) | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **EU** | ✅ (27) | ✅ | ✅ | ✅ (22) | ✅ (agg) | ✅ | ✅ |
| **Eurozone** | ✅ (20) | ✅ | ✅ | ✅ | ✅ (agg) | ✅ | ❌ |
| **ASEAN** | ✅ (10) | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **Nordic** | ✅ (5) | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ |
| **OECD** | ✅ (38) | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ |

---

## ISO Code Format Summary

| Component | Format | Example | Compatible with CountryResolver? |
|-----------|--------|---------|----------------------------------|
| **CountryResolver** | ISO2 | US, DE, FR | N/A (Source of Truth) |
| **WorldBank** | ISO3 | USA, DEU, FRA | ❌ Needs conversion |
| **IMF** | ISO3 | USA, DEU, FRA | ❌ Needs conversion |
| **OECD** | ISO3 | USA, DEU, FRA | ❌ Needs conversion |
| **Eurostat** | ISO2 | US, DE, FR | ✅ Compatible |
| **BIS** | ISO2 | US, DE, FR | ✅ Compatible |
| **Comtrade** | UN Numeric | 840, 276 | ❌ Needs conversion |

---

## Recommended Fix Order

### Phase 1: Eliminate Hardcoded Duplicates (HIGH Priority)

1. **Query Service** (`query.py:1720-1742`)
   - Replace hardcoded REGION_MAPPINGS with CountryResolver.expand_region()
   - Estimated: 30 min

2. **BIS Provider** (`bis.py:234-276`)
   - Add G7, Nordic, ASEAN to REGION_MAPPINGS using CountryResolver patterns
   - Replace _expand_region() with CountryResolver.expand_region()
   - Estimated: 1 hour

### Phase 2: Add Missing Regions (MEDIUM Priority)

3. **OECD Provider** (`oecd.py:192-240`)
   - Add ASEAN expansion
   - Add BRICS+ (or note about limited overlap)
   - Estimated: 30 min

4. **IMF Provider** (`imf.py:149-314`)
   - Add BRICS_PLUS region
   - Estimated: 15 min

5. **Comtrade Provider** (`comtrade.py:604-679`)
   - Add G7, BRICS, ASEAN, Nordic expansion
   - Estimated: 1 hour

### Phase 3: Add Multi-Country Support (MEDIUM Priority)

6. **Eurostat Provider** (`eurostat.py`)
   - Implement fetch_multi_country() method
   - Add region expansion support
   - Estimated: 2 hours

### Phase 4: Update Documentation (LOW Priority)

7. **LLM Prompt** (`simplified_prompt.py`)
   - Add provider-specific region support documentation
   - Fix contradictory instructions
   - Estimated: 1 hour

### Phase 5: Consider ISO Code Conversion (FUTURE)

8. **CountryResolver Enhancement**
   - Add `to_iso3(iso2_code)` method
   - Add `to_un_numeric(iso2_code)` method for Comtrade
   - Providers call CountryResolver + convert format
   - Estimated: 2 hours

---

## Files Modified/To Modify

| File | Current Status | Action Needed |
|------|---------------|---------------|
| `backend/routing/country_resolver.py` | ✅ GOOD | None (source of truth) |
| `backend/services/query.py` | ❌ Hardcoded | Replace lines 1720-1742 |
| `backend/providers/bis.py` | ⚠️ Partial | Add G7, Nordic, ASEAN |
| `backend/providers/oecd.py` | ⚠️ Partial | Add ASEAN, BRICS+ |
| `backend/providers/imf.py` | ⚠️ Missing BRICS+ | Add BRICS_PLUS |
| `backend/providers/comtrade.py` | ⚠️ Only EU | Add G7, BRICS, etc. |
| `backend/providers/eurostat.py` | ❌ No multi | Implement fetch_multi_country |
| `backend/providers/worldbank.py` | ⚠️ Duplicate | Consider consolidation |
| `backend/services/simplified_prompt.py` | ⚠️ Misleading | Update documentation |
| `backend/services/deep_agent_orchestrator.py` | ✅ GOOD | Uses CountryResolver correctly |

---

## Testing Requirements

After fixes, test these queries:

```bash
# G7 queries (should return 7 countries)
"Compare G7 GDP growth rates"
"G7 unemployment 2023"

# BRICS queries (should return 5 countries)
"BRICS GDP comparison"
"Show BRICS current account balance"

# BRICS+ queries (should return 9 countries)
"BRICS+ GDP 2024"

# Nordic queries (should return 5 countries)
"Nordic unemployment rates"

# ASEAN queries (should return 10 countries)
"ASEAN GDP growth ranking"

# EU queries (should return 27 countries OR EU aggregate)
"EU inflation 2024"
```

---

*Last Updated: December 26, 2025*
