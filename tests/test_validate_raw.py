"""Tests for raw file validation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stan.watcher.validate_raw import (
    RawFileValidationError,
    validate_bruker_d,
    validate_raw_file,
    validate_thermo_raw,
)


def _make_valid_tdf(path: Path) -> None:
    """Create a minimal valid analysis.tdf SQLite database."""
    with sqlite3.connect(str(path)) as con:
        con.execute("CREATE TABLE Frames (Id INTEGER, MsmsType INTEGER, Time REAL)")
        con.execute("INSERT INTO Frames VALUES (1, 8, 0.5)")
        con.execute("INSERT INTO Frames VALUES (2, 9, 1.0)")
        con.commit()


def test_valid_bruker_d(tmp_path: Path) -> None:
    """A complete .d directory should pass validation."""
    d = tmp_path / "good.d"
    d.mkdir()
    _make_valid_tdf(d / "analysis.tdf")
    (d / "analysis.tdf_bin").write_bytes(b"fake binary data")
    validate_bruker_d(d)  # should not raise


def test_bruker_d_missing_tdf(tmp_path: Path) -> None:
    """Missing analysis.tdf should fail."""
    d = tmp_path / "bad.d"
    d.mkdir()
    (d / "analysis.tdf_bin").write_bytes(b"x")
    with pytest.raises(RawFileValidationError, match="missing analysis.tdf"):
        validate_bruker_d(d)


def test_bruker_d_missing_tdf_bin(tmp_path: Path) -> None:
    """Missing analysis.tdf_bin should fail."""
    d = tmp_path / "bad.d"
    d.mkdir()
    _make_valid_tdf(d / "analysis.tdf")
    with pytest.raises(RawFileValidationError, match="missing analysis.tdf_bin"):
        validate_bruker_d(d)


def test_bruker_d_corrupt_tdf(tmp_path: Path) -> None:
    """A corrupt .tdf that isn't valid SQLite should fail."""
    d = tmp_path / "bad.d"
    d.mkdir()
    (d / "analysis.tdf").write_bytes(b"not a sqlite database")
    (d / "analysis.tdf_bin").write_bytes(b"x")
    with pytest.raises(RawFileValidationError, match="not a valid SQLite"):
        validate_bruker_d(d)


def test_bruker_d_empty_frames(tmp_path: Path) -> None:
    """An .tdf with zero frames should fail."""
    d = tmp_path / "bad.d"
    d.mkdir()
    with sqlite3.connect(str(d / "analysis.tdf")) as con:
        con.execute("CREATE TABLE Frames (Id INTEGER, MsmsType INTEGER)")
        con.commit()
    (d / "analysis.tdf_bin").write_bytes(b"x")
    with pytest.raises(RawFileValidationError, match="no frames"):
        validate_bruker_d(d)


def test_bruker_d_not_directory(tmp_path: Path) -> None:
    """A file (not a directory) should fail."""
    f = tmp_path / "notadir.d"
    f.write_bytes(b"x")
    with pytest.raises(RawFileValidationError, match="must be a directory"):
        validate_bruker_d(f)


def test_valid_thermo_raw(tmp_path: Path) -> None:
    """A non-empty .raw file with valid header should pass."""
    f = tmp_path / "good.raw"
    # Minimum 100KB with realistic header bytes
    f.write_bytes(b"\x01\xA1" + b"\x00" * 100000)
    validate_thermo_raw(f)  # should not raise


def test_thermo_raw_empty(tmp_path: Path) -> None:
    f = tmp_path / "empty.raw"
    f.write_bytes(b"")
    with pytest.raises(RawFileValidationError, match="Empty"):
        validate_thermo_raw(f)


def test_thermo_raw_too_small(tmp_path: Path) -> None:
    f = tmp_path / "tiny.raw"
    f.write_bytes(b"\x01" * 100)
    with pytest.raises(RawFileValidationError, match="Suspiciously small"):
        validate_thermo_raw(f)


def test_thermo_raw_all_zeros_header(tmp_path: Path) -> None:
    f = tmp_path / "zeros.raw"
    f.write_bytes(b"\x00" * 200000)
    with pytest.raises(RawFileValidationError, match="all zeros"):
        validate_thermo_raw(f)


def test_validate_raw_file_auto_detect(tmp_path: Path) -> None:
    """Auto-detection by extension."""
    d = tmp_path / "auto.d"
    d.mkdir()
    _make_valid_tdf(d / "analysis.tdf")
    (d / "analysis.tdf_bin").write_bytes(b"x")
    validate_raw_file(d)  # should detect as bruker

    r = tmp_path / "auto.raw"
    r.write_bytes(b"\x01\xA1" + b"\x00" * 200000)
    validate_raw_file(r)  # should detect as thermo
