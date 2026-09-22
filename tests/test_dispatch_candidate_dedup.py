"""The dispatcher asks PG about the raws it walked, not for every raw PG knows.

Every 5 minutes `stan hive-dispatch` used to download every `raw_path` in
`runs` (4,642 rows, 504 KB) and in `sample_health` (3,854 rows, 402 KB), plus
the capped `dispatch_attempts`, to learn that 0 of ~4,138 walked raws were
new. PG Farm bills every byte a client reads, so that was ~263 MB/day spent on
the answer "nothing to do". Measured 2026-09-22.

Now the walked candidates go UP (ingress is free) and only the unknown ones
come back. `processed = candidates - unknown` keeps the membership-test
semantics the rest of the dispatcher was written against.

The property that matters more than the bytes: a PG failure must NEVER read as
"everything is new". That would resubmit up to max_submissions_per_run (60)
raws a tick to SLURM for a re-search nobody asked for. A failure must take the
same path it always has -- the preload returns None and the per-file queries
decide.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

import stan.db_pg as db_pg
from stan.community.scripts import dispatch_hive


# ── db_pg: the two queries ───────────────────────────────────────────────

class _Cur:
    def __init__(self, log, rows):
        self.log, self.rows = log, rows

    def execute(self, sql, params=None):
        self.log.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows=()):
        self.log: list = []
        self.rows = list(rows)

    def cursor(self):
        return _Cur(self.log, self.rows)

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_unknown_raw_paths_sends_candidates_and_returns_only_unknowns(monkeypatch):
    conn = _Conn(rows=[("/nfs/new.d",)])
    monkeypatch.setattr(db_pg, "_connect", lambda: conn)
    got = db_pg.unknown_raw_paths_pg("runs", ["/nfs/old.d", "/nfs/new.d"])
    assert got == {"/nfs/new.d"}
    [(sql, params)] = conn.log
    assert "unnest(%s::text[])" in sql and "NOT EXISTS" in sql and "FROM runs" in sql
    assert params == (["/nfs/old.d", "/nfs/new.d"],), "one array param, not N queries"


def test_unknown_raw_paths_ignores_rows_that_were_not_asked_about(monkeypatch):
    conn = _Conn(rows=[("/nfs/new.d",), ("/somewhere/else.d",)])
    monkeypatch.setattr(db_pg, "_connect", lambda: conn)
    assert db_pg.unknown_raw_paths_pg("sample_health", ["/nfs/new.d"]) == {"/nfs/new.d"}


def test_unknown_raw_paths_empty_candidates_skip_the_round_trip(monkeypatch):
    monkeypatch.setattr(db_pg, "_connect", lambda: pytest.fail("no query expected"))
    assert db_pg.unknown_raw_paths_pg("runs", []) == set()


def test_unknown_raw_paths_rejects_other_tables(monkeypatch):
    monkeypatch.setattr(db_pg, "_connect", lambda: _Conn())
    with pytest.raises(ValueError):
        db_pg.unknown_raw_paths_pg("runs; DROP TABLE runs", ["/x.d"])


def test_unknown_raw_paths_propagates_failure(monkeypatch):
    """Swallowing an error here would make every candidate look new."""
    def boom():
        raise RuntimeError("PG Farm unreachable")
    monkeypatch.setattr(db_pg, "_connect", boom)
    with pytest.raises(RuntimeError):
        db_pg.unknown_raw_paths_pg("runs", ["/x.d"])


def test_capped_raws_can_be_scoped_to_candidates(monkeypatch):
    conn = _Conn(rows=[("/nfs/broken.d",)])
    monkeypatch.setattr(db_pg, "_connect", lambda: conn)
    got = db_pg.capped_raws_pg(3, raw_paths=["/nfs/broken.d", "/nfs/fine.d"])
    assert got == {"/nfs/broken.d"}
    [(sql, params)] = conn.log
    assert "raw_path = ANY(%s)" in sql
    assert params == (3, ["/nfs/broken.d", "/nfs/fine.d"])


def test_capped_raws_unscoped_is_unchanged(monkeypatch):
    conn = _Conn(rows=[("/nfs/broken.d",)])
    monkeypatch.setattr(db_pg, "_connect", lambda: conn)
    assert db_pg.capped_raws_pg(3) == {"/nfs/broken.d"}
    [(sql, params)] = conn.log
    assert "ANY" not in sql and params == (3,)


def test_capped_raws_scoped_to_nothing_skips_the_round_trip(monkeypatch):
    monkeypatch.setattr(db_pg, "_connect", lambda: pytest.fail("no query expected"))
    assert db_pg.capped_raws_pg(3, raw_paths=[]) == set()


# ── the preload ──────────────────────────────────────────────────────────

@pytest.fixture
def pg_mode(monkeypatch):
    monkeypatch.setattr(db_pg, "use_pg", lambda: True)

    def no_full_table_reads():
        pytest.fail("the candidate preload must not open its own full-table reads")
    monkeypatch.setattr(db_pg, "_connect", no_full_table_reads)


def test_preload_with_candidates_derives_processed_from_unknowns(pg_mode, monkeypatch, tmp_path):
    asked = {}

    def unknown(table, paths):
        asked[table] = list(paths)
        return {"/nfs/qc_new.d"} if table == "runs" else {"/nfs/mon_new.d"}

    monkeypatch.setattr(db_pg, "unknown_raw_paths_pg", unknown)
    monkeypatch.setattr(db_pg, "capped_raws_pg",
                        lambda n, raw_paths=None: {p for p in raw_paths or [] if "broken" in p})

    sets = dispatch_hive._preload_dedup_sets(
        tmp_path / "unused.db", 3,
        qc_candidates=["/nfs/qc_done.d", "/nfs/qc_new.d"],
        monitor_candidates=["/nfs/mon_done.d", "/nfs/mon_new.d", "/nfs/mon_broken.d"],
    )
    assert sets is not None
    # Only the QC raws are asked about `runs`, only monitor raws about
    # `sample_health`: a monitor raw is never in `runs`, so asking would
    # send every one of them back down the wire as "unknown".
    assert asked["runs"] == ["/nfs/qc_done.d", "/nfs/qc_new.d"]
    assert asked["sample_health"] == ["/nfs/mon_done.d", "/nfs/mon_new.d", "/nfs/mon_broken.d"]
    assert sets["processed"] == {"/nfs/qc_done.d"}
    assert sets["health"] == {"/nfs/mon_done.d", "/nfs/mon_broken.d"}
    assert "/nfs/qc_new.d" not in sets["processed"]


def test_preload_asks_about_caps_only_for_unknown_raws(pg_mode, monkeypatch, tmp_path):
    """A processed raw is skipped before the cap is ever consulted, so asking
    about it would only put its path on the wire for nothing."""
    monkeypatch.setattr(db_pg, "unknown_raw_paths_pg",
                        lambda t, p: {"/nfs/mon_broken.d"} if t == "sample_health" else set())
    seen = {}

    def capped(n, raw_paths=None):
        seen["paths"] = raw_paths
        return {"/nfs/mon_broken.d"}

    monkeypatch.setattr(db_pg, "capped_raws_pg", capped)
    sets = dispatch_hive._preload_dedup_sets(
        tmp_path / "unused.db", 3,
        qc_candidates=["/nfs/qc_done.d"],
        monitor_candidates=["/nfs/mon_done.d", "/nfs/mon_broken.d"],
    )
    assert sorted(seen["paths"]) == ["/nfs/mon_broken.d"]
    assert sets["capped"] == {"/nfs/mon_broken.d"}


@pytest.mark.parametrize("failing_table", ["runs", "sample_health"])
def test_preload_pg_failure_returns_none(pg_mode, monkeypatch, tmp_path, failing_table):
    def unknown(table, paths):
        if table == failing_table:
            raise RuntimeError("SSL SYSCALL error: EOF detected")
        return set()

    monkeypatch.setattr(db_pg, "unknown_raw_paths_pg", unknown)
    monkeypatch.setattr(db_pg, "capped_raws_pg", lambda n, raw_paths=None: set())
    assert dispatch_hive._preload_dedup_sets(
        tmp_path / "unused.db", 3,
        qc_candidates=["/nfs/a.d"], monitor_candidates=["/nfs/b.d"]) is None


# ── end to end: walk, preload, decide ────────────────────────────────────

QC_NAME = "09212026_HE50_60-spd-dia_S1-A2_1_24532.d"
MON_NAME = "blankDia_S1-H6_1_24534.d"


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A watch dir that is a symlink farm into an 'archive', as on Hive."""
    archive = tmp_path / "nfs" / "raw_data" / "tTOF_HT" / "sep26"
    watch = tmp_path / "incoming" / "TIMS-10878"
    archive.mkdir(parents=True)
    watch.mkdir(parents=True)
    for name in (QC_NAME, MON_NAME):
        (archive / name).mkdir()
        os.symlink(archive / name, watch / name)
    cfg = {
        "db_path": str(tmp_path / "stan.db"),
        "out_root": str(tmp_path / "processing"),
        "sbatch_log_dir": str(tmp_path / "logs" / "sbatch"),
        "dispatch_log_dir": str(tmp_path / "logs" / "dispatch"),
        "stan_venv": str(tmp_path / "venv"),
        "max_submissions_per_run": 60,
        "instruments": [{"name": "timsTOF HT", "family": "timsTOF",
                         "vendor": "bruker", "watch_dir": str(watch)}],
    }
    cfg_path = tmp_path / "dispatch.yml"
    cfg_path.write_text(yaml.safe_dump(cfg))

    monkeypatch.setattr(db_pg, "use_pg", lambda: True)
    monkeypatch.setattr(dispatch_hive, "_job_already_queued", lambda stem: False)
    monkeypatch.setattr(db_pg, "capped_raws_pg", lambda n, raw_paths=None: set())
    return SimpleSite(cfg_path, archive.resolve())


class SimpleSite:
    def __init__(self, cfg_path: Path, archive: Path):
        self.cfg_path = cfg_path
        self.qc = str(archive / QC_NAME)
        self.mon = str(archive / MON_NAME)


def _submitted(summary) -> list[str]:
    return [s["raw"] for inst in summary["by_instrument"].values()
            for s in inst["submissions"]]


def test_pg_failure_falls_back_to_per_file_checks_not_to_everything_new(site, monkeypatch):
    """THE safety property. PG down for the preload must not resubmit."""
    def boom(table, paths):
        raise RuntimeError("remaining connection slots are reserved")

    monkeypatch.setattr(db_pg, "unknown_raw_paths_pg", boom)
    per_file = []

    def processed(db_path, raw):
        per_file.append(str(raw))
        return True

    monkeypatch.setattr(dispatch_hive, "_already_processed", processed)
    monkeypatch.setattr(dispatch_hive, "_already_health_processed", processed)

    summary = dispatch_hive.dispatch_all(site.cfg_path, dry_run=True)
    assert _submitted(summary) == []
    assert summary["totals"]["skipped_processed"] == 2
    assert sorted(per_file) == sorted([site.qc, site.mon]), (
        "a failed preload must hand every raw to the per-file checks")


def test_everything_known_submits_nothing_without_per_file_queries(site, monkeypatch):
    monkeypatch.setattr(db_pg, "unknown_raw_paths_pg", lambda t, p: set())
    monkeypatch.setattr(dispatch_hive, "_already_processed",
                        lambda *a: pytest.fail("per-file query on the happy path"))
    monkeypatch.setattr(dispatch_hive, "_already_health_processed",
                        lambda *a: pytest.fail("per-file query on the happy path"))
    summary = dispatch_hive.dispatch_all(site.cfg_path, dry_run=True)
    assert _submitted(summary) == []
    assert summary["totals"]["skipped_processed"] == 2


def test_only_the_unknown_raw_is_submitted(site, monkeypatch):
    monkeypatch.setattr(db_pg, "unknown_raw_paths_pg",
                        lambda t, p: {site.mon} & set(p))
    summary = dispatch_hive.dispatch_all(site.cfg_path, dry_run=True)
    assert _submitted(summary) == [site.mon]
    assert summary["totals"]["skipped_processed"] == 1


def test_candidates_are_the_resolved_paths_pg_stores(site, monkeypatch):
    """The watch dir is a symlink farm; PG stores the real /nfs target. A
    candidate sent in symlink form would match nothing, and every raw would
    come back unknown -- exactly the 'everything is new' failure."""
    asked: dict[str, list[str]] = {}

    def unknown(table, paths):
        asked[table] = list(paths)
        return set()

    monkeypatch.setattr(db_pg, "unknown_raw_paths_pg", unknown)
    dispatch_hive.dispatch_all(site.cfg_path, dry_run=True)
    assert asked["runs"] == [site.qc]
    assert asked["sample_health"] == [site.mon]
    assert "incoming" not in site.qc


def test_capped_unknown_raw_is_skipped(site, monkeypatch):
    monkeypatch.setattr(db_pg, "unknown_raw_paths_pg",
                        lambda t, p: {site.mon} & set(p))
    monkeypatch.setattr(db_pg, "capped_raws_pg",
                        lambda n, raw_paths=None: {site.mon} & set(raw_paths or []))
    summary = dispatch_hive.dispatch_all(site.cfg_path, dry_run=True)
    assert _submitted(summary) == []
    assert summary["totals"]["skipped_max_attempts"] == 1


def test_sqlite_mode_still_dedups(site, monkeypatch, tmp_path):
    """Single-lab installs have no PG; the preload reads their local SQLite."""
    import sqlite3

    monkeypatch.setattr(db_pg, "use_pg", lambda: False)
    monkeypatch.setattr(db_pg, "unknown_raw_paths_pg",
                        lambda *a: pytest.fail("no PG in SQLite mode"))
    cfg = yaml.safe_load(site.cfg_path.read_text())
    with sqlite3.connect(cfg["db_path"]) as con:
        con.executescript(
            "CREATE TABLE runs (raw_path TEXT);"
            "CREATE TABLE sample_health (raw_path TEXT);"
            "CREATE TABLE dispatch_attempts (raw_path TEXT, status TEXT, attempt_count INT);"
        )
        con.execute("INSERT INTO runs VALUES (?)", (site.qc,))
    summary = dispatch_hive.dispatch_all(site.cfg_path, dry_run=True)
    assert _submitted(summary) == [site.mon]


def test_unknown_raw_paths_propagates_a_statement_error(monkeypatch):
    """The failure that matters is not the connect but the query itself.

    Wrapping the execute in the try/except-return-set() that capped_raws_pg
    uses would pass every other test here -- and would make every candidate
    look *processed*, so new QC would silently never be searched.
    """
    class _BadCur(_Cur):
        def execute(self, sql, params=None):
            raise RuntimeError('column "raw_path" does not exist')

    class _BadConn(_Conn):
        def cursor(self):
            return _BadCur(self.log, self.rows)

    monkeypatch.setattr(db_pg, "_connect", lambda: _BadConn())
    with pytest.raises(RuntimeError):
        db_pg.unknown_raw_paths_pg("runs", ["/x.d"])


def test_one_unreadable_watch_dir_does_not_stop_the_others(site, monkeypatch, tmp_path):
    """Walking every instrument first made one bad share abort the whole tick."""
    import yaml as _yaml

    cfg = _yaml.safe_load(site.cfg_path.read_text())
    stale = tmp_path / "incoming" / "stale-mount"
    stale.mkdir()
    cfg["instruments"].insert(0, {"name": "Exploris 480", "family": "Exploris",
                                  "vendor": "thermo", "watch_dir": str(stale)})
    site.cfg_path.write_text(_yaml.safe_dump(cfg))

    real_walk = dispatch_hive._walk_raws

    def walk(watch_dir):
        if watch_dir == stale:
            raise OSError(116, "Stale file handle")
        return real_walk(watch_dir)

    monkeypatch.setattr(dispatch_hive, "_walk_raws", walk)
    monkeypatch.setattr(db_pg, "unknown_raw_paths_pg", lambda t, p: {site.mon} & set(p))
    summary = dispatch_hive.dispatch_all(site.cfg_path, dry_run=True)
    assert _submitted(summary) == [site.mon]
    assert "Stale file handle" in summary["by_instrument"]["Exploris 480"]["error"]
