"""Tests for QC gating evaluation and HOLD flag writing."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from stan.gating.evaluator import GateResult, evaluate_gates
from stan.gating.queue import clear_hold_flag, write_hold_flag


# ── Evaluator ─────────────────────────────────────────────────────────

MOCK_THRESHOLDS = {
    "default": {
        "dia": {
            "n_precursors_min": 5000,
            "median_cv_precursor_max": 20.0,
            "grs_score_min": 50,
        },
        "dda": {
            "n_psms_min": 10000,
        },
    },
}


@patch("stan.gating.evaluator.load_thresholds", return_value=MOCK_THRESHOLDS)
def test_evaluate_pass(mock_load):
    """Metrics above all thresholds should pass."""
    metrics = {"n_precursors": 15000, "median_cv_precursor": 8.0, "grs_score": 85}
    decision = evaluate_gates(metrics, "default", "dia")
    assert decision.result == GateResult.PASS
    assert len(decision.failed_gates) == 0


@patch("stan.gating.evaluator.load_thresholds", return_value=MOCK_THRESHOLDS)
def test_evaluate_fail(mock_load):
    """Metrics below minimums should fail."""
    metrics = {"n_precursors": 2000, "median_cv_precursor": 8.0, "grs_score": 85}
    decision = evaluate_gates(metrics, "default", "dia")
    assert decision.result == GateResult.FAIL
    assert "n_precursors" in decision.failed_gates


@patch("stan.gating.evaluator.load_thresholds", return_value=MOCK_THRESHOLDS)
def test_evaluate_fail_cv_too_high(mock_load):
    """CV above maximum should fail."""
    metrics = {"n_precursors": 15000, "median_cv_precursor": 25.0, "grs_score": 85}
    decision = evaluate_gates(metrics, "default", "dia")
    assert decision.result == GateResult.FAIL
    assert "median_cv_precursor" in decision.failed_gates


@patch("stan.gating.evaluator.load_thresholds", return_value=MOCK_THRESHOLDS)
def test_evaluate_dda(mock_load):
    """DDA metrics should evaluate against DDA thresholds."""
    metrics = {"n_psms": 50000}
    decision = evaluate_gates(metrics, "default", "dda")
    assert decision.result == GateResult.PASS


@patch("stan.gating.evaluator.load_thresholds", return_value=MOCK_THRESHOLDS)
def test_evaluate_diagnosis_generated(mock_load):
    """Failed gates should produce a diagnosis string."""
    metrics = {"n_precursors": 2000, "median_cv_precursor": 8.0, "grs_score": 30}
    decision = evaluate_gates(metrics, "default", "dia")
    assert decision.result == GateResult.FAIL
    assert len(decision.diagnosis) > 0


# ── HOLD Flag ─────────────────────────────────────────────────────────

@patch("stan.gating.evaluator.load_thresholds", return_value=MOCK_THRESHOLDS)
def test_write_hold_flag_on_fail(mock_load, tmp_path: Path):
    """HOLD flag should be written when gate result is FAIL."""
    metrics = {"n_precursors": 1000, "median_cv_precursor": 8.0, "grs_score": 85}
    decision = evaluate_gates(metrics, "default", "dia")
    assert decision.result == GateResult.FAIL

    flag_path = write_hold_flag(tmp_path, decision, "test_run")
    assert flag_path is not None
    assert flag_path.exists()
    content = flag_path.read_text()
    assert "STAN QC HOLD" in content
    assert "test_run" in content


@patch("stan.gating.evaluator.load_thresholds", return_value=MOCK_THRESHOLDS)
def test_no_hold_flag_on_pass(mock_load, tmp_path: Path):
    """No HOLD flag should be written when gate result is PASS."""
    metrics = {"n_precursors": 15000, "median_cv_precursor": 8.0, "grs_score": 85}
    decision = evaluate_gates(metrics, "default", "dia")
    assert decision.result == GateResult.PASS

    flag_path = write_hold_flag(tmp_path, decision, "test_run")
    assert flag_path is None


def test_clear_hold_flag(tmp_path: Path):
    """clear_hold_flag should remove existing flag."""
    flag = tmp_path / "HOLD_test.txt"
    flag.write_text("HOLD")

    assert clear_hold_flag(tmp_path, "test") is True
    assert not flag.exists()


def test_clear_nonexistent_flag(tmp_path: Path):
    """clear_hold_flag should return False for nonexistent flag."""
    assert clear_hold_flag(tmp_path, "nonexistent") is False
