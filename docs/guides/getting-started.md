# Getting Started

This guide walks through the minimum steps to run econ-data-mcp locally with the FastAPI backend and React frontend.

## 1. Prerequisites

- Python 3.10+ (virtualenv recommended)
- Node.js 18+ and npm 9+
- An OpenRouter API key (required)
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
OPENROUTER_API_KEY=pk-...
FRED_API_KEY=optional
COMTRADE_API_KEY=optional
JWT_SECRET=generate_a_random_string
```

Restart uvicorn after editing secrets.

No manual database/index bootstrap is required for local setup:
- `backend/data/indicators.db` is created if missing
- `backend/data/faiss_index` is created/rebuilt on demand when vector search is enabled
- Supabase is optional in development (mock auth is used when Supabase is not configured)

## 4. Run the stack

In one terminal (with the virtualenv activated):

```bash
cd backend
uvicorn backend.main:app --reload --port 3001
```

In another terminal:

```bash
npm run dev
```

Vite proxies `/api/*` requests to `http://localhost:3001`, so no extra configuration is required.

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
- Hosted MCP endpoint: `https://data.openecon.io/mcp`
- Hosted user-facing app: `https://data.openecon.io/chat`
- See [`docs/guides/testing.md`](./testing.md) for a manual verification checklist.
- Review [`docs/reference/trade-data.md`](../reference/trade-data.md) for Comtrade/HS code hints.
- Browse [`docs/development/agents.md`](../development/agents.md) for AI agent integration notes.
