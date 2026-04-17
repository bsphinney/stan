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


# ── IPS Score (cohort-calibrated from real UCD HeLa data) ────────────
#
# IPS is 50% precursors + 30% peptides + 20% proteins, each scored by
# position within the (instrument_family, spd_bucket) reference
# distribution. A run at its cohort median scores 60; p90 scores 90.

def test_ips_dia_at_cohort_median_scores_60():
    """A run hitting its cohort p50 on every component should score 60."""
    # timsTOF fast (60 spd) cohort references
    metrics = {
        "n_precursors": 42778,
        "n_peptides": 38195,
        "n_proteins": 4768,
        "instrument_family": "timsTOF HT",
        "spd": 60,
    }
    assert compute_ips_dia(metrics) == 60


def test_ips_dia_at_cohort_p90_scores_90():
    """A run hitting its cohort p90 on every component should score 90."""
    metrics = {
        "n_precursors": 48757,
        "n_peptides": 43578,
        "n_proteins": 5104,
        "instrument_family": "timsTOF HT",
        "spd": 60,
    }
    assert compute_ips_dia(metrics) == 90


def test_ips_dia_exceptional_run_approaches_100():
    """A run 1.5× above cohort p90 on every metric should saturate near 100."""
    metrics = {
        "n_precursors": 75000,
        "n_peptides": 70000,
        "n_proteins": 8000,
        "instrument_family": "timsTOF HT",
        "spd": 60,
    }
    assert compute_ips_dia(metrics) >= 95


def test_ips_dia_poor_run_scores_low():
    """A run well below cohort p10 should score under 25."""
    metrics = {
        "n_precursors": 5000,
        "n_peptides": 4000,
        "n_proteins": 1500,
        "instrument_family": "timsTOF HT",
        "spd": 60,
    }
    assert compute_ips_dia(metrics) < 25


def test_ips_dia_empty_run_scores_zero():
    """A run with zero identifications should score 0."""
    metrics = {
        "n_precursors": 0,
        "n_peptides": 0,
        "n_proteins": 0,
        "instrument_family": "timsTOF HT",
        "spd": 60,
    }
    assert compute_ips_dia(metrics) == 0


def test_ips_spd_bucket_fallback():
    """If the specific (family, spd_bucket) is absent, fall back to family-wide."""
    # Orbitrap "fast" bucket isn't in our UCD seed — should fall back to
    # ("Lumos", "*") and still return a valid score.
    metrics = {
        "n_precursors": 30522,  # Orbitrap family-wide p50
        "n_peptides": 27907,
        "n_proteins": 3917,
        "instrument_family": "Lumos",
        "spd": 60,  # → "fast" bucket, not in refs
    }
    assert compute_ips_dia(metrics) == 60  # hits family-wide median


def test_ips_global_fallback_for_unknown_family():
    """Unknown instrument_family should still produce a valid 0-100 score."""
    metrics = {
        "n_precursors": 35000,
        "n_peptides": 31000,
        "n_proteins": 4200,
    }
    score = compute_ips_dia(metrics)
    assert 0 <= score <= 100


def test_ips_dda_at_cohort_median_scores_60():
    """An Exploris 480 DDA run hitting the cohort p50 on all three components
    (n_psms, n_peptides, n_proteins) should score exactly 60 by construction.
    DDA has its own per-family cohort references (separate from DIA) because
    DDA and DIA distributions differ on the same instrument."""
    metrics = {
        "n_psms":     18565,  # Exploris 480 DDA p50 (PSMs)
        "n_peptides": 16630,  # Exploris 480 DDA p50 (peptides)
        "n_proteins": 3420,   # Exploris 480 DDA p50 (proteins)
        "instrument_family": "Exploris 480",
    }
    assert compute_ips_dda(metrics) == 60


def test_ips_dda_top_of_cohort_scores_high():
    """A run near the top of its DDA cohort should score ~90, not ~50 as the
    old absolute-anchor formula produced. Regression test for the bug where
    Exploris 480 runs in the 96th percentile of their cohort were scoring
    IPS 49 because the PSM anchor (p50=60k) was calibrated for generic
    Orbitrap DDA, not the instrument-specific ~18k p50 of Exploris 480."""
    metrics = {
        "n_psms":     23088,  # Exploris 480 DDA max in seed
        "n_peptides": 20481,
        "n_proteins": 4117,
        "instrument_family": "Exploris 480",
    }
    assert compute_ips_dda(metrics) >= 85


def test_ips_dda_unknown_family_uses_fallback():
    """Unknown instrument family should still produce a valid 0-100 score
    via the generic DDA fallback anchors."""
    metrics = {
        "n_psms": 40000, "n_peptides": 25000, "n_proteins": 3800,
        "instrument_family": "SomeUnknownInstrument",
    }
    score = compute_ips_dda(metrics)
    assert 0 <= score <= 100


def test_ips_range_always_0_to_100():
    """IPS must always be 0-100 for any input combination."""
    for n_prec in [0, 1000, 50000, 999999]:
        for fam in ["timsTOF HT", "Lumos", "Exploris 480", None]:
            for spd in [5, 30, 60, 200, None]:
                metrics = {
                    "n_precursors": n_prec,
                    "n_peptides": n_prec,
                    "n_proteins": n_prec // 10,
                    "instrument_family": fam,
                    "spd": spd,
                }
                score = compute_ips_dia(metrics)
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
    # SPD-based, no column
    cid = compute_cohort_id("timsTOF", 200, spd=60)
    assert cid == "timsTOF_60spd_standard"

    # Gradient fallback, no column
    cid2 = compute_cohort_id("Astral", 50.0, gradient_min=44)
    assert cid2 == "Astral_30spd_low"

    # With column — column-specific cohort
    cid3 = compute_cohort_id("Astral", 50.0, spd=60, column_model="Aurora Ultimate 25cm")
    assert cid3 == "Astral_60spd_low_aurora ultimate 25cm"

    # sample_type kwarg accepted for forward-compat but not yet in the string
    cid4 = compute_cohort_id("timsTOF", 50.0, spd=100, sample_type="k562")
    assert cid4 == "timsTOF_100spd_low"


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
