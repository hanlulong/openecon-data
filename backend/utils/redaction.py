"""Keep provider credentials out of error text and logs.

httpx puts the full request URL into ``HTTPStatusError`` messages, and several
providers send their key as a query parameter (FRED ``api_key``, CoinGecko
``x_cg_demo_api_key`` / ``x_cg_pro_api_key``) or as a path segment
(ExchangeRate-API ``/v6/<key>/``). Any error text built from those exceptions
must pass through :func:`redact_secrets` before it is raised, logged or shown.

Stdlib only, so both providers and services can import it without cycles.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional
from urllib.parse import quote

MASK = "***"

# Parameter NAMES whose values are credentials, matched on the name's suffix
# (so x_cg_demo_api_key and x_cg_pro_api_key are covered). A bare `key` is
# deliberately not matched: IMF uses `key=` for SDMX series keys.
_SECRET_PARAM_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])"
    r"([A-Za-z0-9_.\-]*(?:api_key|api-key|apikey|token|subscription-key|subscription_key|registrationkey))"
    r"=([^&#\s'\"]+)",
    re.IGNORECASE,
)

# Shorter values are too likely to collide with ordinary text.
_MIN_SECRET_LENGTH = 8


def redact_secrets(text: str, secrets: Iterable[Optional[str]] = ()) -> str:
    """Mask credentials in ``text``.

    (a) The value of every ``name=value`` pair whose name ends in a credential
        suffix (api_key, api-key, apikey, token, subscription-key,
        subscription_key, registrationkey; case-insensitive) becomes ``***``.
    (b) Every literal occurrence of each configured secret of 8+ characters,
        raw or percent-encoded, becomes ``***``. This covers keys carried in a
        URL path.
    """
    if not text:
        return text
    redacted = str(text)
    for secret in secrets or ():
        if not isinstance(secret, str) or len(secret) < _MIN_SECRET_LENGTH:
            continue
        for form in {secret, quote(secret, safe="")}:
            redacted = redacted.replace(form, MASK)
    return _SECRET_PARAM_RE.sub(lambda m: f"{m.group(1)}={MASK}", redacted)
