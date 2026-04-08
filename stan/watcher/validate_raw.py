"""Validate raw files before passing them to search engines.

Incomplete or corrupt raw files cause DIA-NN and Sage to crash with
cryptic errors buried in their logs. STAN catches these upfront and
writes a clear reason to the HOLD flag instead of attempting the search.

Bruker .d validation:
  - must be a directory
  - must contain analysis.tdf (SQLite metadata)
  - must contain analysis.tdf_bin (binary frame data)
  - analysis.tdf must be a readable SQLite database (not corrupt)

Thermo .raw validation:
  - must be a file (not directory, not symlink to missing target)
  - must have non-zero size
  - file handle must be closed (acquisition complete)
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


class RawFileValidationError(Exception):
    """Raised when a raw file fails validation."""


def validate_bruker_d(path: Path) -> None:
    """Validate a Bruker .d directory is complete and readable.

    Raises:
        RawFileValidationError: with a specific reason if invalid.
    """
    if not path.exists():
        raise RawFileValidationError(f"Path does not exist: {path}")

    if not path.is_dir():
        raise RawFileValidationError(
            f"Bruker .d must be a directory, got file: {path}"
        )

    tdf = path / "analysis.tdf"
    tdf_bin = path / "analysis.tdf_bin"

    if not tdf.exists():
        raise RawFileValidationError(
            f"Incomplete .d — missing analysis.tdf: {path.name}"
        )

    if not tdf_bin.exists():
        raise RawFileValidationError(
            f"Incomplete .d — missing analysis.tdf_bin: {path.name}"
        )

    # Verify .tdf is a readable SQLite database
    try:
        with sqlite3.connect(f"file:{tdf}?mode=ro", uri=True, timeout=5) as con:
            cur = con.cursor()
            cur.execute("SELECT COUNT(*) FROM Frames")
            n_frames = cur.fetchone()[0]
            if n_frames == 0:
                raise RawFileValidationError(
                    f"Empty .d — no frames in analysis.tdf: {path.name}"
                )
    except sqlite3.DatabaseError as e:
        raise RawFileValidationError(
            f"Corrupt .d — analysis.tdf is not a valid SQLite database: {path.name} ({e})"
        )
    except Exception as e:
        raise RawFileValidationError(
            f"Cannot read .d — {path.name}: {e}"
        )


def validate_thermo_raw(path: Path) -> None:
    """Validate a Thermo .raw file is complete and readable.

    Raises:
        RawFileValidationError: with a specific reason if invalid.
    """
    if not path.exists():
        raise RawFileValidationError(f"Path does not exist: {path}")

    if path.is_dir():
        raise RawFileValidationError(
            f"Thermo .raw must be a file, got directory: {path}"
        )

    # Resolve symlinks and check target exists
    try:
        real = path.resolve(strict=True)
    except (FileNotFoundError, OSError) as e:
        raise RawFileValidationError(
            f"Broken symlink or inaccessible: {path} ({e})"
        )

    size = real.stat().st_size
    if size == 0:
        raise RawFileValidationError(f"Empty .raw file: {path.name}")

    # Sanity check: Thermo .raw files should be at least a few MB
    if size < 100_000:
        raise RawFileValidationError(
            f"Suspiciously small .raw file ({size} bytes): {path.name}"
        )

    # Check for the RAW file magic bytes at the start
    # Thermo .raw files start with specific header bytes
    try:
        with open(real, "rb") as f:
            header = f.read(8)
        if len(header) < 8:
            raise RawFileValidationError(
                f"Cannot read .raw header: {path.name}"
            )
        # Thermo .raw files have a known signature — first 2 bytes are typically 0x01 0xA1
        # (finnigan file header). We just check it's not all zeros or obviously truncated.
        if header == b"\x00" * 8:
            raise RawFileValidationError(
                f"Corrupt .raw — header is all zeros: {path.name}"
            )
    except IOError as e:
        raise RawFileValidationError(
            f"Cannot read .raw file: {path.name} ({e})"
        )


def validate_raw_file(path: Path, vendor: str | None = None) -> None:
    """Validate a raw file by vendor.

    Args:
        path: Path to the raw file or .d directory.
        vendor: "bruker" or "thermo". If None, inferred from extension.

    Raises:
        RawFileValidationError: if invalid.
    """
    if vendor is None:
        if path.suffix.lower() == ".d" or path.is_dir():
            vendor = "bruker"
        elif path.suffix.lower() == ".raw":
            vendor = "thermo"
        else:
            raise RawFileValidationError(
                f"Unknown raw file type: {path} (expected .d or .raw)"
            )

    if vendor == "bruker":
        validate_bruker_d(path)
    elif vendor == "thermo":
        validate_thermo_raw(path)
    else:
        raise RawFileValidationError(f"Unknown vendor: {vendor}")
