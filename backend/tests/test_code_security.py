#!/usr/bin/env python3
"""
Comprehensive security tests for code execution sandbox.

This test suite verifies that the secure code executor properly blocks
all dangerous operations while allowing legitimate data science code.
"""

import pytest
import asyncio
from pathlib import Path
from backend.services.secure_code_executor import (
    SecurityValidator,
    SecureCodeExecutor,
    SecurityLevel
)


class TestSecurityValidator:
    """Test AST-based security validation"""

    def test_blocks_os_import(self):
        """Test that os module import is blocked"""
        validator = SecurityValidator()
        code = "import os"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block os import"
        assert any("Forbidden import" in v for v in violations)

    def test_blocks_subprocess_import(self):
        """Test that subprocess module import is blocked"""
        validator = SecurityValidator()
        code = "import subprocess"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block subprocess import"
        assert any("Forbidden import" in v for v in violations)

    def test_blocks_socket_import(self):
        """Test that socket module import is blocked"""
        validator = SecurityValidator()
        code = "import socket"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block socket import"

    def test_blocks_sys_import(self):
        """Test that sys module import is blocked"""
        validator = SecurityValidator()
        code = "import sys"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block sys import"

    def test_blocks_ctypes_import(self):
        """Test that ctypes module import is blocked"""
        validator = SecurityValidator()
        code = "import ctypes"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block ctypes import"

    def test_blocks_importlib_import(self):
        """Test that importlib module import is blocked"""
        validator = SecurityValidator()
        code = "import importlib"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block importlib import"

    def test_blocks_threading_import(self):
        """Test that threading module import is blocked"""
        validator = SecurityValidator()
        code = "import threading"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block threading import"

    def test_blocks_pickle_import(self):
        """Test that pickle module import is blocked"""
        validator = SecurityValidator()
        code = "import pickle"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block pickle import (security risk)"

    def test_blocks_from_os_import(self):
        """Test that from os import is blocked"""
        validator = SecurityValidator()
        code = "from os import system"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block from os import"
        assert any("Forbidden import" in v for v in violations)

    def test_blocks_from_subprocess_import(self):
        """Test that from subprocess import is blocked"""
        validator = SecurityValidator()
        code = "from subprocess import run"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block from subprocess import"

    def test_blocks_eval_function(self):
        """Test that eval() function call is blocked"""
        validator = SecurityValidator()
        code = "eval('print(1)')"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block eval()"
        assert any("Forbidden function" in v for v in violations)

    def test_blocks_exec_function(self):
        """Test that exec() function call is blocked"""
        validator = SecurityValidator()
        code = "exec('x = 1')"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block exec()"
        assert any("Forbidden function" in v for v in violations)

    def test_blocks_compile_function(self):
        """Test that compile() function call is blocked"""
        validator = SecurityValidator()
        code = "compile('x=1', 'string', 'exec')"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block compile()"

    def test_blocks_import_builtin(self):
        """Test that __import__() function call is blocked"""
        validator = SecurityValidator()
        code = "__import__('os')"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block __import__()"
        assert any("Dynamic import" in v or "Forbidden" in v for v in violations)

    def test_blocks_getattr_function(self):
        """Test that getattr() function call is blocked"""
        validator = SecurityValidator()
        code = "getattr(obj, 'attr')"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block getattr()"

    def test_blocks_setattr_function(self):
        """Test that setattr() function call is blocked"""
        validator = SecurityValidator()
        code = "setattr(obj, 'attr', value)"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block setattr()"

    def test_blocks_delattr_function(self):
        """Test that delattr() function call is blocked"""
        validator = SecurityValidator()
        code = "delattr(obj, 'attr')"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block delattr()"

    def test_blocks_globals_function(self):
        """Test that globals() function call is blocked"""
        validator = SecurityValidator()
        code = "globals()"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block globals()"

    def test_blocks_locals_function(self):
        """Test that locals() function call is blocked"""
        validator = SecurityValidator()
        code = "locals()"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block locals()"

    def test_blocks_vars_function(self):
        """Test that vars() function call is blocked"""
        validator = SecurityValidator()
        code = "vars()"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block vars()"

    def test_blocks_input_function(self):
        """Test that input() function call is blocked"""
        validator = SecurityValidator()
        code = "input('Enter: ')"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block input()"

    def test_blocks_open_function_strict(self):
        """Test that open() function is blocked in STRICT mode"""
        validator = SecurityValidator(SecurityLevel.STRICT)
        code = "open('/etc/passwd')"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block open() in STRICT mode"

    def test_allows_open_function_moderate(self):
        """Test that open() is allowed in MODERATE mode"""
        validator = SecurityValidator(SecurityLevel.MODERATE)
        code = "with open('data.txt', 'w') as f: f.write('test')"
        is_safe, violations, warnings = validator.validate(code)
        # Should be allowed but with warnings
        assert len(warnings) > 0 or is_safe

    def test_blocks_dunder_attributes(self):
        """Test that dunder attribute access is blocked"""
        validator = SecurityValidator()
        code = "func.__globals__"
        is_safe, violations, warnings = validator.validate(code)
        assert not is_safe or len(warnings) > 0, "Should warn about dunder attributes"

    def test_blocks_hex_obfuscation(self):
        """Test detection of hex-encoded obfuscation"""
        validator = SecurityValidator()
        code = "exec('\\x70\\x72\\x69\\x6e\\x74')"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "Should block exec() with hex strings"

    def test_detects_long_obfuscated_lines(self):
        """Test detection of suspiciously long lines"""
        validator = SecurityValidator()
        # Create a 600 character line
        code = "x = " + "1 + " * 200 + "1"
        is_safe, violations, warnings = validator.validate(code)
        assert len(violations) > 0, "Should detect obfuscated long lines"

    def test_allows_safe_imports(self):
        """Test that safe imports are allowed"""
        validator = SecurityValidator()
        safe_imports = [
            "import pandas as pd",
            "import numpy as np",
            "import matplotlib.pyplot as plt",
            "from pandas import DataFrame",
            "from numpy import array",
        ]
        for code in safe_imports:
            is_safe, violations, _ = validator.validate(code)
            assert is_safe, f"Should allow: {code}. Violations: {violations}"

    def test_allows_safe_operations(self):
        """Test that safe operations are allowed"""
        validator = SecurityValidator()
        safe_code = [
            "x = [1, 2, 3]",
            "print(sum(x))",
            "df = pd.DataFrame({'a': [1, 2]})",
            "result = df.sum()",
            "mean = np.mean(x)",
        ]
        for code in safe_code:
            is_safe, violations, _ = validator.validate(code)
            assert is_safe, f"Should allow: {code}. Violations: {violations}"

    def test_detects_lambda_map_pattern(self):
        """Test detection of lambda+map obfuscation pattern"""
        validator = SecurityValidator()
        code = "result = map(lambda x: x*2, data)"
        is_safe, violations, warnings = validator.validate(code)
        assert len(warnings) > 0, "Should warn about lambda+map pattern"


class TestSecureCodeExecutor:
    """Test secure code execution sandbox"""

    @pytest.mark.asyncio
    async def test_executes_safe_code(self):
        """Test that safe code executes successfully"""
        executor = SecureCodeExecutor(SecurityLevel.MODERATE)

        code = """
import pandas as pd
import numpy as np

# Create sample data
data = pd.DataFrame({
    'x': [1, 2, 3, 4, 5],
    'y': [2, 4, 6, 8, 10]
})

# Calculate statistics
print(f"Mean of x: {data['x'].mean()}")
print(f"Mean of y: {data['y'].mean()}")
print(f"Correlation: {data['x'].corr(data['y'])}")
"""

        result = await executor.execute_code(code, "test-session", timeout=10)

        assert result["success"] is True, f"Execution failed: {result.get('error')}"
        assert "Mean of x: 3" in result["output"], "Output should contain mean calculation"

    @pytest.mark.asyncio
    async def test_blocks_os_system(self):
        """Test that os.system() is blocked"""
        executor = SecureCodeExecutor(SecurityLevel.STRICT)

        code = "import os\nos.system('ls')"

        result = await executor.execute_code(code, "test-os-system")

        assert result["success"] is False, "Should block os.system()"
        assert "Security violations" in result["error"]

    @pytest.mark.asyncio
    async def test_blocks_subprocess_run(self):
        """Test that subprocess.run() is blocked"""
        executor = SecureCodeExecutor(SecurityLevel.STRICT)

        code = "import subprocess\nsubprocess.run(['ls'])"

        result = await executor.execute_code(code, "test-subprocess")

        assert result["success"] is False, "Should block subprocess.run()"

    @pytest.mark.asyncio
    async def test_blocks_dynamic_import(self):
        """Test that __import__() is blocked"""
        executor = SecureCodeExecutor(SecurityLevel.STRICT)

        code = "__import__('os').listdir('/')"

        result = await executor.execute_code(code, "test-dynamic-import")

        assert result["success"] is False, "Should block dynamic imports"

    @pytest.mark.asyncio
    async def test_blocks_eval(self):
        """Test that eval() is blocked"""
        executor = SecureCodeExecutor(SecurityLevel.STRICT)

        code = "eval('1+1')"

        result = await executor.execute_code(code, "test-eval")

        assert result["success"] is False, "Should block eval()"

    @pytest.mark.asyncio
    async def test_blocks_exec(self):
        """Test that exec() is blocked"""
        executor = SecureCodeExecutor(SecurityLevel.STRICT)

        code = "exec('x=1')"

        result = await executor.execute_code(code, "test-exec")

        assert result["success"] is False, "Should block exec()"

    @pytest.mark.asyncio
    async def test_blocks_compile(self):
        """Test that compile() is blocked"""
        executor = SecureCodeExecutor(SecurityLevel.STRICT)

        code = "compile('x=1', 'string', 'exec')"

        result = await executor.execute_code(code, "test-compile")

        assert result["success"] is False, "Should block compile()"

    @pytest.mark.asyncio
    async def test_blocks_file_open_strict(self):
        """Test that open() is blocked in STRICT mode"""
        executor = SecureCodeExecutor(SecurityLevel.STRICT)

        code = "open('/etc/passwd', 'r').read()"

        result = await executor.execute_code(code, "test-file-open")

        assert result["success"] is False, "Should block file open in STRICT mode"

    @pytest.mark.asyncio
    async def test_enforces_timeout(self):
        """Test that infinite loops are terminated"""
        executor = SecureCodeExecutor()

        code = """
# Infinite loop
while True:
    pass
"""

        result = await executor.execute_code(
            code,
            "test-timeout",
            timeout=2  # 2 second timeout
        )

        assert result["success"] is False, "Should timeout"
        assert "timed out" in result["error"].lower(), "Error should mention timeout"

    @pytest.mark.asyncio
    async def test_enforces_output_limit(self):
        """Test that output is limited to prevent DoS"""
        executor = SecureCodeExecutor()

        code = """
# Generate large output
for i in range(10000):
    print(f"Line {i}: " + "x" * 100)
"""

        result = await executor.execute_code(
            code,
            "test-output-limit",
            max_output_size=1000
        )

        # Output should be truncated
        assert len(result.get("output", "")) <= 2000, "Output should be limited"

    @pytest.mark.asyncio
    async def test_blocks_pickle_import(self):
        """Test that pickle import is blocked"""
        executor = SecureCodeExecutor(SecurityLevel.STRICT)

        code = "import pickle"

        result = await executor.execute_code(code, "test-pickle")

        assert result["success"] is False, "Should block pickle import"

    @pytest.mark.asyncio
    async def test_blocks_socket_import(self):
        """Test that socket import is blocked"""
        executor = SecureCodeExecutor(SecurityLevel.STRICT)

        code = "import socket"

        result = await executor.execute_code(code, "test-socket")

        assert result["success"] is False, "Should block socket import"

    @pytest.mark.asyncio
    async def test_blocks_requests_import(self):
        """Test that requests import is blocked"""
        executor = SecureCodeExecutor(SecurityLevel.STRICT)

        code = "import requests"

        result = await executor.execute_code(code, "test-requests")

        assert result["success"] is False, "Should block requests import"

    @pytest.mark.asyncio
    async def test_blocks_urllib_import(self):
        """Test that urllib import is blocked"""
        executor = SecureCodeExecutor(SecurityLevel.STRICT)

        code = "import urllib"

        result = await executor.execute_code(code, "test-urllib")

        assert result["success"] is False, "Should block urllib import"

    @pytest.mark.asyncio
    async def test_execution_with_pandas(self):
        """Test execution of pandas data analysis code"""
        executor = SecureCodeExecutor(SecurityLevel.MODERATE)

        code = """
import pandas as pd
import numpy as np

# Create sample data
df = pd.DataFrame({
    'date': pd.date_range('2024-01-01', periods=5),
    'value': [10, 20, 30, 40, 50],
    'group': ['A', 'B', 'A', 'B', 'A']
})

# Perform analysis
print("DataFrame:")
print(df)
print()
print("Group means:")
print(df.groupby('group')['value'].mean())
"""

        result = await executor.execute_code(code, "test-pandas", timeout=10)

        assert result["success"] is True, f"Failed: {result.get('error')}"
        assert "DataFrame" in result["output"], "Should show dataframe output"

    @pytest.mark.asyncio
    async def test_execution_with_numpy(self):
        """Test execution of numpy code"""
        executor = SecureCodeExecutor(SecurityLevel.MODERATE)

        code = """
import numpy as np

# Create arrays
a = np.array([1, 2, 3, 4, 5])
b = np.array([2, 4, 6, 8, 10])

# Perform operations
print(f"Sum of a: {a.sum()}")
print(f"Mean of b: {b.mean()}")
print(f"Dot product: {np.dot(a, b)}")
"""

        result = await executor.execute_code(code, "test-numpy", timeout=10)

        assert result["success"] is True, f"Failed: {result.get('error')}"
        assert "Sum of a: 15" in result["output"], "Should calculate sums"


class TestSecurityLevels:
    """Test different security levels"""

    @pytest.mark.asyncio
    async def test_strict_level_blocks_file_operations(self):
        """Test STRICT security level blocks file operations"""
        executor = SecureCodeExecutor(SecurityLevel.STRICT)
        validator = SecurityValidator(SecurityLevel.STRICT)

        # File operations should be blocked
        code = "open('test.txt', 'w')"
        is_safe, violations, _ = validator.validate(code)
        assert not is_safe, "STRICT should block file operations"

    @pytest.mark.asyncio
    async def test_moderate_level_allows_file_operations_with_warning(self):
        """Test MODERATE security level allows file operations"""
        validator = SecurityValidator(SecurityLevel.MODERATE)

        # File operations should be allowed with warnings
        code = """
with open('data.txt', 'w') as f:
    f.write('test')
"""
        is_safe, violations, warnings = validator.validate(code)
        # In moderate mode, should be allowed but generate warnings
        assert not violations, "MODERATE should allow file operations"
        assert len(warnings) > 0, "MODERATE should warn about file operations"

    def test_security_level_values(self):
        """Test that security levels have correct values"""
        assert SecurityLevel.STRICT.value == "strict"
        assert SecurityLevel.MODERATE.value == "moderate"
        assert SecurityLevel.RELAXED.value == "relaxed"


def test_validator_returns_tuple():
    """Test that validator.validate() returns proper tuple"""
    validator = SecurityValidator()
    code = "import os"
    result = validator.validate(code)

    assert isinstance(result, tuple), "Should return tuple"
    assert len(result) == 3, "Should return (is_safe, violations, warnings)"
    is_safe, violations, warnings = result
    assert isinstance(is_safe, bool)
    assert isinstance(violations, list)
    assert isinstance(warnings, list)


def test_validator_handles_syntax_errors():
    """Test that validator handles syntax errors gracefully"""
    validator = SecurityValidator()
    code = "if true\n  print('invalid')"  # Invalid Python syntax

    is_safe, violations, _ = validator.validate(code)
    assert not is_safe, "Should report syntax errors as violations"
    assert any("Syntax error" in v for v in violations)


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v"])
