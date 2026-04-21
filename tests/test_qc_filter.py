"""Tests for stan.watcher.qc_filter.DEFAULT_QC_PATTERN.

Locks in the matrix of filenames the lab actually uses so a regex
change can't silently drop a QC variant. Anything that should be
classified as a QC standard goes in MUST_MATCH; anything that's
been a real false-positive in production goes in MUST_NOT_MATCH.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from stan.watcher.qc_filter import compile_qc_pattern, is_qc_file


# Filenames the operators in the lab use for HeLa QC standards.
MUST_MATCH = [
    # Spelled out — original behavior
    "HeLa50ng_test",
    "Hela_QC_run",
    "HELA50_S1-A1",
    "HeL50-Dia_60spd_S4-E2_1_21013",
    "HeL5_quick",
    # Lab abbreviation added 2026-04-21
    "HE50_S1-A1",
    "He50_test_run",
    "HE5_quick",
    "HE125ng_S2-B2",
    # Token-anywhere QC
    "QC_daily_check",
    "myrun_QC_2026",
    "qcRun01",
    # Standard prefix variants
    "Std_He_baseline",
    "STD-HE-pre-run",
    "std_hela_check",
    # Real timsTOF filenames Brett showed earlier in this session
    "9apr26_HeL50-11x3k07to13Ra85_100spd_S4-E6_1_20875",
    "Ex300423_HeL50_26apr1-60m_1good",
    "8apr26_HeL2or400Dda-forLowerDiaWins-tf1ba_30spd_S4-E2_1_20835",
]

# Filenames that are NOT QC standards. These are the kinds of names
# that were causing false positives or that we explicitly want to
# stay out of the QC pipeline.
MUST_NOT_MATCH = [
    # Common English words starting with "he"
    "head_sample_01",
    "heart_extract_2026",
    "heat_shock_run",
    "helper_protein_test",
    # Real customer samples we've seen on the timsTOF
    "14April2026_DIA_60spd_SyFl_1_S6-E1_1_20994",
    "8aprDEL-chekAfiTipClog-mousamon50-tip6_S1-A12_1_20850",
    # Should NOT match — "spentHeLtip" has "hel" but not "hela" or "hel\d"
    "8aprDEL-chekAfiTipClog-spentHeLtip6_S1-A6_1_20843",
    # Blank / wash variants — these are excluded by a separate filter,
    # but the QC pattern itself should also not match them
    "blank_S1-H1",
    "wash_run_2026",
    # Pure customer names
    "Brett_research_sample_01",
    "patient_42_extract",
]


@pytest.mark.parametrize("name", MUST_MATCH)
def test_default_pattern_matches_qc_filenames(name):
    pat = compile_qc_pattern()
    assert pat.search(name), (
        f"QC filename {name!r} did not match the default pattern. "
        "If this is intentional (you renamed the lab convention), "
        "remove it from MUST_MATCH; otherwise widen DEFAULT_QC_PATTERN."
    )


@pytest.mark.parametrize("name", MUST_NOT_MATCH)
def test_default_pattern_skips_non_qc_filenames(name):
    pat = compile_qc_pattern()
    assert not pat.search(name), (
        f"Non-QC filename {name!r} matched the default pattern. "
        "Tighten DEFAULT_QC_PATTERN to avoid sending customer samples "
        "through the QC search pipeline."
    )


def test_is_qc_file_strips_extension():
    """is_qc_file should match against the stem, not the full path."""
    pat = compile_qc_pattern()
    assert is_qc_file(Path("/data/HE50_test.d"), pat)
    assert is_qc_file(Path("/data/HeLa50ng_run.raw"), pat)
    assert not is_qc_file(Path("/data/customer_sample.d"), pat)
