"""QC threshold evaluation — pass/warn/fail gating with plain-English diagnosis."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from stan.config import load_thresholds

logger = logging.getLogger(__name__)


class GateResult(Enum):
    """QC gate result."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class GateDecision:
    """Result of evaluating QC thresholds against computed metrics."""

    result: GateResult
    failed_gates: list[str] = field(default_factory=list)
    warned_gates: list[str] = field(default_factory=list)
    diagnosis: str = ""
    metrics: dict = field(default_factory=dict)


# Templated plain-English failure diagnosis patterns
DIAGNOSIS_TEMPLATES: dict[tuple[str, ...], str] = {
    ("n_precursors", "n_psms"): (
        "Low ID count with normal chromatography suggests a search/library issue. "
        "Check spectral library version, FASTA, or DIA window scheme."
    ),
    ("n_precursors", "grs_score"): (
        "Low IDs with poor GRS score — likely LC or source problem. "
        "Check column condition, trap column, spray stability."
    ),
    ("n_psms", "grs_score"): (
        "Low PSMs with poor GRS score — likely LC or source problem. "
        "Check column condition, trap column, spray stability."
    ),
    ("missed_cleavage_rate",): (
        "High missed cleavages suggest incomplete digestion. "
        "Check trypsin activity, digestion time/temperature, or protein denaturation."
    ),
    ("pct_charge_1",): (
        "Elevated singly-charged precursors — possible source contamination, "
        "buffer impurity, or electrospray instability."
    ),
    ("median_cv_precursor",): (
        "High CV with normal ID count — LC reproducibility issue. "
        "Check injection volume consistency, sample carryover, or column equilibration."
    ),
    ("pct_delta_mass_lt5ppm",): (
        "Poor mass accuracy — instrument may need recalibration. "
        "Run a calibration file before next sample injection."
    ),
    ("grs_score",): (
        "Low GRS score indicates chromatographic instability. "
        "Check column, LC plumbing, gradient program, and mobile phase preparation."
    ),
    ("irt_max_deviation",): (
        "Large iRT deviation indicates retention time drift. "
        "Check column condition and equilibration."
    ),
}


def evaluate_gates(
    metrics: dict,
    instrument_model: str,
    acquisition_mode: str,
) -> GateDecision:
    """Evaluate QC metrics against per-instrument thresholds.

    Args:
        metrics: Dict of computed QC metric values.
        instrument_model: Model name for threshold lookup (e.g. "timsTOF Ultra").
        acquisition_mode: "dia" or "dda".

    Returns:
        GateDecision with pass/warn/fail result and diagnosis.
    """
    thresholds = load_thresholds()

    # Get model-specific thresholds, merged with defaults
    default_t = thresholds.get("default", {}).get(acquisition_mode, {})
    model_t = thresholds.get(instrument_model, {}).get(acquisition_mode, {})
    merged = {**default_t, **model_t}

    if not merged:
        logger.warning(
            "No thresholds found for model=%s, mode=%s", instrument_model, acquisition_mode
        )
        return GateDecision(result=GateResult.PASS, metrics=metrics)

    failed: list[str] = []
    warned: list[str] = []

    for threshold_key, threshold_val in merged.items():
        metric_name, direction = _parse_threshold_key(threshold_key)
        metric_val = metrics.get(metric_name)

        if metric_val is None:
            continue

        if direction == "min" and metric_val < threshold_val:
            # Hard fail: below minimum
            failed.append(metric_name)
        elif direction == "max" and metric_val > threshold_val:
            # Hard fail: above maximum
            failed.append(metric_name)
        elif direction == "min" and metric_val < threshold_val * 1.1:
            # Soft warn: within 10% of threshold
            warned.append(metric_name)
        elif direction == "max" and metric_val > threshold_val * 0.9:
            # Soft warn: within 10% of threshold
            warned.append(metric_name)

    if failed:
        result = GateResult.FAIL
    elif warned:
        result = GateResult.WARN
    else:
        result = GateResult.PASS

    diagnosis = _generate_diagnosis(failed, warned)

    return GateDecision(
        result=result,
        failed_gates=failed,
        warned_gates=warned,
        diagnosis=diagnosis,
        metrics=metrics,
    )


def _parse_threshold_key(key: str) -> tuple[str, str]:
    """Parse a threshold key like 'n_precursors_min' into (metric_name, direction).

    Returns:
        (metric_name, "min" | "max")
    """
    if key.endswith("_min"):
        return key[:-4], "min"
    if key.endswith("_max"):
        return key[:-4], "max"
    # Default: assume it's a minimum threshold
    return key, "min"


def _generate_diagnosis(failed: list[str], warned: list[str]) -> str:
    """Generate a plain-English diagnosis from failure patterns."""
    if not failed and not warned:
        return "All QC metrics within expected range."

    all_issues = failed + warned

    # Try to match known failure patterns (longest match first)
    for pattern, template in sorted(
        DIAGNOSIS_TEMPLATES.items(), key=lambda x: -len(x[0])
    ):
        if all(m in all_issues for m in pattern):
            return template

    # Fallback: list the failed/warned metrics
    parts: list[str] = []
    if failed:
        parts.append(f"Failed: {', '.join(failed)}.")
    if warned:
        parts.append(f"Warning: {', '.join(warned)}.")
    return " ".join(parts)
