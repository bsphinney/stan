"""Fetch community baseline data from the STAN HF Space relay.

Instead of building a baseline locally from your own QC history, you can
pull the community reference ranges directly. This gives new users an
immediate comparison point without needing months of historical data.

Usage:
    from stan.community.fetch_baseline import fetch_community_baseline

    baseline = fetch_community_baseline(
        instrument_family="Astral",
        spd=60,
        amount_ng=50,
    )
    # Returns {"n_precursors_q25": 18000, "n_precursors_q50": 22000, ...}
"""

from __future__ import annotations

import json
import logging
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

RELAY_URL = "https://brettsp-stan.hf.space"


def fetch_community_baseline(
    instrument_family: str | None = None,
    spd: int | None = None,
    amount_ng: float | None = None,
    cohort_id: str | None = None,
) -> dict:
    """Fetch community reference ranges for a specific cohort.

    If cohort_id is provided, returns stats for that exact cohort.
    Otherwise builds the cohort_id from instrument_family + spd + amount_ng.

    Args:
        instrument_family: e.g. "Astral", "timsTOF", "Exploris".
        spd: Samples per day (60, 30, 100, etc.).
        amount_ng: HeLa injection amount.
        cohort_id: Full cohort ID (overrides the above).

    Returns:
        Dict with cohort statistics (median, IQR) for key metrics.
        Empty dict if no matching cohort found.
    """
    try:
        with urllib.request.urlopen(f"{RELAY_URL}/api/leaderboard", timeout=30) as r:
            data = json.loads(r.read())
    except Exception as e:
        logger.error("Failed to fetch community data: %s", e)
        return {}

    submissions = data.get("submissions", [])
    if not submissions:
        return {}

    # Filter to matching cohort
    if cohort_id:
        matching = [s for s in submissions if s.get("cohort_id") == cohort_id]
    else:
        matching = submissions
        if instrument_family:
            matching = [s for s in matching if s.get("instrument_family") == instrument_family]
        if spd is not None:
            matching = [s for s in matching if _spd_matches(s.get("spd", 0), spd)]
        if amount_ng is not None:
            matching = [s for s in matching if _amount_matches(s.get("amount_ng", 0), amount_ng)]

    if not matching:
        return {"matching_submissions": 0}

    return _compute_stats(matching)


def _spd_matches(submission_spd: int, target_spd: int) -> bool:
    """Loose SPD match — within 20% of target."""
    if submission_spd == 0 or target_spd == 0:
        return False
    ratio = submission_spd / target_spd
    return 0.8 <= ratio <= 1.25


def _amount_matches(submission_amt: float, target_amt: float) -> bool:
    """Loose amount match — within 2x."""
    if submission_amt == 0 or target_amt == 0:
        return False
    ratio = submission_amt / target_amt
    return 0.5 <= ratio <= 2.0


def _compute_stats(submissions: list[dict]) -> dict:
    """Compute median + IQR for key metrics."""
    def pct(arr: list[float], p: float) -> float:
        if not arr:
            return 0.0
        s = sorted(arr)
        idx = int(len(s) * p / 100)
        return s[min(idx, len(s) - 1)]

    metrics = ["n_precursors", "n_peptides", "n_proteins", "n_psms", "ips_score",
               "median_fragments_per_precursor", "median_points_across_peak"]

    stats: dict = {"n_submissions": len(submissions)}
    for m in metrics:
        vals = [s.get(m, 0) for s in submissions if s.get(m, 0) > 0]
        if vals:
            stats[f"{m}_q25"] = pct(vals, 25)
            stats[f"{m}_median"] = pct(vals, 50)
            stats[f"{m}_q75"] = pct(vals, 75)

    # Instrument breakdown
    instruments = {}
    for s in submissions:
        model = s.get("instrument_model", "unknown")
        instruments[model] = instruments.get(model, 0) + 1
    stats["instrument_breakdown"] = instruments

    return stats


def cache_baseline_locally(cache_dir: Path | None = None) -> Path:
    """Download the full community leaderboard and cache it locally.

    Useful for running offline or avoiding repeated API calls.

    Returns:
        Path to the cached JSON file.
    """
    from stan.config import get_user_config_dir

    if cache_dir is None:
        cache_dir = get_user_config_dir() / "community_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    cache_file = cache_dir / "community_baseline.json"

    try:
        with urllib.request.urlopen(f"{RELAY_URL}/api/leaderboard", timeout=60) as r:
            data = json.loads(r.read())
        cache_file.write_text(json.dumps(data, default=str))
        logger.info("Cached %d community submissions to %s",
                    len(data.get("submissions", [])), cache_file)
    except Exception as e:
        logger.error("Failed to cache community baseline: %s", e)
        raise

    return cache_file
