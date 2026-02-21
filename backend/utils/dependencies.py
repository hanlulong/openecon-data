"""FastAPI dependency functions for request validation and authorization."""

import logging
from fastapi import Depends, HTTPException, status

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)


async def require_promode(settings: Settings = Depends(get_settings)):
    """
    Dependency that ensures Pro Mode is enabled.

    Raises HTTPException(403) if Pro Mode is disabled.
    Use this to protect Pro Mode endpoints that execute AI-generated code.

    Usage:
        @app.post("/api/query/pro", dependencies=[Depends(require_promode)])
        async def query_pro(...):
            # Pro Mode logic
    """
    if not settings.promode_enabled:
        logger.warning(
            "⚠️ Pro Mode access attempt while disabled - endpoint protected"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "Pro Mode is currently disabled",
                "reason": "Pro Mode requires additional security configuration and sandboxing",
                "message": "Pro Mode code execution is disabled by the administrator for security reasons",
                "docs": "https://github.com/anthropics/openecon/blob/main/docs/fix-plan.md#1-pro-mode-security-p0---critical"
            }
        )

    # If we get here, Pro Mode is enabled
    logger.debug("✅ Pro Mode access granted - feature enabled")
