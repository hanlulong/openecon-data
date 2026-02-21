from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
import bcrypt

from ..config import get_settings
from ..models import AuthResponse, AuthUser, LoginRequest, RegisterRequest, User
from .user_store import user_store


bearer_scheme = HTTPBearer(auto_error=False)


class AuthService:
    def __init__(self, secret: str, expires_days: int) -> None:
        self.secret = secret
        self.expires_days = expires_days

    @staticmethod
    def _validate_email(email: str) -> bool:
        return bool(re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email))

    def _hash_password(self, password: str) -> str:
        # Use bcrypt directly - it auto-truncates to 72 bytes
        password_bytes = password.encode('utf-8')
        salt = bcrypt.gensalt()
        hashed = bcrypt.hashpw(password_bytes, salt)
        return hashed.decode('utf-8')

    def _verify_password(self, plain_password: str, hashed_password: str) -> bool:
        # Verify using bcrypt directly
        password_bytes = plain_password.encode('utf-8')
        hashed_bytes = hashed_password.encode('utf-8')
        return bcrypt.checkpw(password_bytes, hashed_bytes)

    def _generate_token(self, user_id: str) -> str:
        expire = datetime.now(timezone.utc) + timedelta(days=self.expires_days)
        payload = {"userId": user_id, "exp": expire}
        return jwt.encode(payload, self.secret, algorithm="HS256")

    def _decode_token(self, token: str) -> Optional[dict]:
        try:
            return jwt.decode(token, self.secret, algorithms=["HS256"])
        except JWTError:
            return None

    def get_user_from_token(self, token: str) -> Optional[User]:
        decoded = self._decode_token(token)
        if not decoded:
            return None
        user_id = decoded.get("userId")
        if not user_id:
            return None
        return user_store.get_user_by_id(user_id)

    def register(self, request: RegisterRequest) -> AuthResponse:
        if not request.email or not request.password or not request.name:
            return AuthResponse(success=False, error="Email, password, and name are required")

        if not self._validate_email(request.email):
            return AuthResponse(success=False, error="Invalid email format")

        if len(request.password) < 12:
            return AuthResponse(success=False, error="Password must be at least 12 characters long")

        # Check password complexity
        if not any(c.isupper() for c in request.password):
            return AuthResponse(success=False, error="Password must contain at least one uppercase letter")
        if not any(c.islower() for c in request.password):
            return AuthResponse(success=False, error="Password must contain at least one lowercase letter")
        if not any(c.isdigit() for c in request.password):
            return AuthResponse(success=False, error="Password must contain at least one digit")

        if user_store.get_user_by_email(request.email):
            return AuthResponse(success=False, error="User with this email already exists")

        password_hash = self._hash_password(request.password)

        user = User(
            id=str(uuid.uuid4()),
            email=request.email,
            passwordHash=password_hash,
            name=request.name,
            createdAt=datetime.now(timezone.utc),
        )
        user_store.create_user(user)

        token = self._generate_token(user.id)
        return AuthResponse(
            success=True,
            token=token,
            user=AuthUser(id=user.id, email=user.email, name=user.name, createdAt=user.createdAt),
        )

    def login(self, request: LoginRequest) -> AuthResponse:
        if not request.email or not request.password:
            return AuthResponse(success=False, error="Email and password are required")

        user = user_store.get_user_by_email(request.email)
        if not user:
            return AuthResponse(success=False, error="Invalid email or password")

        if not self._verify_password(request.password, user.passwordHash):
            return AuthResponse(success=False, error="Invalid email or password")

        updated_user = user_store.update_user(user.id, lastLogin=datetime.now(timezone.utc))

        token = self._generate_token(user.id)
        return AuthResponse(
            success=True,
            token=token,
            user=AuthUser(
                id=user.id,
                email=user.email,
                name=user.name,
                createdAt=user.createdAt,
                lastLogin=updated_user.lastLogin if updated_user else None,
            ),
        )

    def require_user(self, credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> User:
        if not credentials or credentials.scheme.lower() != "bearer":
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="No token provided")

        user = self.get_user_from_token(credentials.credentials)
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")
        return user

    def optional_user(self, credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme)) -> Optional[User]:
        if not credentials or credentials.scheme.lower() != "bearer":
            return None
        return self.get_user_from_token(credentials.credentials)


def get_auth_service() -> AuthService:
    settings = get_settings()
    return AuthService(secret=settings.jwt_secret, expires_days=settings.jwt_expiration_days)
