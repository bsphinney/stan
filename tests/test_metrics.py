"""Tests for metrics extraction, IPS scoring, and community scoring."""

from __future__ import annotations

from stan.metrics.chromatography import compute_ips_dia, compute_ips_dda
from stan.metrics.scoring import (
    amount_bucket,
    compute_cohort_id,
    compute_dia_score,
    compute_dda_score,
    gradient_min_to_spd,
    spd_bucket,
    throughput_bucket,
)


# ── IPS Score ────────────────────────────────────────────────────────

def test_ips_dia_excellent():
    """Excellent DIA metrics should produce a high IPS score."""
    metrics = {
        "n_precursors": 22000,
        "median_fragments_per_precursor": 10.0,
        "median_points_across_peak": 15.0,
        "pct_fragments_quantified": 0.92,
        "missed_cleavage_rate": 0.08,
    }
    score = compute_ips_dia(metrics)
    assert 85 <= score <= 100


def test_ips_dia_poor():
    """Poor DIA metrics should produce a low IPS score."""
    metrics = {
        "n_precursors": 3000,
        "median_fragments_per_precursor": 3.0,
        "median_points_across_peak": 4.0,
        "pct_fragments_quantified": 0.40,
        "missed_cleavage_rate": 0.35,
    }
    score = compute_ips_dia(metrics)
    assert score < 40


def test_ips_dda_excellent():
    """Excellent DDA metrics should produce a high IPS score."""
    metrics = {
        "n_psms": 65000,
        "pct_delta_mass_lt5ppm": 0.97,
        "median_points_across_peak": 14.0,
        "median_hyperscore": 34.0,
        "missed_cleavage_rate": 0.09,
    }
    score = compute_ips_dda(metrics)
    assert 85 <= score <= 100


def test_ips_range():
    """IPS should always be 0-100, even with extreme inputs."""
    for n in [0, 1000, 50000]:
        for pts in [None, 2.0, 8.0, 20.0]:
            metrics = {
                "n_precursors": n,
                "median_fragments_per_precursor": 5.0,
                "median_points_across_peak": pts,
                "pct_fragments_quantified": 0.5,
                "missed_cleavage_rate": 0.2,
            }
            score = compute_ips_dia(metrics)
            assert 0 <= score <= 100


def test_ips_no_points_across_peak():
    """IPS should still work when points-across-peak is unavailable."""
    metrics = {
        "n_precursors": 15000,
        "median_fragments_per_precursor": 8.0,
        "median_points_across_peak": None,
        "pct_fragments_quantified": 0.85,
        "missed_cleavage_rate": 0.12,
    }
    score = compute_ips_dia(metrics)
    assert 40 <= score <= 80  # should be reasonable even without sampling data


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
    # SPD-based, no column
    cid = compute_cohort_id("timsTOF", 200, spd=60)
    assert cid == "timsTOF_60spd_standard"

    # Gradient fallback, no column
    cid2 = compute_cohort_id("Astral", 50.0, gradient_min=44)
    assert cid2 == "Astral_30spd_low"

    # With column — column-specific cohort
    cid3 = compute_cohort_id("Astral", 50.0, spd=60, column_model="Aurora Ultimate 25cm")
    assert cid3 == "Astral_60spd_low_aurora ultimate 25cm"


def test_broad_cohort_id():
    from stan.metrics.scoring import compute_broad_cohort_id
    assert compute_broad_cohort_id("Astral_60spd_low_aurora ultimate 25cm") == "Astral_60spd_low"
    assert compute_broad_cohort_id("timsTOF_30spd_standard") == "timsTOF_30spd_standard"


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
