"""No PG Farm code path may leave the cached connection inside a transaction.

On 2026-09-02 an `ALTER TABLE maintenance_events ADD COLUMN ...` could not run
for ~25 minutes. pg_stat_activity showed one session `idle in transaction`,
`ClientRead`, transaction age 19 minutes, last query the feature_clouds SELECT
from `stan.sync.pg_to_sqlite`. That module ran its statements on the
module-level cached connection from `stan.db_pg._connect()` without a `with`
block, and psycopg2's connection context manager is the only thing that ends a
transaction there -- so the read transaction opened by the first refresh tick
stayed open for the life of the dashboard process.

The cost was not the wait. A *queued* AccessExclusiveLock blocks every NEW
reader too, so the blocked ALTER took `maintenance_events` offline for the whole
app; the open snapshot also pinned VACUUM's cleanup horizon the entire time.

These tests pin the invariant behaviourally: run the mirror against a fake PG
connection that models transaction_status the way the server does, and assert
the connection is IDLE when the pull returns -- on the happy path and on the
failure path.
"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

# psycopg2's own constants, so a change in the driver can't silently
# invalidate what the fake is modelling.
from psycopg2.extensions import (
    TRANSACTION_STATUS_IDLE,
    TRANSACTION_STATUS_INERROR,
    TRANSACTION_STATUS_INTRANS,
)


# ── A PG connection that models transaction state ────────────────────────

class FakeCursor:
    """Minimal psycopg2 cursor: executing moves the connection to INTRANS."""

    def __init__(self, conn):
        self.conn = conn
        self._rows: list = []
        self.closed = False

    @property
    def connection(self):
        return self.conn

    def execute(self, sql, params=None):
        if self.conn.status == TRANSACTION_STATUS_INERROR:
            raise RuntimeError(
                "current transaction is aborted, commands ignored until end "
                "of transaction block"
            )
        self.conn.statements.append(" ".join(str(sql).split()))
        self.conn.status = TRANSACTION_STATUS_INTRANS
        try:
            self._rows = self.conn.responder(sql, params)
        except Exception:
            self.conn.status = TRANSACTION_STATUS_INERROR
            raise

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class FakeConn:
    """Enough psycopg2 connection to pin the transaction-state invariant."""

    def __init__(self, responder):
        self.responder = responder
        self.status = TRANSACTION_STATUS_IDLE
        self.statements: list[str] = []
        self.events: list[str] = []

    @property
    def info(self):
        return SimpleNamespace(transaction_status=self.status)

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.events.append("commit")
        self.status = TRANSACTION_STATUS_IDLE

    def rollback(self):
        self.events.append("rollback")
        self.status = TRANSACTION_STATUS_IDLE

    def close(self):
        self.events.append("close")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        # psycopg2's documented behaviour: commit/rollback, never close.
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False


# Column sets the mirror will find on both sides. Kept small on purpose --
# the point is the transaction state, not the copy.
_PG_RUNS_COLS = ["id", "instrument", "run_name", "run_date", "raw_path"]
_RUN_ROWS = [
    ("r1", "timsTOF HT", "20260902_793_hela", "2026-09-02", "/data/a.d"),
    ("r2", "timsTOF HT", "20260902_793_blank", "2026-09-02", "/data/b.d"),
]


def _responder(sql, params=None):
    """Answer the queries pull_from_pg actually issues."""
    s = " ".join(str(sql).split())
    if "information_schema.columns" in s and "table_name='runs'" in s:
        return [(c,) for c in _PG_RUNS_COLS]
    if "information_schema.columns" in s:
        # Every detail table reports "not migrated yet" -> skipped.
        return []
    if s.startswith("SELECT id, tic_rt_bins"):
        return []
    if "FROM runs" in s:
        return list(_RUN_ROWS)
    return []


@pytest.fixture()
def mirror_db(tmp_path):
    from stan.db import init_db
    p = tmp_path / "mirror.db"
    init_db(p)
    return p


def _patch_connect(monkeypatch, conn):
    """pull_from_pg imports _connect at call time, so patch the module attr."""
    import stan.db_pg as db_pg
    monkeypatch.setattr(db_pg, "_connect", lambda: conn)
    return conn


# ── The invariant ────────────────────────────────────────────────────────

def test_pull_from_pg_leaves_connection_idle(monkeypatch, mirror_db):
    """After a successful mirror pull the cached connection is NOT in a txn.

    This is the exact regression: the leaked session's last statement was the
    feature_clouds SELECT, i.e. the end of this function, and it never
    committed.
    """
    from stan.sync.pg_to_sqlite import pull_from_pg

    conn = _patch_connect(monkeypatch, FakeConn(_responder))
    written = pull_from_pg(db_path=mirror_db)

    assert conn.status == TRANSACTION_STATUS_IDLE, (
        f"connection left {conn.status} after pull_from_pg — this is the "
        f"idle-in-transaction leak. Statements: {conn.statements}"
    )
    assert written["runs"] == len(_RUN_ROWS)
    assert conn.events and conn.events[-1] in ("commit", "rollback")


def test_pull_from_pg_leaves_connection_idle_on_failure(monkeypatch, mirror_db):
    """A mid-pull PG error must still end the transaction, not strand it.

    Without the connection context manager an exception left the session
    `idle in transaction (aborted)` -- holding the same locks as a live one.
    """
    from stan.sync.pg_to_sqlite import pull_from_pg

    def boom(sql, params=None):
        s = " ".join(str(sql).split())
        if "information_schema.columns" in s and "table_name='runs'" in s:
            return [(c,) for c in _PG_RUNS_COLS]
        raise RuntimeError("connection reset by peer")

    conn = _patch_connect(monkeypatch, FakeConn(boom))
    with pytest.raises(RuntimeError):
        pull_from_pg(db_path=mirror_db)

    assert conn.status == TRANSACTION_STATUS_IDLE, (
        "a failed pull stranded the transaction — an aborted transaction "
        "holds locks exactly like a live one"
    )
    assert "rollback" in conn.events


def test_pull_from_pg_commits_before_the_slow_sqlite_write(monkeypatch, mirror_db):
    """Read locks are released before the local write, not after the pull.

    The SQLite side is the slow half (tens of MB of feature-cloud JSON on a
    first sync). Holding AccessShareLock and pinning VACUUM's horizon through
    it is what made a 5-minute refresh loop able to block a migration.
    """
    from stan.sync.pg_to_sqlite import pull_from_pg

    conn = _patch_connect(monkeypatch, FakeConn(_responder))
    pull_from_pg(db_path=mirror_db)

    assert conn.events.count("commit") >= 2, (
        "expected a commit after each PG read phase, got "
        f"{conn.events} for statements {conn.statements}"
    )


def test_pull_from_pg_actually_mirrors_rows(monkeypatch, mirror_db):
    """Guard against 'fixed' by not reading anything."""
    from stan.sync.pg_to_sqlite import pull_from_pg

    _patch_connect(monkeypatch, FakeConn(_responder))
    pull_from_pg(db_path=mirror_db)

    with sqlite3.connect(str(mirror_db)) as con:
        names = {r[0] for r in con.execute("SELECT run_name FROM runs")}
    assert names == {r[2] for r in _RUN_ROWS}


# ── The in-process alarm ─────────────────────────────────────────────────

def test_warn_if_left_in_transaction_flags_a_leak(caplog):
    """A cached connection handed back mid-transaction is reported."""
    import logging
    from stan.db_pg import _warn_if_left_in_transaction

    conn = FakeConn(_responder)
    conn.status = TRANSACTION_STATUS_INTRANS
    with caplog.at_level(logging.WARNING, logger="stan.db_pg"):
        assert _warn_if_left_in_transaction(conn) is True
    assert "inside a transaction" in caplog.text


def test_warn_if_left_in_transaction_quiet_when_idle(caplog):
    """The normal case must not spam a warning on every _connect()."""
    import logging
    from stan.db_pg import _warn_if_left_in_transaction

    conn = FakeConn(_responder)
    with caplog.at_level(logging.WARNING, logger="stan.db_pg"):
        assert _warn_if_left_in_transaction(conn) is False
    assert caplog.text == ""


def test_warn_never_raises_on_an_odd_connection():
    """Diagnostics must not be able to break _connect()."""
    from stan.db_pg import _warn_if_left_in_transaction

    class NoInfo:
        pass

    assert _warn_if_left_in_transaction(NoInfo()) is False


def test_connect_warns_but_does_not_roll_back(monkeypatch, caplog):
    """_connect reports a leaked transaction; it must not silently undo it.

    Rolling back here would discard an outer caller's work the moment anyone
    nests _connect() inside a `with _connect()` block. The auto-heal lives in
    the DB (idle_in_transaction_session_timeout), not here.
    """
    import logging
    import stan.db_pg as db_pg

    conn = FakeConn(_responder)
    conn.status = TRANSACTION_STATUS_INTRANS
    monkeypatch.setattr(db_pg, "_CACHED_CONN", conn)

    with caplog.at_level(logging.WARNING, logger="stan.db_pg"):
        got = db_pg._connect()

    assert got is conn
    assert "rollback" not in conn.events
    assert "inside a transaction" in caplog.text


# ── The visibility check ─────────────────────────────────────────────────

def test_idle_in_transaction_sessions_parses_rows(monkeypatch):
    """stan doctor gets pid, ages and last query for each stuck session."""
    import stan.db_pg as db_pg

    def responder(sql, params=None):
        assert "pg_stat_activity" in " ".join(str(sql).split())
        assert params == (db_pg.IDLE_TX_WARN_SECONDS,)
        return [(73581, "idle in transaction", "stan-dashboard", 1140, 164,
                 'SELECT "run_id", "source", "mz" FROM feature_clouds')]

    conn = FakeConn(responder)
    monkeypatch.setattr(db_pg, "_connect", lambda: conn)

    got = db_pg.idle_in_transaction_sessions()
    assert got == [{
        "pid": 73581,
        "state": "idle in transaction",
        "application_name": "stan-dashboard",
        "xact_age_s": 1140,
        "idle_s": 164,
        "last_query": 'SELECT "run_id", "source", "mz" FROM feature_clouds',
    }]
    # The probe itself must not become the thing it is looking for.
    assert conn.status == TRANSACTION_STATUS_IDLE


def test_idle_in_transaction_sessions_never_raises(monkeypatch):
    """An unreachable or unreadable PG must not break `stan doctor`."""
    import stan.db_pg as db_pg

    def no_pg():
        raise RuntimeError("no PG Farm password")

    monkeypatch.setattr(db_pg, "_connect", no_pg)
    assert db_pg.idle_in_transaction_sessions() == []


def test_pg_configured_is_offline_only(monkeypatch, tmp_path):
    """The doctor gate must not mint a JWT just to say 'no PG here'."""
    import stan.db_pg as db_pg

    def explode(_secret):
        raise AssertionError("pg_configured made a network call")

    monkeypatch.setattr(db_pg, "_mint_jwt", explode)

    monkeypatch.setenv("STAN_PGFARM_TOKEN_FILE", str(tmp_path / "absent"))
    monkeypatch.delenv("PGPASSWORD", raising=False)
    assert db_pg.pg_configured() is False

    monkeypatch.setenv("PGPASSWORD", "eyJhbGciOi.payload.sig")
    assert db_pg.pg_configured() is True
