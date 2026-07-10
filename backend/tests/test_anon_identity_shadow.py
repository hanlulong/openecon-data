"""Anonymous-identity resolution + SHADOW MODE (FIX 8).

Covers ip_identity (/64 masking), resolve_anon_identity priority order, and the
core safety property: shadow mode MEASURES the new scheme (one log line per
anonymous query) but must NOT change the gate decision or the gating key — the
block/allow behavior is identical to legacy mode.
"""
from __future__ import annotations

import logging

import pytest

import backend.main as m
from backend.services.anon_token import issue_anon_token


# ---------------------------------------------------------------------------
# Fake request
# ---------------------------------------------------------------------------

class _Headers(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class _Client:
    def __init__(self, host):
        self.host = host


class _Req:
    def __init__(self, headers=None, host="198.51.100.5"):
        self.headers = _Headers(headers or {})
        self.client = _Client(host)


# ---------------------------------------------------------------------------
# ip_identity
# ---------------------------------------------------------------------------

def test_ip_identity_ipv4_used_whole():
    assert m.ip_identity("203.0.113.7") == "ip4:203.0.113.7"


def test_ip_identity_ipv6_masked_to_64():
    # Host bits below the /64 must be dropped so a rotating /64 is one identity.
    a = m.ip_identity("2001:db8:abcd:1234:5678:9abc:def0:1111")
    b = m.ip_identity("2001:db8:abcd:1234:ffff:ffff:ffff:ffff")
    assert a == "ip6:2001:db8:abcd:1234::/64"
    assert a == b  # same /64 → same identity


def test_ip_identity_rejects_garbage_and_empty():
    assert m.ip_identity("not-an-ip") is None
    assert m.ip_identity("") is None
    assert m.ip_identity(None) is None


# ---------------------------------------------------------------------------
# resolve_anon_identity priority: token > legacy sessionId > IP
# ---------------------------------------------------------------------------

def test_priority_1_valid_token_wins():
    tok = issue_anon_token("sess-xyz")
    req = _Req({"X-OE-Session": tok})
    # Even with a legacy sessionId AND an IP present, the token wins.
    assert m.resolve_anon_identity(req, "legacy-sess") == ("tok:sess-xyz", "token")


def test_priority_2_legacy_session_when_no_token():
    req = _Req({})
    assert m.resolve_anon_identity(req, "legacy-sess") == ("legacy-sess", "legacy")


def test_priority_3_ip_fallback_when_nothing_else():
    req = _Req({}, host="198.51.100.5")
    assert m.resolve_anon_identity(req, None) == ("ip4:198.51.100.5", "ip4")


def test_invalid_token_falls_through_to_legacy():
    req = _Req({"X-OE-Session": "garbage.token"})
    assert m.resolve_anon_identity(req, "legacy-sess") == ("legacy-sess", "legacy")


def test_body_token_used_when_no_header():
    tok = issue_anon_token("body-sid")
    req = _Req({})
    assert m.resolve_anon_identity(req, "legacy", body_token=tok) == ("tok:body-sid", "token")


# ---------------------------------------------------------------------------
# SHADOW MODE must not change the gate decision (mode=shadow vs legacy)
# ---------------------------------------------------------------------------

class _FakeSupabase:
    def __init__(self, count):
        self._count = count

    async def record_anonymous_query(self, *args, **kwargs):
        return self._count


def _install_count(monkeypatch, count):
    monkeypatch.setattr(m, "get_supabase_service", lambda: _FakeSupabase(count))


@pytest.mark.parametrize("mode", ["legacy", "shadow"])
async def test_gate_decision_identical_across_modes(monkeypatch, mode):
    monkeypatch.setattr(m.settings, "anon_query_limit", 20)
    monkeypatch.setattr(m.settings, "anon_identity_mode", mode)
    req = _Req({"user-agent": "pytest"})

    # Under the limit → allowed; the gate keys on the LEGACY session id.
    _install_count(monkeypatch, 10)
    gate = await m.check_anon_gate(req, user=None, body_session_id="s1", conversation_id=None)
    assert gate.blocked is False
    assert gate.session_id == "s1"  # gating key unchanged by mode

    # Over the limit → blocked, identically in both modes.
    _install_count(monkeypatch, 21)
    gate = await m.check_anon_gate(req, user=None, body_session_id="s1", conversation_id=None)
    assert gate.blocked is True
    assert gate.session_id == "s1"


async def test_shadow_logs_once_legacy_does_not(monkeypatch, caplog):
    monkeypatch.setattr(m.settings, "anon_query_limit", 20)
    req = _Req({"user-agent": "pytest"})
    _install_count(monkeypatch, 5)

    # Shadow mode emits exactly one measurement line.
    monkeypatch.setattr(m.settings, "anon_identity_mode", "shadow")
    with caplog.at_level(logging.INFO, logger="openecon"):
        caplog.clear()
        await m.check_anon_gate(req, user=None, body_session_id="s1", conversation_id=None)
    shadow_lines = [r for r in caplog.records if "anon_identity_shadow" in r.getMessage()]
    assert len(shadow_lines) == 1

    # Legacy mode emits none.
    monkeypatch.setattr(m.settings, "anon_identity_mode", "legacy")
    with caplog.at_level(logging.INFO, logger="openecon"):
        caplog.clear()
        await m.check_anon_gate(req, user=None, body_session_id="s1", conversation_id=None)
    assert not [r for r in caplog.records if "anon_identity_shadow" in r.getMessage()]


async def test_registered_user_not_gated_or_shadow_logged(monkeypatch, caplog):
    # Registered users are exempt: no counting, no shadow line.
    monkeypatch.setattr(m.settings, "anon_query_limit", 20)
    monkeypatch.setattr(m.settings, "anon_identity_mode", "shadow")

    class _U:
        id = "user-1"

    req = _Req({"user-agent": "pytest"})
    with caplog.at_level(logging.INFO, logger="openecon"):
        caplog.clear()
        gate = await m.check_anon_gate(req, user=_U(), body_session_id="s1", conversation_id=None)
    assert gate.blocked is False
    assert not [r for r in caplog.records if "anon_identity_shadow" in r.getMessage()]


# ---------------------------------------------------------------------------
# Token issuance helper
# ---------------------------------------------------------------------------

def test_maybe_issue_mints_when_absent(monkeypatch):
    monkeypatch.setattr(m.settings, "anon_identity_mode", "shadow")
    req = _Req({})
    tok = m.maybe_issue_anon_token(req, "sess-1")
    from backend.services.anon_token import verify_anon_token
    # sid seeded from the legacy sessionId for continuity.
    assert verify_anon_token(tok) == "sess-1"


def test_maybe_issue_skips_when_valid_token_present(monkeypatch):
    monkeypatch.setattr(m.settings, "anon_identity_mode", "shadow")
    existing = issue_anon_token("already")
    req = _Req({"X-OE-Session": existing})
    assert m.maybe_issue_anon_token(req, "sess-1") is None


def test_maybe_issue_disabled_in_legacy_mode(monkeypatch):
    monkeypatch.setattr(m.settings, "anon_identity_mode", "legacy")
    req = _Req({})
    assert m.maybe_issue_anon_token(req, "sess-1") is None
