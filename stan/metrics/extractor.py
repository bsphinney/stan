"""Metric extraction from DIA-NN and Sage search outputs.

DIA metrics extracted from report.parquet (DIA-NN output).
DDA metrics extracted from results.sage.parquet (Sage output).

Uses Polars for fast, memory-efficient parquet reads with predicate pushdown.
"""

from __future__ import annotations

import logging
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)


def extract_dia_metrics(report_path: Path, q_cutoff: float = 0.01) -> dict:
    """Extract DIA QC metrics from DIA-NN report.parquet.

    Args:
        report_path: Path to DIA-NN report.parquet output.
        q_cutoff: FDR threshold (default 1%).

    Returns:
        Dict of metric name → value.
    """
    # TODO: verify column names against current DIA-NN docs before production use
    df = pl.read_parquet(
        report_path,
        columns=[
            "Precursor.Id",
            "Stripped.Sequence",
            "Protein.Group",
            "Q.Value",
            "PG.Q.Value",
            "Fragment.Info",
            "Fragment.Quant.Corrected",
            "Precursor.Charge",
            "Missed.Cleavages",
            "File.Name",
            "Precursor.Normalised",
        ],
    )

    filt = df.filter(pl.col("Q.Value") <= q_cutoff)

    if filt.height == 0:
        logger.warning("No precursors pass FDR threshold %.2f in %s", q_cutoff, report_path)
        return _empty_dia_metrics()

    # Fragment counts per precursor
    filt = filt.with_columns(
        (pl.col("Fragment.Info").str.count_matches(";") + 1).alias("n_frag_extracted"),
        pl.col("Fragment.Quant.Corrected")
        .map_elements(
            lambda s: sum(1 for x in str(s).split(";") if x.strip() and float(x) > 0),
            return_dtype=pl.Int32,
        )
        .alias("n_frag_quantified"),
    )

    # CV across replicates (if multiple files)
    n_files = filt["File.Name"].n_unique()
    median_cv: float = 0.0
    if n_files > 1:
        cv_df = (
            filt.group_by(["Precursor.Id", "File.Name"])
            .agg(pl.col("Precursor.Normalised").mean().alias("intensity"))
            .group_by("Precursor.Id")
            .agg(
                pl.col("intensity").std().alias("sd"),
                pl.col("intensity").mean().alias("mean"),
            )
            .with_columns((pl.col("sd") / pl.col("mean") * 100).alias("cv"))
            .filter(pl.col("cv").is_not_null())
        )
        median_cv = float(cv_df["cv"].median()) if cv_df.height > 0 else 0.0

    # Charge state distribution
    charge = (
        filt.group_by("Precursor.Charge")
        .agg(pl.len().alias("n"))
        .with_columns((pl.col("n") / pl.col("n").sum()).alias("pct"))
    )

    def charge_pct(z: int) -> float:
        row = charge.filter(pl.col("Precursor.Charge") == z)
        return float(row["pct"][0]) if len(row) else 0.0

    total_frag_extracted = filt["n_frag_extracted"].sum()
    total_frag_quantified = filt["n_frag_quantified"].sum()

    return {
        "n_precursors": filt["Precursor.Id"].n_unique(),
        "n_peptides": filt["Stripped.Sequence"].n_unique(),
        "n_proteins": filt.filter(pl.col("PG.Q.Value") <= q_cutoff)[
            "Protein.Group"
        ].n_unique(),
        "median_fragments_per_precursor": float(filt["n_frag_extracted"].median()),
        "pct_fragments_quantified": (
            float(total_frag_quantified / total_frag_extracted)
            if total_frag_extracted > 0
            else 0.0
        ),
        "median_cv_precursor": median_cv,
        "missed_cleavage_rate": float(
            filt.filter(pl.col("Missed.Cleavages") >= 1).height / filt.height
        ),
        "pct_charge_1": charge_pct(1),
        "pct_charge_2": charge_pct(2),
        "pct_charge_3": charge_pct(3),
        "pct_charge_4plus": sum(charge_pct(z) for z in range(4, 10)),
    }


def extract_dda_metrics(
    sage_results_path: Path,
    gradient_min: int = 60,
) -> dict:
    """Extract DDA QC metrics from Sage results.sage.parquet.

    Args:
        sage_results_path: Path to Sage results.sage.parquet output.
        gradient_min: Gradient length in minutes (for scan rate calculation).

    Returns:
        Dict of metric name → value.
    """
    # TODO: verify column names against current Sage release notes before production use
    df = pl.read_parquet(sage_results_path)

    # Sage uses 'spectrum_q' or 'q_value' depending on version — try both
    q_col = _find_q_column(df)
    if q_col is None:
        logger.error("No q-value column found in %s", sage_results_path)
        return _empty_dda_metrics()

    filt = df.filter(pl.col(q_col) <= 0.01)

    if filt.height == 0:
        logger.warning("No PSMs pass 1%% FDR in %s", sage_results_path)
        return _empty_dda_metrics()

    # Find column names (Sage column names vary between versions)
    peptide_col = _find_column(df, ["peptide", "sequence", "stripped_peptide"])
    score_col = _find_column(df, ["hyperscore", "score", "sage_discriminant_score"])
    delta_mass_col = _find_column(df, ["delta_mass", "precursor_ppm", "expmass_ppm"])

    n_psms = filt.height
    n_peptides = filt[peptide_col].n_unique() if peptide_col else 0

    median_score = float(filt[score_col].median()) if score_col else 0.0
    pct_score_gt30 = (
        float((filt[score_col] > 30).mean()) if score_col else 0.0
    )

    ms2_scan_rate = n_psms / gradient_min if gradient_min > 0 else 0.0

    if delta_mass_col:
        abs_delta = filt[delta_mass_col].abs()
        median_delta_mass = float(abs_delta.median())
        pct_lt5ppm = float((abs_delta < 5).mean())
    else:
        median_delta_mass = 0.0
        pct_lt5ppm = 0.0

    return {
        "n_psms": n_psms,
        "n_peptides_dda": n_peptides,
        "median_hyperscore": median_score,
        "pct_hyperscore_gt30": pct_score_gt30,
        "ms2_scan_rate": ms2_scan_rate,
        "median_delta_mass_ppm": median_delta_mass,
        "pct_delta_mass_lt5ppm": pct_lt5ppm,
    }


def _find_q_column(df: pl.DataFrame) -> str | None:
    """Find the q-value column in a Sage output DataFrame."""
    candidates = ["spectrum_q", "q_value", "q-value", "posterior_error"]
    for col in candidates:
        if col in df.columns:
            return col
    return None


def _find_column(df: pl.DataFrame, candidates: list[str]) -> str | None:
    """Find the first matching column from a list of candidates."""
    for col in candidates:
        if col in df.columns:
            return col
    return None


def _empty_dia_metrics() -> dict:
    """Return a zeroed DIA metrics dict."""
    return {
        "n_precursors": 0,
        "n_peptides": 0,
        "n_proteins": 0,
        "median_fragments_per_precursor": 0.0,
        "pct_fragments_quantified": 0.0,
        "median_cv_precursor": 0.0,
        "missed_cleavage_rate": 0.0,
        "pct_charge_1": 0.0,
        "pct_charge_2": 0.0,
        "pct_charge_3": 0.0,
        "pct_charge_4plus": 0.0,
    }


def _empty_dda_metrics() -> dict:
    """Return a zeroed DDA metrics dict."""
    return {
        "n_psms": 0,
        "n_peptides_dda": 0,
        "median_hyperscore": 0.0,
        "pct_hyperscore_gt30": 0.0,
        "ms2_scan_rate": 0.0,
        "median_delta_mass_ppm": 0.0,
        "pct_delta_mass_lt5ppm": 0.0,
    }
