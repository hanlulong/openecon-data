#!/usr/bin/env python3
"""
Context-Aware Codex Code Review System

Maintains context about:
- Project conventions (from CLAUDE.md)
- Codebase structure
- Previous reviews
- Test patterns

This module provides context enrichment for code reviews.
"""

import json
import os
import subprocess
from pathlib import Path
from datetime import datetime
from typing import Optional


class ReviewContext:
    """Manages review context for the project."""

    def __init__(self, project_root: Optional[str] = None):
        self.project_root = Path(project_root or self._find_project_root())
        self.context_dir = self.project_root / ".codex_review"
        self.context_dir.mkdir(exist_ok=True)
        self.history_file = self.context_dir / "review_history.json"
        self.conventions_cache = self.context_dir / "conventions_cache.json"

    def _find_project_root(self) -> Path:
        """Find project root by looking for .git directory."""
        current = Path.cwd()
        while current != current.parent:
            if (current / ".git").exists():
                return current
            current = current.parent
        return Path.cwd()

    def get_project_conventions(self) -> str:
        """Extract project conventions from CLAUDE.md and other config files."""
        conventions = []

        # Read CLAUDE.md
        claude_md = self.project_root / "CLAUDE.md"
        if claude_md.exists():
            content = claude_md.read_text()
            # Extract key sections
            conventions.append("## Project Conventions (from CLAUDE.md)\n")
            conventions.append(self._extract_key_sections(content))

        # Read TESTING_PROMPT.md if exists
        testing_md = self.project_root / "TESTING_PROMPT.md"
        if testing_md.exists():
            content = testing_md.read_text()[:2000]  # First 2000 chars
            conventions.append("\n## Testing Guidelines\n")
            conventions.append(content)

        # Read tsconfig.json for TypeScript settings
        tsconfig = self.project_root / "packages" / "frontend" / "tsconfig.json"
        if tsconfig.exists():
            try:
                config = json.loads(tsconfig.read_text())
                conventions.append("\n## TypeScript Configuration\n")
                conventions.append(f"- Strict mode: {config.get('compilerOptions', {}).get('strict', 'unknown')}")
            except Exception:
                pass

        # Read .eslintrc or eslint.config if exists
        eslint_files = [".eslintrc", ".eslintrc.js", ".eslintrc.json", "eslint.config.js"]
        for eslint_file in eslint_files:
            eslint_path = self.project_root / "packages" / "frontend" / eslint_file
            if eslint_path.exists():
                conventions.append(f"\n## ESLint: {eslint_file} exists")
                break

        return "\n".join(conventions)

    def _extract_key_sections(self, content: str) -> str:
        """Extract key sections from CLAUDE.md."""
        key_sections = []
        lines = content.split("\n")
        in_section = False
        section_lines = []

        important_headers = [
            "Critical Rules",
            "Testing Philosophy",
            "Best Practices",
            "Code Style",
            "Important",
        ]

        for line in lines:
            if line.startswith("#"):
                # Check if this is an important section
                if any(h.lower() in line.lower() for h in important_headers):
                    in_section = True
                    section_lines = [line]
                elif in_section:
                    # End of important section
                    key_sections.extend(section_lines)
                    in_section = False
                    section_lines = []
            elif in_section:
                section_lines.append(line)
                # Limit section size
                if len(section_lines) > 50:
                    section_lines.append("...[truncated]")
                    key_sections.extend(section_lines)
                    in_section = False
                    section_lines = []

        if section_lines:
            key_sections.extend(section_lines)

        return "\n".join(key_sections[:100])  # Limit total lines

    def get_related_files(self, changed_files: list[str]) -> dict[str, str]:
        """Get related files for context (tests, types, etc.)."""
        related = {}

        for file in changed_files:
            path = Path(file)

            # Find corresponding test file
            if "test" not in file.lower():
                test_patterns = [
                    path.parent / "__tests__" / f"{path.stem}.test{path.suffix}",
                    path.parent / f"{path.stem}.test{path.suffix}",
                    path.parent / f"{path.stem}.spec{path.suffix}",
                ]
                for test_path in test_patterns:
                    full_test = self.project_root / test_path
                    if full_test.exists():
                        related[str(test_path)] = "test"
                        break

            # Find type definitions for TS files
            if path.suffix in [".ts", ".tsx"]:
                types_path = path.parent / "types.ts"
                full_types = self.project_root / types_path
                if full_types.exists():
                    related[str(types_path)] = "types"

        return related

    def get_file_history(self, file_path: str, limit: int = 5) -> list[dict]:
        """Get recent git history for a file."""
        try:
            result = subprocess.run(
                ["git", "log", f"-{limit}", "--pretty=format:%H|%s|%an|%ad", "--date=short", "--", file_path],
                capture_output=True,
                text=True,
                cwd=self.project_root
            )
            if result.returncode == 0:
                history = []
                for line in result.stdout.strip().split("\n"):
                    if line:
                        parts = line.split("|")
                        if len(parts) >= 4:
                            history.append({
                                "sha": parts[0][:8],
                                "message": parts[1],
                                "author": parts[2],
                                "date": parts[3]
                            })
                return history
        except Exception:
            pass
        return []

    def load_review_history(self) -> list[dict]:
        """Load previous review history."""
        if self.history_file.exists():
            try:
                return json.loads(self.history_file.read_text())
            except Exception:
                pass
        return []

    def save_review(self, report: dict):
        """Save a review to history."""
        history = self.load_review_history()
        history.append({
            "timestamp": datetime.now().isoformat(),
            "summary": report.get("summary", ""),
            "files": report.get("files_reviewed", []),
            "issues_count": report.get("stats", {}).get("total_issues", 0),
        })
        # Keep last 50 reviews
        history = history[-50:]
        self.history_file.write_text(json.dumps(history, indent=2))

    def get_codebase_structure(self) -> str:
        """Get high-level codebase structure."""
        structure = []

        # Backend structure
        backend = self.project_root / "backend"
        if backend.exists():
            structure.append("## Backend (Python/FastAPI)")
            for item in ["main.py", "models.py", "services", "providers", "routing"]:
                path = backend / item
                if path.exists():
                    structure.append(f"  - {item}")

        # Frontend structure
        frontend = self.project_root / "packages" / "frontend" / "src"
        if frontend.exists():
            structure.append("\n## Frontend (React/TypeScript)")
            for item in ["components", "services", "contexts", "hooks", "lib"]:
                path = frontend / item
                if path.exists():
                    structure.append(f"  - src/{item}")

        return "\n".join(structure)

    def build_review_prompt(
        self,
        changed_files: list[str],
        review_focus: Optional[str] = None
    ) -> str:
        """Build a comprehensive review prompt with context."""
        prompt_parts = []

        # Add project conventions
        conventions = self.get_project_conventions()
        if conventions:
            prompt_parts.append("# PROJECT CONTEXT\n")
            prompt_parts.append(conventions)

        # Add codebase structure
        structure = self.get_codebase_structure()
        if structure:
            prompt_parts.append("\n# CODEBASE STRUCTURE\n")
            prompt_parts.append(structure)

        # Add recent review patterns
        history = self.load_review_history()
        if history:
            recent = history[-3:]  # Last 3 reviews
            prompt_parts.append("\n# RECENT REVIEWS\n")
            for r in recent:
                prompt_parts.append(f"- {r['timestamp'][:10]}: {r['issues_count']} issues in {len(r['files'])} files")

        # Add specific review instructions
        prompt_parts.append("\n# REVIEW INSTRUCTIONS\n")
        prompt_parts.append("""
Review the code changes with focus on:
1. **Consistency**: Does the code follow project conventions?
2. **Testing**: Are changes properly tested? Do tests follow patterns?
3. **Security**: Any security concerns?
4. **Performance**: Any performance issues?
5. **Maintainability**: Is the code clean and maintainable?

For each issue, provide:
- Severity: CRITICAL, HIGH, MEDIUM, LOW, INFO
- Category: SECURITY, BUG, PERFORMANCE, TEST, STYLE, ARCHITECTURE
- File and line number
- Clear description
- Suggested fix (with code if applicable)
- Estimated effort: LOW, MEDIUM, HIGH
""")

        if review_focus:
            prompt_parts.append(f"\n**Special Focus**: {review_focus}")

        return "\n".join(prompt_parts)


def main():
    """Test context generation."""
    ctx = ReviewContext()
    print("Project Root:", ctx.project_root)
    print("\n" + "=" * 50)
    print("PROJECT CONVENTIONS:")
    print("=" * 50)
    print(ctx.get_project_conventions()[:1000])
    print("\n" + "=" * 50)
    print("CODEBASE STRUCTURE:")
    print("=" * 50)
    print(ctx.get_codebase_structure())


if __name__ == "__main__":
    main()
