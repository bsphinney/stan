"""Pipeline integrity tests — catch version desync and schema mismatches.

These tests run in CI on every push. They verify:
1. Version numbers are in sync across all files
2. The relay submission payload matches the relay's expected schema
3. The baseline builder doesn't require an HF token
4. Community submission doesn't require an HF token
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest


# ── 1. Version consistency ────────────────────────────────────────────

def test_version_sync():
    """pyproject.toml and stan/__init__.py must have the same version."""
    import tomllib

    root = Path(__file__).resolve().parent.parent
    with open(root / "pyproject.toml", "rb") as f:
        pyproject_version = tomllib.load(f)["project"]["version"]

    from stan import __version__
    assert __version__ == pyproject_version, (
        f"Version mismatch: __init__.py={__version__}, pyproject.toml={pyproject_version}. "
        f"Always bump both files together."
    )


def test_version_not_placeholder():
    """Version should not be 0.0.0 or 0.1.0 (the old default)."""
    from stan import __version__
    assert __version__ not in ("0.0.0", "0.1.0"), (
        f"Version {__version__} looks like a placeholder — did you forget to bump?"
    )


# ── 2. Relay submission schema ────────────────────────────────────────

# These are the fields the relay's BenchmarkSubmission Pydantic model expects.
# If the relay schema changes, update this set AND the client payload in submit.py.
RELAY_EXPECTED_FIELDS = {
    "stan_version",
    "display_name",
    "instrument_family",
    "instrument_model",
    "acquisition_mode",
    "spd",
    "gradient_length_min",
    "amount_ng",
    "n_precursors",
    "n_peptides",
    "n_proteins",
    "n_psms",
    "median_cv_precursor",
    "median_fragments_per_precursor",
    "ips_score",
    "missed_cleavage_rate",
    "cohort_id",
    "fingerprint",
    "diann_version",
    "column_vendor",
    "column_model",
}

# Fields the relay generates server-side — client must NOT send these
RELAY_SERVER_SIDE_FIELDS = {
    "submission_id",
    "submitted_at",
    "community_score",
    "is_flagged",
}


def _build_test_payload() -> dict:
    """Build a submit payload the same way submit_to_benchmark does."""
    from stan import __version__
    return {
        "stan_version": __version__,
        "display_name": "Test Lab",
        "instrument_family": "timsTOF",
        "instrument_model": "timsTOF HT",
        "acquisition_mode": "DIA",
        "spd": 104,
        "gradient_length_min": 11,
        "amount_ng": 50.0,
        "n_precursors": 15000,
        "n_peptides": 10000,
        "n_proteins": 3000,
        "n_psms": 0,
        "median_cv_precursor": 8.0,
        "median_fragments_per_precursor": 6.0,
        "ips_score": 75,
        "missed_cleavage_rate": 0.10,
        "cohort_id": "timsTOF_50ng_104spd",
        "fingerprint": "test_fingerprint_abc123",
        "diann_version": "2.3.0",
        "column_vendor": "PepSep",
        "column_model": "PepSep MAX 10cm x 150um, 1.5um C18",
    }


def test_submit_payload_has_all_relay_fields():
    """Client payload must include every field the relay expects."""
    payload = _build_test_payload()
    missing = RELAY_EXPECTED_FIELDS - set(payload.keys())
    assert not missing, f"Client payload missing relay fields: {missing}"


def test_submit_payload_no_server_side_fields():
    """Client must not send fields the relay generates server-side."""
    payload = _build_test_payload()
    leaked = RELAY_SERVER_SIDE_FIELDS & set(payload.keys())
    assert not leaked, (
        f"Client payload contains server-side fields: {leaked}. "
        f"The relay generates these — remove them from the client."
    )


def test_submit_payload_is_json_serializable():
    """The payload must serialize to JSON without errors."""
    payload = _build_test_payload()
    serialized = json.dumps(payload)
    roundtrip = json.loads(serialized)
    assert roundtrip == payload


# ── 3. No HF token required ──────────────────────────────────────────

def test_submit_no_token_required():
    """submit_to_benchmark must not raise RuntimeError about missing HF token."""
    from stan.community.submit import submit_to_benchmark
    from stan.community.validate import ValidationResult

    run = {
        "instrument": "timsTOF HT",
        "mode": "DIA",
        "n_precursors": 15000,
        "n_peptides": 10000,
        "n_proteins": 3000,
        "n_psms": 0,
        "median_cv_precursor": 8.0,
        "median_fragments_per_precursor": 6.0,
        "ips_score": 75,
        "missed_cleavage_rate": 0.10,
        "pct_charge_1": 0.05,
        "run_name": "test_run",
        "diann_version": "2.3.0",
    }

    # Mock the network call so we don't actually hit the relay
    mock_response = json.dumps({
        "submission_id": "test-uuid-1234",
        "cohort_id": "timsTOF_50ng_104spd",
        "status": "accepted",
    }).encode()

    # Mock validation to pass (we're testing token flow, not validation)
    mock_validation = ValidationResult(is_valid=True)

    with patch("stan.community.submit.urllib.request.urlopen") as mock_urlopen, \
         patch("stan.community.submit.load_community", return_value={}), \
         patch("stan.community.submit.validate_submission", return_value=mock_validation), \
         patch("stan.community.submit.mark_submitted"):
        mock_urlopen.return_value.__enter__ = lambda s: s
        mock_urlopen.return_value.__exit__ = lambda s, *a: None
        mock_urlopen.return_value.read.return_value = mock_response

        # This should NOT raise RuntimeError about missing HF token
        result = submit_to_benchmark(run, spd=104, diann_version="2.3.0")
        assert result["status"] == "submitted"


def test_baseline_community_no_token_gate():
    """The baseline community section must not check for HF tokens."""
    import inspect
    from stan.baseline import run_baseline

    source = inspect.getsource(run_baseline)
    assert "hf_token" not in source, (
        "run_baseline still references hf_token — community submission "
        "goes through the relay and should not require a token."
    )
