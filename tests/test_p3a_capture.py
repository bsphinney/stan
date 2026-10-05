"""P3a (STAN 1.2.16): four per-run cohort attributes, captured for new runs.

* amount_source -- stan.community.amount: the unit-anchored file-name parser,
  the declared > parsed > instrument default > assumed 50 precedence, and the
  problems (conflict, two amounts, above 5,000 ng) that are never sent.
* faims -- a "cv=" compensation voltage in the Thermo scan filters.
* lc_model -- canonical LC names, from the HyStar method of a real timsTOF HT
  .d (tests/fixtures/bruker_hystar) and the DriverIds of a Thermo .raw.
* lc_flow -- nano | capillary | micro, from add-watch / setup / dispatch.yml.

Then where they land: the SQLite migration of an old DB, the PG writer that
drops keys PG does not have yet (mocked cursor), the Hive dispatcher, the
run_one_v1 LC fix, what submit sends and what readiness holds back, and the
consolidation keeping the columns (old rows null).
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
from pathlib import Path

import polars as pl
import pytest
import yaml

from stan.community.amount import (
    amount_problem,
    derive_amount_source,
    parse_amount_ng,
    resolve_amount,
)

FIXTURES = Path(__file__).parent / "fixtures"
HYSTAR = FIXTURES / "bruker_hystar" / "evosep_one_100spd.m" / "hystar.method"


# ── amount: the unit-anchored parse ──────────────────────────────────────

@pytest.mark.parametrize("name,ng", [
    # The four populations behind Brett's 2026-10-05 hold-back decision.
    ("FL271022_FaimHe1ug_CV4680microDia-w6_120m_3.raw", 1000.0),
    ("FaimHe1ug", 1000.0),
    ("HeL50ug", 50000.0),            # 50 µg: almost surely a typo, see amount_problem
    ("Hel0.45ug", 450.0),
    ("100ng", 100.0),
    ("1ug", 1000.0),
    ("Ex1maiMuncitoresc_HeL50ng-fDia_30m_1.raw", 50.0),
    # Both micro signs: U+00B5 (keyboard) and U+03BC (Greek, what NFKC gives).
    ("HeLa_1µg_DIA.raw", 1000.0),
    ("HeLa_1μg_DIA.raw", 1000.0),
    ("HeLa_2mcg.raw", 2000.0),
    # An optional "_" or "-" between number and unit.
    ("HeLa_50_ng.raw", 50.0),
    ("HeLa_1-ug.raw", 1000.0),
    ("HeLa50NG.raw", 50.0),
    # CamelCase after the unit is common in this lab's names.
    ("FL170621_HeLa100ngDIASpcNwin46_90mHighInts.raw", 100.0),
    ("FL130423_PMclean-HeL50ngDia_35m_2lo.raw", 50.0),
])
def test_amount_parses_with_a_unit(name, ng):
    assert parse_amount_ng(name) == ng


@pytest.mark.parametrize("name", [
    "HeL50",                                   # no unit
    "FL-1MaiMuncitoresc_HeL50_90m.raw",
    "FL20170223_Hela4-cntrl-DIA-mito.raw",     # replicate 4, the 2026-04-30 bug
    "03jun2024_HeLa50_DIA_100spd_S1-B2_1_6205.d",
    "Ex_HeLa_120m_2.raw",
    "HeLa_50ngs.raw",                          # unit followed by a lower-case letter
    "x_2ugli.raw",                             # "ugly", not 2 µg (real UC Davis name)
    "",
    None,
])
def test_numbers_without_a_unit_never_parse(name):
    assert parse_amount_ng(name) is None


def test_the_real_ugli_name_reads_its_ng_token_only():
    assert parse_amount_ng("Ex041123_HeLa50ng-DiaW45_4ian90m_2ugli.raw") == 50.0


# ── amount: precedence and source ────────────────────────────────────────

def test_declared_beats_everything():
    assert resolve_amount("HeLa_1ug.raw", declared=200, instrument_default=50) == (200.0, "declared")


def test_file_name_beats_the_instrument_default():
    assert resolve_amount("HeLa_1ug.raw", instrument_default=50) == (1000.0, "parsed")


def test_instrument_default_is_assumed_not_declared():
    assert resolve_amount("HeL50_30m.raw", instrument_default=200) == (200.0, "assumed")


def test_nothing_known_is_an_assumed_50():
    assert resolve_amount("HeL50_30m.raw") == (50.0, "assumed")
    assert resolve_amount("HeL50_30m.raw", declared=0, instrument_default=None) == (50.0, "assumed")


def test_derived_source_for_rows_stored_before_1_2_16():
    assert derive_amount_source("HeLa_100ng.raw", 100.0) == "parsed"
    assert derive_amount_source("HeL50.raw", 50.0) == "assumed"
    assert derive_amount_source("FaimHe1ug.raw", 50.0) == "assumed"   # a conflict, reported below


# ── amount: what is never sent silently ──────────────────────────────────

@pytest.mark.parametrize("name,amount,needle", [
    ("FL271022_FaimHe1ug_CV4680.raw", 50.0, "file name says 1,000 ng"),   # stored 50, name 1 µg
    ("HeLa_100ng.raw", 50.0, "amount conflict"),
    ("HeL50ug.raw", 50.0, "50,000 ng"),                                   # parse above 5,000
    ("K562100ng.raw", 562100.0, "above 5,000"),
    ("HeL50.raw", 6000.0, "6,000 ng is above 5,000"),                     # stored above 5,000
    ("x_200ng_1ug.raw", 200.0, "more than one amount"),
    ("Hel0.45ug.raw", 50.0, "450 ng"),
])
def test_amount_problems(name, amount, needle):
    problem = amount_problem(name, amount)
    assert problem and needle in problem, problem


@pytest.mark.parametrize("name,amount", [
    ("HeLa_100ng.raw", 100.0), ("HeL50.raw", 50.0), ("FaimHe1ug.raw", 1000.0), ("x", None),
])
def test_consistent_amounts_are_fine(name, amount):
    assert amount_problem(name, amount) is None


# ── FAIMS and the MS2 analyzer, from scan filters ────────────────────────

def test_faims_is_a_cv_in_the_filter():
    from stan.tools.trfp import scan_facts_from_filters

    facts = scan_facts_from_filters([
        "FTMS + p NSI cv=-45.00 Full ms [350.0000-1400.0000]",
        "FTMS + c NSI cv=-45.00 d Full ms2 652.33@hcd30.00 [120.0000-1500.0000]",
        "FTMS + p NSI cv=-65.00 Full ms [350.0000-1400.0000]",
    ])
    assert facts == {"ms2_analyzer": "OT", "faims": True}


def test_no_cv_means_no_faims_and_itms_means_ion_trap():
    from stan.tools.trfp import scan_facts_from_filters

    facts = scan_facts_from_filters([
        "FTMS + p NSI Full ms [375.0000-1500.0000]",
        "ITMS + c NSI r d Full ms2 652.33@cid35.00 [120.0000-1500.0000]",
    ])
    assert facts == {"ms2_analyzer": "IT", "faims": False}


def test_cv_must_be_its_own_token():
    from stan.tools.trfp import scan_facts_from_filters

    # "...cv=" inside another word is not a compensation voltage.
    facts = scan_facts_from_filters(["FTMS + p NSI Full ms [350-1400] abcv=3"])
    assert facts["faims"] is False


def test_unread_filters_are_unknown_not_false():
    from stan.tools.trfp import scan_facts_from_filters

    assert scan_facts_from_filters([]) == {"ms2_analyzer": "unknown", "faims": None}
    assert scan_facts_from_filters([None, ""]) == {"ms2_analyzer": "unknown", "faims": None}


def test_detect_scan_facts_without_fisher_py_or_on_a_d(tmp_path, monkeypatch):
    import builtins

    from stan.tools import trfp

    d = tmp_path / "run.d"
    d.mkdir()
    assert trfp.detect_scan_facts(d) == {"ms2_analyzer": "unknown", "faims": None}

    real_import = builtins.__import__

    def no_fisher(name, *a, **k):
        if name == "fisher_py":
            raise ImportError("not installed (test)")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_fisher)
    raw = tmp_path / "run.raw"
    raw.write_bytes(b"\0" * 16)
    assert trfp.detect_scan_facts(raw) == {"ms2_analyzer": "unknown", "faims": None}
    assert trfp.detect_ms2_analyzer(raw) == "unknown"


def test_detect_scan_facts_reads_fisher_py_once(tmp_path, monkeypatch):
    """One pass answers both; detect_ms2_analyzer stays a thin wrapper."""
    import sys
    import types

    from stan.tools import trfp

    opened: list[str] = []

    class FakeRaw:
        def __init__(self, path):
            opened.append(path)

        def get_scan_from_scan_number(self, n):
            if n > 3:
                raise ValueError("out of range")
            return None, None, None, "FTMS + p NSI cv=-50.00 Full ms [350-1400]"

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, "fisher_py", types.SimpleNamespace(RawFile=FakeRaw))
    raw = tmp_path / "run.raw"
    raw.write_bytes(b"\0")
    assert trfp.detect_scan_facts(raw) == {"ms2_analyzer": "OT", "faims": True}
    assert trfp.detect_ms2_analyzer(raw) == "OT"
    assert len(opened) == 2


# ── LC model: vocabulary and the raw-file sources ────────────────────────

@pytest.mark.parametrize("raw,canonical", [
    ("Dionex UltiMate 3000", "UltiMate 3000"),
    ("Dionex.ChromatographySystem", "UltiMate 3000"),
    ("Thermo Vanquish Neo", "Vanquish Neo"),
    ("Thermo.Vanquish.Neo", "Vanquish Neo"),
    ("Thermo.EasyNLC1200", "EASY-nLC 1200"),
    ("EASY-nLC 1000", "EASY-nLC 1000"),
    ("Thermo Easy-nLC", "EASY-nLC"),
    ("Evosep One", "Evosep One"),
    ("EVOSEP_ONE", "Evosep One"),
    ("Evosep One (Sampler0)", "Evosep One"),
    ("nanoElute 2", "nanoElute 2"),
    ("nanoElute", "nanoElute"),
    ("ACQUITY UPLC M-Class", "ACQUITY UPLC M-Class"),
])
def test_lc_model_vocabulary(raw, canonical):
    from stan.metrics.scoring import normalize_lc_model

    assert normalize_lc_model(raw) == canonical


@pytest.mark.parametrize("raw", ["Agilent ICF System", "WPS-3000", "", None, "Some Pump"])
def test_unrecognised_devices_are_none(raw):
    from stan.metrics.scoring import normalize_lc_model

    assert normalize_lc_model(raw) is None


def _bruker_d(tmp_path: Path, hystar: Path | None = HYSTAR, metadata: str | None = None) -> Path:
    d = tmp_path / "28sep26_HeL50_100spd_S4-E1_1_24688.d"
    m = d / "24688.m"
    m.mkdir(parents=True)
    if hystar is not None:
        shutil.copy(hystar, m / "hystar.method")
    if metadata is not None:
        (d / "HyStarMetadata.xml").write_text(metadata, encoding="utf-8")
    return d


def test_bruker_lc_model_from_a_real_hystar_method(tmp_path):
    """The fixture is the real hystar.method of a 2026-09-28 timsTOF HT run:
    the LC method is XML entity-escaped up to three levels deep, and its
    DeviceName list starts with the control framework, not the LC."""
    from stan.metrics.scoring import _bruker_lc_device_names, detect_lc_model, detect_lc_system

    d = _bruker_d(tmp_path)
    names = _bruker_lc_device_names(d)
    assert "Agilent ICF System" in names and "Evosep One" in names
    assert detect_lc_model(d) == "Evosep One"
    assert detect_lc_system(d) == "evosep"         # unchanged


def test_bruker_lc_model_from_hystar_metadata_alone(tmp_path):
    from stan.metrics.scoring import detect_lc_model

    meta = ('<Metadata><Plugin ID="x" Name="ICF System"><PluginMetadata>'
            '<Module ID="SAMPLER0" Name="Evosep One (Sampler0)" Type="ALS"/>'
            '</PluginMetadata></Plugin></Metadata>')
    assert detect_lc_model(_bruker_d(tmp_path, hystar=None, metadata=meta)) == "Evosep One"


def test_bruker_unknown_lc_is_none_not_a_guess(tmp_path):
    from stan.metrics.scoring import detect_lc_model

    m = tmp_path / "x.d" / "1.m"
    m.mkdir(parents=True)
    (m / "hystar.method").write_text(
        "<root>&lt;DeviceName&gt;Agilent ICF System&lt;/DeviceName&gt;</root>")
    assert detect_lc_model(tmp_path / "x.d") is None


def test_thermo_lc_model_from_driver_ids(tmp_path):
    """The DriverIds a 2026-10-05 Exploris 480 raw carries in its first 50 MB
    (the Lumos's are the same set)."""
    from stan.metrics.scoring import detect_lc_model, detect_lc_system

    raw = tmp_path / "Ex051026_HeL50_30m_1.raw"
    blob = b"\x00\x01".join(
        f'<x DriverId value="{d}" />'.encode()
        for d in ("ChromatographySystem", "Dionex.ChromatographySystem",
                  "Dionex.PumpNCS3500RS", "WPS-3000"))
    raw.write_bytes(b"\x00" * 64 + blob + b"\x00" * 64)
    assert detect_lc_model(raw) == "UltiMate 3000"
    assert detect_lc_system(raw) == "custom"


def test_the_raw_lc_scan_runs_once_per_file(tmp_path, monkeypatch):
    """detect_lc_system and detect_lc_model share one `strings` pass."""
    from stan.metrics.scoring import detect_lc_model, detect_lc_system
    from stan.tools import trfp

    calls: list[Path] = []
    real = trfp._scan_lc_from_raw_binary

    def counting(path):
        calls.append(path)
        return real(path)

    monkeypatch.setattr(trfp, "_scan_lc_from_raw_binary", counting)
    monkeypatch.setattr(trfp, "_LC_BINARY_CACHE", {})
    raw = tmp_path / "x.raw"
    raw.write_bytes(b'DriverId value="Thermo.Vanquish.Neo"')
    assert detect_lc_system(raw) == "custom"
    assert detect_lc_model(raw) == "Vanquish Neo"
    assert len(calls) == 1


def test_lc_model_of_a_missing_path_is_none(tmp_path):
    from stan.metrics.scoring import detect_lc_model

    assert detect_lc_model(tmp_path / "nope.raw") is None


@pytest.mark.parametrize("value,flow", [
    ("nano", "nano"), ("Nanoflow", "nano"), ("capillary", "capillary"),
    ("Capillary flow", "capillary"), ("micro", "micro"), ("microflow", "micro"),
    ("fast", ""), ("", ""), (None, ""), (3, ""),
])
def test_lc_flow_normalisation(value, flow):
    from stan.metrics.scoring import normalize_lc_flow

    assert normalize_lc_flow(value) == flow


# ── SQLite: an old DB gains the columns; rows store 1/0/NULL ─────────────

_OLD_RUNS = """CREATE TABLE runs (
    id TEXT PRIMARY KEY, instrument TEXT NOT NULL, run_name TEXT NOT NULL,
    run_date TEXT NOT NULL, raw_path TEXT, mode TEXT, n_precursors INTEGER,
    gate_result TEXT, failed_gates TEXT, diagnosis TEXT,
    amount_ng REAL DEFAULT 50.0, spd INTEGER, gradient_length_min INTEGER
)"""


def test_sqlite_migration_adds_the_four_columns_to_an_old_db(tmp_path):
    from stan.db import init_db, insert_run

    db = tmp_path / "old.db"
    with sqlite3.connect(db) as con:
        con.execute(_OLD_RUNS)
        con.execute("INSERT INTO runs (id, instrument, run_name, run_date) "
                    "VALUES ('old', 'timsTOF HT', 'HeL50.d', '2026-01-01')")
    init_db(db)
    with sqlite3.connect(db) as con:
        cols = {r[1]: r[2] for r in con.execute("PRAGMA table_info(runs)")}
        old = con.execute("SELECT lc_model, lc_flow, amount_source, faims FROM runs "
                          "WHERE id='old'").fetchone()
    assert cols["lc_model"] == "TEXT" and cols["lc_flow"] == "TEXT"
    assert cols["amount_source"] == "TEXT" and cols["faims"] == "INTEGER"
    assert old == (None, None, None, None)   # nothing backfilled

    rid = insert_run(
        instrument="Orbitrap Exploris 480", run_name="FaimHe1ug.raw", raw_path="/x/FaimHe1ug.raw",
        mode="DIA", amount_ng=1000.0, db_path=db,
        metrics={"lc_model": "UltiMate 3000", "lc_flow": "Nanoflow",
                 "amount_source": "parsed", "faims": True},
    )
    rid2 = insert_run(
        instrument="Orbitrap Exploris 480", run_name="b.raw", raw_path="/x/b.raw",
        mode="DIA", db_path=db, metrics={"lc_flow": "warp", "amount_source": "guessed", "faims": None},
    )
    with sqlite3.connect(db) as con:
        row = con.execute("SELECT lc_model, lc_flow, amount_source, faims FROM runs WHERE id=?",
                          (rid,)).fetchone()
        row2 = con.execute("SELECT lc_model, lc_flow, amount_source, faims FROM runs WHERE id=?",
                           (rid2,)).fetchone()
        n_faims = con.execute("SELECT count(*) FROM runs WHERE faims = 1").fetchone()[0]
    assert row == ("UltiMate 3000", "nano", "parsed", 1)
    assert row2 == (None, None, None, None)
    assert n_faims == 1


def test_fresh_db_has_the_columns_from_the_schema(tmp_path):
    from stan.db import init_db

    db = tmp_path / "new.db"
    init_db(db)
    with sqlite3.connect(db) as con:
        cols = {r[1] for r in con.execute("PRAGMA table_info(runs)")}
    assert {"lc_model", "lc_flow", "amount_source", "faims"} <= cols


@pytest.mark.parametrize("value,stored", [
    (True, 1), (False, 0), (1, 1), (0, 0), (None, None), ("true", 1), ("0", 0), ("maybe", None), (2, None),
])
def test_faims_is_stored_as_an_integer(value, stored):
    from stan.db import _faims_int

    out = _faims_int(value)
    assert out == stored and (out is None or type(out) is int)


# ── PG: keys PG lacks are dropped, logged once (mocked cursor) ───────────

PG_COLUMNS_TODAY = {
    "id", "instrument", "run_name", "run_date", "raw_path", "mode", "stan_version",
    "n_precursors", "n_peptides", "n_proteins", "ms2_analyzer", "lc_system", "amount_ng",
    "spd", "gradient_length_min", "gate_result", "failed_gates", "diagnosis",
    "host_origin", "migrated_at",
}


class _FakeCursor:
    def __init__(self, columns: set[str]):
        self.columns = columns
        self.executed: list[tuple[str, list | None]] = []
        self._result: list = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "information_schema.columns" in sql:
            self._result = [(c,) for c in sorted(self.columns)]

    def fetchall(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self, cur):
        self.cur = cur
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def pg(monkeypatch):
    pytest.importorskip("psycopg2")
    from stan import db_pg

    monkeypatch.setattr(db_pg, "_RUNS_WRITE_COLS", None)
    monkeypatch.setattr(db_pg, "_RUNS_DROPPED_LOGGED", set())

    def install(columns):
        cur = _FakeCursor(columns)
        conn = _FakeConn(cur)
        monkeypatch.setattr(db_pg, "_connect", lambda: conn)
        return cur, conn

    return db_pg, install


def _insert(db_pg, name="FaimHe1ug.raw"):
    return db_pg.insert_run_pg(
        instrument="Orbitrap Fusion Lumos", run_name=name, raw_path=f"/x/{name}",
        mode="DIA", host_origin="lumos", amount_ng=1000.0,
        metrics={"n_precursors": 30000, "lc_model": "UltiMate 3000", "lc_flow": "nano",
                 "amount_source": "parsed", "faims": True},
    )


def _insert_sql(cur) -> tuple[str, list]:
    (sql, params), = [(s, p) for s, p in cur.executed if s.startswith("INSERT INTO runs")]
    return sql, params


def test_pg_insert_drops_keys_pg_does_not_have_and_logs_once(pg, caplog):
    db_pg, install = pg
    cur, conn = install(PG_COLUMNS_TODAY)
    with caplog.at_level(logging.WARNING, logger="stan.db_pg"):
        _insert(db_pg)
        _insert(db_pg, "second.raw")
    inserts = [(s, p) for s, p in cur.executed if s.startswith("INSERT INTO runs")]
    assert len(inserts) == 2 and conn.commits == 2
    for sql, params in inserts:
        for col in ("lc_model", "lc_flow", "amount_source", "faims"):
            assert f'"{col}"' not in sql
        assert '"n_precursors"' in sql and '"host_origin"' in sql
        assert sql.count("%s") == len(params)
    # information_schema read once per process, the drop logged once.
    assert sum("information_schema" in s for s, _ in cur.executed) == 1
    warnings = [r for r in caplog.records if "has no column" in r.getMessage()]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "lc_model" in msg and "faims" in msg and "2026-10-05_runs_lc_faims.sql" in msg
    # Keys PG does not have that are not P3a (e.g. tic_rt_bins on this
    # trimmed column list) are dropped by the same rule.
    assert '"tic_rt_bins"' not in inserts[0][0]


def test_pg_insert_writes_the_columns_once_pg_has_them(pg, caplog):
    db_pg, install = pg
    cur, _ = install(PG_COLUMNS_TODAY | {"lc_model", "lc_flow", "amount_source", "faims"})
    with caplog.at_level(logging.WARNING, logger="stan.db_pg"):
        _insert(db_pg)
    sql, params = _insert_sql(cur)
    cols = [c.strip('"') for c in sql[sql.index("(") + 1: sql.index(")")].split(", ")]
    values = dict(zip(cols, params))
    assert values["lc_model"] == "UltiMate 3000"
    assert values["lc_flow"] == "nano"
    assert values["amount_source"] == "parsed"
    assert values["faims"] == 1 and type(values["faims"]) is int
    assert not [r for r in caplog.records if "lc_model" in r.getMessage()]


def test_pg_insert_does_not_narrow_on_an_empty_column_list(pg):
    """A role that cannot see the table must not have every column dropped."""
    db_pg, install = pg
    cur, _ = install(set())
    _insert(db_pg)
    sql, _ = _insert_sql(cur)
    assert '"lc_model"' in sql and '"n_precursors"' in sql


# ── Hive pipeline: amount + flow plumbing ────────────────────────────────

def test_hive_amount_resolution_and_warning(caplog):
    from stan.pipeline.hive_process import _resolve_run_amount

    assert _resolve_run_amount(Path("/x/HeL50_30m.raw"), None, None) == (50.0, "assumed")
    assert _resolve_run_amount(Path("/x/HeLa_100ng.raw"), None, 50.0) == (100.0, "parsed")
    with caplog.at_level(logging.WARNING, logger="stan.pipeline.hive_process"):
        assert _resolve_run_amount(Path("/x/FaimHe1ug.raw"), 50.0, None) == (50.0, "declared")
    assert any("amount conflict" in r.getMessage() for r in caplog.records)


def test_extract_metrics_stamps_lc_model_faims_and_flow(tmp_path, monkeypatch):
    import stan.metrics.chromatography as chrom
    import stan.metrics.extractor as extractor
    from stan.pipeline import hive_process
    from stan.tools import trfp

    monkeypatch.setattr(extractor, "extract_dia_metrics", lambda *a, **k: {"n_precursors": 1})
    monkeypatch.setattr(chrom, "compute_ips_dia", lambda m: 50)
    monkeypatch.setattr(trfp, "detect_scan_facts", lambda p: {"ms2_analyzer": "OT", "faims": True})
    raw = tmp_path / "Ex.raw"
    raw.write_bytes(b'DriverId value="Dionex.ChromatographySystem"')
    m = hive_process._extract_metrics(
        report_path=tmp_path / "report.parquet", raw_path=raw, vendor="thermo", mode_str="dia",
        family="Exploris", spd=38, gradient_min=30, column_vendor="", column_model="",
        lc_flow="nano",
    )
    assert (m["lc_model"], m["lc_flow"], m["faims"], m["ms2_analyzer"]) == (
        "UltiMate 3000", "nano", True, "OT")

    d = _bruker_d(tmp_path)
    m = hive_process._extract_metrics(
        report_path=tmp_path / "report.parquet", raw_path=d, vendor="bruker", mode_str="dia",
        family="timsTOF", spd=100, gradient_min=11, column_vendor="", column_model="",
    )
    assert (m["lc_model"], m["faims"], m["ms2_analyzer"]) == ("Evosep One", False, "tof")
    assert "lc_flow" not in m


def test_hive_process_amount_flag_defaults_to_none():
    import inspect

    from stan.cli import hive_process_cmd

    params = inspect.signature(hive_process_cmd).parameters
    assert params["amount_ng"].default.default is None
    assert params["default_amount_ng"].default.default is None
    assert params["lc_flow"].default.param_decls == ("--lc-flow",)


def _cfg(tmp_path: Path, **inst_extra) -> Path:
    cfg = {
        "db_path": str(tmp_path / "stan.db"), "out_root": str(tmp_path / "proc"),
        "sbatch_log_dir": str(tmp_path / "logs"), "stan_venv": "/venv",
        "instruments": [{"name": "Orbitrap Exploris 480", "family": "Exploris",
                         "vendor": "thermo", "watch_dir": str(tmp_path), **inst_extra}],
    }
    p = tmp_path / "dispatch.yml"
    p.write_text(yaml.safe_dump(cfg))
    return p


def test_dispatcher_passes_flow_and_the_instrument_amount_as_a_default(tmp_path):
    from stan.community.scripts import dispatch_hive as dh

    cfg = dh._load_config(_cfg(tmp_path, lc_flow="nano", amount_ng=200))
    inst = cfg["instruments"][0]
    script = dh._render_sbatch(tmp_path / "Ex_HeL50.raw", inst, cfg)
    assert "--lc-flow nano" in script
    assert "--default-amount-ng 200.0" in script
    assert "--amount-ng" not in script.replace("--default-amount-ng", "")


def test_raw_dispatch_takes_flow_from_dispatch_yml(tmp_path):
    from stan.community.scripts import dispatch_hive as dh

    cfg = dh._load_config(_cfg(tmp_path, lc_flow="nano"))
    inst = dh._with_config_defaults({"name": "Orbitrap Exploris 480", "family": "Exploris",
                                     "vendor": "thermo"}, cfg)
    assert inst["lc_flow"] == "nano"
    other = dh._with_config_defaults({"name": "timsTOF HT"}, cfg)
    assert "lc_flow" not in other


def test_default_template_sets_uc_davis_flows():
    from stan.community.scripts.dispatch_hive import DEFAULT_CONFIG_TEMPLATE

    flows = {}
    name = None
    for line in DEFAULT_CONFIG_TEMPLATE.splitlines():
        s = line.strip()
        if s.startswith("- name:"):
            name = s.split(":", 1)[1].strip()
        elif s.startswith("lc_flow:") and name:
            flows[name] = s.split(":", 1)[1].split("#")[0].strip().strip('"')
    assert flows == {"timsTOF HT": "", "Orbitrap Fusion Lumos": "nano",
                     "Orbitrap Exploris 480": "nano"}


# ── run_one_v1: LC from the raw file, yml only as the fallback ───────────

def test_run_one_v1_sets_lc_system_from_the_raw_when_the_yml_lacks_it(tmp_path):
    from stan.community.scripts.run_one_v1 import _lc_metadata

    d = _bruker_d(tmp_path)
    assert _lc_metadata(d, {}) == {"lc_system": "evosep", "lc_model": "Evosep One"}


def test_run_one_v1_falls_back_to_the_yml(tmp_path):
    from stan.community.scripts.run_one_v1 import _lc_metadata

    assert _lc_metadata(tmp_path / "gone.raw", {"lc_system": "custom"}) == {"lc_system": "custom"}


def test_run_one_v1_uses_the_shared_amount_parser():
    from stan.community.scripts.run_one_v1 import _resolve_amount_ng_from_name

    assert _resolve_amount_ng_from_name("FL_HeLa_1µg_DIA.raw") == 1000.0
    assert _resolve_amount_ng_from_name("HeLa_50_ng.raw") == 50.0
    assert _resolve_amount_ng_from_name("HeL50.raw") is None


# ── add-watch / setup: lc_flow in instruments.yml ────────────────────────

@pytest.fixture()
def cfg_dir(tmp_path, monkeypatch) -> Path:
    import stan.config as cfg

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    user = home / ".stan"
    monkeypatch.setattr(cfg, "_USER_CONFIG_DIR", user)
    monkeypatch.setattr(cfg, "_LEGACY_SUBMIT_NOTED", False)
    return user


def _cli(*args: str):
    from typer.testing import CliRunner

    from stan.cli import app

    return CliRunner().invoke(app, list(args), input="")


def test_add_watch_writes_lc_flow(cfg_dir, tmp_path):
    watch = tmp_path / "exploris"
    watch.mkdir()
    (watch / "HeLa_QC_001.raw").write_bytes(b"x")
    res = _cli("add-watch", str(watch), "--name", "Orbitrap Exploris 480", "--lc-flow", "Nanoflow", "-y")
    assert res.exit_code == 0, res.output
    (b,) = yaml.safe_load((cfg_dir / "instruments.yml").read_text())["instruments"]
    assert b["lc_flow"] == "nano"


def test_add_watch_sets_lc_flow_on_an_existing_block(cfg_dir, tmp_path):
    watch = tmp_path / "lumos"
    watch.mkdir()
    (watch / "HeLa_QC_001.raw").write_bytes(b"x")
    assert _cli("add-watch", str(watch), "--name", "Orbitrap Fusion Lumos", "-y").exit_code == 0
    res = _cli("add-watch", str(watch), "--lc-flow", "capillary")
    assert res.exit_code == 0, res.output
    (b,) = yaml.safe_load((cfg_dir / "instruments.yml").read_text())["instruments"]
    assert b["lc_flow"] == "capillary" and b["name"] == "Orbitrap Fusion Lumos"


def test_add_watch_rejects_an_unknown_flow(cfg_dir, tmp_path):
    watch = tmp_path / "x"
    watch.mkdir()
    res = _cli("add-watch", str(watch), "--vendor", "thermo", "--lc-flow", "warp", "-y")
    assert res.exit_code == 2
    assert not (cfg_dir / "instruments.yml").exists()


def test_setup_asks_the_flow_and_writes_it(cfg_dir, tmp_path, monkeypatch):
    from stan.setup import run_setup
    from tests.test_setup import _DECLINE_EXTRAS, _script

    watch = tmp_path / "exploris"
    watch.mkdir()
    (watch / "HeLa_QC_001.raw").write_bytes(b"x")
    asked = _script(monkeypatch, {
        "Watch directory": str(watch), "Instrument name": "Orbitrap Exploris 480",
        "LC flow regime": "nano", **_DECLINE_EXTRAS,
    })
    run_setup()
    assert any("LC flow regime" in p for p in asked)
    (b,) = yaml.safe_load((cfg_dir / "instruments.yml").read_text())["instruments"]
    assert b["lc_flow"] == "nano"


def test_setup_flow_can_be_skipped(cfg_dir, tmp_path, monkeypatch):
    from stan.setup import run_setup
    from tests.test_setup import _DECLINE_EXTRAS, _script

    watch = tmp_path / "tims"
    (watch / "HeLa_QC_001.d").mkdir(parents=True)
    _script(monkeypatch, {"Watch directory": str(watch), "Instrument name": "timsTOF HT",
                          **_DECLINE_EXTRAS})
    run_setup()
    (b,) = yaml.safe_load((cfg_dir / "instruments.yml").read_text())["instruments"]
    assert "lc_flow" not in b


# ── submit: the four fields, derived sources, held-back amounts ──────────

READY_DIA = {
    "mode": "DIA", "diann_version": "2.3.2",
    "n_precursors": 40000, "n_peptides": 30000, "n_proteins": 5000,
    "pct_charge_1": 0.05, "missed_cleavage_rate": 0.1,
    "ms1_signal": 1e12, "ms2_signal": 1e11, "fwhm_rt_min": 0.1,
    "median_mass_acc_ms1_ppm": 1.0, "median_mass_acc_ms2_ppm": 2.0,
    "peak_capacity": 300.0, "dynamic_range_log10": 3.5,
    "median_points_across_peak": 9.0, "median_peak_width_sec": 5.0,
    "tic_rt_bins": "[0.1, 0.2]", "tic_intensity": "[1, 2]",
}


@pytest.mark.parametrize("name,amount,why", [
    ("FL271022_FaimHe1ug_CV4680.raw", 50.0, "file name says 1,000 ng"),
    ("HeL50ug_DIA.raw", 50.0, "above 5,000"),
    ("HeLa_100ng.raw", 50.0, "amount conflict"),
    ("HeL50.raw", 6000.0, "above 5,000"),
])
def test_readiness_holds_back_amount_problems(name, amount, why):
    from stan.community.submit import submission_readiness

    state, reason = submission_readiness(dict(READY_DIA, run_name=name, amount_ng=amount))
    assert state == "needs_metrics" and why in reason, reason


@pytest.mark.parametrize("name,amount", [
    ("HeLa_100ng.raw", 100.0), ("HeL50.raw", 50.0), ("HeL50.raw", None),
])
def test_readiness_passes_consistent_amounts(name, amount):
    from stan.community.submit import submission_readiness

    assert submission_readiness(dict(READY_DIA, run_name=name, amount_ng=amount)) == ("ready", "")


def _capture_submit(monkeypatch):
    import io
    import json

    from stan.community import submit

    sent: list[dict] = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(req, timeout=30):
        sent.append(json.loads(req.data))
        return Resp(json.dumps({"status": "accepted", "submission_id": "s1"}).encode())

    monkeypatch.setattr(submit.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(submit, "load_community", lambda: {"display_name": "Lab"})
    monkeypatch.setattr(submit, "mark_submitted", lambda *a, **k: None)
    return submit, sent


def _run(**over):
    run = dict(READY_DIA, id="r1", instrument="Orbitrap Exploris 480",
               run_name="Ex051026_HeL50_30m_1.raw", run_date="2026-10-05T00:00:00Z",
               spd=38, amount_ng=50.0, vendor="thermo")
    run.update(over)
    return run


def test_submit_sends_the_four_fields(monkeypatch):
    submit, sent = _capture_submit(monkeypatch)
    submit.submit_to_benchmark(
        _run(lc_model="UltiMate 3000", lc_flow="nano", amount_source="assumed", faims=0),
        spd=38, gradient_length_min=30, amount_ng=50.0, diann_version="2.3.0")
    (p,) = sent
    assert (p["lc_model"], p["lc_flow"], p["amount_source"], p["faims"]) == (
        "UltiMate 3000", "nano", "assumed", False)


def test_submit_derives_the_source_for_an_old_row(monkeypatch):
    submit, sent = _capture_submit(monkeypatch)
    submit.submit_to_benchmark(_run(run_name="Ex_HeLa_100ng_30m.raw", amount_ng=100.0),
                               spd=38, amount_ng=100.0, diann_version="2.3.0")
    submit.submit_to_benchmark(_run(), spd=38, amount_ng=50.0, diann_version="2.3.0")
    assert [p["amount_source"] for p in sent] == ["parsed", "assumed"]
    assert [p["faims"] for p in sent] == [None, None]
    assert [p["lc_model"] for p in sent] == ["", ""]
    assert [p["lc_flow"] for p in sent] == ["", ""]


def test_submit_refuses_an_amount_conflict(monkeypatch):
    submit, sent = _capture_submit(monkeypatch)
    with pytest.raises(ValueError, match="amount conflict"):
        submit.submit_to_benchmark(_run(run_name="FL_FaimHe1ug_120m.raw"),
                                   spd=38, amount_ng=50.0, diann_version="2.3.0")
    assert sent == []


# ── consolidation keeps the new columns; old rows read null ──────────────

def test_normalize_keeps_the_new_columns_and_old_rows_are_null():
    from stan.community.normalize_v1 import normalize

    base = {
        "schema_version": "v1.0.0", "acquisition_mode": "dia", "cohort_id": "Exploris_30spd_low",
        "instrument_family": "Exploris", "spd": 38, "ips_score": 50, "n_precursors": 30000,
        "run_name": "Ex_HeL50.raw", "run_date": "2026-09-01T00:00:00Z",
        "submitted_at": "2026-09-01T00:00:00Z", "fasta_md5": "a", "speclib_md5": "b",
        "diann_version": "2.3.0", "sample_type": "hela",
    }
    old = pl.DataFrame([dict(base, submission_id="old")])
    new = pl.DataFrame([dict(base, submission_id="new", lc_model="UltiMate 3000",
                             lc_flow="nano", amount_source="parsed", faims=True)])
    merged = pl.concat([old, new], how="diagonal_relaxed")
    out = pl.concat([f for f in normalize(merged).values() if f.height], how="diagonal_relaxed")
    for col in ("lc_model", "lc_flow", "amount_source", "faims"):
        assert col in out.columns
    rows = {r["submission_id"]: r for r in out.to_dicts()}
    assert rows["old"]["lc_model"] is None and rows["old"]["faims"] is None
    assert rows["new"]["lc_model"] == "UltiMate 3000" and rows["new"]["faims"] is True
