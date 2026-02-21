#!/bin/bash
# Claude Code Hook - Auto Review on Task Completion
#
# This hook generates a code review prompt after Claude Code finishes a task.
# Uses Claude self-review as the default method (Codex optional).
#
# Hook Event: Stop (runs when main agent finishes responding)

set -e

# Configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(dirname "$SCRIPT_DIR")}"
SELF_REVIEWER="$SCRIPT_DIR/claude_self_review.py"
CODEX_REVIEWER="$SCRIPT_DIR/codex_reviewer.py"
LOG_FILE="/tmp/claude_review_hook.log"
REVIEW_DIR="$PROJECT_DIR/.claude_review"

# Parse stdin JSON to get context
INPUT=$(cat)
HOOK_EVENT=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('hook_event_name',''))" 2>/dev/null || echo "")

# Log hook execution
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

log "Hook triggered: $HOOK_EVENT"

# Only run on Stop events
if [[ "$HOOK_EVENT" != "Stop" ]]; then
    log "Skipping: Not a Stop event"
    exit 0
fi

# Check if there are uncommitted changes
cd "$PROJECT_DIR"
if git diff --quiet && git diff --cached --quiet; then
    log "No uncommitted changes, skipping review"
    exit 0
fi

# Count changed files
CHANGED_FILES=$(git status --porcelain | wc -l)
log "Found $CHANGED_FILES changed files"

# Only review if there are significant changes (more than 2 files)
if [[ "$CHANGED_FILES" -lt 3 ]]; then
    log "Few changes ($CHANGED_FILES files), skipping review"
    exit 0
fi

# Create review directory
mkdir -p "$REVIEW_DIR"

# Check if self-reviewer exists
if [[ ! -f "$SELF_REVIEWER" ]]; then
    log "Error: Self-reviewer script not found at $SELF_REVIEWER"
    exit 0
fi

# ============================================================================
# DEFAULT: Generate Claude self-review prompt
# ============================================================================

log "Generating Claude self-review prompt..."

REVIEW_PROMPT="$REVIEW_DIR/review_prompt.md"
python3 "$SELF_REVIEWER" --uncommitted --output "$REVIEW_PROMPT" >> "$LOG_FILE" 2>&1 || true

if [[ -f "$REVIEW_PROMPT" ]]; then
    log "Review prompt generated at $REVIEW_PROMPT"

    echo ""
    echo "=========================================="
    echo "Code Review Available ($CHANGED_FILES files changed)"
    echo "=========================================="
    echo ""
    echo "A review prompt has been generated at:"
    echo "  $REVIEW_PROMPT"
    echo ""
    echo "To run the review, use one of these options:"
    echo ""
    echo "  Option 1 - Ask Claude to review:"
    echo "    'Please review the code changes in .claude_review/review_prompt.md'"
    echo ""
    echo "  Option 2 - Run manually:"
    echo "    python3 scripts/claude_self_review.py --uncommitted"
    echo ""

    # ========================================================================
    # OPTIONAL: Try Codex review if available and authenticated
    # ========================================================================

    if command -v codex &>/dev/null; then
        # Check if Codex is authenticated
        if [[ -f ~/.codex/auth.json ]] || [[ -f ~/.config/codex/auth.json ]]; then
            # Quick auth check
            if ! codex --version 2>&1 | grep -q "401\|Unauthorized"; then
                echo "  Option 3 - Codex AI Review (authenticated):"
                echo "    python3 scripts/codex_reviewer.py changes --uncommitted"
                echo ""
            fi
        fi
    fi

    echo "=========================================="
else
    log "Failed to generate review prompt"
fi

log "Hook completed"
exit 0
