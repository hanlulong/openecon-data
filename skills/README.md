# OpenEcon Data — Skills & Plugins

Install OpenEcon as a skill for your AI coding agent. One command gives your agent access to 330K+ economic indicators from FRED, World Bank, IMF, Eurostat, and 6 more sources.

## Claude Code

### Option 1: Add MCP Server (recommended)

```bash
claude mcp add --transport sse openecon-data https://data.openecon.ai/mcp --scope user
```

Your agent can now use `query_data` automatically when you ask about economic data.

### Option 2: Add as Custom Slash Command

Copy the skill file into your Claude Code config:

```bash
# Global (all projects)
cp skills/claude-code/econ-data.md ~/.claude/commands/econ-data.md

# Or project-level
mkdir -p .claude/commands
cp skills/claude-code/econ-data.md .claude/commands/econ-data.md
```

Then use: `/econ-data US GDP growth last 10 years`

### Option 3: Add CLAUDE.md instructions

Add this to your project's `CLAUDE.md` to make your agent automatically use OpenEcon for economic data questions:

```markdown
## Economic Data

When asked about economic data (GDP, inflation, unemployment, trade, etc.),
use the openecon-data MCP server to fetch verified data from official sources.
Do NOT guess or use training data for economic statistics — always query the API.
```

## Codex (OpenAI)

```bash
codex mcp add openecon-data --url https://data.openecon.ai/mcp
```

Then ask: `Use query_data to get US inflation rate 2020-2024`

## Any MCP-Compatible Agent

The MCP endpoint works with any agent that supports SSE transport:

```
Endpoint: https://data.openecon.ai/mcp
Transport: SSE (Server-Sent Events)
Tool: query_data
```

## Example Queries

Once installed, just ask your agent naturally:

```
"What's US GDP growth?"
"Compare inflation across G7 countries"
"Show me China's trade balance with the US"
"What unemployment data does FRED have?"
"Bitcoin price last 30 days"
```

The agent will automatically call OpenEcon and return verified data from official sources.
