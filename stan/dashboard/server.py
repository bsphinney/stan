"""FastAPI dashboard backend — serves QC data and instrument config.

Runs on http://localhost:8421. Serves both API routes and the static React frontend.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
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
from stan.db import get_run, get_runs, get_trends, init_db

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


def _get_instruments_watcher() -> ConfigWatcher:
    global _instruments_watcher
    if _instruments_watcher is None:
        _instruments_watcher = ConfigWatcher(resolve_config_path("instruments.yml"))
    elif _instruments_watcher.is_stale():
        _instruments_watcher.reload()
    return _instruments_watcher


def _get_thresholds_watcher() -> ConfigWatcher:
    global _thresholds_watcher
    if _thresholds_watcher is None:
        _thresholds_watcher = ConfigWatcher(resolve_config_path("thresholds.yml"))
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


@app.get("/api/trends/{instrument}")
async def api_trends(instrument: str, limit: int = 100) -> list[dict]:
    """Fetch time-series metrics for trend charts."""
    return get_trends(instrument=instrument, limit=limit)


@app.get("/api/thresholds")
async def api_thresholds() -> dict:
    """Get current QC thresholds (hot-reloaded)."""
    watcher = _get_thresholds_watcher()
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
    gradient_length_min: int = 60
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

    # Use stored amount/gradient from the run if caller didn't override
    amount = body.amount_ng
    if amount == 50.0 and run.get("amount_ng"):
        amount = run["amount_ng"]

    gradient = body.gradient_length_min
    if gradient == 60 and run.get("gradient_length_min"):
        gradient = run["gradient_length_min"]

    try:
        from stan.community.submit import submit_to_benchmark

        result = submit_to_benchmark(
            run=run,
            gradient_length_min=gradient,
            amount_ng=amount,
            hela_source=body.hela_source,
        )
        return result
    except Exception as e:
        logger.exception("Community submission failed")
        raise HTTPException(status_code=500, detail=str(e))


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
