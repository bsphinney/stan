"""step_monitor must record every terminal outcome.

The Hive dispatcher's `_failed_too_many()` reads `dispatch_attempts` to
stop retrying a broken raw. step_monitor's error paths used to `return
record` without writing that table, so the cap could never engage and
every cron tick re-dispatched the same files. On 2026-08-27 that was 26
raws x 33 ticks = 860 SLURM jobs in a day, each failing in ~1 s, for 28
distinct files — mostly `.d` directories with no `analysis.tdf`, which
can never succeed however often they are retried.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from stan.pipeline import hive_steps


@pytest.fixture
def recorded(monkeypatch):
    """Capture record_dispatch_attempt calls instead of touching a DB."""
    calls: list[dict] = []
    monkeypatch.setattr(
        "stan.db.record_dispatch_attempt",
        lambda **kw: calls.append(kw),
    )
    monkeypatch.setattr("stan.db.init_db", lambda *a, **k: None)
    return calls


def test_missing_raw_is_recorded_as_failed(recorded, tmp_path):
    out = hive_steps.step_monitor(
        raw_path=tmp_path / "gone.d", instrument="timsTOF HT",
        vendor="bruker", db_path=tmp_path / "stan.db",
    )
    assert out["status"] == "error"
    assert len(recorded) == 1, "a terminal failure must reach dispatch_attempts"
    assert recorded[0]["status"] == "failed"
    assert "raw not on Hive" in recorded[0]["error"]


def test_unknown_vendor_is_recorded_as_failed(recorded, tmp_path):
    raw = tmp_path / "x.d"
    raw.mkdir()
    out = hive_steps.step_monitor(
        raw_path=raw, instrument="timsTOF HT",
        vendor="nosuchvendor", db_path=tmp_path / "stan.db",
    )
    assert out["status"] == "error"
    assert len(recorded) == 1
    assert recorded[0]["status"] == "failed"
    assert recorded[0]["error_type"] == "UnknownVendor"


def test_empty_rawmeat_is_recorded_as_failed(recorded, tmp_path, monkeypatch):
    """The real-world case: a .d with no analysis.tdf."""
    raw = tmp_path / "broken.d"
    raw.mkdir()
    monkeypatch.setattr(
        "stan.metrics.rawmeat.extract_rawmeat_metrics", lambda p: {}
    )
    out = hive_steps.step_monitor(
        raw_path=raw, instrument="timsTOF HT",
        vendor="bruker", db_path=tmp_path / "stan.db",
    )
    assert out["error"] == "rawmeat extraction returned empty"
    assert len(recorded) == 1
    assert recorded[0]["status"] == "failed"
    assert recorded[0]["error_type"] == "EmptyRawmeat"
    assert recorded[0]["raw_path"] == str(raw)


def test_unexpected_exception_is_recorded_as_failed(recorded, tmp_path, monkeypatch):
    raw = tmp_path / "boom.d"
    raw.mkdir()

    def _explode(p):
        raise RuntimeError("tdf reader segfaulted")

    monkeypatch.setattr(
        "stan.metrics.rawmeat.extract_rawmeat_metrics", _explode
    )
    out = hive_steps.step_monitor(
        raw_path=raw, instrument="timsTOF HT",
        vendor="bruker", db_path=tmp_path / "stan.db",
    )
    assert "RuntimeError" in out["error"]
    assert len(recorded) == 1
    assert recorded[0]["status"] == "failed"
    assert recorded[0]["error_type"] == "RuntimeError"
