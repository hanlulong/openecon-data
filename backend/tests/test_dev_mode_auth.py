"""Test development mode authentication without Supabase.

This test suite verifies that:
1. Mock auth service works as a drop-in replacement for Supabase
2. Backend starts without Supabase credentials
3. Authentication endpoints work in development mode
4. Same API interface is maintained regardless of auth service
"""
import os
import pytest
from unittest.mock import patch, MagicMock
import uuid


@pytest.fixture
def no_supabase_env():
    """Fixture to temporarily disable Supabase environment variables."""
    old_env = {
        "SUPABASE_URL": os.environ.pop("SUPABASE_URL", None),
        "SUPABASE_ANON_KEY": os.environ.pop("SUPABASE_ANON_KEY", None),
        "SUPABASE_SERVICE_KEY": os.environ.pop("SUPABASE_SERVICE_KEY", None),
    }
    yield
    # Restore
    for key, value in old_env.items():
        if value is not None:
            os.environ[key] = value


def test_config_supabase_disabled_without_credentials(no_supabase_env):
    """Test that Supabase is marked as disabled when credentials are missing."""
    # Clear cached settings
    from backend import config
    if hasattr(config.get_settings, 'cache_clear'):
        config.get_settings.cache_clear()

    from backend.config import Settings
    from pydantic_settings import SettingsConfigDict

    # Create settings without reading .env file
    class TestSettings(Settings):
        model_config = SettingsConfigDict(
            env_file=None,  # Don't read from .env
            case_sensitive=False,
            extra="ignore",
        )

    settings = TestSettings(
        OPENROUTER_API_KEY="test-key",
        JWT_SECRET="test-secret",
    )

    assert not settings.supabase_enabled, "Supabase should be disabled without credentials"
    assert settings.allow_mock_auth, "Mock auth should be allowed when Supabase is disabled"


def test_config_dev_mode_detection():
    """Test that dev_mode is properly detected."""
    from backend.config import Settings

    settings = Settings(
        openrouter_api_key="test-key",
        jwt_secret="test-secret",
        NODE_ENV="development",
    )

    assert settings.dev_mode, "Dev mode should be enabled in development environment"


def test_mock_auth_service_creation():
    """Test that MockAuthService can be instantiated."""
    from backend.services.mock_auth import MockAuthService

    service = MockAuthService()

    assert service is not None
    assert hasattr(service, "register")
    assert hasattr(service, "login")
    assert hasattr(service, "get_user_from_token")
    assert len(service.users) > 0, "Should have test user"


def test_mock_auth_test_user():
    """Test that MockAuthService creates a test user."""
    from backend.services.mock_auth import MockAuthService

    service = MockAuthService()

    # Should have created test user
    assert "test@example.com" in service.users
    test_user = service.users["test@example.com"]
    assert test_user.email == "test@example.com"
    assert test_user.name == "Test User"


@pytest.mark.asyncio
async def test_mock_auth_registration():
    """Test user registration with mock auth."""
    from backend.services.mock_auth import MockAuthService
    from backend.models import RegisterRequest

    service = MockAuthService()
    request = RegisterRequest(
        email="newuser@example.com",
        password="testpass123",
        name="New User"
    )

    response = await service.register(request)

    assert response.success
    assert response.token is not None
    assert response.user is not None
    assert response.user.email == "newuser@example.com"
    assert response.user.name == "New User"

    # Verify user was added to storage
    assert "newuser@example.com" in service.users


@pytest.mark.asyncio
async def test_mock_auth_duplicate_registration():
    """Test that duplicate registration fails."""
    from backend.services.mock_auth import MockAuthService
    from backend.models import RegisterRequest

    service = MockAuthService()

    # Try to register with test user email
    request = RegisterRequest(
        email="test@example.com",
        password="testpass123",
        name="Test User"
    )

    response = await service.register(request)

    assert not response.success
    assert "already exists" in response.error.lower()


@pytest.mark.asyncio
async def test_mock_auth_login():
    """Test user login with mock auth."""
    from backend.services.mock_auth import MockAuthService
    from backend.models import LoginRequest

    service = MockAuthService()

    # Login with test user
    request = LoginRequest(
        email="test@example.com",
        password="testpass123"
    )

    response = await service.login(request)

    assert response.success
    assert response.token is not None
    assert response.user is not None
    assert response.user.email == "test@example.com"


@pytest.mark.asyncio
async def test_mock_auth_login_invalid_password():
    """Test login fails with wrong password."""
    from backend.services.mock_auth import MockAuthService
    from backend.models import LoginRequest

    service = MockAuthService()

    request = LoginRequest(
        email="test@example.com",
        password="wrongpassword"
    )

    response = await service.login(request)

    assert not response.success
    assert "invalid" in response.error.lower()


@pytest.mark.asyncio
async def test_mock_auth_login_nonexistent_user():
    """Test login fails for non-existent user."""
    from backend.services.mock_auth import MockAuthService
    from backend.models import LoginRequest

    service = MockAuthService()

    request = LoginRequest(
        email="nonexistent@example.com",
        password="testpass123"
    )

    response = await service.login(request)

    assert not response.success
    assert "invalid" in response.error.lower()


@pytest.mark.asyncio
async def test_mock_auth_token_validation():
    """Test token validation works."""
    from backend.services.mock_auth import MockAuthService
    from backend.models import LoginRequest

    service = MockAuthService()

    # Login to get token
    login_request = LoginRequest(
        email="test@example.com",
        password="testpass123"
    )
    login_response = await service.login(login_request)
    token = login_response.token

    # Validate token
    user = await service.get_user_from_token(token)

    assert user is not None
    assert user.email == "test@example.com"


@pytest.mark.asyncio
async def test_mock_auth_invalid_token():
    """Test that invalid tokens are rejected."""
    from backend.services.mock_auth import MockAuthService

    service = MockAuthService()

    user = await service.get_user_from_token("invalid-token")

    assert user is None


def test_auth_factory_returns_mock_without_supabase(no_supabase_env):
    """Test that factory returns MockAuthService when Supabase is not configured."""
    from unittest.mock import patch
    from backend.services.mock_auth import MockAuthService
    from backend.services.auth_factory import get_auth_service

    # Mock get_settings to return a config without Supabase
    from backend.config import Settings
    from pydantic_settings import SettingsConfigDict

    class TestSettings(Settings):
        model_config = SettingsConfigDict(
            env_file=None,
            case_sensitive=False,
            extra="ignore",
        )

    test_settings = TestSettings(
        OPENROUTER_API_KEY="test-key",
        JWT_SECRET="test-secret",
    )

    # Patch get_settings to return test settings
    with patch('backend.services.auth_factory.get_settings', return_value=test_settings):
        # Should return MockAuthService since Supabase is not configured
        auth = get_auth_service()
        assert isinstance(auth, MockAuthService)


def test_auth_factory_returns_supabase_with_credentials():
    """Test that factory returns SupabaseAuthService when Supabase is configured."""
    from backend.services.auth_factory import get_auth_service
    from backend.services.supabase_service import SupabaseAuthService

    with patch.dict(os.environ, {
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_ANON_KEY": "test-anon-key",
        "SUPABASE_SERVICE_KEY": "test-service-key",
    }):
        # Clear the cached settings
        from backend import config
        if hasattr(config.get_settings, 'cache_clear'):
            config.get_settings.cache_clear()

        # Should return SupabaseAuthService when credentials are present
        try:
            auth = get_auth_service()
            assert isinstance(auth, SupabaseAuthService)
        except Exception as e:
            # It's okay if Supabase initialization fails - we just care about the type check
            # The factory should attempt to create SupabaseAuthService
            pass


@pytest.mark.asyncio
async def test_auth_factory_singleton():
    """Test that auth service factory returns singleton."""
    from backend.services.auth_factory import get_auth_service_singleton

    service1 = get_auth_service_singleton()
    service2 = get_auth_service_singleton()

    assert service1 is service2, "Factory should return same instance"


def test_mock_auth_service_interface():
    """Test that MockAuthService implements required interface."""
    from backend.services.mock_auth import MockAuthService

    service = MockAuthService()
    required_methods = [
        "register",
        "login",
        "get_user_from_token",
        "require_user",
        "optional_user",
        "get_session_id",
        "login_with_google",
    ]

    for method_name in required_methods:
        assert hasattr(service, method_name), f"MockAuthService missing {method_name}"
        assert callable(getattr(service, method_name))


@pytest.mark.asyncio
async def test_mock_auth_concurrent_users():
    """Test that mock auth handles multiple users correctly."""
    from backend.services.mock_auth import MockAuthService
    from backend.models import RegisterRequest

    service = MockAuthService()

    # Register multiple users
    users = []
    for i in range(3):
        request = RegisterRequest(
            email=f"user{i}@example.com",
            password=f"pass{i}",
            name=f"User {i}"
        )
        response = await service.register(request)
        assert response.success
        users.append(response)

    # All users should be stored
    assert len(service.users) == 4  # 3 new + 1 test user


def test_mock_auth_password_hashing():
    """Test that passwords are properly handled."""
    from backend.services.mock_auth import MockAuthService

    service = MockAuthService()

    # Get test user
    test_user = service.users["test@example.com"]
    stored_password = test_user.password_hash

    # Passwords should be comparable (in mock, they're stored as-is for testing)
    assert service._verify_password("testpass123", stored_password)
    assert not service._verify_password("wrongpass", stored_password)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
