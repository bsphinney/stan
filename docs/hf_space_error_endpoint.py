"""Error report endpoint for the STAN HF Space relay (app.py).

Copy-paste this code into app.py on the brettsp/stan HF Space.
Add the imports at the top of app.py, and the route + helpers
anywhere after the FastAPI app is created.

Receives anonymous error reports from STAN clients (opt-in via
community.yml error_telemetry: true). Stores them in a local JSON
file on the Space, capped at 1000 entries. No authentication needed.
"""

# ── Add these imports to the top of app.py ──────────────────────────
# (most are likely already imported; just add any missing ones)
#
# import json
# import time
# import threading
# from datetime import datetime, timezone
# from pathlib import Path
# from fastapi import Request
# from pydantic import BaseModel, Field


# ── Paste everything below into app.py after the app is created ─────

# --- Error report storage + rate limiting ---

_ERROR_REPORTS_PATH = Path("error_reports.json")
_ERROR_REPORTS_MAX = 1000
_ERROR_REPORTS_LOCK = threading.Lock()

# Rate limiting: max 100 reports per hour per IP
_ERROR_RATE_LIMIT = 100
_ERROR_RATE_WINDOW = 3600  # seconds
_error_rate_counters: dict[str, list[float]] = {}  # ip -> [timestamps]


def _check_error_rate_limit(ip: str) -> bool:
    """Return True if the IP is within the rate limit, False if exceeded."""
    now = time.time()
    cutoff = now - _ERROR_RATE_WINDOW

    if ip not in _error_rate_counters:
        _error_rate_counters[ip] = []

    # Prune old timestamps
    _error_rate_counters[ip] = [t for t in _error_rate_counters[ip] if t > cutoff]

    if len(_error_rate_counters[ip]) >= _ERROR_RATE_LIMIT:
        return False

    _error_rate_counters[ip].append(now)
    return True


def _append_error_report(report: dict) -> None:
    """Append a report to the JSON file, capped at _ERROR_REPORTS_MAX entries."""
    with _ERROR_REPORTS_LOCK:
        entries: list[dict] = []
        if _ERROR_REPORTS_PATH.exists():
            try:
                entries = json.loads(_ERROR_REPORTS_PATH.read_text())
                if not isinstance(entries, list):
                    entries = []
            except (json.JSONDecodeError, OSError):
                entries = []

        entries.append(report)

        # Keep only the most recent entries
        if len(entries) > _ERROR_REPORTS_MAX:
            entries = entries[-_ERROR_REPORTS_MAX:]

        _ERROR_REPORTS_PATH.write_text(json.dumps(entries, indent=2))


# --- Pydantic model ---

class ErrorReport(BaseModel):
    """Anonymous error report from a STAN client."""

    timestamp: str = ""
    stan_version: str = "unknown"
    python_version: str = ""
    os: str = ""
    os_version: str = ""
    arch: str = ""
    error_type: str = ""
    error_message: str = ""
    traceback: str = ""
    # Optional context — only safe keys (no paths, no serial numbers)
    search_engine: str = ""
    raw_file_name: str = ""
    vendor: str = ""
    acquisition_mode: str = ""
    instrument_model: str = ""


# --- Route ---

@app.post("/api/error-report")
async def error_report(body: ErrorReport, request: Request) -> dict:
    """Accept an anonymous error report from a STAN client.

    Rate limited to 100 reports per hour per IP. Reports are stored
    in error_reports.json on the Space (last 1000 entries).
    No authentication required. No PII is collected.
    """
    # Rate limit by client IP
    client_ip = request.client.host if request.client else "unknown"
    if not _check_error_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded (100/hour)")

    # Build the stored record
    record = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "client_ip_hash": hashlib.sha256(client_ip.encode()).hexdigest()[:16],
        **body.model_dump(),
    }

    # Truncate traceback to prevent abuse (max 5000 chars)
    if len(record.get("traceback", "")) > 5000:
        record["traceback"] = record["traceback"][:5000] + "\n... (truncated)"

    # Truncate error_message similarly (max 1000 chars)
    if len(record.get("error_message", "")) > 1000:
        record["error_message"] = record["error_message"][:1000] + "... (truncated)"

    try:
        _append_error_report(record)
    except Exception:
        logger.exception("Failed to store error report")
        raise HTTPException(status_code=500, detail="Failed to store report")

    logger.info(
        "Error report: %s %s (STAN %s)",
        record.get("error_type", "?"),
        record.get("error_message", "?")[:80],
        record.get("stan_version", "?"),
    )

    return {"status": "ok"}


# --- Optional: admin endpoint to view recent errors ---
# Uncomment if you want to check errors from the Space logs/API.
# Consider adding a simple secret check if exposed.
#
# @app.get("/api/error-reports")
# async def get_error_reports(limit: int = 50) -> dict:
#     """Fetch recent error reports (most recent first)."""
#     if not _ERROR_REPORTS_PATH.exists():
#         return {"reports": [], "count": 0}
#     try:
#         entries = json.loads(_ERROR_REPORTS_PATH.read_text())
#         entries = list(reversed(entries[-limit:]))
#         return {"reports": entries, "count": len(entries)}
#     except Exception:
#         return {"reports": [], "count": 0, "error": "Failed to read reports"}
