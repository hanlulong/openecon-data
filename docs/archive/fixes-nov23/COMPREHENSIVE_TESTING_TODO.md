# Comprehensive Testing & Error Fixing TODO

**Created**: 2025-11-23
**Status**: 🟡 IN PROGRESS
**Goal**: Fix all errors across 100 complex queries, test against production, use general solutions

---

## Phase 1: Project Cleanup & Setup ✅

- [x] Kill stuck background processes
- [x] Review project structure
- [ ] Remove unnecessary MD files
- [ ] Remove unnecessary test files
- [ ] Create comprehensive TODO tracking system
- [ ] Verify 100-query test suite exists

---

## Phase 2: Identify Unnecessary Files

### Unnecessary MD Files (TO REMOVE)
- [ ] `PROMPT_COMPARISON_TESTING.md` - Temporary testing doc
- [ ] `TESTING_FRAMEWORK_SUMMARY.md` - Temporary testing doc
- [ ] `SIMPLIFIED_PROMPT_FINDINGS.md` - Can be archived after review

### Unnecessary Test Files (TO REMOVE)
- [ ] `tests/test_prompt_comparison.py` - Old comparison test
- [ ] `tests/compare_data_values.py` - Duplicate of verify_data_values.py
- [ ] Review other test files for duplicates

### Unnecessary JSON Files (TO REMOVE)
- [ ] `test_results.json` - Old test results
- [ ] `test_results_old_prompt.json` - Old test results
- [ ] `comparison_report.txt` - Old comparison

---

## Phase 3: Run Comprehensive Tests

### Test Execution Strategy
- [ ] Use `comprehensive_test_suite_100.py` for 100 queries
- [ ] Run tests against **local API** (http://localhost:3001)
- [ ] Run tests against **production API** (https://openecon.ai)
- [ ] Compare local vs production results
- [ ] Identify all errors and categorize them

### Test Categories
- [ ] FRED queries (15 queries)
- [ ] World Bank queries (15 queries)
- [ ] UN Comtrade queries (15 queries)
- [ ] Statistics Canada queries (10 queries)
- [ ] IMF queries (10 queries)
- [ ] BIS queries (10 queries)
- [ ] Eurostat queries (10 queries)
- [ ] OECD queries (10 queries)
- [ ] ExchangeRate queries (3 queries)
- [ ] CoinGecko queries (2 queries)

---

## Phase 4: Error Tracking

### Critical Errors (Must Fix)
| ID | Provider | Query | Error Type | Root Cause | General Solution | Status |
|----|----------|-------|------------|------------|------------------|--------|
| - | - | - | - | - | - | ⏳ Pending |

### Provider Routing Errors
| ID | Query | Expected | Actual | Root Cause | Fix | Status |
|----|-------|----------|--------|------------|-----|--------|
| - | - | - | - | - | - | ⏳ Pending |

### Data Accuracy Errors
| ID | Query | Expected Value | Actual Value | Source | Fix | Status |
|----|-------|----------------|--------------|--------|-----|--------|
| - | - | - | - | - | - | ⏳ Pending |

### API Integration Errors
| ID | Provider | Error Message | Root Cause | Fix | Status |
|----|----------|---------------|------------|-----|--------|
| - | - | - | - | - | ⏳ Pending |

---

## Phase 5: Fix Implementation

### Fixes Applied
1. **FRED Series ID Normalization** (2025-11-23)
   - File: `backend/services/query.py` line 467
   - Change: `params["seriesId"] = indicator` → `params["indicator"] = indicator`
   - Impact: Fixed all FRED queries for SimplifiedPrompt
   - Status: ✅ DEPLOYED

### Pending Fixes
- [ ] TBD based on test results

---

## Phase 6: Verification

### Local Testing
- [ ] All 100 queries pass locally
- [ ] No provider routing errors
- [ ] Data values verified against samples

### Production Testing
- [ ] All 100 queries pass on production
- [ ] Production results match local results
- [ ] No regressions from previous fixes

### Manual Verification
- [ ] Sample 10 queries manually verified against authoritative sources
- [ ] Cross-check data values for accuracy
- [ ] Verify units and scaling are correct

---

## Phase 7: Documentation & Cleanup

- [ ] Document all fixes applied
- [ ] Update CLAUDE.md with lessons learned
- [ ] Remove temporary files
- [ ] Commit changes to git
- [ ] Create summary report

---

## Test Execution Commands

### Run Local Tests
```bash
source backend/.venv/bin/activate
python3 tests/run_parallel_tests.py
```

### Run Production Tests
```bash
# Modify run_parallel_tests.py to use https://openecon.ai/api
python3 tests/run_parallel_tests.py --production
```

### Compare Results
```bash
python3 tests/verify_data_values.py test_results_local.json test_results_production.json
```

---

## Progress Tracking

**Current Phase**: Phase 1 - Project Cleanup
**Queries Tested**: 0/100
**Errors Found**: 0
**Errors Fixed**: 1 (FRED normalization)
**Success Rate**: N/A

**Next Action**: Remove unnecessary files and run comprehensive tests

---

## Notes

- **Always test against production** after fixing errors locally
- **Use general solutions** that work for all future queries
- **No hardcoded fixes** for specific test cases
- **Verify data values** against authoritative sources when possible
- **Document root causes** for all errors found

---

**Last Updated**: 2025-11-23 01:40 UTC
