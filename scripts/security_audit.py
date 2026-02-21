#!/usr/bin/env python3
"""
Security Audit Script for econ-data-mcp

Checks for common security issues including:
- Exposed credentials in version control
- Insecure configuration settings
- Missing security headers
- Unauthenticated endpoints

Run: python3 scripts/security_audit.py
"""

import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Tuple

# ANSI color codes
RED = "\033[91m"
YELLOW = "\033[93m"
GREEN = "\033[92m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"

# Patterns that indicate potential secrets (optimized for speed)
SECRET_PATTERNS = [
    (r'sk-or-[a-zA-Z0-9]{20,}', 'OpenRouter API Key'),
    (r'sk-[a-zA-Z0-9]{20,}', 'OpenAI/Generic API Key'),
    (r'xai-[a-zA-Z0-9]{20,}', 'Grok API Key'),
    (r'eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}', 'JWT Token'),
    (r'-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----', 'Private Key'),
    (r'password\s*=\s*["\'][^"\']{8,}["\']', 'Hardcoded Password'),
]

# Files/directories to skip
SKIP_PATTERNS = [
    '.git',
    '__pycache__',
    'node_modules',
    '.venv',
    'venv',
    '.env.example',
    '.env.template',
    '*.pyc',
    '*.log',
    'dist',
    'build',
]

class SecurityAuditor:
    def __init__(self, root_path: str):
        self.root_path = Path(root_path)
        self.issues: List[Dict] = []
        self.warnings: List[Dict] = []

    def should_skip(self, path: Path) -> bool:
        """Check if path should be skipped."""
        path_str = str(path)
        for pattern in SKIP_PATTERNS:
            if pattern in path_str:
                return True
        return False

    def check_env_in_git(self) -> None:
        """Check if .env file is tracked by git."""
        env_file = self.root_path / '.env'
        if env_file.exists():
            # Check if it's in .gitignore
            gitignore = self.root_path / '.gitignore'
            if gitignore.exists():
                content = gitignore.read_text()
                if '.env' not in content:
                    self.issues.append({
                        'severity': 'CRITICAL',
                        'file': '.env',
                        'issue': '.env file exists but is NOT in .gitignore',
                        'recommendation': 'Add .env to .gitignore and rotate all credentials'
                    })
            else:
                self.issues.append({
                    'severity': 'CRITICAL',
                    'file': '.gitignore',
                    'issue': 'No .gitignore file found',
                    'recommendation': 'Create .gitignore with .env excluded'
                })

    def check_git_history_for_secrets(self) -> None:
        """Check if secrets might be in git history."""
        import subprocess
        try:
            # Check if .env was ever committed
            result = subprocess.run(
                ['git', 'log', '--all', '--full-history', '--', '.env'],
                cwd=self.root_path,
                capture_output=True,
                text=True
            )
            if result.stdout.strip():
                self.issues.append({
                    'severity': 'CRITICAL',
                    'file': '.env',
                    'issue': '.env file appears in git history - credentials may be exposed',
                    'recommendation': 'Use git filter-branch or BFG to remove .env from history, then rotate ALL credentials'
                })
        except Exception:
            pass  # Git not available

    def scan_file_for_secrets(self, file_path: Path) -> None:
        """Scan a single file for potential secrets."""
        try:
            content = file_path.read_text(errors='ignore')
            relative_path = file_path.relative_to(self.root_path)
            relative_str = str(relative_path)

            # Skip test files - they may have test credentials
            if '/tests/' in relative_str or 'test_' in relative_str or '_test.' in relative_str:
                return

            for pattern, description in SECRET_PATTERNS:
                matches = re.finditer(pattern, content, re.IGNORECASE)
                for match in matches:
                    # Get line number
                    line_num = content[:match.start()].count('\n') + 1

                    # Skip if it's in a comment or documentation
                    line = content.split('\n')[line_num - 1]
                    if line.strip().startswith('#') or line.strip().startswith('//'):
                        continue
                    if 'example' in line.lower() or 'placeholder' in line.lower():
                        continue
                    if 'your-' in match.group() or 'YOUR_' in match.group():
                        continue
                    if 'test' in line.lower():
                        continue

                    self.warnings.append({
                        'severity': 'WARNING',
                        'file': str(relative_path),
                        'line': line_num,
                        'issue': f'Potential {description} detected',
                        'recommendation': f'Verify this is not a real secret: {match.group()[:20]}...'
                    })
        except Exception as e:
            pass  # Skip files that can't be read

    def check_insecure_endpoints(self) -> None:
        """Check for unauthenticated sensitive endpoints."""
        main_py = self.root_path / 'backend' / 'main.py'
        if main_py.exists():
            content = main_py.read_text()
            lines = content.split('\n')

            # Check for unauthenticated cache endpoints
            for i, line in enumerate(lines):
                if '/api/cache/clear' in line or '/api/cache/stats' in line:
                    # Look at the next 5 lines for the function definition
                    func_lines = '\n'.join(lines[i:i+5])
                    if 'get_required_user' not in func_lines and 'get_current_user' not in func_lines:
                        endpoint = '/api/cache/clear' if 'clear' in line else '/api/cache/stats'
                        self.issues.append({
                            'severity': 'HIGH',
                            'file': 'backend/main.py',
                            'issue': f'{endpoint} endpoint may be unauthenticated',
                            'recommendation': 'Add authentication: user: User = Depends(get_required_user)'
                        })

    def check_cors_config(self) -> None:
        """Check CORS configuration."""
        main_py = self.root_path / 'backend' / 'main.py'
        if main_py.exists():
            content = main_py.read_text()

            if 'allow_origins=["*"]' in content:
                self.issues.append({
                    'severity': 'HIGH',
                    'file': 'backend/main.py',
                    'issue': 'CORS allows all origins (*)',
                    'recommendation': 'Restrict CORS to specific allowed domains'
                })

    def check_debug_mode(self) -> None:
        """Check if debug mode is enabled in production settings."""
        config_py = self.root_path / 'backend' / 'config.py'
        if config_py.exists():
            content = config_py.read_text()

            if 'debug: bool = True' in content or 'DEBUG = True' in content:
                self.warnings.append({
                    'severity': 'MEDIUM',
                    'file': 'backend/config.py',
                    'issue': 'Debug mode may be enabled by default',
                    'recommendation': 'Ensure debug mode is disabled in production'
                })

    def scan_codebase(self) -> None:
        """Scan entire codebase for secrets (optimized for speed)."""
        extensions = {'.py', '.js', '.ts', '.tsx', '.yaml', '.yml', '.env', '.sh'}
        scanned_count = 0
        max_files = 200  # Safety limit for speed

        # Only scan key directories, skip node_modules entirely
        key_dirs = [
            self.root_path / 'backend',
            self.root_path / 'scripts',
            self.root_path / 'packages' / 'frontend' / 'src',
        ]

        # Scan root level files
        for file_path in self.root_path.iterdir():
            if file_path.is_file() and file_path.suffix in extensions:
                if not self.should_skip(file_path):
                    self.scan_file_for_secrets(file_path)
                    scanned_count += 1

        # Scan key directories
        for dir_path in key_dirs:
            if not dir_path.exists():
                continue
            for file_path in dir_path.rglob('*'):
                if scanned_count >= max_files:
                    return
                if self.should_skip(file_path):
                    continue
                if file_path.is_file() and file_path.suffix in extensions:
                    self.scan_file_for_secrets(file_path)
                    scanned_count += 1

    def run_audit(self) -> Tuple[int, int]:
        """Run full security audit."""
        print(f"\n{BOLD}{'='*60}{RESET}")
        print(f"{BOLD}           econ-data-mcp Security Audit{RESET}")
        print(f"{BOLD}{'='*60}{RESET}\n")

        print(f"{BLUE}[*]{RESET} Checking .env file tracking...")
        self.check_env_in_git()

        print(f"{BLUE}[*]{RESET} Checking git history for secrets...")
        self.check_git_history_for_secrets()

        print(f"{BLUE}[*]{RESET} Scanning codebase for potential secrets...")
        self.scan_codebase()

        print(f"{BLUE}[*]{RESET} Checking for insecure endpoints...")
        self.check_insecure_endpoints()

        print(f"{BLUE}[*]{RESET} Checking CORS configuration...")
        self.check_cors_config()

        print(f"{BLUE}[*]{RESET} Checking debug mode settings...")
        self.check_debug_mode()

        # Print results
        print(f"\n{BOLD}{'='*60}{RESET}")
        print(f"{BOLD}                    Results{RESET}")
        print(f"{BOLD}{'='*60}{RESET}\n")

        if self.issues:
            print(f"{RED}{BOLD}CRITICAL/HIGH Issues ({len(self.issues)}):{RESET}\n")
            for issue in self.issues:
                severity_color = RED if issue['severity'] == 'CRITICAL' else YELLOW
                print(f"  {severity_color}[{issue['severity']}]{RESET} {issue['file']}")
                print(f"    Issue: {issue['issue']}")
                print(f"    Fix: {issue['recommendation']}\n")
        else:
            print(f"{GREEN}No critical or high severity issues found.{RESET}\n")

        if self.warnings:
            print(f"{YELLOW}{BOLD}Warnings ({len(self.warnings)}):{RESET}\n")
            for warning in self.warnings[:10]:  # Limit to first 10
                print(f"  {YELLOW}[{warning['severity']}]{RESET} {warning['file']}:{warning.get('line', '?')}")
                print(f"    {warning['issue']}\n")
            if len(self.warnings) > 10:
                print(f"  ... and {len(self.warnings) - 10} more warnings\n")
        else:
            print(f"{GREEN}No warnings found.{RESET}\n")

        # Summary
        print(f"{BOLD}{'='*60}{RESET}")
        print(f"{BOLD}                   Summary{RESET}")
        print(f"{BOLD}{'='*60}{RESET}")
        print(f"  Critical/High Issues: {RED if self.issues else GREEN}{len(self.issues)}{RESET}")
        print(f"  Warnings: {YELLOW if self.warnings else GREEN}{len(self.warnings)}{RESET}")

        if self.issues:
            print(f"\n{RED}{BOLD}ACTION REQUIRED: Please address critical issues immediately!{RESET}")
            print(f"{RED}If API keys are exposed, rotate them NOW.{RESET}\n")

        return len(self.issues), len(self.warnings)


def main():
    """Main entry point."""
    # Find project root (where this script is in scripts/)
    script_path = Path(__file__).resolve()
    project_root = script_path.parent.parent

    if not (project_root / 'backend').exists():
        print(f"{RED}Error: Could not find backend directory. Run from project root.{RESET}")
        sys.exit(1)

    auditor = SecurityAuditor(project_root)
    issues, warnings = auditor.run_audit()

    # Exit with error code if critical issues found
    sys.exit(1 if issues > 0 else 0)


if __name__ == '__main__':
    main()
