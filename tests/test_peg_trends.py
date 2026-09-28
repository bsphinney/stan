"""PEG trend maths behind the dashboard's PEG tab (stan.metrics.peg_trends).

Two properties matter more than any single number here:

* an ``'unknown'`` row -- the pipeline's failure sentinel, stored with
  peg_score 0.0, which is exactly a clean run's score -- never counts, and an
  unmeasured run is dropped rather than read as 0;
* the episode detector reproduces the two UC Davis timsTOF PEG episodes the
  spec quotes, both on a synthetic series shaped like them and, when the
  read-only PG extract is on disk, on the real data.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from stan.metrics import peg_trends as pt
from stan.metrics.peg_trends import PegRun

UTC = timezone.utc

# The read-only extract the spec numbers were computed from. Present on the
# dev Mac during the PEG Watch build; CI has no copy, so that test skips.
EXTRACT = Path(
    "/private/tmp/claude-501/-Users-brettphinney-Documents-STAN/"
    "39187c0f-28e0-4ce0-892b-286c8db04795/scratchpad/peg_extract.json"
)

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
                                 {}, date(2026, 9, 28))
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


def test_week_starts_are_mondays_oldest_first():
    ws = pt.week_starts(date(2026, 9, 28), 26)   # a Monday
    assert len(ws) == 26
    assert ws[-1] == date(2026, 9, 28)
    assert all(w.weekday() == 0 for w in ws)
    assert ws[0] == date(2026, 9, 28) - timedelta(weeks=25)
    assert pt.week_starts(date(2026, 10, 4), 2) == [date(2026, 9, 21), date(2026, 9, 28)]


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


@pytest.mark.skipif(not EXTRACT.exists(), reason="read-only PG extract not on this machine")
def test_real_uc_davis_extract_yields_the_spec_episodes():
    """The numbers quoted in the spec, from the 1,682-run timsTOF HT extract."""
    d = json.loads(EXTRACT.read_text())
    cols = d["runs_cols"]
    rows = []
    for r in d["runs"]:
        x = dict(zip(cols, r))
        rows.append({"run_date_utc": x["t"], "spd": x["spd"], "peg_score": x["score"],
                     "peg_intensity_pct": x["pct"], "peg_n_ions_detected": x["ions"],
                     "peg_class": x["cls"], "n_precursors": x["prec"], "mode": x["mode"]})
    runs = pt.to_peg_runs(rows)
    assert len(runs) == 1682
    eps = _episodes(runs, "2026-09-28")
    assert eps == [
        {"start": "2025-12-15", "end": "2026-04-26", "days": 132, "n": 406,
         "median_pct": 4.25, "heavy_pct": 37, "ongoing": False},
        {"start": "2026-05-21", "end": "2026-09-18", "days": 120, "n": 248,
         "median_pct": 5.18, "heavy_pct": 40, "ongoing": False},
    ]
    s = pt.summary_30d(runs, date(2026, 9, 28))
    assert (s["n_30d"], s["change_pct"], s["clean_30d"], s["heavy_30d"],
            s["clean_rate_30d"], s["streak_clean"]) == (74, -71, 21, 13, 28, 2)


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
    assert t["weekly"][-1] == 2.0 and t["weekly"][-2] == 4.0
    assert t["weekly"][pt.week_starts(as_of).index(pt.week_start(date(2026, 7, 1)))] == 6.0
    o = out[1]
    assert (o["n_90d"], o["median_90d"], o["clean_rate_90d"], o["n_365d"]) == (0, None, None, 0)
    assert o["weekly"] == [None] * 26 and o["lc_system"] == "custom"


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
            "instrument": "timsTOF HT", **kw}


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


def test_build_overview_without_runs_is_valid_and_empty():
    doc = pt.build_overview(as_of=date(2026, 9, 28), instrument="Lumos",
                            instruments=[], runs=[], family_fn=lambda m: "Lumos")
    assert set(doc) == SPEC_KEYS
    assert doc["runs"] == [] and doc["episodes"] == [] and doc["baseline"] is None
    assert doc["rolling"] == {"all": []} and doc["rolling_start"] is None
    assert doc["instrument"] == "Lumos" and doc["instrument_family"] == "Lumos"
    assert doc["summary"]["n_30d"] == 0
    assert pt.empty_overview(date(2026, 9, 28))["instrument"] is None
