"""Mock authentication service for development without Supabase credentials."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4
from fastapi import HTTPException, status, Request
from fastapi.security import HTTPAuthorizationCredentials
from jose import jwt

from ..config import get_settings
from ..models import AuthResponse, AuthUser, LoginRequest, RegisterRequest, User

logger = logging.getLogger(__name__)


class MockUser:
    """In-memory user storage for mock auth."""

    def __init__(self, user_id: str, email: str, password_hash: str, name: str):
        self.id = user_id
        self.email = email
        self.password_hash = password_hash  # In mock, this is just the password
        self.name = name
        self.created_at = datetime.now(timezone.utc).isoformat()
        self.last_sign_in_at = None
        self.user_metadata = {"name": name}


class MockAuthService:
    """Mock authentication service using in-memory storage.

    Provides the same interface as SupabaseAuthService but stores users
    in memory. Useful for development and testing without Supabase.
    """

    def __init__(self):
        self.settings = get_settings()
        self.users: dict[str, MockUser] = {}
        self.tokens: dict[str, str] = {}  # token -> user_id mapping
        # SECURITY: Only create test user when explicitly allowed
        if os.getenv("ALLOW_TEST_USER", "").lower() == "true":
            self._create_test_user()
        else:
            logger.info("⚠️  Mock auth initialized without test user (set ALLOW_TEST_USER=true to enable)")

    def _create_test_user(self) -> None:
        """Create a test user for easy development.

        SECURITY: This should only be called when ALLOW_TEST_USER=true is set.
        The test user has a predictable password and should never be available
        in production environments.
        """
        test_user = MockUser(
            user_id=str(uuid4()),
            email="test@example.com",
            password_hash="testpass123",  # In mock, stored as plain text for testing
            name="Test User"
        )
        self.users[test_user.email] = test_user
        logger.warning(f"⚠️  Created test user: {test_user.email} - DO NOT USE IN PRODUCTION")

    def _hash_password(self, password: str) -> str:
        """Hash password (in mock, just return as-is for testing)."""
        # In a real implementation, use bcrypt or argon2
        return password

    def _verify_password(self, plain: str, hashed: str) -> bool:
        """Verify password (in mock, just compare)."""
        return plain == hashed

    def _generate_token(self, user_id: str) -> str:
        """Generate a mock JWT token."""
        try:
            payload = {
                "sub": user_id,
                "type": "access",
                "iat": int(datetime.now(timezone.utc).timestamp()),
            }
            token = jwt.encode(
                payload,
                self.settings.jwt_secret,
                algorithm="HS256"
            )
            self.tokens[token] = user_id
            return token
        except Exception as e:
            logger.error(f"Error generating token: {e}")
            raise

    async def get_user_from_token(self, token: str) -> Optional[User]:
        """Get user from mock JWT token."""
        try:
            logger.debug(f"🔐 Validating mock token: {token[:20]}...")

            # Try to find token in our token map
            user_id = self.tokens.get(token)
            if not user_id:
                # Try to decode token
                try:
                    payload = jwt.decode(
                        token,
                        self.settings.jwt_secret,
                        algorithms=["HS256"]
                    )
                    user_id = payload.get("sub")
                except Exception as e:
                    logger.debug(f"Token decode error: {e}")
                    return None

            if not user_id:
                logger.warning("❌ Token validation failed: No user ID found")
                return None

            # Find user by ID
            for user in self.users.values():
                if user.id == user_id:
                    logger.info(f"✅ Token validated for user: {user.email} (id={user.id})")
                    return User(
                        id=user.id,
                        email=user.email,
                        passwordHash="",
                        name=user.name,
                        createdAt=user.created_at,
                        lastLogin=user.last_sign_in_at,
                    )

            logger.warning(f"❌ Token validation failed: User {user_id} not found")
            return None
        except Exception as e:
            logger.error(f"❌ Error getting user from token: {e}")
            return None

    async def register(self, request: RegisterRequest) -> AuthResponse:
        """Register a new user in mock storage."""
        try:
            # Check if user already exists
            if request.email in self.users:
                return AuthResponse(success=False, error="User already exists")

            # Create new user
            user_id = str(uuid4())
            new_user = MockUser(
                user_id=user_id,
                email=request.email,
                password_hash=self._hash_password(request.password),
                name=request.name,
            )

            self.users[request.email] = new_user
            token = self._generate_token(user_id)

            logger.info(f"✅ User registered: {request.email} (id={user_id})")

            return AuthResponse(
                success=True,
                token=token,
                user=AuthUser(
                    id=user_id,
                    email=request.email,
                    name=request.name,
                    createdAt=new_user.created_at,
                ),
            )
        except Exception as e:
            logger.exception("Registration error")
            return AuthResponse(success=False, error=str(e))

    async def login(self, request: LoginRequest) -> AuthResponse:
        """Login user with mock auth."""
        try:
            # Find user by email
            user = self.users.get(request.email)
            if not user:
                return AuthResponse(success=False, error="Invalid email or password")

            # Verify password
            if not self._verify_password(request.password, user.password_hash):
                return AuthResponse(success=False, error="Invalid email or password")

            # Generate token
            token = self._generate_token(user.id)
            user.last_sign_in_at = datetime.now(timezone.utc).isoformat()

            logger.info(f"✅ User logged in: {request.email} (id={user.id})")

            return AuthResponse(
                success=True,
                token=token,
                user=AuthUser(
                    id=user.id,
                    email=user.email,
                    name=user.name,
                    createdAt=user.created_at,
                    lastLogin=user.last_sign_in_at,
                ),
            )
        except Exception as e:
            logger.exception("Login error")
            return AuthResponse(success=False, error="Invalid email or password")

    async def login_with_google(self, id_token: str) -> AuthResponse:
        """Google login not supported in mock mode."""
        return AuthResponse(success=False, error="Google login not available in mock mode")

    async def require_user(
        self,
        credentials: Optional[HTTPAuthorizationCredentials]
    ) -> User:
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

    async def optional_user(
        self,
        credentials: Optional[HTTPAuthorizationCredentials]
    ) -> Optional[User]:
        """Optionally extracts user if token is present."""
        if not credentials or credentials.scheme.lower() != "bearer":
            return None
        return await self.get_user_from_token(credentials.credentials)

    async def get_session_id(self, request: Request) -> Optional[str]:
        """Extract session ID from request (for anonymous users)."""
        # Try to get from header first
        session_id = request.headers.get("X-Session-ID")
        if session_id:
            return session_id

        # Try to get from cookie
        session_id = request.cookies.get("session_id")
        return session_id


# Singleton instance
_mock_auth_service: Optional[MockAuthService] = None


def get_mock_auth_service() -> MockAuthService:
    """Get or create the mock auth service singleton."""
    global _mock_auth_service
    if _mock_auth_service is None:
        _mock_auth_service = MockAuthService()
    return _mock_auth_service
