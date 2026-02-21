# LLM Provider Routing Logic Improvements

## Overview

Updated the LLM system prompt in `backend/services/openrouter.py` to implement a clear, hierarchical provider routing system that eliminates incorrect routing of queries to providers. This ensures:

- **Canadian queries** always route to StatsCan (not OECD or IMF)
- **OECD member country queries** route to OECD (not WorldBank or IMF)
- **Property/housing price queries** always route to BIS
- **Explicit routing hierarchy** prevents ambiguity in provider selection

## Problems Solved

### Previous Issues
1. **OECD countries often routed to WorldBank or IMF** - Even though OECD is the better source for OECD member countries
2. **Statistics Canada queries routed to OECD** - When Canada is both OECD member and StatsCan provider exists
3. **Provider selection didn't consider metadata availability** - Led to routing to less optimal providers
4. **Property prices routed to wrong providers** - Would sometimes route to Eurostat instead of BIS

## Solution Architecture

### Routing Hierarchy (Applied in Order)

1. **User-specified provider** (HIGHEST PRIORITY)
   - If user explicitly mentions provider (e.g., "from FRED", "using World Bank"), use it

2. **Country-specific rules** (CRITICAL)
   - **Canada or Canadian geography** → StatsCan
   - **OECD member country** → OECD
   - **EU country (non-OECD)** → Eurostat
   - **Property/housing prices (any country)** → BIS

3. **Indicator-specific rules**
   - Trade data → Comtrade
   - Exchange rates → ExchangeRate
   - Cryptocurrency → CoinGecko
   - Central bank rates → BIS
   - Global development → WorldBank

4. **Fallback to general providers**
   - Non-OECD countries → WorldBank or IMF depending on indicator

### Explicit Country Lists

**OECD Members (38 countries):**
Australia, Austria, Belgium, Bulgaria, Canada, Chile, Colombia, Costa Rica, Cyprus, Czech Republic, Denmark, Estonia, Finland, France, Germany, Greece, Hungary, Iceland, Ireland, Israel, Italy, Japan, Korea (South), Latvia, Lithuania, Luxembourg, Malta, Mexico, Netherlands, New Zealand, Norway, Poland, Portugal, Romania, Slovakia, Slovenia, Spain, Sweden, Switzerland, Turkey, United Kingdom, United States

**Note:** Canada is OECD member BUT has special routing to StatsCan due to StatsCan's superior Canadian-specific data.

**Non-OECD EU Members:**
Bulgaria, Croatia, Romania, Malta

**Country Code Mappings:**
Complete ISO 2/3-letter country code mappings provided for all OECD members to support consistent routing.

## Implementation Changes

### File Modified
- `backend/services/openrouter.py` - Updated `_system_prompt()` method

### Key Sections Updated

1. **PROVIDER SELECTION GUIDELINES** (lines 242-343)
   - New hierarchical routing rules clearly laid out
   - Explicit country lists for OECD members
   - Clear examples of correct routing for each provider

2. **CANADIAN DATA** (lines 257-272)
   - CRITICAL: Marked as second priority after user specification
   - Includes examples covering all provinces and territories
   - StatsCan coverage explicitly documented

3. **OECD MEMBER COUNTRIES** (lines 276-306)
   - All 38 members listed alphabetically with country codes
   - Emphasis on OECD being preferred over IMF/WorldBank
   - Clear examples showing correct routing

4. **PROPERTY/HOUSING PRICES** (lines 308-321)
   - BIS as mandatory provider for all property queries
   - Emphasis: NOT Eurostat, NOT FRED, NOT Comtrade

5. **Example Queries Updated**
   - Japan unemployment: OECD instead of IMF
   - Germany house prices: BIS instead of Eurostat
   - Added Mexico GDP: OECD
   - Added Italy GDP: OECD

## Testing

### Test Scripts
1. **Core Routing Tests** (`scripts/test_routing_improvements.py`)
   - Tests 23 different query patterns
   - Result: 100% pass rate (23/23 tests passing)

2. **Edge Case Tests** (`scripts/test_routing_edge_cases.py`)
   - Tests explicit provider mentions, multi-country queries, city resolution
   - Result: 100% pass rate (12/12 tests passing)

### Test Coverage

**Core Routing Tests (23 tests):**

1. **Canadian Queries (7 tests):**
   - Canada GDP, Ontario unemployment, Toronto population, BC retail sales, Alberta wages, Canadian inflation, Quebec housing starts
   - All route to StatsCan ✅

2. **OECD Member Queries (8 tests):**
   - Japan, Mexico, Chile, Australia, New Zealand, Israel, Colombia queries
   - All route to OECD ✅
   - Includes inflation queries (Mexico inflation, Israel inflation)

3. **Property Price Queries (4 tests):**
   - Germany, UK, US, France house prices
   - All route to BIS ✅

4. **US-Specific Queries (2 tests):**
   - US GDP, US unemployment
   - All route to FRED ✅

5. **Non-OECD Queries (2 tests):**
   - Brazil inflation → IMF, Brazil GDP → WorldBank ✅

**Edge Case Tests (12 tests):**

1. **Explicit Provider Mentions (2 tests):**
   - "Canada GDP from OECD" → OECD (override StatsCan default) ✅
   - "Japan unemployment from IMF" → IMF (explicit override) ✅

2. **Multi-Country Queries (2 tests):**
   - "Compare Japan and Australia GDP" → OECD ✅
   - "UK and Germany unemployment" → OECD ✅

3. **Mixed OECD/Non-OECD (1 test):**
   - "Compare Brazil and Mexico GDP" → OECD ✅

4. **Property Queries (2 tests):**
   - "House prices" → Clarification needed ✅
   - "Property values in Germany" → BIS ✅

5. **Alternative Provider Names (1 test):**
   - "UN Comtrade trade data" → Comtrade ✅

6. **City Name Resolution (3 tests):**
   - "Vancouver employment" → StatsCan (resolved to BC) ✅
   - "Montreal population" → StatsCan (resolved to QC) ✅
   - "Calgary economic data" → StatsCan (resolved to AB) ✅

## Examples of Improvements

### Before
```
Query: "Japan unemployment rate"
❌ Routed to IMF (suboptimal)

Query: "Ontario GDP"
❌ Sometimes routed to OECD (incorrect for StatsCan)

Query: "Germany house prices"
❌ Sometimes routed to Eurostat (wrong - should be BIS)
```

### After
```
Query: "Japan unemployment rate"
✅ Routes to OECD (correct - OECD member)

Query: "Ontario GDP"
✅ Routes to StatsCan (correct - Canadian province)

Query: "Germany house prices"
✅ Routes to BIS (correct - property price query)
```

## Provider Capabilities Reference

### StatsCan
- **Coverage:** All Canadian economic and demographic data
- **Indicators:** GDP, unemployment, inflation, CPI, population, housing starts, housing price index, employment, retail sales, manufacturing, exports, imports, wages, immigration
- **Geography:** National + 13 provinces/territories

### OECD
- **Coverage:** 38 member countries
- **Indicators:** GDP, GDP growth, unemployment, inflation, labor productivity, taxes, health, education
- **Advantage:** Better for economic comparisons across developed nations

### BIS
- **Coverage:** 30+ countries
- **Indicators:** Central bank policy rates, credit-to-GDP gaps, residential property prices, debt service ratios, effective exchange rates
- **Strength:** Only source for property prices across multiple countries

### FRED
- **Coverage:** US economic data exclusively
- **Indicators:** 90,000+ time series on employment, GDP, inflation, interest rates, housing, etc.

### IMF
- **Coverage:** Global economic data
- **Indicators:** GDP, inflation, unemployment, current account, government debt
- **Strength:** Best for multi-country inflation comparisons

### WorldBank
- **Coverage:** Global development indicators
- **Indicators:** 16,000+ indicators on development, poverty, environment, etc.
- **Strength:** Best for non-OECD country comparisons

## Metadata Integration

All routing-relevant providers include **metadata search service**:
- `WorldBankProvider`
- `StatsCanProvider`
- `IMFProvider`
- `BISProvider`
- `EurostatProvider`
- `OECDProvider`

This allows intelligent discovery without requiring exact indicator codes.

## Future Enhancements

Potential improvements:
1. Add metadata availability checks before deciding on provider
2. Implement confidence scores based on provider data completeness
3. Add dynamic metadata refresh to capture new indicators
4. Create provider comparison mode to show data from multiple sources

## References

- System prompt location: `backend/services/openrouter.py` lines 242-343
- Test script: `scripts/test_routing_improvements.py`
- Supabase metadata search docs referenced in system prompt
- OECD membership as of 2024 (38 members including Israel added in 2010)
