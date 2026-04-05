# MCP Client Setup (Claude Code + Codex)

`openecon-data` is a one-stop economic data layer exposed as a single MCP tool: `query_data`.

## Endpoints

- Hosted: `https://data.openecon.ai/mcp`
- Local: `http://localhost:3001/mcp`

## Tool Exposed

- Tool name: `query_data`
- Purpose: query multiple economic data sources through one interface

## Add to Codex

```bash
# Hosted
codex mcp add openecon-data --url https://data.openecon.ai/mcp

# Or local
codex mcp add openecon-data-local --url http://localhost:3001/mcp

# Verify
codex mcp list
codex mcp get openecon-data
```

## Add to Claude Code

```bash
# Hosted
claude mcp add --transport sse openecon-data https://data.openecon.ai/mcp --scope user

# Or local
claude mcp add --transport sse openecon-data-local http://localhost:3001/mcp --scope user

# Verify
claude mcp get openecon-data
```

## Example Prompts (Claude Code / Codex)

- `Use query_data to compare US and Canada GDP growth since 2015.`
- `Use query_data to get China exports to the United States from 2020 to 2024.`
- `Use query_data to fetch US unemployment rate and CPI together since 2010.`
- `Use query_data to show EUR/USD history for the last 24 months.`

## Local Run Prerequisite

If using the local endpoint, start backend first:

```bash
source backend/.venv/bin/activate
npm run dev:backend
```

Then verify:

```bash
curl http://localhost:3001/api/health
curl http://localhost:3001/mcp
```

## Troubleshooting

- If the tool is missing, remove and re-add the MCP server in your client.
- If local MCP fails, confirm backend is running on port `3001`.
- If hosted MCP fails, verify network/firewall rules and endpoint reachability.
