"""Acquisition mode detection from raw file metadata.

Bruker .d: reads MsmsType from analysis.tdf SQLite database.
Thermo .raw: runs ThermoRawFileParser for metadata extraction, parses ScanFilter strings.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class AcquisitionMode(Enum):
    """Detected acquisition mode from raw file."""

    DIA_PASEF = "diaPASEF"
    DDA_PASEF = "ddaPASEF"
    DIA_ORBITRAP = "DIA"
    DDA_ORBITRAP = "DDA"
    UNKNOWN = "unknown"


def is_dia(mode: AcquisitionMode) -> bool:
    """Return True if mode is any DIA variant."""
    return mode in (AcquisitionMode.DIA_PASEF, AcquisitionMode.DIA_ORBITRAP)


def is_dda(mode: AcquisitionMode) -> bool:
    """Return True if mode is any DDA variant."""
    return mode in (AcquisitionMode.DDA_PASEF, AcquisitionMode.DDA_ORBITRAP)


def detect_bruker_mode(d_path: Path) -> AcquisitionMode:
    """Read MsmsType from analysis.tdf Frames table.

    MsmsType values: 0=MS1, 8=ddaPASEF, 9=diaPASEF.
    """
    tdf = d_path / "analysis.tdf"
    if not tdf.exists():
        logger.warning("analysis.tdf not found in %s", d_path)
        return AcquisitionMode.UNKNOWN

    try:
        with sqlite3.connect(str(tdf)) as con:
            rows = con.execute(
                "SELECT DISTINCT MsmsType FROM Frames WHERE MsmsType > 0"
            ).fetchall()
    except sqlite3.Error:
        logger.exception("Failed to read analysis.tdf: %s", tdf)
        return AcquisitionMode.UNKNOWN

    types = {r[0] for r in rows}
    if 9 in types:
        return AcquisitionMode.DIA_PASEF
    if 8 in types:
        return AcquisitionMode.DDA_PASEF

    logger.warning("Unrecognized MsmsType values in %s: %s", tdf, types)
    return AcquisitionMode.UNKNOWN


def detect_thermo_mode(
    raw_path: Path,
    trfp_path: Path,
    output_dir: Path,
) -> AcquisitionMode:
    """Detect acquisition mode from Thermo .raw file via ThermoRawFileParser.

    Runs ThermoRawFileParser with -f=4 -m=0 (metadata-only JSON output),
    then parses ScanFilter strings to identify DIA vs DDA.
    """
    metadata_path = output_dir / f"{raw_path.stem}-metadata.json"

    try:
        subprocess.run(
            [
                "dotnet", str(trfp_path),
                f"-i={raw_path}",
                f"-b={metadata_path}",
                "-f=4",
                "-m=0",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError:
        logger.error(
            "dotnet or ThermoRawFileParser not found at %s. "
            "Cannot detect Thermo acquisition mode.",
            trfp_path,
        )
        return AcquisitionMode.UNKNOWN
    except subprocess.SubprocessError:
        logger.exception("ThermoRawFileParser failed for %s", raw_path)
        return AcquisitionMode.UNKNOWN

    try:
        metadata = json.loads(metadata_path.read_text())
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to parse metadata JSON: %s", metadata_path)
        return AcquisitionMode.UNKNOWN

    return _parse_thermo_scan_filters(metadata, raw_path)


def _parse_thermo_scan_filters(metadata: dict, raw_path: Path) -> AcquisitionMode:
    """Parse ScanFilter strings from ThermoRawFileParser JSON metadata."""
    scan_filters: list[str] = []
    for scan in metadata.get("Scans", metadata.get("scans", [])):
        filt = scan.get("ScanFilter", scan.get("scanFilter", ""))
        if filt:
            scan_filters.append(filt)

    if not scan_filters:
        logger.warning("No ScanFilter strings found in metadata for %s", raw_path)
        return AcquisitionMode.UNKNOWN

    # Pattern matching — not hardcoded string equality
    dia_pattern = re.compile(r"\bDIA\b", re.IGNORECASE)
    dda_pattern = re.compile(r"\b(dd-MS2|Full\s+ms2)\b", re.IGNORECASE)

    dia_count = sum(1 for f in scan_filters if dia_pattern.search(f))
    dda_count = sum(1 for f in scan_filters if dda_pattern.search(f))

    if dia_count > dda_count:
        return AcquisitionMode.DIA_ORBITRAP
    if dda_count > 0:
        return AcquisitionMode.DDA_ORBITRAP

    # Log unrecognized formats for debugging
    unique_filters = set(scan_filters[:10])  # sample for logging
    logger.warning(
        "Could not classify scan filters for %s. Samples: %s",
        raw_path,
        unique_filters,
    )
    return AcquisitionMode.UNKNOWN


def detect_mode(path: Path, vendor: str, **kwargs) -> AcquisitionMode:
    """Dispatch to vendor-specific mode detection.

    Args:
        path: Path to .d directory or .raw file.
        vendor: "bruker" or "thermo".
        **kwargs: For thermo, optional trfp_path and output_dir overrides.
            If not provided, TRFP is auto-discovered from the installed
            tools directory (the one-click installer puts it in
            ~/.stan/tools/trfp/).
    """
    if vendor == "bruker":
        return detect_bruker_mode(path)
    if vendor == "thermo":
        trfp_path = kwargs.get("trfp_path")
        output_dir = kwargs.get("output_dir")

        # Auto-discover TRFP if not provided — the one-click installer
        # and `stan setup` both install it to a known location.
        if trfp_path is None:
            try:
                from stan.tools.trfp import ensure_installed
                trfp_path = str(ensure_installed())
            except Exception:
                logger.debug(
                    "TRFP auto-discovery failed for mode detection: %s",
                    path.name, exc_info=True,
                )
        if output_dir is None:
            import tempfile
            output_dir = Path(tempfile.mkdtemp(prefix="stan_mode_"))

        if trfp_path is None:
            logger.debug("TRFP not available for mode detection: %s", path.name)
            return AcquisitionMode.UNKNOWN
        return detect_thermo_mode(path, Path(trfp_path), Path(output_dir))

    logger.error("Unknown vendor: %s", vendor)
    return AcquisitionMode.UNKNOWN
