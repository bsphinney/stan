"""Chromatography and instrument performance metrics.

IPS (Instrument Performance Score) is a 0-100 composite computed entirely
from search output — no reference TIC, no blank runs, works from run 1.

DIA:  30% depth + 25% spectral + 20% sampling + 15% quant + 10% digestion
DDA:  30% depth + 25% mass_acc + 20% sampling + 15% scoring + 10% digestion

All components use absolute scales with known-good reference ranges per
instrument class, so the score is meaningful even for a single run.
"""

from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

# ── Reference ranges for absolute IPS normalization ─────────────────
# These represent "excellent" values for modern instruments. A score of 100
# means you hit or exceeded the reference. Derived from published benchmarks
# and community data. Updated periodically.

DIA_REFERENCE = {
    "n_precursors": 20000,           # excellent for 1h / 50ng / modern instrument
    "median_fragments_per_precursor": 10.0,
    "median_points_across_peak": 15.0,
    "pct_fragments_quantified": 0.90,
    "missed_cleavage_rate_good": 0.10,  # <= this is excellent
}

DDA_REFERENCE = {
    "n_psms": 60000,
    "pct_delta_mass_lt5ppm": 0.98,
    "median_points_across_peak": 15.0,
    "median_hyperscore": 35.0,
    "missed_cleavage_rate_good": 0.10,
}


def compute_ips_dia(metrics: dict) -> int:
    """Compute DIA Instrument Performance Score (0-100).

    All inputs come directly from extract_dia_metrics() — no reference
    TIC, no blank run, no historical data needed.

    Components:
        30% precursor_depth — n_precursors vs reference
        25% spectral_quality — fragments per precursor vs reference
        20% sampling_quality — points across peak (Matthews & Hayes)
        15% quant_coverage — fraction of fragments quantified
        10% digestion_quality — 1 - missed cleavage rate

    Args:
        metrics: Dict from extract_dia_metrics().

    Returns:
        IPS score as integer 0-100.
    """
    ref = DIA_REFERENCE

    depth = _ratio_score(metrics.get("n_precursors", 0), ref["n_precursors"])
    spectral = _ratio_score(
        metrics.get("median_fragments_per_precursor", 0),
        ref["median_fragments_per_precursor"],
    )
    sampling = _sampling_score(metrics.get("median_points_across_peak"))
    quant = _clamp(metrics.get("pct_fragments_quantified", 0) / ref["pct_fragments_quantified"], 0, 1)
    digestion = _clamp(
        1.0 - metrics.get("missed_cleavage_rate", 0.5) / 0.5,
        0, 1,
    )

    ips = (
        30 * depth
        + 25 * spectral
        + 20 * sampling
        + 15 * quant
        + 10 * digestion
    )

    return int(round(_clamp(ips, 0, 100)))


def compute_ips_dda(metrics: dict) -> int:
    """Compute DDA Instrument Performance Score (0-100).

    Components:
        30% identification_depth — n_psms vs reference
        25% mass_accuracy — pct of PSMs with <5 ppm error
        20% sampling_quality — points across peak
        15% scoring_quality — median hyperscore vs reference
        10% digestion_quality — 1 - missed cleavage rate

    Args:
        metrics: Dict from extract_dda_metrics().

    Returns:
        IPS score as integer 0-100.
    """
    ref = DDA_REFERENCE

    depth = _ratio_score(metrics.get("n_psms", 0), ref["n_psms"])
    mass_acc = _clamp(
        metrics.get("pct_delta_mass_lt5ppm", 0) / ref["pct_delta_mass_lt5ppm"],
        0, 1,
    )
    sampling = _sampling_score(metrics.get("median_points_across_peak"))
    scoring = _ratio_score(metrics.get("median_hyperscore", 0), ref["median_hyperscore"])
    digestion = _clamp(
        1.0 - metrics.get("missed_cleavage_rate", 0.5) / 0.5,
        0, 1,
    )

    ips = (
        30 * depth
        + 25 * mass_acc
        + 20 * sampling
        + 15 * scoring
        + 10 * digestion
    )

    return int(round(_clamp(ips, 0, 100)))


def _ratio_score(value: float, reference: float) -> float:
    """Score as ratio of value to reference, capped at 1.0."""
    if reference <= 0:
        return 0.0
    return _clamp(value / reference, 0.0, 1.0)


def _sampling_score(points_across_peak: float | None) -> float:
    """Score based on data points across peak (Matthews & Hayes 1976).

    0 points → 0.0, 6 points → 0.5, 12+ points → 1.0
    """
    if points_across_peak is None or points_across_peak <= 0:
        return 0.5  # unknown — assume acceptable
    if points_across_peak >= 12:
        return 1.0
    if points_across_peak >= 6:
        return 0.5 + (points_across_peak - 6) / 12  # linear 0.5→1.0
    return points_across_peak / 12  # linear 0→0.5


# ── iRT deviation (kept from previous version) ─────────────────────

DEFAULT_IRT_LIBRARY: dict[str, float] = {
    "LGGNEQVTR": 0.0,
    "GAGSSEPVTGLDAK": 26.1,
    "VEATFGVDESNAK": 33.4,
    "YILAGVENSK": 42.3,
    "TPVISGGPYEYR": 54.6,
    "TPVITGAPYEYR": 57.3,
    "DGLDAASYYAPVR": 64.2,
    "ADVTPADFSEWSK": 67.7,
    "GTFIIDPGGVIR": 70.7,
    "GTFIIDPAAVIR": 87.2,
    "LFLQFGAQGSPFLK": 100.0,
}


def compute_irt_deviation(
    report_path: Path,
    irt_library: dict[str, float] | None = None,
) -> dict:
    """Cross-reference identified precursors against known iRT peptide RTs."""
    if irt_library is None:
        irt_library = DEFAULT_IRT_LIBRARY

    try:
        df = pl.read_parquet(report_path, columns=["Stripped.Sequence", "RT"])
    except Exception:
        logger.exception("Failed to read report for iRT: %s", report_path)
        return {"max_deviation_min": 0.0, "median_deviation_min": 0.0, "n_irt_found": 0}

    irt_seqs = set(irt_library.keys())
    irt_df = df.filter(pl.col("Stripped.Sequence").is_in(irt_seqs))

    if irt_df.height == 0:
        return {"max_deviation_min": 0.0, "median_deviation_min": 0.0, "n_irt_found": 0}

    observed_rts = (
        irt_df.group_by("Stripped.Sequence")
        .agg(pl.col("RT").median().alias("observed_rt"))
    )

    deviations: list[float] = []
    for row in observed_rts.iter_rows(named=True):
        seq = row["Stripped.Sequence"]
        observed = row["observed_rt"]
        expected = irt_library.get(seq, 0.0)
        deviations.append(abs(observed - expected))

    if not deviations:
        return {"max_deviation_min": 0.0, "median_deviation_min": 0.0, "n_irt_found": 0}

    deviations.sort()
    return {
        "max_deviation_min": max(deviations),
        "median_deviation_min": deviations[len(deviations) // 2],
        "n_irt_found": len(deviations),
    }


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
