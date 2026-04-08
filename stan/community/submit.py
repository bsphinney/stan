"""Submit QC metrics to the community HeLa benchmark via the STAN relay.

Submissions are posted to the HF Space relay which handles HF Dataset
uploads server-side. No HF token is required on the client. Raw files
are NEVER uploaded — metrics only.
"""

from __future__ import annotations

import io
import json
import logging
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone

import pyarrow as pa
import pyarrow.parquet as pq

from stan import __version__
from stan.community.fingerprint_dedup import compute_submission_fingerprint
from stan.community.validate import validate_submission
from stan.config import load_community
from stan.db import mark_submitted
from stan.metrics.scoring import compute_cohort_id
from stan.search.community_params import check_diann_version_compatible

logger = logging.getLogger(__name__)

HF_DATASET_REPO = "brettsp/stan-benchmark"
RELAY_URL = "https://brettsp-stan.hf.space"

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
    pa.field("ips_score", pa.int32()),
    pa.field("missed_cleavage_rate", pa.float32()),
    pa.field("community_score", pa.float32()),
    pa.field("cohort_id", pa.string()),
    pa.field("is_flagged", pa.bool_()),
    pa.field("fingerprint", pa.string()),  # for dedup
    pa.field("diann_version", pa.string()),  # pinned for reproducibility
])


def submit_to_benchmark(
    run: dict,
    spd: int | None = None,
    gradient_length_min: int | None = None,
    amount_ng: float = 50.0,
    hela_source: str = "Pierce HeLa Protein Digest Standard",
    asset_hashes: dict[str, str] | None = None,
    diann_version: str | None = None,
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
    """
    try:
        community_config = load_community()
    except Exception:
        community_config = {}

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

    # Version check — community benchmark requires the pinned DIA-NN version
    # because different versions produce non-comparable results
    if diann_version is None:
        # Try to detect from run metadata or system
        from stan.search.version_detect import detect_diann_version
        diann_version = run.get("diann_version") or detect_diann_version() or "unknown"

    is_compat, msg = check_diann_version_compatible(diann_version)
    if not is_compat:
        raise ValueError(f"Submission rejected: {msg}")

    # Build submission
    submission_id = str(uuid.uuid4())
    instrument = run.get("instrument", "")
    instrument_family = _instrument_family(instrument)

    column_model = run.get("column_model", "")
    cohort_id = compute_cohort_id(
        instrument_family, amount_ng, spd=spd, gradient_min=gradient_length_min,
        column_model=column_model,
    )

    # Compute fingerprint for dedup — same (lab, instrument, run_name, amount, spd)
    # will always produce the same fingerprint so resubmissions are detectable
    fingerprint = compute_submission_fingerprint(
        display_name=display_name,
        instrument_model=instrument,
        run_name=run.get("run_name", ""),
        amount_ng=amount_ng,
        spd=spd,
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
        "ips_score": [run.get("ips_score") or 0],
        "missed_cleavage_rate": [run.get("missed_cleavage_rate") or 0.0],
        "community_score": [0.0],  # computed by nightly consolidation
        "cohort_id": [cohort_id],
        "is_flagged": [len(validation.flags) > 0],
        "fingerprint": [fingerprint],
        "diann_version": [diann_version],
    }

    table = pa.table(row, schema=SUBMISSION_SCHEMA)

    # Submit via relay (no token needed — relay handles HF auth server-side)
    # Convert row to JSON-serializable format
    submit_payload = {k: v[0] for k, v in row.items()}
    # Convert non-serializable types
    submit_payload["submitted_at"] = submit_payload["submitted_at"].isoformat()

    try:
        data = json.dumps(submit_payload).encode("utf-8")
        req = urllib.request.Request(
            f"{RELAY_URL}/api/submit",
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": f"STAN/{__version__}",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            if result.get("status") != "ok":
                raise RuntimeError(f"Relay rejected submission: {result.get('error', 'unknown')}")
    except urllib.error.URLError as e:
        logger.error("Failed to submit to relay: %s", e)
        raise RuntimeError(f"Could not reach community relay: {e}") from e

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
