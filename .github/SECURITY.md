# Security Policy

## Supported Versions

OpenEcon is currently in active development. Security updates are provided for the latest version only.

| Version | Supported          |
| ------- | ------------------ |
| latest  | :white_check_mark: |
| < latest| :x:                |

## Security Features

### Authentication & Authorization

- **Password Requirements**: The web UI requires at least 12 characters with mixed case and digits; the API enforces an 8-character server-side floor and delegates credential storage/verification to Supabase Auth (GoTrue)
- **JWT Token Authentication**: Every request is validated against Supabase Auth (real signature + expiry, fails closed); configurable expiration
- **Password Hashing**: Handled by Supabase Auth (bcrypt with per-user salt)
- **Protected Endpoints**: User history and profile endpoints require valid JWT tokens; query history is isolated per user id

### CORS Configuration

- **Explicit Origin Whitelist**: CORS is configured via the `ALLOWED_ORIGINS` environment variable
- **Environment-Aware Fallback**: with no `ALLOWED_ORIGINS` set, development trusts localhost origins (`http://localhost:5173`, `http://localhost:3000`) but **production falls back to the public app URL and its `www` host only** — never localhost, since credentialed CORS is enabled
- **Production Security**: No wildcard (`*`) origins in production

### Code Execution Sandbox (Pro Mode)

Pro Mode allows users to execute Python code for advanced data analysis. Security is layered — an OS-level sandbox is the containment boundary, with Python-level checks as defense-in-depth:

- **OS-Level Sandbox (production)**: dedicated `promode` uid, mount/pid namespace isolation, and an iptables egress allowlist, provisioned by `scripts/setup_promode_sandbox.sh` and gated by a fail-closed startup canary
- **Import Restrictions**: AST-based blocklist of dangerous imports (`subprocess`, `eval`, `exec`, `__import__`, etc.)
- **Operation Restrictions**: dangerous `os.*` operations (`os.remove`, `os.chmod`, `os.posix_spawn`, `os.open`/`read`/`write`, …) and `__builtins__`/`__loader__` access are blocked
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

## Production Posture

The hosted deployment (data.openecon.ai) runs the following; the in-memory
fallbacks below activate only when a self-hoster omits the matching service:

1. **Authentication & history**: Supabase (Postgres + GoTrue). The auth factory
   **fails closed** if Supabase is not configured in production — the in-memory
   user store is a development-only fallback.
2. **Cache, rate-limiter, and conversation state**: Redis (with in-memory
   fallback for single-instance dev).
3. **Rate limiting**: enabled via slowapi with per-endpoint limits (register
   5/min, login 10/min, `/api/query` 30/min, `/api/query/pro` 10/min, default
   200/min) returning `429` + `Retry-After`. Bypassed for loopback/dev only.

### Code Execution Sandbox

Production Pro Mode runs behind a **Layer-2 OS sandbox** (a dedicated `promode`
uid, mount/pid namespace isolation, and an iptables egress allowlist,
provisioned by `scripts/setup_promode_sandbox.sh` and gated by a fail-closed
startup canary). The Python-level AST validator is defense-in-depth on top of
that, not the sole barrier.

Known limitations of the Python-level validator:

- **Blacklist-based**: an AST allowlist is the intended structural replacement
  (a blacklist over CPython's object graph cannot be exhaustive); the OS
  sandbox is the real containment boundary.
- **Resource consumption**: code may consume CPU/memory within the 30s timeout.

**Recommendation for self-hosters** who cannot run the OS sandbox: disable Pro
Mode (`PROMODE_ENABLED=false`, the default), or run it inside a container /
dedicated execution service (Docker, gVisor, Firecracker, cloud functions)
with per-user resource quotas.

## Reporting a Vulnerability

If you discover a security vulnerability, please report it by:

1. **DO NOT** create a public GitHub issue
2. Email the maintainers directly at: security@openecon.ai
3. Include:
   - Description of the vulnerability
   - Steps to reproduce
   - Potential impact
   - Suggested fix (if any)

We aim to respond to security reports within 48 hours.

## Security Changelog

### 2026-07

#### Added
- Layer-2 OS sandbox for Pro Mode (dedicated uid, mount/pid namespaces,
  iptables egress allowlist, fail-closed startup canary)
- Rate limiting via slowapi (per-endpoint limits + `429`/`Retry-After`)
- Redis-backed cache, rate-limiter storage, and conversation persistence
- Supabase auth + history (auth factory fails closed in production)
- Email verification and a Pro-Mode registration gate
- Environment-aware CORS fallback (production never trusts localhost)
- AST-validator hardening: block `__builtins__`/`__loader__` and raw-syscall
  (`os.posix_spawn`, `os.open`/`read`/`write`) escapes

#### Fixed
- Removed insecure JWT_SECRET default
- Fixed CORS wildcard security issue
- Replaced pickle session storage with JSON
- Capped httpx/httpcore logging so provider API keys never reach logs

### 2025-01

#### Added
- Required JWT_SECRET configuration (no insecure default)
- Explicit CORS origin configuration
- JSON-based session storage (replaced pickle)
- Enhanced code execution sandbox with AST + pattern matching

## Acknowledgments

We appreciate the security research community's efforts in keeping OpenEcon secure. Security researchers who responsibly disclose vulnerabilities will be acknowledged here (with permission).
