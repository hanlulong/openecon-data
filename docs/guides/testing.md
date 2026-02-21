# Testing Checklist

Use this checklist to verify that econ-data-mcp is healthy after installation or before deployment.

## 1. Backend quick checks

```bash
source backend/.venv/bin/activate              # Windows: .venv\Scripts\activate
uvicorn backend.main:app --reload --port 3001
```

### Health endpoint

```bash
curl http://localhost:3001/api/health | jq
```

Expected:

- `"status": "ok"`
- `"services.openrouter": true`
- Optional flags (`fred`, `comtrade`) reflect configured keys.

### Query endpoint

```bash
curl -s -X POST http://localhost:3001/api/query \
  -H 'Content-Type: application/json' \
  -d '{"query":"Compare GDP growth for US and Canada since 2015"}' | jq '.intent.apiProvider,.data[0].metadata'
```

Success criteria:

- `apiProvider` is set (e.g., `"WorldBank"`).
- The metadata block contains `source`, `frequency`, `apiUrl`.

### Cache stats

```bash
curl http://localhost:3001/api/cache/stats | jq
```

After a few queries, `hits` should start to increase.

### Backend unit tests

```bash
cd backend
source .venv/bin/activate
python -m unittest discover -s backend/tests
```

Tests cover provider adapters, cache behaviour, and public REST endpoints (via `TestClient` with mocks).

## 2. Frontend checks

```bash
npm run dev
```

- Visit `http://localhost:5173`.
- Send a query from the CTA (“Launch demo” → `/chat`).
- Confirm the chart renders and the “API data sources” panel lists copyable URLs.

### Build verification

```bash
npm run build
```

Vite may warn about bundle size; this is expected until we add code-splitting.

## 3. Manual regression prompts

Use the following prompts to exercise each data provider:

| Prompt | Expected Provider |
|--------|-------------------|
| “Show me US GDP for the last 5 years” | FRED |
| “Compare unemployment between US, UK, and Germany since 2010” | World Bank |
| “Show me US oil imports from 2018 to 2022” | UN Comtrade |
| “What is the trade balance between Canada and US from 2015 to 2020?” | UN Comtrade (balance) |

For each result:

1. Confirm the conversation card stores the response in the chart history.
2. Use the CSV/JSON export buttons and verify the download content.
3. Toggle chart types (line ↔ bar ↔ scatter) where the data density allows.

## 4. Authentication sanity checks (optional)

```bash
TOKEN=$(curl -s -X POST http://localhost:3001/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"name":"Test User","email":"tester@example.com","password":"secret123"}' | jq -r '.token')

curl -H "Authorization: Bearer $TOKEN" http://localhost:3001/api/auth/me | jq
curl -H "Authorization: Bearer $TOKEN" http://localhost:3001/api/user/history | jq
```

You should see the registered user profile and an empty history array (fill by issuing queries while authenticated).

## 5. Troubleshooting tips

- **401 from /api/query**: ensure `OPENROUTER_API_KEY` is present and uvicorn was restarted after editing `.env`.
- **FRED data missing**: add `FRED_API_KEY` and restart the backend; the health endpoint will confirm the flag.
- **Clipboard errors in the browser**: browsers block clipboard access on non-secure contexts when not triggered by a user gesture; the copy buttons must be clicked directly.

