"""Tests for stan.metrics.chromatography.compute_irt_deviation.

Covers the v0.2.125 rewrite that switched the default reference panel
from the dead Biognosys iRT kit to the empirical per-(family, spd)
panel in stan.metrics.cirt.EMPIRICAL_CIRT_PANELS. Regression-guards
the three key behaviors:

1. Known (family, spd) with detected anchors → numeric max + median
2. Known (family, spd) with fewer than min_peptides anchors → None
   (NOT 0 — that was the bug)
3. Unknown (family, spd) with no seeded panel → None
"""
from __future__ import annotations

from pathlib import Path

import polars as pl

from stan.metrics.chromatography import (
    DEFAULT_IRT_LIBRARY,
    compute_irt_deviation,
)
from stan.metrics.cirt import EMPIRICAL_CIRT_PANELS


def _write_report(
    path: Path,
    rows: list[tuple[str, float, float]],  # (sequence, rt_min, q_value)
) -> Path:
    pl.DataFrame({
        "Stripped.Sequence": [r[0] for r in rows],
        "RT": [r[1] for r in rows],
        "Q.Value": [r[2] for r in rows],
    }).write_parquet(str(path))
    return path


def test_uses_empirical_panel_for_known_family_spd(tmp_path):
    # Use the real seeded timsTOF SPD=100 panel so we're exercising
    # the same data path production uses, not a mock.
    panel = EMPIRICAL_CIRT_PANELS[("timsTOF", 100)]
    # Inject small deviations (50 ms, 200 ms, etc.) from each reference RT
    drifts = [0.01, 0.03, -0.02, 0.05, -0.01, 0.04, 0.02, -0.03, 0.01, 0.0]
    rows = [
        (seq, ref + drifts[i % len(drifts)], 0.001)
        for i, (seq, ref) in enumerate(panel)
    ]
    report = _write_report(tmp_path / "r.parquet", rows)

    result = compute_irt_deviation(report, instrument_family="timsTOF", spd=100)
    assert result["n_irt_found"] == len(panel)
    assert result["max_deviation_min"] is not None
    # Max abs drift in test data is 0.05 min
    assert abs(result["max_deviation_min"] - 0.05) < 1e-9
    assert result["median_deviation_min"] is not None


def test_below_min_peptides_returns_none(tmp_path):
    # Only 2 panel peptides detected — must NOT report 0.0 as deviation
    panel = EMPIRICAL_CIRT_PANELS[("timsTOF", 100)]
    rows = [
        (panel[0][0], panel[0][1] + 0.01, 0.001),
        (panel[1][0], panel[1][1] + 0.02, 0.001),
        ("UNRELATEDPEPTIDER", 5.0, 0.001),
    ]
    report = _write_report(tmp_path / "r.parquet", rows)

    result = compute_irt_deviation(report, instrument_family="timsTOF", spd=100)
    assert result["max_deviation_min"] is None, (
        "Must return None (not 0) when <3 anchors match, so GRS can skip"
    )
    assert result["median_deviation_min"] is None
    assert result["n_irt_found"] == 2


def test_unknown_family_spd_returns_none(tmp_path):
    rows = [("SOMEPEPTIDER", 5.0, 0.001)]
    report = _write_report(tmp_path / "r.parquet", rows)

    # No seeded panel for (Astral, 999)
    result = compute_irt_deviation(report, instrument_family="Astral", spd=999)
    assert result["max_deviation_min"] is None
    assert result["median_deviation_min"] is None
    assert result["n_irt_found"] == 0


def test_explicit_reference_rts_override(tmp_path):
    # Caller supplies a custom reference (e.g. a spiked standard)
    reference = {"PEPTIDEAK": 5.0, "PEPTIDEBR": 10.0, "PEPTIDECK": 15.0}
    rows = [
        ("PEPTIDEAK", 5.1, 0.001),
        ("PEPTIDEBR", 10.05, 0.001),
        ("PEPTIDECK", 14.9, 0.001),
    ]
    report = _write_report(tmp_path / "r.parquet", rows)

    result = compute_irt_deviation(report, reference_rts=reference)
    assert result["n_irt_found"] == 3
    assert result["max_deviation_min"] is not None
    # Max deviation ≈ 0.1 (PEPTIDEAK and PEPTIDECK tied)
    assert abs(result["max_deviation_min"] - 0.1) < 1e-9


def test_biognosys_library_still_exposed():
    # Deprecated as a default but still importable for callers that
    # actually spike iRT standards. Shape-check only.
    assert "LGGNEQVTR" in DEFAULT_IRT_LIBRARY
    assert DEFAULT_IRT_LIBRARY["LGGNEQVTR"] == 0.0
    assert DEFAULT_IRT_LIBRARY["LFLQFGAQGSPFLK"] == 100.0
