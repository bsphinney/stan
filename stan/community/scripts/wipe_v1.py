"""Wipe brettsp/stan-benchmark and reset for v1.0 launch.

Three modes:
  --backup   (read-only): mirror every submission + published parquet
             to /tmp/stan_pre_v1_backup/<timestamp>/ AND upload the
             same tarball to a `pre-v1-snapshot/` directory in the HF
             Dataset itself so it's recoverable cloud-side.
  --wipe     (destructive): delete every file under submissions/,
             every top-level benchmark_*.parquet, and cohort_stats/.
             Refuses to run unless --backup completed within the last
             24h (checks /tmp/stan_pre_v1_backup/.last_backup).
  --init-empty: upload an empty parquet with the v1.0 schema to
             benchmark_latest.parquet so the HF Space dashboard
             doesn't crash on a missing file.

USAGE
    python -m stan.community.scripts.wipe_v1 --backup
    python -m stan.community.scripts.wipe_v1 --wipe
    python -m stan.community.scripts.wipe_v1 --init-empty

Requires HF_TOKEN env var (or ~/.cache/huggingface/token).
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

HF_DATASET_REPO = "brettsp/stan-benchmark"
BACKUP_ROOT = Path("/tmp/stan_pre_v1_backup")
LAST_BACKUP_FLAG = BACKUP_ROOT / ".last_backup"
BACKUP_MAX_AGE_SEC = 86400  # 24h


def _token() -> str:
    p = Path.home() / ".cache" / "huggingface" / "token"
    if p.exists():
        return p.read_text().strip()
    return os.environ.get("HF_TOKEN", "")


def _backup(api, token: str) -> None:
    """Download every submission + published parquet to /tmp + push to HF."""
    from huggingface_hub import hf_hub_download

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    snapshot_dir = BACKUP_ROOT / ts
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    files = api.list_repo_files(repo_id=HF_DATASET_REPO, repo_type="dataset")
    targets = [
        f for f in files
        if f.startswith("submissions/")
        or f.startswith("benchmark_")
        or f.startswith("cohort_stats/")
    ]
    logger.info("Backing up %d files to %s", len(targets), snapshot_dir)

    for f in targets:
        try:
            local = hf_hub_download(
                repo_id=HF_DATASET_REPO, filename=f, repo_type="dataset", token=token,
            )
            dest = snapshot_dir / f
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(Path(local).read_bytes())
        except Exception:
            logger.exception("Failed to backup %s", f)

    # Cloud-side mirror: push every backed-up file under pre-v1-snapshot/<ts>/
    logger.info("Mirroring backup to HF Dataset pre-v1-snapshot/%s/", ts)
    for f in targets:
        local_path = snapshot_dir / f
        if not local_path.exists():
            continue
        try:
            api.upload_file(
                path_or_fileobj=str(local_path),
                path_in_repo=f"pre-v1-snapshot/{ts}/{f}",
                repo_id=HF_DATASET_REPO,
                repo_type="dataset",
                commit_message=f"backup: pre-v1 snapshot {ts}",
            )
        except Exception:
            logger.exception("Failed to upload backup mirror for %s", f)

    LAST_BACKUP_FLAG.write_text(ts)
    logger.info("Backup complete: %d files in %s", len(targets), snapshot_dir)
    logger.info("Cloud mirror at HF: pre-v1-snapshot/%s/", ts)


def _check_recent_backup() -> str:
    """Refuse to wipe if no backup within BACKUP_MAX_AGE_SEC."""
    if not LAST_BACKUP_FLAG.exists():
        raise SystemExit(
            "No backup flag at /tmp/stan_pre_v1_backup/.last_backup — "
            "run --backup first."
        )
    age = time.time() - LAST_BACKUP_FLAG.stat().st_mtime
    if age > BACKUP_MAX_AGE_SEC:
        raise SystemExit(
            f"Last backup is {age / 3600:.1f}h old (>24h). Re-run --backup."
        )
    return LAST_BACKUP_FLAG.read_text()


def _wipe(api, token: str) -> None:
    backup_ts = _check_recent_backup()
    logger.warning(
        "WIPE: deleting every submissions/*.parquet, benchmark_*.parquet, "
        "and cohort_stats/* in %s (backup=%s)", HF_DATASET_REPO, backup_ts,
    )
    print(">>> Press Ctrl-C in 5s to abort...", file=sys.stderr)
    time.sleep(5)

    files = api.list_repo_files(repo_id=HF_DATASET_REPO, repo_type="dataset")
    # NEVER delete the snapshot we just made
    targets = [
        f for f in files
        if (
            f.startswith("submissions/")
            or f.startswith("benchmark_")
            or f.startswith("cohort_stats/")
        )
        and not f.startswith("pre-v1-snapshot/")
    ]

    deleted = 0
    failed = 0
    for f in targets:
        try:
            api.delete_file(
                path_in_repo=f,
                repo_id=HF_DATASET_REPO,
                repo_type="dataset",
                commit_message=f"wipe: pre-v1 cleanup ({f})",
            )
            deleted += 1
        except Exception:
            logger.exception("Failed to delete %s", f)
            failed += 1

    logger.info("Wipe complete: deleted=%d failed=%d", deleted, failed)


def _init_empty(api, token: str) -> None:
    """Upload an empty parquet with the v1.0 schema columns."""
    schema = {
        "submission_id": pl.Utf8,
        "stan_version": pl.Utf8,
        "schema_version": pl.Utf8,
        "display_name": pl.Utf8,
        "instrument_family": pl.Utf8,
        "instrument_model": pl.Utf8,
        "acquisition_mode": pl.Utf8,
        "spd": pl.Int64,
        "gradient_length_min": pl.Int64,
        "amount_ng": pl.Float64,
        "n_precursors": pl.Int64,
        "n_peptides": pl.Int64,
        "n_proteins": pl.Int64,
        "n_psms": pl.Int64,
        "median_cv_precursor": pl.Float64,
        "median_fragments_per_precursor": pl.Float64,
        "ips_score": pl.Int64,
        "missed_cleavage_rate": pl.Float64,
        "cohort_id": pl.Utf8,
        "sample_type": pl.Utf8,
        "fingerprint": pl.Utf8,
        "diann_version": pl.Utf8,
        "column_vendor": pl.Utf8,
        "column_model": pl.Utf8,
        "lc_system": pl.Utf8,
        "run_name": pl.Utf8,
        "run_date": pl.Utf8,
        "submitted_at": pl.Utf8,
        "fasta_md5": pl.Utf8,
        "speclib_md5": pl.Utf8,
        "assets_verified": pl.Boolean,
        "community_score": pl.Float32,
        "is_flagged": pl.Boolean,
        "ms1_signal": pl.Float64,
        "ms2_signal": pl.Float64,
        "fwhm_rt_min": pl.Float64,
        "median_mass_acc_ms1_ppm": pl.Float64,
        "median_mass_acc_ms2_ppm": pl.Float64,
        "peak_capacity": pl.Float64,
        "dynamic_range_log10": pl.Float64,
        "tic_rt_bins": pl.List(pl.Float64),
        "tic_intensity": pl.List(pl.Float64),
    }
    df = pl.DataFrame(schema=schema)
    buf = io.BytesIO()
    df.write_parquet(buf)
    buf.seek(0)
    api.upload_file(
        path_or_fileobj=buf,
        path_in_repo="benchmark_latest.parquet",
        repo_id=HF_DATASET_REPO,
        repo_type="dataset",
        commit_message="init: empty v1.0 benchmark_latest.parquet",
    )
    logger.info("Uploaded empty benchmark_latest.parquet (%d cols, 0 rows)", len(schema))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--backup", action="store_true")
    p.add_argument("--wipe", action="store_true")
    p.add_argument("--init-empty", action="store_true")
    args = p.parse_args()

    if not (args.backup or args.wipe or args.init_empty):
        p.print_help()
        sys.exit(1)

    token = _token()
    if not token:
        logger.error("No HF token found")
        sys.exit(1)

    from huggingface_hub import HfApi

    api = HfApi(token=token)

    if args.backup:
        _backup(api, token)
    if args.wipe:
        _wipe(api, token)
    if args.init_empty:
        _init_empty(api, token)


if __name__ == "__main__":
    main()
