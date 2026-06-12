# API Reference

Base URL: `http://localhost:3001` (dev) | `https://data.openecon.ai` (prod)

All endpoints are prefixed with `/api` unless noted otherwise.

---

## Authentication

JWT bearer tokens. Include in header: `Authorization: Bearer <token>`.

Endpoints marked **Auth** require a valid token. Endpoints marked **Optional Auth** use the token if present (for history tracking) but work without it.

### POST /api/auth/register

Create a new account.

**Request:**
```json
{
  "email": "user@example.com",
  "password": "securepassword",
  "name": "Display Name",
  "institution": "Example University",
  "sessionId": "sess-456"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `email` | string | Yes | Email address (max 254 chars) |
| `password` | string | Yes | Server-side minimum 8 chars; the frontend enforces a stronger policy (12+ chars, mixed case, digit) |
| `name` | string | Yes | Display name (max 200 chars) |
| `institution` | string | No | Institution or company (max 200 chars) |
| `sessionId` | string | No | Anonymous session ID. If provided, the query history built before registration is migrated to the new account. |

**Response** `201`:
```json
{
  "success": true,
  "token": null,
  "user": {
    "id": "uuid",
    "email": "user@example.com",
    "name": "Display Name",
    "createdAt": "2026-01-01T00:00:00Z",
    "lastLogin": null
  },
  "error": null,
  "emailVerificationRequired": true
}
```

**Email verification:** when Supabase "Confirm email" is enabled (the production default), registration succeeds but returns `emailVerificationRequired: true` and a `null` token. The user must click the confirmation link emailed to them (Supabase redirects to `EMAIL_CONFIRM_REDIRECT_URL`) before they can log in. When email confirmation is off (or with the development mock-auth service), `token` is returned directly and `emailVerificationRequired` is `false`/absent.

**Errors:** `400` if email already registered or validation fails.

### POST /api/auth/login

Authenticate and receive a JWT.

**Request:**
```json
{
  "email": "user@example.com",
  "password": "securepassword"
}
```

**Response** `200`: Same shape as register response (with `token` set).

**Errors:** `401` if credentials invalid. If the account exists but the email has not been confirmed, the `401` body has `emailVerificationRequired: true` and an error message asking the user to confirm their email first.

### POST /api/auth/forgot-password

Email a password-reset link to the address. Always returns `200` with `{"success": true}` — even for unknown addresses — so the endpoint never discloses which emails have accounts.

**Request:**
```json
{
  "email": "user@example.com"
}
```

### POST /api/auth/reset-password

Set a new password using the recovery token from the emailed reset link.

**Request:**
```json
{
  "accessToken": "recovery-token-from-email-link",
  "password": "newsecurepassword"
}
```

**Response** `200`: `{"success": true}`.

**Errors:** `400` if the token is invalid/expired or password reset is unavailable (e.g. mock auth in development).

### Google sign-in & email verification

Google OAuth and email-confirmation links are handled in the browser directly against Supabase (`supabase.auth.signInWithOAuth({ provider: 'google' })`), so there is no dedicated backend route for them. After OAuth or email confirmation, Supabase redirects to the URLs configured by `EMAIL_CONFIRM_REDIRECT_URL` / `PASSWORD_RESET_REDIRECT_URL`. These flows require Supabase to be configured; the development mock-auth service supports email/password only.

### GET /api/auth/me

**Auth required.**

Returns the authenticated user profile.

**Response** `200`:
```json
{
  "id": "uuid",
  "email": "user@example.com",
  "name": "Display Name",
  "createdAt": "2026-01-01T00:00:00Z",
  "lastLogin": "2026-04-01T12:00:00Z"
}
```

---

## Query Endpoints

### POST /api/query

Process a natural language economic data query. This is the primary endpoint.

**Optional Auth** -- authenticated users get query history tracking.

**Request:**
```json
{
  "query": "US GDP growth 2020-2024",
  "conversationId": "abc-123",
  "sessionId": "sess-456"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `query` | string | Yes | Natural language query (1-5000 chars) |
| `conversationId` | string | No | ID from a previous response to continue a conversation (max 100 chars) |
| `sessionId` | string | No | Anonymous session tracking ID (max 100 chars) |

**Response** `200` -- `QueryResponse`:
```json
{
  "conversationId": "abc-123",
  "intent": { ... },
  "data": [ ... ],
  "clarificationNeeded": false,
  "clarificationQuestions": null,
  "clarificationOptions": null,
  "error": null,
  "message": null,
  "codeExecution": null,
  "isProMode": null,
  "processingSteps": [ ... ],
  "alternativeSeries": [ ... ],
  "processingTimeMs": 1234.5,
  "anonymousQueriesUsed": 3,
  "anonymousQueryLimit": 20,
  "registrationRequired": false
}
```

See [Response Schemas](#response-schemas) below for full field definitions.

**Anonymous free-query limit:** unauthenticated callers (tracked by `sessionId`, falling back to `conversationId`) get a limited number of free queries — `ANON_QUERY_LIMIT`, default **20**. Each response reports `anonymousQueriesUsed` and `anonymousQueryLimit` so a UI can show "N free queries left". Once over the limit, the query is **not** processed and the response (still HTTP `200`) has `registrationRequired: true` with a message asking the user to create a free account. Authenticated users are exempt (these three fields are `null`/`false`). The gate fails open if tracking is unavailable.

**Errors:** `400` if query is empty. `500` only for internal processing errors (data-not-available is returned as `200` with `error` field set).

### POST /api/query/stream

Same as `/api/query` but returns Server-Sent Events (SSE) for real-time progress.

**Optional Auth.**

**Request:** Same as `/api/query`.

**Response:** `text/event-stream` with events:

| Event type | Payload | Description |
|------------|---------|-------------|
| `step` | `{ step, description, status, duration_ms, metadata }` | Processing progress update |
| `data` | Full `QueryResponse` object | Final query result |
| `error` | `{ error, message }` | Processing error |
| `done` | `{}` | Stream complete |

Example SSE stream:
```
event: step
data: {"step":"parsing_query","description":"Understanding your question...","status":"completed","duration_ms":120.5,"metadata":null}

event: step
data: {"step":"fetching_data","description":"Fetching from FRED...","status":"completed","duration_ms":890.2,"metadata":null}

event: data
data: {"conversationId":"abc-123","intent":{...},"data":[...],...}

event: done
data: {}
```

The anonymous free-query limit applies here too: once an unauthenticated session is over the limit, the stream emits a single `data` event with `registrationRequired: true` (and no query processing), followed by `done`.

### POST /api/query/pro

**Pro Mode.** AI-generated Python code execution for advanced analysis. Requires Pro Mode to be enabled on the server (`PROMODE_ENABLED=true`); otherwise the endpoint returns `403`.

**Registered users only.** Pro Mode executes generated code, so anonymous callers are not served: without a valid token the response is HTTP `200` with `registrationRequired: true`, `isProMode: true`, and a message asking the user to create a free account — the query is not executed.

**Request:** Same as `/api/query`.

**Response** `200`: Same `QueryResponse` shape, with `isProMode: true` and `codeExecution` populated:
```json
{
  "conversationId": "abc-123",
  "clarificationNeeded": false,
  "message": "Code executed successfully. Generated 1 file(s).",
  "codeExecution": {
    "code": "import pandas as pd\n...",
    "output": "GDP growth comparison:\n...",
    "error": null,
    "executionTime": 2.5,
    "files": [
      {
        "url": "/static/promode/chart_abc123.png",
        "name": "chart_abc123.png",
        "type": "image"
      }
    ]
  },
  "isProMode": true
}
```

### POST /api/query/pro/stream

Streaming version of Pro Mode. Same SSE event format as `/api/query/stream`, and the same rules as `/api/query/pro`: requires `PROMODE_ENABLED=true` (`403` otherwise), and anonymous callers receive a single `data` event with `registrationRequired: true` followed by `done`.

---

## Conversation Flow

Multi-round conversations are tracked via `conversationId`.

1. **First query:** Omit `conversationId`. The response includes a generated `conversationId`.
2. **Follow-up queries:** Send the same `conversationId` from the previous response.
3. The backend stores conversation context (messages, previous intents) in memory with Redis write-through. Conversations expire after **24 hours** of inactivity. Max **200 messages** per conversation.
4. The LLM receives conversation history and can detect follow-ups, populating `intent.isFollowUp`, `intent.followUpType`, and `intent.resolvedQuery`.

**Follow-up detection fields** (in `ParsedIntent`):

| Field | Type | Description |
|-------|------|-------------|
| `isFollowUp` | bool | Whether this query references a previous query in the conversation |
| `followUpType` | string or null | Category: `"country_change"`, `"indicator_switch"`, `"time_change"`, `"provider_change"`, `"pronoun_reuse"`, `"clarification_answer"` |
| `resolvedQuery` | string or null | The fully explicit rewritten query (e.g., "now for Japan" becomes "Japan GDP growth 2020-2024") |

**Example flow:**
```
POST /api/query  {"query": "US GDP 2020-2024"}
  -> {"conversationId": "abc-123", "data": [...], ...}

POST /api/query  {"query": "now for Japan", "conversationId": "abc-123"}
  -> intent.isFollowUp: true
  -> intent.followUpType: "country_change"
  -> intent.resolvedQuery: "Japan GDP 2020-2024"
```

---

## Export

### POST /api/export

Export query result data to a file.

**Request:**
```json
{
  "data": [ /* array of NormalizedData objects from a query response */ ],
  "format": "csv",
  "filename": "us_gdp_export"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `data` | NormalizedData[] | Yes | Data array from a previous query response |
| `format` | string | Yes | `"csv"`, `"json"`, or `"dta"` (Stata) |
| `filename` | string | No | Custom filename (auto-generated if omitted) |

**Response:** File download with appropriate `Content-Type` and `Content-Disposition` headers.

---

## Feedback

### POST /api/feedback

Submit user feedback.

**Request:**
```json
{
  "type": "bug",
  "message": "The chart doesn't render for...",
  "email": "user@example.com",
  "sessionInfo": { "url": "...", "userAgent": "...", "timestamp": "...", "screenSize": "...", "language": "...", "timezone": "...", "referrer": "..." },
  "conversation": { "messages": "...", "messageCount": 5, "conversationId": "abc-123" },
  "userId": "uuid",
  "userName": "Display Name"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | string | Yes | `"bug"`, `"feature"`, or `"other"` |
| `message` | string | No | Feedback text (max 10,000 chars) |
| `email` | string | No | Contact email |
| All other fields | object | No | Optional context |

**Response** `200`:
```json
{
  "success": true,
  "message": "Thank you for your feedback!",
  "feedbackId": "fb-uuid"
}
```

---

## User History

### GET /api/user/history

**Auth required.**

Retrieve query history for the authenticated user.

**Query params:**

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | 50 | Max results (1-500) |

**Response** `200`:
```json
{
  "history": [
    {
      "id": "uuid",
      "query": "US GDP 2020-2024",
      "conversationId": "abc-123",
      "intent": { ... },
      "data": [ ... ],
      "timestamp": "2026-04-01T12:00:00Z"
    }
  ],
  "total": 1
}
```

### DELETE /api/user/history

**Auth required.**

Delete all query history for the authenticated user.

**Response** `200`:
```json
{
  "success": true,
  "message": "Deleted 15 queries",
  "deleted": 15
}
```

### GET /api/session/history

Retrieve query history for an anonymous session.

**Query params:**

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `session_id` | string | Yes | Session ID (max 100 chars) |
| `limit` | int | No | Max results (1-500, default 50) |

**Response** `200`: Same shape as `/api/user/history`.

---

## Health and Diagnostics

### GET /api/health

No auth. Returns system health status with deep dependency probes (Redis ping, Supabase client, indicator database), each timeboxed to 200ms so the endpoint stays fast.

**Status values:** `"ok"` (all critical services up), `"degraded"` (Redis down, or Supabase down in production — both have fallbacks), `"error"` (indicator database unreachable — queries will not work).

**Response** `200`:
```json
{
  "status": "ok",
  "timestamp": "2026-04-01T12:00:00Z",
  "environment": "production",
  "services": {
    "openrouter": true,
    "fred": true,
    "alphaVantage": false,
    "bls": false,
    "comtrade": true,
    "redis": true,
    "supabase": true,
    "indicators_db": true
  },
  "cache": {
    "keys": 42,
    "hits": 1500,
    "misses": 200,
    "ksize": 1024,
    "vsize": 204800
  },
  "users": {
    "totalUsers": 10,
    "totalQueries": 250
  },
  "promodeEnabled": true
}
```

### GET /api/cache/stats

**Auth required.** Returns cache statistics (same shape as `cache` in health response).

### POST /api/cache/clear

**Auth required.** Clears both in-memory and Redis caches.

**Response** `200`:
```json
{
  "message": "Cache cleared",
  "redisDeleted": 42
}
```

### GET /api/performance/metrics

No auth. Returns detailed performance metrics.

**Response** `200`:
```json
{
  "timestamp": "2026-04-01T12:00:00Z",
  "http_pool": { ... },
  "circuit_breakers": { ... },
  "cache": { ... },
  "metadata_loader": { ... }
}
```

### GET /api/performance/status

No auth. Returns system status summary.

**Response** `200`:
```json
{
  "status": "healthy",
  "timestamp": "2026-04-01T12:00:00Z",
  "cache": {
    "hit_rate": 0.88,
    "entries": 42
  },
  "optimization_enabled": {
    "http_pool": true,
    "circuit_breaker": true,
    "async_metadata_loading": true
  }
}
```

---

## MCP (Model Context Protocol)

**Endpoint:** `/mcp` (SSE transport)

Exposes the `query_data` tool to MCP-compatible clients (Claude Desktop, Claude Code, Codex, etc.). Accepts the same natural language queries as `/api/query`.

Disable with env var `DISABLE_MCP=true`.

Hosted: `https://data.openecon.ai/mcp` | Local: `http://localhost:3001/mcp`

---

## Response Schemas

### QueryResponse

Top-level response for all query endpoints.

| Field | Type | Description |
|-------|------|-------------|
| `conversationId` | string | Conversation identifier. Reuse in subsequent requests for multi-round conversations. |
| `intent` | ParsedIntent or null | LLM-parsed query intent |
| `data` | NormalizedData[] or null | Array of data series returned |
| `clarificationNeeded` | bool | If true, the system needs more information before fetching data |
| `clarificationQuestions` | string[] or null | Human-readable questions for the user |
| `clarificationOptions` | ClarificationOption[] or null | Structured options the user can select from |
| `error` | string or null | Error code if something went wrong (e.g., `"processing_error"`, `"data_not_available"`) |
| `message` | string or null | Human-readable message (used for informational responses or Pro Mode output) |
| `codeExecution` | CodeExecutionResult or null | Pro Mode code and output (only when `isProMode` is true) |
| `isProMode` | bool or null | Whether this response used Pro Mode |
| `processingSteps` | ProcessingStep[] or null | Steps taken to process the query (timing, status) |
| `alternativeSeries` | AlternativeSeries[] or null | Related indicators the user might want to explore |
| `processingTimeMs` | float or null | End-to-end processing time in milliseconds |
| `anonymousQueriesUsed` | int or null | Free queries this anonymous session has used (null for registered users) |
| `anonymousQueryLimit` | int or null | The anonymous free-query limit (`ANON_QUERY_LIMIT`, default 20) |
| `registrationRequired` | bool or null | True when the free-query limit was reached (or Pro Mode was requested anonymously) and the query was **not** processed — the UI should prompt registration |

### ParsedIntent

LLM-parsed interpretation of the user's query.

| Field | Type | Description |
|-------|------|-------------|
| `apiProvider` | string | Selected data provider (e.g., `"FRED"`, `"WorldBank"`, `"IMF"`, `"Eurostat"`) |
| `indicators` | string[] | Indicator codes to fetch |
| `parameters` | object | Provider-specific params (country codes, date ranges, etc.) |
| `clarificationNeeded` | bool | Whether the query is ambiguous |
| `clarificationQuestions` | string[] or null | Questions if clarification needed |
| `confidence` | float or null | LLM confidence score (0-1) |
| `recommendedChartType` | string or null | `"line"`, `"bar"`, `"scatter"`, or `"table"` |
| `queryType` | string | `"data_fetch"`, `"informational"`, `"analysis"`, or `"comparison"` |
| `originalQuery` | string or null | Raw query text |
| `isFollowUp` | bool | Whether this is a follow-up to a previous query in the conversation |
| `followUpType` | string or null | Follow-up category (see [Conversation Flow](#conversation-flow)) |
| `resolvedQuery` | string or null | Explicit rewritten query if this is a follow-up |
| `needsDecomposition` | bool | Whether query needs to be split (e.g., "all provinces") |
| `decompositionType` | string or null | `"provinces"`, `"states"`, `"regions"`, `"countries"` |
| `decompositionEntities` | string[] or null | Entities to iterate over |
| `useProMode` | bool | Whether auto-routing to Pro Mode is recommended |

### NormalizedData

A single data series with metadata and data points.

| Field | Type | Description |
|-------|------|-------------|
| `metadata` | Metadata | Series metadata |
| `data` | DataPoint[] | Time series data points |

### Metadata

| Field | Type | Description |
|-------|------|-------------|
| `source` | string | Provider name |
| `indicator` | string | Human-readable indicator name |
| `country` | string or null | Country name |
| `frequency` | string | Data frequency (e.g., `"Annual"`, `"Monthly"`, `"Quarterly"`) |
| `unit` | string | Unit of measurement |
| `lastUpdated` | string | Last update timestamp |
| `seriesId` | string or null | Provider-specific series identifier |
| `apiUrl` | string or null | Direct API URL for programmatic access to this exact data |
| `sourceUrl` | string or null | Human-readable URL for data verification on the provider's website |
| `seasonalAdjustment` | string or null | e.g., `"Seasonally adjusted"` |
| `dataType` | string or null | e.g., `"Level"`, `"Percent Change"`, `"Index"` |
| `priceType` | string or null | e.g., `"Chained (2017) dollars"`, `"Current prices"` |
| `description` | string or null | Full series description |
| `notes` | string[] or null | Additional footnotes |
| `scaleFactor` | string or null | e.g., `"millions"`, `"billions"` |
| `startDate` | string or null | First available data date |
| `endDate` | string or null | Last available data date |

### DataPoint

| Field | Type | Description |
|-------|------|-------------|
| `date` | string | Date string (format varies by frequency: `"2024"`, `"2024-01"`, `"2024-01-15"`) |
| `value` | float or null | Data value. NaN and infinity are normalized to `null`. |

### AlternativeSeries

| Field | Type | Description |
|-------|------|-------------|
| `code` | string | Indicator code |
| `name` | string | Human-readable name |
| `provider` | string | Data provider |
| `description` | string or null | Brief description |
| `apiUrl` | string or null | Direct API URL |

### ClarificationOption

Structured choice presented when the query is ambiguous.

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | Option identifier |
| `label` | string | Display label |
| `value` | string | Value to use if selected |
| `provider` | string or null | Associated provider |
| `code` | string or null | Associated indicator code |

### ProcessingStep

| Field | Type | Description |
|-------|------|-------------|
| `step` | string | Step identifier (e.g., `"parsing_query"`, `"searching_metadata"`, `"fetching_data"`) |
| `description` | string | Human-readable description |
| `status` | string | `"pending"`, `"in-progress"`, `"completed"`, or `"error"` |
| `duration_ms` | float or null | Step duration in milliseconds |
| `metadata` | object or null | Additional step-specific info |

### CodeExecutionResult

Pro Mode output.

| Field | Type | Description |
|-------|------|-------------|
| `code` | string | Generated Python source code |
| `output` | string | stdout from execution |
| `error` | string or null | Error message if execution failed |
| `executionTime` | float or null | Execution time in seconds |
| `files` | GeneratedFile[] or null | Generated files (charts, data exports) |

### GeneratedFile

| Field | Type | Description |
|-------|------|-------------|
| `url` | string | URL path to the file (e.g., `/static/promode/chart.png`) |
| `name` | string | Filename |
| `type` | string | `"image"`, `"data"`, `"html"`, or `"file"` |

---

## Rate Limiting

Rate limits apply **only in production** for remote IPs. Localhost and development mode are exempt.

| Endpoint pattern | Limit |
|-----------------|-------|
| `/api/auth/register` | 5/minute |
| `/api/auth/login` | 10/minute |
| `/api/query` | 30/minute |
| `/api/query/stream` | 30/minute |
| `/api/query/pro` | 10/minute |
| `/api/query/pro/stream` | 10/minute |
| All other endpoints | 200/minute |
| `/api/health`, `/static/*`, `/mcp` | Exempt |

When rate limited, the response is:
```
HTTP 429 Too Many Requests
Retry-After: 60
{"detail": "Rate limit exceeded. Limit: 30/minute"}
```

If the rate limiter itself fails, the server returns `503` (fail closed).

---

## Circuit Breaker

External provider API calls are protected by per-provider circuit breakers to prevent cascading failures.

**States:**

| State | Behavior |
|-------|----------|
| **Closed** | Normal operation. Requests pass through. Failures are counted within a 5-minute sliding window. |
| **Open** | After 5 failures in the window, the breaker opens. All requests to that provider fail immediately without making API calls. |
| **Half-open** | After a 60-second recovery timeout, the breaker allows limited test requests. 2 consecutive successes close the breaker; any failure reopens it. |

The recovery timeout uses exponential backoff (up to 8x the base timeout) across repeated open/close cycles.

**Defaults:** `failure_threshold=5`, `recovery_timeout=60s`, `success_threshold=2`, `window=300s`.

Check breaker status via `GET /api/performance/metrics` (the `circuit_breakers` field).

---

## Error Handling

Errors are returned as `200` with the `error` field set in `QueryResponse`, except for true server failures which return `500`.

Common error values:

| `error` value | Meaning |
|---------------|---------|
| `"data_not_available"` | No data found for the requested indicator/country/period |
| `"processing_error"` | Internal server error during query processing |
| `"pro_mode_error"` | Pro Mode code generation or execution failed |
| `null` | No error |

The `message` field contains a human-readable explanation when `error` is set.
