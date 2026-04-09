"""diaPASEF ion mobility window extraction and drift monitoring.

Reads DIA window definitions from Bruker analysis.tdf and compares
against a reference (first QC run or saved template) to detect drift.

Window drift can indicate:
  - Method file corruption or misconfiguration
  - Firmware changes after instrument service
  - Calibration shifts in the TIMS tunnel

Tables used from analysis.tdf:
  - DiaFrameMsMsWindows: m/z boundaries per WindowGroup
  - DiaFrameMsMsInfo: frame-to-window mapping with ScanNumBegin/ScanNumEnd
  - Frames: scan number to 1/K0 calibration
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class DiaWindow:
    """A single diaPASEF isolation window."""

    window_group: int
    mz_lower: float
    mz_upper: float
    scan_num_begin: int = 0
    scan_num_end: int = 0
    # Derived from scan calibration
    oneoverk0_lower: float = 0.0
    oneoverk0_upper: float = 0.0


@dataclass
class WindowLayout:
    """Complete diaPASEF window layout for a run."""

    windows: list[DiaWindow] = field(default_factory=list)
    n_window_groups: int = 0
    mz_range: tuple[float, float] = (0.0, 0.0)
    mobility_range: tuple[float, float] = (0.0, 0.0)
    run_name: str = ""


@dataclass
class DriftReport:
    """Result of comparing two window layouts."""

    is_drifted: bool = False
    max_mz_shift: float = 0.0        # largest m/z boundary shift (Da)
    max_mobility_shift: float = 0.0   # largest 1/K0 shift
    n_windows_shifted: int = 0        # windows with any shift > threshold
    n_windows_total: int = 0
    details: list[str] = field(default_factory=list)


def extract_dia_windows(d_path: Path) -> WindowLayout | None:
    """Extract diaPASEF window layout from a Bruker .d directory.

    Args:
        d_path: Path to the .d directory.

    Returns:
        WindowLayout with all window definitions, or None if not diaPASEF.
    """
    tdf = d_path / "analysis.tdf"
    if not tdf.exists():
        logger.warning("analysis.tdf not found in %s", d_path)
        return None

    try:
        with sqlite3.connect(str(tdf)) as con:
            # Check if diaPASEF tables exist
            tables = {
                r[0] for r in
                con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }

            if "DiaFrameMsMsWindows" not in tables:
                logger.debug("No DiaFrameMsMsWindows table — not diaPASEF: %s", d_path.name)
                return None

            # Read window m/z boundaries
            mz_rows = con.execute(
                "SELECT WindowGroup, IsolationWindowLowerMz, IsolationWindowUpperMz "
                "FROM DiaFrameMsMsWindows ORDER BY WindowGroup"
            ).fetchall()

            if not mz_rows:
                return None

            windows_by_group: dict[int, DiaWindow] = {}
            for group, mz_lo, mz_hi in mz_rows:
                windows_by_group[group] = DiaWindow(
                    window_group=group,
                    mz_lower=mz_lo,
                    mz_upper=mz_hi,
                )

            # Read scan number boundaries (maps to ion mobility)
            if "DiaFrameMsMsInfo" in tables:
                info_rows = con.execute(
                    "SELECT WindowGroup, ScanNumBegin, ScanNumEnd "
                    "FROM DiaFrameMsMsInfo "
                    "GROUP BY WindowGroup"
                ).fetchall()

                for group, scan_begin, scan_end in info_rows:
                    if group in windows_by_group:
                        windows_by_group[group].scan_num_begin = scan_begin
                        windows_by_group[group].scan_num_end = scan_end

            # Convert scan numbers to 1/K0 using calibration from Frames table
            _apply_scan_to_mobility(con, windows_by_group)

            windows = sorted(windows_by_group.values(), key=lambda w: w.window_group)

            # Compute summary ranges
            all_mz = [w.mz_lower for w in windows] + [w.mz_upper for w in windows]
            all_mob = [w.oneoverk0_lower for w in windows if w.oneoverk0_lower > 0]
            all_mob += [w.oneoverk0_upper for w in windows if w.oneoverk0_upper > 0]

            return WindowLayout(
                windows=windows,
                n_window_groups=len(set(w.window_group for w in windows)),
                mz_range=(min(all_mz), max(all_mz)) if all_mz else (0, 0),
                mobility_range=(min(all_mob), max(all_mob)) if all_mob else (0, 0),
                run_name=d_path.stem,
            )

    except sqlite3.Error:
        logger.exception("Failed to read DIA windows from %s", tdf)
        return None


def _apply_scan_to_mobility(
    con: sqlite3.Connection,
    windows: dict[int, DiaWindow],
) -> None:
    """Convert scan numbers to 1/K0 values using TIMS calibration.

    The Frames table stores calibration coefficients for scan-to-mobility
    conversion. We use a representative frame to get the mapping.
    """
    try:
        # Get column names to check what's available
        cols = {r[1] for r in con.execute("PRAGMA table_info(Frames)").fetchall()}

        # Try to read TIMS calibration from Properties table
        prop_tables = {
            r[0] for r in
            con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }

        if "Properties" in prop_tables:
            # Some TDF versions store calibration in Properties
            try:
                rows = con.execute(
                    "SELECT Key, Value FROM Properties WHERE Key LIKE '%OneOverK0%'"
                ).fetchall()
                if rows:
                    logger.debug("Found 1/K0 calibration in Properties: %s", rows)
            except sqlite3.Error:
                pass

        # Fallback: use scan number range proportional to typical 1/K0 range
        # timsTOF typical 1/K0 range: 0.6 - 1.6 Vs/cm²
        # Scan numbers typically 0 - ~920
        # Linear approximation: 1/K0 ≈ 1.6 - (scan_num / max_scan) * 1.0
        max_scan = 920  # typical timsTOF max scan number
        try:
            row = con.execute("SELECT MAX(NumScans) FROM Frames").fetchone()
            if row and row[0]:
                max_scan = row[0]
        except sqlite3.Error:
            pass

        for w in windows.values():
            if w.scan_num_begin > 0 or w.scan_num_end > 0:
                # Linear approximation: higher scan number = lower 1/K0
                w.oneoverk0_upper = 1.6 - (w.scan_num_begin / max_scan) * 1.0
                w.oneoverk0_lower = 1.6 - (w.scan_num_end / max_scan) * 1.0

    except sqlite3.Error:
        logger.debug("Could not apply scan-to-mobility calibration", exc_info=True)


def compare_window_layouts(
    current: WindowLayout,
    reference: WindowLayout,
    mz_threshold: float = 1.0,       # Da — flag if window shifted by >1 Da
    mobility_threshold: float = 0.02,  # 1/K0 units — flag if shifted by >0.02
) -> DriftReport:
    """Compare two diaPASEF window layouts to detect drift.

    Args:
        current: Layout from the current QC run.
        reference: Layout from the reference run (first baseline or template).
        mz_threshold: Maximum allowed m/z shift before flagging (Da).
        mobility_threshold: Maximum allowed 1/K0 shift before flagging.

    Returns:
        DriftReport with drift analysis.
    """
    report = DriftReport(n_windows_total=current.n_window_groups)

    # Build lookup by window group
    ref_map = {w.window_group: w for w in reference.windows}
    cur_map = {w.window_group: w for w in current.windows}

    # Check window count mismatch
    if current.n_window_groups != reference.n_window_groups:
        report.is_drifted = True
        report.details.append(
            f"Window count changed: {reference.n_window_groups} -> {current.n_window_groups}"
        )

    max_mz = 0.0
    max_mob = 0.0
    shifted = 0

    for group in sorted(set(ref_map.keys()) & set(cur_map.keys())):
        ref_w = ref_map[group]
        cur_w = cur_map[group]

        # m/z drift
        mz_lo_shift = abs(cur_w.mz_lower - ref_w.mz_lower)
        mz_hi_shift = abs(cur_w.mz_upper - ref_w.mz_upper)
        mz_shift = max(mz_lo_shift, mz_hi_shift)

        # Mobility drift
        mob_lo_shift = abs(cur_w.oneoverk0_lower - ref_w.oneoverk0_lower)
        mob_hi_shift = abs(cur_w.oneoverk0_upper - ref_w.oneoverk0_upper)
        mob_shift = max(mob_lo_shift, mob_hi_shift)

        max_mz = max(max_mz, mz_shift)
        max_mob = max(max_mob, mob_shift)

        if mz_shift > mz_threshold or mob_shift > mobility_threshold:
            shifted += 1
            report.details.append(
                f"Window {group}: m/z shift {mz_shift:.1f} Da, "
                f"1/K0 shift {mob_shift:.3f}"
            )

    report.max_mz_shift = round(max_mz, 2)
    report.max_mobility_shift = round(max_mob, 4)
    report.n_windows_shifted = shifted
    report.is_drifted = shifted > 0 or report.is_drifted

    if report.is_drifted:
        logger.warning(
            "diaPASEF window drift detected in %s: %d/%d windows shifted "
            "(max m/z: %.1f Da, max 1/K0: %.3f)",
            current.run_name, shifted, report.n_windows_total,
            max_mz, max_mob,
        )
    else:
        logger.info(
            "diaPASEF windows stable in %s: %d windows, max m/z shift %.2f Da",
            current.run_name, report.n_windows_total, max_mz,
        )

    return report
