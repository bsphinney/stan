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

    -- Chromatography
    grs_score        INTEGER,
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

    -- Community
    submitted_to_benchmark INTEGER DEFAULT 0,
    submission_id    TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_instrument ON runs(instrument);
CREATE INDEX IF NOT EXISTS idx_runs_date ON runs(run_date);
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
        "run_date": now,
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
        "grs_score": metrics.get("grs_score"),
        "tic_auc": metrics.get("tic_auc"),
        "peak_rt_min": metrics.get("peak_rt_min"),
        "irt_max_deviation_min": metrics.get("irt_max_deviation_min"),
        "ms2_fill_time_median_ms": metrics.get("ms2_fill_time_median_ms"),
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
