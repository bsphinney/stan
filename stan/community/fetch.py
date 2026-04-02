"""Fetch community benchmark data from HF Dataset."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

HF_DATASET_REPO = "bsphinney/stan-community-benchmark"


def fetch_cohort_percentiles() -> dict:
    """Fetch the latest cohort percentiles from the HF Dataset.

    Returns:
        Dict with cohort statistics and percentile arrays.
    """
    try:
        from huggingface_hub import hf_hub_download

        local_path = hf_hub_download(
            repo_id=HF_DATASET_REPO,
            filename="cohort_stats/cohort_percentiles_latest.json",
            repo_type="dataset",
        )

        with open(local_path) as f:
            data = json.load(f)

        return {"cohorts": data, "error": None}

    except Exception:
        logger.exception("Failed to fetch cohort percentiles from HF Dataset")
        return {"cohorts": {}, "error": "Failed to fetch community data"}


def fetch_benchmark_latest() -> Path | None:
    """Download the latest consolidated benchmark parquet.

    Returns:
        Local path to the downloaded parquet, or None on failure.
    """
    try:
        from huggingface_hub import hf_hub_download

        local_path = hf_hub_download(
            repo_id=HF_DATASET_REPO,
            filename="benchmark_latest.parquet",
            repo_type="dataset",
        )
        return Path(local_path)

    except Exception:
        logger.exception("Failed to fetch benchmark_latest.parquet from HF Dataset")
        return None
