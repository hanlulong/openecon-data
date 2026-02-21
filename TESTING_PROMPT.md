# econ-data-mcp Comprehensive Testing & Improvement Prompt

**Purpose:** Guide AI assistants in systematically testing, identifying issues, and implementing robust, future-proof solutions for the econ-data-mcp economic data platform.

---

## ⚠️ FIRST: Read CLAUDE.md

> **MANDATORY:** Before doing ANY testing or development work, read `/home/hanlulong/econ-data-mcp/CLAUDE.md` in full.

CLAUDE.md contains critical project-specific instructions including:
- Backend/frontend server management (restart scripts, process handling)
- Rate limiting behavior (bypassed in development)
- Provider-specific notes and limitations
- Production deployment procedures
- Environment configuration
- Architecture overview and data flow

**Do not proceed with testing until you have read and understood CLAUDE.md.**

---

## ⚠️ OECD Provider - LOW PRIORITY

> **IMPORTANT:** OECD has strict rate limits (60 requests/hour) and should be treated as low priority.

### OECD Rules:
1. **DO NOT test OECD** in automated testing sessions - rate limits cause cascading failures
2. **DO NOT route to OECD** unless the user explicitly requests OECD data
3. **DO NOT prioritize OECD bug fixes** - focus on other providers (FRED, World Bank, IMF, Eurostat, etc.)
4. **OECD queries may fail or timeout** - this is expected due to API limitations
5. When OECD fails, queries automatically fall back to Eurostat/IMF/World Bank

### Why OECD is Low Priority:
- Rate limited to 60 requests/hour (we use 50/hour with safety margin)
- Timeouts are common even with caching
- Most OECD data is available from other providers (IMF, Eurostat, World Bank)
- DSD cache helps but doesn't solve the fundamental rate limit issue

### When to Use OECD:
- ONLY when user explicitly says "OECD" or "from OECD"
- ONLY for data exclusively available from OECD
- Never as a default or fallback provider

---

## The Five Pillars

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  1. UNDERSTAND FIRST     - Read codebase IN DETAIL before any changes      │
│  2. THINK DEEPLY         - Use ultrathink for ALL critical steps           │
│  3. VERIFY DATA ACCURACY - Wrong data is worse than failing tests          │
│  4. BUILD FOR THE FUTURE - Data-driven, extensible, resilient solutions    │
│  5. VERIFY ALWAYS        - Double-check after EVERY step                   │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Core Philosophy

> **CRITICAL: Tests exist to IMPROVE THE FRAMEWORK, not to fix individual test questions.**

**THE GOLDEN RULE:** If your fix only solves the test question, you haven't fixed anything.

---

## 🚨 INFRASTRUCTURE FIRST - NON-NEGOTIABLE 🚨

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                                                                              ║
║   EVERY FIX MUST BE AN INFRASTRUCTURE IMPROVEMENT                            ║
║                                                                              ║
║   ❌ WRONG: "Query X failed → Add mapping for X → Query X passes"            ║
║   ✅ RIGHT: "Query X failed → Why did the system not handle this class       ║
║             of query? → Fix the discovery/routing/parsing mechanism →        ║
║             ALL similar queries now pass"                                    ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### The Infrastructure Mindset

When a test fails, ask these questions IN ORDER:

1. **"What CATEGORY of query is this?"** (GDP query? Trade query? Comparison?)
2. **"What MECHANISM should have handled this?"** (Router? Provider? Parser?)
3. **"Why did that mechanism fail?"** (Missing logic? Incomplete data? Poor search?)
4. **"How many OTHER queries would fail for the SAME reason?"** (If only one, dig deeper)
5. **"What architectural change fixes ALL of them?"**

### Examples of Infrastructure vs Quick Fixes

| Failure | ❌ Quick Fix | ✅ Infrastructure Fix |
|---------|--------------|----------------------|
| "yield curve spread" not found | Add to SERIES_MAPPINGS | Improve FTS search synonyms, add keywords to database |
| Nigeria GDP returns wrong provider | Add if-statement for Nigeria | Fix country-to-provider routing logic |
| "compare X and Y" returns single series | Handle this specific query | Improve comparison detection in router |
| Exchange rate query times out | Increase timeout | Add circuit breaker, caching, fallback providers |
| HS code trade query fails | Hardcode HS code mapping | Improve HS code parser in Comtrade provider |

### The 5-Query Test

Before committing ANY fix, verify it helps at least 5 similar queries:
```
FIX: Improved country detection for African nations
TEST 1: Nigeria GDP ✅
TEST 2: Kenya inflation ✅
TEST 3: South Africa unemployment ✅
TEST 4: Egypt trade balance ✅
TEST 5: Morocco GDP growth ✅
```

If your fix only helps 1-2 queries, the fix is too narrow. Find the underlying pattern.

---

## 🔢 100 QUERY MINIMUM - NON-NEGOTIABLE 🔢

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                                                                              ║
║   EVERY TESTING SESSION MUST TEST AT LEAST 100 DIVERSE QUERIES              ║
║                                                                              ║
║   ❌ WRONG: "Test 25 queries, 25/25 pass, we're done!"                       ║
║   ✅ RIGHT: "Test 100+ queries across ALL categories, ALL providers,         ║
║             ALL edge cases - then verify infrastructure handles future       ║
║             queries we haven't seen yet"                                     ║
║                                                                              ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

### Why 100 Queries Minimum?

1. **Coverage:** 10 providers × 10 query types = 100 minimum combinations
2. **Edge Cases:** Need variety to catch routing, parsing, and data issues
3. **Statistical Significance:** 25 queries can pass by chance; 100 validates the system
4. **Future Proofing:** Diverse tests ensure framework handles novel queries

### Query Distribution

| Category | Minimum | Coverage |
|----------|---------|----------|
| Economic Indicators | 40 | GDP, unemployment, inflation, interest rates, etc. |
| Trade Flows | 20 | Bilateral, HS codes, commodities |
| Financial Data | 20 | Exchange rates, crypto, credit ratios |
| Multi-Country | 10 | G7, BRICS, regional groupings |
| Sequential/Complex | 10 | Follow-ups, multi-step queries |

---

### Testing IS About:
- Diagnosing **systemic weaknesses** in architecture
- Identifying **patterns** that reveal design flaws
- Building **robust framework** that handles future unknowns
- Preventing **entire classes of errors**, not individual instances

### Testing is NOT About:
- Making a specific failing query pass
- Applying quick patches to silence errors
- Adding special cases for particular inputs
- Getting to 100% pass rate as fast as possible

### Red Flags Your Fix is Too Narrow:
- Adding special case for one country/indicator/provider
- Matching on exact query text
- Hardcoding specific values or mappings
- Fix won't help with slightly different phrasings
- **Adding entries to static mapping dictionaries** (e.g., `SERIES_MAPPINGS["GOLD_PRICE_USD"] = "GOLDAMGBD228NLBM"`)

---

## ⚠️ CRITICAL: Indicator Discovery is the ONLY Solution

> **ABSOLUTE RULE: NEVER add hardcoded indicator mappings. ALWAYS fix the discovery mechanism.**

### The Indicator Database Approach

econ-data-mcp has a comprehensive indicator database (`backend/data/indicators.db`) with **330,000+ indicators**:
- FRED: ~138,000 series
- IMF: ~115,000 indicators
- World Bank: ~29,000 indicators
- CoinGecko: ~19,000 cryptocurrencies
- And more from Eurostat, BIS, StatsCan, OECD, Comtrade

**This database uses FTS5 full-text search and should handle ANY indicator query.**

### When an Indicator Query Fails

If a query like "yield curve spread 10y 2y" fails, the correct response is:

1. **WRONG:** Add `"YIELD_CURVE_SPREAD": "T10Y2Y"` to SERIES_MAPPINGS
2. **RIGHT:** Ensure the indicator database contains T10Y2Y and the FTS search can find it

### The Indicator Discovery Stack

The system has THREE levels of indicator resolution:

```
Level 1: Static Mappings (SERIES_MAPPINGS)
├── Fast, for common queries
├── Should be MINIMAL (only most common aliases)
└── DO NOT ADD TO THIS

Level 2: Indicator Database Search (backend/services/indicator_lookup.py)
├── FTS5 full-text search over 330K+ indicators
├── Searches by name, code, keywords, synonyms
├── MUST contain ALL available series from ALL providers
└── If query fails here, ADD TO DATABASE, not to static mappings

Level 3: Provider API Search (e.g., FRED's series/search endpoint)
├── Dynamic fallback when database doesn't have it
├── Caches results for future queries
└── Should rarely be needed if database is complete
```

### How to Fix Missing Indicators

When a test fails because an indicator isn't found:

```bash
# 1. Check if indicator exists in database
python3 -c "
from backend.services.indicator_lookup import get_indicator_lookup
lookup = get_indicator_lookup()
results = lookup.search('yield curve spread', provider='FRED', limit=10)
for r in results:
    print(f\"{r['code']}: {r['name']}\")
"

# 2. If NOT in database, add it by running the fetch script
python3 scripts/fetch_all_indicators.py --provider FRED --update

# 3. If indicator doesn't exist at provider, document it
# (some queries are impossible - e.g., real-time gold spot prices from FRED)
```

### Populating the Indicator Database

**Before testing, ensure the database is complete:**

```bash
# Fetch all indicators from all providers
python3 scripts/fetch_all_indicators.py --all

# Or specific provider
python3 scripts/fetch_all_indicators.py --provider FRED
python3 scripts/fetch_all_indicators.py --provider WorldBank
python3 scripts/fetch_all_indicators.py --provider IMF
```

**The database should be refreshed periodically** to capture new indicators added by providers.

### Fix the Search, Not the Data

If the database has an indicator but search doesn't find it:

1. **Improve the FTS query** in `indicator_database.py`
2. **Add better keywords/synonyms** to the indicator metadata
3. **Improve the ranking algorithm** in `indicator_lookup.py`

These are GENERAL fixes that help ALL queries, not just one.

### Example: Yield Curve Spread

**Query:** "yield curve spread 10y 2y"
**Expected series:** T10Y2Y (10-Year Treasury Constant Maturity Minus 2-Year)

**WRONG fix:**
```python
# Adding to SERIES_MAPPINGS in fred.py
"YIELD_CURVE_SPREAD": "T10Y2Y",
"10Y_2Y_SPREAD": "T10Y2Y",
```

**RIGHT fix:**
1. Ensure T10Y2Y is in the indicator database with keywords: "yield curve spread treasury 10 year 2 year"
2. Ensure FTS search for "yield curve spread" returns T10Y2Y as top result
3. If not, improve the search algorithm or add synonyms to the database

---

### Example: Wrong vs Right Approach

**WRONG (Narrow Fix):**
```python
# Problem: Gold price returns wrong FRED series
# Bad fix: Add specific mapping
SERIES_MAPPINGS["GOLD_PRICE_USD"] = "GOLDAMGBD228NLBM"
SERIES_MAPPINGS["S&P500"] = "SP500"
```
This only fixes the specific indicators mentioned. Future unknown indicators still fail.

**RIGHT (General Fix):**
```python
# Problem: Gold price returns wrong FRED series
# Good fix: Add dynamic series search fallback
async def _find_best_series(self, indicator: str) -> Optional[str]:
    """Search FRED's series/search API when static mappings don't exist."""
    series_list = await self._search_series_dynamic(indicator)
    return self._rank_and_select_best_match(series_list, indicator)
```
This works for ANY future indicator - no code changes needed for new series.

**The Root Cause Question:**
When you identify a failing test, ask: "Why didn't the existing architecture handle this?"

If the answer is "because we didn't have a mapping for X", the fix is NOT to add the mapping.
The fix is to add a mechanism that can discover mappings dynamically (like other providers have).

### The Framework Improvement Mindset

When a test fails, ask yourself:
1. **"What does this failure reveal about the system?"** - Missing abstraction? Gap in data flow?
2. **"How many OTHER queries would fail for the same reason?"** - If only one, dig deeper
3. **"What architectural improvement would prevent this CLASS of error?"**
4. **"Will this fix make the system smarter?"** - Good fix teaches, bad fix memorizes

---

## Critical Requirements

### 1. Use `ultrathink` for All Critical Steps

> **MANDATORY:** Invoke extended thinking for all critical reasoning.

When to use ultrathink:
- **Root cause analysis** - Understanding why something failed
- **Architecture decisions** - Designing framework improvements
- **Pattern recognition** - Identifying systemic issues across failures
- **Complex debugging** - Tracing data flow through multiple components
- **Solution design** - Creating general solutions that handle all cases
- **Compatibility assessment** - Ensuring changes don't break existing functionality
- **Risk evaluation** - Assessing potential side effects of changes

**How to use:**
1. Explicitly state "Using ultrathink for [critical step]"
2. Take time to deeply reason through the problem
3. Consider multiple approaches before deciding
4. Document your reasoning for future reference

### 2. Deep Codebase Understanding BEFORE Changes

> **MANDATORY:** Read and understand the codebase IN DETAIL before making any changes.

You cannot fix what you don't understand. Before testing or implementing any fix:

```
CODEBASE READING CHECKLIST (use ultrathink):

1. DATABASE STRUCTURE:
   [ ] backend/models.py - All Pydantic models
   [ ] Data flow: Query → ParsedIntent → Provider → NormalizedData
   [ ] State management in frontend and backend
   [ ] Caching mechanisms

2. AGENT ARCHITECTURE:
   [ ] backend/agents/__init__.py - Agent exports and structure
   [ ] backend/agents/orchestrator.py - Query orchestration
   [ ] backend/agents/langgraph_*.py - LangGraph state and graph
   [ ] backend/agents/router_agent.py - Query routing logic
   [ ] backend/agents/data_agent.py - Data fetching logic

3. PROVIDER IMPLEMENTATIONS:
   [ ] backend/providers/*.py - Each provider's API integration
   [ ] Parameter mapping for each provider
   [ ] Error handling patterns
   [ ] Data normalization

4. SERVICE LAYER:
   [ ] backend/services/query.py - Main query processing
   [ ] backend/services/llm.py - LLM integration
   [ ] backend/services/metadata_search.py - RAG-based search
   [ ] backend/services/code_executor.py - Pro Mode execution

5. FRONTEND:
   [ ] packages/frontend/src/types/index.ts - Type definitions
   [ ] packages/frontend/src/services/api.ts - API client
   [ ] packages/frontend/src/components/*.tsx - UI components

6. RELATIONSHIPS:
   [ ] How do types align between frontend and backend?
   [ ] How does state flow through the system?
   [ ] Where are the abstraction boundaries?
   [ ] What are the shared utilities?
```

**Only after completing this reading should you start testing or implementing fixes.**

### 3. Double-Check After EVERY Step

> **MANDATORY:** Verify compatibility, consistency, and errors after completing each step.

```
POST-STEP VERIFICATION CHECKLIST:

COMPATIBILITY:
[ ] Does this change work with existing code?
[ ] Are all imports and dependencies satisfied?
[ ] Does this change break any existing tests?
[ ] Is this compatible with both frontend and backend?
[ ] Are types aligned between TypeScript and Python?

CONSISTENCY:
[ ] Does this follow existing code patterns?
[ ] Does this use shared utilities where appropriate?
[ ] Are naming conventions consistent?
[ ] Is error handling consistent with other components?

ERRORS:
[ ] Run pytest to check for test failures
[ ] Run npm run build:frontend to check TypeScript
[ ] Check for runtime errors with curl tests
[ ] Verify no circular imports
```

**Verification commands to run after EVERY change:**
```bash
python3 -c "import backend.main"
pytest backend/tests/ -x -q --tb=short 2>&1 | head -50
npm run build:frontend 2>&1 | tail -20
curl -s http://localhost:3001/api/health | python3 -m json.tool
tail -20 /tmp/backend-dev.log 2>/dev/null | grep -i error
```

**If any check fails:** STOP → Analyze with ultrathink → Fix → Re-run ALL checks

---

## Data Accuracy Verification

> **CRITICAL: Correct data is the entire point. A test that returns wrong data is worse than a test that fails.**

### Why Data Accuracy Matters

The system can:
- Return data from the correct provider ✓
- Have no errors ✓
- Display a beautiful chart ✓
- **But still be WRONG** ✗

A "passing" test with incorrect data is a **silent failure**—the most dangerous kind.

### Verification Protocol (use ultrathink)

For EVERY query result, perform these steps:

```
DATA ACCURACY CHECKLIST:

1. SOURCE VERIFICATION:
   [ ] Search online for the AUTHORITATIVE source for this data
   [ ] Find official government/institution page (not third-party)
   [ ] Example: US GDP → Bureau of Economic Analysis (bea.gov)
   [ ] Example: Japan inflation → Statistics Bureau of Japan

2. VALUE VERIFICATION:
   [ ] Compare returned values with official source
   [ ] Check at least 3-5 data points (not just latest)
   [ ] Verify historical values are correct
   [ ] Tolerance: ±1% for economic data (rounding differences)

3. UNIT VERIFICATION:
   [ ] Is the unit correct? (millions, billions, trillions, percent)
   [ ] Is the scale correct? (per capita, annual rate, seasonally adjusted)
   [ ] Example: GDP in billions vs millions = 1000x error
   [ ] Example: Inflation as 2.5 vs 0.025 = different unit conventions

4. TEMPORAL VERIFICATION:
   [ ] Are dates correct? (year, quarter, month)
   [ ] Is frequency correct? (annual, quarterly, monthly)
   [ ] Is seasonally adjusted vs non-adjusted correct?
   [ ] Are lag periods handled correctly?

5. GEOGRAPHIC VERIFICATION:
   [ ] Is the correct country/region returned?
   [ ] Watch for country code confusion (US vs USA, UK vs GB)
   [ ] Watch for aggregations (EU27 vs EU28, OECD members)

6. SANITY CHECKS:
   [ ] Values within reasonable bounds?
   [ ] GDP per capita: $500-$150,000 (not $5 or $5 million)
   [ ] Unemployment rate: 0%-50% (not negative or >100%)
   [ ] Inflation rate: -10% to +100% (hyperinflation cases exist)
   [ ] Population: actual counts (not in wrong units)
```

### Authoritative Sources Reference

| Data Type | Primary Sources |
|-----------|-----------------|
| US Economic | BEA, BLS, FRED, Census |
| UK Economic | ONS, Bank of England |
| EU Economic | Eurostat, ECB |
| Japan Economic | Statistics Bureau, Bank of Japan |
| China Economic | NBS China, PBOC |
| Global Comparisons | World Bank, IMF, OECD |
| Trade Data | UN Comtrade, WTO |
| Exchange Rates | Central banks, IMF |

### When Data Doesn't Match

1. **Document the discrepancy:**
   ```
   Query: "US GDP 2023"
   Returned: $25.46 trillion
   Official (BEA): $27.36 trillion
   Difference: 7% - SIGNIFICANT ERROR
   ```

2. **Trace the source:** Which provider? What API endpoint? Stale cache? Unit conversion?

3. **Identify root cause:** Wrong indicator code? Wrong transformation? Outdated source?

4. **Implement GENERAL fix:** Not just for this query. Add validation to catch similar issues.

---

## Indicator System (Proactive Discovery)

> **CRITICAL: Don't wait for tests to fail. Proactively build a comprehensive indicator knowledge base.**

### The Indicator Problem

Economic data comes from many providers with:
- Different indicator codes (GDP, NY.GDP.MKTP.CD, NGDP, etc.)
- Different naming conventions
- Different coverage and granularity
- Different update frequencies

Without a systematic approach, we're always playing catch-up.

### Proactive Discovery Protocol

**BEFORE testing, systematically catalog indicators:**

```
INDICATOR DISCOVERY WORKFLOW:

PHASE 1: PROVIDER CATALOG MINING
For EACH provider (FRED, WorldBank, IMF, Eurostat, BIS, Comtrade, StatsCan):
Note: SKIP OECD - rate limited and low priority. See "OECD Provider - LOW PRIORITY" section.
1. Fetch provider's indicator catalog/API documentation
2. Extract ALL available indicators with codes
3. Categorize by topic (GDP, unemployment, inflation, trade, etc.)
4. Document coverage (countries, frequency, time range)
5. Store in structured format (JSON/YAML)

PHASE 2: CROSS-PROVIDER MAPPING
For EACH indicator category:
1. Identify all provider codes for same concept
2. Map synonyms and natural language variations
3. Note methodology differences between providers
4. Determine best provider by use case (coverage, frequency, recency)
5. Create synonym → indicator code mappings

PHASE 3: COVERAGE MATRIX
Build matrix: Indicator × Country × Provider
- Which provider has what for which country?
- What's the most recent data available?
- What frequency is available (daily, monthly, quarterly, annual)?
- What historical depth?

PHASE 4: AUTOMATED VALIDATION
Create validation rules for each indicator type:
- Expected value ranges (GDP per capita: $500-$150,000)
- Unit consistency checks
- Temporal consistency checks
- Cross-provider comparison for same indicator
```

### Indicator Index Structure

**Maintain in `docs/indicators/INDICATOR_INDEX.md`:**

```markdown
## [Indicator Category]

### Canonical Name: [Full official name]

### Synonyms
[term1], [term2], [term3]...

### Provider Mappings
| Provider | Code | Coverage | Frequency | Units | Notes |
|----------|------|----------|-----------|-------|-------|

### Query Patterns That Should Match
- "[country] [indicator]"
- "[indicator] of [country]"
- "show me [indicator] for [country]"

### Related Indicators
- [Related 1]: [codes]
- [Related 2]: [codes]

### Known Issues
- [Caveat 1]
- [Caveat 2]
```

### Building the Indicator Knowledge Base

**Create and maintain these files:**

1. `backend/data/indicators/index.json` - Master indicator index
2. `backend/data/indicators/synonyms.json` - Indicator synonym mappings
3. `backend/data/indicators/provider_codes.json` - Provider-specific codes
4. `backend/data/indicators/coverage.json` - Coverage matrix
5. `docs/indicators/INDICATOR_INDEX.md` - Human-readable documentation

**Automated Discovery Script:** `scripts/discover_indicators.py`
```python
async def discover_all_indicators():
    """Proactively discover and catalog all available indicators."""
    catalogs = {}
    for provider in PROVIDERS:
        catalogs[provider] = await provider.fetch_catalog()
    index = build_unified_index(catalogs)
    mappings = find_equivalent_indicators(index)
    coverage = build_coverage_matrix(index)
    save_index(index, mappings, coverage)
    return index
```

---

## Future-Proofing Principles

> **Build systems that work for queries we haven't seen yet, not just queries we've tested.**

### The Future-Proof Mindset

Ask for every fix:
- **"Will this work in 5 years?"**
- **"Will this work for a country that doesn't exist yet?"**
- **"Will this work for a new indicator type?"**
- **"Will this work when providers change their APIs?"**

### Future-Proofing Checklist

```
BEFORE implementing any solution:

EXTENSIBILITY:
[ ] Can new providers be added without changing this code?
[ ] Can new indicators be added without changing this code?
[ ] Can new countries be added without changing this code?
[ ] Is the solution data-driven, not hard-coded?

ABSTRACTION:
[ ] Is the logic at the right level of abstraction?
[ ] Are patterns extracted, not instances?
[ ] Are there clear interfaces between components?
[ ] Can implementations be swapped without breaking contracts?

CONFIGURATION:
[ ] Are variable parts in configuration, not code?
[ ] Can behavior be changed without deployment?
[ ] Are magic numbers and strings externalized?

RESILIENCE:
[ ] What happens when a provider API changes?
[ ] What happens when a provider goes down?
[ ] What happens with unexpected data formats?
[ ] Are there appropriate fallbacks?

DOCUMENTATION:
[ ] Will someone understand this in 2 years?
[ ] Are assumptions documented?
[ ] Is the "why" explained, not just the "what"?
```

### Future-Proof Architecture Patterns

**1. Data-Driven Routing (GOOD):**
```python
INDICATOR_PROVIDERS = load_from_config("indicator_providers.yaml")
def route_query(intent: ParsedIntent) -> List[str]:
    return INDICATOR_PROVIDERS.get(intent.indicator_type, DEFAULT_PROVIDERS)
```

**2. Provider Abstraction (GOOD):**
```python
class DataProvider(Protocol):
    async def fetch(self, params: QueryParams) -> RawData: ...
    def normalize(self, data: RawData) -> NormalizedData: ...
    def validate(self, data: NormalizedData) -> bool: ...
```

**3. Graceful Degradation (GOOD):**
```python
async def fetch_with_fallback(params: QueryParams) -> DataResult:
    for provider in prioritized_providers:
        try:
            data = await provider.fetch(params)
            if validate(data):
                return DataResult(data=data, source=provider.name)
        except ProviderError as e:
            log.warning(f"{provider.name} failed: {e}")
            continue
    return DataResult(
        error="Data not available from any source",
        suggestions=get_alternative_queries(params),
    )
```

---

## Testing Methodology

### Phase 1: Deep Codebase Review (ultrathink)

Read EVERY relevant file in detail. Understand WHY, not just WHAT.
- Don't skim - read line by line for critical files
- Note any inconsistencies or potential issues
- Document the architecture as you understand it

### Phase 2: Proactive Indicator Discovery

Run discovery workflow BEFORE testing:
1. Fetch all provider catalogs
2. Build unified indicator index
3. Create coverage matrix
4. Generate synonym mappings

### Phase 3: Test Generation

> **CRITICAL: Generate 100 NEW UNIQUE queries each testing session.**
>
> **IMPORTANT: Do NOT reuse previous test queries.** Each session must generate completely NEW queries that haven't been tested before. This ensures:
> - We discover NEW edge cases and failure patterns
> - We don't just re-validate known passing queries
> - We continuously expand our test coverage
> - We catch regressions across different query formulations
>
> The goal is to ensure ALL future user queries can be handled correctly. Each test run must include:
> - Different TYPES of questions (not just country/time variations)
> - Novel phrasings users might naturally use
> - Edge cases and ambiguous queries
> - Multi-step and complex analytical requests
>
> This ensures the framework handles the full spectrum of real-world user queries.

**Query Diversity Requirements:**

### 1. Question Types (vary the structure)
- **Direct data requests:** "US GDP", "Show me inflation"
- **Comparative questions:** "Compare X and Y", "Which is higher, X or Y?"
- **Trend questions:** "How has X changed over time?", "Is X increasing?"
- **Relationship questions:** "What's the correlation between X and Y?"
- **Availability questions:** "Does Eurostat have X?", "Is Y available?"
- **Explanation questions:** "Why did X change in 2020?"
- **Ranking questions:** "Top 10 countries by GDP", "Highest unemployment rates"
- **Aggregate questions:** "Total EU exports", "Average G7 growth"
- **Historical questions:** "What was X during the 2008 crisis?"
- **Forecast questions:** "What's the outlook for X?", "IMF projections"

### 2. Natural Language Variations
- Formal: "Please provide the unemployment rate for Germany"
- Casual: "what's germany's unemployment?"
- Abbreviated: "US GDP 2023"
- Verbose: "I'd like to see the gross domestic product figures for the United States"
- Question form: "What is the inflation rate in Japan?"
- Command form: "Show me Japan inflation"
- Implicit: "Germany vs France economy" (implies comparison)

### 3. Edge Cases to Include
- Ambiguous country names: "Korea" (North or South?), "Congo" (which one?)
- Partial indicators: "growth" (GDP growth? Population growth?)
- Slang/abbreviations: "fed rate", "crypto", "forex"
- Misspellings: "Germeny GDP", "unemplyment rate"
- Multiple indicators: "GDP, unemployment, and inflation for US"
- Conditional queries: "GDP if available, otherwise population"
- Negations: "All G7 except US"

### 4. Complex Analytical Queries
- "Calculate the debt-to-GDP ratio for EU countries"
- "Show the year-over-year change in unemployment"
- "Compare pre and post-pandemic GDP growth"
- "Normalize inflation rates to 2015 baseline"
- "Show trade balance as percentage of GDP"

### 5. Provider-Specific Edge Cases
- FRED-specific series: "UNRATE", "GS10", "T10Y2Y"
- Comtrade HS codes: "HS 8703 imports", "Chapter 27 exports"
- StatsCan vectors: "v41690973", "table 14-10-0287"
- Eurostat datasets: "nama_10_gdp", "prc_hicp_mmor"

### 6. Countries and Regions
- Standard: US, UK, Germany, France, Japan, China, India, Brazil
- Less common: Vietnam, Nigeria, Bangladesh, Egypt, Pakistan, Philippines
- Regions: EU, Eurozone, ASEAN, Nordic countries, Sub-Saharan Africa
- Aggregates: World, High income countries, Emerging markets

### 7. Time Period Variations
- Relative: "last 5 years", "past decade", "since 2010"
- Specific: "2018-2023", "Q2 2020", "January 2024"
- Events: "during COVID", "after the financial crisis", "pre-Brexit"
- Frequencies: "monthly", "quarterly", "annual"

```
Generate 100 queries covering:

ECONOMIC INDICATORS (40):
- GDP, GDP growth, GDP per capita (various countries, time periods)
- Unemployment rates (national, regional, demographic breakdowns)
- Inflation rates (CPI, PPI, core inflation)
- Interest rates (central bank rates, lending rates, yield curves)
- Trade balance, current account, foreign reserves
- Government debt, fiscal balance, tax revenue
- Population, labor force participation, productivity

TRADE FLOWS (20):
- Bilateral trade between specific countries
- Commodity-specific trade (oil, semiconductors, agricultural)
- Trade by HS code categories
- Import/export volumes and values

FINANCIAL DATA (20):
- Exchange rates (major and emerging currency pairs)
- Cryptocurrency prices (Bitcoin, Ethereum, market caps)
- Credit to GDP ratios, bank lending rates

MULTI-COUNTRY (10):
- G7 countries comparisons
- BRICS nations comparisons
- Regional groupings (EU, ASEAN)
- Note: Skip OECD-specific queries (use IMF/World Bank for similar aggregations)

SEQUENTIAL CONVERSATIONS (10 sequences):
- Follow-up questions that reference previous data
- Time period modifications ("now show me 2020-2023")
- Country/indicator changes ("same but for Germany")
```

### Phase 4: Parallel Testing

```
1. Launch parallel subagents for each category
2. Test against PRODUCTION (openecon.ai)
3. Use chrome-devtools MCP for frontend verification
4. Record all results in TEST_TRACKING.md
5. Categorize failures by:
   - Provider routing issues
   - Data accuracy issues
   - Frontend display issues
   - API/verification link issues
```

### Phase 5: Verification Checklist

For EACH query:
- [ ] Data values match authoritative sources
- [ ] Units correct (millions, billions, percent)
- [ ] Time periods match the request
- [ ] Correct country/region returned
- [ ] Provider routing appropriate
- [ ] Chart renders correctly
- [ ] API links functional

---

## Issue Resolution Framework

### When Error Found:

1. **Document the Issue**
   - Exact query that failed
   - Expected behavior vs actual behavior
   - Error messages (if any)
   - Provider involved

2. **Root Cause Analysis (ultrathink)**
   - Trace the data flow from query to response
   - Identify the FIRST point of failure
   - Determine if this is a PATTERN or isolated case
   - Find 5+ OTHER queries that would fail the same way
   - Identify the ARCHITECTURAL weakness

3. **Design General Fix (ultrathink)**
   - Solution must handle ALL similar cases
   - Solution must improve system intelligence
   - Consider edge cases and boundary conditions
   - Ensure backward compatibility
   - Add appropriate error handling

4. **Implement and Validate**
   - Run existing test suite to prevent regressions
   - Test the specific failing query
   - Test 5+ similar queries to verify generality
   - Test edge cases

5. **Verify in Production**
   - Test on production site (openecon.ai)
   - Verify with chrome-devtools
   - Confirm data accuracy against authoritative sources

---

## Anti-Patterns to Avoid

| DON'T | DO |
|-------|-----|
| See failing test → Add if-statement → Move on | Find pattern → Improve architecture → Many tests pass |
| Add special handling for specific queries | Improve general parsing/routing to cover ALL cases |
| Suppress errors, return empty data | Fix why error occurs, add proper fallbacks |
| Focus on getting 100% pass rate quickly | Focus on making the system genuinely robust |
| Fix one query without considering related | Fix underlying issue affecting whole category |
| Wait for tests to discover indicators | Proactively catalog all indicators upfront |
| Hard-code provider for specific query | Use data-driven routing based on indicator type |
| Route to OECD by default or as fallback | Only use OECD when user explicitly requests it |

---

## Tracking Requirements

### Maintain: `docs/testing/TEST_TRACKING.md`

```markdown
## Summary Statistics
| Provider | Tests | Passed | Failed | Accuracy |

## Failure Pattern Analysis
| Pattern | Affected Queries | Root Cause | Architectural Fix |

## Failed Tests (Pending)
| Query | Provider | Issue Type | Root Cause | Status |

## Resolved Issues
| Query | Issue | Framework Improvement | Verification |

## Architectural Improvements Made
- [ ] Description of systemic improvement
```

---

## Commit Workflow

```bash
# 1. Verify
pytest backend/tests/ -x -q
npm run build:frontend
curl http://localhost:3001/api/health

# 2. Commit with detailed message
git add -A
git commit -m "improve: [Component] Description

Root cause: Explanation of underlying systemic issue
Framework improvement: How this strengthens the architecture
Affected queries: Category of queries this helps
Verification: How generality was tested

Generated with Claude Code"

# 3. Push and verify production
git push origin main
curl https://openecon.ai/api/health
```

---

## Success Criteria

### Minimum Requirements:
- **95% accuracy** across all providers
- All provider routing working correctly
- All verification links functional
- No critical frontend errors
- All core tests passing

### Quality Standards:
- **Zero hardcoded fixes** - every fix must be general
- **Complete indicator index** - proactively discovered
- **All solutions documented** with architectural reasoning
- **Test coverage** for new code paths

---

## Execution Flow

```
PHASE 0: UNDERSTAND (ultrathink)
→ Read ALL code in detail
→ Map architecture and data flow
→ Document understanding before proceeding

PHASE 1: DISCOVER INDICATORS
→ Run provider catalog mining
→ Build unified indicator index
→ Create coverage matrix (Indicator × Country × Provider)
→ Generate synonym mappings

PHASE 2: TEST (parallel)
→ Subagents for each category
→ Test against production
→ Record results in tracking doc

PHASE 3: ANALYZE (ultrathink)
→ Group failures by pattern
→ Identify architectural weaknesses
→ Design general solutions

PHASE 4: IMPROVE (ultrathink for each)
→ Implement framework improvements
→ DOUBLE-CHECK after each change
→ Verify generality with 5+ queries
→ Commit with reasoning

PHASE 5: VERIFY
→ Re-run all failed tests
→ Test on production
→ Update indicator index

PHASE 6: DOCUMENT
→ Update tracking documents
→ Update indicator index
→ Document architectural improvements
```

---

## Important Reminders

### Framework Philosophy
1. **Tests improve framework, not pass rates**
2. **Every failure reveals systemic weakness**
3. **Quick fixes are debt—improvements are investments**
4. **If fix only helps one query, you haven't found the problem**

### Data Accuracy
5. **Wrong data is worse than no data**
6. **Verify against authoritative sources**
7. **Check values, units, dates, countries**
8. **Apply sanity checks to all data**

### Indicator Management
9. **Proactively discover indicators** - don't wait for failures
10. **Build coverage matrix** - know what's available where
11. **Maintain synonym mappings** - handle natural language
12. **Update index continuously**

### Future-Proofing
13. **Data-driven, not hard-coded**
14. **Extensible architecture**
15. **Graceful degradation**
16. **Document assumptions**

### Verification
17. **Double-check EVERY step**
18. **Stop if any check fails**
19. **Test against production**
20. **Use ultrathink for critical reasoning**

### Operations
21. **Use restart script:** `python3 scripts/restart_dev.py`
22. **Commit frequently** with architectural reasoning
23. **Provider flexibility** - don't force routing unless user specifies
24. **Read CLAUDE.md first** - essential project context before any work
25. **Skip OECD testing** - low priority, rate limited, use alternatives

---

## Key Deliverables

Every testing session produces:

1. **Test Results** - Documented in `docs/testing/TEST_TRACKING.md`
2. **Framework Improvements** - Code changes with architectural reasoning
3. **Indicator Index Updates** - New entries in `docs/indicators/INDICATOR_INDEX.md`
4. **Data Accuracy Log** - Verification against authoritative sources
5. **Coverage Matrix** - Indicator × Country × Provider mapping

---

**Remember:** A testing session is successful when the **framework has genuinely improved** and the **indicator knowledge base has grown**. Tests are diagnostic tools—the framework and indicator system are what we're building.
