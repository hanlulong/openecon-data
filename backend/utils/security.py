"""Security utilities for sanitization and validation.

This module provides shared security functions for:
- Path traversal prevention
- Identifier sanitization
- Input validation
"""
from __future__ import annotations

import hashlib
import re
from typing import Optional


def sanitize_identifier(
    identifier: str,
    identifier_type: str = "identifier",
    max_length: int = 64,
    allow_chars: str = "-_"
) -> str:
    """
    Validate and sanitize an identifier to prevent path traversal and injection attacks.

    This function is used for sanitizing session IDs, keys, and other identifiers
    that may be used in file paths or database queries.

    Args:
        identifier: The identifier to sanitize
        identifier_type: Type name for error messages (e.g., "session ID", "key")
        max_length: Maximum allowed length for the identifier
        allow_chars: Additional characters to allow besides alphanumeric

    Returns:
        Sanitized identifier safe for use in file paths and queries

    Raises:
        ValueError: If identifier is invalid or empty after sanitization

    Example:
        >>> sanitize_identifier("my-session-123")
        'my-session-123'
        >>> sanitize_identifier("../../../etc/passwd")
        'a1b2c3...'  # SHA256 hash prefix
    """
    if not identifier or not isinstance(identifier, str):
        raise ValueError(f"{identifier_type} must be a non-empty string")

    # Remove any path separators and dangerous characters
    # Defense-in-depth: remove multiple attack vectors
    sanitized = identifier
    for dangerous in ["/", "\\", "..", "\0", "\n", "\r", "\t"]:
        sanitized = sanitized.replace(dangerous, "")

    # Check if all characters are safe (alphanumeric + allowed chars)
    allowed_pattern = f"^[a-zA-Z0-9{re.escape(allow_chars)}]+$"
    if not re.match(allowed_pattern, sanitized):
        # Hash the identifier if it contains special characters
        # This ensures deterministic output for the same input
        sanitized = hashlib.sha256(identifier.encode()).hexdigest()[:16]

    # Limit length to prevent filesystem issues
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length]

    if not sanitized:
        raise ValueError(f"Invalid {identifier_type}")

    return sanitized


def validate_session_id(session_id: str) -> str:
    """
    Validate and sanitize session ID to prevent path traversal.

    Args:
        session_id: The session ID to validate

    Returns:
        Sanitized session ID

    Raises:
        ValueError: If session ID is invalid
    """
    return sanitize_identifier(session_id, "session ID")


def validate_key(key: str) -> str:
    """
    Validate and sanitize a storage key to prevent path traversal.

    Args:
        key: The key to validate

    Returns:
        Sanitized key

    Raises:
        ValueError: If key is invalid
    """
    return sanitize_identifier(key, "key")


def validate_path_component(component: str, component_type: str = "path component") -> str:
    """
    Validate a path component to ensure it's safe for use in file paths.

    Args:
        component: The path component to validate
        component_type: Type name for error messages

    Returns:
        Validated path component

    Raises:
        ValueError: If component contains dangerous characters
    """
    if not component or not isinstance(component, str):
        raise ValueError(f"{component_type} must be a non-empty string")

    # Check for path traversal attempts
    if ".." in component or "/" in component or "\\" in component:
        raise ValueError(f"{component_type} contains invalid characters")

    # Check for null bytes
    if "\0" in component:
        raise ValueError(f"{component_type} contains null bytes")

    return component


def is_safe_filename(filename: str) -> bool:
    """
    Check if a filename is safe for use in file operations.

    Args:
        filename: The filename to check

    Returns:
        True if the filename is safe, False otherwise
    """
    if not filename or not isinstance(filename, str):
        return False

    # Check for dangerous patterns
    dangerous_patterns = ["..", "/", "\\", "\0", "\n", "\r"]
    for pattern in dangerous_patterns:
        if pattern in filename:
            return False

    # Check for overly long filenames
    if len(filename) > 255:
        return False

    return True


def filter_sensitive_env_vars(env: Optional[dict] = None) -> dict:
    """
    Filter out sensitive environment variables for sandboxed execution.

    This function removes environment variables that could leak secrets,
    credentials, or sensitive configuration.

    Args:
        env: Environment variables dict. If None, uses empty dict.

    Returns:
        Filtered environment variables dict
    """
    if env is None:
        return {}

    # Prefixes that indicate sensitive variables
    sensitive_prefixes = [
        # Cloud provider credentials
        'AWS_', 'AZURE_', 'GCP_', 'GOOGLE_', 'ALIBABA_', 'DO_', 'DIGITALOCEAN_',
        # Generic secrets and tokens
        'SECRET_', 'TOKEN_', 'API_KEY', 'APIKEY', 'PASSWORD', 'PASSWD', 'CREDENTIAL',
        'PRIVATE_', 'AUTH_', 'OAUTH', 'JWT', 'BEARER',
        # Database credentials
        'DB_', 'DATABASE_', 'POSTGRES', 'MYSQL', 'MONGO', 'REDIS_', 'SUPABASE_',
        # Service-specific
        'OPENAI_', 'ANTHROPIC_', 'OPENROUTER_', 'GROK_', 'COHERE_',
        'STRIPE_', 'PAYPAL_', 'TWILIO_', 'SENDGRID_', 'MAILGUN_',
        'GITHUB_', 'GITLAB_', 'BITBUCKET_',
        'SLACK_', 'DISCORD_', 'TELEGRAM_',
        # SSH and encryption
        'SSH_', 'GPG_', 'PGP_', 'ENCRYPTION_', 'SIGNING_',
    ]

    # Exact matches to filter
    sensitive_exact = {
        'HOME', 'USER', 'LOGNAME', 'SHELL', 'MAIL',
        'HOSTNAME', 'HOSTTYPE', 'OSTYPE',
        'SSH_AUTH_SOCK', 'SSH_AGENT_PID',
        'GPG_AGENT_INFO', 'GNUPGHOME',
    }

    # Keywords to filter (case-insensitive substring match)
    sensitive_keywords = [
        'secret', 'password', 'passwd', 'token', 'key', 'credential',
        'private', 'auth', 'bearer', 'api_key', 'apikey',
    ]

    filtered = {}
    for key, value in env.items():
        key_upper = key.upper()

        # Check exact matches
        if key in sensitive_exact or key_upper in sensitive_exact:
            continue

        # Check prefixes
        if any(key_upper.startswith(prefix) for prefix in sensitive_prefixes):
            continue

        # Check keywords
        if any(keyword in key.lower() for keyword in sensitive_keywords):
            continue

        filtered[key] = value

    return filtered
