# Complex Query Test Suite - Complete Documentation

## Overview

A comprehensive test framework for validating econ-data-mcp's ability to handle advanced multi-provider queries, calculated metrics, regional aggregations, and edge cases.

**Target**: 85%+ overall pass rate across all test categories

## What Was Created

### 1. Test Case Library
- **File**: `scripts/complex_queries.json`
- **Size**: 50 comprehensive test cases
- **Format**: JSON with detailed specifications

### 2. Test Runners

#### Complex Multi-Provider Queries
- **File**: `scripts/test_complex_queries.py`
- **Tests**: 50 queries across 6 categories
- **Execution Time**: ~30-50 minutes
- **Output**: `complex_query_results.json`

#### Pro Mode AI Code Execution
- **File**: `scripts/test_promode_complex.py`
- **Tests**: 15 advanced analysis queries
- **Execution Time**: ~20-30 minutes
- **Output**: `promode_test_results.json`

#### Edge Cases & Error Handling
- **File**: `scripts/test_edge_cases.py`
- **Tests**: 33 edge case scenarios
- **Execution Time**: ~10-20 minutes
- **Output**: `edge_case_results.json`

### 3. Test Execution Framework
- **File**: `scripts/run_all_complex_tests.sh`
- **Function**: Orchestrates all tests, generates unified report
- **Time**: ~2 hours total

### 4. Analysis Tools
- **File**: `scripts/analyze_test_results.py`
- **Function**: Analyzes results, generates comparisons and HTML reports
- **Formats**: JSON, CSV, HTML

### 5. Documentation
- **File**: `docs/guides/COMPLEX_QUERY_TESTING.md`
- **Content**: Comprehensive testing guide with examples

## Quick Start

### Step 1: Start Backend
```bash
npm run dev:backend
# Or if already running, skip this
```

### Step 2: Run All Tests
```bash
bash scripts/run_all_complex_tests.sh
```

This runs all tests and creates a timestamped results directory like `test_results_20251120_231500/`.

### Step 3: View Results
```bash
# Show summary
cat test_results_latest/complex_queries.json | python -m json.tool | head -50

# Generate HTML report
python3 scripts/analyze_test_results.py \
    --results-dir test_results_latest \
    --export-html test_report.html
```

## Test Categories

### Multi-Provider Queries (10 tests)
Tests system ability to fetch from multiple APIs in a single query.

**Examples:**
- Compare US GDP (FRED) with China GDP (World Bank)
- Show inflation for US, EU, and Japan from different providers
- Exchange rates vs trade volumes

**Validation**: Data returned from multiple providers, properly combined

---

### Calculated Metrics (8 tests)
Tests system ability to derive new metrics from raw data.

**Examples:**
- Trade balance as percentage of GDP
- Per capita calculations
- Year-over-year growth rates
- Real vs nominal spreads

**Validation**: Calculations performed correctly, units validated

---

### Regional Aggregations (7 tests)
Tests system ability to aggregate across countries in a region.

**Examples:**
- Total exports for all ASEAN countries
- GDP for all EU member states
- G7 unemployment rate comparison
- Sub-Saharan Africa inflation

**Validation**: All/most countries included, aggregation performed

---

### Time Series Analysis (10 tests)
Tests handling of long historical data and trend analysis.

**Examples:**
- 10-year US GDP trends
- Correlation between oil prices and CAD/USD
- COVID-19 impact analysis
- Long-term Japan GDP (Lost Decade)

**Validation**: 40+ data points, trends visible, analysis complete

---

### Cross-Domain Queries (8 tests)
Tests combining data from unrelated domains.

**Examples:**
- Crypto market cap vs traditional indices
- Housing prices vs interest rates
- Supply chain impact analysis
- Labor market dynamics

**Validation**: Both domains represented, time alignment handled

---

### Edge Cases (7 tests)
Tests system robustness and error handling.

**Examples:**
- Small economy data (Liechtenstein)
- Historical defunct countries (East Germany, Yugoslavia)
- Large-scale data retrieval (1000 coins)
- Conflicting requirements

**Validation**: Graceful degradation, appropriate error messages

---

### Pro Mode AI Analysis (15 tests)
Tests AI-generated Python code execution.

**Categories:**
- Aggregation & visualization (2 tests)
- Statistical analysis (2 tests)
- Time series forecasting (1 test)
- Clustering & optimization (2 tests)
- Report generation (1 test)
- Plus 7 more specialized analysis types

**Validation**: Code generation, execution, file creation, output quality

---

### Error Handling (33 tests total across categories)
Tests appropriate error handling for:
- Data validation (invalid inputs)
- Ambiguous queries
- Provider mismatches
- Inconsistent requirements
- Performance limits
- Missing data
- Format errors
- Security attempts

**Validation**: Graceful errors, helpful messages, no crashes

## File Structure

```
/home/hanlulong/econ-data-mcp/
├── scripts/
│   ├── complex_queries.json                 # 50 test cases
│   ├── test_complex_queries.py             # Main test runner (50 tests)
│   ├── test_promode_complex.py             # Pro Mode tests (15 tests)
│   ├── test_edge_cases.py                  # Edge case tests (33 tests)
│   ├── run_all_complex_tests.sh            # Unified test orchestrator
│   └── analyze_test_results.py             # Result analysis tool
├── docs/guides/
│   └── COMPLEX_QUERY_TESTING.md            # Detailed testing guide
└── test_results_latest/                    # Latest test results
    ├── complex_queries.json
    ├── promode_complex.json
    └── edge_cases.json
```

## Test Execution Workflow

### 1. Sequential Execution (Recommended)
```bash
bash scripts/run_all_complex_tests.sh
```

**What it does:**
1. ✅ Checks Python venv
2. ✅ Verifies API health
3. ✅ Runs 50 complex query tests (~30min)
4. ✅ Runs 15 Pro Mode tests (~20min)
5. ✅ Runs 33 edge case tests (~15min)
6. ✅ Generates unified report

**Total time**: ~2 hours

### 2. Individual Suite Execution

```bash
# Only complex queries
python3 scripts/test_complex_queries.py

# Only Pro Mode
python3 scripts/test_promode_complex.py --timeout 120

# Only edge cases
python3 scripts/test_edge_cases.py
```

### 3. With Custom Configuration

```bash
# Custom API endpoint
python3 scripts/test_complex_queries.py \
    --api-base http://custom-api:3001/api \
    --timeout 120 \
    --output my_results.json

# All with custom endpoint
API_BASE=http://custom:3001/api bash scripts/run_all_complex_tests.sh
```

## Understanding Results

### Report Structure

Each test produces a JSON report with:

```json
{
  "metadata": {
    "timestamp": "ISO timestamp",
    "api_base": "API endpoint",
    "total_tests": 50
  },
  "summary": {
    "total_tests": 50,
    "passed": 43,
    "failed": 7,
    "pass_rate_percent": "86.0%",
    "performance": {
      "avg_response_time_ms": "3245",
      "median_response_time_ms": "2890",
      "max_response_time_ms": "8934"
    }
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

| Metric | Meaning | Target |
|--------|---------|--------|
| **Pass Rate** | % of tests that passed | ≥ 85% |
| **By Category** | Pass rate per category | ≥ 70% each |
| **Response Time** | How long queries take | < 15s typical |
| **Failures** | Number of failed tests | ≤ 7-8 out of 50 |

## Interpreting Pass/Fail

### PASS: Test Passed
✅ All conditions met:
- Valid response schema
- Expected data returned
- No unexpected errors
- Appropriate behavior

### FAIL: Test Failed
❌ One or more issues:
- Timeout or API error
- Wrong provider detected
- Insufficient data
- Schema validation error

## Success Criteria by Suite

| Suite | Target | Acceptable | Warning |
|-------|--------|-----------|---------|
| Complex (50) | 85%+ (42+) | 75%+ | < 75% |
| Pro Mode (15) | 85%+ (13+) | 70%+ | < 70% |
| Edge Cases (33) | 80%+ (26+) | 70%+ | < 70% |
| **Overall** | **85%+ (98/98)** | **75%+** | **< 75%** |

## Common Issues & Solutions

### Issue: Tests Timeout
**Cause**: API overloaded or slow query

**Solution**:
```bash
# Increase timeout
python3 scripts/test_complex_queries.py --timeout 120
```

### Issue: API Connection Failed
**Cause**: Backend not running

**Solution**:
```bash
# Check if running
curl http://localhost:3001/api/health

# If not, start it
npm run dev:backend
```

### Issue: All Multi-Provider Tests Fail
**Cause**: Issue with multi-provider query handling

**Solution**:
1. Check backend logs: `tail -f /tmp/backend-production.log`
2. Manually test: `curl -X POST http://localhost:3001/api/query -H "Content-Type: application/json" -d '{"query":"Compare GDP"}'`
3. Debug in backend: Add logging to `backend/services/query.py`

### Issue: Pro Mode Tests Fail
**Cause**: Grok LLM unavailable or slow code execution

**Solution**:
```bash
# Check GROK_API_KEY is set
echo $GROK_API_KEY

# Increase timeout
python3 scripts/test_promode_complex.py --timeout 180
```

## Performance Benchmarks

### Expected Response Times
| Query Type | Typical | Acceptable | Slow |
|-----------|---------|-----------|------|
| Single provider | 2-5s | < 10s | > 10s |
| Multi-provider (2) | 5-10s | < 20s | > 20s |
| Multi-provider (3+) | 10-15s | < 30s | > 30s |
| Regional agg (10+ countries) | 10-20s | < 40s | > 40s |
| Pro Mode simple | 20-30s | < 60s | > 60s |
| Pro Mode complex | 40-60s | < 120s | > 120s |

## Data Validation Examples

### What's Checked
```python
# Numeric ranges
assert 0 <= unemployment <= 100  # percent
assert gdp > 0  # always positive
assert -100 <= inflation <= 100  # can be negative (deflation)

# Date sequences
assert dates are monotonic increasing
assert no duplicate dates
assert proper frequency (monthly, quarterly, annual)

# Schema compliance
assert response has conversationId
assert data items have metadata
assert metadata has source, indicator, unit
```

## Analysis & Reporting

### Generate HTML Report
```bash
python3 scripts/analyze_test_results.py \
    --results-dir test_results_latest \
    --export-html report.html
```

### Generate CSV Export
```bash
python3 scripts/analyze_test_results.py \
    --results-dir test_results_latest \
    --export-csv results.csv
```

### Compare Multiple Test Runs
```bash
python3 scripts/analyze_test_results.py \
    --results-dir test_results_latest \
    --compare
```

## Continuous Integration

### Automated Testing
```bash
# Add to crontab (run daily at 2 AM)
0 2 * * * cd /home/hanlulong/econ-data-mcp && bash scripts/run_all_complex_tests.sh
```

### Trend Tracking
```bash
# Extract pass rate over time
for dir in test_results_*/; do
    rate=$(jq '.summary.pass_rate_percent' "$dir/complex_queries.json")
    echo "$(basename $dir): $rate"
done
```

## Expected Coverage

All 11 data providers are tested:

| Provider | Multi-Provider | Pro Mode | Edge Cases | Coverage |
|----------|---|---|---|---|
| FRED | ✅ | ✅ | ✅ | 100% |
| World Bank | ✅ | ✅ | ✅ | 100% |
| UN Comtrade | ✅ | ✅ | ✅ | 100% |
| Statistics Canada | ✅ | ✅ | ✅ | 100% |
| IMF | ✅ | ✅ | ✅ | 100% |
| BIS | ✅ | ✅ | ✅ | 100% |
| Eurostat | ✅ | ✅ | ✅ | 100% |
| OECD | ✅ | ✅ | ✅ | 100% |
| ExchangeRate-API | ✅ | ✅ | ✅ | 100% |
| CoinGecko | ✅ | ✅ | ✅ | 100% |
| Dune Analytics | ✅ | ✅ | ✅ | 100% |

## Modifying Tests

### Add New Test Case
1. Edit `scripts/complex_queries.json`
2. Add object to `test_cases` array
3. Rerun tests

### Add New Validation
1. Edit test runner (`test_complex_queries.py`, etc.)
2. Add logic to `validate_test_case()` method
3. Rerun tests

### Create Custom Test Suite
1. Copy test runner template
2. Modify validation logic
3. Run: `python3 your_test_suite.py`

## Best Practices

### Before Running Tests
- ✅ Ensure backend is running: `npm run dev:backend`
- ✅ Verify API health: `curl http://localhost:3001/api/health`
- ✅ Check available disk space for results
- ✅ Have 2-3 hours available for full suite

### During Testing
- ✅ Monitor backend logs: `tail -f /tmp/backend-production.log`
- ✅ Watch system resources: `top`, `htop`
- ✅ Don't interrupt tests mid-run

### After Testing
- ✅ Review failures immediately
- ✅ Document any fixes applied
- ✅ Archive results for comparison
- ✅ Update progress tracking

## Troubleshooting

### Script Permission Denied
```bash
chmod +x scripts/test_*.py scripts/run_all_complex_tests.sh
```

### ModuleNotFoundError: httpx
```bash
source backend/.venv/bin/activate
pip install httpx
```

### JSON Decode Error
```bash
# Make sure API is returning valid JSON
curl http://localhost:3001/api/health | python -m json.tool
```

### Results Directory Not Found
```bash
# Use the latest results
python3 scripts/analyze_test_results.py --results-dir test_results_latest
```

## Next Steps

1. **Run Tests**: Execute the full suite
2. **Review Results**: Check pass rates and failures
3. **Fix Issues**: Address failing tests one by one
4. **Validate Fixes**: Rerun relevant tests
5. **Document**: Update test notes with findings
6. **Iterate**: Achieve 85%+ target

## Support

For detailed information, see:
- **Testing Guide**: `docs/guides/COMPLEX_QUERY_TESTING.md`
- **Development Guide**: `CLAUDE.md`
- **Architecture**: Backend structure in `backend/README.md`

## Summary

This test suite provides:
- ✅ 50+ comprehensive complex query tests
- ✅ 15 Pro Mode AI code execution tests
- ✅ 33 edge case and error handling tests
- ✅ Unified reporting and analysis
- ✅ Performance benchmarking
- ✅ Data validation

**Goal**: Achieve 85%+ pass rate ensuring econ-data-mcp reliably handles advanced queries across all providers and edge cases.

---

**Created**: November 20, 2025
**Status**: Ready for testing
**Last Updated**: 2025-11-20
