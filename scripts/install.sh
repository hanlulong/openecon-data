#!/usr/bin/env bash
# OpenEcon Data — Zero-config installer for AI coding agents
# Usage: curl -sL https://raw.githubusercontent.com/hanlulong/openecon-data/main/scripts/install.sh | bash

set -e

ENDPOINT="https://data.openecon.ai/mcp"
NAME="openecon-data"

echo "🌍 Installing OpenEcon Data — verified economic data for your AI agent"
echo ""

installed=0

# Detect Claude Code
if command -v claude &> /dev/null; then
    echo "✓ Found Claude Code — adding MCP server..."
    claude mcp add --transport sse "$NAME" "$ENDPOINT" --scope user 2>/dev/null && {
        echo "  ✅ Claude Code configured"
        installed=1
    } || echo "  ⚠️  Claude Code MCP add failed (may already be configured)"
fi

# Detect Codex
if command -v codex &> /dev/null; then
    echo "✓ Found Codex — adding MCP server..."
    codex mcp add "$NAME" --url "$ENDPOINT" 2>/dev/null && {
        echo "  ✅ Codex configured"
        installed=1
    } || echo "  ⚠️  Codex MCP add failed (may already be configured)"
fi

# Check if neither found
if [ "$installed" -eq 0 ]; then
    echo "No AI coding agent detected. Install manually:"
    echo ""
    echo "  Claude Code:  claude mcp add --transport sse $NAME $ENDPOINT --scope user"
    echo "  Codex:        codex mcp add $NAME --url $ENDPOINT"
    echo ""
    echo "Or use the web app directly: https://data.openecon.ai/chat"
    exit 0
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Done! Just ask your agent about economic data:"
echo ""
echo '   "What is US GDP growth?"'
echo '   "Compare inflation across G7 countries"'
echo '   "Show me Japan trade balance with China"'
echo ""
echo "No special commands needed — your agent will"
echo "automatically fetch verified data from official sources."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
