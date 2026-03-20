"""Shared pytest fixtures and environment setup for root integration tests."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def test_environment():
    """Ensure root tests run with stable test-mode environment variables."""
    old_env = os.environ.copy()
    os.environ["ENVIRONMENT"] = "test"
    os.environ["DISABLE_MCP"] = "1"
    os.environ["DISABLE_BACKGROUND_JOBS"] = "1"
    os.environ.setdefault("JWT_SECRET", "test-jwt-secret-for-testing")
    yield
    os.environ.clear()
    os.environ.update(old_env)
