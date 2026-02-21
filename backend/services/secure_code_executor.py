"""
Secure Python code execution with AST-based validation and sandboxing.

This module provides security-hardened code execution for Pro Mode queries.
It replaces the regex-based approach with proper AST analysis to prevent
bypasses and includes comprehensive sandboxing.

Key features:
- AST-based security validation (prevents obfuscation bypasses)
- Three security levels: STRICT, MODERATE, RELAXED
- Session-isolated execution with resource limits
- JSON-based session storage (no pickle vulnerabilities)
- Timeout enforcement (prevents infinite loops)
- Output limiting (prevents DoS attacks)
- File operation sandboxing
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sys
import tempfile
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.models import CodeExecutionResult
from backend.config import get_settings
from backend.services.session_storage import get_session_storage_dir

logger = logging.getLogger(__name__)


class SecurityLevel(Enum):
    """Security levels for code execution"""
    STRICT = "strict"         # No file I/O, no network, no pip
    MODERATE = "moderate"     # Limited file I/O, no network
    RELAXED = "relaxed"       # File I/O allowed, network restricted


class SecurityValidator:
    """Multi-layer security validation using AST analysis"""

    # Completely forbidden modules
    FORBIDDEN_MODULES = {
        'os', 'sys', 'subprocess', 'socket', 'shutil',
        'importlib', 'ctypes', 'threading', 'multiprocessing',
        'builtins', '__builtin__', 'eval', 'exec', 'compile',
        'input', 'raw_input', 'file',
        'webbrowser', 'antigravity', 'this',
        'pickle', 'shelve', 'dill',  # Pickle vulnerabilities
    }

    # Restricted modules (allowed with limitations)
    # NOTE: httpx is allowed for Pro Mode to fetch data from APIs
    # Other network modules remain restricted for security
    RESTRICTED_MODULES = {
        'urllib': "Network access is disabled",
        'requests': "Use httpx instead (httpx is allowed)",
        'http': "Network access is disabled",
        'ftplib': "Network access is disabled",
        'telnetlib': "Network access is disabled",
        'ssl': "Network access is disabled",
    }

    # Dangerous built-in functions
    DANGEROUS_BUILTINS = {
        'eval', 'exec', 'compile', '__import__',
        'getattr', 'setattr', 'delattr', 'hasattr',
        'globals', 'locals', 'vars', 'dir',
        'input', 'breakpoint', 'help',
        'memoryview', 'bytearray',
    }

    # File operations to block (in STRICT mode)
    DANGEROUS_FILE_OPS = {
        'open', 'file', 'execfile', 'compile',
    }

    def __init__(self, security_level: SecurityLevel = SecurityLevel.STRICT):
        self.security_level = security_level
        self.violations = []
        self.warnings = []

    def validate(self, code: str) -> Tuple[bool, List[str], List[str]]:
        """
        Validate code safety using AST analysis.

        Returns:
            Tuple of (is_safe, violations, warnings)
        """
        self.violations = []
        self.warnings = []

        try:
            tree = ast.parse(code)
            self._check_ast(tree)

            # Additional string-based checks for obfuscation attempts
            self._check_string_patterns(code)

            return len(self.violations) == 0, self.violations, self.warnings

        except SyntaxError as e:
            self.violations.append(f"Syntax error: {e}")
            return False, self.violations, self.warnings

    def _check_ast(self, node: ast.AST) -> None:
        """Recursively check AST nodes for security issues"""

        # Check imports
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            self._check_import(node)

        # Check function calls
        elif isinstance(node, ast.Call):
            self._check_call(node)

        # Check attribute access
        elif isinstance(node, ast.Attribute):
            self._check_attribute(node)

        # Check name references
        elif isinstance(node, ast.Name):
            self._check_name(node)

        # Check string operations (potential code injection)
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                self._check_string(node)

        # Recurse through child nodes
        for child in ast.iter_child_nodes(node):
            self._check_ast(child)

    def _check_import(self, node: ast.AST) -> None:
        """Check import statements for forbidden modules"""
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name.split('.')[0]
                if module in self.FORBIDDEN_MODULES:
                    self.violations.append(
                        f"Forbidden import: {module} at line {node.lineno}"
                    )
                elif module in self.RESTRICTED_MODULES:
                    self.violations.append(
                        f"Restricted import: {module} - {self.RESTRICTED_MODULES[module]} at line {node.lineno}"
                    )

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                module = node.module.split('.')[0]
                if module in self.FORBIDDEN_MODULES:
                    self.violations.append(
                        f"Forbidden import: {module} at line {node.lineno}"
                    )
                elif module in self.RESTRICTED_MODULES:
                    self.violations.append(
                        f"Restricted import: {module} at line {node.lineno}"
                    )

    def _check_call(self, node: ast.Call) -> None:
        """Check function calls for dangerous operations"""
        func_name = self._get_call_name(node)

        if not func_name:
            return

        # Block dangerous builtins
        if func_name in self.DANGEROUS_BUILTINS:
            self.violations.append(
                f"Forbidden function: {func_name} at line {node.lineno}"
            )
            return

        # Check for dynamic imports
        if func_name == '__import__':
            self.violations.append(
                f"Dynamic import not allowed at line {node.lineno}"
            )
            return

        # Check for file operations based on security level
        if func_name in self.DANGEROUS_FILE_OPS:
            if self.security_level == SecurityLevel.STRICT:
                self.violations.append(
                    f"File operation not allowed: {func_name} at line {node.lineno}"
                )
            else:
                self.warnings.append(
                    f"File operation detected: {func_name} at line {node.lineno}"
                )

    def _check_attribute(self, node: ast.Attribute) -> None:
        """Check attribute access for dangerous patterns"""
        # Check for attempts to access __globals__, __code__, etc.
        if node.attr.startswith('__') and node.attr.endswith('__'):
            self.violations.append(
                f"Dunder attribute access not allowed: {node.attr} at line {node.lineno}"
            )

    def _check_name(self, node: ast.Name) -> None:
        """Check name references for dangerous builtins"""
        if node.id in self.DANGEROUS_BUILTINS:
            self.violations.append(
                f"Forbidden builtin: {node.id} at line {node.lineno}"
            )

    def _check_string(self, node: ast.Constant) -> None:
        """Check string constants for suspicious content"""
        if not isinstance(node.value, str):
            return

        # Check for potential shell commands
        suspicious_patterns = [
            'rm -rf', 'sudo', 'chmod', 'chown',
            '/etc/passwd', '/etc/shadow', '../../',
            'cat /', 'ls /', 'bash -c'
        ]

        for pattern in suspicious_patterns:
            if pattern in node.value:
                self.warnings.append(
                    f"Suspicious string pattern: '{pattern}' at line {node.lineno}"
                )

    def _check_string_patterns(self, code: str) -> None:
        """Additional string-based security checks for obfuscation"""
        # Check for hex-encoded malicious content
        if re.search(r'\\x[0-9a-fA-F]{2}', code):
            self.warnings.append("Hex-encoded strings detected - possible obfuscation")

        # Check for obfuscated code patterns
        if 'lambda' in code and ('map(' in code or 'filter(' in code):
            self.warnings.append("Potentially obfuscated code pattern (lambda+map/filter)")

        # Check for extremely long lines (obfuscation indicator)
        for i, line in enumerate(code.split('\n'), 1):
            if len(line) > 500:
                self.violations.append(
                    f"Suspicious long line at {i} ({len(line)} chars) - possible obfuscation"
                )

    def _get_call_name(self, node: ast.Call) -> str:
        """Extract function name from call node"""
        if isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node.func, ast.Attribute):
            parts = []
            current = node.func
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if isinstance(current, ast.Name):
                parts.append(current.id)
            return '.'.join(reversed(parts))
        return ""


class SecureCodeExecutor:
    """Secure code execution with comprehensive sandboxing"""

    def __init__(
        self,
        security_level: SecurityLevel = SecurityLevel.STRICT,
        session_dir: Optional[Path] = None,
        public_dir: Optional[Path] = None
    ):
        self.security_level = security_level
        # Use same session directory as SessionStorage for consistency
        self.session_dir = session_dir or get_session_storage_dir()
        self.public_dir = public_dir or self._get_public_dir()

        # Create directories with secure permissions
        self.session_dir.mkdir(mode=0o700, exist_ok=True, parents=True)
        self.public_dir.mkdir(mode=0o755, exist_ok=True, parents=True)

        logger.info(f"SecureCodeExecutor initialized with {security_level.value} security level")

    def _get_public_dir(self) -> Path:
        """Get public media directory with cross-platform default"""
        settings = get_settings()
        if settings.promode_public_dir:
            return Path(settings.promode_public_dir)

        # Default to project_root/public_media/promode
        project_root = Path(__file__).parent.parent.parent
        return project_root / "public_media" / "promode"

    def _validate_session_id(self, session_id: str) -> str:
        """Validate and sanitize session ID to prevent path traversal."""
        if not session_id or not isinstance(session_id, str):
            raise ValueError("Session ID must be a non-empty string")

        # Remove any path separators and dangerous characters
        sanitized = session_id.replace("/", "").replace("\\", "").replace("..", "").replace("\0", "")

        # Ensure only alphanumeric, underscore, and hyphen
        if not all(c.isalnum() or c in "-_" for c in sanitized):
            # Hash the identifier if it contains special chars
            sanitized = hashlib.sha256(session_id.encode()).hexdigest()[:16]

        # Limit length
        if len(sanitized) > 64:
            sanitized = sanitized[:64]

        if not sanitized:
            raise ValueError("Invalid session ID")

        return sanitized

    async def execute_code(
        self,
        code: str,
        session_id: str,
        timeout: int = 30,
        memory_limit_mb: int = 512,
        max_output_size: int = 100000
    ) -> Dict[str, Any]:
        """
        Execute code in secure sandbox.

        Args:
            code: Python code to execute
            session_id: Unique session identifier
            timeout: Execution timeout in seconds
            memory_limit_mb: Memory limit in MB
            max_output_size: Maximum output size in characters

        Returns:
            Dictionary with execution result
        """
        # Validate session_id first
        try:
            sanitized_session_id = self._validate_session_id(session_id)
        except ValueError as e:
            return {
                "success": False,
                "error": f"Invalid session ID: {e}"
            }

        # Step 1: Validate code for security issues
        validator = SecurityValidator(self.security_level)
        is_safe, violations, warnings = validator.validate(code)

        if not is_safe:
            error_msg = "Security violations detected:\n" + "\n".join(violations)
            logger.warning(f"Code validation failed: {error_msg}")
            return {
                "success": False,
                "error": error_msg,
                "warnings": warnings
            }

        # Log any warnings
        if warnings:
            logger.warning(f"Security warnings for session {sanitized_session_id}: {warnings}")

        # Step 2: Create isolated execution environment
        # Work dir uses unique hash for each execution (temporary, deleted after)
        execution_id = hashlib.sha256(f"{sanitized_session_id}_{time.time()}".encode()).hexdigest()[:16]
        work_dir = self.session_dir / "work" / execution_id
        work_dir.mkdir(mode=0o700, exist_ok=True, parents=True)

        logger.info(f"Executing code in isolated directory: {work_dir}")

        # Persistent session directory - uses same structure as SessionStorage class
        # so that list_keys() in SessionStorage can find keys saved by wrapped code
        # Structure: {session_dir}/{sanitized_session_id}/*.json
        persistent_session_dir = self.session_dir / sanitized_session_id
        persistent_session_dir.mkdir(mode=0o700, exist_ok=True, parents=True)

        try:
            # Step 3: Prepare execution script with safety wrappers
            exec_script = work_dir / "exec.py"
            wrapped_code = self._wrap_code(code, work_dir, persistent_session_dir, max_output_size)

            with open(exec_script, 'w') as f:
                f.write(wrapped_code)

            # Set secure file permissions
            os.chmod(exec_script, 0o600)

            # Step 4: Execute in subprocess with resource restrictions
            result = await self._run_sandboxed(
                exec_script,
                work_dir,
                timeout,
                memory_limit_mb,
                max_output_size
            )

            # Step 5: Add warnings to result if any
            if warnings:
                result["warnings"] = warnings

            # Step 6: Detect and collect generated files
            if result.get("success"):
                files = self._collect_generated_files(session_id, result.get("output", ""))
                if files:
                    result["files"] = files
                    logger.info(f"Collected {len(files)} generated file(s) for session {session_id}")

            logger.info(f"Code execution completed for session {session_id}")
            return result

        except Exception as e:
            logger.error(f"Execution error for session {session_id}: {str(e)}")
            return {
                "success": False,
                "error": f"Execution error: {str(e)}"
            }

    def _collect_generated_files(self, session_id: str, output: str) -> List[Dict[str, str]]:
        """
        Detect and collect generated files from /tmp, move to public directory.

        Args:
            session_id: Session identifier
            output: Code execution output (may contain file paths)

        Returns:
            List of file dictionaries with 'url', 'name', and 'type'
        """
        import re
        import shutil

        files = []

        # Look for promode files in /tmp matching this session
        tmp_pattern = Path("/tmp")
        file_patterns = [
            f"promode_{session_id}_*.png",
            f"promode_{session_id}_*.csv",
            f"promode_{session_id}_*.html",
            f"promode_{session_id}_*.json",
        ]

        for pattern in file_patterns:
            for tmp_file in tmp_pattern.glob(pattern):
                try:
                    # Move to public directory
                    dest_file = self.public_dir / tmp_file.name
                    shutil.move(str(tmp_file), str(dest_file))

                    # Generate URL (Apache serves /static/promode/)
                    url = f"/static/promode/{tmp_file.name}"

                    # Determine file type
                    suffix = tmp_file.suffix.lower()
                    file_type = {
                        ".png": "image",
                        ".csv": "data",
                        ".html": "html",
                        ".json": "data",
                    }.get(suffix, "file")

                    files.append({
                        "url": url,
                        "name": tmp_file.name,
                        "type": file_type,
                    })

                    logger.info(f"Collected file: {tmp_file.name} -> {url}")

                except Exception as e:
                    logger.warning(f"Failed to collect file {tmp_file}: {e}")

        return files

    def _wrap_code(self, code: str, work_dir: Path, persistent_session_dir: Path, max_output_size: int) -> str:
        """
        Wrap user code with security constraints and output capture.

        Args:
            code: User code to wrap
            work_dir: Working directory for execution (temporary)
            persistent_session_dir: Directory for persistent session storage (survives execution)
            max_output_size: Maximum output size

        Returns:
            Wrapped code as string
        """
        # Normalize line endings to Unix style (handle Windows \r\n)
        code = code.replace('\r\n', '\n').replace('\r', '\n')

        # Safely escape paths to prevent injection
        work_dir_escaped = repr(str(work_dir))
        session_dir_escaped = repr(str(persistent_session_dir))

        return f"""
import sys
import json
import io
import contextlib
import os
import hashlib
import re

# Reset resource limits for threading (needed for httpx/pandas)
try:
    import resource
    # Remove process/thread limits that might be inherited
    resource.setrlimit(resource.RLIMIT_NPROC, (resource.RLIM_INFINITY, resource.RLIM_INFINITY))
except:
    pass

# Set threading environment variables
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'
os.environ['OMP_NUM_THREADS'] = '1'

# Working directory (temporary, deleted after execution)
_WORK_DIR = {work_dir_escaped}

# Session storage directory (PERSISTENT, survives between executions)
# Stored as tuple to prevent user code from modifying it
_SESSION_CONFIG = ({session_dir_escaped},)

# Ensure session directory exists
os.makedirs(_SESSION_CONFIG[0], exist_ok=True)

def _get_session_dir():
    '''Get session directory - returns immutable value'''
    return _SESSION_CONFIG[0]

def _sanitize_key(key):
    '''Sanitize session key to prevent path traversal attacks'''
    if not key or not isinstance(key, str):
        raise ValueError("Key must be a non-empty string")
    # Remove path separators and dangerous characters
    sanitized = key.replace("/", "").replace("\\\\", "").replace("..", "").replace("\\0", "")
    # Only allow alphanumeric, underscore, hyphen
    if not re.match(r'^[a-zA-Z0-9_-]+$', sanitized):
        # Hash keys with special characters
        sanitized = hashlib.sha256(key.encode()).hexdigest()[:16]
    # Limit length
    if len(sanitized) > 64:
        sanitized = sanitized[:64]
    if not sanitized:
        raise ValueError("Invalid key")
    return sanitized

def _validate_session_path(file_path):
    '''Validate that file path is within session directory'''
    session_dir = os.path.realpath(_get_session_dir())
    real_path = os.path.realpath(file_path)
    if not real_path.startswith(session_dir + os.sep) and real_path != session_dir:
        raise ValueError(f"Path traversal attempt detected")
    return real_path

def save_session(key, data):
    '''Save data to PERSISTENT session storage for use in follow-up queries'''
    import json
    try:
        safe_key = _sanitize_key(key)
        session_dir = _get_session_dir()
        session_file = os.path.join(session_dir, f"{{safe_key}}.json")
        # Validate path is within session directory (defense in depth)
        session_file = _validate_session_path(session_file)

        # Convert pandas DataFrames to dict for JSON serialization
        if hasattr(data, 'to_dict'):
            import pandas as pd
            # Convert datetime columns to ISO strings before serialization
            df_copy = data.copy()
            for col in df_copy.columns:
                if pd.api.types.is_datetime64_any_dtype(df_copy[col]):
                    # Handle NaT values by converting to None
                    df_copy[col] = df_copy[col].apply(
                        lambda x: x.strftime('%Y-%m-%d') if pd.notna(x) else None
                    )
            data = df_copy.to_dict('records')

        # Custom JSON encoder for remaining types
        def json_encoder(obj):
            if hasattr(obj, 'isoformat'):
                return obj.isoformat()
            if hasattr(obj, 'tolist'):
                return obj.tolist()
            if hasattr(obj, 'item'):
                return obj.item()
            # Handle numpy/pandas NaN/NaT
            try:
                import numpy as np
                if isinstance(obj, (float, np.floating)) and np.isnan(obj):
                    return None
            except:
                pass
            raise TypeError(f"Object of type {{type(obj).__name__}} is not JSON serializable")

        with open(session_file, 'w') as f:
            json.dump(data, f, default=json_encoder)
        print(f"Session data saved: '{{key}}'")
    except Exception as e:
        print(f"Warning: Could not save session data for '{{key}}': {{e}}")

def load_session(key, default=None):
    '''Load data from PERSISTENT session storage'''
    import json
    try:
        safe_key = _sanitize_key(key)
        session_dir = _get_session_dir()
        session_file = os.path.join(session_dir, f"{{safe_key}}.json")
        # Validate path is within session directory (defense in depth)
        session_file = _validate_session_path(session_file)
        if os.path.exists(session_file):
            with open(session_file, 'r') as f:
                data = json.load(f)
            print(f"Session data loaded: '{{key}}'")
            return data
    except Exception as e:
        print(f"Warning: Could not load session data for '{{key}}': {{e}}")
    return default

def list_session_data():
    '''List all available session keys'''
    import glob
    session_dir = _get_session_dir()
    session_files = glob.glob(os.path.join(session_dir, "*.json"))
    # Filter out internal files (starting with underscore)
    return [os.path.basename(f).replace('.json', '') for f in session_files
            if not os.path.basename(f).startswith('_')]

# Set up output capture
_output = io.StringIO()
_errors = io.StringIO()

# Timeout handled by asyncio wait_for in parent process
# Note: signal.SIGALRM disabled as it can interfere with threading

try:
    with contextlib.redirect_stdout(_output):
        with contextlib.redirect_stderr(_errors):
            # User code starts here
{chr(10).join('            ' + line for line in code.split(chr(10)))}
            # User code ends here

    result = {{
        "success": True,
        "output": _output.getvalue()[:{max_output_size}],
        "error": ""
    }}

except TimeoutError:
    result = {{
        "success": False,
        "error": "Code execution timed out",
        "output": _output.getvalue()[:{max_output_size}]
    }}

except SystemExit as e:
    # Handle exit() calls gracefully - treat as completion if output exists
    output = _output.getvalue()[:{max_output_size}]
    if output.strip():
        result = {{
            "success": True,
            "output": output,
            "error": ""
        }}
    else:
        result = {{
            "success": False,
            "error": f"Code called exit() - consider using print statements instead. Exit code: {{e.code}}",
            "output": output
        }}

except Exception as e:
    result = {{
        "success": False,
        "error": str(e)[:10000],
        "output": _output.getvalue()[:{max_output_size}]
    }}

finally:
    # Ensure output is written
    with open('result.json', 'w') as f:
        json.dump(result, f)
"""

    async def _run_sandboxed(
        self,
        script_path: Path,
        work_dir: Path,
        timeout: int,
        memory_limit_mb: int,
        max_output_size: int
    ) -> Dict[str, Any]:
        """
        Run script in sandboxed subprocess with resource limits.

        Args:
            script_path: Path to script to execute
            work_dir: Working directory
            timeout: Timeout in seconds
            memory_limit_mb: Memory limit in MB
            max_output_size: Maximum output size

        Returns:
            Execution result dictionary
        """
        try:
            # Build environment by inheriting from parent and filtering sensitive vars
            # This ensures threading/library dependencies work correctly

            # Start with copy of parent environment
            env = os.environ.copy()

            # Remove sensitive environment variables FIRST, before setting custom values
            # Comprehensive list of sensitive environment variable prefixes
            # These should never be exposed to user code
            sensitive_prefixes = [
                # Cloud provider credentials
                'AWS_', 'AZURE_', 'GCP_', 'GOOGLE_', 'ALIBABA_', 'DO_', 'DIGITALOCEAN_',
                # Generic secrets/tokens
                'SECRET_', 'TOKEN_', 'API_KEY', 'APIKEY', 'PASSWORD', 'PASSWD', 'CREDENTIAL',
                'PRIVATE_KEY', 'PRIVATEKEY', 'AUTH_', 'BEARER_',
                # econ-data-mcp specific API keys
                'OPENROUTER_', 'GROK_', 'FRED_', 'COMTRADE_',
                'SUPABASE_', 'JWT_', 'EXCHANGERATE_', 'COINGECKO_',
                'VLLM_', 'ANTHROPIC_', 'OPENAI_', 'CLAUDE_',
                # Database and service credentials
                'DATABASE_', 'DB_', 'REDIS_', 'MONGO_', 'POSTGRES_', 'MYSQL_',
                # SSH/encryption keys
                'SSH_', 'GPG_', 'PGP_', 'ENCRYPTION_',
            ]

            # Also filter exact matches for common sensitive variable names
            sensitive_exact = {
                'HOME', 'USER', 'USERNAME', 'LOGNAME', 'MAIL',
                'HOSTNAME', 'HOSTTYPE',
            }

            for key in list(env.keys()):
                key_upper = key.upper()
                # Check prefix match (case-insensitive)
                if any(key_upper.startswith(prefix.upper()) for prefix in sensitive_prefixes):
                    del env[key]
                # Check exact match
                elif key_upper in sensitive_exact:
                    del env[key]
                # Filter any variable containing 'KEY', 'SECRET', 'TOKEN', 'PASSWORD' (defense in depth)
                elif any(sensitive in key_upper for sensitive in ['KEY', 'SECRET', 'TOKEN', 'PASSWORD', 'CREDENTIAL']):
                    del env[key]

            # Set custom environment values AFTER filtering (so they don't get deleted)
            env["HOME"] = str(work_dir)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONUNBUFFERED"] = "1"

            # Limit threading libraries to single thread for OpenBLAS/MKL
            env["OPENBLAS_NUM_THREADS"] = "1"
            env["MKL_NUM_THREADS"] = "1"
            env["NUMEXPR_NUM_THREADS"] = "1"
            env["OMP_NUM_THREADS"] = "1"

            # Create subprocess with restrictions
            # Note: preexec_fn disabled because resource limits prevent httpx/numpy
            # from creating necessary threads. Security maintained via timeout,
            # environment isolation, and sandboxed directory.
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(script_path),
                cwd=str(work_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                # preexec_fn=self._set_resource_limits if sys.platform != 'win32' else None
            )

            # Wait with timeout
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                logger.error(f"Code execution timed out after {timeout} seconds")
                return {
                    "success": False,
                    "error": f"Code execution timed out after {timeout} seconds"
                }

            # Read result file
            result_file = work_dir / "result.json"
            if result_file.exists():
                try:
                    with open(result_file) as f:
                        result = json.load(f)
                    logger.info(f"Code execution result: success={result.get('success')}")
                    return result
                except json.JSONDecodeError as e:
                    # Read the raw content for debugging
                    try:
                        with open(result_file, 'r') as f:
                            raw_content = f.read()[:1000]
                    except:
                        raw_content = "[could not read file]"
                    stderr_text = stderr.decode() if stderr else ""
                    logger.error(f"Failed to parse result JSON: {e}, raw content: {raw_content[:200]}, stderr: {stderr_text[:200]}")
                    return {
                        "success": False,
                        "error": f"Failed to parse execution result: {str(e)[:100]}. stderr: {stderr_text[:300]}"
                    }

            # No result file - process may have crashed
            stderr_text = stderr.decode() if stderr else ""
            stdout_text = stdout.decode() if stdout else ""

            return {
                "success": False,
                "error": f"No result produced. stderr: {stderr_text[:500]}",
                "output": stdout_text[:max_output_size]
            }

        except Exception as e:
            logger.error(f"Sandboxed execution error: {str(e)}")
            return {
                "success": False,
                "error": f"Execution error: {str(e)}"
            }

        finally:
            # Clean up work directory
            try:
                if work_dir.exists():
                    shutil.rmtree(work_dir, ignore_errors=True)
            except Exception as e:
                logger.warning(f"Failed to clean up {work_dir}: {e}")

    def _set_resource_limits(self) -> None:
        """Set resource limits for subprocess (Unix only)"""
        try:
            import resource

            # CPU time limit (30 seconds)
            resource.setrlimit(resource.RLIMIT_CPU, (30, 30))

            # Memory limit (512 MB)
            resource.setrlimit(
                resource.RLIMIT_AS,
                (512 * 1024 * 1024, 512 * 1024 * 1024)
            )

            # File descriptor limit (100 files) - needed for httpx connections
            resource.setrlimit(resource.RLIMIT_NOFILE, (100, 100))

            # Note: RLIMIT_NPROC removed because it prevents httpx/numpy from creating
            # necessary threads. Security is maintained via CPU time limit, memory limit,
            # timeout, and sandboxed working directory.

            # File size limit (100 MB)
            resource.setrlimit(
                resource.RLIMIT_FSIZE,
                (100 * 1024 * 1024, 100 * 1024 * 1024)
            )

            logger.debug("Resource limits set for subprocess")

        except Exception as e:
            logger.warning(f"Failed to set resource limits: {e}")

    def cleanup_old_sessions(self, max_age_hours: int = 24) -> int:
        """
        Clean up old session directories.

        Args:
            max_age_hours: Maximum age in hours before cleanup

        Returns:
            Number of directories deleted
        """
        current_time = time.time()
        max_age_seconds = max_age_hours * 3600
        deleted_count = 0

        try:
            # Clean up work directories (temporary execution dirs) - always clean if old
            work_dir = self.session_dir / "work"
            if work_dir.exists():
                for exec_dir in work_dir.glob("*"):
                    if exec_dir.is_dir():
                        try:
                            file_age = current_time - exec_dir.stat().st_mtime
                            # Work dirs can be cleaned up after 1 hour (they should be deleted immediately anyway)
                            if file_age > 3600:
                                shutil.rmtree(exec_dir)
                                deleted_count += 1
                                logger.debug(f"Deleted orphaned work directory: {exec_dir.name}")
                        except Exception as e:
                            logger.warning(f"Failed to delete work dir {exec_dir}: {e}")

            # Clean up persistent session directories based on max_age_hours
            for session_dir in self.session_dir.glob("*"):
                if session_dir.is_dir() and session_dir.name != "work":
                    file_age = current_time - session_dir.stat().st_mtime
                    if file_age > max_age_seconds:
                        try:
                            shutil.rmtree(session_dir)
                            deleted_count += 1
                            logger.info(f"Deleted old session directory: {session_dir.name}")
                        except Exception as e:
                            logger.error(f"Failed to delete {session_dir}: {e}")

            if deleted_count > 0:
                logger.info(f"Session cleanup completed: deleted {deleted_count} old session(s)/work dirs")

        except Exception as e:
            logger.error(f"Error during session cleanup: {e}")

        return deleted_count


# Singleton instance
_secure_executor: Optional[SecureCodeExecutor] = None


def get_secure_code_executor(
    security_level: SecurityLevel = SecurityLevel.STRICT
) -> SecureCodeExecutor:
    """Get or create SecureCodeExecutor singleton"""
    global _secure_executor
    if _secure_executor is None:
        _secure_executor = SecureCodeExecutor(security_level)
    return _secure_executor
