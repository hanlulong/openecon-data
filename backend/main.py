from __future__ import annotations

import asyncio
import logging
import contextlib
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi_mcp import FastApiMCP
from starlette.types import ASGIApp, Receive, Scope, Send
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from .config import Settings, get_settings
from .utils.logging_security import SecureLogger, log_secure
from .models import (
    AuthResponse,
    AuthUser,
    ExportRequest,
    FeedbackRequest,
    FeedbackResponse,
    HealthResponse,
    LoginRequest,
    ParsedIntent,
    QueryRequest,
    QueryResponse,
    RegisterRequest,
    StreamEvent,
    User,
    UserQueryHistory,
)
from contextlib import asynccontextmanager
from .services.auth_factory import get_auth_service_singleton
from .services.supabase_service import get_supabase_service
from fastapi.security import HTTPBearer
from .services.cache import cache_service
from .services.redis_cache import get_redis_cache
from .services.conversation import conversation_manager
from .services.export import export_service
from .services.feedback import feedback_service
from .services.query import QueryService
from .services.user_store import user_store
from .utils.dependencies import require_promode

logger = logging.getLogger("openecon")
logging.basicConfig(level=logging.INFO)
# httpx/httpcore log every outbound request URL at INFO. Several provider APIs
# carry their key in the query string or path (FRED ?api_key=, Comtrade,
# CoinGecko, ExchangeRate), so INFO-level HTTP logging writes live secrets into
# the app log verbatim. Cap these loggers at WARNING so request URLs never
# reach the log; this is the structural fix for the leak class, independent of
# any per-provider URL masking.
for _noisy in ("httpx", "httpcore", "httpcore.http11", "httpcore.connection"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
# File handler for debugging (uvicorn reloader pipes child output through sockets)
_LOG_DIR = Path(__file__).resolve().parents[1] / ".omx" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
_fh = logging.FileHandler(_LOG_DIR / "backend-app.log", mode="a")
_fh.setLevel(logging.INFO)
_fh.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
# Lock down the log file: it has historically captured request URLs and user
# emails, so it must never be world-readable.
logging.getLogger().addHandler(_fh)
try:
    import os as _os
    _os.chmod(_LOG_DIR / "backend-app.log", 0o600)
except OSError:
    pass

_background_tasks: set[asyncio.Task] = set()


settings: Settings = get_settings()
# Use factory to get appropriate auth service (Supabase or Mock)
auth_service = get_auth_service_singleton()
query_service = QueryService(
    openrouter_key=settings.openrouter_api_key,
    fred_key=settings.fred_api_key,
    comtrade_key=settings.comtrade_api_key,
    coingecko_key=settings.coingecko_api_key,
    settings=settings,  # Pass settings for flexible LLM provider configuration
)


def get_query_service() -> QueryService:
    """Get the global query service instance.

    Used by agent nodes to access the query service for data fetching.
    """
    return query_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan context manager for startup/shutdown events."""
    # === STARTUP ===
    # Optional: OpenTelemetry auto-instrumentation for LLM observability.
    # Activates ONLY when OTEL_EXPORTER_OTLP_ENDPOINT is set.
    # Traces: LangChain calls, httpx requests, LLM token usage.
    import os
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        try:
            from opentelemetry.instrumentation.langchain import LangchainInstrumentor
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
            LangchainInstrumentor().instrument()
            HTTPXClientInstrumentor().instrument()
            logger.info("📊 OpenTelemetry instrumentation enabled (LangChain + httpx)")
        except Exception as e:
            logger.warning(f"OpenTelemetry instrumentation failed: {e}")

    # Initialize HTTP client pool (reused for all external API calls)
    from .services.http_pool import HTTPClientPool
    logger.info("⚙️  Initializing HTTP client pool...")
    HTTPClientPool()  # Initialize singleton
    logger.info("✅ HTTP client pool ready (connection pooling enabled)")

    # Pre-load OpenAI embedding index in background (non-blocking).
    # This eliminates the 30-40s delay on the first query that uses
    # the IndicatorSelector for variant/niche indicator resolution.
    try:
        from .services.embedding_retrieval import get_embedding_retrieval
        er = get_embedding_retrieval()
        if er._load_index():
            logger.info("✅ OpenAI embedding index pre-loaded (%d indicators)", len(er._codes))
        else:
            logger.info("ℹ️  Embedding index not available (build with EmbeddingRetrieval.build_index())")
    except Exception as e:
        logger.debug("Embedding index pre-load skipped: %s", e)

    # Load metadata asynchronously in background (non-blocking startup)
    from .services.metadata_loader import MetadataLoader
    from .services.vector_search import VECTOR_SEARCH_AVAILABLE
    from .services.async_metadata_loader import AsyncMetadataLoader

    if settings.enable_metadata_loading and VECTOR_SEARCH_AVAILABLE:
        try:
            logger.info("📚 Starting async metadata loading (non-blocking)...")
            logger.info(f"   - Using FAISS: {settings.use_faiss_instead_of_chroma}")
            logger.info(f"   - Timeout: {settings.metadata_loading_timeout}s")

            metadata_loader = MetadataLoader()

            # Create async loader that doesn't block startup
            async_loader = AsyncMetadataLoader(
                load_func=metadata_loader.load_all,
                timeout_seconds=settings.metadata_loading_timeout,
                max_retries=3,
                retry_delay_seconds=5,
            )

            # Start loading in background
            await async_loader.start()
            app.state.metadata_loader = async_loader

            logger.info("✅ Metadata loading started in background (non-blocking)")

        except Exception as e:
            logger.error(f"❌ Error initializing async metadata loader: {e}")
            app.state.metadata_loader = None

    elif settings.enable_metadata_loading and not VECTOR_SEARCH_AVAILABLE:
        logger.warning("⚠️  Vector search not available - skipping metadata loading")
    else:
        logger.info("ℹ️  Metadata loading disabled (set ENABLE_METADATA_LOADING=true to enable)")

    if not settings.disable_background_jobs:
        app.state.cleanup_task = asyncio.create_task(_conversation_cleanup_loop())
        app.state.file_cleanup_task = asyncio.create_task(_file_cleanup_loop())
    else:
        app.state.cleanup_task = None
        app.state.file_cleanup_task = None

    # Validate catalog codes against indicator database (non-blocking)
    try:
        from .services.catalog_validator import run_validation
        run_validation()
    except Exception as e:
        logger.warning(f"Catalog validation skipped: {e}")

    logger.info("🚀 OpenEcon Data Python backend ready (startup time optimized)")

    yield  # Application runs here

    # === SHUTDOWN ===
    # Close HTTP client pool
    from .services.http_pool import close_http_pool
    await close_http_pool()

    # Cancel metadata loader if still running
    metadata_loader_state = getattr(app.state, "metadata_loader", None)
    if metadata_loader_state:
        await metadata_loader_state.cancel()

    cleanup_task: asyncio.Task | None = getattr(app.state, "cleanup_task", None)
    if cleanup_task:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task

    file_cleanup_task: asyncio.Task | None = getattr(app.state, "file_cleanup_task", None)
    if file_cleanup_task:
        file_cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await file_cleanup_task


app = FastAPI(title="OpenEcon Data API", version="1.0.0", lifespan=lifespan)

# Rate limiting configuration using custom middleware approach
# This avoids Pydantic/FastAPI compatibility issues with slowapi decorators
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return a clean user-facing error instead of FastAPI's raw Pydantic dump.

    The default RequestValidationError response serializes the internal
    validator errors with field paths like ['body', 'sessionId'] and Pydantic
    enum codes. Frontends had to special-case parsing those for human-readable
    messages; if they didn't, users saw walls of JSON. Now every validation
    error returns a stable {error, message, fields} contract that the frontend
    can render directly.
    """
    field_messages: list[dict[str, str]] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body", "query", "path"))
        msg = str(err.get("msg") or "Invalid value")
        if loc:
            field_messages.append({"field": loc, "message": msg})
        else:
            field_messages.append({"message": msg})

    primary = field_messages[0]["message"] if field_messages else "Invalid request payload"
    if field_messages and "field" in field_messages[0]:
        primary = f"{field_messages[0]['field']}: {primary}"

    logger.info(
        "Validation error on %s %s: %s",
        request.method,
        request.url.path,
        primary,
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "validation_error",
            "message": primary,
            "fields": field_messages,
        },
    )

# Rate limit configuration per endpoint pattern
RATE_LIMITS = {
    "/api/auth/register": "5/minute",       # Strict: 5 registrations per minute per IP
    "/api/auth/login": "10/minute",         # Strict: 10 logins per minute per IP (brute force protection)
    "/api/query/pro": "10/minute",          # Pro Mode: resource-intensive
    "/api/query/pro/stream": "10/minute",   # Pro Mode streaming
    "/api/query/stream": "30/minute",       # Standard streaming queries
    "/api/query": "30/minute",              # Standard queries
}
DEFAULT_RATE_LIMIT = "200/minute"  # Default for other endpoints


def get_rate_limit_for_path(path: str) -> str:
    """Get the appropriate rate limit for a given path."""
    # Check for exact match first, then check if path starts with pattern
    for pattern, limit in RATE_LIMITS.items():
        if path == pattern or path.startswith(pattern):
            return limit
    return DEFAULT_RATE_LIMIT


from limits import parse as parse_rate_limit

class RateLimitASGIMiddleware:
    """
    Pure ASGI rate limiting middleware.

    Using pure ASGI avoids BaseHTTPMiddleware edge cases with SSE/MCP endpoints.
    """

    def __init__(self, app: ASGIApp, settings_obj: Settings, limiter_obj: Limiter):
        self.app = app
        self.settings = settings_obj
        self.limiter = limiter_obj

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        path = request.url.path

        # Skip rate limiting for health check, static files, and MCP endpoint
        if path in ("/api/health", "/health") or path.startswith("/static") or path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        # Skip rate limiting in development mode
        is_development = self.settings.environment != "production"
        if is_development:
            await self.app(scope, receive, send)
            return

        # In production, get real client IP. Only honor X-Forwarded-For when
        # the direct connection comes from a TRUSTED_PROXIES allowlist —
        # otherwise an attacker can spoof XFF to bypass per-IP rate limits or
        # poison the rate-limit bucket of an arbitrary IP.
        direct_ip = get_remote_address(request)
        forwarded_for_header = request.headers.get("X-Forwarded-For")
        trusted_proxies = set(self.settings.trusted_proxies or [])

        if forwarded_for_header and direct_ip in trusted_proxies:
            # Trusted reverse proxy. Apache (and most proxies) APPEND the peer
            # they received from, so the chain reads "client-supplied …, real
            # client". The leftmost entry is therefore attacker-controlled — a
            # spoofed "X-Forwarded-For: 1.2.3.4" arrives as "1.2.3.4, <real>".
            # Resolve rightmost-untrusted: walk from the right, skip our own
            # trusted proxies, and take the first hop we did not add ourselves.
            chain = [h.strip() for h in forwarded_for_header.split(",") if h.strip()]
            client_ip = direct_ip
            for hop in reversed(chain):
                if hop not in trusted_proxies:
                    client_ip = hop
                    break
        else:
            # Untrusted source — use direct connection IP, ignore XFF
            client_ip = direct_ip
            if forwarded_for_header:
                logger.warning(
                    "Ignoring X-Forwarded-For from untrusted source %s (set TRUSTED_PROXIES to allow)",
                    direct_ip,
                )

        # Skip rate limiting for direct localhost connections (no XFF present)
        if not forwarded_for_header and client_ip in ("127.0.0.1", "::1", "localhost"):
            await self.app(scope, receive, send)
            return

        rate_limit_str = get_rate_limit_for_path(path)

        try:
            limit_key = f"{client_ip}:{path}"
            rate_limit = parse_rate_limit(rate_limit_str)

            if not self.limiter._limiter.hit(rate_limit, limit_key):
                response = JSONResponse(
                    status_code=429,
                    content={"detail": f"Rate limit exceeded. Limit: {rate_limit_str}"},
                    headers={"Retry-After": "60"},
                )
                await response(scope, receive, send)
                return

            await self.app(scope, receive, send)
            return
        except Exception as e:
            # SECURITY: Fail closed - deny request if rate limiter fails
            logger.error(f"Rate limit check failed for {path} from {client_ip}: {e}")
            response = JSONResponse(
                status_code=503,
                content={"detail": "Service temporarily unavailable. Please try again."},
                headers={"Retry-After": "5"},
            )
            await response(scope, receive, send)
            return


class SecureLoggingASGIMiddleware:
    """
    Pure ASGI secure logging middleware.

    Avoids BaseHTTPMiddleware protocol issues with MCP SSE transport.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = SecureLogger.generate_request_id()
        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id
        start_time = time.perf_counter()

        request = Request(scope, receive=receive)
        request_log = SecureLogger.format_request_log(request, request_id)
        log_secure("info", "Request received", request_log, request_id)

        status_code = 500

        async def send_wrapper(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 500)
                headers = list(message.get("headers", []))
                has_request_id = any(k.lower() == b"x-request-id" for k, _ in headers)
                if not has_request_id:
                    headers.append((b"x-request-id", request_id.encode("utf-8")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
            duration_ms = (time.perf_counter() - start_time) * 1000
            response_log = SecureLogger.format_response_log(request_id, status_code, duration_ms)
            log_secure("info", "Request completed", response_log, request_id)
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            error_log = SecureLogger.format_error_log(request_id, e, include_traceback=True)
            log_secure("error", "Request failed", error_log, request_id)
            raise


app.add_middleware(
    CORSMiddleware,
    allow_origins=(
        settings.allowed_origins
        if settings.allowed_origins
        else [settings.app_url, "http://localhost:5173", "http://localhost:3000"]
    ),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
)
app.add_middleware(RateLimitASGIMiddleware, settings_obj=settings, limiter_obj=limiter)
app.add_middleware(SecureLoggingASGIMiddleware)


# ====================
# Helper Functions
# ====================

def format_sse_event(event_type: str, data: dict) -> str:
    """Format data as a Server-Sent Event string."""
    import json
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


async def log_query_to_supabase(
    query: str,
    user: Optional[User],
    session_id: Optional[str],
    conversation_id: str,
    pro_mode: bool = False,
    intent: Optional[dict] = None,
    response_data: Optional[list] = None,
    code_execution: Optional[dict] = None,
    error_message: Optional[str] = None,
) -> None:
    """
    Log query to Supabase for both authenticated and anonymous users.
    Handles errors gracefully without raising exceptions.
    """
    try:
        supabase_service = get_supabase_service()
        await supabase_service.log_query(
            query=query,
            user_id=user.id if user else None,
            session_id=session_id,
            conversation_id=conversation_id,
            pro_mode=pro_mode,
            intent=intent,
            response_data=response_data,
            code_execution=code_execution,
            error_message=error_message,
        )
    except Exception as e:
        logger.error(f"Failed to log {'Pro Mode ' if pro_mode else ''}query to Supabase: {e}")


def schedule_query_log_to_supabase(**kwargs) -> None:
    """Run Supabase query logging in the background so it never blocks responses."""
    task = asyncio.create_task(log_query_to_supabase(**kwargs))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


def get_request_conversation_id(request: QueryRequest, user: Optional[User]) -> Optional[str]:
    """
    Resolve the conversation key for this request.

    Anonymous callers often only send ``sessionId`` on the first few requests, so
    we must treat that as the conversation identifier to preserve clarification
    state across follow-ups.
    """
    if request.conversationId:
        return request.conversationId
    if not user and request.sessionId:
        return request.sessionId
    return None


def save_to_user_history(
    user: User,
    query: str,
    conversation_id: str,
    intent: Optional[ParsedIntent],
    data: Optional[list],
) -> None:
    """Save query to in-memory user history for authenticated users."""
    history_item = UserQueryHistory(
        id=str(uuid.uuid4()),
        userId=user.id,
        query=query,
        conversationId=conversation_id,
        intent=intent,
        data=data,
        timestamp=datetime.now(timezone.utc),
    )
    user_store.add_query_to_history(history_item)


async def _conversation_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(600)  # every 10 minutes
        conversation_manager.cleanup()


async def _file_cleanup_loop() -> None:
    """Clean up old Pro Mode generated files and session data every hour"""
    from backend.services.code_executor import get_code_executor
    from backend.services.session_storage import get_session_storage

    while True:
        await asyncio.sleep(3600)  # every 1 hour
        try:
            # Clean up old files
            code_executor = get_code_executor()
            deleted_files = code_executor.cleanup_old_files(max_age_hours=24)
            if deleted_files > 0:
                logger.info(f"🗑️  Cleaned up {deleted_files} old Pro Mode file(s)")

            # Clean up old session data
            session_storage = get_session_storage()
            deleted_sessions = session_storage.cleanup_old_sessions(max_age_hours=24)
            if deleted_sessions > 0:
                logger.info(f"🗑️  Cleaned up {deleted_sessions} old session(s)")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")


# Auth service conditional dependency functions
bearer_scheme = HTTPBearer(auto_error=False)


async def get_required_user(
    credentials = Depends(bearer_scheme)
) -> User:
    """Get required authenticated user - works with both Supabase and Mock auth."""
    auth = get_auth_service_singleton()
    return await auth.require_user(credentials)


async def get_optional_user(
    credentials = Depends(bearer_scheme)
) -> Optional[User]:
    """Get optional authenticated user - works with both Supabase and Mock auth."""
    auth = get_auth_service_singleton()
    return await auth.optional_user(credentials)


def get_current_user(user: User = Depends(get_required_user)) -> User:
    """Alias for backward compatibility."""
    return user


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Health check with deep dependency probes.

    Previously this endpoint only reported API-key presence. Load balancers
    and uptime monitors saw "healthy" even when Redis, Supabase, or the
    indicator database were unreachable. Each probe is timeboxed (200ms)
    so the endpoint stays fast; failures degrade the status field but
    never raise so the endpoint always responds.
    """
    cache_stats = cache_service.get_stats()
    user_stats = user_store.get_stats()

    services = {
        "openrouter": bool(settings.openrouter_api_key),
        "fred": bool(settings.fred_api_key),
        "alphaVantage": bool(False),
        "bls": bool(False),
        "comtrade": bool(settings.comtrade_api_key),
    }

    # Deep probes — each timeboxed; never raises.
    overall_status = "ok"

    # Redis ping (non-critical — in-memory fallback exists)
    redis_ok = False
    try:
        redis = await asyncio.wait_for(get_redis_cache(), timeout=0.2)
        # connect() is idempotent and returns True if reachable
        redis_ok = await asyncio.wait_for(redis.connect(), timeout=0.2)
    except (asyncio.TimeoutError, Exception) as exc:
        logger.debug("Health probe: redis unreachable (%s)", exc)
    services["redis"] = redis_ok
    if not redis_ok:
        overall_status = "degraded"

    # Supabase (non-critical for read paths — auth still fails closed elsewhere)
    supabase_ok = False
    try:
        sb = get_supabase_service()
        supabase_ok = sb.client is not None
    except Exception as exc:
        logger.debug("Health probe: supabase unavailable (%s)", exc)
    services["supabase"] = supabase_ok
    if not supabase_ok and settings.environment == "production":
        overall_status = "degraded"

    # Indicator database (CRITICAL — without it, no queries work)
    indicators_db_ok = False
    try:
        from .services.indicator_database import get_indicator_lookup
        lookup = await asyncio.wait_for(
            asyncio.to_thread(get_indicator_lookup), timeout=0.2
        )
        indicators_db_ok = bool(lookup)
    except (asyncio.TimeoutError, Exception) as exc:
        logger.warning("Health probe: indicators.db unreachable (%s)", exc)
    services["indicators_db"] = indicators_db_ok
    if not indicators_db_ok:
        overall_status = "error"

    return HealthResponse(
        status=overall_status,
        timestamp=datetime.now(timezone.utc).isoformat(),
        environment=settings.environment or "development",
        services=services,
        cache=cache_stats,
        users=user_stats,
        promodeEnabled=settings.promode_enabled,
    )


@app.post("/api/auth/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(request: RegisterRequest):
    """Register new user (with Supabase Auth or mock auth)."""
    auth = get_auth_service_singleton()
    response = await auth.register(request)
    if not response.success:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content=response.model_dump())
    return response


@app.post("/api/auth/login", response_model=AuthResponse)
async def login(request: LoginRequest):
    """Login user (with Supabase Auth or mock auth)."""
    auth = get_auth_service_singleton()
    response = await auth.login(request)
    if not response.success:
        return JSONResponse(status_code=status.HTTP_401_UNAUTHORIZED, content=response.model_dump())
    return response


@app.get("/api/auth/me", response_model=AuthUser)
async def me(user: User = Depends(get_current_user)) -> AuthUser:
    return AuthUser(
        id=user.id,
        email=user.email,
        name=user.name,
        createdAt=user.createdAt,
        lastLogin=user.lastLogin,
    )


@app.get("/api/user/history")
async def history(limit: Optional[int] = Query(default=None, ge=1, le=500), user: User = Depends(get_current_user)):
    """Get user query history from Supabase (persisted across server restarts)."""
    try:
        logger.info(f"📋 Fetching history for user_id={user.id}, email={user.email}, limit={limit or 50}")
        supabase_service = get_supabase_service()
        queries = await supabase_service.get_user_queries(
            user_id=user.id,
            limit=limit or 50
        )
        logger.info(f"📊 Found {len(queries)} queries for user_id={user.id}")

        # Map Supabase format to frontend format
        history = []
        for q in queries:
            history.append({
                "id": q.get("id"),
                "query": q.get("query"),
                "conversationId": q.get("conversation_id"),
                "intent": q.get("intent"),
                "data": q.get("response_data"),
                "timestamp": q.get("created_at"),
            })

        return {
            "history": history,
            "total": len(history),
        }
    except Exception as e:
        logger.error(f"Failed to fetch user history from Supabase: {e}")
        # Fallback to in-memory store if Supabase fails
        entries = user_store.get_user_history(user.id, limit)
        return {
            "history": [entry.model_dump(mode="json") for entry in entries],
            "total": len(entries),
        }


@app.delete("/api/user/history")
async def clear_history(user: User = Depends(get_current_user)):
    """Clear all query history for the authenticated user."""
    try:
        logger.info(f"🗑️ Clearing history for user_id={user.id}, email={user.email}")
        supabase_service = get_supabase_service()

        # Delete all queries for this user
        deleted_count = await supabase_service.delete_user_queries(user_id=user.id)

        logger.info(f"✅ Deleted {deleted_count} queries for user_id={user.id}")
        return {
            "success": True,
            "message": f"Deleted {deleted_count} queries",
            "deleted": deleted_count
        }
    except Exception as e:
        logger.error(f"Failed to clear user history: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to clear history. Please try again later."
        )


@app.get("/api/session/history")
async def session_history(session_id: str = Query(..., description="Session ID", max_length=100), limit: Optional[int] = Query(default=None, ge=1, le=500)):
    """Get anonymous session query history from Supabase."""
    try:
        supabase_service = get_supabase_service()
        queries = await supabase_service.get_user_queries(
            session_id=session_id,
            limit=limit or 50
        )

        # Map Supabase format to frontend format
        history = []
        for q in queries:
            history.append({
                "id": q.get("id"),
                "query": q.get("query"),
                "conversationId": q.get("conversation_id"),
                "intent": q.get("intent"),
                "data": q.get("response_data"),
                "timestamp": q.get("created_at"),
            })

        return {
            "history": history,
            "total": len(history),
        }
    except Exception as e:
        logger.error(f"Failed to fetch session history from Supabase: {e}")
        return {
            "history": [],
            "total": 0,
        }


@app.post(
    "/api/query",
    response_model=QueryResponse,
    operation_id="query_data",
    summary="Query economic data using natural language",
    description=(
        "Query economic data from 10 providers covering 330,000+ indicators. "
        "Response includes data points, direct API URL (metadata.apiUrl) for programmatic access, "
        "source URL (metadata.sourceUrl) for verification, and alternativeSeries for related indicators. "
        "Examples: 'US GDP 2020-2024', 'Japan inflation rate', 'Compare BRICS GDP growth', "
        "'female youth unemployment in Nigeria', 'bitcoin price last 7 days'."
    ),
    tags=["Economic Data"],
)
async def query_endpoint(request: QueryRequest, user: Optional[User] = Depends(get_optional_user)) -> QueryResponse:
    if not request.query:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"error": "Query is required"})

    query_start = time.perf_counter()
    conversation_id = get_request_conversation_id(request, user)
    logger.info("📝 Query: %s (conversation: %s, user: %s)", request.query, conversation_id, user.id if user else "anonymous")

    # Auto-detect complex queries and route to Pro Mode (code execution)
    # when needed. Available for all users (no auth required).
    auto_pro = settings.promode_enabled
    # Request-level deadline. Without this the sync endpoint can hang
    # indefinitely on slow provider responses, blocking the event loop and
    # exposing the service to resource exhaustion.
    try:
        result = await asyncio.wait_for(
            query_service.process_query(request.query, conversation_id, auto_pro_mode=auto_pro),
            timeout=float(settings.query_timeout_seconds),
        )
    except asyncio.TimeoutError:
        timeout_conversation_id = conversation_id or str(uuid.uuid4())
        logger.warning(
            "Query timed out after %ss: %s (conversation=%s)",
            settings.query_timeout_seconds,
            request.query[:120],
            timeout_conversation_id,
        )
        return JSONResponse(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            content=QueryResponse(
                conversationId=timeout_conversation_id,
                clarificationNeeded=False,
                error="request_timeout",
                message=(
                    f"Query exceeded the {settings.query_timeout_seconds}s server deadline. "
                    "Try the streaming endpoint /api/query/stream for long-running queries."
                ),
                processingTimeMs=round((time.perf_counter() - query_start) * 1000, 1),
            ).model_dump(),
        )

    # Guaranteed post-query conversation state save.
    # This ensures the v2 ConversationState is ALWAYS saved after a successful
    # query, regardless of which execution path was taken. The cube metadata
    # from StatsCan's _get_cube_metadata cache is available here because the
    # data fetch has already completed.
    # SKIP when the delta path already saved the merged state — re-extracting
    # from the (mutated) response intent would overwrite the precise merged
    # state and cause indicator drift (e.g., NY.GDP.PCAP.CD → NY.GDP.MKTP.CD).
    if result.data and result.intent and result.conversationId and not getattr(result, 'delta_state_saved', False):
        try:
            from .services.conversation_state_v2 import extract_state_from_intent as _extract_state
            from .services.conversation_state_v2 import merge_new_state_with_previous as _merge_with_prev
            from .services.conversation_state_v2 import update_answer_members_from_data as _update_answer_members
            _state = _extract_state(result.intent, statscan_provider=query_service.statscan_provider)
            _existing = conversation_manager.get_conversation_state(result.conversationId)
            _state = _merge_with_prev(_state, _existing)
            _update_answer_members(_state, result.data, intent=result.intent)
            conversation_manager.set_conversation_state(result.conversationId, _state)
            logger.info("State saved for %s: indicator=%s, country=%s/%s, provider=%s",
                        result.conversationId, _state.indicator, _state.country, _state.countries, _state.provider)
        except Exception as _save_err:
            logger.error("FAILED to save conversation state for %s: %s",
                         result.conversationId, _save_err, exc_info=True)
    elif getattr(result, 'delta_state_saved', False):
        logger.info("State already saved by delta path for %s — skipping guaranteed save",
                     result.conversationId)

    # Provider transparency: warn user when data came from a different provider than requested
    try:
        result = query_service._add_provider_transparency(result, request.query)
    except Exception as _transp_exc:
        logger.debug("Non-critical: provider transparency annotation failed: %s", _transp_exc)

    # Add alternative series suggestions if data was returned and not already present
    if result.data and not result.alternativeSeries:
        try:
            result.alternativeSeries = query_service._build_alternative_series(result.intent, result.data)
        except Exception as _alt_exc:
            logger.debug("Non-critical: alternative series building failed: %s", _alt_exc)

    # Record end-to-end processing time
    elapsed_ms = (time.perf_counter() - query_start) * 1000
    result.processingTimeMs = round(elapsed_ms, 1)
    logger.info("Query completed in %.2fs: %s", elapsed_ms / 1000, request.query[:80])

    # Don't treat "data_not_available" as a server error - return 200 with error message
    # Only return 500 for actual processing errors
    if result.error == "processing_error":
        return JSONResponse(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=result.model_dump())

    # Save to user history if authenticated
    if user and result.data:
        save_to_user_history(user, request.query, result.conversationId, result.intent, result.data)

    # Log query to Supabase (for both authenticated and anonymous users)
    schedule_query_log_to_supabase(
        query=request.query,
        user=user,
        session_id=None if user else request.sessionId,
        conversation_id=result.conversationId,
        pro_mode=False,
        intent=result.intent.model_dump() if result.intent else None,
        response_data=[d.model_dump() for d in result.data] if result.data else None,
        error_message=result.error,
    )

    return result


@app.post(
    "/api/query/stream",
    operation_id="query_data_stream",
    summary="Query economic data with real-time streaming updates",
    description="Same as /api/query but streams progress updates in real-time using Server-Sent Events. Each event shows processing steps as they complete.",
    tags=["Economic Data"],
)
async def query_stream_endpoint(request: QueryRequest, user: Optional[User] = Depends(get_optional_user)):
    """Streaming version of query endpoint using Server-Sent Events"""
    import json
    import asyncio

    if not request.query:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"error": "Query is required"})

    conversation_id = get_request_conversation_id(request, user)
    logger.info("📝 Stream Query: %s (conversation: %s, user: %s)", request.query, conversation_id, user.id if user else "anonymous")

    # Queue for streaming events
    event_queue: asyncio.Queue = asyncio.Queue()

    async def generate_events():
        """Generate Server-Sent Events with proper resource cleanup"""
        # Import here to avoid circular dependency
        from backend.utils.processing_steps import ProcessingTracker, activate_processing_tracker, reset_processing_tracker

        tracker_token = None
        query_task = None
        client_disconnected = False

        try:
            # Create tracker with callback that puts events in queue
            def stream_callback(step):
                event = StreamEvent(
                    event="step",
                    data={
                        "step": step.step,
                        "description": step.description,
                        "status": step.status,  # Include status for real-time UI updates
                        "duration_ms": step.duration_ms,
                        "metadata": step.metadata
                    }
                )
                try:
                    event_queue.put_nowait(event)
                except Exception as _q_exc:
                    logger.debug("Stream event queue put failed (client likely disconnected): %s", _q_exc)

            tracker = ProcessingTracker(stream_callback=stream_callback)
            tracker_token = activate_processing_tracker(tracker)

            # Start processing query in background (don't await yet)
            # Auto-detect complex queries for Pro Mode (all users)
            auto_pro = settings.promode_enabled
            query_task = asyncio.create_task(query_service.process_query(request.query, conversation_id, auto_pro_mode=auto_pro))

            # Stream events as they come
            processing_complete = False
            _stream_start = time.perf_counter()
            _STREAM_TIMEOUT = 300  # 5 minute overall timeout for streaming queries
            while not processing_complete:
                # Check overall streaming timeout
                if time.perf_counter() - _stream_start > _STREAM_TIMEOUT:
                    logger.warning("Streaming query timed out after %ds: %s", _STREAM_TIMEOUT, request.query[:80])
                    yield f"event: error\ndata: {json.dumps({'error': 'Query timed out. Try a simpler query or narrow the scope.'})}\n\n"
                    yield f"event: done\ndata: {json.dumps({'done': True})}\n\n"
                    break
                # Try to get events with timeout
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                    yield f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"
                except asyncio.TimeoutError:
                    # No event available
                    pass
                except asyncio.CancelledError:
                    # Client disconnected
                    client_disconnected = True
                    logger.info("📡 Client disconnected during streaming")
                    break

                # Check if query is complete
                if query_task.done():
                    # Drain remaining events
                    while not event_queue.empty():
                        try:
                            event = event_queue.get_nowait()
                            yield f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"
                        except Exception as _drain_exc:
                            logger.debug("Event queue drain interrupted: %s", _drain_exc)
                            break
                    processing_complete = True

            # If client disconnected, cancel the query task
            if client_disconnected:
                if query_task and not query_task.done():
                    query_task.cancel()
                    try:
                        await query_task
                    except asyncio.CancelledError:
                        logger.info("📡 Query task cancelled due to client disconnect")
                return

            # Get result and handle exceptions from query processing
            try:
                result = await query_task
            except asyncio.CancelledError:
                logger.info("📡 Query task was cancelled")
                return
            except Exception as query_error:
                logger.exception("Query task failed")
                # Sanitize error message - don't expose internal details to client
                error_data = {"error": "processing_error", "message": "Failed to process your query. Please try again."}
                yield f"event: error\ndata: {json.dumps(error_data)}\n\n"
                yield f"event: done\ndata: {json.dumps({})}\n\n"
                return

            # Guaranteed post-query conversation state save (streaming path).
            # SKIP when the delta path already saved the merged state.
            if result.data and result.intent and result.conversationId and not getattr(result, 'delta_state_saved', False):
                try:
                    from backend.services.conversation_state_v2 import extract_state_from_intent as _extract_state
                    from backend.services.conversation_state_v2 import merge_new_state_with_previous as _merge_with_prev
                    _state = _extract_state(result.intent, statscan_provider=query_service.statscan_provider)
                    _existing = conversation_manager.get_conversation_state(result.conversationId)
                    _state = _merge_with_prev(_state, _existing)
                    conversation_manager.set_conversation_state(result.conversationId, _state)
                    logger.info("Stream state saved for %s: indicator=%s, country=%s/%s", result.conversationId, _state.indicator, _state.country, _state.countries)
                except Exception as _save_err:
                    logger.error("FAILED to save stream conversation state: %s", _save_err, exc_info=True)
            elif getattr(result, 'delta_state_saved', False):
                logger.info("Stream state already saved by delta path for %s", result.conversationId)

            # Save to user history if authenticated
            if user and result.data:
                save_to_user_history(user, request.query, result.conversationId, result.intent, result.data)

            # Log query to Supabase (for both authenticated and anonymous users)
            # Use request.sessionId for consistency with non-streaming path, fallback to conversationId
            schedule_query_log_to_supabase(
                query=request.query,
                user=user,
                session_id=None if user else (request.sessionId or result.conversationId),
                conversation_id=result.conversationId,
                pro_mode=False,
                intent=result.intent.model_dump() if result.intent else None,
                response_data=[d.model_dump() for d in result.data] if result.data else None,
                error_message=result.error,
            )

            # Send final result
            if result.error:
                error_data = {"error": result.error, "message": result.message or result.error}
                yield f"event: error\ndata: {json.dumps(error_data)}\n\n"
            else:
                result_dict = result.model_dump(mode="json")
                yield f"event: data\ndata: {json.dumps(result_dict)}\n\n"

            # Send done event
            done_data = {"conversationId": result.conversationId}
            yield f"event: done\ndata: {json.dumps(done_data)}\n\n"

        except asyncio.CancelledError:
            # Client disconnected - cleanup handled below
            logger.info("📡 Stream cancelled (client disconnect)")
        except Exception as e:
            logger.exception("Streaming query error")
            # Sanitize error message - don't expose internal details to client
            error_event = {
                "error": "processing_error",
                "message": "An unexpected error occurred. Please try again."
            }
            yield f"event: error\ndata: {json.dumps(error_event)}\n\n"
            yield f"event: done\ndata: {json.dumps({})}\n\n"
        finally:
            # Always cleanup resources
            if tracker_token is not None:
                try:
                    reset_processing_tracker(tracker_token)
                except Exception as cleanup_error:
                    logger.warning(f"Error resetting tracker: {cleanup_error}")

            # Cancel any pending query task
            if query_task and not query_task.done():
                query_task.cancel()
                try:
                    await query_task
                except asyncio.CancelledError:
                    pass  # Expected when cancelling
                except Exception as _cancel_exc:
                    logger.debug("Unexpected error while cancelling query task: %s", _cancel_exc)
                logger.info("📡 Cleaned up pending query task")

    return StreamingResponse(
        generate_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        }
    )


@app.post(
    "/api/query/pro",
    response_model=QueryResponse,
    operation_id="query_pro_mode",
    summary="Pro Mode: Generate and execute custom Python code for advanced analysis",
    description="Pro Mode uses AI to generate and execute custom Python code based on your query. Supports data analysis, custom visualizations, API calls, and more. Requires authentication.",
    tags=["Pro Mode"],
    dependencies=[Depends(require_promode)],
)
async def query_pro_endpoint(request: QueryRequest, user: Optional[User] = Depends(get_optional_user)) -> QueryResponse:
    """Pro Mode endpoint: Generate and execute Python code using Grok"""
    if not request.query:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"error": "Query is required"})

    logger.info("⚡ Pro Mode Query: %s (conversation: %s, user: %s)", request.query, request.conversationId, user.id if user else "anonymous")

    _pro_start = time.perf_counter()
    try:
        return await asyncio.wait_for(
            _pro_query_inner(request, user),
            timeout=float(settings.pro_query_timeout_seconds),
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Pro mode query timed out after %ss: %s",
            settings.pro_query_timeout_seconds,
            request.query[:120],
        )
        return QueryResponse(
            conversationId=request.conversationId or str(uuid.uuid4()),
            clarificationNeeded=False,
            error="request_timeout",
            message=(
                f"Pro Mode query exceeded the {settings.pro_query_timeout_seconds}s server deadline. "
                "Try a simpler query or use the streaming endpoint /api/query/pro/stream."
            ),
            isProMode=True,
            processingTimeMs=round((time.perf_counter() - _pro_start) * 1000, 1),
        )


async def _pro_query_inner(request: QueryRequest, user: Optional[User]) -> QueryResponse:
    """Inner body of /api/query/pro. Extracted so the outer endpoint can wrap
    it in asyncio.wait_for. Preserves the original try/except contract.
    """
    try:
        # Import services here to avoid circular imports
        from backend.services.grok import get_grok_service
        from backend.services.code_executor import get_code_executor

        grok_service = get_grok_service()
        code_executor = get_code_executor()

        # Get or create conversation
        conversation_id = conversation_manager.get_or_create(request.conversationId)

        # Get conversation history for context
        conversation_history = conversation_manager.get_messages(conversation_id)

        # Check for available session data
        from backend.services.session_storage import get_session_storage
        session_storage = get_session_storage()
        session_id = conversation_id[:8]
        available_keys = session_storage.list_keys(session_id)

        # Build available data context
        available_data = {}

        # Add session storage context
        if available_keys:
            available_data["session_data_available"] = available_keys
            available_data["note"] = "Use load_session(key) to access this data - it's already fetched and ready!"

        # Dynamically discover Statistics Canada metadata for categorical queries
        from backend.services.statscan_metadata import get_statscan_metadata_service
        from backend.services.query_complexity import QueryComplexityAnalyzer

        # Analyze query for categorical patterns
        analysis = QueryComplexityAnalyzer.detect_complexity(request.query)

        # If query is categorical and mentions StatsCan indicators, discover metadata
        if 'categorical_breakdown' in analysis.get('complexity_factors', []):
            logger.info(f"🔍 Categorical query detected, attempting dynamic metadata discovery...")

            # Try to extract indicator from query
            indicator_keywords = {
                'unemployment': 'Labour Force',
                'employment': 'Labour Force',
                'population': 'Population',
                'gdp': 'Gross domestic product',
                'immigration': 'Immigration',
                'wages': 'Wages',
            }

            indicator_found = None
            query_lower = request.query.lower()
            for keyword, search_term in indicator_keywords.items():
                if keyword in query_lower:
                    indicator_found = search_term
                    break

            if indicator_found:
                metadata_service = get_statscan_metadata_service()
                try:
                    # Discover metadata for the indicator
                    discovered = await metadata_service.discover_for_query(
                        indicator=indicator_found,
                        category=None  # Let it find all dimensions
                    )

                    if discovered:
                        logger.info(
                            f"✅ Discovered StatsCan metadata: product {discovered['product_id']} "
                            f"with {discovered['dimension_count']} dimensions"
                        )
                        available_data["statscan_metadata"] = {
                            "product_id": discovered["product_id"],
                            "product_title": discovered["product_title"],
                            "dimensions": discovered["dimensions"],
                            "cube_start_date": discovered.get("cube_start_date"),
                            "cube_end_date": discovered.get("cube_end_date"),
                            "note": (
                                f"Discovered metadata for {discovered['product_title']}. "
                                f"Use coordinate API with product_id={discovered['product_id']} "
                                f"and dimension IDs from 'dimensions' dict."
                            )
                        }
                    else:
                        logger.warning(f"No metadata discovered for '{indicator_found}'")
                except Exception as e:
                    logger.exception(f"Error discovering StatsCan metadata: {e}")

        # If no metadata discovered, provide fallback vector IDs
        if "statscan_metadata" not in available_data:
            from backend.providers.statscan import StatsCanProvider
            available_data["statscan_vectors"] = {
                "GDP": 65201210,
                "UNEMPLOYMENT": 2062815,  # Overall unemployment rate, 15 years and over
                "INFLATION": 41690973,
                "CPI": 41690914,
                "POPULATION": 1,
                "HOUSING_STARTS": 50483,
                "EMPLOYMENT_RATE": 14609,
                "note": "These are VERIFIED vector IDs that work with Vector API (getDataFromVectorsAndLatestNPeriods). For categorical breakdowns, Pro Mode will discover appropriate dimensions."
            }

        # Always provide available_data (even if just metadata)
        if not available_data:
            available_data = None

        # Add current user query to conversation
        conversation_manager.add_message(conversation_id, "user", request.query)

        # Generate code using Grok with conversation and session context
        logger.info("Generating code with Grok (conversation: %s, history: %d, session data: %s)...",
                   conversation_id, len(conversation_history), available_keys or "none")
        generated_code = await grok_service.generate_code(
            query=request.query,
            conversation_history=conversation_history,
            available_data=available_data,
            session_id=session_id
        )

        # Execute the generated code with session storage
        logger.info("Executing generated code with session: %s...", conversation_id[:8])
        execution_result = await code_executor.execute_code(
            generated_code,
            session_id=conversation_id[:8]  # Use short session ID
        )

        # Build response message
        if execution_result.error:
            response_message = f"Code generated but execution failed: {execution_result.error}"
        elif execution_result.files:
            response_message = f"Code executed successfully. Generated {len(execution_result.files)} file(s)."
        else:
            response_message = "Code executed successfully."

        # Add assistant response to conversation
        conversation_manager.add_message(
            conversation_id,
            "assistant",
            f"Generated and executed code. Output: {execution_result.output[:200]}"
        )

        # Build response
        response = QueryResponse(
            conversationId=conversation_id,
            clarificationNeeded=False,
            message=response_message,
            codeExecution=execution_result,
            isProMode=True
        )

        # Save to user history if authenticated
        if user:
            save_to_user_history(user, request.query, conversation_id, None, None)

        # Log query to Supabase (for both authenticated and anonymous users)
        schedule_query_log_to_supabase(
            query=request.query,
            user=user,
            session_id=None if user else conversation_id,
            conversation_id=conversation_id,
            pro_mode=True,
            code_execution=execution_result.model_dump() if execution_result else None,
            error_message=execution_result.error if execution_result else None,
        )

        return response

    except Exception as e:
        logger.error(f"Pro Mode error: {str(e)}", exc_info=True)
        # Sanitize error message - don't expose internal details to client
        return QueryResponse(
            conversationId=request.conversationId or str(uuid.uuid4()),
            clarificationNeeded=False,
            error="pro_mode_error",
            message="Pro Mode encountered an error. Please try again or simplify your query.",
            isProMode=True
        )


@app.post(
    "/api/query/pro/stream",
    operation_id="query_pro_mode_stream",
    summary="Pro Mode with real-time streaming updates",
    description="Same as /api/query/pro but streams progress updates in real-time using Server-Sent Events. Requires authentication.",
    tags=["Pro Mode"],
    dependencies=[Depends(require_promode)],
)
async def query_pro_stream_endpoint(request: QueryRequest, user: Optional[User] = Depends(get_optional_user)):
    """Streaming Pro Mode endpoint with real-time code generation and execution updates"""
    import json
    import asyncio

    if not request.query:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"error": "Query is required"})

    logger.info("⚡ Pro Mode Stream Query: %s (conversation: %s, user: %s)", request.query, request.conversationId, user.id)

    # Queue for streaming events
    event_queue: asyncio.Queue = asyncio.Queue()

    async def generate_events():
        """Generate Server-Sent Events for Pro Mode with proper resource cleanup"""
        from backend.utils.processing_steps import ProcessingTracker, activate_processing_tracker, reset_processing_tracker
        from backend.services.grok import get_grok_service
        from backend.services.code_executor import get_code_executor
        from backend.services.session_storage import get_session_storage

        tracker_token = None

        try:
            # Create tracker with callback
            def stream_callback(step):
                event = StreamEvent(
                    event="step",
                    data={
                        "step": step.step,
                        "description": step.description,
                        "duration_ms": step.duration_ms,
                        "metadata": step.metadata
                    }
                )
                try:
                    event_queue.put_nowait(event)
                except Exception as _q_exc:
                    logger.debug("Pro Mode stream event queue put failed (client likely disconnected): %s", _q_exc)

            tracker = ProcessingTracker(stream_callback=stream_callback)
            tracker_token = activate_processing_tracker(tracker)

            # Initialize services
            grok_service = get_grok_service()
            code_executor = get_code_executor()
            session_storage = get_session_storage()

            # Get or create conversation
            conversation_id = conversation_manager.get_or_create(request.conversationId)
            conversation_history = conversation_manager.get_messages(conversation_id)

            # Check for available session data
            session_id = conversation_id[:8]
            available_keys = session_storage.list_keys(session_id)

            available_data = {}

            # Add session storage context
            if available_keys:
                available_data["session_data_available"] = available_keys
                available_data["note"] = "Use load_session(key) to access this data - it's already fetched and ready!"

            # Dynamically discover Statistics Canada metadata for categorical queries
            from backend.services.statscan_metadata import get_statscan_metadata_service
            from backend.services.query_complexity import QueryComplexityAnalyzer

            # Analyze query for categorical patterns
            analysis = QueryComplexityAnalyzer.detect_complexity(request.query)

            # If query is categorical and mentions StatsCan indicators, discover metadata
            if 'categorical_breakdown' in analysis.get('complexity_factors', []):
                logger.info(f"🔍 Categorical query detected, attempting dynamic metadata discovery...")

                # Try to extract indicator from query
                indicator_keywords = {
                    'unemployment': 'Labour Force',
                    'employment': 'Labour Force',
                    'population': 'Population',
                    'gdp': 'Gross domestic product',
                    'immigration': 'Immigration',
                    'wages': 'Wages',
                }

                indicator_found = None
                query_lower = request.query.lower()
                for keyword, search_term in indicator_keywords.items():
                    if keyword in query_lower:
                        indicator_found = search_term
                        break

                if indicator_found:
                    metadata_service = get_statscan_metadata_service()
                    try:
                        # Discover metadata for the indicator
                        discovered = await metadata_service.discover_for_query(
                            indicator=indicator_found,
                            category=None  # Let it find all dimensions
                        )

                        if discovered:
                            logger.info(
                                f"✅ Discovered StatsCan metadata: product {discovered['product_id']} "
                                f"with {discovered['dimension_count']} dimensions"
                            )
                            available_data["statscan_metadata"] = {
                                "product_id": discovered["product_id"],
                                "product_title": discovered["product_title"],
                                "dimensions": discovered["dimensions"],
                                "cube_start_date": discovered.get("cube_start_date"),
                                "cube_end_date": discovered.get("cube_end_date"),
                                "note": (
                                    f"Discovered metadata for {discovered['product_title']}. "
                                    f"Use coordinate API with product_id={discovered['product_id']} "
                                    f"and dimension IDs from 'dimensions' dict."
                                )
                            }
                        else:
                            logger.warning(f"No metadata discovered for '{indicator_found}'")
                    except Exception as e:
                        logger.exception(f"Error discovering StatsCan metadata: {e}")

            # If no metadata discovered, provide fallback vector IDs
            if "statscan_metadata" not in available_data:
                from backend.providers.statscan import StatsCanProvider
                available_data["statscan_vectors"] = {
                    "GDP": 65201210,
                    "UNEMPLOYMENT": 2062815,  # Overall unemployment rate, 15 years and over
                    "INFLATION": 41690973,
                    "CPI": 41690914,
                    "POPULATION": 1,
                    "HOUSING_STARTS": 50483,
                    "EMPLOYMENT_RATE": 14609,
                    "note": "These are VERIFIED vector IDs that work with Vector API (getDataFromVectorsAndLatestNPeriods). For categorical breakdowns, Pro Mode will discover appropriate dimensions."
                }

            # Always provide available_data (even if just metadata)
            if not available_data:
                available_data = None

            # Add current user query to conversation
            conversation_manager.add_message(conversation_id, "user", request.query)

            # Step 1: Initialize session
            init_step = {
                "step": "initializing_session",
                "description": "🔧 Initializing Pro Mode session...",
                "status": "in_progress",
                "metadata": {"conversation_id": conversation_id, "session_id": session_id}
            }
            yield f"event: step\ndata: {json.dumps(init_step)}\n\n"

            async def initialize_session():
                with tracker.track("initializing_session", "🔧 Initializing Pro Mode session...", {"conversation_id": conversation_id}):
                    await asyncio.sleep(0.3)  # Simulate initialization
                return True

            init_task = asyncio.create_task(initialize_session())
            while not init_task.done():
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                    yield f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"
                except asyncio.TimeoutError:
                    pass
            while not event_queue.empty():
                event = event_queue.get_nowait()
                yield f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"
            await init_task

            # Step 2: Analyze query
            analyze_step = {
                "step": "analyzing_query",
                "description": "🔍 Analyzing your request...",
                "status": "in_progress",
                "metadata": {"conversation_id": conversation_id, "query_length": len(request.query)}
            }
            yield f"event: step\ndata: {json.dumps(analyze_step)}\n\n"

            async def analyze_query():
                with tracker.track("analyzing_query", "🔍 Analyzing your request...", {"conversation_id": conversation_id}):
                    await asyncio.sleep(0.2)  # Simulate analysis
                return True

            analyze_task = asyncio.create_task(analyze_query())
            while not analyze_task.done():
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                    yield f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"
                except asyncio.TimeoutError:
                    pass
            while not event_queue.empty():
                event = event_queue.get_nowait()
                yield f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"
            await analyze_task

            # Step 3: Generate code
            logger.info("🤖 Generating code with Grok (conversation: %s, history: %d)...", conversation_id, len(conversation_history))

            gen_code_step = {
                "step": "generating_code",
                "description": "🤖 Generating custom Python code...",
                "status": "in_progress",
                "metadata": {"conversation_id": conversation_id, "history_length": len(conversation_history)}
            }
            yield f"event: step\ndata: {json.dumps(gen_code_step)}\n\n"

            # Stream code generation in real-time
            generated_code_chunks = []
            code_gen_start = time.time()

            async for code_chunk in grok_service.generate_code_stream(
                query=request.query,
                conversation_history=conversation_history,
                available_data=available_data,
                session_id=session_id
            ):
                generated_code_chunks.append(code_chunk)
                # Yield code chunks as they arrive
                yield f"event: code_chunk\ndata: {json.dumps({'chunk': code_chunk})}\n\n"

            generated_code = "".join(generated_code_chunks)
            # Strip markdown code fences from streaming response
            generated_code = grok_service._extract_code_from_markdown(generated_code)
            code_gen_duration = (time.time() - code_gen_start) * 1000

            # Emit completion event for tracking
            with tracker.track(
                "generating_code",
                "🤖 Generating custom Python code...",
                {"conversation_id": conversation_id, "history_length": len(conversation_history), "duration_ms": code_gen_duration}
            ):
                pass  # Tracker just records the duration

            # Drain any pending events from tracker
            while not event_queue.empty():
                event = event_queue.get_nowait()
                yield f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"

            # Step 4: Validate code security
            validate_step = {
                "step": "validating_code",
                "description": "✅ Validating code security...",
                "status": "in_progress",
                "metadata": {"conversation_id": conversation_id, "code_length": len(generated_code)}
            }
            yield f"event: step\ndata: {json.dumps(validate_step)}\n\n"

            async def validate_code():
                with tracker.track("validating_code", "✅ Validating code security...", {"conversation_id": conversation_id}):
                    # Actual validation happens in code_executor, this is just a progress step
                    await asyncio.sleep(0.2)
                return True

            validate_task = asyncio.create_task(validate_code())
            while not validate_task.done():
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                    yield f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"
                except asyncio.TimeoutError:
                    pass
            while not event_queue.empty():
                event = event_queue.get_nowait()
                yield f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"
            await validate_task

            # Step 5: Check for package installations (conditional)
            # Only show this step if code contains import statements
            import re
            has_imports = bool(re.search(r'^\s*(import|from)\s+\w+', generated_code, re.MULTILINE))

            if has_imports:
                install_step = {
                    "step": "installing_packages",
                    "description": "📦 Checking package dependencies...",
                    "status": "in_progress",
                    "metadata": {"conversation_id": conversation_id}
                }
                yield f"event: step\ndata: {json.dumps(install_step)}\n\n"

                async def check_packages():
                    with tracker.track("installing_packages", "📦 Checking package dependencies...", {"conversation_id": conversation_id}):
                        await asyncio.sleep(0.3)
                    return True

                install_task = asyncio.create_task(check_packages())
                while not install_task.done():
                    try:
                        event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                        yield f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"
                    except asyncio.TimeoutError:
                        pass
                while not event_queue.empty():
                    event = event_queue.get_nowait()
                    yield f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"
                await install_task

            # Step 6: Execute code
            logger.info("⚡ Executing generated code with session: %s...", session_id)

            exec_step = {
                "step": "executing_code",
                "description": "⚡ Executing Python code...",
                "status": "in_progress",
                "metadata": {"conversation_id": conversation_id}
            }
            yield f"event: step\ndata: {json.dumps(exec_step)}\n\n"

            async def execute_code_with_streaming():
                with tracker.track(
                    "executing_code",
                    "⚡ Executing Python code...",
                    {"conversation_id": conversation_id}
                ) as update_execution_metadata:
                    execution_result = await code_executor.execute_code(
                        generated_code,
                        session_id=session_id
                    )
                    update_execution_metadata({
                        "has_error": bool(execution_result.error),
                        "files": len(execution_result.files or []),
                    })
                await asyncio.sleep(0)
                return execution_result

            exec_task = asyncio.create_task(execute_code_with_streaming())
            while not exec_task.done():
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                    yield f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"
                except asyncio.TimeoutError:
                    pass
            while not event_queue.empty():
                event = event_queue.get_nowait()
                yield f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"
            execution_result = await exec_task

            # Step 7: Process results
            process_step = {
                "step": "processing_results",
                "description": "📊 Processing results...",
                "status": "in_progress",
                "metadata": {"conversation_id": conversation_id, "has_files": bool(execution_result.files)}
            }
            yield f"event: step\ndata: {json.dumps(process_step)}\n\n"

            async def process_results():
                with tracker.track("processing_results", "📊 Processing results...", {"conversation_id": conversation_id}):
                    await asyncio.sleep(0.2)
                return True

            process_task = asyncio.create_task(process_results())
            while not process_task.done():
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=0.1)
                    yield f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"
                except asyncio.TimeoutError:
                    pass
            while not event_queue.empty():
                event = event_queue.get_nowait()
                yield f"event: {event.event}\ndata: {json.dumps(event.data)}\n\n"
            await process_task

            reset_processing_tracker(tracker_token)

            # Build response message
            if execution_result.error:
                response_message = f"Code generated but execution failed: {execution_result.error}"
            elif execution_result.files:
                response_message = f"Code executed successfully. Generated {len(execution_result.files)} file(s)."
            else:
                response_message = "Code executed successfully."

            # Add assistant response to conversation
            conversation_manager.add_message(
                conversation_id,
                "assistant",
                f"Generated and executed code. Output: {execution_result.output[:200]}"
            )

            # Build response
            response = QueryResponse(
                conversationId=conversation_id,
                clarificationNeeded=False,
                message=response_message,
                codeExecution=execution_result,
                isProMode=True,
                processingSteps=tracker.to_list()
            )

            # Save to user history if authenticated
            if user:
                save_to_user_history(user, request.query, conversation_id, None, None)

            # Log query to Supabase (for both authenticated and anonymous users)
            schedule_query_log_to_supabase(
                query=request.query,
                user=user,
                session_id=None if user else conversation_id,
                conversation_id=conversation_id,
                pro_mode=True,
                code_execution=execution_result.model_dump() if execution_result else None,
                error_message=execution_result.error if execution_result else None,
            )

            # Send final result
            if response.error:
                error_data = {"error": response.error, "message": response.message or response.error}
                yield f"event: error\ndata: {json.dumps(error_data)}\n\n"
            else:
                result_dict = response.model_dump(mode="json")
                yield f"event: data\ndata: {json.dumps(result_dict)}\n\n"

            # Send done event
            done_data = {"conversationId": conversation_id}
            yield f"event: done\ndata: {json.dumps(done_data)}\n\n"

        except asyncio.CancelledError:
            # Client disconnected - cleanup handled below
            logger.info("📡 Pro Mode stream cancelled (client disconnect)")
        except Exception as e:
            logger.exception("Pro Mode streaming error")
            # Sanitize error message - don't expose internal details to client
            error_event = {
                "error": "pro_mode_error",
                "message": "Pro Mode encountered an error. Please try again or simplify your query."
            }
            yield f"event: error\ndata: {json.dumps(error_event)}\n\n"
            yield f"event: done\ndata: {json.dumps({})}\n\n"
        finally:
            # Always cleanup resources
            if tracker_token is not None:
                try:
                    reset_processing_tracker(tracker_token)
                except Exception as cleanup_error:
                    logger.warning(f"Error resetting Pro Mode tracker: {cleanup_error}")
            logger.info("📡 Pro Mode streaming resources cleaned up")

    return StreamingResponse(
        generate_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


@app.post("/api/export")
async def export_endpoint(request: ExportRequest) -> Response:
    if not request.data:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"error": "Data is required"})

    if request.format not in {"csv", "json", "dta"}:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"error": "Format must be csv, json, or dta"})

    if request.format == "csv":
        content = export_service.generate_csv(request.data)
        media_type = "text/csv"
    elif request.format == "dta":
        content = export_service.generate_dta(request.data)
        media_type = "application/x-stata-dta"
    else:
        content = export_service.generate_json(request.data)
        media_type = "application/json"

    filename = request.filename or export_service.generate_filename(request.data, request.format)
    # Sanitize filename to prevent path traversal and header injection
    import re as _re
    filename = _re.sub(r'[^\w\s\-.]', '', filename.split('/')[-1].split('\\')[-1])[:200]
    if not filename:
        filename = "export"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}

    return Response(content=content, media_type=media_type, headers=headers)


@app.post("/api/feedback", response_model=FeedbackResponse)
async def submit_feedback(request: FeedbackRequest):
    """
    Submit user feedback (bug report, feature request, or other).

    Persists the feedback synchronously (fast — JSON append) and dispatches
    the email notification as a background asyncio.to_thread task so the
    blocking SMTP/HTTP calls do not stall the event loop. Phase 3.8.
    """
    try:
        response = await feedback_service.submit_feedback_async(request)
        return response
    except Exception as e:
        logger.error(f"Failed to submit feedback: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to submit feedback. Please try again later."
        )


@app.get("/api/debug/conversation-state/{conversation_id}")
async def debug_conversation_state(
    conversation_id: str,
    user: User = Depends(get_required_user),
):
    """Debug endpoint: inspect conversation state for a given ID. Dev-only.

    SECURITY: previously gated solely on ALLOW_TEST_USER env var. An accidental
    production flag flip would expose every conversation's state (including
    other users') to anonymous callers. Now requires authentication AND the
    dev gate.
    """
    # Only available when ALLOW_TEST_USER is set (development/testing)
    import os as _os
    if not _os.getenv("ALLOW_TEST_USER"):
        return JSONResponse(status_code=404, content={"error": "Not found"})
    ctx = conversation_manager._get(conversation_id)
    if not ctx:
        return {"found": False, "conversation_id": conversation_id}
    state = ctx.conversation_state
    return {
        "found": True,
        "conversation_id": conversation_id,
        "message_count": len(ctx.messages),
        "has_last_intent": ctx.last_intent is not None,
        "has_conversation_state": state is not None,
        "state": state.model_dump() if state and hasattr(state, 'model_dump') else str(state),
        "last_intent_indicators": ctx.last_intent.indicators if ctx.last_intent else None,
        "last_intent_provider": ctx.last_intent.apiProvider if ctx.last_intent else None,
    }


@app.get("/api/cache/stats")
async def cache_stats(user: User = Depends(get_required_user)):
    """Get cache statistics. Requires authentication."""
    return cache_service.get_stats()


@app.post("/api/cache/clear")
async def cache_clear(user: User = Depends(get_required_user)):
    """Clear the cache. Requires authentication."""
    logger.info(f"Cache cleared by user: {user.email}")
    redis_deleted = 0
    try:
        redis_cache = await get_redis_cache()
        redis_deleted = await redis_cache.clear_all()
    except Exception as exc:
        logger.warning(f"Redis cache clear failed, continuing with in-memory clear: {exc}")
    cache_service.clear()
    return {"message": "Cache cleared", "redisDeleted": redis_deleted}


@app.get("/api/performance/metrics")
async def performance_metrics():
    """Get detailed performance metrics for all components."""
    from .services.http_pool import HTTPClientPool
    from .services.circuit_breaker import CircuitBreakerRegistry

    http_pool_stats = HTTPClientPool.get_stats()
    circuit_breaker_stats = CircuitBreakerRegistry.get_all_stats()
    cache_stats = cache_service.get_stats()

    metadata_loader = getattr(app.state, "metadata_loader", None)
    metadata_status = None
    if metadata_loader:
        metadata_status = metadata_loader.get_status()

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "http_pool": http_pool_stats,
        "circuit_breakers": circuit_breaker_stats,
        "cache": cache_stats,
        "metadata_loader": metadata_status,
    }


@app.get("/api/performance/status")
async def performance_status():
    """Get current system status and bottleneck indicators."""
    cache_stats = cache_service.get_stats()

    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "cache": {
            "hit_rate": cache_stats.get("hit_rate", 0),
            "entries": cache_stats.get("keys", 0),
        },
        "optimization_enabled": {
            "http_pool": True,
            "circuit_breaker": True,
            "async_metadata_loading": settings.enable_metadata_loading,
        }
    }


@app.get("/")
async def root():
    return {"status": "ok"}


if not settings.disable_mcp:
    # Create and mount the MCP server **after** routes are registered so the OpenAPI schema
    # includes the endpoints we want to expose as tools.
    mcp = FastApiMCP(
        app,
        name="OpenEcon Data MCP Server",
        description=(
            "Query economic data from 10 providers (FRED, World Bank, IMF, BIS, Eurostat, "
            "StatsCan, Comtrade, CoinGecko, ExchangeRate-API, OECD) using natural language. "
            "Covers 330,000+ indicators. Response includes: data points, metadata with direct "
            "API URL (apiUrl) for programmatic access, source URL (sourceUrl) for verification, "
            "and alternativeSeries suggesting related indicators. "
            "Supports: year ranges ('GDP 2020-2024'), multi-country ('BRICS inflation'), "
            "variants ('female youth unemployment'), explicit provider ('from Eurostat'), "
            "and informational queries ('What FRED data is available?')."
        ),
        include_operations=[
            "query_data",
        ],
    )

    # Mount the MCP server - this adds the /mcp endpoint for SSE transport
    mcp.mount()
    logger.info("✅ MCP server mounted at /mcp endpoint")
