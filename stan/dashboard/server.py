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
    qc_only: bool = True,
    include_hidden: bool = False,
) -> list[dict]:
    """Fetch recent QC runs, optionally filtered by instrument.

    qc_only defaults to True so legacy non-QC rows (historical
    baseline on mixed dirs) don't appear in the dashboard. Pass
    qc_only=false on the query string for debugging/cleanup.

    include_hidden defaults to False so rows the operator soft-
    deleted (hidden=1) are omitted. Pass include_hidden=true when
    reviewing or restoring hidden runs.
    """
    return get_runs(
        instrument=instrument, limit=limit, offset=offset,
        qc_only=qc_only, include_hidden=include_hidden,
    )


class RunHideBody(BaseModel):
    hidden: bool = True
    reason: str = ""


@app.post("/api/runs/{run_id}/hide")
async def api_run_hide(run_id: str, body: RunHideBody) -> dict:
    """Soft-delete (or restore) a QC run row.

    POST with {"hidden": true, "reason": "..."} to hide; {"hidden": false}
    to restore. Hidden rows stay in the DB but are filtered out of the
    default /api/runs response. Returns 404 if the run_id doesn't exist.
    """
    from stan.db import set_run_hidden
    ok = set_run_hidden(run_id, body.hidden, reason=body.reason)
    if not ok:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"run_id": run_id, "hidden": body.hidden, "reason": body.reason}


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
    return get_trends(instrument=instrument, limit=limit, qc_only=True)


@app.get("/api/today/tic-overview")
async def api_today_tic_overview(
    date: str | None = None,
    instrument: str | None = None,
) -> dict:
    """Return today's QC runs + their TIC traces in one call.

    Powers the Today's Runs tab's TIC overlay. The QC-only scope is
    intentional: non-QC files live in the `sample_health` table and
    don't have tic_traces rows yet (Ship B MVP). Sample/Blank facets
    are a follow-up.

    Args:
        date: ISO date YYYY-MM-DD. Defaults to today (local time).
        instrument: Optional name filter.

    Returns:
        {
          "date": "2026-04-20",
          "runs": [
            {
              "run_id", "run_name", "instrument", "mode", "run_date",
              "ips_score", "gate_result", "spd", "gradient_length_min",
              "n_precursors", "n_psms",
              "time_of_day_rank": int,  # 0 = earliest, for color
              "has_tic": bool,
              "tic": {"rt_min": [...], "intensity": [...]} or null
            }, ...
          ],
          "n_runs": int,
          "n_with_tic": int
        }
    """
    import json as _json
    import sqlite3
    from datetime import datetime, timezone
    from stan.db import get_db_path

    db_path = get_db_path()
    if not db_path.exists():
        return {"date": date, "runs": [], "n_runs": 0, "n_with_tic": 0}

    # Default to local "today" — the dashboard runs on the instrument
    # PC, so the operator thinks in local time.
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    # SQLite comparison: run_date is ISO with 'T' separator. Match by
    # date prefix (10-char) so timezone suffixes don't trip us up.
    where = ["substr(r.run_date, 1, 10) = ?",
             "(r.hidden IS NULL OR r.hidden = 0)"]
    params: list = [date]
    if instrument:
        where.append("r.instrument = ?")
        params.append(instrument)
    sql = (
        "SELECT r.id AS run_id, r.run_name, r.instrument, r.mode, "
        "       r.run_date, r.ips_score, r.gate_result, r.spd, "
        "       r.gradient_length_min, r.n_precursors, r.n_psms, "
        "       r.diagnosis, r.amount_ng, "
        "       t.rt_min AS tic_rt, t.intensity AS tic_intensity "
        "FROM runs r "
        "LEFT JOIN tic_traces t ON t.run_id = r.id "
        "WHERE " + " AND ".join(where) +
        " ORDER BY r.run_date ASC"
    )

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, params).fetchall()

        # Pull cIRT observations for just these runs in a second query,
        # keyed by run_id. Joining this into the main SELECT would
        # multiply rows; a separate {run_id: [...]} lookup is cleaner.
        run_ids = [r["run_id"] for r in rows]
        cirt_by_run: dict[str, list[dict]] = {}
        if run_ids:
            placeholders = ",".join(["?"] * len(run_ids))
            try:
                for a in con.execute(
                    f"SELECT run_id, peptide, observed_rt_min, reference_rt_min "
                    f"FROM irt_anchor_rts WHERE run_id IN ({placeholders})",
                    run_ids,
                ).fetchall():
                    cirt_by_run.setdefault(a["run_id"], []).append({
                        "peptide": a["peptide"],
                        "observed_rt_min": a["observed_rt_min"],
                        "reference_rt_min": a["reference_rt_min"],
                    })
            except sqlite3.OperationalError:
                # Older DB without irt_anchor_rts — no cIRT markers,
                # UI falls back to TIC-only rendering.
                pass

    runs: list[dict] = []
    n_with_tic = 0
    for rank, r in enumerate(rows):
        d = dict(r)
        tic_rt = d.pop("tic_rt", None)
        tic_int = d.pop("tic_intensity", None)
        has_tic = tic_rt is not None and tic_int is not None
        tic_payload = None
        if has_tic:
            try:
                tic_payload = {
                    "rt_min": _json.loads(tic_rt),
                    "intensity": _json.loads(tic_int),
                }
                n_with_tic += 1
            except Exception:
                has_tic = False
        d["has_tic"] = has_tic
        d["tic"] = tic_payload
        d["time_of_day_rank"] = rank

        # cIRT markers per peptide, with deviation classified.
        # Thresholds mirror stan/community/validate.py: < 0.5 min = green,
        # < 1.5 min = yellow, >= 1.5 min = red. Reference may be null on
        # older rows backfilled before v0.2.116; UI skips those.
        markers = []
        for a in cirt_by_run.get(d["run_id"], []):
            obs = a["observed_rt_min"]
            ref = a["reference_rt_min"]
            if obs is None or ref is None:
                dev_class = "unknown"
                dev = None
            else:
                dev = obs - ref
                adev = abs(dev)
                if adev < 0.5:
                    dev_class = "green"
                elif adev < 1.5:
                    dev_class = "yellow"
                else:
                    dev_class = "red"
            markers.append({
                "peptide": a["peptide"],
                "observed_rt_min": obs,
                "reference_rt_min": ref,
                "deviation_min": dev,
                "deviation_class": dev_class,
            })
        d["cirt_markers"] = markers

        runs.append(d)

    # Attach the current column per instrument that appears today so
    # the overlay can annotate "Aurora 25cm, installed 12d ago" in
    # its header. The maintenance_events table is the source of
    # truth; get_last_event returns the most recent column_change.
    from stan.db import get_last_event
    from datetime import datetime as _dt

    instruments_today = sorted({r["instrument"] for r in runs if r.get("instrument")})
    columns: dict[str, dict] = {}
    for inst in instruments_today:
        ev = get_last_event(inst, "column_change")
        if not ev:
            continue
        installed_at = ev.get("event_date") or ""
        days_ago = None
        try:
            ts = _dt.fromisoformat(installed_at.replace("Z", "+00:00"))
            days_ago = (_dt.now(ts.tzinfo) - ts).days
        except Exception:
            pass
        columns[inst] = {
            "vendor": ev.get("column_vendor") or "",
            "model": ev.get("column_model") or "",
            "serial": ev.get("column_serial") or "",
            "installed_at": installed_at,
            "days_ago": days_ago,
            "notes": ev.get("notes") or "",
        }

    return {
        "date": date,
        "runs": runs,
        "n_runs": len(runs),
        "n_with_tic": n_with_tic,
        "columns": columns,
    }


@app.get("/api/cirt/{instrument}")
async def api_cirt(instrument: str, limit: int = 500) -> dict:
    """Fetch cIRT anchor retention-time history for an instrument.

    Joins the irt_anchor_rts table to runs so the dashboard can chart
    each peptide's observed RT over time with the run metadata it needs
    (run_date, spd, run_name). Grouped per peptide on the server side
    for convenience — the UI just picks an SPD bucket and iterates.
    """
    import sqlite3
    from stan.db import get_db_path

    db_path = get_db_path()
    if not db_path.exists():
        return {"peptides": {}, "n_runs": 0}

    try:
        with sqlite3.connect(str(db_path)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                """
                SELECT r.id AS run_id, r.run_name, r.run_date, r.spd,
                       a.peptide, a.observed_rt_min, a.reference_rt_min
                FROM runs r
                JOIN irt_anchor_rts a ON a.run_id = r.id
                WHERE r.instrument = ?
                ORDER BY r.run_date ASC
                LIMIT ?
                """,
                (instrument, limit * 30),  # x30 because each run has up to 10 anchors
            ).fetchall()
    except sqlite3.OperationalError:
        # Table may not exist yet if user never ran `stan backfill-cirt`
        return {"peptides": {}, "n_runs": 0}

    peptides: dict[str, dict] = {}
    run_ids: set[str] = set()
    for r in rows:
        run_ids.add(r["run_id"])
        p = peptides.setdefault(r["peptide"], {
            "reference_rt_min": r["reference_rt_min"],
            "observations": [],
        })
        p["observations"].append({
            "run_id": r["run_id"],
            "run_name": r["run_name"],
            "run_date": r["run_date"],
            "spd": r["spd"],
            "observed_rt_min": r["observed_rt_min"],
        })
    return {"peptides": peptides, "n_runs": len(run_ids)}


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


# ── Sample Health (rawmeat-based monitor for non-QC files) ──────────

@app.get("/api/sample-health")
async def api_sample_health(
    instrument: str | None = None,
    verdict: str | None = None,
    limit: int = 200,
) -> dict:
    """Return recent Sample Health Monitor rows for the dashboard tab.

    These are non-QC, non-excluded files processed via rawmeat — separate
    from the QC `runs` table and not part of the community benchmark."""
    from stan.db import get_sample_health
    rows = get_sample_health(instrument=instrument, verdict=verdict, limit=limit)
    counts = {"pass": 0, "warn": 0, "fail": 0}
    for r in rows:
        v = r.get("verdict")
        if v in counts:
            counts[v] += 1
    return {"rows": rows, "counts": counts}


# ── Fleet (stan.control) ─────────────────────────────────────────────

@app.get("/api/fleet/hosts")
async def api_fleet_hosts() -> dict:
    """List every host directory on the shared mirror and surface each
    host's most recent status.json for the Fleet tab."""
    import json
    from stan.config import get_hive_mirror_root

    root = get_hive_mirror_root()
    if root is None:
        return {"root": None, "hosts": []}

    hosts = []
    for h in sorted(p for p in root.iterdir() if p.is_dir()):
        entry: dict = {"name": h.name, "status": None, "error": None}
        sp = h / "status.json"
        if sp.exists():
            try:
                entry["status"] = json.loads(sp.read_text(encoding="utf-8"))
            except Exception as e:
                entry["error"] = f"status.json parse error: {e}"
        hosts.append(entry)
    return {"root": str(root), "hosts": hosts}


@app.post("/api/fleet/command")
async def api_fleet_command(body: dict) -> dict:
    """Enqueue a whitelisted command for the named host and return the
    command id. Poll /api/fleet/result/<host>/<id> to see the response."""
    from stan.config import get_hive_mirror_root
    from stan.control import COMMAND_WHITELIST, enqueue_command

    host = body.get("host", "")
    action = body.get("action", "")
    args = body.get("args") or {}
    if not host or not action:
        raise HTTPException(status_code=400, detail="host and action required")
    if action not in COMMAND_WHITELIST:
        raise HTTPException(status_code=400, detail=f"action {action!r} not in whitelist")

    root = get_hive_mirror_root()
    if root is None:
        raise HTTPException(status_code=503, detail="no hive mirror mounted")
    host_dir = root / host
    if not host_dir.exists():
        raise HTTPException(status_code=404, detail=f"no such host: {host}")

    cmd_file = enqueue_command(action, args, mirror_dir=host_dir)
    return {"id": cmd_file.stem, "action": action, "host": host}


@app.get("/api/fleet/result/{host}/{cmd_id}")
async def api_fleet_result(host: str, cmd_id: str) -> dict:
    """Return the result file for the given command, or `pending: true`
    if it hasn't been processed yet. Frontend polls this until the
    action completes."""
    import json
    from stan.config import get_hive_mirror_root

    root = get_hive_mirror_root()
    if root is None:
        raise HTTPException(status_code=503, detail="no hive mirror mounted")
    result_path = root / host / "commands" / "results" / f"{cmd_id}.result.json"
    if not result_path.exists():
        return {"pending": True, "id": cmd_id, "host": host}
    try:
        return json.loads(result_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"result parse error: {e}")


# ── Static frontend ──────────────────────────────────────────────────

_FRONTEND_DIR = Path(__file__).parent / "public"


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    """Serve the dashboard frontend."""
    index_path = _FRONTEND_DIR / "index.html"
    if index_path.exists():
        try:
            return HTMLResponse(index_path.read_text(encoding="utf-8"))
        except Exception as e:
            # Log server-side crashes so they show up in the Hive mirror
            logger.exception("Failed to serve dashboard HTML: %s", e)
            try:
                err_log = get_db_path().parent / "dashboard_errors.log"
                with open(err_log, "a", encoding="utf-8") as f:
                    import traceback
                    f.write(f"SERVER {e}\n{traceback.format_exc()}\n")
            except Exception:
                pass
            raise
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
