"""Metric extraction from DIA-NN and Sage search outputs.

DIA metrics extracted from report.parquet (DIA-NN output).
DDA metrics extracted from results.sage.parquet (Sage output).

Uses Polars for fast, memory-efficient parquet reads with predicate pushdown.

Scope note on literature-survey metrics added 2026-04: the extractor pulls
every single-run metric from DIA-NN outputs that the NIST MSQC / QCloud2 /
PTXQC / 2024 Framework literature considers standard AND that is free from
data DIA-NN already writes. This covers mass-accuracy drift (report.stats.tsv),
chromatographic shape across the gradient (RT-decile peak widths from
report.parquet), gradient utilization (C-2A middle-50% band), peak capacity
(single-number LC health), dynamic range, and digestion-quality shape
(≥2 missed cleavage fraction). Cross-run metrics (intensity-binned CV,
data-completeness across replicates) are deliberately NOT in here — they
live at a different layer because they need multi-run cohort context.
"""

from __future__ import annotations

import csv
import logging
import math
import shutil
import sqlite3
import tempfile
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)


# ── DIA-NN report.stats.tsv parser ──────────────────────────────────

def _parse_diann_stats_tsv(stats_path: Path) -> dict[str, float | None]:
    """Read DIA-NN's per-run stats file.

    DIA-NN writes `report.stats.tsv` alongside `report.parquet` with one row
    per input raw file and ~20 columns of chromatography + mass-accuracy
    metrics that are not repeated in report.parquet. Column names vary
    slightly across DIA-NN versions, so we match flexibly.

    Returns a dict with the fields STAN cares about, averaged across all
    rows in the file (one row = one run; averaging lets a multi-run batch
    report produce a single summary). Missing fields return None so
    downstream code can distinguish "not measured" from "zero".
    """
    empty = {
        "median_mass_acc_ms1_ppm": None,
        "median_mass_acc_ms2_ppm": None,
        "fwhm_scans": None,
        "fwhm_rt_min": None,
        "ms1_signal": None,
        "ms2_signal": None,
        "normalisation_factor": None,
    }
    if not stats_path.exists():
        return empty

    # DIA-NN uses slightly different header spellings between versions;
    # we try every known variant and take the first match.
    aliases = {
        "median_mass_acc_ms1_ppm": [
            "Median.Mass.Acc.MS1.Corrected", "Median.Mass.Acc.MS1",
            "MS1 mass accuracy", "Mass accuracy MS1",
        ],
        "median_mass_acc_ms2_ppm": [
            "Median.Mass.Acc.MS2.Corrected", "Median.Mass.Acc.MS2",
            "MS2 mass accuracy", "Mass accuracy MS2",
        ],
        "fwhm_scans":            ["FWHM.Scans", "Median.FWHM.Scans"],
        "fwhm_rt_min":           ["FWHM.RT", "Median.FWHM.RT"],
        "ms1_signal":            ["MS1.Signal", "MS1 signal"],
        "ms2_signal":            ["MS2.Signal", "MS2 signal"],
        "normalisation_factor":  ["Normalisation.Factor", "Normalisation factor"],
    }

    collected: dict[str, list[float]] = {k: [] for k in aliases}
    try:
        with stats_path.open() as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                for out_key, candidates in aliases.items():
                    for col in candidates:
                        v = row.get(col)
                        if v is None or v == "":
                            continue
                        try:
                            collected[out_key].append(float(v))
                            break  # first match wins per row
                        except ValueError:
                            pass
    except Exception:
        logger.exception("Failed to parse %s", stats_path)
        return empty

    return {
        k: (sum(v) / len(v)) if v else None
        for k, v in collected.items()
    }


# ── Chromatographic shape aggregations on report.parquet ────────────

def _rt_decile_peak_widths(df: pl.DataFrame) -> dict[str, float | None]:
    """Median peak width in early / middle / late thirds of the gradient.

    NIST MSQC C-4A / C-4B / C-4C. Distinguishes dead-volume broadening
    (early peaks wide) from column-collapse (late peaks wide) that a
    single median FWHM cannot separate.

    Needs the filtered precursor DataFrame with at least `RT` (or `RT.Start`
    + `RT.Stop` to derive per-peak width). Returns None values if columns
    aren't available.
    """
    if "RT" not in df.columns:
        return {"peak_width_early_sec": None, "peak_width_middle_sec": None, "peak_width_late_sec": None}
    if "RT.Start" not in df.columns or "RT.Stop" not in df.columns:
        return {"peak_width_early_sec": None, "peak_width_middle_sec": None, "peak_width_late_sec": None}

    w = df.with_columns(
        ((pl.col("RT.Stop") - pl.col("RT.Start")) * 60).alias("peak_width_sec")
    ).filter(pl.col("peak_width_sec") > 0)
    if w.height == 0:
        return {"peak_width_early_sec": None, "peak_width_middle_sec": None, "peak_width_late_sec": None}

    rt_min = float(w["RT"].min())
    rt_max = float(w["RT"].max())
    span = rt_max - rt_min
    if span <= 0:
        return {"peak_width_early_sec": None, "peak_width_middle_sec": None, "peak_width_late_sec": None}

    cut_a = rt_min + span / 3
    cut_b = rt_min + 2 * span / 3

    early  = w.filter(pl.col("RT") <  cut_a)["peak_width_sec"]
    middle = w.filter((pl.col("RT") >= cut_a) & (pl.col("RT") < cut_b))["peak_width_sec"]
    late   = w.filter(pl.col("RT") >= cut_b)["peak_width_sec"]
    return {
        "peak_width_early_sec":  float(early.median())  if len(early)  else None,
        "peak_width_middle_sec": float(middle.median()) if len(middle) else None,
        "peak_width_late_sec":   float(late.median())   if len(late)   else None,
    }


def _c2a_band(df: pl.DataFrame) -> dict[str, float | None]:
    """C-2A: the RT span covered by the middle 50% of IDs.

    If a 60-min gradient has IDs packed into the middle 20 min, the
    gradient is either wrong or the column has failed (retention loss).
    A healthy run should use most of the gradient — the middle-50%
    band should cover a substantial fraction of total RT span.

    Returns:
      c2a_rt_start_min, c2a_rt_stop_min — bounds of the middle 50% band
      c2a_width_min — total width of that band
      ids_per_minute_in_c2a — precursor density inside the band (a
        gradient-independent ID rate useful for cross-method comparison)
    """
    if "RT" not in df.columns:
        return {
            "c2a_rt_start_min": None, "c2a_rt_stop_min": None,
            "c2a_width_min": None, "ids_per_minute_in_c2a": None,
        }
    rts = df["RT"].drop_nulls()
    if len(rts) == 0:
        return {
            "c2a_rt_start_min": None, "c2a_rt_stop_min": None,
            "c2a_width_min": None, "ids_per_minute_in_c2a": None,
        }
    q25 = float(rts.quantile(0.25))
    q75 = float(rts.quantile(0.75))
    width = q75 - q25
    n_in_band = df.filter((pl.col("RT") >= q25) & (pl.col("RT") <= q75)).height
    return {
        "c2a_rt_start_min": q25,
        "c2a_rt_stop_min":  q75,
        "c2a_width_min":    width,
        "ids_per_minute_in_c2a": (n_in_band / width) if width > 0 else None,
    }


def _peak_capacity(median_peak_width_sec: float | None, gradient_min: float | None) -> float | None:
    """Single-number separation quality.

    n_c = 1 + t_grad / (4·σ), where σ = FWHM / 2.355 for a Gaussian peak.
    CPTAC system-suitability metric. Higher is better; typical good values
    for a 60 min Orbitrap DIA run land around 200-400.
    """
    if not median_peak_width_sec or median_peak_width_sec <= 0:
        return None
    if not gradient_min or gradient_min <= 0:
        return None
    sigma_sec = median_peak_width_sec / 2.355
    four_sigma_min = (4 * sigma_sec) / 60.0
    if four_sigma_min <= 0:
        return None
    return 1.0 + gradient_min / four_sigma_min


def _dynamic_range(df: pl.DataFrame) -> float | None:
    """log10(p99 / p01) of Precursor.Normalised intensity.

    Compresses when the source is dirty or the LC pressure drops because
    low-intensity precursors fall below the detection floor. QCloud2 /
    Practical Primer 2024 recommend this as a "single number" ion-current
    health indicator that survives ID-count-based comparisons.
    """
    if "Precursor.Normalised" not in df.columns:
        return None
    intens = df["Precursor.Normalised"].drop_nulls().filter(pl.col("Precursor.Normalised") > 0) \
        if False else df.filter(pl.col("Precursor.Normalised") > 0)["Precursor.Normalised"]
    if len(intens) < 10:
        return None
    p01 = float(intens.quantile(0.01))
    p99 = float(intens.quantile(0.99))
    if p01 <= 0 or p99 <= 0:
        return None
    return math.log10(p99 / p01)


def _compute_pts_peak_bruker(
    d_path: Path,
    report_df: pl.DataFrame,
    q_cutoff: float = 0.01,
    max_precursors: int = 2000,
) -> float | None:
    """Compute median points-across-peak from Bruker DIA window scheme.

    Reads the DIA window layout and frame times from analysis.tdf inside
    the .d directory, then for each precursor counts how many DIA frames
    fall within the precursor's elution window AND cover its m/z.

    Validated against Spectronaut on 12 Affinisep Dec 2025 test files.

    Args:
        d_path: Path to the .d directory (Bruker raw data).
        report_df: DIA-NN report DataFrame, already filtered to 1% FDR.
        q_cutoff: FDR threshold (used only if report_df is unfiltered).
        max_precursors: Subsample to this many precursors for performance.

    Returns:
        Median points-across-peak, or None if data is unavailable.
    """
    tdf_path = d_path / "analysis.tdf"
    if not tdf_path.exists():
        logger.debug("analysis.tdf not found in %s", d_path)
        return None

    # Required columns in report_df
    need_cols = {"RT.Start", "RT.Stop"}
    # Precursor.Mz may be named differently — check variants
    mz_col = None
    for candidate in ("Precursor.Mz", "Precursor.mz"):
        if candidate in report_df.columns:
            mz_col = candidate
            break
    if mz_col is None or not need_cols.issubset(set(report_df.columns)):
        logger.debug(
            "Missing columns for Bruker pts/peak (need Precursor.Mz, RT.Start, RT.Stop). "
            "Available: %s", report_df.columns
        )
        return None

    # Copy TDF to temp file — sqlite3 can't open files on network/Quobyte mounts
    try:
        tmp_dir = tempfile.mkdtemp(prefix="stan_tdf_")
        tmp_tdf = Path(tmp_dir) / "analysis.tdf"
        shutil.copy2(tdf_path, tmp_tdf)
    except Exception:
        logger.exception("Failed to copy analysis.tdf to temp dir")
        return None

    try:
        con = sqlite3.connect(str(tmp_tdf))

        # 1. Read DIA window scheme
        try:
            windows = con.execute(
                "SELECT WindowGroup, IsolationMz, IsolationWidth "
                "FROM DiaFrameMsMsWindows"
            ).fetchall()
        except sqlite3.OperationalError:
            logger.debug("DiaFrameMsMsWindows table not found in %s", tdf_path)
            con.close()
            return None

        if not windows:
            logger.debug("No DIA windows found in %s", tdf_path)
            con.close()
            return None

        # Build lookup: window_group -> list of (mz_low, mz_high)
        wg_windows: dict[int, list[tuple[float, float]]] = {}
        for wg, iso_mz, iso_width in windows:
            half = iso_width / 2.0
            wg_windows.setdefault(wg, []).append((iso_mz - half, iso_mz + half))

        # 2. Read DIA frame times with window groups.
        # DiaFrameMsMsInfo on some acquisitions has multiple rows per
        # Frame.Id (one per sub-window), and the naive JOIN then
        # returns Time repeated N times for that frame. The
        # points-across-peak counter below would then count one
        # precursor's matching frame up to N times, inflating the
        # metric by a factor of the window count per cycle — the
        # ~100x too-high values we saw on timsTOF Ultra 2 data
        # (median of 99k where normal is ~15).
        #
        # Safeguard: include Frame.Id and dedupe in Python. One
        # (frame_id, wg) pair per row; the counter iterates that
        # set directly, so a frame with multiple WGs contributes at
        # most one "hit" per precursor (the inner-loop `break`
        # handles that).
        try:
            rows_raw = con.execute(
                "SELECT f.Id, f.Time, i.WindowGroup "
                "FROM Frames f "
                "JOIN DiaFrameMsMsInfo i ON f.Id = i.Frame "
                "WHERE f.MsMsType = 9"
            ).fetchall()
        except sqlite3.OperationalError:
            logger.debug("Failed to query DIA frame times from %s", tdf_path)
            con.close()
            return None

        con.close()

        if not rows_raw:
            logger.debug("No diaPASEF frames found in %s", tdf_path)
            return None

        # Dedupe by (frame_id, window_group) — if the JOIN surfaced
        # duplicates for any reason, collapse them. Then group by
        # frame_id so each frame contributes EXACTLY ONE entry per
        # (time, {wg_set}) pair. The inner mz-match loop iterates
        # the wg_set and stops at the first hit (break), so a frame
        # that fires multiple WGs which all cover the precursor's
        # m/z still only contributes 1 to the count.
        by_frame: dict[int, tuple[float, set[int]]] = {}
        for fid, t, wg in rows_raw:
            entry = by_frame.get(fid)
            if entry is None:
                by_frame[fid] = (t, {wg})
            else:
                entry[1].add(wg)

        frames = [(t, wgs) for (t, wgs) in by_frame.values()]
        frames.sort(key=lambda x: x[0])
        frame_times = [f[0] for f in frames]
        frame_wg_sets = [f[1] for f in frames]

        # 3. For each precursor, count DIA frames covering its m/z within its RT window
        # Subsample for performance
        prec = report_df.select([mz_col, "RT.Start", "RT.Stop"]).drop_nulls()
        if prec.height == 0:
            return None

        if prec.height > max_precursors:
            prec = prec.sample(n=max_precursors, seed=42)

        mz_vals = prec[mz_col].to_list()
        rt_starts = prec["RT.Start"].to_list()  # in minutes
        rt_stops = prec["RT.Stop"].to_list()     # in minutes

        import bisect

        counts = []
        n_frames = len(frame_times)
        for mz, rt_start, rt_stop in zip(mz_vals, rt_starts, rt_stops):
            # Convert RT from minutes to seconds (analysis.tdf times are in seconds)
            t_start = rt_start * 60.0
            t_stop = rt_stop * 60.0

            # Binary search for frame time range
            i_lo = bisect.bisect_left(frame_times, t_start)
            i_hi = bisect.bisect_right(frame_times, t_stop)

            count = 0
            for i in range(i_lo, min(i_hi, n_frames)):
                # A frame is counted as "covering" the precursor if ANY
                # of its window groups has a window whose m/z range
                # includes the precursor m/z. We stop at the first hit.
                frame_matches = False
                for wg in frame_wg_sets[i]:
                    mz_ranges = wg_windows.get(wg)
                    if not mz_ranges:
                        continue
                    for mz_lo, mz_hi in mz_ranges:
                        if mz_lo <= mz <= mz_hi:
                            frame_matches = True
                            break
                    if frame_matches:
                        break
                if frame_matches:
                    count += 1
            counts.append(count)

        if not counts:
            return None

        # Median
        counts.sort()
        n = len(counts)
        if n % 2 == 1:
            return float(counts[n // 2])
        else:
            return float((counts[n // 2 - 1] + counts[n // 2]) / 2.0)

    except Exception:
        logger.exception("Failed to compute Bruker pts/peak from %s", d_path)
        return None
    finally:
        # Clean up temp copy
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


def extract_dia_metrics(
    report_path: Path,
    q_cutoff: float = 0.01,
    gradient_min: float | None = None,
    raw_path: Path | None = None,
    vendor: str | None = None,
) -> dict:
    """Extract DIA QC metrics from DIA-NN report.parquet.

    Args:
        report_path: Path to DIA-NN report.parquet output. report.stats.tsv
            is read automatically if present in the same directory.
        q_cutoff: FDR threshold (default 1%).
        gradient_min: Gradient length in minutes. Used to compute peak
            capacity. If None, peak_capacity will be None in the result.
        raw_path: Path to the raw data file/directory (.d for Bruker).
            Used for accurate pts/peak calculation on Bruker instruments.
        vendor: Instrument vendor ("bruker" or "thermo"). When "bruker"
            and raw_path is a .d directory, pts/peak is computed from
            DIA window coverage in analysis.tdf.

    Returns:
        Dict of metric name → value.
    """
    # Read all available columns first to discover RT-related columns
    all_cols = pl.read_parquet_schema(report_path)
    available = set(all_cols.keys()) if hasattr(all_cols, 'keys') else set(all_cols)

    # DIA-NN 2.0 renamed File.Name → Run — detect which exists
    file_col = "Run" if "Run" in available else "File.Name"

    # Required columns
    want = [
        "Precursor.Id", "Stripped.Sequence", "Protein.Group",
        "Q.Value", "PG.Q.Value", "Fragment.Info", "Fragment.Quant.Corrected",
        "Precursor.Charge", "Missed.Cleavages", file_col, "Precursor.Normalised",
        "Precursor.Mz",
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

    # Normalize DIA-NN 2.0 "Run" column to "File.Name" for consistency
    if file_col == "Run" and "Run" in df.columns:
        df = df.rename({"Run": "File.Name"})

    filt = df.filter(pl.col("Q.Value") <= q_cutoff)

    if filt.height == 0:
        logger.warning("No precursors pass FDR threshold %.2f in %s", q_cutoff, report_path)
        return _empty_dia_metrics()

    # Fragment counts per precursor (columns may not exist in DIA-NN 2.0+)
    has_fragment_info = "Fragment.Info" in available
    has_fragment_quant = "Fragment.Quant.Corrected" in available
    if has_fragment_info and has_fragment_quant:
        filt = filt.with_columns(
            (pl.col("Fragment.Info").str.count_matches(";") + 1).alias("n_frag_extracted"),
            pl.col("Fragment.Quant.Corrected")
            .map_elements(
                lambda s: sum(1 for x in str(s).split(";") if x.strip() and float(x) > 0),
                return_dtype=pl.Int32,
            )
            .alias("n_frag_quantified"),
        )
    else:
        logger.debug("Fragment.Info/Fragment.Quant.Corrected not in report — skipping fragment metrics")

    # CV across replicates (if multiple files). Single-injection QC runs
    # (the common case for STAN) have no replicates and CV is undefined —
    # write None / NULL rather than 0.0 so the dashboard and community
    # schema can distinguish "no replicate available" from "replicate had
    # zero variance" (which would be suspicious in itself).
    n_files = filt["File.Name"].n_unique()
    median_cv: float | None = None
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
        median_cv = float(cv_df["cv"].median()) if cv_df.height > 0 else None

    # Charge state distribution
    charge = (
        filt.group_by("Precursor.Charge")
        .agg(pl.len().alias("n"))
        .with_columns((pl.col("n") / pl.col("n").sum()).alias("pct"))
    )

    def charge_pct(z: int) -> float:
        row = charge.filter(pl.col("Precursor.Charge") == z)
        return float(row["pct"][0]) if len(row) else 0.0

    total_frag_extracted = filt["n_frag_extracted"].sum() if "n_frag_extracted" in filt.columns else 0
    total_frag_quantified = filt["n_frag_quantified"].sum() if "n_frag_quantified" in filt.columns else 0

    # ── Points across peak (Matthews & Hayes 1976) ────────────────
    # For Bruker timsTOF: count actual DIA frames covering each precursor's
    # m/z within its elution window using the DIA scheme from analysis.tdf.
    # For Thermo (or when .d path unavailable): fall back to cycle-time estimate.
    median_peak_width_sec: float | None = None
    median_points_across_peak: float | None = None

    if "RT.Start" in filt.columns and "RT.Stop" in filt.columns:
        # Direct peak width from elution window boundaries
        peak_widths = filt.with_columns(
            ((pl.col("RT.Stop") - pl.col("RT.Start")) * 60).alias("peak_width_sec")
        ).filter(pl.col("peak_width_sec") > 0)

        if peak_widths.height > 0:
            median_peak_width_sec = float(peak_widths["peak_width_sec"].median())

        # Bruker: use DIA window coverage from analysis.tdf (accurate method)
        is_bruker = (
            vendor == "bruker"
            or (raw_path is not None and str(raw_path).endswith(".d"))
        )
        d_path = raw_path if (raw_path and raw_path.suffix == ".d") else None
        if is_bruker and d_path and d_path.is_dir():
            bruker_pts = _compute_pts_peak_bruker(d_path, filt)
            if bruker_pts is not None:
                median_points_across_peak = bruker_pts

        # Thermo / fallback: cycle-time estimate (will be fixed in v0.2.106)
        if median_points_across_peak is None and not is_bruker:
            fc = "Run" if "Run" in filt.columns else (
                "File.Name" if "File.Name" in filt.columns else None
            )
            if "RT" in filt.columns and fc is not None:
                cycle_times = (
                    filt.sort([fc, "RT"])
                    .with_columns(
                        (pl.col("RT").diff().over(fc) * 60).alias("dt_sec")
                    )
                    .filter(pl.col("dt_sec") > 0)
                    .filter(pl.col("dt_sec") < 10)  # filter outliers (>10s gaps)
                )
                if cycle_times.height > 0 and median_peak_width_sec:
                    median_cycle_sec = float(cycle_times["dt_sec"].median())
                    if median_cycle_sec > 0:
                        median_points_across_peak = median_peak_width_sec / median_cycle_sec

    elif "RT" in filt.columns and evidence_col and evidence_col in filt.columns:
        # Fallback: use Evidence column (number of scans supporting the ID)
        evidence_vals = filt[evidence_col].drop_nulls()
        if evidence_vals.len() > 0:
            median_points_across_peak = float(evidence_vals.median())

    # ── Literature-survey metrics (added 2026-04) ──────────────────
    # These are all free from data DIA-NN already produces.

    # Dynamic range: log10(p99/p01) of precursor intensity
    dyn_range = _dynamic_range(filt)

    # Peak capacity: single-number LC separation quality
    peak_cap = _peak_capacity(median_peak_width_sec, gradient_min)

    # RT-decile peak widths: early/middle/late FWHM
    rt_deciles = _rt_decile_peak_widths(filt)

    # C-2A: gradient utilization (middle 50% RT band)
    c2a = _c2a_band(filt)

    # Missed cleavages ≥2 (more sensitive than ≥1 for digestion quality)
    has_mc = "Missed.Cleavages" in filt.columns
    mc2_rate = float(
        filt.filter(pl.col("Missed.Cleavages") >= 2).height / filt.height
    ) if has_mc and filt.height > 0 else 0.0

    # Median precursor intensity
    median_intensity = None
    if "Precursor.Normalised" in filt.columns:
        vals = filt["Precursor.Normalised"].drop_nulls()
        if len(vals) > 0:
            median_intensity = float(vals.median())

    # Parse report.stats.tsv for mass accuracy + FWHM + signal
    stats_path = Path(report_path).parent / "report.stats.tsv"
    stats = _parse_diann_stats_tsv(stats_path)

    return {
        "n_precursors": filt["Precursor.Id"].n_unique(),
        "n_peptides": filt["Stripped.Sequence"].n_unique() if "Stripped.Sequence" in filt.columns else 0,
        "n_proteins": filt.filter(pl.col("PG.Q.Value") <= q_cutoff)[
            "Protein.Group"
        ].n_unique() if "PG.Q.Value" in filt.columns and "Protein.Group" in filt.columns else 0,
        "median_fragments_per_precursor": float(filt["n_frag_extracted"].median()) if "n_frag_extracted" in filt.columns else None,
        "pct_fragments_quantified": (
            float(total_frag_quantified / total_frag_extracted)
            if total_frag_extracted > 0
            else 0.0
        ),
        "median_cv_precursor": median_cv,
        "missed_cleavage_rate": float(
            filt.filter(pl.col("Missed.Cleavages") >= 1).height / filt.height
        ) if has_mc else 0.0,
        "missed_cleavage_rate_2plus": mc2_rate,
        "pct_charge_1": charge_pct(1),
        "pct_charge_2": charge_pct(2),
        "pct_charge_3": charge_pct(3),
        "pct_charge_4plus": sum(charge_pct(z) for z in range(4, 10)),
        "median_peak_width_sec": median_peak_width_sec,
        "median_points_across_peak": median_points_across_peak,
        # Literature-survey metrics
        "dynamic_range_log10": dyn_range,
        "peak_capacity": peak_cap,
        "median_precursor_intensity": median_intensity,
        # v0.2.212: search-engine provenance recorded at extract time so
        # the live watcher path doesn't have to remember to populate it.
        # Required for community submissions — relay rejects null versions.
        "search_engine": "diann",
        "diann_version": _detect_engine_version(),
        **rt_deciles,
        **c2a,
        **stats,
    }


def _detect_engine_version() -> str:
    """Best-effort lookup of the installed DIA-NN binary's version.

    Returns "unknown" if detection fails — never raises, never returns
    None (NULL diann_version is what we're trying to eliminate).
    """
    try:
        from stan.search.version_detect import detect_diann_version
        return detect_diann_version() or "unknown"
    except Exception:
        return "unknown"


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
        # v0.2.212: DDA QC searches with Sage on STAN.
        "search_engine": "sage",
        "diann_version": None,  # not applicable to DDA
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
        "median_fragments_per_precursor": None,
        "pct_fragments_quantified": 0.0,
        "median_cv_precursor": None,
        "missed_cleavage_rate": 0.0,
        "missed_cleavage_rate_2plus": 0.0,
        "pct_charge_1": 0.0,
        "pct_charge_2": 0.0,
        "pct_charge_3": 0.0,
        "pct_charge_4plus": 0.0,
        "median_peak_width_sec": None,
        "median_points_across_peak": None,
        "dynamic_range_log10": None,
        "peak_capacity": None,
        "median_precursor_intensity": None,
        "peak_width_early_sec": None,
        "peak_width_middle_sec": None,
        "peak_width_late_sec": None,
        "c2a_rt_start_min": None,
        "c2a_rt_stop_min": None,
        "c2a_width_min": None,
        "ids_per_minute_in_c2a": None,
        "median_mass_acc_ms1_ppm": None,
        "median_mass_acc_ms2_ppm": None,
        "fwhm_scans": None,
        "fwhm_rt_min": None,
        "ms1_signal": None,
        "ms2_signal": None,
        "normalisation_factor": None,
        "search_engine": "diann",
        "diann_version": _detect_engine_version(),
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
