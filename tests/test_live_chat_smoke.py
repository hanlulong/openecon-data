from __future__ import annotations

import os
import time
import uuid
from typing import Any, Dict, Optional

import pytest
import requests


LIVE_SMOKE_ENABLED = os.environ.get("OPENECON_LIVE_SMOKE") == "1"
BASE_URL = os.environ.get("OPENECON_LIVE_SMOKE_URL", "https://data.openecon.ai").rstrip("/")
QUERY_URL = f"{BASE_URL}/api/query"
TIMEOUT_SECONDS = float(os.environ.get("OPENECON_LIVE_SMOKE_TIMEOUT", "120"))

pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(
        not LIVE_SMOKE_ENABLED,
        reason="Set OPENECON_LIVE_SMOKE=1 to run live production smoke tests.",
    ),
]


class LiveApiClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session_id = str(uuid.uuid4())
        self.conversation_id: Optional[str] = None

    def query(self, text: str) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"query": text, "sessionId": self.session_id}
        if self.conversation_id:
            payload["conversationId"] = self.conversation_id

        last_error: Optional[BaseException] = None
        for attempt in range(3):
            try:
                response = self.session.post(QUERY_URL, json=payload, timeout=TIMEOUT_SECONDS)
                if response.status_code in {502, 503, 504} and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                response.raise_for_status()
                body = response.json()
                self.conversation_id = body.get("conversationId") or self.conversation_id
                return body
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt == 2:
                    break
                time.sleep(2 * (attempt + 1))

        raise AssertionError(f"Live query failed after retries for '{text}': {last_error}")


def _countries(response: Dict[str, Any]) -> set[str]:
    countries = set()
    for series in response.get("data") or []:
        metadata = series.get("metadata") or {}
        country = metadata.get("country")
        if country:
            countries.add(str(country))
    return countries


def _series_ids(response: Dict[str, Any]) -> set[str]:
    series_ids = set()
    for series in response.get("data") or []:
        metadata = series.get("metadata") or {}
        series_id = metadata.get("seriesId")
        if series_id:
            series_ids.add(str(series_id))
    return series_ids


def test_live_direct_query_returns_expected_world_bank_series() -> None:
    client = LiveApiClient()

    response = client.query("imports share of gdp in china")

    assert response.get("clarificationNeeded") is False
    assert len(response.get("data") or []) >= 1
    assert "China" in _countries(response)
    assert "NE.IMP.GNFS.ZS" in _series_ids(response)


def test_live_clarification_reply_resolves_in_same_conversation() -> None:
    client = LiveApiClient()

    clarification = client.query("employment in Canada")
    assert clarification.get("clarificationNeeded") is True
    options = clarification.get("clarificationOptions") or []
    labels = [str(option.get("label") or "") for option in options]
    assert "number employed" in labels
    assert clarification.get("conversationId")

    resolved = client.query("1")
    assert resolved.get("conversationId") == clarification.get("conversationId")
    assert resolved.get("clarificationNeeded") is False
    assert len(resolved.get("data") or []) >= 1
    countries = _countries(resolved)
    assert "Canada" in countries or "CAN" in countries or "CA" in countries


def test_live_group_follow_up_can_override_pending_scope() -> None:
    client = LiveApiClient()

    clarification = client.query("imports share of gdp in G20")
    assert clarification.get("clarificationNeeded") is True
    options = clarification.get("clarificationOptions") or []
    labels = [str(option.get("label") or "") for option in options]
    assert "compare member countries" in labels

    narrowed = client.query("show only US")
    assert narrowed.get("clarificationNeeded") is False
    assert len(narrowed.get("data") or []) >= 1
    assert "United States" in _countries(narrowed)
    assert "NE.IMP.GNFS.ZS" in _series_ids(narrowed)
