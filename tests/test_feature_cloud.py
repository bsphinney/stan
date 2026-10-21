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


def _age(*paths: Path, seconds: float = 2 * 3600) -> None:
    """Backdate files past STALE_JOURNAL_AGE_S, as robocopy's kept mtimes are."""
    import os
    import time

    t = time.time() - seconds
    for p in paths:
        os.utime(p, (t, t))


def _leave_stale_journal(feat: Path, orig_pages: int | None = None,
                         age_s: float = 2 * 3600) -> Path:
    """Leave a header-only ``-journal`` beside ``feat`` that nothing holds.

    ``orig_pages`` is the header's "database size when the write began".
    tTOF run 24515 has 0 there, beside a finished 11,009-page sidecar
    (19,710 features): 4DFF wrote the whole file in one transaction and
    never deleted the journal. SQLite calls the journal hot, and a
    ``mode=ro`` open refuses the whole file, so shard 1 of the ion-cloud
    job failed every hour for four weeks on that one run.
    """
    con = sqlite3.connect(str(feat))
    page = con.execute("PRAGMA page_size").fetchone()[0]
    con.close()
    if orig_pages is None:
        orig_pages = feat.stat().st_size // page
    header = (
        bytes.fromhex("d9d505f920a163d7")         # journal magic
        + (0).to_bytes(4, "big")                  # nRec
        + (7).to_bytes(4, "big")                  # checksum nonce
        + orig_pages.to_bytes(4, "big")
        + (512).to_bytes(4, "big")                # sector size
        + page.to_bytes(4, "big")
    )
    journal = Path(f"{feat}-journal")
    journal.write_bytes(header.ljust(512, b"\0"))
    _age(feat, journal, seconds=age_s)
    return journal


def _crash_mid_write(feat: Path) -> None:
    """Kill a writer after it has overwritten pages it had not committed."""
    import subprocess
    import sys

    code = (
        "import os, sqlite3, sys\n"
        "con = sqlite3.connect(sys.argv[1], isolation_level=None)\n"
        "con.execute('PRAGMA cache_size=1')\n"     # spill pages to the file
        "con.execute('BEGIN')\n"
        "con.execute('UPDATE LcTimsMsFeature SET Intensity = 7')\n"
        "os._exit(0)\n"
    )
    subprocess.run([sys.executable, "-c", code, str(feat)],
                   check=True, timeout=60)


def _many_rows(n: int = 2000) -> list[tuple]:
    return [(400.0 + i * 0.01, 2, float(i), 0.9, 1e4) for i in range(n)]


def test_extract_reads_past_a_stale_journal_that_changed_nothing(
        tmp_path: Path) -> None:
    feat = tmp_path / "stuck.d.features"
    _make_features_db(feat, _many_rows())
    journal = _leave_stale_journal(feat)
    before = (feat.read_bytes(), journal.read_bytes())

    cloud = extract_feature_cloud(feat, max_points=10_000)

    assert cloud.n_total == 2000
    assert set(cloud.intensity) == {1e4}
    # Read as it lies: the raw .d is never written, journal included.
    assert (feat.read_bytes(), journal.read_bytes()) == before


def test_extract_reads_the_run_24515_shape(tmp_path: Path) -> None:
    """Journal began on an empty file; the finished file passes quick_check."""
    feat = tmp_path / "24515.d.features"
    _make_features_db(feat, _many_rows())
    journal = _leave_stale_journal(feat, orig_pages=0)
    before = (feat.read_bytes(), journal.read_bytes())

    cloud = extract_feature_cloud(feat, max_points=10_000)

    assert cloud.n_total == 2000
    assert (feat.read_bytes(), journal.read_bytes()) == before


def test_drift_reads_the_run_24515_shape_without_truncating_it(
        tmp_path: Path) -> None:
    """The drift detector used to open sidecars read-write, which here
    would let SQLite roll the journal back and cut the file to 0 bytes."""
    from stan.metrics.feature_drift import _query_z2_features

    feat = tmp_path / "24515.d.features"
    _make_features_db(feat, _many_rows())
    journal = _leave_stale_journal(feat, orig_pages=0)
    before = (feat.read_bytes(), journal.read_bytes())

    mz, _mob, _inten = _query_z2_features(feat)

    assert len(mz) == 2000
    assert (feat.read_bytes(), journal.read_bytes()) == before


def test_extract_refuses_an_unfinished_write_that_fails_quick_check(
        tmp_path: Path) -> None:
    feat = tmp_path / "torn.d.features"
    _make_features_db(feat, _many_rows(20_000))
    with feat.open("r+b") as fh:                  # lose the back half
        fh.truncate(feat.stat().st_size // 2)
    _leave_stale_journal(feat, orig_pages=0)

    with pytest.raises(RuntimeError, match="quick_check"):
        extract_feature_cloud(feat)


def test_extract_waits_while_a_4dff_may_still_be_writing(tmp_path: Path) -> None:
    """A journal and file touched in the last hour may have a live writer:
    the NFS locks that would say so are not trusted across nodes."""
    feat = tmp_path / "live.d.features"
    _make_features_db(feat, _many_rows())
    _leave_stale_journal(feat, orig_pages=0, age_s=0)

    with pytest.raises(RuntimeError, match="may still be writing"):
        extract_feature_cloud(feat)


def test_extract_refuses_a_journal_holding_pages(tmp_path: Path) -> None:
    """An interrupted write left uncommitted pages in the file itself.

    Reading those bytes as they lie would publish values that were never
    committed (Intensity 7 here) as the run's cloud, and it would be kept:
    the backfill never rebuilds a stored cloud.
    """
    feat = tmp_path / "crashed.d.features"
    _make_features_db(feat, _many_rows())
    _crash_mid_write(feat)
    journal = Path(f"{feat}-journal")
    assert journal.stat().st_size > 4096, "writer did not spill a page"
    _age(feat, journal)
    before = (feat.read_bytes(), journal.read_bytes())

    with pytest.raises(RuntimeError, match="never committed"):
        extract_feature_cloud(feat)

    assert (feat.read_bytes(), journal.read_bytes()) == before


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


# ─────────────────────────────────────────────────────────────
#  publish_feature_cloud — the ingest-path hook
# ─────────────────────────────────────────────────────────────

def test_publish_stores_cloud_for_a_bruker_d(tmp_path: Path) -> None:
    from stan.metrics.feature_cloud import publish_feature_cloud

    d = tmp_path / "run.d"
    d.mkdir()
    _make_features_db(d / "run.d.features", [
        (400.5, 2, 600.0, 0.90, 1e5),
        (800.2, 1, 800.0, 1.25, 5e4),
    ])
    db_path = tmp_path / "stan.db"
    stan_db.init_db(db_path=db_path)

    assert publish_feature_cloud(d, "run-1", db_path=db_path) == 2
    got = stan_db.get_feature_cloud("run-1", db_path=db_path)
    assert got["charge"] == [2, 1]


def test_publish_is_a_noop_without_a_sidecar(tmp_path: Path) -> None:
    """No sidecar is the normal case mid-4DFF — must not raise."""
    from stan.metrics.feature_cloud import publish_feature_cloud

    d = tmp_path / "run.d"
    d.mkdir()
    db_path = tmp_path / "stan.db"
    stan_db.init_db(db_path=db_path)
    assert publish_feature_cloud(d, "run-1", db_path=db_path) == 0


def test_publish_skips_thermo_and_never_raises(tmp_path: Path) -> None:
    """This sits in the ingest path; it may not fail a run for any reason."""
    from stan.metrics.feature_cloud import publish_feature_cloud

    thermo = tmp_path / "run.raw"
    thermo.write_bytes(b"")
    assert publish_feature_cloud(thermo, "run-1", db_path=tmp_path / "s.db") == 0
    # Nonexistent path, unwritable DB target — still 0, still no exception.
    assert publish_feature_cloud(tmp_path / "gone.d", "run-1") == 0


def test_default_cap_matches_the_drift_cloud_budget() -> None:
    """Both ion clouds share one storage budget — keep them in step.

    ``feature_clouds`` and ``drift_peak_clouds`` hold the same kind of
    thing for the same run, so letting their point caps drift apart makes
    a run's storage cost depend on which view happened to produce it.
    Raising this is a deliberate call (it is the biggest lever on table
    size: ~70 KB/run in PG at 5,000, ~3x that at 15,000), not something
    to bump in passing.
    """
    from stan.metrics.feature_cloud import DEFAULT_MAX_POINTS
    assert DEFAULT_MAX_POINTS == 5_000
