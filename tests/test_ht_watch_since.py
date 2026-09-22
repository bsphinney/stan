"""ht-watch reads a bounded window from PG, and the bound never truncates a plate.

Every 20 minutes `stan ht-watch` pulled up to 20,000 rows of `sample_health`
and of `runs` -- every column, the whole history, ~4.7 MB -- and then threw
away everything older than 14 days in Python. PG Farm bills every byte a
client reads, so that was ~340 MB/day. Measured 2026-09-22.

The readers now take a `since` applied in SQL. The bound has to be generous,
because the analysis is not a date filter: `analyse_submission` pulls in the
submission's whole tray and up to 300 injections either side of it
(ht_outliers.NEIGHBOURHOOD), so a bound that clips the start of a long plate
turns a complete plate into an "incomplete" one, and an incomplete plate that
has gone quiet is exactly what the stalled-plate email fires on.

So: a 50-day floor, widened once when the earliest run of any watched
submission, less a 35-day margin, reaches behind it, and a full read -- the
pre-bound behaviour -- when even that does not settle. The margin is the time
300 injections actually take, idle weeks included: up to 32 days on the HT.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

import stan.db_pg as db_pg
from stan.reports import ht_watch

NOW = datetime(2026, 9, 22, 18, 0, tzinfo=timezone.utc)


# ── the readers ──────────────────────────────────────────────────────────

class _Cur:
    def __init__(self, conn):
        self.conn = conn
        self.description = [("id",), ("run_date",)]
        self._rows: list = []

    def execute(self, sql, params=None):
        sql = " ".join(sql.split())
        self.conn.log.append((sql, params))
        if "information_schema.columns" in sql:
            self._rows = [("id",), ("run_date",), ("tic_rt_bins",)]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self):
        self.log: list = []

    def cursor(self):
        return _Cur(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def conn(monkeypatch):
    c = _Conn()
    monkeypatch.setattr(db_pg, "_connect", lambda: c)
    monkeypatch.setattr(db_pg, "_RUNS_COLS_CACHE", None)
    return c


def _select(conn, table):
    return [(s, p) for s, p in conn.log if f"FROM {table}" in s][-1]


def test_sample_health_default_is_unbounded(conn):
    """Existing callers (the dashboard's HT tab among them) are unchanged."""
    db_pg.get_sample_health_pg(limit=20000)
    sql, params = _select(conn, "sample_health")
    assert "run_date >=" not in sql
    assert params == (20000,)


def test_sample_health_since_compares_text_with_a_day_of_slack(conn):
    """sample_health.run_date is TEXT holding LOCAL time with an offset
    ('2026-08-23T20:13:45.944-07:00'), so the comparison is on the date prefix
    and one day early: over-including a day is harmless, clipping one is not."""
    db_pg.get_sample_health_pg(limit=20000, since=datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc))
    sql, params = _select(conn, "sample_health")
    assert "run_date >= %s" in sql
    assert "run_date IS NULL" in sql, "an undated row cannot be placed before the bound"
    assert params == ("2026-08-22", 20000)
    assert all(not isinstance(p, datetime) for p in params), (
        "a datetime against a TEXT column would compare the wrong things")


def test_sample_health_since_accepts_a_date(conn):
    db_pg.get_sample_health_pg(since=date(2026, 8, 23))
    assert _select(conn, "sample_health")[1][0] == "2026-08-22"


def test_runs_default_is_unbounded(conn):
    db_pg.get_runs_pg(limit=20000)
    sql, params = _select(conn, "runs")
    assert "run_date >=" not in sql
    assert params == (20000, 0)


def test_runs_since_compares_as_timestamptz(conn):
    """runs.run_date is `timestamp with time zone`: compare it as one."""
    since = datetime(2026, 8, 23, 1, 0, tzinfo=timezone.utc)
    db_pg.get_runs_pg(limit=20000, since=since)
    sql, params = _select(conn, "runs")
    assert "run_date >= %s" in sql and "run_date IS NULL" in sql
    assert "(hidden IS NULL OR hidden = 0)" in sql
    assert params == (since, 20000, 0)
    assert "tic_rt_bins" not in sql


# ── the watcher's bound ──────────────────────────────────────────────────

def _health(name, when, **kw):
    row = {"run_name": name, "run_date": when.astimezone(
        timezone(timedelta(hours=-7))).isoformat(), "verdict": "pass",
        "reasons": "[]", "ms1_total_tic": 1e10, "ms1_max_intensity": 1e8,
        "instrument": "timsTOF HT"}
    row.update(kw)
    return row


def _plate(sub_token, start, n=12, first_inj=24000, tray="S5"):
    """n wells of one submission, one injection every ~15 min."""
    rows = []
    wells = [f"{r}{c}" for c in range(1, 13) for r in "ABCDEFGH"]
    for i in range(n):
        rows.append(_health(
            f"{start:%Y%m%d}_{sub_token}_100spd_COH-{i}_{tray}-{wells[i]}_1_{first_inj + i}.d",
            start + timedelta(minutes=15 * i)))
    return rows


class FakeStore:
    """PG as the watcher sees it: rows filtered by `since` exactly as the SQL
    does, and a record of every window asked for."""

    def __init__(self, health, qc=()):
        self.health, self.qc = list(health), list(qc)
        self.health_since: list = []
        self.runs_since: list = []

    @staticmethod
    def _keep(r, since):
        if since is None or not r.get("run_date"):
            return True
        floor = (since.astimezone(timezone.utc).date() - timedelta(days=1)).isoformat()
        return str(r["run_date"])[:10] >= floor

    def get_sample_health_pg(self, instrument=None, verdict=None, limit=200, since=None):
        self.health_since.append(since)
        return [dict(r) for r in self.health if self._keep(r, since)][:limit]

    def get_runs_pg(self, instrument=None, limit=50, offset=0, qc_only=False,
                    include_hidden=False, since=None):
        self.runs_since.append(since)
        return [dict(r) for r in self.qc if self._keep(r, since)][:limit]


@pytest.fixture
def store(monkeypatch, tmp_path):
    def make(health, qc=()):
        s = FakeStore(health, qc)
        monkeypatch.setattr(db_pg, "use_pg", lambda: True)
        monkeypatch.setattr(db_pg, "get_sample_health_pg", s.get_sample_health_pg)
        monkeypatch.setattr(db_pg, "get_runs_pg", s.get_runs_pg)
        monkeypatch.setattr(ht_watch, "recipient", lambda: None)
        return s
    return make


def test_routine_tick_reads_thirty_days_not_everything(store, tmp_path):
    s = store(_plate("793", NOW - timedelta(days=2)))
    ht_watch.run_watch(dry_run=True, state_dir=tmp_path, now=NOW)
    assert len(s.health_since) == 1 and len(s.runs_since) == 1
    since = s.health_since[0]
    assert since is not None, "the routine tick must be bounded"
    assert since <= NOW - timedelta(days=ht_watch.HT_LOOKBACK_DAYS)
    assert ht_watch.HT_LOOKBACK_DAYS >= 30
    assert s.runs_since == s.health_since


def test_long_running_submission_widens_the_window(store, tmp_path):
    """A plate that began 26 days ago is still being acquired. The 30-day floor
    would sit 4 days behind its first well -- inside the 300-injection reach
    of the analysis -- so the watcher must read back to first well - 7 d."""
    start = NOW - timedelta(days=26)
    rows = _plate("793", start, n=6) + _plate("793", NOW - timedelta(days=1), n=6,
                                               first_inj=24006)
    s = store(rows)
    ht_watch.run_watch(dry_run=True, state_dir=tmp_path, now=NOW)
    assert len(s.health_since) == 2
    widened = s.health_since[-1]
    assert widened <= start - timedelta(days=ht_watch.HT_MARGIN_DAYS)
    assert s.runs_since == s.health_since


def test_unsettled_window_falls_back_to_a_full_read(store, tmp_path):
    """A short sample code matched by run names every few days, back through
    history, keeps pushing the earliest match back. Rather than chase it one
    window at a time, read everything -- exactly the pre-bound behaviour, so
    the analysis cannot differ from it."""
    rows = _plate("COH", NOW - timedelta(days=1), n=6)
    for k, days in enumerate(range(27, 200, 6)):
        rows += _plate("COH", NOW - timedelta(days=days), n=1,
                       first_inj=10000 + 10 * k)
    s = store(rows)
    ht_watch.run_watch(dry_run=True, state_dir=tmp_path, now=NOW)
    assert s.health_since[-1] is None
    assert s.runs_since[-1] is None
    assert len(s.health_since) <= 3, "bounded number of reads per tick"


def test_isolated_old_namesake_does_not_force_a_full_read(store, tmp_path):
    """A matching name 200 days back, with months of silence in between, is a
    different batch reusing the code, not the start of this submission. It is
    invisible to the bounded read and must stay that way: chasing it would
    put the full 4.7 MB back on the wire every 20 minutes for as long as the
    code stays in the 14-day window."""
    rows = _plate("COH", NOW - timedelta(days=1), n=6)
    rows += _plate("COH", NOW - timedelta(days=200), n=1, first_inj=5000)
    s = store(rows)
    ht_watch.run_watch(dry_run=True, state_dir=tmp_path, now=NOW)
    assert len(s.health_since) == 1 and s.health_since[0] is not None


def test_bounded_and_full_reads_reach_the_same_verdict(store, tmp_path, monkeypatch):
    """The point of the whole exercise: fewer bytes, same alerts."""
    rows = _plate("793", NOW - timedelta(days=3), n=24)
    # four consecutive dead wells -> a consecutive_failures alert
    for r in rows[10:14]:
        r["verdict"] = "fail"
        r["reasons"] = '["low signal"]'
    # plus a lot of unrelated history the bound should drop
    rows += [_health(f"20260301_old_{i}_S2-A1_1_{9000 + i}.d",
                     NOW - timedelta(days=150 + i)) for i in range(50)]

    s = store(rows)
    bounded = ht_watch.run_watch(dry_run=True, state_dir=tmp_path / "a", now=NOW)

    monkeypatch.setattr(ht_watch, "HT_LOOKBACK_DAYS", 100000)
    full = ht_watch.run_watch(dry_run=True, state_dir=tmp_path / "b", now=NOW)

    assert bounded["alerts"], "the fixture must actually alert"
    assert bounded["alerts"] == full["alerts"]
    assert bounded["submissions_checked"] == full["submissions_checked"]
    assert s.health_since[0] is not None


def test_reasons_are_decoded_on_the_pg_path(store):
    """stan.db.get_sample_health decodes the JSON `reasons` column; calling
    the PG reader directly must do the same, or find_outliers reports the
    raw string '["low signal"]' as the reason."""
    s = store([_health("x_S1-A1_1_1.d", NOW, reasons='["low signal"]')])
    health, _qc = ht_watch._fetch_ht_rows(NOW - timedelta(days=30))
    assert health[0]["reasons"] == ["low signal"]
    assert s.health_since == [NOW - timedelta(days=30)]


def test_sqlite_mode_applies_the_same_bound(monkeypatch):
    """A single-lab install reads its local SQLite -- no egress to save -- but
    must see the same window, or the two backends could alert differently."""
    monkeypatch.setattr(db_pg, "use_pg", lambda: False)
    old = {"run_name": "a", "run_date": "2026-05-01T10:00:00"}
    new = {"run_name": "b", "run_date": "2026-09-20T10:00:00"}
    undated = {"run_name": "c", "run_date": None}
    monkeypatch.setattr("stan.db.get_sample_health", lambda **k: [old, new, undated])
    monkeypatch.setattr("stan.db.get_runs", lambda **k: [old, new])
    health, qc = ht_watch._fetch_ht_rows(NOW - timedelta(days=30))
    assert [r["run_name"] for r in health] == ["b", "c"]
    assert [r["run_name"] for r in qc] == ["b"]
    health, qc = ht_watch._fetch_ht_rows(None)
    assert len(health) == 3 and len(qc) == 2


def test_quiet_stretch_neighbourhood_is_read(store):
    """300 injections can span a month when the instrument sits idle.

    On the HT, June/July ran at ~15 injections a day, so a plate that started
    12 days ago can have its 300-injection neighbourhood 40 days back. A
    window sized on the SPD (a 7-day margin under a 30-day floor) read none of
    it, and the plate's membership and rerun cohort stopped matching the
    dashboard's unbounded read.
    """
    rows = _plate("793", NOW - timedelta(days=12), n=6, first_inj=24000)
    quiet = NOW - timedelta(days=40)
    neighbours = [_health(f"{quiet:%Y%m%d}_other_100spd_S4-A{i + 1}_1_{23800 + i}.d",
                          quiet + timedelta(minutes=15 * i)) for i in range(5)]
    store(rows + neighbours)
    health, _qc, _subs, _since = ht_watch.load_ht_rows(NOW)
    names = {r["run_name"] for r in health}
    missing = [n["run_name"] for n in neighbours if n["run_name"] not in names]
    assert not missing, f"neighbourhood rows clipped by the window: {missing}"
