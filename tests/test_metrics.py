"""Tests for metrics extraction, GRS scoring, and community scoring."""

from __future__ import annotations

from stan.metrics.chromatography import compute_grs
from stan.metrics.scoring import (
    amount_bucket,
    compute_cohort_id,
    compute_dia_score,
    compute_dda_score,
    gradient_min_to_spd,
    spd_bucket,
    throughput_bucket,
)


# ── GRS Score ─────────────────────────────────────────────────────────

def test_grs_perfect():
    """Perfect TIC data should produce a high GRS score."""
    tic_data = {
        "shape_correlation": 0.98,
        "tic_auc": 1000.0,
        "tic_auc_reference": 1000.0,
        "peak_rt_min": 30.0,
        "peak_rt_reference": 30.0,
        "carryover_ratio": 0.01,
    }
    score = compute_grs(tic_data)
    assert 90 <= score <= 100


def test_grs_poor():
    """Poor TIC data should produce a low GRS score."""
    tic_data = {
        "shape_correlation": 0.3,
        "tic_auc": 200.0,
        "tic_auc_reference": 1000.0,
        "peak_rt_min": 45.0,
        "peak_rt_reference": 30.0,
        "carryover_ratio": 0.8,
    }
    score = compute_grs(tic_data)
    assert score < 50


def test_grs_range():
    """GRS should always be 0–100."""
    for shape in [0.0, 0.5, 1.0]:
        for carry in [0.0, 0.5, 1.0]:
            score = compute_grs({
                "shape_correlation": shape,
                "tic_auc": 500, "tic_auc_reference": 1000,
                "peak_rt_min": 30, "peak_rt_reference": 30,
                "carryover_ratio": carry,
            })
            assert 0 <= score <= 100


# ── Cohort Bucketing ──────────────────────────────────────────────────

def test_spd_bucket():
    # Evosep standard methods
    assert spd_bucket(500) == "200+spd"
    assert spd_bucket(300) == "200+spd"
    assert spd_bucket(200) == "200+spd"
    assert spd_bucket(100) == "100spd"
    assert spd_bucket(60) == "60spd"
    assert spd_bucket(40) == "60spd"  # Whisper 40 SPD → same bucket as 60
    assert spd_bucket(30) == "30spd"
    assert spd_bucket(15) == "15spd"  # Evosep Extended
    assert spd_bucket(5) == "deep"  # traditional 2h+


def test_throughput_bucket_spd_preferred():
    """SPD takes priority over gradient_min when both are provided."""
    assert throughput_bucket(spd=60) == "60spd"
    assert throughput_bucket(spd=60, gradient_min=88) == "60spd"


def test_throughput_bucket_gradient_fallback():
    """Falls back to gradient_min → estimated SPD when spd is None."""
    # 21 min gradient → ~55 SPD → 60spd bucket
    assert throughput_bucket(gradient_min=21) == "60spd"
    # 44 min gradient → ~26 SPD → 30spd bucket
    assert throughput_bucket(gradient_min=44) == "30spd"
    # 88 min gradient → ~13 SPD → 15spd bucket
    assert throughput_bucket(gradient_min=88) == "15spd"


def test_gradient_min_to_spd():
    # Evosep-like cycle times (gradient + ~25% overhead)
    assert gradient_min_to_spd(2) >= 200  # ~2 min → very high SPD
    assert 40 <= gradient_min_to_spd(21) <= 80  # ~21 min → 60 SPD range
    assert 20 <= gradient_min_to_spd(44) <= 35  # ~44 min → 30 SPD range
    assert 8 <= gradient_min_to_spd(88) <= 15  # ~88 min → Extended range


def test_amount_bucket():
    assert amount_bucket(10) == "ultra-low"
    assert amount_bucket(50) == "low"
    assert amount_bucket(100) == "mid"
    assert amount_bucket(200) == "standard"
    assert amount_bucket(500) == "high"
    assert amount_bucket(1000) == "very-high"


def test_cohort_id():
    # SPD-based
    cid = compute_cohort_id("timsTOF", 200, spd=60)
    assert cid == "timsTOF_60spd_standard"

    # Gradient fallback
    cid2 = compute_cohort_id("Astral", 50.0, gradient_min=44)
    assert cid2 == "Astral_30spd_low"


# ── Community Scores ──────────────────────────────────────────────────

def test_dia_score_with_percentiles():
    metrics = {"n_precursors": 15000, "n_peptides": 10000, "median_cv_precursor": 8.0, "grs_score": 85}
    cohort = {
        "n_precursors": [5000, 8000, 10000, 12000, 15000, 18000, 20000],
        "n_peptides": [3000, 5000, 7000, 10000, 12000, 14000],
        "median_cv_precursor": [4.0, 6.0, 8.0, 10.0, 15.0, 20.0],
        "grs_score": [40, 55, 65, 75, 85, 90, 95],
    }
    score = compute_dia_score(metrics, cohort)
    assert 0 <= score <= 100


def test_dda_score_with_percentiles():
    metrics = {"n_psms": 50000, "n_peptides_dda": 14000, "pct_delta_mass_lt5ppm": 0.95, "ms2_scan_rate": 500}
    cohort = {
        "n_psms": [10000, 20000, 35000, 50000, 70000],
        "n_peptides_dda": [5000, 8000, 12000, 14000, 18000],
        "pct_delta_mass_lt5ppm": [0.5, 0.7, 0.85, 0.95, 0.98],
        "ms2_scan_rate": [100, 200, 350, 500, 700],
    }
    score = compute_dda_score(metrics, cohort)
    assert 0 <= score <= 100


def test_score_empty_cohort():
    """With no cohort data, scores should default to middle (50)."""
    metrics = {"n_precursors": 10000, "n_peptides": 8000, "median_cv_precursor": 10, "grs_score": 70}
    score = compute_dia_score(metrics, {})
    assert 40 <= score <= 60  # should be around 50 with default percentile
