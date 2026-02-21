# Query and Prompt Architecture Improvements

**Status**: ✅ Completed and Tested
**Date**: 2025-11-23
**Test Results**: 47/47 tests passed (100% success rate)

## Executive Summary

I've reviewed and improved the query processing architecture, moving from a **monolithic 1,300-line LLM prompt** to a **modular, code-first design**. This provides:

- **Better reliability**: Deterministic routing logic instead of LLM decision-making
- **Easier maintenance**: Provider routing rules in testable Python code
- **Improved performance**: Shorter prompt → faster LLM responses
- **Better testability**: Comprehensive unit tests validate routing behavior

## Current Problems

### 1. Prompt is Too Long (1,300+ Lines)

**File**: `backend/services/openrouter.py` lines 55-1333

**Issues**:
- Exceeds optimal LLM context window usage
- Difficult for model to follow all rules consistently
- Hard to maintain and update
- Increases LLM API costs

### 2. Mixed Concerns

The prompt currently does EVERYTHING:
- ✅ Intent extraction (belongs in prompt)
- ❌ Provider routing (should be in code)
- ❌ Parameter validation (should be in code)
- ❌ Format validation (should be in code)
- ❌ Documentation links (not needed)

### 3. Brittle Provider Routing

Provider selection uses nested priority rules in the prompt:
- "CRITICAL RULE #1", "ABSOLUTE HIGHEST PRIORITY", "MANDATORY"
- Multiple contradicting priority hierarchies
- Hard to debug when routing goes wrong
- Adding new providers requires prompt surgery

### 4. Too Many Hardcoded Examples

50+ specific query examples hardcoded in prompt:
- Doesn't generalize well
- Becomes outdated quickly
- Makes prompt even longer

## New Architecture

### Component 1: ProviderRouter Service

**File**: `backend/services/provider_router.py`

**Purpose**: Deterministic provider selection using Python code

```python
routed_provider = ProviderRouter.route_provider(intent, original_query)
```

**Features**:
- Explicit provider detection ("from OECD", "using IMF")
- US-only indicator detection (Case-Shiller → FRED)
- Country-specific routing (Canada → StatsCan, China → WorldBank)
- Indicator-specific routing (debt → IMF, housing → BIS)
- Fallback provider logic

**Benefits**:
- 100% deterministic (same input = same output)
- Fully unit tested (47 tests, all passing)
- Easy to debug and modify
- Can be tested without calling LLM

### Component 2: Simplified Prompt

**File**: `backend/services/simplified_prompt.py`

**Purpose**: Extract user intent, nothing more

**Length**: ~200 lines (vs 1,300+ previously)

**Focus**:
- What data does the user want?
- What time period?
- Which countries?
- Is clarification needed?

**What it DOESN'T do**:
- ❌ Provider routing (ProviderRouter handles this)
- ❌ Parameter validation (ParameterValidator handles this)
- ❌ Complex decision trees

**Benefits**:
- Faster LLM responses
- Clearer instructions
- Easier to maintain
- Better generalization

### Component 3: Test Suite

**File**: `tests/test_provider_router.py`

**Coverage**: 47 test cases across 4 categories:
1. Explicit provider detection (18 tests) ✅
2. US-only indicator detection (9 tests) ✅
3. Canadian query detection (9 tests) ✅
4. Complete routing logic (11 tests) ✅

**Results**: 100% pass rate

## Test Results

```
================================================================================
TEST SUMMARY
================================================================================
Total Tests: 47

Results:
  ✅ Explicit Provider Detection:     18/18 passed (100%)
  ✅ US-Only Indicator Detection:      9/9 passed (100%)
  ✅ Canadian Query Detection:         9/9 passed (100%)
  ✅ Provider Routing Logic:          11/11 passed (100%)

Overall Accuracy: 47/47 (100%)
================================================================================
```

### Key Test Cases

**1. Explicit Provider Overrides** (CRITICAL)
```
Query: "Get Italy GDP from OECD"
LLM suggests: WorldBank
Router corrects: OECD ✅

Query: "Show me Russia imports from Comtrade for the last 10 years"
LLM suggests: WorldBank
Router corrects: Comtrade ✅
```

**2. US-Only Indicators**
```
Query: "Case-Shiller home price index"
Router: FRED ✅ (Case-Shiller is US-only)

Query: "Federal funds rate"
Router: FRED ✅ (US-specific indicator)
```

**3. Canadian Queries**
```
Query: "Canada GDP"
Router: StatsCan ✅

Query: "Ontario unemployment"
Router: StatsCan ✅
```

**4. Smart Routing**
```
Query: "Germany house prices"
Router: BIS ✅ (Non-US housing → BIS)

Query: "US government debt"
Router: IMF ✅ (Fiscal data → IMF)

Query: "Bitcoin price"
Router: CoinGecko ✅ (Crypto → CoinGecko)
```

## Architecture Comparison

### OLD: Monolithic Prompt
```
┌─────────────────────────────────────┐
│  1,300-Line Prompt                  │
│  ─────────────────────────────────  │
│  ▪ Intent extraction                │
│  ▪ Provider routing (inconsistent)  │
│  ▪ Parameter validation             │
│  ▪ Format validation                │
│  ▪ 50+ hardcoded examples           │
│  ▪ Complex priority hierarchies     │
└─────────────────────────────────────┘
              ↓
         [LLM Parse]
              ↓
      ParsedIntent (maybe wrong provider)
```

### NEW: Modular Code-First
```
┌─────────────────────────┐
│  Simplified Prompt      │
│  ─────────────────────  │
│  ▪ Intent extraction    │
│  ▪ Basic structure      │
│  ▪ Clarification logic  │
│  (~200 lines)           │
└─────────────────────────┘
           ↓
      [LLM Parse]
           ↓
   ParsedIntent (rough)
           ↓
┌─────────────────────────┐
│  ProviderRouter (code)  │
│  ─────────────────────  │
│  ▪ Explicit detection   │
│  ▪ Deterministic rules  │
│  ▪ 100% testable        │
└─────────────────────────┘
           ↓
   ParsedIntent (correct provider)
           ↓
┌─────────────────────────┐
│  ParameterValidator     │
│  ─────────────────────  │
│  ▪ Apply defaults       │
│  ▪ Validate params      │
│  ▪ Check confidence     │
└─────────────────────────┘
           ↓
    Final Intent ✅
```

## Benefits

### 1. Reliability
- **OLD**: LLM sometimes ignores routing rules
- **NEW**: Deterministic code guarantees correct routing

### 2. Testability
- **OLD**: Can only test by calling LLM (slow, non-deterministic)
- **NEW**: Unit tests validate logic without LLM calls

### 3. Maintainability
- **OLD**: Updating routing requires modifying 1,300-line prompt
- **NEW**: Update Python code, run tests, deploy

### 4. Performance
- **OLD**: 1,300-line prompt = high token cost, slow response
- **NEW**: 200-line prompt = lower cost, faster response

### 5. Debuggability
- **OLD**: When routing fails, hard to know why
- **NEW**: Clear code path with logging at each step

### 6. Extensibility
- **OLD**: Adding new provider = rewriting prompt examples
- **NEW**: Add routing rules to Python, add tests, done

## Migration Plan

### Phase 1: Integrate ProviderRouter (Low Risk)
1. Update `backend/services/query.py` to use ProviderRouter
2. Keep existing prompt unchanged
3. Add post-processing step to override provider

```python
# In QueryService.process_query()
intent = await self.openrouter.parse_query(query, history)

# NEW: Override provider with deterministic routing
from .provider_router import ProviderRouter
final_provider = ProviderRouter.route_provider(intent, query)
if final_provider != intent.apiProvider:
    logger.info(f"🔄 Router override: {intent.apiProvider} → {final_provider}")
    intent.apiProvider = final_provider
```

**Benefits**:
- Immediate improvement to routing reliability
- No change to LLM prompt (low risk)
- Can be deployed independently

### Phase 2: Switch to Simplified Prompt (Medium Risk)
1. Update `OpenRouterService._system_prompt()` to use `SimplifiedPrompt.generate()`
2. Test with diverse queries
3. Monitor accuracy metrics

**Rollback Plan**:
- Keep old prompt in git history
- Easy rollback if issues arise

### Phase 3: Optimize and Refine
1. Remove unnecessary provider override code in query.py (since routing is already correct)
2. Fine-tune simplified prompt based on production data
3. Add more test cases

## Files Created

1. **`backend/services/provider_router.py`** (345 lines)
   - Deterministic provider routing service
   - Comprehensive routing logic
   - Fallback provider support

2. **`backend/services/simplified_prompt.py`** (233 lines)
   - Simplified LLM prompt generator
   - Format validation
   - ~200 lines vs 1,300+ previously

3. **`tests/test_provider_router.py`** (348 lines)
   - 47 comprehensive unit tests
   - 100% pass rate
   - Tests all routing scenarios

4. **`tests/test_prompt_comparison.py`** (410 lines)
   - Integration tests comparing old vs new
   - Tests with real LLM calls
   - Validates end-to-end behavior

5. **`docs/PROMPT_ARCHITECTURE_IMPROVEMENTS.md`** (this file)
   - Complete documentation
   - Test results
   - Migration plan

## Recommendations

### Immediate Actions (Do Now)

1. ✅ **Run Tests**: Verify all tests pass
   ```bash
   python3 tests/test_provider_router.py
   ```

2. ✅ **Review Code**: Examine the new architecture
   - `backend/services/provider_router.py`
   - `backend/services/simplified_prompt.py`

3. **Decide**: Choose migration approach
   - Option A: Deploy ProviderRouter immediately (low risk)
   - Option B: Wait for more testing
   - Option C: Gradual rollout with A/B testing

### Future Improvements

1. **Parameter Validator Enhancement**
   - Move default time period logic from prompt to code
   - Add smart defaults for all providers
   - Validate parameter combinations

2. **Metadata-Aware Routing**
   - Use MetadataSearchService to verify provider has requested indicator
   - Automatic fallback if primary provider lacks data

3. **Machine Learning Routing** (Advanced)
   - Learn from query history which providers work best
   - Predict optimal provider based on query patterns
   - Fall back to deterministic rules if unsure

4. **A/B Testing Framework**
   - Test new prompt changes safely
   - Compare metrics (accuracy, clarification rate, user satisfaction)
   - Data-driven optimization

## Conclusion

The new architecture provides a **solid foundation** for reliable, maintainable query processing. The **modular design** separates concerns properly:

- **LLM Prompt**: Extract user intent
- **ProviderRouter**: Select correct data source (deterministic)
- **ParameterValidator**: Validate and enrich parameters
- **QueryService**: Orchestrate the pipeline

**Test Results**: 47/47 tests passed (100%)
**Risk Level**: Low (can be deployed incrementally)
**Impact**: Improved reliability, easier maintenance, better user experience

## Next Steps

**Awaiting your decision:**

1. Should we proceed with Phase 1 (integrate ProviderRouter)?
2. Do you want to see more test results?
3. Should we do A/B testing in production first?

Let me know how you'd like to proceed!
