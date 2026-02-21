#!/usr/bin/env python3
"""
Claude Self-Review Script

When Codex is unavailable, this script generates a review prompt
that can be used with Claude Code itself.

Usage:
    python3 scripts/claude_self_review.py --uncommitted
    python3 scripts/claude_self_review.py --base main
"""

import argparse
import subprocess
import sys
import json
from pathlib import Path
from datetime import datetime


def get_git_diff(uncommitted: bool = False, base: str = None) -> tuple[str, list[str]]:
    """Get git diff and list of changed files."""
    try:
        if uncommitted:
            # Get both staged and unstaged changes
            diff_result = subprocess.run(
                ["git", "diff", "HEAD"],
                capture_output=True, text=True
            )
            files_result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True, text=True
            )
            files = [line[3:] for line in files_result.stdout.strip().split("\n") if line]
        elif base:
            diff_result = subprocess.run(
                ["git", "diff", base],
                capture_output=True, text=True
            )
            files_result = subprocess.run(
                ["git", "diff", "--name-only", base],
                capture_output=True, text=True
            )
            files = [f for f in files_result.stdout.strip().split("\n") if f]
        else:
            return "", []

        return diff_result.stdout, files
    except Exception as e:
        print(f"Error getting git diff: {e}", file=sys.stderr)
        return "", []


def generate_review_prompt(diff: str, files: list[str], focus: str = "all") -> str:
    """Generate a review prompt for Claude."""

    prompt = f"""# Code Review Request

Please review the following code changes thoroughly. This is an automated review request.

## Changed Files ({len(files)} files)
{chr(10).join(f"- {f}" for f in files[:30])}
{"..." if len(files) > 30 else ""}

## Review Focus Areas
1. **Security**: Check for vulnerabilities, injection risks, auth issues, secrets exposure
2. **Bugs**: Logic errors, edge cases, null handling, race conditions
3. **Performance**: Inefficient algorithms, memory leaks, unnecessary operations
4. **Architecture**: Design patterns, separation of concerns, coupling
5. **Testing**: Missing tests, coverage gaps

## Review Output Format
For each issue found, provide:
- **Severity**: CRITICAL, HIGH, MEDIUM, or LOW
- **Category**: SECURITY, BUG, PERFORMANCE, ARCHITECTURE, or STYLE
- **File**: Path and line number
- **Issue**: Brief description
- **Fix**: Suggested code change

## Git Diff
```diff
{diff[:50000]}
```
{"... (diff truncated)" if len(diff) > 50000 else ""}

Please provide a structured review with actionable findings.
"""
    return prompt


def main():
    parser = argparse.ArgumentParser(description="Generate Claude self-review prompt")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--uncommitted", "-u", action="store_true",
                       help="Review uncommitted changes")
    group.add_argument("--base", "-b", type=str, metavar="BRANCH",
                       help="Review changes against base branch")
    parser.add_argument("--output", "-o", type=str,
                        help="Save prompt to file")
    parser.add_argument("--focus", "-f", type=str,
                        choices=["security", "performance", "all"],
                        default="all", help="Review focus area")

    args = parser.parse_args()

    # Get diff
    diff, files = get_git_diff(
        uncommitted=args.uncommitted,
        base=args.base
    )

    if not files:
        print("No changes detected.")
        sys.exit(0)

    print(f"Found {len(files)} changed files")

    # Generate prompt
    prompt = generate_review_prompt(diff, files, args.focus)

    if args.output:
        Path(args.output).write_text(prompt)
        print(f"Review prompt saved to: {args.output}")
    else:
        # Save to default location
        review_dir = Path(".codex_review")
        review_dir.mkdir(exist_ok=True)
        output_path = review_dir / "claude_review_prompt.md"
        output_path.write_text(prompt)

        print(f"\nReview prompt saved to: {output_path}")
        print("\nTo run the review with Claude Code:")
        print(f"  claude 'Please review the changes in {output_path}'")
        print("\nOr copy the prompt and paste it into a conversation.")


if __name__ == "__main__":
    main()
