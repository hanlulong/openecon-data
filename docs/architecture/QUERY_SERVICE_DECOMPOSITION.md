# Query Service Decomposition Plan

## Current State

`backend/services/query.py` is a 3,575 LOC "god object" that handles:
- Query parsing and intent extraction
- Provider selection and routing
- Data fetching orchestration
- Response formatting
- Error handling
- Caching logic
- Conversation management

This violates the Single Responsibility Principle and makes the code difficult to:
- Test in isolation
- Understand and maintain
- Extend with new features

## Proposed Architecture

### Phase 1: Extract Core Services (Priority: HIGH)

#### 1.1 QueryParser Service
**File:** `backend/services/query_parser.py`
**Responsibility:** Parse natural language queries into structured intents

```python
class QueryParser:
    async def parse(self, query: str) -> ParsedIntent:
        """Parse natural language query into structured intent."""
        pass

    async def validate_intent(self, intent: ParsedIntent) -> ValidationResult:
        """Validate the parsed intent for completeness."""
        pass
```

**Extract from query.py:**
- `_parse_query()` method
- LLM prompt construction
- Intent validation logic

#### 1.2 ProviderOrchestrator Service
**File:** `backend/services/provider_orchestrator.py`
**Responsibility:** Coordinate data fetching across providers

```python
class ProviderOrchestrator:
    async def fetch(self, intent: ParsedIntent) -> FetchResult:
        """Fetch data from appropriate provider(s)."""
        pass

    async def fetch_with_fallback(self, intent: ParsedIntent) -> FetchResult:
        """Fetch with automatic fallback to alternative providers."""
        pass
```

**Extract from query.py:**
- Provider selection logic
- Parallel fetching coordination
- Fallback chain management

#### 1.3 ResponseFormatter Service
**File:** `backend/services/response_formatter.py`
**Responsibility:** Format provider responses for the frontend

```python
class ResponseFormatter:
    def format(self, data: List[dict], intent: ParsedIntent) -> QueryResponse:
        """Format raw data into standardized response."""
        pass

    def format_error(self, error: Exception, intent: ParsedIntent) -> QueryResponse:
        """Format error into user-friendly response."""
        pass
```

**Extract from query.py:**
- Data normalization
- Chart data preparation
- Error message formatting

### Phase 2: Support Services (Priority: MEDIUM)

#### 2.1 IntentEnhancer Service
**File:** `backend/services/intent_enhancer.py`
**Responsibility:** Enrich parsed intents with additional context

```python
class IntentEnhancer:
    async def enhance(self, intent: ParsedIntent) -> ParsedIntent:
        """Add resolved countries, time ranges, indicators."""
        pass
```

**Extract from query.py:**
- Country resolution
- Time range parsing
- Indicator lookup

#### 2.2 QueryValidator Service
**File:** `backend/services/query_validator.py`
**Responsibility:** Validate queries before processing

```python
class QueryValidator:
    def validate(self, query: str) -> ValidationResult:
        """Validate query for basic requirements."""
        pass

    def sanitize(self, query: str) -> str:
        """Sanitize query for safe processing."""
        pass
```

### Phase 3: Refactored QueryService (Priority: HIGH)

The refactored `QueryService` becomes a thin orchestration layer:

```python
class QueryService:
    def __init__(
        self,
        parser: QueryParser,
        enhancer: IntentEnhancer,
        orchestrator: ProviderOrchestrator,
        formatter: ResponseFormatter,
        validator: QueryValidator,
    ):
        self.parser = parser
        self.enhancer = enhancer
        self.orchestrator = orchestrator
        self.formatter = formatter
        self.validator = validator

    async def process_query(self, query: str, conversation_id: str) -> QueryResponse:
        # 1. Validate
        validation = self.validator.validate(query)
        if not validation.valid:
            return self.formatter.format_validation_error(validation)

        # 2. Parse
        intent = await self.parser.parse(query)

        # 3. Enhance
        intent = await self.enhancer.enhance(intent)

        # 4. Fetch
        result = await self.orchestrator.fetch(intent)

        # 5. Format
        return self.formatter.format(result.data, intent)
```

**Target LOC:** ~200 lines (down from 3,575)

## Implementation Strategy

### Step 1: Create Interfaces (Week 1)
- Define abstract base classes for each service
- Create type definitions for data transfer
- Write comprehensive tests for expected behavior

### Step 2: Extract Services Incrementally (Weeks 2-4)
- Extract one service at a time
- Maintain backward compatibility
- Run full test suite after each extraction

### Step 3: Integration and Testing (Week 5)
- Wire up new services in dependency injection
- Performance testing
- Load testing

### Step 4: Cleanup (Week 6)
- Remove old code from query.py
- Update documentation
- Final review

## Migration Path

To avoid breaking changes, we use the Strangler Fig pattern:

1. New code calls new services
2. Old code paths continue to work
3. Gradually migrate all callers
4. Remove old code when no longer used

## Success Metrics

| Metric | Current | Target |
|--------|---------|--------|
| query.py LOC | 3,575 | <200 |
| Test coverage | ~60% | >90% |
| Cyclomatic complexity | High | Low |
| Time to add new provider | ~2 days | <4 hours |

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Breaking existing functionality | Comprehensive test suite before refactoring |
| Performance regression | Benchmark before/after each phase |
| Incomplete extraction | Clear interfaces defined upfront |
| Team confusion | Documentation and knowledge sharing |

## Next Steps

1. [ ] Create abstract base classes for each service
2. [ ] Write tests for expected behavior
3. [ ] Extract QueryParser first (lowest coupling)
4. [ ] Iterate through remaining services
5. [ ] Update main.py to use new service composition
