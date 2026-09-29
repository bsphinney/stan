"""pandas 3 left alphatims' dummy frame un-zeroed and shifted every frame
window (Hive venv, 2026-05-07 to v1.2.2). A shifted read must never become
a PEG or drift number."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pandas as pd
import pytest

from stan.metrics.alphatims_guard import frame_table_problem


def _frames(dummy_id: int, dummy_peaks: int) -> pd.DataFrame:
    return pd.DataFrame({
        "Id": [dummy_id, 1, 2, 3],
        "NumPeaks": [dummy_peaks, 30329, 480, 287088],
        "MsMsType": [0, 0, 9, 0],
    })


class _FakeTimsTOF:
    frames = _frames(0, 0)

    def __init__(self, path, use_hdf_if_available=True):
        self.path = path

    def __getitem__(self, fid):  # pragma: no cover - never reached when shifted
        raise AssertionError("a shifted frame table must not be read")


@pytest.fixture
def fake_alphatims(monkeypatch):
    """Install a stand-in ``alphatims.bruker`` whose frame table we control."""
    pkg = types.ModuleType("alphatims")
    bruker = types.ModuleType("alphatims.bruker")
    bruker.TimsTOF = _FakeTimsTOF
    pkg.bruker = bruker
    monkeypatch.setitem(sys.modules, "alphatims", pkg)
    monkeypatch.setitem(sys.modules, "alphatims.bruker", bruker)
    return _FakeTimsTOF


def test_zeroed_dummy_frame_is_fine():
    data = types.SimpleNamespace(frames=_frames(0, 0))
    assert frame_table_problem(data) is None


def test_pandas3_copy_of_frame_1_is_refused():
    # What pandas 3 left behind on the HT: the dummy is a copy of frame 1.
    data = types.SimpleNamespace(frames=_frames(1, 30329))
    problem = frame_table_problem(data)
    assert problem and "not zeroed" in problem and "NumPeaks=30329" in problem
    assert "pandas<3" in problem


def test_unreadable_frame_table_is_refused():
    assert "cannot check" in frame_table_problem(types.SimpleNamespace(frames=pd.DataFrame({"Id": [0]})))
    assert "cannot check" in frame_table_problem(object())


def test_peg_reader_raises_unavailable_instead_of_reading_shifted_frames(fake_alphatims, monkeypatch):
    from stan.metrics.peg_io import PegReaderUnavailable, read_ms1_bruker

    monkeypatch.setattr(fake_alphatims, "frames", _frames(1, 30329))
    with pytest.raises(PegReaderUnavailable, match="not zeroed"):
        list(read_ms1_bruker(Path("/data/QC_1.d"), n_scans=4))


def test_window_drift_reports_unknown_instead_of_a_shifted_drift(fake_alphatims, monkeypatch):
    from stan.metrics.window_drift import detect_window_drift

    monkeypatch.setattr(fake_alphatims, "frames", _frames(1, 30329))
    result = detect_window_drift(Path("/data/QC_1.d"))
    assert result.drift_class == "unknown"
