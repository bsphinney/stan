"""The hourly ion-cloud backfill must ask PG only for runs it will work on.

WHY THIS EXISTS. PG Farm runs on Google Cloud and bills every byte read out of
it. `cron_ioncloud.sh` submits 4 shards every hour, and until 2026-09-22 each
shard downloaded EVERY `.d` run (1,723 rows, 319 KB) plus EVERY stored cloud
key (597 rows, 23 KB), then threw away three quarters of the runs in Python to
find its shard and most of the rest because they already had a cloud. That was
~1.36 MB an hour, ~33 MB a day, to learn that nothing had changed.

Now PG does both filters: NOT EXISTS against `feature_clouds` (an index-only
anti-join on its primary key) and a hash of the run id for the shard. Measured
on live PG the same day: 1,126 cloudless runs split 296/296/260/274, ~51 KB per
shard, and the union of the four shards equals the unsharded set exactly.

These tests drive `main()` against a fake PG that APPLIES the predicates the
script sends, so they pin behaviour -- every missing cloud is built exactly
once across the shards, stored clouds are not re-read, `--force` still
rebuilds -- rather than the SQL's spelling.
"""
from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from psycopg2.extensions import TRANSACTION_STATUS_IDLE, TRANSACTION_STATUS_INTRANS

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "feature_cloud_backfill.py"


@pytest.fixture()
def fcb(monkeypatch, tmp_path):
    """Import the Hive driver fresh, with its log dir pointed at tmp_path."""
    spec = importlib.util.spec_from_file_location("feature_cloud_backfill", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "LOG_DIR", tmp_path / "logs")
    return mod


# ── A PG that answers the way the real one would ─────────────────────────

def _pg_shard(run_id: str, nshards: int) -> int:
    """Python twin of the SQL shard predicate, byte for byte.

    `get_byte(decode(md5(id::text), 'hex'), 0)` is the first byte of the MD5
    digest; hashlib gives the same digest for the same UTF-8 text.
    """
    return hashlib.md5(run_id.encode("utf-8")).digest()[0] % nshards


class FakePG:
    """Enough of psycopg2 to run main(), applying the WHERE it is sent."""

    def __init__(self, runs: list[tuple[str, str, str]], stored: set[str]):
        self.runs = runs            # (id, run_name, raw_path), newest first
        self.stored = set(stored)   # run_ids with a 'runs' cloud
        self.statements: list[tuple[str, tuple]] = []
        self.events: list[str] = []
        self.status = TRANSACTION_STATUS_IDLE
        self.upserted: list[str] = []

    # connection
    def cursor(self):
        return self

    def commit(self):
        self.events.append("commit")
        self.status = TRANSACTION_STATUS_IDLE

    def rollback(self):
        self.events.append("rollback")
        self.status = TRANSACTION_STATUS_IDLE

    def close(self):
        self.events.append("close")

    # cursor
    def execute(self, sql, params=None):
        s = " ".join(str(sql).split())
        params = tuple(params or ())
        self.statements.append((s, params))
        self.status = TRANSACTION_STATUS_INTRANS
        self._rows: list = []
        if s.startswith("CREATE TABLE"):
            return
        if s.startswith("INSERT INTO feature_clouds"):
            self.upserted.append(params[0])
            self.stored.add(params[0])
            return
        if "count(*)" in s and "FROM feature_clouds" in s:
            self._rows = [(len(self.stored),)]
            return
        if s.startswith("SELECT 1 FROM feature_clouds"):
            self._rows = [(1,)]
            return
        if s.startswith("SELECT run_id FROM feature_clouds"):
            self._rows = [(r,) for r in sorted(self.stored)]
            return
        if s.startswith("SELECT id, run_name, raw_path FROM runs"):
            self._rows = self._select_runs(s, params)
            return
        raise AssertionError(f"unexpected statement: {s}")

    def _select_runs(self, s: str, params: tuple) -> list:
        # Interpolate exactly as psycopg2 would, so a stray unescaped `%`
        # fails here instead of on Hive.
        n_ph = len(re.findall(r"%s", s))
        assert n_ph == len(params), f"{n_ph} placeholders, {len(params)} params: {s}"
        s % tuple("?" for _ in params)  # raises on a bare % the driver would reject
        p = list(params)
        rows = list(self.runs)
        if "run_date >= %s" in s:
            p.pop(0)  # the fixtures carry no dates; --since is tested on the SQL
        if "NOT EXISTS" in s:
            rows = [r for r in rows if r[0] not in self.stored]
        if "md5(" in s:
            nshards, shard = p.pop(0), p.pop(0)
            rows = [r for r in rows if _pg_shard(r[0], nshards) == shard]
        if "LIMIT %s" in s:
            rows = rows[: p.pop(0)]
        return rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _make_runs(tmp_path: Path, n: int, with_sidecar=lambda i: True):
    """n `.d` runs on disk; sidecars where with_sidecar(i) says so."""
    runs = []
    for i in range(n):
        d = tmp_path / "raw" / f"run{i:03d}.d"
        d.mkdir(parents=True)
        if with_sidecar(i):
            (d / f"run{i:03d}.d.features").write_bytes(b"")
        runs.append((f"id-{i:03d}", f"run{i:03d}", str(d)))
    return runs


def _wire(monkeypatch, fcb, pg: FakePG, extracted: list[str]):
    monkeypatch.setattr(fcb, "_connect", lambda: pg)

    def fake_extract(path, max_points=5000):
        pg.events.append(f"extract:{Path(path).name}")
        extracted.append(Path(path).name)
        return SimpleNamespace(mz=[1.0], mobility=[1.0], rt=[1.0], charge=[2],
                               intensity=[1.0], n_points=1, n_total=1)

    monkeypatch.setattr(fcb, "extract_feature_cloud", fake_extract)


def _runs_selects(pg: FakePG) -> list[str]:
    return [s for s, _ in pg.statements if s.startswith("SELECT id, run_name, raw_path")]


# ── The query itself ─────────────────────────────────────────────────────

def test_query_asks_pg_for_missing_clouds_in_this_shard_only(fcb):
    sql, params = fcb.build_runs_query(since="", limit=0, skip_stored=True,
                                       shard=2, nshards=4)
    s = " ".join(sql.split())
    assert ("NOT EXISTS (SELECT 1 FROM feature_clouds f WHERE "
            "f.run_id = runs.id::text AND f.source = 'runs')") in s
    assert "get_byte(decode(md5(id::text), 'hex'), 0) %% %s = %s" in s
    assert params == (4, 2)
    assert s.endswith("ORDER BY run_date DESC")


def test_force_drops_the_stored_filter_but_keeps_the_shard(fcb):
    sql, params = fcb.build_runs_query(since="", limit=0, skip_stored=False,
                                       shard=1, nshards=4)
    assert "NOT EXISTS" not in sql
    assert "md5(" in sql and params == (4, 1)


def test_single_shard_sends_no_shard_predicate(fcb):
    sql, params = fcb.build_runs_query(since="", limit=0, skip_stored=True,
                                       shard=0, nshards=1)
    assert "md5(" not in sql and params == ()


def test_since_and_limit_keep_their_placeholders_in_order(fcb):
    sql, params = fcb.build_runs_query(since="2026-09-01", limit=25,
                                       skip_stored=True, shard=3, nshards=4)
    assert "run_date >= %s" in sql and sql.rstrip().endswith("LIMIT %s")
    assert params == ("2026-09-01", 4, 3, 25)
    assert sql.count("%s") == len(params)


def test_no_default_since_so_late_sidecars_are_still_found(fcb):
    """A sidecar that appears months after its run must still be picked up."""
    sql, _ = fcb.build_runs_query(since="", limit=0, skip_stored=True,
                                  shard=0, nshards=4)
    assert "run_date" not in sql.split("ORDER BY")[0]


# ── main(), end to end against the fake ──────────────────────────────────

def test_four_shards_build_every_missing_cloud_exactly_once(monkeypatch, fcb, tmp_path):
    runs = _make_runs(tmp_path, 40)
    stored = {r[0] for r in runs[::3]}            # a third already published
    pg = FakePG(runs, stored)
    extracted: list[str] = []
    _wire(monkeypatch, fcb, pg, extracted)

    for shard in range(4):
        rc = fcb.main(["--shard", str(shard), "--nshards", "4", "--cache-dir", ""])
        assert rc == 0

    want = sorted(r[0] for r in runs if r[0] not in stored)
    assert sorted(pg.upserted) == want, "a missing cloud was skipped or built twice"
    assert not set(pg.upserted) & stored, "a stored cloud was rebuilt without --force"


def test_shards_stay_disjoint_when_they_start_at_different_times(monkeypatch, fcb, tmp_path):
    """Array tasks on `low` start whenever a slot frees up, not together.

    With row-position sharding over a NOT EXISTS result, shard 0 finishing
    first would shrink the list shard 1 numbers, shifting runs between shards
    so some are built twice and others by nobody that tick. Hashing the id
    makes a run's shard a property of the run, not of what else is pending.
    """
    runs = _make_runs(tmp_path, 40)
    pg = FakePG(runs, set())
    extracted: list[str] = []
    _wire(monkeypatch, fcb, pg, extracted)

    for shard in (0, 2, 1, 3):  # sequential: each sees the previous one's writes
        fcb.main(["--shard", str(shard), "--nshards", "4", "--cache-dir", ""])

    assert sorted(pg.upserted) == sorted(r[0] for r in runs)


def test_stored_cloud_keys_are_not_downloaded(monkeypatch, fcb, tmp_path):
    """The old per-shard `SELECT run_id FROM feature_clouds` is gone."""
    runs = _make_runs(tmp_path, 12)
    pg = FakePG(runs, {runs[0][0]})
    _wire(monkeypatch, fcb, pg, [])

    fcb.main(["--shard", "0", "--nshards", "4", "--cache-dir", ""])

    sent = [s for s, _ in pg.statements]
    assert not any(s.startswith("SELECT run_id FROM feature_clouds") for s in sent)
    (runs_sql,) = _runs_selects(pg)
    assert "NOT EXISTS" in runs_sql and "md5(" in runs_sql


def test_force_rebuilds_clouds_that_already_exist(monkeypatch, fcb, tmp_path):
    runs = _make_runs(tmp_path, 8)
    stored = {r[0] for r in runs}
    pg = FakePG(runs, stored)
    _wire(monkeypatch, fcb, pg, [])

    fcb.main(["--force", "--cache-dir", ""])

    (runs_sql,) = _runs_selects(pg)
    assert "NOT EXISTS" not in runs_sql
    assert sorted(pg.upserted) == sorted(stored)


def test_runs_without_a_sidecar_are_skipped_not_fatal(monkeypatch, fcb, tmp_path):
    runs = _make_runs(tmp_path, 6, with_sidecar=lambda i: i % 2 == 0)
    pg = FakePG(runs, set())
    _wire(monkeypatch, fcb, pg, [])

    assert fcb.main(["--cache-dir", ""]) == 0
    assert sorted(pg.upserted) == [r[0] for r in runs if int(r[0][-3:]) % 2 == 0]


def test_read_transaction_is_closed_before_the_first_extraction(monkeypatch, fcb, tmp_path):
    """The commit the running Hive copy lacked (found 2026-09-22).

    Without it the read transaction stays open through run #1's sidecar
    extraction: read locks on runs/feature_clouds and a pinned VACUUM horizon.
    """
    runs = _make_runs(tmp_path, 3)
    pg = FakePG(runs, set())
    _wire(monkeypatch, fcb, pg, [])

    fcb.main(["--cache-dir", ""])

    first_extract = next(i for i, e in enumerate(pg.events) if e.startswith("extract:"))
    assert pg.events[first_extract - 1] == "commit", pg.events


def test_json_cache_fallback_does_not_reference_the_missing_table(monkeypatch, fcb, tmp_path):
    """With PG out of the picture the NOT EXISTS would name a table that may
    not exist, so the cache directory decides what is already stored."""
    runs = _make_runs(tmp_path, 6)
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / f"{runs[0][0]}.json").write_text("{}")
    pg = FakePG(runs, set())
    extracted: list[str] = []
    _wire(monkeypatch, fcb, pg, extracted)

    fcb.main(["--no-pg", "--cache-dir", str(cache)])

    (runs_sql,) = _runs_selects(pg)
    assert "NOT EXISTS" not in runs_sql
    assert f"{runs[0][1]}.d.features" not in extracted
    assert len(extracted) == 5
    assert pg.upserted == []


def test_out_of_range_shard_is_refused(fcb):
    """--shard 4 of 4 would match nothing, silently, forever."""
    with pytest.raises(SystemExit):
        fcb.main(["--shard", "4", "--nshards", "4"])
