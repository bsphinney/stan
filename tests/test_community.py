"""Tests for community benchmark validation and scoring."""

from __future__ import annotations

from stan.community.validate import validate_submission


def test_validate_dia_pass():
    """Valid DIA metrics should pass validation."""
    metrics = {
        "n_precursors": 15000,
        "n_peptides": 10000,
        "n_proteins": 3000,
        "median_cv_precursor": 8.0,
        "pct_charge_1": 0.05,
        "missed_cleavage_rate": 0.10,
    }
    result = validate_submission(metrics, "dia")
    assert result.is_valid is True
    assert len(result.rejected_gates) == 0


def test_validate_dia_fail_low_precursors():
    """DIA with precursors below hard gate should be rejected."""
    metrics = {
        "n_precursors": 500,  # below 5000 minimum
        "n_peptides": 300,
        "n_proteins": 100,
        "median_cv_precursor": 8.0,
        "pct_charge_1": 0.05,
        "missed_cleavage_rate": 0.10,
    }
    result = validate_submission(metrics, "dia")
    assert result.is_valid is False
    assert any("n_precursors" in g for g in result.rejected_gates)


def test_validate_dia_high_cv_flagged_not_rejected():
    """High CV is now a soft flag, not a hard rejection. CV moved out
    of HARD_GATES because cluster re-searches produce single-run
    submissions with no inter-replicate CV available."""
    metrics = {
        "n_precursors": 15000,
        "n_peptides": 10000,
        "n_proteins": 3000,
        "median_cv_precursor": 65.0,  # above 60.0 — flagged, not rejected
        "pct_charge_1": 0.05,
        "missed_cleavage_rate": 0.10,
    }
    result = validate_submission(metrics, "dia")
    assert result.is_valid is True
    assert any("median_cv_precursor" in f for f in result.flags)


def test_validate_dia_missing_cv_accepted():
    """Cluster re-searches produce CV=None — must not reject."""
    metrics = {
        "n_precursors": 15000,
        "n_peptides": 10000,
        "n_proteins": 3000,
        "median_cv_precursor": None,
        "pct_charge_1": 0.05,
        "missed_cleavage_rate": 0.10,
    }
    result = validate_submission(metrics, "dia")
    assert result.is_valid is True


def test_validate_dda_pass():
    """Valid DDA metrics should pass validation."""
    metrics = {
        "n_psms": 30000,
        "n_peptides_dda": 12000,
        "pct_delta_mass_lt5ppm": 0.95,
        "ms2_scan_rate": 400,
    }
    result = validate_submission(metrics, "dda")
    assert result.is_valid is True


def test_validate_dda_fail():
    """DDA with PSMs below hard gate should be rejected."""
    metrics = {
        "n_psms": 3000,  # below 5000 minimum
        "n_peptides_dda": 2000,  # below 3000 minimum
        "pct_delta_mass_lt5ppm": 0.95,
        "ms2_scan_rate": 400,
    }
    result = validate_submission(metrics, "dda")
    assert result.is_valid is False


def test_validate_dia_skips_dda_gates():
    """DIA validation should not apply DDA-specific gates."""
    metrics = {
        "n_precursors": 15000,
        "n_peptides": 10000,
        "n_proteins": 3000,
        "median_cv_precursor": 8.0,
        "pct_charge_1": 0.05,
        "missed_cleavage_rate": 0.10,
        "n_psms": 0,  # would fail DDA gate but should be skipped
    }
    result = validate_submission(metrics, "dia")
    assert result.is_valid is True


def test_validate_soft_flag():
    """Unusually high precursor count should be flagged but not rejected."""
    metrics = {
        "n_precursors": 55000,  # above soft flag of 50000
        "n_peptides": 40000,
        "n_proteins": 8000,
        "median_cv_precursor": 8.0,
        "pct_charge_1": 0.05,
        "missed_cleavage_rate": 0.10,
    }
    result = validate_submission(metrics, "dia")
    assert result.is_valid is True
    assert len(result.flags) > 0
