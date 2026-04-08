"""Detect DIA-NN and Sage versions from their binaries.

Different versions produce different search results, so STAN tracks the
version used for every run. This is critical for the community benchmark:
a submission from DIA-NN 1.8.1 is not directly comparable to one from
DIA-NN 2.3.0.
"""

from __future__ import annotations

import logging
import re
import subprocess

logger = logging.getLogger(__name__)


def detect_diann_version(diann_exe: str = "diann") -> str | None:
    """Run DIA-NN with no arguments and parse the version from the header.

    DIA-NN prints something like:
        DIA-NN 2.3.0 Academia  (Data-Independent Acquisition by Neural Networks)
        Compiled on Sep 26 2025 02:56:25

    Returns:
        Version string like "2.3.0" or None if detection failed.
    """
    try:
        result = subprocess.run(
            [diann_exe],
            capture_output=True, text=True, timeout=30,
        )
        output = (result.stdout or "") + (result.stderr or "")

        # Match "DIA-NN X.Y.Z" anywhere in the first few lines
        m = re.search(r"DIA-NN\s+(\d+\.\d+(?:\.\d+)?)", output)
        if m:
            return m.group(1)

        # Fallback: try with --help
        result = subprocess.run(
            [diann_exe, "--help"],
            capture_output=True, text=True, timeout=30,
        )
        output = (result.stdout or "") + (result.stderr or "")
        m = re.search(r"DIA-NN\s+(\d+\.\d+(?:\.\d+)?)", output)
        if m:
            return m.group(1)

    except FileNotFoundError:
        logger.warning("DIA-NN not found at: %s", diann_exe)
    except subprocess.TimeoutExpired:
        logger.warning("DIA-NN version detection timed out")
    except Exception:
        logger.exception("Failed to detect DIA-NN version")

    return None


def detect_sage_version(sage_exe: str = "sage") -> str | None:
    """Run Sage with --version and parse the output.

    Sage prints something like:
        sage 0.14.7

    Returns:
        Version string like "0.14.7" or None.
    """
    try:
        result = subprocess.run(
            [sage_exe, "--version"],
            capture_output=True, text=True, timeout=10,
        )
        output = (result.stdout or "") + (result.stderr or "")
        m = re.search(r"sage\s+(\d+\.\d+(?:\.\d+)?)", output, re.IGNORECASE)
        if m:
            return m.group(1)
    except FileNotFoundError:
        logger.warning("Sage not found at: %s", sage_exe)
    except Exception:
        logger.exception("Failed to detect Sage version")

    return None


def check_diann_commercial_license(version: str | None) -> bool:
    """Return True if this DIA-NN version is free for commercial use.

    DIA-NN versions <= 1.9.1 are free for all users.
    Versions >= 1.9.2 require a paid license for commercial use.
    """
    if not version:
        return False
    try:
        parts = [int(p) for p in version.split(".")]
        # Pad to 3 parts
        while len(parts) < 3:
            parts.append(0)
        major, minor, patch = parts[:3]

        if major < 1:
            return True
        if major == 1 and minor < 9:
            return True
        if major == 1 and minor == 9 and patch < 2:
            return True
        return False
    except (ValueError, IndexError):
        return False
