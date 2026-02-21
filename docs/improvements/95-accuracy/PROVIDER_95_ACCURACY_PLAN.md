# Provider 95% Accuracy Plan

**Goal**: Achieve 95% accuracy for IMF, Comtrade, Eurostat, and BIS providers
**Date Started**: 2025-11-21
**Current Status**: Analysis Phase

---

## Current State (from MASTER.md)

| Provider | Current Accuracy | Target | Gap | Priority |
|----------|------------------|--------|-----|----------|
| **IMF** | 80% | 95% | +15pp | HIGH |
| **UN Comtrade** | 80% | 95% | +15pp | HIGH |
| **Eurostat** | 67% | 95% | +28pp | CRITICAL |
| **BIS** | 67% | 95% | +28pp | CRITICAL |

---

## IMF (80% → 95%) - Target: +15 percentage points

### Current Issues
1. Missing fiscal indicators from GFS dataset
2. Pagination not implemented for large queries
3. Country groups not supported (G7, G20, BRICS, ASEAN)
4. Timeout issues with large multi-country requests

### Implementation Plan

#### Phase 1: Add GFS Indicators (Expected: +5pp)
**File**: `backend/providers/imf.py`

**New Indicators to Add**:
```python
# Government Finance Statistics (GFS)
"GOVERNMENT_REVENUE_TO_GDP": "GGR_NGDP",
"TAX_REVENUE": "GGTAX_NGDP",
"GOVERNMENT_EXPENDITURE_TO_GDP": "GGEXP_NGDP",
"FISCAL_DEFICIT_TO_GDP": "GGXCNL_NGDP",  # Already exists
"PRIMARY_BALANCE_TO_GDP": "GGXONLB_NGDP",
"INTEREST_PAYMENTS": "GGXINT_NGDP",
```

**Test Queries** (5):
1. "Show me government revenue as % of GDP for France"
2. "Get tax revenue for Germany from 2020 to 2024"
3. "Show primary balance for Italy"
4. "What are interest payments on government debt for Spain?"
5. "Compare government expenditure across G7 countries"

#### Phase 2: Implement Pagination (Expected: +3pp)
**File**: `backend/providers/imf.py`

**Changes**:
- Add `_fetch_with_pagination()` method
- Chunk large country lists into batches of 20
- Aggregate results from multiple API calls

**Test Queries** (3):
1. "Get GDP growth for all EU countries"
2. "Show unemployment rates for all G20 countries"
3. "Compare inflation across all OECD member countries"

#### Phase 3: Add Country Groups (Expected: +5pp)
**File**: `backend/providers/imf.py`

**New Constants**:
```python
COUNTRY_GROUPS = {
    "G7": ["USA", "CAN", "GBR", "DEU", "FRA", "ITA", "JPN"],
    "G20": [...],  # 20 countries
    "BRICS": ["BRA", "RUS", "IND", "CHN", "ZAF"],
    "ASEAN": ["IDN", "THA", "MYS", "PHL", "VNM", "SGP", "MMR", "KHM", "LAO", "BRN"],
    "EU27": [...],  # 27 countries
    "EUROZONE": [...],  # 19 countries
}
```

**Test Queries** (5):
1. "Show GDP growth for G7 countries"
2. "Compare inflation in BRICS nations"
3. "Get unemployment rates for ASEAN countries"
4. "Show debt-to-GDP for Eurozone countries"
5. "Compare fiscal deficits across G20"

#### Phase 4: Fix Timeout Issues (Expected: +2pp)
**File**: `backend/providers/imf.py`

**Changes**:
- Increase timeout from 60s to 120s for batch queries
- Implement request chunking (20 countries per request)
- Add better error handling for partial failures

**Test Queries** (2):
1. "Get inflation data for all countries in the world"
2. "Compare GDP growth for all European countries from 2010 to 2024"

### Total Expected Improvement: +15pp (80% → 95%)

---

## UN Comtrade (80% → 95%) - Target: +15 percentage points

### Current Issues
1. No HS code search/mapping beyond hardcoded list
2. No commodity name-to-code converter
3. Bilateral trade direction logic inconsistent
4. Missing trade balance calculation for specific commodities

### Implementation Plan

#### Phase 1: HS Code Search System (Expected: +6pp)
**Files**:
- `backend/providers/comtrade.py`
- `backend/providers/comtrade_metadata.py` (already has HS_CODE_MAPPINGS)

**New Method**:
```python
def search_hs_code(self, commodity_name: str) -> Optional[str]:
    """Search for HS code by fuzzy matching commodity name.

    Uses:
    1. Exact match in HS_CODE_MAPPINGS
    2. Partial match (contains)
    3. Synonym matching
    """
```

**Test Queries** (6):
1. "Show me imports of electric vehicles to USA"
2. "Get exports of renewable energy equipment from China"
3. "Show pharmaceutical imports for Canada"
4. "Get exports of agricultural machinery from Germany"
5. "Show clothing imports to UK"
6. "Get electronics exports from Japan to US"

#### Phase 2: Commodity Name Converter (Expected: +3pp)
**File**: `backend/providers/comtrade.py`

**Implementation**:
- Add `_normalize_commodity_name()` method
- Use fuzzy string matching (fuzzywuzzy library)
- Cache successful matches

**Test Queries** (3):
1. "Show me trade in vaccines"
2. "Get imports of solar panels"
3. "Show exports of lithium batteries"

#### Phase 3: Fix Trade Direction Logic (Expected: +4pp)
**File**: `backend/providers/comtrade.py`

**Issues**:
- "US exports to China" vs "China imports from US" should return same data
- Current logic doesn't handle bilateral queries consistently

**Changes**:
- Clarify reporter/partner roles in `fetch_trade_data()`
- Add validation for bilateral queries
- Document trade flow direction clearly

**Test Queries** (4):
1. "Show US exports to China"
2. "Show China imports from US" (should match #1)
3. "Get bilateral trade between US and Mexico"
4. "Show trade balance of US with EU"

#### Phase 4: Commodity-Specific Trade Balance (Expected: +2pp)
**File**: `backend/providers/comtrade.py`

**Changes**:
- Extend `fetch_trade_balance()` to support commodity parameter
- Add validation for commodity codes

**Test Queries** (2):
1. "What is US trade balance in oil?"
2. "Show China's trade balance in electronics"

### Total Expected Improvement: +15pp (80% → 95%)

---

## Eurostat (67% → 95%) - Target: +28 percentage points

### Current Issues
1. **CRITICAL**: Only 22 hardcoded indicators (insufficient coverage)
2. No intelligent frequency selection (queries fail due to wrong frequency)
3. Missing EU aggregations (EU27, Eurozone)
4. Dimension combination errors in SDMX queries

### Implementation Plan

#### Phase 1: Expand Indicator Mappings (Expected: +15pp)
**File**: `backend/providers/eurostat.py`

**Strategy**: Add top 500 most-queried Eurostat datasets

**Categories to Add**:
- National Accounts (50 indicators)
- Labor Market (40 indicators)
- Prices & Inflation (30 indicators)
- International Trade (40 indicators)
- Population & Demographics (30 indicators)
- Government Finance (30 indicators)
- Industry & Production (40 indicators)
- Retail & Services (30 indicators)
- Agriculture (20 indicators)
- Energy (30 indicators)
- Environment (20 indicators)
- Science & Technology (20 indicators)
- Regional Statistics (30 indicators)
- Health (20 indicators)
- Education (20 indicators)

**Total**: 500 indicators (from 22)

**Research Sources**:
- Eurostat API catalog: https://ec.europa.eu/eurostat/api/dissemination/catalogue/
- Most downloaded datasets list
- User query analysis from production logs

**Test Queries** (15):
1. "Show GDP for Germany"
2. "Get unemployment rate for France"
3. "Show inflation in Italy"
4. "Get exports from Spain"
5. "Show population of Poland"
6. "Get government debt for Greece"
7. "Show industrial production in Netherlands"
8. "Get retail sales for Belgium"
9. "Show agricultural output for Ireland"
10. "Get energy consumption for Sweden"
11. "Show CO2 emissions for Denmark"
12. "Get R&D spending for Finland"
13. "Show regional GDP for Germany"
14. "Get life expectancy for Portugal"
15. "Show education spending for Austria"

#### Phase 2: Intelligent Frequency Selection (Expected: +5pp)
**File**: `backend/providers/eurostat.py`

**Implementation**:
```python
def _detect_available_frequencies(self, dataset_code: str) -> List[str]:
    """Query dataset metadata to determine available frequencies.

    Returns: ["A", "Q", "M"] or subset
    """

def _select_best_frequency(self, dataset_code: str, preferred: str) -> str:
    """Select best available frequency for dataset.

    Fallback order: M → Q → A
    """
```

**Test Queries** (5):
1. "Show monthly unemployment for France"
2. "Get quarterly GDP for Germany"
3. "Show annual inflation for Italy"
4. "Get monthly retail sales for Spain"
5. "Show quarterly government debt for Greece"

#### Phase 3: EU Aggregations (Expected: +5pp)
**File**: `backend/providers/eurostat.py`

**New Constants**:
```python
EU_AGGREGATIONS = {
    "EU": "EU27_2020",
    "EU27": "EU27_2020",
    "EUROPEAN_UNION": "EU27_2020",
    "EUROZONE": "EA19",
    "EURO_AREA": "EA19",
}
```

**Test Queries** (5):
1. "Show EU27 GDP growth"
2. "Get Eurozone inflation rate"
3. "Compare EU vs US unemployment"
4. "Show Euro area trade balance"
5. "Get European Union government debt"

#### Phase 4: Fix Dimension Errors (Expected: +3pp)
**File**: `backend/providers/eurostat.py`

**Changes**:
- Validate dimension combinations before querying
- Add fallback dimension values from dataset metadata
- Better error messages for invalid combinations

**Test Queries** (3):
1. "Show HICP for all EU countries"
2. "Get labor costs for manufacturing sector"
3. "Show house prices for residential properties"

### Total Expected Improvement: +28pp (67% → 95%)

---

## BIS (67% → 95%) - Target: +28 percentage points

### Current Issues
1. **CRITICAL**: Weak `_select_best_series()` algorithm (wrong series selected)
2. Only 13-15 hardcoded indicators (insufficient coverage)
3. Country-specific data issues (some countries return no data)
4. No frequency converter (only supports native frequencies)

### Implementation Plan

#### Phase 1: Improve Series Selection (Expected: +10pp)
**File**: `backend/providers/bis.py`

**Current Algorithm Issues**:
- Preferences are too rigid
- Doesn't handle missing preferred values gracefully
- No scoring for data completeness

**New Algorithm**:
```python
def _select_best_series(self, series_data, series_dimensions, indicator_code):
    """Enhanced series selection with multi-criteria scoring.

    Scoring criteria:
    1. Data completeness (30%)
    2. Recency of data (20%)
    3. Preference match (30%)
    4. Geographic coverage (20%)
    """
```

**Test Queries** (10):
1. "Show BIS policy rates for US"
2. "Get credit data for Japan"
3. "Show property prices for UK"
4. "Get exchange rates for Canada"
5. "Show CPI for Germany"
6. "Get credit gap for France"
7. "Show debt service ratio for Australia"
8. "Get long-term interest rates for Italy"
9. "Show credit-to-GDP for Spain"
10. "Get property prices for China"

#### Phase 2: Expand Indicator Mappings (Expected: +8pp)
**File**: `backend/providers/bis.py`

**Strategy**: Add 50 more BIS indicators with verification

**Categories**:
- Central bank policy (10 indicators)
- Credit aggregates (10 indicators)
- Property prices (10 indicators)
- Exchange rates (10 indicators)
- Interest rates (10 indicators)
- Financial stability (10 indicators)

**Verification Process**:
1. Test each indicator manually
2. Verify data availability for major countries
3. Document working dimension combinations
4. Only add if passes verification

**Test Queries** (8):
1. "Show BIS global liquidity"
2. "Get international banking statistics"
3. "Show derivatives market data"
4. "Get debt securities issuance"
5. "Show credit default swap spreads"
6. "Get long-term government bond yields"
7. "Show cross-border banking flows"
8. "Get syndicated loan volumes"

#### Phase 3: Fix Country Issues (Expected: +5pp)
**File**: `backend/providers/bis.py`

**Implementation**:
```python
def _get_countries_with_data(self, indicator_code: str) -> List[str]:
    """Query BIS metadata to get list of countries with data for indicator."""

def _validate_country(self, indicator_code: str, country_code: str) -> bool:
    """Check if country has data for indicator before querying."""
```

**Test Queries** (5):
1. "Show BIS data for emerging markets"
2. "Get policy rates for all G20 countries"
3. "Show credit data for Asian countries"
4. "Get property prices for Latin America"
5. "Show exchange rates for European countries"

#### Phase 4: Frequency Converter (Expected: +5pp)
**File**: `backend/providers/bis.py`

**Implementation**:
```python
def _convert_frequency(self, data_points, from_freq, to_freq):
    """Convert data frequency (Q→A, M→Q, M→A).

    Methods:
    - Quarterly to Annual: Sum or average
    - Monthly to Quarterly: Sum or average
    - Monthly to Annual: Sum or average
    """
```

**Test Queries** (5):
1. "Show annual credit growth" (from quarterly)
2. "Get quarterly policy rates" (from monthly)
3. "Show annual property price changes" (from quarterly)
4. "Get quarterly inflation" (from monthly)
5. "Show annual exchange rate movements" (from monthly)

### Total Expected Improvement: +28pp (67% → 95%)

---

## Testing Strategy

### Test File Structure
**File**: `scripts/test_all_providers_95.py`

**Structure**:
```python
class ProviderAccuracyTest:
    def __init__(self):
        self.test_cases = {
            "IMF": [...],  # 25 queries
            "COMTRADE": [...],  # 25 queries
            "EUROSTAT": [...],  # 25 queries
            "BIS": [...],  # 25 queries
        }

    def test_provider(self, provider: str) -> TestResult:
        """Test all queries for a provider."""

    def validate_data_quality(self, response) -> bool:
        """Validate:
        - Non-null values
        - Reasonable value ranges
        - Sufficient data points
        - Correct units
        """
```

### Success Criteria
For each query:
- ✅ **Pass**: Returns correct data with valid values
- ⚠️ **Clarification**: Requests valid clarification (acceptable)
- ❌ **Fail**: Error, wrong data, or unreasonable values

### Tracking Document
**File**: `docs/PROVIDER_95_TRACKING.md`

**Format**:
```markdown
## IMF - Query Tracking

| # | Query | Status | Error | Root Cause | Solution | Verified |
|---|-------|--------|-------|------------|----------|----------|
| 1 | ... | ❌ FAIL | 404 | Missing indicator | Add GFS mapping | ⏳ |
| 2 | ... | ✅ PASS | - | - | - | ✅ |
```

---

## Implementation Timeline

### Week 1: Analysis & Setup
- [x] Create this plan document
- [ ] Create test file with 100 queries (25 per provider)
- [ ] Create tracking document
- [ ] Run initial baseline tests

### Week 2: IMF Improvements
- [ ] Phase 1: Add GFS indicators
- [ ] Phase 2: Implement pagination
- [ ] Phase 3: Add country groups
- [ ] Phase 4: Fix timeouts
- [ ] Test and verify 95% accuracy

### Week 3: Comtrade Improvements
- [ ] Phase 1: HS code search
- [ ] Phase 2: Commodity converter
- [ ] Phase 3: Fix trade direction
- [ ] Phase 4: Commodity trade balance
- [ ] Test and verify 95% accuracy

### Week 4: Eurostat Improvements
- [ ] Phase 1: Expand to 500 indicators
- [ ] Phase 2: Intelligent frequency selection
- [ ] Phase 3: EU aggregations
- [ ] Phase 4: Fix dimension errors
- [ ] Test and verify 95% accuracy

### Week 5: BIS Improvements
- [ ] Phase 1: Improve series selection
- [ ] Phase 2: Expand to 50+ indicators
- [ ] Phase 3: Fix country issues
- [ ] Phase 4: Frequency converter
- [ ] Test and verify 95% accuracy

### Week 6: Final Verification
- [ ] Run comprehensive 100-query test suite
- [ ] Validate all 4 providers at 95%+
- [ ] Deploy to production
- [ ] Update documentation

---

## Success Metrics

### Target Accuracy by Provider
- IMF: 95%+ (24/25 queries)
- Comtrade: 95%+ (24/25 queries)
- Eurostat: 95%+ (24/25 queries)
- BIS: 95%+ (24/25 queries)

### Overall Goal
- **100 queries total**
- **95+ queries passing** (95% overall accuracy)
- **Maximum 5 failures** across all providers

---

## Notes

### Key Principles
1. **No hardcoded solutions for specific test queries**
   - All fixes must be general and work for any similar query
2. **Data quality validation required**
   - Not just HTTP 200, but correct values
3. **Document all failures**
   - Track in `PROVIDER_95_TRACKING.md`
   - Never remove until verified fixed
4. **Research before implementing**
   - Check official API docs
   - Test manually before coding
5. **Verify all fixes**
   - Test original failing query
   - Test similar queries
   - Update tracking document

### Resources
- IMF DataMapper: https://www.imf.org/external/datamapper/api/help
- UN Comtrade API: https://comtradeplus.un.org/
- Eurostat API: https://ec.europa.eu/eurostat/web/main/data/web-services
- BIS Statistics: https://stats.bis.org/api/v1/

---

**Last Updated**: 2025-11-21
**Status**: Analysis Phase Complete - Ready for Implementation
