"""Tests for v1.0 community-benchmark schema normalization."""

from __future__ import annotations

import polars as pl

from stan.community.normalize_v1 import (
    detect_sample_type,
    normalize,
)


def test_detect_sample_type_defaults_hela():
    assert detect_sample_type("hela_qc_001.d") == "hela"
    assert detect_sample_type(None) == "hela"
    assert detect_sample_type("") == "hela"


def test_detect_sample_type_recognises_k562():
    assert detect_sample_type("K562_test.raw") == "k562"
    assert detect_sample_type("k562_dia_60spd.d") == "k562"


def test_detect_sample_type_recognises_yeast():
    assert detect_sample_type("yeast_qc.raw") == "yeast"
    assert detect_sample_type("sc_pombe.d") == "yeast"


def _row(**kwargs) -> dict:
    """Build a synthetic submission row with sane defaults."""
    base = {
        "submission_id": kwargs.get("submission_id", "test-id"),
        "stan_version": kwargs.get("stan_version", "0.2.66"),
        "schema_version": kwargs.get("schema_version", None),
        "display_name": kwargs.get("display_name", "Test Lab"),
        "instrument_family": kwargs.get("instrument_family", "timsTOF"),
        "instrument_model": kwargs.get("instrument_model", "timsTOF HT"),
        "acquisition_mode": kwargs.get("acquisition_mode", "DIA"),
        "spd": kwargs.get("spd", 60),
        "ips_score": kwargs.get("ips_score", 75),
        "cohort_id": kwargs.get(
            "cohort_id", "timsTOF_60spd_low_10cm x 150um, 1.5um c18 (evosep)"
        ),
        "run_name": kwargs.get("run_name", "qc_run.d"),
        "run_date": kwargs.get("run_date", None),
        "run_date_inferred": kwargs.get("run_date_inferred", False),
        "submitted_at": kwargs.get("submitted_at", "2026-04-01T12:00:00Z"),
        "fasta_md5": kwargs.get("fasta_md5", None),
        "speclib_md5": kwargs.get("speclib_md5", None),
        "diann_version": kwargs.get("diann_version", None),
        "sample_type": kwargs.get("sample_type", None),
        "n_precursors": kwargs.get("n_precursors", 30000),
    }
    return base


def test_normalize_lowercases_mode():
    df = pl.DataFrame([_row(acquisition_mode="DIA"), _row(acquisition_mode="dda")])
    out = normalize(df)
    all_modes = pl.concat(
        [out["v1"], out["historical"], out["quarantine"]], how="diagonal_relaxed"
    )["acquisition_mode"].to_list()
    assert all(m == m.lower() for m in all_modes if m)


def test_normalize_collapses_cohort_suffix():
    df = pl.DataFrame([_row(
        cohort_id="timsTOF_60spd_low_10cm x 150um, 1.5um c18 (evosep)"
    )])
    out = normalize(df)
    combined = pl.concat(
        [out["v1"], out["historical"], out["quarantine"]], how="diagonal_relaxed"
    )
    assert combined["cohort_id"].to_list() == ["timsTOF_60spd_low"]


def test_normalize_backfills_sample_type_from_run_name():
    df = pl.DataFrame([_row(run_name="K562_test.d", sample_type=None)])
    out = normalize(df)
    combined = pl.concat(
        [out["v1"], out["historical"], out["quarantine"]], how="diagonal_relaxed"
    )
    assert combined["sample_type"].to_list() == ["k562"]


def test_normalize_backfills_run_date_from_submitted_at():
    df = pl.DataFrame([_row(run_date=None, submitted_at="2026-04-01T12:00:00Z")])
    out = normalize(df)
    combined = pl.concat(
        [out["v1"], out["historical"], out["quarantine"]], how="diagonal_relaxed"
    )
    assert combined["run_date"].to_list() == ["2026-04-01T12:00:00Z"]
    assert combined["run_date_inferred"].to_list() == [True]


def test_normalize_quarantines_zero_ips_zero_spd():
    df = pl.DataFrame([_row(ips_score=0, spd=0)])
    out = normalize(df)
    assert out["v1"].height == 0
    assert out["historical"].height == 0
    assert out["quarantine"].height == 1


def test_normalize_quarantines_empty_cohort():
    df = pl.DataFrame([_row(cohort_id="")])
    out = normalize(df)
    assert out["quarantine"].height == 1


def test_normalize_v1_requires_assets_verified_for_dia():
    """A DIA row missing speclib_md5 cannot reach the v1 split."""
    df = pl.DataFrame([_row(
        schema_version="v1.0.0",
        acquisition_mode="dia",
        fasta_md5="abc123",
        speclib_md5=None,
    )])
    out = normalize(df)
    assert out["v1"].height == 0
    assert out["historical"].height == 1


def test_normalize_v1_passes_with_full_assets():
    """A DIA row with both fasta and speclib md5 lands in v1."""
    df = pl.DataFrame([_row(
        schema_version="v1.0.0",
        acquisition_mode="dia",
        fasta_md5="abc123",
        speclib_md5="def456",
        ips_score=75,
        spd=60,
    )])
    out = normalize(df)
    assert out["v1"].height == 1
    assert out["v1"]["assets_verified"].to_list() == [True]
    assert out["v1"]["schema_version"].to_list() == ["v1.0.0"]


def test_normalize_pre_1_0_marker_for_legacy():
    """Old submissions with no asset hashes still land in historical."""
    df = pl.DataFrame([_row(stan_version="0.2.0", fasta_md5=None, speclib_md5=None)])
    out = normalize(df)
    assert out["historical"].height == 1
    assert out["historical"]["schema_version"].to_list() == ["pre-1.0"]
    assert out["historical"]["assets_verified"].to_list() == [False]


def test_normalize_empty_input():
    df = pl.DataFrame([_row()]).head(0)
    out = normalize(df)
    assert out["v1"].height == 0
    assert out["historical"].height == 0
    assert out["quarantine"].height == 0
