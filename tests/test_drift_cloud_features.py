"""Tests for the /api/runs/{run_id}/features-by-charge endpoint (v0.2.192+).

Covers the Ziggy-style per-charge ion scatter data path without requiring
the Bruker 4DFF binary — we build a tiny synthetic LcTimsMsFeature SQLite
next to a mock .d directory and confirm the endpoint returns sensibly
grouped charge-state traces.

These tests must NOT import from ``stan.metrics.features`` — that module
is actively being developed by a parallel worker and importing from it
introduces an ordering coupling we want to avoid.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

import stan.db as stan_db
import stan.dashboard.server as server


# ─────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────

def _make_stan_db(db_path: Path) -> None:
    """Initialise a STAN SQLite DB at the given path."""
    stan_db.init_db(db_path=db_path)


def _insert_run(db_path: Path, run_id: str, raw_path: str,
                run_name: str = "test_run") -> None:
    """Insert a minimal runs row so the endpoint can look up raw_path."""
    with sqlite3.connect(str(db_path)) as con:
        con.execute(
            "INSERT INTO runs (id, instrument, run_name, run_date, raw_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, "timsTOF Test", run_name, "2026-04-23T12:00:00", raw_path),
        )
        con.commit()


def _make_features_db(path: Path, rows: list[tuple]) -> None:
    """Build a minimal LcTimsMsFeature SQLite mirroring 4DFF output.

    rows: list of (MZ, Charge, RT, Mobility, Intensity) tuples.
    """
    con = sqlite3.connect(str(path))
    con.execute(
        """CREATE TABLE LcTimsMsFeature (
            Id INTEGER PRIMARY KEY,
            MZ REAL,
            Charge INTEGER,
            RT REAL,
            RT_lower REAL,
            RT_upper REAL,
            Mobility REAL,
            Mobility_lower REAL,
            Mobility_upper REAL,
            Intensity REAL,
            ClusterCount INTEGER
        )"""
    )
    con.executemany(
        "INSERT INTO LcTimsMsFeature "
        "(MZ, Charge, RT, RT_lower, RT_upper, "
        " Mobility, Mobility_lower, Mobility_upper, Intensity, ClusterCount) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (mz, z, rt, rt - 5, rt + 5, mob, mob - 0.02, mob + 0.02, inten, 3)
            for (mz, z, rt, mob, inten) in rows
        ],
    )
    con.commit()
    con.close()


# ─────────────────────────────────────────────────────────────
#  _locate_features_file
# ─────────────────────────────────────────────────────────────

def test_locate_features_file_inside_d(tmp_path: Path) -> None:
    """Prefer <d>/<stem>.features when both locations exist."""
    d = tmp_path / "run01.d"
    d.mkdir()
    inside = d / "run01.features"
    inside.write_bytes(b"")
    sibling = tmp_path / "run01.features"
    sibling.write_bytes(b"")
    assert server._locate_features_file(str(d)) == inside


def test_locate_features_file_sibling_fallback(tmp_path: Path) -> None:
    """Fall back to the sibling .features file when not inside the .d."""
    d = tmp_path / "run01.d"
    d.mkdir()
    sibling = tmp_path / "run01.features"
    sibling.write_bytes(b"")
    assert server._locate_features_file(str(d)) == sibling


def test_locate_features_file_missing_returns_none(tmp_path: Path) -> None:
    d = tmp_path / "run01.d"
    d.mkdir()
    assert server._locate_features_file(str(d)) is None


def test_locate_features_file_empty_raw_path_returns_none() -> None:
    assert server._locate_features_file(None) is None
    assert server._locate_features_file("") is None


def test_locate_features_file_nonexistent_d_returns_none(tmp_path: Path) -> None:
    assert server._locate_features_file(str(tmp_path / "does_not_exist.d")) is None


# ─────────────────────────────────────────────────────────────
#  /api/runs/{run_id}/features-by-charge endpoint
# ─────────────────────────────────────────────────────────────

def test_features_endpoint_returns_grouped_by_charge(
    tmp_path: Path, monkeypatch,
) -> None:
    """Happy path — 4DFF .features exists and is grouped by charge state."""
    # Build a .d with an inside .features file.
    d = tmp_path / "myrun.d"
    d.mkdir()
    feat = d / "myrun.features"
    _make_features_db(feat, [
        # +2 peptides along the main ridge
        (400.5, 2, 600.0, 0.90, 1e5),
        (500.7, 2, 700.0, 0.95, 2e5),
        (600.0, 2, 800.0, 1.00, 3e5),
        # +1 contamination above the ridge
        (800.2, 1, 800.0, 1.25, 5e4),
        # +3 below
        (450.0, 3, 650.0, 0.78, 8e4),
        # unassigned
        (700.0, 0, 750.0, 1.10, 1e4),
    ])

    # Build a STAN DB with a runs row pointing at the .d.
    db_path = tmp_path / "stan.db"
    _make_stan_db(db_path)
    _insert_run(db_path, "run-abc", str(d), run_name="myrun")

    monkeypatch.setattr(stan_db, "get_db_path", lambda: db_path)
    monkeypatch.setattr(server, "get_db_path", lambda: db_path)

    client = TestClient(server.app)
    resp = client.get("/api/runs/run-abc/features-by-charge")
    assert resp.status_code == 200
    body = resp.json()

    assert body["has_features"] is True
    assert body["n_features"] == 6
    assert body["run_name"] == "myrun"
    assert set(body["by_charge"].keys()) == {"0", "1", "2", "3"}

    # Each charge bucket has parallel-length arrays
    plus2 = body["by_charge"]["2"]
    assert len(plus2["mz"]) == 3
    assert len(plus2["mobility"]) == 3
    assert len(plus2["rt"]) == 3
    assert len(plus2["intensity"]) == 3

    # m/z and mobility ranges reflect the spread of inputs
    assert body["mz_range"][0] <= 400.5
    assert body["mz_range"][1] >= 800.2
    assert body["mobility_range"][0] <= 0.78
    assert body["mobility_range"][1] >= 1.25


def test_features_endpoint_missing_file_returns_friendly_stub(
    tmp_path: Path, monkeypatch,
) -> None:
    """No .features file → HTTP 200 + has_features=False + helpful reason."""
    d = tmp_path / "empty.d"
    d.mkdir()

    db_path = tmp_path / "stan.db"
    _make_stan_db(db_path)
    _insert_run(db_path, "run-empty", str(d))

    monkeypatch.setattr(stan_db, "get_db_path", lambda: db_path)
    monkeypatch.setattr(server, "get_db_path", lambda: db_path)

    client = TestClient(server.app)
    resp = client.get("/api/runs/run-empty/features-by-charge")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_features"] is False
    assert "stan run-4dff" in body["reason"]


def test_features_endpoint_unknown_run(tmp_path: Path, monkeypatch) -> None:
    """Unknown run_id → friendly stub (not a 500)."""
    db_path = tmp_path / "stan.db"
    _make_stan_db(db_path)
    monkeypatch.setattr(stan_db, "get_db_path", lambda: db_path)
    monkeypatch.setattr(server, "get_db_path", lambda: db_path)

    client = TestClient(server.app)
    resp = client.get("/api/runs/does-not-exist/features-by-charge")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_features"] is False
    assert "not found" in body["reason"].lower()


def test_features_endpoint_bad_source_400(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "stan.db"
    _make_stan_db(db_path)
    monkeypatch.setattr(stan_db, "get_db_path", lambda: db_path)
    monkeypatch.setattr(server, "get_db_path", lambda: db_path)

    client = TestClient(server.app)
    resp = client.get("/api/runs/foo/features-by-charge?source=garbage")
    assert resp.status_code == 400


def test_features_endpoint_caps_at_50k(tmp_path: Path, monkeypatch) -> None:
    """Tables larger than 50k rows should be uniformly downsampled."""
    d = tmp_path / "big.d"
    d.mkdir()
    feat = d / "big.features"

    # Build 60k rows — exceeds the 50k cap.
    rows = [
        (400.0 + (i % 1000) * 0.5, 2, 600.0, 0.9 + (i % 10) * 0.01, 1e4 + i)
        for i in range(60_000)
    ]
    _make_features_db(feat, rows)

    db_path = tmp_path / "stan.db"
    _make_stan_db(db_path)
    _insert_run(db_path, "run-big", str(d))
    monkeypatch.setattr(stan_db, "get_db_path", lambda: db_path)
    monkeypatch.setattr(server, "get_db_path", lambda: db_path)

    client = TestClient(server.app)
    resp = client.get("/api/runs/run-big/features-by-charge")
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_features"] is True
    assert body["n_total"] == 60_000
    # Must be well under 50k after downsampling (rowid % step filter).
    assert body["n_features"] <= 50_000
    # And not trivially small — we should keep the bulk of the cloud.
    assert body["n_features"] >= 20_000


def test_features_endpoint_sample_health_source(
    tmp_path: Path, monkeypatch,
) -> None:
    """source=sample_health reads from the sample_health table, not runs."""
    d = tmp_path / "sh.d"
    d.mkdir()
    feat = d / "sh.features"
    _make_features_db(feat, [
        (500.0, 2, 700.0, 0.95, 1e5),
        (500.0, 2, 710.0, 0.96, 2e5),
    ])

    db_path = tmp_path / "stan.db"
    _make_stan_db(db_path)
    # Insert a sample_health row (not a runs row).
    with sqlite3.connect(str(db_path)) as con:
        con.execute(
            "INSERT INTO sample_health "
            "(id, instrument, run_name, run_date, raw_path, verdict) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("sh-1", "timsTOF Test", "sh_run", "2026-04-23T12:00:00",
             str(d), "pass"),
        )
        con.commit()

    monkeypatch.setattr(stan_db, "get_db_path", lambda: db_path)
    monkeypatch.setattr(server, "get_db_path", lambda: db_path)

    client = TestClient(server.app)
    resp = client.get(
        "/api/runs/sh-1/features-by-charge?source=sample_health"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_features"] is True
    assert body["n_features"] == 2
