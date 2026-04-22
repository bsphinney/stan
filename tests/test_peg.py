"""Tests for stan.metrics.peg — PEG contamination detection.

Synthetic-spectra tests because we can't check in real .d files. The
algorithm is pure-Python given (m/z, intensity) tuples, so the IO
layer (alphatims for Bruker, fisher_py for Thermo) gets tested
separately when we wire it in.
"""
from __future__ import annotations

import pytest

from stan.metrics.peg import (
    ADDUCTS,
    END_GROUP_MASS,
    PEG_REFERENCE,
    PEG_REPEAT_MASS,
    PROTON_MASS,
    SODIUM_MASS,
    PegIon,
    classify_peg_score,
    compute_peg_score,
    detect_peg_in_spectra,
    generate_peg_reference,
)


# ── Reference table sanity ─────────────────────────────────────────

def test_reference_size_default():
    # v0.2.167: default reference aligned with Rardin 2018 Skyline panel:
    # n=1..20 x {H+, Na+, NH4+} x charge 1 = exactly 60 ions.
    assert len(PEG_REFERENCE) == 60


def test_reference_size_extended():
    """extended=True restores the research superset."""
    from stan.metrics.peg import generate_peg_reference
    ext = generate_peg_reference(extended=True)
    assert 150 <= len(ext) <= 220
    # Extended must include +K adduct and doubly-charged species.
    assert any(i.adduct == "+K" for i in ext)
    assert any(i.charge == 2 for i in ext)


def test_known_peg_marker_ions_present():
    """The five textbook PEG markers must appear in the reference list."""
    expected = {
        459.2800,  # PEG10 [M+Na]+
        503.3062,  # PEG11 [M+H]+
        525.2881,  # PEG11 [M+Na]+
        547.3324,  # PEG12 [M+H]+
        569.3144,  # PEG12 [M+Na]+
    }
    seen_mz = {round(ion.mz, 4) for ion in PEG_REFERENCE}
    missing = expected - seen_mz
    assert not missing, f"missing canonical PEG ions: {missing}"


def test_repeat_unit_44_026():
    """Adjacent oligomers of the same adduct/charge must be 44.026 apart."""
    # Get all [M+Na]+ singly-charged ions, sort by m/z, check spacing
    na_singly = sorted(
        [i.mz for i in PEG_REFERENCE if i.adduct == "+Na" and i.charge == 1]
    )
    diffs = [na_singly[i + 1] - na_singly[i] for i in range(len(na_singly) - 1)]
    for d in diffs:
        assert abs(d - PEG_REPEAT_MASS) < 1e-6, (
            f"adjacent [M+Na]+ ions should be {PEG_REPEAT_MASS} apart, got {d}"
        )


def test_doubly_charged_at_half_singly_spacing():
    """Doubly-charged ions are spaced 22.013 Da (= 44.026 / 2)."""
    na_2 = sorted(
        [i.mz for i in PEG_REFERENCE if i.adduct == "+Na" and i.charge == 2]
    )
    if len(na_2) >= 2:
        diff = na_2[1] - na_2[0]
        assert abs(diff - PEG_REPEAT_MASS / 2) < 1e-6


# ── detect_peg_in_spectra ──────────────────────────────────────────

def _peg_ion(n: int, adduct: str = "+Na") -> float:
    """Compute a known PEG ion m/z (singly charged) for use in tests."""
    m = n * PEG_REPEAT_MASS + END_GROUP_MASS
    if adduct == "+H":
        return m + PROTON_MASS
    if adduct == "+Na":
        return m + SODIUM_MASS
    raise ValueError(adduct)


def test_clean_spectrum_scores_zero():
    """A spectrum with only random non-PEG peaks should score 0."""
    spectra = [[
        (250.123, 1e6),  # random m/z far from any PEG ion
        (380.456, 5e5),
        (612.789, 2e5),
    ]]
    r = detect_peg_in_spectra(spectra)
    assert r.n_ions_detected == 0
    assert r.intensity_pct == 0.0
    assert r.peg_score == 0.0
    assert r.peg_class == "clean"


def test_three_peg_ions_detected():
    spectra = [[
        (_peg_ion(10), 1e5),  # PEG10 [M+Na]+
        (_peg_ion(11), 1e5),  # PEG11 [M+Na]+
        (_peg_ion(12), 1e5),  # PEG12 [M+Na]+
        (380.456, 1e5),       # non-PEG
    ]]
    r = detect_peg_in_spectra(spectra)
    assert r.n_ions_detected == 3
    # 3 PEG ions out of 4 total scanned = 75% intensity_pct
    assert 70 < r.intensity_pct < 80


def test_5_ppm_tolerance_boundary():
    """Peaks at exactly 5 ppm should match; at 6 ppm should not."""
    base = _peg_ion(11)
    just_in = base * (1 + 4.5e-6)   # +4.5 ppm
    just_out = base * (1 + 6.0e-6)  # +6 ppm
    r_in = detect_peg_in_spectra([[(just_in, 1e5)]], tolerance_ppm=5.0)
    r_out = detect_peg_in_spectra([[(just_out, 1e5)]], tolerance_ppm=5.0)
    assert r_in.n_ions_detected == 1
    assert r_out.n_ions_detected == 0


def test_intensity_threshold_filters_noise():
    """Peaks below intensity_threshold should be ignored entirely."""
    spectra = [[(_peg_ion(11), 5e3)]]  # below default 1e4 threshold
    r = detect_peg_in_spectra(spectra)
    assert r.n_ions_detected == 0
    assert r.total_intensity == 0  # noise peak shouldn't count toward TIC either


def test_same_ion_in_multiple_scans_counts_once():
    """Detecting PEG10[Na] across 5 scans = 1 ion detected, not 5."""
    spectra = [[(_peg_ion(10), 1e5)] for _ in range(5)]
    r = detect_peg_in_spectra(spectra)
    assert r.n_ions_detected == 1
    # But intensity adds up across scans
    assert r.intensity_pct == 100.0  # all 5 hits are PEG → 100%


def test_heavy_contamination_classifies_heavy():
    """Many ions + high intensity_pct → score > 70 → heavy."""
    # Use 20 oligomers across +H and +Na — all PEG, no other peaks
    spectra = [[
        (_peg_ion(n, "+Na"), 1e5) for n in range(5, 25)
    ] + [
        (_peg_ion(n, "+H"), 1e5) for n in range(5, 25)
    ]]
    r = detect_peg_in_spectra(spectra)
    assert r.n_ions_detected >= 15  # well above the 15-ion saturation
    assert r.intensity_pct == 100.0
    assert r.peg_class == "heavy"
    assert r.peg_score >= 70


def test_classify_thresholds():
    assert classify_peg_score(0.0) == "clean"
    assert classify_peg_score(19.9) == "clean"
    assert classify_peg_score(20.0) == "trace"
    assert classify_peg_score(49.9) == "trace"
    assert classify_peg_score(50.0) == "moderate"
    assert classify_peg_score(69.9) == "moderate"
    assert classify_peg_score(70.0) == "heavy"
    assert classify_peg_score(100.0) == "heavy"


def test_score_monotonic_in_n_ions_and_intensity():
    """More PEG ions OR higher intensity_pct → higher score."""
    base = compute_peg_score(5, 5.0)
    more_ions = compute_peg_score(10, 5.0)
    more_int = compute_peg_score(5, 10.0)
    assert more_ions > base
    assert more_int > base


def test_empty_spectra_returns_clean_zero():
    r = detect_peg_in_spectra([])
    assert r.n_ions_detected == 0
    assert r.peg_score == 0.0
    assert r.peg_class == "clean"


def test_custom_reference_list():
    """Caller can pass a narrowed reference (e.g., only +Na adducts)."""
    na_only = generate_peg_reference()
    na_only = [i for i in na_only if i.adduct == "+Na"]
    spectra = [[
        (_peg_ion(11, "+H"), 1e5),  # +H — not in na_only ref
        (_peg_ion(11, "+Na"), 1e5),  # +Na — should match
    ]]
    r = detect_peg_in_spectra(spectra, reference=na_only)
    assert r.n_ions_detected == 1
