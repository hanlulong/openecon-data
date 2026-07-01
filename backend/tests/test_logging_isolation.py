"""The production app log must not receive log lines from test runs.

backend/main.py attaches a FileHandler for .omx/logs/backend-app.log so the
systemd service has a debuggable request log. The suite imports backend.main
in-process, so without a guard every pytest run interleaves mock-auth and
fake-provider log lines into the SAME file the production service writes,
making live incidents un-debuggable. The handler is therefore skipped when
pytest is the running program.
"""

import logging


def test_backend_app_log_handler_not_attached_under_pytest():
    import backend.main  # noqa: F401  (import triggers the logging setup)

    file_handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)
    ]
    offenders = [
        h.baseFilename
        for h in file_handlers
        if "backend-app.log" in getattr(h, "baseFilename", "")
    ]
    assert not offenders, (
        "backend.main attached the production backend-app.log handler during a "
        f"pytest run: {offenders}. Test log lines would interleave with real "
        "service traffic."
    )
