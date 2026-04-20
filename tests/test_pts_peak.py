"""Tests for _compute_pts_peak_bruker — the Bruker points-across-peak
algorithm documented in docs/SPEC_pts_per_peak.md.

Guards against regressions like v0.2.105–v0.2.128 where pts/peak could
silently fall through to the broken fallback formula (peak_width / dt),
which produces 10–30× too-high values.

Validated against the real 03jun2024_HeLa50ng_DIA_100spd_S1-B2_1_6205.d
file on Hive on 2026-04-20: returns 8.0, matching the spec's validated
value exactly. The synthetic tests below exercise the algorithm's
logic without needing that ~30 MB fixture checked in.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import polars as pl
import pytest


def _build_fake_tdf(
    tdf_path: Path,
    dia_frames: list[tuple[int, float, int]],  # (frame_id, time_sec, window_group)
    windows: list[tuple[int, float, float]],   # (window_group, isolation_mz, isolation_width)
    non_dia_frames: list[tuple[int, float]] | None = None,  # MS1/other frames
) -> None:
    """Build a minimal analysis.tdf containing just the tables the
    pts/peak algorithm reads. Mirrors Bruker TDF's real column types
    so the queries in extractor.py work unchanged.
    """
    con = sqlite3.connect(str(tdf_path))
    con.execute("""
        CREATE TABLE Frames (
            Id INTEGER PRIMARY KEY,
            Time REAL NOT NULL,
            MsMsType INTEGER NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE DiaFrameMsMsInfo (
            Frame INTEGER NOT NULL,
            WindowGroup INTEGER NOT NULL,
            PRIMARY KEY (Frame, WindowGroup)
        )
    """)
    con.execute("""
        CREATE TABLE DiaFrameMsMsWindows (
            WindowGroup INTEGER NOT NULL,
            IsolationMz REAL NOT NULL,
            IsolationWidth REAL NOT NULL
        )
    """)
    for fid, t, wg in dia_frames:
        con.execute("INSERT INTO Frames (Id, Time, MsMsType) VALUES (?, ?, 9)", (fid, t))
        con.execute("INSERT INTO DiaFrameMsMsInfo (Frame, WindowGroup) VALUES (?, ?)", (fid, wg))
    for wg, iso_mz, iso_width in windows:
        con.execute(
            "INSERT INTO DiaFrameMsMsWindows (WindowGroup, IsolationMz, IsolationWidth) VALUES (?, ?, ?)",
            (wg, iso_mz, iso_width),
        )
    for fid, t in (non_dia_frames or []):
        con.execute("INSERT INTO Frames (Id, Time, MsMsType) VALUES (?, ?, 0)", (fid, t))
    con.commit()
    con.close()


def _make_report(
    rows: list[tuple[float, float, float, float]],  # (mz, rt_start_min, rt_stop_min, q_value)
) -> pl.DataFrame:
    return pl.DataFrame({
        "Precursor.Mz": [r[0] for r in rows],
        "RT.Start":     [r[1] for r in rows],
        "RT.Stop":      [r[2] for r in rows],
        "Q.Value":      [r[3] for r in rows],
        # stub columns the rest of extractor expects but _compute_pts_peak_bruker doesn't use
        "RT":           [(r[1] + r[2]) / 2 for r in rows],
    })


def test_returns_none_when_tdf_missing(tmp_path):
    from stan.metrics.extractor import _compute_pts_peak_bruker
    # A .d directory that has no analysis.tdf — extractor.py must
    # return None cleanly instead of crashing.
    d = tmp_path / "missing.d"
    d.mkdir()
    report = _make_report([(500.0, 1.0, 1.1, 0.001)])
    assert _compute_pts_peak_bruker(d, report) is None


def test_returns_none_when_no_dia_frames(tmp_path):
    from stan.metrics.extractor import _compute_pts_peak_bruker
    d = tmp_path / "no_dia.d"
    d.mkdir()
    # Only MS1 frames, no DIA (MsMsType != 9)
    _build_fake_tdf(
        d / "analysis.tdf",
        dia_frames=[],
        windows=[(1, 500.0, 25.0)],
        non_dia_frames=[(1, 0.1), (2, 0.2)],
    )
    report = _make_report([(500.0, 1.0, 1.1, 0.001)])
    assert _compute_pts_peak_bruker(d, report) is None


def test_counts_only_frames_covering_mz(tmp_path):
    """Precursor at 500 Da should only match windows that cover m/z=500."""
    from stan.metrics.extractor import _compute_pts_peak_bruker
    d = tmp_path / "mz_cover.d"
    d.mkdir()
    # 10 DIA frames @ 100 ms apart, alternating WG 1 and WG 2.
    # WG 1 = isolation m/z 500 ± 12.5  (covers 500)
    # WG 2 = isolation m/z 700 ± 12.5  (does NOT cover 500)
    dia = [(i + 1, (i + 1) * 0.1, (i % 2) + 1) for i in range(10)]
    windows = [(1, 500.0, 25.0), (2, 700.0, 25.0)]
    _build_fake_tdf(d / "analysis.tdf", dia_frames=dia, windows=windows)
    # Precursor at 500 Da, RT window spans ALL 10 frames (0.1 s to 1.0 s)
    # In minutes: 0.001 min to 0.017 min — but RT.Start * 60 = 0.06 s,
    # so widen to ensure we cover all frames: 0.0 to 0.5 min (= 30 s)
    report = _make_report([(500.0, 0.0, 0.5, 0.001)])
    # Should see 5 matching frames (every other frame)
    result = _compute_pts_peak_bruker(d, report)
    assert result == 5.0, f"expected 5 (one per WG-1 frame), got {result}"


def test_counts_only_frames_in_rt_window(tmp_path):
    """Precursor RT window [0.05, 0.10 min] = [3, 6 s] should only
    count frames with time in [3, 6] seconds."""
    from stan.metrics.extractor import _compute_pts_peak_bruker
    d = tmp_path / "rt_window.d"
    d.mkdir()
    # 20 DIA frames at 500 ms apart, all WG 1 (covers m/z 500)
    dia = [(i + 1, (i + 1) * 0.5, 1) for i in range(20)]  # times: 0.5, 1.0, ... 10.0 sec
    windows = [(1, 500.0, 25.0)]
    _build_fake_tdf(d / "analysis.tdf", dia_frames=dia, windows=windows)
    # RT window 3 s to 6 s (in minutes: 0.05 to 0.10) — frames at 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.0
    # That's 7 frames covering both endpoints (bisect_right is inclusive)
    report = _make_report([(500.0, 3.0 / 60, 6.0 / 60, 0.001)])
    result = _compute_pts_peak_bruker(d, report)
    assert result == 7.0, f"expected 7 frames in [3,6]s, got {result}"


def test_spec_like_example_produces_plausible_count(tmp_path):
    """Recreate the spec's Bruker-specific table scenario:
    - 11 WGs, 1 window each, isolation 25 Da
    - 11 DIA frames per cycle at 100 ms cadence → 1.1 s cycle
    - Precursor with 3.5 s peak width → expect ~3 matching cycles"""
    from stan.metrics.extractor import _compute_pts_peak_bruker
    d = tmp_path / "spec.d"
    d.mkdir()
    # 11 WGs tiling m/z 400-675 with 25 Da windows
    windows = [(wg + 1, 400.0 + 25.0 * wg + 12.5, 25.0) for wg in range(11)]
    # 30 seconds of acquisition at 100 ms frame cadence, cycling WG 1..11
    dia = []
    for i in range(300):
        t = (i + 1) * 0.1
        wg = (i % 11) + 1
        dia.append((i + 1, t, wg))
    _build_fake_tdf(d / "analysis.tdf", dia_frames=dia, windows=windows)
    # Precursor at m/z 412.5 (falls in WG 1 = 400-425)
    # RT window 10 s (~0.167 min) to 13.5 s (~0.225 min) = 3.5 s peak
    # Expect ~3 matching cycles (1 WG=1 hit per cycle, 3 cycles in 3.5s @ 1.1s cycle)
    report = _make_report([(412.5, 10.0 / 60, 13.5 / 60, 0.001)])
    result = _compute_pts_peak_bruker(d, report)
    # Plausible range 2-5 given cycle phase; must NOT be the broken
    # fallback-formula result (10× higher = ~35+).
    assert result is not None, "algorithm returned None on valid input"
    assert 2 <= result <= 5, (
        f"expected ~3 pts/peak, got {result}. Anything >10 means we're "
        "counting each frame rather than distinct-WG-covering-frames "
        "(the broken fallback formula behavior the spec warns against)."
    )


def test_multiple_wg_per_frame_counted_once(tmp_path):
    """Defensive regression test for the dedup fix:
    if DiaFrameMsMsInfo has multiple rows per frame (one per WG), a
    frame that has two WGs each covering the precursor m/z must still
    only count +1, not +2. Pre-dedup code would have counted twice."""
    from stan.metrics.extractor import _compute_pts_peak_bruker
    d = tmp_path / "dup.d"
    d.mkdir()
    # Build a TDF where frame 1 has BOTH WG=1 and WG=2 rows (bypass the
    # PK constraint in _build_fake_tdf by inserting manually).
    tdf = d / "analysis.tdf"
    con = sqlite3.connect(str(tdf))
    con.execute("CREATE TABLE Frames (Id INTEGER PRIMARY KEY, Time REAL NOT NULL, MsMsType INTEGER NOT NULL)")
    con.execute("CREATE TABLE DiaFrameMsMsInfo (Frame INTEGER NOT NULL, WindowGroup INTEGER NOT NULL)")
    con.execute("CREATE TABLE DiaFrameMsMsWindows (WindowGroup INTEGER NOT NULL, IsolationMz REAL NOT NULL, IsolationWidth REAL NOT NULL)")
    # One frame, two WG rows, both covering m/z 500
    con.execute("INSERT INTO Frames VALUES (1, 1.0, 9)")
    con.execute("INSERT INTO DiaFrameMsMsInfo VALUES (1, 1)")
    con.execute("INSERT INTO DiaFrameMsMsInfo VALUES (1, 2)")
    con.execute("INSERT INTO DiaFrameMsMsWindows VALUES (1, 500.0, 25.0)")
    con.execute("INSERT INTO DiaFrameMsMsWindows VALUES (2, 500.0, 25.0)")
    con.commit()
    con.close()

    report = _make_report([(500.0, 0.0, 0.5, 0.001)])
    result = _compute_pts_peak_bruker(d, report)
    assert result == 1.0, (
        f"expected 1 (one frame, counted once despite 2 WG rows), got {result}. "
        "Pre-v0.2.129 code would return 2 because the JOIN duplicated the frame "
        "time and the counting loop incremented per row."
    )
