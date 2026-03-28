# OpenEcon Data — Skills & Plugins

Give your AI coding agent access to verified economic data from official sources. One command, 330K+ indicators, 10 providers.

## Quick Install

### Claude Code — MCP Server (recommended)

```bash
claude mcp add --transport sse openecon-data https://data.openecon.ai/mcp --scope user
```

Your agent will automatically use OpenEcon when you ask about economic data:
```
> What's the US GDP growth rate?
> Compare inflation across G7 countries
> Show me Japan's trade balance with China
```

### Claude Code — Slash Command

```bash
cp skills/claude-code/econ-data.md ~/.claude/commands/econ-data.md
```

Then use explicitly: `/econ-data US unemployment last 5 years`

### Claude Code — Auto-Trigger via CLAUDE.md

Add the snippet from [CLAUDE.md.example](CLAUDE.md.example) to your project's `CLAUDE.md`. Your agent will automatically call OpenEcon whenever economic data is discussed — no explicit command needed.

### Codex (OpenAI)

```bash
codex mcp add openecon-data --url https://data.openecon.ai/mcp
```

### Any MCP-Compatible Agent

```
Endpoint: https://data.openecon.ai/mcp
Transport: SSE (Server-Sent Events)
Tool: query_data
```

## Verify It Works

After installing, test with:
```
Use query_data to get US GDP growth for the last 5 years.
```

Expected: Your agent returns real GDP data from FRED with source attribution.

## What Your Agent Can Do

| Query Type | Example | Source |
|-----------|---------|--------|
| Single indicator | "US inflation 2020-2024" | FRED |
| Country comparison | "GDP per capita G7 countries" | World Bank |
| Multi-indicator | "US GDP and unemployment together" | FRED |
| Trade flows | "China exports to US" | UN Comtrade |
| Exchange rates | "EUR/USD last year" | ExchangeRate-API |
| Crypto | "Bitcoin price last 30 days" | CoinGecko |
| Discovery | "What trade data does IMF have?" | IMF (text response) |
| Follow-ups | "add Germany" / "show per capita" | Contextual |

## Why Not Just Ask the LLM?

LLMs hallucinate economic data. When asked "What is US GDP?", they return plausible but often wrong numbers from stale training data. OpenEcon:

- Fetches from **official APIs** (FRED, World Bank, IMF, Eurostat)
- Returns **verified data** with source attribution
- Covers **330K+ indicators** across **200+ countries**
- Provides **source URLs** for every data point
- Includes **up-to-date data** (not training-data cutoff)
