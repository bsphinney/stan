"""Submit QC metrics to the community HeLa benchmark on HF Dataset.

Submissions are individual parquet files uploaded to the submissions/ directory
of the HF Dataset repo. Raw files are NEVER uploaded — metrics only.
"""

from __future__ import annotations

import io
import logging
import uuid
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq

from stan import __version__
from stan.community.validate import validate_submission
from stan.config import load_community
from stan.db import mark_submitted
from stan.metrics.scoring import compute_cohort_id

logger = logging.getLogger(__name__)

HF_DATASET_REPO = "bsphinney/stan-community-benchmark"

# Submission parquet schema
SUBMISSION_SCHEMA = pa.schema([
    pa.field("submission_id", pa.string()),
    pa.field("submitted_at", pa.timestamp("us", tz="UTC")),
    pa.field("stan_version", pa.string()),
    pa.field("display_name", pa.string()),
    pa.field("instrument_family", pa.string()),
    pa.field("instrument_model", pa.string()),
    pa.field("acquisition_mode", pa.string()),
    pa.field("spd", pa.int32()),
    pa.field("gradient_length_min", pa.int32()),
    pa.field("amount_ng", pa.float32()),
    pa.field("n_precursors", pa.int32()),
    pa.field("n_peptides", pa.int32()),
    pa.field("n_proteins", pa.int32()),
    pa.field("n_psms", pa.int32()),
    pa.field("median_cv_precursor", pa.float32()),
    pa.field("median_fragments_per_precursor", pa.float32()),
    pa.field("grs_score", pa.int32()),
    pa.field("missed_cleavage_rate", pa.float32()),
    pa.field("community_score", pa.float32()),
    pa.field("cohort_id", pa.string()),
    pa.field("is_flagged", pa.bool_()),
])


def submit_to_benchmark(
    run: dict,
    spd: int | None = None,
    gradient_length_min: int | None = None,
    amount_ng: float = 50.0,
    hela_source: str = "Pierce HeLa Protein Digest Standard",
    asset_hashes: dict[str, str] | None = None,
) -> dict:
    """Submit a QC run to the community benchmark.

    Args:
        run: Run dict from the local SQLite database.
        spd: Samples per day (primary throughput — e.g. 60 for Evosep 60 SPD).
            If not provided, falls back to gradient_length_min.
        gradient_length_min: Gradient length in minutes (fallback for custom LC).
        amount_ng: Amount of HeLa digest injected (ng).
        hela_source: HeLa digest source/vendor.
        asset_hashes: MD5 hashes of the speclib and FASTA used in the search.
            Keys: "fasta_md5", "speclib_md5". Used to verify the correct
            community library was used.

    Returns:
        Dict with submission_id and status.

    Raises:
        ValueError: If submission fails validation.
        RuntimeError: If HF token is not configured.
    """
    community_config = load_community()
    hf_token = community_config.get("hf_token", "")
    if not hf_token:
        raise RuntimeError(
            "HF token not configured. Set hf_token in ~/.stan/community.yml"
        )

    display_name = community_config.get("display_name", "") or "Anonymous Lab"
    mode = run.get("mode", "").lower()

    # Validate (including library hash verification)
    from stan.search.community_params import SEARCH_PARAMS_VERSION

    validation = validate_submission(
        run, mode,
        search_params_version=SEARCH_PARAMS_VERSION,
        asset_hashes=asset_hashes,
    )
    if not validation.is_valid:
        raise ValueError(
            f"Submission rejected: {'; '.join(validation.rejected_gates)}"
        )

    # Build submission
    submission_id = str(uuid.uuid4())
    instrument = run.get("instrument", "")
    instrument_family = _instrument_family(instrument)

    cohort_id = compute_cohort_id(
        instrument_family, amount_ng, spd=spd, gradient_min=gradient_length_min,
    )

    row = {
        "submission_id": [submission_id],
        "submitted_at": [datetime.now(timezone.utc)],
        "stan_version": [__version__],
        "display_name": [display_name],
        "instrument_family": [instrument_family],
        "instrument_model": [instrument],
        "acquisition_mode": [run.get("mode", "")],
        "spd": [spd or 0],
        "gradient_length_min": [gradient_length_min or 0],
        "amount_ng": [amount_ng],
        "n_precursors": [run.get("n_precursors") or 0],
        "n_peptides": [run.get("n_peptides") or 0],
        "n_proteins": [run.get("n_proteins") or 0],
        "n_psms": [run.get("n_psms") or 0],
        "median_cv_precursor": [run.get("median_cv_precursor") or 0.0],
        "median_fragments_per_precursor": [run.get("median_fragments_per_precursor") or 0.0],
        "grs_score": [run.get("grs_score") or 0],
        "missed_cleavage_rate": [run.get("missed_cleavage_rate") or 0.0],
        "community_score": [0.0],  # computed by nightly consolidation
        "cohort_id": [cohort_id],
        "is_flagged": [len(validation.flags) > 0],
    }

    table = pa.table(row, schema=SUBMISSION_SCHEMA)

    # Upload to HF Dataset
    buf = io.BytesIO()
    pq.write_table(table, buf)
    buf.seek(0)

    try:
        from huggingface_hub import HfApi

        api = HfApi(token=hf_token)
        api.upload_file(
            path_or_fileobj=buf,
            path_in_repo=f"submissions/{submission_id}.parquet",
            repo_id=HF_DATASET_REPO,
            repo_type="dataset",
        )
    except Exception:
        logger.exception("Failed to upload submission to HF Dataset")
        raise

    # Mark as submitted in local DB
    run_id = run.get("id", "")
    if run_id:
        try:
            mark_submitted(run_id, submission_id)
        except Exception:
            logger.exception("Failed to mark run as submitted locally")

    logger.info(
        "Submitted to community benchmark: %s (cohort: %s)", submission_id[:8], cohort_id
    )

    return {
        "submission_id": submission_id,
        "cohort_id": cohort_id,
        "is_flagged": len(validation.flags) > 0,
        "flags": validation.flags,
        "status": "submitted",
    }


def _instrument_family(model: str) -> str:
    """Map instrument model name to family for cohort bucketing."""
    model_lower = model.lower()
    if "timstof" in model_lower or "tims tof" in model_lower:
        return "timsTOF"
    if "astral" in model_lower:
        return "Astral"
    if "exploris" in model_lower:
        return "Exploris"
    if "orbitrap" in model_lower:
        return "Orbitrap"
    return model
