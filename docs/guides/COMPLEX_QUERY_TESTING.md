# Complex Query Testing Guide

## Overview

This guide describes the comprehensive test suite for econ-data-mcp's complex multi-provider queries, Pro Mode execution, and edge case handling.

**Target**: 85%+ accuracy on complex queries across all categories

## Test Suite Structure

### 1. Complex Multi-Provider Queries (50 tests)
- **File**: `scripts/complex_queries.json` + `scripts/test_complex_queries.py`
- **Categories**: 6 distinct categories
- **Target**: 85%+ pass rate

#### Categories

| Category | Tests | Focus | Example Query |
|----------|-------|-------|----------------|
| **Multi-Provider** | 10 | Multiple data sources | "Compare US GDP (FRED) with China GDP (World Bank)" |
| **Calculated Metrics** | 8 | Derived indicators | "Trade balance as % of GDP" |
| **Regional Aggregations** | 7 | Country groupings | "Total exports for all ASEAN countries" |
| **Time Series Analysis** | 10 | Historical trends | "10-year trends with correlation" |
| **Cross-Domain** | 8 | Mixed data types | "Crypto vs traditional markets" |
| **Edge Cases** | 7 | Unusual requests | "Small economy data", "Defunct countries" |

### 2. Pro Mode Complex Analysis (15 tests)
- **File**: `scripts/test_promode_complex.py`
- **Focus**: AI-generated code execution
- **Target**: 85%+ code generation + execution

#### Categories

| Category | Tests | Focus | Example |
|----------|-------|-------|---------|
| **Aggregation** | 2 | Multi-country aggregation | "GDP for all EU countries" |
| **Calculation** | 1 | Mathematical operations | "Per capita GDP ranking" |
| **Correlation** | 1 | Relationship analysis | "Oil price vs exchange rate" |
| **Time Series** | 1 | Trend decomposition | "Unemployment decomposition" |
| **Forecast** | 1 | Predictive modeling | "GDP growth forecast" |
| **Multivariate** | 1 | Multiple comparisons | "Housing prices vs rates" |
| **Statistical** | 2 | Regression/hypothesis | "Inflation-unemployment" |
| **Visualization** | 2 | Chart generation | "Heatmaps, correlations" |
| **Other Analysis** | 4 | Clustering, optimization | "Country clustering" |

### 3. Edge Cases & Error Handling (33 tests)
- **File**: `scripts/test_edge_cases.py`
- **Focus**: System robustness
- **Target**: 80%+ appropriate handling

#### Categories

| Category | Tests | Focus | Example |
|----------|-------|-------|---------|
| **Data Validation** | 8 | Invalid inputs | Currency codes, future dates |
| **Ambiguity** | 6 | Unclear queries | "bank data", "growth rate" |
| **Provider Mismatch** | 3 | Wrong provider routing | "Census data from World Bank" |
| **Inconsistency** | 4 | Conflicting requirements | "Annual data with monthly frequency" |
| **Performance** | 3 | Large datasets | "36,500 daily data points" |
| **Boundary** | 4 | Extreme values | "1 day", "world total" |
| **Missing Data** | 4 | Unavailable data | "Fictional countries" |
| **Format** | 3 | Export formats | "Invalid formats" |
| **Security** | 2 | Injection attempts | "SQL injection", "Path traversal" |

## Running Tests

### Quick Start

Run all test suites:

```bash
# From repository root
bash scripts/run_all_complex_tests.sh
```

This script:
1. Activates Python virtual environment
2. Checks API health
3. Runs all three test suites sequentially
4. Generates unified report

### Individual Test Suites

#### Complex Queries

```bash
python3 scripts/test_complex_queries.py \
    --api-base http://localhost:3001/api \
    --timeout 60 \
    --output complex_results.json
```

**Options:**
- `--api-base`: API endpoint (default: http://localhost:3001/api)
- `--timeout`: Request timeout in seconds (default: 60)
- `--output`: Output report file (default: complex_query_results.json)
- `--test-file`: Test cases JSON file (default: scripts/complex_queries.json)

#### Pro Mode Tests

```bash
python3 scripts/test_promode_complex.py \
    --api-base http://localhost:3001/api \
    --timeout 120 \
    --output promode_results.json
```

**Options:**
- `--api-base`: API endpoint (default: http://localhost:3001/api)
- `--timeout`: Request timeout in seconds (default: 120)
- `--output`: Output report file (default: promode_test_results.json)

#### Edge Cases

```bash
python3 scripts/test_edge_cases.py \
    --api-base http://localhost:3001/api \
    --timeout 60 \
    --output edge_results.json
```

**Options:**
- `--api-base`: API endpoint (default: http://localhost:3001/api)
- `--timeout`: Request timeout in seconds (default: 60)
- `--output`: Output report file (default: edge_case_results.json)

## Understanding Results

### Report Structure

Each test generates a JSON report with:

```json
{
  "metadata": {
    "timestamp": "2025-11-20T...",
    "api_base": "http://localhost:3001/api",
    "total_tests": 50
  },
  "summary": {
    "total_tests": 50,
    "passed": 43,
    "failed": 7,
    "pass_rate_percent": "86.0%"
  },
  "by_category": [
    {
      "category": "multi_provider",
      "total": 10,
      "passed": 9,
      "pass_rate": "90.0%",
      "avg_response_time_ms": "3245"
    }
  ],
  "failures": [
    {
      "test_id": "MP001",
      "query": "Compare US GDP...",
      "error": "...",
      "notes": "..."
    }
  ],
  "detailed_results": [...]
}
```

### Key Metrics

#### Pass Rate
- **Target**: 85%+ overall
- **By Category**: 70%+ minimum for any category
- **Calculation**: (Passed / Total) × 100

#### Response Time
- **Average**: Expected 1-10 seconds for most queries
- **Multi-provider**: 5-15 seconds
- **Pro Mode**: 10-60 seconds
- **Median**: More stable than average

#### Data Quality
- **Data returned**: Should have data for queries expecting data
- **Minimum data points**: Varies by test
- **Schema validity**: All responses must follow contract

### Interpreting Failures

**Pass Criteria:**
1. ✅ Valid response schema
2. ✅ No unexpected errors
3. ✅ Appropriate behavior for query type
4. ✅ Data validation passed

**Common Failure Reasons:**
- Timeout (API overload or slow query)
- API error (provider unavailable)
- Wrong provider detected
- Insufficient data returned
- Schema validation failed

## Test Case Categories Explained

### Multi-Provider Queries

**What They Test**: System ability to fetch from multiple APIs in single query

**Example**: "Compare US GDP (FRED) with China GDP (World Bank)"

**What Should Happen**:
1. ✅ Parse query to identify multiple providers
2. ✅ Fetch from FRED for US data
3. ✅ Fetch from World Bank for China data
4. ✅ Normalize to common format
5. ✅ Return combined result

**Success Criteria**:
- Both data series returned
- Correct providers identified
- Data points >= 5
- Response time < 15s

### Calculated Metrics

**What They Test**: System ability to derive new metrics from raw data

**Example**: "Trade balance as percentage of GDP"

**What Should Happen**:
1. ✅ Parse intent to identify calculation
2. ✅ Fetch exports and imports (for trade balance)
3. ✅ Fetch GDP
4. ✅ Calculate: (exports - imports) / GDP × 100
5. ✅ Return calculated series

**Success Criteria**:
- Calculation performed
- Units correct (percent)
- At least 3 data points
- Math validated

### Regional Aggregations

**What They Test**: Ability to aggregate across countries in a region

**Example**: "Total exports for all ASEAN countries"

**What Should Happen**:
1. ✅ Identify ASEAN member list
2. ✅ Fetch data for each country
3. ✅ Aggregate/sum across countries
4. ✅ Return time series of totals

**Success Criteria**:
- All/most ASEAN countries included
- Aggregation performed
- Time series returned

### Time Series Analysis

**What They Test**: Handling of long historical data and trends

**Example**: "10-year trends with correlation analysis"

**What Should Happen**:
1. ✅ Fetch 10 years of historical data
2. ✅ Analyze trends
3. ✅ Calculate correlations if multi-series
4. ✅ Return complete time series

**Success Criteria**:
- 40+ quarterly data points (10 years × 4)
- Trends visible in data
- Correlation calculated if applicable

### Cross-Domain Queries

**What They Test**: Combining data from unrelated domains

**Example**: "Crypto market cap vs traditional indices"

**What Should Happen**:
1. ✅ Identify crypto and traditional market data
2. ✅ Fetch from CoinGecko (crypto)
3. ✅ Fetch from FRED (traditional)
4. ✅ Normalize/align time series
5. ✅ Enable comparison

**Success Criteria**:
- Both domains represented
- Time periods aligned
- Unit conversions handled

### Edge Cases

**What They Test**: System robustness and error handling

**Example**: "Economic data for fictional country"

**What Should Happen**:
1. ✅ Detect invalid input
2. ✅ Return error or request clarification
3. ✅ Do NOT crash or return garbage data

**Success Criteria**:
- Graceful error handling
- Helpful error messages
- No system crashes

## Pro Mode Testing Details

### What Pro Mode Does

Pro Mode uses AI (Grok LLM) to generate and execute Python code for advanced analysis.

**Flow**:
1. User asks complex question
2. LLM generates Python code
3. Code executed in sandbox
4. Results returned with visualizations

### Testing Pro Mode

Each Pro Mode test validates:

1. **Code Generation**: Did LLM create code?
2. **Code Execution**: Did code run without errors?
3. **File Generation**: Were visualizations created?
4. **Output Quality**: Is output reasonable?

### Example Pro Mode Query

**Query**: "Create a bar chart showing GDP for all EU countries"

**Expected Process**:
```python
# 1. Fetch EU country list
# 2. Fetch GDP for each country from World Bank
# 3. Sort by GDP
# 4. Create matplotlib bar chart
# 5. Save as PNG
```

**Success Criteria**:
- ✅ Code generated
- ✅ Code executed without errors
- ✅ PNG file created
- ✅ Chart shows all countries
- ✅ Response < 60 seconds

## Performance Benchmarks

### Target Response Times

| Query Type | Target | Acceptable | Warning |
|-----------|--------|-----------|---------|
| Single provider | < 5s | < 10s | > 10s |
| Multi-provider (2) | < 10s | < 20s | > 20s |
| Multi-provider (3+) | < 15s | < 30s | > 30s |
| Pro Mode simple | < 30s | < 60s | > 60s |
| Pro Mode complex | < 60s | < 120s | > 120s |
| Regional agg (10+ countries) | < 20s | < 40s | > 40s |

### Performance Optimization Tips

If tests are slow:

1. **Check API health**: `curl http://localhost:3001/api/health`
2. **Check network**: High latency to providers?
3. **Check backend logs**: `tail -f /tmp/backend-production.log`
4. **Profile slow queries**: Add logging to identify bottleneck
5. **Consider caching**: Results cached 5 minutes by default

## Fixing Failing Tests

### Step 1: Identify Root Cause

```bash
# Get error details from report
cat complex_results.json | python -m json.tool | grep -A5 "TESTID"
```

### Step 2: Classify Failure

| Symptom | Likely Cause | Fix |
|---------|-------------|-----|
| Timeout | Slow query or API overload | Increase timeout or optimize query |
| No data | Provider issue or indicator not found | Check provider, verify indicator |
| Wrong provider | LLM parsing error | Update system prompt or add example |
| Schema error | API contract change | Update validation logic |
| Clarification | Ambiguous query | Rephrase or add context |

### Step 3: Verify Fix

1. Manually test the failing query via API
2. Check backend logs for errors
3. Run individual test: `python3 scripts/test_complex_queries.py --api-base ...`
4. Confirm fix in full test suite

### Common Issues & Solutions

#### Issue: All Multi-Provider Tests Fail

**Cause**: API not returning multiple series

**Solution**:
```python
# In backend/services/query.py
# Ensure _fetch_data() returns combined results
# Check that providers are called sequentially
```

#### Issue: Pro Mode Tests Timeout

**Cause**: Code generation or execution too slow

**Solution**:
1. Increase timeout: `--timeout 180`
2. Check Grok LLM availability
3. Monitor backend memory usage

#### Issue: Edge Case Tests Fail

**Cause**: System not gracefully handling errors

**Solution**:
1. Add input validation
2. Improve error messages
3. Add clarification logic

## Data Validation

### Numeric Value Checks

All numeric data should be validated:

```python
# Check ranges are reasonable
assert 0 <= inflation <= 100, "Inflation should be 0-100%"
assert gdp > 0, "GDP should be positive"
assert unemployment <= 100, "Unemployment can't exceed 100%"
```

### Unit Validation

Cross-reference units with authoritative sources:

| Indicator | Expected Unit | Example Value Range |
|-----------|----------------|-------------------|
| GDP | USD | $1T - $20T (for major economies) |
| Unemployment | Percent | 0% - 30% |
| Inflation | Percent | -5% to +50% |
| Exchange Rate | Ratio | 0.5 - 2.0 (major pairs) |
| Trade Volume | USD | $100M - $1T |

### Time Series Validation

- ✅ Dates are sequential
- ✅ No duplicate dates
- ✅ Expected frequency (monthly, quarterly, annual)
- ✅ No unexplained gaps

## Continuous Testing

### Schedule

- **Daily**: Edge case tests (quick smoke test)
- **Weekly**: Full suite after code changes
- **Before Release**: Full suite + manual spot checks

### Automation

```bash
# Add to crontab for nightly testing
0 2 * * * cd /home/hanlulong/econ-data-mcp && bash scripts/run_all_complex_tests.sh
```

### Trend Tracking

Track pass rates over time:

```bash
# Extract pass rate from latest report
jq '.summary.pass_rate_percent' test_results_latest/complex_queries.json
```

## Advanced Topics

### Custom Test Cases

Add new test case to `scripts/complex_queries.json`:

```json
{
  "id": "MP011",
  "category": "multi_provider",
  "query": "Your custom query here",
  "description": "Description",
  "expected_providers": ["FRED", "WORLDBANK"],
  "acceptance_criteria": {
    "has_data": true,
    "min_data_points": 5
  }
}
```

### Debugging Failed Queries

1. **Enable debug logging**:
```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

2. **Trace query flow**:
- Check LLM parsing: `/api/query` response intent
- Check provider selection: Backend logs
- Check data fetch: Provider API calls

3. **Add custom validation**:
```python
# In test_complex_queries.py validate_response_schema()
# Add custom checks for specific queries
```

### Performance Profiling

Profile slow queries:

```bash
# In backend/main.py, add timing instrumentation
import time
start = time.time()
# ... query execution ...
print(f"Query took {time.time() - start:.2f}s")
```

## Test Coverage Matrix

| Feature | Multi-Provider | Pro Mode | Edge Cases |
|---------|---|---|---|
| FRED | ✅ Yes | ✅ Yes | ✅ Yes |
| World Bank | ✅ Yes | ✅ Yes | ✅ Yes |
| Comtrade | ✅ Yes | ✅ Yes | ✅ Yes |
| Statistics Canada | ✅ Yes | ✅ Yes | ✅ Yes |
| IMF | ✅ Yes | ✅ Yes | ✅ Yes |
| BIS | ✅ Yes | ✅ Yes | ✅ Yes |
| Eurostat | ✅ Yes | ✅ Yes | ✅ Yes |
| OECD | ✅ Yes | ✅ Yes | ✅ Yes |
| ExchangeRate | ✅ Yes | ✅ Yes | ✅ Yes |
| CoinGecko | ✅ Yes | ✅ Yes | ✅ Yes |
| Dune | ✅ Yes | ✅ Yes | ✅ Yes |

## Conclusion

The complex query test suite provides comprehensive validation of econ-data-mcp's:
- Multi-provider integration
- Data normalization and combination
- Calculated metrics and aggregations
- Pro Mode AI code execution
- Error handling and robustness

**Success Target**: 85%+ pass rate across all categories

For issues or questions, refer to CLAUDE.md's debugging section or contact the development team.
