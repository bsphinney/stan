"""TIC (Total Ion Current) extraction from Bruker timsTOF .d files.

Reads MS1 frame intensities from analysis.tdf SQLite database and computes
shape metrics for LC health monitoring. Ported from DE-LIMP R logic.

The TIC trace is the summed intensity of all ions per MS1 frame over time.
It reflects loading amount, LC gradient quality, and spray stability.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TICTrace:
    """Raw TIC trace data from a single run."""

    rt_min: list[float]          # retention time in minutes
    intensity: list[float]       # summed intensity per MS1 frame
    run_name: str = ""


@dataclass
class TICMetrics:
    """Computed TIC shape metrics for QC evaluation."""

    total_auc: float = 0.0             # trapezoid AUC of smoothed TIC
    peak_rt_min: float = 0.0           # RT of max intensity
    peak_tic: float = 0.0              # max intensity value
    gradient_width_min: float = 0.0    # RT range where TIC > 10% of peak
    baseline_ratio: float = 0.0        # median(lowest 10%) / peak
    late_signal_ratio: float = 0.0     # signal in last 20% of gradient / total
    asymmetry: float = 0.0             # left/right width ratio at 50% height
    n_frames: int = 0                  # number of MS1 frames


def extract_tic_bruker(d_path: Path) -> TICTrace | None:
    """Extract MS1 TIC trace from a Bruker .d directory.

    Reads the Frames table from analysis.tdf, filtering to MS1 frames
    (MsMsType = 0). Falls back to ScanMode = 0 for older TDF schemas.

    Args:
        d_path: Path to the .d directory.

    Returns:
        TICTrace with RT (minutes) and intensity arrays, or None on failure.
    """
    tdf = d_path / "analysis.tdf"
    if not tdf.exists():
        logger.warning("analysis.tdf not found in %s", d_path)
        return None

    try:
        with sqlite3.connect(str(tdf)) as con:
            # Check which intensity column exists
            cols = [r[1] for r in con.execute("PRAGMA table_info(Frames)").fetchall()]

            if "SummedIntensities" in cols:
                tic_col = "SummedIntensities"
            elif "AccumulatedIntensity" in cols:
                tic_col = "AccumulatedIntensity"
            elif "MaxIntensity" in cols:
                tic_col = "MaxIntensity"
            else:
                # Fallback: find any intensity-like column
                tic_col = next((c for c in cols if "ntensit" in c.lower()), None)
                if not tic_col:
                    logger.warning("No intensity column found in Frames table")
                    return None

            # Filter to MS1 frames
            if "MsMsType" in cols:
                ms1_filter = "WHERE MsMsType = 0"
            elif "ScanMode" in cols:
                ms1_filter = "WHERE ScanMode = 0"
            else:
                ms1_filter = ""
                logger.info("No MsMsType/ScanMode column — using all frames")

            rows = con.execute(
                f"SELECT Time, {tic_col} FROM Frames {ms1_filter} ORDER BY Time"
            ).fetchall()

    except sqlite3.Error:
        logger.exception("Failed to read TIC from %s", tdf)
        return None

    if not rows:
        logger.warning("No MS1 frames found in %s", d_path.name)
        return None

    rt_min = [r[0] / 60.0 for r in rows]  # Bruker Time is in seconds
    intensity = [float(r[1]) if r[1] is not None else 0.0 for r in rows]

    return TICTrace(
        rt_min=rt_min,
        intensity=intensity,
        run_name=d_path.stem,
    )


def compute_tic_metrics(trace: TICTrace) -> TICMetrics:
    """Compute shape metrics from a TIC trace.

    Metrics follow DE-LIMP's approach: smoothed AUC, peak position,
    gradient width at 10% height, baseline ratio, tailing, and asymmetry.

    Args:
        trace: TICTrace from extract_tic_bruker().

    Returns:
        TICMetrics with all shape metrics.
    """
    n = len(trace.rt_min)
    if n < 10:
        return TICMetrics(n_frames=n)

    rt = trace.rt_min
    raw = trace.intensity

    # Smooth with moving average (2% window, minimum 3 points)
    window = max(3, n // 50)
    smoothed = _moving_average(raw, window)

    # Peak
    peak_idx = max(range(n), key=lambda i: smoothed[i])
    peak_tic = smoothed[peak_idx]
    peak_rt = rt[peak_idx]

    if peak_tic <= 0:
        return TICMetrics(n_frames=n)

    # AUC (trapezoid rule on smoothed)
    total_auc = sum(
        (rt[i + 1] - rt[i]) * (smoothed[i] + smoothed[i + 1]) / 2.0
        for i in range(n - 1)
    )

    # Gradient width: RT range where TIC > 10% of peak
    threshold_10pct = peak_tic * 0.10
    above = [i for i in range(n) if smoothed[i] >= threshold_10pct]
    if above:
        gradient_width = rt[above[-1]] - rt[above[0]]
    else:
        gradient_width = 0.0

    # Baseline ratio: median of lowest 10% vs peak
    sorted_vals = sorted(smoothed)
    n_low = max(1, n // 10)
    baseline_median = sorted_vals[n_low // 2]
    baseline_ratio = baseline_median / peak_tic if peak_tic > 0 else 0.0

    # Late signal ratio: signal in last 20% of gradient window
    if above:
        grad_start = rt[above[0]]
        grad_end = rt[above[-1]]
        grad_range = grad_end - grad_start
        if grad_range > 0:
            late_cutoff = grad_end - 0.2 * grad_range
            late_auc = sum(
                (rt[i + 1] - rt[i]) * (smoothed[i] + smoothed[i + 1]) / 2.0
                for i in range(n - 1)
                if rt[i] >= late_cutoff
            )
            late_signal_ratio = late_auc / total_auc if total_auc > 0 else 0.0
        else:
            late_signal_ratio = 0.0
    else:
        late_signal_ratio = 0.0

    # Asymmetry: left/right width at 50% height
    threshold_50pct = peak_tic * 0.50
    left_rt = peak_rt
    right_rt = peak_rt
    for i in range(peak_idx, -1, -1):
        if smoothed[i] < threshold_50pct:
            left_rt = rt[i]
            break
    for i in range(peak_idx, n):
        if smoothed[i] < threshold_50pct:
            right_rt = rt[i]
            break
    left_width = peak_rt - left_rt
    right_width = right_rt - peak_rt
    asymmetry = left_width / right_width if right_width > 0 else 0.0

    return TICMetrics(
        total_auc=round(total_auc, 1),
        peak_rt_min=round(peak_rt, 2),
        peak_tic=round(peak_tic, 0),
        gradient_width_min=round(gradient_width, 2),
        baseline_ratio=round(baseline_ratio, 4),
        late_signal_ratio=round(late_signal_ratio, 4),
        asymmetry=round(asymmetry, 3),
        n_frames=n,
    )


def _moving_average(values: list[float], window: int) -> list[float]:
    """Simple centered moving average."""
    n = len(values)
    half = window // 2
    result = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        result.append(sum(values[lo:hi]) / (hi - lo))
    return result
