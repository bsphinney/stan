"""RawMeat-style identification-free QC metrics for Bruker timsTOF `.d` files.

Reads `analysis.tdf` (SQLite) directly — no DIA-NN/Sage run needed. The
point is a ~30 ms-per-file diagnostic that catches injection failures,
spray collapse, and acquisition cutoffs on any sample, not just HeLa.

STAN uses this module in two places:
  * The Sample Health Monitor (new in v0.2.89): every non-QC, non-excluded
    file is rawmeat-processed on acquisition and stored in `sample_health`
    for the operator to review.
  * Dashboard "expanded view" of individual runs (TIC, pressure,
    accumulation traces).

Adapted from the peptide wizard's fork. Two deliberate divergences from
the upstream:
  * `spray_cv_pct` was stripped — it computed CV across ALL MS1 frames
    including the gradient ramp, so a clean run reported 130 % and
    panicked operators. Kept as internal only; not returned in summary.
  * `stability_score` (100 − 10 × n_dropouts) was replaced with a
    dropout RATE normalized against n_ms1_frames, so runtime-length
    doesn't dominate the score.
"""

from __future__ import annotations

import logging
import math
import sqlite3
import statistics
from pathlib import Path

logger = logging.getLogger(__name__)

_MSMS_LABELS = {0: "MS1", 2: "MS2", 8: "ddaPASEF", 9: "diaPASEF"}
_DROPOUT_THRESHOLD = 0.25     # frame intensity < 25 % of local median = dropout
_ROLLING_WINDOW = 11           # frames on each side for the local median


def _tdf_path(d_path: Path) -> Path:
    tdf = d_path / "analysis.tdf"
    if not tdf.exists():
        raise FileNotFoundError(f"analysis.tdf not found in {d_path}")
    return tdf


def extract_rawmeat_metrics(d_path) -> dict:
    """Extract RawMeat-style metrics from a Bruker timsTOF .d directory.

    Returns `{}` on missing/unreadable `analysis.tdf` so callers can
    degrade gracefully instead of raising on bad/incomplete acquisitions.
    """
    d_path = Path(d_path)
    try:
        tdf = _tdf_path(d_path)
    except FileNotFoundError as e:
        logger.warning("rawmeat: %s", e)
        return {}

    try:
        with sqlite3.connect(str(tdf)) as con:
            col_names = {r[1] for r in con.execute("PRAGMA table_info(Frames)")}
            has_pressure = "Pressure" in col_names
            has_acc_time = "AccumulationTime" in col_names

            select_cols = "Id, Time, MsMsType, SummedIntensities, NumScans, MaxIntensity"
            if has_acc_time:
                select_cols += ", AccumulationTime, RampTime"
            if has_pressure:
                select_cols += ", Pressure"

            rows = con.execute(f"SELECT {select_cols} FROM Frames ORDER BY Id").fetchall()
            meta_rows = con.execute("SELECT Key, Value FROM GlobalMetadata").fetchall()
    except sqlite3.Error as e:
        logger.warning("rawmeat: sqlite error on %s: %s", d_path, e)
        return {}

    if not rows:
        return {}

    meta = {r[0]: r[1] for r in meta_rows}

    rts, msms_types, summed_int, max_int = [], [], [], []
    acc_times, pressures = [], []

    col_idx = 6
    for row in rows:
        rts.append(row[1])
        msms_types.append(row[2])
        summed_int.append(row[3] or 0)
        max_int.append(row[5] or 0)
        idx = col_idx
        if has_acc_time:
            acc_times.append(row[idx])
            idx += 2
        if has_pressure:
            pressures.append(row[idx])

    n_frames = len(rows)

    # ── TIC split by MS level ─────────────────────────────────────────
    ms1_rt, ms1_int = [], []
    ms2_rt, ms2_int = [], []
    for rt, mtype, sint in zip(rts, msms_types, summed_int):
        if mtype == 0:
            ms1_rt.append(round(rt, 3))
            ms1_int.append(sint)
        else:
            ms2_rt.append(round(rt, 3))
            ms2_int.append(sint)

    ms1_maxint = [max_int[i] for i, t in enumerate(msms_types) if t == 0]
    n_ms1 = len(ms1_maxint)

    # ── Dropout detection (rolling-median test on MS1 MaxIntensity) ──
    # Using MaxIntensity instead of SummedIntensities because the former
    # is more sensitive to spray dropouts; SummedIntensities is
    # dominated by the gradient plateau.
    dropouts = []
    if n_ms1 >= _ROLLING_WINDOW * 2:
        half = _ROLLING_WINDOW
        for i in range(half, n_ms1 - half):
            window = ms1_maxint[i - half: i] + ms1_maxint[i + 1: i + half + 1]
            local_med = statistics.median(window)
            if local_med > 0 and ms1_maxint[i] < _DROPOUT_THRESHOLD * local_med:
                dropouts.append(round(ms1_rt[i], 2))

    # Normalized dropout RATE (per 100 MS1 frames). A long run with the
    # same fraction of dropouts as a short run should score the same.
    dropout_rate_per_100 = round(100 * len(dropouts) / n_ms1, 2) if n_ms1 else 0.0

    # ── Accumulation time ─────────────────────────────────────────────
    acc_data: dict = {}
    if acc_times:
        ms1_acc = [acc_times[i] for i in range(n_frames)
                   if msms_types[i] == 0 and acc_times[i]]
        ms2_acc = [acc_times[i] for i in range(n_frames)
                   if msms_types[i] != 0 and acc_times[i]]
        acc_data = {
            "median_ms1_acc_ms": round(statistics.median(ms1_acc), 2) if ms1_acc else None,
            "median_ms2_acc_ms": round(statistics.median(ms2_acc), 2) if ms2_acc else None,
        }

    # ── Pressure ──────────────────────────────────────────────────────
    pressure_data: dict = {}
    if pressures:
        valid = [p for p in pressures if p is not None and p > 0]
        if valid:
            pressure_data = {
                "pressure_mean_mbar":  round(statistics.mean(valid), 4),
                "pressure_min_mbar":   round(min(valid), 4),
                "pressure_max_mbar":   round(max(valid), 4),
                "pressure_range_mbar": round(max(valid) - min(valid), 4),
            }

    # ── Frame type breakdown ──────────────────────────────────────────
    type_counts: dict[str, int] = {}
    for mtype in msms_types:
        label = _MSMS_LABELS.get(mtype, f"type{mtype}")
        type_counts[label] = type_counts.get(label, 0) + 1

    # ── Intensity + dynamic range ─────────────────────────────────────
    # `ms1_max_intensity` = max SummedIntensities across MS1 frames
    # (i.e. the "brightest MS1 frame's TIC"). This is the right metric
    # for the injection-health check — a failed injection has very
    # low peak SummedIntensities regardless of per-peak MaxIntensity.
    ms1_total_tic = sum(ms1_int)
    ms1_max_intensity = max(ms1_int) if ms1_int else 0
    dyn_range = None
    nz = [v for v in ms1_maxint if v > 0]
    if nz:
        med_nz = statistics.median(nz)
        peak = max(nz)
        if med_nz > 0 and peak > 0:
            dyn_range = round(math.log10(peak / med_nz), 1)

    # ── Summary + metadata ────────────────────────────────────────────
    rt_min_s = min(rts) if rts else 0
    rt_max_s = max(rts) if rts else 0
    summary = {
        "n_frames_total":        n_frames,
        "n_ms1_frames":          type_counts.get("MS1", 0),
        "n_ms2_frames":          n_frames - type_counts.get("MS1", 0),
        "frame_types":           type_counts,
        "rt_start_s":            round(rt_min_s, 1),
        "rt_end_s":              round(rt_max_s, 1),
        "rt_duration_min":       round((rt_max_s - rt_min_s) / 60, 2),
        "ms1_total_tic":         ms1_total_tic,
        "ms1_max_intensity":     ms1_max_intensity,
        "dynamic_range_log10":   dyn_range,
        "n_dropouts":            len(dropouts),
        "dropout_rate_per_100_ms1": dropout_rate_per_100,
    }
    summary.update(pressure_data)
    summary.update(acc_data)

    metadata = {
        "instrument":       meta.get("InstrumentName", ""),
        "serial_number":    meta.get("InstrumentSerialNumber", ""),
        "software":         meta.get("AcquisitionSoftware", ""),
        "software_version": meta.get("AcquisitionSoftwareVersion", ""),
        "acquisition_date": meta.get("AcquisitionDateTime", ""),
        "operator":         meta.get("OperatorName", ""),
        "method":           meta.get("MethodName", ""),
    }

    return {
        "tic": {"ms1_rt": ms1_rt, "ms1_int": ms1_int,
                "ms2_rt": ms2_rt, "ms2_int": ms2_int},
        "dropouts_rt": dropouts,
        "summary": summary,
        "metadata": metadata,
    }


# ── Thermo adapter ──────────────────────────────────────────────────────

def extract_rawmeat_thermo(raw_path) -> dict:
    """Thermo `.raw` analog of `extract_rawmeat_metrics` for Bruker `.d`.

    Reuses `stan.metrics.tic.extract_tic_thermo` (fisher_py fast path,
    TRFP mzML fallback) to pull MS1 TIC, then derives the same summary
    keys the Sample Health Monitor needs so `evaluate_sample_health()`
    works unchanged across vendors.

    Fields NOT available from Thermo TIC (pressure, accumulation time,
    frame-type counts for ddaPASEF/diaPASEF) are simply absent from
    the summary — the evaluator skips them gracefully.

    Costs a lot more than Bruker rawmeat (~1-3 s with fisher_py, up to
    5 min with TRFP fallback on a 60-min DIA run) because fisher_py
    iterates every MS1 scan and TRFP has to convert to mzML. For QC
    cadences (1 file per hour) that's fine.
    """
    from pathlib import Path as _Path
    from stan.metrics.tic import extract_tic_thermo

    raw_path = _Path(raw_path)
    trace = extract_tic_thermo(raw_path)
    if trace is None or not trace.rt_min:
        return {}

    rt_min = list(trace.rt_min)
    intensity = [float(v) for v in trace.intensity]
    n_ms1 = len(intensity)

    # Rolling-median dropout test — identical to the Bruker path but
    # on MS1 TIC values instead of per-frame MaxIntensity.
    dropouts_rt = []
    if n_ms1 >= _ROLLING_WINDOW * 2:
        half = _ROLLING_WINDOW
        for i in range(half, n_ms1 - half):
            window = intensity[i - half: i] + intensity[i + 1: i + half + 1]
            local_med = statistics.median(window)
            if local_med > 0 and intensity[i] < _DROPOUT_THRESHOLD * local_med:
                dropouts_rt.append(round(rt_min[i], 2))

    dropout_rate_per_100 = round(100 * len(dropouts_rt) / n_ms1, 2) if n_ms1 else 0.0

    nz = [v for v in intensity if v > 0]
    dyn_range = None
    if nz:
        med_nz = statistics.median(nz)
        peak = max(nz)
        if med_nz > 0 and peak > 0:
            dyn_range = round(math.log10(peak / med_nz), 1)

    rt_start = min(rt_min) if rt_min else 0.0
    rt_end = max(rt_min) if rt_min else 0.0

    summary = {
        "n_frames_total":           n_ms1,
        "n_ms1_frames":             n_ms1,
        "n_ms2_frames":             None,          # Thermo: not tracked here
        "frame_types":              {"MS1": n_ms1},
        "rt_start_s":               round(rt_start * 60, 1),
        "rt_end_s":                 round(rt_end * 60, 1),
        "rt_duration_min":          round(rt_end - rt_start, 2),
        "ms1_total_tic":            sum(intensity),
        "ms1_max_intensity":        max(intensity) if intensity else 0,
        "dynamic_range_log10":      dyn_range,
        "n_dropouts":               len(dropouts_rt),
        "dropout_rate_per_100_ms1": dropout_rate_per_100,
    }

    return {
        "tic": {"ms1_rt": rt_min, "ms1_int": intensity,
                "ms2_rt": [], "ms2_int": []},
        "dropouts_rt": dropouts_rt,
        "summary": summary,
        "metadata": {},   # Thermo metadata could be added via fisher_py later
    }


# ── Sample-health heuristic ──────────────────────────────────────────────

def evaluate_sample_health(rawmeat: dict, rolling_median_max_intensity: float | None = None,
                           expected_rt_duration_min: float | None = None) -> dict:
    """Classify a rawmeat result as PASS / WARN / FAIL.

    Heuristic (tunable, intentionally conservative on first release):

      FAIL if any of:
        * `ms1_max_intensity` < 5 % of rolling 30-day instrument median
          (when that median is available; skipped on new instruments)
        * `dropout_rate_per_100_ms1` > 3 (i.e. >3 % of MS1 frames dropping
          out — typical clean runs are <0.5)
        * `rt_duration_min` < 50 % of expected method runtime
          (when caller supplies it; skipped otherwise)

      WARN if any of:
        * `ms1_max_intensity` < 30 % of rolling median
        * `dropout_rate_per_100_ms1` > 1
        * `rt_duration_min` < 80 % of expected

      PASS otherwise.

    Returns `{"verdict": "pass"|"warn"|"fail", "reasons": [str, ...]}`.
    """
    if not rawmeat or "summary" not in rawmeat:
        return {"verdict": "fail", "reasons": ["rawmeat extraction returned empty"]}

    s = rawmeat["summary"]
    max_i = s.get("ms1_max_intensity") or 0
    dropout_rate = s.get("dropout_rate_per_100_ms1") or 0.0
    rt_min = s.get("rt_duration_min") or 0.0

    reasons: list[str] = []
    verdict = "pass"

    if rolling_median_max_intensity and rolling_median_max_intensity > 0:
        ratio = max_i / rolling_median_max_intensity
        if ratio < 0.05:
            reasons.append(f"MS1 max intensity is {ratio:.0%} of instrument median "
                           "— injection likely failed")
            verdict = "fail"
        elif ratio < 0.30 and verdict != "fail":
            reasons.append(f"MS1 max intensity is {ratio:.0%} of instrument median")
            verdict = "warn"

    if dropout_rate > 3.0:
        reasons.append(f"{dropout_rate:.1f}% of MS1 frames are spray dropouts "
                       "— spray likely collapsed mid-run")
        verdict = "fail"
    elif dropout_rate > 1.0 and verdict != "fail":
        reasons.append(f"{dropout_rate:.1f}% of MS1 frames are spray dropouts")
        if verdict == "pass":
            verdict = "warn"

    if expected_rt_duration_min and expected_rt_duration_min > 0:
        rt_ratio = rt_min / expected_rt_duration_min
        if rt_ratio < 0.50:
            reasons.append(f"Run duration is {rt_ratio:.0%} of expected "
                           "— acquisition cut off early?")
            verdict = "fail"
        elif rt_ratio < 0.80 and verdict != "fail":
            reasons.append(f"Run duration is {rt_ratio:.0%} of expected")
            if verdict == "pass":
                verdict = "warn"

    return {"verdict": verdict, "reasons": reasons}
