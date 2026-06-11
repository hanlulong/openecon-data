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

    # FAIL-CLOSED sandbox health gate. Even when Pro Mode is enabled, refuse to run
    # untrusted code unless the namespace jail self-test passes. This makes a broken
    # bind-mount set (e.g. after a venv/python bump) a loud 503 instead of silently
    # executing code with degraded isolation. The canary result is cached per-process.
    try:
        from ..services.secure_code_executor import get_secure_code_executor
        executor = get_secure_code_executor()
        sandbox_ok = await executor.canary_self_test()
    except Exception as e:
        logger.error(f"Pro Mode sandbox canary errored - failing closed: {e}")
        sandbox_ok = False

    if not sandbox_ok:
        logger.error("⛔ Pro Mode sandbox canary FAILED - refusing code execution (fail-closed)")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "Pro Mode sandbox unavailable",
                "reason": "The code-execution sandbox failed its self-test",
                "message": "Pro Mode is temporarily unavailable due to a sandbox health check failure. An administrator has been alerted.",
            }
        )

    # If we get here, Pro Mode is enabled
    logger.debug("✅ Pro Mode access granted - feature enabled")
