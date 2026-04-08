"""Tests for Track C dual-mode instrument fingerprint."""

from __future__ import annotations

from stan.community.fingerprint import Fingerprint, compute_fingerprint, _diagnose_fingerprint


def test_compute_fingerprint_balanced():
    """Balanced DDA+DIA runs should produce a balanced fingerprint."""
    dda_run = {
        "id": "dda-1",
        "pct_delta_mass_lt5ppm": 0.95,
        "ms2_scan_rate": 500,
        "median_hyperscore": 32,
        "n_peptides_dda": 14000,
    }
    dia_run = {
        "id": "dia-1",
        "n_precursors": 18000,
        "median_cv_precursor": 6.0,
        "median_fragments_per_precursor": 8.0,
        "pct_fragments_quantified": 0.85,
        "n_peptides": 13000,
    }
    cohort = {
        "pct_delta_mass_lt5ppm": [0.5, 0.7, 0.85, 0.95, 0.98],
        "ms2_scan_rate": [100, 200, 350, 500, 700],
        "median_hyperscore": [20, 25, 30, 32, 38],
        "n_precursors": [5000, 10000, 15000, 18000, 22000],
        "median_cv_precursor": [4.0, 6.0, 8.0, 12.0, 18.0],
        "fragment_sensitivity": [2.0, 4.0, 5.5, 6.8, 8.0],
    }

    fp = compute_fingerprint(dda_run, dia_run, cohort)

    assert 0 <= fp.mass_accuracy <= 100
    assert 0 <= fp.duty_cycle <= 100
    assert 0 <= fp.spectral_quality <= 100
    assert 0 <= fp.precursor_depth <= 100
    assert 0 <= fp.quantitative_cv <= 100
    assert 0 <= fp.fragment_sensitivity <= 100
    assert fp.peptide_recovery_ratio > 0


def test_fingerprint_to_dict():
    """Fingerprint.to_dict() should contain all axes."""
    fp = Fingerprint(
        mass_accuracy=80, duty_cycle=70, spectral_quality=75,
        precursor_depth=85, quantitative_cv=90, fragment_sensitivity=65,
        peptide_recovery_ratio=1.05, diagnosis="Healthy",
    )
    d = fp.to_dict()
    assert "axes" in d
    assert len(d["axes"]) == 6
    assert d["peptide_recovery_ratio"] == 1.05


def test_diagnose_calibration_drift():
    """Low mass accuracy only should diagnose calibration drift."""
    fp = Fingerprint(
        mass_accuracy=15, duty_cycle=70, spectral_quality=75,
        precursor_depth=80, quantitative_cv=85, fragment_sensitivity=75,
    )
    diag = _diagnose_fingerprint(fp)
    assert "recalibrate" in diag.lower() or "calibration" in diag.lower()


def test_diagnose_healthy():
    """All axes above 50 should be diagnosed as healthy."""
    fp = Fingerprint(
        mass_accuracy=75, duty_cycle=70, spectral_quality=80,
        precursor_depth=85, quantitative_cv=80, fragment_sensitivity=75,
        peptide_recovery_ratio=1.0,
    )
    diag = _diagnose_fingerprint(fp)
    assert "balanced" in diag.lower() or "healthy" in diag.lower()


def test_peptide_recovery_ratio():
    """Low recovery ratio should be flagged."""
    dda_run = {
        "id": "d1", "pct_delta_mass_lt5ppm": 0.95, "ms2_scan_rate": 500,
        "median_hyperscore": 32, "n_peptides_dda": 15000,
    }
    dia_run = {
        "id": "d2", "n_precursors": 18000, "median_cv_precursor": 6.0,
        "median_fragments_per_precursor": 8.0, "pct_fragments_quantified": 0.85,
        "n_peptides": 10000,  # ratio = 10000/15000 = 0.67 < 0.75
    }
    fp = compute_fingerprint(dda_run, dia_run, {})
    assert fp.peptide_recovery_ratio < 0.75
