"""Tests for SQLite database operations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from stan.db import get_run, get_runs, get_trends, init_db, insert_run, mark_submitted


def test_init_db(tmp_path: Path):
    """Database initialization should create the runs table."""
    db_path = tmp_path / "test.db"
    init_db(db_path)
    assert db_path.exists()

    import sqlite3
    with sqlite3.connect(str(db_path)) as con:
        tables = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    table_names = [t[0] for t in tables]
    assert "runs" in table_names


def test_insert_and_get_run(tmp_path: Path):
    """Insert a run and retrieve it by ID."""
    db_path = tmp_path / "test.db"
    init_db(db_path)

    metrics = {"n_precursors": 15000, "n_peptides": 10000, "ips_score": 85}
    run_id = insert_run(
        instrument="timsTOF Ultra",
        run_name="HeLa_QC_001",
        raw_path="/data/HeLa_QC_001.d",
        mode="DIA",
        metrics=metrics,
        gate_result="pass",
        db_path=db_path,
    )

    assert run_id is not None

    run = get_run(run_id, db_path=db_path)
    assert run is not None
    assert run["instrument"] == "timsTOF Ultra"
    assert run["run_name"] == "HeLa_QC_001"
    assert run["n_precursors"] == 15000
    assert run["gate_result"] == "pass"


def test_get_runs_pagination(tmp_path: Path):
    """get_runs should support limit and offset."""
    db_path = tmp_path / "test.db"
    init_db(db_path)

    for i in range(5):
        insert_run(
            instrument="Test",
            run_name=f"run_{i}",
            raw_path=f"/data/run_{i}.d",
            mode="DIA",
            metrics={"n_precursors": 10000 + i},
            db_path=db_path,
        )

    all_runs = get_runs(limit=10, db_path=db_path)
    assert len(all_runs) == 5

    page = get_runs(limit=2, offset=0, db_path=db_path)
    assert len(page) == 2


def test_get_runs_filter_instrument(tmp_path: Path):
    """get_runs should filter by instrument name."""
    db_path = tmp_path / "test.db"
    init_db(db_path)

    insert_run(instrument="A", run_name="r1", raw_path="/r1", mode="DIA", metrics={}, db_path=db_path)
    insert_run(instrument="B", run_name="r2", raw_path="/r2", mode="DDA", metrics={}, db_path=db_path)

    runs_a = get_runs(instrument="A", db_path=db_path)
    assert len(runs_a) == 1
    assert runs_a[0]["instrument"] == "A"


def test_get_trends(tmp_path: Path):
    """get_trends should return runs ordered by date ascending."""
    db_path = tmp_path / "test.db"
    init_db(db_path)

    for i in range(3):
        insert_run(
            instrument="Test",
            run_name=f"run_{i}",
            raw_path=f"/data/run_{i}.d",
            mode="DIA",
            metrics={"ips_score": 70 + i},
            db_path=db_path,
        )

    trends = get_trends("Test", db_path=db_path)
    assert len(trends) == 3
    # Should be ascending by date
    dates = [t["run_date"] for t in trends]
    assert dates == sorted(dates)


def test_mark_submitted(tmp_path: Path):
    """mark_submitted should update the run record."""
    db_path = tmp_path / "test.db"
    init_db(db_path)

    run_id = insert_run(
        instrument="Test",
        run_name="test",
        raw_path="/test",
        mode="DIA",
        metrics={},
        db_path=db_path,
    )

    mark_submitted(run_id, "sub-123", db_path=db_path)

    run = get_run(run_id, db_path=db_path)
    assert run["submitted_to_benchmark"] == 1
    assert run["submission_id"] == "sub-123"


# ── health_tic_traces tests (v0.2.251) ──────────────────────────────────────

def test_health_tic_traces_table_created(tmp_path: Path):
    """init_db should create the health_tic_traces table alongside tic_traces."""
    db_path = tmp_path / "test.db"
    init_db(db_path)

    with sqlite3.connect(str(db_path)) as con:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert "health_tic_traces" in tables
    assert "tic_traces" in tables


def test_insert_health_tic_trace_round_trip(tmp_path: Path):
    """insert_health_tic_trace should store a TIC row readable by health_id."""
    from stan.db import insert_health_tic_trace, insert_sample_health

    db_path = tmp_path / "test.db"
    init_db(db_path)

    health_id = insert_sample_health(
        instrument="timsTOF Ultra",
        run_name="Sample_001.d",
        run_date="2026-04-27T10:00:00",
        raw_path="/data/Sample_001.d",
        verdict="pass",
        reasons=[],
        rawmeat_summary={},
        db_path=db_path,
    )

    rt_min = [float(i) * 0.5 for i in range(128)]
    intensity = [float(i) * 1000.0 for i in range(128)]
    insert_health_tic_trace(health_id, rt_min, intensity, db_path=db_path)

    with sqlite3.connect(str(db_path)) as con:
        row = con.execute(
            "SELECT health_id, rt_min, intensity, n_frames "
            "FROM health_tic_traces WHERE health_id = ?",
            (health_id,),
        ).fetchone()

    assert row is not None
    assert row[0] == health_id
    stored_rt = json.loads(row[1])
    stored_int = json.loads(row[2])
    assert len(stored_rt) == 128
    assert len(stored_int) == 128


def test_existing_qc_tic_traces_unaffected(tmp_path: Path):
    """QC runs in tic_traces should remain readable after health_tic_traces is populated."""
    from stan.db import insert_health_tic_trace, insert_sample_health, insert_tic_trace

    db_path = tmp_path / "test.db"
    init_db(db_path)

    # Insert a QC run with a TIC trace
    run_id = insert_run(
        instrument="timsTOF Ultra",
        run_name="HeLa_QC_001.d",
        raw_path="/data/HeLa_QC_001.d",
        mode="DIA",
        metrics={"n_precursors": 20000},
        db_path=db_path,
    )
    qc_rt = [float(i) for i in range(50)]
    qc_int = [float(i) * 500.0 for i in range(50)]
    insert_tic_trace(run_id, qc_rt, qc_int, db_path=db_path)

    # Insert a sample_health row with its own TIC
    health_id = insert_sample_health(
        instrument="timsTOF Ultra",
        run_name="BSA_001.d",
        run_date="2026-04-27T11:00:00",
        raw_path="/data/BSA_001.d",
        verdict="pass",
        reasons=[],
        rawmeat_summary={},
        db_path=db_path,
    )
    h_rt = [float(i) * 0.25 for i in range(128)]
    h_int = [float(i) * 200.0 for i in range(128)]
    insert_health_tic_trace(health_id, h_rt, h_int, db_path=db_path)

    # Both tables remain independently readable
    with sqlite3.connect(str(db_path)) as con:
        qc_row = con.execute(
            "SELECT run_id FROM tic_traces WHERE run_id = ?", (run_id,)
        ).fetchone()
        h_row = con.execute(
            "SELECT health_id FROM health_tic_traces WHERE health_id = ?",
            (health_id,),
        ).fetchone()

    assert qc_row is not None and qc_row[0] == run_id
    assert h_row is not None and h_row[0] == health_id
