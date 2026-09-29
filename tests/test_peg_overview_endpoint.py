"""GET /api/peg/overview and the PEG Watch DB readers behind it.

What this pins, in order of how badly it would hurt to lose it:

* The endpoint is PUBLIC on the hosted dashboard, so no run name, sample name
  or maintenance note may appear anywhere in its response.
* Only real PEG on QC runs counts: an ``'unknown'`` row (the failure sentinel,
  score 0.0), a NULL row, a hidden row, a blank, a failed acquisition and the
  bogus 1980 row are all excluded -- from the runs AND from the ladder built
  out of their hits.
* One acquisition is one run, on both backends and in the share rows, and
  the copy kept never depends on the order the store returns rows in.
* A store that cannot be read is a 503, never a cached "no PEG".
* SQLite's mixed-offset TEXT dates land on the right UTC minute.
* Nothing local reproduces a PG type error, so the PG SQL is checked for the
  shapes CLAUDE.md warns about (timestamptz formatting, integer hidden,
  explicit column lists), and the PG branch is exercised end to end with
  fake readers.
"""

from __future__ import annotations

import json
import re
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

_DEFAULT = object()


def _insert_run(con, rid, name, run_date, *, instrument=TIMS, spd=100, cls="clean",
                pct=0.1, score=5.0, hidden=0, lc="evosep", prec=30000, mode="diaPASEF",
                ions=_DEFAULT, version=None, raw_path=None):
    if ions is _DEFAULT:
        ions = 3 if score is not None else None
    con.execute(
        "INSERT INTO runs (id, instrument, run_name, run_date, raw_path, mode, spd, "
        "peg_score, peg_intensity_pct, peg_n_ions_detected, peg_class, n_precursors, "
        "lc_system, hidden, stan_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rid, instrument, name, run_date, raw_path or f"/data/{name}", mode, spd, score,
         pct, ions, cls, prec, lc, hidden, version),
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
    # Only t0-t2 (all August) have stored hits; every other run found 3 PEG
    # ions and kept none, so its ladder is unknown and September has no
    # month to show -- not a month of clean ladders.
    assert lad["months"] == ["2026-08"] and lad["nruns"] == [3]
    # +NH4 on t0, t1; +H on t1; +Na on t2. The +Na PEG15 hits on the unknown,
    # hidden, blank and 1980 rows must be nowhere.
    assert lad["adducts"] == {"+H": 1, "+NH4": 2, "+Na": 1}
    assert all(v == 0 for v in lad["share"][lad["n"].index(15)])
    assert lad["share"][lad["n"].index(9)] == [round(2 / 3, 3)]


def test_ladder_denominator_skips_runs_with_ions_but_no_hits(client, db_path):
    """The verifier's case: 2 runs with PEG10, 1 with ions and no hits, 1 clean.

    share(n=10) is 2/3, not 2/4: the run whose hits were never stored is not
    evidence that PEG10 was absent.
    """
    inst = "timsTOF Ultra"
    with sqlite3.connect(db_path) as con:
        for rid, day, ions in [("u1", 2, 9), ("u2", 3, 9), ("u3", 4, 3), ("u4", 5, 0)]:
            _insert_run(con, rid, f"HeLa_u_{rid}.d", f"2026-09-{day:02d}T10:00:00Z",
                        instrument=inst, ions=ions, cls="heavy" if ions else "clean",
                        pct=6.0 if ions else 0.0)
        _hit(con, "u1", 10, "+NH4")
        _hit(con, "u2", 10, "+NH4")
    lad = client.get("/api/peg/overview", params={"instrument": inst}).json()["ladder"]
    assert lad["months"] == ["2026-09"] and lad["nruns"] == [3]
    assert lad["share"][lad["n"].index(10)] == [round(2 / 3, 3)]


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
    assert doc["sharing"] == {"enabled": False, "source": "off", "display_name": None,
                              "relay_url": RELAY_URL}


def test_missing_store_file_is_empty_not_500(tmp_path, monkeypatch):
    monkeypatch.delenv("STAN_DB_BACKEND", raising=False)
    monkeypatch.setattr(stan_db, "get_db_path", lambda: tmp_path / "absent.db")
    monkeypatch.setattr(server, "_PEG_CACHE", {})
    r = TestClient(server.app).get("/api/peg/overview")
    assert r.status_code == 200 and r.json()["runs"] == []


# ── Sharing status ───────────────────────────────────────────────────

def test_sharing_off_by_default(client):
    s = client.get("/api/peg/overview").json()["sharing"]
    assert s == {"enabled": False, "source": "off", "display_name": "Test Lab",
                 "relay_url": RELAY_URL}


@pytest.mark.parametrize("flag", [True, "true", "yes", 1])
def test_sharing_on_from_community_yml(client, monkeypatch, flag):
    monkeypatch.setattr("stan.config.load_community",
                        lambda: {"display_name": "Clogged PeakTail", "peg_share": flag})
    s = client.get("/api/peg/overview").json()["sharing"]
    assert s["enabled"] is True and s["display_name"] == "Clogged PeakTail"
    assert s["source"] == "config"


def test_sharing_env_override_and_hosted_name(client, monkeypatch):
    """The hosted container has no community.yml, only env vars."""
    def _missing():
        raise FileNotFoundError("community.yml")

    monkeypatch.setattr("stan.config.load_community", _missing)
    monkeypatch.setenv("STAN_PEG_SHARE", "1")
    monkeypatch.setenv("STAN_DISPLAY_NAME", "Clogged PeakTail")
    s = client.get("/api/peg/overview").json()["sharing"]
    assert s == {"enabled": True, "source": "env", "display_name": "Clogged PeakTail",
                 "relay_url": RELAY_URL}


def test_sharing_source_prefers_this_hosts_own_config(client, monkeypatch):
    """community.yml is what `stan peg-sync` on this host obeys; name it first.

    ``env`` is the weaker claim -- on the hosted dashboard it means "this
    host was told", not "this host sends" -- so it is reported only when
    the config does not already say so.
    """
    monkeypatch.setattr("stan.config.load_community",
                        lambda: {"display_name": "Clogged PeakTail", "peg_share": True})
    monkeypatch.setenv("STAN_PEG_SHARE", "1")
    assert client.get("/api/peg/overview").json()["sharing"]["source"] == "config"
    monkeypatch.setattr("stan.config.load_community",
                        lambda: {"display_name": "Clogged PeakTail", "peg_share": False})
    assert client.get("/api/peg/overview").json()["sharing"]["source"] == "env"
    monkeypatch.setenv("STAN_PEG_SHARE", "0")
    s = client.get("/api/peg/overview").json()["sharing"]
    assert (s["enabled"], s["source"]) == (False, "opted_out")


@pytest.mark.parametrize("flag", [False, "false", "no", 0, "0", "off"])
def test_explicit_opt_out_is_told_apart_from_no_setting(client, monkeypatch, flag):
    """``peg_share: false`` here is a decision; no key at all is not.

    The tab lets the relay's board speak for a host that has no setting of
    its own (the hosted dashboard, whose sync runs on Hive), but must not
    override a lab that switched sharing off on this very machine -- its
    runs from before the switch stay on the board for the whole window, and
    reading them as "sharing is on" told it the opposite of what it chose
    (review round 2, RG-UI-1). Both answers used to be ``source: "off"``.
    """
    monkeypatch.setattr("stan.config.load_community",
                        lambda: {"display_name": "E2E Lab", "peg_share": flag})
    s = client.get("/api/peg/overview").json()["sharing"]
    assert (s["enabled"], s["source"]) == (False, "opted_out")


@pytest.mark.parametrize("cfg", [{"display_name": "E2E Lab"},
                                 {"display_name": "E2E Lab", "peg_share": None},
                                 {"display_name": "E2E Lab", "peg_share": ""}])
def test_no_peg_share_value_is_no_setting(client, monkeypatch, cfg):
    """An absent or empty ``peg_share:`` says nothing either way."""
    monkeypatch.setattr("stan.config.load_community", lambda: cfg)
    s = client.get("/api/peg/overview").json()["sharing"]
    assert (s["enabled"], s["source"]) == (False, "off")


def test_explicit_env_opt_out_on_a_host_without_config(client, monkeypatch):
    """``STAN_PEG_SHARE=0`` is the hosted container's only way to say no."""
    def _missing():
        raise FileNotFoundError("community.yml")

    monkeypatch.setattr("stan.config.load_community", _missing)
    monkeypatch.setenv("STAN_DISPLAY_NAME", "E2E Lab")
    monkeypatch.setenv("STAN_PEG_SHARE", "0")
    assert client.get("/api/peg/overview").json()["sharing"]["source"] == "opted_out"
    monkeypatch.setenv("STAN_PEG_SHARE", "")
    assert client.get("/api/peg/overview").json()["sharing"]["source"] == "off"


def test_hosted_sharing_is_only_what_this_host_is_told(client, monkeypatch):
    """The hosted container knows the name but not the Hive cron's peg_share.

    Documents the deploy dependency: at UC Davis the sync runs on Hive, so
    ucd.stan-proteomics.org reports sharing on only with STAN_PEG_SHARE=1 in
    its own app settings -- without it the tab says PEG stays in the lab.
    """
    def _missing():
        raise FileNotFoundError("community.yml")

    monkeypatch.setattr("stan.config.load_community", _missing)
    monkeypatch.setenv("STAN_DISPLAY_NAME", "Clogged PeakTail")
    s = client.get("/api/peg/overview").json()["sharing"]
    assert s == {"enabled": False, "source": "off", "display_name": "Clogged PeakTail",
                 "relay_url": RELAY_URL}


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


def test_locked_store_is_503_and_nothing_is_cached(client, db_path, monkeypatch):
    """A lock (or SQLITE_IOERR) is not "no PEG measured".

    The readers swallowed every OperationalError as [], and the endpoint
    cached the empty answer for PEG_OVERVIEW_TTL_S: ten minutes of an empty
    tab telling the lab to install PEG dependencies, after one lock.
    """
    real_connect = stan_db.connect
    monkeypatch.setattr(stan_db, "connect", lambda p, **kw: real_connect(p, timeout=0.05))
    holder = sqlite3.connect(db_path)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        r = client.get("/api/peg/overview")
        assert r.status_code == 503
        assert server._PEG_CACHE == {}
    finally:
        holder.rollback()
        holder.close()
    r = client.get("/api/peg/overview")
    assert r.status_code == 200 and len(r.json()["runs"]) == 31


def test_lock_during_the_build_is_503_not_a_cached_empty_tab(client, db_path, monkeypatch):
    """Instruments already cached, then the store locks: still a 503."""
    assert client.get("/api/peg/overview").status_code == 200
    server._PEG_CACHE.pop(next(k for k in server._PEG_CACHE if k[0] == "overview"))
    real_connect = stan_db.connect
    monkeypatch.setattr(stan_db, "connect", lambda p, **kw: real_connect(p, timeout=0.05))
    holder = sqlite3.connect(db_path)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        assert client.get("/api/peg/overview").status_code == 503
        assert not any(k[0] == "overview" for k in server._PEG_CACHE)
    finally:
        holder.rollback()
        holder.close()
    assert len(client.get("/api/peg/overview").json()["runs"]) == 31


class _LockedFor:
    """A real connection whose queries touching ``needle`` hit a lock."""

    def __init__(self, con, needle):
        object.__setattr__(self, "_con", con)
        object.__setattr__(self, "_needle", needle)

    def execute(self, sql, params=()):
        if self._needle in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._con.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._con, name)

    def __setattr__(self, name, value):
        setattr(self._con, name, value)

    def __enter__(self):
        self._con.__enter__()
        return self

    def __exit__(self, *exc):
        return self._con.__exit__(*exc)


def test_side_panel_lock_degrades_instead_of_reading_empty(client, monkeypatch):
    """A locked column log is a degraded panel, not "no column changes"."""
    real_connect = stan_db.connect
    monkeypatch.setattr(stan_db, "connect",
                        lambda p, **kw: _LockedFor(real_connect(p, **kw), "maintenance_events"))
    r = client.get("/api/peg/overview")
    assert r.status_code == 200
    doc = r.json()
    assert doc["degraded"] == ["column_periods"] and doc["column_periods"] == []
    assert len(doc["runs"]) == 31
    assert not any(k[0] == "overview" for k in server._PEG_CACHE)


def test_missing_table_is_still_empty_not_an_error(tmp_path, monkeypatch):
    path = tmp_path / "bare.db"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE unrelated (x INTEGER)")
    assert stan_db.get_peg_runs(db_path=path) == []
    assert stan_db.get_peg_instruments(db_path=path) == []


def test_empty_instrument_list_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.delenv("STAN_DB_BACKEND", raising=False)
    path = tmp_path / "empty.db"
    stan_db.init_db(path)
    monkeypatch.setattr(stan_db, "get_db_path", lambda: path)
    monkeypatch.setattr(server, "_peg_today", lambda: AS_OF)
    monkeypatch.setattr(server, "_PEG_CACHE", {})
    monkeypatch.setattr("stan.config.load_community", lambda: {})
    c = TestClient(server.app)
    assert c.get("/api/peg/overview").json()["instruments"] == []
    assert server._PEG_CACHE == {}
    with sqlite3.connect(path) as con:
        _insert_run(con, "late", "HeLa_late.d", "2026-09-27T10:00:00Z")
    assert [i["instrument"] for i in c.get("/api/peg/overview").json()["instruments"]] == [TIMS]


# ── One acquisition, one run ─────────────────────────────────────────

def _seed_duplicates(con, order):
    """One raw file ingested three times, as live PG holds 241 of them.

    The instrument-PC copy (0.2.222, no ladder) reads heavy; the Hive copies
    have the ladder, and the newer of those (by number: 1.0.44 > 0.2.376,
    though not as text) is the one kept. Offsets differ, the instant does not.
    """
    copies = {
        "pc": dict(stamp="2026-09-15T03:00:00-07:00", version="0.2.222", cls="heavy",
                   pct=7.5, score=80.0, raw_path="D:/Data/dup.d"),
        "hive_old": dict(stamp="2026-09-15T10:00:00Z", version="0.2.376", cls="trace",
                         pct=1.9, score=30.0, raw_path="/quobyte/dup.d"),
        "hive_new": dict(stamp="2026-09-15 10:00:00+00:00", version="1.0.44", cls="clean",
                         pct=0.4, score=9.0, raw_path="/nfs/dup.d"),
    }
    for rid in order:
        c = copies[rid]
        _insert_run(con, rid, "HeLa_dup_1_3001.d", c["stamp"], cls=c["cls"], pct=c["pct"],
                    score=c["score"], version=c["version"], raw_path=c["raw_path"])
    _hit(con, "hive_old", 9, "+NH4")
    _hit(con, "hive_new", 12, "+Na")


@pytest.mark.parametrize("order", [("pc", "hive_old", "hive_new"),
                                   ("hive_new", "hive_old", "pc"),
                                   ("hive_old", "pc", "hive_new")])
def test_a_duplicated_acquisition_counts_once_whatever_the_row_order(client, db_path, order):
    with sqlite3.connect(db_path) as con:
        _seed_duplicates(con, order)
    doc = client.get("/api/peg/overview").json()
    dup = [row for row in doc["runs"] if row[0] == "2026-09-15T10:00"]
    assert dup == [["2026-09-15T10:00", 100, 0.4, 9.0, 3, 0, 30000]]    # the 1.0.44 copy
    assert len(doc["runs"]) == 32
    assert doc["instruments"][0]["n_runs"] == 32
    lad = doc["ladder"]
    sep = lad["months"].index("2026-09")
    assert lad["nruns"][sep] == 1                   # the kept copy has hits
    assert lad["share"][lad["n"].index(12)][sep] == 1.0
    assert lad["share"][lad["n"].index(9)][sep] == 0.0      # the dropped copy's hit
    assert next(x for x in doc["lab_lc"] if x["instrument"] == TIMS)["n_90d"] == 32

    share = [r for r in stan_db.get_peg_share_rows() if r["run_name"] == "HeLa_dup_1_3001.d"]
    assert len(share) == 1
    assert (share[0]["peg_class"], share[0]["peg_intensity_pct"]) == ("clean", 0.4)
    assert share[0]["run_date"] == "2026-09-15T10:00:00Z"
    # the client ranks by the same keys, so it gets them (and never sends them)
    assert (share[0]["id"], share[0]["stan_version"], share[0]["has_hits"]) == (
        "hive_new", "1.0.44", True)


def test_hits_outrank_a_newer_version(db_path):
    """A newer copy without its ladder loses to one with it (spec 4.1)."""
    with sqlite3.connect(db_path) as con:
        _insert_run(con, "new_nohits", "HeLa_d2.d", "2026-09-16T10:00:00Z", pct=5.0,
                    cls="heavy", score=80.0, version="1.1.12", raw_path="/a/HeLa_d2.d")
        _insert_run(con, "old_hits", "HeLa_d2.d", "2026-09-16T10:00:00Z", pct=0.5,
                    cls="clean", score=9.0, version="0.2.376", raw_path="/b/HeLa_d2.d")
        _hit(con, "old_hits", 9, "+H")
    row = next(r for r in stan_db.get_peg_share_rows() if r["run_name"] == "HeLa_d2.d")
    assert row["peg_intensity_pct"] == 0.5
    kept = [r for r in stan_db.get_peg_runs(TIMS) if r["run_date_utc"] == "2026-09-16T10:00:00Z"]
    assert len(kept) == 1 and kept[0]["has_hits"] is True


# ── One rule for the tab, the share reader and the share client ─────

#: Copies of three acquisitions, in the shapes live PG duplicates them. Per
#: acquisition: (copies, the id the rule keeps). Copy: (id, run_name, stamp,
#: class, pct, score, stan_version, has_hits).
_ONE_RULE = {
    # One file under two directories: the instrument PC's path and Hive's.
    # The ladder is on the OLDER processing, and hits outrank the version.
    "2026-09-19T10:00:00Z": ([
        ("a_pc", r"D:\Data\QC\HeLa_rule_A_1.d", "2026-09-19T03:00:00-07:00",
         "heavy", 7.5, 80.0, "1.1.12", False),
        ("a_hive", "/quobyte/proteomics-grp/STAN/raw/HeLa_rule_A_1.d/",
         "2026-09-19T10:00:00Z", "clean", 0.4, 9.0, "0.2.376", True),
    ], "a_hive"),
    # No ladder on either: the newer version by number (text says 1.0.9).
    "2026-09-19T11:00:00Z": ([
        ("b_old", "HeLa_rule_B_1.d", "2026-09-19T11:00:00Z", "trace", 1.5, 30.0, "1.0.9", False),
        ("b_new", "HeLa_rule_B_1.d", "2026-09-19 11:00:00+00:00", "moderate", 3.0, 55.0,
         "1.0.10", False),
    ], "b_new"),
    # Same processing on both (43 such /quobyte vs /nfs pairs on live PG):
    # only the id can decide, and it must decide the same way everywhere.
    "2026-09-19T12:00:00Z": ([
        ("c_1", "/nfs/lssc0/flinders/HeLa_rule_C_1.d", "2026-09-19T12:00:00Z",
         "heavy", 8.0, 85.0, "1.0.44", False),
        ("c_2", "/quobyte/proteomics-grp/HeLa_rule_C_1.d", "2026-09-19T05:00:00-07:00",
         "clean", 0.2, 5.0, "1.0.44", False),
    ], "c_2"),
}


def test_one_dedupe_rule_for_the_tab_the_share_rows_and_the_client(client, db_path):
    """The overview, the share reader and the share client keep the same copy.

    Built through the real store and the real client: an acquisition the tab
    counts at one PEG value must go to the board at that value, once --
    including when its copies differ only by directory, which the client's
    run_key (a basename) has always merged.
    """
    from stan.community.peg_submit import build_peg_records

    by_id = {}
    with sqlite3.connect(db_path) as con:
        for stamp, (copies, _keep) in _ONE_RULE.items():
            for rid, name, when, cls, pct, score, ver, hits in copies:
                _insert_run(con, rid, name, when, cls=cls, pct=pct, score=score,
                            version=ver, raw_path=f"/copy/{rid}/x.d")
                if hits:
                    _hit(con, rid, 9, "+NH4")
                by_id[rid] = (cls, pct)

    # 1. The tab: one run per acquisition, at the kept copy's value.
    doc = client.get("/api/peg/overview").json()
    cols = doc["runs_cols"]
    for stamp, (_copies, keep) in _ONE_RULE.items():
        got = [r for r in doc["runs"] if r[cols.index("t")] == stamp[:16]]
        assert [r[cols.index("pct")] for r in got] == [by_id[keep][1]], stamp
    assert doc["instruments"][0]["n_runs"] == 31 + len(_ONE_RULE)

    # 2. The share reader: the same copies, carrying the keys the client ranks by.
    share = stan_db.get_peg_share_rows()
    mine = {r["run_date"]: r for r in share if r["run_date"] in _ONE_RULE}
    assert {s: r["id"] for s, r in mine.items()} == {s: k for s, (_c, k) in _ONE_RULE.items()}
    assert len(share) == len(REAL_NAMES) + len(_ONE_RULE)

    # 3. The client, from the reader: one record each, at the tab's value.
    records, skipped = build_peg_records(share)
    sent = {r["run_date"]: (r["peg_class"], r["peg_intensity_pct"]) for r in records
            if r["run_date"] in _ONE_RULE}
    assert sent == {s: by_id[k] for s, (_c, k) in _ONE_RULE.items()}
    assert "duplicate_run_key" not in skipped

    # 4. The client alone, handed every copy (its second line of defence):
    #    it still keeps exactly what the reader kept.
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        ids = [c[0] for copies, _k in _ONE_RULE.values() for c in copies]
        raw = [dict(r) for r in con.execute(
            "SELECT id, run_name, instrument, run_date, spd, mode, amount_ng, lc_system, "
            "peg_score, peg_intensity_pct, peg_n_ions_detected, peg_class, stan_version "
            f"FROM runs WHERE id IN ({','.join('?' * len(ids))})", ids)]
        hit_ids = {r[0] for r in con.execute("SELECT run_id FROM peg_ion_hits")}
    for r in raw:
        r["has_hits"] = r["id"] in hit_ids
    for rows in (raw, list(reversed(raw))):
        alone, skipped = build_peg_records(rows)
        assert {r["run_date"]: (r["peg_class"], r["peg_intensity_pct"]) for r in alone} == sent
        assert skipped == {"duplicate_run_key": 3}


def test_share_rows_come_in_a_total_order(db_path):
    """Same-second runs on two instruments: the order is fixed, not heap order."""
    with sqlite3.connect(db_path) as con:
        _insert_run(con, "z1", "HeLa_same_b.d", "2026-09-17T10:00:00Z")
        _insert_run(con, "z2", "HeLa_same_a.d", "2026-09-17T10:00:00Z", instrument=EXPL)
        _insert_run(con, "z3", "HeLa_same_a.d", "2026-09-17T10:00:00Z")
    rows = [(r["instrument"], r["run_name"]) for r in stan_db.get_peg_share_rows()
            if r["run_date"] == "2026-09-17T10:00:00Z"]
    assert rows == [(EXPL, "HeLa_same_a.d"), (TIMS, "HeLa_same_a.d"), (TIMS, "HeLa_same_b.d")]


# ── Failed acquisitions ──────────────────────────────────────────────

def test_failed_acquisitions_are_not_counted_clean(client, db_path):
    """0 precursors + 0 PEG ions + exactly 0 %: nothing was measured.

    detect_peg_in_spectra returns that when it summed no MS1, and it scores
    0 and classifies 'clean'. Live PG holds 21 such timsTOF rows.
    """
    with sqlite3.connect(db_path) as con:
        _insert_run(con, "f_empty", "HeLa_aborted_1.d", "2026-09-18T10:00:00Z",
                    pct=0.0, score=0.0, prec=0, ions=0)
        _insert_run(con, "f_ions", "HeLa_prec0_ions.d", "2026-09-18T11:00:00Z",
                    pct=0.0, score=0.0, prec=0, ions=6)
        _insert_run(con, "f_nullprec", "HeLa_dda_1.d", "2026-09-18T12:00:00Z",
                    pct=0.0, score=0.0, prec=None, ions=0)
    names = {r["run_name"] for r in stan_db.get_peg_share_rows()}
    assert "HeLa_aborted_1.d" not in names
    assert {"HeLa_prec0_ions.d", "HeLa_dda_1.d"} <= names
    stamps = {r["run_date_utc"] for r in stan_db.get_peg_runs(TIMS)}
    assert "2026-09-18T10:00:00Z" not in stamps
    assert {"2026-09-18T11:00:00Z", "2026-09-18T12:00:00Z"} <= stamps
    assert client.get("/api/peg/overview").json()["instruments"][0]["n_runs"] == 33


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
                     peg_intensity_pct=0.353, peg_class="clean", has_hits=True)
                for d in range(1, 21)]
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
    assert "has_hits" not in r.text                 # internal, not in the payload


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
    # failed acquisitions: 0 precursors, 0 ions, exactly 0 %
    assert ("NOT (COALESCE(r.n_precursors, -1) = 0 "
            "AND COALESCE(r.peg_n_ions_detected, -1) = 0 AND r.peg_intensity_pct = 0)") in sql
    _assert_one_row_per_acquisition(sql)


#: The acquisition key the share client's run_key hashes: trimmed instrument,
#: run_name's basename (trim, drop trailing separators, drop the directory),
#: and the UTC second.
_PG_ACQ_KEY = (
    r"regexp_replace(COALESCE(r.instrument, ''), '^\s+|\s+$', '', 'g'), "
    r"regexp_replace(regexp_replace(regexp_replace(COALESCE(r.run_name, ''), "
    r"'^\s+|\s+$', '', 'g'), '[/\\]+$', ''), '^.*[/\\]', ''), "
    "date_trunc('second', r.run_date)"
)


def _assert_one_row_per_acquisition(sql: str) -> None:
    """The canonical-row rule, identical to peg_trends.pick_canonical."""
    key = _PG_ACQ_KEY
    assert f"SELECT DISTINCT ON ({key})" in sql
    order = sql.split(f"ORDER BY {key}, ", 1)[1]
    hits = ("EXISTS (SELECT 1 FROM peg_ion_hits x WHERE x.run_id = r.id "
            "AND x.source = 'runs') DESC")
    version = ("string_to_array(substring(r.stan_version FROM "
               "'^\\s*v?([0-9]+(?:\\.[0-9]+)*)'), '.')::numeric[] DESC NULLS LAST")
    assert order.startswith(f"{hits}, {version}, r.id::text COLLATE \"C\" DESC")


def test_pg_runs_sql(fake_pg):
    fake_pg.answer = lambda sql, p: (
        ["run_date_utc", "run_name", "peg_score"], [("2026-09-01T00:00:00Z", "a.d", 1)])
    rows = db_pg.get_peg_runs_pg(TIMS)
    sql, params = fake_pg.calls[-1]
    _assert_runs_sql_is_safe(sql)
    assert "to_char(run_date AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')" in sql
    assert "peg_score::numeric" in sql and "peg_intensity_pct::numeric" in sql
    assert "AS has_hits" in sql and "AND r.instrument = %s" in sql
    assert sql.endswith("ORDER BY run_date ASC, instrument, run_name")
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
    _assert_runs_sql_is_safe(sql)                     # hits of the kept copy only
    assert "to_char(c.run_date AT TIME ZONE 'UTC', 'YYYY-MM')" in sql
    assert "count(DISTINCT h.run_id)" in sql and "h.source = 'runs'" in sql
    assert "JOIN (SELECT DISTINCT ON" in sql and "c.id = h.run_id" in sql
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
    # A total order: run_date alone left copies of one file tied, and the
    # client kept whichever the sort happened to put first.
    assert sql.endswith("ORDER BY run_date ASC, instrument, run_name")
    # The keys the client ranks copies by, so its rank is the reader's.
    inner = sql.split(" FROM (", 1)[1]
    assert "r.id, r.stan_version, EXISTS (SELECT 1 FROM peg_ion_hits x" in inner
    outer = sql.split(" FROM (", 1)[0]
    assert ", id, stan_version, has_hits" in outer


@pytest.mark.parametrize("name", [
    r"D:\Data\QC\HeLa_1.d", "/quobyte/proteomics-grp/STAN/raw/HeLa_1.d/",
    " HeLa_1.d ", "HeLa_1.d", "a/b\\c.raw", "x.d\\\\", "/", "", "  ",
    "/nfs/x/HeLa 2.d", "HeLa_1.d / ",
])
def test_pg_basename_is_run_basename(name):
    """PG's acquisition basename, run with the same patterns, is run_basename.

    Nothing local runs PG, but these patterns use only what POSIX AREs and
    Python's ``re`` read alike (anchors, ``\\s``, a bracket of ``/`` and
    ``\\``), so applying them here in the SQL's order checks what PG does.
    """
    from stan.community.peg_submit import run_basename

    s = re.sub(r"^\s+|\s+$", "", name)                 # regexp_replace(..., 'g')
    s = re.sub(r"[/\\]+$", "", s, count=1)
    s = re.sub(r"^.*[/\\]", "", s, count=1)
    assert s == run_basename(name)
    assert _PG_ACQ_KEY.count(r"'^\s+|\s+$', '', 'g'") == 2
    assert r"'[/\\]+$', ''), '^.*[/\\]', '')" in _PG_ACQ_KEY


def test_pg_lab_lc_sql_and_assembly(fake_pg):
    def answer(sql, params):
        if "604800" in sql:
            return ["i", "w", "m"], [(TIMS, 25, 2.0), (TIMS, 0, 5.0)]
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
        _assert_one_row_per_acquisition(sql)
        assert params["blank"] == PG_BLANK_WASH_REGEX
        # naive UTC datetimes, compared with the naive UTC timestamp `t`
        assert params["end"] == datetime(2026, 9, 29) and params["end"].tzinfo is None
        assert params["s90"] == datetime(2026, 7, 1)
        assert params["s365"] == datetime(2025, 9, 29)
        # 26 trailing 7-day buckets ending with as_of: the relay's weeks
        assert params["wstart"] == datetime(2026, 3, 31) and params["wstart"].tzinfo is None
    assert "percentile_cont(0.5) WITHIN GROUP (ORDER BY pct)" in fake_pg.calls[0][0]
    wk_sql = next(q for q, _ in fake_pg.calls if "604800" in q)
    assert "floor(extract(epoch FROM (t - %(wstart)s)) / 604800)::int" in wk_sql
    assert "t >= %(wstart)s" in wk_sql and "date_trunc('week'" not in wk_sql
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
                       "peg_n_ions_detected", "peg_class",
                       "id", "stan_version", "has_hits"}      # ranking keys, never sent
    json.dumps(rows)


def test_share_rows_survive_a_store_without_stan_version(tmp_path, monkeypatch):
    """stan_version is a migration column; an unmigrated file lacks it.

    Selected blind, "no such column" answered [] and the lab shared nothing.
    Guarded by PRAGMA table_info, the rows come back ranked without it.
    """
    monkeypatch.delenv("STAN_DB_BACKEND", raising=False)
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as con:
        con.execute(
            "CREATE TABLE runs (id TEXT PRIMARY KEY, instrument TEXT, run_name TEXT, "
            "run_date TEXT, spd INTEGER, mode TEXT, amount_ng REAL, lc_system TEXT, "
            "hidden INTEGER DEFAULT 0, n_precursors INTEGER, peg_score REAL, "
            "peg_n_ions_detected INTEGER, peg_intensity_pct REAL, peg_class TEXT)")
        for rid in ("r1", "r2"):
            con.execute(
                "INSERT INTO runs VALUES (?, ?, 'HeLa_old.d', '2026-09-01T10:00:00Z', 100, "
                "'diaPASEF', 50, 'evosep', 0, 30000, 5.0, 3, 0.1, 'clean')", (rid, TIMS))
    rows = stan_db.get_peg_share_rows(db_path=path)
    assert [(r["id"], r["stan_version"], r["has_hits"]) for r in rows] == [("r2", None, False)]
    assert len(stan_db.get_peg_runs(TIMS, db_path=path)) == 1


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
