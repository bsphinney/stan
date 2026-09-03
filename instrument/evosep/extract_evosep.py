#!/usr/bin/env python3
"""Evosep One column-health / clog early-warning extractor.

Reads the pressure time-series the Evosep One writes for every procedure it
runs and turns them into a compact JSON of column-health signals for the STAN
dashboard.

WHY THIS EXISTS
---------------
The Bruker Compass database records an LC failure only as an *error string*
("Evosep One: pressure limit exceeded") — i.e. after a run has already died,
and only when it died. The Evosep One itself writes a full pressure trace per
run, so the same event is visible as a *rising curve* well before the abort,
and partial occlusions that never abort a run are visible too — those are
completely invisible to the error log.

WHICH PUMP IS THE COLUMN
------------------------
`Pump-HP` — the high-pressure pump. Evidence, from the data:
  * Pump-HP runs 300-520 bar; Pumps A/B/C/D never exceed ~10 bar in normal
    operation. Only a pump pushing through a packed analytical bed develops
    hundreds of bar.
  * Pump-HP_Actual-flow sits at 1.68 uL/min +/- 0.30 — nano-flow, i.e. the
    analytical column flow. A/B/C/D move solvent at the loading/wash scale.
  * Pump-HP pressure traces the gradient profile and is reproducible run to
    run to <1 bar; A/B/C/D pressures are flat near zero.
  * `maintenance-info.txt` lists pumphp with product number 1001 while
    pumpa..pumpd are all product number 1002 — a different device class.
Pumps A and B are still worth watching: a *low*-pressure spike there is the
Evotip-seating / needle-block signature, a different failure mode.

WHAT IT EMITS
-------------
Per run: method, start datetime, duration, peak / median / plateau pressure,
a downsampled curve, low-pressure-side maxima, and instrument wear counters.
Plus per-method baselines, an intra-run reference envelope, detected step
changes in baseline (candidate column changes / interventions), and a list of
flagged runs.

The whole history is always ANALYSED; only the per-run arrays are windowed on
the way out, because the document is stored as one PG row and shipped on every
panel load. Unwindowed, the 2023-onward mirror is ~13 MB. So:

  * `runs`, `flags`, `methods[].series`  — last `--runs-window-days` (90)
  * `daily`                             — one entry per calendar day, forever
  * `columns`                           — one summary per column fitted
  * `methods[].steps`, `wear`, `column` — whole history

`--max-doc-mb` (default 1.0) fails the extract rather than let that regress.

Read-only. Nothing under the log root is ever written or modified.

Log layout: the copy script maintains a stable `<HOST>_mirror` folder; older
pulls sit in `<HOST>_<YYYYMMDD_HHMMSS>` folders. Every folder for the
instrument is read and de-duplicated by run folder, so the two coexist.

Usage
-----
    python3 extract_evosep.py --out evosep_column_health.json
    python3 extract_evosep.py --root /path/to/evosep_logs --host-dir TIMS-10878_mirror

    # everything, for offline analysis rather than publishing
    python3 extract_evosep.py --runs-window-days 0 --keep-all-runs \
        --max-doc-mb 0 --out full.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics as st
import sys
from datetime import datetime, timedelta, timezone

# ── Constants ─────────────────────────────────────────────────────────────

#: Default location of the mirrored Evosep log tree on Hive.
DEFAULT_ROOT = "/quobyte/proteomics-grp/brett/evosep_logs"

#: Bruker maintenance extract, used only to score this feature against the
#: instrument's own failure log. Absence is not fatal.
DEFAULT_BRUKER_JSON = "/quobyte/proteomics-grp/STAN/bruker_maintenance.json"

#: Hardware over-pressure cut-out of the Evosep One high-pressure pump, in bar.
#: Confirmed empirically: across the whole corpus the maximum pressure ever
#: recorded is 519.9 bar and exactly the runs that touch it are the ones
#: Compass logged as "pressure limit exceeded".
CEILING_BAR = 520.0

#: The analytical-column pump (see module docstring).
COLUMN_PUMP = "Pump-HP"

#: Low-pressure pumps whose pressure reports Evotip seating, not column state.
TIP_PUMPS = ("Pump-A", "Pump-B")

#: Relative-time window used for the "plateau" pressure. Chosen from the data:
#: across 100-samples-per-day runs the Pump-HP trace is flat to <1 bar between
#: 45 % and 80 % of the run, after the loading transient and before the wash
#: ramp. Comparing the same relative window across runs of the same method
#: holds solvent composition constant, so what is left is column resistance.
PLATEAU_LO, PLATEAU_HI = 0.45, 0.80

#: Runs shorter than this fraction of the method's median duration are treated
#: as aborted rather than as a short method.
ABORT_FRAC = 0.6

#: A run is "elevated" when its plateau exceeds the LOCAL baseline by this
#: fraction, and "critical" at the higher one. Local, not global: the column
#: baseline genuinely drifts over its life, so "abnormally high" has to mean
#: "high relative to where this system just was", not relative to a global
#: median that mixes a fresh column with a spent one.
ELEVATED_FRAC, CRITICAL_FRAC = 0.10, 0.20

#: Runs of the same method used for that local baseline, and the minimum
#: number of them required before a baseline is trusted at all.
LOCAL_WINDOW, LOCAL_MIN = 12, 6

#: Pump-A/B pressure threshold for a *fault*, in bar. Calibrated from the
#: data, not guessed: every run begins with a tip-seating pressure test that
#: legitimately reaches ~50 bar, so 50 is normal. The two runs Compass logged
#: as "No Evotip was present" both reached 65-67 bar and aborted after 0.9
#: min, so the fault threshold sits above the routine test.
TIP_SPIKE_BAR = 60.0

#: Pressure below which a method is a fluidics utility (priming, preparation)
#: rather than an analytical separation, so it is excluded from column-health
#: baselines. Real gradients on this system plateau above 100 bar.
ANALYTICAL_MIN_BAR = 100.0

#: A method whose plateau pressure spans less than this (p95-p05, bar) across
#: its whole history is running against a regulated SETPOINT, not against the
#: column. System-and-column-wash is the example: 34 runs over 15 days, all at
#: 399.6 bar +/- 0.3, straddling two clog events. A number that cannot move
#: cannot report column resistance, so it is excluded from column health.
CONTROLLED_SPAN_BAR = 2.0

#: A trend in bar/day is only meaningful over a real stretch of time. Below
#: these, the slope is extrapolation from noise and is not reported.
TREND_MIN_DAYS, TREND_MIN_RUNS = 1.0, 10

#: Hours after a wash / preparation procedure during which an elevated
#: baseline is re-equilibration rather than a developing blockage.
POST_INTERVENTION_H = 8.0

#: Number of points in the downsampled per-run curve shipped to the UI.
CURVE_POINTS = 48

#: Per method, how many of the most recent runs keep their curve in the
#: output. Curves dominate the document size, so they are kept only where the
#: panel draws one: every flagged run, plus this recent tail for comparison.
CURVE_KEEP_TAIL = 12

#: Runs of the same method whose curves form the ROLLING reference a run is
#: compared against. Rolling and trailing, so a run is measured against how
#: this same system behaved immediately before it — not against a global
#: "calmest ever" window that may sit at a different baseline entirely and
#: would make every later run look pre-breached.
REF_WINDOW = 5

#: Bar above the rolling reference, sustained, that counts as leaving the
#: healthy envelope.
BREACH_BAR = 30.0

#: Fraction of the run skipped before breach detection: the start-up transient
#: is not reproducible enough to alarm on.
BREACH_SKIP = 0.08

#: Grid points after a candidate breach whose MEDIAN deviation must also clear
#: BREACH_BAR before the breach is believed. Without this, a single point in
#: one of the steep loading transitions — where a fraction of a second of
#: timing shift is worth tens of bar — reads as an early breach and inflates
#: the reported warning time. On the 2026-08-28 02:07 clog that artifact moved
#: the answer from a real 8.7 min of warning to a false 11.1 min.
BREACH_PERSIST = 3

#: A baseline step of at least this fraction between consecutive runs marks a
#: candidate intervention (column change, wash, seal service).
STEP_FRAC = 0.06

#: Days between two runs beyond which they are not "consecutive" and the
#: difference between them is not an event. The instrument idles over
#: weekends and holidays, so this has to be generous; it exists to reject
#: HOLES IN THE RECORD, not quiet periods. The mirror is backfilled in
#: chunks, which makes those holes real — a 2023 chunk beside a 2026 chunk
#: produced a confident "intervention" whose two defining runs were 1,141
#: days apart.
STEP_MAX_GAP_DAYS = 21.0

#: Flow bin width for the per-column pressure table, in uL/min. Fine enough
#: to separate the methods actually run (the 30/60/100 SPD gradients sit at
#: distinct nano-flow rates) without splitting one method across two bins.
PRESSURE_BIN_UL = 0.1

#: Minimum runs before a flow bin is reported at all. A "typical pressure"
#: from two runs is not a reference value.
PRESSURE_MIN_BIN = 5

#: Runs at each end of a column's life used for the new-vs-now comparison.
PRESSURE_NEW_RUNS = 20

#: Percent deviation in bar-per-(uL/min) from the column's own median that
#: counts as breaking Darcy linearity. Generous, because the ratio also moves
#: with the solvent composition each method sits at mid-gradient.
LINEARITY_TOL_PCT = 25.0

#: Days between consecutive runs inside a column segment beyond which the
#: segment cannot be treated as one column's life — the record has a hole in
#: it and however many columns were fitted across that hole are averaged.
SEGMENT_GAP_WARN_DAYS = 45.0

#: Setpoint value at or above which the pump is holding PRESSURE (bar) rather
#: than FLOW (uL/min). The two differ by two orders of magnitude on this
#: instrument -- 400 vs 1.5 -- so this only has to land between them.
CONTROL_PRESSURE_MIN_SETPOINT = 50.0

#: A pressure-controlled run only belongs in the wash-flow trend if it
#: actually reached its setpoint; otherwise the flow is not comparable with
#: the others. Every qualifying run on this system sits at 398.9-399.8 bar.
WASH_SETPOINT_TOL_BAR = 5.0

#: Fractional step in wash flow within one column's runs that suggests the
#: segment boundary is in the wrong place. Generous: a real column's wash
#: flow drifts by a few percent over its life, so only a step several times
#: that size indicates two different columns in one group.
WASH_SEGMENT_STEP_FRAC = 0.15

#: Drop in analytical resistance, in percent, that marks a candidate column
#: change. Calibrated on the two known boundaries: 2026-07-30 was -13.6 % and
#: 2026-09-02 was -18.2 %, while ordinary run-to-run movement on a settled
#: column is under 2 %.
COLUMN_DROP_PCT = 8.0

#: Hours either side of a candidate in which to look for washes, and the rise
#: in wash flow that corroborates a change. Washes bracketing a candidate
#: WITHOUT a rise are evidence against it — that is what separates a real
#: change from a wash recovery, which lifts wash flow only briefly.
COLUMN_WASH_WINDOW_H = 48.0
COLUMN_WASH_RISE_PCT = 10.0

#: Detections closer together than this are one event seen twice; the larger
#: is kept. A naive detector reported the July change at both 07-20 and 07-31
#: with identical figures for want of this.
COLUMN_MERGE_DAYS = 3.0

#: Hours within which a logged column_change and a detected step are taken to
#: be the same event. Generous, because a logged event_date is often a
#: placeholder noon: the 2026-09-02 change was logged six hours early.
COLUMN_LOGGED_TOL_H = 36.0

#: Injections below which the current column is reported but flagged as too
#: new to characterise. It is still reported — it is the one Brett looks at.
COLUMN_PROVISIONAL_RUNS = 50

#: Runs at each end of a column's life used for its "when new" and "now"
#: resistance. Small enough to catch the install before fouling starts, large
#: enough that one bad injection cannot set the baseline.
COLUMN_FRESH_RUNS = 20

#: Brett's wash-level rule. A level reaching this fraction of the column's own
#: fresh flow is a replacement; it only counts after flow had fallen below the
#: decline threshold, and the level is a median of this many consecutive
#: washes because single readings are too noisy (see the function's docstring).
WASH_RECOVER_FRAC = 0.97
WASH_DECLINE_FRAC = 0.92
WASH_LEVEL_SMOOTH = 3

#: Fraction of its own fresh wash flow at which a column gets replaced.
#: Measured on the retired column: fresh 2.267, replaced at 1.738 = 76.7 %.
#: Relative by design, so it carries to a column type never seen before.
WASH_REPLACE_FRAC = 0.767

#: Washes at each end used for a column's fresh and current wash flow.
WASH_FRESH_N = 5

#: Gate on the SIGNAL, not on injection count. Below this decline an
#: extrapolation is fitting noise: at 266 injections the retired column read
#: ABOVE fresh and the flat fit through it projected 31,744 runs remaining.
RUNS_REMAINING_MIN_DECLINE = 0.08

#: Washes needed before a trend is worth fitting at all.
RUNS_REMAINING_MIN_WASHES = 12

#: Minimum width of the reported range, as a fraction of the estimate. The
#: fit's standard error is uncertainty in the LINE; it says nothing about
#: whether the decline stays linear, and retrospectively this projection runs
#: 10-30 % low. Without a floor, a tidy run of washes yields a zero-width
#: "range" that reads as precision the method does not have.
RUNS_REMAINING_MIN_SPREAD = 0.30

#: Window (days before a candidate) used as "where this column sat before its
#: recent trouble", and how far below that the post-drop level must land to be
#: a new column rather than a blockage clearing. 14-7 days back skips the
#: episode itself while staying on the same column.
RECOVERY_BASELINE_D = (14.0, 7.0)
RECOVERY_TOL = 0.05

#: Sample attribution. The acquisition's well appears in its raw name as
#: `_S<slot>-<well>_`; the submission number is field 2 of the filename,
#: anchored at the start so plate wells and instrument serials later in the
#: name cannot be misread as one (see HT_work/CLAUDE.md).
SAMPLE_WELL_RE = re.compile(r"_(S\d+-[A-H]\d+)_")
SUBMISSION_RE = re.compile(r"^\d{8}_(\d{2,4})_")

#: Minutes either side of a procedure's start in which to look for its
#: acquisition. Measured offset is +2.53 min median, +2.5 to +10.2 range over
#: 289 matched pairs, so this is generous but far below a run length.
ATTRIB_TOL_MIN = 25.0

#: A sample-impact step must clear this, in bar per (uL/min) — an absolute
#: floor as well as the robust one, so a very quiet column does not make a
#: trivial step "significant". ~3.5 % of this system's 230 bar/(uL/min).
IMPACT_MIN_BAR_PER_UL = 8.0

#: Runs of a method needed in a segment before sample impact is scored, and
#: the first runs of a segment skipped as column conditioning — a fresh
#: column settling is not a sample's fault.
IMPACT_MIN_RUNS = 20
IMPACT_CONDITIONING_RUNS = 5

#: Hours around a logged column/capillary change in which a step is explained
#: by the intervention rather than by any sample.
IMPACT_EVENT_GUARD_H = 12.0

#: Flow sd above which a method is not flow-controlled, so its pressure is
#: held at a setpoint and cannot step.
IMPACT_FLOW_SD_MAX = 0.15

#: An attributed acquisition whose name says it is a control — a blank, a
#: wash, a QC standard — is not a submitted sample, so it never reaches the
#: per-submission tally. The step itself is still reported: it happened, and
#: a blank that fouls a column is worth knowing about. Matched on the
#: ACQUISITION name, because the Evosep log knows only method and vial.
CONTROL_RUN_RE = re.compile(
    r"(?i)(blank|wash|_qc[_-]|^qc[_-]|hela|hel-?\d|std[_\-\s]?he)")

#: Kozeny-Carman constants for the theory smell test ONLY. `phi` is the
#: packing/flow-resistance constant for a well-packed bed (~500-1000 depending
#: who you ask); `eta` is the viscosity of a mid-gradient water/ACN mix at
#: column-oven temperature. Both are uncertain to tens of percent, which is
#: exactly why the measured table is the deliverable.
DARCY_PHI = 700.0
DARCY_ETA_PAS = 0.65e-3

#: Where to look for the column catalogue, in order. The deployed STAN
#: checkout is preferred so the file has one owner; the evosep work dir is an
#: interim copy for while the Hive checkout lags the release that added it.
#: First existing path wins, so the interim copy stops being used the moment
#: the checkout catches up.
DEFAULT_COLUMNS_YML = ":".join((
    "/quobyte/proteomics-grp/brett/stan/config/columns.yml",
    "/quobyte/proteomics-grp/STAN/evosep/columns.yml",
))

#: Days of per-run detail kept in the PUBLISHED document. The full history is
#: still analysed — baselines, steps, column segments and the daily aggregates
#: all see every run — but only this much per-run detail is shipped.
#:
#: Sizing, measured on the real 568-run extract: a run record costs ~496 B and
#: its `methods[].series` entry another ~56 B. The 2023-onward mirror is
#: ~23,000 runs, so an unwindowed document is **~13 MB** in one PG row and on
#: every panel load. That is the regression this bounds.
RUNS_WINDOW_DAYS = 90

#: Hard ceiling on the published document. Asserted at publish time so a
#: regression fails loudly instead of quietly serving megabytes to a phone.
MAX_DOC_MB = 1.0

#: Within the window, keep only run records a consumer can actually use: one
#: carrying a pressure curve (the panel draws those) or one that was flagged.
#: Verified against every consumer — the panel reads `d.runs` ONLY to find
#: ceiling-touching runs that carry a `ref_curve`; `stan/reports/
#: instrument_watch.py` reads `instrument_host`, `flags`, `column` and
#: `ceiling_bar` and never touches `runs`; `server.py` passes the document
#: through untouched. A curve-less, unflagged run record has no reader and
#: costs 431 B. Set --keep-all-runs to ship them anyway.
LEAN_RUNS = True

RUN_DIR_RE = re.compile(r"^(?P<method>.+)_(?P<date>\d{4}-\d{2}-\d{2})_"
                        r"(?P<h>\d{2})-(?P<m>\d{2})-(?P<s>\d{2})$")

#: The stable incremental mirror written by copy_evosep_logs.ps1 since
#: 2026-09-02: `<HOST>_mirror`, one folder that is topped up in place.
HOST_MIRROR_RE = re.compile(r"^(?P<host>.+)_mirror$")

#: The legacy layout — one `<HOST>_<YYYYMMDD_HHMMSS>` folder per pull, each
#: holding only that pull's window. Still read, because the first pull lives
#: in one and it may hold a run the mirror has not caught up to yet.
HOST_TS_RE = re.compile(r"^(?P<host>.+)_(?P<ts>\d{8}_\d{6})$")

#: Days of log that must PRECEDE an inferred column install before the
#: inference is believable. A downward pressure step only means "a column was
#: installed here" if the record also shows the old column's baseline before
#: it; with less history than this, the same step is equally consistent with a
#: glass-capillary swap, a wash, or a column fitted before the log window even
#: opened.
#:
#: Calibrated against a known-truth failure. The real column change on this
#: system was 2026-07-31 (recorded by the operator in the run names
#: `07312026_HE50_60-spd-dia-new-zdf-column`), which is 14 days BEFORE the
#: first mirrored log (2026-08-14). With only that window the extractor
#: confidently reported an install of 2026-08-19 — a date that is in fact when
#: a new glass capillary went in, not a column. An inference that cannot see
#: the event has to say so rather than name the largest step it happens to
#: contain.
INSTALL_MIN_PRIOR_DAYS = 14.0


# ── Parsing ───────────────────────────────────────────────────────────────

def parse_series(path: str) -> list[tuple[float, float]]:
    """Parse one Evosep signal file into [(seconds, value), ...].

    Format is a one-line header then `HH:MM:SS.mmm<TAB><float>` rows. Bad rows
    are skipped rather than fatal: these are instrument-written logs and a run
    that was killed mid-write can leave a truncated final line.
    """
    out: list[tuple[float, float]] = []
    try:
        with open(path, "r", errors="replace") as fh:
            fh.readline()  # header
            for line in fh:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 2:
                    continue
                try:
                    hh, mm, ss = parts[0].split(":")
                    out.append((int(hh) * 3600 + int(mm) * 60 + float(ss),
                                float(parts[1])))
                except (ValueError, IndexError):
                    continue
    except OSError:
        return []
    return out


#: The Evosep names its folders by method and time; the SAMPLE it injected is
#: in journal.txt as the vial position. That is the only thread from a
#: pressure trace back to a submission, so it is worth parsing.
JOURNAL_WELL_RE = re.compile(r"Procedure\.Vialposition:.*\((?P<well>S\d+-[A-H]\d+)\)")


def parse_journal(path: str) -> dict:
    """Read the per-run journal: which vial this procedure injected.

    Present on 505 of 506 analytical runs since 2026-08 and absent from the
    2023-era logs, so callers must treat the well as optional.
    """
    try:
        with open(path, errors="ignore") as fh:
            text = fh.read()
    except OSError:
        return {}
    m = JOURNAL_WELL_RE.search(text)
    return {"well": m.group("well")} if m else {}


def parse_control_mode(path: str, dur_s: float | None = None) -> dict:
    """Is this pump holding a PRESSURE or a FLOW setpoint on this run?

    Read from the pump's own setpoint channel, not from the method name, so it
    cannot go wrong when a method is renamed or a new one appears. The two are
    unmistakable in magnitude: an Evosep pressure setpoint is in bar (400) and
    a flow setpoint in uL/min (1.5), so a single threshold separates them.

    It matters because the two modes put the measurement on opposite axes.
    Under flow control the pressure is the column's resistance. Under pressure
    control the pressure is pinned by definition and cannot move, so the
    resistance shows up as FLOW instead -- which is why the system-and-column
    washes are the one signal needing no baseline, no flow normalisation and
    no reference table.
    """
    series = parse_series(path)
    # Read the setpoint over the SAME plateau window as the pressure and flow.
    # A wash ramps through lower setpoints on its way to 400 bar, so a median
    # over the whole run returned 202.5 for a run that plainly held 400 —
    # which then failed the setpoint-tolerance check and dropped a real point.
    if dur_s:
        window = [v for t, v in series
                  if PLATEAU_LO * dur_s <= t <= PLATEAU_HI * dur_s and v > 0]
        if window:
            series = [(0.0, v) for v in window]
    vals = [v for _, v in series if v > 0]
    if not vals:
        return {}
    m = _median(vals)
    if m >= CONTROL_PRESSURE_MIN_SETPOINT:
        return {"control_mode": "pressure", "setpoint_bar": round(m, 1)}
    return {"control_mode": "flow", "setpoint_ul_min": round(m, 3)}


def parse_maintenance_info(path: str) -> dict:
    """Pull the instrument wear counters out of `maintenance-info.txt`.

    The Evosep writes one of these per run. It carries monotonically
    increasing lifetime counters, so differencing them across runs gives real
    wear rates: pump seal displacement in mL (when to service a seal) and the
    total analysis count (instrument lifetime).
    """
    info: dict = {}
    try:
        with open(path, "r", errors="replace") as fh:
            txt = fh.read()
    except OSError:
        return info

    m = re.search(r"Total analyses:\s*(\d+)", txt)
    if m:
        info["total_analyses"] = int(m.group(1))
    m = re.search(r"Loop volume:\s*([\d.]+)", txt)
    if m:
        info["loop_volume_ul"] = float(m.group(1))

    seals: dict[str, int] = {}
    for block in txt.split("Component: "):
        name = block.split("\n", 1)[0].strip().lower()
        if not name.startswith("pump"):
            continue
        m = re.search(r"Displacement \(seal\):\s*(\d+)", block)
        if m:
            seals[name] = int(m.group(1))
    if seals:
        info["pump_seal_ml"] = seals

    # Per-method lifetime analysis counters live in the `instrument` block.
    counts: dict[str, int] = {}
    if "Component: instrument" in txt:
        blk = txt.split("Component: instrument", 1)[1]
        for line in blk.splitlines():
            m = re.match(r"\s*([\w \-]+ samples per day|Whisper[\w \-]*):\s*(\d+)\s*$", line)
            if m:
                counts[m.group(1).strip()] = int(m.group(2))
    if counts:
        info["method_lifetime_counts"] = counts
    return info


# ── Small numeric helpers ─────────────────────────────────────────────────

def _median(xs):
    return st.median(xs) if xs else None


def _pct(xs, q):
    if not xs:
        return None
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[i]


def _downsample(series, n=CURVE_POINTS):
    """Bin a series into n equal relative-time bins, taking the median of each.

    Median rather than mean so a single spike cannot move the drawn curve, and
    relative time so runs of the same method line up regardless of small
    duration differences.
    """
    if not series:
        return []
    total = series[-1][0] or 1.0
    bins: list[list[float]] = [[] for _ in range(n)]
    for t, v in series:
        i = min(n - 1, int(t / total * n))
        bins[i].append(v)
    out, last = [], 0.0
    for b in bins:
        last = st.median(b) if b else last
        out.append(round(last, 1))
    return out


def _at_minute(curve, dur_min, t_min):
    """Value of a relative-time downsampled curve at an ABSOLUTE minute.

    Curves are binned by relative time so runs line up regardless of small
    duration differences — but a run that ABORTS is short precisely because it
    failed, so its relative axis is compressed and every feature shifts
    earlier. Comparing an aborted run to a healthy one bin-for-bin therefore
    misaligns the steep loading transient and manufactures a false early
    breach. The instrument's schedule is absolute, so comparisons are made at
    absolute minutes and interpolated here.
    """
    if not curve or not dur_min:
        return None
    n = len(curve)
    x = (t_min / dur_min) * n - 0.5
    if x <= 0:
        return curve[0]
    if x >= n - 1:
        return curve[n - 1]
    i = int(x)
    f = x - i
    return curve[i] * (1 - f) + curve[i + 1] * f


def _linreg(xs, ys):
    """Least-squares slope and r^2. Returns (slope, r2)."""
    n = len(xs)
    if n < 3:
        return 0.0, 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0, 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    syy = sum((y - my) ** 2 for y in ys)
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else 0.0
    return slope, r2


# ── Per-run extraction ────────────────────────────────────────────────────

def extract_run(run_dir: str, name: str) -> dict | None:
    """Build the signal record for one Evosep procedure run."""
    m = RUN_DIR_RE.match(name)
    if not m:
        return None
    method = m.group("method")
    start = f"{m.group('date')}T{m.group('h')}:{m.group('m')}:{m.group('s')}"

    hp = parse_series(os.path.join(run_dir, f"{COLUMN_PUMP}_Pressure.txt"))
    if len(hp) < 10:
        return None

    dur_s = hp[-1][0]
    vals = [v for _, v in hp]
    # Plateau uses only positive pressure: a pump that is off logs 0.0 and
    # would otherwise drag the median down and hide a real elevation.
    plateau_vals = [v for t, v in hp
                    if PLATEAU_LO * dur_s <= t <= PLATEAU_HI * dur_s and v > 0]

    # When the run first touched the pump cut-out, if it ever did. This — not
    # the end of the log — is the moment the failure actually happened, so it
    # is what a warning time must be measured against.
    t_ceiling = next((t for t, v in hp if v >= CEILING_BAR - 5), None)

    rec: dict = {
        "run": name,
        "method": method,
        "start": start,
        "duration_min": round(dur_s / 60.0, 2),
        "n_points": len(hp),
        "peak_bar": round(max(vals), 1),
        "median_bar": round(st.median(vals), 1),
        "plateau_bar": round(_median(plateau_vals), 1) if plateau_vals else None,
        "p95_bar": round(_pct(vals, 0.95), 1),
        "curve": _downsample(hp),
        "t_ceiling_min": (round(t_ceiling / 60.0, 2) if t_ceiling is not None else None),
    }

    flow = parse_series(os.path.join(run_dir, f"{COLUMN_PUMP}_Actual-flow.txt"))
    fv = [v for t, v in flow if t > 60]
    if fv:
        rec["flow_ul_min"] = round(st.mean(fv), 3)
        rec["flow_sd"] = round(st.pstdev(fv), 3)
    # Flow measured in the SAME window as the plateau pressure. A
    # pressure-per-flow figure is only meaningful if both numbers describe the
    # same moment: `flow_ul_min` averages the whole run, including the loading
    # transient and the wash ramp, which is why its sd runs to ~0.8 uL/min —
    # most of a nano-flow rate. The plateau window is flat in both channels.
    pfv = [v for t, v in flow
           if PLATEAU_LO * dur_s <= t <= PLATEAU_HI * dur_s and v > 0]
    if pfv:
        rec["plateau_flow_ul_min"] = round(_median(pfv), 3)
        rec["plateau_flow_sd"] = round(st.pstdev(pfv), 3)

    # Low-pressure side: the Evotip / needle-seating channel.
    tip_max = 0.0
    for p in TIP_PUMPS:
        s = parse_series(os.path.join(run_dir, f"{p}_Pressure.txt"))
        if s:
            tip_max = max(tip_max, max(v for _, v in s))
    rec["tip_pressure_max_bar"] = round(tip_max, 1)

    rec.update(parse_maintenance_info(os.path.join(run_dir, "maintenance-info.txt")))
    rec.update(parse_journal(os.path.join(run_dir, "journal.txt")))
    rec.update(parse_control_mode(
        os.path.join(run_dir, f"{COLUMN_PUMP}_Setpoint.txt"), dur_s))
    return rec


# ── Aggregation ───────────────────────────────────────────────────────────

def build_envelope(runs: list[dict], method: str, ref_runs: list[dict]) -> dict | None:
    """Median +/- spread intra-run pressure profile for a method.

    Built from a *healthy* reference window so a run can be compared against
    "what this method looks like when the column is fine" rather than against
    an average that already contains the failures.
    """
    curves = [r["curve"] for r in ref_runs if r.get("curve")]
    if len(curves) < 3:
        return None
    n = min(len(c) for c in curves)
    med = [st.median([c[i] for c in curves]) for i in range(n)]
    sd = [st.pstdev([c[i] for c in curves]) for i in range(n)]
    return {
        "method": method,
        "n_reference_runs": len(curves),
        "reference_runs": [r["run"] for r in ref_runs],
        "median": [round(v, 1) for v in med],
        "sd": [round(v, 2) for v in sd],
    }


def detect_steps(series: list[tuple[str, float]]) -> list[dict]:
    """Find *sustained* run-to-run baseline steps — candidate interventions.

    A column change, a system-and-column wash or a seal service shows up as a
    step in plateau pressure between two consecutive runs of a method. Because
    STAN's `maintenance_events` table is usually empty (nobody logs the
    change), detecting them from the pressure itself is what makes the "days
    on this column" number available at all.

    "Sustained" matters: a single clogged sample also makes a big step, but
    the level comes back down on the next run. Requiring the new level to hold
    across the following runs separates an intervention (permanent change)
    from an incident (transient excursion).
    """
    steps = []
    for i in range(1, len(series)):
        (t0, v0), (t1, v1) = series[i - 1], series[i]
        if not v0 or not v1:
            continue
        # Two runs either side of a hole in the record are not "consecutive"
        # in any useful sense, and the pressure difference across the hole is
        # not an event. The mirror is backfilled in chunks, so this is real,
        # not hypothetical: a 2023 chunk sitting next to a 2026 chunk produced
        # a confident "intervention" dated 2023-07-18 with 1,141 days between
        # the two runs that defined it.
        try:
            gap_days = ((datetime.fromisoformat(t1) - datetime.fromisoformat(t0))
                        .total_seconds() / 86400.0)
        except ValueError:
            gap_days = 0.0
        if gap_days > STEP_MAX_GAP_DAYS:
            continue
        frac = (v1 - v0) / v0
        if abs(frac) < STEP_FRAC:
            continue
        mid = (v0 + v1) / 2.0
        # The step must HOLD: the median of the next few runs has to stay on
        # the new side of the midpoint between the old and new level.
        after = [v for _, v in series[i:i + 4]]
        if len(after) < 3:
            continue
        held = (st.median(after) > mid) if frac > 0 else (st.median(after) < mid)
        if not held:
            continue
        # ...and it must step off a level that was itself STABLE. Without
        # this, the trailing edge of a single clogged sample (a spike back
        # down to normal) reads as an intervention, and a spurious
        # intervention silently resets the column-age clock.
        before = [v for _, v in series[max(0, i - 4):i]]
        if len(before) < 3:
            continue
        was_stable = (st.median(before) > mid) if frac < 0 else (st.median(before) < mid)
        if not was_stable:
            continue
        steps.append({
            "at": t1,
            "previous_run": t0,
            "from_bar": round(v0, 1),
            "to_bar": round(v1, 1),
            "change_pct": round(frac * 100, 1),
            "direction": "drop" if frac < 0 else "rise",
        })
    return steps


def add_local_baselines(runs: list[dict]) -> None:
    """Attach a trailing local baseline to each run of an analytical method.

    The baseline for a run is the median plateau of the previous LOCAL_WINDOW
    runs of the same method. Trailing only — a run is never compared against
    its own future, so the number the panel shows is exactly the number a live
    watchdog would have had at the moment the run started.
    """
    hist: dict[str, list[float]] = {}
    for r in runs:
        m = r["method"]
        prev = hist.setdefault(m, [])
        if r.get("plateau_bar") and len(prev) >= LOCAL_MIN:
            base = st.median(prev[-LOCAL_WINDOW:])
            r["local_baseline_bar"] = round(base, 1)
            if base:
                r["pct_over_baseline"] = round(
                    (r["plateau_bar"] - base) / base * 100, 1)
        if r.get("plateau_bar"):
            prev.append(r["plateau_bar"])


def summarise_method(method: str, runs: list[dict]) -> dict:
    """Baseline, distribution and trend for one method."""
    runs = sorted(runs, key=lambda r: r["start"])
    plats = [(r["start"], r["plateau_bar"]) for r in runs if r.get("plateau_bar")]
    vals = [v for _, v in plats]
    med_dur = _median([r["duration_min"] for r in runs]) or 0.0

    # Baseline = median of the most recent quarter of runs (min 5), so it
    # tracks the current column rather than the whole history.
    tail = vals[-max(5, len(vals) // 4):] if vals else []
    baseline = _median(tail)

    # An analytical method holds a real gradient at real backpressure and has
    # a consistent duration. Priming / preparation procedures do neither, and
    # mixing them into a column baseline is meaningless.
    durs = [r["duration_min"] for r in runs]
    dur_stable = (len(durs) < 2 or (med_dur > 0 and st.pstdev(durs) / med_dur < 0.5))
    span = ((_pct(vals, 0.95) - _pct(vals, 0.05)) if len(vals) > 4 else None)
    controlled = span is not None and span < CONTROLLED_SPAN_BAR
    analytical = (bool(vals) and _median(vals) >= ANALYTICAL_MIN_BAR
                  and dur_stable and not controlled)

    out: dict = {
        "method": method,
        "n_runs": len(runs),
        "analytical": analytical,
        "pressure_controlled": bool(controlled),
        "duration_stable": bool(dur_stable),
        "median_duration_min": round(med_dur, 2),
        "first_run": runs[0]["start"] if runs else None,
        "last_run": runs[-1]["start"] if runs else None,
    }
    if vals:
        out.update({
            "baseline_bar": round(baseline, 1),
            "plateau_min_bar": round(min(vals), 1),
            "plateau_max_bar": round(max(vals), 1),
            "plateau_median_bar": round(st.median(vals), 1),
            "plateau_p05_bar": round(_pct(vals, 0.05), 1),
            "plateau_p95_bar": round(_pct(vals, 0.95), 1),
            "plateau_sd_bar": round(st.pstdev(vals), 2) if len(vals) > 1 else 0.0,
            "series": [{"start": t, "plateau_bar": round(v, 1)} for t, v in plats],
            "steps": detect_steps(plats),
        })
        # Trend over the runs since the last detected step, in bar/day.
        steps = out["steps"]
        since = steps[-1]["at"] if steps else (plats[0][0] if plats else None)
        seg = [(t, v) for t, v in plats if t >= since] if since else plats
        if len(seg) >= TREND_MIN_RUNS:
            t0 = datetime.fromisoformat(seg[0][0])
            xs = [(datetime.fromisoformat(t) - t0).total_seconds() / 86400.0
                  for t, _ in seg]
            ys = [v for _, v in seg]
            slope, r2 = (_linreg(xs, ys) if xs[-1] >= TREND_MIN_DAYS else (None, None))
        else:
            slope = r2 = None
        if slope is not None:
            out["trend_bar_per_day"] = round(slope, 2)
            out["trend_days"] = round(xs[-1], 2)
            out["trend_r2"] = round(r2, 3)
            out["trend_n_runs"] = len(seg)
            out["trend_since"] = since
            base0 = ys[0] or 1.0
            out["trend_pct_per_day"] = round(slope / base0 * 100, 3)
    return out


def flag_runs(runs: list[dict], methods: dict, envelopes: dict) -> list[dict]:
    """Produce the operator's triage list.

    Flags carry context as well as a number. In particular a run that is
    elevated within a few hours of a wash or preparation procedure is almost
    always re-equilibrating, not blocking: after the 2026-08-28 11:30 wash the
    column sat 20-24 % high for six hours and then settled by itself. Calling
    that a clog would train the operator to ignore the panel, so it is
    labelled rather than suppressed.
    """
    # Start times of the non-analytical / wash procedures, for that context.
    interventions = sorted(
        datetime.fromisoformat(r["start"]) for r in runs
        if not (methods.get(r["method"]) or {}).get("analytical")
        or (methods.get(r["method"]) or {}).get("pressure_controlled"))

    flags = []
    refs: dict[str, list[float]] = {}          # method -> current reference curve
    hist: dict[str, list[list[float]]] = {}    # method -> recent clean curves

    for r in runs:
        ms = methods.get(r["method"]) or {}
        # Column-pressure baselines only mean something for an analytical
        # separation. Aborts and Evotip faults are worth reporting on any
        # procedure, including a wash, so those checks are not gated.
        analytical = bool(ms.get("analytical"))
        base = r.get("local_baseline_bar") if analytical else None
        med_dur = ms.get("median_duration_min") or 0.0
        reasons, sev, kinds = [], None, []

        if analytical and r["peak_bar"] >= CEILING_BAR - 5:
            reasons.append(
                f"hit the {CEILING_BAR:.0f} bar pump cut-out "
                f"(peak {r['peak_bar']:.0f} bar) — this is what a clog abort looks like")
            sev = "critical"
            kinds.append("ceiling")

        if base and r.get("plateau_bar"):
            over = (r["plateau_bar"] - base) / base
            if over >= ELEVATED_FRAC:
                reasons.append(
                    f"column backpressure {r['plateau_bar']:.0f} bar is "
                    f"{over * 100:.0f}% above the {base:.0f} bar running baseline")
                sev = "critical" if over >= CRITICAL_FRAC else (sev or "elevated")
                kinds.append("high_pressure")

        if r.get("tip_pressure_max_bar", 0) >= TIP_SPIKE_BAR:
            reasons.append(
                f"low-pressure side reached {r['tip_pressure_max_bar']:.0f} bar — "
                "Evotip missing or not seated (not a column problem)")
            sev = sev or "elevated"
            kinds.append("tip")

        if (ms.get("duration_stable") and med_dur
                and r["duration_min"] < med_dur * ABORT_FRAC):
            reasons.append(f"ended after {r['duration_min']:.1f} min vs "
                           f"{med_dur:.1f} min normal — run aborted")
            sev = sev or "elevated"
            kinds.append("aborted")

        # First moment the run left the healthy envelope, measured against the
        # ROLLING reference: the lead time an in-run watchdog would have had.
        ref = refs.get(r["method"])
        if ref and r.get("curve") and med_dur:
            n = len(ref)
            grid = [k * med_dur / (n - 1) for k in range(n)]   # absolute minutes
            lo = max(1, int(BREACH_SKIP * n))
            mine = [_at_minute(r["curve"], r["duration_min"], t) for t in grid]
            breach, devs = None, []
            for i in range(lo, n):
                # Only where the run actually has data: a run that aborted at
                # minute 13 has nothing to say about minute 14.
                if grid[i] > r["duration_min"] or ref[i] is None or mine[i] is None:
                    continue
                devs.append(mine[i] - ref[i])
                if breach is not None or mine[i] <= ref[i] + BREACH_BAR:
                    continue
                # Must persist: the next few points have to stay breached too.
                fut = [mine[j] - ref[j] for j in range(i + 1, min(n, i + 1 + BREACH_PERSIST))
                       if mine[j] is not None and ref[j] is not None
                       and grid[j] <= r["duration_min"]]
                if len(fut) >= 2 and st.median(fut) > BREACH_BAR:
                    breach = grid[i]
            if breach is not None:
                end_of_warning = (r.get("t_ceiling_min")
                                  if r.get("t_ceiling_min") is not None
                                  else r["duration_min"])
                r["envelope_breach_min"] = round(breach, 2)
                r["envelope_lead_min"] = round(max(0.0, end_of_warning - breach), 2)
                r["envelope_lead_to"] = ("cut-out" if r.get("t_ceiling_min") is not None
                                         else "end of run")
                r["envelope_max_dev_bar"] = round(max(devs), 1) if devs else None
                # Ship the reference (and its absolute grid) for the runs the
                # panel actually draws, so the chart shows the real comparison.
                if r["peak_bar"] >= CEILING_BAR - 5:
                    r["ref_curve"] = [round(v, 1) for v in ref]
                    r["ref_grid_min"] = [round(t, 3) for t in grid]
                    r["curve_on_ref_grid"] = [
                        (round(v, 1) if v is not None and grid[k] <= r["duration_min"]
                         else None)
                        for k, v in enumerate(mine)]

        # Feed only clean runs of a duration typical for the method into the
        # rolling reference.
        if r.get("curve") and not reasons and med_dur and \
                abs(r["duration_min"] - med_dur) <= 0.15 * med_dur:
            h = hist.setdefault(r["method"], [])
            h.append((r["curve"], r["duration_min"]))
            del h[:-REF_WINDOW]
            if len(h) >= 3:
                n = min(len(c) for c, _ in h)
                grid = [k * med_dur / (n - 1) for k in range(n)]
                refs[r["method"]] = [
                    st.median([_at_minute(c, dd, t) for c, dd in h]) for t in grid]

        if reasons:
            r["flagged"] = True
            # Was there a wash / preparation shortly before this run?
            t = datetime.fromisoformat(r["start"])
            prior = [i for i in interventions if i < t]
            hours = ((t - prior[-1]).total_seconds() / 3600.0) if prior else None
            post = (hours is not None and hours <= POST_INTERVENTION_H
                    and "high_pressure" in kinds)
            if post and "ceiling" not in kinds:
                reasons.append(
                    f"note: a wash/preparation ran {hours:.1f} h earlier — "
                    "an elevated baseline this soon after is usually "
                    "re-equilibration, not a blockage")
            flags.append({
                "run": r["run"], "method": r["method"], "start": r["start"],
                "severity": sev, "kinds": kinds, "reasons": reasons,
                "post_intervention": bool(post),
                "hours_since_intervention": (round(hours, 1) if hours is not None else None),
                "plateau_bar": r.get("plateau_bar"), "peak_bar": r["peak_bar"],
                "baseline_bar": base, "pct_over_baseline": r.get("pct_over_baseline"),
                "duration_min": r["duration_min"],
                "tip_pressure_max_bar": r.get("tip_pressure_max_bar"),
                "envelope_breach_min": r.get("envelope_breach_min"),
                "envelope_lead_min": r.get("envelope_lead_min"),
            })
    return flags


def cross_check_bruker(runs: list[dict], flags: list[dict], path: str) -> dict:
    """Score the pressure signal against Compass's own failure log.

    This is the honesty check on the whole feature. Compass records an LC
    failure only as a post-mortem error string; if the pressure trace is worth
    anything it must (a) independently identify those same runs and (b) show
    the failure developing before the abort.
    """
    try:
        with open(path) as fh:
            bru = json.load(fh)
    except (OSError, ValueError):
        return {"available": False, "path": path}

    fails = bru.get("failures_recent") or []
    covered_to = (bru.get("summary") or {}).get("last_run") or ""
    # Only runs inside the window the Evosep logs actually cover can be scored.
    lo, hi = runs[0]["start"], runs[-1]["start"]

    def near(ts: str, tol_min: int = 5):
        """Match a Compass failure to an Evosep run by start time."""
        try:
            t = datetime.fromisoformat(ts.replace(" ", "T"))
        except ValueError:
            return None
        best, bestd = None, None
        for r in runs:
            d = abs((datetime.fromisoformat(r["start"]) - t).total_seconds())
            if bestd is None or d < bestd:
                best, bestd = r, d
        return (best, bestd / 60.0) if best and bestd <= tol_min * 60 else None

    matched, missed = [], []
    for f in fails:
        ts = f.get("start_date") or ""
        if not (lo[:16] <= ts.replace(" ", "T")[:16] <= hi[:16]):
            continue
        if f.get("category") not in ("LC pressure / clog",
                                     "Evotip missing / not picked up"):
            continue
        hit = near(ts)
        entry = {"compass_time": ts, "category": f.get("category"),
                 "file": f.get("fname"), "well": f.get("well")}
        if hit:
            r, dmin = hit
            entry.update({
                "evosep_run": r["run"], "offset_min": round(dmin, 1),
                "peak_bar": r["peak_bar"], "plateau_bar": r.get("plateau_bar"),
                "tip_pressure_max_bar": r.get("tip_pressure_max_bar"),
                "duration_min": r["duration_min"],
                "flagged_by_pressure": bool(r.get("flagged")),
                "envelope_lead_min": r.get("envelope_lead_min"),
            })
            matched.append(entry)
        else:
            missed.append(entry)

    clog = [m for m in matched if m["category"] == "LC pressure / clog"]
    tip = [m for m in matched if m["category"] != "LC pressure / clog"]
    # Critical flags raised inside the Compass-covered window that Compass
    # itself never recorded as a failure: silent excursions.
    silent = [f for f in flags
              if f["severity"] == "critical"
              and f["start"][:16] <= covered_to.replace(" ", "T")[:16]
              and not any(m.get("evosep_run") == f["run"] for m in matched)]

    return {
        "available": True,
        "compass_covers_to": covered_to,
        "evosep_window": [lo, hi],
        "matched": matched,
        "unmatched_compass_failures": missed,
        "clog_failures_in_window": len(clog),
        "clog_failures_detected": sum(1 for m in clog if m["flagged_by_pressure"]),
        "tip_failures_in_window": len(tip),
        "tip_failures_detected": sum(1 for m in tip if m["flagged_by_pressure"]),
        "silent_critical_excursions": silent,
    }


def column_age(runs: list[dict], methods: dict, column_events: list[dict]) -> dict:
    """How old is the column, and how far has its baseline moved.

    Uses a logged `column_change` maintenance event when STAN has one, and
    falls back to the largest detected downward baseline step — which is what
    a new column looks like in the pressure trace.
    """
    analytical = [m for m in methods.values() if m.get("analytical")]
    primary = max(analytical, key=lambda m: m.get("n_runs", 0), default=None)
    if not primary or not primary.get("series"):
        return {"known": False}

    series = primary["series"]
    # The inference is computed either way, so that when an operator has
    # logged the change we can show what the pressure trace would have said
    # — and a reader can see the inference was off rather than trusting it.
    # The MOST RECENT qualifying drop, not the largest one ever. This question
    # is "how old is the column fitted right now", so the boundary that
    # matters is the last one — and it is the same boundary `column_segments`
    # uses, so the two blocks agree.
    #
    # The old rule (largest drop) was invisible on a fortnight of log and
    # absurd on years of it: against the 2023 + 2026 mirror it named
    # 2023-07-18, i.e. a column three years old, because that drop happened to
    # be the deepest in the record. A wash can also produce the most recent
    # drop, which is why this stays an inference and is labelled as one.
    drops = [s for s in primary.get("steps", []) if s["direction"] == "drop"]
    inferred = max(drops, key=lambda s: s["at"])["at"] if drops else None

    installed, source, first_run_note = None, None, None
    if column_events:
        # The NEWEST logged change wins, always — it names the column that is
        # physically installed, which is the question being asked.
        #
        # An earlier version preferred whichever event had a resolvable
        # `first_run`, on the theory that anchored evidence is better. That
        # was wrong in the worst way: the timsTOF has two REAL changes
        # (2026-07-31 anchored to run 23229, and 2026-09-02), and preferring
        # the anchored one silently reported the column that had just been
        # REMOVED — a 33-day-old age for a column a few hours old. A missing
        # anchor is a reason to lower confidence in the date, never a reason
        # to skip back to a different physical column.
        newest = sorted(column_events, key=lambda e: e["event_date"])[-1]
        anchor = newest.get("first_run_resolved")
        if anchor:
            # The operator named the first run on the new column: that is the
            # actual boundary, not the day they got round to logging it.
            installed = anchor
            source = "logged column change, anchored to its first run"
            first_run_note = {
                "first_run": newest.get("first_run"),
                "matched_by": newest.get("first_run_match"),
                "event_date": newest["event_date"],
            }
        else:
            installed = newest["event_date"]
            source = "logged maintenance event"
            if newest.get("first_run"):
                first_run_note = {
                    "first_run": newest.get("first_run"),
                    "matched_by": None,
                    "unresolved": True,
                }
    elif inferred:
        installed = inferred
        source = "inferred from a step drop in baseline pressure"

    window_start = series[0]["start"]
    out: dict = {"known": bool(installed), "method": primary["method"],
                 "installed": installed, "source": source,
                 "window_first_run": window_start}
    if first_run_note:
        out["logged_first_run"] = first_run_note
    if len(column_events) > 1:
        # Say which of several logged events was used, so a wrong answer is
        # traceable to the row that caused it rather than looking like a bug.
        out["logged_events_considered"] = [
            {"event_date": e["event_date"], "first_run": e.get("first_run"),
             "anchored": bool(e.get("first_run_resolved")),
             "used": e is newest}
            for e in sorted(column_events, key=lambda e: e["event_date"])]
    if not installed:
        return out

    # How much log sits BEFORE the claimed install — the thing that decides
    # whether the claim is evidence or an artifact of a short window.
    if column_events:
        # A logged install is a stated boundary, not a floor — UNLESS the date
        # post-dates the whole pressure record, in which case it is the day
        # someone got round to logging the change rather than the day of it.
        # Real example: Brett logged the 2026-07-31 column change on
        # 2026-09-02, so `event_date` was 33 days late and there is not one
        # run on the "new" column in the log. Trusting that would be worse
        # than the inference it replaced, so it fails closed instead.
        # Anchored to a named first run -> the boundary is known to the
        # minute. Unanchored -> the event_date is usually a placeholder noon,
        # so the install is only good to the day. That is a weaker claim, not
        # a reason to look at a different column.
        out["confidence"] = "logged" if anchor else "inferred"
        if not anchor:
            out["caveat"] = (
                "No `first_run` is recorded for this column change, so the "
                "install time is the logged event date — typically a "
                "placeholder noon — and is accurate only to the day. Record "
                "the first run on the new column to anchor it to the minute.")
        if installed > series[-1]["start"]:
            out["confidence"] = "unverifiable"
            out["installed_is_lower_bound"] = True
            out["caveat"] = (
                f"The column was logged as installed {installed[:16]}, after "
                f"the last run in the pressure record "
                f"({series[-1]['start'][:16]}) — so no run on this column has "
                f"reached the log yet and nothing here measures it. Age and "
                f"wear become meaningful once its runs are mirrored.")
        if inferred and inferred[:10] != installed[:10]:
            # Record the disagreement rather than quietly discarding it: this
            # is how the 2026-08-19 answer (a glass-capillary swap) was shown
            # to be wrong once 2026-07-31 was known.
            delta = ((datetime.fromisoformat(inferred)
                      - datetime.fromisoformat(installed)).total_seconds() / 86400.0)
            out["inferred_installed"] = inferred
            out["inferred_disagrees_by_days"] = round(delta, 2)
            out["inference_note"] = (
                "The pressure trace's largest downward step is "
                f"{inferred[:10]}, {abs(delta):.1f} days "
                f"{'after' if delta > 0 else 'before'} the logged install. The "
                "logged date wins; the step is most likely a different "
                "intervention (capillary swap, wash, seal service).")
    else:
        prior_days = ((datetime.fromisoformat(installed)
                       - datetime.fromisoformat(window_start)).total_seconds()
                      / 86400.0)
        out["log_days_before_install"] = round(prior_days, 2)
        if prior_days < INSTALL_MIN_PRIOR_DAYS:
            out["confidence"] = "unverifiable"
            out["installed_is_lower_bound"] = True
            out["caveat"] = (
                f"Only {prior_days:.1f} days of pressure log precede this step, "
                f"so it cannot be distinguished from a capillary swap, a wash, "
                f"or a column installed before the log window opened on "
                f"{window_start[:10]}. Treat the install date as "
                f"'on or before' — and the age/injection counts below as "
                f"lower bounds. Pull more history "
                f"(copy_evosep_logs.bat /all) to resolve it.")
        else:
            out["confidence"] = "inferred"

    since = [s for s in series if s["start"] >= installed]
    if len(since) < 2:
        return out

    first = st.median([s["plateau_bar"] for s in since[:5]])
    last = st.median([s["plateau_bar"] for s in since[-5:]])
    t_install = datetime.fromisoformat(installed)
    t0 = datetime.fromisoformat(since[0]["start"])
    t1 = datetime.fromisoformat(since[-1]["start"])

    # Age runs from the INSTALL, not from the first run that happens to be in
    # the document. Those coincide only when the log reaches back past the
    # install; when it does not they diverge by however much history is
    # missing. On the 30-minute tick's 3-day window, an anchored install of
    # 2026-07-31 measured from `since[0]` reported a 3-day-old column that was
    # actually 33 days old — a number that looks perfectly reasonable and is
    # off by an order of magnitude.
    covers = series[0]["start"] <= installed
    out.update({
        "runs_since": len(since),
        "days_since": round((t1 - t_install).total_seconds() / 86400.0, 2),
        "observed_days": round((t1 - t0).total_seconds() / 86400.0, 2),
        "log_covers_install": covers,
        "baseline_at_install_bar": round(first, 1),
        "baseline_now_bar": round(last, 1),
        "baseline_change_pct": round((last - first) / first * 100, 1) if first else None,
    })
    if not covers:
        # Runs before the document's window are real but unseen, so every
        # count here is a floor and the "baseline at install" is really the
        # baseline at the start of the window.
        out["counts_are_lower_bounds"] = True
        out["coverage_note"] = (
            f"The document starts at {series[0]['start'][:16]}, after the "
            f"install at {installed[:16]}, so `runs_since`, "
            f"`injections_since` and `baseline_at_install_bar` describe only "
            f"the observed part of this column's life.")

    # Total injections on the column from the instrument's own lifetime
    # counter — the real number, including everything STAN never sees.
    tot = [r.get("total_analyses") for r in runs
           if r["start"] >= installed and r.get("total_analyses")]
    if tot:
        out["injections_since"] = max(tot) - min(tot)
        out["instrument_total_analyses"] = max(tot)
    return out


def daily_aggregates(runs: list[dict], flags: list[dict],
                     methods: dict) -> list[dict]:
    """One entry per calendar day, over the WHOLE history.

    This is what makes a multi-year ageing curve affordable: ~1,200 entries
    for 2023-onward instead of ~23,000 run records, at roughly 1/40th the
    bytes. Only analytical methods contribute a pressure number — utility
    procedures (Preparation, wash) run against a regulated setpoint and would
    drag the median away from the column.
    """
    analytical = {m for m, v in methods.items() if v.get("analytical")}
    flagged_by_day: dict[str, int] = {}
    for f in flags:
        day = str(f.get("start", ""))[:10]
        if day:
            flagged_by_day[day] = flagged_by_day.get(day, 0) + 1

    by_day: dict[str, list[dict]] = {}
    for r in runs:
        by_day.setdefault(r["start"][:10], []).append(r)

    out = []
    for day in sorted(by_day):
        rs = by_day[day]
        bars = [r["plateau_bar"] for r in rs
                if r["method"] in analytical and r.get("plateau_bar") is not None]
        entry = {
            "date": day,
            "n_runs": len(rs),
            "n_analytical": len(bars),
            "n_flagged": flagged_by_day.get(day, 0),
            "n_at_ceiling": sum(1 for r in rs
                                if (r.get("peak_bar") or 0) >= CEILING_BAR - 5),
        }
        if bars:
            entry["plateau_median_bar"] = round(_median(bars), 1)
            entry["plateau_p95_bar"] = round(_pct(bars, 0.95), 1)
        out.append(entry)
    return out


def column_segments(runs: list[dict], methods: dict,
                    column_events: list[dict]) -> list[dict]:
    """One summary per column fitted over the history.

    The point of keeping years of pressure: how long each column lasted, how
    many injections it took, and how far its baseline drifted before it was
    replaced. Boundaries come from logged `column_change` events when STAN has
    them, and otherwise from downward baseline steps — which carry the same
    caveat as `column_age`: a drop is only *evidence* of an install when the
    record reaches back far enough to show the previous column first.
    """
    analytical = [m for m in methods.values() if m.get("analytical")]
    primary = max(analytical, key=lambda m: m.get("n_runs", 0), default=None)
    if not primary or not primary.get("series"):
        return []
    series = primary["series"]

    if column_events:
        bounds = sorted(e["event_date"] for e in column_events)
        source = "logged maintenance event"
    else:
        bounds = sorted(s["at"] for s in primary.get("steps", [])
                        if s["direction"] == "drop")
        source = "inferred from a step drop in baseline pressure"
    # The first column on record starts with the record itself.
    starts = [series[0]["start"]] + [b for b in bounds if b > series[0]["start"]]

    out = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else None
        seg = [s for s in series
               if s["start"] >= start and (end is None or s["start"] < end)]
        if len(seg) < 2:
            continue
        bars = [s["plateau_bar"] for s in seg if s.get("plateau_bar") is not None]
        if not bars:
            continue
        first = _median(bars[:5])
        last = _median(bars[-5:])
        t0 = datetime.fromisoformat(seg[0]["start"])
        t1 = datetime.fromisoformat(seg[-1]["start"])
        entry = {
            "installed": start,
            "source": source if i or column_events else "start of the record",
            "retired": end,
            "n_runs": len(seg),
            "days": round((t1 - t0).total_seconds() / 86400.0, 2),
            "baseline_at_install_bar": round(first, 1),
            "baseline_at_end_bar": round(last, 1),
            "baseline_change_pct": round((last - first) / first * 100, 1) if first else None,
        }
        tot = [r.get("total_analyses") for r in runs
               if r["start"] >= start and (end is None or r["start"] < end)
               and r.get("total_analyses")]
        if tot:
            entry["injections"] = max(tot) - min(tot)
        # Same honesty rule as column_age: the first segment is bounded by the
        # start of the log, not by an observed install, so its age is a floor.
        if i == 0 and not column_events:
            entry["installed_is_lower_bound"] = True
            entry["days_is_lower_bound"] = True
        out.append(entry)
    return out


def load_column_catalogue(path: str | None) -> dict:
    """Read `config/columns.yml` — operating conditions and the part list.

    Optional, like everything else that reaches outside the log mirror. A
    missing file or a missing PyYAML degrades to "no catalogue", which costs
    the table its part numbers and its stated oven temperature but not its
    measurements.
    """
    for candidate in (path or "").split(":"):
        if not candidate or not os.path.exists(candidate):
            continue
        try:
            import yaml  # type: ignore
            with open(candidate) as fh:
                doc = yaml.safe_load(fh) or {}
            doc["_source"] = candidate
            return doc
        except Exception:
            continue
    return {}


def pressure_flow_table(runs: list[dict], methods: dict, segments: list[dict],
                        catalogue: dict) -> dict:
    """Expected backpressure per flow rate, per column, MEASURED.

    Brett's ask: "a table that records appropriate pressures for this column at
    1 ul/min, 0.5 ul/min etc". The point is that the only threshold today is
    the pump's 520 bar cut-out, which is a property of the PUMP. A column-
    specific expectation turns "412 bar is under 520, carry on" into "412 bar
    at 1.0 uL/min is 55 % above what this column does healthy" — the
    difference between catching a clog forming and noticing it after it has
    already killed the run.

    Both axes are measured: the Evosep logs `Actual-flow` beside `Pressure`,
    so flow is read, never assumed from the method's nominal rate. Bins fall
    where the data actually is and the measured flow is what gets reported.

    Grouped per column segment, so a column change resets the table rather
    than averaging a fresh column together with a spent one.

    TEMPERATURE. Backpressure is proportional to mobile-phase viscosity, and
    viscosity roughly halves between 25 C and 55 C — so the same healthy
    column reads about twice as high cold. Every figure here is therefore
    stamped with the oven temperature from the catalogue. That temperature is
    OPERATOR-REPORTED: it is recorded nowhere per run (verified — not in the
    Evosep procedure logs, not in HyStarMetadata.xml, not in analysis.tdf's
    GlobalMetadata, not in the Compass extract), so a run acquired with the
    oven off or still warming cannot be told apart from a restricted column by
    temperature. See `cold_start` for what the pressure itself says about it.
    """
    defaults = (catalogue or {}).get("defaults") or {}
    analytical = {m for m, v in methods.items() if v.get("analytical")}
    usable = [r for r in runs
              if r["method"] in analytical
              and r.get("plateau_bar") and r.get("plateau_flow_ul_min")]
    if not usable:
        return {}

    out: dict = {
        "catalogue_source": (catalogue or {}).get("_source"),
        "measured_at_oven_c": defaults.get("oven_c"),
        "oven_c_source": defaults.get("oven_c_source"),
        "oven_device": defaults.get("oven_device"),
        "temperature_recorded_per_run": False,
        "temperature_note": (
            "Oven temperature is not recorded anywhere per run — checked the "
            "Evosep procedure logs, HyStarMetadata.xml, analysis.tdf "
            "GlobalMetadata and the Compass extract. Every pressure below "
            "therefore ASSUMES the oven was at the stated temperature; a cold "
            "or warming column reads high for entirely benign reasons and "
            "cannot be distinguished from a partial blockage by temperature."),
        "flow_bin_ul_min": PRESSURE_BIN_UL,
        "columns": [],
    }

    for seg in segments or [{"installed": usable[0]["start"], "retired": None}]:
        lo, hi = seg["installed"], seg.get("retired")
        rs = [r for r in usable
              if r["start"] >= lo and (hi is None or r["start"] < hi)]
        if len(rs) < PRESSURE_MIN_BIN:
            continue
        rs.sort(key=lambda r: r["start"])
        entry = {
            "installed": lo,
            "retired": hi,
            "n_runs": len(rs),
            "installed_is_lower_bound": seg.get("installed_is_lower_bound", False),
            "bins": [],
        }
        # A segment is only ONE column if the record is continuous through it.
        # Against the part-backfilled mirror the "previous column" segment
        # spans a 1,063-day hole (2023-09-16 -> 2026-08-14) and therefore
        # averages every column fitted across three years — read as one
        # column's life it would say the current column is 36 % stiffer than
        # "the one it replaced", which the data does not support.
        gaps = []
        def _gap(a: str, b: str) -> float:
            return ((datetime.fromisoformat(b) - datetime.fromisoformat(a))
                    .total_seconds() / 86400.0)
        # Holes INSIDE the segment.
        for i in range(1, len(rs)):
            g = _gap(rs[i - 1]["start"], rs[i]["start"])
            if g > SEGMENT_GAP_WARN_DAYS:
                gaps.append({"kind": "interior", "from": rs[i - 1]["start"][:10],
                             "to": rs[i]["start"][:10], "days": round(g, 1)})
        # ...and, just as important, the segment's stated life extending well
        # beyond the runs that were actually observed. On the part-backfilled
        # mirror the "previous column" segment is stated as
        # 2023-07-13 -> 2026-07-31 but every run in it is from 2023: its last
        # observed run is 2023-09-16, 1,048 days before the boundary. Read as
        # "the column replaced on 2026-07-31" it would say the current column
        # is 36 % stiffer than the one it replaced. The data does not say that
        # — it compares a 2026 column with whatever was fitted in 2023.
        head = _gap(lo, rs[0]["start"])
        if head > SEGMENT_GAP_WARN_DAYS:
            gaps.append({"kind": "unobserved_start", "from": lo[:10],
                         "to": rs[0]["start"][:10], "days": round(head, 1)})
        if hi:
            tail = _gap(rs[-1]["start"], hi)
            if tail > SEGMENT_GAP_WARN_DAYS:
                gaps.append({"kind": "unobserved_end", "from": rs[-1]["start"][:10],
                             "to": hi[:10], "days": round(tail, 1)})
        entry["observed_from"] = rs[0]["start"][:16]
        entry["observed_to"] = rs[-1]["start"][:16]
        if gaps:
            entry["spans_gaps"] = gaps
            entry["is_one_column"] = False
            entry["gap_note"] = (
                "The observed runs do not cover this segment's stated life, "
                "so these figures do not describe the column at its "
                "boundaries — they describe whatever was fitted during the "
                "part that was observed. Do not compare them with another "
                "segment as though both were single columns.")
        # The catalogue only knows what is fitted NOW, so claim the part only
        # for the current (unretired) segment — the earlier column may have
        # been a different part and there is no label for it.
        cat_cols = (catalogue or {}).get("columns") or []
        if hi is None and cat_cols:
            c = cat_cols[0]
            entry["column"] = {k: c.get(k) for k in
                               ("id", "vendor", "model", "part_number",
                                "length_cm", "bore_um", "particle_um")}

        by_bin: dict[float, list[dict]] = {}
        for r in rs:
            b = round(round(r["plateau_flow_ul_min"] / PRESSURE_BIN_UL)
                      * PRESSURE_BIN_UL, 3)
            by_bin.setdefault(b, []).append(r)

        new_cut = {id(r) for r in rs[:PRESSURE_NEW_RUNS]}
        for b in sorted(by_bin):
            bin_runs = by_bin[b]
            if len(bin_runs) < PRESSURE_MIN_BIN:
                continue
            bars = [r["plateau_bar"] for r in bin_runs]
            flows = [r["plateau_flow_ul_min"] for r in bin_runs]
            med_bar, med_flow = _median(bars), _median(flows)
            row = {
                "flow_ul_min": round(med_flow, 3),
                "n_runs": len(bin_runs),
                "plateau_median_bar": round(med_bar, 1),
                "plateau_p5_bar": round(_pct(bars, 0.05), 1),
                "plateau_p95_bar": round(_pct(bars, 0.95), 1),
                "bar_per_ul_min": round(med_bar / med_flow, 1) if med_flow else None,
                "methods": sorted({r["method"] for r in bin_runs}),
            }
            when_new = [r["plateau_bar"] for r in bin_runs if id(r) in new_cut]
            when_now = [r["plateau_bar"] for r in bin_runs[-PRESSURE_NEW_RUNS:]]
            if when_new:
                row["when_new_bar"] = round(_median(when_new), 1)
                row["n_when_new"] = len(when_new)
            if when_now:
                row["now_bar"] = round(_median(when_now), 1)
            if when_new and when_now:
                a, z = _median(when_new), _median(when_now)
                row["wear_pct"] = round((z - a) / a * 100, 1) if a else None
            entry["bins"].append(row)

        # Darcy: pressure is linear in flow, so bar-per-(uL/min) should be
        # about the same in every bin for a healthy bed. A bin that breaks
        # that is evidence of a restriction rather than of normal resistance,
        # which is a different fault from "high everywhere".
        ratios = [r["bar_per_ul_min"] for r in entry["bins"] if r.get("bar_per_ul_min")]
        if len(ratios) >= 2:
            ref = _median(ratios)
            entry["bar_per_ul_min_median"] = round(ref, 1)
            for row in entry["bins"]:
                if row.get("bar_per_ul_min") and ref:
                    dev = (row["bar_per_ul_min"] - ref) / ref * 100
                    row["linearity_dev_pct"] = round(dev, 1)
                    row["breaks_linearity"] = abs(dev) > LINEARITY_TOL_PCT
        # The smell test needs a geometry. Only the current column has a label,
        # so for older segments the catalogue geometry is an ASSUMPTION and is
        # flagged as one — it still catches an order-of-magnitude problem,
        # which is all this is for.
        geom = entry.get("column") or (cat_cols[0] if cat_cols else None)
        if geom and entry["bins"]:
            chk = darcy_check(geom, entry["bins"], defaults.get("oven_c"))
            if chk:
                chk["geometry_assumed"] = "column" not in entry
                if chk["geometry_assumed"]:
                    chk["geometry_note"] = (
                        "No column label was recorded for this segment; the "
                        "current catalogue part's geometry is assumed.")
                entry["theory_check"] = chk
        if entry["bins"]:
            out["columns"].append(entry)

    out["cold_start"] = cold_start_effect(usable)
    return out


def darcy_check(col: dict, bins: list[dict], oven_c: float | None) -> dict:
    """Smell test only: does the measured table sit near Kozeny-Carman?

    dP = phi * eta * L * u / dp^2, with u the superficial velocity Q/A. This
    is NEVER the published number — phi (packing quality) and eta (viscosity
    of whatever water/ACN mix the method sits at mid-gradient, at an oven
    temperature nobody records) are each uncertain to tens of percent, so the
    calculation cannot be more than an order-of-magnitude check on the
    measurement. It earns its place by catching the case where the installed
    column is not the part we think it is: a factor-of-two disagreement means
    the geometry is wrong or the bed is genuinely restricted.
    """
    L, bore, dp = col.get("length_cm"), col.get("bore_um"), col.get("particle_um")
    if not (L and bore and dp) or not bins:
        return {}
    area = math.pi * (bore * 1e-6 / 2.0) ** 2          # m^2
    rows = []
    for b in bins:
        q = b["flow_ul_min"] * 1e-9 / 60.0             # m^3/s
        u = q / area                                    # m/s
        pred_pa = (DARCY_PHI * DARCY_ETA_PAS * (L / 100.0) * u) / (dp * 1e-6) ** 2
        pred = pred_pa / 1e5                            # bar
        rows.append({"flow_ul_min": b["flow_ul_min"],
                     "predicted_bar": round(pred, 1),
                     "measured_bar": b["plateau_median_bar"],
                     "measured_over_predicted": round(
                         b["plateau_median_bar"] / pred, 2) if pred else None})
    ratios = [r["measured_over_predicted"] for r in rows
              if r["measured_over_predicted"]]
    out = {"phi": DARCY_PHI, "eta_mpa_s": DARCY_ETA_PAS * 1000,
           "assumed_oven_c": oven_c, "rows": rows}
    if ratios:
        med = _median(ratios)
        out["median_ratio"] = round(med, 2)
        out["agrees"] = 0.5 <= med <= 2.0
        out["note"] = (
            f"Measured pressure is {med:.2f}x the Kozeny-Carman estimate. "
            + ("Within the uncertainty on packing constant and viscosity, so "
               "nothing to explain — the measured table stands."
               if 0.5 <= med <= 2.0 else
               "More than a factor of two out: either the installed column is "
               "not this part, or the bed is genuinely restricted."))
    return out


def attach_expected_pressure(runs: list[dict], reference: dict,
                             methods: dict | None = None) -> dict:
    """Stamp each run with an ABSOLUTE expected pressure and its excess.

    Why this exists, in one measurement. During the 2026-08-31 clog:

        23:32   plateau 417.8   trailing baseline 321.8   -> +29.8 %  critical
        00:43   plateau 408.3   trailing baseline 361.1   -> +13.1 %  elevated

    The plateau barely moved, but the trailing baseline climbed 321.8 -> 361.1
    *because of the clog* and absorbed 12 points of it, demoting the run a
    severity level. A sustained blockage therefore gets quieter the longer it
    lasts, and a slow creep never fires at all — the baseline rises with it the
    whole way. Any measure relative to recent runs has this built in.

    The fix is a reference that cannot chase: the column's own pressure at this
    flow, fixed for the segment.

    THE STATISTIC IS p5, NOT THE MEDIAN. The median of a bin includes the clog
    episodes that happened in it, so it sits above the healthy level and
    shrinks the excess it is meant to expose — the 1.5 uL/min bin has median
    349.5 bar against a healthy plateau of 320-325. The 5th percentile lands on
    the healthy floor instead: 320.9 bar for that bin, and 227.4 against a
    226-236 healthy floor at 1.0 uL/min. Both match to about a bar, on
    independent methods.

    Against the same two runs this gives +30.2 % and +27.2 % — the clog stays
    a clog.
    """
    cols = (reference or {}).get("columns") or []
    if not cols:
        return {"available": False,
                "reason": "no per-column pressure reference has enough runs yet"}
    bin_w = (reference or {}).get("flow_bin_ul_min") or PRESSURE_BIN_UL
    # The reference covers analytical methods only, so utility procedures have
    # nothing to be measured against and are not "missing" an expectation.
    analytical = ({m for m, v in (methods or {}).items() if v.get("analytical")}
                  if methods else None)
    stamped = 0
    missing: dict[str, int] = {}
    no_bin_flows: set[float] = set()
    for r in runs:
        flow, bar = r.get("plateau_flow_ul_min"), r.get("plateau_bar")
        if not flow or not bar:
            continue
        if analytical is not None and r["method"] not in analytical:
            continue
        seg = next((c for c in cols
                    if r["start"] >= c["installed"]
                    and (c.get("retired") is None or r["start"] < c["retired"])), None)
        # Deliberately NO fall-back to another column's reference. The column
        # fitted 2026-09-02 runs at 398.9-452.6 bar where its predecessor's
        # healthy floor was 320.9, so borrowing that reference would report
        # +24 % and +41 % "over expected" for a column hours old and fire
        # alarms purely from having swapped columns. The parts are not even
        # confirmed to match — no label was recorded for the earlier one.
        reason = None
        if not seg:
            reason = ("no reference for the column installed at this time — "
                      "too few runs on it yet to measure one, and another "
                      "column's reference would not describe it")
        else:
            b = next((x for x in seg["bins"]
                      if abs(x["flow_ul_min"] - flow) <= bin_w), None)
            if not b or not b.get("plateau_p5_bar"):
                reason = "no reference bin near this run's flow"
                no_bin_flows.add(round(flow, 2))
            else:
                exp = b["plateau_p5_bar"]
                r["expected_plateau_bar"] = exp
                r["pct_over_expected"] = round((bar - exp) / exp * 100, 1)
                stamped += 1
        if reason:
            r["expected_unavailable"] = reason
            missing[reason] = missing.get(reason, 0) + 1
    return {
        "available": stamped > 0,
        "n_runs_stamped": stamped,
        "n_runs_without_expectation": sum(missing.values()),
        "why_missing": [{"reason": k, "n_runs": v} for k, v in sorted(missing.items())],
        "flows_without_a_bin_ul_min": sorted(no_bin_flows)[:20],
        "no_cross_column_fallback": (
            "A run is never measured against a different column's reference. "
            "The column fitted 2026-09-02 runs at 398.9-452.6 bar where its "
            "predecessor's healthy floor was 320.9, so borrowing would report "
            "+24 % and +41 % over expected for a column hours old."),
        "statistic": "p5 of this column's own plateau distribution in the run's flow bin",
        "why_p5": (
            "The median of a bin includes the clog episodes that happened in "
            "it, so it sits above the healthy level and shrinks the very "
            "excess it is meant to expose. p5 lands on the healthy floor: "
            "320.9 bar against a 320-325 healthy plateau at 1.5 uL/min, and "
            "227.4 against 226-236 at 1.0 uL/min."),
        "why_absolute": (
            "A trailing baseline chases a sustained fault and absorbs it — "
            "measured at 12 percentage points during the 2026-08-31 clog, "
            "enough to demote a critical run to elevated. This reference is "
            "fixed for the column, so a clog stays a clog and a slow creep "
            "is visible."),
        "assumes_oven_c": (reference or {}).get("measured_at_oven_c"),
        "temperature_caveat": (
            f"Assumes the column oven is at "
            f"{(reference or {}).get('measured_at_oven_c')} C "
            f"({(reference or {}).get('oven_c_source')}). Oven temperature is "
            f"recorded nowhere per run, so a cold or warming column reads high "
            f"for a benign reason and cannot be told from a restriction."),
    }


def detect_column_changes(runs: list[dict], methods: dict) -> list[dict]:
    """Find column changes from the pressure record, two channels.

    A new column shows as a step DOWN in analytical resistance — bar per
    (uL/min), which is flow-normalised so methods at different throughputs are
    directly comparable — and, if a wash happens to bracket it, a step UP in
    wash flow at a fixed 400 bar.

    WHY THE RESISTANCE CHANNEL LEADS AND THE WASH CHANNEL ONLY CORROBORATES.
    Requiring both to move sounds stronger and is wrong here: measured across
    the two known changes, there was **not one qualifying wash in the 72 hours
    before the 2026-07-30 change**, so a both-channels rule vetoes the one
    boundary that was independently validated by a blind changepoint scan. The
    washes are ~142 points over three months against 1,079 analytical runs —
    too sparse to be a required witness.

    The discriminator that motivated two channels still holds, because it is
    the resistance channel that provides it: washing a fouled column lifts its
    wash flow temporarily but does NOT durably lower analytical resistance, so
    scanning resistance rejects wash recoveries by construction. That is what
    a naive wash-only detector got wrong, calling the 2026-08-24 and 08-31
    wash recoveries column changes.

    Calibration, from the two boundaries we know:

        2026-07-30 15:36   resistance 215.6 -> 186.3  -13.6%   no washes
        2026-09-02 11:00   resistance 228.5 -> 186.9  -18.2%   wash +29.3%
    """
    analytical = {m for m, v in methods.items() if v.get("analytical")}
    # Resistance for the method with the most runs: densest, least noisy.
    by_method: dict[str, list[dict]] = {}
    for r in runs:
        if (r.get("control_mode") == "flow" and r["method"] in analytical
                and r.get("plateau_bar") and r.get("plateau_flow_ul_min")):
            by_method.setdefault(r["method"], []).append(r)
    if not by_method:
        return []
    primary = max(by_method, key=lambda m: len(by_method[m]))
    rs = sorted(by_method[primary], key=lambda r: r["start"])
    series = [(r["start"], r["plateau_bar"] / r["plateau_flow_ul_min"]) for r in rs]

    washes = sorted(
        (r["start"], r["plateau_flow_ul_min"]) for r in runs
        if r.get("control_mode") == "pressure" and r.get("plateau_flow_ul_min")
        and r.get("plateau_bar") and r.get("setpoint_bar")
        and abs(r["plateau_bar"] - r["setpoint_bar"]) <= WASH_SETPOINT_TOL_BAR)

    out: list[dict] = []
    for s in detect_steps(series):
        if s["direction"] != "drop" or abs(s["change_pct"]) < COLUMN_DROP_PCT:
            continue
        t = datetime.fromisoformat(s["at"])
        lo = [f for w, f in washes
              if 0 < (t - datetime.fromisoformat(w)).total_seconds()
              <= COLUMN_WASH_WINDOW_H * 3600]
        hi = [f for w, f in washes
              if 0 <= (datetime.fromisoformat(w) - t).total_seconds()
              <= COLUMN_WASH_WINDOW_H * 3600]
        rise = None
        if lo and hi:
            a, b = _median(lo), _median(hi)
            rise = (b - a) / a * 100 if a else None
        if rise is not None and rise < COLUMN_WASH_RISE_PCT:
            # Washes bracket it and flow did NOT rise: evidence against.
            continue
        # RECOVERY TEST — the one that actually separates a column change from
        # a clog clearing. Both are a sustained drop, and both can lift wash
        # flow, so neither channel alone nor the pair of them can tell them
        # apart. What distinguishes them is WHERE the drop lands: clearing a
        # blockage returns resistance to the level the column already held,
        # while a new column goes materially BELOW anything the old one
        # sustained. Without this the detector called the 2026-08-19 capillary
        # swap and the 2026-08-29 clog recovery column changes, splitting one
        # real column into three.
        base = [v for w, v in series
                if RECOVERY_BASELINE_D[1] * 86400
                <= (t - datetime.fromisoformat(w)).total_seconds()
                <= RECOVERY_BASELINE_D[0] * 86400]
        # `series` is resistance, so detect_steps' from_bar/to_bar are already
        # bar per (uL/min). Dividing by flow again scaled them by 2/3 and made
        # every candidate look below baseline, so nothing was ever rejected.
        after_level = s["to_bar"]
        if base:
            settled = _median(base)
            if after_level >= settled * (1 - RECOVERY_TOL):
                continue
        out.append({
            "at": s["at"],
            "method": primary,
            "resistance_before": round(s["from_bar"], 1),
            "resistance_after": round(s["to_bar"], 1),
            "resistance_change_pct": s["change_pct"],
            "wash_flow_change_pct": round(rise, 1) if rise is not None else None,
            "n_washes_either_side": [len(lo), len(hi)],
            "provenance": "detected-confirmed" if rise is not None else "detected",
        })
    # Merge detections that are really one event seen twice.
    merged: list[dict] = []
    for c in sorted(out, key=lambda x: x["at"]):
        if merged and ((datetime.fromisoformat(c["at"])
                        - datetime.fromisoformat(merged[-1]["at"])).total_seconds()
                       / 86400.0) < COLUMN_MERGE_DAYS:
            if abs(c["resistance_change_pct"]) > abs(merged[-1]["resistance_change_pct"]):
                merged[-1] = c
            continue
        merged.append(c)
    return merged


def runs_remaining(washes: list[tuple[int, float]], done: int) -> dict:
    """How many more injections before this column hits its replacement point.

    Wash flow at a fixed 400 bar is the column's permeability measured under
    an identical condition every time, so its decline is the cleanest wear
    signal available. Fit flow against injections and extrapolate to the flow
    at which a column actually gets replaced.

    THE TRIGGER IS RELATIVE. Measured on the retired column: fresh 2.267,
    replaced at 1.738 -- 76.7 % of fresh. Expressed as a fraction it needs no
    absolute value and no geometry, so it transfers to a column type never
    seen before, the same principle as `wear_pct_of_fresh`.

    THE GATE IS ON THE SIGNAL, NEVER ON INJECTION COUNT. A prospective system
    cannot know that 450 injections is halfway. Retrospective fits on the
    retired column show why the signal is the right gate: at 266 injections
    the column read 2.358, *above* fresh, and the flat fit through that
    extrapolated to 31,744 injections remaining. Requiring a real decline
    before extrapolating suppresses that regime automatically.

    A RANGE, NEVER A POINT. The slope's standard error is propagated into the
    extrapolation. "Roughly 200-400 injections left" is honest; "313" is not.
    """
    out = {"gate_open": False, "n_washes": len(washes)}
    if len(washes) < RUNS_REMAINING_MIN_WASHES:
        out["gate_reason"] = (
            f"only {len(washes)} qualifying washes on this column; "
            f"{RUNS_REMAINING_MIN_WASHES} needed before a trend means anything")
        return out
    fresh = _median([f for _, f in washes[:WASH_FRESH_N]])
    now = _median([f for _, f in washes[-WASH_FRESH_N:]])
    trigger = fresh * WASH_REPLACE_FRAC
    out.update({"fresh_flow_ul_min": round(fresh, 3),
                "now_flow_ul_min": round(now, 3),
                "trigger_flow_ul_min": round(trigger, 3),
                "pct_of_fresh": round(now / fresh * 100, 1)})
    decline = (fresh - now) / fresh
    if decline < RUNS_REMAINING_MIN_DECLINE:
        out["gate_reason"] = (
            f"wash flow is {now / fresh * 100:.1f} % of fresh — less than the "
            f"{RUNS_REMAINING_MIN_DECLINE * 100:.0f} % decline needed before "
            f"an extrapolation carries information. Not a statement that the "
            f"column is fine; only that it is too early to project.")
        return out

    xs = [float(x) for x, _ in washes]
    ys = [f for _, f in washes]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        out["gate_reason"] = "all washes at the same injection count"
        return out
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    inter = my - slope * mx
    resid = [y - (inter + slope * x) for x, y in zip(xs, ys)]
    if n > 2:
        se = math.sqrt(sum(r * r for r in resid) / (n - 2) / sxx)
    else:
        se = float("inf")
    out["slope_per_injection"] = round(slope, 6)
    out["slope_se"] = round(se, 6)
    if slope >= 0 or slope + se >= 0:
        out["gate_reason"] = (
            "the fitted decline is not distinguishable from flat "
            f"(slope {slope:.2e} +/- {se:.2e} per injection), so any "
            "projection would be extrapolating noise")
        return out

    def at(sl):
        return (trigger - inter) / sl if sl else None
    lo, hi = at(slope - se), at(slope + se)   # steeper -> sooner
    ends = sorted(v for v in (at(slope), lo, hi) if v is not None)
    left = [e - done for e in (ends[0], ends[-1])]
    # The standard error captures uncertainty in the FIT, not in the model.
    # Assuming the decline stays linear is itself an assumption, and measured
    # retrospectively this projection lands 10-30 % low. So widen the range to
    # at least that, or a perfectly straight run of washes would report a
    # spuriously precise answer — on noiseless input the SE is zero and the
    # "range" collapses to a point, which is exactly what must not be shown.
    mid = (left[0] + left[1]) / 2.0
    floor = abs(mid) * RUNS_REMAINING_MIN_SPREAD / 2.0
    left = [min(left[0], mid - floor), max(left[1], mid + floor)]
    left = [round(v) for v in left]
    out.update({
        "gate_open": True,
        "estimate_low": max(0, left[0]),
        "estimate_high": max(0, left[1]),
        "injections_done": done,
        "basis": "linear fit of wash flow against injections on this column",
    })
    return out


def detect_column_changes_by_wash(runs: list[dict]) -> list[dict]:
    """Brett's rule: a column change is a wash that returns flow to FRESH.

    A system-and-column wash holds a 400 bar setpoint, so flow is permeability
    measured identically every time. Washing a fouled column lifts its flow
    but cannot restore it to the fresh-column level; only a replacement can.

    LEVEL, NOT STEP SIZE. The 2026-08-31 wash recovery jumped +18.7 %, LARGER
    than the real 2026-07-30 column change's +10.4 %. Size cannot separate
    them. Level can, and because "fresh" is each column's own first washes the
    rule needs no maintenance log to calibrate against — which is what lets it
    run over years nobody recorded. It found twelve changes across 2023-07 to
    2026-09; ten had no logged counterpart.

    The level is a median of consecutive washes, not one reading: run-to-run
    scatter swings from 0.25 % to 6.6 % month to month and its 90th percentile
    is 6-9 % almost everywhere, against a 3-8 % signal, so a single wash
    crosses the threshold by chance in the noisy months.
    """
    ws = sorted(
        (r for r in runs
         if r.get("control_mode") == "pressure" and r.get("plateau_bar")
         and r.get("plateau_flow_ul_min") and r.get("setpoint_bar")
         and abs(r["plateau_bar"] - r["setpoint_bar"]) <= WASH_SETPOINT_TOL_BAR),
        key=lambda r: r["start"])
    if len(ws) < WASH_FRESH_N * 2:
        return []
    out: list[dict] = []
    fresh = lowest = None
    since: list[float] = []
    for i, w in enumerate(ws):
        if i and ((datetime.fromisoformat(w["start"])
                   - datetime.fromisoformat(ws[i - 1]["start"])).days
                  > SEGMENT_GAP_WARN_DAYS):
            fresh, lowest, since = None, None, []
            continue
        if fresh is None:
            since.append(w["plateau_flow_ul_min"])
            if len(since) >= WASH_FRESH_N:
                fresh = lowest = _median(since)
            continue
        lvl = _median([x["plateau_flow_ul_min"]
                       for x in ws[max(0, i - WASH_LEVEL_SMOOTH + 1):i + 1]])
        lowest = min(lowest, lvl)
        if lowest < fresh * WASH_DECLINE_FRAC and lvl >= fresh * WASH_RECOVER_FRAC:
            out.append({
                "at": w["start"],
                "provenance": "detected-wash-level",
                "prev_fresh_flow": round(fresh, 3),
                "fell_to_pct_of_fresh": round(lowest / fresh * 100, 1),
                "recovered_to_pct_of_fresh": round(lvl / fresh * 100, 1),
            })
            fresh, lowest, since = None, None, [w["plateau_flow_ul_min"]]
    return out


def column_lifetimes(runs: list[dict], methods: dict, events: list[dict],
                     detected: list[dict], washes: list[dict] | None = None) -> dict:
    """Injections per column: how long each lasted and how much work it did.

    Brett's ask — the number of injections on the previous column and on the
    one fitted now. It is the first figure this system has produced that gives
    a new column something to be judged against.

    Boundaries come from two places and they answer different questions. A
    logged `column_change` says WHICH column was fitted; the pressure record
    says WHEN. They are not interchangeable: the 2026-09-02 event is logged at
    a placeholder noon six hours before the swap actually happened, and taking
    that timestamp put three washes and two analytical runs on the wrong side
    — which produced a wrong clinical conclusion, not just an off-by-a-few
    count. So a detected timestamp wins over a logged one, and the
    disagreement is recorded rather than smoothed away.
    """
    analytical = {m for m, v in methods.items() if v.get("analytical")}
    inj = sorted((r for r in runs if r["method"] in analytical
                  and r.get("control_mode") == "flow"),
                 key=lambda r: r["start"])
    if not inj:
        return {"available": False, "reason": "no analytical runs"}

    logged = sorted((e["event_date"] for e in events or []), key=str)
    bounds: list[dict] = []
    for c in detected:
        near = [t for t in logged
                if abs((datetime.fromisoformat(t) - datetime.fromisoformat(c["at"]))
                       .total_seconds()) / 3600.0 <= COLUMN_LOGGED_TOL_H]
        b = {"at": c["at"], "provenance": c["provenance"],
             "resistance_before": c.get("resistance_before"),
             "resistance_after": c.get("resistance_after"),
             "resistance_change_pct": c["resistance_change_pct"],
             "wash_flow_change_pct": c.get("wash_flow_change_pct")}
        if near:
            b["provenance"] = "logged+detected"
            b["logged_at"] = near[0]
            b["logged_minus_detected_hours"] = round(
                (datetime.fromisoformat(near[0])
                 - datetime.fromisoformat(c["at"])).total_seconds() / 3600.0, 2)
        bounds.append(b)
    # A logged change the pressure record did not confirm still marks a column.
    for t in logged:
        if not any(abs((datetime.fromisoformat(t) - datetime.fromisoformat(b["at"]))
                       .total_seconds()) / 3600.0 <= COLUMN_LOGGED_TOL_H
                   for b in bounds):
            bounds.append({"at": t, "provenance": "logged",
                           "resistance_change_pct": None,
                           "wash_flow_change_pct": None})
    bounds.sort(key=lambda b: b["at"])

    starts = [{"at": inj[0]["start"], "provenance": "start-of-record"}] + [
        b for b in bounds if b["at"] > inj[0]["start"]]
    cols = []
    for i, b in enumerate(starts):
        lo = b["at"]
        hi = starts[i + 1]["at"] if i + 1 < len(starts) else None
        mine = [r for r in inj if r["start"] >= lo and (hi is None or r["start"] < hi)]
        if not mine:
            continue
        t0 = datetime.fromisoformat(mine[0]["start"])
        t1 = datetime.fromisoformat(mine[-1]["start"])
        days = (t1 - t0).total_seconds() / 86400.0
        gaps = [((datetime.fromisoformat(mine[j]["start"])
                  - datetime.fromisoformat(mine[j - 1]["start"])).total_seconds() / 86400.0)
                for j in range(1, len(mine))]
        big = [round(g, 1) for g in gaps if g > SEGMENT_GAP_WARN_DAYS]
        # Wear as a fraction of THIS column's own value when new. That is the
        # only basis that transfers: 187.5 -> 239.9 is +28 % whether the
        # column is a PepSep Max, a different geometry, or a type never seen
        # before. Every absolute number measured today -- the 187.5 fresh
        # resistance, the 320.9 bar p5 floor, the 2.26 uL/min wash flow --
        # belongs to ONE column type and must never be applied to another.
        # A ratio to the column's own install value carries no such baggage.
        # An aborted or partial procedure still counts as an injection but
        # has no usable plateau — `plateau_bar` is None while
        # `plateau_flow_ul_min` is present, and dividing crashed the extract.
        # Keep it in the injection count (it was an injection) and out of the
        # resistance, and COUNT the exclusions so a systematically-null field
        # cannot hide as "no data".
        usable = [r for r in mine
                  if r.get("plateau_bar") and r.get("plateau_flow_ul_min")]
        res = [r["plateau_bar"] / r["plateau_flow_ul_min"] for r in usable]
        fresh = _median(res[:COLUMN_FRESH_RUNS]) if len(res) >= 3 else None
        nowv = _median(res[-COLUMN_FRESH_RUNS:]) if len(res) >= 3 else None
        entry = {
            "installed": lo,
            "retired": hi,
            "is_current": hi is None,
            "boundary_provenance": b["provenance"],
            "injections": len(mine),
            "days": round(days, 2),
            "injections_per_day": round(len(mine) / days, 1) if days > 0.5 else None,
            "first_run": mine[0]["start"],
            "last_run": mine[-1]["start"],
        }
        if len(usable) < len(mine):
            # Named to match the field team-lead wired into the panel. We
            # both patched the deployed copy within 20 seconds of each other
            # and mine silently won; adopting their name rather than mine is
            # how the two stay reconciled.
            entry["runs_without_plateau"] = len(mine) - len(usable)
        if fresh:
            entry["fresh_resistance_bar_per_ul_min"] = round(fresh, 1)
            entry["now_resistance_bar_per_ul_min"] = round(nowv, 1)
            entry["wear_pct_of_fresh"] = round((nowv - fresh) / fresh * 100, 1)
            if b["provenance"] == "start-of-record":
                # Its install is not in the record, so "fresh" is just where
                # the record opens — mid-life, and an unknown amount of wear
                # already done.
                entry["fresh_is_start_of_record"] = True
                entry["wear_is_lower_bound"] = True
        for k in ("logged_at", "logged_minus_detected_hours",
                  "resistance_before", "resistance_after",
                  "resistance_change_pct", "wash_flow_change_pct"):
            if b.get(k) is not None:
                entry[k] = b[k]
        if big:
            entry["spans_gaps_days"] = big
            entry["injections_is_lower_bound"] = True
            entry["gap_note"] = (
                "The mirror is missing runs inside this column's life, so the "
                "count is a floor. A 'column' spanning a gap is usually "
                "several columns concatenated.")
        if hi is None and len(mine) < COLUMN_PROVISIONAL_RUNS:
            entry["provisional"] = True
            entry["provisional_note"] = (
                f"Only {len(mine)} injections so far — reported because it is "
                f"the column in use, but too few to characterise.")
        # Wash-flow projection, for the column in use.
        if hi is None and washes:
            mw = [(sum(1 for r in mine if r["start"] <= w["t"]), w["flow_ul_min"])
                  for w in washes
                  if w["t"] >= lo and (hi is None or w["t"] < hi)]
            entry["runs_remaining"] = runs_remaining(mw, len(mine))

        # The instrument's own lifetime counter is an independent witness.
        # Only worth comparing when most runs actually carry the counter --
        # it lives in maintenance-info.txt, which a partial mirror may not
        # have. Comparing 2 counter readings against 982 runs would report a
        # 980-injection "disagreement" that is purely missing input.
        tot = [r.get("total_analyses") for r in mine if r.get("total_analyses")]
        if len(tot) >= max(5, 0.5 * len(mine)):
            entry["counter_delta"] = max(tot) - min(tot)
            d = entry["counter_delta"] - entry["injections"]
            if abs(d) > max(5, 0.1 * entry["injections"]):
                entry["counter_disagrees_by"] = d
                entry["counter_note"] = (
                    "The Evosep's own counter and the runs mirrored here "
                    "differ; the counter includes procedures the mirror has "
                    "not received, so it is usually the higher of the two.")
        cols.append(entry)

    cur = next((c for c in reversed(cols) if c["is_current"]), None)
    prev = next((c for c in reversed(cols) if not c["is_current"]), None)

    # The distribution is the point of the exercise: it turns one 982-injection
    # figure into something to plan against. Retired columns only, and only
    # those whose install is actually observed — a `start-of-record` column's
    # life is a floor and would drag the median down.
    done = [c for c in cols if not c["is_current"]
            and c["boundary_provenance"] != "start-of-record"]
    dist = None
    if done:
        d_days = sorted(c["days"] for c in done)
        d_inj = sorted(c["injections"] for c in done)
        dist = {
            "n_columns": len(done),
            "days": {"median": round(_median(d_days), 1),
                     "min": round(d_days[0], 1), "max": round(d_days[-1], 1)},
            "injections": {"median": round(_median(d_inj)),
                           "min": d_inj[0], "max": d_inj[-1]},
            "note": ("Retired columns with an observed install only. Days and "
                     "injections disagree on purpose — days conflates a busy "
                     "month with a quiet one, injections is the axis a "
                     "purchase is planned against."),
        }
        if cur:
            n = cur["injections"]
            med = _median(d_inj)
            dist["current_in_context"] = {
                "injections_so_far": n,
                "median_predecessor_injections": round(med),
                "pct_of_median": round(n / med * 100, 1) if med else None,
                "shorter_predecessors": sum(1 for x in d_inj if x < n),
                "of": len(d_inj),
            }
    return {
        "available": True,
        "n_columns": len(cols),
        "current": cur,
        "previous": prev,
        "lifetime_distribution": dist,
        "columns": cols,
        "limits": [
            "A boundary is `logged+detected`, `detected-confirmed`, "
            "`detected`, `logged` or `start-of-record`. Counts either side of "
            "a weaker boundary are correspondingly weaker — never mix them "
            "without reading the provenance.",
            "A logged change says WHICH column; the pressure says WHEN. Where "
            "both exist the detected time is used and the difference is "
            "reported as `logged_minus_detected_hours`.",
            "Counts include analytical, flow-controlled injections only — not "
            "washes, Preparation or Diagnostics.",
            "These lifetimes are treated as COMPLETE because the operator "
            "changes columns only on failure, never on a schedule — so a "
            "column change always leaves the decline-then-recovery signature "
            "the wash-level rule detects. That completeness rests on stated "
            "practice, not on the data proving no change was missed. If the "
            "practice changes, proactively-replaced columns become invisible "
            "and these become lower bounds again.",
            "`runs_remaining` extrapolates wash flow to 76.7 % of this "
            "column's own fresh value. That threshold was derived from the "
            "one column it was then tested on, so the retrospective fit is "
            "partly circular and is NOT validation. The column fitted "
            "2026-09-02 is the first genuine prospective test: its fresh wash "
            "flow is measured, so the rule can be scored honestly when it "
            "eventually fires.",
            "The projection under-predicts on the data we have — it warns "
            "early rather than late, which is the correct direction for a "
            "warning to err, but it is not a schedule.",
        ],
    }


def attach_pct_over_fresh(runs: list[dict], lifetimes: dict) -> dict:
    """Stamp each run with its excess over ITS OWN column's fresh resistance.

    The column-type-agnostic basis. `pct_over_expected` measures against the
    p5 of the column's own flow bin, which is a healthy FLOOR but is drawn
    from the column's whole life and so carries its ageing: the retired
    column's p5 was 213.9 bar per (uL/min) against a fresh value of 185.5, so
    an end-of-life run read +9.1 % on that basis and +25.8 % on this one — a
    factor of nearly three, in the direction of under-calling wear.

    Fresh is measured, not assumed, and measured three times independently:
    186.1, 185.5 and 186.9 on three separate columns, agreeing to +/-0.7 %.
    A denominator that reproducible makes the ratio a number rather than a
    trend, and it needs no geometry, no flow bin and no column type — so it
    transfers to a column this system has never seen.
    """
    cols = (lifetimes or {}).get("columns") or []
    fresh_by = [(c["installed"], c.get("retired"),
                 c.get("fresh_resistance_bar_per_ul_min"),
                 c.get("wear_is_lower_bound", False)) for c in cols]
    n = 0
    for r in runs:
        bar, flow = r.get("plateau_bar"), r.get("plateau_flow_ul_min")
        if not bar or not flow:
            continue
        hit = next(((f, lb) for lo, hi, f, lb in fresh_by
                    if f and r["start"] >= lo and (hi is None or r["start"] < hi)), None)
        if not hit:
            continue
        fresh, lower_bound = hit
        r["pct_over_fresh"] = round((bar / flow - fresh) / fresh * 100, 1)
        r["fresh_basis_bar_per_ul_min"] = fresh
        if lower_bound:
            r["pct_over_fresh_is_lower_bound"] = True
        n += 1
    return {
        "available": n > 0,
        "n_runs_stamped": n,
        "basis": "this column's own resistance when new (median of its first runs)",
        "why": (
            "Column-type agnostic: a ratio to the column's own install value "
            "carries no geometry, no flow bin and no absolute number, so it "
            "transfers to a column type never measured before. The fresh "
            "value is reproducible — 186.1, 185.5, 186.9 bar per (uL/min) on "
            "three separate columns, +/-0.7 %."),
        "vs_pct_over_expected": (
            "`pct_over_expected` measures against the p5 of the column's own "
            "flow bin, a healthy floor drawn from the whole life and so "
            "carrying its ageing. On the retired column an end-of-life run "
            "read +9.1 % that way and +25.8 % this way. Thresholds calibrated "
            "on one basis do not carry to the other."),
    }


def wash_flow_trend(runs: list[dict], segments: list[dict]) -> dict:
    """Flow through the column during pressure-controlled washes.

    The cleanest column-health signal on the instrument, and the only one that
    needs no correction of any kind. A system-and-column wash holds a 400 bar
    setpoint, so the pressure is pinned by definition and the FLOW the pump
    achieves is the column's permeability, measured under an identical
    condition every time.

    That means none of the things that complicate the other signals apply:
      * no trailing baseline, so nothing can chase a developing fault
      * no flow normalisation, because flow IS the measurement
      * no reference table, because every point is taken at the same pressure

    Consecutive washes getting WORSE is the clinically useful pattern: a dirty
    column improves as you wash it, a blocked one does not.

    Runs are assigned to a column by install TIMESTAMP, never by date. The
    2026-09-02 change happened mid-morning, so four washes earlier that day
    belong to the column being removed — grouping by date puts them in the new
    column's figures and makes a fresh column look restricted.
    """
    pts = []
    for r in runs:
        if r.get("control_mode") != "pressure":
            continue
        sp, bar, flow = (r.get("setpoint_bar"), r.get("plateau_bar"),
                         r.get("plateau_flow_ul_min"))
        if not sp or not bar or not flow:
            continue
        # Only runs that actually reached the setpoint are comparable.
        if abs(bar - sp) > WASH_SETPOINT_TOL_BAR:
            continue
        seg = next((s["installed"] for s in reversed(segments or [])
                    if r["start"] >= s["installed"]
                    and (s.get("retired") is None or r["start"] < s["retired"])), None)
        if seg is None and segments:
            # A wash before the first segment's nominal start is still on the
            # column that segment describes; the start is just where the
            # record happens to begin.
            seg = segments[0]["installed"]
        pts.append({"t": r["start"], "flow_ul_min": round(flow, 3),
                    "pressure_bar": round(bar, 1), "method": r["method"],
                    "segment_installed": seg})
    if not pts:
        return {"available": False,
                "reason": "no pressure-controlled runs reached their setpoint"}
    pts.sort(key=lambda p: p["t"])

    by_seg: dict = {}
    for p in pts:
        by_seg.setdefault(p["segment_installed"], []).append(p["flow_ul_min"])
    segs = []
    for k in sorted(by_seg, key=lambda x: (x is None, x)):
        f = by_seg[k]
        segs.append({"installed": k, "n": len(f),
                     "median_ul_min": round(_median(f), 3),
                     "p5_ul_min": round(_pct(f, 0.05), 3),
                     "p95_ul_min": round(_pct(f, 0.95), 3),
                     "min_ul_min": round(min(f), 3),
                     "max_ul_min": round(max(f), 3)})
    # A segment whose wash flows STEP part-way through is evidence that its
    # install timestamp is wrong -- the runs before the step belong to the
    # previous column. Real case: the 2026-09-02 change is logged at a
    # placeholder noon (05:00 local) but actually happened around 11:00, so
    # three washes at 1.735-1.748 (the blocked column being removed) and two
    # at 2.252-2.258 (the new one) land in the same group and halve its
    # apparent permeability.
    for entry in segs:
        pl = [p for p in pts if p["segment_installed"] == entry["installed"]]
        if len(pl) < 3:
            continue
        # Split at the largest jump between CONSECUTIVE washes, then check the
        # levels either side really differ. Maximising the difference of
        # medians instead lands a point early or late when the two groups are
        # close in size -- it put the boundary at 2 runs where the
        # discontinuity plainly sits at 3.
        best = None
        jump = max(range(1, len(pl)),
                   key=lambda i: abs(pl[i]["flow_ul_min"] - pl[i - 1]["flow_ul_min"]))
        a = _median([x["flow_ul_min"] for x in pl[:jump]])
        b = _median([x["flow_ul_min"] for x in pl[jump:]])
        if a and abs(b - a) / a > WASH_SEGMENT_STEP_FRAC:
            best = (jump, abs(b - a) / a, pl[jump]["t"], a, b)
        if best:
            i, frac, t, a, b = best
            days_in = ((datetime.fromisoformat(t)
                        - datetime.fromisoformat(pl[0]["t"])).total_seconds()
                       / 86400.0)
            entry["boundary_warning"] = {
                "step_at": t,
                "days_into_segment": round(days_in, 2),
                "n_before": i,
                "median_before_ul_min": round(a, 3),
                "median_after_ul_min": round(b, 3),
                "change_pct": round((b - a) / a * 100, 1),
                "note": (
                    f"Wash flow steps {(b - a) / a * 100:+.0f}% "
                    f"{days_in:.1f} days into this column's runs. Close to "
                    f"the start (hours) that usually means the install "
                    f"timestamp is a placeholder and the {i} run(s) before "
                    f"{t[:16]} are still the previous column. Well into the "
                    f"segment (days) it is more likely real: a column doing "
                    f"most of its fouling early."),
            }

    setpoints = sorted({p["pressure_bar"] for p in pts})
    return {
        "available": True,
        "setpoint_bar": _median([r["setpoint_bar"] for r in runs
                                 if r.get("control_mode") == "pressure"
                                 and r.get("setpoint_bar")]),
        "tolerance_bar": WASH_SETPOINT_TOL_BAR,
        "n": len(pts),
        "observed_pressure_range_bar": [setpoints[0], setpoints[-1]],
        "series": pts,
        "by_segment": segs,
        "limits": [
            "Comparable only with other wash flows. The washes run near "
            "2.3 uL/min against 100 SPD's 1.5, and viscous heating at the "
            "higher flow lowers apparent resistance — so a wash flow must "
            "never be turned into a bar-per-(uL/min) figure and set beside an "
            "analytical method's.",
            "Higher flow at the same pressure means a more permeable column. "
            "The trend direction is what carries information; the absolute "
            "value depends on the wash solvent as well as the column.",
            "Runs are assigned to a column by install timestamp, not by date "
            "— a change mid-day would otherwise credit that morning's washes "
            "to the wrong column.",
        ],
    }


def cold_start_effect(runs: list[dict], gap_h: float = 8.0) -> dict:
    """Do runs after an idle gap read higher than runs mid-sequence?

    This is the false-positive mode that matters for clog alerting: with the
    oven temperature unrecorded, a column that is cold or still warming looks
    exactly like a partial blockage. If the effect is real in the data, an
    alerter should stand down on the first run after a gap; if it is not, that
    caution is unnecessary. Either way it should be measured, not assumed.
    """
    by_method: dict[str, list[dict]] = {}
    for r in runs:
        by_method.setdefault(r["method"], []).append(r)
    first, rest = [], []
    for rs in by_method.values():
        rs.sort(key=lambda r: r["start"])
        for i, r in enumerate(rs):
            if not r.get("local_baseline_bar"):
                continue
            rel = r["plateau_bar"] / r["local_baseline_bar"]
            if i == 0:
                continue
            prev = datetime.fromisoformat(rs[i - 1]["start"])
            gap = (datetime.fromisoformat(r["start"]) - prev).total_seconds() / 3600.0
            (first if gap >= gap_h else rest).append(rel)
    if len(first) < 5 or len(rest) < 20:
        return {"measurable": False,
                "note": "too few runs after an idle gap to measure"}
    a, b = _median(first), _median(rest)
    excess = ((a - b) / b * 100) if b else 0.0
    thresh = ELEVATED_FRAC * 100
    return {
        "measurable": True,
        "gap_hours": gap_h,
        "n_after_gap": len(first),
        "n_in_sequence": len(rest),
        "median_rel_after_gap": round(a, 4),
        "median_rel_in_sequence": round(b, 4),
        "excess_pct": round(excess, 2),
        "elevated_threshold_pct": thresh,
        "material_for_alerting": excess >= thresh / 2.0,
        "note": (
            "Ratio of each run's plateau to its own trailing baseline, so "
            "column ageing cancels. "
            + (f"The first run after an idle gap reads {excess:.1f}% high, "
               f"against an 'elevated' threshold of {thresh:.0f}% — so the "
               f"cold-column effect is real but roughly "
               f"{thresh / excess:.0f}x too small to trip an alert on its "
               f"own. No stand-down after a gap is needed on this evidence."
               if 0 < excess < thresh / 2.0 else
               f"The first run after an idle gap reads {excess:.1f}% high "
               f"against an 'elevated' threshold of {thresh:.0f}%. That is "
               f"large enough to raise flags by itself, so an alerter should "
               f"stand down on the first run after a gap.")),
    }


def load_sample_index(instrument: str | None) -> list[dict]:
    """Per-injection acquisitions from STAN, for attributing a pressure step.

    `sample_health` rather than `runs`: `runs` holds only searched QC, while
    `sample_health` has a row per injection — 467 rows against 317 Evosep
    procedures over the same fortnight, which is what makes a 91 % join
    possible. Optional, like every other reach outside the log mirror.
    """
    if not instrument:
        return []
    try:
        from stan.db_pg import _connect  # type: ignore
        with _connect() as pg, pg.cursor() as cur:
            cur.execute(
                "SELECT run_name, run_date FROM sample_health"
                " WHERE instrument = %s ORDER BY run_date DESC LIMIT 20000",
                (instrument,))
            rows = cur.fetchall()
    except Exception:
        return []
    out = []
    for name, when in rows:
        t = to_log_time(when)
        m = SAMPLE_WELL_RE.search(name or "")
        if t and m:
            out.append({"run_name": name, "t": t, "well": m.group(1)})
    return out


def attribute_run(run: dict, index: list[dict]) -> dict | None:
    """Match one Evosep procedure to the acquisition it ran.

    Joined on vial position AND time: the MS starts a couple of minutes after
    the LC (measured median +2.53 min, range +2.5 to +10.2 over 289 matched
    pairs), so the acquisition is the one in the same well that starts just
    after this procedure did.
    """
    well, t0 = run.get("well"), run.get("start")
    if not well or not t0 or not index:
        return None
    start = datetime.fromisoformat(t0)
    best, best_dt = None, None
    for row in index:
        if row["well"] != well:
            continue
        dt = (datetime.fromisoformat(row["t"]) - start).total_seconds() / 60.0
        if -ATTRIB_TOL_MIN <= dt <= ATTRIB_TOL_MIN and (best_dt is None or abs(dt) < abs(best_dt)):
            best, best_dt = row, dt
    if not best:
        return None
    out = {"run_name": best["run_name"], "well": well,
           "ms_after_lc_min": round(best_dt, 2)}
    m = SUBMISSION_RE.match(best["run_name"])
    if m:
        out["submission"] = f"PROT_{int(m.group(1)):04d}"
    return out


def sample_pressure_impact(runs: list[dict], methods: dict, segments: list[dict],
                           events: list[dict], index: list[dict]) -> dict:
    """Injections after which the column's baseline pressure stayed higher.

    Brett's ask: flag samples that degrade column health, to track bad samples
    and the people who submit them.

    THE DISTINCTION THE FEATURE RESTS ON. A viscous or particulate sample that
    runs high and then lets the baseline return is annoying; a sample after
    which every SUBSEQUENT run sits higher has left something on the column.
    Only the second is damage, so the statistic is the step in baseline across
    an injection, never the injection's own pressure.

    WHY A STEP AND NOT A PER-RUN DELTA. Computing (median of the k after) -
    (median of the k before) for every run and flagging whatever crosses a
    threshold looks right and is not: a single step lands inside the window of
    k consecutive runs, so one clog is reported k times with near-identical
    magnitudes and nothing says which injection caused it. Measured on real
    data that turned 4 real events into 18 flags. `detect_steps` already
    localises a sustained level change to the run where it begins — with the
    "must hold" and "stepped off a stable level" requirements this feature
    needs anyway — so the step is found once and attributed to one injection.

    HONEST LIMITS, and they ship with the numbers because this feature names
    people:
      * It is CORRELATION. The injection present when the level rose is the
        best available attribution, not proof of cause; a sample that merely
        coincides with a clog already forming will be blamed for it.
      * RUN ORDER CONFOUNDS IT. A submission that ran late in a column's life
        sits on a steeper part of the drift curve, so ordering alone can make
        one submitter look worse than another running identical material.
      * Therefore the aggregate reports injections beside total delta, and a
        per-injection rate — a 96-injection plate accumulates more than an
        8-injection one for reasons that have nothing to do with sample
        quality.
    """
    analytical = {m for m, v in methods.items() if v.get("analytical")}
    ev_times = []
    for e in events or []:
        t = e.get("event_date")
        if t:
            ev_times.append(t)

    flags: list[dict] = []
    for seg in segments or [{"installed": None, "retired": None}]:
        lo, hi = seg.get("installed"), seg.get("retired")
        for method in sorted(analytical):
            rs = [r for r in runs
                  if r["method"] == method and r.get("plateau_bar")
                  and r.get("plateau_flow_ul_min")
                  and (lo is None or r["start"] >= lo)
                  and (hi is None or r["start"] < hi)]
            if len(rs) < IMPACT_MIN_RUNS:
                continue
            rs.sort(key=lambda r: r["start"])
            flows = [r["plateau_flow_ul_min"] for r in rs]
            # Flow-controlled methods only. Where the pump holds PRESSURE
            # (the washes, setpoint 400 bar) the pressure cannot step, so a
            # step there would be meaningless.
            if st.pstdev(flows) > IMPACT_FLOW_SD_MAX:
                continue
            flow = _median(flows)
            at2run = {r["start"]: r for r in rs}
            for s in detect_steps([(r["start"], r["plateau_bar"]) for r in rs]):
                if s["direction"] != "rise":
                    continue
                run = at2run.get(s["at"])
                if not run:
                    continue
                # Conditioning: a fresh column settles, and that is not a
                # sample's fault.
                if rs.index(run) < IMPACT_CONDITIONING_RUNS:
                    continue
                # An intervention nearby explains the step better than any
                # sample does.
                near_event = any(
                    abs((datetime.fromisoformat(s["at"])
                         - datetime.fromisoformat(t)).total_seconds()) / 3600.0
                    <= IMPACT_EVENT_GUARD_H for t in ev_times)
                if near_event:
                    continue
                delta = (s["to_bar"] - s["from_bar"]) / flow if flow else 0.0
                if delta < IMPACT_MIN_BAR_PER_UL:
                    continue
                f = {
                    "at": s["at"],
                    "method": method,
                    "column_installed": lo,
                    "from_bar": s["from_bar"],
                    "to_bar": s["to_bar"],
                    "change_pct": s["change_pct"],
                    "delta_bar_per_ul_min": round(delta, 1),
                    "flow_ul_min": round(flow, 3),
                    "well": run.get("well"),
                }
                who = attribute_run(run, index)
                if who:
                    f.update({k: who[k] for k in
                              ("run_name", "submission", "ms_after_lc_min")
                              if k in who})
                    if CONTROL_RUN_RE.search(who["run_name"]):
                        f["is_control"] = True
                        f.pop("submission", None)
                # Confidence is about how well the step is pinned to THIS
                # injection, not about how large it is.
                f["confidence"] = (
                    "high" if who and who.get("submission") and delta >= 2 * IMPACT_MIN_BAR_PER_UL
                    else "medium" if who else "low")
                if not who:
                    f["attribution_note"] = (
                        "No acquisition matched this procedure by vial and "
                        "time, so the step is located but the sample is not "
                        "identified.")
                flags.append(f)

    flags.sort(key=lambda f: f["at"])
    by_sub: dict[str, dict] = {}
    for r in runs:
        who = attribute_run(r, index) if r.get("well") else None
        sub = (who or {}).get("submission")
        if sub:
            by_sub.setdefault(sub, {"submission": sub, "n_injections": 0,
                                    "n_flagged": 0, "total_delta": 0.0})
            by_sub[sub]["n_injections"] += 1
    for f in flags:
        sub = f.get("submission")
        if sub and not f.get("is_control"):
            e = by_sub.setdefault(sub, {"submission": sub, "n_injections": 0,
                                        "n_flagged": 0, "total_delta": 0.0})
            e["n_flagged"] += 1
            e["total_delta"] += f["delta_bar_per_ul_min"]
    subs = []
    for e in by_sub.values():
        if not e["n_flagged"]:
            continue
        e["total_delta_bar_per_ul_min"] = round(e.pop("total_delta"), 1)
        e["delta_per_injection"] = (
            round(e["total_delta_bar_per_ul_min"] / e["n_injections"], 2)
            if e["n_injections"] else None)
        subs.append(e)
    subs.sort(key=lambda e: -(e["delta_per_injection"] or 0))

    return {
        "n_flags": len(flags),
        "n_attributed": sum(1 for f in flags if f.get("submission")),
        "threshold_bar_per_ul_min": IMPACT_MIN_BAR_PER_UL,
        "flags": flags,
        "by_submission": subs,
        "limits": [
            "Correlation, not cause: the injection present when the baseline "
            "rose is the best available attribution, not proof. A sample that "
            "coincides with a clog already forming will be blamed for it.",
            "Run order confounds comparison: a submission that ran late in a "
            "column's life sits on a steeper part of the drift curve, so "
            "ordering alone can make one submitter look worse than another "
            "running identical material.",
            "Compare submissions by `delta_per_injection`, never by "
            "`total_delta_bar_per_ul_min` — a 96-injection plate accumulates "
            "more than an 8-injection one regardless of sample quality.",
        ],
    }


def wear_counters(runs: list[dict]) -> dict:
    """Instrument-lifetime wear counters and their observed rate."""
    with_info = [r for r in runs if r.get("total_analyses")]
    if not with_info:
        return {}
    with_info.sort(key=lambda r: r["start"])
    first, last = with_info[0], with_info[-1]
    days = ((datetime.fromisoformat(last["start"])
             - datetime.fromisoformat(first["start"])).total_seconds() / 86400.0) or 1.0

    out = {
        "total_analyses": last["total_analyses"],
        "analyses_in_window": last["total_analyses"] - first["total_analyses"],
        "window_days": round(days, 2),
        "analyses_per_day": round(
            (last["total_analyses"] - first["total_analyses"]) / days, 1),
        "loop_volume_ul": last.get("loop_volume_ul"),
        "method_lifetime_counts": last.get("method_lifetime_counts") or {},
    }
    seals_now = last.get("pump_seal_ml") or {}
    seals_then = first.get("pump_seal_ml") or {}
    if seals_now:
        out["pump_seal_ml"] = {
            k: {"ml": v,
                "ml_in_window": v - seals_then.get(k, v),
                "ml_per_day": round((v - seals_then.get(k, v)) / days, 2)}
            for k, v in sorted(seals_now.items())
        }
    return out


def to_log_time(value) -> str | None:
    """Normalise a timestamp onto the clock the Evosep logs actually use.

    The Evosep writes naive LOCAL time in its folder names and traces, while
    STAN stores `run_date` and `event_date` as timezone-aware UTC. Mixing them
    is wrong twice over: comparing an aware and a naive datetime raises, and
    comparing their ISO strings silently skews every result by the UTC offset
    — 7-8 hours here, which is enough to put a column change on the wrong day
    and to land an anchor in the middle of the previous night's runs.

    Converting to local time and then dropping the tzinfo puts everything on
    the instrument's clock, which is the one the log folder names use.
    """
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    elif isinstance(value, datetime):
        dt = value
    else:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat()


def load_column_events(instrument: str | None) -> list[dict]:
    """Fetch logged `column_change` events from STAN, if STAN is importable.

    Optional by design: the extractor must run on a box that only has the log
    mirror. A missing STAN, a missing DB or a corrupt DB all degrade to "no
    logged events" rather than failing the extract.
    """
    if not instrument:
        return []
    try:
        from stan.db import get_events  # type: ignore
        evs = get_events(instrument, limit=200)
        out = [e for e in evs if e.get("event_type") == "column_change"]
    except Exception:
        return []
    # Everything downstream compares against naive local log timestamps.
    for e in out:
        local = to_log_time(e.get("event_date"))
        if local:
            e["event_date"] = local
        anchor = resolve_first_run(e.get("first_run"), instrument)
        if anchor:
            e["first_run_resolved"], e["first_run_match"] = anchor
    return [e for e in out if e.get("event_date")]


def resolve_first_run(text: str | None,
                      instrument: str) -> tuple[str, str] | None:
    """Turn an operator's free-text "first run on the new column" into a time.

    The field is deliberately free text, because both of these are things a
    person will reasonably type and both identify the run:

        20260731_HE50_60-spd-dia-new-zdf-column_S1-A1_1_23232.d
        23232

    So: try the run name as written (with and without the `.d`), then the
    trailing injection counter, which is the last `_`-separated number in
    every raw name this facility produces. Anything unresolvable returns None
    and the caller falls back to inference — a typo must never fail the
    extract.

    Note the anchor comes from STAN's `runs` table, not from the Evosep logs:
    the Evosep names its folders `<method>_<date>_<time>` and has no idea what
    the mass spectrometer called the acquisition.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        from stan.db_pg import _connect  # type: ignore
        with _connect() as pg, pg.cursor() as cur:
            bare = text[:-2] if text.lower().endswith(".d") else text
            cur.execute(
                "SELECT run_date FROM runs WHERE instrument = %s"
                " AND (run_name = %s OR run_name = %s) ORDER BY run_date LIMIT 1",
                (instrument, text, bare + ".d"))
            row = cur.fetchone()
            if row and row[0]:
                return to_log_time(row[0]), "run name"
            m = re.search(r"(\d{3,})\s*$", bare)
            if m:
                cur.execute(
                    "SELECT run_date, run_name FROM runs WHERE instrument = %s"
                    " AND run_name ~ %s ORDER BY run_date LIMIT 1",
                    (instrument, rf"_{m.group(1)}(\.d)?$"))
                row = cur.fetchone()
                if row and row[0]:
                    return (to_log_time(row[0]),
                            f"injection counter {m.group(1)}")
    except Exception:
        return None
    return None


# ── Driver ────────────────────────────────────────────────────────────────

def newest_host_dir(root: str) -> str | None:
    """Pick the newest `<HOST>_<timestamp>` mirror under the root."""
    try:
        cands = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    except OSError:
        return None
    if not cands:
        return None
    # The trailing _YYYYMMDD_HHMMSS sorts lexically in time order.
    return sorted(cands)[-1]


def host_mirrors(root: str, host_dir: str | None = None) -> list[str]:
    """Every log folder belonging to one instrument PC, lowest priority first.

    Two layouts coexist under the root and both must be read:

    * `<HOST>_mirror` — the stable incremental mirror (2026-09-02 onward).
      This is the canonical source: one folder, topped up in place, holding
      the whole 2023-onward history.
    * `<HOST>_<YYYYMMDD_HHMMSS>` — the legacy one-folder-per-pull layout,
      each holding only that pull's window. The very first pull lives in one
      of these, and while the mirror is still being seeded it may hold a run
      the mirror has not reached yet.

    Reading only the newest folder is the failure this guards against, in
    either layout: the legacy pulls defaulted to a 30-day window, so a routine
    pull after a full one silently cut the extract from two years to one
    month — and after the 2026-09-02 rewrite, a name-based "newest" pick would
    miss `<HOST>_mirror` altogether and read a stale timestamped copy.

    Returned lowest-priority first, with the mirror LAST, so that when the
    caller de-duplicates by run folder the mirror's copy is the one that
    wins. Overlap between the two layouts is expected — the mirror was seeded
    from the first timestamped pull — and de-duplication, not exclusion, is
    what keeps those runs from being counted twice.
    """
    if host_dir:
        return [host_dir]
    try:
        entries = [d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))]
    except OSError:
        return []

    mirrors = [(m.group("host"), d) for d in entries
               for m in [HOST_MIRROR_RE.match(d)] if m]
    stamped = [(m.group("host"), m.group("ts"), d) for d in entries
               for m in [HOST_TS_RE.match(d)] if m]

    if mirrors:
        # A stable mirror exists: it names the host we care about. If somehow
        # several do, the most recently written one wins.
        host = max(mirrors, key=lambda t: os.path.getmtime(os.path.join(root, t[1])))[0]
    elif stamped:
        host = max(stamped, key=lambda t: t[1])[0]
    else:
        # Unstamped, un-mirrored layout (hand-made folder): old behaviour.
        return sorted(entries)[-1:] if entries else []

    out = [d for _, ts, d in
           sorted((t for t in stamped if t[0] == host), key=lambda t: t[1])]
    out += [d for h, d in mirrors if h == host]
    return out


def main(argv=None) -> int:
    global CURVE_POINTS

    ap = argparse.ArgumentParser(
        description="Extract Evosep One column-health signals to JSON.")
    ap.add_argument("--root", default=DEFAULT_ROOT,
                    help=f"Evosep log mirror root (default: {DEFAULT_ROOT})")
    ap.add_argument("--host-dir", default=None,
                    help="Specific <HOST>_<timestamp> folder (default: newest)")
    ap.add_argument("--out", default=None, help="Output JSON file (default: stdout)")
    ap.add_argument("--instrument", default=None,
                    help="STAN instrument name, for column_change cross-reference")
    ap.add_argument("--bruker-json", default=DEFAULT_BRUKER_JSON,
                    help="Bruker maintenance JSON, to score pressure flags "
                         "against Compass's own failure log "
                         f"(default: {DEFAULT_BRUKER_JSON})")
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                    help="Only extract runs starting on or after this date. "
                         "The full instrument history is several GB; a cron "
                         "should window it rather than re-reading everything.")
    ap.add_argument("--max-runs", type=int, default=None,
                    help="Keep only the newest N runs (applied after --since).")
    ap.add_argument("--curve-points", type=int, default=CURVE_POINTS,
                    help=f"Points per downsampled curve (default: {CURVE_POINTS})")
    ap.add_argument("--indent", type=int, default=None,
                    help="Pretty-print the JSON with this indent")
    ap.add_argument("--runs-window-days", type=int, default=RUNS_WINDOW_DAYS,
                    metavar="N",
                    help="Days of per-run detail kept in the output; the full "
                         "history is still analysed and summarised in `daily` "
                         "and `columns`. 0 keeps every run — use that for "
                         f"analysis, not for publishing (default: {RUNS_WINDOW_DAYS})")
    ap.add_argument("--keep-all-runs", action="store_true",
                    help="Within the window, keep run records that carry no "
                         "curve and no flag. No consumer reads those; this is "
                         "for offline analysis.")
    ap.add_argument("--columns-yml", default=DEFAULT_COLUMNS_YML,
                    help="Column catalogue (operating temperature + part "
                         f"list) (default: {DEFAULT_COLUMNS_YML})")
    ap.add_argument("--reference-from", default=None, metavar="JSON",
                    help="Take the per-column pressure reference from a "
                         "previously published document instead of computing "
                         "it from this extract. The 30-minute tick's 3-day "
                         "window is far too thin to build a reference, but a "
                         "per-run `pct_over_expected` is exactly what it needs "
                         "— so the daily full extract computes the table and "
                         "the short ticks carry it forward.")
    ap.add_argument("--max-doc-mb", type=float, default=MAX_DOC_MB,
                    metavar="MB",
                    help="Fail if the document exceeds this. 0 disables the "
                         f"check (default: {MAX_DOC_MB})")
    args = ap.parse_args(argv)
    CURVE_POINTS = args.curve_points

    mirrors = host_mirrors(args.root, args.host_dir)
    if not mirrors:
        print(f"error: no host folder under {args.root}", file=sys.stderr)
        return 2
    host = mirrors[-1]

    # <root>/<HOST_ts>/<serial>/<method>_<date>_<time>/
    # Union over every mirror of this host: a later, narrower pull must not
    # discard the history an earlier full pull captured. Mirrors are walked
    # oldest-first so the newest copy of a run folder wins — a run that was
    # still being written when an early pull ran is complete in a later one.
    found: dict[tuple[str, str], str] = {}
    serial_set: set[str] = set()
    for mirror in mirrors:
        host_path = os.path.join(args.root, mirror)
        try:
            serials_here = sorted(d for d in os.listdir(host_path)
                                  if os.path.isdir(os.path.join(host_path, d)))
        except OSError:
            continue
        for serial in serials_here:
            serial_set.add(serial)
            base = os.path.join(host_path, serial)
            for name in sorted(os.listdir(base)):
                rd = os.path.join(base, name)
                if not os.path.isdir(rd):
                    continue
                if args.since:
                    m = RUN_DIR_RE.match(name)
                    if not m or m.group("date") < args.since:
                        continue
                found[(serial, name)] = rd

    serials = sorted(serial_set)
    runs: list[dict] = []
    for (serial, name), rd in found.items():
        rec = extract_run(rd, name)
        if rec:
            rec["serial"] = serial
            runs.append(rec)

    runs.sort(key=lambda r: r["start"])
    if args.max_runs:
        runs = runs[-args.max_runs:]
    if not runs:
        print(f"error: no parseable runs under {host_path}", file=sys.stderr)
        return 2

    by_method: dict[str, list[dict]] = {}
    for r in runs:
        by_method.setdefault(r["method"], []).append(r)

    methods = {m: summarise_method(m, rs) for m, rs in by_method.items()}
    add_local_baselines(runs)

    # Reference envelopes: the 5 consecutive runs of a method with the lowest
    # spread in plateau pressure — i.e. the calmest healthy stretch on record.
    envelopes: dict[str, dict] = {}
    for m, rs in by_method.items():
        rs = [r for r in sorted(rs, key=lambda x: x["start"])
              if r.get("plateau_bar") and r.get("curve")]
        if len(rs) < 5:
            continue
        best, best_sd = None, None
        for i in range(len(rs) - 4):
            w = rs[i:i + 5]
            sd = st.pstdev([x["plateau_bar"] for x in w])
            if best_sd is None or sd < best_sd:
                best, best_sd = w, sd
        env = build_envelope(runs, m, best)
        if env:
            env["reference_plateau_sd_bar"] = round(best_sd, 2)
            envelopes[m] = env

    flags = flag_runs(runs, methods, envelopes)
    col_events = load_column_events(args.instrument)
    validation = cross_check_bruker(runs, flags, args.bruker_json)

    # Curves are the bulk of the document. Keep them where the UI actually
    # draws one — flagged runs, and a recent tail per method for comparison —
    # and drop the rest so the cached JSON stays small enough to ship.
    keep = {f["run"] for f in flags}
    for rs in by_method.values():
        for r in sorted(rs, key=lambda x: x["start"])[-CURVE_KEEP_TAIL:]:
            keep.add(r["run"])
    for r in runs:
        if r["run"] not in keep:
            r.pop("curve", None)

    wear = wear_counters(runs)
    # The lifetime counter blocks are identical-ish on every run and dominate
    # the document; keep the running total per run (it is what dates a column
    # change) and hold the full breakdown once, in `wear`.
    for r in runs:
        r.pop("method_lifetime_counts", None)
        r.pop("pump_seal_ml", None)
        r.pop("loop_volume_ul", None)

    # Whole-history summaries, computed BEFORE any windowing so they see every
    # run. These are what let the panel draw a multi-year curve from a document
    # that stays under a megabyte.
    daily = daily_aggregates(runs, flags, methods)
    columns = column_segments(runs, methods, col_events)
    catalogue = load_column_catalogue(args.columns_yml)
    pressure_ref = pressure_flow_table(runs, methods, columns, catalogue)
    # An absolute expectation per run. Prefer a carried-forward reference:
    # a 3-day window cannot build one, but it can be measured against one.
    ref_for_expected = pressure_ref
    ref_source = "this extract"
    if args.reference_from and os.path.exists(args.reference_from):
        try:
            with open(args.reference_from) as fh:
                prev = (json.load(fh) or {}).get("pressure_reference") or {}
            if prev.get("columns"):
                ref_for_expected, ref_source = prev, args.reference_from
        except Exception:
            pass
    expected_meta = attach_expected_pressure(runs, ref_for_expected, methods)
    expected_meta["reference_from"] = ref_source
    # Flags are scored against the TRAILING baseline, which a sustained fault
    # drags upward with it. Carrying the absolute figure onto each flag lets a
    # consumer threshold on something that cannot be chased.
    _exp = {r["run"]: r for r in runs if r.get("expected_plateau_bar")}
    for f in flags:
        src = _exp.get(f.get("run"))
        if src:
            f["expected_plateau_bar"] = src["expected_plateau_bar"]
            f["pct_over_expected"] = src["pct_over_expected"]
    # Two independent detectors: analytical resistance (dense, precise timing)
    # and Brett's wash-level rule (sparse, but needs no maintenance log and
    # reaches back over years). Merge, preferring the richer provenance where
    # both see the same event.
    detected_changes = detect_column_changes(runs, methods)
    for c in detect_column_changes_by_wash(runs):
        near = next((d for d in detected_changes
                     if abs((datetime.fromisoformat(d["at"])
                             - datetime.fromisoformat(c["at"])).total_seconds())
                     / 86400.0 < COLUMN_MERGE_DAYS), None)
        if near:
            near["provenance"] = "detected-both-channels"
            near.update({k: v for k, v in c.items()
                         if k.startswith(("prev_fresh", "fell_to", "recovered_to"))})
        else:
            detected_changes.append(c)
    detected_changes.sort(key=lambda d: d["at"])
    wash = wash_flow_trend(runs, columns)
    lifetimes = column_lifetimes(runs, methods, col_events, detected_changes,
                                 (wash or {}).get("series"))
    fresh_meta = attach_pct_over_fresh(runs, lifetimes)
    sample_index = load_sample_index(args.instrument)
    impact = sample_pressure_impact(runs, methods, columns, col_events, sample_index)
    col_block = column_age(runs, methods, col_events)
    # Counted over the whole record, so `summary` describes the instrument's
    # history rather than whatever slice happens to be shipped. `runs_window`
    # is where the trimmed counts live.
    full_n_runs, full_first = len(runs), runs[0]["start"]
    full_n_flagged = len(flags)
    full_n_critical = sum(1 for f in flags if f["severity"] == "critical")
    full_n_at_ceiling = sum(d["n_at_ceiling"] for d in daily)

    # ── Windowing ────────────────────────────────────────────────────────
    window = None
    if args.runs_window_days and args.runs_window_days > 0:
        cut = (datetime.fromisoformat(runs[-1]["start"])
               - timedelta(days=args.runs_window_days)).isoformat()
        window = {"days": args.runs_window_days, "from": cut}
        runs = [r for r in runs if r["start"] >= cut]
        flags = [f for f in flags if f.get("start", "") >= cut]
        for m in methods.values():
            if m.get("series"):
                m["series"] = [s for s in m["series"] if s["start"] >= cut]
        n_in_window = len(runs)
        lean = not (args.keep_all_runs or not LEAN_RUNS)
        if lean:
            flagged = {f["run"] for f in flags}
            runs = [r for r in runs
                    if r.get("curve") or r.get("ref_curve") or r["run"] in flagged]
        # Say out loud what was dropped and why. A short `runs` array for a
        # 90-day window is deliberate policy, and a future reader must be able
        # to tell that from a truncated extract — an extract that looks like a
        # success while being empty is exactly how the wrong-DIA-NN-container
        # trap and the host_mirrors() near-miss both worked.
        window.update({
            "n_runs_in_window": n_in_window,
            "n_runs_kept": len(runs),
            "n_runs_pruned": n_in_window - len(runs),
            "n_flags_kept": len(flags),
            "runs_kept_rule": (
                "runs carrying a pressure curve, a reference curve, or a flag"
                if lean else "every run in the window"),
            "why": (
                "The whole history is analysed; only per-run arrays are "
                "windowed, because this document is one PG row fetched on "
                "every panel load. A curve-less unflagged run record has no "
                "reader (the panel reads `runs` only for ceiling runs with a "
                "`ref_curve`; instrument_watch reads `flags`/`column` only). "
                "Whole-history shape lives in `daily` and `columns`. Use "
                "--runs-window-days 0 --keep-all-runs for everything."
                if lean else
                "Per-run arrays windowed; whole-history shape in `daily` "
                "and `columns`."),
        })

    doc = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_root": args.root,
        "host_dir": host,
        "host_dirs": mirrors,
        "instrument_host": host.rsplit("_", 2)[0],
        "serials": serials,
        "column_pump": COLUMN_PUMP,
        "ceiling_bar": CEILING_BAR,
        "plateau_window": [PLATEAU_LO, PLATEAU_HI],
        # `n_runs` / `first_run` stay whole-history so the panel's header keeps
        # describing the record, not the window. `runs_window` says what the
        # per-run arrays were trimmed to.
        "summary": {
            "n_runs": full_n_runs,
            "n_methods": len(methods),
            "first_run": full_first,
            "last_run": runs[-1]["start"] if runs else full_first,
            "n_flagged": full_n_flagged,
            "n_critical": full_n_critical,
            "n_at_ceiling": full_n_at_ceiling,
            "n_analytical_methods": sum(1 for m in methods.values()
                                        if m.get("analytical")),
            "n_days": len(daily),
        },
        "runs_window": window,
        "wear": wear,
        "daily": daily,
        "columns": columns,
        "pressure_reference": pressure_ref,
        "expected_pressure": expected_meta,
        "wash_flow": wash,
        "column_lifetimes": lifetimes,
        "fresh_basis": fresh_meta,
        "sample_impact": impact,
        "methods": methods,
        "envelopes": envelopes,
        "flags": flags,
        "validation": validation,
        "column": col_block,
        "column_events_logged": col_events,
        "runs": runs,
    }

    text = json.dumps(doc, indent=args.indent, default=str)

    # Budget check. A document this size is served to every panel load and
    # stored as one PG row, so a regression here is a slow dashboard for
    # everyone — fail loudly rather than ship it.
    mb = len(text.encode()) / 1024 / 1024
    if args.max_doc_mb and mb > args.max_doc_mb:
        big = sorted(((len(json.dumps(v, default=str)), k) for k, v in doc.items()),
                     reverse=True)[:4]
        print(f"error: document is {mb:.2f} MB, over the {args.max_doc_mb:.2f} MB "
              f"budget. Largest keys: "
              + ", ".join(f"{k} {b/1024:.0f} KB" for b, k in big),
              file=sys.stderr)
        print("       lower --runs-window-days, or raise --max-doc-mb "
              "deliberately.", file=sys.stderr)
        return 3

    if args.out:
        # Atomic publish: a dashboard polling the file must never see a
        # half-written document.
        tmp = args.out + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(text)
        os.replace(tmp, args.out)
        print(f"wrote {args.out} ({mb * 1024:.1f} KB, {len(runs)} run records "
              f"of {full_n_runs} runs, {len(daily)} days)",
              file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
