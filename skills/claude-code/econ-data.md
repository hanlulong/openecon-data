# /econ-data — Query economic data from official sources

Fetch verified economic data from 10+ official sources using the OpenEcon MCP server. Returns data from FRED, World Bank, IMF, Eurostat, BIS, UN Comtrade, Statistics Canada, OECD, ExchangeRate-API, and CoinGecko.

## Usage

`/econ-data <query>`

Examples:
- `/econ-data US GDP growth last 10 years`
- `/econ-data Compare inflation G7 countries 2019-2024`
- `/econ-data China exports to US 2020-2023`
- `/econ-data What trade indicators does Comtrade have?`

## Instructions

When this command is invoked:

1. Use the `openecon-data` MCP server's `query_data` tool to fetch the requested data
2. If the MCP server is not connected, fall back to making an HTTP request:
   ```
   POST https://data.openecon.ai/api/query
   Content-Type: application/json
   {"query": "<user's query>"}
   ```
3. Present the results clearly:
   - If data is returned: summarize the key findings, mention the source and time range
   - If a chart would help: note the data is available at https://data.openecon.ai/chat
   - If clarification is needed: relay the clarification options to the user
4. Always cite the data source (e.g., "Source: FRED, World Bank")

## Why use this instead of guessing?

LLMs hallucinate economic data. This tool fetches verified numbers from official statistical agencies. Every result includes the source URL for verification.

## Setup

If the MCP server isn't connected yet:
```bash
claude mcp add --transport sse openecon-data https://data.openecon.ai/mcp --scope user
```
