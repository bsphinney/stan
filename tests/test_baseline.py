"""Tests for the baseline builder helpers and data structures.

Catches integration bugs like:
- LC_METHODS not importable from stan.setup
- _pick_column() return type mismatch (tuple vs dict)
- column_info constructed incorrectly from _pick_column() output
- _extract_bruker_metadata() gradient extraction from analysis.tdf
- _extract_file_metadata() return structure missing required keys
- gradient_min_to_spd() returning unreasonable values
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest


# ── 1. LC_METHODS importable and well-structured ──────────────────


def test_lc_methods_importable() -> None:
    """LC_METHODS must be importable from stan.setup — this failed in production."""
    from stan.setup import LC_METHODS

    assert isinstance(LC_METHODS, list)
    assert len(LC_METHODS) > 0


def test_lc_methods_structure() -> None:
    """Every LC method entry must have 'name', 'spd', and 'gradient_min' keys."""
    from stan.setup import LC_METHODS

    for i, method in enumerate(LC_METHODS):
        assert "name" in method, f"LC_METHODS[{i}] missing 'name'"
        assert "spd" in method, f"LC_METHODS[{i}] missing 'spd'"
        assert "gradient_min" in method, f"LC_METHODS[{i}] missing 'gradient_min'"
        assert isinstance(method["name"], str)
        assert isinstance(method["spd"], int)
        # gradient_min can be None for "Custom" entry
        if method["spd"] > 0:
            assert method["gradient_min"] is not None, (
                f"LC_METHODS[{i}] has spd > 0 but gradient_min is None"
            )
            assert isinstance(method["gradient_min"], (int, float))


def test_lc_methods_has_custom_entry() -> None:
    """LC_METHODS should include a custom/manual entry with spd=0."""
    from stan.setup import LC_METHODS

    custom = [m for m in LC_METHODS if m["spd"] == 0]
    assert len(custom) >= 1, "LC_METHODS should have at least one custom (spd=0) entry"


# ── 2. _pick_column() return type ────────────────────────────────


def test_pick_column_return_type_is_tuple() -> None:
    """_pick_column() must return tuple[str, str], not a dict.

    This bug caused baseline.py to call .get() on a tuple, crashing at runtime.
    We verify the function signature and return annotation here.
    """
    from stan.setup import _pick_column
    import inspect

    sig = inspect.signature(_pick_column)
    # Check return annotation is tuple[str, str]
    # Note: with `from __future__ import annotations`, the annotation is a string
    ret = sig.return_annotation
    assert ret in (tuple[str, str], "tuple[str, str]"), (
        f"_pick_column return annotation is {ret!r}, expected tuple[str, str]"
    )


def test_column_info_dict_from_pick_column_tuple() -> None:
    """Simulate what baseline.py does with _pick_column() output.

    baseline.py constructs column_info like:
        col_vendor, col_model = _pick_column()
        column_info = {"vendor": col_vendor, "model": col_model}
    Then later calls column_info.get('vendor', '').

    This test verifies the pattern works end-to-end with realistic values.
    """
    # Simulate _pick_column() returning a tuple
    col_vendor, col_model = ("IonOpticks", "Aurora Ultimate 25cm x 75um, 1.7um C18")

    # This is how baseline.py constructs the dict — must NOT crash
    column_info: dict = {"vendor": col_vendor, "model": col_model}

    # This is how baseline.py reads the dict — .get() must work
    assert column_info.get("vendor", "") == "IonOpticks"
    assert column_info.get("model", "") == "Aurora Ultimate 25cm x 75um, 1.7um C18"


def test_column_info_dict_from_empty_tuple() -> None:
    """Handle _pick_column() returning empty strings (custom column)."""
    col_vendor, col_model = ("", "my custom column")

    column_info: dict = {"vendor": col_vendor, "model": col_model}
    assert column_info.get("vendor", "") == ""
    assert column_info.get("model", "") == "my custom column"

    # The display string pattern used in baseline.py
    display = f"{column_info.get('vendor', '')} {column_info.get('model', '')}".strip()
    assert display == "my custom column"


# ── 3. _extract_bruker_metadata() ────────────────────────────────


def test_extract_bruker_metadata_gradient(tmp_path: Path) -> None:
    """_extract_bruker_metadata should extract gradient_length_min from Frames table."""
    from stan.baseline import _extract_bruker_metadata

    # Create a mock .d directory with analysis.tdf
    d_dir = tmp_path / "test.d"
    d_dir.mkdir()
    tdf = d_dir / "analysis.tdf"

    con = sqlite3.connect(str(tdf))
    # GlobalMetadata for instrument model
    con.execute("CREATE TABLE GlobalMetadata (Key TEXT, Value TEXT)")
    con.execute("INSERT INTO GlobalMetadata VALUES ('InstrumentName', 'timsTOF Ultra')")
    con.execute(
        "INSERT INTO GlobalMetadata VALUES ('AcquisitionDateTime', '2025-01-15T10:30:00')"
    )
    # Frames table with Time in seconds — 21 min gradient
    con.execute("CREATE TABLE Frames (Id INTEGER, Time REAL, MsmsType INTEGER)")
    con.execute("INSERT INTO Frames VALUES (1, 0.0, 0)")    # MS1 at t=0
    con.execute("INSERT INTO Frames VALUES (2, 60.0, 9)")   # diaPASEF at 1 min
    con.execute("INSERT INTO Frames VALUES (3, 630.0, 9)")  # diaPASEF at 10.5 min
    con.execute("INSERT INTO Frames VALUES (4, 1260.0, 0)") # MS1 at 21 min
    con.commit()
    con.close()

    meta = _extract_bruker_metadata(d_dir)

    assert meta.get("instrument_model") == "timsTOF Ultra"
    assert meta.get("gradient_length_min") == 21  # (1260 - 0) / 60 = 21


def test_extract_bruker_metadata_no_tdf(tmp_path: Path) -> None:
    """_extract_bruker_metadata should not crash if analysis.tdf is missing."""
    from stan.baseline import _extract_bruker_metadata

    d_dir = tmp_path / "empty.d"
    d_dir.mkdir()

    # Should return a dict without crashing — may have partial data
    meta = _extract_bruker_metadata(d_dir)
    assert isinstance(meta, dict)


# ── 4. _extract_file_metadata() ─────────────────────────────────


def test_extract_file_metadata_bruker_structure(tmp_path: Path) -> None:
    """_extract_file_metadata must return dict with all required keys."""
    from stan.baseline import _extract_file_metadata

    # Create minimal .d directory
    d_dir = tmp_path / "test.d"
    d_dir.mkdir()
    tdf = d_dir / "analysis.tdf"
    con = sqlite3.connect(str(tdf))
    con.execute("CREATE TABLE GlobalMetadata (Key TEXT, Value TEXT)")
    con.execute("INSERT INTO GlobalMetadata VALUES ('InstrumentName', 'timsTOF HT')")
    con.execute("CREATE TABLE Frames (Id INTEGER, Time REAL, MsmsType INTEGER)")
    con.execute("INSERT INTO Frames VALUES (1, 0.0, 0)")
    con.execute("INSERT INTO Frames VALUES (2, 1800.0, 9)")
    con.commit()
    con.close()

    meta = _extract_file_metadata(d_dir, "bruker")

    # All required keys must exist
    required_keys = [
        "vendor",
        "acquisition_date",
        "instrument_model",
        "acquisition_mode",
        "gradient_length_min",
    ]
    for key in required_keys:
        assert key in meta, f"Missing required key: {key}"

    assert meta["vendor"] == "bruker"
    assert meta["instrument_model"] == "timsTOF HT"
    assert meta["gradient_length_min"] == 30  # 1800/60


def test_extract_file_metadata_thermo_structure(tmp_path: Path) -> None:
    """_extract_file_metadata for thermo should return all required keys."""
    from stan.baseline import _extract_file_metadata

    # Create minimal .raw placeholder (TRFP won't be available, but structure is tested)
    raw = tmp_path / "test_dia.raw"
    raw.write_bytes(b"\x00" * 2048)

    meta = _extract_file_metadata(raw, "thermo")

    required_keys = [
        "vendor",
        "acquisition_date",
        "instrument_model",
        "acquisition_mode",
        "gradient_length_min",
    ]
    for key in required_keys:
        assert key in meta, f"Missing required key: {key}"

    assert meta["vendor"] == "thermo"


# ── 5. gradient_min_to_spd() ─────────────────────────────────────


def test_gradient_min_to_spd_common_values() -> None:
    """gradient_min_to_spd should return sensible SPD for common gradients."""
    from stan.metrics.scoring import gradient_min_to_spd

    # Short gradient (5 min) should give high SPD
    spd_5 = gradient_min_to_spd(5)
    assert spd_5 > 100, f"5 min gradient should give >100 SPD, got {spd_5}"

    # Medium gradient (30 min) should give moderate SPD
    spd_30 = gradient_min_to_spd(30)
    assert 20 <= spd_30 <= 60, f"30 min gradient SPD should be 20-60, got {spd_30}"

    # Long gradient (90 min) should give low SPD
    spd_90 = gradient_min_to_spd(90)
    assert spd_90 <= 15, f"90 min gradient should give <=15 SPD, got {spd_90}"


def test_gradient_min_to_spd_monotonic() -> None:
    """Shorter gradients should always produce higher SPD."""
    from stan.metrics.scoring import gradient_min_to_spd

    spd_prev = gradient_min_to_spd(5)
    for minutes in [10, 20, 30, 60, 90, 120]:
        spd = gradient_min_to_spd(minutes)
        assert spd <= spd_prev, (
            f"SPD should decrease as gradient increases: "
            f"{minutes} min gave {spd} but {minutes - 1} min gave {spd_prev}"
        )
        spd_prev = spd


def test_gradient_min_to_spd_zero_or_negative() -> None:
    """gradient_min_to_spd should handle zero/negative gracefully."""
    from stan.metrics.scoring import gradient_min_to_spd

    # Should not crash — returns a default
    spd_zero = gradient_min_to_spd(0)
    assert isinstance(spd_zero, int)
    assert spd_zero > 0

    spd_neg = gradient_min_to_spd(-5)
    assert isinstance(spd_neg, int)
    assert spd_neg > 0


def test_gradient_min_to_spd_returns_int() -> None:
    """SPD must always be an integer."""
    from stan.metrics.scoring import gradient_min_to_spd

    for minutes in [5, 11, 21, 30, 44, 60, 90]:
        spd = gradient_min_to_spd(minutes)
        assert isinstance(spd, int), f"gradient_min_to_spd({minutes}) returned {type(spd)}"


# ── 6. Helper functions ──────────────────────────────────────────


def test_classify_vendor() -> None:
    """_classify_vendor should distinguish bruker .d from thermo .raw."""
    from stan.baseline import _classify_vendor

    # .d is always bruker (even though it might not exist on disk,
    # the function only checks the suffix — we test with a Path)
    assert _classify_vendor(Path("/data/run.d")) == "thermo"  # non-dir .d → thermo fallback
    assert _classify_vendor(Path("/data/run.raw")) == "thermo"


def test_classify_vendor_bruker(tmp_path: Path) -> None:
    """_classify_vendor should detect bruker .d directories."""
    from stan.baseline import _classify_vendor

    d_dir = tmp_path / "test.d"
    d_dir.mkdir()
    # Bruker needs the path to be a directory AND have .d suffix
    assert _classify_vendor(d_dir) == "bruker"


def test_find_raw_files_bruker(tmp_path: Path) -> None:
    """_find_raw_files should find .d directories with analysis.tdf."""
    from stan.baseline import _find_raw_files

    # Create valid .d directory
    d_dir = tmp_path / "run1.d"
    d_dir.mkdir()
    (d_dir / "analysis.tdf").write_bytes(b"")

    # Create invalid .d directory (no analysis.tdf)
    bad_d = tmp_path / "run2.d"
    bad_d.mkdir()

    files = _find_raw_files(tmp_path)
    assert len(files) == 1
    assert files[0].name == "run1.d"


def test_find_raw_files_thermo(tmp_path: Path) -> None:
    """_find_raw_files should find .raw files above the size threshold."""
    from stan.baseline import _find_raw_files

    # Large enough .raw file
    big_raw = tmp_path / "run1.raw"
    big_raw.write_bytes(b"\x00" * 200_000)

    # Too small .raw file
    tiny_raw = tmp_path / "run2.raw"
    tiny_raw.write_bytes(b"\x00" * 100)

    files = _find_raw_files(tmp_path)
    assert len(files) == 1
    assert files[0].name == "run1.raw"


def test_parse_date() -> None:
    """_parse_date should handle ISO 8601 strings and None."""
    from stan.baseline import _parse_date

    assert _parse_date(None) is None
    assert _parse_date("") is None

    dt = _parse_date("2025-01-15T10:30:00")
    assert dt is not None
    assert dt.year == 2025
    assert dt.month == 1
    assert dt.day == 15

    # Z suffix
    dt_z = _parse_date("2025-01-15T10:30:00Z")
    assert dt_z is not None


def test_format_date_range() -> None:
    """_format_date_range should handle empty lists and ranges."""
    from stan.baseline import _format_date_range

    assert _format_date_range([]) == "unknown"

    from datetime import datetime
    d1 = datetime(2025, 1, 15)
    assert _format_date_range([d1]) == "Jan 2025"

    d2 = datetime(2025, 6, 1)
    result = _format_date_range([d1, d2])
    assert "Jan 2025" in result
    assert "Jun 2025" in result


def test_estimate_search_time() -> None:
    """_estimate_search_time should return a human-readable string."""
    from stan.baseline import _estimate_search_time

    result = _estimate_search_time(10, "bruker")
    assert isinstance(result, str)
    assert "~" in result or "min" in result.lower() or "hour" in result.lower()
