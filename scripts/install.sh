#!/usr/bin/env bash
# OpenEcon Data — Zero-config installer for AI coding agents
# Usage: curl -fsSL https://raw.githubusercontent.com/hanlulong/openecon-data/main/scripts/install.sh | bash

set -e
trap 'echo "Error on line $LINENO. Installation may be incomplete." >&2' ERR

ENDPOINT="https://data.openecon.ai/mcp"
NAME="openecon-data"

echo "🌍 Installing OpenEcon Data — verified economic data for your AI agent"
echo ""

installed=0

# Detect Claude Code
CLAUDE_CMD=""
if command -v claude > /dev/null 2>&1; then
    CLAUDE_CMD="claude"
else
    # Check common npm global paths
    for dir in "$HOME/.npm-global/bin" "$HOME/.nvm/versions/node"/*/bin /usr/local/bin; do
        if [ -x "$dir/claude" ]; then
            CLAUDE_CMD="$dir/claude"
            break
        fi
    done
fi

if [ -n "$CLAUDE_CMD" ]; then
    echo "✓ Found Claude Code — configuring MCP server..."
    # Remove first for idempotency, then add fresh
    "$CLAUDE_CMD" mcp remove "$NAME" --scope user > /dev/null 2>&1 || true
    if "$CLAUDE_CMD" mcp add --transport sse "$NAME" "$ENDPOINT" --scope user 2>&1; then
        echo "  ✅ Claude Code configured"
        installed=1
    else
        echo "  ⚠️  Claude Code setup encountered an issue. Try manually:"
        echo "      claude mcp add --transport sse $NAME $ENDPOINT --scope user"
    fi
fi

# Detect Codex
CODEX_CMD=""
if command -v codex > /dev/null 2>&1; then
    CODEX_CMD="codex"
fi

if [ -n "$CODEX_CMD" ]; then
    echo "✓ Found Codex — configuring MCP server..."
    if "$CODEX_CMD" mcp add "$NAME" --url "$ENDPOINT" 2>&1; then
        echo "  ✅ Codex configured"
        installed=1
    else
        echo "  ⚠️  Codex setup encountered an issue. Try manually:"
        echo "      codex mcp add $NAME --url $ENDPOINT"
    fi
fi

# Check if neither found
if [ "$installed" -eq 0 ]; then
    echo "No AI coding agent detected in PATH."
    echo ""
    echo "Install manually:"
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
