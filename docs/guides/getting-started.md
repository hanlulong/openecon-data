# Getting Started

This guide walks through the minimum steps to run openecon-data locally with the FastAPI backend and React frontend.

## 1. Prerequisites

- Python 3.10+ (virtualenv recommended)
- Node.js 18+ and npm 9+
- **Required for the backend to start:**
  - `OPENROUTER_API_KEY` — required when `LLM_PROVIDER=openrouter` (the default). Get one at https://openrouter.ai/keys. To skip it, point `LLM_PROVIDER` at a local model server (`vllm`, `ollama`, or `lm-studio`).
  - `JWT_SECRET` — required, no default. Generate with `openssl rand -hex 32`.
- Optional: an OpenAI API key if you want OpenAI embedding models
- Optional data keys: `FRED_API_KEY`, `COMTRADE_API_KEY`

## 2. Install dependencies

```bash
# Install Node packages (root + workspace)
npm install

# Create a Python virtual environment for the backend
cd backend
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 3. Configure environment

Create a `.env` file in the repository root:

```
LLM_PROVIDER=openrouter
LLM_MODEL=openai/gpt-4o-mini
OPENROUTER_API_KEY=sk-or-...                  # required (see prerequisites)
JWT_SECRET=generate_a_random_string           # required — openssl rand -hex 32
EMBEDDING_MODEL=sentence-transformers/all-MiniLM-L6-v2
# Optional OpenAI embedding setup:
# OPENAI_API_KEY=sk-...
# EMBEDDING_MODEL=text-embedding-3-small
# EMBEDDING_DIMENSIONS=1536
# Optional data-provider keys (leave commented out to run without them):
# FRED_API_KEY=your_fred_api_key
# COMTRADE_API_KEY=your_comtrade_api_key
```

Tip: instead of writing `.env` by hand, run `./scripts/setup.sh` (or `cp .env.example .env`) and edit the generated file.

Restart the backend after editing secrets (use `python3 scripts/restart_dev.py --backend`).

No manual database/index bootstrap is required for local setup:
- `backend/data/indicators.db` is created if missing
- `backend/data/faiss_index` is created/rebuilt on demand when vector search is enabled
- Supabase is optional in development (mock auth is used when Supabase is not configured). With mock auth, a dev test user is available by default: `test@example.com` / `testpass123` (disable with `ALLOW_TEST_USER=false`). Google sign-in, email verification, and password reset require Supabase.
- Redis is optional — the backend tries `redis://localhost:6379/0` and falls back to in-memory caching if Redis is not running

## 4. Run the stack

Use the restart script (recommended):

```bash
python3 scripts/restart_dev.py
```

This starts both the backend (port 3001) and frontend (port 5173). Vite proxies `/api/*` requests to `http://localhost:3001`, so no extra configuration is required.

## 5. Smoke test

From a browser, open `http://localhost:5173` and try one of the example prompts.

From the CLI:

```bash
curl http://localhost:3001/api/health | jq '.status'
curl -X POST http://localhost:3001/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Compare GDP growth for US and Canada since 2015"}' | jq '.data[0].metadata'
```

You should receive normalized data with provenance metadata (`source`, `unit`, `apiUrl`, etc.).

## 6. Next steps

- If you want to use this project as an MCP server in AI coding assistants, see [`docs/mcp/setup.md`](../mcp/setup.md).
- Hosted MCP endpoint: `https://data.openecon.ai/mcp`
- Hosted user-facing app: `https://data.openecon.ai/chat`
- See [`docs/guides/testing.md`](./testing.md) for a manual verification checklist.
- Review [`docs/reference/trade-data.md`](../reference/trade-data.md) for Comtrade/HS code hints.
- Browse [`docs/development/agents.md`](../development/agents.md) for AI agent integration notes.
