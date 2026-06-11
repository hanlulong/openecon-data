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
import sys
import time
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
        'subprocess', 'socket', 'shutil',
        'importlib', 'ctypes', 'threading', 'multiprocessing',
        'builtins', '__builtin__', 'eval', 'exec', 'compile',
        'input', 'raw_input', 'file',
        'webbrowser', 'antigravity', 'this',
        'pickle', 'shelve', 'dill',  # Pickle vulnerabilities
        'inspect', 'types', 'gc', '__main__', 'pdb',  # Reflection/debugging escape vectors
    }

    # Modules allowed because the wrapper already provides them,
    # but with dangerous attribute access blocked (see DANGEROUS_OS_ATTRS).
    # 'os' and 'sys' are pre-imported by the execution wrapper for session
    # management, path operations, and environment setup. Blocking them
    # entirely causes "Forbidden import: os" errors when the LLM generates
    # code that redundantly imports them.
    WRAPPER_PROVIDED_MODULES = {'os', 'sys'}

    # Dangerous os attributes - these are blocked even though os is allowed.
    # This provides defense-in-depth: os.path.join is fine, os.system is not.
    DANGEROUS_OS_ATTRS = {
        'system', 'popen', 'exec', 'execl', 'execle', 'execlp', 'execlpe',
        'execv', 'execve', 'execvp', 'execvpe', 'spawn', 'spawnl', 'spawnle',
        'spawnlp', 'spawnlpe', 'spawnv', 'spawnve', 'spawnvp', 'spawnvpe',
        'kill', 'killpg', 'fork', 'forkpty', 'remove', 'unlink', 'rmdir',
        'removedirs', 'rename', 'renames', 'replace', 'link', 'symlink',
        'chown', 'chmod', 'chroot', 'lchmod', 'lchown', 'setuid', 'setgid',
        'seteuid', 'setegid', 'setreuid', 'setregid',
    }

    # Dangerous sys attributes
    DANGEROUS_SYS_ATTRS = {
        'exit', 'modules', '_getframe', '_current_frames',
        'settrace', 'setprofile', 'setrecursionlimit',
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
                # os and sys are allowed (wrapper-provided) - import os is fine

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
                # For wrapper-provided modules, check if importing dangerous attributes
                # e.g., "from os import system" should be blocked, "from os import path" is ok
                elif module in self.WRAPPER_PROVIDED_MODULES and node.names:
                    dangerous_attrs = (
                        self.DANGEROUS_OS_ATTRS if module == 'os'
                        else self.DANGEROUS_SYS_ATTRS if module == 'sys'
                        else set()
                    )
                    for alias in node.names:
                        if alias.name in dangerous_attrs:
                            self.violations.append(
                                f"Forbidden {module} import: {module}.{alias.name} at line {node.lineno}"
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

        # Block dangerous os.* operations (os module is allowed for os.path, os.makedirs, etc.)
        if isinstance(node.value, ast.Name):
            if node.value.id == 'os' and node.attr in self.DANGEROUS_OS_ATTRS:
                self.violations.append(
                    f"Forbidden os operation: os.{node.attr} at line {node.lineno}"
                )
            elif node.value.id == 'sys' and node.attr in self.DANGEROUS_SYS_ATTRS:
                self.violations.append(
                    f"Forbidden sys operation: sys.{node.attr} at line {node.lineno}"
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

    # ----- Layer-1 namespace jail wiring (see backend/sandbox/promode_jail.sh) -----
    # Absolute, checked-in path to the static jail script. SECURITY INVARIANT: this
    # path is constant and the file is read-only; it is never derived from or written
    # by LLM output, so the LLM cannot influence the mount set.
    _SANDBOX_DIR = Path(__file__).resolve().parent.parent / "sandbox"
    JAIL_SCRIPT = _SANDBOX_DIR / "promode_jail.sh"
    MPL_CACHE_DIR = _SANDBOX_DIR / "mpl_cache"
    # The venv whose site-packages (numpy/pandas/matplotlib/httpx + numpy.libs) the
    # jail bind-mounts read-only. Derived from the running interpreter so it tracks
    # the actual deployment.
    VENV_DIR = Path(sys.prefix)
    VENV_PY = Path(sys.executable)
    UNSHARE = Path("/usr/bin/unshare")
    # Fixed mount points INSIDE the jail (must match promode_jail.sh).
    JAIL_SESS_PATH = "/sess"   # persistent session dir is bound here
    JAIL_TMP_PATH = "/tmp"     # per-exec output dir is bound here (plots land here)

    # Lazily-computed result of the boot canary self-test. None=not yet run,
    # True=jail healthy, False=jail broken (Pro Mode fails closed).
    _canary_ok: Optional[bool] = None

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

        # Under Layer 2 the jail runs as the 'promode' uid, which must read the
        # exec script and write into the scratch binds. Open these dirs to the
        # shared group (group rwx) so the cross-uid handoff works while still
        # excluding "other". Under Layer-1-only this is a no-op (stays 0o700).
        self._apply_sharing_perms(work_dir)

        logger.info(f"Executing code in isolated directory: {work_dir}")

        # Persistent session directory - uses same structure as SessionStorage class
        # so that list_keys() in SessionStorage can find keys saved by wrapped code
        # Structure: {session_dir}/{sanitized_session_id}/*.json
        persistent_session_dir = self.session_dir / sanitized_session_id
        persistent_session_dir.mkdir(mode=0o700, exist_ok=True, parents=True)

        # Per-execution OUTPUT directory. This is bound to the jail's /tmp, so any
        # file the generated code writes to /tmp/promode_*.png lands HERE (and only
        # here) rather than the world-writable host /tmp. SECURITY INVARIANT: file
        # collection globs THIS dir, not global /tmp, which closes the previous
        # TOCTOU / arbitrary-file-publish hole (predictable promode_{session}_* names
        # in shared /tmp could be planted/raced by any local user).
        tmp_out_dir = self.session_dir / "work" / f"{execution_id}_out"
        tmp_out_dir.mkdir(mode=0o700, exist_ok=True, parents=True)
        self._apply_sharing_perms(persistent_session_dir)
        self._apply_sharing_perms(tmp_out_dir)

        try:
            # Step 3: Prepare execution script with safety wrappers.
            # The session dir the wrapped code reads/writes is the jail-internal
            # path (/sess), since the host path does not exist inside the namespace.
            exec_script = work_dir / "exec.py"
            wrapped_code = self._wrap_code(
                code, work_dir, Path(self.JAIL_SESS_PATH), max_output_size
            )

            with open(exec_script, 'w') as f:
                f.write(wrapped_code)

            # Set secure file permissions. Group-readable only when Layer 2 is
            # active (so the promode uid can read the script); otherwise 0o600.
            os.chmod(exec_script, 0o640 if self._layer2_active() else 0o600)

            # Step 4: Execute in subprocess with resource restrictions
            result = await self._run_sandboxed(
                exec_script,
                work_dir,
                persistent_session_dir,
                tmp_out_dir,
                timeout,
                memory_limit_mb,
                max_output_size
            )

            # Step 5: Add warnings to result if any
            if warnings:
                result["warnings"] = warnings

            # Step 6: Detect and collect generated files from the PER-EXEC output
            # dir (not global /tmp).
            if result.get("success"):
                files = self._collect_generated_files(tmp_out_dir, session_id)
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
        finally:
            # Clean up the per-exec output dir. Files are already collected/moved by
            # this point; anything left is a stale/uncollected artifact.
            try:
                if tmp_out_dir.exists():
                    shutil.rmtree(tmp_out_dir, ignore_errors=True)
            except Exception as e:
                logger.warning(f"Failed to clean up output dir {tmp_out_dir}: {e}")

    def _collect_generated_files(self, tmp_out_dir: Path, session_id: str) -> List[Dict[str, str]]:
        """
        Detect and collect generated files from the PER-EXECUTION output dir.

        SECURITY INVARIANT: we glob ``tmp_out_dir`` (the host dir bound to the
        jail's /tmp), NOT the world-writable global /tmp. The generated code is
        told to write plots to /tmp/promode_*.<ext>, which — because /tmp inside
        the jail IS this dir outside — appears here and nowhere else. This removes
        the old TOCTOU / arbitrary-file-publish vector where a local attacker could
        pre-create predictable promode_{session}_* names in shared /tmp and have
        them published to the public web dir.

        Args:
            tmp_out_dir: Per-execution output directory (bound to jail /tmp)
            session_id: Session identifier (used only to namespace published names)

        Returns:
            List of file dictionaries with 'url', 'name', and 'type'
        """
        files = []

        if not tmp_out_dir.exists():
            return files

        # Collect any promode_* artifact written inside the isolated output dir.
        # The dir is already per-execution, so we do not need to match session_id
        # in the glob; we only accept the known publishable extensions.
        allowed_suffixes = {".png", ".csv", ".html", ".json"}

        for out_file in sorted(tmp_out_dir.glob("promode_*")):
            try:
                if not out_file.is_file():
                    continue
                suffix = out_file.suffix.lower()
                if suffix not in allowed_suffixes:
                    continue

                # Publish under a name that is unique across executions to avoid
                # collisions in the shared public dir (the per-exec dir name is a
                # 16-hex execution hash). Keep the leading 'promode_' so existing
                # /static/promode/ URL handling and cleanup globs still match.
                unique_name = f"promode_{tmp_out_dir.name}_{out_file.name[len('promode_'):]}"
                dest_file = self.public_dir / unique_name

                shutil.move(str(out_file), str(dest_file))
                # World-readable so Apache can serve it; not writable.
                try:
                    os.chmod(dest_file, 0o644)
                except OSError:
                    pass

                url = f"/static/promode/{unique_name}"
                file_type = {
                    ".png": "image",
                    ".csv": "data",
                    ".html": "html",
                    ".json": "data",
                }.get(suffix, "file")

                files.append({"url": url, "name": unique_name, "type": file_type})
                logger.info(f"Collected file: {out_file.name} -> {url}")

            except Exception as e:
                logger.warning(f"Failed to collect file {out_file}: {e}")

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

# NOTE: We intentionally do NOT touch RLIMIT_NPROC here.
# The parent sets resource limits via preexec_fn (RLIMIT_AS/CPU/FSIZE/NOFILE/CORE)
# but deliberately leaves RLIMIT_NPROC unset so OpenBLAS/httpx worker threads can
# still be created. The old code raised RLIMIT_NPROC to INFINITY to undo a limit
# that is no longer applied — removing it avoids a no-op that also failed silently
# inside the namespace jail.

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
        session_dir: Path,
        tmp_out_dir: Path,
        timeout: int,
        memory_limit_mb: int,
        max_output_size: int
    ) -> Dict[str, Any]:
        """
        Run script inside the Layer-1 namespace mount-isolation jail with
        RLIMIT resource caps.

        Args:
            script_path: Host path to the wrapped script (lives in work_dir)
            work_dir: Per-exec scratch dir (bound to jail /work, also HOME)
            session_dir: Persistent session dir (bound to jail /sess)
            tmp_out_dir: Per-exec output dir (bound to jail /tmp; plots land here)
            timeout: Timeout in seconds
            memory_limit_mb: Memory limit in MB (informational; RLIMIT_AS caps hard)
            max_output_size: Maximum output size

        Returns:
            Execution result dictionary
        """
        self._memory_limit_mb = memory_limit_mb
        self._cpu_timeout = timeout
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
                # OpenEcon Data specific API keys
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

            # Set custom environment values AFTER filtering (so they don't get deleted).
            # NOTE: the jail script re-exports HOME/MPLCONFIGDIR/*_NUM_THREADS for the
            # final python process; these here mostly affect the unshare/bash wrapper.
            env["HOME"] = str(work_dir)
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PYTHONUNBUFFERED"] = "1"

            # Limit threading libraries to single thread for OpenBLAS/MKL
            env["OPENBLAS_NUM_THREADS"] = "1"
            env["MKL_NUM_THREADS"] = "1"
            env["NUMEXPR_NUM_THREADS"] = "1"
            env["OMP_NUM_THREADS"] = "1"

            # ----------------------------------------------------------------
            # Build the jailed command.
            #
            # SECURITY INVARIANT: the LLM-generated code never runs directly on the
            # host. It runs as the python process inside:
            #   unshare --user --map-root-user --mount --pid --fork
            #     -> /bin/bash promode_jail.sh
            #        -> pivot_root into a tmpfs root with ONLY system runtime + venv
            #           + 3 scratch binds; real /home, /etc, repo, .env are absent.
            #
            #  --user --map-root-user : unprivileged user namespace; "root" inside is
            #                           our own uid, no real host privilege. This is
            #                           what lets us mount/pivot_root WITHOUT sudo.
            #  --mount                : private mount namespace (binds don't leak out).
            #  --pid --fork           : new PID namespace so the jail can mount a fresh
            #                           /proc that only shows jail processes.
            #
            # The script path passed to the jail is the JAIL-INTERNAL path
            # (/work/<name>), because work_dir is bound at /work inside the jail.
            # ----------------------------------------------------------------
            jail_script_path = f"/work/{script_path.name}"
            inner_cmd = [
                str(self.UNSHARE),
                "--user", "--map-root-user", "--mount", "--pid", "--fork",
                # --kill-child=SIGKILL: when the unshare process dies (e.g. our
                # timeout handler SIGKILLs the process group), it SIGKILLs the
                # forked jail init (PID 1 of the new pid namespace), which cascades
                # to every process inside. WITHOUT this, a `while True: pass` jail
                # gets orphaned (reparented to host init) and burns CPU forever,
                # because killing the unshare parent does NOT reach the forked
                # grandchild across the pid-namespace boundary. This is the control
                # that makes the wall-clock timeout actually terminate runaway code.
                "--kill-child=SIGKILL",
                "--", "/bin/bash", str(self.JAIL_SCRIPT),
                str(self.VENV_PY),     # $1 venv python to exec
                jail_script_path,      # $2 script (jail-internal path)
                str(work_dir),         # $3 -> /work
                str(session_dir),      # $4 -> /sess
                str(tmp_out_dir),      # $5 -> /tmp
                str(self.MPL_CACHE_DIR),  # $6 -> /mpl (prebaked fonts)
                str(self.VENV_DIR),    # $7 venv root to bind RO
                str(int(timeout)),     # $8 hard in-jail timeout (timeout(1) SIGKILL)
                str(self._jail_mem_bytes(memory_limit_mb)),  # $9 RLIMIT_AS (prlimit)
            ]

            # LAYER 2 (optional hardening): run the whole jail as the dedicated,
            # network-allowlisted 'promode' uid via passwordless `sudo -u promode`.
            # Enabled only when that user exists AND the scoped sudoers drop-in is in
            # place (provisioned by scripts/setup_promode_sandbox.sh). If absent we
            # fall back to Layer-1-only (still safe: mount isolation hides all secrets).
            argv = self._maybe_wrap_with_promode_uid(inner_cmd)

            # Re-enable preexec_fn: apply RLIMIT_AS/CPU/FSIZE/NOFILE/CORE in the child
            # before exec. This bounds memory/CPU/file-size DoS. We deliberately do NOT
            # set RLIMIT_NPROC (it strangles OpenBLAS/httpx threads — the original
            # reason the whole preexec_fn was disabled).
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(work_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                preexec_fn=self._set_resource_limits if sys.platform != 'win32' else None
            )

            # Wait with timeout. The AUTHORITATIVE wall-clock kill happens INSIDE
            # the jail via timeout(1) at `timeout` seconds (it runs as the jail uid
            # and can always SIGKILL python). We wait a bit longer here so the jail's
            # self-kill fires first and we still collect any partial output. The
            # external _kill_process_tree below is only a backstop for the rare case
            # the jail wedges before timeout(1) takes effect.
            outer_timeout = timeout + 8
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=outer_timeout
                )
            except asyncio.TimeoutError:
                # Backstop: the in-jail timeout(1) should have killed python already.
                # If we are still here, force-kill the whole tree (see method doc for
                # the Layer-2 cross-uid + pid-namespace subtleties).
                self._kill_process_tree(process)
                await process.wait()
                logger.error(f"Code execution timed out after {timeout} seconds")
                return {
                    "success": False,
                    "error": f"Code execution timed out after {timeout} seconds"
                }

            # Detect the in-jail hard timeout. `timeout(1)` (PID 1 of the jail) exits
            # 124 when the deadline is hit, or 137 (128+SIGKILL) when it had to
            # SIGKILL after the grace period. In either case python was killed before
            # it could write result.json, so surface a clear timeout error rather than
            # the generic "No result produced".
            if process.returncode in (124, 137) and not (work_dir / "result.json").exists():
                logger.error(f"Code execution timed out after {timeout} seconds (in-jail timeout)")
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
        """
        preexec_fn: set hard RLIMITs in the child before exec, and detach into a new
        session/process group so the timeout path can kill the entire jail tree.

        These limits are INHERITED by every descendant (unshare -> bash -> python),
        so they bound the jailed code even though python is several execs deep.

        IMPORTANT: we do NOT set RLIMIT_NPROC. Capping process/thread count strangles
        OpenBLAS and httpx worker-thread creation — that was the original reason the
        entire preexec_fn was disabled. Memory/CPU/file caps below give DoS protection
        without breaking the scientific stack.
        """
        try:
            import os as _os
            import resource

            # New session + process group. The timeout handler kills this whole group
            # (killpg) so the unshare-forked python child can't outlive the timeout.
            try:
                _os.setsid()
            except OSError:
                pass

            # Address-space (virtual memory) cap. Bounds runaway allocations like
            # bytearray(10**10) -> raises MemoryError instead of OOM-killing the box.
            # 1 GiB: numpy/OpenBLAS reserve large virtual ranges, so too-tight an AS
            # cap breaks legitimate sci code; 1 GiB is comfortably above real use.
            mem_bytes = max(int(getattr(self, "_memory_limit_mb", 512)), 768) * 1024 * 1024
            mem_bytes = max(mem_bytes, 1024 * 1024 * 1024)  # floor at 1 GiB
            resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))

            # CPU-seconds cap (SIGXCPU then SIGKILL). Bounds `while True: pass` even if
            # the wall-clock timeout path were to fail. Add margin over wall timeout
            # because wall != CPU time (I/O waits don't count as CPU).
            cpu_limit = int(getattr(self, "_cpu_timeout", 30)) + 5
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit + 2))

            # Max written file size: 100 MiB. Caps disk-fill DoS via huge plot/CSV.
            resource.setrlimit(
                resource.RLIMIT_FSIZE,
                (100 * 1024 * 1024, 100 * 1024 * 1024)
            )

            # Open file descriptors: 256. Enough for httpx connection pools + numpy
            # mmaps; bounds fd-exhaustion. (Was 100 — too tight once the bash wrapper
            # and TLS sockets are added.)
            resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))

            # No core dumps (could otherwise leak in-memory data to disk).
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

            logger.debug("Resource limits set for subprocess")

        except Exception as e:
            logger.warning(f"Failed to set resource limits: {e}")

    @staticmethod
    def _jail_mem_bytes(memory_limit_mb: int) -> int:
        """RLIMIT_AS in bytes for the in-jail prlimit. Floored at 1 GiB because
        numpy/OpenBLAS reserve large virtual ranges; too-tight an AS cap breaks
        legitimate scientific code. Mirrors the floor in _set_resource_limits."""
        mb = max(int(memory_limit_mb or 512), 768)
        return max(mb * 1024 * 1024, 1024 * 1024 * 1024)

    def _kill_process_tree(self, process) -> None:
        """
        Forcibly terminate the entire jail process tree on timeout.

        This is subtle and security-critical. Two layers of indirection sit between
        our child `process` and the runaway code:
          Layer 1:  process = unshare  -> (forked) jail python
          Layer 2:  process = sudo     -> unshare -> (forked) jail python
        and SUDO runs its child in a SEPARATE session/process group, so a simple
        killpg(getpgid(process.pid)) kills sudo but NOT the unshare beneath it,
        orphaning the namespaced python (which then burns CPU forever — observed).

        Robust strategy: walk the descendant tree via /proc PPIDs and SIGKILL every
        descendant (and the root). Killing the `unshare` process triggers its
        --kill-child=SIGKILL, which reaps the forked jail init (PID 1 of the pid ns)
        and thus the whole namespace. We also killpg as a belt-and-suspenders.
        """
        import os as _os
        import signal as _signal

        root = process.pid

        # Build PID -> PPID map from /proc, then collect all descendants of root.
        def _descendants(rootpid: int):
            children = {}
            try:
                for entry in _os.listdir("/proc"):
                    if not entry.isdigit():
                        continue
                    pid = int(entry)
                    try:
                        with open(f"/proc/{pid}/stat", "rb") as fh:
                            data = fh.read()
                        # field 4 (ppid) — parse robustly around the comm in parens.
                        rparen = data.rfind(b")")
                        fields = data[rparen + 2:].split()
                        ppid = int(fields[1])
                    except (OSError, ValueError, IndexError):
                        continue
                    children.setdefault(ppid, []).append(pid)
            except OSError:
                return []
            out, stack = [], [rootpid]
            while stack:
                cur = stack.pop()
                for ch in children.get(cur, []):
                    out.append(ch)
                    stack.append(ch)
            return out

        # Kill deepest-first: descendants then the root. Under Layer 2 this includes
        # the `unshare` process, whose death --kill-child uses to reap the namespace.
        try:
            for pid in reversed([root, *_descendants(root)]):
                try:
                    _os.kill(pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        except Exception:
            pass

        # Belt-and-suspenders: also nuke the process group of the root.
        try:
            _os.killpg(_os.getpgid(root), _signal.SIGKILL)
        except Exception:
            pass

        # Last resort: the asyncio handle's own kill.
        try:
            process.kill()
        except Exception:
            pass

    def _layer2_active(self) -> bool:
        """True if the promode uid + sudoers drop-in are provisioned (Layer 2)."""
        try:
            if getattr(self, "_promode_uid_available", None) is None:
                # Trigger detection (also caches) without building a command.
                self._maybe_wrap_with_promode_uid([])
            return bool(self._promode_uid_available)
        except Exception:
            return False

    def _apply_sharing_perms(self, path: Path) -> None:
        """
        When Layer 2 is active, make a scratch dir (and the parent chain up to and
        including the session root) group-shared + setgid so the promode uid can:
          (a) TRAVERSE every ancestor dir to reach the per-exec dir (needs g+x on
              each ancestor), and
          (b) READ/WRITE the leaf dirs, with files it creates inheriting the shared
              group so the backend uid can collect them.
        No-op under Layer-1 (dirs stay 0o700, owned only by the backend uid).
        """
        if not self._layer2_active():
            return
        try:
            import grp
            gid = grp.getgrnam("promode-share").gr_gid
            session_root = self.session_dir.resolve()
            # Walk from the target up to (and including) the session root, applying
            # group ownership + setgid + group-rwx so the whole path is reachable.
            cur = path.resolve()
            while True:
                try:
                    os.chown(cur, -1, gid)
                    os.chmod(cur, 0o2770)  # rwx owner+group, setgid, none for other
                except (PermissionError, FileNotFoundError):
                    pass
                if cur == session_root or cur == cur.parent:
                    break
                cur = cur.parent
        except Exception as e:
            logger.warning(f"Could not apply Layer-2 sharing perms to {path}: {e}")

    def _maybe_wrap_with_promode_uid(self, inner_cmd: List[str]) -> List[str]:
        """
        LAYER 2: if the dedicated 'promode' user and the scoped sudoers drop-in are
        provisioned, run the jail as that uid via passwordless `sudo -n -u promode`.
        The promode uid is the one the egress iptables allowlist is scoped to, and it
        has NO secrets in its environment/home. If not provisioned, return the command
        unchanged (Layer-1-only: still safe — mount isolation already hides secrets).

        Detection is best-effort and cached, so a missing user simply degrades to
        Layer 1 instead of breaking Pro Mode.
        """
        try:
            if getattr(self, "_promode_uid_available", None) is None:
                import grp
                import os as _os
                import pwd
                import shutil as _shutil
                available = False
                reason = ""
                try:
                    pwd.getpwnam("promode")
                    # 1. The backend uid CANNOT stat /etc/sudoers.d/* (the dir is
                    #    root-only 0750), so probe the actual capability instead:
                    #    does passwordless `sudo -n -u promode true` succeed? This
                    #    tests exactly what we need and is immune to PATH/readability
                    #    quirks under systemd. Cached via _promode_uid_available.
                    # Probe with the EXACT granted command. The sudoers drop-in
                    # grants ONLY `/usr/bin/unshare` to promode, so a probe with a
                    # different binary (e.g. /usr/bin/true) is denied ("a password
                    # is required") even though the real jail invocation would
                    # succeed. Running `unshare --version` is harmless and exercises
                    # the precise grant the jail relies on.
                    import subprocess as _subprocess
                    sudo_bin = _shutil.which("sudo") or "/usr/bin/sudo"
                    try:
                        _probe = _subprocess.run(
                            [sudo_bin, "-n", "-u", "promode", str(self.UNSHARE), "--version"],
                            capture_output=True, timeout=5,
                        )
                        has_sudoers = (_probe.returncode == 0)
                    except Exception:
                        has_sudoers = False
                    # 2. CRITICAL: THIS process must be a member of 'promode-share'.
                    #    Without it, _apply_sharing_perms cannot chgrp the scratch
                    #    dirs to the shared group, so the promode uid cannot read the
                    #    exec script ("/work/exec.py: Permission denied") nor write
                    #    output we can collect. Group membership is only picked up on
                    #    a fresh process (service restart after provisioning), so we
                    #    must NOT claim Layer 2 is usable until then — otherwise every
                    #    Pro Mode call hard-fails. Falling back to Layer-1 is safe.
                    try:
                        share_gid = grp.getgrnam("promode-share").gr_gid
                        in_group = share_gid in _os.getgroups()
                    except KeyError:
                        in_group = False
                    available = bool(has_sudoers and in_group)
                    if not available:
                        if not has_sudoers:
                            reason = "sudo -n -u promode probe failed (sudoers not provisioned)"
                        elif not in_group:
                            reason = ("backend process not in 'promode-share' group "
                                      "(restart the service after provisioning)")
                except KeyError:
                    reason = "promode user does not exist"
                self._promode_uid_available = available
                if available:
                    logger.info("Layer-2 promode uid available: jail will run as 'promode'")
                else:
                    logger.info(f"Layer-2 not active ({reason}): using Layer-1-only jail")

            if self._promode_uid_available:
                # Absolute sudo path: the subprocess is launched with a scrubbed
                # env under systemd, so a bare "sudo" argv[0] fails PATH lookup
                # with [Errno 2]. The inner unshare/bash are already absolute.
                import shutil as _shutil2
                sudo_bin = _shutil2.which("sudo") or "/usr/bin/sudo"
                return [sudo_bin, "-n", "-u", "promode", *inner_cmd]
        except Exception as e:
            logger.warning(f"promode uid detection failed, using Layer-1-only: {e}")
        return inner_cmd

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

    async def canary_self_test(self, force: bool = False) -> bool:
        """
        Boot canary: run the jail once with a tiny script that exercises the full
        scientific stack inside the sandbox. If it fails, the jail is misconfigured
        (e.g. a venv/python bump moved a path out of the bind set) and Pro Mode must
        fail closed rather than silently 500 on every real request at 3am.

        Caches the result on the class so it runs once per process. Returns True if
        the jail is healthy. Callers should treat False as "Pro Mode unavailable".
        """
        if not force and SecureCodeExecutor._canary_ok is not None:
            return SecureCodeExecutor._canary_ok

        canary_code = (
            "import numpy, pandas, matplotlib\n"
            "matplotlib.use('Agg')\n"
            "import matplotlib.pyplot as plt\n"
            "import httpx\n"
            "print('CANARY_OK')\n"
        )
        try:
            result = await self.execute_code(
                canary_code,
                session_id="__canary__",
                timeout=30,
                memory_limit_mb=768,
            )
            ok = bool(result.get("success")) and "CANARY_OK" in (result.get("output") or "")
            if not ok:
                logger.error(
                    "Pro Mode sandbox CANARY FAILED — jail is broken, failing closed. "
                    f"result={result!r}"
                )
            else:
                logger.info("Pro Mode sandbox canary passed: jail is healthy")
        except Exception as e:
            ok = False
            logger.error(f"Pro Mode sandbox canary raised — failing closed: {e}")

        SecureCodeExecutor._canary_ok = ok
        return ok


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
