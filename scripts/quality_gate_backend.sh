#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python}"
if [[ -x "$ROOT_DIR/backend/.venv/bin/python" ]]; then
  PYTHON_BIN="$ROOT_DIR/backend/.venv/bin/python"
fi

cd "$ROOT_DIR/backend"

"$PYTHON_BIN" -m pytest -q \
  tests/test_query_service.py \
  tests/test_query_pipeline.py \
  routing/tests/test_unified_router.py \
  routing/tests/test_semantic_provider_router.py \
  tests/test_indicator_resolver.py \
  tests/test_data_agent.py \
  tests/test_cache_service.py \
  tests/test_faiss_vector_search.py \
  tests/test_conversation_state.py \
  tests/test_simplified_prompt.py
