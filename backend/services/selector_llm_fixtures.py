"""Recorded selector-LLM fixtures (offline test determinism).

The indicator selector's Step-2 adjudication (`IndicatorSelector._llm_pick`)
POSTs a fully-rendered prompt to a chat-completions endpoint and consumes the
raw response `content` string. That single network hop is the last source of
non-determinism in the offline suite once query embeddings are replayed
(`embedding_retrieval` fixtures). This module records real responses and
replays them verbatim so selector tests run with no network at all.

Contract (mirrors `embedding_retrieval`'s query-embedding fixtures):

  env `OPENECON_SELECTOR_FIXTURES`:
    "replay" -> look the recorded response up by (model, prompt). A miss raises
                KeyError naming the query and the exact re-record command —
                never a silent default, so an un-recorded prompt is a
                diagnosable failure, not a masked one.
    "record" -> call happens live; the caller feeds the real response here to
                be appended/overwritten.
    unset    -> live behavior, no fixture I/O.

Key derivation: sha256(model + "|" + whitespace-normalized prompt).

  Why normalize whitespace in the KEY (only): the prompt is machine-rendered
  and otherwise deterministic — it interpolates the user query, provider, and
  the enriched candidate `options` block, none of which carry a wall-clock
  date, timestamp, or random ordering (the one data-derived field,
  "last data: <end_date>", comes from the static local indicator DB, not the
  clock). The only realistic churn is another maintainer reflowing the
  LLM_SELECTION_PROMPT template's indentation or blank lines. Collapsing runs
  of whitespace means such a purely cosmetic edit keeps the key stable and the
  recorded decision still replays — while any change to the actual prose or the
  candidate set changes the key, misses, and forces an honest re-record. Case
  and token order are preserved: those change the LLM's decision, so folding
  them would let one recording stand in for a genuinely different prompt.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

FIXTURE_ENV_VAR = "OPENECON_SELECTOR_FIXTURES"
FIXTURE_FILE = (
    Path(__file__).parent.parent / "tests" / "fixtures" / "selector_llm_responses.json"
)

# The command a developer runs to (re)record. Embedding fixtures are pinned to
# replay during recording so the candidate set — and therefore the prompt the
# key is derived from — is itself deterministic across record and replay.
RERECORD_CMD = (
    "OPENECON_SELECTOR_FIXTURES=record OPENECON_EMBED_FIXTURES=replay "
    "python -m pytest backend/tests/test_query_service.py"
)


def mode() -> Optional[str]:
    """Current fixture mode: "record", "replay", or None (live). Read fresh
    each call so per-test env overrides (conftest) take effect."""
    return os.environ.get(FIXTURE_ENV_VAR)


def _normalize_prompt(prompt: str) -> str:
    """Collapse all runs of whitespace to single spaces for the fixture KEY.

    Applied only to the lookup key (and to the stored `prompt` echo so the file
    is self-consistent with what is hashed), never to the replayed response.
    """
    return " ".join(str(prompt).split())


def fixture_key(model: str, prompt: str) -> str:
    """sha256(model + '|' + whitespace-normalized prompt) — the lookup key."""
    payload = f"{model}|{_normalize_prompt(prompt)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_fixtures() -> Dict[str, Any]:
    if not FIXTURE_FILE.exists():
        return {}
    with open(FIXTURE_FILE) as f:
        return json.load(f)


def get_recorded_response(fixtures: Dict[str, Any], model: str, prompt: str) -> Optional[str]:
    """Return the recorded raw response content for (model, prompt), or None on
    a miss. None means 'not recorded for THIS model' — the caller iterates the
    same attempt chain the live path would, so a different endpoint's recording
    can still satisfy the call before it declares a hard cache miss."""
    entry = fixtures.get(fixture_key(model, prompt))
    if entry is None:
        return None
    return entry.get("response")


def record_response(model: str, prompt: str, response: str) -> None:
    """Append or overwrite one recorded response.

    Keyed on (model, normalized prompt), so re-recording an unchanged prompt
    REPLACES the stale entry in place. A prompt whose prose or candidate set
    changed hashes to a NEW key and leaves the old entry orphaned; to prune
    orphans, delete FIXTURE_FILE and re-record the full suite:

        rm backend/tests/fixtures/selector_llm_responses.json
        <RERECORD_CMD>

    The whole file is rewritten sorted + indented so recording order never
    churns the diff (mirrors the embedding fixture writer).
    """
    fixtures = load_fixtures()
    fixtures[fixture_key(model, prompt)] = {
        "model": model,
        "prompt": _normalize_prompt(prompt),
        "response": response,
    }
    FIXTURE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(FIXTURE_FILE, "w") as f:
        json.dump(fixtures, f, indent=2, sort_keys=True)
        f.write("\n")


def missing_fixture_error(query: str, provider: str, models: list[str]) -> KeyError:
    """Build the loud cache-miss error the replay path raises. Names the query,
    the provider, and every model whose key was tried, plus the re-record cmd."""
    tried = ", ".join(sorted({m for m in models if m})) or "(none)"
    return KeyError(
        f"No recorded selector-LLM fixture for query {query!r} "
        f"(provider={provider}, models tried=[{tried}]). Re-record with: {RERECORD_CMD}"
    )
