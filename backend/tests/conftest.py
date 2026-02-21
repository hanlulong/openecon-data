"""
Shared pytest fixtures for econ-data-mcp backend tests.

This module provides common fixtures used across all test modules.
Import fixtures from here instead of defining them in individual test files.
"""
from __future__ import annotations

import os
import pytest
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

# Set test environment before importing application modules
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DISABLE_MCP", "1")
os.environ.setdefault("DISABLE_BACKGROUND_JOBS", "1")
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-testing")


# ============================================================================
# Environment Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def test_environment():
    """Ensure test environment is set for all tests."""
    old_env = os.environ.copy()
    os.environ["ENVIRONMENT"] = "test"
    os.environ["DISABLE_MCP"] = "1"
    os.environ["DISABLE_BACKGROUND_JOBS"] = "1"
    yield
    os.environ.clear()
    os.environ.update(old_env)


@pytest.fixture
def no_supabase_env():
    """Fixture to temporarily disable Supabase environment variables."""
    keys_to_remove = ["SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_ROLE_KEY"]
    old_values = {}
    for key in keys_to_remove:
        if key in os.environ:
            old_values[key] = os.environ.pop(key)
    yield
    os.environ.update(old_values)


# ============================================================================
# Data Fixtures
# ============================================================================

@pytest.fixture
def sample_data_points() -> List[Dict[str, Any]]:
    """Sample data points for testing."""
    return [
        {"date": "2023-01-01", "value": 100.0},
        {"date": "2023-02-01", "value": 105.2},
        {"date": "2023-03-01", "value": 103.8},
        {"date": "2023-04-01", "value": 108.1},
        {"date": "2023-05-01", "value": 110.5},
    ]


@pytest.fixture
def sample_metadata() -> Dict[str, Any]:
    """Sample metadata for testing."""
    return {
        "source": "FRED",
        "indicator": "Gross Domestic Product",
        "country": "US",
        "frequency": "quarterly",
        "unit": "Billions of Dollars",
        "lastUpdated": datetime.now().isoformat(),
        "seriesId": "GDP",
        "seasonalAdjustment": "Seasonally Adjusted Annual Rate",
        "dataType": "Level",
        "description": "Gross Domestic Product",
    }


@pytest.fixture
def sample_normalized_data(sample_data_points, sample_metadata) -> Dict[str, Any]:
    """Sample NormalizedData structure for testing."""
    return {
        "metadata": sample_metadata,
        "data": sample_data_points,
    }


@pytest.fixture
def sample_parsed_intent() -> Dict[str, Any]:
    """Sample ParsedIntent structure for testing."""
    return {
        "apiProvider": "FRED",
        "indicators": ["GDP"],
        "parameters": {
            "country": "US",
            "startDate": "2023-01-01",
            "endDate": "2023-12-31",
        },
        "clarificationNeeded": False,
        "clarificationQuestions": None,
        "confidence": 0.95,
        "recommendedChartType": "line",
        "originalQuery": "Show me US GDP",
        "needsDecomposition": False,
        "decompositionType": None,
        "decompositionEntities": None,
        "useProMode": False,
    }


# ============================================================================
# Service Mocks
# ============================================================================

@pytest.fixture
def mock_cache():
    """Mock cache service that returns None (cache miss)."""
    with patch("backend.services.cache.cache_service") as mock:
        mock.get.return_value = None
        mock.set.return_value = None
        mock.clear.return_value = None
        yield mock


@pytest.fixture
def mock_http_client():
    """Mock HTTP client for provider tests."""
    mock_client = AsyncMock()
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_client.get.return_value = mock_response
    return mock_client


@pytest.fixture
def mock_openrouter_response():
    """Mock OpenRouter API response for LLM tests."""
    return {
        "choices": [
            {
                "message": {
                    "content": '{"apiProvider": "FRED", "indicators": ["GDP"], "parameters": {"country": "US"}}'
                }
            }
        ]
    }


# ============================================================================
# Auth Fixtures
# ============================================================================

@pytest.fixture
def test_user() -> Dict[str, Any]:
    """Sample test user data."""
    return {
        "id": "test-user-123",
        "email": "test@example.com",
        "created_at": datetime.now().isoformat(),
    }


@pytest.fixture
def auth_token() -> str:
    """Generate a test JWT token."""
    import jwt
    payload = {
        "sub": "test-user-123",
        "email": "test@example.com",
        "exp": datetime.now().timestamp() + 3600,
    }
    return jwt.encode(payload, os.environ.get("JWT_SECRET", "test-secret"), algorithm="HS256")


@pytest.fixture
def auth_headers(auth_token) -> Dict[str, str]:
    """HTTP headers with authorization token."""
    return {"Authorization": f"Bearer {auth_token}"}


# ============================================================================
# Provider Fixtures
# ============================================================================

@pytest.fixture
def fred_sample_response() -> Dict[str, Any]:
    """Sample FRED API response."""
    return {
        "observations": [
            {"date": "2023-01-01", "value": "26000.0"},
            {"date": "2023-04-01", "value": "26500.0"},
            {"date": "2023-07-01", "value": "27000.0"},
            {"date": "2023-10-01", "value": "27500.0"},
        ],
        "realtime_start": "2023-01-01",
        "realtime_end": "2023-12-31",
        "observation_start": "2023-01-01",
        "observation_end": "2023-12-31",
        "units": "Billions of Dollars",
        "output_type": 1,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "count": 4,
        "offset": 0,
        "limit": 100000,
    }


@pytest.fixture
def worldbank_sample_response() -> List[Any]:
    """Sample World Bank API response."""
    return [
        {"page": 1, "pages": 1, "per_page": 50, "total": 4},
        [
            {"date": "2023", "value": 25000000000000, "indicator": {"id": "NY.GDP.MKTP.CD"}},
            {"date": "2022", "value": 24000000000000, "indicator": {"id": "NY.GDP.MKTP.CD"}},
            {"date": "2021", "value": 23000000000000, "indicator": {"id": "NY.GDP.MKTP.CD"}},
            {"date": "2020", "value": 21000000000000, "indicator": {"id": "NY.GDP.MKTP.CD"}},
        ],
    ]


@pytest.fixture
def imf_sample_response() -> Dict[str, Any]:
    """Sample IMF API response."""
    return {
        "CompactData": {
            "DataSet": {
                "Series": {
                    "@REF_AREA": "US",
                    "@INDICATOR": "NGDP_RPCH",
                    "Obs": [
                        {"@TIME_PERIOD": "2023", "@OBS_VALUE": "2.5"},
                        {"@TIME_PERIOD": "2022", "@OBS_VALUE": "2.1"},
                        {"@TIME_PERIOD": "2021", "@OBS_VALUE": "5.9"},
                    ],
                }
            }
        }
    }


# ============================================================================
# Cleanup Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset singleton services between tests."""
    yield
    # Reset auth service singleton
    try:
        import backend.services.auth_factory as auth_factory
        auth_factory._auth_service = None
    except ImportError:
        pass

    # Clear user store
    try:
        from backend.services.user_store import user_store
        user_store.clear()
    except ImportError:
        pass

    # Clear cache
    try:
        from backend.services.cache import cache_service
        cache_service.clear()
    except ImportError:
        pass
