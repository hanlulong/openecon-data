#!/usr/bin/env python3
"""
Versatile Codex Code Reviewer Agent

This script uses the Codex CLI to perform comprehensive code reviews on:
- Git changes (uncommitted, branch diff, specific commits)
- Specific files or directories
- Entire codebase or infrastructure
- Architecture and design patterns

Uses highest reasoning level (xhigh) with gpt-5.2-codex for deep analysis.

Usage:
    # Review git changes
    python scripts/codex_reviewer.py changes --uncommitted
    python scripts/codex_reviewer.py changes --base main
    python scripts/codex_reviewer.py changes --commit HEAD

    # Review specific files
    python scripts/codex_reviewer.py files backend/services/*.py
    python scripts/codex_reviewer.py files packages/frontend/src/components/

    # Review infrastructure
    python scripts/codex_reviewer.py infra --focus "testing"
    python scripts/codex_reviewer.py infra --focus "security"

    # Review entire codebase
    python scripts/codex_reviewer.py codebase --area backend
    python scripts/codex_reviewer.py codebase --area frontend
"""

import argparse
import json
import subprocess
import sys
import re
import os
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import Optional
from datetime import datetime

# Import context manager
try:
    from codex_review_context import ReviewContext
    HAS_CONTEXT = True
except ImportError:
    HAS_CONTEXT = False


# ============================================================================
# Configuration
# ============================================================================

# Model and reasoning configuration
DEFAULT_MODEL = "gpt-5.2-codex"
DEFAULT_REASONING = "xhigh"

# Review prompts for different modes
PROMPTS = {
    "changes": """You are an expert code reviewer. Analyze the code changes thoroughly.

## Review Focus
1. **Security**: Vulnerabilities, injection risks, auth issues, secrets exposure
2. **Bugs**: Logic errors, edge cases, null handling, race conditions
3. **Performance**: Inefficient algorithms, memory leaks, unnecessary operations
4. **Testing**: Missing tests, coverage gaps, flaky patterns
5. **Architecture**: Design patterns, separation of concerns, coupling

## Output Format
For each issue:
- **Severity**: CRITICAL, HIGH, MEDIUM, LOW, or INFO
- **Category**: SECURITY, BUG, PERFORMANCE, TEST, STYLE, ARCHITECTURE
- **File**: Path and line number
- **Title**: Brief issue title
- **Description**: Detailed explanation
- **Fix**: Suggested code fix
- **Effort**: LOW, MEDIUM, or HIGH

Be thorough but avoid nitpicking trivial style issues.""",

    "files": """You are an expert code reviewer. Analyze the specified files thoroughly.

## Review Focus
1. **Code Quality**: Clean code principles, readability, maintainability
2. **Security**: Vulnerabilities, input validation, auth patterns
3. **Performance**: Bottlenecks, optimization opportunities
4. **Error Handling**: Exception handling, edge cases, error messages
5. **Best Practices**: Language idioms, framework patterns, modern approaches

## Output Format
For each issue:
- **Severity**: CRITICAL, HIGH, MEDIUM, LOW, or INFO
- **Category**: SECURITY, BUG, PERFORMANCE, QUALITY, STYLE
- **File**: Path and line number
- **Description**: What's wrong and why it matters
- **Recommendation**: Specific improvement with code example

Focus on actionable improvements that enhance code quality.""",

    "infrastructure": """You are a senior software architect. Analyze the project infrastructure.

## Analysis Areas
1. **Project Structure**: Directory organization, module boundaries, dependencies
2. **Testing Infrastructure**: Test framework, coverage, CI/CD integration
3. **Build System**: Build tools, bundling, optimization
4. **Configuration**: Environment handling, secrets management
5. **Development Workflow**: Scripts, automation, developer experience
6. **Documentation**: README, API docs, inline documentation

## Output Format
For each finding:
- **Area**: Infrastructure area affected
- **Current State**: What exists now
- **Issue/Gap**: What's missing or problematic
- **Recommendation**: Specific improvement
- **Priority**: HIGH, MEDIUM, LOW
- **Effort**: Estimated implementation effort

Provide actionable recommendations to improve project infrastructure.""",

    "codebase": """You are a principal engineer performing a comprehensive codebase review.

## Analysis Scope
1. **Architecture**: Overall design, patterns used, component relationships
2. **Code Quality**: Consistency, maintainability, technical debt
3. **Security Posture**: Authentication, authorization, data protection
4. **Performance**: Scalability, bottlenecks, resource usage
5. **Testing Strategy**: Coverage, test quality, testing patterns
6. **Error Handling**: Resilience, logging, monitoring readiness
7. **Dependencies**: Third-party libraries, version management

## Output Format
Provide a structured analysis:
1. **Executive Summary**: Overall assessment (1-2 paragraphs)
2. **Strengths**: What the codebase does well
3. **Critical Issues**: Must-fix problems (with severity)
4. **Improvement Areas**: Recommended enhancements
5. **Technical Debt**: Areas needing refactoring
6. **Action Items**: Prioritized list of recommendations

Be thorough and provide specific, actionable feedback.""",

    "security": """You are a security engineer performing a security audit.

## Security Analysis
1. **Authentication**: Login flows, session management, token handling
2. **Authorization**: Access controls, permission checks, RBAC
3. **Input Validation**: SQL injection, XSS, command injection
4. **Data Protection**: Encryption, sensitive data handling, PII
5. **API Security**: Rate limiting, CORS, request validation
6. **Dependencies**: Known vulnerabilities, outdated packages
7. **Secrets Management**: API keys, credentials, environment variables
8. **Error Handling**: Information disclosure, stack traces

## Output Format
For each vulnerability:
- **Severity**: CRITICAL, HIGH, MEDIUM, LOW
- **OWASP Category**: Relevant OWASP Top 10 category
- **Location**: File and line number
- **Description**: Vulnerability explanation
- **Exploitation**: How it could be exploited
- **Remediation**: Specific fix with code example
- **References**: Relevant security guidelines

Focus on real security issues, not theoretical concerns.""",

    "testing": """You are a QA architect reviewing the testing strategy.

## Testing Analysis
1. **Test Coverage**: What's tested, what's missing
2. **Test Quality**: Assertions, mocking patterns, test isolation
3. **Test Types**: Unit, integration, e2e distribution
4. **Test Reliability**: Flaky tests, race conditions, timing issues
5. **Test Maintainability**: DRY, fixtures, helpers
6. **CI/CD Integration**: Test automation, pipeline configuration
7. **Performance Testing**: Load tests, benchmarks

## Output Format
1. **Coverage Summary**: Areas with good/poor coverage
2. **Critical Gaps**: Must-add tests
3. **Quality Issues**: Tests that need improvement
4. **Recommendations**: Testing strategy improvements
5. **Priority Actions**: Ordered list of improvements

Provide specific test cases that should be added."""
}


@dataclass
class ReviewSuggestion:
    """A single review suggestion with structured data."""
    severity: str
    category: str
    file: str
    line: Optional[int]
    title: str
    description: str
    suggested_fix: Optional[str]
    effort: str


@dataclass
class ReviewReport:
    """Complete review report."""
    timestamp: str
    review_type: str
    review_mode: str
    model: str
    reasoning_effort: str
    files_reviewed: list[str]
    summary: str
    suggestions: list[dict]
    stats: dict
    raw_output: str = ""


# ============================================================================
# Codex Execution
# ============================================================================

def check_codex_auth() -> bool:
    """Check if codex is authenticated."""
    try:
        result = subprocess.run(
            ["codex", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except Exception:
        return False


def run_codex_exec(
    prompt: str,
    model: str = DEFAULT_MODEL,
    reasoning: str = DEFAULT_REASONING,
    files: Optional[list[str]] = None,
    timeout: int = 600,
) -> tuple[int, str, str]:
    """Run codex exec with specified prompt and configuration."""

    cmd = [
        "codex", "exec",
        "-m", model,
        "-c", f"model_reasoning_effort={reasoning}",
        "-s", "read-only",  # Safe sandbox for review
    ]

    # Build the full prompt
    full_prompt = prompt
    if files:
        files_list = "\n".join(f"- {f}" for f in files)
        full_prompt = f"{prompt}\n\n## Files to Review\n{files_list}"

    # Use stdin for prompt
    cmd.append("-")

    print(f"Running Codex with model={model}, reasoning={reasoning}...")

    try:
        result = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        # Check for auth errors
        if "token" in result.stderr.lower() and "refresh" in result.stderr.lower():
            return 1, "", "Authentication expired. Run 'codex login' to re-authenticate."

        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"Review timed out after {timeout} seconds"
    except FileNotFoundError:
        return 1, "", "Codex CLI not found. Install with: npm install -g @openai/codex"
    except Exception as e:
        return 1, "", str(e)


def run_codex_review(
    uncommitted: bool = False,
    base: Optional[str] = None,
    commit: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    reasoning: str = DEFAULT_REASONING,
    title: Optional[str] = None,
) -> tuple[int, str, str]:
    """Run codex review for git changes."""

    cmd = [
        "codex", "review",
        "-c", f"model={model}",
        "-c", f"model_reasoning_effort={reasoning}",
    ]

    if uncommitted:
        cmd.append("--uncommitted")
    elif base:
        cmd.extend(["--base", base])
    elif commit:
        cmd.extend(["--commit", commit])

    if title:
        cmd.extend(["--title", title])

    target = '--uncommitted' if uncommitted else f'--base {base}' if base else f'--commit {commit}' if commit else ''
    print(f"Running: codex review {target} (model={model}, reasoning={reasoning})...")

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )

        if "token" in result.stderr.lower() and "refresh" in result.stderr.lower():
            return 1, "", "Authentication expired. Run 'codex login' to re-authenticate."

        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "Review timed out after 10 minutes"
    except FileNotFoundError:
        return 1, "", "Codex CLI not found. Install with: npm install -g @openai/codex"
    except Exception as e:
        return 1, "", str(e)


# ============================================================================
# File Discovery
# ============================================================================

def get_changed_files(
    uncommitted: bool = False,
    base: Optional[str] = None,
    commit: Optional[str] = None
) -> list[str]:
    """Get list of changed files from git."""
    try:
        if uncommitted:
            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                capture_output=True, text=True
            ).stdout.strip().split("\n")

            unstaged = subprocess.run(
                ["git", "diff", "--name-only"],
                capture_output=True, text=True
            ).stdout.strip().split("\n")

            untracked = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                capture_output=True, text=True
            ).stdout.strip().split("\n")

            files = set(staged + unstaged + untracked)
            return [f for f in files if f]

        elif base:
            result = subprocess.run(
                ["git", "diff", "--name-only", base],
                capture_output=True, text=True
            )
            return [f for f in result.stdout.strip().split("\n") if f]

        elif commit:
            result = subprocess.run(
                ["git", "show", "--name-only", "--format=", commit],
                capture_output=True, text=True
            )
            return [f for f in result.stdout.strip().split("\n") if f]
    except Exception:
        pass

    return []


def expand_file_patterns(patterns: list[str]) -> list[str]:
    """Expand file patterns (globs) to actual file paths."""
    import glob

    files = []
    for pattern in patterns:
        if '*' in pattern or '?' in pattern:
            matches = glob.glob(pattern, recursive=True)
            files.extend(matches)
        elif os.path.isdir(pattern):
            # Recursively get files from directory
            for root, _, filenames in os.walk(pattern):
                for fname in filenames:
                    if not fname.startswith('.'):
                        files.append(os.path.join(root, fname))
        elif os.path.isfile(pattern):
            files.append(pattern)

    return sorted(set(files))


def get_codebase_files(area: Optional[str] = None) -> list[str]:
    """Get relevant files for codebase review."""
    files = []

    if area in (None, "backend"):
        backend_patterns = [
            "backend/*.py",
            "backend/services/*.py",
            "backend/providers/*.py",
            "backend/routing/*.py",
            "backend/models/*.py",
        ]
        for pattern in backend_patterns:
            files.extend(expand_file_patterns([pattern]))

    if area in (None, "frontend"):
        frontend_patterns = [
            "packages/frontend/src/**/*.ts",
            "packages/frontend/src/**/*.tsx",
        ]
        for pattern in frontend_patterns:
            files.extend(expand_file_patterns([pattern]))

    if area in (None, "infra"):
        infra_patterns = [
            "*.json",
            "*.toml",
            "*.yaml",
            "*.yml",
            ".github/**/*",
            "scripts/*.py",
        ]
        for pattern in infra_patterns:
            files.extend(expand_file_patterns([pattern]))

    # Filter out test files for non-testing reviews
    if area != "tests":
        files = [f for f in files if "test" not in f.lower() and "__pycache__" not in f]

    return sorted(set(files))[:100]  # Limit to 100 files


# ============================================================================
# Output Parsing
# ============================================================================

def parse_review_output(output: str) -> list[dict]:
    """Parse codex review output into structured suggestions."""
    suggestions = []

    severity_pattern = r'\*\*(CRITICAL|HIGH|MEDIUM|LOW|INFO)\*\*'
    category_pattern = r'\*\*(SECURITY|BUG|PERFORMANCE|TEST|STYLE|ARCHITECTURE|QUALITY)\*\*'

    sections = re.split(r'\n(?=\*\*(?:CRITICAL|HIGH|MEDIUM|LOW|INFO)\*\*|\d+\.\s+\*\*)', output)

    for section in sections:
        if not section.strip():
            continue

        suggestion = {
            "severity": "medium",
            "category": "improvement",
            "file": "",
            "line": None,
            "title": "",
            "description": section.strip(),
            "suggested_fix": None,
            "effort": "medium"
        }

        severity_match = re.search(severity_pattern, section, re.IGNORECASE)
        if severity_match:
            suggestion["severity"] = severity_match.group(1).lower()

        category_match = re.search(category_pattern, section, re.IGNORECASE)
        if category_match:
            suggestion["category"] = category_match.group(1).lower()

        file_match = re.search(r'(?:File|In|`)([\w./\-_]+\.\w+)`?', section)
        if file_match:
            suggestion["file"] = file_match.group(1)

        line_match = re.search(r'[Ll]ine\s*:?\s*(\d+)', section)
        if line_match:
            suggestion["line"] = int(line_match.group(1))

        title_match = re.search(r'\*\*([^*]+)\*\*', section)
        if title_match:
            suggestion["title"] = title_match.group(1).strip()
        else:
            first_line = section.strip().split('\n')[0]
            suggestion["title"] = first_line[:100]

        fix_match = re.search(r'```[\w]*\n(.*?)```', section, re.DOTALL)
        if fix_match:
            suggestion["suggested_fix"] = fix_match.group(1).strip()

        if suggestion["title"] or suggestion["description"]:
            suggestions.append(suggestion)

    return suggestions


def generate_report(
    review_type: str,
    review_mode: str,
    model: str,
    reasoning: str,
    files: list[str],
    output: str,
    suggestions: list[dict]
) -> ReviewReport:
    """Generate a structured review report."""

    severity_counts = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    category_counts = {}

    for s in suggestions:
        sev = s.get("severity", "medium").lower()
        if sev in severity_counts:
            severity_counts[sev] += 1

        cat = s.get("category", "other").lower()
        category_counts[cat] = category_counts.get(cat, 0) + 1

    total = len(suggestions)
    critical_high = severity_counts["critical"] + severity_counts["high"]

    if critical_high > 0:
        summary = f"Found {total} issues, {critical_high} require immediate attention."
    elif total > 0:
        summary = f"Found {total} suggestions for improvement."
    else:
        summary = "No significant issues found. Code looks good!"

    return ReviewReport(
        timestamp=datetime.now().isoformat(),
        review_type=review_type,
        review_mode=review_mode,
        model=model,
        reasoning_effort=reasoning,
        files_reviewed=files,
        summary=summary,
        suggestions=suggestions,
        stats={
            "total_issues": total,
            "by_severity": severity_counts,
            "by_category": category_counts,
        },
        raw_output=output,
    )


# ============================================================================
# Output Formatting
# ============================================================================

def print_report(report: ReviewReport, verbose: bool = False):
    """Print report to console."""
    print("\n" + "=" * 70)
    print("CODEX CODE REVIEW REPORT")
    print("=" * 70)
    print(f"\nTimestamp: {report.timestamp}")
    print(f"Mode: {report.review_mode}")
    print(f"Type: {report.review_type}")
    print(f"Model: {report.model} (reasoning: {report.reasoning_effort})")
    print(f"Files Reviewed: {len(report.files_reviewed)}")
    print(f"\nSummary: {report.summary}")

    print(f"\n--- Statistics ---")
    stats = report.stats
    print(f"Total Issues: {stats['total_issues']}")
    print(f"By Severity: {stats['by_severity']}")
    print(f"By Category: {stats['by_category']}")

    if report.suggestions:
        print(f"\n--- Suggestions ({len(report.suggestions)}) ---\n")

        severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        sorted_suggestions = sorted(
            report.suggestions,
            key=lambda x: severity_order.get(x.get("severity", "medium"), 2)
        )

        for i, s in enumerate(sorted_suggestions, 1):
            sev = s.get("severity", "medium").upper()
            cat = s.get("category", "").upper()
            title = s.get("title", "No title")
            file = s.get("file", "")
            line = s.get("line", "")

            loc = f"{file}:{line}" if file and line else file or ""

            print(f"{i}. [{sev}][{cat}] {title}")
            if loc:
                print(f"   Location: {loc}")

            if verbose:
                desc = s.get("description", "")
                if desc and len(desc) > 200:
                    desc = desc[:200] + "..."
                if desc:
                    print(f"   Description: {desc}")

                fix = s.get("suggested_fix", "")
                if fix:
                    print(f"   Fix: {fix[:100]}...")

            print()

    print("=" * 70)


def save_report(report: ReviewReport, output_path: Optional[str] = None) -> str:
    """Save report to JSON file."""
    if not output_path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = f"review_report_{timestamp}.json"

    with open(output_path, "w") as f:
        json.dump(asdict(report), f, indent=2)

    return output_path


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Versatile Codex Code Reviewer Agent",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Prerequisites:
    codex login                          # Authenticate (required once)

Review Modes:
    changes  - Review git changes (uncommitted, branch diff, commits)
    files    - Review specific files or directories
    infra    - Review project infrastructure
    codebase - Review entire codebase architecture
    security - Security-focused audit
    testing  - Testing strategy review

Examples:
    # Review uncommitted changes
    python scripts/codex_reviewer.py changes --uncommitted

    # Review against main branch
    python scripts/codex_reviewer.py changes --base main

    # Review specific files
    python scripts/codex_reviewer.py files backend/services/*.py

    # Review infrastructure
    python scripts/codex_reviewer.py infra --focus testing

    # Full codebase review
    python scripts/codex_reviewer.py codebase --area backend

    # Security audit
    python scripts/codex_reviewer.py security

    # Testing strategy review
    python scripts/codex_reviewer.py testing
        """
    )

    # Mode subparsers
    subparsers = parser.add_subparsers(dest="mode", help="Review mode")

    # Changes mode
    changes_parser = subparsers.add_parser("changes", help="Review git changes")
    changes_group = changes_parser.add_mutually_exclusive_group(required=True)
    changes_group.add_argument("--uncommitted", "-u", action="store_true",
                               help="Review uncommitted changes")
    changes_group.add_argument("--base", "-b", type=str, metavar="BRANCH",
                               help="Review changes against base branch")
    changes_group.add_argument("--commit", "-c", type=str, metavar="SHA",
                               help="Review specific commit")
    changes_parser.add_argument("--title", "-t", type=str,
                                help="Optional review title")

    # Files mode
    files_parser = subparsers.add_parser("files", help="Review specific files")
    files_parser.add_argument("paths", nargs="+", help="Files or directories to review")

    # Infrastructure mode
    infra_parser = subparsers.add_parser("infra", help="Review project infrastructure")
    infra_parser.add_argument("--focus", type=str,
                              choices=["testing", "build", "ci", "config", "docs", "all"],
                              default="all", help="Infrastructure focus area")

    # Codebase mode
    codebase_parser = subparsers.add_parser("codebase", help="Review entire codebase")
    codebase_parser.add_argument("--area", type=str,
                                 choices=["backend", "frontend", "all"],
                                 default="all", help="Codebase area to review")

    # Security mode
    security_parser = subparsers.add_parser("security", help="Security audit")
    security_parser.add_argument("--area", type=str,
                                 choices=["backend", "frontend", "all"],
                                 default="all", help="Area to audit")

    # Testing mode
    testing_parser = subparsers.add_parser("testing", help="Testing strategy review")
    testing_parser.add_argument("--area", type=str,
                                choices=["backend", "frontend", "all"],
                                default="all", help="Testing area to review")

    # Global options
    for p in [parser, changes_parser, files_parser, infra_parser,
              codebase_parser, security_parser, testing_parser]:
        p.add_argument("--model", "-m", type=str, default=DEFAULT_MODEL,
                       help=f"Model to use (default: {DEFAULT_MODEL})")
        p.add_argument("--reasoning", "-r", type=str, default=DEFAULT_REASONING,
                       choices=["low", "medium", "high", "xhigh"],
                       help=f"Reasoning effort (default: {DEFAULT_REASONING})")
        p.add_argument("--output", "-o", type=str, metavar="FILE",
                       help="Save report to JSON file")
        p.add_argument("--verbose", "-v", action="store_true",
                       help="Show detailed descriptions")
        p.add_argument("--json", action="store_true",
                       help="Output raw JSON")
        p.add_argument("--raw", action="store_true",
                       help="Include raw Codex output")
        p.add_argument("--context", action="store_true",
                       help="Include project context")

    args = parser.parse_args()

    if not args.mode:
        parser.print_help()
        sys.exit(1)

    # Check Codex availability
    if not check_codex_auth():
        print("Error: Codex CLI not available. Please install and authenticate:")
        print("  npm install -g @openai/codex")
        print("  codex login")
        sys.exit(1)

    # Initialize context if requested
    ctx = None
    if HAS_CONTEXT and getattr(args, 'context', False):
        print("Loading project context...")
        ctx = ReviewContext()

    # Execute based on mode
    files = []
    output = ""
    review_type = ""

    if args.mode == "changes":
        review_type = "git-changes"
        files = get_changed_files(
            uncommitted=args.uncommitted,
            base=args.base,
            commit=args.commit
        )

        if not files:
            print("No changes detected.")
            sys.exit(0)

        print(f"Found {len(files)} changed files")

        returncode, stdout, stderr = run_codex_review(
            uncommitted=args.uncommitted,
            base=args.base,
            commit=args.commit,
            model=args.model,
            reasoning=args.reasoning,
            title=getattr(args, 'title', None),
        )
        output = stdout

    elif args.mode == "files":
        review_type = "file-review"
        files = expand_file_patterns(args.paths)

        if not files:
            print("No files found matching patterns.")
            sys.exit(1)

        print(f"Reviewing {len(files)} files...")

        prompt = PROMPTS["files"]
        if ctx:
            prompt = ctx.build_review_prompt(files) + "\n\n" + prompt

        returncode, stdout, stderr = run_codex_exec(
            prompt=prompt,
            model=args.model,
            reasoning=args.reasoning,
            files=files,
        )
        output = stdout

    elif args.mode == "infra":
        review_type = f"infrastructure-{args.focus}"
        files = get_codebase_files(area="infra")

        print(f"Reviewing infrastructure ({args.focus})...")

        prompt = PROMPTS["infrastructure"]
        if args.focus != "all":
            prompt += f"\n\n**Focus Area**: {args.focus}"

        returncode, stdout, stderr = run_codex_exec(
            prompt=prompt,
            model=args.model,
            reasoning=args.reasoning,
            files=files,
        )
        output = stdout

    elif args.mode == "codebase":
        review_type = f"codebase-{args.area}"
        files = get_codebase_files(area=args.area if args.area != "all" else None)

        print(f"Reviewing codebase ({args.area}, {len(files)} files)...")

        prompt = PROMPTS["codebase"]
        if ctx:
            prompt = ctx.get_project_conventions() + "\n\n" + prompt

        returncode, stdout, stderr = run_codex_exec(
            prompt=prompt,
            model=args.model,
            reasoning=args.reasoning,
            files=files[:50],  # Limit for large codebases
            timeout=900,  # 15 min for full codebase
        )
        output = stdout

    elif args.mode == "security":
        review_type = f"security-audit-{args.area}"
        files = get_codebase_files(area=args.area if args.area != "all" else None)

        print(f"Running security audit ({args.area})...")

        returncode, stdout, stderr = run_codex_exec(
            prompt=PROMPTS["security"],
            model=args.model,
            reasoning=args.reasoning,
            files=files,
            timeout=900,
        )
        output = stdout

    elif args.mode == "testing":
        review_type = f"testing-review-{args.area}"
        # Include test files for testing review
        files = expand_file_patterns([
            "backend/tests/**/*.py",
            "packages/frontend/src/**/*.test.ts",
            "packages/frontend/src/**/*.test.tsx",
        ])

        print(f"Reviewing testing strategy ({len(files)} test files)...")

        returncode, stdout, stderr = run_codex_exec(
            prompt=PROMPTS["testing"],
            model=args.model,
            reasoning=args.reasoning,
            files=files,
        )
        output = stdout

    # Handle errors
    if returncode != 0:
        print(f"Error: {stderr}")
        sys.exit(1)

    # Parse and generate report
    suggestions = parse_review_output(output)
    report = generate_report(
        review_type=review_type,
        review_mode=args.mode,
        model=args.model,
        reasoning=args.reasoning,
        files=files,
        output=output,
        suggestions=suggestions,
    )

    if args.raw:
        report.raw_output = output

    # Save to context history
    if ctx:
        ctx.save_review(asdict(report))

    # Output
    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print_report(report, verbose=args.verbose)
        if args.raw:
            print("\n--- RAW OUTPUT ---")
            print(output)

    # Save if requested
    if args.output:
        saved_path = save_report(report, args.output)
        print(f"\nReport saved to: {saved_path}")

    # Exit code based on severity
    critical_high = report.stats["by_severity"]["critical"] + report.stats["by_severity"]["high"]
    sys.exit(1 if critical_high > 0 else 0)


if __name__ == "__main__":
    main()
