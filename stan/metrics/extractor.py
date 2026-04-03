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
    # Read all available columns first to discover RT-related columns
    all_cols = pl.read_parquet_schema(report_path)
    available = set(all_cols.keys()) if hasattr(all_cols, 'keys') else set(all_cols)

    # Required columns
    want = [
        "Precursor.Id", "Stripped.Sequence", "Protein.Group",
        "Q.Value", "PG.Q.Value", "Fragment.Info", "Fragment.Quant.Corrected",
        "Precursor.Charge", "Missed.Cleavages", "File.Name", "Precursor.Normalised",
    ]

    # Optional RT columns for points-across-peak (column names vary by DIA-NN version)
    rt_cols: list[str] = []
    for candidate in ["RT", "RT.Start", "RT.Stop", "iRT", "Predicted.RT"]:
        if candidate in available:
            rt_cols.append(candidate)

    # Evidence column (number of MS2 scans supporting the ID)
    evidence_col = None
    for candidate in ["Evidence", "Ms2.Scan.Count", "Scan.Evidence"]:
        if candidate in available:
            evidence_col = candidate
            break

    cols_to_read = [c for c in want if c in available] + rt_cols
    if evidence_col:
        cols_to_read.append(evidence_col)

    df = pl.read_parquet(report_path, columns=cols_to_read)

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

    # ── Points across peak (Matthews & Hayes 1976) ────────────────
    # Compute from RT.Start/RT.Stop if available, or estimate from RT + Evidence
    median_peak_width_sec: float | None = None
    median_points_across_peak: float | None = None

    if "RT.Start" in filt.columns and "RT.Stop" in filt.columns:
        # Direct peak width from elution window boundaries
        peak_widths = filt.with_columns(
            ((pl.col("RT.Stop") - pl.col("RT.Start")) * 60).alias("peak_width_sec")
        ).filter(pl.col("peak_width_sec") > 0)

        if peak_widths.height > 0:
            median_peak_width_sec = float(peak_widths["peak_width_sec"].median())

            # Estimate cycle time from consecutive RTs in the same file
            if "RT" in filt.columns and "File.Name" in filt.columns:
                cycle_times = (
                    filt.sort(["File.Name", "RT"])
                    .with_columns(
                        (pl.col("RT").diff().over("File.Name") * 60).alias("dt_sec")
                    )
                    .filter(pl.col("dt_sec") > 0)
                    .filter(pl.col("dt_sec") < 10)  # filter outliers (>10s gaps)
                )
                if cycle_times.height > 0:
                    median_cycle_sec = float(cycle_times["dt_sec"].median())
                    if median_cycle_sec > 0:
                        median_points_across_peak = median_peak_width_sec / median_cycle_sec

    elif "RT" in filt.columns and evidence_col and evidence_col in filt.columns:
        # Fallback: use Evidence column (number of scans supporting the ID)
        evidence_vals = filt[evidence_col].drop_nulls()
        if evidence_vals.len() > 0:
            median_points_across_peak = float(evidence_vals.median())

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
        "median_peak_width_sec": median_peak_width_sec,
        "median_points_across_peak": median_points_across_peak,
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

    # ── Points across peak for DDA ─────────────────────────────────
    # Sage reports retention_time per PSM. For DDA, we estimate peak width
    # from the spread of PSM RTs per peptide (multiple PSMs from the same
    # peptide across its elution window).
    median_peak_width_sec: float | None = None
    median_points_across_peak: float | None = None

    rt_col = _find_column(df, ["retention_time", "rt", "RT"])
    if rt_col and peptide_col:
        # Group PSMs by peptide and compute RT spread per peptide
        pep_rt = (
            filt.group_by(peptide_col)
            .agg(
                pl.col(rt_col).min().alias("rt_min"),
                pl.col(rt_col).max().alias("rt_max"),
                pl.len().alias("n_scans"),
            )
            .filter(pl.col("n_scans") >= 3)  # need 3+ PSMs to estimate width
            .with_columns(
                ((pl.col("rt_max") - pl.col("rt_min")) * 60).alias("peak_width_sec")
            )
            .filter(pl.col("peak_width_sec") > 0)
        )

        if pep_rt.height > 0:
            median_peak_width_sec = float(pep_rt["peak_width_sec"].median())
            median_points_across_peak = float(pep_rt["n_scans"].median())

    elif rt_col:
        # Fallback: estimate MS1 cycle time from consecutive scan RTs
        sorted_rts = filt.sort(rt_col)
        diffs = sorted_rts.with_columns(
            (pl.col(rt_col).diff() * 60).alias("dt_sec")
        ).filter(pl.col("dt_sec") > 0).filter(pl.col("dt_sec") < 5)

        if diffs.height > 0:
            median_cycle = float(diffs["dt_sec"].median())
            # Estimate: typical DDA peak ~10-15s, points = peak_width / cycle
            if median_cycle > 0 and gradient_min > 0:
                # Rough estimate from total PSMs and gradient
                estimated_peak_width = gradient_min * 60 / (n_psms / 10) if n_psms > 0 else 10.0
                estimated_peak_width = max(3.0, min(30.0, estimated_peak_width))
                median_peak_width_sec = estimated_peak_width
                median_points_across_peak = estimated_peak_width / median_cycle

    return {
        "n_psms": n_psms,
        "n_peptides_dda": n_peptides,
        "median_hyperscore": median_score,
        "pct_hyperscore_gt30": pct_score_gt30,
        "ms2_scan_rate": ms2_scan_rate,
        "median_delta_mass_ppm": median_delta_mass,
        "pct_delta_mass_lt5ppm": pct_lt5ppm,
        "median_peak_width_sec": median_peak_width_sec,
        "median_points_across_peak": median_points_across_peak,
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
        "median_peak_width_sec": None,
        "median_points_across_peak": None,
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
        "median_peak_width_sec": None,
        "median_points_across_peak": None,
    }
