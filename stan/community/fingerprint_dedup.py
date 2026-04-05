"""Submission fingerprinting for duplicate detection.

Prevents the same QC run from being submitted twice to the community
benchmark — e.g. when a lab re-runs its baseline builder, or when two
users at the same institution submit overlapping data.

The fingerprint is a stable hash of identifying fields that don't change
between submissions of the same run:
    display_name + instrument_model + run_name + amount_ng + spd

Timestamps and metric values are intentionally excluded because they
vary between analyses of the same raw file.
"""

from __future__ import annotations

import hashlib
import logging

logger = logging.getLogger(__name__)


def compute_submission_fingerprint(
    display_name: str,
    instrument_model: str,
    run_name: str,
    amount_ng: float,
    spd: int | None = None,
) -> str:
    """Compute a stable fingerprint for a submission.

    Identical inputs produce identical fingerprints, so resubmissions
    of the same run are detectable without comparing every field.

    Args:
        display_name: Lab display name.
        instrument_model: Instrument model string.
        run_name: Raw file/directory name.
        amount_ng: Injection amount in nanograms.
        spd: Samples per day (if known).

    Returns:
        16-character hex fingerprint.
    """
    # Normalize inputs to prevent trivial differences creating new fingerprints
    parts = [
        display_name.strip().lower(),
        instrument_model.strip(),
        run_name.strip().split("/")[-1].split("\\")[-1],  # basename only
        f"{amount_ng:.1f}",
        str(spd or 0),
    ]
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]
