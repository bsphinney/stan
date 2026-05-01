"""Community benchmark submission validation — hard gates, soft flags, and library verification.

Submissions are validated in two ways:
1. Metric gates: reject submissions with implausible metric values
2. Library verification: reject submissions that didn't use the correct
   frozen community speclib/FASTA. This is critical — the whole benchmark
   depends on everyone searching the same library.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from stan.search.community_params import SEARCH_PARAMS_VERSION

logger = logging.getLogger(__name__)

# Expected MD5 hashes of frozen community search assets.
# These are checked client-side before submission and server-side during
# nightly consolidation. If a submission's hashes don't match, it's rejected.
EXPECTED_ASSET_HASHES: dict[str, str] = {
    # FASTA (shared) — UniProt human + universal contaminants (21,044 entries)
    "human_hela_202604.fasta": "8de1d9bd0a052b175f88f66f82500d92",
    # timsTOF empirical library — built from UCD longitudinal HeLa QC (6 files, 54K precursors)
    "hela_timstof_202604.parquet": "ad72bfb2730644c69147ba8f34bfe982",
    # Orbitrap/Astral empirical library — built from PXD054015 (8 files, 170K precursors)
    "hela_orbitrap_202604.parquet": "ac84e40f5b2f23e1286f28a7baeccec2",
}

# Hard gates: submissions below these are rejected outright.
# These catch clearly failed runs that should NOT contribute to community
# reference ranges. A run with <5000 precursors on HeLa is broken, not
# just underperforming.
HARD_GATES: dict[str, float] = {
    # DIA — raised to 5000 to exclude failed runs from baseline
    "n_precursors_min": 5000,
    "n_peptides_min": 3000,
    "n_proteins_min": 1500,
    "pct_charge_1_max": 0.50,
    "missed_cleavage_rate_max": 0.60,
    # DDA
    "n_psms_min": 5000,
    "n_peptides_dda_min": 3000,
    "ms2_scan_rate_min": 5.0,
}

# CV (median_cv_precursor) was retired as a community-benchmark metric
# in v0.2.265. It required replicate injections that cluster
# re-searches don't have, and IPS already captures the cohort-
# calibrated depth signal CV was approximating. The DB column is
# kept for backward compatibility with historical local stan.dbs;
# new community submissions no longer ship it.
OPTIONAL_GATES: dict[str, float] = {}

# Soft flags: unusual but not rejected — flagged for review
SOFT_FLAGS: dict[str, tuple[str, float]] = {
    # metric_name: (direction, threshold)
    "n_precursors_high": ("max", 50000),  # suspiciously high
    "n_psms_high": ("max", 200000),
    # DDA mass-accuracy hint. Was a hard gate at 0.50 in v0.2.282 and
    # earlier; demoted to a soft flag on 2026-04-30 because real-world
    # DDA on older Bruker tunes routinely lands at 0.30–0.45 with no
    # impact on downstream IDs. Flag for review, don't reject.
    "pct_delta_mass_lt5ppm_low": ("min", 0.50),
}


@dataclass
class ValidationResult:
    """Result of validating a community benchmark submission."""

    is_valid: bool = True
    rejected_gates: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def validate_submission(
    metrics: dict,
    mode: str,
    search_params_version: str | None = None,
    asset_hashes: dict[str, str] | None = None,
) -> ValidationResult:
    """Validate a submission against hard gates, soft flags, and library checks.

    Args:
        metrics: Dict of computed QC metrics.
        mode: "dia" or "dda".
        search_params_version: Version string of search params used (e.g. "v1.0.0").
        asset_hashes: Dict of filename → MD5 hash of community assets used.
            Must include FASTA hash and (for DIA) speclib hash.

    Returns:
        ValidationResult indicating whether submission passes.
    """
    result = ValidationResult()

    # ── Library / FASTA version check ─────────────────────────────────
    if search_params_version is not None and search_params_version != SEARCH_PARAMS_VERSION:
        result.rejected_gates.append(
            f"Search params version mismatch: got {search_params_version}, "
            f"expected {SEARCH_PARAMS_VERSION}. Re-run with the current STAN version."
        )
        result.is_valid = False

    if asset_hashes is not None:
        _validate_asset_hashes(asset_hashes, mode, result)

    # Apply optional gates first — populate flags only when value present
    for gate_key, threshold in OPTIONAL_GATES.items():
        metric_name, direction = _parse_gate_key(gate_key)
        value = metrics.get(metric_name)
        if value is None:
            continue
        if direction == "max" and value > threshold:
            result.flags.append(
                f"{metric_name}={value} above maximum {threshold}"
            )
        elif direction == "min" and value < threshold:
            result.flags.append(
                f"{metric_name}={value} below minimum {threshold}"
            )

    # Apply hard gates
    for gate_key, threshold in HARD_GATES.items():
        metric_name, direction = _parse_gate_key(gate_key)

        # Skip gates not relevant to this mode
        if mode == "dia" and metric_name in ("n_psms", "n_peptides_dda", "pct_delta_mass_lt5ppm", "ms2_scan_rate"):
            continue
        if mode == "dda" and metric_name in ("n_precursors", "n_peptides", "n_proteins", "median_cv_precursor", "pct_charge_1", "missed_cleavage_rate"):
            continue

        value = metrics.get(metric_name)
        if value is None:
            result.errors.append(f"Missing required metric: {metric_name}")
            result.is_valid = False
            continue

        if direction == "min" and value < threshold:
            result.rejected_gates.append(
                f"{metric_name}={value} below minimum {threshold}"
            )
            result.is_valid = False
        elif direction == "max" and value > threshold:
            result.rejected_gates.append(
                f"{metric_name}={value} above maximum {threshold}"
            )
            result.is_valid = False

    # Apply soft flags (don't reject, just flag)
    for flag_key, (direction, threshold) in SOFT_FLAGS.items():
        metric_name = flag_key.rsplit("_", 1)[0]  # strip _high/_low suffix
        value = metrics.get(metric_name)
        if value is None:
            continue

        if direction == "max" and value > threshold:
            result.flags.append(f"{metric_name}={value} unusually high (>{threshold})")
        elif direction == "min" and value < threshold:
            result.flags.append(f"{metric_name}={value} unusually low (<{threshold})")

    # Flag sample-type mismatches. Now that we support multi-standard
    # cohorts, the concern is not "non-HeLa in a HeLa benchmark" but
    # rather "filename says one standard, sample_type field says another".
    # For example, filename contains K562 but sample_type is "hela" →
    # the auto-detection may have failed or the user overrode it wrongly.
    run_name = metrics.get("run_name", "")
    sample_type = metrics.get("sample_type", "hela")
    _SAMPLE_TYPE_PATTERNS: list[tuple[str, str]] = [
        ("k562", "k562"),
        ("hek293", "hek293"),
        ("hek-293", "hek293"),
        ("hek_293", "hek293"),
        ("jurkat", "jurkat"),
        ("a549", "a549"),
        ("u2os", "u2os"),
        ("mcf7", "mcf7"),
        ("nih3t3", "nih3t3"),
        ("yeast", "yeast"),
        ("ecoli", "ecoli"),
        ("e.coli", "ecoli"),
        ("e_coli", "ecoli"),
    ]
    if run_name:
        import re as _re
        detected_type = "hela"
        for pattern, st in _SAMPLE_TYPE_PATTERNS:
            if _re.search(pattern, run_name, _re.IGNORECASE):
                detected_type = st
                break
        if detected_type != "hela" and sample_type == "hela":
            result.flags.append(
                f"run_name contains '{detected_type}' but sample_type is 'hela' — "
                f"possible mismatch. If this is a {detected_type} run, set sample_type "
                f"accordingly so it lands in the correct benchmark cohort."
            )
        elif detected_type == "hela" and sample_type not in ("hela", ""):
            result.flags.append(
                f"sample_type is '{sample_type}' but run_name doesn't contain a "
                f"matching cell-line keyword — verify the sample type is correct."
            )

    if result.rejected_gates:
        logger.warning("Submission rejected: %s", result.rejected_gates)
    if result.flags:
        logger.info("Submission flagged: %s", result.flags)

    return result


def _parse_gate_key(key: str) -> tuple[str, str]:
    """Parse 'n_precursors_min' into ('n_precursors', 'min')."""
    if key.endswith("_min"):
        return key[:-4], "min"
    if key.endswith("_max"):
        return key[:-4], "max"
    return key, "min"


def _validate_asset_hashes(
    asset_hashes: dict[str, str],
    mode: str,
    result: ValidationResult,
) -> None:
    """Validate that community search assets match expected hashes.

    This is the key anti-tampering check: if someone searches with a different
    FASTA or speclib, their precursor/PSM counts aren't comparable.
    """
    # Check FASTA hash (required for all submissions)
    fasta_key = "human_hela_202604.fasta"
    expected_fasta = EXPECTED_ASSET_HASHES.get(fasta_key, "")
    submitted_fasta = asset_hashes.get("fasta_md5", "")

    if expected_fasta and submitted_fasta and submitted_fasta != expected_fasta:
        result.rejected_gates.append(
            f"FASTA hash mismatch: submission used a different FASTA than the "
            f"community standard. Expected MD5 {expected_fasta[:12]}..., "
            f"got {submitted_fasta[:12]}... — results are not comparable."
        )
        result.is_valid = False

    # Check speclib hash (DIA only)
    if mode == "dia":
        speclib_md5 = asset_hashes.get("speclib_md5", "")
        # Check against both vendor-specific speclibs
        expected_speclibs = [
            EXPECTED_ASSET_HASHES.get("hela_timstof_202604.predicted.speclib", ""),
            EXPECTED_ASSET_HASHES.get("hela_orbitrap_202604.predicted.speclib", ""),
        ]
        valid_speclibs = [h for h in expected_speclibs if h]

        if speclib_md5 and valid_speclibs and speclib_md5 not in valid_speclibs:
            result.rejected_gates.append(
                f"Speclib hash mismatch: submission used a non-community spectral "
                f"library. Got MD5 {speclib_md5[:12]}... — this does not match any "
                f"of the frozen community HeLa speclibs. DIA benchmark requires the "
                f"exact community speclib for cross-lab comparability."
            )
            result.is_valid = False

        if not speclib_md5 and valid_speclibs:
            result.flags.append(
                "No speclib hash provided — cannot verify community library was used. "
                "Upgrade to latest STAN version for automatic hash verification."
            )


def compute_file_md5(filepath: str) -> str:
    """Compute MD5 hash of a file (for speclib/FASTA verification).

    Args:
        filepath: Path to the file on the local filesystem.

    Returns:
        Hex digest string.
    """
    import hashlib
    from pathlib import Path

    h = hashlib.md5()
    with open(Path(filepath), "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()
