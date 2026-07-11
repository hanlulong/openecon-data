"""T14 (Tier-4) — captured sandbox stderr must reach the result payload/logs.

The wrapped script captured stderr into an io.StringIO but every result branch
discarded it, so pandas/numpy warnings and real tracebacks vanished. The wrapped
script now records result["stderr"] (last 2000 chars) in all four branches, and
_merge_stderr folds that tail into the field the CodeExecutor wrapper actually
forwards (output on success, error on failure) since a bare stderr key would be
dropped before reaching the client.
"""
from __future__ import annotations

from pathlib import Path

from backend.services.secure_code_executor import SecureCodeExecutor


def _bare_executor() -> SecureCodeExecutor:
    return SecureCodeExecutor.__new__(SecureCodeExecutor)


# --- _merge_stderr folding ---------------------------------------------------

def test_success_appends_stderr_to_output():
    result = {"success": True, "output": "chart done", "error": "", "stderr": "UserWarning: deprecated"}
    SecureCodeExecutor._merge_stderr(result, "sess")
    assert "chart done" in result["output"]
    assert "[stderr]" in result["output"]
    assert "UserWarning: deprecated" in result["output"]


def test_failure_appends_stderr_to_error():
    result = {"success": False, "output": "", "error": "ValueError: boom", "stderr": "Traceback ...\nValueError: boom"}
    SecureCodeExecutor._merge_stderr(result, "sess")
    assert "ValueError: boom" in result["error"]
    assert "[stderr]" in result["error"]
    assert "Traceback" in result["error"]


def test_empty_stderr_is_a_noop():
    result = {"success": True, "output": "clean", "error": "", "stderr": "   "}
    SecureCodeExecutor._merge_stderr(result, "sess")
    assert result["output"] == "clean"
    assert "[stderr]" not in result["output"]


def test_missing_stderr_key_is_a_noop():
    result = {"success": True, "output": "clean", "error": ""}
    SecureCodeExecutor._merge_stderr(result, "sess")
    assert result["output"] == "clean"


def test_success_with_no_prior_output_still_surfaces_stderr():
    result = {"success": True, "output": "", "error": "", "stderr": "some warning"}
    SecureCodeExecutor._merge_stderr(result, "sess")
    assert result["output"].startswith("[stderr]")
    assert "some warning" in result["output"]


def test_stderr_tail_is_capped_at_2000_chars():
    long_err = "A" * 500 + "B" * 3000  # 3500 chars; only the last 2000 matter
    result = {"success": False, "output": "", "error": "err", "stderr": long_err}
    SecureCodeExecutor._merge_stderr(result, "sess")
    # The appended block carries at most the trailing 2000 chars.
    assert result["error"].count("A") == 0
    assert result["error"].count("B") == 2000


# --- wrapped-script template records stderr in every branch ------------------

def test_wrapped_script_records_stderr_in_all_branches():
    executor = _bare_executor()
    wrapped = executor._wrap_code(
        "print('hi')", Path("/tmp/work"), Path("/tmp/sess"), 100000
    )
    # success, TimeoutError, SystemExit(x2), Exception -> at least 5 occurrences.
    assert wrapped.count('"stderr"') >= 5
    assert "_errors.getvalue()[-2000:]" in wrapped
