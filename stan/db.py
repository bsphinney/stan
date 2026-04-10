"""SQLite database for longitudinal QC run storage."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from stan.config import get_user_config_dir

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    instrument  TEXT NOT NULL,
    run_name    TEXT NOT NULL,
    run_date    TEXT NOT NULL,
    raw_path    TEXT,
    mode        TEXT,

    -- DIA metrics
    n_precursors     INTEGER,
    n_peptides       INTEGER,
    n_proteins       INTEGER,
    median_cv_precursor REAL,
    median_fragments_per_precursor REAL,
    pct_fragments_quantified REAL,

    -- DDA metrics
    n_psms           INTEGER,
    n_peptides_dda   INTEGER,
    median_hyperscore REAL,
    ms2_scan_rate    REAL,
    median_delta_mass_ppm REAL,

    -- Shared
    missed_cleavage_rate REAL,
    pct_charge_1    REAL,
    pct_charge_2    REAL,
    pct_charge_3    REAL,

    -- Chromatography / peak shape
    median_peak_width_sec REAL,
    median_points_across_peak REAL,
    ips_score        INTEGER,
    tic_auc          REAL,
    peak_rt_min      REAL,
    irt_max_deviation_min REAL,
    ms2_fill_time_median_ms REAL,

    -- Gate result
    gate_result      TEXT,
    failed_gates     TEXT,
    diagnosis        TEXT,

    -- Run metadata
    amount_ng        REAL DEFAULT 50.0,
    spd              INTEGER,
    gradient_length_min INTEGER,
    column_vendor    TEXT,
    column_model     TEXT,

    -- Community
    submitted_to_benchmark INTEGER DEFAULT 0,
    submission_id    TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_instrument ON runs(instrument);
CREATE INDEX IF NOT EXISTS idx_runs_date ON runs(run_date);

CREATE TABLE IF NOT EXISTS maintenance_events (
    id          TEXT PRIMARY KEY,
    instrument  TEXT NOT NULL,
    event_type  TEXT NOT NULL,   -- column_change, source_clean, calibration, pm, lc_service, other
    event_date  TEXT NOT NULL,
    notes       TEXT DEFAULT '',
    operator    TEXT DEFAULT '',
    -- For column tracking: what was installed
    column_vendor TEXT,
    column_model  TEXT,
    column_serial TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_instrument ON maintenance_events(instrument);
CREATE INDEX IF NOT EXISTS idx_events_date ON maintenance_events(event_date);

CREATE TABLE IF NOT EXISTS tic_traces (
    run_id      TEXT PRIMARY KEY REFERENCES runs(id),
    rt_min      TEXT NOT NULL,   -- JSON array of floats
    intensity   TEXT NOT NULL,   -- JSON array of floats
    n_frames    INTEGER,
    UNIQUE(run_id)
);
"""


def get_db_path() -> Path:
    """Return the path to the STAN SQLite database."""
    return get_user_config_dir() / "stan.db"


def init_db(db_path: Path | None = None) -> None:
    """Initialize the database schema and apply any pending migrations."""
    if db_path is None:
        db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(str(db_path)) as con:
        con.executescript(_SCHEMA)
        _migrate(con)
    logger.info("Database initialized: %s", db_path)


def _migrate(con: sqlite3.Connection) -> None:
    """Apply schema migrations for columns added after initial release."""
    existing = {row[1] for row in con.execute("PRAGMA table_info(runs)").fetchall()}

    migrations: list[tuple[str, str]] = [
        ("amount_ng", "ALTER TABLE runs ADD COLUMN amount_ng REAL DEFAULT 50.0"),
        ("spd", "ALTER TABLE runs ADD COLUMN spd INTEGER"),
        ("gradient_length_min", "ALTER TABLE runs ADD COLUMN gradient_length_min INTEGER"),
        ("column_vendor", "ALTER TABLE runs ADD COLUMN column_vendor TEXT"),
        ("column_model", "ALTER TABLE runs ADD COLUMN column_model TEXT"),
        ("median_peak_width_sec", "ALTER TABLE runs ADD COLUMN median_peak_width_sec REAL"),
        ("median_points_across_peak", "ALTER TABLE runs ADD COLUMN median_points_across_peak REAL"),
        ("ips_score", "ALTER TABLE runs ADD COLUMN ips_score INTEGER"),
        # From report.stats.tsv (added 2026-04-10)
        ("ms1_signal", "ALTER TABLE runs ADD COLUMN ms1_signal REAL"),
        ("ms2_signal", "ALTER TABLE runs ADD COLUMN ms2_signal REAL"),
        ("fwhm_rt_min", "ALTER TABLE runs ADD COLUMN fwhm_rt_min REAL"),
        ("fwhm_scans", "ALTER TABLE runs ADD COLUMN fwhm_scans REAL"),
        ("median_mass_acc_ms1_ppm", "ALTER TABLE runs ADD COLUMN median_mass_acc_ms1_ppm REAL"),
        ("median_mass_acc_ms2_ppm", "ALTER TABLE runs ADD COLUMN median_mass_acc_ms2_ppm REAL"),
        ("peak_capacity", "ALTER TABLE runs ADD COLUMN peak_capacity REAL"),
        ("dynamic_range_log10", "ALTER TABLE runs ADD COLUMN dynamic_range_log10 REAL"),
    ]

    for col, ddl in migrations:
        if col not in existing:
            con.execute(ddl)
            logger.info("Migration: added column '%s' to runs table", col)


def insert_run(
    instrument: str,
    run_name: str,
    raw_path: str,
    mode: str,
    metrics: dict,
    gate_result: str = "",
    failed_gates: list[str] | None = None,
    diagnosis: str = "",
    amount_ng: float = 50.0,
    spd: int | None = None,
    gradient_length_min: int | None = None,
    db_path: Path | None = None,
    run_date: str | None = None,
) -> str:
    """Insert a QC run record into the database.

    Args:
        instrument: Instrument name.
        run_name: Run/file name.
        raw_path: Path to raw data file.
        mode: "DIA" or "DDA".
        metrics: Dict of all computed metrics.
        gate_result: "pass", "warn", or "fail".
        failed_gates: List of failed metric names.
        diagnosis: Plain-English diagnosis string.
        amount_ng: HeLa injection amount in nanograms (default 50).
        spd: Samples per day (primary throughput measure).
        gradient_length_min: LC gradient length in minutes (fallback).
        db_path: Optional override for database path.
        run_date: ISO-format acquisition date (from raw file metadata or mtime).
            Falls back to current UTC time if not provided. Historical
            baseline runs must pass this to preserve real acquisition dates.

    Returns:
        The generated run ID (UUID).
    """
    if db_path is None:
        db_path = get_db_path()

    run_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()

    row = {
        "id": run_id,
        "instrument": instrument,
        "run_name": run_name,
        "run_date": run_date or now,
        "raw_path": raw_path,
        "mode": mode,
        # DIA
        "n_precursors": metrics.get("n_precursors"),
        "n_peptides": metrics.get("n_peptides"),
        "n_proteins": metrics.get("n_proteins"),
        "median_cv_precursor": metrics.get("median_cv_precursor"),
        "median_fragments_per_precursor": metrics.get("median_fragments_per_precursor"),
        "pct_fragments_quantified": metrics.get("pct_fragments_quantified"),
        # DDA
        "n_psms": metrics.get("n_psms"),
        "n_peptides_dda": metrics.get("n_peptides_dda"),
        "median_hyperscore": metrics.get("median_hyperscore"),
        "ms2_scan_rate": metrics.get("ms2_scan_rate"),
        "median_delta_mass_ppm": metrics.get("median_delta_mass_ppm"),
        # Shared
        "missed_cleavage_rate": metrics.get("missed_cleavage_rate"),
        "pct_charge_1": metrics.get("pct_charge_1"),
        "pct_charge_2": metrics.get("pct_charge_2"),
        "pct_charge_3": metrics.get("pct_charge_3"),
        # Chromatography
        "ips_score": metrics.get("ips_score"),
        "tic_auc": metrics.get("tic_auc"),
        "peak_rt_min": metrics.get("peak_rt_min"),
        "irt_max_deviation_min": metrics.get("irt_max_deviation_min"),
        "ms2_fill_time_median_ms": metrics.get("ms2_fill_time_median_ms"),
        # From report.stats.tsv
        "ms1_signal": metrics.get("ms1_signal"),
        "ms2_signal": metrics.get("ms2_signal"),
        "fwhm_rt_min": metrics.get("fwhm_rt_min"),
        "fwhm_scans": metrics.get("fwhm_scans"),
        "median_mass_acc_ms1_ppm": metrics.get("median_mass_acc_ms1_ppm"),
        "median_mass_acc_ms2_ppm": metrics.get("median_mass_acc_ms2_ppm"),
        "peak_capacity": metrics.get("peak_capacity"),
        "dynamic_range_log10": metrics.get("dynamic_range_log10"),
        # Run metadata
        "amount_ng": amount_ng,
        "spd": spd,
        "gradient_length_min": gradient_length_min,
        # Gating
        "gate_result": gate_result,
        "failed_gates": json.dumps(failed_gates or []),
        "diagnosis": diagnosis,
    }

    cols = ", ".join(row.keys())
    placeholders = ", ".join(f":{k}" for k in row.keys())

    with sqlite3.connect(str(db_path)) as con:
        con.execute(f"INSERT INTO runs ({cols}) VALUES ({placeholders})", row)

    logger.info("Inserted run %s: %s (%s)", run_id[:8], run_name, gate_result)
    return run_id


def insert_tic_trace(
    run_id: str,
    rt_min: list[float],
    intensity: list[float],
    db_path: Path | None = None,
) -> None:
    """Store a TIC trace for a run. Local-only — never uploaded to community."""
    if db_path is None:
        db_path = get_db_path()

    # Downsample to ~500 points max for storage efficiency
    n = len(rt_min)
    if n > 500:
        step = n // 500
        rt_min = rt_min[::step]
        intensity = intensity[::step]

    with sqlite3.connect(str(db_path)) as con:
        con.execute(
            "INSERT OR REPLACE INTO tic_traces (run_id, rt_min, intensity, n_frames) "
            "VALUES (?, ?, ?, ?)",
            (
                run_id,
                json.dumps([round(r, 3) for r in rt_min]),
                json.dumps([round(v, 0) for v in intensity]),
                n,
            ),
        )


def get_tic_trace(run_id: str, db_path: Path | None = None) -> dict | None:
    """Fetch a TIC trace for a single run."""
    if db_path is None:
        db_path = get_db_path()

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM tic_traces WHERE run_id = ?", (run_id,)
        ).fetchone()

    if not row:
        return None
    return {
        "run_id": row["run_id"],
        "rt_min": json.loads(row["rt_min"]),
        "intensity": json.loads(row["intensity"]),
        "n_frames": row["n_frames"],
    }


def get_tic_traces_for_instrument(
    instrument: str,
    limit: int = 20,
    db_path: Path | None = None,
) -> list[dict]:
    """Fetch recent TIC traces for an instrument, joined with run metadata."""
    if db_path is None:
        db_path = get_db_path()

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT t.run_id, t.rt_min, t.intensity, t.n_frames, "
            "r.run_name, r.run_date, r.gate_result "
            "FROM tic_traces t "
            "JOIN runs r ON t.run_id = r.id "
            "WHERE r.instrument = ? "
            "ORDER BY r.run_date DESC LIMIT ?",
            (instrument, limit),
        ).fetchall()

    return [
        {
            "run_id": row["run_id"],
            "rt_min": json.loads(row["rt_min"]),
            "intensity": json.loads(row["intensity"]),
            "n_frames": row["n_frames"],
            "run_name": row["run_name"],
            "run_date": row["run_date"],
            "gate_result": row["gate_result"],
        }
        for row in rows
    ]


def get_runs(
    instrument: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db_path: Path | None = None,
) -> list[dict]:
    """Fetch recent runs from the database.

    Args:
        instrument: Filter by instrument name (None for all).
        limit: Maximum rows to return.
        offset: Pagination offset.
        db_path: Optional override for database path.

    Returns:
        List of run dicts ordered by run_date descending.
    """
    if db_path is None:
        db_path = get_db_path()

    query = "SELECT * FROM runs"
    params: list = []

    if instrument:
        query += " WHERE instrument = ?"
        params.append(instrument)

    query += " ORDER BY run_date DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(query, params).fetchall()

    return [dict(row) for row in rows]


def get_run(run_id: str, db_path: Path | None = None) -> dict | None:
    """Fetch a single run by ID."""
    if db_path is None:
        db_path = get_db_path()

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()

    return dict(row) if row else None


def get_trends(
    instrument: str,
    limit: int = 100,
    db_path: Path | None = None,
) -> list[dict]:
    """Fetch time-series metrics for trend plots.

    Returns runs ordered by date ascending for charting.
    """
    if db_path is None:
        db_path = get_db_path()

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT * FROM runs WHERE instrument = ? ORDER BY run_date ASC LIMIT ?",
            (instrument, limit),
        ).fetchall()

    return [dict(row) for row in rows]


def mark_submitted(run_id: str, submission_id: str, db_path: Path | None = None) -> None:
    """Mark a run as submitted to the community benchmark."""
    if db_path is None:
        db_path = get_db_path()

    with sqlite3.connect(str(db_path)) as con:
        con.execute(
            "UPDATE runs SET submitted_to_benchmark = 1, submission_id = ? WHERE id = ?",
            (submission_id, run_id),
        )
    logger.info("Run %s marked as submitted (submission %s)", run_id[:8], submission_id[:8])


# ── Maintenance events ─────────────────────────────────────────────

EVENT_TYPES = [
    "column_change",   # New LC column installed
    "source_clean",    # Ion source cleaned
    "calibration",     # Mass calibration performed
    "pm",              # Scheduled preventive maintenance
    "lc_service",      # LC pump/valve/tubing service
    "other",           # Free-text
]


def log_event(
    instrument: str,
    event_type: str,
    notes: str = "",
    operator: str = "",
    event_date: str | None = None,
    column_vendor: str | None = None,
    column_model: str | None = None,
    column_serial: str | None = None,
    db_path: Path | None = None,
) -> str:
    """Record a maintenance event for an instrument.

    Args:
        instrument: Instrument name (must match instruments.yml).
        event_type: One of EVENT_TYPES.
        notes: Free-text description.
        operator: Who performed the maintenance.
        event_date: ISO 8601. Defaults to now.
        column_vendor/model/serial: For column_change events.

    Returns:
        Event ID.
    """
    if db_path is None:
        db_path = get_db_path()
    if event_date is None:
        event_date = datetime.now(timezone.utc).isoformat(timespec="seconds")

    event_id = str(uuid.uuid4())[:12]
    row = {
        "id": event_id,
        "instrument": instrument,
        "event_type": event_type,
        "event_date": event_date,
        "notes": notes,
        "operator": operator,
        "column_vendor": column_vendor,
        "column_model": column_model,
        "column_serial": column_serial,
    }

    with sqlite3.connect(str(db_path)) as con:
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        con.execute(f"INSERT INTO maintenance_events ({cols}) VALUES ({placeholders})", row)

    logger.info("Logged event %s: %s on %s (%s)", event_id, event_type, instrument, notes[:50])
    return event_id


def get_events(
    instrument: str | None = None,
    limit: int = 100,
    db_path: Path | None = None,
) -> list[dict]:
    """Fetch maintenance events, newest first."""
    if db_path is None:
        db_path = get_db_path()
    if not db_path.exists():
        return []

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        if instrument:
            rows = con.execute(
                "SELECT * FROM maintenance_events WHERE instrument = ? ORDER BY event_date DESC LIMIT ?",
                (instrument, limit),
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM maintenance_events ORDER BY event_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_last_event(instrument: str, event_type: str, db_path: Path | None = None) -> dict | None:
    """Get the most recent event of a given type for an instrument."""
    if db_path is None:
        db_path = get_db_path()
    if not db_path.exists():
        return None

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT * FROM maintenance_events WHERE instrument = ? AND event_type = ? ORDER BY event_date DESC LIMIT 1",
            (instrument, event_type),
        ).fetchone()
    return dict(row) if row else None


def _bruker_injection_number(run_name: str) -> int | None:
    """Extract the absolute injection counter from a Bruker .d filename.

    Bruker filenames end with _N_NNNN.d where the last number is the
    instrument's absolute injection counter (increments for every injection,
    not just QC). Example: 03jun2024_HeLa50ng_DIA_100spd_S1-B2_1_6205.d → 6205.
    """
    import re
    # Strip .d suffix
    name = run_name
    if name.endswith(".d"):
        name = name[:-2]
    # Match the last number group
    m = re.search(r"_(\d+)$", name)
    if m:
        n = int(m.group(1))
        # Sanity check: injection counters are typically >100
        if n > 50:
            return n
    return None


def get_column_lifetime(instrument: str, db_path: Path | None = None) -> dict:
    """Get column health stats since the last column change.

    For Bruker instruments: uses the absolute injection counter embedded in
    filenames (e.g., _6205.d) to give the REAL total injection count on the
    column, including non-QC samples STAN doesn't see.

    For Thermo instruments: does NOT attempt to count injections because
    STAN only sees QC runs while the instrument runs hundreds of real
    samples between them — showing "5 injections" when the column has
    done 500 would be misleading. Instead, tracks days-on-column +
    QC depth trend (% decline per week). The user logs column changes
    via `stan log column-change` and STAN shows "47 days on column,
    precursor trend: -2.1%/week".

    Returns:
        {
            column_installed: date string,
            column_vendor: str,
            column_model: str,
            qc_runs_since_change: int — QC runs STAN has processed
            total_injections_on_column: int | None — Bruker only (from filename counter)
            days_on_column: int,
            runs_since_change: list[dict],
            depth_trend_pct_per_week: float | None — precursor count trend
        }
    """
    if db_path is None:
        db_path = get_db_path()
    if not db_path.exists():
        return {"qc_runs_since_change": 0, "runs_since_change": []}

    # Find last column change
    last_change = get_last_event(instrument, "column_change", db_path=db_path)
    since_date = last_change["event_date"] if last_change else "1970-01-01"

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        runs = con.execute(
            """SELECT run_name, run_date, n_precursors, n_psms, n_peptides,
                      n_proteins, ips_score, gate_result
               FROM runs
               WHERE instrument = ? AND run_date >= ?
               ORDER BY run_date ASC""",
            (instrument, since_date),
        ).fetchall()

    runs_list = [dict(r) for r in runs]

    result = {
        "column_installed": last_change["event_date"] if last_change else None,
        "column_vendor": last_change.get("column_vendor") if last_change else None,
        "column_model": last_change.get("column_model") if last_change else None,
        "qc_runs_since_change": len(runs_list),
        "total_injections_on_column": None,
        "days_on_column": 0,
        "runs_since_change": runs_list,
        "depth_trend_pct_per_week": None,
    }

    if runs_list:
        first = datetime.fromisoformat(runs_list[0]["run_date"].replace("Z", "+00:00"))
        last_dt = datetime.fromisoformat(runs_list[-1]["run_date"].replace("Z", "+00:00"))
        result["days_on_column"] = max(0, (last_dt - first).days)

        # Bruker: extract absolute injection counters from filenames
        first_inj = _bruker_injection_number(runs_list[0]["run_name"])
        last_inj = _bruker_injection_number(runs_list[-1]["run_name"])
        if first_inj is not None and last_inj is not None and last_inj > first_inj:
            result["total_injections_on_column"] = last_inj - first_inj

        # Compute depth trend (% change per week) via simple linear regression
        depths = [(i, r.get("n_precursors") or r.get("n_psms") or 0)
                  for i, r in enumerate(runs_list)
                  if (r.get("n_precursors") or r.get("n_psms") or 0) > 0]
        if len(depths) >= 5 and result["days_on_column"] > 7:
            xs = [d[0] for d in depths]
            ys = [d[1] for d in depths]
            n = len(xs)
            x_mean = sum(xs) / n
            y_mean = sum(ys) / n
            num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
            den = sum((x - x_mean) ** 2 for x in xs)
            if den > 0 and y_mean > 0:
                slope = num / den  # IDs per run index
                # Convert to % per week: slope * (runs per week) / mean * 100
                runs_per_day = len(runs_list) / max(1, result["days_on_column"])
                runs_per_week = runs_per_day * 7
                pct_per_week = (slope * runs_per_week / y_mean) * 100
                result["depth_trend_pct_per_week"] = round(pct_per_week, 2)

    return result


def time_since_last_qc(instrument: str, db_path: Path | None = None) -> dict:
    """How long since the last QC run on this instrument?

    Returns:
        {
            last_run_date: str,
            last_run_name: str,
            hours_ago: float,
            status: 'ok' | 'overdue' | 'critical',
        }
    """
    if db_path is None:
        db_path = get_db_path()
    if not db_path.exists():
        return {"hours_ago": None, "status": "critical"}

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT run_name, run_date FROM runs WHERE instrument = ? ORDER BY run_date DESC LIMIT 1",
            (instrument,),
        ).fetchone()

    if not row:
        return {"hours_ago": None, "status": "critical"}

    last_date = datetime.fromisoformat(row["run_date"].replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    hours = (now - last_date).total_seconds() / 3600

    # Status thresholds (configurable later)
    if hours < 24:
        status = "ok"
    elif hours < 72:
        status = "overdue"
    else:
        status = "critical"

    return {
        "last_run_date": row["run_date"],
        "last_run_name": row["run_name"],
        "hours_ago": round(hours, 1),
        "status": status,
    }
