# /econ-data — Verified economic data from official sources

Query 330K+ economic indicators from 10 official sources: FRED, World Bank, IMF, Eurostat, BIS, UN Comtrade, Statistics Canada, OECD, ExchangeRate-API, and CoinGecko.

**No hallucinated numbers** — every result comes from official statistical agencies with source attribution.

## Usage

`/econ-data <natural language query>`

## Examples

```
/econ-data US GDP growth last 10 years
/econ-data Compare inflation across G7 countries 2019-2024
/econ-data China exports to the United States 2020-2023
/econ-data Bitcoin price last 30 days
/econ-data EUR/USD exchange rate this year
/econ-data What trade indicators does Comtrade have?
/econ-data unemployment rate BRICS nations
```

## Instructions

When this command is invoked with `$ARGUMENTS`:

1. **Try MCP first:** Call the `openecon-data` MCP server's `query_data` tool with the user's query as the `query` parameter.

2. **HTTP fallback:** If the MCP server is not connected, make this request instead:
   ```bash
   curl -s -X POST https://data.openecon.ai/api/query \
     -H "Content-Type: application/json" \
     -d '{"query": "$ARGUMENTS"}'
   ```

3. **Parse the response:**
   - `data` field contains the time series (array of `{date, value}` points)
   - `data[].metadata.source` = provider name (FRED, World Bank, etc.)
   - `data[].metadata.indicator` = indicator description
   - `data[].metadata.seriesId` = official series code
   - `message` field contains text responses (for informational queries)
   - `clarificationNeeded` = true means the system needs more info — relay options to user

4. **Present results:**
   - Summarize key findings in a table or bullet points
   - Include the data source, time range, and number of observations
   - For multi-country data, show a comparison
   - Mention that interactive charts are available at https://data.openecon.ai/chat
   - Always cite: "Source: [provider name]"

5. **Handle errors gracefully:**
   - If no data found, suggest rephrasing or checking https://data.openecon.ai/chat
   - If timeout, the query may be complex — suggest trying a simpler version

## What This Covers

| Category | Examples |
|----------|---------|
| GDP & Growth | GDP, GDP per capita, GDP growth rate, real GDP |
| Prices & Inflation | CPI, PPI, HICP, inflation rate, deflator |
| Labor | Unemployment, employment, labor force, wages, jobless claims |
| Trade | Exports, imports, trade balance, bilateral trade flows |
| Finance | Interest rates, bond yields, exchange rates, money supply |
| Government | Debt, fiscal balance, spending, tax revenue |
| Development | Poverty, education, health, life expectancy, population |
| Commodities | Oil prices, natural gas, crypto, currency exchange rates |

**200+ countries covered** including all G7, G20, BRICS, EU, ASEAN, Nordic, and OECD members.

## Setup

If the MCP server is not connected:
```bash
claude mcp add --transport sse openecon-data https://data.openecon.ai/mcp --scope user
```

This only needs to be done once. The server will be available in all future sessions.
