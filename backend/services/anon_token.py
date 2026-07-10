"""Anonymous session tokens (FIX 8).

Signed, stateless identifiers for anonymous browsers so the free-query quota can
follow the browser rather than a shared or rotating IP address.

Design: same jose HS256 scheme and secret as services/auth.py, but the payload
carries ``kind="anon"`` and a ``sid`` (opaque session id) instead of a
``userId``. The two token families can never be confused:

  * verify_anon_token() rejects any token whose ``kind`` is not ``"anon"``.
  * the login-token path (auth.get_user_from_token / main.get_optional_user)
    already rejects tokens that lack a ``userId``.

So a login JWT presented as an anon token is rejected (no ``kind``), and an anon
token presented as a login JWT is rejected (no ``userId``). No change to
services/auth.py is required.

The ``sid`` is an opaque quota identifier, not a credential: it grants no
privileges, so echoing a client-supplied session id into it carries the same
trust as today's client-chosen ``sessionId``.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt

from ..config import get_settings

# 90-day lifetime: long enough that a returning anonymous browser keeps its
# quota identity for months, short enough to bound token replay.
ANON_TOKEN_TTL_DAYS = 90
_ANON_KIND = "anon"


def issue_anon_token(sid: Optional[str] = None) -> str:
    """Mint a signed anonymous-session token.

    Args:
        sid: Stable session id to embed. When omitted a fresh uuid4 is used
            (a brand-new anonymous session). Callers may seed it from an
            existing frontend ``sessionId`` to keep the identity continuous.

    Returns:
        A compact HS256 JWT string.
    """
    settings = get_settings()
    now = datetime.now(timezone.utc)
    payload = {
        "sid": sid or str(uuid.uuid4()),
        "kind": _ANON_KIND,
        "iat": now,
        "exp": now + timedelta(days=ANON_TOKEN_TTL_DAYS),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def verify_anon_token(token: Optional[str]) -> Optional[str]:
    """Validate an anon token and return its ``sid``.

    Returns None when the token is missing, malformed, wrong-signature, expired,
    not ``kind == "anon"``, or carries no usable ``sid`` — never raises.
    """
    if not token:
        return None
    settings = get_settings()
    try:
        # jose validates the signature and the exp claim (raises on expiry).
        decoded = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except JWTError:
        return None
    if decoded.get("kind") != _ANON_KIND:
        return None
    sid = decoded.get("sid")
    if not sid or not isinstance(sid, str):
        return None
    return sid
