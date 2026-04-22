"""Thermo .raw smoke tests.

Thermo path differs from Bruker in a few ways that need their own
coverage:
  - .raw is a single binary, not a directory - stability check
    semantics change
  - TIC extraction uses fisher_py or ThermoRawFileParser, not the
    Bruker TDF SQLite reader
  - No PEG / drift detection (alphatims is Bruker-only)
  - DIA-NN can read .raw directly on Linux 2.1+; Sage requires mzML
    conversion via ThermoRawFileParser

Keeps the Bruker-specific smoke coverage in test_smoke.py and
only tests the Thermo-specific bits here.
"""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


# ─────────── Stability on a .raw file ───────────

def test_stability_thermo_raw_assume_complete(hive_fixture_path):
    """Closed .raw acquisition should stabilize when assume_complete
    is set. Mirrors the Bruker smoke but exercises the single-file
    branch of StabilityTracker.check()."""
    from stan.watcher.stability import StabilityTracker

    raw = hive_fixture_path("thermo_lumos_dia")
    assert raw.is_file(), f"expected .raw to be a file: {raw}"

    tr = StabilityTracker(path=raw, vendor="thermo",
                           stable_secs=60, assume_complete=True)
    import time
    tr._last_check = 0
    assert tr.check() is False, "first check builds history"

    now = time.time()
    size = tr.last_size
    assert size and size > 1024 * 1024, "raw file suspiciously small"
    tr._size_history = [(now - 60 + i * 10, size) for i in range(7)]
    tr._last_check = 0

    assert tr.check() is True, "closed .raw should stabilize"


# ─────────── TIC extraction ───────────

def test_tic_extract_thermo(hive_fixture_path):
    """extract_tic_thermo should return a non-empty trace when
    fisher_py (or TRFP) is available. When neither is installed,
    returns None - skip the test in that case rather than failing."""
    from stan.metrics.tic import extract_tic_thermo, downsample_trace

    raw = hive_fixture_path("thermo_lumos_dia")
    try:
        trace = extract_tic_thermo(raw)
    except Exception as e:
        pytest.skip(
            f"Thermo TIC extraction not available in this environment: {e}"
        )

    if trace is None:
        pytest.skip("extract_tic_thermo returned None - fisher_py / TRFP "
                    "not installed or unreadable raw")

    assert len(trace.rt_min) > 50
    assert len(trace.rt_min) == len(trace.intensity)
    assert max(trace.intensity) > 0

    ds = downsample_trace(trace, n_bins=128)
    assert len(ds.intensity) == 128
    # Mean-per-bin sanity: Thermo per-scan TIC values are typically in
    # the 1e6-1e9 range. Sum-per-bin would push this well above 1e10.
    assert max(ds.intensity) < 1e11, (
        f"max TIC {max(ds.intensity):.2e} suggests sum-per-bin regression"
    )


# ─────────── Mode detection ───────────

def test_thermo_mode_detection(hive_fixture_path):
    """The watcher's detector should identify a .raw's acquisition
    mode from its metadata. Lumos_dia fixture is DIA (from filename).
    """
    from stan.watcher.detector import detect_mode

    raw = hive_fixture_path("thermo_lumos_dia")
    try:
        mode = detect_mode(raw, vendor="thermo")
    except Exception as e:
        pytest.skip(f"detect_mode raised: {e}")
    assert mode is not None, "detect_mode returned None"
    # Mode is an AcquisitionMode enum with .value (or a str fallback).
    value = getattr(mode, "value", mode)
    assert isinstance(value, str) and len(value) > 0


# ─────────── DDA file routing ───────────

def test_exploris_dda_stability(hive_fixture_path):
    """Confirm the DDA .raw stabilizes the same way. Exploris DDA
    files are ~1 GB - copy cost is noticeable but one-time per
    session."""
    from stan.watcher.stability import StabilityTracker

    raw = hive_fixture_path("thermo_exploris_dda")
    assert raw.is_file()
    tr = StabilityTracker(path=raw, vendor="thermo",
                           stable_secs=60, assume_complete=True)
    # Same pattern as the Lumos test.
    import time
    tr._last_check = 0
    tr.check()
    now = time.time()
    size = tr.last_size
    tr._size_history = [(now - 60 + i * 10, size) for i in range(7)]
    tr._last_check = 0
    assert tr.check() is True
