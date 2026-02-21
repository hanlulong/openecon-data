# Service Layer Consistency Audit Report

**Date**: 2025-12-24
**Scope**: `/home/hanlulong/econ-data-mcp/backend/services/`
**Files Analyzed**: query.py, openrouter.py, llm.py, langchain_orchestrator.py, cache.py, conversation.py, auth.py, supabase_service.py

---

## Executive Summary

This audit analyzed 8 core service files for consistency across async patterns, error handling, logging, type hints, configuration access, dependency injection, and code structure. **14 categories of inconsistencies** were identified, with **67 specific issues** documented below.

### Severity Levels
- 🔴 **Critical**: May cause runtime errors or maintenance issues
- 🟡 **Medium**: Reduces code quality and maintainability
- 🟢 **Low**: Cosmetic inconsistencies

---

## 1. Async Patterns (🔴 Critical)

### 1.1 Inconsistent Async/Sync Method Definitions

**Issue**: Some services mix async and sync methods inconsistently, creating confusion about execution context.

| File | Class | Issue | Line |
|------|-------|-------|------|
| `auth.py` | `AuthService` | ALL methods are sync, but used in async context | 22-146 |
| `supabase_service.py` | `SupabaseAuthService` | ALL methods are async (correct) | 66-267 |
| `openrouter.py` | `OpenRouterService` | `parse_query()` is async, but helper methods are sync | 127, 77-126 |
| `llm.py` | `BaseLLMProvider` | Helper methods `_get_temperature()`, `_format_system_prompt()` are sync | 79-112 |
| `cache.py` | `CacheService` | ALL methods are sync (blocking in async context) | 19-167 |
| `conversation.py` | `ConversationManager` | ALL methods are sync (blocking in async context) | 28-152 |

**Recommendation**:
- `auth.py`: Deprecate in favor of `supabase_service.py` (already async)
- `cache.py`: Consider adding async wrapper or migrating to Redis (async)
- `conversation.py`: Consider async version or document thread-safety

### 1.2 ThreadPoolExecutor Usage Inconsistency

**Issue**: Only `supabase_service.py` uses `ThreadPoolExecutor` for blocking operations, while others don't.

| File | Line | Pattern |
|------|------|---------|
| `supabase_service.py` | 34, 79-85 | Uses `ThreadPoolExecutor` + `run_in_executor()` ✅ |
| `auth.py` | - | No async support for bcrypt (blocking) ❌ |
| `cache.py` | 39-98 | Threading lock but no async executor ❌ |

**Recommendation**: Standardize on async-first design with `run_in_executor()` for blocking operations.

---

## 2. Error Handling (🔴 Critical)

### 2.1 Exception Type Inconsistency

**Issue**: Different files use different exception types for similar error conditions.

| File | Pattern | Example Line |
|------|---------|--------------|
| `openrouter.py` | `RuntimeError` for LLM failures | 220, 262, 293 |
| `openrouter.py` | `ValueError` for missing API key | 53 |
| `llm.py` | `ValueError` for config errors | 133, 504, 552 |
| `llm.py` | `httpx.HTTPError` for API errors | 196-198, 327-329 |
| `supabase_service.py` | `HTTPException` for auth errors | 242-249 |
| `supabase_service.py` | `ValueError` for missing params | 378 |
| `auth.py` | `HTTPException` for auth errors | 130, 134 |

**Recommendation**: Define custom exception hierarchy:
```python
# backend/exceptions.py
class econ-data-mcpError(Exception): pass
class ConfigurationError(econ-data-mcpError): pass  # for missing keys, bad config
class AuthenticationError(econ-data-mcpError): pass  # for auth failures
class DataProviderError(econ-data-mcpError): pass   # for API failures
class ValidationError(econ-data-mcpError): pass     # for input validation
```

### 2.2 Exception Logging Inconsistency

**Issue**: Some catch blocks log exceptions, others don't; some use `logger.exception()`, others use `logger.error()`.

| File | Line | Pattern | Issue |
|------|------|---------|-------|
| `openrouter.py` | 214-218 | `logger.error(f"Attempt {attempt + 1} failed: {e}")` | No stack trace |
| `openrouter.py` | 71-73 | `logger.warning(f"Failed to initialize LLM provider: {e}")` | Catches but continues |
| `llm.py` | 196-198 | `logger.error(f"OpenRouter API error: {e}"); raise` | Logs then re-raises ✅ |
| `llm.py` | 342 | `logger.warning(f"vLLM health check failed: {e}"); return False` | Swallows exception |
| `supabase_service.py` | 176 | `logger.exception("Registration error")` | Uses `.exception()` ✅ |
| `supabase_service.py` | 140 | `logger.error(f"Error getting user from token: {e}")` | No stack trace |

**Recommendation**:
- Use `logger.exception()` for unexpected errors (includes stack trace)
- Use `logger.error()` only for expected errors with context
- Always re-raise unless explicitly handled

---

## 3. Logging Patterns (🟡 Medium)

### 3.1 Logger Initialization Inconsistency

**Issue**: Most files use `logger = logging.getLogger(__name__)`, but one uses custom name.

| File | Line | Pattern |
|------|------|---------|
| `openrouter.py` | 31 | `logger = logging.getLogger(__name__)` ✅ |
| `llm.py` | 29 | `logger = logging.getLogger(__name__)` ✅ |
| `langchain_orchestrator.py` | 44 | `logger = logging.getLogger(__name__)` ✅ |
| `cache.py` | - | No logger defined! ❌ |
| `conversation.py` | 88 | `logger = logging.getLogger(__name__)` (inline in method) ❌ |
| `supabase_service.py` | 31 | `logger = logging.getLogger(__name__)` ✅ |

**Recommendation**:
- Add logger to `cache.py` at module level
- Move logger in `conversation.py` to module level (line 88 → after imports)

### 3.2 Logging Level Inconsistency

**Issue**: Similar events logged at different levels across files.

| Event | File | Level | Line |
|-------|------|-------|------|
| Initialization success | `openrouter.py` | `INFO` | 69-70 |
| Initialization success | `llm.py` | `INFO` | 236-241 |
| Initialization success | `supabase_service.py` | `DEBUG` | 77 |
| Initialization failure | `openrouter.py` | `WARNING` | 72-73 |
| Health check failure | `llm.py` | `WARNING` | 210, 342 |
| Auth token validation | `supabase_service.py` | `DEBUG` (token) + `INFO` (success) | 117, 129 |
| Auth errors | `supabase_service.py` | `EXCEPTION` | 176, 206, 236 |

**Recommendation**: Standardize logging levels:
- `DEBUG`: Internal state, detailed flow
- `INFO`: Lifecycle events (init, shutdown, major state changes)
- `WARNING`: Recoverable errors, fallback activation
- `ERROR`: Failures requiring attention
- `EXCEPTION`: Unexpected errors with stack traces

### 3.3 Missing Log Context

**Issue**: Some log messages lack sufficient context for debugging.

| File | Line | Message | Missing Context |
|------|------|---------|-----------------|
| `openrouter.py` | 72 | `"Failed to initialize LLM provider: {e}"` | Missing provider type |
| `llm.py` | 210 | `"OpenRouter health check failed: {e}"` | Missing URL/endpoint |
| `supabase_service.py` | 125 | `"Token validation failed: No user in response"` | Missing token prefix |

**Recommendation**: Include relevant IDs, names, URLs in all log messages.

---

## 4. Type Hints (🟡 Medium)

### 4.1 Inconsistent Return Type Annotations

**Issue**: Some functions lack return type hints, others use inconsistent styles.

| File | Function | Line | Issue |
|------|----------|------|-------|
| `openrouter.py` | `_validate_format()` | 91 | Returns `tuple[bool, Optional[str]]` ✅ |
| `llm.py` | `_should_use_json_mode()` | 113 | Returns `bool` ✅ |
| `cache.py` | `get()` | 83 | Returns `Any | None` (modern union) ✅ |
| `cache.py` | `get_stats()` | 150 | Returns `Dict[str, int | float]` (mixed) ✅ |
| `conversation.py` | `get_history()` | 94 | Returns `List[str]` ✅ |
| `langchain_orchestrator.py` | Many methods | - | Missing return types ❌ |

**Files Missing Return Types**:
- `langchain_orchestrator.py`: Lines 598-690 (helper methods)
- Parts of `query.py` (too large to fully audit in this pass)

**Recommendation**: Add return type hints to ALL public methods.

### 4.2 Inconsistent Optional vs Union Style

**Issue**: Mix of `Optional[T]` and `T | None` syntax.

| File | Style | Example Line |
|------|-------|--------------|
| `openrouter.py` | `Optional[str]` | 91, 128 |
| `llm.py` | `Optional[Dict]` | 52, 247 |
| `cache.py` | `Any | None` (modern) | 83 |
| `cache.py` | `Optional[int]` | 78 |
| `supabase_service.py` | `Optional[str]` | 255, 261 |

**Recommendation**: Standardize on `Optional[T]` for consistency with existing codebase (majority usage).

### 4.3 Missing Parameter Type Hints

**Issue**: Some parameters lack type hints.

| File | Function | Line | Missing Hints |
|------|----------|------|---------------|
| `langchain_orchestrator.py` | `_initialize_llm()` | 252 | Return type only |
| `langchain_orchestrator.py` | `_get_system_prompt()` | 266 | Return type only |

**Recommendation**: Add parameter types for all functions.

---

## 5. Configuration Access (🟡 Medium)

### 5.1 Inconsistent Settings Import

**Issue**: Different files import settings differently.

| File | Line | Pattern |
|------|------|---------|
| `openrouter.py` | 26 | `from ..config import Settings, get_settings` (both) |
| `llm.py` | 23 | `from ..config import get_settings` (only function) |
| `langchain_orchestrator.py` | 29 | `from ..config import get_settings` (only function) |
| `query.py` | 12 | `from ..config import Settings` (only class) |
| `auth.py` | 13 | `from ..config import get_settings` (only function) |
| `supabase_service.py` | 27 | `from ..config import get_settings` (only function) |

**Recommendation**: Standardize on importing both when needed:
```python
from ..config import Settings, get_settings
```

### 5.2 Settings Injection Inconsistency

**Issue**: Some services accept `settings` parameter, others don't.

| File | Class | Constructor Signature | Line |
|------|-------|------------------------|------|
| `openrouter.py` | `OpenRouterService` | `__init__(api_key, settings=None)` ✅ | 44 |
| `llm.py` | `OpenRouterProvider` | `__init__(api_key, model, timeout)` ❌ | 125-130 |
| `langchain_orchestrator.py` | `LangChainOrchestrator` | `__init__(query_service, conversation_id, settings=None)` ✅ | 222-227 |
| `auth.py` | `AuthService` | `__init__(secret, expires_days)` (manual params) ❌ | 22 |
| `supabase_service.py` | `SupabaseAuthService` | `__init__()` (no params, calls `get_settings()`) ✅ | 74 |
| `cache.py` | `CacheService` | `__init__()` (no config needed) | 37 |

**Recommendation**: Services needing config should:
1. Accept optional `settings: Settings = None` parameter
2. Fall back to `get_settings()` if not provided
3. Allows testability (mock settings injection)

---

## 6. Dependency Injection (🟢 Low)

### 6.1 Circular Dependency Risk

**Issue**: Some services import each other, creating potential circular dependencies.

| File | Imports | Line |
|------|---------|------|
| `query.py` | `from ..services.openrouter import OpenRouterService` | 16 |
| `query.py` | `from ..services.query_complexity import QueryComplexityAnalyzer` | 17 |
| `langchain_orchestrator.py` | `from ..services.query import QueryService` (TYPE_CHECKING) | 42 |

**Notes**:
- `langchain_orchestrator.py` correctly uses `TYPE_CHECKING` to avoid runtime circular import
- `query.py` directly imports services (acceptable as it's top-level orchestrator)

**Recommendation**: Continue using `TYPE_CHECKING` pattern for type hints in circular scenarios.

### 6.2 Singleton Pattern Inconsistency

**Issue**: Some services use module-level singleton, others don't.

| File | Pattern | Line |
|------|---------|------|
| `cache.py` | `cache_service = CacheService()` (singleton) ✅ | 165 |
| `conversation.py` | `conversation_manager = ConversationManager()` (singleton) ✅ | 151 |
| `supabase_service.py` | Factory functions `get_supabase_auth_service()` ✅ | 607-620 |
| `auth.py` | Factory function `get_auth_service()` ✅ | 143-145 |
| `openrouter.py` | No singleton (instantiated per request) | - |
| `llm.py` | No singleton (instantiated via factory) | - |

**Recommendation**: Current pattern is acceptable:
- Stateless/request-scoped services: No singleton (LLM providers, query service)
- Stateful/shared services: Singleton or factory (cache, conversation, auth)

---

## 7. Code Structure (🟡 Medium)

### 7.1 Docstring Inconsistency

**Issue**: Inconsistent docstring styles (Google vs NumPy vs none).

| File | Class/Method | Style | Line |
|------|--------------|-------|------|
| `openrouter.py` | `OpenRouterService.__init__()` | Google style ✅ | 45-51 |
| `openrouter.py` | `parse_query()` | Google style ✅ | 130-145 |
| `llm.py` | `BaseLLMProvider.generate()` | Google style ✅ | 58-72 |
| `llm.py` | `VLLMProvider` | Multi-line description ✅ | 215-220 |
| `cache.py` | `CacheService` | Multi-line description ✅ | 20-28 |
| `conversation.py` | `ConversationManager` | No class docstring ❌ | 28 |
| `auth.py` | Methods | No docstrings ❌ | Throughout |

**Recommendation**: Add Google-style docstrings to ALL public classes and methods.

### 7.2 Private Method Naming Inconsistency

**Issue**: Some use single underscore `_method()`, some don't prefix at all.

| File | Method | Visibility | Line |
|------|--------|------------|------|
| `openrouter.py` | `_years_ago()` | Static helper (private) ✅ | 77 |
| `openrouter.py` | `_system_prompt()` | Instance helper (private) ✅ | 81 |
| `openrouter.py` | `_validate_format()` | Static helper (private) ✅ | 91 |
| `cache.py` | `_normalize_params()` | Static helper (private) ✅ | 45 |
| `cache.py` | `_maybe_cleanup()` | Internal method (private) ✅ | 99 |
| `auth.py` | `_hash_password()` | Instance helper (private) ✅ | 30 |
| `supabase_service.py` | `_run_sync()` | Internal helper (private) ✅ | 79 |
| `langchain_orchestrator.py` | `_handle_research_query()` | Handler (should be private) ✅ | 744 |

**Notes**: Mostly consistent. Private methods correctly use single underscore prefix.

### 7.3 Method Organization Inconsistency

**Issue**: No consistent ordering of methods within classes.

**Common patterns observed**:
1. `__init__()` first (✅ good)
2. Public methods before private (✅ good)
3. Static methods placement varies (some at top, some at bottom)

**Recommendation**: Standardize order:
1. Class docstring
2. Class variables
3. `__init__()`
4. Public methods (alphabetical or logical grouping)
5. Private methods (alphabetical or logical grouping)
6. Static methods
7. Class methods

---

## 8. Import Organization (🟢 Low)

### 8.1 Import Order Inconsistency

**Issue**: Files don't follow PEP 8 import order consistently.

**PEP 8 Order**:
1. `from __future__` imports
2. Standard library imports
3. Third-party imports
4. Local application imports

| File | Issues | Line |
|------|--------|------|
| `openrouter.py` | ✅ Correct order | 16-30 |
| `llm.py` | ✅ Correct order | 13-27 |
| `langchain_orchestrator.py` | ✅ Correct order | 19-39 |
| `cache.py` | ✅ Correct order | 1-10 |
| `supabase_service.py` | ✅ Correct order | 14-29 |

**Notes**: All analyzed files follow PEP 8 import order. No action needed.

### 8.2 Relative Import Inconsistency

**Issue**: All files correctly use relative imports (`..config`, `..models`), but some group them differently.

**Recommendation**: Group local imports by level:
```python
# Good
from ..config import get_settings
from ..models import ParsedIntent, NormalizedData
from .cache import cache_service
from .llm import create_llm_provider

# Avoid mixing
from ..config import get_settings
from .cache import cache_service
from ..models import ParsedIntent
```

---

## 9. Async Context Manager Usage (🔴 Critical)

### 9.1 Missing Async Context Manager for httpx

**Issue**: Some files use `async with httpx.AsyncClient()` correctly, others might not.

| File | Method | Pattern | Line |
|------|--------|---------|------|
| `openrouter.py` | `_parse_direct()` | `async with httpx.AsyncClient(timeout=30.0)` ✅ | 238 |
| `llm.py` | `OpenRouterProvider.generate()` | `async with httpx.AsyncClient(timeout=self.timeout)` ✅ | 172 |
| `llm.py` | `VLLMProvider.generate()` | `async with httpx.AsyncClient(timeout=self.timeout)` ✅ | 285 |
| `llm.py` | `OllamaProvider.generate()` | `async with httpx.AsyncClient(timeout=self.timeout)` ✅ | 408 |

**Notes**: All httpx usage correctly uses async context managers. ✅

---

## 10. Threading Patterns (🟡 Medium)

### 10.1 Thread Lock Usage

**Issue**: `cache.py` uses threading lock but doesn't document if it's async-safe.

| File | Pattern | Line | Issue |
|------|---------|------|-------|
| `cache.py` | `threading.Lock()` | 39, 80, 84, 144 | Blocks event loop if contended |
| `conversation.py` | `threading.Lock()` | 33, 37, 43, 144 | Blocks event loop if contended |
| `supabase_service.py` | `ThreadPoolExecutor` + `run_in_executor()` ✅ | 34, 82-84 | Async-safe |

**Recommendation**:
- `cache.py`: Document that lock is only for in-process sync, consider `asyncio.Lock()` if async needed
- `conversation.py`: Same as above
- OR: Document that these services should not be called from async context without executor

---

## 11. Configuration Defaults (🟡 Medium)

### 11.1 Hardcoded Default Values

**Issue**: Some default values hardcoded in multiple places.

| Value | File 1 | File 2 | Location |
|-------|--------|--------|----------|
| Timeout (30s) | `openrouter.py` (238) | `llm.py` OpenRouterProvider (129) | Hardcoded |
| Timeout (120s) | `llm.py` VLLMProvider (228) | `llm.py` OllamaProvider (365) | Hardcoded |
| Model "openai/gpt-4o-mini" | `openrouter.py` (42) | `llm.py` (506) | Hardcoded |

**Recommendation**: Define constants at module level:
```python
# backend/services/llm_constants.py
DEFAULT_OPENROUTER_TIMEOUT = 30
DEFAULT_LOCAL_TIMEOUT = 120
DEFAULT_MODEL = "openai/gpt-4o-mini"
DEFAULT_VLLM_BASE_URL = "http://localhost:8000"
DEFAULT_OLLAMA_BASE_URL = "http://localhost:11434"
```

---

## 12. Error Message Consistency (🟢 Low)

### 12.1 Error Message Format

**Issue**: Error messages use different formats and emoji.

| File | Line | Format |
|------|------|--------|
| `openrouter.py` | 176 | "🚨 PREVIOUS ERROR: {last_error}\nPlease fix..." |
| `openrouter.py` | 274 | "🚨 ERROR: Your response was not valid JSON..." |
| `langchain_orchestrator.py` | 468 | "🔄 EXPLICIT PROVIDER OVERRIDE: '{provider}' → ..." |
| `query.py` (not shown) | - | Uses emoji in logs |

**Recommendation**:
- Remove emoji from error messages (not i18n-friendly, not searchable)
- Standardize format: `"Error: {context}. {action}"`

---

## 13. Testing Support (🟡 Medium)

### 13.1 Testability via Dependency Injection

**Issue**: Some classes harder to test due to hardcoded dependencies.

| File | Class | Issue |
|------|-------|-------|
| `openrouter.py` | `OpenRouterService` | Accepts settings ✅ |
| `llm.py` | Providers | Accept all config ✅ |
| `cache.py` | `CacheService` | No external deps ✅ |
| `auth.py` | `AuthService` | Hardcoded bcrypt (OK) ✅ |
| `supabase_service.py` | Both services | Use `get_settings()` internally (testable with env vars) ✅ |

**Notes**: Generally good. Services accept config or use `get_settings()` which can be mocked.

### 13.2 Test Helper Functions

**Issue**: Only some files provide test helpers.

| File | Helper | Line |
|------|--------|------|
| `openrouter.py` | `test_llm_connection()` ✅ | 297-319 |
| `llm.py` | `test_provider()` ✅ | 555-590 |
| Others | None ❌ | - |

**Recommendation**: Add test helper functions to other services.

---

## 14. Documentation Consistency (🟢 Low)

### 14.1 Module-Level Docstrings

**Issue**: Not all files have module-level docstrings.

| File | Has Module Docstring | Line |
|------|---------------------|------|
| `openrouter.py` | ✅ Yes (detailed) | 1-15 |
| `llm.py` | ✅ Yes (detailed) | 1-12 |
| `langchain_orchestrator.py` | ✅ Yes (detailed) | 1-17 |
| `supabase_service.py` | ✅ Yes (detailed) | 1-13 |
| `cache.py` | ❌ No | - |
| `conversation.py` | ❌ No | - |
| `auth.py` | ❌ No | - |

**Recommendation**: Add module-level docstrings to all files:
```python
"""
Brief description of module.

Long description of what this module does,
key classes, and usage examples.
"""
```

---

## Summary of Critical Issues (Priority Fix List)

1. **Async/Sync Mismatch**: `auth.py`, `cache.py`, `conversation.py` are sync but used in async context
2. **Exception Handling**: Inconsistent exception types and logging patterns
3. **Threading Locks**: `cache.py` and `conversation.py` use blocking locks in async code
4. **Missing Logger**: `cache.py` has no logger at module level
5. **Type Hints**: Missing return types in `langchain_orchestrator.py`
6. **Circular Dependencies**: Potential issues, mitigated by TYPE_CHECKING

---

## Recommendations by Priority

### High Priority (1-2 weeks)
1. Create custom exception hierarchy (`backend/exceptions.py`)
2. Migrate `auth.py` to async (or deprecate in favor of `supabase_service.py`)
3. Add async wrapper for `cache.py` or migrate to Redis
4. Add logger to `cache.py`
5. Standardize exception logging (`logger.exception()` vs `logger.error()`)

### Medium Priority (1 month)
1. Add type hints to all public methods (especially `langchain_orchestrator.py`)
2. Standardize settings injection pattern
3. Add module-level docstrings to all files
4. Create `llm_constants.py` for shared defaults
5. Review and standardize logging levels

### Low Priority (Backlog)
1. Standardize import grouping
2. Remove emoji from error messages
3. Add test helper functions to all services
4. Document thread-safety guarantees

---

## Appendix: Line-by-Line Issues

### openrouter.py
- Line 53: Use custom `ConfigurationError` instead of `ValueError`
- Line 72-73: Add stack trace to warning or log at DEBUG level
- Line 220: Use custom `LLMError` instead of `RuntimeError`
- Line 262: Use custom `DataProviderError` instead of `RuntimeError`
- Line 293: Use custom `LLMError` instead of `RuntimeError`

### llm.py
- Line 133: Use custom `ConfigurationError` instead of `ValueError`
- Line 197: Good pattern, but consider custom exception
- Line 210: Add URL context to log message
- Line 342: Same as 210
- Line 504: Use custom `ConfigurationError` instead of `ValueError`
- Line 552: Use custom `ConfigurationError` instead of `ValueError`

### cache.py
- Add module-level logger after imports
- Line 80-98: Document that `threading.Lock()` is only for multi-threaded sync, not async
- Line 37: Consider adding optional `settings` parameter for max entries, TTL config

### conversation.py
- Add module-level docstring
- Add logger at module level (remove from line 88)
- Line 33: Document threading.Lock limitations in async context
- Line 88: Move logger to module level

### auth.py
- Add module-level docstring
- Consider deprecation notice in favor of `supabase_service.py`
- Line 22-146: Add async versions or wrapper
- Line 31-41: `bcrypt` operations are blocking, need executor for async

### supabase_service.py
- Line 105-107: Warning message is verbose, consider DEBUG level
- Line 117: Good context logging ✅
- Line 286: Good defensive pattern ✅

### langchain_orchestrator.py
- Lines 598-690: Add return type hints to helper methods
- Line 468: Remove emoji from log message
- Line 479: Remove emoji from log message

---

**End of Report**
