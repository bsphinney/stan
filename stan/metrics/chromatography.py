"""Chromatography metrics: GRS score, iRT deviation, TIC analysis.

GRS (Gradient Reproducibility Score) is a 0–100 composite for LC health:
  GRS = 40 × shape_r_scaled + 25 × auc_scaled + 20 × peak_rt_scaled + 15 × carryover_scaled

Interpretation: 90+ excellent, 70–89 good, 50–69 watch, <50 investigate.
"""

from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

# Standard iRT peptides with normalized retention times (Biognosys iRT kit)
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


def compute_grs(tic_data: dict) -> int:
    """Compute Gradient Reproducibility Score (0–100).

    Args:
        tic_data: Dict with keys:
            - shape_correlation: float (0–1), Pearson r of TIC shape vs reference
            - tic_auc: float, total ion current area under curve
            - tic_auc_reference: float, expected TIC AUC for this method
            - peak_rt_min: float, RT of TIC apex in minutes
            - peak_rt_reference: float, expected TIC apex RT
            - carryover_ratio: float (0–1), blank signal / previous run signal

    Returns:
        GRS score as integer 0–100.
    """
    shape_r = tic_data.get("shape_correlation", 0.0)
    tic_auc = tic_data.get("tic_auc", 0.0)
    tic_auc_ref = tic_data.get("tic_auc_reference", 1.0)
    peak_rt = tic_data.get("peak_rt_min", 0.0)
    peak_rt_ref = tic_data.get("peak_rt_reference", 0.0)
    carryover = tic_data.get("carryover_ratio", 0.0)

    # Scale each component to 0–1
    shape_r_scaled = _clamp(shape_r, 0.0, 1.0)

    # AUC: z-score relative to reference, scaled
    if tic_auc_ref > 0:
        auc_z = abs(tic_auc - tic_auc_ref) / max(tic_auc_ref * 0.1, 1.0)
        auc_scaled = _clamp(1.0 - auc_z / 3.0, 0.0, 1.0)
    else:
        auc_scaled = 0.0

    # Peak RT deviation: smaller is better
    if peak_rt_ref > 0:
        rt_dev = abs(peak_rt - peak_rt_ref) / peak_rt_ref
        peak_rt_scaled = _clamp(1.0 - rt_dev * 5.0, 0.0, 1.0)
    else:
        peak_rt_scaled = 0.0

    # Carryover: lower is better
    carryover_scaled = _clamp(1.0 - carryover, 0.0, 1.0)

    # Weighted composite
    grs = (
        40 * shape_r_scaled
        + 25 * auc_scaled
        + 20 * peak_rt_scaled
        + 15 * carryover_scaled
    )

    return int(round(_clamp(grs, 0.0, 100.0)))


def compute_irt_deviation(
    report_path: Path,
    irt_library: dict[str, float] | None = None,
) -> dict:
    """Cross-reference identified precursors against known iRT peptide RTs.

    Args:
        report_path: Path to DIA-NN report.parquet.
        irt_library: Dict of peptide sequence → normalized RT. Uses default if None.

    Returns:
        Dict with max_deviation_min, median_deviation_min, n_irt_found.
    """
    if irt_library is None:
        irt_library = DEFAULT_IRT_LIBRARY

    try:
        df = pl.read_parquet(
            report_path,
            columns=["Stripped.Sequence", "RT"],
        )
    except Exception:
        logger.exception("Failed to read report for iRT: %s", report_path)
        return {"max_deviation_min": 0.0, "median_deviation_min": 0.0, "n_irt_found": 0}

    # Find iRT peptides in the report
    irt_seqs = set(irt_library.keys())
    irt_df = df.filter(pl.col("Stripped.Sequence").is_in(irt_seqs))

    if irt_df.height == 0:
        logger.warning("No iRT peptides found in %s", report_path)
        return {"max_deviation_min": 0.0, "median_deviation_min": 0.0, "n_irt_found": 0}

    # Compute observed vs expected RT for each iRT peptide
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
    median_dev = deviations[len(deviations) // 2]
    max_dev = max(deviations)

    return {
        "max_deviation_min": max_dev,
        "median_deviation_min": median_dev,
        "n_irt_found": len(deviations),
    }


def _clamp(value: float, lo: float, hi: float) -> float:
    """Clamp a value between lo and hi."""
    return max(lo, min(hi, value))
