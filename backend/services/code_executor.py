"""
Python code execution service with security-hardened execution.

This module provides a secure wrapper around the AST-based code executor.
It maintains backward compatibility with the existing CodeExecutor interface
while using the more secure SecureCodeExecutor implementation internally.

Security improvements:
- AST-based validation instead of regex (prevents obfuscation bypasses)
- Multi-layer security checks (imports, builtins, file operations, attributes)
- Resource limits (CPU, memory, file descriptors)
- Session isolation
- JSON-based session storage (no pickle vulnerabilities)
- Timeout enforcement
- Output limiting
"""
import asyncio
import logging
import time
from pathlib import Path
from typing import Optional, List

from backend.models import CodeExecutionResult, GeneratedFile
from backend.services.secure_code_executor import (
    SecureCodeExecutor,
    SecurityLevel,
    get_secure_code_executor
)

logger = logging.getLogger(__name__)

# Default security level for backward compatibility
DEFAULT_SECURITY_LEVEL = SecurityLevel.STRICT

# Safety limits
MAX_EXECUTION_TIME = 90  # seconds
MAX_OUTPUT_SIZE = 100000  # characters


class CodeExecutor:
    """
    Secure Python code executor with comprehensive safety controls.

    This class wraps SecureCodeExecutor to maintain backward compatibility
    with the existing interface while providing enhanced security.
    """

    def __init__(self, security_level: SecurityLevel = DEFAULT_SECURITY_LEVEL):
        """
        Initialize code executor with security level.

        Args:
            security_level: Security level for execution (STRICT, MODERATE, RELAXED)
        """
        self.security_level = security_level
        self.timeout = MAX_EXECUTION_TIME
        self.max_output = MAX_OUTPUT_SIZE
        self._secure_executor = SecureCodeExecutor(security_level)

        logger.info(f"CodeExecutor initialized with {security_level.value} security")

    async def execute_code(
        self,
        code: str,
        session_id: Optional[str] = None
    ) -> CodeExecutionResult:
        """
        Execute Python code in a restricted environment.

        Args:
            code: Python code to execute
            session_id: Optional session ID for data persistence

        Returns:
            CodeExecutionResult with output or error
        """
        if not session_id:
            session_id = "anonymous"

        start_time = time.perf_counter()

        try:
            logger.info(f"Executing code for session {session_id}")

            # Use secure executor to run the code
            result = await self._secure_executor.execute_code(
                code=code,
                session_id=session_id,
                timeout=self.timeout,
                max_output_size=self.max_output
            )

            execution_time = time.perf_counter() - start_time

            # Convert result to CodeExecutionResult format
            if result.get("success"):
                # Convert file dicts to GeneratedFile objects
                files = None
                raw_files = result.get("files")
                if raw_files:
                    files = []
                    for f in raw_files:
                        # Skip malformed entries missing required fields
                        if not f.get("url") or not f.get("name"):
                            logger.warning(f"Skipping malformed file entry: {f}")
                            continue
                        files.append(GeneratedFile(
                            url=f["url"],
                            name=f["name"],
                            type=f.get("type", "file")
                        ))
                    files = files if files else None  # Set to None if empty after filtering
                return CodeExecutionResult(
                    code=code,
                    output=result.get("output", "(No output produced)"),
                    executionTime=execution_time,
                    files=files
                )
            else:
                return CodeExecutionResult(
                    code=code,
                    output=result.get("output", ""),
                    error=result.get("error", "Unknown error"),
                    executionTime=execution_time
                )

        except asyncio.CancelledError:
            # Re-raise cancellation for proper async handling
            raise
        except Exception as e:
            execution_time = time.perf_counter() - start_time
            logger.error(f"Code execution failed for session {session_id}: {str(e)}")
            return CodeExecutionResult(
                code=code,
                output="",
                error=str(e),
                executionTime=execution_time
            )

    def cleanup_old_files(self, max_age_hours: int = 24) -> int:
        """
        Clean up old generated files and sessions.

        Args:
            max_age_hours: Maximum age in hours before cleanup

        Returns:
            Number of items deleted
        """
        deleted_count = self._secure_executor.cleanup_old_sessions(max_age_hours)
        logger.info(f"Cleanup completed: deleted {deleted_count} old session(s)")
        return deleted_count


# Singleton instance
_code_executor: Optional[CodeExecutor] = None


def get_code_executor(
    security_level: SecurityLevel = DEFAULT_SECURITY_LEVEL
) -> CodeExecutor:
    """
    Get or create CodeExecutor singleton.

    Args:
        security_level: Security level for the executor

    Returns:
        CodeExecutor instance
    """
    global _code_executor
    if _code_executor is None:
        _code_executor = CodeExecutor(security_level)
    return _code_executor
