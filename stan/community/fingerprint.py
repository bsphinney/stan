"""Track C — Dual Mode Instrument Health Fingerprint.

When a lab submits both Track A (DDA) and Track B (DIA) from the same instrument
within 24h, STAN links them via session_id and computes a 6-axis radar fingerprint.

Axes (each 0–100 within cohort):
  1. Mass accuracy → pct_delta_mass_lt5ppm (DDA)
  2. Duty cycle → ms2_scan_rate percentile (DDA)
  3. Spectral quality → median_hyperscore percentile (DDA)
  4. Precursor depth → n_precursors percentile (DIA)
  5. Quantitative CV → inverted median_cv_precursor percentile (DIA)
  6. Fragment sensitivity → median_fragments_per_precursor × pct_fragments_quantified (DIA)

A perfectly healthy instrument shows a regular hexagon.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from stan.db import get_runs
from stan.metrics.scoring import _percentile_rank

logger = logging.getLogger(__name__)

FINGERPRINT_AXES = [
    "mass_accuracy",
    "duty_cycle",
    "spectral_quality",
    "precursor_depth",
    "quantitative_cv",
    "fragment_sensitivity",
]

# Failure pattern templates from spec §12
FINGERPRINT_PATTERNS: list[tuple[dict[str, tuple[float, float]], str]] = [
    # pattern: axis_name → (min, max) range that triggers the diagnosis
    (
        {ax: (0, 30) for ax in FINGERPRINT_AXES},
        "Source fouling or spray instability — all axes compressed.",
    ),
    (
        {"fragment_sensitivity": (0, 30), "quantitative_cv": (0, 30)},
        "Column aging or degradation — fragment sensitivity + CV drop only.",
    ),
    (
        {"mass_accuracy": (0, 20)},
        "Calibration drift — recalibrate.",
    ),
    (
        {"duty_cycle": (0, 30), "spectral_quality": (60, 100)},
        "AGC/fill time misconfigured — duty cycle low, spectral quality high.",
    ),
    (
        {"precursor_depth": (0, 30), "fragment_sensitivity": (50, 100)},
        "Search/library issue, not hardware — precursor depth low, fragment sensitivity normal.",
    ),
    (
        {"quantitative_cv": (0, 30)},
        "LC injection volume or carryover — CV high, IDs normal.",
    ),
]


@dataclass
class Fingerprint:
    """6-axis instrument health fingerprint."""

    mass_accuracy: float = 0.0
    duty_cycle: float = 0.0
    spectral_quality: float = 0.0
    precursor_depth: float = 0.0
    quantitative_cv: float = 0.0
    fragment_sensitivity: float = 0.0
    peptide_recovery_ratio: float = 0.0
    diagnosis: str = ""
    session_id: str = ""
    dda_run_id: str = ""
    dia_run_id: str = ""

    def to_dict(self) -> dict:
        return {
            "axes": {
                "mass_accuracy": self.mass_accuracy,
                "duty_cycle": self.duty_cycle,
                "spectral_quality": self.spectral_quality,
                "precursor_depth": self.precursor_depth,
                "quantitative_cv": self.quantitative_cv,
                "fragment_sensitivity": self.fragment_sensitivity,
            },
            "peptide_recovery_ratio": self.peptide_recovery_ratio,
            "diagnosis": self.diagnosis,
            "session_id": self.session_id,
            "dda_run_id": self.dda_run_id,
            "dia_run_id": self.dia_run_id,
        }


def compute_fingerprint(
    dda_run: dict,
    dia_run: dict,
    cohort_percentiles: dict,
) -> Fingerprint:
    """Compute a 6-axis fingerprint from paired DDA + DIA runs.

    Args:
        dda_run: DDA run dict from the database.
        dia_run: DIA run dict from the database.
        cohort_percentiles: Cohort percentile arrays from consolidation.

    Returns:
        Fingerprint with all axes and diagnosis.
    """
    pr = _percentile_rank

    # DDA axes
    mass_accuracy = pr(
        dda_run.get("pct_delta_mass_lt5ppm", 0),
        cohort_percentiles.get("pct_delta_mass_lt5ppm", []),
    )
    duty_cycle = pr(
        dda_run.get("ms2_scan_rate", 0),
        cohort_percentiles.get("ms2_scan_rate", []),
    )
    spectral_quality = pr(
        dda_run.get("median_hyperscore", 0),
        cohort_percentiles.get("median_hyperscore", []),
    )

    # DIA axes
    precursor_depth = pr(
        dia_run.get("n_precursors", 0),
        cohort_percentiles.get("n_precursors", []),
    )
    quantitative_cv = 100 - pr(
        dia_run.get("median_cv_precursor", 0),
        cohort_percentiles.get("median_cv_precursor", []),
    )

    frag_per_prec = dia_run.get("median_fragments_per_precursor", 0) or 0
    frag_quant = dia_run.get("pct_fragments_quantified", 0) or 0
    fragment_sensitivity_raw = frag_per_prec * frag_quant
    fragment_sensitivity = pr(
        fragment_sensitivity_raw,
        cohort_percentiles.get("fragment_sensitivity", []),
    )

    # Peptide recovery ratio
    n_pep_dia = dia_run.get("n_peptides", 0) or 0
    n_pep_dda = dda_run.get("n_peptides_dda", 0) or 0
    recovery_ratio = n_pep_dia / n_pep_dda if n_pep_dda > 0 else 0.0

    fp = Fingerprint(
        mass_accuracy=mass_accuracy,
        duty_cycle=duty_cycle,
        spectral_quality=spectral_quality,
        precursor_depth=precursor_depth,
        quantitative_cv=quantitative_cv,
        fragment_sensitivity=fragment_sensitivity,
        peptide_recovery_ratio=round(recovery_ratio, 3),
        dda_run_id=dda_run.get("id", ""),
        dia_run_id=dia_run.get("id", ""),
    )

    fp.diagnosis = _diagnose_fingerprint(fp)

    return fp


def find_paired_runs(instrument: str, window_hours: int = 24) -> list[tuple[dict, dict]]:
    """Find DDA+DIA run pairs from the same instrument within a time window.

    Returns:
        List of (dda_run, dia_run) tuples.
    """
    runs = get_runs(instrument=instrument, limit=200)

    dda_runs = [r for r in runs if r.get("mode") == "DDA"]
    dia_runs = [r for r in runs if r.get("mode") == "DIA"]

    pairs: list[tuple[dict, dict]] = []
    used_dia: set[str] = set()

    for dda in dda_runs:
        dda_time = datetime.fromisoformat(dda["run_date"])
        for dia in dia_runs:
            if dia["id"] in used_dia:
                continue
            dia_time = datetime.fromisoformat(dia["run_date"])
            if abs((dda_time - dia_time).total_seconds()) <= window_hours * 3600:
                pairs.append((dda, dia))
                used_dia.add(dia["id"])
                break

    return pairs


def _diagnose_fingerprint(fp: Fingerprint) -> str:
    """Match fingerprint axes against known failure patterns."""
    axes = {
        "mass_accuracy": fp.mass_accuracy,
        "duty_cycle": fp.duty_cycle,
        "spectral_quality": fp.spectral_quality,
        "precursor_depth": fp.precursor_depth,
        "quantitative_cv": fp.quantitative_cv,
        "fragment_sensitivity": fp.fragment_sensitivity,
    }

    for pattern, diagnosis in FINGERPRINT_PATTERNS:
        if _matches_pattern(axes, pattern):
            # Check recovery ratio for additional context
            extra = ""
            if fp.peptide_recovery_ratio > 0 and fp.peptide_recovery_ratio < 0.75:
                extra = " DIA method may also need optimization (recovery ratio < 0.75)."
            return diagnosis + extra

    if fp.peptide_recovery_ratio > 0 and fp.peptide_recovery_ratio < 0.75:
        return "DIA method optimization needed — peptide recovery ratio below 0.75."

    return "Instrument health looks balanced."


def _matches_pattern(axes: dict[str, float], pattern: dict[str, tuple[float, float]]) -> bool:
    """Check if axis values fall within the pattern's ranges."""
    for axis_name, (lo, hi) in pattern.items():
        value = axes.get(axis_name, 50.0)
        if not (lo <= value <= hi):
            return False
    return True
