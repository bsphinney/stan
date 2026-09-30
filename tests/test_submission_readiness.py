"""One rule decides what a community sync would send (submit-all and the
dashboard's Sync button). Before 1.2.10 the button offered 699 runs of which
346 were already on the site and most of the rest would be rejected."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from stan.community import submit
from stan.community.submit import DuplicateSubmission, submission_readiness

READY_DIA = {
    "mode": "DIA", "diann_version": "2.3.2",
    "n_precursors": 40000, "n_peptides": 30000, "n_proteins": 5000,
    "pct_charge_1": 0.05, "missed_cleavage_rate": 0.1,
    "ms1_signal": 1e12, "ms2_signal": 1e11, "fwhm_rt_min": 0.1,
    "median_mass_acc_ms1_ppm": 1.0, "median_mass_acc_ms2_ppm": 2.0,
    "peak_capacity": 300.0, "dynamic_range_log10": 3.5,
    "median_points_across_peak": 9.0, "median_peak_width_sec": 5.0,
    "tic_rt_bins": "[0.1, 0.2]", "tic_intensity": "[1, 2]",
}


def test_complete_dia_run_is_ready():
    assert submission_readiness(dict(READY_DIA)) == ("ready", "")


@pytest.mark.parametrize("field,value", [
    ("tic_rt_bins", None), ("tic_intensity", "[]"), ("peak_capacity", None),
    ("median_peak_width_sec", None),
])
def test_missing_row_metrics_mean_waiting(field, value):
    state, why = submission_readiness(dict(READY_DIA, **{field: value}))
    assert state == "needs_metrics" and field in why


def test_too_few_ids_is_ineligible():
    state, why = submission_readiness(dict(READY_DIA, n_precursors=1200, n_proteins=400))
    assert state == "ineligible" and "below minimum" in why


def test_unknown_diann_version_is_ineligible():
    assert submission_readiness(dict(READY_DIA, diann_version="unknown"))[0] == "ineligible"


DDA = {"mode": "ddaPASEF", "n_psms": 30000, "n_peptides_dda": 20000,
       "n_peptides": 20000, "n_proteins": 4000, "ms2_scan_rate": 50.0}


def test_dda_does_not_need_dia_metrics():
    assert submission_readiness(dict(DDA, diann_version="2.3.0")) == ("ready", "")


def test_dda_without_a_diann_version_is_ineligible():
    """Review of 1.2.10: submit_to_benchmark checks the version for DDA too and
    fails 'unknown'; these 18 rows were counted ready and failed every sync."""
    assert submission_readiness(dict(DDA))[0] == "ineligible"


def test_tic_known_from_the_side_table_counts():
    """Rows from get_runs carry no TIC arrays; the backlog marks _has_tic instead."""
    run = dict(READY_DIA, tic_rt_bins=None, tic_intensity=None, _has_tic=True)
    assert submission_readiness(run) == ("ready", "")


def test_relay_409_duplicate_becomes_DuplicateSubmission(monkeypatch):
    sid = "0f3c2a4e-1111-2222-3333-444455556666"
    detail = ("Duplicate submission: fingerprint abc already exists. This run appears to have been "
              f"submitted before from the same lab. Existing submission_id: {sid}.")

    def fake_urlopen(req, timeout=30):
        raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {},
                                     io.BytesIO(json.dumps({"detail": detail}).encode()))

    monkeypatch.setattr(submit.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(submit, "load_community", lambda: {"display_name": "Lab", "community_submit": True})
    run = dict(READY_DIA, id="r1", instrument="timsTOF HT", run_name="x.d", run_date="2026-09-01T00:00:00Z",
               spd=60, amount_ng=50, vendor="bruker")
    with pytest.raises(DuplicateSubmission) as e:
        submit.submit_to_benchmark(run, spd=60, gradient_length_min=21, amount_ng=50, diann_version="2.3.0")
    assert e.value.existing_submission_id == sid
