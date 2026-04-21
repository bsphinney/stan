"""QC file detection from filenames.

Identifies HeLa QC standard runs mixed in with other sample files.
Used by both the watcher (real-time) and baseline (retroactive).

Default patterns match common QC naming conventions across labs:
  - HeLa / hela / HELA / HeL50 / HeLa50 / HeLa50ng  (full or partial)
  - HE50 / He50 / HE5  (the lab abbreviation — added 2026-04-21)
  - QC / qc / QCex  (qc anywhere in the name)
  - Std_He / STD_HE / std_hela / Std_HeLa  (std followed by he)

Users can override with a custom regex in instruments.yml:
    qc_pattern: "(?i)(he(la?|l?\\d)|qc|std.*he)"
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Default QC filename patterns (case-insensitive).
#
# `he(l[a5\d]|\d)` matches:
#   he + l + a    → "HeLa", "Hela", "HELA"
#   he + l + 5    → "HeL5", legacy variant
#   he + l + \d   → "HeL50", "HeL3hr", any digit after "HeL"
#   he + \d       → "HE50", "He5", "He125ng" — the lab abbreviation
#                   added 2026-04-21 after Brett's lab started using it.
#
# Crucially does NOT match plain "hel" with non-digit/a follow-up, so
# "helper", "spentHeLtip", "HelPER" stay non-QC. Likewise "head" /
# "heart" / "heat" stay non-QC because `he\d` requires a digit, not a
# letter, after the "he".
#
# `qc` matches any token like "QC", "qc", "QCex".
# `std[_\-\s]?he` matches "Std_He", "std-he", "STD HELA", etc.
DEFAULT_QC_PATTERN = r"(?i)(he(l[a5\d]|\d)|qc|std[_\-\s]?he)"


def compile_qc_pattern(pattern: str | None = None) -> re.Pattern:
    """Compile a QC filename regex pattern.

    Args:
        pattern: Custom regex string, or None for the default pattern.

    Returns:
        Compiled regex pattern.
    """
    pat = pattern or DEFAULT_QC_PATTERN
    try:
        return re.compile(pat)
    except re.error:
        logger.warning("Invalid qc_pattern '%s', falling back to default", pat)
        return re.compile(DEFAULT_QC_PATTERN)


def is_qc_file(path: Path, pattern: re.Pattern | None = None) -> bool:
    """Check if a raw file path looks like a QC/HeLa standard run.

    Checks the filename stem (without extension). For Bruker .d files,
    this is the directory name.

    Args:
        path: Path to .d directory or .raw file.
        pattern: Compiled regex pattern (from compile_qc_pattern).

    Returns:
        True if the filename matches the QC pattern.
    """
    if pattern is None:
        pattern = compile_qc_pattern()

    name = path.stem
    return bool(pattern.search(name))


def filter_qc_files(files: list[Path], pattern: re.Pattern | None = None) -> list[Path]:
    """Filter a list of raw files to only QC/HeLa standards.

    Args:
        files: List of raw file paths.
        pattern: Compiled regex pattern.

    Returns:
        Filtered list containing only QC files.
    """
    if pattern is None:
        pattern = compile_qc_pattern()

    matched = [f for f in files if is_qc_file(f, pattern)]
    logger.info(
        "QC filter: %d/%d files matched pattern", len(matched), len(files)
    )
    return matched
