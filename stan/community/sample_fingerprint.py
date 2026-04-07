"""Pierce HeLa digest fingerprinting — verify sample identity.

Detects whether a QC run used the standard Pierce HeLa Protein Digest
(Thermo cat# 88328/88329) or a different sample. Built from the
intersection of precursors detected in >90% of runs across 673 QC
runs on 4 instrument platforms (312 Lumos + 214 timsTOF HT + 57
Exploris 480 + 90 Lumos). April 2026 reference.

Three fingerprint tiers:
  Universal (4,638 precursors) — works on any instrument
  Orbitrap  (~10,000 precursors) — finer check for Thermo instruments
  timsTOF  (~16,000 precursors) — finer check for Bruker instruments

Usage:
    from stan.community.sample_fingerprint import score_sample_match
    score = score_sample_match(report_path, instrument_family="Lumos")
    # Returns 0-100: 100 = perfect Pierce HeLa match

The fingerprint data lives in the HF Dataset at
fingerprints/pierce_hela_universal.json and is downloaded once on
first use.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)

# Embedded universal fingerprint — the 4,638 precursors detected in >90%
# of Pierce HeLa QC runs across ALL instruments. This is the instrument-
# agnostic "is it Pierce HeLa?" check. Too large to inline here (~200KB),
# so it's loaded from a JSON file shipped with the package or downloaded
# from the HF Dataset on first use.
_FINGERPRINT_CACHE: dict[str, list[str]] = {}


def _get_fingerprint_path() -> Path:
    """Path to the cached fingerprint JSON."""
    from stan.config import get_user_config_dir
    return get_user_config_dir() / "fingerprints" / "pierce_hela.json"


def _download_fingerprint() -> dict:
    """Download the fingerprint from the HF Dataset."""
    try:
        from huggingface_hub import hf_hub_download
        p = hf_hub_download(
            "brettsp/stan-benchmark",
            "fingerprints/pierce_hela.json",
            repo_type="dataset",
        )
        return json.loads(Path(p).read_text())
    except Exception:
        logger.warning("Could not download fingerprint from HF Dataset")
        return {}


def load_fingerprint(tier: str = "universal") -> list[str]:
    """Load the Pierce HeLa fingerprint for a given tier.

    Args:
        tier: 'universal', 'Lumos', 'Exploris_480', 'timsTOF_HT', or 'Orbitrap'
              (Orbitrap = merged Lumos + Exploris fingerprint)

    Returns:
        List of Precursor.Id strings that define the fingerprint.
    """
    if tier in _FINGERPRINT_CACHE:
        return _FINGERPRINT_CACHE[tier]

    fp_path = _get_fingerprint_path()
    if not fp_path.exists():
        # Download from HF
        data = _download_fingerprint()
        if data:
            fp_path.parent.mkdir(parents=True, exist_ok=True)
            fp_path.write_text(json.dumps(data, indent=2))
    else:
        data = json.loads(fp_path.read_text())

    if not data:
        return []

    # Map tier names to fingerprint keys
    if tier == "universal":
        result = data.get("universal", [])
    elif tier == "Orbitrap":
        # Merge Lumos + Exploris for a broader Orbitrap fingerprint
        lumos = set(data.get("fingerprints", {}).get("Lumos_DIA", []))
        expl = set(data.get("fingerprints", {}).get("Exploris_480_DIA", []))
        result = sorted(lumos & expl)  # intersection = reliable on both
    elif tier in data.get("fingerprints", {}):
        result = data["fingerprints"][tier]
    else:
        # Try partial match
        for key in data.get("fingerprints", {}):
            if tier.lower() in key.lower():
                result = data["fingerprints"][key]
                break
        else:
            result = data.get("universal", [])

    _FINGERPRINT_CACHE[tier] = result
    return result


def score_sample_match(
    report_path: Path,
    instrument_family: str | None = None,
    q_cutoff: float = 0.01,
) -> dict:
    """Score how well a DIA-NN report matches the Pierce HeLa fingerprint.

    Args:
        report_path: Path to DIA-NN report.parquet.
        instrument_family: 'Lumos', 'Exploris 480', 'timsTOF HT', 'Astral',
            etc. Used to select the best fingerprint tier. If None, uses
            the universal fingerprint.
        q_cutoff: FDR threshold for filtering precursors.

    Returns:
        {
            score: int 0-100 (100 = perfect match),
            fingerprint_size: int (how many sentinel precursors in the fingerprint),
            detected: int (how many fingerprint precursors found in this run),
            missing: int (fingerprint precursors NOT found),
            pct_detected: float (detected / fingerprint_size * 100),
            tier: str (which fingerprint tier was used),
            verdict: str ('pierce_hela' | 'likely_pierce' | 'uncertain' | 'different_sample'),
        }
    """
    # Pick the right fingerprint tier
    if instrument_family:
        fam = instrument_family.lower()
        if "timstof" in fam:
            tier = "timsTOF_HT"
        elif "lumos" in fam:
            tier = "Lumos_DIA"
        elif "exploris" in fam:
            tier = "Exploris_480_DIA"
        elif "astral" in fam:
            tier = "Orbitrap"  # Astral is Orbitrap family
        else:
            tier = "universal"
    else:
        tier = "universal"

    fingerprint = load_fingerprint(tier)
    if not fingerprint:
        # Fallback to universal
        fingerprint = load_fingerprint("universal")
        tier = "universal"

    if not fingerprint:
        return {
            "score": -1,
            "verdict": "no_fingerprint",
            "message": "Fingerprint data not available. Run stan init to download.",
        }

    fp_set = set(fingerprint)

    # Read the report and get detected precursors at 1% FDR
    try:
        df = pl.read_parquet(report_path, columns=["Precursor.Id", "Q.Value"])
        detected_all = set(
            df.filter(pl.col("Q.Value") < q_cutoff)["Precursor.Id"]
            .unique()
            .to_list()
        )
    except Exception as e:
        logger.error("Failed to read %s: %s", report_path, e)
        return {"score": -1, "verdict": "read_error", "message": str(e)}

    # Score = fraction of fingerprint precursors detected in this run
    detected = fp_set & detected_all
    missing = fp_set - detected_all
    pct = 100 * len(detected) / len(fp_set) if fp_set else 0

    # Map percentage to a 0-100 score with thresholds
    if pct >= 85:
        score = int(pct)
        verdict = "pierce_hela"
    elif pct >= 70:
        score = int(pct)
        verdict = "likely_pierce"
    elif pct >= 50:
        score = int(pct)
        verdict = "uncertain"
    else:
        score = int(pct)
        verdict = "different_sample"

    return {
        "score": score,
        "fingerprint_size": len(fp_set),
        "detected": len(detected),
        "missing": len(missing),
        "pct_detected": round(pct, 1),
        "tier": tier,
        "verdict": verdict,
    }
