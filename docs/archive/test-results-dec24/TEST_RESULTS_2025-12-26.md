# econ-data-mcp Comprehensive Test Results - December 26, 2025

## Executive Summary

**Overall Success Rate: 89% (89/100 queries)**

Testing covered 4 categories with 25 queries each:
- Economic Indicators: 68% success
- Trade & Financial: 100% success
- Multi-Country/Edge Cases: 92% success
- Natural Language Variations: 96% success

---

## Failure Pattern Analysis

### Critical Issues (Require Immediate Fix)

#### 1. Federal Reserve Interest Rate Misrouting
- **Query:** "Federal Reserve interest rate"
- **Expected:** FEDFUNDS or DFF series
- **Actual:** Returned Global Economic Policy Uncertainty Index
- **Root Cause:** FRED series mapping lacks explicit mapping for "federal reserve interest rate"
- **Impact:** HIGH - Core US monetary policy indicator
- **Fix Type:** General - Add intelligent series search for interest rate queries

#### 2. Frequency Specifications Ignored
- **Queries:** "UK unemployment monthly", "South Korea unemployment rate quarterly"
- **Expected:** Monthly/quarterly data
- **Actual:** Annual data returned
- **Root Cause:** Frequency parameter not passed to provider or not filtered
- **Impact:** MEDIUM - Affects granularity requirements
- **Fix Type:** General - Implement frequency filtering in data fetch pipeline

#### 3. Core Inflation Not Detected
- **Query:** "Japan core inflation excluding food and energy"
- **Expected:** Core CPI excluding volatile components
- **Actual:** Headline inflation
- **Root Cause:** "core" and "excluding" qualifiers not parsed
- **Impact:** MEDIUM - Wrong economic indicator
- **Fix Type:** General - Add inflation variant detection in intent parsing

### Moderate Issues (Should Fix)

#### 4. Real vs Nominal GDP Confusion
- **Query:** "China real GDP annual"
- **Expected:** Real GDP (constant prices)
- **Actual:** Nominal GDP (current prices)
- **Root Cause:** "real" qualifier not triggering constant-price series
- **Impact:** MEDIUM - Different economic measure
- **Fix Type:** General - Add GDP variant detection

#### 5. Yield Curve Spread Not Computed
- **Query:** "yield curve spread 10y 2y"
- **Expected:** T10Y2Y or computed 10Y-2Y difference
- **Actual:** Only 10-year yield returned
- **Root Cause:** "spread" not triggering multi-series fetch or calculation
- **Impact:** MEDIUM - Important financial indicator
- **Fix Type:** General - Add spread/difference calculation patterns

#### 6. Country Group Incomplete
- **Queries:** "G7 unemployment rates" (6/7), "Nordic countries GDP per capita" (4/5)
- **Expected:** All group members
- **Actual:** Missing Japan (G7), Norway (Nordic)
- **Root Cause:** Incomplete country group mappings
- **Impact:** MEDIUM - Incomplete data
- **Fix Type:** Data - Update country group definitions

### Minor Issues (Low Priority)

#### 7. Trade Data Timeouts
- **Queries:** "ASEAN total exports", "Middle East oil exports"
- **Expected:** Trade data
- **Actual:** Timeout or no data
- **Root Cause:** UN Comtrade API limitations for regional aggregates
- **Impact:** LOW - Alternative providers available
- **Fix Type:** Fallback - Route regional trade to World Bank

#### 8. Country Misrouting
- **Query:** "Does Brazil have a trade deficit?"
- **Expected:** Brazil trade data
- **Actual:** US trade data (FRED default)
- **Root Cause:** Question-form query confused LLM routing
- **Impact:** LOW - Rare edge case
- **Fix Type:** General - Improve country extraction for inquiry-style queries

---

## Provider Performance

| Provider | Queries Routed | Success Rate | Issues |
|----------|----------------|--------------|--------|
| FRED | 18 | 89% | Interest rate mapping, yield spread |
| World Bank | 22 | 100% | None |
| Eurostat | 8 | 100% | None |
| IMF | 10 | 90% | Some country coverage gaps |
| UN Comtrade | 15 | 87% | Regional aggregates fail |
| CoinGecko | 5 | 100% | None |
| ExchangeRate-API | 5 | 100% | None |
| Statistics Canada | 3 | 100% | None |
| BIS | 3 | 100% | None |
| OECD | 3 | 67% | Rate limited, timeout issues |

---

## Recommended Framework Improvements

### Priority 1: FRED Series Discovery
```
Issue: Static mappings miss many common queries
Solution: Implement dynamic FRED series search for unmapped queries
Affected Queries: Federal funds rate, yield curve spread
Implementation: Add fallback to fred.search_series() API
```

### Priority 2: Frequency Filtering
```
Issue: Monthly/quarterly requests return annual data
Solution: Add frequency parameter to fetch pipeline
Affected Queries: UK unemployment monthly, Korea quarterly
Implementation: Pass frequency to provider.fetch(), filter results
```

### Priority 3: Indicator Variant Detection
```
Issue: Core/real/nominal qualifiers ignored
Solution: Parse qualifiers in intent and route to correct series
Affected Queries: Core inflation, real GDP
Implementation: Enhance LLM prompt or add post-processing
```

### Priority 4: Country Group Mappings
```
Issue: Incomplete G7, Nordic, etc. definitions
Solution: Audit and complete all country group mappings
Affected Queries: G7, Nordic, BRICS aggregates
Implementation: Update backend/data/country_groups.json
```

### Priority 5: Regional Trade Fallback
```
Issue: UN Comtrade fails for regional queries
Solution: Add fallback to World Bank for regional trade
Affected Queries: ASEAN exports, Middle East oil
Implementation: Add fallback chain in provider_router.py
```

---

## Test Coverage Matrix

| Category | US | Europe | Asia | Americas | Africa | Multi |
|----------|----|---------|----- |----------|--------|-------|
| GDP | OK | OK | OK | OK | OK | OK |
| Unemployment | OK | OK | OK | OK | OK | OK |
| Inflation | Partial | OK | Partial | OK | OK | OK |
| Trade | OK | OK | OK | OK | OK | Fail |
| Interest Rates | Fail | OK | OK | - | - | - |
| Exchange Rates | OK | OK | OK | OK | - | - |
| Crypto | OK | - | - | - | - | - |

---

## Next Steps

1. **Implement FRED series discovery** - Add dynamic search fallback
2. **Add frequency filtering** - Pass and filter by frequency parameter
3. **Enhance indicator parsing** - Detect core/real/nominal qualifiers
4. **Complete country groups** - Audit G7, Nordic, BRICS, ASEAN mappings
5. **Add regional trade fallback** - World Bank as backup for Comtrade
6. **Re-test after fixes** - Verify all 100 queries pass

---

## Appendix: All Failed/Partial Queries

| Query | Status | Issue |
|-------|--------|-------|
| Federal Reserve interest rate | FAIL | Wrong indicator returned |
| UK retail price index | FAIL | RPI not available |
| Sweden government debt to GDP ratio | FAIL | Data not found |
| UK unemployment monthly | PARTIAL | Annual instead of monthly |
| South Korea unemployment rate quarterly | PARTIAL | Annual instead of quarterly |
| Japan core inflation excluding food and energy | PARTIAL | Headline instead of core |
| China real GDP annual | PARTIAL | Nominal instead of real |
| yield curve spread 10y 2y | PARTIAL | Only 10Y, no spread |
| Canada employment rate | PARTIAL | Units unclear (thousands) |
| ASEAN total exports | FAIL | Comtrade timeout |
| Middle East oil exports | FAIL | Comtrade timeout |
| Does Brazil have a trade deficit? | PARTIAL | Returned US instead |
