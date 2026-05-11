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
    except subprocess.CalledProcessError as e:
        # TRFP sometimes exits non-zero on certain .raw files (0x8007000C
        # path errors, locked files, etc.). Non-fatal — the caller falls
        # back to filename tokens or the DIA default. Quiet INFO log so
        # the traceback doesn't scare operators.
        logger.info(
            "TRFP mode-detect returned exit %d for %s; falling back",
            e.returncode, raw_path.name,
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
    """Parse ThermoRawFileParser JSON metadata to determine DIA vs DDA mode.

    TRFP -f=4 -m=0 (metadata-only) produces FileProperties, InstrumentProperties,
    MsData, ScanSettings, and SampleData — it does NOT include a per-scan Scans
    array with individual ScanFilter strings. Per-scan filters are only emitted
    with full spectrum output (-f=1 mzML), which is too slow for mode detection.

    Strategy (in priority order):
    1. Per-scan ScanFilter strings — present when caller passes full-spectrum JSON.
       The canonical DDA marker is the 'd' flag: "FTMS + c NSI d Full ms2 <mz>@..."
       The 'd' means data-dependent; DIA scans omit it.
    2. SampleData acquisition method name — "dia"/"dda" substring match. Weak
       signal; only used when per-scan data is absent.
    3. MsData MS2/MS1 ratio — ratio ≥ 15 strongly implies DIA (DDA top-N rarely
       exceeds 15); ratio < 15 is ambiguous and left as UNKNOWN.
    """
    # --- Strategy 1: per-scan ScanFilter strings (full-spectrum JSON) ---
    scan_filters: list[str] = []
    for scan in metadata.get("Scans", metadata.get("scans", [])):
        filt = scan.get("ScanFilter", scan.get("scanFilter", ""))
        if filt:
            scan_filters.append(filt)

    if scan_filters:
        # The 'd' flag immediately before "Full ms2" (or anywhere before the
        # isolation m/z) marks data-dependent acquisition. DIA scans omit it.
        # Match " d " as a whitespace-bounded token to avoid hitting "dd-" etc.
        dda_pattern = re.compile(r"\bNSI\s+d\s+Full\b|\bNSI\s+sa\s+d\s+Full\b", re.IGNORECASE)
        ms2_filters = [f for f in scan_filters if "ms2" in f.lower()]
        if ms2_filters:
            dda_count = sum(1 for f in ms2_filters if dda_pattern.search(f))
            if dda_count > len(ms2_filters) * 0.5:
                logger.info("Mode from scan-filter 'd' flag: DDA for %s", raw_path.name)
                return AcquisitionMode.DDA_ORBITRAP
            logger.info("Mode from scan-filter (no 'd' flag): DIA for %s", raw_path.name)
            return AcquisitionMode.DIA_ORBITRAP

        logger.warning("No MS2 ScanFilter strings found in metadata for %s", raw_path)
        return AcquisitionMode.UNKNOWN

    # --- Strategy 2: acquisition method name in SampleData ---
    for item in metadata.get("SampleData", []):
        name = item.get("name", "")
        value = item.get("value", "")
        if "method" in name.lower() and value:
            method_lower = value.lower()
            # Use non-alpha lookaround instead of \b — underscores are \w
            # so \bdia\b would NOT match "_dia_". We want to match "dia"
            # when surrounded by non-letter chars (underscores, spaces,
            # backslashes, dots, digits) but NOT inside words like "media"
            # or "paladia". (?<![a-z]) and (?![a-z]) handles this correctly.
            if re.search(r"(?<![a-z])dia(?![a-z])", method_lower):
                logger.info("Mode from method name token 'dia': DIA for %s", raw_path.name)
                return AcquisitionMode.DIA_ORBITRAP
            if re.search(r"(?<![a-z])dda(?![a-z])", method_lower):
                logger.info("Mode from method name token 'dda': DDA for %s", raw_path.name)
                return AcquisitionMode.DDA_ORBITRAP

    # --- Strategy 3: MS2/MS1 scan count ratio from MsData ---
    ms1_count = ms2_count = 0
    for item in metadata.get("MsData", []):
        name = item.get("name", "")
        value = item.get("value", "")
        if "Number of MS1" in name:
            try:
                ms1_count = int(value)
            except (ValueError, TypeError):
                pass
        elif "Number of MS2" in name:
            try:
                ms2_count = int(value)
            except (ValueError, TypeError):
                pass

    if ms1_count > 10 and ms2_count > 0:
        ratio = ms2_count / ms1_count
        if ratio >= 15:
            # Very high ratio — almost certainly DIA (top-N DDA rarely exceeds 15)
            logger.info(
                "Mode from MS2/MS1 ratio %.1f (≥15): DIA for %s", ratio, raw_path.name,
            )
            return AcquisitionMode.DIA_ORBITRAP
        logger.info(
            "MS2/MS1 ratio %.1f ambiguous (<15) — returning UNKNOWN for %s",
            ratio, raw_path.name,
        )

    logger.warning(
        "Could not classify Thermo acquisition mode for %s — no scan filters, "
        "no dia/dda method token, MS2/MS1 ratio ambiguous or unavailable.",
        raw_path.name,
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
