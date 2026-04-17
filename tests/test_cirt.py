"""Tests for stan.metrics.cirt — panel selection + per-run extraction."""
from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from stan.metrics.cirt import (
    EMPIRICAL_CIRT_PANELS,
    derive_panel_from_cohort,
    extract_anchor_rts,
    get_panel,
)


def _write_report(
    path: Path,
    rows: list[tuple[str, float, float]],  # (sequence, rt, q_value)
) -> Path:
    df = pl.DataFrame(
        {
            "Stripped.Sequence": [r[0] for r in rows],
            "RT": [r[1] for r in rows],
            "Q.Value": [r[2] for r in rows],
        }
    )
    df.write_parquet(str(path))
    return path


def test_seeded_panels_are_sane():
    """Every seeded panel has unique, tryptic-looking, RT-sorted entries."""
    for key, panel in EMPIRICAL_CIRT_PANELS.items():
        family, spd = key
        assert panel, f"empty panel for {key}"
        seqs = [s for s, _ in panel]
        assert len(set(seqs)) == len(seqs), f"duplicate peptide in {key}"
        rts = [rt for _, rt in panel]
        assert rts == sorted(rts), f"panel for {key} not RT-sorted"
        for seq, rt in panel:
            assert seq.endswith(("K", "R")), f"{key}: {seq} not tryptic"
            assert 9 <= len(seq) <= 18, f"{key}: {seq} length out of range"
            assert rt > 0, f"{key}: {seq} has non-positive reference RT"


def test_get_panel_returns_copy():
    """get_panel must not leak the internal dict — mutation must not corrupt."""
    p1 = get_panel("timsTOF", 100)
    assert p1, "timsTOF/100 panel should be seeded"
    p1.append(("FAKE_PEPTIDE", 999.0))
    p2 = get_panel("timsTOF", 100)
    assert ("FAKE_PEPTIDE", 999.0) not in p2


def test_get_panel_missing_returns_empty():
    assert get_panel("Astral", 60) == []
    assert get_panel(None, 60) == []
    assert get_panel("timsTOF", None) == []


def test_extract_anchor_rts_basic(tmp_path):
    panel = [("PEPTIDEAK", 5.0), ("PEPTIDEBR", 10.0), ("PEPTIDECK", 15.0)]
    rows = [
        ("PEPTIDEAK", 5.1, 0.001),   # hit
        ("PEPTIDEBR", 10.2, 0.002),  # hit
        ("PEPTIDECK", 15.5, 0.05),   # above FDR — should be filtered
        ("OTHERPEP",  7.0, 0.001),   # not in panel — should be ignored
    ]
    report = _write_report(tmp_path / "report.parquet", rows)
    observed = extract_anchor_rts(report, panel)
    assert observed == {"PEPTIDEAK": 5.1, "PEPTIDEBR": 10.2}


def test_extract_anchor_rts_empty_panel(tmp_path):
    report = _write_report(
        tmp_path / "r.parquet",
        [("PEPTIDEAK", 5.0, 0.001)],
    )
    assert extract_anchor_rts(report, []) == {}


def test_extract_anchor_rts_missing_file(tmp_path):
    panel = [("PEPTIDEAK", 5.0)]
    missing = tmp_path / "does_not_exist.parquet"
    # Must not raise — empty dict is the contract
    assert extract_anchor_rts(missing, panel) == {}


def test_extract_anchor_rts_collapses_charges(tmp_path):
    """Same peptide at two charges → returned as one median RT."""
    panel = [("PEPTIDEAK", 5.0)]
    rows = [
        ("PEPTIDEAK", 5.0, 0.001),  # charge +2
        ("PEPTIDEAK", 5.4, 0.001),  # charge +3
    ]
    report = _write_report(tmp_path / "r.parquet", rows)
    observed = extract_anchor_rts(report, panel)
    # Median of 5.0, 5.4 = 5.2
    assert observed["PEPTIDEAK"] == pytest.approx(5.2)


def test_derive_panel_from_cohort(tmp_path):
    """A small synthetic cohort should yield tryptic peptides only."""
    # 5 runs, 3 stable peptides + 1 noisy one + 1 non-tryptic
    reports = []
    for i, drift in enumerate([0.0, 0.05, -0.03, 0.02, -0.01]):
        rows = [
            ("STABLEAAAAR",   5.0 + drift, 0.001),
            ("STABLEBBBBK",  15.0 + drift, 0.001),
            ("STABLECCCCK",  25.0 + drift, 0.001),
            ("NOISYDDDDK",   10.0 + drift * 20, 0.001),  # huge CV
            ("NONTRYPTICAA",  8.0 + drift, 0.001),       # doesn't end in K/R
        ]
        reports.append(_write_report(tmp_path / f"r{i}.parquet", rows))

    panel = derive_panel_from_cohort(reports, n_anchors=5, max_cv_pct=3.0)
    seqs = {s for s, _ in panel}
    assert "STABLEAAAAR" in seqs
    assert "STABLEBBBBK" in seqs
    assert "STABLECCCCK" in seqs
    assert "NOISYDDDDK" not in seqs, "high-CV peptide should be rejected"
    assert "NONTRYPTICAA" not in seqs, "non-tryptic should be rejected"


def test_derive_panel_empty_cohort():
    assert derive_panel_from_cohort([]) == []


def test_derive_panel_respects_min_presence(tmp_path):
    """A peptide missing from too many runs must be rejected."""
    reports = []
    for i in range(5):
        rows = [("ALWAYSHEREK", 5.0, 0.001)]
        if i < 2:
            rows.append(("RAREPEPR", 10.0, 0.001))
        reports.append(_write_report(tmp_path / f"r{i}.parquet", rows))

    panel = derive_panel_from_cohort(reports, n_anchors=5, min_presence=0.9)
    seqs = {s for s, _ in panel}
    assert "ALWAYSHEREK" in seqs
    assert "RAREPEPR" not in seqs, "peptide in 2/5 runs should be rejected at 0.9"
