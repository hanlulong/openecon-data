[![MseeP.ai Security Assessment Badge](https://mseep.net/pr/hanlulong-openecon-data-badge.png)](https://mseep.ai/app/hanlulong-openecon-data)

<p align="center">
  <img src="packages/frontend/public/favicon.svg" width="80" height="80" alt="OpenEcon logo" />
</p>

<h1 align="center">OpenEcon Data</h1>

<p align="center">
  <strong>Give your AI agent accurate economic data.</strong><br/>
  330K indicators from FRED, World Bank, IMF, Eurostat, and 6 more sources — one MCP command away.
</p>

<p align="center">
  <a href="https://data.openecon.ai/chat"><img src="https://img.shields.io/badge/Try_it-Live_Demo-blue?style=flat-square" alt="Live Demo" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-AGPL--3.0-blue?style=flat-square" alt="AGPL-3.0 License" /></a>
  <a href="https://github.com/hanlulong/openecon-data/stargazers"><img src="https://img.shields.io/github/stars/hanlulong/openecon-data?style=flat-square" alt="Stars" /></a>
  <a href="https://github.com/hanlulong/openecon-data/issues"><img src="https://img.shields.io/github/issues/hanlulong/openecon-data?style=flat-square" alt="Issues" /></a>
  <img src="https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/MCP-Server-purple?style=flat-square" alt="MCP Server" />
  <img src="https://img.shields.io/github/last-commit/hanlulong/openecon-data?style=flat-square" alt="Last Commit" />
</p>

<p align="center">
  <a href="https://openecon.ai">Website</a> &middot;
  <a href="https://data.openecon.ai/chat">Live App</a> &middot;
  <a href="docs/README.md">Docs</a> &middot;
  <a href="docs/reference/api.md">API Reference</a> &middot;
  <a href="docs/development/DEVELOPER_CONTRIBUTOR_GUIDE.md">Contributing</a>
</p>

<p align="center">
  <a href="README.md">English</a> &middot;
  <a href="README.zh-CN.md">简体中文</a>
</p>

---

## Install (One Line, Then Just Talk)

```bash
curl -fsSL https://raw.githubusercontent.com/hanlulong/openecon-data/main/scripts/install.sh | bash
```

That's it. The script auto-detects Claude Code and Codex, configures everything. Then just ask:

```
You: "What's the US GDP growth rate?"         → your agent fetches real data from FRED
You: "Compare inflation across G7 countries"   → World Bank data for 7 countries
You: "Bitcoin price last 30 days"              → CoinGecko live data
```

No special syntax. No "use query_data". Just ask naturally — your agent handles the rest.

<details>
<summary><b>Manual install (if you prefer)</b></summary>

**Claude Code:**
```bash
claude mcp add --transport sse openecon-data https://data.openecon.ai/mcp --scope user
```

**Codex:**
```bash
codex mcp add openecon-data --url https://data.openecon.ai/mcp
```

**Any MCP agent:** Endpoint `https://data.openecon.ai/mcp` (SSE transport)

See [skills/README.md](skills/README.md) for slash commands and auto-trigger options.
</details>

---

<p align="center">
  <img src="docs/assets/demo.gif" width="800" alt="OpenEcon Data — type a query in plain English, get a chart with data from FRED, World Bank, and more" />
</p>

## Why Your Agent Needs This

AI agents hallucinate economic data. When you ask an LLM "What is US GDP?", you get a plausible-sounding number that may be outdated or wrong. OpenEcon solves this:

| | Without OpenEcon | With OpenEcon |
|---|---|---|
| **Data source** | LLM's training data (stale) | Official APIs (FRED, World Bank, IMF) |
| **Accuracy** | Approximate, often wrong | Verified, with source attribution |
| **Coverage** | Whatever the LLM remembers | 330K+ indicators, 200+ countries |
| **Recency** | Months or years behind | Up to real-time (FRED, ExchangeRate) |
| **Verifiable** | No source link | Every result includes source URL |

## How It Compares

| Feature | OpenEcon | fredapi | pandas-datareader | fred-mcp-server |
|---------|----------|---------|-------------------|-----------------|
| Natural language queries | Yes | No | No | No |
| Data sources | 10+ | 1 (FRED) | 5 | 1 (FRED) |
| MCP server for AI agents | Yes | No | No | Yes |
| Web UI with charts | Yes | No | No | No |
| Conversational follow-ups | Yes | No | No | No |
| Smart indicator discovery | 330K indexed | Manual codes | Manual codes | Manual codes |
| Self-hostable | Yes | N/A | N/A | Yes |
| No code required | Yes | No | No | No |

## What You Can Ask

```
"US GDP growth last 10 years"                    → FRED, quarterly chart
"Compare China, India, Brazil GDP 2018-2024"     → World Bank, multi-country comparison
"Inflation rate BRICS countries 2019-2023"        → World Bank, 5 countries auto-expanded
"EUR/USD exchange rate last 24 months"            → ExchangeRate-API, currency pair chart
"US unemployment and CPI together since 2010"     → FRED, dual-axis overlay
"China exports to the US 2020-2024"               → UN Comtrade, bilateral trade flow
"China manufacturing PMI this year"               → ChinaMacro, fresh official NBS data
"中国社会融资规模最近一年"                          → ChinaMacro, PBoC social financing
"Credit to GDP ratio US, UK, Japan from BIS"      → BIS, financial stability data
"Bitcoin price last year"                         → CoinGecko, crypto chart
"What inflation indicators does FRED have?"       → Indicator discovery, text response
```

**Conversational follow-ups work naturally:**
```
You: "US GDP last 5 years"          → chart with US GDP
You: "add Germany and Japan"         → updates to 3 countries
You: "what about per capita?"        → switches to GDP per capita
You: "show only 2020-2023"           → narrows time range
```

## Quick Start

### Use the web app (no setup)

**[data.openecon.ai/chat](https://data.openecon.ai/chat)** — try instantly in your browser, no install. Your first 20 queries need no signup. Create a free account (email or Google) to keep going, save your history, and unlock Pro Mode.

### Self-host

```bash
git clone https://github.com/hanlulong/openecon-data.git
cd openecon-data
./scripts/setup.sh            # Installs npm + Python deps, creates backend/.venv, copies .env.example → .env
```

Then edit `.env` and set the two values the backend needs to start:

```bash
OPENROUTER_API_KEY=sk-or-...                  # required (LLM parsing) — https://openrouter.ai/keys
JWT_SECRET=...                                # required — generate with: openssl rand -hex 32
```

Start both servers:

```bash
python3 scripts/restart_dev.py
# Backend: http://localhost:3001  |  Frontend: http://localhost:5173
```

Then ask your first question — plain English in, sourced data out:

```bash
curl -X POST http://localhost:3001/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "US unemployment rate since 2023"}'
```

```jsonc
{
  "data": [{
    "metadata": {
      "source": "FRED",
      "indicator": "Unemployment Rate",
      "unit": "Percent",
      "sourceUrl": "https://fred.stlouisfed.org/series/UNRATE"  // every result links to its source
    },
    "data": [{ "date": "2023-01", "value": 3.4 }, /* ... */]
  }]
}
```

<details>
<summary><b>Manual setup (if you prefer not to use setup.sh)</b></summary>

```bash
npm install
python3 -m venv backend/.venv
source backend/.venv/bin/activate            # Windows: backend\.venv\Scripts\activate
pip install --upgrade pip
pip install -r backend/requirements.txt
cp .env.example .env                         # then set OPENROUTER_API_KEY and JWT_SECRET
python3 scripts/restart_dev.py
```
</details>

<details>
<summary><b>Requirements</b></summary>

- Python 3.10+
- Node.js 18+
- **Required to start the backend:**
  - `OPENROUTER_API_KEY` — [OpenRouter API key](https://openrouter.ai/keys) for LLM parsing (required unless you set `LLM_PROVIDER` to a local model like `vllm`/`ollama`/`lm-studio`)
  - `JWT_SECRET` — any random secret; generate with `openssl rand -hex 32`
- Optional: FRED API key, Comtrade API key, CoinGecko API key
- Optional: Supabase credentials (enables real auth, Google sign-in, and persistent history; mock auth is used in development when omitted)

See [Getting Started Guide](docs/guides/getting-started.md) for full setup instructions.
</details>

### Use the HTTP API

Prefer to call the service directly? The same natural-language queries work over HTTP. Base URL: `https://data.openecon.ai`.

```bash
curl -X POST https://data.openecon.ai/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "US unemployment rate"}'
```

There's also a streaming endpoint — `POST /api/query/stream` returns Server-Sent Events for real-time progress.

See the [API Reference](docs/reference/api.md) for the full endpoint list and request/response schemas.

## How It Works

```
  "Compare US and           ┌──────────────┐        ┌────────────────┐
   Japan inflation"    ───▶ │  LLM Parser  │  ───▶  │  LLM Router    │
                            │  (intent,    │        │  (semantic      │
                            │   countries, │        │   routing +     │
                            │   dates)     │        │   330K index)   │
                            └──────────────┘        └───────┬────────┘
                                                            │
                            ┌────────────┐          ┌───────▼────────┐
                            │ Chart +    │  ◀────── │  Fetch from    │
                            │ CSV/JSON/  │          │  best provider │
                            │ DTA/Python │          │  (FRED, WB,    │
                            └────────────┘          │   IMF, ...)    │
                                                    └────────────────┘
```

1. **Parse** — An LLM extracts intent, countries, indicators, and date range from plain English
2. **Route** — Semantic routing picks the best provider and series from 330K+ indicators
3. **Fetch** — Data retrieved from official APIs with automatic fallback if a source is down
4. **Return** — Interactive chart, or structured data via MCP for your agent

## Features

**MCP Server** — First-class [Model Context Protocol](https://modelcontextprotocol.io) support. Give Claude Code, Codex, or any MCP-compatible agent access to verified economic data.

**Natural Language** — No API docs, no country codes, no series IDs. Just describe what you want.

**Ask in Your Language** — Query in English, 中文, Español, Français, and more. The parser detects your language, finds the right indicator, and (for key messages) replies in kind. Try "北京的GDP" or "PIB de México".

**330K Indicator Discovery** — Full-text search across FRED, World Bank, IMF, Eurostat, BIS, and more. Ask "What trade data does Comtrade have?" and get a browsable list.

**Multi-Round Conversations** — Follow up naturally: add countries, change time ranges, switch indicators. Context is preserved across turns, so "now add Germany" just works.

**Smart Routing** — The system understands what you mean, not just what you type. It picks the right provider (FRED for US data, World Bank for global comparisons, Comtrade for trade flows) based on the meaning of your query.

**Multi-Country Comparisons** — Say "G7", "BRICS", "EU", "ASEAN", "Nordic" or list specific countries. Auto-expands to all members.

**Fast** — Repeat queries return in ~0.1 seconds. First-time queries typically take a few seconds; the first uncached query can take longer.

**Resilient** — If one provider is down, the system automatically falls back to the next-best source. No manual retries needed.

**Clarifies Ambiguity** — When a query could mean multiple things ("inflation" could be CPI, PCE, or GDP deflator), the system asks you to pick rather than guessing wrong.

**Multi-Format Export** — CSV, JSON, DTA (Stata), and Python code. Every export includes source attribution.

**Pro Mode** — AI-generated Python for advanced analysis: custom transformations, derived indicators, bespoke charts. Available to registered users on the hosted app. Disabled by default when self-hosting; set `PROMODE_ENABLED=true` only with proper sandboxing, since it executes generated code.

**Streaming** — Real-time progress via Server-Sent Events.

**Accounts & Auth** — Try the first 20 queries with no signup. Sign in with email + password or Google to save history and unlock Pro Mode. Includes email verification, password reset, and JWT-based sessions. Authentication is backed by Supabase (with a local mock-auth fallback for development).

**Self-Hostable** — AGPL-3.0 licensed. Add new providers by implementing a single base class.

## Performance

| Metric | Value |
|--------|-------|
| First query | typically a few seconds (first uncached query can take longer) |
| Repeat query (cached) | ~0.1s |
| Indicator database | 330,000+ indexed series across 11 providers |

## Data Sources

11 providers, 330K+ indexed indicators:

| Provider | Coverage | Indicators | API Key |
|----------|----------|-----------|---------|
| **FRED** | US macroeconomic data (GDP, CPI, employment, rates) | 90,000+ series | Free |
| **World Bank** | Global development (200+ countries, poverty, health) | 16,000+ indicators | None |
| **IMF** | Balance of payments, exchange rates, fiscal data | Extensive | None |
| **Eurostat** | EU member states (HICP, labor, trade) | Extensive | None |
| **UN Comtrade** | Bilateral trade flows by HS commodity code | All HS codes | Free |
| **BIS** | Credit-to-GDP, property prices, debt securities | Curated | None |
| **Statistics Canada** | Canadian economic tables (labor, trade, prices) | 40,000+ tables | None |
| **OECD** | OECD member country statistics | Extensive | None |
| **ExchangeRate-API** | 160+ currency pairs, live and historical | Live & historical | Free |
| **CoinGecko** | Cryptocurrency prices and market data | 10,000+ coins | Free |
| **ChinaMacro** | Fresh Chinese headline macro (PMI, M2, social financing, CPI/PPI, 10Y yield) | 14 curated series | None |

## Who Is This For?

| Role | How they use it |
|------|----------------|
| **AI Agent Builders** | Add economic data capabilities to any MCP-compatible agent — verified data, not hallucinations |
| **Economists & Researchers** | Quick data pulls for papers without writing API code |
| **Policy Analysts** | Cross-country comparisons (G7, BRICS, EU) with one query |
| **Students** | Learn by exploring — ask questions, see data, export for assignments |
| **Journalists** | Fact-check economic claims against official sources in seconds |

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌──────────────────────────┐
│  User / Agent   │────▶│  FastAPI Backend  │────▶│  Data Providers          │
│                 │     │                  │     │                          │
│  "US inflation" │     │  LLM Parser      │     │  FRED · World Bank · IMF │
│                 │◀────│  LLM Router      │◀────│  Eurostat · BIS · ...    │
│  Chart + Data   │     │  330K Index      │     │                          │
└─────────────────┘     └──────────────────┘     └──────────────────────────┘
        │                        │
   React Frontend          MCP Endpoint
   (Vite + Recharts)     (SSE Transport)
```

**Stack:** Python · FastAPI · React · TypeScript · Vite · Recharts · Redis · OpenRouter

## Contributing

Contributions welcome! See the [Developer & Contributor Guide](docs/development/DEVELOPER_CONTRIBUTOR_GUIDE.md).

- [Open issues](https://github.com/hanlulong/openecon-data/issues) — bug reports and feature requests
- [Documentation](docs/README.md) — full docs index
- [Security policy](.github/SECURITY.md) — responsible disclosure

If you find this useful, a star helps others discover the project.

## License

[AGPL-3.0](LICENSE) — Free to use, modify, and self-host. If you run a modified version as a service, you must share your changes. For commercial licensing, [contact us](mailto:security@openecon.ai).
