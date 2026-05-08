"""Tests for submit_after_upload flag in hive-mode watcher dispatch."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_instrument_watcher(config: dict):
    """Build a minimal InstrumentWatcher with a given config dict."""
    from stan.watcher.daemon import InstrumentWatcher

    watcher = InstrumentWatcher.__new__(InstrumentWatcher)
    watcher._config = config
    watcher._name = config.get("name", "TestInstrument")
    watcher._events: list = []
    watcher._lock = __import__("threading").Lock()

    def _record_event(category, path, detail=""):
        watcher._events.append((category, str(path), detail))

    watcher._record_event = _record_event
    return watcher


def _make_upload_result(dest: str) -> dict:
    return {
        "status": "done",
        "dest": dest,
        "size_bytes": 1024,
        "error": None,
        "source": "/raw/foo.d",
    }


def _patch_upload_module(upload_rv: dict, ssh_rv: dict | None = None):
    """Context manager: monkey-patch upload + SSH on the live module object.

    Returns (upload_mock, ssh_mock, ctx_manager).  Use as::

        with _patch_upload_module(...) as (upload_mock, ssh_mock):
            watcher._on_hive_upload(raw, "qc")
    """
    import contextlib
    import stan.sync.upload_to_hive as _mod

    @contextlib.contextmanager
    def _ctx():
        upload_mock = MagicMock(return_value=upload_rv)
        ssh_mock = MagicMock(return_value=ssh_rv or {})
        orig_upload = _mod.upload_raw_to_incoming
        orig_ssh = _mod.submit_one_via_ssh
        _mod.upload_raw_to_incoming = upload_mock
        _mod.submit_one_via_ssh = ssh_mock
        try:
            yield upload_mock, ssh_mock
        finally:
            _mod.upload_raw_to_incoming = orig_upload
            _mod.submit_one_via_ssh = orig_ssh

    return _ctx()


# ---------------------------------------------------------------------------
# Test 1: submit_after_upload=false → only upload runs, SSH is never called
# ---------------------------------------------------------------------------
def test_submit_after_upload_false_skips_ssh(tmp_path: Path) -> None:
    raw = tmp_path / "foo.d"
    raw.mkdir()

    config = {
        "name": "timsTOF HT",
        "processing_mode": "hive",
        "hive_upload_dir": str(tmp_path / "incoming"),
        "submit_after_upload": False,
    }
    watcher = _make_instrument_watcher(config)
    upload_rv = _make_upload_result(str(tmp_path / "incoming" / "foo.d"))

    with _patch_upload_module(upload_rv) as (upload_mock, ssh_mock):
        watcher._on_hive_upload(raw, "qc")

    assert upload_mock.called, "upload_raw_to_incoming should still be called"
    assert not ssh_mock.called, (
        "submit_one_via_ssh must NOT be called when submit_after_upload=false"
    )

    categories = [ev[0] for ev in watcher._events]
    assert "hive_upload_done" in categories
    assert "hive_submit_deferred" in categories, (
        f"Expected hive_submit_deferred event; got: {categories}"
    )


# ---------------------------------------------------------------------------
# Test 2: no submit_after_upload key (default True) → upload AND SSH run
# ---------------------------------------------------------------------------
def test_submit_after_upload_default_calls_ssh(tmp_path: Path) -> None:
    raw = tmp_path / "bar.d"
    raw.mkdir()

    ssh_key = tmp_path / "id_ed25519"
    ssh_key.write_text("fake key")

    config = {
        "name": "timsTOF HT",
        "processing_mode": "hive",
        "hive_upload_dir": str(tmp_path / "incoming"),
        "ssh_key": str(ssh_key),
        "hive_user": "brettsp",
        "hive_host": "hive.hpc.ucdavis.edu",
        "hive_venv": "/quobyte/proteomics-grp/brett/stan_venv",
        "vendor": "bruker",
        # submit_after_upload deliberately omitted → defaults to True
    }
    watcher = _make_instrument_watcher(config)
    upload_rv = _make_upload_result(str(tmp_path / "incoming" / "bar.d"))
    ssh_rv = {"status": "submitted", "job_id": "12345", "raw": "/hive/bar.d", "error": None}

    with _patch_upload_module(upload_rv, ssh_rv) as (upload_mock, ssh_mock), patch(
        "stan.config.resolve_vendor_family",
        return_value=("bruker", "timsTOF"),
    ):
        watcher._on_hive_upload(raw, "qc")

    assert upload_mock.called, "upload_raw_to_incoming should be called"
    assert ssh_mock.called, (
        "submit_one_via_ssh should be called when submit_after_upload is absent (defaults True)"
    )

    categories = [ev[0] for ev in watcher._events]
    assert "hive_upload_done" in categories
    assert "hive_submit_deferred" not in categories, (
        "hive_submit_deferred must NOT appear when submit_after_upload defaults to True"
    )
