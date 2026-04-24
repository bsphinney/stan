"""DIA window drift detection for Bruker diaPASEF acquisitions.

Measures whether the declared isolation windows in analysis.tdf still
sit on top of the observed peptide ion cloud. Windows that drift
off-ridge (instrument retune, firmware update, calibration shift)
silently waste fragmentation — you still get a report.parquet, but
the IDs drop because a fraction of peptides are missed by every window.

v0.2.175 retune: Brett tuned against two visually-verified files
(1mai25...12816.d = clean per DataAnalysis, 22April2026...21144.d =
drifted). The prior algorithm flagged BOTH as drifted — 12816 because
its per-window IM centre sat ~0.06 /K0 below the window geometric
centre (a systematic instrument-design offset, NOT drift), and 21144
only via a coverage-<0.50 threshold that every Bruker file trips.

Key algorithm changes:

1. **Peptide-zone restriction**: both the ridge search and the
   per-window analysis are restricted to 1/K0 ∈ [0.70, 1.15]. Outside
   that range is singly-charged contamination (~1.20-1.35) or
   instrument floor — including them flips the mode to a phantom peak
   that has nothing to do with peptide drift. Windows whose geometric
   centre falls outside the peptide zone are skipped entirely.

2. **Ridge-outside-edge metric**: `drift_from_edge` = 0 if the ridge
   peak sits inside the declared window IM range; otherwise it's the
   signed distance from ridge to the nearest window edge. A window
   is "severely drifted" when BOTH:
       |drift_from_edge| > 0.015 /K0  (ridge clearly outside)
       in-window coverage < 0.50      (capture badly impaired)
   Classification uses count-of-severe + intensity-weighted severity.

3. **Signed median_drift_im** still reports ridge-minus-window-centre
   for the peptide-zone windows. The dashboard's directional trend
   chart reads this value; changing to absolute would break it.

4. **Global coverage** is now reported as a diagnostic but does NOT
   feed classification — Bruker MS1 naturally has ~50% of intensity
   outside DIA windows (they apply to MS2), so it's uninformative.

Requires alphatims — `pip install stan-proteomics[peg]` (shares the
same extra as PEG detection since both need MS1 spectrum access).
"""
from __future__ import annotations

import logging
import random
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


N_FRAMES_DEFAULT = 500
INTENSITY_THRESHOLD_DEFAULT = 10.0  # v0.2.186: was 100 — too aggressive a cut
                                    # for the display cloud. Lower threshold
                                    # lets the background scatter through so
                                    # the cloud matches Bruker DA's "show
                                    # every peak" view. Ridge-finding still
                                    # works fine because histogram mode is
                                    # dominated by the intense ridge ions.
MOBILITY_HIST_BINS = 80
RANDOM_SEED = 42

# Peptide-zone filter: multi-charge tryptic HeLa peptides overwhelmingly
# sit in 1/K0 ∈ [0.70, 1.15] on Bruker timsTOF HT. Above 1.15 is mostly
# singly-charged contamination (solvent clusters, autolysis peaks) — a
# dense pile at ~1.25 that will dominate any intensity-weighted mode
# unless we filter it out. Below 0.70 is the instrument's low-IM
# boundary for the diaPASEF scan table. Changing these ranges invalidates
# the per-window ridge placement.
PEPTIDE_IM_LO = 0.70
PEPTIDE_IM_HI = 1.15

# Per-window drift thresholds. Tuned 2026-04-23 against two
# visually-classified test files:
#   12816.d (OK per DataAnalysis): 0 severely-drifted windows
#   21144.d (drifted per DataAnalysis): 3 severely-drifted low-m/z
#     narrow windows, ridge 0.02-0.06 below im_lo, coverage 0.07-0.29
OUTSIDE_EDGE_THRESHOLD = 0.015   # |ridge − nearest window edge|
LOW_COVERAGE_FOR_DRIFT = 0.50    # per-window capture fraction
# v0.2.195: windows below this coverage are "unreliable" — so little
# peptide-zone intensity is actually inside the declared window that
# the histogram mode is driven by whatever out-of-window contamination
# is densest (typically +1 solvent clusters at 1/K0 ~1.25 dominating
# low-m/z slices). Real drift leaves 20-50% of signal inside the
# declared range; <20% means we're looking at the wrong ion population.
# Brett's 21149 false-positive (m/z 299-325 window, coverage 18%,
# ridge mode 1.125 in a 0.70-0.79 declared range) is the canonical
# case — the "drift" was +1 contamination, not peptide movement.
MIN_COVERAGE_FOR_SEVERITY = 0.20

# Run-level classification thresholds.
# v0.2.186: lowered SEVERE_COUNT_DRIFTED 3 → 2 because frame-sampling
# randomness caused identical files to land on either side of the
# count=3 threshold. TIMS scored 21144 as warn (2 severe windows)
# while a local re-score found 3. A count of 2 already indicates a
# clear pattern (not noise) so this catches borderline cases.
SEVERE_COUNT_DRIFTED = 2          # ≥2 severely-drifted windows → drifted
SEVERE_COUNT_WARN = 1             # ≥1 → warn
SEVERE_INT_FRAC_DRIFTED = 0.08    # severely-drifted intensity fraction
SEVERE_INT_FRAC_WARN = 0.03


def detect_drift_best(
    d_path,
    features_path=None,
):
    """Preferred drift detector — 4DFF features if available, MS1 mode fallback.

    v0.2.196: prefer feature-based detection when a ``.features`` file
    exists (generated by ``stan run-4dff``). Feature-based uses
    charge-2 peptide features only, eliminating the +1 contamination
    false-positives that plagued the MS1-mode detector on low-coverage
    slices (Brett's canonical 21149 case).

    Falls back to ``detect_window_drift`` (MS1-histogram mode) when
    the features file is missing OR the feature-based path returns
    ``drift_class == "unknown"`` (no z=2 features, no alphatims,
    bad feature DB). Callers that want the MS1 detector unconditionally
    can still import ``detect_window_drift`` directly.
    """
    # Delayed import avoids a circular dependency when
    # feature_drift.py imports symbols from this module.
    from stan.metrics.feature_drift import detect_feature_drift

    result = detect_feature_drift(Path(d_path), features_path=features_path)
    if result.drift_class != "unknown":
        return result
    return detect_window_drift(Path(d_path))


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
    im_mode: float           # peptide-zone ridge peak in this m/z slice
    drift_im: float          # im_mode − im_center (signed, vs geometric centre)
    drift_edge: float = 0.0  # signed distance from ridge to nearest window edge (0 if inside)
    coverage: float = 0.0    # fraction of slice intensity inside window's 1/K0 range
    severely_drifted: bool = False
    # v0.2.182: true iff window centre falls inside PEPTIDE_IM_LO..HI and
    # had enough peptide-zone intensity to run drift analysis. Windows
    # outside the peptide zone (very high / very low m/z tails) are still
    # reported for visualisation parity with Bruker DataAnalysis, but
    # drift values on them are sentinel (im_mode=im_center, drift_*=0,
    # coverage=0, severely_drifted=False) and classification ignores them.
    in_peptide_zone: bool = True


@dataclass
class DriftResult:
    """Run-level drift summary."""
    n_windows: int = 0
    n_window_groups: int = 0
    global_coverage: float = 0.0
    median_drift_im: float = 0.0
    median_abs_drift_im: float = 0.0
    p90_abs_drift_im: float = 0.0
    # v0.2.175: severity counters drive classification
    n_severely_drifted: int = 0
    severe_intensity_fraction: float = 0.0
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


def _classify_drift(n_severe: int, severe_int_frac: float) -> str:
    """Run-level verdict from per-window severity counters.

    v0.2.175: drift classification is now driven by the count of
    *severely-drifted* windows (ridge outside window edge AND low capture
    coverage) and the fraction of peptide-zone intensity they contain —
    NOT by median |drift|. A systematic ridge-off-centre offset (common
    on well-calibrated Bruker instruments) is no longer classified as
    drift. Global MS1 coverage is no longer part of classification
    because Bruker MS1 has ~50% of intensity outside DIA windows by
    design (DIA windows apply to MS2 precursor isolation).

    DEPRECATED as of v0.2.205 — replaced by ``score_drift`` which
    derives the verdict from the same whole-cloud metrics (p90, median,
    coverage) that drive the badge on the dashboard. Retained only for
    callers that still want the old per-window severity verdict.
    """
    if n_severe >= SEVERE_COUNT_DRIFTED or severe_int_frac >= SEVERE_INT_FRAC_DRIFTED:
        return "drifted"
    if n_severe >= SEVERE_COUNT_WARN or severe_int_frac >= SEVERE_INT_FRAC_WARN:
        return "warn"
    return "ok"


def score_drift(
    p90_abs_drift: float,
    median_abs_drift: float,
    global_coverage: float,
) -> tuple[float, str]:
    """Compute the whole-cloud drift score (0-100, higher=worse) + class.

    v0.2.205: replaces ``_classify_drift`` for run-level verdicts.
    The old per-window severity classifier produced counter-intuitive
    results — file A with a cleaner cloud could still be called
    "drifted" because a handful of narrow m/z slices tripped the
    severity flag, while file B with a visibly worse cloud (lower
    coverage, wider ridge, larger centroid shift) escaped with "warn"
    because its drift was spread evenly instead of concentrated.

    The new score is driven by the three metrics the user actually
    sees on the dashboard cloud:

      p90_abs_drift      — how wide the ridge is (1/K0 spread)
      |median_abs_drift| — how far the centroid has walked
      global_coverage    — fraction of features inside any window

    Charge-2 priority (v0.2.205): when the score is computed via the
    feature-based detector (``detect_feature_drift``), all three
    metrics are derived from z=2 peptide features only — +1 solvent
    and +3 contributions are filtered at ``_query_z2_features``.
    Bottom-up proteomics is dominated by +2 precursors, so the score
    reflects the signal the user actually cares about. The MS1-mode
    fallback in ``detect_window_drift`` doesn't have per-peak charge
    assignment and uses all MS1 ions — it's a less charge-selective
    classifier and should only fire when no ``.features`` file is
    available.

    Weights (tuned against TIMS DIAwinFallen test files — Brett's
    reference set 21151/21155/21156/21158/21159):

      spread   = 30 × min(p90        / 0.08, 1)
      centroid = 20 × min(|median|   / 0.06, 1)
      coverage = piecewise 0-50 (see below)

    Coverage dominates because a run that misses most of its z=2
    features (e.g. 21158 at 41% coverage) is broken regardless of
    ridge spread — the whole run loses quantitation on >half the
    peptides. Piecewise ramp:

      cov ≥ 0.60                    → 0         (healthy)
      0.50 ≤ cov < 0.60             → 15 ×  (0.60 - cov) / 0.10     (0  → 15)
      0.40 ≤ cov < 0.50             → 15 + 30 × (0.50 - cov) / 0.10 (15 → 45)
      cov < 0.40                    → 45 + 5  ×  (0.40 - cov) / 0.40 (45 → 50)

    Class thresholds:
      <40 ok, 40-69 warn, ≥70 drifted.

    Keeps the number on the badge and the class in agreement by
    construction — the verdict is derivable from the score alone.
    """
    p90 = max(0.0, float(p90_abs_drift))
    med = abs(float(median_abs_drift))
    cov = max(0.0, min(1.0, float(global_coverage)))

    spread_component = 30.0 * min(p90 / 0.08, 1.0)
    centroid_component = 20.0 * min(med / 0.06, 1.0)

    if cov >= 0.60:
        coverage_component = 0.0
    elif cov >= 0.50:
        coverage_component = 15.0 * (0.60 - cov) / 0.10
    elif cov >= 0.40:
        coverage_component = 15.0 + 30.0 * (0.50 - cov) / 0.10
    else:
        coverage_component = 45.0 + 5.0 * max(0.0, 0.40 - cov) / 0.40

    score = spread_component + centroid_component + coverage_component
    score = max(0.0, min(100.0, score))

    if score >= 70.0:
        drift_class = "drifted"
    elif score >= 40.0:
        drift_class = "warn"
    else:
        drift_class = "ok"
    return score, drift_class


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

    # Run-level global coverage still reported (diagnostic only, not
    # used for classification). Uses the union of ALL declared windows,
    # including those whose centres are outside the peptide zone.
    inside_any = np.zeros(len(mzs), dtype=bool)
    for w in subs:
        if w.im_lo == 0 and w.im_hi == 0:
            continue
        m = ((mzs >= w.mz_lo) & (mzs <= w.mz_hi) &
             (mobs >= w.im_lo) & (mobs <= w.im_hi))
        inside_any |= m

    # Per-window drift is measured only for windows whose geometric
    # centre falls in the peptide-zone 1/K0 range. Windows centred above
    # ~1.15 (high-m/z, low charge) don't have a reliable peptide ridge
    # to compare against — any mode we'd extract would be dominated by
    # charge-1 contamination or too-sparse signal to trust.
    per_window: list[WindowDriftMetric] = []
    per_window_slc_int: list[float] = []  # peptide-zone intensity per window
    for w in subs:
        if w.im_lo == 0 and w.im_hi == 0:
            continue
        ctr = (w.im_lo + w.im_hi) / 2.0
        # v0.2.182: windows outside the peptide zone get a sentinel entry
        # so they still appear in the Bruker-DA-style visualisation. No
        # drift analysis is run on them — charge-1 contamination lives
        # in these tails and would produce phantom drift.
        if not (PEPTIDE_IM_LO <= ctr <= PEPTIDE_IM_HI):
            per_window.append(WindowDriftMetric(
                window_group=w.window_group,
                mz_range=(round(w.mz_lo, 2), round(w.mz_hi, 2)),
                im_range=(round(w.im_lo, 4), round(w.im_hi, 4)),
                im_center=round(ctr, 4),
                im_mode=round(ctr, 4),
                drift_im=0.0, drift_edge=0.0, coverage=0.0,
                severely_drifted=False, in_peptide_zone=False,
            ))
            continue
        # Slice on m/z AND clamp to peptide zone in IM — we search for
        # the ridge across the FULL peptide zone, not just inside the
        # window, so we can detect "ridge is outside window" drift.
        slc_mask = ((mzs >= w.mz_lo) & (mzs <= w.mz_hi) &
                    (mobs >= PEPTIDE_IM_LO) & (mobs <= PEPTIDE_IM_HI))
        if not slc_mask.any():
            # Window IS in the peptide zone but has no signal — still
            # emit a sentinel so the vis shows the window. Flag as
            # out-of-zone for classification-exclusion purposes.
            per_window.append(WindowDriftMetric(
                window_group=w.window_group,
                mz_range=(round(w.mz_lo, 2), round(w.mz_hi, 2)),
                im_range=(round(w.im_lo, 4), round(w.im_hi, 4)),
                im_center=round(ctr, 4), im_mode=round(ctr, 4),
                drift_im=0.0, drift_edge=0.0, coverage=0.0,
                severely_drifted=False, in_peptide_zone=False,
            ))
            continue
        slc_mobs = mobs[slc_mask]
        slc_ints = ints[slc_mask]
        slc_int_sum = float(slc_ints.sum())
        if slc_int_sum < 1000.0:
            # Too little peptide-zone intensity to trust; emit sentinel.
            per_window.append(WindowDriftMetric(
                window_group=w.window_group,
                mz_range=(round(w.mz_lo, 2), round(w.mz_hi, 2)),
                im_range=(round(w.im_lo, 4), round(w.im_hi, 4)),
                im_center=round(ctr, 4), im_mode=round(ctr, 4),
                drift_im=0.0, drift_edge=0.0, coverage=0.0,
                severely_drifted=False, in_peptide_zone=False,
            ))
            continue
        hist, edges = np.histogram(
            slc_mobs, bins=MOBILITY_HIST_BINS,
            range=(PEPTIDE_IM_LO, PEPTIDE_IM_HI), weights=slc_ints,
        )
        # 3-point [1,2,1] smoothing reduces bin-edge jitter on the mode
        # without masking real shifts (<0.02 /K0 smoothing radius).
        hist_smooth = np.convolve(hist, [1.0, 2.0, 1.0], mode="same")
        if hist_smooth.max() == 0:
            continue
        peak_idx = int(hist_smooth.argmax())
        peak_im = float((edges[peak_idx] + edges[peak_idx + 1]) / 2.0)

        # Coverage = peptide-zone intensity captured by this window
        in_win = (slc_mobs >= w.im_lo) & (slc_mobs <= w.im_hi)
        cov = float(slc_ints[in_win].sum()) / slc_int_sum

        # Distance from ridge to nearest window EDGE. Zero if inside.
        if w.im_lo <= peak_im <= w.im_hi:
            drift_edge = 0.0
        elif peak_im < w.im_lo:
            drift_edge = peak_im - w.im_lo  # negative
        else:
            drift_edge = peak_im - w.im_hi  # positive

        # v0.2.195: require MIN_COVERAGE_FOR_SEVERITY (20%) — below that
        # the histogram mode reflects out-of-window contamination, not
        # peptide drift, and we'd flag false positives on low-m/z tails.
        severe = (abs(drift_edge) > OUTSIDE_EDGE_THRESHOLD and
                  cov < LOW_COVERAGE_FOR_DRIFT and
                  cov >= MIN_COVERAGE_FOR_SEVERITY)

        per_window.append(WindowDriftMetric(
            window_group=w.window_group,
            mz_range=(round(w.mz_lo, 2), round(w.mz_hi, 2)),
            im_range=(round(w.im_lo, 4), round(w.im_hi, 4)),
            im_center=round(ctr, 4),
            im_mode=round(peak_im, 4),
            drift_im=round(peak_im - ctr, 4),
            drift_edge=round(drift_edge, 4),
            coverage=round(cov, 3),
            severely_drifted=severe,
        ))
        per_window_slc_int.append(slc_int_sum)

    # v0.2.182: per_window now contains ALL windows (peptide-zone ones
    # with real drift data + tail-end ones as sentinels for the viz).
    # Classification aggregation must only look at in_peptide_zone=True
    # windows — sentinels would spuriously pull medians toward 0.
    evaluated = [pw for pw in per_window if pw.in_peptide_zone]
    if not evaluated:
        return DriftResult(drift_class="unknown", per_window=per_window)

    coverage = float(ints[inside_any].sum()) / total_int
    drifts = sorted(pw.drift_im for pw in evaluated)
    median_drift = drifts[len(drifts) // 2]
    abs_drifts = sorted(abs(d) for d in drifts)
    med_abs = abs_drifts[len(abs_drifts) // 2]
    p90_abs = abs_drifts[int(0.9 * len(abs_drifts))]

    n_severe = sum(1 for pw in evaluated if pw.severely_drifted)
    total_slc_int = sum(per_window_slc_int)
    severe_int = sum(
        si for pw, si in zip(evaluated, per_window_slc_int)
        if pw.severely_drifted
    )
    severe_int_frac = severe_int / total_slc_int if total_slc_int > 0 else 0.0

    # v0.2.186: cap raised 5000 → 20000 for Bruker-DA density parity.
    # At 5000 points the ridge was too sparse — Brett's DA comparison
    # showed a continuous solid band while STAN had visible gaps
    # between dots. 20000 points triples payload size (~800 KB per
    # run) but the cloud ridge reads as a coherent band.
    # Intensity-weighted random sample so high-signal peaks are
    # preferentially retained. log10 intensity for colormap.
    CLOUD_CAP = 20000
    cloud_mz: list[float] = []
    cloud_im: list[float] = []
    cloud_log_i: list[float] = []
    try:
        n_pts = len(mzs)
        if n_pts > 0:
            log_int = np.log10(np.maximum(ints, 1.0))
            if n_pts > CLOUD_CAP:
                # v0.2.191: log weighting (v0.2.186) flattened the ridge
                # against the background too much — Brett's comparison
                # against Bruker DA showed STAN's ridge barely visible
                # against the haze. sqrt-intensity weighting is the
                # middle ground: ridge dots are ~100x more likely than
                # background (vs log's ~5x and linear's ~10000x), so
                # the ridge is clearly denser but background still
                # shows up for context.
                w = np.sqrt(np.maximum(ints, 1.0))
                w = w / w.sum()
                rng = np.random.default_rng(RANDOM_SEED)
                idx = rng.choice(n_pts, size=CLOUD_CAP, replace=False, p=w)
            else:
                idx = np.arange(n_pts)
            cloud_mz = [float(x) for x in mzs[idx]]
            cloud_im = [float(x) for x in mobs[idx]]
            cloud_log_i = [float(x) for x in log_int[idx]]
    except Exception:
        logger.debug("drift cloud sampling failed", exc_info=True)

    # v0.2.205: classify from whole-cloud metrics (spread + centroid
    # + coverage) so the badge score and class always match what the
    # user sees on the dashboard cloud. Per-window severity counters
    # remain in the result for drill-down but no longer drive the class.
    _, drift_class = score_drift(
        p90_abs_drift=p90_abs,
        median_abs_drift=med_abs,
        global_coverage=coverage,
    )
    return DriftResult(
        # v0.2.182: n_windows counts EVALUATED windows (peptide-zone
        # ones) — that's the count that matters for classification.
        # per_window below includes all windows for viz.
        n_windows=len(evaluated),
        n_window_groups=len(set(pw.window_group for pw in evaluated)),
        global_coverage=round(coverage, 3),
        median_drift_im=round(median_drift, 4),
        median_abs_drift_im=round(med_abs, 4),
        p90_abs_drift_im=round(p90_abs, 4),
        n_severely_drifted=n_severe,
        severe_intensity_fraction=round(severe_int_frac, 4),
        drift_class=drift_class,
        per_window=per_window,
        cloud_mz=cloud_mz,
        cloud_im=cloud_im,
        cloud_log_intensity=cloud_log_i,
    )
