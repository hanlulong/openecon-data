# Codebase Consistency Audit Plan

**Created**: 2025-12-25
**Status**: In Progress

## Audit Objectives
1. Identify type mismatches between backend and frontend
2. Find undefined variable references
3. Check import consistency
4. Verify model field usage across codebase
5. Identify dead code and unused imports
6. Check error handling patterns
7. Verify async/await consistency

## Batch Processing Plan

### Batch 1: Core Models and Configuration (HIGH PRIORITY)
Files to analyze:
- [ ] `backend/models.py` - Core Pydantic models
- [ ] `backend/models_eql.py` - EQL models
- [ ] `backend/config.py` - Configuration
- [ ] `packages/frontend/src/types/index.ts` - Frontend types

Focus: Type definitions, field names, model consistency

### Batch 2: Main Entry Points (HIGH PRIORITY)
Files to analyze:
- [ ] `backend/main.py` - FastAPI app, all endpoints
- [ ] `packages/frontend/src/App.tsx` - React app entry
- [ ] `packages/frontend/src/services/api.ts` - API client

Focus: API contracts, request/response handling

### Batch 3: Core Services - Query Processing (HIGH PRIORITY)
Files to analyze:
- [ ] `backend/services/query.py` - Main query service
- [ ] `backend/services/openrouter.py` - LLM service
- [ ] `backend/services/grok.py` - Pro Mode code generation
- [ ] `backend/services/code_executor.py` - Code execution
- [ ] `backend/services/secure_code_executor.py` - Sandbox execution

Focus: Data flow, error handling, type conversions

### Batch 4: Agent System (MEDIUM PRIORITY)
Files to analyze:
- [ ] `backend/agents/orchestrator.py` - Agent orchestrator
- [ ] `backend/agents/router_agent.py` - Query routing
- [ ] `backend/agents/data_agent.py` - Data fetching
- [ ] `backend/agents/research_agent.py` - Research queries
- [ ] `backend/agents/comparison_agent.py` - Comparisons
- [ ] `backend/services/deep_agent_orchestrator.py` - Deep Agents

Focus: Agent interfaces, state management

### Batch 5: LangChain/LangGraph Integration (MEDIUM PRIORITY)
Files to analyze:
- [ ] `backend/agents/langgraph_graph.py` - LangGraph graph
- [ ] `backend/agents/langgraph_state.py` - LangGraph state
- [ ] `backend/services/langchain_orchestrator.py` - LangChain orchestrator
- [ ] `backend/services/langchain_react_agent.py` - ReAct agent
- [ ] `backend/services/langchain_tools.py` - LangChain tools

Focus: LangChain API compatibility, state types

### Batch 6: Data Providers (MEDIUM PRIORITY)
Files to analyze:
- [ ] `backend/providers/base.py` - Base provider
- [ ] `backend/providers/fred.py` - FRED
- [ ] `backend/providers/worldbank.py` - World Bank
- [ ] `backend/providers/eurostat.py` - Eurostat
- [ ] `backend/providers/imf.py` - IMF
- [ ] `backend/providers/bis.py` - BIS
- [ ] `backend/providers/statscan.py` - Statistics Canada
- [ ] `backend/providers/oecd.py` - OECD
- [ ] `backend/providers/comtrade.py` - UN Comtrade
- [ ] `backend/providers/exchangerate.py` - Exchange rates
- [ ] `backend/providers/coingecko.py` - Crypto

Focus: Provider interface consistency, error handling

### Batch 7: Metadata and Search Services (MEDIUM PRIORITY)
Files to analyze:
- [ ] `backend/services/metadata_search.py` - Metadata search
- [ ] `backend/services/metadata_loader.py` - Metadata loading
- [ ] `backend/services/catalog_service.py` - Catalog
- [ ] `backend/services/catalog_indexer.py` - Indexing
- [ ] `backend/services/catalog_search.py` - Search
- [ ] `backend/services/vector_search.py` - Vector search
- [ ] `backend/services/faiss_vector_search.py` - FAISS

Focus: Search interfaces, data structures

### Batch 8: Memory and State Management (MEDIUM PRIORITY)
Files to analyze:
- [ ] `backend/memory/conversation_state.py` - Conversation state
- [ ] `backend/memory/state_manager.py` - State manager
- [ ] `backend/services/conversation.py` - Conversation service
- [ ] `backend/services/session_storage.py` - Session storage

Focus: State types, serialization

### Batch 9: Authentication and Caching (LOWER PRIORITY)
Files to analyze:
- [ ] `backend/services/auth.py` - Authentication
- [ ] `backend/services/auth_factory.py` - Auth factory
- [ ] `backend/services/mock_auth.py` - Mock auth
- [ ] `backend/services/cache.py` - Cache
- [ ] `backend/services/redis_cache.py` - Redis cache

Focus: Auth interfaces, cache types

### Batch 10: Frontend Components (MEDIUM PRIORITY)
Files to analyze:
- [ ] `packages/frontend/src/components/ChatPage.tsx` - Main chat
- [ ] `packages/frontend/src/components/MessageChart.tsx` - Charts
- [ ] `packages/frontend/src/components/CodeExecutionDisplay.tsx` - Code display
- [ ] `packages/frontend/src/components/ProcessingSteps.tsx` - Steps
- [ ] `packages/frontend/src/components/Auth.tsx` - Auth
- [ ] `packages/frontend/src/contexts/AuthContext.tsx` - Auth context

Focus: Props types, API response handling

### Batch 11: Utility Services (LOWER PRIORITY)
Files to analyze:
- [ ] `backend/services/export.py` - Export
- [ ] `backend/services/rate_limiter.py` - Rate limiting
- [ ] `backend/services/circuit_breaker.py` - Circuit breaker
- [ ] `backend/services/http_pool.py` - HTTP pool
- [ ] `backend/utils/*.py` - Utilities

Focus: Utility function signatures

## Issue Categories

### Category A: Type Mismatches
- Backend model vs frontend type definitions
- Function return types vs actual returns
- API response structure inconsistencies

### Category B: Undefined References
- Variables used but not defined
- Imports that don't exist
- Methods called on wrong objects

### Category C: Dead Code
- Unused imports
- Unreachable code
- Deprecated functions still present

### Category D: Error Handling
- Unhandled exceptions
- Inconsistent error formats
- Missing try/catch blocks

### Category E: Async Issues
- Missing await keywords
- Sync functions called in async context
- Race conditions

## Progress Tracking

| Batch | Status | Issues Found | Issues Fixed |
|-------|--------|--------------|--------------|
| 1 | Pending | 0 | 0 |
| 2 | Pending | 0 | 0 |
| 3 | Pending | 0 | 0 |
| 4 | Pending | 0 | 0 |
| 5 | Pending | 0 | 0 |
| 6 | Pending | 0 | 0 |
| 7 | Pending | 0 | 0 |
| 8 | Pending | 0 | 0 |
| 9 | Pending | 0 | 0 |
| 10 | Pending | 0 | 0 |
| 11 | Pending | 0 | 0 |

## Issues Log

### Critical Issues
| ID | File | Line | Issue | Status |
|----|------|------|-------|--------|

### Major Issues
| ID | File | Line | Issue | Status |

### Minor Issues
| ID | File | Line | Issue | Status |
