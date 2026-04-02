"""Community scoring and cohort bucketing.

DIA_Score and DDA_Score are percentile-based composite scores computed
within cohorts (instrument_family × gradient_bucket × amount_bucket).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

COHORT_MINIMUM = 5  # minimum submissions before leaderboard appears


def gradient_bucket(minutes: int) -> str:
    """Classify gradient length into a cohort bucket."""
    if minutes <= 30:
        return "ultra-short"
    if minutes <= 45:
        return "short"
    if minutes <= 75:
        return "standard-1h"
    if minutes <= 120:
        return "long-2h"
    return "extended"


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


def compute_cohort_id(instrument_family: str, gradient_min: int, amount_ng: float) -> str:
    """Build a cohort ID string for grouping benchmark submissions."""
    gb = gradient_bucket(gradient_min)
    ab = amount_bucket(amount_ng)
    return f"{instrument_family}_{gb}_{ab}"


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
