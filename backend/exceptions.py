"""Custom exception hierarchy for econ-data-mcp.

This module provides a standardized exception hierarchy used across
all backend services and agents. Using specific exception types enables:
- More precise error handling
- Better error logging and debugging
- Clearer API error responses
- Consistent error patterns across the codebase

Exception Hierarchy:
    EconDataMcpError (base)
    ├── ConfigurationError
    ├── AuthenticationError
    ├── AuthorizationError
    ├── ValidationError
    ├── DataProviderError
    │   ├── ProviderNotFoundError
    │   ├── ProviderRateLimitError
    │   ├── ProviderTimeoutError
    │   └── DataNotAvailableError
    ├── QueryError
    │   ├── QueryParsingError
    │   └── InvalidQueryError
    ├── ExecutionError
    │   ├── CodeExecutionError
    │   └── TimeoutError
    └── ExternalServiceError
"""
from __future__ import annotations

from typing import Optional, Dict, Any


class EconDataMcpError(Exception):
    """Base exception for all econ-data-mcp errors.

    All custom exceptions should inherit from this class to enable
    catching all econ-data-mcp-specific errors with a single except block.

    Attributes:
        message: Human-readable error message
        code: Error code for programmatic handling
        details: Additional error context
    """

    def __init__(
        self,
        message: str,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.message = message
        self.code = code or self.__class__.__name__
        self.details = details or {}
        super().__init__(message)

    def to_dict(self) -> Dict[str, Any]:
        """Convert exception to dictionary for API responses."""
        return {
            "error": self.code,
            "message": self.message,
            "details": self.details,
        }


# Configuration Errors
class ConfigurationError(EconDataMcpError):
    """Raised when there's a configuration problem.

    Examples:
        - Missing required environment variable
        - Invalid configuration value
        - Missing API key
    """
    pass


# Authentication and Authorization Errors
class AuthenticationError(EconDataMcpError):
    """Raised when authentication fails.

    Examples:
        - Invalid credentials
        - Expired token
        - Missing authentication header
    """
    pass


class AuthorizationError(EconDataMcpError):
    """Raised when user lacks permission.

    Examples:
        - Accessing admin-only endpoint
        - Rate limit exceeded for free tier
    """
    pass


# Validation Errors
class ValidationError(EconDataMcpError):
    """Raised when input validation fails.

    Examples:
        - Invalid parameter format
        - Missing required field
        - Value out of range
    """

    def __init__(
        self,
        message: str,
        field: Optional[str] = None,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.field = field
        details = details or {}
        if field:
            details["field"] = field
        super().__init__(message, code, details)


# Data Provider Errors
class DataProviderError(EconDataMcpError):
    """Base class for data provider errors.

    Attributes:
        provider: Name of the provider that failed
    """

    def __init__(
        self,
        message: str,
        provider: Optional[str] = None,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.provider = provider
        details = details or {}
        if provider:
            details["provider"] = provider
        super().__init__(message, code, details)


class ProviderNotFoundError(DataProviderError):
    """Raised when requested provider doesn't exist."""
    pass


class ProviderRateLimitError(DataProviderError):
    """Raised when provider rate limit is exceeded.

    Attributes:
        retry_after: Seconds to wait before retrying
    """

    def __init__(
        self,
        message: str,
        provider: Optional[str] = None,
        retry_after: Optional[int] = None,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.retry_after = retry_after
        details = details or {}
        if retry_after:
            details["retry_after"] = retry_after
        super().__init__(message, provider, code, details)


class ProviderTimeoutError(DataProviderError):
    """Raised when provider request times out."""
    pass


class DataNotAvailableError(DataProviderError):
    """Raised when requested data is not available.

    This can mean:
        - Indicator doesn't exist for the country/time period
        - Data is behind a paywall
        - Data has been deprecated
    """
    pass


# Query Errors
class QueryError(EconDataMcpError):
    """Base class for query-related errors."""
    pass


class QueryParsingError(QueryError):
    """Raised when query cannot be parsed by LLM."""
    pass


class InvalidQueryError(QueryError):
    """Raised when query is invalid or unsupported."""
    pass


# Execution Errors
class ExecutionError(EconDataMcpError):
    """Base class for code execution errors."""
    pass


class CodeExecutionError(ExecutionError):
    """Raised when Pro Mode code execution fails.

    Attributes:
        code: The code that failed
        stdout: Standard output before failure
        stderr: Standard error output
    """

    def __init__(
        self,
        message: str,
        code: Optional[str] = None,
        stdout: Optional[str] = None,
        stderr: Optional[str] = None,
        error_code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.code_content = code
        self.stdout = stdout
        self.stderr = stderr
        details = details or {}
        if stdout:
            details["stdout"] = stdout
        if stderr:
            details["stderr"] = stderr
        super().__init__(message, error_code, details)


class ExecutionTimeoutError(ExecutionError):
    """Raised when code execution times out.

    Attributes:
        timeout: The timeout value that was exceeded
    """

    def __init__(
        self,
        message: str,
        timeout: Optional[float] = None,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.timeout = timeout
        details = details or {}
        if timeout:
            details["timeout"] = timeout
        super().__init__(message, code, details)


# External Service Errors
class ExternalServiceError(EconDataMcpError):
    """Raised when an external service (LLM, Supabase, etc.) fails.

    Attributes:
        service: Name of the external service
    """

    def __init__(
        self,
        message: str,
        service: Optional[str] = None,
        code: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.service = service
        details = details or {}
        if service:
            details["service"] = service
        super().__init__(message, code, details)


# Convenience functions for error handling
def is_retryable_error(error: Exception) -> bool:
    """Check if an error is retryable.

    Args:
        error: The exception to check

    Returns:
        True if the error is temporary and can be retried
    """
    retryable_types = (
        ProviderRateLimitError,
        ProviderTimeoutError,
        ExecutionTimeoutError,
    )
    return isinstance(error, retryable_types)


def get_error_response(error: Exception) -> Dict[str, Any]:
    """Convert any exception to an API error response.

    Args:
        error: The exception to convert

    Returns:
        Dictionary suitable for API error response
    """
    if isinstance(error, EconDataMcpError):
        return error.to_dict()

    # For non-econ-data-mcp exceptions, create a generic response
    return {
        "error": "InternalError",
        "message": str(error),
        "details": {},
    }
