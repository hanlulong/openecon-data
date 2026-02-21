# OECD API Research Report

**Date:** 2025-11-23
**Task:** Research OECD API structure and identify improvements for econ-data-mcp
**Sources:** OECD official documentation, Stack Overflow, .Stat Suite documentation, web research

---

## Executive Summary

The OECD SDMX API uses a specific structure that differs significantly from our current implementation. Key findings:

1. **OECD has STRICT rate limiting**: 20 data downloads per hour (updated from initial 20/minute in Nov 2024)
2. **Agency identifiers are REQUIRED**: Queries without proper agency codes fail or return empty results
3. **Filter syntax uses dot notation**: `REF_AREA.INDICATOR.MEASURE.FREQ` with `+` for multiple values
4. **Time parameters**: Use `startPeriod`/`endPeriod` (not `startTime`/`endTime`)
5. **Dataflow identification**: Full format is `AGENCY,DATAFLOW_ID,VERSION`

---

## OECD SDMX API Structure

### Base URL Format

```
https://sdmx.oecd.org/public/rest/data/{AGENCY},{DATAFLOW_ID},{VERSION}/{FILTER_KEY}?{PARAMETERS}
```

**Example:**
```
https://sdmx.oecd.org/public/rest/data/OECD.SDD.STES,DSD_STES@DF_CLI/USA.M.LI...AA...H?startPeriod=2023-02&dimensionAtObservation=AllDimensions
```

### Key Components

1. **Agency Identifier**:
   - Format: `OECD.{DIVISION}.{SUBDEPARTMENT}`
   - Examples:
     - `OECD.SDD.NAD` - National Accounts Division (GDP, QNA, etc.)
     - `OECD.SDD.TPS` - Labour and Social Statistics (Employment, Unemployment)
     - `OECD.ECO.MAD` - Economic Outlook (Inflation, CPI, Prices)
     - `OECD.CFE.EDS` - Regional Statistics
     - `OECD.ENV` - Environment
     - `OECD.TAD` - Trade and Agriculture
   - **CRITICAL**: Using agency ID "OECD" alone returns few or zero results
   - Our implementation correctly maps structures to agencies (lines 426-492)

2. **Dataflow Identifier**:
   - Modern format: `DSD_STRUCTURE@DF_DATAFLOW`
   - Examples:
     - `DSD_NAMAIN1@DF_QNA` - Quarterly National Accounts
     - `DSD_LFS@DF_IALFS` - Labour Force Statistics
     - `DSD_STES@DF_CLI` - Composite Leading Indicators

3. **Version**:
   - Usually `1.0`
   - Can use `+` to get latest version

4. **Filter Key**:
   - Dot-separated dimension values
   - Order MUST match DSD (Data Structure Definition)
   - Use `+` to combine multiple values: `USA+DEU+FRA`
   - Leave blank for wildcard: `..FOREST.THOUSAND_SQKM` (wildcards dimension 1)
   - Empty dimensions create consecutive dots: `.....` means "all values"

---

## OECD Rate Limiting (CRITICAL)

### Current Limits (as of November 2024)

- **Maximum**: 20 data downloads per hour
- **Enforcement**: Users exceeding limits are temporarily blocked
- **VPN Blocking**: Traffic from VPNs/anonymized sources not permitted
- **Implementation**: Started November 8, 2024

### Our Implementation Review

**File**: `/home/hanlulong/econ-data-mcp/backend/providers/oecd.py`

**Strengths:**
- ✅ Line 593-595: We wait for rate limiter BEFORE making requests
- ✅ Line 610: We record each request for rate limiting
- ✅ Lines 617-622: Aggressive retry strategy with backoff (5 attempts, 3s initial delay)
- ✅ Uses `retry_async` wrapper for automatic retry handling

**Potential Issues:**
- ⚠️ 20 requests/hour = 1 request every 3 minutes
- ⚠️ Our default retry delays (3s, 6s, 12s, 24s) may not account for hourly limits
- ⚠️ Need to verify `rate_limiter.py` enforces 3-minute minimum delays for OECD

**Recommendations:**
1. Update rate limiter config to enforce minimum 180-second (3-minute) delay between OECD requests
2. Implement aggressive caching (24h+ TTL) for OECD data to reduce API hits
3. Consider batching user requests when possible
4. Add user-facing warnings when OECD queries may be slow due to rate limits

---

## Filter Syntax Deep Dive

### How to Build Filter Keys

From Stack Overflow and .Stat documentation, the proper approach:

```python
# Method 1: Manual construction with dimension order
# Dimensions: REF_AREA . INDICATOR . MEASURE . FREQ
filter_key = "USA+DEU+FRA..FOREST+GRSL+WETL.THOUSAND_SQKM+PCNT"
#            └─ Countries ─┘ └─ Indicators ──────┘ └─ Measures ─────┘

# Method 2: Using wildcards for "all values"
filter_key = "USA.........."  # Only filter country, get all other dimensions
filter_key = "..FOREST......."  # Only filter indicator, get all countries/measures
```

### Key Rules

1. **Number of dots matters**: Must match DSD dimension count minus 1
2. **Order is fixed**: Cannot rearrange dimensions - must match DSD order
3. **Multiple values**: Use `+` (OR logic) - e.g., `USA+DEU+FRA`
4. **Wildcards**: Leave blank (consecutive dots) - e.g., `..` means "all values for dimension 2"
5. **No partial wildcards**: Can't do `US*` - must use full codes or wildcard entire dimension

### Common OECD Dimension Patterns

Based on research and our implementation:

```
Pattern 1 (National Accounts):
REF_AREA.INDICATOR.MEASURE.TRANSFORMATION.FREQ.TIME_PERIOD

Pattern 2 (Labour Force):
REF_AREA.SEX.AGE.INDICATOR.FREQ.TIME_PERIOD

Pattern 3 (Prices):
REF_AREA.SUBJECT.MEASURE.FREQ.TIME_PERIOD
```

**Our implementation handles this dynamically** (lines 532-563):
- Uses `DimensionKeyBuilder` service to query DSD and build proper keys
- Falls back to smart defaults if DSD unavailable
- Correctly uses dots for dimension separation

---

## Time Period Parameters

### Correct Parameters (SDMX 2.0+)

```python
params = {
    "startPeriod": "2020",        # ✅ Correct
    "endPeriod": "2024",          # ✅ Correct
    "dimensionAtObservation": "AllDimensions"  # ✅ Correct for SDMX-JSON
}
```

### Deprecated Parameters

```python
params = {
    "startTime": "2020",   # ❌ Old SDMX 1.0 format
    "endTime": "2024"      # ❌ Old SDMX 1.0 format
}
```

**Our implementation**: ✅ CORRECT (lines 520-530) - Uses `startPeriod`/`endPeriod`

---

## Response Format and Parsing

### SDMX-JSON 2.0 Structure

```json
{
  "meta": {
    "prepared": "2024-11-23T10:00:00Z"
  },
  "data": {
    "dataSets": [{
      "observations": {
        "0:0:0:0:0": [125.4],  // Key is dimension indices, value is array
        "0:0:0:0:1": [127.8]
      }
    }],
    "structures": [{
      "dimensions": {
        "observation": [
          {
            "id": "REF_AREA",
            "values": [{"id": "USA", "name": "United States"}]
          },
          {
            "id": "TIME_PERIOD",
            "values": [{"id": "2020"}, {"id": "2021"}]
          }
        ]
      }
    }]
  }
}
```

### Key Parsing Details

1. **Observation keys**: Colon-separated indices (e.g., `"0:0:0:0:5"`)
2. **Index mapping**: Each number maps to position in dimension's `values` array
3. **OECD quirk**: Doesn't populate `position` field - use array index instead
4. **Value format**: Array where first element is the numeric value

**Our implementation** (lines 624-795):
- ✅ Correctly parses SDMX-JSON 2.0 format
- ✅ Uses array indices (not position field) - lines 660-706
- ✅ Handles multiple dimensions with filtering
- ✅ Converts OECD time formats (annual/quarterly/monthly) to ISO dates

---

## Country Code Handling

### OECD Country Codes

OECD uses **ISO 3166-1 alpha-3** codes:
- USA (not US)
- DEU (not DE or GERMANY)
- GBR (not UK or GB)
- FRA, ITA, JPN, CAN, AUS, etc.

### Country Groups

- `OECD` - All 38 OECD members
- `G7` - Group of Seven
- `G20` - Group of Twenty
- `EA19` - Euro Area (19 countries)
- `EU27_2020` - European Union

**Our implementation** (lines 49-154):
- ✅ Comprehensive mapping of 38+ OECD countries
- ✅ Handles alternative names (UK → GBR, Korea → KOR)
- ✅ Supports country groups (OECD, G7, EA19, EU27_2020)
- ✅ Smart normalization with fuzzy matching (lines 188-221)

---

## Common OECD Indicators and Datasets

### National Accounts
- **Dataset**: `DSD_NAMAIN1@DF_QNA` or `DF_TABLE1`
- **Agency**: `OECD.SDD.NAD`
- **Indicators**: GDP, GNI, consumption, investment
- **Frequency**: Quarterly (Q) or Annual (A)

### Labour Force Statistics
- **Dataset**: `DSD_LFS@DF_IALFS`
- **Agency**: `OECD.SDD.TPS`
- **Indicators**: Unemployment rate, employment rate
- **Frequency**: Monthly (M)

### Prices and Inflation
- **Dataset**: `DSD_PRICES@DF_PRICES_ALL`
- **Agency**: `OECD.ECO.MAD`
- **Indicators**: CPI, PPI, inflation
- **Frequency**: Monthly (M) or Annual (A)

### Trade
- **Dataset**: Various (e.g., `DF_TRADE`)
- **Agency**: `OECD.TAD`
- **Indicators**: Exports, imports, trade balance

---

## Our Current Implementation Analysis

### File: `/home/hanlulong/econ-data-mcp/backend/providers/oecd.py`

#### Strengths

✅ **Dynamic Indicator Resolution** (lines 223-399)
- Multi-layer fallback strategy:
  1. Cache lookup (fastest)
  2. Metadata search with SDMX catalogs
  3. LLM selection of best dataflow (confidence threshold >0.6)
  4. Local catalog lookup with scoring
  5. Smart agency extraction from structure

✅ **Proper SDMX URL Construction** (lines 532-589)
- Uses `DimensionKeyBuilder` for dynamic dimension discovery
- Falls back to smart defaults if DSD unavailable
- Correct parameter names (`startPeriod`, `dimensionAtObservation`)

✅ **Robust Country Code Mapping** (lines 49-221)
- 38+ OECD countries mapped
- Alternative names supported
- Fuzzy matching for robustness

✅ **Advanced Response Filtering** (lines 624-795)
- Filters by country, frequency, measure, transformation
- Uses array indices (not broken position field)
- Handles OECD-specific quirks correctly

✅ **Rate Limiting Awareness** (lines 593-622)
- Waits before requests
- Records requests for tracking
- Aggressive retry strategy with backoff

✅ **Agency Detection** (lines 426-492)
- Maps structure prefixes to correct agencies
- Handles edge cases (EAG, LSO, REG, etc.)
- Default fallback to OECD.SDD.NAD

#### Potential Improvements

⚠️ **Rate Limit Configuration**
- **Current**: 5 retries with 3s initial delay, 2x backoff
- **Issue**: 20 requests/hour = 180 seconds between requests
- **Fix needed**: Verify `rate_limiter.py` enforces 180s minimum delay

⚠️ **Cache TTL**
- **Current**: 86400s (24 hours) for indicator resolution
- **Recommendation**: Extend to 7 days for OECD data to reduce API hits
- **Location**: Line 281, 380

⚠️ **Error Messages**
- **Current**: Generic "No data found" errors
- **Fix needed**: Add specific guidance about OECD rate limits when errors occur

⚠️ **DSD Caching**
- Uses `DimensionKeyBuilder` which queries DSD structure
- Each query hits OECD API to get structure definition
- **Fix needed**: Cache DSD structures locally (currently may be doing this, need to verify)

---

## Why Explicit Provider Override Isn't Working

### Root Cause: LLM Prompt Issues

Based on analysis of test results (`OECD_TEST_SUMMARY.md`):

**Problem 1: Negative Instruction**
```python
# File: backend/services/openrouter.py, line 75
"OECD: OECD member countries economic data (38 members) - Use only when World Bank
or IMF don't have the data, as OECD has rate limiting issues"
```
- Tells LLM to AVOID OECD
- Creates preference for World Bank/IMF
- Contradicts explicit user requests

**Problem 2: Competing Preferences**
```python
# Lines 68, 71
"WorldBank: **PREFERRED PROVIDER** - Global development indicators..."
"IMF: **PREFERRED PROVIDER** - Cross-country economic comparisons..."
```
- LLM sees these as preferred
- OECD is treated as fallback only

**Problem 3: Weak Override Enforcement**
- Lines 102-110 claim explicit source mentions override automatic selection
- But in practice, LLM ignores this
- Test results: 0/8 explicit "from OECD" requests routed correctly

### Recommended Fixes (from test summary)

1. **Update OECD Description**:
```python
"OECD: OECD member countries economic data (38 members) - Preferred for OECD
comparative statistics, labor market data, productivity, wages, tax statistics,
and when explicitly requested"
```

2. **Add OECD Specializations**:
```python
🎯 OECD Specializations:
- Labor productivity and unit labor costs
- Average wages and earnings
- Tax revenue as % of GDP
- R&D expenditure
- OECD averages and aggregates
- Multi-country OECD comparisons
```

3. **Strengthen Override Rules**:
```python
🚨 USER-SPECIFIED DATA SOURCE (ABSOLUTE PRIORITY - NO EXCEPTIONS):
- If user includes "from OECD", "using OECD", "according to OECD", or "OECD data"
- You MUST set apiProvider: "OECD"
- This is MANDATORY and overrides all other provider selection logic
- Do NOT use WorldBank, IMF, or other providers when OECD is explicitly requested
```

---

## Comparison: R Package vs Our Implementation

### R Package Approach (from web research)

The `expersso/OECD` R package abstracts complexity:
1. Users browse OECD Data Explorer visually
2. Click "Developer API" button to get dataset + filter strings
3. Pass these strings directly to `get_dataset()`
4. Package handles URL construction internally

**Example**:
```r
library(OECD)
dataset <- get_dataset(
  dataset = "OECD.SDD.NAD,DSD_NAAG@DF_NAAG_I,1.0",
  filter = "A.USA+EU.B1GQ_R_POP+B1GQ_R_GR.USD_PPP_PS+PC.",
  start_time = 2010,
  end_time = 2020
)
```

### Our Approach

We do NOT require users to know dataset/filter strings:
1. User types natural language: "Show me GDP for USA from OECD"
2. LLM parses intent → indicator="GDP", country="USA", provider="OECD"
3. `OECDProvider.fetch_indicator()` handles:
   - Indicator resolution via metadata search
   - Agency detection from dataflow structure
   - Country code normalization
   - Filter key construction via DSD lookup
   - SDMX query execution
   - Response parsing and normalization

**Advantage**: Better UX - no need to learn OECD/SDMX syntax
**Challenge**: More complex implementation requiring dynamic discovery

---

## Specific Recommendations for Our Implementation

### 1. Rate Limiter Configuration

**File**: `backend/services/rate_limiter.py` (verify settings)

**Required changes**:
```python
PROVIDER_LIMITS = {
    "OECD": {
        "requests_per_hour": 20,
        "min_delay_seconds": 180,  # 3 minutes between requests
        "burst_allowance": 2,      # Allow 2 rapid requests max
    }
}
```

### 2. Cache TTL Extension

**File**: `backend/providers/oecd.py`

**Change line 281 and 380**:
```python
# FROM:
cache_service.set(cache_key, result, ttl=86400)  # 24 hours

# TO:
cache_service.set(cache_key, result, ttl=604800)  # 7 days
```

**Rationale**: OECD data doesn't change frequently, aggressive caching reduces API hits

### 3. DSD Structure Caching

**Verify**: `backend/services/dsd_cache.py` caches structures persistently

**If not implemented**:
```python
# Cache DSD structures to disk, not just memory
DSD_CACHE_PATH = Path(__file__).parent.parent / "data" / "dsd_cache" / "oecd"

def cache_dsd_structure(agency, dsd_id, version, structure):
    """Cache DSD structure to disk for reuse"""
    cache_file = DSD_CACHE_PATH / f"{agency}_{dsd_id}_{version}.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    with open(cache_file, "w") as f:
        json.dump(structure, f)
```

### 4. Enhanced Error Messages

**File**: `backend/providers/oecd.py`

**Lines 627, 778, 792** - Add OECD-specific guidance:
```python
if not datasets or not observations:
    raise DataNotAvailableError(
        f"No data found for {country_code} {indicator} from OECD. "
        f"This may be due to: (1) OECD rate limiting (max 20 requests/hour), "
        f"(2) Data not available for this country/indicator combination, "
        f"(3) Time period out of range. Try a different time range or use "
        f"World Bank or IMF as alternative sources."
    )
```

### 5. LLM Prompt Updates

**File**: `backend/services/openrouter.py`

**Apply all 3 fixes from test summary**:
1. Remove negative OECD instruction (line 75)
2. Add OECD specializations section
3. Strengthen explicit override rules (lines 102-110)

### 6. Metadata Indexing

**File**: `backend/services/metadata_search.py`

**Verify OECD dataflows are indexed**:
- Should load from `backend/data/metadata/sdmx/oecd_dataflows.json`
- Should be searchable via `search_with_sdmx_fallback()`
- Should return results for common indicators (GDP, Unemployment, CPI, etc.)

**Test**:
```python
results = await metadata_search.search_with_sdmx_fallback(
    provider="OECD",
    indicator="GDP"
)
# Should return 10+ matching dataflows
```

---

## Key Insights from Research

### 1. OECD API is Different from Other Providers

- **World Bank/IMF**: Simple indicator codes (e.g., `NY.GDP.MKTP.CD`)
- **OECD**: Requires agency + dataflow + version + complex filter keys
- **Implication**: Can't use same simple patterns, need dynamic discovery

### 2. Rate Limiting is SEVERE

- 20 requests/hour = extremely restrictive
- Our retry strategy alone won't solve this
- MUST implement aggressive caching and delay enforcement
- Users need to understand OECD queries may be slow

### 3. DSD Structure is Critical

- Can't query data without knowing dimension order
- Must query structure definition first (adds extra API call)
- Caching structures is essential to avoid double API hits

### 4. User-Specified Provider Override is Broken

- Current implementation: 0% success rate for explicit "from OECD" requests
- Root cause: LLM prompt discourages OECD usage
- Fix is simple: Update prompt configuration

### 5. Our Implementation is Actually Very Good

- Dynamic indicator resolution is sophisticated
- Agency detection logic is comprehensive
- Response parsing handles OECD quirks correctly
- Main issues are: (1) LLM routing, (2) Rate limit config, (3) Cache TTL

---

## Testing Recommendations

### Unit Tests Needed

1. **Rate Limiter**:
   - Verify 180s minimum delay enforced
   - Test burst allowance behavior
   - Confirm requests are tracked correctly

2. **Dimension Key Builder**:
   - Test with various DSD structures
   - Verify fallback when DSD unavailable
   - Test country code insertion at correct position

3. **Country Code Normalization**:
   - Test all 38+ OECD countries
   - Verify alternative names work
   - Test country groups (OECD, G7, EU27_2020)

4. **Indicator Resolution**:
   - Test cache hit/miss behavior
   - Verify metadata search integration
   - Test LLM selection with various confidence scores
   - Verify fallback to local catalog

### Integration Tests Needed

1. **Full Query Flow**:
   - Natural language → LLM parsing → OECD provider → normalized data
   - Test explicit "from OECD" routing
   - Test multi-country queries
   - Test OECD aggregates (OECD average)

2. **Rate Limit Handling**:
   - Simulate 20 requests in 1 hour
   - Verify 21st request waits appropriately
   - Test retry behavior on 429 errors

3. **Error Scenarios**:
   - Invalid country code
   - Invalid indicator
   - No data for time range
   - Rate limit exceeded
   - DSD structure unavailable

### Production Testing

Re-run the test suite from `OECD_TEST_SUMMARY.md` after implementing fixes:
- 30 queries covering explicit requests, aggregates, and member countries
- Expected success rate: >90% (27+ out of 30)
- All explicit "from OECD" requests must route correctly (8/8)

---

## Conclusion

### Summary of Findings

1. **OECD API Structure**: Our implementation correctly uses SDMX-JSON 2.0 format with proper agency/dataflow/version identifiers
2. **Rate Limiting**: Severely restrictive (20/hour), need to verify our limiter config and extend cache TTL
3. **Filter Syntax**: Complex dimension-based keys - our dynamic DSD lookup approach is correct
4. **Country Codes**: ISO 3166-1 alpha-3 - our mapping is comprehensive
5. **Response Parsing**: OECD has quirks (no position field) - we handle correctly
6. **Main Problem**: LLM prompt discourages OECD usage, causing 100% routing failure

### High-Priority Fixes

1. **Update LLM prompt** (30 minutes)
   - Remove negative OECD instruction
   - Add OECD specializations
   - Strengthen explicit override rules

2. **Verify rate limiter config** (15 minutes)
   - Ensure 180s minimum delay for OECD
   - Confirm request tracking works

3. **Extend cache TTL** (5 minutes)
   - Change from 24h to 7 days
   - Reduce API hits significantly

4. **Test routing** (1 hour)
   - Re-run OECD test suite
   - Verify explicit requests work
   - Confirm aggregates route correctly

### Low-Priority Improvements

1. **Enhanced error messages** (30 minutes)
   - Add rate limit guidance
   - Suggest alternative providers

2. **DSD structure caching** (if not already implemented) (2 hours)
   - Cache to disk, not memory
   - Reduce double API hits

3. **User-facing warnings** (1 hour)
   - Notify users OECD queries may be slow
   - Suggest alternative providers for faster results

---

## References and Sources

### Official Documentation
- [OECD Data API](https://data.oecd.org/api/)
- [API documentation (SDMX-JSON)](https://data.oecd.org/api/sdmx-json-documentation/)
- [API documentation (SDMX-ML)](https://data.oecd.org/api/sdmx-ml-documentation/)
- [OECD Data Explorer FAQ](https://www.oecd.org/en/data/insights/data-explainers/2024/09/OECD-DE-FAQ.html)
- [API Best Practices (2024)](https://www.oecd.org/en/data/insights/data-explainers/2024/11/Api-best-practices-and-recommendations.html)
- [OECD Data Explorer Platform Status](https://www.oecd.org/en/data/insights/data-explainers/2025/02/OECD-Data-Explorer-News.html)

### Technical Resources
- [Download OECD API data using Python and SDMX - Stack Overflow](https://stackoverflow.com/questions/77806733/download-oecd-api-data-using-python-and-sdmx)
- [.Stat SDMX RESTful Web Service Cheat Sheet](https://sis-cc.gitlab.io/dotstatsuite-documentation/using-api/restful/)
- [Typical use cases - .Stat Suite documentation](https://sis-cc.gitlab.io/dotstatsuite-documentation/using-api/typical-use-cases/)
- [SDMX Introduction with Examples | StatSilk](https://www.statsilk.com/sdmx/sdmx-introduction-simple-sdmx-ml-example-and-tutorial)
- [Extracting Data from OECD Databases in R (2024)](https://www.r-bloggers.com/2024/12/extracting-data-from-oecd-databases-in-r-using-the-oecd-and-rsdmx-packages/)

### Related Packages
- [GitHub - expersso/OECD](https://github.com/expersso/OECD) - R package for OECD data
- [sdmx1 Python library documentation](https://sdmx1.readthedocs.io/en/latest/sources.html)

---

**End of Report**
