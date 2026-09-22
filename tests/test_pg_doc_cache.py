"""The published-document readers must not re-download an unchanged document.

`evosep_column_health` (864 KB as text) and `bruker_maintenance` (77 KB) are
single-row documents that change about once a day, and were read in full every
20 minutes by the Hive watchdog, every 30 by cron_evosep.sh, and on every page
view by the hosted dashboard. PG Farm runs on Google Cloud and bills every
byte a client reads out of it; measured 2026-09-22 that was ~72 MB/day from the
crons alone, to learn nothing had changed.

The fix is a cache keyed on the row's `xmin` -- the id of the transaction that
wrote the row version. ANY write, by any writer, produces a new row version
with a new xmin, so "xmin unchanged" means "the same document PG holds", not
"probably the same". That matters because one of these readers is the
watchdog: it has to see exactly what PG serves, never a projection of it
(CLAUDE.md, "A watchdog must not live inside what it watches").

These tests pin:
  * a hit costs the ~100-byte xmin probe and nothing else;
  * a changed xmin always re-fetches, so the caller sees the new document;
  * the file cache (for short-lived cron processes) is optional, atomic, and
    a corrupt or missing file is a miss, never an error or a wrong answer;
  * no path leaves the shared connection inside a transaction.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from psycopg2.extensions import (
    TRANSACTION_STATUS_IDLE,
    TRANSACTION_STATUS_INERROR,
    TRANSACTION_STATUS_INTRANS,
)

import stan.db_pg as db_pg


# ── a fake PG that holds single-row documents ────────────────────────────

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._rows: list = []

    def execute(self, sql, params=None):
        if self.conn.status == TRANSACTION_STATUS_INERROR:
            raise RuntimeError("current transaction is aborted")
        norm = " ".join(str(sql).split())
        self.conn.statements.append(norm)
        self.conn.status = TRANSACTION_STATUS_INTRANS
        try:
            self._rows = self.conn.respond(norm)
        except Exception:
            self.conn.status = TRANSACTION_STATUS_INERROR
            raise

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DocPG:
    """Tables -> (xmin, doc). `None` for a table means it does not exist."""

    def __init__(self, tables: dict):
        self.tables = tables
        self.status = TRANSACTION_STATUS_IDLE
        self.statements: list[str] = []

    # the server side
    def respond(self, sql: str):
        for table, row in self.tables.items():
            if f"FROM {table} " not in sql + " ":
                continue
            if row is None:
                raise RuntimeError(f'relation "{table}" does not exist')
            xmin, doc = row
            if doc is None:
                return []
            if "doc" in sql.split("FROM")[0]:
                return [(xmin, json.dumps(doc))]
            return [(xmin,)]
        raise AssertionError(f"unexpected SQL: {sql}")

    def write(self, table: str, doc: dict):
        """Any writer: new row version, new xmin."""
        xmin, _ = self.tables[table]
        self.tables[table] = (str(int(xmin) + 1), doc)

    def doc_fetches(self, table: str) -> int:
        return sum(1 for s in self.statements
                   if f"FROM {table}" in s and "doc" in s.split("FROM")[0])

    # the connection side
    @property
    def info(self):
        return SimpleNamespace(transaction_status=self.status)

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.status = TRANSACTION_STATUS_IDLE

    def rollback(self):
        self.status = TRANSACTION_STATUS_IDLE

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


EVOSEP = {"summary": {"last_run": "2026-09-22T10:00:00", "n_runs": 568},
          "runs": [{"run": "r1", "pressure_bar": 312.5, "flags": []}]}
BRUKER = {"backup_date": "2026-09-21", "instrument": "timsTOF HT"}


@pytest.fixture
def pg(monkeypatch, tmp_path):
    """Fresh fake PG, empty in-process cache, no file cache unless asked."""
    fake = DocPG({
        "evosep_column_health": ("5000", EVOSEP),
        "bruker_maintenance": ("4000", BRUKER),
    })
    monkeypatch.setattr(db_pg, "_connect", lambda: fake)
    monkeypatch.setattr(db_pg, "_DOC_CACHE", {})
    monkeypatch.delenv("STAN_PG_DOC_CACHE_DIR", raising=False)
    return fake


def _forget_process_cache(monkeypatch):
    """What a new cron process looks like: nothing in memory."""
    monkeypatch.setattr(db_pg, "_DOC_CACHE", {})


# ── in-process cache ─────────────────────────────────────────────────────

def test_first_read_returns_the_document(pg):
    assert db_pg.get_evosep_column_health_pg() == EVOSEP
    assert db_pg.get_bruker_maintenance_pg() == BRUKER


def test_unchanged_document_is_not_downloaded_again(pg):
    db_pg.get_evosep_column_health_pg()
    for _ in range(5):
        assert db_pg.get_evosep_column_health_pg() == EVOSEP
    assert pg.doc_fetches("evosep_column_health") == 1, (
        "every read after the first must cost only the xmin probe")
    probes = [s for s in pg.statements
              if s == "SELECT xmin::text FROM evosep_column_health WHERE id = 1"]
    assert len(probes) == 6


def test_any_write_is_seen_on_the_next_read(pg):
    """The watchdog must see what PG holds -- a new write is a new document."""
    assert db_pg.get_evosep_column_health_pg() == EVOSEP
    newer = {"summary": {"last_run": "2026-09-23T09:00:00", "n_runs": 569}}
    pg.write("evosep_column_health", newer)
    assert db_pg.get_evosep_column_health_pg() == newer
    assert pg.doc_fetches("evosep_column_health") == 2


def test_rewrite_of_identical_content_is_still_a_new_version(pg):
    """Keyed on xmin, not content or updated_at: every writer is caught."""
    db_pg.get_bruker_maintenance_pg()
    pg.write("bruker_maintenance", dict(BRUKER))
    db_pg.get_bruker_maintenance_pg()
    assert pg.doc_fetches("bruker_maintenance") == 2


def test_tables_are_cached_independently(pg):
    db_pg.get_evosep_column_health_pg()
    db_pg.get_bruker_maintenance_pg()
    pg.write("bruker_maintenance", {"backup_date": "2026-09-22"})
    assert db_pg.get_evosep_column_health_pg() == EVOSEP
    assert db_pg.get_bruker_maintenance_pg() == {"backup_date": "2026-09-22"}
    assert pg.doc_fetches("evosep_column_health") == 1


def test_callers_cannot_corrupt_the_cache(pg):
    """The dashboard and the alerter share one process-wide cache; a caller
    mutating what it was handed must not change what the next caller gets."""
    first = db_pg.get_evosep_column_health_pg()
    first["summary"]["n_runs"] = -1
    first["runs"].clear()
    assert db_pg.get_evosep_column_health_pg() == EVOSEP


def test_missing_table_is_none_and_leaves_no_transaction(pg):
    pg.tables["evosep_column_health"] = None
    assert db_pg.get_evosep_column_health_pg() is None
    assert pg.status == TRANSACTION_STATUS_IDLE


def test_missing_row_is_none(pg):
    pg.tables["bruker_maintenance"] = ("4000", None)
    assert db_pg.get_bruker_maintenance_pg() is None


@pytest.mark.parametrize("warm", [False, True])
def test_connection_is_idle_after_hit_and_miss(pg, warm):
    if warm:
        db_pg.get_evosep_column_health_pg()
    db_pg.get_evosep_column_health_pg()
    assert pg.status == TRANSACTION_STATUS_IDLE


# ── optional file cache (cron processes) ─────────────────────────────────

@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "cache"
    monkeypatch.setenv("STAN_PG_DOC_CACHE_DIR", str(d))
    return d


def test_no_file_is_written_without_the_env_var(pg, tmp_path):
    db_pg.get_evosep_column_health_pg()
    assert not any(tmp_path.rglob("*")), "file cache must be opt-in"


def test_file_cache_serves_a_new_process(pg, cache_dir, monkeypatch):
    """Each cron tick is a fresh process; the memory cache helps it not at all."""
    db_pg.get_evosep_column_health_pg()
    _forget_process_cache(monkeypatch)
    assert db_pg.get_evosep_column_health_pg() == EVOSEP
    assert pg.doc_fetches("evosep_column_health") == 1


def test_file_cache_is_refreshed_when_pg_changes(pg, cache_dir, monkeypatch):
    db_pg.get_evosep_column_health_pg()
    newer = {"summary": {"last_run": "2026-09-23T09:00:00"}}
    pg.write("evosep_column_health", newer)
    _forget_process_cache(monkeypatch)
    assert db_pg.get_evosep_column_health_pg() == newer
    _forget_process_cache(monkeypatch)
    assert db_pg.get_evosep_column_health_pg() == newer
    assert pg.doc_fetches("evosep_column_health") == 2


@pytest.mark.parametrize("damage", [
    "garbage",
    "truncate",
    "flip_a_byte",
    "empty",
])
def test_corrupt_file_is_a_miss(pg, cache_dir, monkeypatch, damage):
    db_pg.get_evosep_column_health_pg()
    [f] = [p for p in cache_dir.iterdir() if "evosep_column_health" in p.name]
    raw = f.read_bytes()
    if damage == "garbage":
        f.write_bytes(b"\x00\xffnot a cache file")
    elif damage == "truncate":
        f.write_bytes(raw[: len(raw) // 2])
    elif damage == "flip_a_byte":
        # Same xmin in the header, body silently altered: must not be trusted.
        i = raw.rindex(b"312.5")
        f.write_bytes(raw[:i] + b"999.9" + raw[i + 5:])
    else:
        f.write_bytes(b"")
    _forget_process_cache(monkeypatch)
    assert db_pg.get_evosep_column_health_pg() == EVOSEP
    assert pg.doc_fetches("evosep_column_health") == 2
    # ...and the bad file has been replaced with a good one.
    _forget_process_cache(monkeypatch)
    assert db_pg.get_evosep_column_health_pg() == EVOSEP
    assert pg.doc_fetches("evosep_column_health") == 2


def test_missing_file_is_a_miss(pg, cache_dir, monkeypatch):
    db_pg.get_evosep_column_health_pg()
    for p in cache_dir.iterdir():
        p.unlink()
    _forget_process_cache(monkeypatch)
    assert db_pg.get_evosep_column_health_pg() == EVOSEP
    assert pg.doc_fetches("evosep_column_health") == 2


def test_file_write_is_atomic(pg, cache_dir, monkeypatch):
    """A failed rename leaves the previous good file and no temp debris."""
    db_pg.get_evosep_column_health_pg()
    [good] = list(cache_dir.iterdir())
    before = good.read_bytes()

    def _boom(*a, **k):
        raise OSError("quobyte hiccup")

    monkeypatch.setattr(db_pg.os, "replace", _boom)
    pg.write("evosep_column_health", {"summary": {"n_runs": 1}})
    _forget_process_cache(monkeypatch)
    # The read itself still succeeds -- a cache problem is never a read problem.
    assert db_pg.get_evosep_column_health_pg() == {"summary": {"n_runs": 1}}
    assert list(cache_dir.iterdir()) == [good]
    assert good.read_bytes() == before


def test_unwritable_cache_dir_does_not_break_the_read(pg, tmp_path, monkeypatch):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    monkeypatch.setenv("STAN_PG_DOC_CACHE_DIR", str(blocker / "cache"))
    assert db_pg.get_bruker_maintenance_pg() == BRUKER


def test_file_cache_is_private(pg, cache_dir):
    """The Evosep document carries customer sample names (see
    _EVOSEP_IDENTIFYING_FIELDS in the dashboard); the cache copy must not be
    world-readable on the shared volume."""
    db_pg.get_evosep_column_health_pg()
    for p in cache_dir.iterdir():
        assert p.stat().st_mode & 0o077 == 0, oct(p.stat().st_mode)


def test_file_from_another_database_is_a_miss(pg, cache_dir, monkeypatch):
    """xmin is per-cluster: the same number in another database is a different row."""
    db_pg.get_evosep_column_health_pg()
    monkeypatch.setitem(db_pg.PG_DEFAULTS, "database", "some-other-db")
    _forget_process_cache(monkeypatch)
    assert db_pg.get_evosep_column_health_pg() == EVOSEP
    assert pg.doc_fetches("evosep_column_health") == 2, (
        "a cache file written against another database was trusted")
