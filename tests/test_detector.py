"""Tests for acquisition mode detection."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from stan.watcher.detector import AcquisitionMode, detect_bruker_mode, is_dda, is_dia


def test_detect_bruker_dia(mock_d_dir: Path) -> None:
    """diaPASEF .d directory (MsmsType=9) should be detected as DIA_PASEF."""
    mode = detect_bruker_mode(mock_d_dir)
    assert mode == AcquisitionMode.DIA_PASEF


def test_detect_bruker_dda(mock_d_dir_dda: Path) -> None:
    """ddaPASEF .d directory (MsmsType=8) should be detected as DDA_PASEF."""
    mode = detect_bruker_mode(mock_d_dir_dda)
    assert mode == AcquisitionMode.DDA_PASEF


def test_detect_bruker_missing_tdf(tmp_path: Path) -> None:
    """Empty .d directory (no analysis.tdf) should return UNKNOWN."""
    d_dir = tmp_path / "empty.d"
    d_dir.mkdir()
    mode = detect_bruker_mode(d_dir)
    assert mode == AcquisitionMode.UNKNOWN


def test_detect_bruker_ms1_only(tmp_path: Path) -> None:
    """Frames with only MsmsType=0 (MS1) should return UNKNOWN."""
    d_dir = tmp_path / "ms1_only.d"
    d_dir.mkdir()
    tdf = d_dir / "analysis.tdf"
    con = sqlite3.connect(str(tdf))
    con.execute("CREATE TABLE Frames (Id INTEGER, MsmsType INTEGER)")
    con.execute("INSERT INTO Frames VALUES (1, 0)")
    con.commit()
    con.close()

    mode = detect_bruker_mode(d_dir)
    assert mode == AcquisitionMode.UNKNOWN


def test_acquisition_mode_enum_values() -> None:
    """Verify string values of AcquisitionMode enum."""
    assert AcquisitionMode.DIA_PASEF.value == "diaPASEF"
    assert AcquisitionMode.DDA_PASEF.value == "ddaPASEF"
    assert AcquisitionMode.DIA_ORBITRAP.value == "DIA"
    assert AcquisitionMode.DDA_ORBITRAP.value == "DDA"
    assert AcquisitionMode.UNKNOWN.value == "unknown"


def test_is_dia() -> None:
    """is_dia should return True for DIA modes only."""
    assert is_dia(AcquisitionMode.DIA_PASEF) is True
    assert is_dia(AcquisitionMode.DIA_ORBITRAP) is True
    assert is_dia(AcquisitionMode.DDA_PASEF) is False
    assert is_dia(AcquisitionMode.UNKNOWN) is False


def test_is_dda() -> None:
    """is_dda should return True for DDA modes only."""
    assert is_dda(AcquisitionMode.DDA_PASEF) is True
    assert is_dda(AcquisitionMode.DDA_ORBITRAP) is True
    assert is_dda(AcquisitionMode.DIA_PASEF) is False
    assert is_dda(AcquisitionMode.UNKNOWN) is False
