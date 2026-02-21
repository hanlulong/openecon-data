"""
Secure logging utilities with PII and credential redaction
Created: 2025-11-20
Purpose: Prevent sensitive data leakage in logs
"""
import uuid
import re
import json
import hashlib
from typing import Dict, Any, Set, Optional, List, Tuple, TYPE_CHECKING
import logging

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger(__name__)


class SecureLogger:
    """
    Secure request/response logger with automatic redaction

    Features:
    - Redacts sensitive headers (auth tokens, API keys)
    - Redacts sensitive parameters (passwords, secrets)
    - Detects and removes PII patterns
    - Generates request IDs for tracing
    - Configurable redaction rules
    """

    # Headers that should ALWAYS be redacted
    SENSITIVE_HEADERS: Set[str] = {
        'authorization',
        'x-authorization',
        'cookie',
        'set-cookie',
        'x-api-key',
        'api-key',
        'x-auth-token',
        'x-access-token',
        'x-refresh-token',
        'x-csrf-token',
        'x-forwarded-for',
        'x-real-ip',
        'proxy-authorization',
        'www-authenticate',
    }

    # Query parameters that should be redacted
    SENSITIVE_PARAMS: Set[str] = {
        'password',
        'pass',
        'pwd',
        'secret',
        'token',
        'api_key',
        'apikey',
        'access_token',
        'refresh_token',
        'client_secret',
        'private_key',
        'auth',
        'credentials',
        'session',
        'sessionid',
    }

    # Patterns that might contain PII
    PII_PATTERNS: List[Tuple[re.Pattern, str]] = [
        # Email addresses
        (re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'), '[EMAIL_REDACTED]'),
        # US Social Security Numbers
        (re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), '[SSN_REDACTED]'),
        # Credit card numbers (basic pattern)
        (re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'), '[CARD_REDACTED]'),
        # Phone numbers (US format)
        (re.compile(r'\b(\+?1[\s-]?)?\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{4}\b'), '[PHONE_REDACTED]'),
        # IPv4 addresses (optional - uncomment if needed)
        # (re.compile(r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b'), '[IP_REDACTED]'),
        # JWT tokens
        (re.compile(r'eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'), '[JWT_REDACTED]'),
        # API keys (common patterns) - sk_test_, sk_live_, pk_, api_, etc.
        (re.compile(r'(sk|pk|api)_[A-Za-z0-9_-]{20,}'), '[API_KEY_REDACTED]'),
    ]

    @classmethod
    def generate_request_id(cls) -> str:
        """
        Generate unique request ID for distributed tracing
        Format: req_[timestamp]_[random]
        """
        import time
        timestamp = int(time.time() * 1000)
        random_part = uuid.uuid4().hex[:8]
        return f"req_{timestamp}_{random_part}"

    @classmethod
    def sanitize_headers(cls, headers: Dict[str, Any]) -> Dict[str, Any]:
        """
        Remove or redact sensitive headers

        Args:
            headers: Original headers dictionary

        Returns:
            Sanitized headers safe for logging
        """
        if not headers:
            return {}

        sanitized = {}
        for key, value in headers.items():
            key_lower = key.lower().strip()

            # Check if header is sensitive
            if key_lower in cls.SENSITIVE_HEADERS:
                # For auth headers, show type but not value
                if key_lower == 'authorization' and value:
                    # Extract auth type (Bearer, Basic, etc)
                    parts = str(value).split(' ', 1)
                    if len(parts) == 2:
                        sanitized[key] = f"{parts[0]} [REDACTED]"
                    else:
                        sanitized[key] = '[REDACTED]'
                else:
                    sanitized[key] = '[REDACTED]'
            else:
                # Check for sensitive patterns in header value
                sanitized[key] = cls.redact_pii(str(value))

        return sanitized

    @classmethod
    def sanitize_params(cls, params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Redact sensitive query parameters

        Args:
            params: Query parameters

        Returns:
            Sanitized parameters
        """
        if not params:
            return {}

        sanitized = {}
        for key, value in params.items():
            key_lower = key.lower().strip()

            # Check if parameter name suggests sensitivity
            if any(sensitive in key_lower for sensitive in cls.SENSITIVE_PARAMS):
                sanitized[key] = '[REDACTED]'
            else:
                # Still check value for PII patterns
                if isinstance(value, str):
                    sanitized[key] = cls.redact_pii(value)
                else:
                    sanitized[key] = value

        return sanitized

    @classmethod
    def redact_pii(cls, text: str, max_length: int = 1000) -> str:
        """
        Redact PII patterns from text

        Args:
            text: Text to redact
            max_length: Truncate if longer than this

        Returns:
            Redacted text
        """
        if not text:
            return text

        # Truncate very long strings
        if len(text) > max_length:
            text = text[:max_length] + '...[TRUNCATED]'

        # Apply PII pattern redaction
        for pattern, replacement in cls.PII_PATTERNS:
            text = pattern.sub(replacement, text)

        return text

    @classmethod
    def sanitize_body(cls, body: Any, max_depth: int = 3, current_depth: int = 0) -> Any:
        """
        Recursively sanitize request/response body

        Args:
            body: Body content (dict, list, or primitive)
            max_depth: Maximum recursion depth
            current_depth: Current recursion level

        Returns:
            Sanitized body
        """
        if current_depth >= max_depth:
            return "[MAX_DEPTH_REACHED]"

        if isinstance(body, dict):
            sanitized = {}
            for key, value in body.items():
                key_lower = str(key).lower()

                # Check if key suggests sensitive data
                if any(s in key_lower for s in cls.SENSITIVE_PARAMS):
                    sanitized[key] = '[REDACTED]'
                else:
                    sanitized[key] = cls.sanitize_body(value, max_depth, current_depth + 1)
            return sanitized

        elif isinstance(body, list):
            # Process lists but limit size
            if len(body) > 100:
                return f"[LIST_TOO_LONG: {len(body)} items]"
            return [cls.sanitize_body(item, max_depth, current_depth + 1) for item in body]

        elif isinstance(body, str):
            return cls.redact_pii(body, max_length=500)

        else:
            # Primitives (int, float, bool, None)
            return body

    @classmethod
    def format_request_log(
        cls,
        request: 'Request',
        request_id: str,
        include_headers: bool = False
    ) -> Dict[str, Any]:
        """
        Format request for secure logging

        Args:
            request: FastAPI request object
            request_id: Unique request identifier
            include_headers: Whether to include sanitized headers

        Returns:
            Log-safe request summary
        """
        log_data = {
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "query_params": cls.sanitize_params(dict(request.query_params)),
        }

        # Add client info (be careful with IP addresses)
        if request.client:
            # Hash IP for privacy while maintaining uniqueness for debugging
            ip_hash = hashlib.sha256(request.client.host.encode()).hexdigest()[:8]
            log_data["client_hash"] = ip_hash

        # Optionally include sanitized headers
        if include_headers:
            log_data["headers"] = cls.sanitize_headers(dict(request.headers))

        # User agent is generally safe and useful
        log_data["user_agent"] = request.headers.get("user-agent", "unknown")[:200]

        return log_data

    @classmethod
    def format_response_log(
        cls,
        request_id: str,
        status_code: int,
        duration_ms: float,
        response_size: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Format response for secure logging

        Args:
            request_id: Request identifier for correlation
            status_code: HTTP status code
            duration_ms: Request processing time
            response_size: Optional response body size

        Returns:
            Log-safe response summary
        """
        log_data = {
            "request_id": request_id,
            "status_code": status_code,
            "duration_ms": round(duration_ms, 2),
        }

        if response_size is not None:
            log_data["response_size_bytes"] = response_size

        # Add status category for easier filtering
        if 200 <= status_code < 300:
            log_data["status_category"] = "success"
        elif 300 <= status_code < 400:
            log_data["status_category"] = "redirect"
        elif 400 <= status_code < 500:
            log_data["status_category"] = "client_error"
        elif 500 <= status_code < 600:
            log_data["status_category"] = "server_error"
        else:
            log_data["status_category"] = "unknown"

        return log_data

    @classmethod
    def format_error_log(
        cls,
        request_id: str,
        error: Exception,
        include_traceback: bool = False
    ) -> Dict[str, Any]:
        """
        Format error for secure logging

        Args:
            request_id: Request identifier
            error: Exception object
            include_traceback: Whether to include stack trace

        Returns:
            Log-safe error summary
        """
        import traceback

        log_data = {
            "request_id": request_id,
            "error_type": type(error).__name__,
            "error_message": cls.redact_pii(str(error)),
        }

        if include_traceback:
            # Sanitize traceback to remove sensitive file paths
            tb = traceback.format_exc()
            # Replace absolute paths with relative ones
            tb = re.sub(r'/home/[^/]+/', '~/', tb)
            tb = re.sub(r'C:\\Users\\[^\\]+\\', 'C:\\Users\\***\\', tb)
            log_data["traceback"] = tb

        return log_data


# Helper function for structured logging
def log_secure(level: str, message: str, data: Dict[str, Any], request_id: Optional[str] = None):
    """
    Helper for consistent structured logging

    Args:
        level: Log level (info, warning, error)
        message: Log message
        data: Structured data to log
        request_id: Optional request ID for correlation
    """
    if request_id:
        data["request_id"] = request_id

    # Convert to JSON for structured logging
    log_entry = {
        "message": message,
        "data": data
    }

    log_json = json.dumps(log_entry, default=str)

    if level == "info":
        logger.info(log_json)
    elif level == "warning":
        logger.warning(log_json)
    elif level == "error":
        logger.error(log_json)
    else:
        logger.debug(log_json)
