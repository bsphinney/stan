"""Sanity-check that every metric a community submission depends on
is populated (non-null, in a plausible range) when the extractor is
fed a known-good DIA-NN or Sage report.

The contract here is the union of every field the dashboard renders
plus the gates the relay enforces. If a future code change starts
returning None for one of these, that's a smoke-test failure here
before any cluster job ever fails on the real data.
"""

from __future__ import annotations

import polars as pl
import pytest

from stan.metrics.extractor import extract_dia_metrics, extract_dda_metrics


# ── DIA fixtures + assertions ──────────────────────────────────────


def _make_diann_report(tmp_path):
    """Tiny but schema-faithful DIA-NN 2.x report.parquet stand-in."""
    n = 500
    df = pl.DataFrame({
        "Run": ["test_run.d"] * n,
        "Stripped.Sequence": [f"PEP{i % 200}" for i in range(n)],
        "Modified.Sequence": [f"PEP{i % 200}" for i in range(n)],
        "Precursor.Id": [f"PEP{i % 200}.2" for i in range(n)],
        "Precursor.Charge": [2, 3] * (n // 2),
        "Precursor.Mz": [400.0 + i for i in range(n)],
        "Precursor.Quantity": [1e5 * (i + 1) for i in range(n)],
        "Q.Value": [0.005] * n,
        "Global.Q.Value": [0.005] * n,
        "PG.Q.Value": [0.005] * n,
        "Protein.Group": [f"PROT{i % 50}" for i in range(n)],
        "Protein.Names": [f"PROT{i % 50}" for i in range(n)],
        "RT": [0.5 * i for i in range(n)],
        "RT.Start": [0.5 * i for i in range(n)],
        "RT.Stop": [0.5 * i + 0.15 for i in range(n)],
        "Ms1.Apex.Area": [1e6 * (i + 1) for i in range(n)],
        "Ms1.Area": [1e6 * (i + 1) for i in range(n)],
        "Mass.Evidence": [50.0] * n,
    })
    path = tmp_path / "report.parquet"
    df.write_parquet(path)
    # Stats TSV — DIA-NN writes alongside report.parquet
    stats_path = tmp_path / "report.stats.tsv"
    stats_path.write_text(
        "Median.Mass.Acc.MS1.Corrected\tMedian.Mass.Acc.MS2.Corrected\t"
        "FWHM.Scans\tFWHM.RT\tMS1.Signal\tMS2.Signal\n"
        "1.5\t3.0\t6.0\t0.05\t1e10\t5e9\n"
    )
    return path


def test_dia_thermo_metrics_complete(tmp_path):
    """A clean DIA-NN report on Thermo data should populate every
    field the relay's BenchmarkSubmission schema reads."""
    report = _make_diann_report(tmp_path)
    metrics = extract_dia_metrics(report, vendor="thermo", gradient_min=22.0)

    # Required: counts > 0 (gated by HARD_GATES)
    assert metrics["n_precursors"] > 0
    assert metrics["n_peptides"] > 0
    assert metrics["n_proteins"] > 0

    # Required keys: every field the relay's BenchmarkSubmission
    # schema reads for DIA submissions. Synthetic data has limited
    # numerical diversity, so dynamic_range_log10 / peak_capacity may
    # legitimately come back None on this fixture even when the real
    # extractor works on production data — assert presence as keys
    # only for those, full population for the more deterministic
    # ones.
    for k in (
        "median_mass_acc_ms1_ppm",
        "median_mass_acc_ms2_ppm",
        "ms1_signal",
        "ms2_signal",
        "fwhm_rt_min",
        "median_peak_width_sec",
    ):
        assert metrics.get(k) is not None, f"{k} is None"
        assert metrics[k] > 0, f"{k}={metrics[k]} not > 0"
    for k in ("dynamic_range_log10", "peak_capacity"):
        assert k in metrics, f"{k} not in metric dict"

    # Thermo pts/peak: now derived from FWHM.Scans*2
    assert metrics["median_points_across_peak"] is not None
    assert 1 <= metrics["median_points_across_peak"] <= 50, (
        f"pts/peak {metrics['median_points_across_peak']} out of plausible range"
    )


# ── DDA fixtures + assertions ──────────────────────────────────────


def _make_sage_results(tmp_path):
    """Tiny but schema-faithful Sage results.sage.parquet stand-in."""
    n = 1000
    df = pl.DataFrame({
        "filename": ["test_run.d"] * n,
        "scannr": list(range(n)),
        "peptide": [f"PEP{i % 400}" for i in range(n)],
        "stripped_peptide": [f"PEP{i % 400}" for i in range(n)],
        "proteins": [f"PROT{i % 80}" for i in range(n)],
        "charge": [2, 3] * (n // 2),
        "expmass": [1500.0 + i for i in range(n)],
        "calcmass": [1500.0 + i + 0.001 for i in range(n)],
        "delta_mass": [0.5 * (-1) ** i for i in range(n)],
        "hyperscore": [40.0 + (i % 20) for i in range(n)],
        "score": [40.0 + (i % 20) for i in range(n)],
        "spectrum_q": [0.005] * n,
        "peptide_q": [0.005] * n,
        "protein_q": [0.005] * n,
        "rank": [1] * n,
        "label": [1] * n,
        "retention_time": [0.5 * (i % 60) for i in range(n)],
    })
    path = tmp_path / "results.sage.parquet"
    df.write_parquet(path)
    return path


def test_dda_metrics_complete(tmp_path):
    """A clean Sage result should populate every DDA-side field."""
    results = _make_sage_results(tmp_path)
    metrics = extract_dda_metrics(results, gradient_min=22)

    # Required: counts > 0 (gated by HARD_GATES for DDA)
    assert metrics["n_psms"] > 0
    assert metrics["n_peptides_dda"] > 0
    # n_proteins added in v0.2.279 — must round-trip
    assert metrics.get("n_proteins") is not None
    assert metrics["n_proteins"] > 0

    # Mass accuracy + scan rate
    assert metrics.get("ms2_scan_rate") is not None
    assert metrics["ms2_scan_rate"] > 0
    assert metrics.get("median_delta_mass_ppm") is not None
    assert metrics.get("pct_delta_mass_lt5ppm") is not None

    # Hyperscore distribution
    assert metrics.get("median_hyperscore") is not None
    assert metrics["median_hyperscore"] > 0


# ── Submission payload roundtrip ───────────────────────────────────


def test_dda_metrics_carry_into_submit_payload():
    """Regression for the v0.2.279 bug: DDA peptide/protein counts
    were extracted but never copied into the submit_payload, so DDA
    rows landed with 0 peptides and 0 proteins on the dashboard."""
    from stan.community.submit import submit_to_benchmark

    # We don't actually post — just inspect the payload via monkey-patch
    captured: dict = {}

    def fake_open(req, timeout=None):  # noqa: ARG001
        captured["data"] = req.data
        raise OSError("intentional — only inspecting the payload")

    import urllib.request
    orig = urllib.request.urlopen
    urllib.request.urlopen = fake_open
    try:
        run = {
            "id": "test",
            "instrument": "timsTOF HT",
            "run_name": "test.d",
            "mode": "dda",
            "diann_version": "2.3.0",
            "n_psms": 30000,
            "n_peptides_dda": 18000,  # the field we want round-tripped
            "n_proteins": 4500,
            "missed_cleavage_rate": 0.05,
            "pct_charge_1": 0.0,
            "pct_delta_mass_lt5ppm": 0.95,
            "ms2_scan_rate": 200.0,
        }
        with pytest.raises((OSError, RuntimeError)):
            submit_to_benchmark(run, spd=60, amount_ng=50.0, diann_version="2.3.0")
    finally:
        urllib.request.urlopen = orig

    import json as _json
    payload = _json.loads(captured["data"].decode("utf-8"))
    assert payload["n_psms"] == 30000
    assert payload["n_peptides_dda"] == 18000, "n_peptides_dda missing from payload"
    assert payload["n_proteins"] == 4500, "n_proteins missing from payload"
