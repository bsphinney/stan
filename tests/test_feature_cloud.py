"""Tests for the DB-backed charge-labeled ion cloud (v1.0.16+).

The Plotly ion-cloud view used to read the 4DFF ``.features`` sidecar off
the local filesystem, which meant it only ever rendered on a host with
the Bruker ``.d`` mounted — never the fleet dashboard, whose raw data
lives on Hive. These tests cover the path that fixed that: extract →
store → serve, with the sidecar as the fallback rather than the
requirement.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import stan.db as stan_db
import stan.dashboard.server as server
from stan.metrics.feature_cloud import (
    FeatureCloud,
    cloud_to_json,
    extract_feature_cloud,
    load_feature_cloud_json,
)


def _make_features_db(path: Path, rows: list[tuple]) -> None:
    """Build a minimal LcTimsMsFeature SQLite mirroring 4DFF output.

    rows: (MZ, Charge, RT, Mobility, Intensity).
    """
    con = sqlite3.connect(str(path))
    con.execute(
        """CREATE TABLE LcTimsMsFeature (
            Id INTEGER PRIMARY KEY,
            MZ REAL, Charge INTEGER, RT REAL,
            Mobility REAL, Intensity REAL
        )"""
    )
    con.executemany(
        "INSERT INTO LcTimsMsFeature (MZ, Charge, RT, Mobility, Intensity) "
        "VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()


def _insert_run(db_path: Path, run_id: str, raw_path: str,
                run_name: str = "test_run") -> None:
    with sqlite3.connect(str(db_path)) as con:
        con.execute(
            "INSERT INTO runs (id, instrument, run_name, run_date, raw_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, "timsTOF Test", run_name, "2026-08-26T12:00:00", raw_path),
        )
        con.commit()


# ─────────────────────────────────────────────────────────────
#  extract_feature_cloud
# ─────────────────────────────────────────────────────────────

def test_extract_groups_every_charge(tmp_path: Path) -> None:
    feat = tmp_path / "run.d.features"
    _make_features_db(feat, [
        (400.5, 2, 600.0, 0.90, 1e5),
        (500.7, 2, 700.0, 0.95, 2e5),
        (800.2, 1, 800.0, 1.25, 5e4),
        (450.0, 3, 650.0, 0.78, 8e4),
        (700.0, 0, 750.0, 1.10, 1e4),
    ])
    cloud = extract_feature_cloud(feat)
    assert cloud.n_points == 5
    assert cloud.n_total == 5
    bc = cloud.by_charge()
    assert set(bc) == {"0", "1", "2", "3"}
    assert len(bc["2"]["mz"]) == 2
    # Parallel arrays stay aligned inside a bucket.
    assert bc["1"]["mz"] == [800.2]
    assert bc["1"]["mobility"] == [1.25]
    assert bc["1"]["rt"] == [800.0]


def test_extract_drops_unusable_rows(tmp_path: Path) -> None:
    """Zero/negative m/z or mobility can't be plotted — drop, don't crash."""
    feat = tmp_path / "run.d.features"
    _make_features_db(feat, [
        (400.5, 2, 600.0, 0.90, 1e5),
        (0.0, 2, 601.0, 0.91, 1e5),     # no m/z
        (401.0, 2, 602.0, 0.0, 1e5),    # no mobility
        (402.0, 2, 603.0, 0.92, 0.0),   # zero intensity — filtered in SQL
    ])
    cloud = extract_feature_cloud(feat)
    assert cloud.n_points == 1
    assert cloud.mz == [400.5]


def test_extract_downsamples_below_cap(tmp_path: Path) -> None:
    """The stride must land under the cap, not merely near it."""
    feat = tmp_path / "big.d.features"
    _make_features_db(
        feat,
        [(400.0 + i * 0.01, 2, float(i), 0.9, 1e4) for i in range(1000)],
    )
    cloud = extract_feature_cloud(feat, max_points=300)
    assert cloud.n_total == 1000
    assert 0 < cloud.n_points <= 300


def test_extract_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        extract_feature_cloud(tmp_path / "nope.features")


def test_extract_missing_table_raises(tmp_path: Path) -> None:
    """A 4DFF run that aborted leaves a file with no LcTimsMsFeature."""
    feat = tmp_path / "aborted.d.features"
    sqlite3.connect(str(feat)).close()
    with pytest.raises(RuntimeError, match="LcTimsMsFeature"):
        extract_feature_cloud(feat)


# ─────────────────────────────────────────────────────────────
#  JSON cache round-trip
# ─────────────────────────────────────────────────────────────

def test_cache_round_trip(tmp_path: Path) -> None:
    feat = tmp_path / "run.d.features"
    _make_features_db(feat, [
        (400.5, 2, 600.0, 0.90, 1e5),
        (800.2, 1, 800.0, 1.25, 5e4),
    ])
    cloud = extract_feature_cloud(feat)
    cache = tmp_path / "cloud.json"
    cache.write_text(json.dumps(cloud_to_json(cloud, "run-1", "myrun")))

    back = load_feature_cloud_json(cache)
    assert back.mz == cloud.mz
    assert back.charge == cloud.charge
    assert back.n_total == cloud.n_total
    assert back.features_path == str(feat)


def test_cache_rejects_ragged_arrays(tmp_path: Path) -> None:
    """A truncated cache file must fail loudly, not store mismatched points."""
    cache = tmp_path / "bad.json"
    cache.write_text(json.dumps({
        "mz": [1.0, 2.0], "mobility": [0.9], "rt": [1.0, 2.0],
        "charge": [2, 2], "intensity": [1.0, 2.0],
    }))
    with pytest.raises(ValueError, match="ragged"):
        load_feature_cloud_json(cache)


# ─────────────────────────────────────────────────────────────
#  DB round-trip
# ─────────────────────────────────────────────────────────────

def test_insert_and_get_feature_cloud(tmp_path: Path) -> None:
    db_path = tmp_path / "stan.db"
    stan_db.init_db(db_path=db_path)
    n = stan_db.insert_feature_cloud(
        run_id="r1", mz=[400.5, 800.2], mobility=[0.9, 1.25],
        rt=[600.0, 800.0], charge=[2, 1], intensity=[1e5, 5e4],
        n_total=1234, features_path="/x/y.d.features", db_path=db_path,
    )
    assert n == 2
    got = stan_db.get_feature_cloud("r1", db_path=db_path)
    assert got["mz"] == [400.5, 800.2]
    assert got["charge"] == [2, 1]
    assert got["n_points"] == 2
    assert got["n_total"] == 1234
    assert got["features_path"] == "/x/y.d.features"


def test_insert_feature_cloud_is_idempotent(tmp_path: Path) -> None:
    """--force re-backfills must overwrite, not duplicate or fail."""
    db_path = tmp_path / "stan.db"
    stan_db.init_db(db_path=db_path)
    for mz in ([1.0], [2.0, 3.0]):
        stan_db.insert_feature_cloud(
            run_id="r1", mz=mz, mobility=[0.9] * len(mz),
            rt=[1.0] * len(mz), charge=[2] * len(mz),
            intensity=[1.0] * len(mz), db_path=db_path,
        )
    got = stan_db.get_feature_cloud("r1", db_path=db_path)
    assert got["mz"] == [2.0, 3.0]


def test_insert_feature_cloud_rejects_ragged(tmp_path: Path) -> None:
    db_path = tmp_path / "stan.db"
    stan_db.init_db(db_path=db_path)
    with pytest.raises(ValueError, match="equal length"):
        stan_db.insert_feature_cloud(
            run_id="r1", mz=[1.0, 2.0], mobility=[0.9],
            rt=[1.0, 2.0], charge=[2, 2], intensity=[1.0, 2.0],
            db_path=db_path,
        )


def test_get_feature_cloud_absent_returns_none(tmp_path: Path) -> None:
    db_path = tmp_path / "stan.db"
    stan_db.init_db(db_path=db_path)
    assert stan_db.get_feature_cloud("nope", db_path=db_path) is None


# ─────────────────────────────────────────────────────────────
#  Endpoint: stored cloud beats (and survives without) the sidecar
# ─────────────────────────────────────────────────────────────

def test_endpoint_serves_stored_cloud_without_raw_data(
    tmp_path: Path, monkeypatch,
) -> None:
    """The regression this whole feature exists for.

    raw_path points at a directory this host cannot see — exactly the
    fleet case (dashboard on the Mac, .d on the Flinders NFS export).
    Before v1.0.16 that returned has_features=False and the UI fell back
    to an empty SVG cloud.
    """
    db_path = tmp_path / "stan.db"
    stan_db.init_db(db_path=db_path)
    _insert_run(db_path, "run-remote",
                "/nfs/lssc0/flinders/nowhere/25aug26_HeLa.d", "25aug26_HeLa")
    stan_db.insert_feature_cloud(
        run_id="run-remote", mz=[400.5, 800.2, 450.0],
        mobility=[0.90, 1.25, 0.78], rt=[600.0, 800.0, 650.0],
        charge=[2, 1, 3], intensity=[1e5, 5e4, 8e4],
        n_total=9999, db_path=db_path,
    )
    monkeypatch.setattr(stan_db, "get_db_path", lambda: db_path)
    monkeypatch.setattr(server, "get_db_path", lambda: db_path)

    body = TestClient(server.app).get(
        "/api/runs/run-remote/features-by-charge"
    ).json()
    assert body["has_features"] is True
    assert body["from_store"] is True
    assert body["run_name"] == "25aug26_HeLa"
    assert body["n_features"] == 3
    assert body["n_total"] == 9999
    assert set(body["by_charge"]) == {"1", "2", "3"}
    assert body["mz_range"] == [400.5, 800.2]
    assert body["mobility_range"] == [0.78, 1.25]


def test_endpoint_falls_back_to_sidecar_when_nothing_stored(
    tmp_path: Path, monkeypatch,
) -> None:
    """No stored cloud → still read the local .features if it's there."""
    d = tmp_path / "local.d"
    d.mkdir()
    _make_features_db(d / "local.d.features", [(400.5, 2, 600.0, 0.90, 1e5)])

    db_path = tmp_path / "stan.db"
    stan_db.init_db(db_path=db_path)
    _insert_run(db_path, "run-local", str(d), "local")
    monkeypatch.setattr(stan_db, "get_db_path", lambda: db_path)
    monkeypatch.setattr(server, "get_db_path", lambda: db_path)

    body = TestClient(server.app).get(
        "/api/runs/run-local/features-by-charge"
    ).json()
    assert body["has_features"] is True
    assert body.get("from_store") is None
    assert body["n_features"] == 1


def test_endpoint_reason_names_both_missing_halves(
    tmp_path: Path, monkeypatch,
) -> None:
    """Neither stored nor on disk → say so, and say what to run."""
    d = tmp_path / "bare.d"
    d.mkdir()
    db_path = tmp_path / "stan.db"
    stan_db.init_db(db_path=db_path)
    _insert_run(db_path, "run-bare", str(d))
    monkeypatch.setattr(stan_db, "get_db_path", lambda: db_path)
    monkeypatch.setattr(server, "get_db_path", lambda: db_path)

    body = TestClient(server.app).get(
        "/api/runs/run-bare/features-by-charge"
    ).json()
    assert body["has_features"] is False
    assert "stan run-4dff" in body["reason"]
    assert "backfill-feature-cloud" in body["reason"]


def test_empty_cloud_is_not_served_as_present(tmp_path: Path, monkeypatch) -> None:
    """A zero-point row must fall through, not render an empty plot."""
    db_path = tmp_path / "stan.db"
    stan_db.init_db(db_path=db_path)
    _insert_run(db_path, "run-x", "/nowhere/x.d")
    with sqlite3.connect(str(db_path)) as con:
        con.execute(
            "INSERT INTO feature_clouds (run_id, source, mz, mobility, rt, "
            "charge, intensity, n_points, n_total) "
            "VALUES ('run-x','runs','[]','[]','[]','[]','[]',0,0)"
        )
    monkeypatch.setattr(stan_db, "get_db_path", lambda: db_path)
    monkeypatch.setattr(server, "get_db_path", lambda: db_path)
    body = TestClient(server.app).get(
        "/api/runs/run-x/features-by-charge"
    ).json()
    assert body["has_features"] is False


def test_by_charge_shape_matches_endpoint_contract() -> None:
    """FeatureCloud.by_charge must emit exactly what DriftCloudPlotly reads."""
    cloud = FeatureCloud(
        mz=[1.0], mobility=[0.9], rt=[10.0], charge=[2], intensity=[5.0],
    )
    bucket = cloud.by_charge()["2"]
    assert sorted(bucket) == ["intensity", "mobility", "mz", "rt"]
