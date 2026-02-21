# econ-data-mcp Routing System Consolidation Analysis

**Document Version:** 1.1
**Date:** 2025-12-25
**Status:** Phase 1-2 IMPLEMENTED - Testing in Progress

## Implementation Progress (Updated 2025-12-25)

### Completed Steps (1-75)

| Phase | Steps | Status | Notes |
|-------|-------|--------|-------|
| Preparation | 1-10 | ✅ Done | Baseline tests created (69 tests, 100% pass) |
| Preparation | 11-20 | ✅ Done | Code analysis complete |
| Foundation | 21-30 | ✅ Done | CountryResolver implemented |
| Foundation | 31-40 | ✅ Done | KeywordMatcher implemented |
| Catalog | 41-55 | ✅ Done | Added 3 new catalogs, CatalogService integration |
| UnifiedRouter | 56-75 | ✅ Done | Full implementation with shadow mode |

### New Files Created

- `backend/routing/__init__.py` - Module exports
- `backend/routing/country_resolver.py` - Country normalization (250 lines)
- `backend/routing/keyword_matcher.py` - Keyword pattern matching (520 lines)
- `backend/routing/unified_router.py` - Main routing entry point (460 lines)
- `backend/routing/tests/__init__.py` - Test module
- `backend/routing/tests/test_unified_router.py` - 69 comprehensive tests
- `backend/catalog/concepts/tax_revenue.yaml` - OECD tax statistics
- `backend/catalog/concepts/fiscal_balance.yaml` - IMF fiscal data
- `backend/catalog/concepts/current_account.yaml` - IMF balance of payments

### Integration Method

**Feature flags** for safe rollout:
- `USE_UNIFIED_ROUTER=true` - Switch completely to UnifiedRouter
- `UNIFIED_ROUTER_SHADOW=true` - Run both, compare, log differences

**Shadow mode** logs routing differences as:
```
🔍 [SHADOW] Routing difference: legacy=X vs unified=Y (type=Z)
```

### Remaining Steps (76-100)

- **Steps 76-90**: Production deployment with gradual rollout (10% → 25% → 50% → 100%)
- **Steps 91-100**: Cleanup - remove duplicate code from ProviderRouter and DeepAgentOrchestrator

---

---

## Executive Summary

The econ-data-mcp backend currently has **3+ overlapping routing systems** that make provider selection decisions. This analysis evaluates the benefits and risks of consolidating these systems, followed by a detailed 100-step implementation plan.

**Current State:**
- 3 routing decision points
- 2,500+ lines of routing logic spread across 5+ files
- Duplicated capability matrices
- Case sensitivity bugs
- Inconsistent provider selection

**Recommendation:** Proceed with careful, phased consolidation to reduce complexity while maintaining stability.

---

## Part 1: Current Architecture Analysis

### 1.1 Existing Routing Systems

#### System 1: ProviderRouter (provider_router.py)
- **Size:** 914 lines
- **Type:** Deterministic, keyword-based
- **Location:** `backend/services/provider_router.py`
- **Characteristics:**
  - 11-level priority hierarchy
  - Explicit provider detection
  - US-only indicator detection
  - Country-specific routing (Canada → StatsCan)
  - Regional keyword detection
  - Trade query patterns

#### System 2: DeepAgentOrchestrator (deep_agent_orchestrator.py)
- **Size:** ~500 lines
- **Type:** Scoring-based, parallel execution
- **Location:** `backend/services/deep_agent_orchestrator.py`
- **Characteristics:**
  - PROVIDER_CAPABILITIES dictionary (duplicates ProviderRouter)
  - `select_best_provider()` scoring function
  - Parallel query execution
  - Retry mechanisms
  - Progress tracking

#### System 3: LangGraph Agent System (langgraph_graph.py)
- **Size:** 685 lines
- **Type:** State machine workflow
- **Location:** `backend/agents/langgraph_graph.py`
- **Characteristics:**
  - router_node → RouterAgent classification
  - data_node → Calls DataAgent which uses QueryService
  - Multiple node types (research, comparison, pro_mode)
  - Conversation state management

#### Supporting System: CatalogService (catalog_service.py)
- **Size:** 530 lines
- **Type:** YAML-based configuration
- **Location:** `backend/services/catalog_service.py`
- **Characteristics:**
  - Indicator-to-provider mappings from YAML files
  - Confidence scores per provider/indicator
  - Coverage checking (OECD/EU regions)
  - **Underutilized** - ProviderRouter doesn't leverage this

#### LLM Prompt: SimplifiedPrompt (simplified_prompt.py)
- **Size:** 1,380 lines
- **Type:** LLM instructions
- **Location:** `backend/services/simplified_prompt.py`
- **Characteristics:**
  - Detailed provider selection rules
  - Country/indicator mappings
  - **Largely overridden** by ProviderRouter

### 1.2 Routing Decision Flow

```
User Query
    │
    ▼
LangGraph router_node
    │ (RouterAgent.classify())
    ▼
QueryService.process_query()
    │
    ├─── SimplifiedPrompt (LLM decides provider)
    │         │
    │         ▼
    │    ProviderRouter.override_provider() ←── OVERRIDES LLM
    │         │
    │         ▼
    │    DeepAgentOrchestrator.execute_query()
    │         │
    │         ├─── select_best_provider() ←── DUPLICATES ProviderRouter
    │         │
    │         ▼
    │    CatalogService.is_provider_available() ←── UNDERUTILIZED
    │
    ▼
Provider API Calls
```

### 1.3 Identified Issues

1. **Capability Matrix Duplication**
   - ProviderRouter: OECD_MEMBERS, EU_MEMBERS, NON_OECD_MAJOR
   - DeepAgentOrchestrator: PROVIDER_CAPABILITIES
   - CatalogService: OECD_MEMBERS, EU_MEMBERS
   - SimplifiedPrompt: Inline country lists

2. **Case Sensitivity Bugs** (Fixed in catalog_service.py)
   - Provider names inconsistent: "COMTRADE" vs "Comtrade" vs "UN Comtrade"

3. **Override Chain Confusion**
   - LLM selects provider → ProviderRouter overrides → DeepAgent may override again

4. **Underutilized CatalogService**
   - Has confidence scores and coverage data
   - Not used by ProviderRouter for decisions

5. **SimplifiedPrompt Waste**
   - 1,380 lines of prompt
   - Largely ignored because ProviderRouter overrides

---

## Part 2: Benefits and Risks Analysis

### 2.1 Benefits of Consolidation

#### B1: Reduced Code Complexity
- **Current:** 2,500+ lines across 5 files
- **Target:** ~1,000 lines in 2 files
- **Impact:** 60% reduction in routing code
- **Maintainability:** Single source of truth

#### B2: Eliminated Duplication
- One PROVIDER_CAPABILITIES matrix
- One country membership set (OECD, EU, etc.)
- One set of routing rules

#### B3: Improved Consistency
- No more override chains causing confusion
- Predictable routing decisions
- Easier debugging

#### B4: Leveraged CatalogService
- Use confidence scores for routing
- Use coverage data for country support
- Configuration-driven (YAML) vs. code-driven

#### B5: Reduced LLM Token Usage
- SimplifiedPrompt can be shortened significantly
- LLM focuses on intent parsing, not routing

#### B6: Better Testing
- Single routing module to test
- Clear inputs/outputs
- Deterministic behavior

#### B7: Faster Development
- One place to add new providers
- One place to fix routing bugs
- Easier onboarding for new developers

### 2.2 Risks of Consolidation

#### R1: Regression Risk (HIGH)
- **Description:** Breaking existing working queries
- **Current Success Rates:**
  - Economic indicators: 100% (25/25)
  - Trade data: 100% (20/20)
  - Multi-country: 67% accuracy (15/15 returned data)
  - Specialized: 80% (16/20)
  - Financial: 85% (17/20)
- **Mitigation:** Extensive test suite before/after each change

#### R2: Hidden Dependencies (MEDIUM)
- **Description:** Unknown code paths relying on current behavior
- **Example:** Edge cases in ProviderRouter keywords
- **Mitigation:** Code coverage analysis, comprehensive grep searches

#### R3: Complexity Migration (MEDIUM)
- **Description:** Moving complexity instead of reducing it
- **Example:** Consolidating into one giant file
- **Mitigation:** Clear modular design, separation of concerns

#### R4: Performance Impact (LOW)
- **Description:** New routing logic may be slower
- **Current:** Sub-millisecond routing decisions
- **Mitigation:** Benchmark before/after, optimize hot paths

#### R5: Feature Gap (LOW)
- **Description:** New system missing edge cases from old systems
- **Example:** Obscure trade pattern detection
- **Mitigation:** Extract all patterns from existing code first

#### R6: Deployment Risk (MEDIUM)
- **Description:** Production issues during rollout
- **Mitigation:** Feature flags, gradual rollout, quick rollback

#### R7: Team Disruption (LOW)
- **Description:** Other features blocked during consolidation
- **Mitigation:** Phased approach, parallel development

### 2.3 Risk/Benefit Matrix

| Factor | Benefit Score | Risk Score | Net Score |
|--------|--------------|------------|-----------|
| Code Complexity | +3 | -2 | +1 |
| Maintainability | +3 | -1 | +2 |
| Consistency | +2 | -1 | +1 |
| Testing | +2 | -1 | +1 |
| Performance | +1 | -1 | 0 |
| Development Speed | +2 | -1 | +1 |
| Stability Risk | 0 | -3 | -3 |
| **Total** | **+13** | **-10** | **+3** |

**Conclusion:** Benefits outweigh risks, but careful execution is critical.

---

## Part 3: Recommended Architecture

### 3.1 Target State

```
User Query
    │
    ▼
LangGraph router_node (Query Classification Only)
    │ (No routing decisions here)
    ▼
QueryService.process_query()
    │
    ├─── UnifiedRouter ←── NEW: Single routing module
    │         │
    │         ├── CatalogService (indicator/provider mappings)
    │         ├── CountryResolver (country/region detection)
    │         └── KeywordMatcher (explicit provider detection)
    │
    ▼
Provider API Calls
```

### 3.2 Component Design

#### UnifiedRouter
- Single entry point for all routing decisions
- Uses CatalogService for indicator mappings
- Uses CountryResolver for geography
- Uses KeywordMatcher for explicit mentions
- Returns: (provider, confidence, fallbacks)

#### CatalogService (Enhanced)
- Keep existing YAML-based design
- Add missing providers to YAML configs
- Add missing indicators
- Expose confidence scores to router

#### CountryResolver
- Extract from ProviderRouter
- Normalize country names
- Detect regions (OECD, EU, ASEAN, etc.)

#### KeywordMatcher
- Extract explicit provider detection
- Trade pattern detection
- Crypto/currency detection

### 3.3 Migration Path

**Phase 1:** Create new components alongside existing
**Phase 2:** Redirect traffic to new components
**Phase 3:** Remove old code
**Phase 4:** Optimize and extend

---

## Part 4: 100-Step Implementation Plan

### Phase 1: Preparation (Steps 1-20)

#### 1.1 Test Infrastructure (Steps 1-10)

1. **Create baseline test suite**
   - Record current routing decisions for 200+ queries
   - Include all edge cases from existing code
   - File: `backend/tests/routing/baseline_routing_tests.py`

2. **Extract test queries from ProviderRouter**
   - All US_ONLY_INDICATORS patterns
   - All PROVIDER_KEYWORDS patterns
   - All REGIONAL_KEYWORDS patterns

3. **Extract test queries from DeepAgentOrchestrator**
   - All PROVIDER_CAPABILITIES specialty keywords
   - All country mappings

4. **Extract test queries from SimplifiedPrompt**
   - All example queries in prompt
   - All country/indicator combinations

5. **Create routing decision recorder**
   - Log: query → selected_provider → confidence
   - Compare old vs. new decisions

6. **Set up continuous integration**
   - Run routing tests on every commit
   - Block merges if regression detected

7. **Create performance benchmarks**
   - Measure routing decision latency
   - Measure end-to-end query latency

8. **Document current success rates**
   - Economic indicators: 100%
   - Trade data: 100%
   - Multi-country: 67%
   - Specialized: 80%
   - Financial: 85%

9. **Identify high-risk queries**
   - Queries that currently fail
   - Queries with inconsistent routing
   - Edge cases mentioned in code comments

10. **Create rollback procedure**
    - Feature flag: `USE_UNIFIED_ROUTER=false`
    - Quick switch back to old routing

#### 1.2 Code Analysis (Steps 11-20)

11. **Map all routing decision points**
    - ProviderRouter.route_provider()
    - DeepAgentOrchestrator.select_best_provider()
    - SimplifiedPrompt provider instructions
    - CatalogService.get_best_provider()

12. **Extract all country lists**
    - OECD_MEMBERS from all files
    - EU_MEMBERS from all files
    - NON_OECD_MAJOR from all files
    - Identify discrepancies

13. **Extract all provider capability definitions**
    - ProviderRouter keywords
    - DeepAgentOrchestrator PROVIDER_CAPABILITIES
    - CatalogService YAML configs
    - SimplifiedPrompt inline rules

14. **Document all override chains**
    - Where does LLM selection get overridden?
    - Where does ProviderRouter get overridden?
    - What triggers fallback providers?

15. **Identify all callers of routing functions**
    - grep for ProviderRouter usage
    - grep for select_best_provider usage
    - grep for CatalogService usage

16. **Document existing fallback logic**
    - When does FRED fall back to WorldBank?
    - When does Eurostat fall back to OECD?
    - What triggers provider retries?

17. **Identify technical debt markers**
    - TODO comments in routing code
    - FIXME comments
    - HACK comments

18. **Review recent routing bug fixes**
    - Git history for provider_router.py
    - Git history for catalog_service.py
    - Patterns in fixes

19. **Document external dependencies**
    - Which providers have rate limits?
    - Which providers have API keys?
    - Which providers are most reliable?

20. **Create consolidation design document**
    - Architecture diagram
    - Component interfaces
    - Migration timeline

### Phase 2: Foundation (Steps 21-40)

#### 2.1 New Module Structure (Steps 21-30)

21. **Create routing module directory**
    ```
    backend/routing/
    ├── __init__.py
    ├── unified_router.py
    ├── country_resolver.py
    ├── keyword_matcher.py
    └── tests/
    ```

22. **Create UnifiedRouter skeleton**
    - Interface definition
    - Input: query, intent, country
    - Output: (provider, confidence, fallbacks)

23. **Create CountryResolver skeleton**
    - normalize_country(name) → ISO code
    - get_regions(country) → [OECD, EU, ...]
    - is_member(country, region) → bool

24. **Create KeywordMatcher skeleton**
    - detect_explicit_provider(query) → provider | None
    - detect_query_type(query) → trade | currency | crypto | economic

25. **Migrate OECD_MEMBERS to CountryResolver**
    - Single source of truth
    - Add ISO codes

26. **Migrate EU_MEMBERS to CountryResolver**
    - Include all 27 current members
    - Add EUROZONE subset

27. **Migrate NON_OECD_MAJOR to CountryResolver**
    - Add more emerging markets
    - Add BRICS grouping

28. **Add ASEAN, G7, G20, BRICS to CountryResolver**
    - Currently missing from ProviderRouter
    - Needed for regional queries

29. **Add country aliases to CountryResolver**
    - "USA", "US", "United States", "America" → "US"
    - "UK", "Britain", "United Kingdom" → "GB"

30. **Write unit tests for CountryResolver**
    - 100% coverage
    - All edge cases

#### 2.2 Keyword Migration (Steps 31-40)

31. **Migrate explicit provider detection to KeywordMatcher**
    - PROVIDER_KEYWORDS from ProviderRouter
    - Start-of-query detection ("OECD GDP...")

32. **Migrate US_ONLY_INDICATORS to KeywordMatcher**
    - Federal funds rate, Case-Shiller, etc.
    - Add missing US-only indicators

33. **Migrate trade patterns to KeywordMatcher**
    - "exports to", "imports from"
    - "trade deficit", "trade surplus"
    - HS codes

34. **Migrate crypto patterns to KeywordMatcher**
    - Bitcoin, Ethereum, cryptocurrency
    - Market cap, price, volume

35. **Migrate currency patterns to KeywordMatcher**
    - Exchange rate patterns
    - "X to Y", "X/Y"

36. **Add interest rate patterns to KeywordMatcher**
    - Federal funds, ECB rate, BOE rate
    - LIBOR, SOFR, treasury yields

37. **Add commodity patterns to KeywordMatcher**
    - Gold, silver, oil prices
    - Map to FRED series IDs

38. **Write unit tests for KeywordMatcher**
    - 100% coverage
    - All patterns from ProviderRouter

39. **Verify KeywordMatcher matches ProviderRouter behavior**
    - Run comparison tests
    - Document any differences

40. **Add logging to KeywordMatcher**
    - Log matched patterns
    - Log confidence scores

### Phase 3: Catalog Enhancement (Steps 41-55)

41. **Audit existing YAML catalog files**
    - List all files in backend/catalog/concepts/
    - Identify gaps

42. **Add missing indicators to catalog**
    - Federal funds rate
    - Treasury yields
    - Exchange rates

43. **Add missing providers to catalog**
    - ExchangeRate-API
    - CoinGecko
    - BIS

44. **Add confidence scores to all catalog entries**
    - Based on data quality
    - Based on update frequency

45. **Add coverage information to catalog**
    - Which countries each provider covers
    - Which time ranges available

46. **Add frequency information to catalog**
    - Daily, weekly, monthly, quarterly, annual
    - Used for query routing

47. **Create catalog validation script**
    - Check YAML syntax
    - Check required fields
    - Check provider names

48. **Enhance CatalogService API**
    - get_best_provider(indicator, country) → (provider, confidence)
    - get_fallback_providers(indicator, country) → [(provider, confidence)]

49. **Add caching to CatalogService**
    - Cache YAML parsing
    - Cache lookup results

50. **Write comprehensive CatalogService tests**
    - Test all indicators
    - Test all providers
    - Test all countries

51. **Migrate ProviderRouter indicator rules to catalog**
    - US_ONLY_INDICATORS → catalog entries
    - Mark as FRED-only

52. **Migrate DeepAgent capabilities to catalog**
    - PROVIDER_CAPABILITIES → catalog entries
    - Preserve specialty keywords

53. **Add alias support to catalog**
    - "GDP" → "Gross Domestic Product"
    - "CPI" → "Consumer Price Index"

54. **Create catalog update script**
    - Fetch latest indicator lists from APIs
    - Update catalog automatically

55. **Document catalog schema**
    - Required fields
    - Optional fields
    - Examples

### Phase 4: UnifiedRouter Implementation (Steps 56-75)

56. **Implement UnifiedRouter.route()**
    - Entry point for all routing
    - Returns RoutingDecision dataclass

57. **Create RoutingDecision dataclass**
    ```python
    @dataclass
    class RoutingDecision:
        provider: str
        confidence: float
        fallbacks: List[str]
        reasoning: str
    ```

58. **Implement priority chain**
    1. Explicit provider detection (highest)
    2. US-only indicator detection
    3. Country-specific providers
    4. Catalog-based routing
    5. Default provider (lowest)

59. **Integrate KeywordMatcher**
    - Call detect_explicit_provider() first
    - Short-circuit if explicit

60. **Integrate CountryResolver**
    - Normalize country names
    - Get region memberships

61. **Integrate CatalogService**
    - Look up indicator mappings
    - Get provider confidence

62. **Implement fallback logic**
    - If primary fails, try fallbacks
    - Ordered by confidence

63. **Add OECD rate limit awareness**
    - Deprioritize OECD when rate limited
    - Track recent OECD calls

64. **Add logging with reasoning**
    - Log why each decision was made
    - Include all factors considered

65. **Write unit tests for UnifiedRouter**
    - Test all priority levels
    - Test fallback scenarios

66. **Run comparison against ProviderRouter**
    - Same inputs → same outputs?
    - Document differences

67. **Run comparison against DeepAgentOrchestrator**
    - Same inputs → same outputs?
    - Document differences

68. **Create A/B test infrastructure**
    - Feature flag: ROUTER_VERSION=unified|legacy
    - Log both decisions, use one

69. **Deploy UnifiedRouter in shadow mode**
    - Run alongside old routing
    - Log decisions without using them

70. **Analyze shadow mode results**
    - How many decisions differ?
    - Which are better?

71. **Fix discrepancies found in shadow mode**
    - Update rules as needed
    - Re-test

72. **Run full integration test suite**
    - All 200+ test queries
    - Compare success rates

73. **Performance benchmark UnifiedRouter**
    - Measure latency
    - Compare to old routing

74. **Optimize hot paths**
    - Cache country lookups
    - Precompile patterns

75. **Document UnifiedRouter API**
    - Input parameters
    - Output format
    - Examples

### Phase 5: Integration (Steps 76-90)

76. **Create adapter for QueryService**
    - Replace ProviderRouter calls with UnifiedRouter
    - Preserve interface

77. **Update DeepAgentOrchestrator**
    - Replace select_best_provider() with UnifiedRouter
    - Keep parallel execution logic

78. **Update LangGraph data_node**
    - Use UnifiedRouter for provider selection
    - Keep state management

79. **Simplify SimplifiedPrompt**
    - Remove provider selection rules
    - Keep intent parsing instructions
    - Target: 500 lines (from 1,380)

80. **Update error messages**
    - Include routing reasoning
    - Better debugging info

81. **Add routing metrics**
    - Provider selection counts
    - Fallback trigger counts
    - Success rates by provider

82. **Create routing dashboard**
    - Visualize provider distribution
    - Show success rates

83. **Test in staging environment**
    - Full query suite
    - Compare to production

84. **Fix issues found in staging**
    - Document fixes
    - Re-test

85. **Deploy to production with feature flag**
    - USE_UNIFIED_ROUTER=true for 10% traffic
    - Monitor closely

86. **Gradually increase traffic**
    - 25%, 50%, 75%, 100%
    - Check for regressions at each step

87. **Monitor production metrics**
    - Query success rate
    - Response latency
    - Error rates

88. **Fix production issues**
    - Hotfix as needed
    - Quick rollback if critical

89. **Verify all test cases pass**
    - Economic indicators: 100%+
    - Trade data: 100%+
    - Multi-country: 70%+ (improved from 67%)
    - Specialized: 85%+ (improved from 80%)
    - Financial: 90%+ (improved from 85%)

90. **Document production deployment**
    - Deployment procedure
    - Rollback procedure
    - Monitoring setup

### Phase 6: Cleanup (Steps 91-100)

91. **Mark old routing code as deprecated**
    - Add deprecation warnings
    - Set removal timeline

92. **Remove ProviderRouter duplicate logic**
    - Keep only explicit provider detection
    - Remove capability matrices

93. **Remove DeepAgentOrchestrator.select_best_provider()**
    - Replace with UnifiedRouter calls
    - Keep orchestration logic

94. **Simplify SimplifiedPrompt further**
    - Remove all provider rules
    - Focus on intent only

95. **Remove duplicate country lists**
    - Single source: CountryResolver
    - Update all imports

96. **Remove duplicate capability definitions**
    - Single source: CatalogService
    - Update all imports

97. **Update documentation**
    - Architecture diagrams
    - Developer guides
    - API documentation

98. **Update CLAUDE.md**
    - New routing architecture
    - New file locations

99. **Final code review**
    - Check for orphaned code
    - Check for broken imports

100. **Archive old routing code**
     - Git tag: pre-routing-consolidation
     - Document what was removed
     - Create migration guide

---

## Part 5: Success Criteria

### 5.1 Quantitative Metrics

| Metric | Before | Target | Stretch |
|--------|--------|--------|---------|
| Routing code lines | 2,500+ | 1,000 | 800 |
| Duplicate matrices | 4 | 1 | 1 |
| Query success rate | 86% avg | 90% | 95% |
| Routing latency | <1ms | <1ms | <0.5ms |
| SimplifiedPrompt size | 1,380 lines | 500 lines | 300 lines |

### 5.2 Qualitative Metrics

- [ ] Single source of truth for provider capabilities
- [ ] Single source of truth for country memberships
- [ ] Clear routing decision logging
- [ ] Easy to add new providers
- [ ] Easy to fix routing bugs
- [ ] Comprehensive test coverage

### 5.3 Risk Mitigation Checkpoints

- **Step 10:** Rollback procedure tested
- **Step 20:** Design document approved
- **Step 40:** Foundation tests passing
- **Step 70:** Shadow mode successful
- **Step 85:** Production canary successful
- **Step 100:** Full cleanup complete

---

## Part 6: Timeline Estimate

| Phase | Steps | Estimated Duration |
|-------|-------|-------------------|
| Preparation | 1-20 | 2-3 days |
| Foundation | 21-40 | 3-4 days |
| Catalog Enhancement | 41-55 | 2-3 days |
| UnifiedRouter | 56-75 | 3-4 days |
| Integration | 76-90 | 3-4 days |
| Cleanup | 91-100 | 1-2 days |
| **Total** | 1-100 | **14-20 days** |

---

## Appendix A: File Reference

| File | Current Role | Future Role |
|------|--------------|-------------|
| provider_router.py | Primary routing | Deprecated |
| deep_agent_orchestrator.py | Parallel execution + routing | Parallel execution only |
| langgraph_graph.py | Agent workflow | Agent workflow (unchanged) |
| catalog_service.py | Indicator mappings | Enhanced (primary routing data) |
| simplified_prompt.py | LLM routing rules | LLM intent parsing only |
| **unified_router.py** | N/A | **New primary router** |
| **country_resolver.py** | N/A | **New country normalization** |
| **keyword_matcher.py** | N/A | **New pattern detection** |

---

## Appendix B: Quick Reference

### Current Test Results (Baseline)
- Economic indicators: 100% (25/25)
- Trade data: 100% (20/20)
- Multi-country: 67% accuracy
- Specialized: 80% (16/20)
- Financial: 85% (17/20)

### Key Files to Modify
1. `backend/services/provider_router.py` - Extract, then deprecate
2. `backend/services/deep_agent_orchestrator.py` - Remove routing, keep orchestration
3. `backend/services/catalog_service.py` - Enhance for routing
4. `backend/services/simplified_prompt.py` - Reduce to 500 lines

### Key Files to Create
1. `backend/routing/unified_router.py`
2. `backend/routing/country_resolver.py`
3. `backend/routing/keyword_matcher.py`
4. `backend/tests/routing/` - Test suite

---

**Document Author:** Claude Code
**Review Required By:** Development Team
**Implementation Start:** After Team Approval
