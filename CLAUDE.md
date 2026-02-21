# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

---

## IMPORTANT: Re-read After Context Compaction

**After ANY context compaction or conversation continuation, you MUST re-read this file and TESTING_PROMPT.md before making changes.** Core principles like "NEVER hardcode mappings" can be lost during compaction. Always verify you understand the infrastructure-first approach before implementing fixes.

---

## Critical Rules

### 1. Always Use Restart Script

```bash
python3 scripts/restart_dev.py           # Both backend + frontend
python3 scripts/restart_dev.py --backend  # Backend only
python3 scripts/restart_dev.py --frontend # Frontend only
python3 scripts/restart_dev.py --status   # Check running services
```

**NEVER** manually run `uvicorn`, `nohup`, or start services directly. The script handles cleanup, process management, and health checks. Manual process management leads to port conflicts and zombie processes.

### 2. OECD Provider - Low Priority

- **Do not route to OECD** unless user explicitly requests it
- **Exclude from automated testing** - 60 req/hour rate limit causes cascading failures
- **Do not prioritize OECD bug fixes** - focus on FRED, World Bank, IMF, Eurostat
- OECD queries may fail/timeout - this is expected; fallback to Eurostat/IMF/World Bank

### 3. Rate Limiting

Rate limiting is **bypassed** for localhost and development mode. Only applies in production for remote IPs.

Production limits (per IP, per minute):
- `/api/auth/register`: 5/min | `/api/auth/login`: 10/min
- `/api/query/*`: 30/min | `/api/query/pro/*`: 10/min
- Default: 200/min | Health/static/MCP: exempt

### 4. Frontend Verification

- **Always use chrome-devtools MCP** when making frontend changes
- **Verify production** at https://openecon.ai/chat after deploying fixes

### 5. Development Mode Auth

Development works without Supabase credentials. Backend uses `MockAuthService`:
- **Test user**: `test@example.com` / `testpass123` (only available when `ALLOW_TEST_USER=true`)
- **Production**: Fails if Supabase not configured (security: fail closed)
- To enable test user in development: `export ALLOW_TEST_USER=true`

### 6. Testing Philosophy - INFRASTRUCTURE FIRST

> **🚨 CRITICAL: Every fix MUST be an infrastructure improvement, not a query-specific patch.**

For detailed testing guidance, see **[TESTING_PROMPT.md](TESTING_PROMPT.md)**. Key principles:

**The Infrastructure Rule:**
- ❌ WRONG: "Query X failed → Add mapping for X → Query X passes"
- ✅ RIGHT: "Query X failed → Fix the mechanism that should have handled this → ALL similar queries pass"

**Before ANY fix, ask:**
1. What CATEGORY of query is this?
2. What MECHANISM should have handled it?
3. Why did that mechanism fail?
4. How many OTHER queries would fail the same way?
5. What architectural change fixes ALL of them?

**Testing Standards:**
- **TEST 100 QUERIES MINIMUM** - 25 queries is NOT enough; need 100+ across all providers
- Tests improve the framework, not just pass rates
- Always implement general solutions, never hardcoded fixes
- Validate returned data values against authoritative sources
- A passing test must return CORRECT data, not just ANY data
- Every fix must help at least 5 similar queries (the "5-Query Test")

### 7. Indicator Discovery - NEVER Hardcode Mappings

> **ABSOLUTE RULE: When an indicator query fails, fix the DISCOVERY system, not add hardcoded mappings.**

econ-data-mcp has a comprehensive indicator database (`backend/data/indicators.db`) with **330,000+ indicators** using FTS5 full-text search.

**When a query fails because an indicator isn't found:**

```bash
# WRONG: Adding to SERIES_MAPPINGS in providers/*.py
"YIELD_CURVE_SPREAD": "T10Y2Y"  # ❌ NEVER DO THIS

# RIGHT: Ensure indicator database is complete
python3 scripts/fetch_all_indicators.py --provider FRED --update
```

**The indicator resolution stack:**
1. Static SERIES_MAPPINGS (minimal, common aliases only - DO NOT ADD)
2. Indicator Database FTS5 Search (330K+ indicators - ENSURE COMPLETE)
3. Provider API Search fallback (FRED series/search, etc.)

**If a query fails:**
1. Check if indicator exists in database: `lookup.search('yield curve', provider='FRED')`
2. If missing, run: `python3 scripts/fetch_all_indicators.py --provider FRED`
3. If search doesn't find it, improve the FTS query or add keywords to metadata
4. **NEVER add entries to static mapping dictionaries**

See [TESTING_PROMPT.md](TESTING_PROMPT.md) section "Indicator Discovery is the ONLY Solution" for details.

---

## Project Overview

econ-data-mcp is an AI-powered economic data aggregation service with a natural language interface. Users query economic data using plain English, and the system uses an LLM to parse intent, fetch data from multiple APIs, and return visualized results.

**Architecture:** Python FastAPI backend + React/TypeScript frontend (Vite)

**Core Features:**
- Natural language queries via OpenRouter/Ollama/LM-Studio
- 10+ data sources: FRED, World Bank, UN Comtrade, Statistics Canada, IMF, BIS, Eurostat, OECD, ExchangeRate-API, CoinGecko
- Pro Mode: AI-generated Python code execution for advanced analysis
- Streaming queries with real-time progress (SSE)
- RAG-based metadata search for indicator discovery
- Supabase authentication and persistent query history

---

## Quick Reference

### Development

```bash
# Start services (ALWAYS use this)
python3 scripts/restart_dev.py

# Check status
python3 scripts/restart_dev.py --status

# Monitor logs
tail -f /tmp/backend-dev.log
tail -f /tmp/frontend-dev.log

# Health check
curl http://localhost:3001/api/health
```

Frontend: http://localhost:5173 | Backend: http://localhost:3001

### Testing

```bash
source backend/.venv/bin/activate
pytest backend/tests/                    # Backend tests
npm run test --workspace=packages/frontend  # Frontend tests (placeholder)

# Manual API test
curl -X POST http://localhost:3001/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "Show me US GDP for the last 3 years"}'
```

### Building & Code Quality

```bash
npm run build:frontend     # Build frontend to packages/frontend/dist
npm run format             # Format with Prettier
npm run lint --workspace=packages/frontend  # Lint frontend
```

### Production Deployment

```bash
# 1. Build frontend (required for frontend changes)
npm run build:frontend

# 2. Backend auto-reloads on file changes (no action needed)

# 3. Verify
curl https://openecon.ai/api/health
```

Apache2 serves frontend from `packages/frontend/dist` and proxies `/api/*` to backend on port 3001.

---

## Architecture

### Request Flow

1. **User Input** → Natural language query via `ChatPage.tsx`
2. **API Request** → Frontend sends to `POST /api/query`
3. **LLM Parsing** → OpenRouter parses intent into `ParsedIntent`
4. **Data Fetching** → `QueryService` routes to appropriate provider
5. **Normalization** → Data transformed to `NormalizedData` format
6. **Caching** → Results cached in-memory with TTL
7. **Response** → Frontend displays charts via `MessageChart.tsx`

### Backend Structure (`backend/`)

| Component | Description |
|-----------|-------------|
| `main.py` | FastAPI app, routes, CORS, lifecycle |
| `config.py` | Pydantic Settings from `.env` |
| `models.py` | `ParsedIntent`, `NormalizedData`, request/response models |
| `services/query.py` | Main orchestration layer |
| `services/openrouter.py` | LLM API calls |
| `services/cache.py` | In-memory TTL cache |
| `services/code_executor.py` | Sandboxed Pro Mode execution |
| `providers/*.py` | FRED, World Bank, IMF, etc. |

### Frontend Structure (`packages/frontend/src/`)

| Component | Description |
|-----------|-------------|
| `components/ChatPage.tsx` | Main chat interface |
| `components/MessageChart.tsx` | Recharts visualization |
| `components/LandingPage.tsx` | Marketing page |
| `services/api.ts` | Axios API client |
| `contexts/AuthContext.tsx` | Auth state management |

---

## Environment Configuration

### Required

```bash
OPENROUTER_API_KEY=sk-or-...     # Get from https://openrouter.ai/keys
JWT_SECRET=<openssl rand -hex 32> # Required, no default
```

### Recommended

```bash
# Data provider API keys
FRED_API_KEY=...
COMTRADE_API_KEY=...
COINGECKO_API_KEY=...

# Supabase (for auth + query history)
SUPABASE_URL=...
SUPABASE_ANON_KEY=...
SUPABASE_SERVICE_ROLE_KEY=...

# LLM configuration
LLM_PROVIDER=openrouter          # openrouter, ollama, or lm-studio
LLM_MODEL=openai/gpt-4o-mini

# Pro Mode
GROK_API_KEY=...
PROMODE_PUBLIC_DIR=/path/to/public_media/promode
PROMODE_SESSION_DIR=/tmp/promode_sessions

# Production
NODE_ENV=production
ALLOWED_ORIGINS=https://openecon.ai,https://www.openecon.ai
```

---

## API Endpoints

### Public
- `GET /api/health` - Health check
- `POST /api/auth/register` - User registration
- `POST /api/auth/login` - User login
- `POST /api/query` - Process query
- `POST /api/query/stream` - Streaming query (SSE)
- `POST /api/query/pro` - Pro Mode query
- `POST /api/query/pro/stream` - Streaming Pro Mode
- `POST /api/export` - Export as CSV/JSON

### Protected (JWT required)
- `GET /api/auth/me` - Current user info
- `GET /api/user/history` - Query history
- `DELETE /api/user/history` - Clear history

### Debug
- `GET /api/cache/stats` - Cache statistics
- `POST /api/cache/clear` - Clear cache

### MCP
- `/mcp` - Model Context Protocol endpoint (SSE transport)

---

## Key Features

### Pro Mode

AI-generated Python code execution for complex queries. Components:
- `QueryComplexityAnalyzer` - Detects Pro Mode queries
- `GrokService` - Generates Python via X.AI Grok
- `CodeExecutor` - Sandboxed execution with security restrictions

Security: Import blacklist, 30s timeout, 100K char limit, regex validation, JSON session storage.

### Streaming Queries

Server-Sent Events for real-time progress:
- Event types: `step`, `data`, `error`, `done`
- `ProcessingTracker` in `backend/utils/processing_steps.py`

### Metadata Search (RAG)

Semantic search for indicator discovery:
- `MetadataSearchService` - Search over provider metadata
- `VectorSearch` - FAISS-based similarity search
- Natural language → indicator code mapping

### MCP Server

Exposes `query_data` operation to MCP clients (Claude Desktop, VS Code). Disable with `DISABLE_MCP=true`.

---

## Data Providers

| Provider | Coverage | API Key | Notes |
|----------|----------|---------|-------|
| **FRED** | US economic (90K+ series) | Recommended | Primary for US data |
| **World Bank** | Global development (16K+ indicators) | No | |
| **UN Comtrade** | International trade | Recommended | |
| **Statistics Canada** | Canadian data (40K+ tables) | No | |
| **IMF** | International financial | No | |
| **BIS** | Central bank data | No | |
| **Eurostat** | EU statistics | No | |
| **OECD** | OECD members | No | **LOW PRIORITY** - 60 req/hr limit |
| **ExchangeRate-API** | Currency rates | Optional | |
| **CoinGecko** | Crypto prices | Optional | |

---

## Supabase Integration

Provides authentication and persistent query history.

**Tables:**
- `users` - Managed by Supabase Auth
- `queries` - Query history (query, user_id, session_id, intent, response_data, etc.)

**Fallback:** If credentials missing, uses in-memory `user_store`.

---

## Documentation

- **[TESTING_PROMPT.md](TESTING_PROMPT.md)** - Comprehensive testing guide
- **[docs/README.md](docs/README.md)** - Full documentation index
- **[docs/guides/getting-started.md](docs/guides/getting-started.md)** - First-time setup
- **[docs/guides/cross-platform-setup.md](docs/guides/cross-platform-setup.md)** - Windows/macOS/Linux setup
- **[.github/SECURITY.md](.github/SECURITY.md)** - Security policy
- **[docs/reference/trade-data.md](docs/reference/trade-data.md)** - UN Comtrade & HS codes

### Provider-Specific

**Statistics Canada debugging:**
- [SDMX User Guide](https://www.statcan.gc.ca/en/developers/sdmx/user-guide)
- [WDS User Guide](https://www.statcan.gc.ca/en/developers/wds/user-guide)
- Metadata cache: `backend/data/statscan_metadata_cache.json`
- Update cache: `python3 scripts/fetch_statscan_metadata.py`
