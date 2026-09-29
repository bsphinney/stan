"""The instrument-PC watcher never stores an empty MS1 read as a clean 0.0.

``InstrumentWatcher._run_peg_and_drift`` (stan/watcher/daemon.py) is the
single-lab install's twin of ``hive_process._run_peg_and_drift``. When the
reader yields nothing -- no spectra, empty spectra, or every peak under
``detect_peg_in_spectra``'s 1e4 floor -- the detector answers score 0.0,
0 ions, 0 %, class ``'clean'``: exactly a spotless run. Stored, that is what
``stan peg-sync`` publishes as clean (spec 4.1). Hive and the Thermo backfill
leave PEG NULL there; so must the watcher.
"""

from __future__ import annotations

import logging
import types

import pytest

from stan.metrics import peg_io
from stan.metrics.peg import PEG_REFERENCE


def _ladder_spectra():
    """A real PEG signal: five +H oligomers above the floor."""
    ions = [i for i in PEG_REFERENCE if i.adduct == "+H" and 5 <= i.n <= 9]
    return [[(400.0, 5e6), (ion.mz, 1e6)] for ion in ions]


@pytest.fixture
def watcher(monkeypatch, tmp_path):
    """A bare InstrumentWatcher whose DB writes are recorded, not made."""
    from stan import db
    from stan.watcher.daemon import InstrumentWatcher

    calls: list = []
    monkeypatch.setattr(db, "update_peg_result",
                        lambda **kw: calls.append(("scalars", kw)) or True)
    monkeypatch.setattr(db, "insert_peg_ion_hits",
                        lambda **kw: calls.append(("hits", kw)) or len(kw["matches"]))
    raw = tmp_path / "HeLa_QC_1.raw"          # a file, so drift (Bruker-only) is skipped
    raw.write_bytes(b"")
    w = InstrumentWatcher.__new__(InstrumentWatcher)
    return types.SimpleNamespace(w=w, calls=calls, raw=raw)


_NO_SIGNAL = {
    "no-spectra": [],
    "empty-spectra": [[], []],
    "below-1e4-floor": [[(445.2, 50.0)], [(489.3, 900.0)]],
}


@pytest.mark.parametrize("spectra", list(_NO_SIGNAL.values()), ids=list(_NO_SIGNAL))
def test_no_ms1_signal_writes_nothing_and_warns(watcher, monkeypatch, caplog, spectra):
    monkeypatch.setattr(peg_io, "read_ms1_any", lambda *a, **k: iter(spectra))
    with caplog.at_level(logging.WARNING, logger="stan.watcher.daemon"):
        watcher.w._run_peg_and_drift(watcher.raw, "r1", table="runs")
    assert watcher.calls == [], "an empty read was stored as a measurement"
    assert any("no MS1 signal" in r.getMessage() and watcher.raw.name in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)


def test_real_signal_is_still_written(watcher, monkeypatch):
    # The control: the recorders do see a real measurement, scalars then hits.
    monkeypatch.setattr(peg_io, "read_ms1_any", lambda *a, **k: iter(_ladder_spectra()))
    watcher.w._run_peg_and_drift(watcher.raw, "r1", table="sample_health")
    assert [c[0] for c in watcher.calls] == ["scalars", "hits"]
    scalars = watcher.calls[0][1]
    assert scalars["run_id"] == "r1" and scalars["table"] == "sample_health"
    assert scalars["peg_n_ions_detected"] == 5 and scalars["peg_intensity_pct"] > 0


def test_no_signal_still_runs_drift_on_a_bruker_d(watcher, monkeypatch, tmp_path):
    """Only the PEG write is skipped; drift keeps its own path, as before."""
    from stan import db
    from stan.metrics import window_drift

    d = tmp_path / "HeLa_QC_2.d"
    d.mkdir()
    monkeypatch.setattr(peg_io, "read_ms1_any", lambda *a, **k: iter([]))
    drift = types.SimpleNamespace(global_coverage=None, median_drift_im=None,
                                  p90_abs_drift_im=None, drift_class="unknown",
                                  per_window=[])
    monkeypatch.setattr(window_drift, "detect_window_drift", lambda p: drift)
    monkeypatch.setattr(db, "update_drift_result",
                        lambda **kw: watcher.calls.append(("drift", kw)) or True)
    watcher.w._run_peg_and_drift(d, "r2", table="runs")
    assert [c[0] for c in watcher.calls] == ["drift"]
