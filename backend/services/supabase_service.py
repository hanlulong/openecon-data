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

    def _decode_supabase_token(self, token: str) -> Optional[dict]:
        """Decode Supabase JWT token for extracting claims.

        IMPORTANT: This method is for extracting token claims only.
        For authentication, ALWAYS use get_user_from_token() which validates
        the token through Supabase's auth.get_user() API.

        Note: Supabase JWTs are signed with the project's JWT secret, not the
        service key. For proper validation, use auth.get_user() which handles
        signature verification server-side.
        """
        try:
            # Decode without verification - only for extracting claims
            # Actual authentication must use get_user_from_token() -> auth.get_user()
            payload = jwt.decode(
                token,
                options={"verify_signature": False}  # Claims extraction only
            )
            logger.warning(
                "⚠️ _decode_supabase_token used for claims extraction. "
                "For authentication, use get_user_from_token() instead."
            )
            return payload
        except JWTError as e:
            logger.debug(f"JWT decode error: {e}")
            return None

    async def get_user_from_token(self, token: str) -> Optional[User]:
        """Get user from Supabase auth token asynchronously."""
        try:
            logger.debug(f"Validating token: {token[:50]}...")

            def get_user_sync():
                return self.client.auth.get_user(token)

            response = await self._run_sync(get_user_sync)

            if not response or not response.user:
                logger.warning("Token validation failed: No user in response")
                return None

            supabase_user = response.user
            logger.info(f"Token validated for user: {supabase_user.email} (id={supabase_user.id})")

            return User(
                id=supabase_user.id,
                email=supabase_user.email or "",
                passwordHash="",
                name=supabase_user.user_metadata.get("name", supabase_user.email or "User"),
                createdAt=supabase_user.created_at,
                lastLogin=supabase_user.last_sign_in_at,
            )
        except Exception as e:
            logger.error(f"Error getting user from token: {e}")
            return None

    async def register(self, request: RegisterRequest) -> AuthResponse:
        """Register a new user with Supabase Auth asynchronously."""
        try:
            def signup_sync():
                return self.client.auth.sign_up({
                    "email": request.email,
                    "password": request.password,
                    "options": {
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

            return AuthResponse(
                success=True,
                token=response.session.access_token if response.session else None,
                user=AuthUser(
                    id=response.user.id,
                    email=response.user.email or "",
                    name=request.name,
                    createdAt=response.user.created_at,
                ),
            )
        except Exception as e:
            logger.exception("Registration error")
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
            logger.exception("Login error")
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
        """Initialize SupabaseService with async wrapper."""
        settings = get_settings()

        if not settings.supabase_url or not settings.supabase_service_key:
            logger.warning("Supabase credentials not configured - database operations will fail")
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


# ============================================================================
# FastAPI Dependency Functions
# ============================================================================

async def get_required_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)
) -> User:
    """FastAPI dependency that requires an authenticated user."""
    service = get_supabase_auth_service()
    return await service.require_user(credentials)


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)
) -> Optional[User]:
    """FastAPI dependency that optionally returns user if authenticated."""
    service = get_supabase_auth_service()
    return await service.optional_user(credentials)
