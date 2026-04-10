"""Extract acquisition date from raw files.

Bruker .d: reads AcquisitionDateTime from analysis.tdf GlobalMetadata table.
Thermo .raw: reads from ThermoRawFileParser JSON metadata (if available).

Returns ISO 8601 datetime string or None if extraction fails.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def get_acquisition_date(raw_path: Path) -> str | None:
    """Extract the real acquisition datetime from a raw file.

    Args:
        raw_path: Path to a .d directory or .raw file.

    Returns:
        ISO 8601 datetime string (e.g. '2024-06-04T15:32:57') or None.
    """
    path = Path(raw_path)

    if path.suffix.lower() == ".d" and path.is_dir():
        return _bruker_acquisition_date(path)
    elif path.suffix.lower() == ".raw" and path.is_file():
        return _thermo_acquisition_date(path)
    return None


def _bruker_acquisition_date(d_path: Path) -> str | None:
    """Read AcquisitionDateTime from analysis.tdf GlobalMetadata."""
    tdf = d_path / "analysis.tdf"
    if not tdf.exists():
        return None
    try:
        with sqlite3.connect(str(tdf)) as con:
            row = con.execute(
                "SELECT Value FROM GlobalMetadata WHERE Key = 'AcquisitionDateTime'"
            ).fetchone()
            if row and row[0]:
                # Parse ISO 8601 with timezone, return without tz for storage
                dt_str = row[0]
                # Handle timezone offset (e.g. 2024-06-04T15:32:57.862-07:00)
                dt = datetime.fromisoformat(dt_str)
                return dt.isoformat(timespec="seconds")
    except Exception:
        logger.debug("Failed to read acquisition date from %s", tdf, exc_info=True)
    return None


def _thermo_acquisition_date(raw_path: Path) -> str | None:
    """Read acquisition date from a Thermo .raw file.

    Tries three sources, in order:
      1. fisher_py RawFile.CreationDate (from .raw header — accurate)
      2. ThermoRawFileParser metadata JSON sidecar (if a previous step wrote one)
      3. None — caller can fall back to file mtime
    """
    # Try fisher_py first (reads straight from .raw header)
    try:
        from fisher_py import RawFile  # type: ignore

        rf = RawFile(str(raw_path))
        # fisher_py exposes CreationDate as a .NET DateTime; convert to ISO
        creation = getattr(rf, "creation_date", None) or getattr(rf, "CreationDate", None)
        if creation is not None:
            # .NET DateTime → ISO string; also handle already-ISO strings
            if hasattr(creation, "ToString"):
                dt_str = creation.ToString("o")
            else:
                dt_str = str(creation)
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
            return dt.isoformat(timespec="seconds")
    except Exception:
        logger.debug("fisher_py unavailable or failed for %s", raw_path, exc_info=True)

    # Fall back to ThermoRawFileParser JSON sidecar if present
    json_path = raw_path.with_suffix(".json")
    if not json_path.exists():
        json_path = raw_path.parent / (raw_path.stem + "-metadata.json")
    if json_path.exists():
        try:
            import json as _json
            meta = _json.loads(json_path.read_text())
            acq_date = meta.get("CreationDate") or meta.get("creation_date")
            if acq_date:
                dt = datetime.fromisoformat(acq_date)
                return dt.isoformat(timespec="seconds")
        except Exception:
            logger.debug("Failed to read %s", json_path, exc_info=True)

    return None
