"""Feature-based DIA window drift detection for Bruker diaPASEF.

Replacement for the MS1-mode histogram approach in ``window_drift.py``
that false-positives on low-coverage windows where +1 solvent/autolysis
contamination (1/K0 ~1.20-1.30) dominates the histogram even after
the 20 % coverage floor was added. Rather than asking "where is the
densest mode of MS1 intensity inside this m/z × IM slice", this
detector asks "where are the charge-2 peptide features 4DFF detected
inside this m/z slice" — charge-1 contamination is filtered at the
feature-finder step, not post-hoc.

Algorithm per sub-window (m/z_lo..m/z_hi × im_lo..im_hi):
  1. Pull all 4DFF features with:
        Charge == 2
        MZ in (m/z_lo, m/z_hi)
        Mobility in (PEPTIDE_IM_LO, PEPTIDE_IM_HI)  -- peptide zone
     Charge-1 (solvent) and charge-3+ (which typically track drift
     well anyway but add noise at low counts) are excluded.
  2. ``n_features_in_slice``: count of features in (1).
  3. ``im_mean``: intensity-weighted mean Mobility of those features.
  4. ``drift_im``: im_mean - declared centre.
  5. ``drift_edge``: 0 if im_mean sits inside (im_lo, im_hi); else the
     signed distance to the nearest edge.
  6. ``capture_fraction``: fraction of those features that fall inside
     the declared (im_lo, im_hi) range.
  7. Per-window severity:
        |drift_edge| > 0.015
        AND capture_fraction < 0.50
        AND n_features_in_slice >= 5
     The n>=5 floor was the missing guardrail in the MS1-mode version —
     noise-dominated slices with only a handful of features can't be
     trusted to report a meaningful drift.
  8. Run-level verdict reuses ``SEVERE_COUNT_WARN=1`` / ``SEVERE_COUNT_DRIFTED=2``
     from ``window_drift`` for consistency.

Features required: charge-2-only gives ~50-70 % of all 4DFF features
on a HeLa diaPASEF run, which is plenty (~50k-200k z=2 features per
60 SPD QC acquisition) to characterise every sub-window.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from stan.metrics.features import find_features_file
from stan.metrics.window_drift import (
    DriftResult,
    LOW_COVERAGE_FOR_DRIFT,
    OUTSIDE_EDGE_THRESHOLD,
    PEPTIDE_IM_HI,
    PEPTIDE_IM_LO,
    SEVERE_COUNT_DRIFTED,
    SEVERE_COUNT_WARN,
    SEVERE_INT_FRAC_DRIFTED,
    SEVERE_INT_FRAC_WARN,
    WindowDriftMetric,
    _classify_drift,
    _extract_sub_windows,
)

logger = logging.getLogger(__name__)


# Feature-based detector tunables. Tuned against 21144.d (drifted per
# Bruker DA) and 12816.d / 21149.d (clean/warn false-positive on the
# MS1-mode detector). With z=2 features only, HeLa QC runs yield
# enough density that 5 features is a reliable minimum for a valid
# drift measurement in any given sub-window.
MIN_FEATURES_FOR_SEVERITY = 5
TARGET_CHARGE = 2


def _query_z2_features(features_path: Path):
    """Fetch (mz, mobility, intensity) of every charge-2 feature.

    Returns three lists. Empty lists if the table has no z=2 rows or
    the file doesn't contain the expected columns.
    """
    with sqlite3.connect(str(features_path)) as con:
        cols = {
            r[1] for r in
            con.execute("PRAGMA table_info(LcTimsMsFeature)").fetchall()
        }
        needed = {"MZ", "Mobility", "Intensity", "Charge"}
        if not needed.issubset(cols):
            logger.warning(
                "Features file %s missing columns %s (have %s)",
                features_path.name, needed - cols, sorted(cols),
            )
            return [], [], []
        rows = con.execute(
            "SELECT MZ, Mobility, Intensity FROM LcTimsMsFeature "
            "WHERE Charge = ? AND Intensity > 0",
            (TARGET_CHARGE,),
        ).fetchall()
    if not rows:
        return [], [], []
    return (
        [r[0] for r in rows],
        [r[1] for r in rows],
        [r[2] for r in rows],
    )


def detect_feature_drift(
    d_path: str | Path,
    features_path: str | Path | None = None,
) -> DriftResult:
    """Measure DIA window drift from 4DFF features.

    Parameters
    ----------
    d_path :
        Path to the ``.d`` directory. Used to read
        ``analysis.tdf → DiaFrameMsMsWindows`` for the declared
        window geometry, and to derive the scan→mobility calibration.
    features_path :
        Optional override. If ``None``, we look for
        ``<d>/<stem>.features`` (4DFF's default output location).
        Returns ``DriftResult(drift_class="unknown")`` if absent —
        caller (``detect_drift_best``) is expected to fall back to
        the MS1-mode detector in that case.
    """
    d_path = Path(d_path)
    feat = Path(features_path) if features_path else find_features_file(d_path)
    if feat is None or not feat.exists():
        logger.info(
            "No .features file for %s — run `stan run-4dff` first "
            "or call detect_drift_best() to fall back to MS1 mode.",
            d_path.name,
        )
        return DriftResult(drift_class="unknown")

    # We need the scan→mobility calibration to convert
    # (scan_begin, scan_end) into (im_hi, im_lo). alphatims is how
    # window_drift.py does it — we mirror that exactly so geometry
    # lines up with the MS1-based detector for apples-to-apples
    # comparison in the dashboard.
    try:
        from alphatims.bruker import TimsTOF
    except ImportError:
        logger.warning(
            "alphatims not installed — feature drift requires scan→mobility "
            "calibration. Install via `pip install stan-proteomics[peg]`."
        )
        return DriftResult(drift_class="unknown")
    try:
        import numpy as np
    except ImportError:
        logger.warning("numpy required for feature drift")
        return DriftResult(drift_class="unknown")

    try:
        data = TimsTOF(str(d_path), use_hdf_if_available=True)
    except (ValueError, TypeError, RuntimeError) as e:
        logger.warning(
            "alphatims TimsTOF init failed for %s (%s: %s) — feature drift unknown.",
            d_path.name, type(e).__name__, e,
        )
        return DriftResult(drift_class="unknown")

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

    mz_list, mob_list, int_list = _query_z2_features(feat)
    if not mz_list:
        logger.info("No z=%d features in %s", TARGET_CHARGE, feat.name)
        return DriftResult(drift_class="unknown")

    mzs = np.asarray(mz_list, dtype=np.float64)
    mobs = np.asarray(mob_list, dtype=np.float64)
    ints = np.asarray(int_list, dtype=np.float64)

    per_window: list[WindowDriftMetric] = []
    per_window_features: list[int] = []

    for w in subs:
        if w.im_lo == 0 and w.im_hi == 0:
            continue
        ctr = (w.im_lo + w.im_hi) / 2.0
        # Windows outside the peptide zone → sentinel (no analysis).
        if not (PEPTIDE_IM_LO <= ctr <= PEPTIDE_IM_HI):
            per_window.append(WindowDriftMetric(
                window_group=w.window_group,
                mz_range=(round(w.mz_lo, 2), round(w.mz_hi, 2)),
                im_range=(round(w.im_lo, 4), round(w.im_hi, 4)),
                im_center=round(ctr, 4), im_mode=round(ctr, 4),
                drift_im=0.0, drift_edge=0.0, coverage=0.0,
                severely_drifted=False, in_peptide_zone=False,
            ))
            continue

        slice_mask = (
            (mzs >= w.mz_lo)
            & (mzs <= w.mz_hi)
            & (mobs >= PEPTIDE_IM_LO)
            & (mobs <= PEPTIDE_IM_HI)
        )
        slc_mobs = mobs[slice_mask]
        slc_ints = ints[slice_mask]
        n_feat = int(slc_mobs.size)

        if n_feat == 0:
            per_window.append(WindowDriftMetric(
                window_group=w.window_group,
                mz_range=(round(w.mz_lo, 2), round(w.mz_hi, 2)),
                im_range=(round(w.im_lo, 4), round(w.im_hi, 4)),
                im_center=round(ctr, 4), im_mode=round(ctr, 4),
                drift_im=0.0, drift_edge=0.0, coverage=0.0,
                severely_drifted=False, in_peptide_zone=False,
            ))
            continue

        int_sum = float(slc_ints.sum())
        if int_sum > 0:
            im_mean = float((slc_mobs * slc_ints).sum() / int_sum)
        else:
            im_mean = float(slc_mobs.mean())

        # Capture fraction = fraction of z=2 features IN this m/z
        # slice that sit inside the declared IM window.
        in_win = (slc_mobs >= w.im_lo) & (slc_mobs <= w.im_hi)
        cov = float(in_win.sum()) / float(n_feat)

        if w.im_lo <= im_mean <= w.im_hi:
            drift_edge = 0.0
        elif im_mean < w.im_lo:
            drift_edge = im_mean - w.im_lo  # negative
        else:
            drift_edge = im_mean - w.im_hi  # positive

        severe = (
            abs(drift_edge) > OUTSIDE_EDGE_THRESHOLD
            and cov < LOW_COVERAGE_FOR_DRIFT
            and n_feat >= MIN_FEATURES_FOR_SEVERITY
        )

        per_window.append(WindowDriftMetric(
            window_group=w.window_group,
            mz_range=(round(w.mz_lo, 2), round(w.mz_hi, 2)),
            im_range=(round(w.im_lo, 4), round(w.im_hi, 4)),
            im_center=round(ctr, 4),
            im_mode=round(im_mean, 4),
            drift_im=round(im_mean - ctr, 4),
            drift_edge=round(drift_edge, 4),
            coverage=round(cov, 3),
            severely_drifted=severe,
        ))
        per_window_features.append(n_feat)

    evaluated = [pw for pw in per_window if pw.in_peptide_zone]
    if not evaluated:
        return DriftResult(drift_class="unknown", per_window=per_window)

    drifts = sorted(pw.drift_im for pw in evaluated)
    median_drift = drifts[len(drifts) // 2]
    abs_drifts = sorted(abs(d) for d in drifts)
    med_abs = abs_drifts[len(abs_drifts) // 2]
    p90_abs = abs_drifts[int(0.9 * len(abs_drifts))]

    n_severe = sum(1 for pw in evaluated if pw.severely_drifted)
    total_feat = sum(per_window_features)
    severe_feat = sum(
        nf for pw, nf in zip(evaluated, per_window_features) if pw.severely_drifted
    )
    severe_frac = severe_feat / total_feat if total_feat > 0 else 0.0

    # Global coverage here is the fraction of z=2 features that fall
    # inside any declared window (geometry cross-check, not a
    # classification input — same semantics as window_drift).
    inside_any_global = 0
    for i, w in enumerate(subs):
        if w.im_lo == 0 and w.im_hi == 0:
            continue
        m = (
            (mzs >= w.mz_lo) & (mzs <= w.mz_hi)
            & (mobs >= w.im_lo) & (mobs <= w.im_hi)
        )
        inside_any_global += int(m.sum())
    global_coverage = (
        inside_any_global / float(mzs.size) if mzs.size else 0.0
    )

    drift_class = _classify_drift(n_severe, severe_frac)
    logger.info(
        "feature-drift %s: class=%s n_severe=%d frac=%.3f total_z2=%d",
        d_path.name, drift_class, n_severe, severe_frac, int(mzs.size),
    )
    # Reference reads SEVERE_COUNT_DRIFTED / SEVERE_COUNT_WARN /
    # SEVERE_INT_FRAC_DRIFTED / SEVERE_INT_FRAC_WARN to keep the
    # importer happy (they're part of the public contract of this
    # module). Unused locally because _classify_drift already uses them.
    _ = (
        SEVERE_COUNT_DRIFTED, SEVERE_COUNT_WARN,
        SEVERE_INT_FRAC_DRIFTED, SEVERE_INT_FRAC_WARN,
    )

    return DriftResult(
        n_windows=len(evaluated),
        n_window_groups=len(set(pw.window_group for pw in evaluated)),
        global_coverage=round(global_coverage, 3),
        median_drift_im=round(median_drift, 4),
        median_abs_drift_im=round(med_abs, 4),
        p90_abs_drift_im=round(p90_abs, 4),
        n_severely_drifted=n_severe,
        severe_intensity_fraction=round(severe_frac, 4),
        drift_class=drift_class,
        per_window=per_window,
    )
