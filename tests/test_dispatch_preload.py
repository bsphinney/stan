"""Bulk dedup preload must agree with the per-file queries it replaces.

The dispatch walk used to run three queries per raw. In PG mode each is a
round-trip to PG Farm over SSL, so a tick scanning ~2,500 files issued
~2,500 remote queries — measured 2026-08-27, the linker phase took ~2 min
while the whole tick took 10-25 min, and every query also landed on the
instance FRAN shares. The preload must return exactly the same verdicts.
"""

from __future__ import annotations

import sqlite3

import pytest

from stan.community.scripts.dispatch_hive import (
    _already_health_processed,
    _already_processed,
    _failed_too_many,
    _preload_dedup_sets,
)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr("stan.db_pg.use_pg", lambda: False)
    p = tmp_path / "stan.db"
    con = sqlite3.connect(p)
    con.executescript(
        """
        CREATE TABLE runs (raw_path TEXT);
        CREATE TABLE sample_health (raw_path TEXT);
        CREATE TABLE dispatch_attempts (
            raw_path TEXT, status TEXT, attempt_count INT);
        INSERT INTO runs VALUES ('/data/qc_done.d');
        INSERT INTO sample_health VALUES ('/data/monitor_done.d');
        INSERT INTO dispatch_attempts VALUES ('/data/broken.d', 'failed', 3);
        INSERT INTO dispatch_attempts VALUES ('/data/flaky.d', 'failed', 1);
        INSERT INTO dispatch_attempts VALUES ('/data/fine.d', 'ok', 9);
        """
    )
    con.commit()
    con.close()
    return p


def test_preload_matches_per_file_queries(db):
    sets = _preload_dedup_sets(db, max_attempts=3)
    assert sets is not None

    from pathlib import Path
    cases = [
        ("/data/qc_done.d", "processed", _already_processed),
        ("/data/monitor_done.d", "health", _already_health_processed),
        ("/data/never_seen.d", "processed", _already_processed),
        ("/data/never_seen.d", "health", _already_health_processed),
    ]
    for raw, key, fn in cases:
        assert (raw in sets[key]) is fn(db, Path(raw)), f"{raw} via {key}"


def test_capped_set_matches_failed_too_many(db):
    from pathlib import Path
    sets = _preload_dedup_sets(db, max_attempts=3)
    for raw in ("/data/broken.d", "/data/flaky.d", "/data/fine.d", "/data/new.d"):
        assert (raw in sets["capped"]) is _failed_too_many(db, Path(raw), 3), raw


def test_capped_respects_max_attempts_threshold(db):
    """A higher cap must un-cap a raw that has not reached it yet."""
    assert "/data/broken.d" in _preload_dedup_sets(db, max_attempts=3)["capped"]
    assert "/data/broken.d" not in _preload_dedup_sets(db, max_attempts=9)["capped"]


def test_preload_returns_none_when_it_cannot_load(tmp_path, monkeypatch):
    """A failed preload must fall back, never crash the tick."""
    monkeypatch.setattr("stan.db_pg.use_pg", lambda: False)
    assert _preload_dedup_sets(tmp_path / "does_not_exist.db", 3) is None
