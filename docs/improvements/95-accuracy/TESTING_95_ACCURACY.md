# Testing Guide: Achieving 95% Provider Accuracy

This guide explains how to run tests and track progress toward 95% accuracy for IMF, Comtrade, Eurostat, and BIS providers.

---

## Quick Start

### 1. Start Backend
```bash
# Activate virtual environment
source backend/.venv/bin/activate

# Start backend on port 3001
npm run dev:backend
```

### 2. Run Tests
```bash
# Test all providers (100 queries)
python3 scripts/test_all_providers_95.py

# Test specific provider
python3 scripts/test_all_providers_95.py --provider IMF
python3 scripts/test_all_providers_95.py --provider COMTRADE
python3 scripts/test_all_providers_95.py --provider EUROSTAT
python3 scripts/test_all_providers_95.py --provider BIS

# Save results to custom file
python3 scripts/test_all_providers_95.py --output my_results.json
```

### 3. Review Results
```bash
# View detailed JSON report
cat test_results_95.json | python3 -m json.tool

# Check tracking document
cat docs/PROVIDER_95_TRACKING.md
```

---

## Test Suite Overview

### Test Queries

- **100 total queries** (25 per provider)
- **Diverse coverage**: Basic queries, advanced queries, edge cases
- **Realistic scenarios**: Based on actual user queries

### Query Distribution

| Provider | Queries | Categories |
|----------|---------|------------|
| **IMF** | 25 | GDP (5), Unemployment (5), Inflation (5), Gov Finance (5), Other (5) |
| **Comtrade** | 25 | Basic (5), Commodity (5), Trade Balance (5), Multi-Country (5), Advanced (5) |
| **Eurostat** | 25 | GDP (5), Labor (5), Prices (5), Trade (5), Other (5) |
| **BIS** | 25 | Policy Rates (5), Credit (5), Property (5), Exchange (5), Other (5) |

---

## Test Results Classification

### Status Types

1. **✅ PASS** - Query succeeded with valid data
   - HTTP 200 response
   - Data field present
   - Valid metadata
   - Non-null data points
   - Reasonable value ranges

2. **⚠️ CLARIFICATION** - Query needs clarification (acceptable)
   - `clarificationNeeded: true`
   - Valid clarification questions provided
   - Counts toward soft accuracy

3. **❌ FAIL** - Query failed
   - HTTP error (404, 500, 429, timeout)
   - No data returned
   - All values null
   - Invalid/suspicious values
   - Does NOT count toward accuracy

### Accuracy Metrics

- **Soft Accuracy** = (PASS + CLARIFICATION) / TOTAL × 100
  - **Target**: 95%
  - Includes clarifications (valid LLM behavior)

- **Hard Accuracy** = PASS / TOTAL × 100
  - More stringent metric
  - Only counts perfect passes

---

## Interpreting Test Results

### Console Output

```
==================================================================================
Testing IMF Provider (25 queries)
==================================================================================

[1/25] Testing: Show me GDP growth for Germany from 2020 to 2024
    ✅ PASS: 20 data points (3.2s)

[2/25] Testing: Get government revenue for Germany
    ❌ FAIL: Missing indicator (2.1s)

[3/25] Testing: Compare inflation across Eurozone countries
    ⚠️ CLARIFICATION (4.5s)
```

### Summary Report

```
==================================================================================
TEST SUMMARY
==================================================================================

IMF:
  Total: 25
  ✅ Passed: 18 (72.0%)
  ⚠️ Clarifications: 2 (8.0%)
  ❌ Failed: 5 (20.0%)
  📊 Soft Accuracy: 80.0% (pass + clarification)
  📈 Hard Accuracy: 72.0% (pass only)

Overall:
  📊 Soft Accuracy: 82.5% (TARGET: 95%)

⚠️ BELOW TARGET: Need 12.5pp improvement to reach 95%
```

### JSON Report

Detailed results saved to `test_results_95.json`:

```json
{
  "timestamp": "2025-11-21T10:30:00Z",
  "summary": {
    "total_queries": 100,
    "total_passed": 75,
    "total_clarifications": 8,
    "total_failed": 17
  },
  "providers": {
    "IMF": {
      "total": 25,
      "passed": 18,
      "clarifications": 2,
      "failed": 5,
      "soft_accuracy": 80.0,
      "hard_accuracy": 72.0,
      "test_cases": [...]
    }
  }
}
```

---

## Workflow for Fixing Failures

### Step 1: Identify Failures

Run test and identify failed queries:

```bash
python3 scripts/test_all_providers_95.py --provider IMF
```

Example failures:
```
❌ FAIL: Missing indicator (Query: "Get government revenue for Germany")
❌ FAIL: Timeout (Query: "Get GDP for all EU countries")
❌ FAIL: HTTP 404 (Query: "Show tax revenue for France")
```

### Step 2: Update Tracking Document

Edit `docs/PROVIDER_95_TRACKING.md`:

```markdown
## IMF Provider - Failure Tracking

| # | Query | Status | Error Type | Error Message | Root Cause | Solution | Verified |
|---|-------|--------|------------|---------------|------------|----------|----------|
| 1 | Get government revenue for Germany | ❌ FAIL | Missing indicator | data_not_available | No GFS indicators | Add GFS mappings | ⏳ |
| 2 | Get GDP for all EU countries | ❌ FAIL | Timeout | Query exceeded 120s | No pagination | Implement chunking | ⏳ |
| 3 | Show tax revenue for France | ❌ FAIL | HTTP 404 | Indicator not found | Missing GGTAX_NGDP | Add to mappings | ⏳ |
```

### Step 3: Group by Root Cause

Organize failures by common causes:

**Missing Indicators** (3 failures):
- Government revenue
- Tax revenue
- Interest payments

**Performance Issues** (2 failures):
- EU-wide queries timing out
- G20 queries timing out

### Step 4: Implement Fixes

Fix by category (general solutions, not query-specific):

```python
# Example: Add GFS indicators to IMF provider
INDICATOR_MAPPINGS = {
    ...
    "GOVERNMENT_REVENUE": "GGR_NGDP",
    "TAX_REVENUE": "GGTAX_NGDP",
    "INTEREST_PAYMENTS": "GGXINT_NGDP",
}
```

### Step 5: Verify Fixes

Test original failing queries:

```bash
# Manual test
curl -X POST http://localhost:3001/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Get government revenue for Germany"}'
```

### Step 6: Re-run Test Suite

```bash
python3 scripts/test_all_providers_95.py --provider IMF
```

Check improvement:
```
IMF:
  📊 Soft Accuracy: 88.0% (was 80.0%) ✅ +8pp improvement!
```

### Step 7: Update Tracking

Mark fixed queries as verified:

```markdown
| 1 | Get government revenue for Germany | ✅ FIXED | - | - | No GFS indicators | Add GFS mappings | ✅ |
```

---

## Troubleshooting

### Test Hangs or Times Out

**Problem**: Queries taking too long

**Solutions**:
1. Increase timeout: Edit `TIMEOUT = 120` in test script
2. Test fewer providers: Use `--provider` flag
3. Check backend logs for errors

### Connection Errors

**Problem**: `Connection refused` or `Connection timeout`

**Solutions**:
1. Verify backend is running: `curl http://localhost:3001/api/health`
2. Check port 3001 is correct
3. Restart backend

### False Failures

**Problem**: Query works manually but fails in test

**Solutions**:
1. Check data quality validation logic
2. Verify response format
3. Add debug logging to test script

### Inconsistent Results

**Problem**: Same query passes/fails randomly

**Solutions**:
1. Check for race conditions
2. Verify API rate limits not being hit
3. Test with longer delays between queries

---

## Best Practices

### DO
✅ Run tests frequently during development
✅ Fix general issues (affects multiple queries)
✅ Validate fixes with manual testing
✅ Update tracking document after every fix
✅ Test edge cases thoroughly
✅ Check for regressions

### DON'T
❌ Hardcode solutions for specific test queries
❌ Remove failed queries from tracking
❌ Skip verification step
❌ Assume HTTP 200 = success (validate data!)
❌ Make changes without testing
❌ Fix only failing queries (think about similar cases)

---

## Progress Tracking

### Target Metrics

| Provider | Current | Target | Gap | Priority |
|----------|---------|--------|-----|----------|
| IMF | 80% | 95% | +15pp | HIGH |
| Comtrade | 80% | 95% | +15pp | HIGH |
| Eurostat | 67% | 95% | +28pp | CRITICAL |
| BIS | 67% | 95% | +28pp | CRITICAL |

### Weekly Goals

**Week 1**: Baseline + IMF to 95%
**Week 2**: Comtrade to 95%
**Week 3**: Eurostat to 95%
**Week 4**: BIS to 95%
**Week 5**: Final verification + deployment

---

## Related Documents

- **Implementation Plan**: `docs/PROVIDER_95_ACCURACY_PLAN.md`
- **Failure Tracking**: `docs/PROVIDER_95_TRACKING.md`
- **MASTER Status**: `MASTER.md`

---

**Last Updated**: 2025-11-21
