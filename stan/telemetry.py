"""Opt-in anonymous error telemetry for STAN.

Collects error type, message, sanitized traceback, STAN version, OS, Python
version, and optional instrument context. Never collects file paths, serial
numbers, or patient data.

Telemetry is only active when ``error_telemetry: true`` is set in
``~/.stan/community.yml``. Reports are sent fire-and-forget in a daemon
thread so they never block the main process. If the relay is unreachable,
the error is silently dropped.

All errors are also appended to ``~/.stan/error_log.json`` (last 100) for
local debugging regardless of the telemetry opt-in setting.
"""

from __future__ import annotations

import json
import logging
import platform
import re
import threading
import traceback
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

RELAY_URL = "https://brettsp-stan.hf.space"
_ERROR_LOG_PATH = Path.home() / ".stan" / "error_log.json"
_MAX_LOCAL_ERRORS = 100
_POST_TIMEOUT = 5  # seconds


def _get_stan_version() -> str:
    """Return the installed STAN version string."""
    try:
        from stan import __version__
        return __version__
    except Exception:
        return "unknown"


def _is_opted_in() -> bool:
    """Check if error telemetry is enabled in community.yml."""
    try:
        from stan.config import load_community
        comm = load_community()
        return bool(comm.get("error_telemetry", False))
    except Exception:
        return False


def _sanitize_traceback(tb_str: str) -> str:
    """Strip full file paths from a traceback string, keeping only filenames.

    Replaces patterns like:
        File "/home/user/project/stan/search/local.py", line 42
    with:
        File "local.py", line 42
    """
    return re.sub(
        r'File "([^"]+)"',
        lambda m: f'File "{Path(m.group(1)).name}"',
        tb_str,
    )


def _build_payload(
    error: Exception,
    context: dict | None = None,
) -> dict:
    """Build the JSON payload for an error report."""
    tb_str = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    sanitized_tb = _sanitize_traceback(tb_str)

    payload: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "stan_version": _get_stan_version(),
        "python_version": platform.python_version(),
        "os": platform.system(),
        "os_version": platform.release(),
        "arch": platform.machine(),
        "error_type": type(error).__qualname__,
        "error_message": str(error),
        "traceback": sanitized_tb,
    }

    if context:
        # Only allow safe context keys — no paths, no serial numbers
        safe_keys = {
            "search_engine",
            "raw_file_name",
            "vendor",
            "acquisition_mode",
            "instrument_model",
        }
        for key in safe_keys:
            if key in context:
                val = context[key]
                # For raw_file_name, ensure it's just a stem (no path)
                if key == "raw_file_name" and val:
                    val = Path(str(val)).stem
                payload[key] = val

    return payload


def _append_local_log(payload: dict) -> None:
    """Append an error to the local error log file (last N entries)."""
    try:
        _ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

        entries: list[dict] = []
        if _ERROR_LOG_PATH.exists():
            try:
                entries = json.loads(_ERROR_LOG_PATH.read_text())
                if not isinstance(entries, list):
                    entries = []
            except (json.JSONDecodeError, OSError):
                entries = []

        entries.append(payload)
        # Keep only the last N entries
        entries = entries[-_MAX_LOCAL_ERRORS:]

        _ERROR_LOG_PATH.write_text(json.dumps(entries, indent=2))
    except Exception:
        # Never crash on logging failures
        pass


def _send_report(payload: dict) -> None:
    """POST the error report to the relay. Runs in a daemon thread."""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{RELAY_URL}/api/error-report",
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"STAN/{_get_stan_version()}",
            },
        )
        with urllib.request.urlopen(req, timeout=_POST_TIMEOUT):
            pass  # fire-and-forget — we don't need the response
    except Exception:
        # Silently ignore all network errors
        pass


def report_error(error: Exception, context: dict | None = None) -> None:
    """Report an error for telemetry and local logging.

    Safe to call from anywhere. Never raises, never blocks.

    Args:
        error: The exception to report.
        context: Optional dict with safe context keys:
            search_engine, raw_file_name (stem only), vendor,
            acquisition_mode, instrument_model.
    """
    try:
        payload = _build_payload(error, context)

        # Always write to local log
        _append_local_log(payload)

        # Only send to relay if opted in
        if _is_opted_in():
            thread = threading.Thread(
                target=_send_report,
                args=(payload,),
                daemon=True,
                name="stan-telemetry",
            )
            thread.start()
    except Exception:
        # The telemetry system must never crash the application
        pass
