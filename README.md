<p align="center">
  <img src="packages/frontend/public/favicon.svg" width="80" height="80" alt="OpenEcon logo" />
</p>

<h1 align="center">OpenEcon Data</h1>

<p align="center">
  <strong>The open-source ChatGPT for economic data.</strong><br/>
  Ask in plain English. Get charts from FRED, World Bank, IMF, Eurostat, and 6 more sources — instantly.
</p>

<p align="center">
  <a href="https://data.openecon.io/chat"><img src="https://img.shields.io/badge/Try_it-Live_Demo-blue?style=flat-square" alt="Live Demo" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="MIT License" /></a>
  <a href="https://github.com/hanlulong/openecon-data/stargazers"><img src="https://img.shields.io/github/stars/hanlulong/openecon-data?style=flat-square" alt="Stars" /></a>
  <a href="https://github.com/hanlulong/openecon-data/issues"><img src="https://img.shields.io/github/issues/hanlulong/openecon-data?style=flat-square" alt="Issues" /></a>
  <img src="https://img.shields.io/badge/Python-3.10+-blue?style=flat-square&logo=python&logoColor=white" alt="Python" />
  <img src="https://img.shields.io/badge/React-18+-61DAFB?style=flat-square&logo=react&logoColor=white" alt="React" />
  <img src="https://img.shields.io/badge/MCP-Compatible-purple?style=flat-square" alt="MCP Compatible" />
</p>

<p align="center">
  <a href="https://openecon.ai">Website</a> &middot;
  <a href="https://data.openecon.io/chat">Live App</a> &middot;
  <a href="docs/README.md">Docs</a> &middot;
  <a href="docs/development/DEVELOPER_CONTRIBUTOR_GUIDE.md">Contributing</a>
</p>

---

<p align="center">
  <img src="docs/assets/demo.gif" width="800" alt="OpenEcon Data — type a query in plain English, get a chart with data from FRED, World Bank, and more" />
</p>

## Why OpenEcon Data?

Economists, researchers, and analysts waste hours wrangling APIs, deciphering country codes, and normalizing data from different sources. OpenEcon Data eliminates that friction:

- **Ask in English** — "Compare US and Japan inflation since 2015" just works
- **10+ sources, one interface** — FRED, World Bank, IMF, Eurostat, BIS, UN Comtrade, StatsCan, OECD, ExchangeRate-API, CoinGecko
- **Conversational** — follow up naturally: "now add Germany", "what about 2010?", "show only G7"
- **MCP-native** — plug into Claude, Codex, or any MCP-compatible AI agent with one command
- **Self-hostable** — MIT-licensed, run locally or deploy your own instance
- **330,000+ indicators** — full-text search across the world's major economic databases

<p align="center">
  <b>330K+</b> indicators &nbsp;&middot;&nbsp; <b>10</b> data providers &nbsp;&middot;&nbsp; <b>200+</b> countries &nbsp;&middot;&nbsp; <b>67</b> curated concepts &nbsp;&middot;&nbsp; <b>< 5s</b> avg. query time
</p>

## Who Is This For?

| Role | How they use it |
|------|----------------|
| **Economists & Researchers** | Quick data pulls for papers — GDP, inflation, trade, debt — without writing API code |
| **Policy Analysts** | Cross-country comparisons (G7, BRICS, EU) with one query |
| **Students** | Learn by exploring — ask questions, see data, export for assignments |
| **Journalists** | Fact-check claims against official sources in seconds |
| **AI Agent Builders** | Add economic data capabilities to any MCP-compatible agent |

## Quick Start

### Use the hosted app (no setup)

Try it now at **[data.openecon.io/chat](https://data.openecon.io/chat)** — no account required.

### Add to your AI agent (one command)

**Claude Code:**
```bash
claude mcp add --transport sse openecon-data https://data.openecon.io/mcp --scope user
```

**Codex:**
```bash
codex mcp add openecon-data --url https://data.openecon.io/mcp
```

Then ask your agent:
```
Use query_data to compare US, UK, and Japan inflation from 2015 to 2025.
```

### Self-host

```bash
git clone https://github.com/hanlulong/openecon-data.git
cd openecon-data
cp .env.example .env          # Add your OPENROUTER_API_KEY
pip install -r requirements.txt
npm install
python3 scripts/restart_dev.py
# Backend: http://localhost:3001  |  Frontend: http://localhost:5173
```

<details>
<summary><b>Requirements</b></summary>

- Python 3.10+
- Node.js 18+
- An [OpenRouter API key](https://openrouter.ai/keys) (required for LLM parsing)
- Optional: FRED API key, Comtrade API key, CoinGecko API key
- Optional: Supabase credentials (for auth + persistent history)

See [Getting Started Guide](docs/guides/getting-started.md) for full setup instructions.
</details>

## Example Queries

| Query | Source | What you get |
|-------|--------|-------------|
| "US GDP growth for the last 10 years" | FRED | Quarterly GDP growth chart |
| "Compare China, India, and Brazil GDP 2018-2024" | World Bank | Multi-country comparison |
| "Inflation rate BRICS countries 2019-2023" | World Bank | 5-country chart (BR, RU, IN, CN, ZA) |
| "EUR/USD exchange rate last 24 months" | ExchangeRate-API | Currency pair line chart |
| "US unemployment rate and CPI together since 2010" | FRED | Dual-axis overlay chart |
| "China exports to the United States 2020-2024" | UN Comtrade | Bilateral trade flow |
| "EU debt-to-GDP ratios across member states" | Eurostat | 27-country comparison |
| "Credit to GDP ratio US, UK, Japan from BIS" | BIS | Financial stability data |
| "Bitcoin price history for the last year" | CoinGecko | Crypto price chart |
| "What inflation indicators does FRED have?" | FRED | Indicator discovery (text) |

**Conversational follow-ups:**
```
You: "US GDP last 5 years"          → chart with US GDP
You: "add Germany and Japan"         → chart updates to 3 countries
You: "what about per capita?"        → switches to GDP per capita
You: "show only 2020-2023"           → narrows time range
```

## How It Works

```
  "Compare US and           ┌──────────────┐        ┌────────────────┐
   Japan inflation"    ───▶ │  LLM Parser  │  ───▶  │  Smart Router  │
                            │  (intent,    │        │  (67 curated   │
                            │   countries, │        │   concepts +   │
                            │   dates)     │        │   330K index)  │
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
2. **Route** — A curated catalog of 67 economic concepts + 330K indicator index picks the best provider
3. **Fetch** — Data is retrieved, normalized, and aligned across sources
4. **Visualize** — Interactive chart with CSV, JSON, DTA (Stata), and Python export

## Features

**Natural Language Interface** — No API docs, no country codes, no series IDs. Just describe what you want.

**Smart Indicator Discovery** — 330K+ indicators indexed with full-text search. Ask "What trade data does Comtrade have?" and get a browsable list.

**Conversational Follow-ups** — Refine results naturally: add countries, change time ranges, switch indicators — the system remembers context.

**Multi-Country Comparisons** — Say "G7", "BRICS", "EU", "ASEAN", "Nordic", or list specific countries. Auto-expands to all members.

**Curated Concept Catalog** — 67 hand-verified economic concepts map natural language to the right indicator at the right provider. "US jobless claims" routes to FRED/ICSA, not a generic unemployment rate.

**Streaming Results** — Real-time progress via Server-Sent Events. See each step as it happens.

**MCP Server** — First-class [Model Context Protocol](https://modelcontextprotocol.io) support. Any MCP-compatible AI agent can query economic data through the hosted endpoint.

**Multi-Format Export** — Download as CSV, JSON, DTA (Stata), or Python code. Every export includes source attribution.

**Self-Hostable & Extensible** — MIT-licensed. Add new providers by implementing a single base class.

## Data Sources

| Provider | Coverage | Indicators | API Key |
|----------|----------|-----------|---------|
| **FRED** | US macroeconomic data | 90,000+ series | Free |
| **World Bank** | Global development | 16,000+ indicators | None |
| **IMF** | International financial statistics | Extensive | None |
| **Eurostat** | EU member states | Extensive | None |
| **UN Comtrade** | International trade flows | All HS codes | Free |
| **BIS** | Central bank & financial stability | Curated | None |
| **Statistics Canada** | Canadian economic data | 40,000+ tables | None |
| **OECD** | OECD member countries | Extensive | None |
| **ExchangeRate-API** | 160+ currencies | Live & historical | Free |
| **CoinGecko** | Cryptocurrencies | 10,000+ coins | Free |

## Architecture

```
┌─────────────────┐     ┌──────────────────┐     ┌──────────────────────────┐
│  User / Agent   │────▶│  FastAPI Backend  │────▶│  Data Providers          │
│                 │     │                  │     │                          │
│  "US inflation" │     │  LLM Parser      │     │  FRED · World Bank · IMF │
│                 │◀────│  Query Router    │◀────│  Eurostat · BIS · ...    │
│  Chart + Data   │     │  Normalizer      │     │                          │
└─────────────────┘     └──────────────────┘     └──────────────────────────┘
        │                        │
   React Frontend          MCP Endpoint
   (Vite + Recharts)     (SSE Transport)
```

**Stack:** Python · FastAPI · React · TypeScript · Vite · Recharts · FAISS · OpenRouter

## OpenEcon Ecosystem

| Project | Description |
|---------|-------------|
| **[OpenEcon Data](https://data.openecon.io/chat)** | Query economic data in plain English, chart results, export CSV/JSON *(this repo)* |
| **[Econ Writing Skill](https://github.com/hanlulong/econ-writing-skill)** | Reusable writing workflows for economists — prompt patterns, structure templates, analysis guardrails |
| **[Awesome AI for Economists](https://github.com/hanlulong/awesome-ai-for-economists)** | Curated tools, papers, and references for applying AI in economic research |

## Contributing

Contributions are welcome! See the [Developer & Contributor Guide](docs/development/DEVELOPER_CONTRIBUTOR_GUIDE.md) for setup instructions, architecture overview, and code standards.

- [Open issues](https://github.com/hanlulong/openecon-data/issues) — bug reports and feature requests
- [Documentation](docs/README.md) — full docs index
- [Security policy](.github/SECURITY.md) — responsible disclosure

If you find this useful, consider giving it a star — it helps others discover the project.

## License

[MIT](LICENSE)
