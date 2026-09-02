"""FastAPI dashboard backend — serves QC data and instrument config.

Runs on http://localhost:8421. Serves both API routes and the static React frontend.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from stan import __version__
from stan.config import (
    ConfigWatcher,
    load_ui_prefs,
    resolve_config_path,
)
from stan.db import get_db_path, get_run, get_runs, get_tic_trace, get_tic_traces_for_instrument, get_trends, init_db

logger = logging.getLogger(__name__)

app = FastAPI(title="STAN Dashboard", version=__version__)

# v0.2.307: tighten CORS + add Origin gate on state-changing requests.
# Pre-fix the dashboard had `allow_origins=["*"]` and the
# /api/fleet/command endpoint had zero auth, so any drive-by URL the
# operator visited while the dashboard was open could `fetch` POST
# update_stan / apply_config / submit_all and trigger code execution
# on every instrument PC. Defense:
#   1) CORS now allows only the dashboard's own localhost origins.
#   2) A request middleware rejects POST/PUT/DELETE/PATCH whose
#      Origin header points anywhere other than our own origins.
#      Missing Origin is allowed — covers operator CLI clients
#      (curl, requests) which is fine because the listener is
#      already 127.0.0.1-bound, not externally reachable.
_DASHBOARD_ORIGINS_BASE = (
    "http://localhost:8421",
    "http://127.0.0.1:8421",
)

# v0.2.314: env-var escape hatch for Tailscale / cloudflared / LAN
# deployments. When the dashboard is reached at a non-localhost URL
# (e.g. http://lumosrox.tail-foo-bar.ts.net:8421 over Tailscale, or
# https://godmode.stan-proteomics.org behind a Cloudflare tunnel),
# browsers send that URL as the Origin header on POST requests and
# the v0.2.307 gate would otherwise 403 every godmode action.
# Set STAN_DASHBOARD_EXTRA_ORIGINS to a comma-separated list, e.g.
#   set STAN_DASHBOARD_EXTRA_ORIGINS=http://lumosrox.tail-foo-bar.ts.net:8421
# before launching `stan dashboard`. Multiple origins separated by
# commas. No wildcards — explicit allowlist remains the security
# property the gate provides.
import os as _os  # noqa: E402
_extra = [
    o.strip() for o in (_os.environ.get("STAN_DASHBOARD_EXTRA_ORIGINS") or "").split(",")
    if o.strip()
]
_DASHBOARD_ORIGINS = tuple(_DASHBOARD_ORIGINS_BASE) + tuple(_extra)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_DASHBOARD_ORIGINS),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _enforce_origin_on_writes(request, call_next):
    """Reject mutating requests whose Origin isn't our own dashboard.

    Browser CSRF protection. Read-only methods (GET/HEAD/OPTIONS) are
    not gated — the dashboard exposes no patient data and the cohort
    aggregates are read-safe. Mutating methods (POST/PUT/DELETE/PATCH)
    require an Origin header matching one of `_DASHBOARD_ORIGINS`, OR
    no Origin header at all (CLI client on the same machine).
    """
    if request.method in ("POST", "PUT", "DELETE", "PATCH"):
        origin = request.headers.get("origin")
        if origin and origin not in _DASHBOARD_ORIGINS:
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        f"Cross-origin request rejected: {origin!r} is not "
                        f"a STAN dashboard origin."
                    ),
                },
            )
    return await call_next(request)

# Config watchers — hot-reload on API access
_instruments_watcher: ConfigWatcher | None = None
_thresholds_watcher: ConfigWatcher | None = None
_ui_prefs_watcher: ConfigWatcher | None = None


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


def _get_ui_prefs_watcher() -> ConfigWatcher | None:
    """Return a hot-reloading watcher for ``ui_prefs.yml`` if it exists.

    Returns None when the file is missing — the dashboard falls back to
    its built-in defaults in that case, and the ``/api/ui-prefs`` route
    returns a 404.
    """
    global _ui_prefs_watcher
    if _ui_prefs_watcher is None:
        try:
            _ui_prefs_watcher = ConfigWatcher(resolve_config_path("ui_prefs.yml"))
        except FileNotFoundError:
            return None
    elif _ui_prefs_watcher.is_stale():
        _ui_prefs_watcher.reload()
    return _ui_prefs_watcher


# ── Startup ──────────────────────────────────────────────────────────

# How often the dashboard re-pulls central runs, and whether it does at all.
# Set to 0 to turn the mirror off entirely.
PG_REFRESH_SECONDS = int(_os.environ.get("STAN_PG_REFRESH_SECONDS", "300"))


# True once a PG pull has succeeded. Kept distinct from ``use_pg()``: a
# mirror-fed dashboard is central-backed while still reading SQLite, so
# neither flag implies the other.
_PG_BACKED = False


def _pull_from_pg_once() -> int:
    """Copy central PG data into the local SQLite the dashboard reads.

    Returns the number of ``runs`` pulled, or -1 when PG isn't
    configured/reachable. Never raises: a dashboard that can't reach PG
    should still serve whatever it already has.
    """
    try:
        from stan.sync.pg_to_sqlite import pull_from_pg
    except Exception:
        return -1
    global _PG_BACKED
    try:
        written = pull_from_pg()
        _PG_BACKED = True
        logger.info(
            "PG refresh: %s",
            ", ".join(f"{k}={v}" for k, v in written.items()),
        )
        return written.get("runs", 0)
    except Exception as e:  # noqa: BLE001 - never take the dashboard down
        logger.warning("PG refresh skipped: %s", str(e).strip().splitlines()[0][:120])
        return -1


async def _pg_refresh_loop() -> None:
    """Background refresher so the fleet view tracks PG in near-real-time."""
    import asyncio
    while True:
        try:
            n = await asyncio.to_thread(_pull_from_pg_once)
            if n >= 0:
                logger.info("PG refresh: %d runs", n)
        except Exception:
            logger.debug("PG refresh loop error", exc_info=True)
        await asyncio.sleep(max(60, PG_REFRESH_SECONDS))


@app.post("/api/refresh")
async def api_refresh() -> dict:
    """Pull from PG right now (the dashboard's ↻ shortcut).

    A no-op when the mirror is off: the panels that matter read PG on
    every request, so there is nothing to catch up on.
    """
    import asyncio
    if not _mirror_enabled():
        return {"ok": True, "runs": -1, "direct": True}
    n = await asyncio.to_thread(_pull_from_pg_once)
    return {"ok": n >= 0, "runs": n, "direct": False}


def _mirror_enabled() -> bool:
    """Should the SQLite mirror keep running?

    Yes by default, even in PG-direct mode. The panels that carry the
    dashboard -- runs, trends, TIC, PEG, drift, sample health -- no
    longer go through it, but a handful of endpoints still issue raw
    SQLite SQL against the mirrored ``runs`` table and would freeze at
    the last pull without it: /api/warnings, /api/today/tic-overview,
    /api/utilization, /api/fleet/comparison, /api/fleet/instruments,
    plus get_column_lifetime and time_since_last_qc. Port those and
    this can default to off. (/api/cirt was one of them until
    ``stan.db.get_cirt_history`` gave it a PG path.)

    ``STAN_PG_REFRESH_SECONDS=0`` turns it off today for an install that
    doesn't use those views and wants the PG Farm connection slot back.
    """
    return PG_REFRESH_SECONDS > 0


@app.on_event("startup")
async def startup() -> None:
    """Initialize database, then keep it in step with central PG."""
    import asyncio

    from stan.db_pg import use_pg

    # A PG-direct dashboard still touches SQLite for the tables that
    # haven't been ported (maintenance_events, uploads, scan_cache), so
    # the local DB is created either way.
    init_db()
    if use_pg():
        logger.info(
            "reading PG Farm directly; SQLite mirror %s",
            "still refreshing for the un-ported views"
            if _mirror_enabled() else "disabled",
        )
    if _mirror_enabled():
        asyncio.create_task(_pg_refresh_loop())


def _require_store() -> None:
    """404 when there is no store to read.

    Was ``if not get_db_path().exists()`` inline in the PEG/drift
    endpoints. In PG mode a local stan.db is not expected to exist at
    all, so that check turned a perfectly healthy central install into a
    404 -- hence the explicit backend test here.
    """
    from stan.db import get_db_path
    from stan.db_pg import use_pg
    if use_pg():
        return
    if not get_db_path().exists():
        raise HTTPException(status_code=404, detail="database not found")


# ── API Routes ───────────────────────────────────────────────────────

# Read-only gate for public hosting. A no-op unless STAN_DASHBOARD_READONLY
# is set, so a local operator install is unaffected. See
# stan/dashboard/readonly.py for why this exists — in short, server.py's
# Origin middleware assumes a 127.0.0.1 listener, and that assumption is
# false the moment the app is published.
from stan.dashboard.readonly import install_readonly_gate, is_readonly  # noqa: E402

install_readonly_gate(app)


@app.get("/api/capabilities")
async def api_capabilities() -> dict:
    """What this install can do, so the UI hides what doesn't apply.

    A Hive/PG-backed dashboard is a read-only window onto a fleet whose
    searching happens on the cluster; there is no local watcher to configure
    and the instruments.yml it would edit isn't the one in force. Editing it
    there is at best inert and at worst misleading, so the Config tab is
    hidden. Override with STAN_SHOW_CONFIG=1.
    """
    from stan.db_pg import use_pg

    force = (_os.environ.get("STAN_SHOW_CONFIG") or "").strip().lower()
    direct = bool(use_pg())
    central = direct or _PG_BACKED
    show_config = True if force in ("1", "true", "yes") else not central
    return {
        "central_mode": central,
        "show_config": show_config and not is_readonly(),
        "readonly": is_readonly(),
        # "pg" means every read in this response cycle came from PG Farm;
        # "sqlite" means the local file, whether or not a mirror fills it.
        "db_backend": "pg" if direct else "sqlite",
        "pg_direct": direct,
        "mirror_active": _mirror_enabled(),
        "version": __version__,
    }


@app.get("/api/version")
async def api_version() -> dict:
    return {"version": __version__}


@app.get("/api/community/identity")
async def api_community_identity() -> dict:
    """Return this lab's community submission identity for arcade scoring.

    The arcade leaderboard tags every score with the lab's display_name
    so other STAN users can see who holds the high score. Loads from
    the local community.yml, falling back to "anonymous" so games still
    work for labs that haven't claimed a name yet.
    """
    try:
        from stan.config import load_community
        cfg = load_community() or {}
        name = (cfg.get("display_name") or "").strip()
        return {
            "display_name": name or "anonymous",
            "claimed": bool(name),
        }
    except Exception:
        return {"display_name": "anonymous", "claimed": False}


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


@app.get("/api/warnings")
async def api_warnings() -> dict:
    """System-level warnings for the dashboard banner.

    Added v0.2.174. Currently surfaces one warning type:

    - stale_instrument_name: an instrument name has runs in the DB
      (last 60 days) but is NOT present in the loaded instruments.yml.
      This typically means a rename like `data_bruker` → `timsTOF HT`
      left behind orphan runs on the old name, which shows up as two
      separate cards on the homepage. Surfaces the exact
      `stan fix-instrument-names` command to merge them.
    """
    import os as _os
    import sqlite3
    from datetime import datetime, timedelta
    from stan.db import get_db_path

    warnings: list[dict] = []

    # Skip stale-name detection when the dashboard is pointed at a
    # multi-instrument fleet DB via STAN_DB_PATH. The local instruments.yml
    # only knows about THIS host's instruments, so any other instrument
    # appearing in the DB is "fleet view, not orphan rename" — flagging it
    # would suggest catastrophic merges (e.g. "merge Exploris into Lumos").
    # See: STAN-Godmode pattern, v0.2.341+.
    if _os.environ.get("STAN_DB_PATH"):
        return {"warnings": warnings}

    # Pull configured names from instruments.yml.
    watcher = _get_instruments_watcher()
    configured: set[str] = set()
    if watcher is not None:
        for inst in (watcher.data.get("instruments") or []):
            name = (inst or {}).get("name")
            if isinstance(name, str) and name:
                configured.add(name)

    # Pull DB names with recent activity (last 60 days).
    db_path = get_db_path()
    if db_path.exists() and configured:
        cutoff = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
        db_names: dict[str, int] = {}
        with sqlite3.connect(str(db_path)) as con:
            for table in ("runs", "sample_health"):
                try:
                    rows = con.execute(
                        f"SELECT instrument, COUNT(*) FROM {table} "
                        f"WHERE substr(run_date, 1, 10) >= ? AND instrument IS NOT NULL "
                        f"GROUP BY instrument",
                        (cutoff,),
                    ).fetchall()
                except sqlite3.OperationalError:
                    continue
                for name, n in rows:
                    if not name:
                        continue
                    db_names[name] = db_names.get(name, 0) + int(n)

        # Any DB name not in instruments.yml → candidate stale name.
        for name, n in db_names.items():
            if name in configured:
                continue
            # Heuristic: the user almost certainly meant to merge this into
            # one of the configured instruments. We can't know which one
            # for sure, so we list all configured targets in the message.
            targets = sorted(configured)
            warnings.append({
                "kind": "stale_instrument_name",
                "severity": "warn",
                "stale_name": name,
                "recent_run_count": n,
                "suggested_targets": targets,
                "message": (
                    f"Instrument name \"{name}\" has {n} run(s) in the last 60 days "
                    f"but is NOT in your instruments.yml. This usually means the "
                    f"instrument was renamed and left orphan runs behind — they will "
                    f"appear as a second card on the homepage. Merge with: "
                    f"stan fix-instrument-names --from \"{name}\" --to \"<new_name>\""
                ),
            })

    return {"warnings": warnings}


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


_BLANK_PATTERN = None  # lazy-compiled below
def _classify_run_class(run_name: str) -> str:
    """Classify a row by filename: qc | blank | sample.

    Mirrors the watcher's pattern surfaces but applied at API time so
    we don't need a runs.run_class column. QC = matches stan's QC
    regex (hel[a5] | qc | std_he). Blank = matches the watcher
    exclude pattern below. Everything else = sample.

    Blank pattern expanded 2026-04-23 to catch Brett's `_wa_` wash
    convention on Lumos (FLapr26_wa_20260422...raw). `wa` alone is
    too risky (matches `wave`, `work`); `_wa_` / `_wa.` / `-wa-` /
    trailing `_wa` at end-of-name are the safe anchored forms.
    Common other aliases included: rinse, buffer, solvent, empty.
    """
    import re as _re
    global _BLANK_PATTERN
    if _BLANK_PATTERN is None:
        _BLANK_PATTERN = _re.compile(
            r"(?i)("
            r"wash|blank|blnk|blk|rinse|buffer|solvent|empty|"
            r"[_\-]wa[_\-]|[_\-]wa$"
            r")"
        )
    from stan.watcher.qc_filter import compile_qc_pattern
    name = (run_name or "").rsplit(".", 1)[0]
    if compile_qc_pattern().search(name):
        return "qc"
    if _BLANK_PATTERN.search(name):
        return "blank"
    return "sample"


@app.get("/api/today/tic-overview")
async def api_today_tic_overview(
    date: str | None = None,
    days: int = 7,
    instrument: str | None = None,
) -> dict:
    """Return recent runs + their TIC traces in three faceted buckets.

    Buckets: qc (from `runs` joined to `tic_traces`), sample and blank
    (from `sample_health` joined to `health_tic_traces`, classified by
    filename). Powers the This Week's QCs tab's three-panel TIC overlay.

    v0.2.157: default window changed from today-only to last 7 days so
    the card isn't empty when the operator hasn't run an acquisition
    yet today. Pass ``date=YYYY-MM-DD&days=1`` for the legacy today-
    only behavior.

    Args:
        date: ISO date YYYY-MM-DD. Defaults to today (local time) and
            is interpreted as the END of the window.
        days: How many days back from ``date`` to include (default 7).
            ``days=1`` gives today-only (legacy behavior).
        instrument: Optional name filter.

    Returns:
        {
          "date": "2026-04-20",
          "runs": [...],          # legacy flat list (QC only) for compat
          "facets": {              # new faceted shape — preferred
            "qc": [run, ...],
            "sample": [run, ...],
            "blank": [run, ...]
          },
          "n_runs": int,
          "n_with_tic": int,
          "columns": {instrument: {vendor, model, ...}}
        }
        Each run object has run_id / run_name / instrument / mode /
        run_date / ips_score / gate_result / spd / n_precursors /
        n_psms / time_of_day_rank / has_tic / tic / cirt_markers /
        run_class / source ("runs" or "sample_health").
    """
    import json as _json
    import sqlite3
    from datetime import datetime, timedelta
    from stan.db import get_db_path

    db_path = get_db_path()
    if not db_path.exists():
        return {"date": date, "runs": [], "n_runs": 0, "n_with_tic": 0}

    # Default to local "today" - the dashboard runs on the instrument
    # PC, so the operator thinks in local time.
    if date is None:
        date = datetime.now().strftime("%Y-%m-%d")

    # v0.2.157: window is [end_date - days + 1, end_date] inclusive.
    # days=7 with today = Mon through Sun if today is Sun. days=1
    # reproduces the legacy today-only behavior.
    try:
        end_dt = datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        end_dt = datetime.now()
    start_dt = end_dt - timedelta(days=max(1, days) - 1)
    start_prefix = start_dt.strftime("%Y-%m-%d")
    end_prefix = end_dt.strftime("%Y-%m-%d")

    # SQLite comparison: run_date is ISO with 'T' separator. Match by
    # date prefix (10-char) so timezone suffixes don't trip us up.
    where = ["substr(r.run_date, 1, 10) >= ?",
             "substr(r.run_date, 1, 10) <= ?",
             "(r.hidden IS NULL OR r.hidden = 0)"]
    params: list = [start_prefix, end_prefix]
    if instrument:
        where.append("r.instrument = ?")
        params.append(instrument)
    sql = (
        "SELECT r.id AS run_id, r.run_name, r.instrument, r.mode, "
        "       r.run_date, r.ips_score, r.gate_result, r.spd, "
        "       r.gradient_length_min, r.n_precursors, r.n_psms, "
        "       r.diagnosis, r.amount_ng, "
        "       t.rt_min AS tic_rt, t.intensity AS tic_intensity, t.bp_intensity AS tic_bp "
        "FROM runs r "
        "LEFT JOIN tic_traces t ON t.run_id = r.id "
        "WHERE " + " AND ".join(where) +
        " ORDER BY r.run_date ASC"
    )

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, params).fetchall()

        # Same join, against sample_health for non-QC files. The
        # rawmeat monitor pipeline writes here; v0.2.132 added the
        # health_tic_traces sibling table populated by the watcher.
        # Older sample_health rows have no TIC — they still appear
        # in the facet but render with no line, just metadata.
        sh_where = ["substr(s.run_date, 1, 10) >= ?",
                    "substr(s.run_date, 1, 10) <= ?"]
        sh_params: list = [start_prefix, end_prefix]
        if instrument:
            sh_where.append("s.instrument = ?")
            sh_params.append(instrument)
        sh_sql = (
            "SELECT s.id AS run_id, s.run_name, s.instrument, "
            "       s.run_date, s.verdict AS gate_result, "
            "       s.dynamic_range_log10, s.ms1_total_tic, "
            "       t.rt_min AS tic_rt, t.intensity AS tic_intensity, t.bp_intensity AS tic_bp "
            "FROM sample_health s "
            "LEFT JOIN health_tic_traces t ON t.health_id = s.id "
            "WHERE " + " AND ".join(sh_where) +
            " ORDER BY s.run_date ASC"
        )
        try:
            sh_rows = con.execute(sh_sql, sh_params).fetchall()
        except sqlite3.OperationalError:
            # health_tic_traces may not exist on older DBs that haven't
            # migrated yet — gracefully degrade to no Sample/Blank facets.
            sh_rows = []

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
        tic_bp = d.pop("tic_bp", None)
        has_tic = tic_rt is not None and tic_int is not None
        tic_payload = None
        if has_tic:
            try:
                tic_payload = {
                    "rt_min": _json.loads(tic_rt),
                    "intensity": _json.loads(tic_int),
                }
                # v0.2.300: per-frame base peak chromatogram (Bruker only
                # for now — Thermo path doesn't populate it yet). Frontend
                # toggles the rendered y-array between intensity and
                # bp_intensity at draw time.
                if tic_bp:
                    try:
                        tic_payload["bp_intensity"] = _json.loads(tic_bp)
                    except Exception:
                        pass
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
        d["source"] = "runs"
        d["run_class"] = _classify_run_class(d.get("run_name", ""))

        runs.append(d)

    # Process sample_health rows the same way (no cIRT markers — those
    # only exist for QC runs that went through DIA-NN).
    sh_runs: list[dict] = []
    sh_with_tic = 0
    for r in sh_rows:
        d = dict(r)
        tic_rt = d.pop("tic_rt", None)
        tic_int = d.pop("tic_intensity", None)
        tic_bp = d.pop("tic_bp", None)
        has_tic = tic_rt is not None and tic_int is not None
        tic_payload = None
        if has_tic:
            try:
                tic_payload = {
                    "rt_min": _json.loads(tic_rt),
                    "intensity": _json.loads(tic_int),
                }
                if tic_bp:
                    try:
                        tic_payload["bp_intensity"] = _json.loads(tic_bp)
                    except Exception:
                        pass
                sh_with_tic += 1
            except Exception:
                has_tic = False
        d["has_tic"] = has_tic
        d["tic"] = tic_payload
        # Stub fields the QC schema has so the UI's Sparkline component
        # doesn't choke on missing keys.
        d.setdefault("mode", None)
        d.setdefault("ips_score", None)
        d.setdefault("spd", None)
        d.setdefault("n_precursors", None)
        d.setdefault("n_psms", None)
        d["cirt_markers"] = []
        d["source"] = "sample_health"
        d["run_class"] = _classify_run_class(d.get("run_name", ""))
        sh_runs.append(d)

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

    # Bucket every row (QC + sample_health) into qc / sample / blank
    # by run_class. time_of_day_rank is recomputed PER FACET so each
    # panel's color ramp goes light → dark within its own runs (a
    # single global rank would make a panel with one early run get
    # the lightest color regardless of its actual time-of-day).
    facets: dict[str, list[dict]] = {"qc": [], "sample": [], "blank": []}
    for d in runs + sh_runs:
        cls = d.get("run_class") or "sample"
        if cls not in facets:
            cls = "sample"
        facets[cls].append(d)
    # Sort each facet by run_date and assign per-facet rank for color
    for cls, items in facets.items():
        items.sort(key=lambda x: x.get("run_date") or "")
        for i, item in enumerate(items):
            item["time_of_day_rank"] = i

    return {
        "date": date,
        # Legacy: flat QC list, kept so old UI code keeps working until
        # the next dashboard reload picks up the new `facets` shape.
        "runs": [r for r in facets["qc"]],
        "facets": facets,
        "n_runs": len(runs) + len(sh_runs),
        "n_with_tic": n_with_tic + sh_with_tic,
        "columns": columns,
    }


@app.get("/api/cirt/{instrument}")
async def api_cirt(instrument: str, limit: int = 500) -> dict:
    """Fetch cIRT anchor retention-time history for an instrument.

    Joins the irt_anchor_rts table to runs so the dashboard can chart
    each peptide's observed RT over time with the run metadata it needs
    (run_date, spd, run_name). Grouped per peptide on the server side
    for convenience — the UI just picks an SPD bucket and iterates.

    Backend-agnostic: ``get_cirt_history`` answers from PG when
    ``use_pg()``, SQLite otherwise. Reading sqlite3 directly here meant
    the panel stayed empty on the Hive/PG dashboard even with anchors
    populated centrally.
    """
    from stan.db import get_cirt_history

    # x30 because each run contributes up to a full 10-anchor panel and
    # `limit` is expressed in runs.
    rows = get_cirt_history(instrument, limit=limit * 30)

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


@app.get("/api/runs/{run_id}/peg")
async def api_run_peg(run_id: str, source: str = "runs") -> dict:
    """Return the per-ion PEG breakdown for a run.

    Powers the dashboard's PEG lollipop chart. Includes the summary
    scalar fields alongside the per-ion hits so the frontend doesn't
    need a second request for the badge metadata.

    Args:
        run_id: runs.id or sample_health.id depending on ``source``.
        source: "runs" or "sample_health" — which parent table the
            run_id belongs to. Defaults to "runs" (QC rows).
    """
    from stan.db import get_detail_summary, get_peg_ion_hits

    if source not in ("runs", "sample_health"):
        raise HTTPException(status_code=400, detail="source must be 'runs' or 'sample_health'")

    _require_store()

    hits = get_peg_ion_hits(run_id=run_id, table=source)

    # Pull the summary fields from the parent row so the UI can show
    # "score 62 — 14 ions detected" alongside the chart.
    summary = get_detail_summary(run_id, source, (
        "run_name", "peg_score", "peg_n_ions_detected",
        "peg_intensity_pct", "peg_class",
    ))

    return {"run_id": run_id, "source": source, "summary": summary, "hits": hits}


@app.get("/api/runs/{run_id}/drift")
async def api_run_drift(run_id: str, source: str = "runs") -> dict:
    """Return the per-window DIA drift breakdown for a run.

    Powers the dashboard's drift scatter chart. See ``api_run_peg`` for
    the ``source`` parameter semantics.
    """
    from stan.db import get_detail_summary, get_drift_window_centroids

    if source not in ("runs", "sample_health"):
        raise HTTPException(status_code=400, detail="source must be 'runs' or 'sample_health'")

    _require_store()

    windows = get_drift_window_centroids(run_id=run_id, table=source)

    summary = get_detail_summary(run_id, source, (
        "run_name", "drift_coverage", "drift_median_im",
        "drift_p90_abs_im", "drift_class",
    ))

    return {"run_id": run_id, "source": source, "summary": summary, "windows": windows}


@app.get("/api/runs/{run_id}/drift-cloud")
async def api_run_drift_cloud(run_id: str, source: str = "runs") -> dict:
    """Return the downsampled MS1 peak cloud for the Bruker-DataAnalysis-
    style drift visualization. {mz, im, log_intensity, n_points}.
    v0.2.173+."""
    from stan.db import get_drift_peak_cloud
    if source not in ("runs", "sample_health"):
        raise HTTPException(status_code=400,
                            detail="source must be 'runs' or 'sample_health'")
    cloud = get_drift_peak_cloud(run_id=run_id, table=source)
    if cloud is None:
        return {"run_id": run_id, "source": source, "cloud": None}
    return {"run_id": run_id, "source": source, "cloud": cloud}


def _locate_features_file(raw_path: str | None) -> Path | None:
    """Find the .features SQLite next to a raw .d directory.

    Mirrors ``stan.metrics.features.find_features_file`` but implemented
    inline to avoid importing from that module — a parallel worker is
    actively editing it, and we want zero coupling.

    v0.2.203 fix: 4DFF's actual output name is ``<d_full_name>.features``
    (the ``.d`` suffix is PRESERVED before ``.features``, e.g.
    ``foo.d/foo.d.features``). Earlier revisions used ``d.stem`` which
    strips the ``.d`` and produced a guaranteed miss — the dashboard
    then surfaced the misleading "no .features file found" banner even
    when 4DFF had successfully written the file. Try both name forms
    plus parent-directory variants so all 4DFF placements are covered.

    Returns None if raw_path is empty, the .d doesn't exist, or no
    .features file can be located.
    """
    if not raw_path:
        return None
    d = Path(raw_path)
    if not d.exists():
        return None
    full = d.name   # "foo.d"
    stem = d.stem   # "foo"
    candidates = [
        d / f"{full}.features",          # 4DFF current: foo.d/foo.d.features
        d / f"{stem}.features",          # legacy:       foo.d/foo.features
        d.parent / f"{full}.features",   # sibling variant
        d.parent / f"{stem}.features",   # Ziggy-style sibling
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _stored_feature_cloud(run_id: str, source: str) -> dict | None:
    """Build the features-by-charge payload from the ``feature_clouds`` table.

    Returns None when nothing is stored for this run (or the table
    predates the migration), so the caller can fall through to the
    on-disk ``.features`` sidecar.
    """
    try:
        from stan.db import get_feature_cloud
        cloud = get_feature_cloud(run_id=run_id, table=source)
    except Exception:  # noqa: BLE001 - never take the drift modal down
        logger.debug("stored feature cloud lookup failed", exc_info=True)
        return None
    if not cloud or not cloud.get("mz"):
        return None

    mz = cloud["mz"]
    mob = cloud["mobility"]
    rt = cloud["rt"]
    charge = cloud["charge"]
    inten = cloud["intensity"]

    by_charge: dict[str, dict[str, list]] = {}
    for i, z in enumerate(charge):
        b = by_charge.setdefault(
            str(int(z)), {"mz": [], "mobility": [], "rt": [], "intensity": []}
        )
        b["mz"].append(mz[i])
        b["mobility"].append(mob[i])
        b["rt"].append(rt[i])
        b["intensity"].append(inten[i])

    return {
        "run_id": run_id,
        "source": source,
        "has_features": True,
        "from_store": True,
        "features_path": cloud.get("features_path", ""),
        "n_features": len(mz),
        "n_total": cloud.get("n_total") or len(mz),
        "by_charge": by_charge,
        "mz_range": [round(min(mz), 2), round(max(mz), 2)],
        "mobility_range": [round(min(mob), 4), round(max(mob), 4)],
    }


@app.get("/api/runs/{run_id}/features-by-charge")
async def api_run_features_by_charge(run_id: str, source: str = "runs") -> dict:
    """Return per-charge MS1 feature points from a 4DFF .features file.

    Powers the Ziggy-style Plotly scatter in the dashboard: one trace
    per charge state, so +1 contamination and +2/+3 peptides can be
    toggled independently. Reads the .features SQLite (LcTimsMsFeature
    table) that 4DFF writes next to the Bruker .d. Falls back to
    ``{"has_features": false, "reason": ...}`` when no file exists so
    the frontend can show a friendly "run ``stan run-4dff`` first"
    message and revert to the legacy SVG cloud.

    Caps at 50,000 features — anything larger is uniformly downsampled
    to keep the browser responsive.
    """
    import sqlite3 as _sqlite

    if source not in ("runs", "sample_health"):
        raise HTTPException(
            status_code=400, detail="source must be 'runs' or 'sample_health'"
        )

    db_path = get_db_path()
    if not db_path.exists():
        raise HTTPException(status_code=404, detail="database not found")

    # Pull raw_path + run_name for this row. Both runs and sample_health
    # carry a raw_path column so the lookup is symmetric.
    raw_path: str | None = None
    run_name: str | None = None
    with _sqlite.connect(str(db_path)) as con:
        con.row_factory = _sqlite.Row
        row = con.execute(
            f"SELECT run_name, raw_path FROM {source} WHERE id = ?",
            (run_id,),
        ).fetchone()
        if row is not None:
            raw_path = row["raw_path"]
            run_name = row["run_name"]

    # v1.0.16: the stored cloud is tried before the sidecar. The
    # dashboard is a SQLite reader that syncs from PG and generally runs
    # nowhere near the raw data (Brett's Mac; the .d lives on the
    # Flinders NFS export, visible only from Hive), so the
    # file-on-local-disk path is the exception, not the rule. Serving
    # from the DB first also keeps the view identical everywhere instead
    # of "works on the box with the mount".
    stored = _stored_feature_cloud(run_id, source)
    if stored is not None:
        stored["run_name"] = run_name
        return stored

    if raw_path is None:
        return {
            "run_id": run_id,
            "source": source,
            "has_features": False,
            "reason": f"run {run_id} not found in {source} table",
        }

    feat_path = _locate_features_file(raw_path)
    if feat_path is None:
        return {
            "run_id": run_id,
            "source": source,
            "run_name": run_name,
            "has_features": False,
            "reason": (
                "no charge-labeled ion cloud stored for this run, and no "
                ".features file is reachable from this host — run "
                f"`stan run-4dff {raw_path}` on the machine that can see "
                "the raw data, then `stan backfill-feature-cloud` there to "
                "publish it to every dashboard"
            ),
        }

    # Read the LcTimsMsFeature table. Use a fresh sqlite3 connection —
    # do NOT import from stan.metrics.features (race risk).
    try:
        fcon = _sqlite.connect(str(feat_path))
        fcon.row_factory = _sqlite.Row
        total = fcon.execute(
            "SELECT COUNT(*) FROM LcTimsMsFeature WHERE Intensity > 0"
        ).fetchone()[0]
        # Uniform sampling via ROWID modulo when the table is huge.
        # Use ceil-style division so 60_000 / 50_000 → step=2 (not 1),
        # guaranteeing we actually drop below the cap.
        cap = 50_000
        if total > cap:
            step = max(2, (total + cap - 1) // cap)
            cursor = fcon.execute(
                "SELECT MZ, Charge, RT, Mobility, Intensity "
                "FROM LcTimsMsFeature "
                "WHERE Intensity > 0 AND (rowid % ?) = 0",
                (step,),
            )
        else:
            cursor = fcon.execute(
                "SELECT MZ, Charge, RT, Mobility, Intensity "
                "FROM LcTimsMsFeature WHERE Intensity > 0"
            )
        by_charge: dict[str, dict[str, list[float]]] = {}
        mz_min = float("inf")
        mz_max = float("-inf")
        im_min = float("inf")
        im_max = float("-inf")
        n_kept = 0
        for r in cursor:
            mz = float(r["MZ"] or 0.0)
            z = int(r["Charge"] or 0)
            rt = float(r["RT"] or 0.0)
            im = float(r["Mobility"] or 0.0)
            inten = float(r["Intensity"] or 0.0)
            if mz <= 0 or im <= 0:
                continue
            key = str(z)
            bucket = by_charge.setdefault(
                key, {"mz": [], "mobility": [], "rt": [], "intensity": []}
            )
            bucket["mz"].append(round(mz, 4))
            bucket["mobility"].append(round(im, 5))
            bucket["rt"].append(round(rt, 2))
            bucket["intensity"].append(round(inten, 1))
            if mz < mz_min:
                mz_min = mz
            if mz > mz_max:
                mz_max = mz
            if im < im_min:
                im_min = im
            if im > im_max:
                im_max = im
            n_kept += 1
        fcon.close()
    except _sqlite.Error as exc:
        raise HTTPException(
            status_code=500,
            detail=f"failed to read .features file: {exc}",
        ) from exc

    if n_kept == 0:
        return {
            "run_id": run_id,
            "source": source,
            "run_name": run_name,
            "has_features": False,
            "reason": ".features file contains no usable rows",
        }

    return {
        "run_id": run_id,
        "source": source,
        "run_name": run_name,
        "has_features": True,
        "features_path": str(feat_path),
        "n_features": n_kept,
        "n_total": total,
        "by_charge": by_charge,
        "mz_range": [round(mz_min, 2), round(mz_max, 2)],
        "mobility_range": [round(im_min, 4), round(im_max, 4)],
    }


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
        _data = yaml.safe_load(body.yaml_content)
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")

    config_path = resolve_config_path("thresholds.yml")
    config_path.write_text(body.yaml_content)

    watcher = _get_thresholds_watcher()
    watcher.reload()

    return {"status": "ok"}


@app.get("/api/ui-prefs")
async def api_ui_prefs() -> dict:
    """Lab-wide UI preference defaults from ``ui_prefs.yml``.

    This endpoint serves the fallback defaults when a user's browser
    ``localStorage`` is empty. Per-user selection always wins; the YAML
    exists so a PI can set a sensible starting view for everyone in the
    lab without asking each operator to click through settings.

    Returns a JSON object with only the whitelisted keys (see
    ``UI_PREF_KEYS`` in ``stan.config``). Responds 404 when the file
    doesn't exist — the frontend treats that as "use built-in defaults".
    """
    watcher = _get_ui_prefs_watcher()
    if watcher is None:
        raise HTTPException(status_code=404, detail="ui_prefs.yml not configured")
    # Run the result through load_ui_prefs's whitelist via re-reading
    # the file, so unknown keys get filtered consistently.
    return load_ui_prefs()


@app.get("/api/instruments/{instrument}/events")
async def api_events(instrument: str, limit: int = 50) -> list[dict]:
    """Fetch maintenance events for an instrument."""
    from stan.db import get_events
    return get_events(instrument=instrument, limit=limit)


class LogEventRequest(BaseModel):
    event_type: str
    event_date: str | None = None
    #: Downtime is an interval, not an instant. Set for event_type
    #: "downtime"; left null for point events like a source clean.
    end_date: str | None = None
    notes: str = Field(default="", max_length=2000)
    operator: str = Field(default="", max_length=80)
    column_vendor: str | None = Field(default=None, max_length=80)
    column_model: str | None = Field(default=None, max_length=120)
    column_serial: str | None = Field(default=None, max_length=120)
    #: Opt-in per entry. Maintenance notes can name people and customers, so
    #: nothing reaches the community reliability leaderboard unless someone
    #: deliberately ticks this. Defaults off.
    share_community: bool = False


@app.post("/api/instruments/{instrument}/events")
async def api_log_event(
    instrument: str, body: LogEventRequest, request: Request
) -> dict:
    """Log a maintenance event from the dashboard UI.

    On the hosted dashboard this route is reachable only by a signed-in,
    allow-listed operator (see readonly._PRIVILEGED_PATTERNS), and the
    authenticated identity is recorded on the row. These entries drive
    LC-column age and the planned reliability leaderboard, so an unsigned
    entry that nobody can trace is worse than no entry.
    """
    from stan.dashboard.auth import caller_email
    from stan.db import EVENT_TYPES, log_event
    if body.event_type not in EVENT_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid event type. Valid: {EVENT_TYPES}")
    # Compare on the calendar-day prefix only. event_date is TEXT with mixed
    # shapes in the wild -- the older Trends form writes "2026-08-10T12:00:00Z"
    # while a date input yields a bare "2026-08-10" -- so a raw string compare
    # rejects a same-day span ("2026-08-10" < "2026-08-10T12:00:00Z" is True).
    if body.end_date and str(body.end_date)[:10] < str(body.event_date or "")[:10]:
        raise HTTPException(status_code=400, detail="end_date is before the start date")
    event_id = log_event(
        instrument=instrument,
        event_type=body.event_type,
        event_date=body.event_date,
        end_date=body.end_date,
        notes=body.notes,
        operator=body.operator,
        column_vendor=body.column_vendor,
        column_model=body.column_model,
        column_serial=body.column_serial,
        created_by=caller_email(request),
        share_community=body.share_community,
    )
    return {"event_id": event_id, "status": "logged"}


@app.get("/api/ht/submission")
async def api_ht_submission(
    request: Request, q: str, instrument: str = "timsTOF HT",
    metric: str = "ms1_total_tic", token: str | None = None,
) -> dict:
    """Sample health for one high-throughput submission, plus its outliers.

    `q` is matched as a substring of the run filename, because that is where
    the submission lives: `08132026__60SPD_DIA-SK-10_S3-E7_1_23686.d` is
    sample 10 of submission SK. Matching the filename is what the operator
    actually knows, and it needs no submission table STAN does not have.

    Reads the sample health the monitor pipeline has already computed -- it
    does not recompute anything. New acquisitions are linked and monitored
    automatically within about five minutes, so a submission run today is
    normally complete by the time anyone asks about it; a sample that has
    not been monitored yet simply is not here, which `n_samples` makes
    visible rather than silently under-reporting.
    """
    from stan.db import get_sample_health
    from stan.metrics.ht_outliers import (
        MIN_EFFECT, analyse_submission,
    )

    q = (q or "").strip()
    if len(q) < 2:
        raise HTTPException(
            status_code=400,
            detail="Enter at least 2 characters of the submission number.")

    # Access. On a public host this data is gated (it carries a customer's
    # submission number and sample names). A collaborator reaches their own
    # plate with a share link instead of an account, and the token is checked
    # against THIS submission -- editing the number in the URL gets them
    # nothing, which is the usual way secret-link schemes leak.
    from stan.dashboard.auth import is_privileged
    from stan.dashboard.ht_share import make_token, verify_token
    shared = bool(token) and verify_token(q, token)
    if is_readonly() and not shared and not is_privileged(request):
        from stan.dashboard.readonly import LOGIN_URL
        raise HTTPException(
            status_code=403,
            detail={"message": "High-throughput submission data requires a "
                               "sign-in or a share link for this submission.",
                    "login_url": LOGIN_URL})

    try:
        all_rows = get_sample_health(limit=20000) or []
    except Exception:
        logger.warning("HT: could not read sample health", exc_info=True)
        raise HTTPException(status_code=503, detail="Sample health unavailable")

    inst = (instrument or "").strip().lower()
    all_rows = [r for r in all_rows
                if not inst or inst in str(r.get("instrument") or "").lower()]

    # The HeLa standards dropped into the plate are QC runs, so they live in
    # `runs` with an identification count, not in sample_health. They are the
    # control series: identical material, so a trend across them is the
    # instrument rather than the samples. Coloured by precursors because that
    # is what a standard is for -- TIC would say nothing about whether the
    # instrument is still identifying peptides.
    qc_rows: list[dict] = []
    try:
        from stan.db import get_runs
        qc_rows = [r for r in (get_runs(limit=20000) or [])
                   if not inst or inst in str(r.get("instrument") or "").lower()]
    except Exception:
        logger.warning("HT: could not read QC runs", exc_info=True)

    # One analysis path, shared with the email watcher. Building the view
    # here instead is how the endpoint quietly missed per-tray scoring: the
    # screen and the alert must never disagree about whether a plate is in
    # trouble.
    result = analyse_submission(q, all_rows, qc_rows, metric=metric)
    result["instrument"] = instrument
    result["min_effect"] = MIN_EFFECT
    result["shared_view"] = shared
    # Only an operator is handed the link to give out; a collaborator viewing
    # via a share link is not shown the token that would let them mint others.
    if not shared:
        result["share_token"] = make_token(q)
    return result


# The HyStar SampleTable format, taken from the Core's exported queues. HyStar
# fills a fresh plate column-major (A1,B1..H1,A2..); the acquisition software
# appends _S<tray>-<well>_1_<counter> itself, so the Sample ID stops at the
# sample name.
_QUEUE_COLUMNS = [
    "CheckToRun", "Vial", "Sample ID", "Separation Method", "MS Method",
    "Status", "Volume [µl]", "Data Path", "Result Path", "Sample Comment",
    "Start Date", "End Date",
]
_QUEUE_SEP_METHOD = r"D:\Methods\EvoSepLCmeth\100spd.m?HyStar_LC"
_QUEUE_MS_METHOD = (r"D:\Methods\MSmeth\ela\wBPS_11Ian24"
                    r"\DIA_11x3-k07t13Ra85.m?OtofImpacTEMControl")
_QUEUE_DATA_PATH = r"D:\Data"
_QUEUE_RUN_RE = re.compile(
    r"^(?P<date>\d{8})_(?:(?P<sub>\d{2,4})_)?(?P<method>[^_]+)_"
    r"(?P<samp>.+?)_S\d+-[A-H]\d{1,2}_\d+_\d+")


@app.get("/api/ht/rerun-queue")
async def api_ht_rerun_queue(
    request: Request, q: str, instrument: str = "timsTOF HT",
    token: str | None = None, run_date: str | None = None,
):
    """A HyStar SampleTable (.xlsx) for this submission's flagged wells.

    The same list the "Needs re-run" table shows, exported in the format the
    instrument loads directly -- one row per flagged well, filled onto a fresh
    plate column-major. Streamed as a download; nothing is written server-side.
    """
    import datetime as _dt
    import io as _io
    from fastapi.responses import StreamingResponse
    try:
        import openpyxl
        from openpyxl.styles import Font
    except ImportError:
        raise HTTPException(status_code=503, detail="xlsx export unavailable")

    from stan.dashboard.auth import is_privileged
    from stan.dashboard.ht_share import verify_token
    from stan.db import get_sample_health
    from stan.metrics.ht_outliers import analyse_submission, parse_well

    q = (q or "").strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="submission too short")
    shared = bool(token) and verify_token(q, token)
    if is_readonly() and not shared and not is_privileged(request):
        raise HTTPException(status_code=403, detail="sign-in or share link required")

    all_rows = get_sample_health(limit=20000) or []
    inst = (instrument or "").strip().lower()
    qc_rows = [r for r in (get_sample_health(limit=20000) or [])
               if not inst or inst in str(r.get("instrument") or "").lower()]
    result = analyse_submission(q, all_rows, qc_rows)
    flagged = [r for r in result.get("needs_rerun", [])
               if parse_well(r.get("run_name"))]
    flagged.sort(key=lambda r: (lambda w: (w["plate"], w["row"], w["col"]))(
        parse_well(r["run_name"])))

    rd = (run_date or _dt.date.today().strftime("%Y%m%d"))
    rows = ROWS = "ABCDEFGH"
    slots = [f"S1-{r}{c}" for c in range(1, 13) for r in rows]

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "SampleTable"
    ws.append(_QUEUE_COLUMNS)
    for i, r in enumerate(flagged):
        if i >= len(slots):
            break
        name = r.get("run_name") or ""
        m = _QUEUE_RUN_RE.match(name)
        if m:
            sub = m.group("sub") or (str(int(q)) if q.isdigit() else q)
            sid = f"{rd}_{sub}_{m.group('method')}_{m.group('samp')}"
        else:
            sid = name.removesuffix(".d")
        w = parse_well(name)
        ws.append(["True", slots[i], sid, _QUEUE_SEP_METHOD, _QUEUE_MS_METHOD,
                   None, 0, _QUEUE_DATA_PATH, None,
                   f"rerun of {w['plate']}-{w['row']}{w['col']}", None, None])
    f = Font(name="Tahoma", size=10)
    for row in ws.iter_rows():
        for c in row:
            c.font = f
    for col, width in (("C", 32.86), ("D", 33.57), ("E", 67.71), ("F", 23.71)):
        ws.column_dimensions[col].width = width

    buf = _io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = f"rerun_queue_{(q or 'submission')}_{rd}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.get("/api/ht/fran-link")
async def api_ht_fran_link(q: str) -> dict:
    """Where this submission lives in FRAN.

    A link, not a proxy. FRAN already holds the searches for a submission --
    engine, organism, precursor and protein counts -- keyed on the same
    number that appears in the raw filenames, and it applies its own
    authorization to them. Fetching that here would mean copying both the
    data and the access rules, and the copy is what goes stale.

    Gated with the rest of /api/ht on a public host. It leaks nothing --
    the URL is built from the query string alone -- so an exception could be
    carved out, but adding one to a security allow-list for a route nothing
    calls is the wrong trade: the HT tab builds this link client-side.
    Whoever follows it still meets FRAN's own authorization.
    """
    from stan.db import fran_submission_url

    q = (q or "").strip()
    return {"submission": q, "url": fran_submission_url(q)}


@app.get("/api/ht/manifest")
async def api_ht_manifest(
    request: Request, q: str, include: str = "samples",
    instrument: str = "timsTOF HT", token: str | None = None,
) -> dict:
    """Raw files belonging to a submission, for an external search tool.

    Same access rules as the rest of /api/ht -- a signed-in operator, or a
    share link for this submission. The list carries customer sample names
    and resolved paths, so it is not public.
    """
    from stan.dashboard.auth import is_privileged
    from stan.dashboard.ht_share import verify_token
    from stan.db import get_runs, get_sample_health
    from stan.metrics.ht_manifest import INCLUDE_CHOICES, build_manifest

    q = (q or "").strip()
    if len(q) < 2:
        raise HTTPException(status_code=400, detail="Submission is too short.")
    if include not in INCLUDE_CHOICES:
        raise HTTPException(
            status_code=400,
            detail=f"include must be one of {list(INCLUDE_CHOICES)}")

    shared = bool(token) and verify_token(q, token)
    if is_readonly() and not shared and not is_privileged(request):
        from stan.dashboard.readonly import LOGIN_URL
        raise HTTPException(
            status_code=403,
            detail={"message": "High-throughput data requires a sign-in or a "
                               "share link for this submission.",
                    "login_url": LOGIN_URL})

    inst = (instrument or "").strip().lower()
    health = [r for r in (get_sample_health(limit=20000) or [])
              if not inst or inst in str(r.get("instrument") or "").lower()]
    qc = [r for r in (get_runs(limit=20000) or [])
          if not inst or inst in str(r.get("instrument") or "").lower()]
    return build_manifest(q, health, qc, include=include)


@app.get("/api/maintenance/calendar")
async def api_maintenance_calendar(days: int = 90) -> dict:
    """Every instrument's maintenance and downtime over a window.

    One call for the whole fleet so the calendar does not fan out per
    instrument. Downtime carries an end_date and is rendered as a span;
    everything else is a point event.
    """
    from stan.db import DOWNTIME_EVENT_TYPES, get_events
    from datetime import datetime, timedelta, timezone

    days = max(1, min(int(days), 730))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    out = []
    for ev in get_events(limit=5000) or []:
        d = dict(ev)
        start = str(d.get("event_date") or "")
        # Compare on the date prefix: event_date is TEXT with mixed formats
        # (with/without timezone), so only YYYY-MM-DD is reliably orderable.
        if start[:10] < cutoff[:10]:
            continue
        d["is_downtime"] = d.get("event_type") in DOWNTIME_EVENT_TYPES
        out.append(d)
    out.sort(key=lambda e: str(e.get("event_date") or ""), reverse=True)
    return {"days": days, "events": out,
            "downtime_types": sorted(DOWNTIME_EVENT_TYPES)}


@app.get("/api/maintenance/bruker")
async def api_maintenance_bruker() -> dict:
    """Bruker timsTOF acquisition-history maintenance signals.

    A read-only summary extracted from the instrument's Compass Server
    PostgreSQL BACKUP by the Hive-side extractor (never the live DB, no
    password). On the hosted dashboard the document arrives through PG Farm
    (the extractor upserts it there); a local install falls back to the JSON
    cache the extractor writes into the STAN config dir, exactly where
    thresholds.yml and ui_prefs.yml are found.

    Responds 404 when nothing has been produced yet -- the panel treats that
    as "the extractor hasn't run" and hides itself, like the ui_prefs.yml 404
    path.
    """
    from stan.db_pg import get_bruker_maintenance_pg, use_pg
    if use_pg():
        try:
            doc = get_bruker_maintenance_pg()
        except Exception as exc:  # noqa: BLE001 - fall through to the file cache
            logger.warning("Bruker maintenance PG read failed: %s", exc)
            doc = None
        if doc:
            return doc
    try:
        path = resolve_config_path("bruker_maintenance.json")
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Bruker maintenance data not found (extractor has not run)",
        )
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        logger.warning("Bruker maintenance cache unreadable: %s", exc)
        raise HTTPException(status_code=503, detail="Bruker maintenance cache unreadable")


@app.get("/api/maintenance/evosep")
async def api_maintenance_evosep() -> dict:
    """Evosep One column-health / clog early-warning signals.

    The Evosep writes a full pressure time-series for every procedure it runs.
    A Hive-side extractor reads those logs READ-ONLY and reduces them to
    per-run backpressure signals, per-method baselines, detected interventions
    and flagged runs. Compass records an LC failure only as a post-mortem
    error string; this is the same event seen as a curve, minutes earlier.

    Delivery mirrors /api/maintenance/bruker exactly: PG Farm first (the
    publisher upserts the document there, so the hosted dashboard is
    nightly-fresh rather than deploy-frozen), then the JSON cache in the STAN
    config dir, where thresholds.yml and ui_prefs.yml are found.

    Responds 404 when nothing has been produced yet -- the panel treats that
    as "the extractor hasn't run" and hides itself.
    """
    from stan.db_pg import get_evosep_column_health_pg, use_pg
    if use_pg():
        try:
            doc = get_evosep_column_health_pg()
        except Exception as exc:  # noqa: BLE001 - fall through to the file cache
            logger.warning("Evosep column health PG read failed: %s", exc)
            doc = None
        if doc:
            return doc
    try:
        path = resolve_config_path("evosep_column_health.json")
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Evosep column health data not found (extractor has not run)",
        )
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        logger.warning("Evosep column health cache unreadable: %s", exc)
        raise HTTPException(
            status_code=503, detail="Evosep column health cache unreadable")


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


# ── Arcade leaderboard ───────────────────────────────────────────────
#
# The arcade used to POST straight to the HF Space relay, which never
# grew the endpoint — every board read 404. Scores now go to the store
# of record (PG Farm when the install has it, local SQLite otherwise),
# so a local install, the hosted dashboard and the community Space all
# read one board.
#
# Everything a player types here is UNTRUSTED and, on PG, world-readable
# by every lab running STAN. Names are length-capped on write in
# stan.db.insert_arcade_score and HTML-escaped on render in
# public/arcade.html. Neither is optional; see the escapeHtml() comment
# there for the incident that made this explicit.

#: Game ids are not whitelisted — a new game should work without a
#: server change — but they must be a boring slug, since the id is a
#: query-string value and a grouping key.
_ARCADE_GAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")

#: Well clear of any real score, well inside PG's BIGINT.
_ARCADE_SCORE_MAX = 10 ** 12
_ARCADE_LEVEL_MAX = 10 ** 6


def _valid_arcade_game(game: str) -> bool:
    return bool(game) and len(game) <= 32 and set(game) <= _ARCADE_GAME_CHARS


class ArcadeScoreBody(BaseModel):
    # This is the one write the PUBLIC dashboard accepts without a login (a
    # shared leaderboard is useless if only signed-in operators can post).
    # Deliberately NOT length-validated here: the handler already truncates
    # to ARCADE_NAME_MAX / ARCADE_AFFILIATION_MAX, and truncating is the
    # right behaviour for a game score -- rejecting the whole submission
    # because someone typed a long lab name would just lose their score.
    game: str
    score: float
    level: int | None = None
    won: bool = False
    player_name: str = ""      # optional — blank is stored as "anonymous"
    affiliation: str = ""      # optional — never required, never prompted twice


def _load_community_cfg() -> dict:
    """`load_community()`, but a missing file is an empty config.

    `load_community()` raises FileNotFoundError when no community.yml
    exists. That is the normal state of a fresh install and of the public
    read-only host, and it is precisely the case the sync flow exists to
    handle — it mints a pseudonym and writes the file. Letting the raise
    escape turned sync-status into a 500 on the public dashboard and made
    the first sync from a new install impossible.
    """
    cfg: dict = {}
    try:
        from stan.config import load_community
        cfg = load_community() or {}
    except FileNotFoundError:
        cfg = {}
    except Exception:
        logger.warning("could not read community.yml", exc_info=True)
        cfg = {}

    # A hosted deployment has no ~/.stan/community.yml — it is a container
    # reading PG, not a lab install. Without this it would fall through to
    # minting a NEW pseudonym and publish this lab's runs under a second
    # identity, splitting them from the ones already on the community site.
    # STAN_DISPLAY_NAME carries the real name in that environment.
    env_name = (_os.environ.get("STAN_DISPLAY_NAME") or "").strip()
    if env_name and not (cfg.get("display_name") or "").strip():
        cfg["display_name"] = env_name
    return cfg


@app.get("/api/community/sync-status")
async def api_community_sync_status(request: Request) -> dict:
    """What a community sync would do right now, without doing it.

    Powers the Sync button's label so the operator sees the size of the
    action before taking it.
    """
    from stan.community.pseudonym import generate_pseudonym
    from stan.dashboard.auth import caller_email, is_privileged
    from stan.dashboard.readonly import LOGIN_URL

    if is_readonly() and not is_privileged(request):
        # Public visitor on the hosted dashboard. Counting eligible runs and
        # minting a name is wasted work, but hand back the login URL so the
        # UI can offer a way in rather than a dead end.
        return {"display_name": None, "suggested_name": None,
                "pending": 0, "readonly": True,
                "can_sign_in": True, "login_url": LOGIN_URL,
                "signed_in_as": caller_email(request)}

    cfg = _load_community_cfg()
    name = (cfg.get("display_name") or "").strip()
    # One read serves both the pending count and the "has this lab published
    # before?" question, so the status endpoint stays a single query.
    published_before = True
    try:
        from stan.db import get_runs
        rows = get_runs(limit=100000)
        pending = len(_pending_community_runs(rows))
        published_before = any(r.get("submitted_to_benchmark") for r in rows)
    except Exception:
        logger.debug("sync-status count failed", exc_info=True)
        pending = -1

    # Only invent a pseudonym for a genuinely new install. Offering one to a
    # lab that already publishes would quietly start a SECOND identity on the
    # community site, and nothing in the UI would tell the operator that the
    # pre-filled name was not theirs. Erring toward an empty box makes them
    # type the real name instead of accepting a plausible-looking wrong one.
    suggested = name
    if not suggested and not published_before:
        suggested = generate_pseudonym()

    return {
        "display_name": name or None,
        "suggested_name": suggested or None,
        "pending": pending,
        # False here means "this caller may sync", which on the hosted
        # dashboard is true only for a signed-in, allow-listed operator.
        "readonly": False,
        "signed_in_as": caller_email(request),
    }


def _pending_community_runs(rows: list[dict]) -> list[dict]:
    """Runs eligible for the community benchmark, mirroring `stan submit-all`.

    All three exclusions the CLI applies, kept in step deliberately, so the
    count on the Sync button is what would actually be pushed rather than a
    larger number the operator then sees silently shrink:

    1. not a QC file at all (the instrument's own QC naming pattern),
    2. washes and blanks, which are not QC results,
    3. zero identifications — a failed search, which would only pollute the
       cohort percentiles.
    """
    import re

    try:
        from stan.watcher.qc_filter import compile_qc_pattern, is_qc_file
        qc_pat = compile_qc_pattern()
    except Exception:
        logger.debug("QC pattern unavailable; counting on name rules alone",
                     exc_info=True)
        qc_pat = is_qc_file = None

    skip = re.compile(r"(?i)(wash|blank|blnk|blk|DELETE)")
    out = []
    for r in rows:
        if r.get("submitted_to_benchmark"):
            continue
        name = str(r.get("run_name") or "")
        if is_qc_file is not None and not is_qc_file(Path(name), qc_pat):
            continue
        if skip.search(name):
            continue
        if (r.get("n_precursors") or 0) + (r.get("n_psms") or 0) <= 0:
            continue
        out.append(r)
    return out


@app.post("/api/community/sync")
async def api_community_sync(body: dict | None = None) -> dict:
    """Submit un-submitted QC runs to the community benchmark.

    One HTTP POST per run, so this runs in a thread and reports counts when
    done. The read-only gate refuses it on a public host — a public
    dashboard must not publish on someone else's behalf.
    """
    import asyncio

    body = body or {}
    display_name = str(body.get("display_name") or "").strip()[:60]

    def _run() -> dict:
        import yaml

        from stan.community.pseudonym import generate_pseudonym
        from stan.community.submit import submit_to_benchmark
        from stan.config import get_user_config_dir
        from stan.db import get_runs

        cfg = _load_community_cfg()
        if display_name:
            cfg["display_name"] = display_name
        elif not (cfg.get("display_name") or "").strip():
            # First sync from a fresh install: mint a pseudonym rather than
            # publishing as "anonymous" forever.
            cfg["display_name"] = generate_pseudonym()
        cfg["community_submit"] = True
        try:
            path = get_user_config_dir() / "community.yml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(yaml.safe_dump(cfg, sort_keys=False))
        except Exception:
            logger.warning("could not persist community.yml", exc_info=True)

        runs = _pending_community_runs(get_runs(limit=100000))
        sent = failed = 0
        errors: list[str] = []
        for run in runs:
            try:
                submit_to_benchmark(
                    run,
                    spd=run.get("spd"),
                    gradient_length_min=run.get("gradient_length_min"),
                    amount_ng=run.get("amount_ng") or 50.0,
                    diann_version=run.get("diann_version"),
                )
                sent += 1
            except Exception as e:  # noqa: BLE001 - one bad run must not stop the rest
                failed += 1
                if len(errors) < 5:
                    errors.append(f"{str(run.get('run_name'))[:40]}: {str(e)[:90]}")
        return {"submitted": sent, "failed": failed,
                "display_name": cfg.get("display_name"), "errors": errors}

    try:
        result = await asyncio.to_thread(_run)
    except Exception as e:  # noqa: BLE001
        logger.exception("community sync failed")
        return {"ok": False, "error": str(e)[:200]}
    return {"ok": True, **result}


@app.get("/api/arcade/leaderboard")
async def api_arcade_leaderboard(game: str | None = None, limit: int = 10) -> dict:
    """Top scores for one game (or across all games when `game` is omitted).

    Never fails hard: an unreachable PG Farm or a stan.db that predates
    the table returns an empty board rather than a 500, and the arcade
    treats an empty local board as its cue to try the community relay.
    """
    from stan.db import get_arcade_leaderboard
    from stan.db_pg import use_pg

    if game is not None and not _valid_arcade_game(game.strip().lower()):
        raise HTTPException(status_code=400, detail="invalid game id")
    rows = get_arcade_leaderboard(game=game, limit=max(1, min(limit, 50)))
    return {
        "game": game,
        "scores": rows,
        "count": len(rows),
        "backend": "pg" if use_pg() else "sqlite",
        # Lets the page say "read-only here" up front instead of
        # discovering it from a 403 after somebody types their name.
        "read_only": is_readonly(),
    }


@app.post("/api/arcade/score")
async def api_arcade_score(body: ArcadeScoreBody) -> dict:
    """Record one game-over score.

    Refused with 403 on a publicly-hosted dashboard — see
    stan/dashboard/readonly.py. That is deliberate: the public instance
    has no way to tell a player from a script, so it reads the board
    without accepting writes.
    """
    import math

    from stan.db import insert_arcade_score

    game = (body.game or "").strip().lower().replace(" ", "_")
    if not _valid_arcade_game(game):
        raise HTTPException(status_code=400, detail="invalid game id")
    if not math.isfinite(body.score) or not (0 <= body.score <= _ARCADE_SCORE_MAX):
        raise HTTPException(status_code=400, detail="score out of range")
    level = body.level
    if level is not None:
        level = max(0, min(int(level), _ARCADE_LEVEL_MAX))

    try:
        stored = insert_arcade_score(
            game=game,
            score=int(body.score),
            level=level,
            won=bool(body.won),
            player_name=body.player_name,
            affiliation=body.affiliation,
        )
    except Exception as e:  # noqa: BLE001 - a game-over must never 500
        logger.warning("arcade: could not store score: %s",
                       str(e).strip().splitlines()[0][:160])
        raise HTTPException(
            status_code=503, detail="leaderboard store unavailable") from e

    # Echo back what was actually stored, so the page shows the
    # truncated name rather than what was typed.
    return {
        "ok": True,
        "id": stored["id"],
        "backend": stored["backend"],
        "player_name": stored["player_name"],
        "affiliation": stored["affiliation"],
    }


# ── Fleet (stan.control) ─────────────────────────────────────────────

@app.get("/api/utilization")
async def api_utilization(days: int = 90) -> dict:
    """Instrument throughput + capacity utilisation from the Hive counter.

    Reads the aggregate JSON written by ``scripts/count_acquisitions.py`` on
    Hive. That counter walks the Flinders archive and emits **per-day counts
    only** -- no filenames, paths, or sample data -- because STAN never
    ingests patient samples, so the ``runs`` table alone would report a
    handful of QC injections against a hundred real acquisitions.

    Returns per-instrument daily and ISO-weekly counts, plus utilisation
    against each nominal SPD capacity (a 100 SPD method run flat out is 100
    acquisitions/day, so 47 of them is 47%).
    """
    import json
    from datetime import date, timedelta

    from stan.config import get_hive_mirror_root

    # PG first: the mirror file only reaches hosts that mount Quobyte, and a
    # hosted dashboard has none. Fall back to the file so a local install
    # with no PG still works.
    raw = None
    try:
        from stan.db_pg import get_utilization_snapshot, use_pg
        if use_pg():
            blob = get_utilization_snapshot()
            if blob:
                raw = json.loads(blob)
    except Exception:  # noqa: BLE001
        logger.debug("PG utilization snapshot unavailable", exc_info=True)

    if raw is None:
        root = get_hive_mirror_root()
        path = (root / "utilization.json") if root else None
        if path is None or not path.exists():
            return {"available": False, "reason":
                    "no utilization snapshot yet — run "
                    "scripts/count_acquisitions.py on Hive.", "instruments": {}}
        try:
            raw = json.loads(path.read_text())
        except Exception as e:  # noqa: BLE001
            return {"available": False, "reason": f"unreadable: {e}", "instruments": {}}

    cutoff = date.today() - timedelta(days=int(days))
    caps = raw.get("capacities") or [100, 60]
    out: dict = {}
    for name, blk in (raw.get("instruments") or {}).items():
        daily_all = blk.get("daily") or {}
        daily = {d: n for d, n in daily_all.items()
                 if _parse_day(d) and _parse_day(d) >= cutoff}
        weekly: dict = {}
        for d, n in daily.items():
            dt = _parse_day(d)
            iso = dt.isocalendar()
            weekly[f"{iso[0]}-W{iso[1]:02d}"] = weekly.get(f"{iso[0]}-W{iso[1]:02d}", 0) + n
        # Hour-of-week grid: 7 rows (days) x 24 cols (hours), most recent
        # week. Counts, not booleans -- "did anything run" and "was it busy"
        # are different questions and the heatmap can answer both.
        hourly_all = blk.get("hourly") or {}
        grid_days: list = []
        if hourly_all:
            last_day = max(k[:10] for k in hourly_all)
            end = _parse_day(last_day) or date.today()
            for back in range(6, -1, -1):
                d = end - timedelta(days=back)
                ds = d.isoformat()
                grid_days.append({
                    "date": ds,
                    "dow": d.strftime("%a"),
                    "hours": [hourly_all.get(f"{ds}T{h:02d}", 0) for h in range(24)],
                })

        active = [n for n in daily.values() if n > 0]
        mean_active = (sum(active) / len(active)) if active else 0.0
        out[name] = {
            "daily": dict(sorted(daily.items())),
            "weekly": dict(sorted(weekly.items())),
            "total": sum(daily.values()),
            "active_days": len(active),
            "mean_per_active_day": round(mean_active, 1),
            "peak_day": max(daily.values()) if daily else 0,
            "utilization_pct": {
                str(c): round(100.0 * mean_active / c, 1) for c in caps
            },
            "hour_grid": grid_days,
            "peak_utilization_pct": {
                str(c): round(100.0 * (max(daily.values()) if daily else 0) / c, 1)
                for c in caps
            },
        }
    return {"available": True, "generated_at": raw.get("generated_at"),
            "capacities": caps, "days": days, "instruments": out}


def _parse_day(s: str):
    """Parse a YYYY-MM-DD key, returning None if it is malformed."""
    from datetime import datetime as _dt
    try:
        return _dt.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


@app.get("/api/fleet/comparison")
async def api_fleet_comparison(
    amount_ng: float = 50.0,
    days: int = 90,
    min_runs: int = 3,
) -> dict:
    """Cross-instrument sensitivity on the same QC standard.

    Groups QC runs by (instrument, spd) and reports median precursor,
    peptide and protein-group depth so instruments can be compared at
    matched load. Grouping keeps SPD separate on purpose: throughput is
    the dominant driver of depth, so pooling a 30 SPD run with a 100 SPD
    run on the same instrument would compare gradient length, not
    sensitivity. Medians (not means) because a single failed injection
    otherwise drags a cohort's depth down.

    Only runs at the same loading (``amount_ng``) are compared -- depth
    scales with load, so mixing 50 ng and 200 ng would be meaningless.
    """
    import sqlite3
    from stan.db import connect, get_db_path

    db_path = get_db_path()
    if not db_path.exists():
        return {"cohorts": [], "amount_ng": amount_ng, "days": days}

    sql = """
        SELECT instrument, spd, mode, gradient_length_min,
               n_precursors, n_peptides, n_proteins, ips_score
        FROM runs
        WHERE (hidden IS NULL OR hidden = 0)
          AND n_precursors IS NOT NULL
          AND amount_ng = ?
          AND run_date >= date('now', ?)
    """
    try:
        with connect(db_path) as con:
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute(sql, (amount_ng, f"-{int(days)} days"))]
    except sqlite3.OperationalError as e:
        logger.warning("fleet comparison query failed: %s", e)
        return {"cohorts": [], "amount_ng": amount_ng, "days": days}

    def _median(vals: list) -> float | None:
        vals = sorted(v for v in vals if v is not None)
        if not vals:
            return None
        mid = len(vals) // 2
        if len(vals) % 2:
            return float(vals[mid])
        return (float(vals[mid - 1]) + float(vals[mid])) / 2.0

    buckets: dict = {}
    for r in rows:
        key = (r["instrument"], r["spd"])
        buckets.setdefault(key, []).append(r)

    cohorts = []
    for (instrument, spd), rs in buckets.items():
        if len(rs) < min_runs:
            continue
        cohorts.append({
            "instrument": instrument,
            "spd": spd,
            "mode": rs[0].get("mode"),
            "gradient_length_min": _median([r.get("gradient_length_min") for r in rs]),
            "n_runs": len(rs),
            "precursors": _median([r.get("n_precursors") for r in rs]),
            "peptides": _median([r.get("n_peptides") for r in rs]),
            "proteins": _median([r.get("n_proteins") for r in rs]),
            "ips": _median([r.get("ips_score") for r in rs]),
        })

    cohorts.sort(key=lambda c: (-(c["precursors"] or 0), c["instrument"]))
    return {"cohorts": cohorts, "amount_ng": amount_ng, "days": days,
            "min_runs": min_runs}


@app.get("/api/fleet/instruments")
async def api_fleet_instruments() -> dict:
    """Per-instrument ingest freshness, derived from the runs table.

    Replaces the old instrument-PC heartbeat as the Fleet tab's liveness
    signal. All searching now happens on Hive, so the instrument PCs no
    longer run a watcher and their status.json heartbeats are
    permanently stale -- they reported "104d ago" in red for machines
    that were behaving exactly as intended. What actually matters is
    whether QC runs are still landing, so report that instead.
    """
    import sqlite3
    from stan.db import connect, get_db_path

    db_path = get_db_path()
    if not db_path.exists():
        return {"instruments": []}
    try:
        with connect(db_path) as con:
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute("""
                SELECT instrument,
                       COUNT(*) AS n_runs,
                       MAX(run_date) AS last_run_date,
                       SUM(CASE WHEN run_date >= date('now','-7 days')
                                THEN 1 ELSE 0 END) AS n_last_7d
                FROM runs
                WHERE (hidden IS NULL OR hidden = 0)
                GROUP BY instrument ORDER BY instrument
            """)]
    except sqlite3.OperationalError as e:
        logger.warning("fleet instruments query failed: %s", e)
        return {"instruments": []}
    return {"instruments": rows}



@app.get("/api/fleet/hosts")
async def api_fleet_hosts() -> dict:
    """List every host directory on the shared mirror and surface each
    host's most recent status.json for the Fleet tab."""
    import json
    from stan.config import get_hive_mirror_root

    root = get_hive_mirror_root()
    if root is None:
        return {"root": None, "hosts": []}

    # The mirror root also holds shared infrastructure directories
    # (incoming/, processing/, logs/, scripts/, backlog/, temp_keys/ ...).
    # Listing every subdirectory rendered those as phantom hosts with empty
    # heartbeat/version/runs columns. A real host is one that has actually
    # synced state, so key off the files a host writes.
    host_markers = ("status.json", "stan.db", "instruments.yml")

    hosts = []
    candidates = [
        d for d in sorted(root.iterdir())
        if d.is_dir() and any((d / m).exists() for m in host_markers)
    ]
    for h in candidates:
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
