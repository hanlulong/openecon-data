"""Structural request-channel attribution for query analytics.

Every query row logged to Supabase carries ONE channel label so traffic can be
split by origin in SQL (``intent->'_request_meta'->>'channel'``) without a
schema migration. The label is derived ONLY from transport facts — request
headers the calling stack sets itself and the resolved peer address — never
from the query text, so it is a STRUCTURAL rule (no semantic judgment).

Channels, in precedence order:

``coze``      Coze's service mesh names itself in forwarded headers
              (``x-psm: stone.coze.*`` / ``x-tlb-server-name: data.coze.cn``).
``mcp``       fastapi_mcp's in-process self-call for an MCP ``tools/call``.
              Tagged by :func:`install_mcp_channel_tagging` with the MCP
              client's own ``clientInfo.name``; honored only from a direct
              loopback peer (the self-call never traverses a proxy).
``internal``  our own batteries/probes (``X-OpenEcon-Internal`` header). An
              analytics label only — it confers no privilege, so it is honored
              from any peer (scripts may target the public URL).
``browser``   Fetch-metadata headers (``sec-fetch-*``) or a ``Mozilla/`` UA.
``loopback``  any other direct loopback peer (local curl, dev tooling).
``api:<ua>``  everything else, keyed by the User-Agent product token
              (``api:go-http-client``, ``api:python-httpx``, ``api:unknown``).
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

INTERNAL_HEADER = "x-openecon-internal"
MCP_CLIENT_HEADER = "x-openecon-mcp-client"

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_UA_TOKEN_RE = re.compile(r"[^a-z0-9._-]+")
_MAX_LABEL = 64


def _lower_headers(headers: Any) -> Dict[str, str]:
    """Case-insensitive view over Starlette Headers or a plain mapping."""
    if headers is None:
        return {}
    try:
        items = headers.items()
    except Exception:
        return {}
    out: Dict[str, str] = {}
    for key, value in items:
        try:
            out[str(key).lower()] = str(value)
        except Exception:
            continue
    return out


def is_coze_request(headers: Any) -> bool:
    """True when Coze's service mesh forwarded the request (structural:
    the mesh names itself in x-psm / x-tlb-server-name; verified against live
    traffic 2026-07-20)."""
    h = _lower_headers(headers)
    return h.get("x-psm", "").startswith("stone.coze.") or h.get("x-tlb-server-name", "") == "data.coze.cn"


def ua_family(user_agent: Optional[str]) -> str:
    """User-Agent product token, lowercased, version stripped.

    ``Go-http-client/2.0`` -> ``go-http-client``; ``curl/8.5.0`` -> ``curl``;
    empty -> ``unknown``. Pure string structure, no semantic classification.
    """
    first = (user_agent or "").strip().split(" ", 1)[0].split("/", 1)[0]
    token = _UA_TOKEN_RE.sub("-", first.lower()).strip("-")
    return (token or "unknown")[:_MAX_LABEL]


def _clean_label(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    cleaned = "".join(ch for ch in str(value) if ch.isprintable()).strip()
    return cleaned[:100] or None


def derive_request_channel(
    headers: Any,
    *,
    direct_host: Optional[str] = None,
    forwarded_for: Optional[str] = None,
) -> Dict[str, Optional[str]]:
    """Return ``{"channel": ..., "client_name": ...}`` for one request.

    ``direct_host`` is the TCP peer (``request.client.host``) and
    ``forwarded_for`` the raw X-Forwarded-For header; together they decide
    whether the peer is a direct loopback caller (no proxy hop). Never raises.
    """
    try:
        h = _lower_headers(headers)
        xff = forwarded_for if forwarded_for is not None else h.get("x-forwarded-for")
        direct_loopback = (direct_host or "") in _LOOPBACK_HOSTS and not (xff or "").strip()

        if is_coze_request(h):
            return {"channel": "coze", "client_name": None}
        mcp_client = h.get(MCP_CLIENT_HEADER)
        if mcp_client is not None and direct_loopback:
            return {"channel": "mcp", "client_name": _clean_label(mcp_client) or "unknown"}
        if h.get(INTERNAL_HEADER) is not None:
            return {"channel": "internal", "client_name": _clean_label(h.get(INTERNAL_HEADER))}
        user_agent = h.get("user-agent", "")
        if any(k.startswith("sec-fetch-") for k in h) or user_agent.startswith("Mozilla/"):
            return {"channel": "browser", "client_name": None}
        if direct_loopback:
            return {"channel": "loopback", "client_name": None}
        return {"channel": f"api:{ua_family(user_agent)}", "client_name": None}
    except Exception as exc:  # attribution must never break a request
        logger.debug("request channel derivation failed: %s", exc)
        return {"channel": "unknown", "client_name": None}


def derive_channel_for_request(request: Any) -> Dict[str, Optional[str]]:
    """:func:`derive_request_channel` for a Starlette ``Request`` (or None)."""
    if request is None:
        return {"channel": "unknown", "client_name": None}
    try:
        client = getattr(request, "client", None)
        host = getattr(client, "host", None) if client is not None else None
        return derive_request_channel(getattr(request, "headers", None), direct_host=host)
    except Exception as exc:
        logger.debug("request channel derivation failed: %s", exc)
        return {"channel": "unknown", "client_name": None}


def _current_mcp_client_name() -> str:
    """clientInfo.name of the MCP session handling the current tools/call.

    The MCP low-level server sets ``request_ctx`` for the duration of a
    request handler, and fastapi_mcp issues its httpx self-call from inside
    that handler, so the context variable is visible here.
    """
    try:
        from mcp.server.lowlevel.server import request_ctx

        ctx = request_ctx.get()
        params = getattr(ctx.session, "client_params", None)
        info = getattr(params, "clientInfo", None) if params is not None else None
        name = getattr(info, "name", None) if info is not None else None
        return _clean_label(name) or "unknown"
    except Exception:
        return "unknown"


async def _tag_mcp_request(request: Any) -> None:
    try:
        request.headers[MCP_CLIENT_HEADER] = _current_mcp_client_name()
    except Exception as exc:  # tagging must never break a tool call
        logger.debug("MCP channel tagging failed: %s", exc)


def install_mcp_channel_tagging(http_client: Any) -> bool:
    """Add a request hook to fastapi_mcp's self-call httpx client so every
    tool invocation reaching ``/api/query`` carries the MCP client's name.

    Composes with whatever client fastapi_mcp was built with (existing hooks
    are preserved). Returns False when the client does not support hooks.
    """
    try:
        hooks = dict(http_client.event_hooks)
        request_hooks = list(hooks.get("request", []))
        if _tag_mcp_request not in request_hooks:
            request_hooks.append(_tag_mcp_request)
        hooks["request"] = request_hooks
        hooks.setdefault("response", [])
        http_client.event_hooks = hooks
        return True
    except Exception as exc:
        logger.warning("Could not install MCP channel tagging: %s", exc)
        return False
