"""DIA window drift detection for Bruker diaPASEF acquisitions.

Measures whether the declared isolation windows in analysis.tdf still
sit on top of the observed peptide ion cloud. Windows that drift
off-ridge (instrument retune, firmware update, calibration shift)
silently waste fragmentation — you still get a report.parquet, but
the IDs drop because a fraction of peptides are missed by every window.

Validated against Brett's known-drifted file
04172026__60SPD_DIA-SM1_S3-A1_1_21029.d:
    v2 coverage=35.5%, median drift=+0.081 /K0
vs the clean reference 03jun2024_HeLa50ng_DIA_100spd_S1-B2_1_6205.d:
    v2 coverage=73.0%, median drift=+0.024 /K0

Two non-obvious choices baked into the algorithm:

1. **Windows use alphatims's CALIBRATED scan→mobility mapping, not the
   linear approximation** (`1/K0 ≈ 1.6 - scan/max_scan`). The linear
   approximation is ~0.05 /K0 off from real Bruker calibration — big
   enough to mask drift that's genuinely smaller than the calibration
   error. Peaks are placed via real calibration, so windows must be too.

2. **Per-window centroid is the MODE (peak of intensity-weighted 1/K0
   histogram) of the window's m/z slice, NOT the intensity-weighted
   mean.** The m/z slice contains both peptide signal and contaminant
   background across the full 1/K0 range; a weighted mean averages
   the two and muddles the drift signal. The mode tracks the peptide
   peak cleanly.

Requires alphatims — `pip install stan-proteomics[peg]` (shares the
same extra as PEG detection since both need MS1 spectrum access).
"""
from __future__ import annotations

import logging
import random
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


N_FRAMES_DEFAULT = 500
INTENSITY_THRESHOLD_DEFAULT = 100.0
MOBILITY_HIST_BINS = 80
RANDOM_SEED = 42


# Drift class thresholds (validated on 2 real timsTOF files, to be
# recalibrated on a larger sweep). Tune per instrument family once we
# have enough clean-run distribution data.
COVERAGE_OK = 0.65
COVERAGE_WARN = 0.50
DRIFT_OK = 0.04     # |median drift| in 1/K0 units
DRIFT_WARN = 0.06
P90_DRIFT_OK = 0.07
P90_DRIFT_WARN = 0.09


@dataclass
class _SubWindow:
    """One isolation sub-window extracted from DiaFrameMsMsWindows."""
    window_group: int
    scan_begin: int
    scan_end: int
    mz_lo: float
    mz_hi: float
    im_lo: float = 0.0  # filled after calibration
    im_hi: float = 0.0


@dataclass
class WindowDriftMetric:
    """Per-window drift measurement."""
    window_group: int
    mz_range: tuple[float, float]
    im_range: tuple[float, float]
    im_center: float
    im_mode: float           # peak of observed 1/K0 density in this m/z slice
    drift_im: float          # im_mode − im_center (signed)
    coverage: float          # fraction of slice intensity inside window's 1/K0 range


@dataclass
class DriftResult:
    """Run-level drift summary."""
    n_windows: int = 0
    n_window_groups: int = 0
    global_coverage: float = 0.0
    median_drift_im: float = 0.0
    median_abs_drift_im: float = 0.0
    p90_abs_drift_im: float = 0.0
    drift_class: str = "unknown"  # ok | warn | drifted | unknown
    per_window: list[WindowDriftMetric] = field(default_factory=list)
    # v0.2.173: downsampled (mz, im, log_intensity) cloud for the
    # Bruker DataAnalysis-style visualization. Capped at ~5000 points.
    cloud_mz: list = field(default_factory=list)
    cloud_im: list = field(default_factory=list)
    cloud_log_intensity: list = field(default_factory=list)


def _extract_sub_windows(d_path: Path, scan_to_mobility) -> list[_SubWindow]:
    """Read every sub-window row from DiaFrameMsMsWindows.

    Unlike mobility_windows.extract_dia_windows (which aggregates to
    one DiaWindow per WindowGroup), this keeps EVERY row — a WG with
    three mobility ranges becomes three _SubWindow instances so drift
    is measured per sub-window, not averaged across the group.
    """
    tdf = d_path / "analysis.tdf"
    if not tdf.exists():
        return []
    with sqlite3.connect(str(tdf)) as con:
        cols = {
            r[1] for r in
            con.execute("PRAGMA table_info(DiaFrameMsMsWindows)").fetchall()
        }
        if "IsolationMz" in cols and "IsolationWidth" in cols:
            rows = con.execute(
                "SELECT WindowGroup, ScanNumBegin, ScanNumEnd, "
                "       IsolationMz, IsolationWidth "
                "FROM DiaFrameMsMsWindows"
            ).fetchall()
            subs = [
                _SubWindow(
                    window_group=g, scan_begin=sb, scan_end=se,
                    mz_lo=mz - w / 2.0, mz_hi=mz + w / 2.0,
                )
                for g, sb, se, mz, w in rows
            ]
        elif "IsolationWindowLowerMz" in cols and "IsolationWindowUpperMz" in cols:
            rows = con.execute(
                "SELECT WindowGroup, ScanNumBegin, ScanNumEnd, "
                "       IsolationWindowLowerMz, IsolationWindowUpperMz "
                "FROM DiaFrameMsMsWindows"
            ).fetchall()
            subs = [
                _SubWindow(
                    window_group=g, scan_begin=sb, scan_end=se,
                    mz_lo=lo, mz_hi=hi,
                )
                for g, sb, se, lo, hi in rows
            ]
        else:
            return []

    n = len(scan_to_mobility)
    for w in subs:
        if 0 <= w.scan_begin < n:
            w.im_hi = float(scan_to_mobility[w.scan_begin])
        if 0 <= w.scan_end < n:
            w.im_lo = float(scan_to_mobility[w.scan_end])
    return subs


def _classify_drift(coverage: float, med_abs_drift: float, p90_abs_drift: float) -> str:
    """Three-class verdict from the two primary metrics."""
    cov_level = ("ok" if coverage >= COVERAGE_OK
                 else "warn" if coverage >= COVERAGE_WARN
                 else "drifted")
    drift_level = ("ok" if med_abs_drift < DRIFT_OK and p90_abs_drift < P90_DRIFT_OK
                   else "warn" if med_abs_drift < DRIFT_WARN and p90_abs_drift < P90_DRIFT_WARN
                   else "drifted")
    # Worst of the two wins.
    priority = {"ok": 0, "warn": 1, "drifted": 2}
    return cov_level if priority[cov_level] >= priority[drift_level] else drift_level


def detect_window_drift(
    d_path: Path,
    n_frames: int = N_FRAMES_DEFAULT,
    intensity_threshold: float = INTENSITY_THRESHOLD_DEFAULT,
) -> DriftResult:
    """Measure drift of DIA isolation windows vs the observed ion cloud.

    Requires alphatims for Bruker MS1 access. On non-Bruker files or
    when alphatims isn't installed, returns a DriftResult with
    drift_class="unknown" and empty per_window — caller should treat
    that as "drift check not available for this run", not a failure.
    """
    try:
        from alphatims.bruker import TimsTOF
    except ImportError:
        logger.warning(
            "alphatims not installed — window drift detection disabled. "
            "Install via `pip install stan-proteomics[peg]`."
        )
        return DriftResult(drift_class="unknown")
    import numpy as np

    # alphatims 1.0.9 + polars 1.35+ throws ValueError
    # "search side must be one of 'left' or 'right'" during TimsTOF
    # init — a library compatibility break, not our bug. Brett 2026-04-22:
    # every one of 220 drift backfill runs failed with that error. Catch
    # it so the backfill doesn't burn through 220 rows producing zero
    # results; return "unknown" and let the caller skip gracefully.
    try:
        data = TimsTOF(str(d_path), use_hdf_if_available=True)
    except (ValueError, TypeError, RuntimeError) as e:
        logger.warning(
            "alphatims TimsTOF init failed for %s (%s: %s) — drift unknown. "
            "Likely alphatims/polars version mismatch; try pinning alphatims.",
            d_path.name, type(e).__name__, e,
        )
        return DriftResult(drift_class="unknown")

    # Calibrated scan → 1/K0 mapping from alphatims. Essential —
    # without this the window positions are ~0.05 /K0 off from reality.
    scan_to_mobility = None
    for attr in ("mobility_values", "scan_to_mobility_values"):
        if hasattr(data, attr):
            arr = getattr(data, attr)
            if hasattr(arr, "__len__") and len(arr) > 100:
                scan_to_mobility = np.asarray(arr, dtype=np.float64)
                break
    if scan_to_mobility is None:
        logger.warning("No scan→mobility calibration available from alphatims")
        return DriftResult(drift_class="unknown")

    subs = _extract_sub_windows(d_path, scan_to_mobility)
    if not subs:
        return DriftResult(drift_class="unknown")

    # Gather MS1 peaks from a random sample of frames
    ms1 = [int(fid) for fid, msms in zip(data.frames.Id, data.frames.MsMsType)
           if msms == 0]
    rng = random.Random(RANDOM_SEED)
    if len(ms1) > n_frames:
        ms1 = rng.sample(ms1, n_frames)

    mzs_list, mobs_list, ints_list = [], [], []
    for fid in ms1:
        frame = data[fid]
        if "mz_values" not in frame.columns:
            continue
        mz = frame["mz_values"].to_numpy()
        ints = frame["intensity_values"].to_numpy().astype(np.float64)
        mob = frame["mobility_values"].to_numpy()
        mask = ints >= intensity_threshold
        mzs_list.append(mz[mask])
        mobs_list.append(mob[mask])
        ints_list.append(ints[mask])
    if not mzs_list:
        return DriftResult(drift_class="unknown")

    mzs = np.concatenate(mzs_list)
    mobs = np.concatenate(mobs_list)
    ints = np.concatenate(ints_list)
    total_int = float(ints.sum())
    if total_int == 0:
        return DriftResult(drift_class="unknown")

    inside_any = np.zeros(len(mzs), dtype=bool)
    per_window: list[WindowDriftMetric] = []
    im_hist_lo = float(scan_to_mobility.min())
    im_hist_hi = float(scan_to_mobility.max())

    for w in subs:
        if w.im_lo == 0 and w.im_hi == 0:
            continue
        mz_mask = (mzs >= w.mz_lo) & (mzs <= w.mz_hi)
        if not mz_mask.any():
            continue
        mz_mobs = mobs[mz_mask]
        mz_ints = ints[mz_mask]
        # Intensity-weighted histogram → MODE (peak bin midpoint)
        hist, edges = np.histogram(
            mz_mobs, bins=MOBILITY_HIST_BINS,
            range=(im_hist_lo, im_hist_hi), weights=mz_ints,
        )
        if hist.max() == 0:
            continue
        peak_idx = int(hist.argmax())
        peak_im = float((edges[peak_idx] + edges[peak_idx + 1]) / 2.0)
        # What fraction of slice intensity falls inside the window's 1/K0 range?
        in_win = (mz_mobs >= w.im_lo) & (mz_mobs <= w.im_hi)
        global_mask = mz_mask.copy()
        global_mask[mz_mask] = in_win
        inside_any |= global_mask
        slice_int = float(mz_ints.sum())
        per_window.append(WindowDriftMetric(
            window_group=w.window_group,
            mz_range=(round(w.mz_lo, 2), round(w.mz_hi, 2)),
            im_range=(round(w.im_lo, 4), round(w.im_hi, 4)),
            im_center=round((w.im_lo + w.im_hi) / 2.0, 4),
            im_mode=round(peak_im, 4),
            drift_im=round(peak_im - (w.im_lo + w.im_hi) / 2.0, 4),
            coverage=round(float(mz_ints[in_win].sum()) / slice_int, 3)
                     if slice_int > 0 else 0.0,
        ))

    if not per_window:
        return DriftResult(drift_class="unknown")

    coverage = float(ints[inside_any].sum()) / total_int
    drifts = sorted(w.drift_im for w in per_window)
    median_drift = drifts[len(drifts) // 2]
    abs_drifts = sorted(abs(d) for d in drifts)
    med_abs = abs_drifts[len(abs_drifts) // 2]
    p90_abs = abs_drifts[int(0.9 * len(abs_drifts))]

    # v0.2.173: downsampled cloud for the Bruker DataAnalysis-style
    # visualization. Intensity-weighted random sample so high-signal
    # peaks are preferentially retained - the cloud ridge shows up
    # cleanly at ~5000 points. log10 intensity for colormap.
    CLOUD_CAP = 5000
    cloud_mz: list[float] = []
    cloud_im: list[float] = []
    cloud_log_i: list[float] = []
    try:
        n_pts = len(mzs)
        if n_pts > 0:
            log_int = np.log10(np.maximum(ints, 1.0))
            if n_pts > CLOUD_CAP:
                # Weighted sample by intensity - brighter peaks more likely
                # to be picked, so the ridge is visible in the cloud.
                w = ints / ints.sum()
                rng = np.random.default_rng(RANDOM_SEED)
                idx = rng.choice(n_pts, size=CLOUD_CAP, replace=False, p=w)
            else:
                idx = np.arange(n_pts)
            cloud_mz = [float(x) for x in mzs[idx]]
            cloud_im = [float(x) for x in mobs[idx]]
            cloud_log_i = [float(x) for x in log_int[idx]]
    except Exception:
        logger.debug("drift cloud sampling failed", exc_info=True)

    return DriftResult(
        n_windows=len(per_window),
        n_window_groups=len(set(w.window_group for w in per_window)),
        global_coverage=round(coverage, 3),
        median_drift_im=round(median_drift, 4),
        median_abs_drift_im=round(med_abs, 4),
        p90_abs_drift_im=round(p90_abs, 4),
        drift_class=_classify_drift(coverage, med_abs, p90_abs),
        per_window=per_window,
        cloud_mz=cloud_mz,
        cloud_im=cloud_im,
        cloud_log_intensity=cloud_log_i,
    )
