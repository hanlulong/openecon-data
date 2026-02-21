"""Factory for creating the appropriate auth service based on configuration."""
from __future__ import annotations

import logging
from typing import Union

from ..config import get_settings

logger = logging.getLogger(__name__)


def get_auth_service() -> Union[object]:
    """Get the appropriate auth service based on configuration.

    Returns:
        - MockAuthService if Supabase is not configured (dev/test mode only)
        - SupabaseAuthService if Supabase is configured (production mode)

    This factory ensures the same interface is used regardless of which
    auth service is active, allowing seamless switching without code changes.

    Raises:
        RuntimeError: If running in production without Supabase configured
    """
    settings = get_settings()

    if settings.supabase_enabled:
        # Production mode: use Supabase
        from .supabase_service import SupabaseAuthService
        logger.info("✅ Using Supabase authentication")
        return SupabaseAuthService()
    else:
        # SECURITY: Fail closed in production - require Supabase
        if settings.environment == "production":
            raise RuntimeError(
                "SECURITY ERROR: Supabase must be configured in production. "
                "Set SUPABASE_URL, SUPABASE_ANON_KEY, and SUPABASE_SERVICE_KEY "
                "environment variables, or set NODE_ENV=development for local testing."
            )
        # Development/test mode: use mock auth
        from .mock_auth import MockAuthService
        logger.info("⚠️  Using mock authentication (development mode only)")
        return MockAuthService()


# Singleton instance
_auth_service: Union[object, None] = None


def get_auth_service_singleton():
    """Get or create the auth service singleton.

    The type of service (Supabase or Mock) is determined by configuration.
    """
    global _auth_service
    if _auth_service is None:
        _auth_service = get_auth_service()
    return _auth_service
