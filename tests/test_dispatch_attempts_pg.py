"""dispatch_attempts must go to PG Farm when the PG backend is configured.

This table was the last one still written to the Quobyte SQLite file, and it is
where STAN's recurring index corruption kept landing -- twice on 2026-09-01,
the first occurrence taking down 7,357 jobs. These tests pin the routing so a
refactor cannot silently send it back to SQLite.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


def test_record_dispatch_attempt_routes_to_pg(monkeypatch, tmp_path):
    """With use_pg() true, the write goes to PG and never touches SQLite."""
    import stan.db as db
    import stan.db_pg as db_pg

    calls = []
    monkeypatch.setattr(db_pg, "use_pg", lambda: True)
    monkeypatch.setattr(db_pg, "record_dispatch_attempt_pg",
                        lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(db_pg, "host_origin_from_instrument", lambda _i: "timstof")

    sqlite_db = tmp_path / "should_not_be_touched.db"
    db.record_dispatch_attempt("/data/x.d", "failed", "boom", "RuntimeError",
                               db_path=sqlite_db)

    assert len(calls) == 1, "PG writer was not called"
    assert calls[0][0][0] == "/data/x.d"
    assert calls[0][0][1] == "failed"
    assert not sqlite_db.exists(), "SQLite was written despite use_pg()"


def test_record_dispatch_attempt_falls_back_to_sqlite(monkeypatch, tmp_path):
    """A PG failure must not lose the record, and must never raise."""
    import stan.db as db
    import stan.db_pg as db_pg

    def boom(*a, **k):
        raise RuntimeError("PG unreachable")

    monkeypatch.setattr(db_pg, "use_pg", lambda: True)
    monkeypatch.setattr(db_pg, "record_dispatch_attempt_pg", boom)
    monkeypatch.setattr(db_pg, "host_origin_from_instrument", lambda _i: "timstof")

    sqlite_db = tmp_path / "fallback.db"
    db.init_db(sqlite_db)
    db.record_dispatch_attempt("/data/y.d", "failed", db_path=sqlite_db)

    with sqlite3.connect(str(sqlite_db)) as con:
        rows = con.execute(
            "SELECT raw_path, status FROM dispatch_attempts").fetchall()
    assert ("/data/y.d", "failed") in rows, "fallback did not write to SQLite"


def test_capped_reader_prefers_pg(monkeypatch, tmp_path):
    """The dispatcher's retry-cap query reads PG when configured."""
    from stan.community.scripts import dispatch_hive
    import stan.db_pg as db_pg

    monkeypatch.setattr(db_pg, "use_pg", lambda: True)
    monkeypatch.setattr(db_pg, "capped_raws_pg", lambda n: {"/data/capped.d"})

    got = dispatch_hive._capped_from_sqlite(tmp_path / "unused.db", 3)
    assert got == {"/data/capped.d"}


def test_capped_reader_empty_when_nothing_recorded(monkeypatch, tmp_path):
    """A missing table means 'nothing recorded', not 'skip everything'.

    Failing closed here would make the dispatcher treat every raw as capped
    and silently stop dispatching.
    """
    from stan.community.scripts import dispatch_hive
    import stan.db_pg as db_pg

    monkeypatch.setattr(db_pg, "use_pg", lambda: False)
    assert dispatch_hive._capped_from_sqlite(tmp_path / "absent.db", 3) == set()
