# Tests

Integration test suites for the econ-data-mcp project.

## Main Test Suites

### Comprehensive Testing

- **test_all_providers_comprehensive.py** - Complete test suite for all 11 providers (20 queries per provider)
- **test_all_providers_production.py** - Production testing with data value verification
- **test_all_providers_quick.py** - Quick smoke tests (5 queries per provider)
- **comprehensive_test_suite_100.py** - Extended test suite with 100 complex queries

### Provider-Specific Tests

- **test_bis_fixes.py** - BIS (Bank for International Settlements) provider tests
- **test_eurostat_comprehensive.py** - Eurostat provider tests
- **test_imf_comprehensive.py** - IMF (International Monetary Fund) provider tests
- **test_oecd_comprehensive.py** - OECD provider tests
- **test_statscan_comprehensive.py** - Statistics Canada provider tests

## Running Tests

```bash
# Activate virtual environment
source backend/.venv/bin/activate

# Run comprehensive tests (all providers, 20 queries each)
python3 tests/test_all_providers_comprehensive.py

# Run quick tests (all providers, 5 queries each)
python3 tests/test_all_providers_quick.py

# Run production tests with value verification
python3 tests/test_all_providers_production.py

# Run extended test suite (100 complex queries)
python3 tests/comprehensive_test_suite_100.py

# Run provider-specific tests
python3 tests/test_bis_fixes.py
python3 tests/test_eurostat_comprehensive.py
python3 tests/test_imf_comprehensive.py
python3 tests/test_oecd_comprehensive.py
python3 tests/test_statscan_comprehensive.py
```

## Test Structure

All test scripts:
- Test against the local API (http://localhost:3001) by default
- Some scripts support testing production (https://openecon.ai)
- Save results to JSON files in `scripts/test_results/`
- Report success rate and provide detailed error information

## Backend Unit Tests

Backend unit tests are located in `backend/tests/` and use pytest:

```bash
cd backend
pytest tests/
```

See [docs/guides/testing.md](../docs/guides/testing.md) for complete testing documentation.
