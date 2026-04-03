"""Nightly consolidation script for the community benchmark.

Runs via GitHub Actions at 4am UTC:
1. Downloads all submissions/*.parquet from HF Dataset
2. Validates each submission
3. Computes cohort percentiles and community scores
4. Writes benchmark_latest.parquet and cohort_percentiles_latest.json back

Usage:
    python stan/community/scripts/consolidate.py

Requires HF_TOKEN environment variable.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

HF_DATASET_REPO = "brettsp/stan-benchmark"
COHORT_MINIMUM = 5


def main() -> None:
    """Run nightly consolidation."""
    token = os.environ.get("HF_TOKEN", "")
    if not token:
        logger.error("HF_TOKEN not set")
        sys.exit(1)

    from huggingface_hub import HfApi

    api = HfApi(token=token)

    # 1. List all submission files
    logger.info("Listing submissions...")
    files = api.list_repo_files(repo_id=HF_DATASET_REPO, repo_type="dataset")
    submission_files = [f for f in files if f.startswith("submissions/") and f.endswith(".parquet")]
    logger.info("Found %d submission files", len(submission_files))

    if not submission_files:
        logger.info("No submissions to consolidate")
        return

    # 2. Download and concatenate all submissions
    from huggingface_hub import hf_hub_download

    dfs: list[pl.DataFrame] = []
    for fname in submission_files:
        try:
            local_path = hf_hub_download(
                repo_id=HF_DATASET_REPO,
                filename=fname,
                repo_type="dataset",
                token=token,
            )
            df = pl.read_parquet(local_path)
            dfs.append(df)
        except Exception:
            logger.exception("Failed to read %s — skipping", fname)

    if not dfs:
        logger.warning("No valid submissions loaded")
        return

    all_submissions = pl.concat(dfs, how="diagonal_relaxed")
    logger.info("Total submissions: %d", all_submissions.height)

    # 3. Compute cohort percentiles
    cohort_stats = _compute_cohort_percentiles(all_submissions)

    # 4. Compute community scores
    scored = _compute_community_scores(all_submissions, cohort_stats)

    # 5. Separate flagged vs clean
    flagged = scored.filter(pl.col("is_flagged"))
    clean = scored.filter(~pl.col("is_flagged"))

    # 6. Upload results
    _upload_parquet(api, token, clean, "benchmark_latest.parquet")
    if flagged.height > 0:
        _upload_parquet(api, token, flagged, "benchmark_flagged.parquet")

    _upload_json(api, token, cohort_stats, "cohort_stats/cohort_percentiles_latest.json")

    logger.info("Consolidation complete. %d clean, %d flagged", clean.height, flagged.height)


def _compute_cohort_percentiles(df: pl.DataFrame) -> dict:
    """Compute percentile arrays per cohort for scoring."""
    cohorts: dict = {}

    for cohort_id in df["cohort_id"].unique().to_list():
        cohort_df = df.filter(pl.col("cohort_id") == cohort_id)
        n = cohort_df.height

        stats: dict = {"n_submissions": n}

        if n >= COHORT_MINIMUM:
            for metric in [
                "n_precursors", "n_peptides", "n_psms", "n_peptides_dda",
                "median_cv_precursor", "grs_score", "pct_delta_mass_lt5ppm",
                "ms2_scan_rate",
            ]:
                if metric in cohort_df.columns:
                    values = cohort_df[metric].drop_nulls().sort().to_list()
                    stats[metric] = values

        cohorts[cohort_id] = stats

    return cohorts


def _compute_community_scores(df: pl.DataFrame, cohort_stats: dict) -> pl.DataFrame:
    """Recompute community_score for each submission based on cohort percentiles."""
    scores: list[float] = []

    for row in df.iter_rows(named=True):
        cohort_id = row.get("cohort_id", "")
        cohort = cohort_stats.get(cohort_id, {})
        mode = (row.get("acquisition_mode") or "").lower()

        if cohort.get("n_submissions", 0) < COHORT_MINIMUM:
            scores.append(0.0)
            continue

        if "dia" in mode:
            score = _dia_score(row, cohort)
        elif "dda" in mode:
            score = _dda_score(row, cohort)
        else:
            score = 0.0

        scores.append(round(score, 1))

    return df.with_columns(pl.Series("community_score", scores, dtype=pl.Float32))


def _dia_score(row: dict, cohort: dict) -> float:
    """DIA composite score within cohort."""
    return (
        0.40 * _pctile(row.get("n_precursors", 0), cohort.get("n_precursors", []))
        + 0.25 * _pctile(row.get("n_peptides", 0), cohort.get("n_peptides", []))
        + 0.20 * (100 - _pctile(row.get("median_cv_precursor", 0), cohort.get("median_cv_precursor", [])))
        + 0.15 * _pctile(row.get("grs_score", 0), cohort.get("grs_score", []))
    )


def _dda_score(row: dict, cohort: dict) -> float:
    """DDA composite score within cohort."""
    return (
        0.35 * _pctile(row.get("n_psms", 0), cohort.get("n_psms", []))
        + 0.25 * _pctile(row.get("n_peptides_dda", 0), cohort.get("n_peptides_dda", []))
        + 0.20 * _pctile(row.get("pct_delta_mass_lt5ppm", 0), cohort.get("pct_delta_mass_lt5ppm", []))
        + 0.20 * _pctile(row.get("ms2_scan_rate", 0), cohort.get("ms2_scan_rate", []))
    )


def _pctile(value: float, sorted_values: list) -> float:
    """Percentile rank of value within sorted_values (0–100)."""
    if not sorted_values:
        return 50.0
    n = len(sorted_values)
    below = sum(1 for v in sorted_values if v < value)
    equal = sum(1 for v in sorted_values if v == value)
    return min(100.0, max(0.0, (below + 0.5 * equal) / n * 100))


def _upload_parquet(api, token: str, df: pl.DataFrame, path_in_repo: str) -> None:
    """Upload a Polars DataFrame as parquet to HF Dataset."""
    buf = io.BytesIO()
    df.write_parquet(buf)
    buf.seek(0)
    api.upload_file(
        path_or_fileobj=buf,
        path_in_repo=path_in_repo,
        repo_id=HF_DATASET_REPO,
        repo_type="dataset",
    )
    logger.info("Uploaded %s (%d rows)", path_in_repo, df.height)


def _upload_json(api, token: str, data: dict, path_in_repo: str) -> None:
    """Upload a dict as JSON to HF Dataset."""
    buf = io.BytesIO(json.dumps(data, default=str).encode())
    buf.seek(0)
    api.upload_file(
        path_or_fileobj=buf,
        path_in_repo=path_in_repo,
        repo_id=HF_DATASET_REPO,
        repo_type="dataset",
    )
    logger.info("Uploaded %s", path_in_repo)


if __name__ == "__main__":
    main()
