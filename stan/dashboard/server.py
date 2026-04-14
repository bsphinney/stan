"""FastAPI dashboard backend — serves QC data and instrument config.

Runs on http://localhost:8421. Serves both API routes and the static React frontend.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from stan import __version__
from stan.config import (
    ConfigWatcher,
    get_user_config_dir,
    load_yaml,
    resolve_config_path,
)
from stan.db import get_db_path, get_run, get_runs, get_tic_trace, get_tic_traces_for_instrument, get_trends, init_db

logger = logging.getLogger(__name__)

app = FastAPI(title="STAN Dashboard", version=__version__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Config watchers — hot-reload on API access
_instruments_watcher: ConfigWatcher | None = None
_thresholds_watcher: ConfigWatcher | None = None


def _get_instruments_watcher() -> ConfigWatcher | None:
    global _instruments_watcher
    if _instruments_watcher is None:
        try:
            _instruments_watcher = ConfigWatcher(resolve_config_path("instruments.yml"))
        except FileNotFoundError:
            return None
    elif _instruments_watcher.is_stale():
        _instruments_watcher.reload()
    return _instruments_watcher


def _get_thresholds_watcher() -> ConfigWatcher | None:
    global _thresholds_watcher
    if _thresholds_watcher is None:
        try:
            _thresholds_watcher = ConfigWatcher(resolve_config_path("thresholds.yml"))
        except FileNotFoundError:
            return None
    elif _thresholds_watcher.is_stale():
        _thresholds_watcher.reload()
    return _thresholds_watcher


# ── Startup ──────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup() -> None:
    """Initialize database on startup."""
    init_db()


# ── API Routes ───────────────────────────────────────────────────────

@app.get("/api/version")
async def api_version() -> dict:
    return {"version": __version__}


@app.get("/api/runs")
async def api_runs(
    instrument: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """Fetch recent QC runs, optionally filtered by instrument."""
    return get_runs(instrument=instrument, limit=limit, offset=offset)


@app.get("/api/runs/{run_id}")
async def api_run_detail(run_id: str) -> dict:
    """Fetch a single run with all metrics."""
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    # Parse JSON fields
    if run.get("failed_gates"):
        try:
            run["failed_gates"] = json.loads(run["failed_gates"])
        except (json.JSONDecodeError, TypeError):
            pass
    return run


@app.get("/api/instruments")
async def api_instruments() -> dict:
    """List instruments from instruments.yml (hot-reloaded)."""
    watcher = _get_instruments_watcher()
    if watcher is None:
        return {"instruments": []}
    return watcher.data


class InstrumentsUpdate(BaseModel):
    yaml_content: str


@app.post("/api/instruments")
async def api_update_instruments(body: InstrumentsUpdate) -> dict:
    """Update instruments.yml from the dashboard UI."""
    try:
        data = yaml.safe_load(body.yaml_content)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")

    if not isinstance(data, dict) or "instruments" not in data:
        raise HTTPException(status_code=400, detail="YAML must contain 'instruments' key")

    config_path = resolve_config_path("instruments.yml")
    config_path.write_text(body.yaml_content)

    # Force reload
    watcher = _get_instruments_watcher()
    watcher.reload()

    return {"status": "ok", "instruments": len(data.get("instruments", []))}


@app.delete("/api/instruments/{index}")
async def api_delete_instrument(index: int) -> dict:
    """Delete an instrument by its index in the instruments list."""
    config_path = resolve_config_path("instruments.yml")
    data = yaml.safe_load(config_path.read_text()) or {}
    instruments = data.get("instruments", [])

    if index < 0 or index >= len(instruments):
        raise HTTPException(status_code=404, detail="Instrument index out of range")

    removed = instruments.pop(index)
    data["instruments"] = instruments
    config_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    watcher = _get_instruments_watcher()
    watcher.reload()

    return {"status": "ok", "removed": removed.get("name", "unknown"), "remaining": len(instruments)}


@app.get("/api/trends/{instrument}")
async def api_trends(instrument: str, limit: int = 100) -> list[dict]:
    """Fetch time-series metrics for trend charts."""
    return get_trends(instrument=instrument, limit=limit)


@app.get("/api/thresholds")
async def api_thresholds() -> dict:
    """Get current QC thresholds (hot-reloaded)."""
    watcher = _get_thresholds_watcher()
    if watcher is None:
        return {}
    return watcher.data


class ThresholdsUpdate(BaseModel):
    yaml_content: str


@app.post("/api/thresholds")
async def api_update_thresholds(body: ThresholdsUpdate) -> dict:
    """Update thresholds.yml from the dashboard UI."""
    try:
        data = yaml.safe_load(body.yaml_content)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")

    config_path = resolve_config_path("thresholds.yml")
    config_path.write_text(body.yaml_content)

    watcher = _get_thresholds_watcher()
    watcher.reload()

    return {"status": "ok"}


@app.get("/api/instruments/{instrument}/events")
async def api_events(instrument: str, limit: int = 50) -> list[dict]:
    """Fetch maintenance events for an instrument."""
    from stan.db import get_events
    return get_events(instrument=instrument, limit=limit)


class LogEventRequest(BaseModel):
    event_type: str
    event_date: str | None = None
    notes: str = ""
    operator: str = ""
    column_vendor: str | None = None
    column_model: str | None = None
    column_serial: str | None = None


@app.post("/api/instruments/{instrument}/events")
async def api_log_event(instrument: str, body: LogEventRequest) -> dict:
    """Log a maintenance event from the dashboard UI."""
    from stan.db import log_event, EVENT_TYPES
    if body.event_type not in EVENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid event type. Valid: {EVENT_TYPES}")
    event_id = log_event(
        instrument=instrument,
        event_type=body.event_type,
        event_date=body.event_date,
        notes=body.notes,
        operator=body.operator,
        column_vendor=body.column_vendor,
        column_model=body.column_model,
        column_serial=body.column_serial,
    )
    return {"event_id": event_id, "status": "logged"}


@app.get("/api/instruments/{instrument}/column-life")
async def api_column_life(instrument: str) -> dict:
    """Column lifetime stats since last column change."""
    from stan.db import get_column_lifetime
    return get_column_lifetime(instrument=instrument)


@app.get("/api/instruments/{instrument}/last-qc")
async def api_last_qc(instrument: str) -> dict:
    """Time since last QC run on this instrument."""
    from stan.db import time_since_last_qc
    return time_since_last_qc(instrument=instrument)


@app.get("/api/runs/{run_id}/tic")
async def api_tic_trace(run_id: str) -> dict:
    """Fetch TIC trace for a single run."""
    trace = get_tic_trace(run_id)
    if not trace:
        raise HTTPException(status_code=404, detail="No TIC trace for this run")
    return trace


@app.get("/api/instruments/{instrument}/tic")
async def api_instrument_tic(instrument: str, limit: int = 20) -> dict:
    """Fetch recent TIC traces for an instrument (for overlay plot)."""
    traces = get_tic_traces_for_instrument(instrument, limit=min(limit, 50))
    return {"instrument": instrument, "traces": traces, "count": len(traces)}


@app.get("/api/community/cohort")
async def api_community_cohort() -> dict:
    """Fetch community cohort data.

    Returns cached cohort percentiles — updated by nightly consolidation.
    """
    try:
        from stan.community.fetch import fetch_cohort_percentiles

        return fetch_cohort_percentiles()
    except Exception:
        logger.exception("Failed to fetch community cohort")
        return {"cohorts": {}, "error": "Failed to fetch community data"}


class CommunitySubmitRequest(BaseModel):
    run_id: str
    spd: int | None = None
    gradient_length_min: int | None = None
    amount_ng: float = 50.0
    hela_source: str = "Pierce HeLa Protein Digest Standard"


@app.post("/api/community/submit")
async def api_community_submit(body: CommunitySubmitRequest) -> dict:
    """Submit a QC run to the community benchmark.

    If amount_ng is not provided in the request, falls back to the value
    stored in the run record (from the instrument config), then to 50 ng.
    """
    run = get_run(body.run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Use stored values from the run if caller didn't override
    amount = body.amount_ng
    if amount == 50.0 and run.get("amount_ng"):
        amount = run["amount_ng"]

    spd = body.spd or run.get("spd")
    gradient = body.gradient_length_min or run.get("gradient_length_min")

    try:
        from stan.community.submit import submit_to_benchmark

        result = submit_to_benchmark(
            run=run,
            spd=spd,
            gradient_length_min=gradient,
            amount_ng=amount,
            hela_source=body.hela_source,
        )
        return result
    except Exception as e:
        logger.exception("Community submission failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── Dashboard error capture ──────────────────────────────────────────
# The frontend's window.onerror POSTs JS errors here so they show up
# in the server log (and the Hive mirror) for remote debugging.

_DASH_ERROR_LOG: list[dict] = []
_DASH_ERROR_MAX = 50


@app.post("/api/dashboard-error")
async def api_dashboard_error(request: Request) -> dict:
    """Receive a frontend JS error report."""
    try:
        body = await request.json()
    except Exception:
        return {"status": "ignored"}
    entry = {
        "ts": body.get("ts", ""),
        "msg": str(body.get("msg", ""))[:500],
        "src": str(body.get("src", ""))[:200],
        "line": body.get("line"),
        "col": body.get("col"),
        "stack": str(body.get("stack", ""))[:2000],
    }
    _DASH_ERROR_LOG.append(entry)
    if len(_DASH_ERROR_LOG) > _DASH_ERROR_MAX:
        _DASH_ERROR_LOG.pop(0)
    logger.warning(
        "Dashboard JS error: %s (line %s:%s)\n%s",
        entry["msg"][:100], entry["line"], entry["col"], entry["stack"][:500],
    )
    # Also write to a dedicated file for Hive mirror
    try:
        err_log = get_db_path().parent / "dashboard_errors.log"
        with open(err_log, "a", encoding="utf-8") as f:
            f.write(f"{entry['ts']} | {entry['msg'][:200]} | line {entry['line']}:{entry['col']}\n")
            if entry["stack"]:
                f.write(f"  {entry['stack'][:500]}\n")
    except Exception:
        pass
    return {"status": "logged"}


@app.get("/api/dashboard-errors")
async def api_dashboard_errors() -> list[dict]:
    """Return the last N dashboard JS errors for remote debugging."""
    return _DASH_ERROR_LOG


# ── Static frontend ──────────────────────────────────────────────────

_FRONTEND_DIR = Path(__file__).parent / "public"


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the dashboard frontend."""
    index_path = _FRONTEND_DIR / "index.html"
    if index_path.exists():
        return HTMLResponse(index_path.read_text())
    return HTMLResponse(_FALLBACK_HTML)


# Mount static files if the directory exists
if _FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_FRONTEND_DIR)), name="static")


_FALLBACK_HTML = """<!DOCTYPE html>
<html><head><title>STAN Dashboard</title></head>
<body style="font-family: sans-serif; padding: 2rem;">
<h1>STAN Dashboard</h1>
<p>Frontend not built yet. API is running at <code>/api/</code>.</p>
<ul>
<li><a href="/api/version">/api/version</a></li>
<li><a href="/api/runs">/api/runs</a></li>
<li><a href="/api/instruments">/api/instruments</a></li>
<li><a href="/api/thresholds">/api/thresholds</a></li>
<li><a href="/docs">/docs</a> (Swagger UI)</li>
</ul>
</body></html>
"""
