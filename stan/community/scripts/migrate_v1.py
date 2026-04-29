"""One-shot v1.0 migration of the community benchmark dataset.

Re-runs the v1.0 normalization on every existing submission, splits
into v1 / historical / quarantine, and (in --apply mode) uploads the
three published parquets to the HF Dataset.

USAGE
    # Dry-run (default) — writes report to /tmp, never touches HF
    python -m stan.community.scripts.migrate_v1

    # Apply — re-uploads benchmark_latest.parquet, benchmark_historical.parquet,
    # benchmark_quarantine.parquet to the HF Dataset.
    python -m stan.community.scripts.migrate_v1 --apply

The original `submissions/<id>.parquet` files are NEVER modified.

Requires HF_TOKEN env var (or ~/.cache/huggingface/token).
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
from pathlib import Path

import polars as pl

from stan.community.normalize_v1 import normalize

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

HF_DATASET_REPO = "brettsp/stan-benchmark"
REPORT_DIR = Path("/tmp/stan_migrate_v1")


def _token() -> str:
    p = Path.home() / ".cache" / "huggingface" / "token"
    if p.exists():
        return p.read_text().strip()
    return os.environ.get("HF_TOKEN", "")


def _load_all_submissions(api, token: str) -> pl.DataFrame:
    from huggingface_hub import hf_hub_download

    files = api.list_repo_files(repo_id=HF_DATASET_REPO, repo_type="dataset")
    sub_files = sorted(f for f in files if f.startswith("submissions/") and f.endswith(".parquet"))
    logger.info("Loading %d submission files...", len(sub_files))

    dfs = []
    for fname in sub_files:
        try:
            local = hf_hub_download(
                repo_id=HF_DATASET_REPO, filename=fname, repo_type="dataset", token=token
            )
            dfs.append(pl.read_parquet(local))
        except Exception:
            logger.exception("Failed to read %s", fname)

    if not dfs:
        raise SystemExit("No submissions to migrate")

    return pl.concat(dfs, how="diagonal_relaxed")


def _write_report(splits: dict[str, pl.DataFrame], original: pl.DataFrame) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    summary = {
        "original_rows": original.height,
        "original_columns": sorted(original.columns),
        "v1_rows": splits["v1"].height,
        "historical_rows": splits["historical"].height,
        "quarantine_rows": splits["quarantine"].height,
        "v1_unique_cohorts": (
            splits["v1"]["cohort_id"].n_unique() if splits["v1"].height else 0
        ),
        "historical_unique_cohorts": (
            splits["historical"]["cohort_id"].n_unique() if splits["historical"].height else 0
        ),
        "v1_mode_distribution": (
            dict(zip(
                splits["v1"]["acquisition_mode"].value_counts(sort=True)["acquisition_mode"].to_list(),
                splits["v1"]["acquisition_mode"].value_counts(sort=True)["count"].to_list()
            )) if splits["v1"].height else {}
        ),
    }

    (REPORT_DIR / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    splits["v1"].write_parquet(REPORT_DIR / "benchmark_latest.parquet")
    splits["historical"].write_parquet(REPORT_DIR / "benchmark_historical.parquet")
    splits["quarantine"].write_parquet(REPORT_DIR / "benchmark_quarantine.parquet")
    logger.info("Report written to %s", REPORT_DIR)
    logger.info("Summary: %s", json.dumps(summary, indent=2, default=str))


def _upload_parquet(api, df: pl.DataFrame, path_in_repo: str) -> None:
    if df.height == 0:
        logger.warning("Skipping upload of empty %s", path_in_repo)
        return
    buf = io.BytesIO()
    df.write_parquet(buf)
    buf.seek(0)
    api.upload_file(
        path_or_fileobj=buf,
        path_in_repo=path_in_repo,
        repo_id=HF_DATASET_REPO,
        repo_type="dataset",
        commit_message=f"v1.0 migration: republish {path_in_repo} ({df.height} rows)",
    )
    logger.info("Uploaded %s (%d rows)", path_in_repo, df.height)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Upload normalized parquets to HF Dataset"
    )
    args = parser.parse_args()

    token = _token()
    if not token:
        logger.error("No HF token found")
        sys.exit(1)

    from huggingface_hub import HfApi

    api = HfApi(token=token)
    original = _load_all_submissions(api, token)
    logger.info("Loaded %d total rows", original.height)

    splits = normalize(original)
    _write_report(splits, original)

    if not args.apply:
        logger.info("DRY-RUN complete. Review %s and re-run with --apply.", REPORT_DIR)
        return

    logger.warning("APPLY mode — uploading to HF Dataset %s", HF_DATASET_REPO)
    _upload_parquet(api, splits["v1"], "benchmark_latest.parquet")
    _upload_parquet(api, splits["historical"], "benchmark_historical.parquet")
    _upload_parquet(api, splits["quarantine"], "benchmark_quarantine.parquet")
    logger.info("Migration complete.")


if __name__ == "__main__":
    main()
