"""Unified Supabase service for authentication and database operations.

Consolidates supabase_auth.py and supabase_client.py functionality.

Components:
1. SupabaseAuthService: Authentication and user management via Supabase Auth
2. SupabaseService: Database operations (queries, sessions, conversations)

Notes:
- Auth operations use thread pool to prevent blocking event loop
- Database operations are wrapped with AsyncSupabase for non-blocking I/O
- All methods are async for consistency with async event loop
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial, lru_cache
from typing import Optional, Dict, Any, List

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from supabase import create_client, Client

from ..config import get_settings
from ..models import AuthResponse, AuthUser, LoginRequest, RegisterRequest, User
from .async_supabase import AsyncSupabase

logger = logging.getLogger(__name__)

# Thread pool for blocking auth operations
_auth_executor = ThreadPoolExecutor(max_workers=2)
bearer_scheme = HTTPBearer(auto_error=False)


def _token_amr_methods(token: str) -> set:
    """Return the set of auth methods (``amr``) carried by a Supabase access
    token, e.g. {"otp"} for a recovery/email-link token vs {"password"} or
    {"oauth"} for a regular interactive session.

    Claims are read WITHOUT signature verification — callers MUST have already
    authenticated the token via Supabase (``auth.get_user``) before trusting it.
    This is only used to distinguish *which flow* minted an already-valid token.
    """
    try:
        claims = jwt.get_unverified_claims(token)
        return {m.get("method") for m in (claims.get("amr") or []) if isinstance(m, dict)}
    except Exception:
        return set()


# ============================================================================
# Supabase Client Functions
# ============================================================================

@lru_cache
def get_supabase_client() -> Client:
    """Get authenticated Supabase client (service role for backend operations)."""
    settings = get_settings()
    return create_client(
        settings.supabase_url,
        settings.supabase_service_key
    )


@lru_cache
def get_supabase_anon_client() -> Client:
    """Get anonymous Supabase client (for frontend-like operations)."""
    settings = get_settings()
    return create_client(
        settings.supabase_url,
        settings.supabase_anon_key
    )


# ============================================================================
# Authentication Service
# ============================================================================

class SupabaseAuthService:
    """Authentication service using Supabase Auth.

    Uses thread pool executor for blocking Supabase auth operations to
    prevent blocking the async event loop. Auth operations are relatively
    short-lived but can take 100-500ms depending on network conditions.
    """

    def __init__(self):
        self.client = get_supabase_client()
        self.settings = get_settings()
        logger.debug("SupabaseAuthService initialized with thread pool support")

    async def _run_sync(self, func, *args, **kwargs):
        """Run synchronous function in thread pool without blocking event loop."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _auth_executor,
            partial(func, *args, **kwargs)
        )

    async def get_user_from_token(self, token: str) -> Optional[User]:
        """Validate a Supabase access token and return the user.

        STATELESS on purpose: validated against the GoTrue REST `/user` endpoint
        carrying the token's own credentials, instead of
        ``self.client.auth.get_user(token)`` which mutates the SHARED service-role
        client's session — leaking identity across requests and racing the
        2-thread auth executor (the same hazard reset_password was rewritten to
        avoid). This runs on every authenticated request, so it must be safe under
        concurrency and must never pollute shared state.
        """
        import httpx
        try:
            base = self.settings.supabase_url.rstrip("/")

            def validate_sync():
                return httpx.get(
                    f"{base}/auth/v1/user",
                    headers={
                        "apikey": self.settings.supabase_anon_key,
                        "Authorization": f"Bearer {token}",
                    },
                    timeout=10,
                )

            resp = await self._run_sync(validate_sync)
            if resp.status_code != 200:
                return None
            u = resp.json() or {}
            uid = u.get("id")
            if not uid:
                return None
            meta = u.get("user_metadata") or {}
            return User(
                id=uid,
                email=u.get("email") or "",
                passwordHash="",
                name=meta.get("name") or u.get("email") or "User",
                createdAt=u.get("created_at"),
                lastLogin=u.get("last_sign_in_at"),
            )
        except Exception as e:
            logger.warning("Token validation error: %s", str(e)[:160])
            return None

    async def register(self, request: RegisterRequest) -> AuthResponse:
        """Register a new user with Supabase Auth asynchronously."""
        try:
            def signup_sync():
                return self.client.auth.sign_up({
                    "email": request.email,
                    "password": request.password,
                    "options": {
                        "email_redirect_to": self.settings.email_confirm_redirect_url,
                        "data": {
                            "name": request.name,
                        }
                    }
                })

            response = await self._run_sync(signup_sync)

            if not response.user:
                error_message = "Registration failed"
                if hasattr(response, 'error') and response.error:
                    error_message = str(response.error)
                return AuthResponse(success=False, error=error_message)

            # When Supabase "Confirm email" is enabled, sign_up returns a user
            # but NO session — the account is unconfirmed and cannot log in until
            # they click the link we just emailed. Signal that to the UI so it
            # shows "check your inbox" rather than trying to auto-log-in.
            session = response.session
            return AuthResponse(
                success=True,
                token=session.access_token if session else None,
                emailVerificationRequired=session is None,
                user=AuthUser(
                    id=response.user.id,
                    email=response.user.email or "",
                    name=request.name,
                    createdAt=response.user.created_at,
                ),
            )
        except Exception as e:
            # Most "registration errors" are expected client outcomes (duplicate
            # email, weak password rejected by Supabase) — log without a traceback.
            logger.warning("Registration failed: %s", str(e)[:200])
            return AuthResponse(success=False, error=str(e))

    async def login(self, request: LoginRequest) -> AuthResponse:
        """Login user with Supabase Auth asynchronously."""
        try:
            def signin_sync():
                return self.client.auth.sign_in_with_password({
                    "email": request.email,
                    "password": request.password,
                })

            response = await self._run_sync(signin_sync)

            if not response.user or not response.session:
                return AuthResponse(success=False, error="Invalid email or password")

            user = response.user
            return AuthResponse(
                success=True,
                token=response.session.access_token,
                user=AuthUser(
                    id=user.id,
                    email=user.email or "",
                    name=user.user_metadata.get("name", user.email or "User"),
                    createdAt=user.created_at,
                    lastLogin=user.last_sign_in_at,
                ),
            )
        except Exception as e:
            # Distinguish "email not confirmed" from bad credentials so the user
            # knows to check their inbox rather than thinking the password is wrong.
            msg = str(getattr(e, "message", "") or e).lower()
            if "not confirmed" in msg or "email_not_confirmed" in msg:
                return AuthResponse(
                    success=False,
                    emailVerificationRequired=True,
                    error="Please confirm your email first — check your inbox for the confirmation link.",
                )
            # Invalid credentials are the common case here — log without a
            # traceback (reserve exception() for genuine infra failures).
            logger.warning("Login failed: %s", msg[:200])
            return AuthResponse(success=False, error="Invalid email or password")

    async def login_with_google(self, id_token: str) -> AuthResponse:
        """Login with Google OAuth token asynchronously."""
        try:
            def google_signin_sync():
                return self.client.auth.sign_in_with_id_token({
                    "provider": "google",
                    "token": id_token,
                })

            response = await self._run_sync(google_signin_sync)

            if not response.user or not response.session:
                return AuthResponse(success=False, error="Google authentication failed")

            user = response.user
            return AuthResponse(
                success=True,
                token=response.session.access_token,
                user=AuthUser(
                    id=user.id,
                    email=user.email or "",
                    name=user.user_metadata.get("full_name") or user.user_metadata.get("name") or user.email or "User",
                    createdAt=user.created_at,
                    lastLogin=user.last_sign_in_at,
                ),
            )
        except Exception as e:
            logger.exception("Google login error")
            return AuthResponse(success=False, error=str(e))

    async def send_password_reset(self, email: str) -> bool:
        """Email a password-reset link via Supabase (routed through our SMTP).

        Always reports success to the caller regardless of whether the address
        is registered, so the endpoint never leaks which emails have accounts.
        """
        try:
            def reset_sync():
                return self.client.auth.reset_password_for_email(
                    email,
                    {"redirect_to": self.settings.password_reset_redirect_url},
                )

            await self._run_sync(reset_sync)
        except Exception as e:
            # Unknown address / rate-limit are expected here; never raise or dump
            # a traceback (and we still return success to avoid email enumeration).
            logger.warning("Password reset email not sent: %s", str(e)[:200])
        return True

    async def reset_password(self, access_token: str, new_password: str) -> AuthResponse:
        """Set a new password using the recovery token from the emailed link.

        Implemented STATELESSLY against the GoTrue REST API on purpose. Calling
        ``self.client.auth.get_user(token)`` leaks the passed token's session
        onto the shared service-role client, so a following admin call runs as
        that user ("user not allowed" / "Session ... does not exist") and it
        races every other request that also touches the shared client. Here we
        validate the token and set the password with plain HTTP calls that carry
        their own credentials, mutating nothing shared.
        """
        import httpx
        invalid = AuthResponse(
            success=False,
            error="This reset link is invalid or has expired. Request a new one.",
        )
        try:
            # SECURITY: only tokens minted by the email-recovery flow may set a
            # password here. Recovery/email-link tokens carry an amr method of
            # 'otp'/'recovery'/'magiclink'/'email'; a normal session token carries
            # 'password' or 'oauth'. Without this, a leaked/stolen *regular*
            # session token could be POSTed here to take over the account with no
            # current-password challenge (the admin API sets the password
            # unconditionally). Reject anything that isn't a recovery-class token.
            methods = _token_amr_methods(access_token)
            if not methods & {"otp", "recovery", "magiclink", "email"}:
                logger.warning("reset_password rejected a non-recovery token (amr=%s)", methods or "<none>")
                return invalid

            base = self.settings.supabase_url.rstrip("/")

            # 1) Validate the token server-side (signature + expiry) and resolve
            #    the user id — stateless GET, no client session mutation.
            def validate_sync():
                return httpx.get(
                    f"{base}/auth/v1/user",
                    headers={
                        "apikey": self.settings.supabase_anon_key,
                        "Authorization": f"Bearer {access_token}",
                    },
                    timeout=10,
                )

            vresp = await self._run_sync(validate_sync)
            if vresp.status_code != 200:
                return invalid
            uid = (vresp.json() or {}).get("id")
            if not uid:
                return invalid

            # 2) Set the new password via the GoTrue admin REST endpoint using the
            #    service-role key directly (no shared admin client).
            def update_sync():
                return httpx.put(
                    f"{base}/auth/v1/admin/users/{uid}",
                    headers={
                        "apikey": self.settings.supabase_service_key,
                        "Authorization": f"Bearer {self.settings.supabase_service_key}",
                        "Content-Type": "application/json",
                    },
                    json={"password": new_password},
                    timeout=10,
                )

            uresp = await self._run_sync(update_sync)
            if uresp.status_code not in (200, 201):
                detail = ""
                try:
                    body = uresp.json() or {}
                    detail = str(body.get("msg") or body.get("error_description") or body.get("error") or "")
                except Exception:
                    pass
                low = detail.lower()
                if "password" in low and any(k in low for k in ("short", "least", "weak", "length", "6 char")):
                    return AuthResponse(success=False, error="Password is too weak. Use at least 8 characters.")
                logger.warning("reset_password admin update failed: %s %s", uresp.status_code, detail[:160])
                return AuthResponse(
                    success=False,
                    error="Could not reset password. The link may have expired — request a new one.",
                )

            logger.info("Password reset completed for user %s", uid)
            return AuthResponse(success=True)
        except Exception as e:
            logger.warning("Password reset failed: %s", str(getattr(e, "message", "") or e)[:200])
            return AuthResponse(
                success=False,
                error="Could not reset password. The link may have expired — request a new one.",
            )

    async def require_user(self, credentials: Optional[HTTPAuthorizationCredentials]) -> User:
        """Requires authenticated user from credentials."""
        if not credentials or credentials.scheme.lower() != "bearer":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="No token provided"
            )

        user = await self.get_user_from_token(credentials.credentials)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or expired token"
            )
        return user

    async def optional_user(self, credentials: Optional[HTTPAuthorizationCredentials]) -> Optional[User]:
        """Optionally extracts user if token is present."""
        if not credentials or credentials.scheme.lower() != "bearer":
            return None
        return await self.get_user_from_token(credentials.credentials)

    async def get_session_id(self, request: Request) -> Optional[str]:
        """Extract session ID from request (for anonymous users)."""
        session_id = request.headers.get("X-Session-ID")
        if session_id:
            return session_id
        return request.cookies.get("session_id")


# ============================================================================
# Database Service
# ============================================================================

class SupabaseService:
    """Service for Supabase database operations.

    Uses AsyncSupabase wrapper to prevent blocking the event loop
    during database operations. All methods are async and use thread
    pool execution to avoid blocking.
    """

    def __init__(self):
        """Initialize SupabaseService with async wrapper.

        SECURITY: Fails closed in production. Matches the auth_factory pattern
        — silently returning an unconfigured client allowed silent data loss
        when Supabase env vars were missing in production. Now: production
        must have credentials configured at startup; development/test still
        degrades gracefully to an unconfigured (None) client.
        """
        settings = get_settings()

        if not settings.supabase_url or not settings.supabase_service_key:
            if settings.environment == "production":
                raise RuntimeError(
                    "SECURITY ERROR: Supabase must be configured in production. "
                    "Set SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables, "
                    "or set NODE_ENV=development for local testing."
                )
            logger.warning("Supabase credentials not configured - database operations will be skipped (dev mode)")
            self.client = None
        else:
            self.client = AsyncSupabase(
                settings.supabase_url,
                settings.supabase_service_key
            )
            logger.debug("SupabaseService initialized with AsyncSupabase wrapper")

    # ========== Query Tracking ==========

    async def log_query(
        self,
        query: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        conversation_id: Optional[str] = None,
        pro_mode: bool = False,
        intent: Optional[Dict[str, Any]] = None,
        response_data: Optional[Dict[str, Any]] = None,
        code_execution: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
        processing_time_ms: Optional[float] = None,
        user_agent: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Log a user query to the database asynchronously."""
        if not self.client:
            return {}

        # Ensure at least one of user_id or session_id is set
        # This satisfies the database constraint 'valid_user_or_session'
        effective_session_id = session_id
        if not user_id and not session_id:
            # Generate anonymous session ID from conversation_id if available
            if conversation_id:
                effective_session_id = f"anon_{conversation_id}"
            else:
                import uuid
                effective_session_id = f"anon_{uuid.uuid4()}"
            logger.debug(f"Generated anonymous session ID for query logging: {effective_session_id}")

        data = {
            "query": query,
            "user_id": user_id,
            "session_id": effective_session_id,
            "conversation_id": conversation_id,
            "pro_mode": pro_mode,
            "intent": intent,
            "response_data": response_data,
            "code_execution": code_execution,
            "error_message": error_message,
            "processing_time_ms": processing_time_ms,
            "user_agent": user_agent,
            "ip_address": ip_address,
        }

        # Remove None values
        data = {k: v for k, v in data.items() if v is not None}

        result = await self.client.insert(
            "user_queries",
            data,
            timeout=2.0
        )
        return result if result else {}

    async def record_anonymous_query(
        self,
        session_id: str,
        user_agent: Optional[str] = None,
        ip_address: Optional[str] = None,
        pro_mode: bool = False,
    ) -> Optional[int]:
        """Atomically increment an anonymous session's prompt counter.

        Drives the registration gate AND keeps the anonymous_sessions analytics
        table current. Returns the NEW query_count, or None when the count can't
        be determined (no client / RPC failure) so the caller can FAIL OPEN and
        never wrongly block a user.
        """
        if not self.client or not session_id:
            return None
        try:
            result = await self.client.rpc(
                "record_anonymous_query",
                {
                    "p_session_id": str(session_id),
                    "p_user_agent": user_agent,
                    "p_ip": ip_address,
                    "p_pro_mode": bool(pro_mode),
                },
                timeout=3.0,
            )
        except Exception as exc:
            logger.debug("record_anonymous_query RPC failed: %s", exc)
            return None
        if isinstance(result, bool):
            return None
        if isinstance(result, int):
            return result
        if isinstance(result, list) and result:
            v = result[0]
            if isinstance(v, int):
                return v
            if isinstance(v, dict):
                inner = next(iter(v.values()), None)
                return inner if isinstance(inner, int) else None
        return None

    async def convert_anonymous_session(self, session_id: str, user_id: str) -> int:
        """Attach an anonymous session's query history to a newly-registered
        user (so nothing is lost at the registration wall). Returns the number
        of migrated queries (0 on failure)."""
        if not self.client or not session_id or not user_id:
            return 0
        try:
            result = await self.client.rpc(
                "convert_anonymous_session",
                {"p_session_id": str(session_id), "p_user_id": str(user_id)},
                timeout=5.0,
            )
        except Exception as exc:
            logger.debug("convert_anonymous_session RPC failed: %s", exc)
            return 0
        if isinstance(result, int):
            return result
        if isinstance(result, list) and result:
            v = result[0]
            if isinstance(v, int):
                return v
            if isinstance(v, dict):
                inner = next(iter(v.values()), 0)
                return inner if isinstance(inner, int) else 0
        return 0

    async def set_user_institution(self, user_id: str, institution: str) -> None:
        """Store the institution/company a user entered at registration (powers
        future 'trusted by' social proof). Best-effort; never raises."""
        if not self.client or not user_id or not institution:
            return
        try:
            await self.client.rpc(
                "set_user_institution",
                {"p_user_id": str(user_id), "p_institution": str(institution)},
                timeout=3.0,
            )
        except Exception as exc:
            logger.debug("set_user_institution RPC failed: %s", exc)

    async def get_user_queries(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        limit: int = 50,
        offset: int = 0
    ) -> list[Dict[str, Any]]:
        """Get user queries from database asynchronously."""
        if not self.client:
            return []

        filters = {}
        if user_id:
            filters["user_id"] = user_id
        elif session_id:
            filters["session_id"] = session_id

        return await self.client.select(
            "user_queries",
            filters=filters,
            order_by="created_at",
            order_asc=False,
            limit=limit,
            offset=offset,
            timeout=5.0
        )

    async def delete_user_queries(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> int:
        """Delete user queries from database asynchronously."""
        if not self.client:
            return 0

        if not user_id and not session_id:
            raise ValueError("Either user_id or session_id must be provided")

        filters = {}
        if user_id:
            filters["user_id"] = user_id
        elif session_id:
            filters["session_id"] = session_id

        queries = await self.client.select(
            "user_queries",
            columns="id",
            filters=filters,
            timeout=5.0
        )
        count = len(queries)

        if count > 0:
            success = await self.client.delete(
                "user_queries",
                filters,
                timeout=5.0
            )
            return count if success else 0

        return 0

    # ========== Session Tracking ==========

    async def create_or_update_session(
        self,
        session_id: str,
        user_agent: Optional[str] = None,
        ip_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create or update an anonymous session asynchronously."""
        if not self.client:
            return {}

        existing = await self.client.select(
            "anonymous_sessions",
            filters={"session_id": session_id},
            limit=1,
            timeout=5.0
        )

        if existing:
            session = existing[0]
            update_data = {}
            if user_agent:
                update_data["user_agent"] = user_agent
            if ip_address:
                update_data["ip_address"] = ip_address

            if update_data:
                await self.client.update(
                    "anonymous_sessions",
                    update_data,
                    {"session_id": session_id},
                    timeout=5.0
                )
            return session
        else:
            data = {
                "session_id": session_id,
                "user_agent": user_agent,
                "ip_address": ip_address,
            }
            result = await self.client.insert(
                "anonymous_sessions",
                data,
                timeout=5.0
            )
            return result if result else {}

    async def convert_session_to_user(
        self,
        session_id: str,
        user_id: str
    ) -> bool:
        """Mark a session as converted to a registered user asynchronously."""
        if not self.client:
            return False

        try:
            await self.client.update(
                "anonymous_sessions",
                {"converted_to_user_id": user_id},
                {"session_id": session_id},
                timeout=5.0
            )

            await self.client.update(
                "user_queries",
                {"user_id": user_id},
                {"session_id": session_id},
                timeout=5.0
            )

            return True
        except Exception as e:
            logger.error(f"Failed to convert session {session_id} to user {user_id}: {e}")
            return False

    # ========== Conversation Management ==========

    async def save_conversation(
        self,
        conversation_id: str,
        messages: list[Dict[str, Any]],
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Save or update a conversation asynchronously."""
        if not self.client:
            return {}

        existing = await self.client.select(
            "conversations",
            filters={"id": conversation_id},
            limit=1,
            timeout=5.0
        )

        data = {
            "id": conversation_id,
            "messages": messages,
            "user_id": user_id,
            "session_id": session_id,
        }

        if existing:
            await self.client.update(
                "conversations",
                data,
                {"id": conversation_id},
                timeout=5.0
            )
            return data
        else:
            result = await self.client.insert(
                "conversations",
                data,
                timeout=5.0
            )
            return result if result else {}

    async def get_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Get a conversation by ID asynchronously."""
        if not self.client:
            return None

        result = await self.client.select(
            "conversations",
            filters={"id": conversation_id},
            limit=1,
            timeout=5.0
        )
        return result[0] if result else None

    # ========== Analytics & Admin ==========

    async def get_stats(self) -> Dict[str, Any]:
        """Get overall statistics asynchronously."""
        if not self.client:
            return {
                "total_users": 0,
                "total_queries": 0,
                "total_sessions": 0,
                "error": "Supabase not configured"
            }

        try:
            users = await self.client.select(
                "user_profiles",
                columns="id",
                timeout=5.0
            )
            user_count = len(users)

            queries = await self.client.select(
                "user_queries",
                columns="id",
                timeout=5.0
            )
            query_count = len(queries)

            sessions = await self.client.select(
                "anonymous_sessions",
                columns="id",
                timeout=5.0
            )
            session_count = len(sessions)

            return {
                "total_users": user_count,
                "total_queries": query_count,
                "total_sessions": session_count,
            }
        except Exception as e:
            logger.error(f"Failed to get statistics: {e}")
            return {
                "total_users": 0,
                "total_queries": 0,
                "total_sessions": 0,
                "error": str(e)
            }

    async def get_recent_activity(self, limit: int = 20) -> list[Dict[str, Any]]:
        """Get recent query activity across all users asynchronously."""
        if not self.client:
            return []

        return await self.client.select(
            "user_queries",
            order_by="created_at",
            order_asc=False,
            limit=limit,
            timeout=5.0
        )


# ============================================================================
# Singleton Instances & Factory Functions
# ============================================================================

_auth_service: Optional[SupabaseAuthService] = None
_database_service: Optional[SupabaseService] = None


def get_supabase_auth_service() -> SupabaseAuthService:
    """Get or create the Supabase auth service singleton."""
    global _auth_service
    if _auth_service is None:
        _auth_service = SupabaseAuthService()
    return _auth_service


def get_supabase_db_service() -> SupabaseService:
    """Get or create the Supabase database service singleton."""
    global _database_service
    if _database_service is None:
        _database_service = SupabaseService()
    return _database_service


# Legacy alias for backward compatibility
def get_supabase_service() -> SupabaseService:
    """Alias for get_supabase_db_service for backward compatibility."""
    return get_supabase_db_service()


# NOTE: the FastAPI auth dependencies live in main.py (get_required_user /
# get_optional_user), which route through the auth-service FACTORY so they pick
# Supabase vs Mock correctly. An earlier duplicate pair lived here and hard-wired
# the Supabase auth service, bypassing the factory — removed to keep a single
# source of truth and avoid a foot-gun where importing from here installs a
# second, factory-ignoring auth gate.
