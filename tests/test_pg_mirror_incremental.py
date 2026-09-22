"""The PG -> SQLite mirror must not re-download data that has not changed.

PG Farm runs on Google Cloud and bills every byte a client reads out of it.
Until v1.1.8 the dashboard mirror copied ``runs``, every TIC array and all the
detail tables in full on every refresh tick -- 73 MB, every ~5.5 minutes, from
an always-on Azure instance: ~19 GB/day of egress for data that almost never
changes (``drift_peak_clouds`` alone was 40 MB a tick for 81 rows). The
Library flagged it on 2026-09-22.

These tests run the sync logic against an in-memory PG that models the one
thing the fingerprints rely on: every INSERT or UPDATE gives the row version a
fresh, larger transaction id (``xmin``), and a DELETE removes it. The SQL that
computes those fingerprints on the real server is exercised against live PG
Farm separately (see ``_PgSource``); the rules pinned here are:

* an unchanged table costs one fingerprint round trip and no row fetches;
* a change fetches only the keys whose fingerprint moved;
* deletions propagate, but only for rows the mirror itself brought in --
  a row a local watcher wrote is never deleted because PG lacks it;
* a mirrored row deleted locally is restored even when PG has not changed;
* an interrupted pull resumes instead of re-downloading everything.
"""
from __future__ import annotations

import sqlite3

import pytest


# ── An in-memory PG with xmin semantics ─────────────────────────────────


class FakePg:
    """Tables of rows, each row version stamped with a monotonically rising xid."""

    def __init__(self, columns: dict[str, list[str]]):
        self.columns_by_table = columns
        self.rows: dict[str, list[dict]] = {t: [] for t in columns}
        self._xid = 1000
        self.tic: dict[str, tuple[list, list]] = {}

    def _next(self) -> int:
        self._xid += 1
        return self._xid

    def insert(self, table: str, **row) -> None:
        row["_xmin"] = self._next()
        self.rows[table].append(row)

    def update(self, table: str, where: dict, **changes) -> None:
        xid = self._next()
        for r in self.rows[table]:
            if all(r.get(k) == v for k, v in where.items()):
                r.update(changes)
                r["_xmin"] = xid

    def delete(self, table: str, **where) -> None:
        self.rows[table] = [
            r for r in self.rows[table]
            if not all(r.get(k) == v for k, v in where.items())
        ]


class FakeSource:
    """Same interface as ``stan.sync.pg_to_sqlite._PgSource``, over a FakePg."""

    def __init__(self, pg: FakePg):
        self.pg = pg
        self.fetched_rows = 0
        self.fetch_calls: list[tuple[str, int]] = []
        self.key_list_calls: list[str] = []
        self.fail_fetch_on_call: int | None = None
        self.after_key_fps = None  # hook: mutate PG between the key read and the fetch

    def columns(self, table):
        return list(self.pg.columns_by_table.get(table, []))

    @staticmethod
    def _fp(rows):
        return ",".join(str(x) for x in sorted(r["_xmin"] for r in rows))

    def table_fp(self, table):
        rows = self.pg.rows[table]
        return f"{len(rows)}:{self._fp(rows)}"

    def key_fps(self, table, key_cols, since="", newest_first=False):
        self.key_list_calls.append(table)
        groups: dict[tuple, list] = {}
        for r in self.pg.rows[table]:
            if since and str(r.get("run_date", "")) < since:
                continue
            groups.setdefault(tuple(r[k] for k in key_cols), []).append(r)
        out = [(k, self._fp(v)) for k, v in groups.items()]
        if newest_first:
            dates = {r["id"]: r.get("run_date", "") for r in self.pg.rows.get("runs", [])}
            out.sort(key=lambda kv: dates.get(kv[0][0], ""), reverse=True)
        if self.after_key_fps is not None:
            self.after_key_fps(table)
        return out

    def fetch(self, table, cols, key_cols, keys):
        if self.fail_fetch_on_call is not None and len(self.fetch_calls) + 1 == self.fail_fetch_on_call:
            self.fetch_calls.append((table, -1))
            raise RuntimeError("connection reset by peer")
        wanted = {tuple(k) for k in keys}
        out = [
            tuple(r.get(c) for c in cols)
            for r in self.pg.rows[table]
            if tuple(r[k] for k in key_cols) in wanted
        ]
        self.fetch_calls.append((table, len(keys)))
        self.fetched_rows += len(out)
        return out

    def fetch_tic(self, ids):
        return [(i, *self.pg.tic[i]) for i in ids if i in self.pg.tic]


RUNS = ["id", "instrument", "run_name", "run_date", "raw_path"]
PEG = ["run_id", "source", "mz", "observed_intensity", "adduct", "repeat_n", "charge"]
SAMPLE = ["id", "instrument", "run_name", "run_date", "verdict"]


@pytest.fixture()
def local(tmp_path):
    from stan.db import init_db
    p = tmp_path / "mirror.db"
    init_db(p)
    con = sqlite3.connect(str(p))
    yield con
    con.close()


@pytest.fixture()
def pg():
    db = FakePg({"runs": RUNS, "peg_ion_hits": PEG, "sample_health": SAMPLE})
    for i in range(3):
        db.insert("runs", id=f"r{i}", instrument="timsTOF HT", run_name=f"hela_{i}",
                  run_date=f"2026-09-0{i + 1}", raw_path=f"/d/{i}.d")
        db.insert("peg_ion_hits", run_id=f"r{i}", source="ms1", mz=100.0 + i,
                  observed_intensity=1.0, adduct="H", repeat_n=1, charge=1)
    db.tic["r0"] = ([0.1, 0.2], [5.0, 6.0])
    return db


def _pull(local, src, since=""):
    from stan.sync.pg_to_sqlite import _pull
    return _pull(local, src, since=since)


def _names(local, table="runs", col="run_name"):
    return {r[0] for r in local.execute(f"SELECT {col} FROM {table}")}


# ── Steady state: nothing changed, nothing downloaded ───────────────────


def test_first_pull_copies_everything(local, pg):
    src = FakeSource(pg)
    written = _pull(local, src)
    assert _names(local) == {"hela_0", "hela_1", "hela_2"}
    assert written["runs"] == 3
    assert written["peg_ion_hits"] == 3
    assert written["tic_traces"] == 1
    assert local.execute("SELECT rt_min FROM tic_traces WHERE run_id='r0'").fetchone()[0] == "[0.1, 0.2]"


def test_unchanged_pg_costs_no_row_fetches(local, pg):
    """The regression this file exists for: a quiet tick must move ~nothing."""
    _pull(local, FakeSource(pg))
    src = FakeSource(pg)
    written = _pull(local, src)
    assert src.fetch_calls == [], f"unchanged tables were re-fetched: {src.fetch_calls}"
    assert src.key_list_calls == [], "an unchanged table should not even list its keys"
    assert written["runs"] == 0 and written["peg_ion_hits"] == 0


# ── Changes fetch only what moved ────────────────────────────────────────


def test_new_run_fetches_only_that_run(local, pg):
    _pull(local, FakeSource(pg))
    pg.insert("runs", id="r9", instrument="timsTOF HT", run_name="hela_9",
              run_date="2026-09-09", raw_path="/d/9.d")
    src = FakeSource(pg)
    written = _pull(local, src)
    assert written["runs"] == 1
    assert src.fetch_calls == [("runs", 1)]
    assert "hela_9" in _names(local)


def test_updated_row_is_refetched(local, pg):
    _pull(local, FakeSource(pg))
    pg.update("runs", {"id": "r1"}, run_name="hela_1_renamed")
    src = FakeSource(pg)
    _pull(local, src)
    assert src.fetch_calls == [("runs", 1)]
    assert "hela_1_renamed" in _names(local) and "hela_1" not in _names(local)


def test_changed_group_is_replaced_not_appended(local, pg):
    """Multi-row groups (peg hits per run) must end up equal to PG's, no leftovers."""
    _pull(local, FakeSource(pg))
    pg.delete("peg_ion_hits", run_id="r1")
    pg.insert("peg_ion_hits", run_id="r1", source="ms1", mz=555.0,
              observed_intensity=2.0, adduct="Na", repeat_n=2, charge=1)
    _pull(local, FakeSource(pg))
    rows = local.execute("SELECT mz FROM peg_ion_hits WHERE run_id='r1'").fetchall()
    assert rows == [(555.0,)]


def test_many_changes_are_fetched_in_chunks(local, pg, monkeypatch):
    import stan.sync.pg_to_sqlite as m
    monkeypatch.setitem(m._MIRRORED, "runs", (("id",), 2))
    for i in range(10, 17):
        pg.insert("runs", id=f"r{i}", instrument="timsTOF HT", run_name=f"hela_{i}",
                  run_date="2026-09-10", raw_path=f"/d/{i}.d")
    src = FakeSource(pg)
    _pull(local, src)
    run_fetches = [n for t, n in src.fetch_calls if t == "runs"]
    assert max(run_fetches) <= 2 and sum(run_fetches) == 10
    assert len(_names(local)) == 10


# ── Deletions: propagate for mirrored rows, never for local ones ────────


def test_row_deleted_in_pg_is_deleted_locally(local, pg):
    _pull(local, FakeSource(pg))
    pg.delete("runs", id="r2")
    pg.delete("peg_ion_hits", run_id="r2")
    _pull(local, FakeSource(pg))
    assert "hela_2" not in _names(local)
    assert local.execute("SELECT count(*) FROM peg_ion_hits WHERE run_id='r2'").fetchone()[0] == 0


def test_local_only_row_survives(local, pg):
    """A run the local watcher wrote is not the mirror's to delete."""
    local.execute(
        "INSERT INTO runs (id, instrument, run_name, run_date, raw_path) "
        "VALUES ('local1', 'Exploris', 'local_only', '2026-09-05', '/x.raw')"
    )
    local.commit()
    _pull(local, FakeSource(pg))
    pg.delete("runs", id="r0")
    _pull(local, FakeSource(pg))
    assert "local_only" in _names(local)
    assert "hela_0" not in _names(local)


def test_mirrored_row_deleted_locally_is_restored(local, pg):
    _pull(local, FakeSource(pg))
    local.execute("DELETE FROM runs WHERE id='r1'")
    local.commit()
    src = FakeSource(pg)
    _pull(local, src)
    assert "hela_1" in _names(local)
    assert src.fetch_calls == [("runs", 1)]


def test_since_pull_is_partial_and_says_so(local, pg):
    """A windowed pull must not record the table as fully in sync."""
    _pull(local, FakeSource(pg), since="2026-09-02")
    assert _names(local) == {"hela_1", "hela_2"}
    # PG has not changed, but r0 was never mirrored: an unfiltered pull must
    # still compare keys rather than skip on the table fingerprint.
    _pull(local, FakeSource(pg))
    assert _names(local) == {"hela_0", "hela_1", "hela_2"}


def test_since_never_deletes_outside_its_window(local, pg):
    _pull(local, FakeSource(pg))
    _pull(local, FakeSource(pg), since="2026-09-03")
    assert _names(local) == {"hela_0", "hela_1", "hela_2"}, "runs outside the window were deleted"


# ── Robustness ───────────────────────────────────────────────────────────


def test_interrupted_pull_resumes_without_refetching_done_chunks(local, pg, monkeypatch):
    import stan.sync.pg_to_sqlite as m
    monkeypatch.setitem(m._MIRRORED, "runs", (("id",), 1))
    src = FakeSource(pg)
    src.fail_fetch_on_call = 3  # r0, r1 land; the third fetch dies
    with pytest.raises(RuntimeError):
        _pull(local, src)
    assert len(_names(local)) == 2
    src2 = FakeSource(pg)
    _pull(local, src2)
    assert _names(local) == {"hela_0", "hela_1", "hela_2"}
    assert [n for t, n in src2.fetch_calls if t == "runs"] == [1], (
        "a resumed pull re-downloaded chunks that had already landed"
    )


def test_table_missing_in_pg_is_skipped(local, pg):
    written = _pull(local, FakeSource(pg))
    assert "drift_peak_clouds" not in written  # not in the fake's schema -> not migrated


def test_tic_only_for_changed_runs(local, pg):
    _pull(local, FakeSource(pg))
    local.execute("UPDATE tic_traces SET rt_min='[9.9]' WHERE run_id='r0'")
    local.commit()
    pg.insert("runs", id="r5", instrument="timsTOF HT", run_name="hela_5",
              run_date="2026-09-05", raw_path="/d/5.d")
    pg.tic["r5"] = ([1.0], [2.0])
    written = _pull(local, FakeSource(pg))
    assert written["tic_traces"] == 1
    # r0 did not change, so its (locally edited) TIC was not re-downloaded.
    assert local.execute("SELECT rt_min FROM tic_traces WHERE run_id='r0'").fetchone()[0] == "[9.9]"


def test_malformed_tic_does_not_block_runs(local, pg):
    """A bad TIC value must cost that trace, not the whole runs sync."""
    pg.tic["r0"] = ("not json at all", "[1]")
    pg.tic["r1"] = (5, 6)  # a scalar JSONB: psycopg2 hands back an int
    written = _pull(local, FakeSource(pg))
    assert _names(local) == {"hela_0", "hela_1", "hela_2"}
    assert written["tic_traces"] == 0


# ── Changes the fingerprints cannot see ─────────────────────────────────


def test_new_local_column_triggers_full_refetch(local, pg):
    """PG holds data in a column the local schema only gains later.

    The normal rollout order: Hive migrates and backfills first, a surface
    upgrades days later. No PG row changed, so no xmin moved -- the shape of
    what is copied did, and that must restart the table.
    """
    pg.columns_by_table["runs"].append("zz_added")
    for r in pg.rows["runs"]:
        r["zz_added"] = "filled"
    _pull(local, FakeSource(pg))
    local.execute("ALTER TABLE runs ADD COLUMN zz_added TEXT")
    local.commit()
    _pull(local, FakeSource(pg))
    vals = {r[0] for r in local.execute("SELECT zz_added FROM runs")}
    assert vals == {"filled"}, "rows copied before the column existed were never refreshed"


def test_rekeying_a_table_restarts_it_cleanly(local, pg, monkeypatch):
    import stan.sync.pg_to_sqlite as m
    _pull(local, FakeSource(pg))
    monkeypatch.setitem(m._MIRRORED, "peg_ion_hits", (("run_id",), 200))
    pg.delete("peg_ion_hits", run_id="r2")
    written = _pull(local, FakeSource(pg))
    assert "peg_ion_hits" in written, "re-keyed table stopped syncing"
    got = {r[0] for r in local.execute("SELECT run_id FROM peg_ion_hits")}
    assert got == {"r0", "r1"}


def test_change_during_pull_is_caught_next_tick(local, pg):
    """Fingerprints must come from the read BEFORE the fetch, never after."""
    src = FakeSource(pg)

    def mutate(table):
        if table == "runs":
            src.after_key_fps = None
            pg.update("runs", {"id": "r1"}, run_name="hela_1_mid_pull")

    src.after_key_fps = mutate
    _pull(local, src)
    src2 = FakeSource(pg)
    _pull(local, src2)
    assert ("runs", 1) in src2.fetch_calls, "a row changed mid-pull was recorded as in sync"
    assert "hela_1_mid_pull" in _names(local)


# ── feature_clouds: the fat table, drained newest-first ─────────────────

CLOUDS = ["run_id", "source", "mz", "mobility", "rt", "charge", "intensity", "n_points", "n_total"]


@pytest.fixture()
def cloud_pg(pg):
    pg.columns_by_table["feature_clouds"] = list(CLOUDS)
    pg.rows["feature_clouds"] = []
    for i in range(3):
        pg.insert("feature_clouds", run_id=f"r{i}", source="runs", mz="[1]", mobility="[1]",
                  rt="[1]", charge="[2]", intensity="[9]", n_points=1, n_total=1)
    return pg


def _clouds(local):
    return {r[0]: r[1] for r in local.execute("SELECT run_id, intensity FROM feature_clouds")}


def test_clouds_drain_newest_first_then_go_quiet(local, cloud_pg, monkeypatch):
    monkeypatch.setenv("STAN_PG_CLOUD_MAX_PULL", "2")
    _pull(local, FakeSource(cloud_pg))
    assert set(_clouds(local)) == {"r2", "r1"}, "the newest runs should light up first"
    _pull(local, FakeSource(cloud_pg))
    assert set(_clouds(local)) == {"r0", "r1", "r2"}
    src = FakeSource(cloud_pg)
    _pull(local, src)
    assert not [c for c in src.fetch_calls if c[0] == "feature_clouds"]


def test_rebackfilled_cloud_is_refetched(local, cloud_pg):
    """Before v1.1.8 this needed STAN_PG_CLOUD_FULL_REFRESH; now it is automatic."""
    _pull(local, FakeSource(cloud_pg))
    cloud_pg.update("feature_clouds", {"run_id": "r1"}, intensity="[42]")
    src = FakeSource(cloud_pg)
    _pull(local, src)
    assert _clouds(local)["r1"] == "[42]"
    assert ("feature_clouds", 1) in src.fetch_calls


def test_cloud_deleted_in_pg_is_deleted_locally(local, cloud_pg):
    _pull(local, FakeSource(cloud_pg))
    cloud_pg.delete("feature_clouds", run_id="r0")
    _pull(local, FakeSource(cloud_pg))
    assert set(_clouds(local)) == {"r1", "r2"}


def test_full_refresh_flag_no_longer_leaks(local, cloud_pg, monkeypatch):
    """The old flag re-downloaded the newest 50 clouds every tick, forever."""
    monkeypatch.setenv("STAN_PG_CLOUD_FULL_REFRESH", "1")
    _pull(local, FakeSource(cloud_pg))
    src = FakeSource(cloud_pg)
    _pull(local, src)
    assert not [c for c in src.fetch_calls if c[0] == "feature_clouds"]


# ── The SQL itself (FakeSource bypasses it, so pin it here) ─────────────


class _RecordingCursor:
    def __init__(self, rows=None):
        self.calls: list[tuple[str, object]] = []
        self.rows = rows if rows is not None else [(3, "abc")]
        self.commits = 0
        self.connection = self

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_pgsource_sql_shapes():
    from stan.sync.pg_to_sqlite import _PgSource

    cur = _RecordingCursor()
    src = _PgSource(cur)
    assert src.table_fp("runs") == "3:abc"
    sql, params = cur.calls[-1]
    # xid has no ordering operator: the ::bigint cast is what makes this run.
    assert "string_agg(xmin::text, ',' ORDER BY xmin::text::bigint)" in sql
    assert sql.startswith("SELECT count(*), md5(") and sql.endswith("FROM runs")
    assert params is None

    cur.rows = [("a", "b", "fp1")]
    assert src.key_fps("peg_ion_hits", ("run_id", "source")) == [(("a", "b"), "fp1")]
    sql, _ = cur.calls[-1]
    assert sql.endswith('GROUP BY "run_id", "source"')

    cur.rows = [("r1", "fp1")]
    src.key_fps("runs", ("id",), since="2026-09-01")
    sql, params = cur.calls[-1]
    assert "WHERE run_date >= %s GROUP BY" in sql and params == ("2026-09-01",)

    src.fetch("peg_ion_hits", ["run_id", "mz"], ("run_id", "source"), [("a", "b")])
    sql, params = cur.calls[-1]
    assert 'WHERE ("run_id", "source") IN %s' in sql and params == ((("a", "b"),),)

    src.fetch("runs", ["id"], ("id",), [("r1",), ("r2",)])
    sql, params = cur.calls[-1]
    assert 'WHERE "id" IN %s' in sql and params == (("r1", "r2"),)

    assert cur.commits == len(cur.calls), "every read must end its transaction"
