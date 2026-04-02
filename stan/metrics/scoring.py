"""Community scoring and cohort bucketing.

DIA_Score and DDA_Score are percentile-based composite scores computed
within cohorts (instrument_family × throughput_bucket × amount_bucket).

Throughput is expressed in SPD (samples per day) — the universal unit
across Evosep, Vanquish Neo, and traditional LC setups.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

COHORT_MINIMUM = 5  # minimum submissions before leaderboard appears

# ── Throughput bucketing (SPD-first) ─────────────────────────────────

# Confirmed Evosep gradient times (evosep.com, April 2026):
#   500 SPD  ~2.2 min gradient    300 SPD  ~2.3 min gradient
#   200 SPD  ~4.8 min gradient    100 SPD  ~11 min gradient
#    60 SPD  ~21 min gradient      40 SPD  ~31 min (Whisper)
#    30 SPD  ~44 min gradient     Ext      ~88 min gradient


def spd_bucket(spd: int) -> str:
    """Classify throughput (samples per day) into a cohort bucket.

    This is the primary bucketing method.  Labs select their Evosep,
    Vanquish Neo, or equivalent method by SPD.

    Buckets:
        ≥200  "200+spd"   Evosep 500/300/200, Vanquish Neo 180
        80–199 "100spd"   Evosep 100 SPD
        40–79  "60spd"    Evosep 60 SPD (most popular)
        25–39  "30spd"    Evosep 30 SPD, Whisper 40
        10–24  "15spd"    Evosep Extended, traditional 1h
        <10    "deep"     Traditional 2h+ gradients
    """
    if spd >= 200:
        return "200+spd"
    if spd >= 80:
        return "100spd"
    if spd >= 40:
        return "60spd"
    if spd >= 25:
        return "30spd"
    if spd >= 10:
        return "15spd"
    return "deep"


def gradient_min_to_spd(minutes: int) -> int:
    """Estimate SPD from gradient length for labs using custom LC methods.

    Uses cycle time ≈ gradient + 25 % overhead (wash, equilibration, loading).
    SPD = 1440 / cycle_time.  This is an approximation — labs should set
    their actual SPD in instruments.yml when possible.
    """
    if minutes <= 0:
        return 30  # fallback to default
    cycle = minutes * 1.25
    return max(1, int(1440 / cycle))


def throughput_bucket(spd: int | None = None, gradient_min: int | None = None) -> str:
    """Resolve throughput bucket from SPD (preferred) or gradient length (fallback).

    Args:
        spd: Samples per day (primary — use this when known).
        gradient_min: Gradient length in minutes (fallback for custom LC methods).

    Returns:
        Throughput bucket string for cohort ID.
    """
    if spd is not None and spd > 0:
        return spd_bucket(spd)
    if gradient_min is not None and gradient_min > 0:
        return spd_bucket(gradient_min_to_spd(gradient_min))
    return spd_bucket(30)  # default: 30 SPD


def amount_bucket(ng: float) -> str:
    """Classify injection amount (ng) into a cohort bucket.

    Buckets reflect modern proteomics workflows where many labs inject
    10–200 ng on Astral/timsTOF platforms.  Submissions are compared
    only within the same bucket so that a 50 ng run isn't penalised
    against a 500 ng run.
    """
    if ng <= 25:
        return "ultra-low"  # single-cell / very low input
    if ng <= 75:
        return "low"  # 50 ng standard QC (default)
    if ng <= 150:
        return "mid"
    if ng <= 300:
        return "standard"
    if ng <= 600:
        return "high"
    return "very-high"


def compute_cohort_id(
    instrument_family: str,
    amount_ng: float,
    spd: int | None = None,
    gradient_min: int | None = None,
) -> str:
    """Build a cohort ID string for grouping benchmark submissions.

    Args:
        instrument_family: e.g. "timsTOF", "Astral", "Exploris".
        amount_ng: HeLa injection amount in nanograms.
        spd: Samples per day (primary throughput measure).
        gradient_min: Gradient length in minutes (fallback if spd not set).
    """
    tb = throughput_bucket(spd=spd, gradient_min=gradient_min)
    ab = amount_bucket(amount_ng)
    return f"{instrument_family}_{tb}_{ab}"


def compute_dia_score(
    metrics: dict,
    cohort_percentiles: dict,
) -> float:
    """Compute DIA community composite score (0–100).

    DIA_Score =
      40 × percentile_rank(n_precursors)
    + 25 × percentile_rank(n_peptides)
    + 20 × (100 - percentile_rank(median_cv_precursor))  # lower CV = better
    + 15 × percentile_rank(grs_score)
    """
    pr = _percentile_rank

    score = (
        0.40 * pr(metrics.get("n_precursors", 0), cohort_percentiles.get("n_precursors", []))
        + 0.25 * pr(metrics.get("n_peptides", 0), cohort_percentiles.get("n_peptides", []))
        + 0.20 * (100 - pr(
            metrics.get("median_cv_precursor", 0),
            cohort_percentiles.get("median_cv_precursor", []),
        ))
        + 0.15 * pr(metrics.get("grs_score", 0), cohort_percentiles.get("grs_score", []))
    )
    return round(score, 1)


def compute_dda_score(
    metrics: dict,
    cohort_percentiles: dict,
) -> float:
    """Compute DDA community composite score (0–100).

    DDA_Score =
      35 × percentile_rank(n_psms)
    + 25 × percentile_rank(n_peptides_dda)
    + 20 × percentile_rank(pct_delta_mass_lt5ppm)
    + 20 × percentile_rank(ms2_scan_rate)
    """
    pr = _percentile_rank

    score = (
        0.35 * pr(metrics.get("n_psms", 0), cohort_percentiles.get("n_psms", []))
        + 0.25 * pr(
            metrics.get("n_peptides_dda", 0), cohort_percentiles.get("n_peptides_dda", [])
        )
        + 0.20 * pr(
            metrics.get("pct_delta_mass_lt5ppm", 0),
            cohort_percentiles.get("pct_delta_mass_lt5ppm", []),
        )
        + 0.20 * pr(
            metrics.get("ms2_scan_rate", 0), cohort_percentiles.get("ms2_scan_rate", [])
        )
    )
    return round(score, 1)


def _percentile_rank(value: float, sorted_values: list[float]) -> float:
    """Compute percentile rank (0–100) of value within sorted_values."""
    if not sorted_values:
        return 50.0  # no cohort data → assume middle

    n = len(sorted_values)
    count_below = sum(1 for v in sorted_values if v < value)
    count_equal = sum(1 for v in sorted_values if v == value)

    # Average rank method
    percentile = (count_below + 0.5 * count_equal) / n * 100
    return min(100.0, max(0.0, percentile))
