"""Column aging detection via longitudinal TIC AUC trend analysis.

Monitors TIC AUC and peak RT over time per instrument. A declining TIC AUC
trend or shifting peak RT indicates column degradation.

Uses a simple linear regression (no scipy dependency) for trend detection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from stan.db import get_trends

logger = logging.getLogger(__name__)


@dataclass
class ColumnHealthReport:
    """Column health assessment from longitudinal trends."""

    instrument: str
    n_runs: int
    tic_auc_trend_slope: float  # negative = declining = possible aging
    peak_rt_trend_slope: float  # shift in RT indicates column change
    tic_auc_r2: float
    peak_rt_r2: float
    status: str  # "healthy" | "watch" | "degraded"
    message: str


def assess_column_health(instrument: str, min_runs: int = 10) -> ColumnHealthReport | None:
    """Assess column health from longitudinal run data.

    Args:
        instrument: Instrument name.
        min_runs: Minimum number of runs needed for trend analysis.

    Returns:
        ColumnHealthReport, or None if insufficient data.
    """
    runs = get_trends(instrument=instrument, limit=200)

    # Filter to runs with TIC AUC data
    runs_with_tic = [r for r in runs if r.get("tic_auc") is not None]

    if len(runs_with_tic) < min_runs:
        return None

    # Extract time series (use index as x-axis for simplicity)
    tic_values = [r["tic_auc"] for r in runs_with_tic]
    rt_values = [r.get("peak_rt_min", 0) for r in runs_with_tic if r.get("peak_rt_min") is not None]

    # Compute linear trends
    tic_slope, tic_r2 = _linear_trend(tic_values)
    rt_slope, rt_r2 = _linear_trend(rt_values) if len(rt_values) >= min_runs else (0.0, 0.0)

    # Classify
    status, message = _classify_health(tic_slope, tic_r2, rt_slope, rt_r2, tic_values)

    return ColumnHealthReport(
        instrument=instrument,
        n_runs=len(runs_with_tic),
        tic_auc_trend_slope=round(tic_slope, 4),
        peak_rt_trend_slope=round(rt_slope, 4),
        tic_auc_r2=round(tic_r2, 4),
        peak_rt_r2=round(rt_r2, 4),
        status=status,
        message=message,
    )


def _linear_trend(values: list[float]) -> tuple[float, float]:
    """Compute simple linear regression slope and R² for a value series.

    Returns:
        (slope, r_squared). Slope is per-run-index, not per-time.
    """
    n = len(values)
    if n < 2:
        return 0.0, 0.0

    x_values = list(range(n))
    x_mean = sum(x_values) / n
    y_mean = sum(values) / n

    ss_xy = sum((x - x_mean) * (y - y_mean) for x, y in zip(x_values, values))
    ss_xx = sum((x - x_mean) ** 2 for x in x_values)
    ss_yy = sum((y - y_mean) ** 2 for y in values)

    if ss_xx == 0 or ss_yy == 0:
        return 0.0, 0.0

    slope = ss_xy / ss_xx
    r_squared = (ss_xy ** 2) / (ss_xx * ss_yy)

    return slope, r_squared


def _classify_health(
    tic_slope: float,
    tic_r2: float,
    rt_slope: float,
    rt_r2: float,
    tic_values: list[float],
) -> tuple[str, str]:
    """Classify column health based on trend analysis."""
    if not tic_values:
        return "healthy", "Insufficient data for trend analysis."

    mean_tic = sum(tic_values) / len(tic_values)
    if mean_tic == 0:
        return "healthy", "No TIC data available."

    # Normalize slope as percentage of mean per run
    normalized_slope = (tic_slope / mean_tic) * 100 if mean_tic > 0 else 0

    if tic_r2 > 0.5 and normalized_slope < -1.0:
        # Strong declining trend
        return "degraded", (
            f"TIC AUC declining {abs(normalized_slope):.1f}%/run (R²={tic_r2:.2f}). "
            "Column may need replacement."
        )

    if tic_r2 > 0.3 and normalized_slope < -0.5:
        # Moderate declining trend
        msg = f"TIC AUC trending down {abs(normalized_slope):.1f}%/run (R²={tic_r2:.2f}). "
        if rt_r2 > 0.3 and abs(rt_slope) > 0.05:
            msg += "Peak RT also shifting — monitor column condition."
        else:
            msg += "Monitor over next several runs."
        return "watch", msg

    return "healthy", "Column performance stable."
