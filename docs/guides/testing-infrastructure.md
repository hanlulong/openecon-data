# Testing Infrastructure Guide

This guide covers the testing infrastructure and best practices for the econ-data-mcp codebase.

## Overview

econ-data-mcp uses a comprehensive testing setup:

| Component | Framework | Coverage Target |
|-----------|-----------|-----------------|
| Backend (Python) | pytest + pytest-asyncio | 70% |
| Frontend (TypeScript) | Vitest + React Testing Library | 60% |

## Running Tests

### Backend Tests

```bash
# Navigate to backend
cd backend

# Activate virtual environment
source .venv/bin/activate

# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ -v --cov=. --cov-report=term-missing

# Run specific test file
pytest tests/test_providers.py -v

# Run tests by marker
pytest tests/ -m "unit" -v           # Fast unit tests
pytest tests/ -m "integration" -v    # Integration tests
pytest tests/ -m "provider" -v       # Provider-specific tests
```

### Frontend Tests

```bash
# From project root
npm run test --workspace=packages/frontend

# Watch mode (rerun on file changes)
npm run test:watch --workspace=packages/frontend

# With coverage report
npm run test:coverage --workspace=packages/frontend

# Run specific test file
npm run test --workspace=packages/frontend -- src/components/__tests__/ChatPage.test.tsx
```

## Test Structure

### Backend Test Organization

```
backend/tests/
├── conftest.py              # Shared fixtures
├── test_auth.py             # Authentication tests
├── test_cache.py            # Cache service tests
├── test_providers.py        # Data provider tests
├── test_query.py            # Query service tests
└── providers/
    ├── test_fred.py
    ├── test_worldbank.py
    └── ...
```

### Frontend Test Organization

```
packages/frontend/src/
├── components/__tests__/
│   ├── ChatPage.test.tsx
│   ├── MessageChart.test.tsx
│   └── ProcessingSteps.test.tsx
├── contexts/__tests__/
│   └── AuthContext.test.tsx
├── hooks/__tests__/
│   └── useMobile.test.ts
├── lib/__tests__/
│   └── export.test.ts
├── services/__tests__/
│   └── api.test.ts
└── test/
    ├── setup.ts             # Test setup and matchers
    ├── test-utils.tsx       # Custom render with providers
    └── mocks/
        └── api.ts           # API mock implementations
```

## Writing Tests

### Backend Tests

#### Using Fixtures

```python
import pytest

@pytest.mark.asyncio
async def test_provider_fetch(mock_http_client, sample_normalized_data):
    """Test that provider fetches and normalizes data correctly."""
    # mock_http_client is automatically injected from conftest.py
    mock_http_client.get.return_value = sample_response

    result = await provider.fetch(query)

    assert result is not None
    assert len(result.data) > 0
```

#### Marking Tests

```python
@pytest.mark.unit
def test_simple_function():
    """Fast, isolated test."""
    pass

@pytest.mark.integration
async def test_with_external_service():
    """May hit external services."""
    pass

@pytest.mark.provider
async def test_specific_provider():
    """Provider-specific behavior."""
    pass
```

### Frontend Tests

#### Using Custom Render

```tsx
import { renderWithProviders, mockAuthenticatedContext } from '../../test/test-utils';

describe('MyComponent', () => {
  it('renders authenticated state', () => {
    renderWithProviders(<MyComponent />, {
      authValue: mockAuthenticatedContext,
    });

    expect(screen.getByText('Welcome')).toBeDefined();
  });
});
```

#### Mocking the API

```tsx
import { mockApi, mockApiError } from '../../test/mocks/api';

vi.mock('../../services/api', () => ({
  api: mockApi,
}));

describe('Component with API', () => {
  it('handles API error', async () => {
    mockApiError('query', 'Network error', 500);

    renderWithProviders(<Component />);

    await waitFor(() => {
      expect(screen.getByText(/error/i)).toBeDefined();
    });
  });
});
```

## CI/CD Integration

Tests run automatically on every push and pull request:

1. **Backend Tests**: Run with pytest, coverage uploaded to Codecov
2. **Frontend Tests**: Run with Vitest, coverage uploaded to Codecov
3. **Linting**: Python (ruff) and TypeScript (ESLint) - blocking
4. **Security Audit**: Dependency vulnerability checks

### Coverage Requirements

- Backend: **70% minimum** (enforced in CI)
- Frontend: **60% minimum** (enforced via thresholds)
- Patch coverage: **80% target** (informational)

## Best Practices

### General

1. **Test behavior, not implementation** - Focus on what the code does, not how
2. **Use descriptive test names** - `it('displays error when API fails')` not `it('test error')`
3. **One assertion per test** when possible
4. **Keep tests independent** - No shared state between tests
5. **Mock external dependencies** - API calls, timers, etc.

### Backend Specific

1. Use async fixtures for database/HTTP operations
2. Mark slow tests with `@pytest.mark.slow`
3. Use parametrize for testing multiple inputs
4. Clean up resources in fixtures (use `yield`)

### Frontend Specific

1. Use `userEvent` over `fireEvent` for user interactions
2. Query by role/label, not test IDs when possible
3. Use `waitFor` for async operations
4. Mock recharts and complex components to avoid canvas issues

## Troubleshooting

### Common Issues

**Backend tests hang**: Check for unclosed async connections

```python
@pytest.fixture
async def client():
    async with AsyncClient() as client:
        yield client
    # Automatically closed
```

**Frontend tests fail with "not wrapped in act()"**: Use `waitFor` or `act`

```tsx
await waitFor(() => {
  expect(screen.getByText('Loaded')).toBeDefined();
});
```

**Coverage not uploading**: Check codecov token in GitHub secrets

### Debugging

```bash
# Backend - verbose output with print statements
pytest tests/ -v -s

# Frontend - debug mode
DEBUG_PRINT_LIMIT=100000 npm run test --workspace=packages/frontend
```

## Resources

- [Pytest Documentation](https://docs.pytest.org/)
- [Vitest Documentation](https://vitest.dev/)
- [React Testing Library](https://testing-library.com/docs/react-testing-library/intro/)
- [Testing Library Queries](https://testing-library.com/docs/queries/about)
