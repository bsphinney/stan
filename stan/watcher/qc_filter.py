"""QC file detection from filenames.

Identifies HeLa QC standard runs mixed in with other sample files.
Used by both the watcher (real-time) and baseline (retroactive).

Default patterns match common QC naming conventions across labs:
  - HeLa / hela / HELA / HeL50 / HeLa50 / HeLa50ng
  - QC / qc (anywhere in the name)
  - Std_He / STD_HE / std_hela
  - Standard.*HeLa

Users can override with a custom regex in instruments.yml:
    qc_pattern: "(?i)(hel[a5]|qc|std.*he)"
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Default QC filename patterns (case-insensitive)
# Matches:
#   HeLa, hela, HELA, HeL50, HeLa50ng, HeLa50  (hel followed by a or 5)
#   QC, qc, QCex  (qc anywhere in the name)
#   Std_He, STD_HE, std_hela, Std_HeLa  (std followed by he)
DEFAULT_QC_PATTERN = r"(?i)(hel[a5]|qc|std[_\-\s]?he)"


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
