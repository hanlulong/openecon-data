# econ-data-mcp Codebase Consistency Improvement Plan

**Generated:** 2025-12-24
**Status:** In Progress
**Total Issues Found:** 85+ inconsistencies across all components

## Executive Summary

Comprehensive analysis of the econ-data-mcp codebase revealed inconsistencies across 6 major areas:
1. **Backend Agents** (8 files) - 15 inconsistencies
2. **Backend Providers** (11 files) - 18 inconsistencies
3. **Backend Core Services** (9 files) - 14 inconsistencies
4. **Backend Utility Services** (8 files) - 12 inconsistencies
5. **Frontend Components** (8+ files) - 18 inconsistencies
6. **Backend/Frontend Type Alignment** - 9 misalignments

---

## Batch Processing Plan

### Batch 1: Critical Security & Type Safety (HIGH PRIORITY)

| # | Issue | File(s) | Line(s) | Fix |
|---|-------|---------|---------|-----|
| 1.1 | Duplicated session ID sanitization | `session_storage.py`, `secure_code_executor.py` | 54-95, 299-319 | Extract to `utils/security.py` |
| 1.2 | Duplicated JSON encoder | `session_storage.py`, `secure_code_executor.py` | 19-34, 585-599 | Extract to `utils/serialization.py` |
| 1.3 | Frontend `any` types | `ChatPage.tsx`, `MessageChart.tsx`, `AuthContext.tsx` | 250, 272, 343, 82 | Replace with proper types |
| 1.4 | Missing clipboard check | `ChatPage.tsx` | 1119 | Add navigator?.clipboard check |
| 1.5 | CodeExecutionResult.files union | `types/index.ts` | 72 | Standardize to `GeneratedFile[]` only |
| 1.6 | ExportFormat missing data field | `types/index.ts` | 86-89 | Add `data: NormalizedData[]` |

### Batch 2: Code Patterns & Deduplication (MEDIUM PRIORITY)

| # | Issue | File(s) | Line(s) | Fix |
|---|-------|---------|---------|-----|
| 2.1 | DataReference creation duplicated 4x | `orchestrator.py`, `data_agent.py`, `comparison_agent.py` | 290-312, 319-349, 299-316 | Create shared `create_data_reference()` utility |
| 2.2 | Provider mapping duplicated 3x | `router_agent.py`, `comparison_agent.py` | 265-283, 390-404, 467-476 | Create `constants/providers.py` |
| 2.3 | Entity context building duplicated | `langgraph_graph.py` | 55-67, 127-129, 182-184, 250-253 | Extract to helper function |
| 2.4 | BaseProvider class unused | All providers | - | Either extend it or remove it |
| 2.5 | Singleton patterns inconsistent | `code_executor.py`, `circuit_breaker.py`, `export.py` | Various | Standardize on factory pattern |

### Batch 3: Error Handling Standardization (MEDIUM PRIORITY)

| # | Issue | File(s) | Line(s) | Fix |
|---|-------|---------|---------|-----|
| 3.1 | Mixed exception handling granularity | All agents, services | Various | Create exception hierarchy in `exceptions.py` |
| 3.2 | logger.error vs logger.exception | All services | Various | Use `logger.exception()` for unexpected errors |
| 3.3 | Missing try-except in router_agent | `router_agent.py` | All methods | Add try-except blocks |
| 3.4 | Missing try-except in langgraph nodes | `langgraph_graph.py` | Router, research, data, comparison nodes | Add try-except blocks |
| 3.5 | Inconsistent retry logic | Provider files | Various | Use BaseProvider retry methods or create shared utility |
| 3.6 | Frontend error utilities unused | `lib/errors.ts` vs components | Various | Use `getErrorMessage()`, `handleApiError()` |

### Batch 4: Types, Hints & Validation (MEDIUM PRIORITY)

| # | Issue | File(s) | Line(s) | Fix |
|---|-------|---------|---------|-----|
| 4.1 | Missing return type annotations | `orchestrator.py`, `router_agent.py`, `comparison_agent.py` | 111, 231, 227, 232 | Add `-> bool` return types |
| 4.2 | Dict vs dict inconsistency | Agent files | Various | Standardize on `Dict[...]` from typing |
| 4.3 | Missing `__future__` annotations | `code_executor.py`, `session_storage.py`, `grok.py` | Top of file | Add `from __future__ import annotations` |
| 4.4 | Provider return type inconsistency | All providers | fetch methods | Standardize: single `NormalizedData` vs `List[NormalizedData]` |
| 4.5 | HealthResponse.promodeEnabled | `types/index.ts` | 165 | Make required (not optional) |
| 4.6 | User.createdAt optional mismatch | `types/index.ts` | 95 | Make required string |

### Batch 5: Logging & Documentation (LOW PRIORITY)

| # | Issue | File(s) | Line(s) | Fix |
|---|-------|---------|---------|-----|
| 5.1 | Emoji logging inconsistent | Agent files, services | Various | Either use everywhere or nowhere |
| 5.2 | Log level inconsistency | All files | Various | Use DEBUG for cache hits, INFO for operations |
| 5.3 | Missing docstrings on private methods | Agent files | Various | Add docstrings to all private methods |
| 5.4 | Frontend console.* vs logger.* | `lib/supabase.ts` | 29, 254, 276, 332 | Replace with logger.* calls |
| 5.5 | Event handler naming | `ChatPage.tsx` | Various | Prefix all handlers with `handle` |

### Batch 6: Import & Structure Cleanup (LOW PRIORITY)

| # | Issue | File(s) | Line(s) | Fix |
|---|-------|---------|---------|-----|
| 6.1 | Absolute vs relative imports | `langgraph_graph.py` | 41, 42, 115, 171, 172 | Convert to relative imports (`from ..`) |
| 6.2 | UUID import placement | Agent files | Various | Move to module top level |
| 6.3 | Provider timeout inconsistency | Provider files | Various | Standardize: 30s default, 60s for heavy APIs |
| 6.4 | Hardcoded TOKEN_KEY | `api.ts` | 12 | Import from `constants/storage.ts` |
| 6.5 | Unnecessary await on sync function | `api.ts` | 122 | Remove `await` from getOrCreateSessionId() |

---

## Detailed Issue Breakdown by Component

### Backend Agents (15 Issues)

#### Critical
1. **Code Duplication**: `_create_data_reference()` duplicated 4 times (orchestrator.py:290-312, 483-508, data_agent.py:319-349, comparison_agent.py:299-316)
2. **Import Inconsistency**: langgraph_graph.py uses absolute imports (`from backend.`) while others use relative (`from ..`)

#### Moderate
3. **Error Handling**: No try-except in router_agent.py; inconsistent use in langgraph nodes
4. **Type Hints**: Missing return types on `_should_use_deep_agent`, `_matches_patterns`, `is_asset_comparison`, `_can_extract_indicator`
5. **Dict Typing**: Mix of `dict[...]` and `Dict[...]` styles
6. **Provider Mapping**: Duplicated 3 times with slight variations

#### Minor
7. **Logging with Emojis**: langgraph_graph.py uses emojis, others don't
8. **UUID Import**: Some at module level, some inside methods
9. **Docstrings**: Inconsistent coverage on private methods
10. **Log Message Format**: Varying verbosity across files

### Backend Providers (18 Issues)

#### Critical
1. **BaseProvider Unused**: Defined but no provider extends it - shared retry logic ignored
2. **Return Type Inconsistency**: Some return `NormalizedData`, others return `List[NormalizedData]`

#### Moderate
3. **Method Signatures Vary**: FRED uses `params: Dict`, WorldBank uses named parameters
4. **Timeout Inconsistency**: Range from 15s to 300s with no clear pattern
5. **Retry Logic**: Each provider implements its own or uses none
6. **No Caching at Provider Level**: All caching is external

#### Minor
7. **Emoji Logging**: Some providers use, some don't
8. **Metadata Completeness**: Some include `seasonalAdjustment`, `dataType`, `priceType`, others don't
9. **DataPoint Usage**: CoinGecko uses model, others use plain dicts
10. **Constructor Patterns**: Vary between `api_key`, `metadata_search_service`, or no params

### Backend Services (26 Issues)

#### Critical
1. **Duplicated Session Sanitization**: Nearly identical in session_storage.py and secure_code_executor.py
2. **Duplicated JSON Encoder**: Two different implementations for same purpose
3. **Settings Access**: Some use `get_settings()` with fallbacks, some assume settings exist

#### Moderate
4. **Exception Handling Patterns**: 4 different patterns (graceful degradation, re-raise, mixed, minimal)
5. **Async/Sync Mixing**: session_storage.py and export.py are sync in async context
6. **Hardcoded Configuration**: rate_limiter.py has hardcoded OECD limits
7. **Missing Logger**: cache.py has no module-level logger

#### Minor
8. **Log Level Inconsistency**: Cache hits logged as INFO instead of DEBUG
9. **Singleton Pattern Varies**: Global variable, class registry, or module instance
10. **Docstring Styles**: Mix of Google style, one-liners, multi-line without Args

### Frontend Components (18 Issues)

#### Critical
1. **Any Types**: Used in ChatPage.tsx, MessageChart.tsx, AuthContext.tsx (type safety loss)
2. **Missing Clipboard Check**: ChatPage.tsx:1119 doesn't verify navigator?.clipboard
3. **ExportFormat Missing Field**: Doesn't match backend ExportRequest

#### Moderate
4. **Error Handling Patterns**: 3 different patterns, lib/errors.ts utilities unused
5. **Mixed Fetch vs Axios**: Streaming uses fetch, rest uses axios
6. **Unnecessary Await**: getOrCreateSessionId() is sync but awaited

#### Minor
7. **Console vs Logger**: lib/supabase.ts uses console.* directly
8. **Modal State Patterns**: Inconsistent object vs boolean state
9. **Event Handler Naming**: Not all handlers prefixed with `handle`
10. **Hardcoded TOKEN_KEY**: Should use STORAGE_KEYS constant

### Type Alignment (9 Issues)

#### Critical
1. **CodeExecutionResult.files**: Frontend allows `string[]`, backend only `GeneratedFile[]`
2. **ExportFormat vs ExportRequest**: Frontend missing `data` field

#### Moderate
3. **User.createdAt**: Backend required, frontend optional
4. **HealthResponse.promodeEnabled**: Backend has default, frontend treats as optional
5. **datetime vs string**: Consistent serialization but types don't match

#### Minor
6. **StreamEvent Missing**: Backend has it, frontend doesn't
7. **EQL Models Missing**: Backend-only, may be intentional
8. **ProcessingStep.duration_ms**: Uses snake_case (unusual but consistent)

---

## Implementation Order

### Phase 1: Critical Fixes (Batch 1) - Estimated: 2-3 hours
- Create shared security utilities
- Fix type safety issues
- Fix clipboard check

### Phase 2: Code Deduplication (Batch 2) - Estimated: 3-4 hours
- Extract DataReference creation utility
- Create provider constants
- Standardize singleton pattern

### Phase 3: Error Handling (Batch 3) - Estimated: 2-3 hours
- Create exception hierarchy
- Standardize logging patterns
- Add missing try-except blocks

### Phase 4: Type Improvements (Batch 4) - Estimated: 2 hours
- Add missing type annotations
- Standardize type imports
- Fix type alignment

### Phase 5: Cleanup (Batches 5-6) - Estimated: 1-2 hours
- Standardize logging
- Fix imports
- Add documentation

---

## Files to Create

1. **`backend/utils/security.py`** - Shared sanitization functions
2. **`backend/utils/serialization.py`** - Shared JSON encoders
3. **`backend/exceptions.py`** - Custom exception hierarchy
4. **`backend/constants/providers.py`** - Provider name mappings
5. **`backend/agents/utils.py`** - Shared agent utilities (DataReference creation)

---

## Verification Checklist

After each batch:
- [ ] Run `pytest backend/tests/` - All tests pass
- [ ] Run `npm run build:frontend` - No TypeScript errors
- [ ] Test key queries on localhost
- [ ] Verify production site works

---

## Notes

- **Do not break existing functionality** - All fixes must be backward compatible
- **Add tests** for new utility functions
- **Document** any intentional deviations from patterns
- **Commit after each batch** with clear commit message
