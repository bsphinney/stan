"""PEG trend maths behind the dashboard's PEG tab (stan.metrics.peg_trends).

Two properties matter more than any single number here:

* an ``'unknown'`` row -- the pipeline's failure sentinel, stored with
  peg_score 0.0, which is exactly a clean run's score -- never counts, and an
  unmeasured run is dropped rather than read as 0;
* one acquisition is one run: the same raw file sits in PG up to five times
  with PEG readings that disagree, and every number here must count it once;
* the episode detector reproduces the UC Davis timsTOF PEG episodes, on a
  synthetic series shaped like them and, when the name-free real extract is
  in tests/fixtures, on the real data through the real readers.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from stan.metrics import peg_trends as pt
from stan.metrics.peg_trends import PegRun

UTC = timezone.utc

# UC Davis's timsTOF HT real-PEG QC rows, read-only from PG Farm 2026-09-28,
# with the run names replaced by an acquisition number and the ids by their
# rank. Duplicate ingests are kept on purpose: they are what it tests.
EXTRACT = Path(__file__).parent / "fixtures" / "peg_timstof_extract.json"

# `stan submit-all`'s blank/wash skip, verbatim from stan/cli.py.
SUBMIT_ALL_BLANK_RE = r"(?i)(wash|blank|blnk|blk|DELETE)"

NAMES = [
    "03Feb25_Hela50-Dia_60spd_S1-A7_1_11184.d",
    "Blank_after_HeLa_1_2001.d",
    "BLANK.d",
    "hela_wash_02.raw",
    "col_WASH.d",
    "blnk_injection.d",
    "HeLa_BLK_3.d",
    "HeLa_DELETE_me.d",
    "HeLa_delete_me.d",
    "QC_HeLa_200ng.raw",
    "blanket_test.d",   # contains "blank": submit-all skips it, so must we
    "",
]


def run(day: date | str, pct: float, cls: str = "clean", **kw) -> PegRun:
    """A PegRun at noon UTC on ``day``."""
    d = date.fromisoformat(day) if isinstance(day, str) else day
    return PegRun(t=datetime(d.year, d.month, d.day, 12, tzinfo=UTC), pct=pct, cls=cls, **kw)


def daily_runs(first: str, last: str, pct: float, cls: str = "clean", **kw) -> list[PegRun]:
    d0, d1 = date.fromisoformat(first), date.fromisoformat(last)
    return [run(d0 + timedelta(days=i), pct, cls, **kw) for i in range((d1 - d0).days + 1)]


# ── Filters ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", NAMES)
def test_blank_filter_matches_submit_all(name):
    """Same answer as submit-all for every name: one definition of 'QC'."""
    assert pt.is_blank_or_wash(name) == bool(re.search(SUBMIT_ALL_BLANK_RE, name))


@pytest.mark.parametrize("name", NAMES)
def test_pg_regex_is_the_same_alternation(name):
    """The SQL-side filter (PG ``!~*``) must agree with the Python one.

    Aggregating readers filter in SQL and never see a name in Python; if the
    two patterns drifted, a blank could be in the ladder but not the runs.
    """
    assert bool(re.search(pt.PG_BLANK_WASH_REGEX, name, re.IGNORECASE)) == \
        pt.is_blank_or_wash(name)


def test_blank_filter_handles_none():
    assert pt.is_blank_or_wash(None) is False


def test_unknown_sentinel_is_not_real():
    """score 0.0 + 'unknown' is a failed read, not a spotless run."""
    assert pt.is_real_peg(0.0, 0.0, "unknown") is False


@pytest.mark.parametrize("score,pct,cls,ok", [
    (0.0, 0.0, "clean", True),
    (50.0, 2.0, "moderate", True),
    (None, 0.0, "clean", False),
    (0.0, None, "clean", False),
    (0.0, 0.0, None, False),
    (0.0, 0.0, "", False),
    (0.0, 0.0, "CLEAN", False),
    # PG float4 can hold these; one reaching json.dumps is a 500.
    (float("nan"), 1.0, "trace", False),
    (10.0, float("nan"), "clean", False),
    (10.0, float("inf"), "heavy", False),
    ("7.5", "0.353", "clean", True),
])
def test_is_real_peg(score, pct, cls, ok):
    assert pt.is_real_peg(score, pct, cls) is ok


def test_non_finite_aggregates_become_null():
    row = pt.finalize_lab_lc_row("x", {"evosep": 1}, 1, float("nan"), 0, 1, float("inf"),
                                 {})
    assert row["median_90d"] is None and row["median_365d"] is None
    json.dumps(row, allow_nan=False)


def test_to_peg_runs_drops_unknown_null_and_bogus_dates():
    rows = [
        {"run_date_utc": "2026-09-01T10:00:00Z", "peg_score": 0.0,
         "peg_intensity_pct": 0.0, "peg_class": "unknown"},
        {"run_date_utc": "2026-09-02T10:00:00Z", "peg_score": None,
         "peg_intensity_pct": None, "peg_class": None},
        {"run_date_utc": "1980-01-02T00:00:00Z", "peg_score": 5.0,
         "peg_intensity_pct": 0.2, "peg_class": "clean"},
        {"run_date_utc": "2026-09-04T10:00:00Z", "peg_score": 80.0,
         "peg_intensity_pct": 9.0, "peg_class": "heavy", "spd": 60},
        {"run_date_utc": "2026-09-03T10:00:00Z", "peg_score": 0.0,
         "peg_intensity_pct": 0.0, "peg_class": "clean", "spd": "100"},
    ]
    out = pt.to_peg_runs(rows)
    assert [r.cls for r in out] == ["clean", "heavy"]   # sorted by time
    assert out[0].spd == 100


@pytest.mark.parametrize("prec,ions,pct,failed", [
    (0, 0, 0.0, True),          # summed no MS1: reads 0 % / 'clean', measured nothing
    ("0", "0", "0", True),
    (0, 6, 0.0, False),         # found PEG ions: it saw spectra
    (0, 0, 0.4, False),
    (None, 0, 0.0, False),      # no precursor count (DDA, unsearched) is not 0
    (31000, 0, 0.0, False),     # a genuinely clean run identifies precursors
])
def test_is_failed_acquisition(prec, ions, pct, failed):
    assert pt.is_failed_acquisition(prec, ions, pct) is failed


def test_to_peg_runs_drops_failed_acquisitions_and_carries_has_hits():
    base = {"peg_score": 0.0, "peg_intensity_pct": 0.0, "peg_class": "clean"}
    rows = [
        dict(base, run_date_utc="2026-09-01T10:00:00Z", n_precursors=0,
             peg_n_ions_detected=0),
        dict(base, run_date_utc="2026-09-02T10:00:00Z", n_precursors=30000,
             peg_n_ions_detected=0, has_hits=0),
        dict(base, run_date_utc="2026-09-03T10:00:00Z", peg_n_ions_detected=4,
             has_hits=1),
    ]
    out = pt.to_peg_runs(rows)
    assert [r.day.day for r in out] == [2, 3]
    assert [r.has_hits for r in out] == [False, True]
    # ions 0 is a known ladder (nothing seen); ions without stored hits is not
    assert out[0].ladder_known and out[1].ladder_known
    assert not run("2026-09-04", 1.0, ions=4, has_hits=False).ladder_known
    assert not run("2026-09-04", 1.0, ions=None).ladder_known


# ── One row per acquisition ─────────────────────────────────────────

@pytest.mark.parametrize("raw,key", [
    ("1.0.10", (1, 0, 10)), ("v1.2", (1, 2)), ("0.2.376", (0, 2, 376)),
    ("1.0.44rc2", (1, 0, 44)), (" 1.1.12 ", (1, 1, 12)),
    (None, ()), ("", ()), ("unknown", ()),
])
def test_version_key(raw, key):
    assert pt.version_key(raw) == key


def test_version_key_is_numeric_not_text():
    assert pt.version_key("1.0.10") > pt.version_key("1.0.9")
    assert pt.version_key("1.0.44") > pt.version_key("0.2.376")   # text order is backwards
    assert pt.version_key("1.0.1") > pt.version_key("1.0")
    assert pt.version_key("0.0.1") > pt.version_key(None)


def _copy(rid, pct, *, hits=False, ver="0.2.376", name="HeLa_1.d", when="2026-02-01T10:00:00Z",
          inst="timsTOF HT"):
    return {"id": rid, "instrument": inst, "run_name": name, "run_date_utc": when,
            "stan_version": ver, "has_hits": hits, "peg_intensity_pct": pct}


def _kept(rows):
    return sorted(r["id"] for r in pt.pick_canonical(rows))


def test_pick_canonical_rank_and_order_independence():
    """Hits first, then the newest version (numerically), then the highest id.

    Live PG: an instrument-PC 0.2.222 copy without hits beside the Hive
    0.2.376 copy with them, reading 5.24 % against 1.89 % -- the choice
    decides the number, so it can never depend on row order.
    """
    groups = [
        [_copy("a", 5.2, ver="1.1.12"), _copy("b", 1.9, hits=True, ver="0.2.222")],
        [_copy("c", 1.0, ver="1.0.9", name="x.d"), _copy("d", 2.0, ver="1.0.10", name="x.d")],
        [_copy("e", 1.0, name="y.d"), _copy("f", 3.0, name="y.d")],
        [_copy("g", 0.1, ver=None, name="z.d"), _copy("h", 0.2, ver="unknown", name="z.d"),
         _copy("i", 0.3, ver="0.0.1", name="z.d")],
    ]
    rows = [r for g in groups for r in g]
    assert _kept(rows) == ["b", "d", "f", "i"]
    assert _kept(list(reversed(rows))) == ["b", "d", "f", "i"]
    assert _kept(rows[1::2] + rows[0::2]) == ["b", "d", "f", "i"]


def test_pick_canonical_keeps_distinct_acquisitions():
    rows = [
        _copy("a", 1.0),
        _copy("b", 1.0, when="2026-02-01T10:00:01Z"),        # a second later
        _copy("c", 1.0, name="HeLa_2.d"),                     # another file
        _copy("d", 1.0, inst="Orbitrap Exploris 480"),        # another instrument
    ]
    assert _kept(rows) == ["a", "b", "c", "d"]
    # returned in input order
    assert [r["id"] for r in pt.pick_canonical(rows)] == ["a", "b", "c", "d"]


def test_acquisition_key_is_what_run_key_hashes():
    """One raw file under two directories is one acquisition.

    The share client's run_key hashes (instrument, basename(run_name), UTC
    second). Keyed on the full name, the reader kept an instrument-PC path
    and a Hive path as two runs while the client sent one of them, so the
    tab and the board counted the same file differently.
    """
    rows = [
        _copy("a", 7.5, ver="1.1.12", name=r"D:\Data\QC\HeLa_1.d"),
        _copy("b", 0.4, hits=True, name="/quobyte/proteomics-grp/STAN/raw/HeLa_1.d/"),
        _copy("c", 1.0, name=" HeLa_1.d", inst="timsTOF HT "),
    ]
    assert _kept(rows) == ["b"]
    assert len({pt.acquisition_key(r) for r in rows}) == 1
    # the extension still separates a .d from a .raw of the same stem
    assert _kept([_copy("d", 1.0, name="x.d"), _copy("r", 1.0, name="x.raw")]) == ["d", "r"]


def test_canonical_rank_is_the_one_pick_canonical_uses():
    rows = [_copy("z", 5.2, ver="1.1.12"), _copy("a", 1.9, hits=True, ver="0.2.222")]
    best = max(rows, key=pt.canonical_rank)
    assert [best["id"]] == _kept(rows) == ["a"]


# ── Time handling ───────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expect", [
    ("2025-02-03T11:47:26-08:00", "2025-02-03T19:47:26Z"),
    ("2025-02-03T19:47:26+00:00", "2025-02-03T19:47:26Z"),
    ("2026-09-01 22:47:38+00:00", "2026-09-01T22:47:38Z"),
    ("2025-11-24T12:00:00Z", "2025-11-24T12:00:00Z"),
    ("2026-09-21T20:13:45.944-07:00", "2026-09-22T03:13:45Z"),
    ("2026-09-21T20:13:45.1234567+00:00", "2026-09-21T20:13:45Z"),
    ("2024-01-19T00:53:00", "2024-01-19T00:53:00Z"),     # naive -> UTC
    ("2025-12-23", "2025-12-23T00:00:00Z"),
])
def test_parse_utc_normalises_every_stored_shape(raw, expect):
    """Mixed offsets for the same instant must land on one UTC timestamp."""
    assert pt.utc_iso(pt.parse_utc(raw)) == expect


def test_parse_utc_objects_and_garbage():
    aware = datetime(2026, 1, 1, 23, 30, tzinfo=timezone(timedelta(hours=-8)))
    assert pt.utc_iso(pt.parse_utc(aware)) == "2026-01-02T07:30:00Z"
    assert pt.utc_iso(pt.parse_utc(datetime(2026, 1, 1, 5))) == "2026-01-01T05:00:00Z"
    assert pt.utc_iso(pt.parse_utc(date(2026, 1, 1))) == "2026-01-01T00:00:00Z"
    assert pt.parse_utc(None) is None
    assert pt.parse_utc("") is None
    assert pt.parse_utc("not a date") is None


def test_week_window_trails_the_as_of_day():
    """The relay's lc-compare buckets: 26 x 7 days ending at the next midnight."""
    start, end = pt.week_window(date(2026, 9, 28), 26)       # a Monday
    assert end == datetime(2026, 9, 29, tzinfo=UTC)
    assert start == end - timedelta(days=182) == datetime(2026, 3, 31, tzinfo=UTC)
    assert pt.week_index(start, start) == 0
    assert pt.week_index(start - timedelta(seconds=1), start) is None
    assert pt.week_index(datetime(2026, 9, 28, 23, 59, tzinfo=UTC), start) == 25
    assert pt.week_index(datetime(2026, 9, 22, tzinfo=UTC), start) == 25
    assert pt.week_index(datetime(2026, 9, 21, 23, 59, tzinfo=UTC), start) == 24
    assert pt.week_index(end, start) is None


# ── Rolling median ──────────────────────────────────────────────────

def test_rolling_median_window_and_min_runs():
    days = [date(2026, 1, 1) + timedelta(days=i) for i in range(6)]
    vals = [1.0, 2.0, 3.0, 4.0, 5.0, 100.0]
    s = pt.rolling_median_daily(days, vals, date(2026, 1, 1), date(2026, 1, 20))
    assert s[:4] == [None] * 4          # fewer than 5 runs so far
    assert s[4] == 3.0                  # median of 1..5
    assert s[5] == 3.5                  # median of 1..5,100
    # Day 1-15 is the first whose window (d-14, d] excludes Jan 1.
    assert s[14] == 4.0                 # 2,3,4,5,100
    assert s[15] is None                # only 4 runs left in the window


def test_rolling_median_empty_and_none_values():
    assert pt.rolling_median_daily([], [], date(2026, 1, 1), date(2026, 1, 3)) == [None] * 3
    days = [date(2026, 1, 1)] * 6
    s = pt.rolling_median_daily(days, [1, 1, 1, 1, None, None], date(2026, 1, 1),
                                date(2026, 1, 1))
    assert s == [None]                  # the NULLs are dropped, not read as 0


def test_rolling_median_accepts_datetimes():
    ts = [datetime(2026, 1, 1, h, tzinfo=UTC) for h in range(5)]
    assert pt.rolling_median_daily(ts, [0, 1, 2, 3, 4], date(2026, 1, 1),
                                   date(2026, 1, 1)) == [2]


# ── Episodes ────────────────────────────────────────────────────────

def _episodes(runs: list[PegRun], as_of: str, **kw) -> list[dict]:
    start = min(r.day for r in runs)
    daily = pt.rolling_median_daily([r.t for r in runs], [r.pct for r in runs],
                                    start, date.fromisoformat(as_of))
    return pt.detect_episodes(daily, start, runs, **kw)


def uc_davis_shaped_series() -> list[PegRun]:
    """One QC a day, shaped like the timsTOF HT's 2025-26 PEG history.

    Clean at 0.1 %, hot at 6 % from 2025-12-15, a 12-day clean spell in
    February (which must not split the episode: it is shorter than the 21-day
    merge gap), clean again from 2026-04-20 and hot 2026-05-21 .. 09-11.
    A 14-day trailing median of one run a day stays at or above 3 % until
    more than half its window is clean -- with 7 of 14 clean it is the mean
    of the middle pair, (0.1 + 6) / 2 = 3.05 -- so it lags the last hot run
    by 7 days, and the hot runs stop 7 days before each episode ends.
    """
    return (
        daily_runs("2025-10-01", "2025-12-14", 0.1)
        + daily_runs("2025-12-15", "2026-01-31", 6.0, "heavy")
        + daily_runs("2026-02-01", "2026-02-12", 0.1)
        + daily_runs("2026-02-13", "2026-04-19", 6.0, "heavy")
        + daily_runs("2026-04-20", "2026-05-20", 0.1)
        + daily_runs("2026-05-21", "2026-09-11", 6.0, "moderate")
        + daily_runs("2026-09-12", "2026-09-28", 0.1)
    )


def test_synthetic_reproduction_of_the_uc_davis_episodes():
    eps = _episodes(uc_davis_shaped_series(), "2026-09-28")
    assert [(e["start"], e["end"], e["days"], e["ongoing"]) for e in eps] == [
        ("2025-12-15", "2026-04-26", 132, False),
        ("2026-05-21", "2026-09-18", 120, False),
    ]
    first = eps[0]
    assert first["n"] == 133                         # one run a day, inclusive
    # 12 clean in February + 7 clean trailing days, out of 133: still hot
    assert first["median_pct"] == 6.0
    # heavy share counts the heavy class, not the threshold
    assert eps[1]["heavy_pct"] == 0


def test_episode_start_is_pulled_back_to_the_first_hot_run():
    """The trailing median notices a week late; the episode must not."""
    runs = daily_runs("2026-01-01", "2026-01-31", 0.0) + \
        daily_runs("2026-02-01", "2026-03-15", 5.0, "heavy")
    eps = _episodes(runs, "2026-03-20")
    assert eps[0]["start"] == "2026-02-01"


def test_a_gap_longer_than_21_days_splits_episodes():
    runs = (daily_runs("2026-01-01", "2026-01-10", 0.0)
            + daily_runs("2026-01-11", "2026-02-20", 5.0, "heavy")
            + daily_runs("2026-02-21", "2026-04-10", 0.0)
            + daily_runs("2026-04-11", "2026-05-20", 5.0, "heavy")
            + daily_runs("2026-05-21", "2026-06-30", 0.0))
    eps = _episodes(runs, "2026-06-30")
    assert [e["start"] for e in eps] == ["2026-01-11", "2026-04-11"]


def _hot_spans(first_start: date, gap: int) -> tuple[list, list[PegRun], date]:
    """Two 20-day hot spans whose hot days are ``gap`` days apart."""
    d0 = first_start
    a = [d0 + timedelta(days=i) for i in range(21)]
    b = [a[-1] + timedelta(days=gap + i) for i in range(21)]
    hot = set(a + b)
    end = b[-1] + timedelta(days=40)
    days = [d0 + timedelta(days=i) for i in range((end - d0).days + 1)]
    daily = [5.0 if d in hot else 0.0 for d in days]
    runs = [run(d, 5.0 if d in hot else 0.0, "heavy" if d in hot else "clean") for d in days]
    return daily, runs, d0


@pytest.mark.parametrize("gap,n_episodes", [(20, 1), (21, 2), (22, 2)])
def test_hot_days_merge_only_when_closer_than_the_gap(gap, n_episodes):
    """Spec 4.2 and the docstring: hot days *closer than* gap_days merge."""
    daily, runs, d0 = _hot_spans(date(2026, 1, 1), gap)
    eps = pt.detect_episodes(daily, d0, runs, gap_days=21)
    assert len(eps) == n_episodes


def test_short_bursts_are_dropped():
    """Five hot injections in a row are a blip, not an episode."""
    runs = daily_runs("2026-03-01", "2026-03-05", 9.0, "heavy")
    assert _episodes(runs, "2026-04-01") == []
    # ... and with min_days lowered the same burst is reported
    assert len(_episodes(runs, "2026-04-01", min_days=5)) == 1


def test_ongoing_flag():
    runs = daily_runs("2026-01-01", "2026-01-31", 0.0) + \
        daily_runs("2026-02-01", "2026-03-15", 5.0, "heavy")
    # No runs after 03-15, so the window drains rather than cleans: the median
    # stays hot while it still holds 5 runs (03-11..03-15), i.e. through 03-24.
    assert _episodes(runs, "2026-03-15")[0]["ongoing"] is True
    assert _episodes(runs, "2026-03-27")[0]["ongoing"] is True
    assert _episodes(runs, "2026-03-28")[0]["ongoing"] is False


def test_episodes_empty_and_single_run():
    assert pt.detect_episodes([], date(2026, 1, 1), []) == []
    assert _episodes([run("2026-01-01", 50.0, "heavy")], "2026-01-10") == []


def _load_extract(tmp_path: Path) -> tuple[Path, dict]:
    """The real extract as a SQLite store, duplicates and all."""
    import stan.db as stan_db

    fx = json.loads(EXTRACT.read_text())
    path = tmp_path / "stan.db"
    stan_db.init_db(path)
    with sqlite3.connect(path) as con:
        for row in fx["runs"]:
            x = dict(zip(fx["runs_cols"], row))
            con.execute(
                "INSERT INTO runs (id, instrument, run_name, run_date, raw_path, mode, spd, "
                "peg_score, peg_intensity_pct, peg_n_ions_detected, peg_class, n_precursors, "
                "lc_system, stan_version, hidden) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)",
                (x["id"], fx["instrument"], f"acq{x['acq']:05d}.d", x["t"], f"/p/{x['id']}",
                 x["mode"], x["spd"], x["score"], x["pct"], x["ions"], x["cls"], x["prec"],
                 fx["lc_system"], x["ver"]))
            for n, adduct in x["hits"]:
                con.execute(
                    "INSERT INTO peg_ion_hits (run_id, source, mz, observed_intensity, "
                    "adduct, repeat_n, charge, ppm_error) VALUES (?, 'runs', ?, 1, ?, ?, 1, 0)",
                    (x["id"], 100.0 + n, adduct, n))
    return path, fx


@pytest.mark.skipif(not EXTRACT.exists(), reason="tests/fixtures/peg_timstof_extract.json absent")
def test_real_uc_davis_extract_through_the_readers(tmp_path, monkeypatch):
    """The real timsTOF HT history, end to end through the SQLite readers.

    1,674 rows are 1,404 acquisitions. Counted once each (and without the
    failed acquisitions), the first episode ends 2026-04-07 after 113 days,
    not 2026-04-26 after 132, and the Feb-Apr ladder no longer reads cleaner
    than the months around it.
    """
    import stan.db as stan_db

    monkeypatch.delenv("STAN_DB_BACKEND", raising=False)
    path, fx = _load_extract(tmp_path)
    inst, as_of = fx["instrument"], date.fromisoformat(fx["as_of"])
    assert len(fx["runs"]) == 1674
    assert len({(r[1], r[2]) for r in fx["runs"]}) == 1404      # (acq, t)

    runs = stan_db.get_peg_runs(inst, db_path=path)
    assert len(runs) == 1385            # 1,404 acquisitions less 19 failed ones
    doc = pt.build_overview(
        as_of=as_of, instrument=inst, instruments=[], runs=runs,
        ladder_rows=stan_db.get_peg_ladder_month_counts(inst, db_path=path),
        column_events=[tuple(e) for e in fx["column_changes"]],
        family_fn=lambda m: "timsTOF")
    assert doc["episodes"] == [
        {"start": "2025-12-15", "end": "2026-04-07", "days": 113, "n": 169,
         "median_pct": 5.3, "heavy_pct": 41, "ongoing": False},
        {"start": "2026-05-21", "end": "2026-09-18", "days": 120, "n": 236,
         "median_pct": 5.31, "heavy_pct": 42, "ongoing": False},
    ]
    s = doc["summary"]
    assert (s["n_30d"], s["change_pct"], s["clean_30d"], s["heavy_30d"],
            s["clean_rate_30d"], s["streak_clean"]) == (71, -73, 19, 13, 27, 0)
    periods = {p["installed"]: (p["n_qc"], p["median_pct"]) for p in doc["column_periods"]}
    assert periods["2025-12-23"] == (106, 5.65) and periods["2026-03-12"] == (166, 1.64)
    assert doc["impact"]["60"]["heavy"] == [112, 34730]
    assert doc["impact"]["100"]["clean"] == [378, 36688]
    lad = doc["ladder"]
    j = lad["n"].index(10)
    share10 = {m: lad["share"][j][i] for i, m in enumerate(lad["months"])}
    # Known-ladder denominator: the episode months are dirty, not clean.
    assert [share10[m] for m in ("2026-01", "2026-02", "2026-03", "2026-04", "2026-05")] == \
        [0.887, 0.829, 0.831, 0.651, 0.747]
    assert all(0 <= v <= 1 for row in lad["share"] for v in row)

    share = stan_db.get_peg_share_rows(db_path=path)
    assert len(share) == len(runs)
    assert len({(r["run_name"], r["run_date"]) for r in share}) == len(share)
    assert stan_db.get_peg_instruments(db_path=path) == [
        {"instrument": inst, "n_runs": 1385, "evosep": True}]


# ── Baseline ────────────────────────────────────────────────────────

def test_baseline_needs_twenty_runs():
    assert pt.best_baseline(daily_runs("2026-01-01", "2026-01-19", 0.0)) is None
    assert pt.best_baseline([]) is None


def test_baseline_finds_the_cleanest_window():
    runs = (daily_runs("2025-01-01", "2025-03-31", 2.0)
            + daily_runs("2025-04-01", "2025-06-29", 0.2)
            + daily_runs("2025-06-30", "2025-12-31", 4.0))
    b = pt.best_baseline(runs, as_of=date(2025, 12, 31))
    # Every window whose median AND upper quartile are 0.2 ties; the newest
    # wins. That is the one ending 22 days into the 4 % spell (68 of its 90
    # runs clean), so the quoted level is the clean stretch's, not a blend.
    assert b == {"median_pct": 0.2, "start": "2025-04-23", "end": "2025-07-21", "n": 90}


def test_baseline_ties_go_to_the_cleaner_upper_quartile():
    """A median of 0 with 49 % heavy runs is not a baseline.

    Both windows below have a median of exactly 0. The dense one has more
    runs, but nearly half of them are heavily contaminated; the upper
    quartile tells them apart where the median and the run count cannot.
    """
    clean = daily_runs("2025-01-01", "2025-03-31", 0.0)[::3]            # 30 runs, all 0
    half_dirty = []
    for r in daily_runs("2025-06-01", "2025-08-29", 0.0):               # 90 days
        half_dirty.append(r)
        if r.day.day % 2:
            half_dirty.append(run(r.day, 9.0, "heavy"))
    b = pt.best_baseline(clean + half_dirty, as_of=date(2025, 12, 31))
    assert b["end"] <= "2025-05-31" and b["n"] >= 20


def test_baseline_ties_then_go_to_the_best_attested_window():
    """Two equally clean stretches: prefer more runs, then the newer one."""
    sparse = daily_runs("2025-01-01", "2025-03-31", 0.0)[::3]      # 30 runs
    dense = daily_runs("2025-06-01", "2025-08-29", 0.0)             # 90 runs
    b = pt.best_baseline(sparse + dense, as_of=date(2025, 12, 31))
    assert b["n"] == 90 and b["end"] == "2025-08-29"


# ── Summary ─────────────────────────────────────────────────────────

def test_summary_windows_and_change():
    as_of = date(2026, 9, 28)
    runs = (daily_runs("2026-07-31", "2026-08-29", 8.0, "heavy")   # previous 30 d
            + daily_runs("2026-08-30", "2026-09-26", 2.0, "trace")  # last 30 d ...
            + [run("2026-09-27", 0.0), run("2026-09-28", 0.0)])     # ... ending clean
    s = pt.summary_30d(runs, as_of)
    assert s["n_30d"] == 30
    assert s["median_30d"] == 2.0 and s["median_prev_30d"] == 8.0
    assert s["change_pct"] == -75
    assert (s["clean_30d"], s["heavy_30d"], s["clean_rate_30d"]) == (2, 0, 7)
    assert s["streak_clean"] == 2


def test_summary_empty_and_no_previous_window():
    s = pt.summary_30d([], date(2026, 9, 28))
    assert s == {"n_30d": 0, "median_30d": None, "median_prev_30d": None,
                 "change_pct": None, "clean_30d": 0, "heavy_30d": 0,
                 "clean_rate_30d": None, "streak_clean": 0}
    one = pt.summary_30d([run("2026-09-20", 1.0, "trace")], date(2026, 9, 28))
    assert one["n_30d"] == 1 and one["change_pct"] is None and one["streak_clean"] == 0
    # A change from 0 % has no percentage.
    zero_prev = [run("2026-08-10", 0.0), run("2026-09-20", 1.0, "trace")]
    assert pt.summary_30d(zero_prev, date(2026, 9, 28))["change_pct"] is None


def test_change_pct_has_the_relays_floor():
    """Below 0.1 % the previous median is noise; the relay answers None there.

    0.05 % -> 0.5 % read as "+900 %" on the lab's own tab while the board,
    for the same runs, showed no change at all.
    """
    as_of = date(2026, 9, 28)
    tiny = pt.summary_30d([run("2026-08-10", 0.05), run("2026-09-20", 0.5, "trace")], as_of)
    assert tiny["median_prev_30d"] == 0.05 and tiny["change_pct"] is None
    at_floor = pt.summary_30d([run("2026-08-10", 0.1), run("2026-09-20", 0.15)], as_of)
    assert at_floor["change_pct"] == 50


def test_change_floor_is_the_relays_constant():
    """The relay is deployed on its own and cannot import this; pin the value."""
    src = (Path(__file__).resolve().parents[1] / "hf_space" / "app.py").read_text()
    m = re.search(r"^PEG_CHANGE_FLOOR_PCT\s*=\s*([0-9.]+)", src, re.M)
    assert m, "PEG_CHANGE_FLOOR_PCT not found in hf_space/app.py"
    assert float(m.group(1)) == pt.PEG_CHANGE_FLOOR_PCT


# ── Ladder ──────────────────────────────────────────────────────────

def test_ladder_share_is_max_over_adducts_per_month():
    hits = [
        ("2026-08", 9, "+NH4", 6), ("2026-08", 9, "+H", 2),
        ("2026-09", 9, "+H", 1), ("2026-09", 12, "+Na", 4),
        ("2024-12", 9, "+NH4", 3),                  # before Jan of last year
    ]
    lad = pt.ladder_by_month(hits, {"2026-08": 10, "2026-09": 8, "2024-12": 3,
                                    "2025-05": 0}, date(2026, 9, 28))
    assert lad["months"] == ["2026-08", "2026-09"]      # 2025-05 had no runs
    assert lad["n"] == list(range(2, 21))
    assert lad["nruns"] == [10, 8]
    j9, j12 = lad["n"].index(9), lad["n"].index(12)
    assert lad["share"][j9] == [0.6, 0.125]
    assert lad["share"][j12] == [0.0, 0.5]
    assert all(len(row) == 2 for row in lad["share"])
    # adducts: every detection, across all rows given
    assert lad["adducts"] == {"+H": 3, "+NH4": 9, "+Na": 4}


def test_ladder_empty():
    lad = pt.ladder_by_month([], {}, date(2026, 9, 28))
    assert lad["months"] == [] and lad["nruns"] == [] and lad["adducts"] == {}
    assert lad["share"] == [[] for _ in range(2, 21)]


# ── Column periods ──────────────────────────────────────────────────

def test_column_periods():
    runs = (daily_runs("2026-01-01", "2026-01-31", 0.1)
            + daily_runs("2026-02-01", "2026-02-28", 6.0, "heavy")
            + daily_runs("2026-03-01", "2026-03-10", 1.0, "trace"))
    events = [
        ("2026-02-01T12:00:00Z", None),
        ("2026-01-10", "PepSep 15cm"),
        ("2026-02-01", "PepSep 10cm"),       # same day: one install
        ("2026-03-01T08:00:00-08:00", None),
        ("2026-12-01", "future"),            # after as_of: ignored
    ]
    p = pt.column_periods(events, runs, date(2026, 3, 10))
    assert [(x["installed"], x["retired"]) for x in p] == [
        ("2026-01-10", "2026-02-01"), ("2026-02-01", "2026-03-01"), ("2026-03-01", None)]
    assert p[0] == {"installed": "2026-01-10", "retired": "2026-02-01",
                    "column_model": "PepSep 15cm", "n_qc": 22, "median_pct": 0.1,
                    "heavy_pct": 0, "clean_pct": 100}
    assert p[1]["column_model"] == "PepSep 10cm"
    assert (p[1]["n_qc"], p[1]["median_pct"], p[1]["heavy_pct"]) == (28, 6.0, 100)
    assert p[2]["n_qc"] == 10


def test_column_period_without_runs_is_kept():
    """A column fitted today must show as current, with nothing measured."""
    p = pt.column_periods([("2026-01-01", None), ("2026-03-10", "new")],
                          daily_runs("2026-01-01", "2026-03-09", 1.0, "trace"),
                          date(2026, 3, 10))
    assert p[-1] == {"installed": "2026-03-10", "retired": None, "column_model": "new",
                     "n_qc": 0, "median_pct": None, "heavy_pct": None, "clean_pct": None}


def test_column_periods_none_logged():
    assert pt.column_periods([], daily_runs("2026-01-01", "2026-01-05", 1.0)) == []


# ── Precursor cost ──────────────────────────────────────────────────

def test_impact_by_class_dia_only_and_thresholds():
    runs = []
    for i in range(12):
        runs.append(run("2026-01-01", 0.0, "clean", spd=60, prec=40000 + i, mode="diaPASEF"))
        runs.append(run("2026-01-02", 9.0, "heavy", spd=60, prec=36000 + i, mode="DIA"))
    runs.append(run("2026-01-03", 1.0, "trace", spd=60, prec=39000, mode="diaPASEF"))
    runs.append(run("2026-01-03", 1.0, "trace", spd=60, prec=None, mode="diaPASEF"))
    runs += [run("2026-01-04", 0.0, "clean", spd=60, prec=99999, mode="ddaPASEF")] * 5
    # 100 SPD: plenty clean, too few heavy -> no contrast, omitted
    runs += [run("2026-01-05", 0.0, "clean", spd=100, prec=30000, mode="dia")] * 12
    runs += [run("2026-01-05", 9.0, "heavy", spd=100, prec=25000, mode="dia")] * 9
    # Failed searches: 0 precursors is not a depth, and would drag clean down.
    runs += [run("2026-01-06", 0.4, "clean", spd=60, prec=0, mode="diaPASEF")] * 6
    imp = pt.impact_by_class(runs)
    assert list(imp) == ["60"]
    assert imp["60"]["clean"] == [12, round((40005 + 40006) / 2)]
    assert imp["60"]["heavy"] == [12, round((36005 + 36006) / 2)]
    assert imp["60"]["trace"] == [1, 39000]
    assert imp["60"]["moderate"] == [0, None]


def test_impact_empty():
    assert pt.impact_by_class([]) == {}


# ── Instruments and LC summary ──────────────────────────────────────

def test_instruments_from_counts_and_default():
    inst = pt.instruments_from_counts([
        ("timsTOF HT", "evosep", 1600), ("timsTOF HT", "", 4),
        ("Orbitrap Exploris 480", "custom", 1300), ("Orbitrap Exploris 480", "", 20),
        ("Ghost", "evosep", 0),
    ])
    assert inst == [
        {"instrument": "timsTOF HT", "n_runs": 1604, "evosep": True},
        {"instrument": "Orbitrap Exploris 480", "n_runs": 1320, "evosep": False},
    ]
    assert pt.pick_default_instrument(inst) == "timsTOF HT"
    assert pt.pick_default_instrument([]) is None


def test_lab_lc_summary():
    as_of = date(2026, 9, 28)

    def row(inst, lc, day, pct, cls="clean"):
        return {"instrument": inst, "lc_system": lc, "run_date_utc": f"{day}T10:00:00Z",
                "peg_intensity_pct": pct, "peg_class": cls}

    rows = [
        row("timsTOF HT", "evosep", "2026-09-28", 2.0),
        row("timsTOF HT", "evosep", "2026-09-27", 4.0, "heavy"),   # previous week
        row("timsTOF HT", "evosep", "2026-07-01", 6.0, "heavy"),   # 90 d, not in 26 wk? yes
        row("timsTOF HT", "", "2026-01-01", 1.0),                  # 365 d only
        row("timsTOF HT", "evosep", "2025-01-01", 9.0, "heavy"),   # older than a year
        row("timsTOF HT", "evosep", "2026-10-01", 50.0, "heavy"),  # after as_of: ignored
        row("Orbitrap Exploris 480", "custom", "2024-05-01", 0.3),  # idle for a year
    ]
    out = pt.lab_lc_summary(rows, as_of)
    assert [o["instrument"] for o in out] == ["timsTOF HT", "Orbitrap Exploris 480"]
    t = out[0]
    assert t["lc_system"] == "evosep"
    assert (t["n_90d"], t["median_90d"], t["clean_rate_90d"]) == (3, 4.0, 33)
    assert (t["n_365d"], t["median_365d"]) == (4, 3.0)
    assert len(t["weekly"]) == 26
    # 09-27 and 09-28 share the last trailing week (09-22 .. 09-28)
    assert t["weekly"][-1] == 3.0 and t["weekly"][-2] is None
    w0, _ = pt.week_window(as_of)
    assert t["weekly"][pt.week_index(datetime(2026, 7, 1, 10, tzinfo=UTC), w0)] == 6.0
    o = out[1]
    assert (o["n_90d"], o["median_90d"], o["clean_rate_90d"], o["n_365d"]) == (0, None, None, 0)
    assert o["weekly"] == [None] * 26 and o["lc_system"] == "custom"


def test_lab_lc_weekly_ends_on_a_full_week_like_the_relay():
    """Monday as_of, runs through Sunday: the sparkline must not end on null.

    Calendar weeks left the last point empty on a Monday and, on other days,
    a 1-6 day stub beside the relay's full trailing week in the same panel.
    """
    as_of = date(2026, 9, 28)                                  # a Monday

    def row(day, pct):
        return {"instrument": "timsTOF HT", "lc_system": "evosep",
                "run_date_utc": f"{day}T10:00:00Z", "peg_intensity_pct": pct,
                "peg_class": "trace"}

    steady = [row(as_of - timedelta(days=i + 1), 3.0) for i in range(28)]
    weekly = pt.lab_lc_summary(steady, as_of)[0]["weekly"]
    assert weekly[-4:] == [3.0, 3.0, 3.0, 3.0]
    # One outlier on as_of joins the six days before it; as a calendar week
    # of its own on a Monday it would have been the whole last point.
    weekly = pt.lab_lc_summary(steady + [row(as_of, 9.0)], as_of)[0]["weekly"]
    assert weekly[-1] == 3.0

    # Bucket for bucket the relay's _peg_week_edges: (t - edges[0]) // 7 days.
    end = datetime(2026, 9, 29, tzinfo=UTC)
    edges = [end - timedelta(days=7 * (26 - i)) for i in range(27)]
    for t in (edges[0], edges[0] + timedelta(days=6, hours=23), edges[13], end - timedelta(seconds=1)):
        assert pt.week_index(t, edges[0]) == int((t - edges[0]) // timedelta(days=7))


# ── The overview document ───────────────────────────────────────────

SPEC_KEYS = {
    "as_of", "instrument", "instruments", "lc_system", "instrument_family", "runs_cols",
    "runs", "rolling_start", "rolling", "episodes", "baseline", "summary", "ladder",
    "column_periods", "impact", "lab_lc",
}


def _row(day: str, pct: float, cls: str, spd: int = 100, **kw) -> dict:
    return {"run_date_utc": f"{day}T08:30:00Z", "spd": spd, "peg_score": 10.0,
            "peg_intensity_pct": pct, "peg_n_ions_detected": 3, "peg_class": cls,
            "n_precursors": 30000, "mode": "diaPASEF", "lc_system": "evosep",
            "instrument": "timsTOF HT", "has_hits": True, **kw}


def test_build_overview_shape():
    rows = [_row((date(2026, 8, 1) + timedelta(days=i)).isoformat(), 0.5, "clean")
            for i in range(25)]
    rows += [_row("2026-09-01", 4.0, "heavy", spd=60), _row("2026-10-30", 1, "clean"),
             _row("2026-09-02", 0.0, "unknown")]
    doc = pt.build_overview(
        as_of=date(2026, 9, 28), instrument="timsTOF HT",
        instruments=[{"instrument": "timsTOF HT", "n_runs": 26, "evosep": True}],
        runs=rows, ladder_rows=[("2026-08", 9, "+NH4", 5)],
        column_events=[("2026-08-10", None)],
        lab_lc=[{"instrument": "timsTOF HT", "lc_system": "evosep", "n_90d": 26}],
        family_fn=lambda m: "timsTOF" if "tims" in m.lower() else m,
    )
    assert set(doc) == SPEC_KEYS
    assert doc["runs_cols"] == ["t", "spd", "pct", "score", "ions", "cls", "prec"]
    assert len(doc["runs"]) == 26                     # unknown + future run dropped
    assert doc["runs"][0] == ["2026-08-01T08:30", 100, 0.5, 10.0, 3, 0, 30000]
    assert doc["runs"][-1][5] == 3                    # heavy -> 3
    assert doc["rolling_start"] == "2026-08-01"
    assert set(doc["rolling"]) == {"all", "100"}      # 60 SPD has 1 run, not 20
    assert len(doc["rolling"]["all"]) == (date(2026, 9, 28) - date(2026, 8, 1)).days + 1
    assert doc["lc_system"] == "evosep" and doc["instrument_family"] == "timsTOF"
    assert doc["lab_lc"][0] == {"instrument": "timsTOF HT", "family": "timsTOF",
                                "lc_system": "evosep", "n_90d": 26}
    assert doc["ladder"]["months"] == ["2026-08", "2026-09"]
    assert doc["ladder"]["share"][doc["ladder"]["n"].index(9)][0] == 0.2
    assert doc["column_periods"][0]["n_qc"] == 17
    assert doc["summary"]["n_30d"] == 1
    json.dumps(doc)                                   # serialisable as-is


def test_ladder_denominator_is_runs_with_a_known_ladder():
    """A run with PEG ions but no stored hits is unknown, not "oligomer absent".

    Live PG had 206 such timsTOF rows, 147 in Feb-Apr 2026; counted in the
    denominator they halved the share and drew the worst months as clean.
    """
    rows = [
        _row("2026-03-02", 6.0, "heavy", peg_n_ions_detected=9, has_hits=True),
        _row("2026-03-03", 6.0, "heavy", peg_n_ions_detected=9, has_hits=True),
        _row("2026-03-04", 5.0, "heavy", peg_n_ions_detected=3, has_hits=False),
        _row("2026-03-05", 0.0, "clean", peg_n_ions_detected=0, has_hits=False),
    ]
    doc = pt.build_overview(as_of=date(2026, 3, 31), instrument="timsTOF HT",
                            instruments=[], runs=rows,
                            ladder_rows=[("2026-03", 10, "+NH4", 2)],
                            family_fn=lambda m: "timsTOF")
    lad = doc["ladder"]
    assert lad["months"] == ["2026-03"] and lad["nruns"] == [3]
    assert lad["share"][lad["n"].index(10)] == [round(2 / 3, 3)]
    assert len(doc["runs"]) == 4                    # still a run everywhere else


def test_build_overview_without_runs_is_valid_and_empty():
    doc = pt.build_overview(as_of=date(2026, 9, 28), instrument="Lumos",
                            instruments=[], runs=[], family_fn=lambda m: "Lumos")
    assert set(doc) == SPEC_KEYS
    assert doc["runs"] == [] and doc["episodes"] == [] and doc["baseline"] is None
    assert doc["rolling"] == {"all": []} and doc["rolling_start"] is None
    assert doc["instrument"] == "Lumos" and doc["instrument_family"] == "Lumos"
    assert doc["summary"]["n_30d"] == 0
    assert pt.empty_overview(date(2026, 9, 28))["instrument"] is None
