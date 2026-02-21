# Security Policy

## Supported Versions

OpenEcon is currently in active development. Security updates are provided for the latest version only.

| Version | Supported          |
| ------- | ------------------ |
| latest  | :white_check_mark: |
| < latest| :x:                |

## Security Features

### Authentication & Authorization

- **Strong Password Requirements**: Minimum 12 characters with uppercase, lowercase, and digits
- **JWT Token Authentication**: Secure token-based authentication with configurable expiration
- **Password Hashing**: bcrypt with automatic salt generation
- **Protected Endpoints**: User history and profile endpoints require valid JWT tokens

### CORS Configuration

- **Explicit Origin Whitelist**: CORS must be explicitly configured via `ALLOWED_ORIGINS` environment variable
- **Default Development Mode**: Defaults to localhost origins in development (`http://localhost:5173`, `http://localhost:3000`)
- **Production Security**: No wildcard (`*`) origins in production

### Code Execution Sandbox (Pro Mode)

Pro Mode allows users to execute Python code for advanced data analysis. Security measures include:

- **Import Restrictions**: Blacklist of dangerous imports (`subprocess`, `eval`, `exec`, `__import__`, etc.)
- **Operation Restrictions**: File system operations (`os.remove`, `os.chmod`, etc.) are blocked
- **Execution Timeout**: 30-second timeout prevents infinite loops
- **Output Size Limit**: 100,000 character limit on output
- **Safe Session Storage**: JSON-based serialization (not pickle) prevents code injection
- **Package Whitelist**: Only pre-approved data science packages can be auto-installed

### Data Storage

- **Session Data**: Stored as JSON (not pickle) to prevent deserialization attacks
- **Session Cleanup**: Automatic cleanup of sessions older than 24 hours
- **In-Memory User Store**: Development mode only - use a proper database in production
- **No Sensitive Data Logging**: API keys and tokens are not logged

### API Security

- **Input Validation**: All user inputs validated via Pydantic models
- **Query Length Limits**: Prevents resource exhaustion attacks
- **Error Message Sanitization**: Stack traces not exposed to clients in production
- **Cache TTL**: Automatic cache expiration prevents stale data attacks

## Configuration Requirements

### Required Environment Variables

The following environment variables **MUST** be set:

```bash
# REQUIRED: JWT secret for token signing
# Generate with: openssl rand -hex 32
JWT_SECRET=your_secure_random_string_here

# REQUIRED: OpenRouter API key for LLM functionality
OPENROUTER_API_KEY=your_openrouter_api_key
```

### Recommended Environment Variables

```bash
# CORS configuration (highly recommended for production)
ALLOWED_ORIGINS=https://yourdomain.com,https://www.yourdomain.com

# API keys for data providers (improves functionality)
FRED_API_KEY=your_fred_api_key
COMTRADE_API_KEY=your_comtrade_api_key

# Environment setting
NODE_ENV=production
```

## Security Best Practices

### For Deployment

1. **Always set a strong JWT_SECRET**: Use `openssl rand -hex 32` to generate
2. **Configure ALLOWED_ORIGINS**: Never use `*` in production
3. **Use HTTPS**: Always deploy behind HTTPS in production
4. **Set NODE_ENV=production**: Enables production-mode error handling
5. **Regular Updates**: Keep dependencies up to date
6. **Monitor Logs**: Review application logs for suspicious activity

### For Development

1. **Never commit .env files**: The .env file is in .gitignore
2. **Use .env.example as template**: Copy and fill in your own values
3. **Rotate API keys regularly**: Especially if accidentally exposed
4. **Test with realistic data**: Don't use production data in development

### Pro Mode Safety

When using Pro Mode code execution:

1. **Review code before execution**: Understand what the code does
2. **Don't run untrusted code**: Only execute code you understand
3. **Monitor resource usage**: Code execution has timeouts but can still consume resources
4. **Clear old sessions**: Use the automatic cleanup or manual deletion

## Known Limitations

### Development-Only Features

The following features are **NOT production-ready** and should be replaced:

1. **In-Memory User Store**: Replace with PostgreSQL, MongoDB, or similar
2. **In-Memory Cache**: Replace with Redis or Memcached for multi-instance deployments
3. **File-Based Sessions**: Use Redis or database-backed sessions
4. **No Rate Limiting**: Implement rate limiting before production deployment

### Code Execution Sandbox

The Pro Mode code execution sandbox has limitations:

- **Not 100% secure**: Blacklist-based security can potentially be bypassed
- **Resource consumption**: Malicious code could consume CPU/memory within timeout
- **Shared environment**: All code runs in the same Python environment
- **File system access**: Limited but not completely isolated

**Recommendation**: For production, consider:
- Using a containerized execution environment (Docker, Kubernetes)
- Implementing per-user resource quotas
- Using a dedicated code execution service (e.g., AWS Lambda, Google Cloud Functions)

## Reporting a Vulnerability

If you discover a security vulnerability, please report it by:

1. **DO NOT** create a public GitHub issue
2. Email the maintainers directly at: [your-security-email@domain.com]
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

We aim to respond to security reports within 48 hours.

## Security Changelog

### 2025-01-XX (Current)

#### Added
- Required JWT_SECRET configuration (no insecure default)
- Strong password requirements (12 characters, complexity rules)
- Explicit CORS origin configuration
- JSON-based session storage (replaced pickle)
- Enhanced code execution sandbox with regex-based pattern matching
- Additional dangerous operation checks

#### Fixed
- Removed insecure JWT_SECRET default
- Fixed CORS wildcard security issue
- Fixed pickle deserialization vulnerability
- Enhanced code validation in Pro Mode

#### Security Improvements
- Password minimum length: 6 → 12 characters
- Added password complexity requirements
- Improved input validation
- Better error message sanitization

## Acknowledgments

We appreciate the security research community's efforts in keeping OpenEcon secure. Security researchers who responsibly disclose vulnerabilities will be acknowledged here (with permission).
