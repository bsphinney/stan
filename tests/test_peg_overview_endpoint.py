"""GET /api/peg/overview and the PEG Watch DB readers behind it.

What this pins, in order of how badly it would hurt to lose it:

* The endpoint is PUBLIC on the hosted dashboard, so no run name, sample name
  or maintenance note may appear anywhere in its response.
* Only real PEG on QC runs counts: an ``'unknown'`` row (the failure sentinel,
  score 0.0), a NULL row, a hidden row, a blank and the bogus 1980 row are
  all excluded -- from the runs AND from the ladder built out of their hits.
* SQLite's mixed-offset TEXT dates land on the right UTC minute.
* Nothing local reproduces a PG type error, so the PG SQL is checked for the
  shapes CLAUDE.md warns about (timestamptz formatting, integer hidden,
  explicit column lists), and the PG branch is exercised end to end with
  fake readers.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import stan.dashboard.server as server
import stan.db as stan_db
import stan.db_pg as db_pg
from stan.community.submit import RELAY_URL
from stan.dashboard.readonly import install_readonly_gate
from stan.metrics.peg_trends import PG_BLANK_WASH_REGEX

AS_OF = date(2026, 9, 28)
TIMS, EXPL = "timsTOF HT", "Orbitrap Exploris 480"
SECRET_NOTE = "swapped for customer Smith-Lab submission 4471"

SPEC_KEYS = {
    "as_of", "instrument", "instruments", "lc_system", "instrument_family", "runs_cols",
    "runs", "rolling_start", "rolling", "episodes", "baseline", "summary", "ladder",
    "column_periods", "impact", "lab_lc", "sharing",
}
CLASSES = [("clean", 0.1, 5.0), ("trace", 1.5, 30.0), ("moderate", 3.0, 55.0),
           ("heavy", 8.0, 85.0)]


# ── Fixture data ─────────────────────────────────────────────────────

def _insert_run(con, rid, name, run_date, *, instrument=TIMS, spd=100, cls="clean",
                pct=0.1, score=5.0, hidden=0, lc="evosep", prec=30000, mode="diaPASEF"):
    con.execute(
        "INSERT INTO runs (id, instrument, run_name, run_date, raw_path, mode, spd, "
        "peg_score, peg_intensity_pct, peg_n_ions_detected, peg_class, n_precursors, "
        "lc_system, hidden) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, instrument, name, run_date, f"/data/{name}", mode, spd, score, pct,
         3 if score is not None else None, cls, prec, lc, hidden),
    )


def _hit(con, rid, n, adduct):
    con.execute(
        "INSERT INTO peg_ion_hits (run_id, source, mz, observed_intensity, adduct, "
        "repeat_n, charge, ppm_error) VALUES (?, 'runs', ?, 20000, ?, ?, 1, 0.5)",
        (rid, 100.0 + n, adduct, n),
    )


REAL_NAMES: list[str] = []
EXCLUDED_NAMES = {
    "unknown": "HeLa_sentinel_unknown_1_2001.d",
    "null": "HeLa_null_peg_1_2002.d",
    "hidden": "HeLa_hidden_heavy_1_2003.d",
    "blank": "Blank_after_HeLa_1_2004.d",
    "bogus": "HeLa_1980_bogus_1_2005.d",
}


def _seed(path: Path) -> None:
    """30 real timsTOF QC runs, 3 Exploris ones, and one of every exclusion."""
    REAL_NAMES.clear()
    with sqlite3.connect(path) as con:
        for i in range(30):
            d = AS_OF - timedelta(days=34 - i)
            cls, pct, score = CLASSES[i % 4]
            name = f"28Sep26_HeLa50ng_{'60' if i % 5 == 0 else '100'}spd_S1-A{i}_1_{1000 + i}.d"
            REAL_NAMES.append(name)
            # Three spellings of a timestamp, as SQLite really holds them.
            stamp = [f"{d}T10:00:00-07:00", f"{d} 17:00:00+00:00", f"{d}T17:00:00Z"][i % 3]
            _insert_run(con, f"t{i}", name, stamp, spd=60 if i % 5 == 0 else 100,
                        cls=cls, pct=pct, score=score)
        _hit(con, "t0", 9, "+NH4")
        _hit(con, "t1", 9, "+NH4")
        _hit(con, "t1", 9, "+H")          # same run, second adduct: still one run
        _hit(con, "t2", 11, "+Na")
        # A timestamp that crosses midnight UTC.
        _insert_run(con, "tz", "HeLa_tz_1_1999.d", "2026-09-21T23:30:00-08:00",
                    cls="heavy", pct=9.9, score=90.0)
        REAL_NAMES.append("HeLa_tz_1_1999.d")

        _insert_run(con, "x_unknown", EXCLUDED_NAMES["unknown"], "2026-09-10T12:00:00Z",
                    cls="unknown", pct=0.0, score=0.0)
        _insert_run(con, "x_null", EXCLUDED_NAMES["null"], "2026-09-11T12:00:00Z",
                    cls=None, pct=None, score=None)
        _insert_run(con, "x_hidden", EXCLUDED_NAMES["hidden"], "2026-09-12T12:00:00Z",
                    cls="heavy", pct=40.0, score=99.0, hidden=1)
        _insert_run(con, "x_blank", EXCLUDED_NAMES["blank"], "2026-09-13T12:00:00Z",
                    cls="heavy", pct=40.0, score=99.0)
        _insert_run(con, "x_bogus", EXCLUDED_NAMES["bogus"], "1980-01-02T00:00:00+00:00",
                    cls="heavy", pct=40.0, score=99.0)
        for rid in ("x_unknown", "x_hidden", "x_blank", "x_bogus"):
            _hit(con, rid, 15, "+Na")

        for i in range(3):
            name = f"HeLa_Exploris_QC_{i}.raw"
            REAL_NAMES.append(name)
            _insert_run(con, f"e{i}", name, f"2026-09-{20 + i}T09:00:00-07:00",
                        instrument=EXPL, lc="custom", spd=38, mode="DIA", pct=0.2)

        for eid, etype, when, model in [
            ("ev1", "column_change", "2026-09-01T12:00:00Z", "PepSep 10cm"),
            ("ev2", "source_clean", "2026-09-05T12:00:00Z", None),
        ]:
            con.execute(
                "INSERT INTO maintenance_events (id, instrument, event_type, event_date, "
                "notes, operator, column_model) VALUES (?,?,?,?,?,?,?)",
                (eid, TIMS, etype, when, SECRET_NOTE, "Jane Operator", model),
            )


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch) -> Path:
    """A seeded SQLite store with PG out of the picture and a pinned date."""
    monkeypatch.delenv("STAN_DB_BACKEND", raising=False)
    monkeypatch.delenv("STAN_DISPLAY_NAME", raising=False)
    monkeypatch.delenv("STAN_PEG_SHARE", raising=False)
    path = tmp_path / "stan.db"
    stan_db.init_db(path)
    _seed(path)
    monkeypatch.setattr(stan_db, "get_db_path", lambda: path)
    monkeypatch.setattr(server, "_peg_today", lambda: AS_OF)
    monkeypatch.setattr(server, "_PEG_CACHE", {})
    # Never read the developer's real ~/.stan/community.yml.
    monkeypatch.setattr("stan.config.load_community", lambda: {"display_name": "Test Lab"})
    return path


@pytest.fixture
def client(db_path) -> TestClient:
    return TestClient(server.app)


# ── The SQLite path ──────────────────────────────────────────────────

def test_overview_shape_and_exclusions(client):
    r = client.get("/api/peg/overview")
    assert r.status_code == 200, r.text
    doc = r.json()
    assert set(doc) == SPEC_KEYS
    assert doc["as_of"] == "2026-09-28"
    assert doc["instrument"] == TIMS            # default: the most real-PEG runs
    assert doc["instrument_family"] == "timsTOF"
    assert doc["lc_system"] == "evosep"
    assert doc["runs_cols"] == ["t", "spd", "pct", "score", "ions", "cls", "prec"]
    # 30 + the midnight-crossing run; none of the five exclusions
    assert len(doc["runs"]) == 31
    assert all(0 <= row[5] <= 3 for row in doc["runs"])
    assert doc["runs"] == sorted(doc["runs"], key=lambda row: row[0])
    # The 0.0 'unknown' would have been the only exact-0 score.
    assert all(row[3] != 0.0 for row in doc["runs"])
    assert doc["instruments"] == [
        {"instrument": TIMS, "n_runs": 31, "evosep": True},
        {"instrument": EXPL, "n_runs": 3, "evosep": False},
    ]
    assert doc["rolling_start"] == str(AS_OF - timedelta(days=34))
    assert set(doc["rolling"]) == {"all", "100"}    # 60 SPD has 6 runs, not 20
    assert len(doc["rolling"]["all"]) == 35
    # Runs sit 34..5 days before as_of, so the last 30 days hold i = 5..29
    # (25 runs) plus the 09-22 run.
    assert doc["summary"]["n_30d"] == 26
    # From 09-01 (i = 7..29, plus 09-22): 7 heavy, 6 clean, 6 trace, 5 moderate.
    # The source_clean event is not a column change.
    assert doc["column_periods"] == [{
        "installed": "2026-09-01", "retired": None, "column_model": "PepSep 10cm",
        "n_qc": 24, "median_pct": 2.25, "heavy_pct": 29, "clean_pct": 25}]


def test_no_run_names_notes_or_operators_anywhere(client):
    """The public GET must not carry a single identifying string."""
    text = client.get("/api/peg/overview").text
    for name in REAL_NAMES + list(EXCLUDED_NAMES.values()):
        assert name not in text
        assert Path(name).stem not in text
    assert "HeLa" not in text
    assert SECRET_NOTE not in text and "Smith" not in text
    assert "Jane" not in text
    assert "/data/" not in text                     # no raw paths either
    text2 = client.get(f"/api/peg/overview?instrument={EXPL}").text
    assert "HeLa" not in text2


def test_mixed_offsets_land_on_the_utc_minute(client):
    runs = client.get("/api/peg/overview").json()["runs"]
    stamps = [row[0] for row in runs]
    first = AS_OF - timedelta(days=34)
    # -07:00, +00:00 and Z spellings of 17:00 UTC all read 17:00.
    for i in range(3):
        assert f"{first + timedelta(days=i)}T17:00" in stamps
    # 23:30 -08:00 on the 21st is 07:30 UTC on the 22nd.
    assert "2026-09-22T07:30" in stamps
    assert not any(s.startswith("1980") for s in stamps)


def test_ladder_counts_only_real_qc_runs(client):
    lad = client.get("/api/peg/overview").json()["ladder"]
    assert lad["months"] == ["2026-08", "2026-09"]
    # +NH4 on t0, t1; +H on t1; +Na on t2. The +Na PEG15 hits on the unknown,
    # hidden, blank and 1980 rows must be nowhere.
    assert lad["adducts"] == {"+H": 1, "+NH4": 2, "+Na": 1}
    assert all(v == 0 for v in lad["share"][lad["n"].index(15)])
    aug = lad["months"].index("2026-08")
    assert lad["share"][lad["n"].index(9)][aug] == round(2 / lad["nruns"][aug], 3)


def test_lab_lc_lists_every_instrument_with_its_family(client):
    lab = client.get("/api/peg/overview").json()["lab_lc"]
    assert [(x["instrument"], x["family"], x["lc_system"]) for x in lab] == [
        (TIMS, "timsTOF", "evosep"), (EXPL, "Exploris", "custom")]
    assert lab[0]["n_90d"] == 31 and len(lab[0]["weekly"]) == 26
    assert lab[1]["n_90d"] == 3 and lab[1]["median_90d"] == 0.2


def test_explicit_instrument(client):
    doc = client.get("/api/peg/overview", params={"instrument": EXPL}).json()
    assert doc["instrument"] == EXPL and doc["instrument_family"] == "Exploris"
    assert doc["lc_system"] == "custom"
    assert len(doc["runs"]) == 3
    assert doc["runs"][0][0] == "2026-09-20T16:00"
    assert doc["column_periods"] == [] and doc["impact"] == {}


def test_unknown_instrument_is_an_empty_document_not_a_store_read(client, monkeypatch):
    """Arbitrary query strings must not become PG reads or cache entries."""
    def _boom(*a, **kw):
        raise AssertionError("per-instrument reader called for an unknown instrument")

    monkeypatch.setattr(stan_db, "get_peg_runs", _boom)
    r = client.get("/api/peg/overview", params={"instrument": "<script>nope</script>"})
    assert r.status_code == 200
    doc = r.json()
    assert set(doc) == SPEC_KEYS
    assert doc["runs"] == [] and doc["episodes"] == [] and doc["baseline"] is None
    assert [i["instrument"] for i in doc["instruments"]] == [TIMS, EXPL]
    assert not any(k[0] == "overview" for k in server._PEG_CACHE)


def test_empty_store_is_a_valid_empty_document(tmp_path, monkeypatch):
    monkeypatch.delenv("STAN_DB_BACKEND", raising=False)
    path = tmp_path / "empty.db"
    stan_db.init_db(path)
    monkeypatch.setattr(stan_db, "get_db_path", lambda: path)
    monkeypatch.setattr(server, "_peg_today", lambda: AS_OF)
    monkeypatch.setattr(server, "_PEG_CACHE", {})
    monkeypatch.setattr("stan.config.load_community", lambda: {})
    r = TestClient(server.app).get("/api/peg/overview")
    assert r.status_code == 200, r.text
    doc = r.json()
    assert set(doc) == SPEC_KEYS
    assert doc["instrument"] is None and doc["instruments"] == []
    assert doc["runs"] == [] and doc["episodes"] == [] and doc["lab_lc"] == []
    assert doc["rolling"] == {"all": []} and doc["summary"]["n_30d"] == 0
    assert doc["sharing"] == {"enabled": False, "display_name": None, "relay_url": RELAY_URL}


def test_missing_store_file_is_empty_not_500(tmp_path, monkeypatch):
    monkeypatch.delenv("STAN_DB_BACKEND", raising=False)
    monkeypatch.setattr(stan_db, "get_db_path", lambda: tmp_path / "absent.db")
    monkeypatch.setattr(server, "_PEG_CACHE", {})
    r = TestClient(server.app).get("/api/peg/overview")
    assert r.status_code == 200 and r.json()["runs"] == []


# ── Sharing status ───────────────────────────────────────────────────

def test_sharing_off_by_default(client):
    s = client.get("/api/peg/overview").json()["sharing"]
    assert s == {"enabled": False, "display_name": "Test Lab", "relay_url": RELAY_URL}


@pytest.mark.parametrize("flag", [True, "true", "yes", 1])
def test_sharing_on_from_community_yml(client, monkeypatch, flag):
    monkeypatch.setattr("stan.config.load_community",
                        lambda: {"display_name": "Clogged PeakTail", "peg_share": flag})
    s = client.get("/api/peg/overview").json()["sharing"]
    assert s["enabled"] is True and s["display_name"] == "Clogged PeakTail"


def test_sharing_env_override_and_hosted_name(client, monkeypatch):
    """The hosted container has no community.yml, only env vars."""
    def _missing():
        raise FileNotFoundError("community.yml")

    monkeypatch.setattr("stan.config.load_community", _missing)
    monkeypatch.setenv("STAN_PEG_SHARE", "1")
    monkeypatch.setenv("STAN_DISPLAY_NAME", "Clogged PeakTail")
    s = client.get("/api/peg/overview").json()["sharing"]
    assert s == {"enabled": True, "display_name": "Clogged PeakTail", "relay_url": RELAY_URL}


def test_sharing_status_is_not_cached(client, monkeypatch):
    assert client.get("/api/peg/overview").json()["sharing"]["enabled"] is False
    monkeypatch.setenv("STAN_PEG_SHARE", "1")
    assert client.get("/api/peg/overview").json()["sharing"]["enabled"] is True


# ── Caching and failure modes ────────────────────────────────────────

def _count_calls(monkeypatch, name: str) -> list:
    calls: list = []
    real = getattr(stan_db, name)

    def wrapped(*a, **kw):
        calls.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(stan_db, name, wrapped)
    return calls


def test_overview_is_cached_per_instrument_and_day(client, monkeypatch):
    calls = _count_calls(monkeypatch, "get_peg_runs")
    a = client.get("/api/peg/overview").json()
    b = client.get("/api/peg/overview", params={"instrument": TIMS}).json()
    assert a == b and len(calls) == 1           # default resolves to the same key
    client.get("/api/peg/overview", params={"instrument": EXPL})
    assert len(calls) == 2
    monkeypatch.setattr(server, "_peg_today", lambda: AS_OF + timedelta(days=1))
    client.get("/api/peg/overview")
    assert len(calls) == 3                      # a new UTC day is a new key


def test_cache_expires(client, monkeypatch):
    calls = _count_calls(monkeypatch, "get_peg_runs")
    monkeypatch.setattr(server, "PEG_OVERVIEW_TTL_S", 0.0)
    client.get("/api/peg/overview")
    client.get("/api/peg/overview")
    assert len(calls) == 2


def test_side_panel_failure_degrades_and_is_not_cached(client, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("peg_ion_hits not migrated")

    monkeypatch.setattr(stan_db, "get_peg_ladder_month_counts", _boom)
    calls = _count_calls(monkeypatch, "get_peg_runs")
    r = client.get("/api/peg/overview")
    assert r.status_code == 200
    doc = r.json()
    assert doc["degraded"] == ["ladder"]
    assert len(doc["runs"]) == 31 and doc["ladder"]["months"]   # rest intact
    client.get("/api/peg/overview")
    assert len(calls) == 2                      # retried, not pinned for 10 min


def test_runs_failure_is_503_not_an_empty_tab(client, monkeypatch):
    """A PG outage must not render as 'no PEG measured'."""
    def _boom(*a, **kw):
        raise RuntimeError("server closed the connection")

    monkeypatch.setattr(stan_db, "get_peg_runs", _boom)
    r = client.get("/api/peg/overview")
    assert r.status_code == 503
    assert "closed" not in r.text


def test_public_get_passes_the_readonly_gate(monkeypatch):
    """The hosted dashboard sets STAN_DASHBOARD_READONLY; this GET stays open.

    The gate is installed at import time from the environment, so the route
    is mounted on a fresh app (the pattern tests/test_arcade_leaderboard.py
    uses) rather than re-importing server.py.
    """
    app = FastAPI()
    app.add_api_route("/api/peg/overview", lambda: {"ok": True}, methods=["GET"])
    monkeypatch.setenv("STAN_DASHBOARD_READONLY", "1")
    assert install_readonly_gate(app) is True
    c = TestClient(app)
    assert c.get("/api/peg/overview").status_code == 200
    assert c.get("/api/peg/overview?instrument=timsTOF%20HT").status_code == 200


# ── The PG branch, with the *_pg readers faked ───────────────────────

def test_pg_branch_uses_the_pg_readers_and_still_filters(monkeypatch):
    monkeypatch.setattr(server, "_peg_today", lambda: AS_OF)
    monkeypatch.setattr(server, "_PEG_CACHE", {})
    monkeypatch.setattr("stan.config.load_community", lambda: {"display_name": "PG Lab"})
    monkeypatch.setattr(db_pg, "use_pg", lambda: True)
    monkeypatch.setattr(stan_db, "get_db_path",
                        lambda: (_ for _ in ()).throw(AssertionError("SQLite touched")))
    seen: dict = {}

    def runs_pg(instrument):
        seen["runs"] = instrument
        base = {"instrument": TIMS, "spd": 100, "peg_n_ions_detected": 2,
                "n_precursors": 31000, "mode": "diaPASEF", "lc_system": "evosep"}
        rows = [dict(base, run_date_utc=f"2026-09-{d:02d}T18:27:00Z",
                     run_name=f"HeLa_pg_{d}.d", peg_score=7.5,
                     peg_intensity_pct=0.353, peg_class="clean") for d in range(1, 21)]
        # The SQL filters these; the Python half must hold even if it didn't.
        rows.append(dict(base, run_date_utc="2026-09-21T10:00:00Z",
                         run_name="Blank_pg.d", peg_score=90.0,
                         peg_intensity_pct=40.0, peg_class="heavy"))
        rows.append(dict(base, run_date_utc="2026-09-22T10:00:00Z",
                         run_name="HeLa_pg_unknown.d", peg_score=0.0,
                         peg_intensity_pct=0.0, peg_class="unknown"))
        return rows

    monkeypatch.setattr(db_pg, "get_peg_runs_pg", runs_pg)
    monkeypatch.setattr(db_pg, "get_peg_instrument_counts_pg",
                        lambda rx: seen.setdefault("rx", rx) and [(TIMS, "evosep", 20)])
    monkeypatch.setattr(db_pg, "get_peg_ladder_month_counts_pg",
                        lambda inst, rx: [("2026-09", 9, "+NH4", 5)])
    monkeypatch.setattr(db_pg, "get_column_change_events_pg",
                        lambda inst: [("2026-09-02 05:00:00+00", None)])
    monkeypatch.setattr(db_pg, "get_peg_lab_lc_summary_pg", lambda as_of, rx: [{
        "instrument": TIMS, "lc_system": "evosep", "n_90d": 20, "median_90d": 0.353,
        "clean_rate_90d": 100, "n_365d": 20, "median_365d": 0.353, "weekly": [None] * 26}])

    r = TestClient(server.app).get("/api/peg/overview")
    assert r.status_code == 200, r.text
    doc = r.json()
    assert seen["runs"] == TIMS and seen["rx"] == PG_BLANK_WASH_REGEX
    assert len(doc["runs"]) == 20
    assert doc["runs"][0] == ["2026-09-01T18:27", 100, 0.353, 7.5, 2, 0, 31000]
    assert doc["ladder"]["share"][doc["ladder"]["n"].index(9)] == [0.25]
    assert doc["column_periods"][0]["installed"] == "2026-09-02"
    assert doc["lab_lc"][0]["family"] == "timsTOF"
    assert doc["sharing"]["display_name"] == "PG Lab"
    assert "HeLa" not in r.text and "Blank" not in r.text


# ── The PG SQL itself, against a fake connection ─────────────────────

class _FakeCursor:
    def __init__(self, answer):
        self.answer = answer
        self.calls: list[tuple[str, object]] = []
        self.description = None
        self._rows: list = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        cols, rows = self.answer(sql, params)
        self.description = [(c,) for c in cols]
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cur):
        self.cur = cur

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_pg(monkeypatch):
    """Route db_pg._connect to a recording cursor; set .answer per test."""
    cur = _FakeCursor(lambda sql, params: ([], []))
    monkeypatch.setattr(db_pg, "_connect", lambda: _FakeConn(cur))
    monkeypatch.setattr(db_pg, "_RUNS_COLS_CACHE", ["id", "run_name", "peg_score"])
    return cur


def _assert_runs_sql_is_safe(sql: str) -> None:
    assert "SELECT *" not in sql.upper()
    assert "tic_" not in sql                          # never the inline TIC arrays
    assert "hidden, 0) = 0" in sql                    # integer, not boolean
    assert "TIMESTAMPTZ '2015-01-01" in sql
    assert "IN ('clean', 'trace', 'moderate', 'heavy')" in sql
    assert "peg_intensity_pct <> 'NaN'" in sql        # float4 NaN is not a measurement
    assert "substr(" not in sql                       # no substr on timestamptz


def test_pg_runs_sql(fake_pg):
    fake_pg.answer = lambda sql, p: (
        ["run_date_utc", "run_name", "peg_score"], [("2026-09-01T00:00:00Z", "a.d", 1)])
    rows = db_pg.get_peg_runs_pg(TIMS)
    sql, params = fake_pg.calls[-1]
    _assert_runs_sql_is_safe(sql)
    assert "to_char(run_date AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')" in sql
    assert "peg_score::numeric" in sql and "peg_intensity_pct::numeric" in sql
    assert params == (TIMS,)
    assert rows == [{"run_date_utc": "2026-09-01T00:00:00Z", "run_name": "a.d",
                     "peg_score": 1}]


def test_pg_instrument_counts_and_ladder_filter_blanks_in_sql(fake_pg):
    fake_pg.answer = lambda sql, p: (["i", "l", "n"], [(TIMS, "evosep", 7)])
    assert db_pg.get_peg_instrument_counts_pg(PG_BLANK_WASH_REGEX) == [(TIMS, "evosep", 7)]
    sql, params = fake_pg.calls[-1]
    _assert_runs_sql_is_safe(sql)
    assert "!~*" in sql and params == (PG_BLANK_WASH_REGEX,)

    fake_pg.answer = lambda sql, p: (["m", "n", "a", "k"], [("2026-09", 9, "+H", 3)])
    assert db_pg.get_peg_ladder_month_counts_pg(TIMS, PG_BLANK_WASH_REGEX) == [
        ("2026-09", 9, "+H", 3)]
    sql, params = fake_pg.calls[-1]
    assert "COALESCE(r.hidden, 0) = 0" in sql and "r.peg_class" in sql
    assert "to_char(r.run_date AT TIME ZONE 'UTC', 'YYYY-MM')" in sql
    assert "count(DISTINCT h.run_id)" in sql and "h.source = 'runs'" in sql
    assert params == (TIMS, PG_BLANK_WASH_REGEX)


def test_pg_column_events_sql_leaves_notes_behind(fake_pg):
    fake_pg.answer = lambda sql, p: (["d", "m"], [("2026-09-01", "PepSep")])
    assert db_pg.get_column_change_events_pg(TIMS) == [("2026-09-01", "PepSep")]
    sql, params = fake_pg.calls[-1]
    assert "notes" not in sql and "operator" not in sql and "SELECT *" not in sql
    assert "event_type = 'column_change'" in sql and params == (TIMS,)


@pytest.mark.parametrize("has_sample_type", [False, True])
def test_pg_share_rows_sql(fake_pg, monkeypatch, has_sample_type):
    cols = ["id", "run_name"] + (["sample_type"] if has_sample_type else [])
    monkeypatch.setattr(db_pg, "_RUNS_COLS_CACHE", cols)
    fake_pg.answer = lambda sql, p: (["run_name"], [])
    db_pg.get_peg_share_rows_pg()
    sql, _ = fake_pg.calls[-1]
    _assert_runs_sql_is_safe(sql)
    assert ("sample_type" in sql) is has_sample_type
    assert "run_name" in sql and "lc_system" in sql and "amount_ng::numeric" in sql


def test_pg_lab_lc_sql_and_assembly(fake_pg):
    def answer(sql, params):
        if "date_trunc('week', t)" in sql:
            return ["i", "w", "m"], [(TIMS, "2026-09-28", 2.0), (TIMS, "2026-04-06", 5.0)]
        if "percentile_cont" in sql:
            return ["i", "n90", "m90", "c90", "n365", "m365"], [
                (TIMS, 40, 3.14159, 10, 400, 2.5), (EXPL, 0, None, 0, 5, 0.25)]
        return ["i", "lc", "n"], [(TIMS, "evosep", 400), (TIMS, "", 3), (EXPL, "custom", 5)]

    fake_pg.answer = answer
    out = db_pg.get_peg_lab_lc_summary_pg(AS_OF, PG_BLANK_WASH_REGEX)
    assert len(fake_pg.calls) == 3
    for sql, params in fake_pg.calls:
        assert "SELECT *" not in sql.upper() and "hidden, 0) = 0" in sql
        assert "(run_date AT TIME ZONE 'UTC') AS t" in sql and "!~* %(blank)s" in sql
        assert params["blank"] == PG_BLANK_WASH_REGEX
        # naive UTC datetimes, compared with the naive UTC timestamp `t`
        assert params["end"] == datetime(2026, 9, 29) and params["end"].tzinfo is None
        assert params["s90"] == datetime(2026, 7, 1)
        assert params["s365"] == datetime(2025, 9, 29)
        assert params["w0"] == datetime(2026, 4, 6)
    assert "percentile_cont(0.5) WITHIN GROUP (ORDER BY pct)" in fake_pg.calls[0][0]
    tims, expl = out
    assert tims == {"instrument": TIMS, "lc_system": "evosep", "n_90d": 40,
                    "median_90d": 3.142, "clean_rate_90d": 25, "n_365d": 400,
                    "median_365d": 2.5, "weekly": [5.0] + [None] * 24 + [2.0]}
    assert expl["median_90d"] is None and expl["clean_rate_90d"] is None
    assert expl["lc_system"] == "custom" and expl["n_365d"] == 5


# ── The share-client reader ──────────────────────────────────────────

def test_share_rows_keep_names_for_hashing_and_drop_the_rest(db_path):
    rows = stan_db.get_peg_share_rows()
    names = {r["run_name"] for r in rows}
    assert names == set(REAL_NAMES)                 # every LC, every instrument
    assert not names & set(EXCLUDED_NAMES.values())
    tz = next(r for r in rows if r["run_name"] == "HeLa_tz_1_1999.d")
    assert tz["run_date"] == tz["run_date_utc"] == "2026-09-22T07:30:00Z"
    assert {r["lc_system"] for r in rows} == {"evosep", "custom"}
    assert set(tz) == {"run_name", "instrument", "run_date", "run_date_utc", "spd", "mode",
                       "amount_ng", "lc_system", "peg_score", "peg_intensity_pct",
                       "peg_n_ions_detected", "peg_class"}
    json.dumps(rows)


def test_share_rows_include_sample_type_when_the_column_exists(db_path):
    with sqlite3.connect(db_path) as con:
        con.execute("ALTER TABLE runs ADD COLUMN sample_type TEXT")
        con.execute("UPDATE runs SET sample_type = 'hela'")
    rows = stan_db.get_peg_share_rows()
    assert rows and all(r["sample_type"] == "hela" for r in rows)


def test_dashboard_readers_never_return_names(db_path):
    for row in stan_db.get_peg_runs():
        assert "run_name" not in row and "raw_path" not in row
    assert stan_db.get_column_change_events(TIMS) == [("2026-09-01T12:00:00Z", "PepSep 10cm")]
