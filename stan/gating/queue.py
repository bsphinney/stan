"""Queue gating — write HOLD flag to pause instrument sample queue on QC failure."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from stan.gating.evaluator import GateDecision, GateResult

logger = logging.getLogger(__name__)


def write_hold_flag(output_dir: Path, decision: GateDecision, run_name: str) -> Path | None:
    """Write a HOLD flag file if the QC decision is FAIL.

    The HOLD file is placed in the instrument output directory where autosampler
    queue software (Xcalibur, timsControl) can detect it.

    Args:
        output_dir: Directory to write the HOLD flag to.
        decision: GateDecision from evaluator.
        run_name: Name of the run that failed.

    Returns:
        Path to the HOLD file if written, None otherwise.
    """
    if decision.result != GateResult.FAIL:
        return None

    flag_path = output_dir / f"HOLD_{run_name}.txt"

    content = (
        f"STAN QC HOLD\n"
        f"Run: {run_name}\n"
        f"Result: {decision.result.value}\n"
        f"Failed gates: {', '.join(decision.failed_gates)}\n"
        f"Diagnosis: {decision.diagnosis}\n"
        f"Time: {datetime.now(timezone.utc).isoformat()}\n"
    )

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        flag_path.write_text(content)
        logger.warning("HOLD flag written: %s", flag_path)
    except OSError:
        logger.exception("Failed to write HOLD flag: %s", flag_path)
        return None

    return flag_path


def clear_hold_flag(output_dir: Path, run_name: str) -> bool:
    """Remove a HOLD flag file (e.g., after manual override).

    Args:
        output_dir: Directory containing the HOLD flag.
        run_name: Name of the run.

    Returns:
        True if the flag was removed, False if it didn't exist.
    """
    flag_path = output_dir / f"HOLD_{run_name}.txt"

    if flag_path.exists():
        flag_path.unlink()
        logger.info("HOLD flag cleared: %s", flag_path)
        return True

    return False
