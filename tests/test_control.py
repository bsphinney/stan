"""Tests for stan.control — backfill action handlers and whitelist registration."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from stan.control import (
    COMMAND_WHITELIST,
    _action_backfill_features,
    _action_backfill_metrics,
    _action_backfill_peg,
    _action_backfill_tic,
    _action_backfill_window_drift,
    _sanitize_str_arg,
    _spawn_detached,
)


# ── Whitelist registration ────────────────────────────────────────────────

def test_all_backfill_handlers_in_whitelist():
    """Every new backfill action must be registered in COMMAND_WHITELIST."""
    expected = {
        "backfill_metrics",
        "backfill_peg",
        "backfill_window_drift",
        "backfill_tic",
        "backfill_features",
    }
    missing = expected - set(COMMAND_WHITELIST.keys())
    assert not missing, f"Missing from COMMAND_WHITELIST: {missing}"


def test_whitelist_handler_references():
    """Whitelist entries for new handlers point to the correct functions."""
    assert COMMAND_WHITELIST["backfill_metrics"] is _action_backfill_metrics
    assert COMMAND_WHITELIST["backfill_peg"] is _action_backfill_peg
    assert COMMAND_WHITELIST["backfill_window_drift"] is _action_backfill_window_drift
    assert COMMAND_WHITELIST["backfill_tic"] is _action_backfill_tic
    assert COMMAND_WHITELIST["backfill_features"] is _action_backfill_features


# ── _sanitize_str_arg ─────────────────────────────────────────────────────

@pytest.mark.parametrize("value", [
    "lumosRox",
    "timsTOF HT",
    "instrument-name_123",
    "Exploris 480",
])
def test_sanitize_str_arg_valid(value):
    assert _sanitize_str_arg(value) == value


@pytest.mark.parametrize("bad", [
    "rm -rf /; echo pwned",
    "foo|bar",
    "a&b",
    "$(evil)",
    "`cmd`",
    "new\nline",
    "carriage\rreturn",
    123,          # not a string
    None,
])
def test_sanitize_str_arg_rejects_metacharacters(bad):
    assert _sanitize_str_arg(bad) is None


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture()
def mock_popen():
    """Patch subprocess.Popen so no real process is spawned."""
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    with patch("stan.control.subprocess") as mock_subprocess:
        mock_subprocess.Popen.return_value = mock_proc
        mock_subprocess.DEVNULL = -1
        mock_subprocess.DETACHED_PROCESS = 8
        mock_subprocess.CREATE_NEW_PROCESS_GROUP = 512
        yield mock_subprocess


@pytest.fixture()
def no_baseline():
    """Patch _baseline_in_progress to return None (no active baseline)."""
    with patch("stan.control._baseline_in_progress", return_value=None):
        yield


@pytest.fixture()
def active_baseline():
    """Make _baseline_in_progress report an active baseline (age=30s)."""
    with patch("stan.control._baseline_in_progress", return_value=30.0):
        yield


# ── _spawn_detached ───────────────────────────────────────────────────────

def test_spawn_detached_returns_pid(tmp_path, mock_popen):
    log_path = tmp_path / "test.log"
    pid = _spawn_detached([["stan", "backfill-metrics"]], log_path)
    assert pid == 12345
    assert mock_popen.Popen.called


# ── backfill_metrics ──────────────────────────────────────────────────────

def test_backfill_metrics_empty_args(no_baseline, mock_popen, tmp_path):
    result = _action_backfill_metrics({})
    assert result["status"] == "started"
    assert result["pid"] == 12345
    assert "backfill_metrics_" in result["log_path"]
    assert result["cmd"] == "stan backfill-metrics"


def test_backfill_metrics_all_flags(no_baseline, mock_popen, tmp_path):
    result = _action_backfill_metrics(
        {"push": True, "dry_run": True, "force": True, "only": "lumosRox"}
    )
    assert result["status"] == "started"
    cmd = result["cmd"]
    assert "--force" in cmd
    assert "--push" in cmd
    assert "--dry-run" in cmd
    assert "--only=lumosRox" in cmd


def test_backfill_metrics_rejects_metachar_only(no_baseline, mock_popen):
    result = _action_backfill_metrics({"only": "foo;bar"})
    assert result.get("error") == "invalid arg"
    assert result.get("field") == "only"


def test_backfill_metrics_blocks_when_baseline_active(active_baseline, mock_popen):
    result = _action_backfill_metrics({})
    assert "error" in result
    assert "baseline in progress" in result["error"]


def test_backfill_metrics_force_bypasses_baseline(active_baseline, mock_popen):
    result = _action_backfill_metrics({"force": True})
    assert result["status"] == "started"


# ── backfill_peg ──────────────────────────────────────────────────────────

def test_backfill_peg_empty_args(no_baseline, mock_popen):
    result = _action_backfill_peg({})
    assert result["status"] == "started"
    assert result["cmd"] == "stan backfill-peg"


def test_backfill_peg_with_instrument(no_baseline, mock_popen):
    result = _action_backfill_peg({"instrument": "lumosRox", "force": True})
    assert "--instrument=lumosRox" in result["cmd"]
    assert "--force" in result["cmd"]


def test_backfill_peg_rejects_metachar_instrument(no_baseline, mock_popen):
    result = _action_backfill_peg({"instrument": "rm -rf /|evil"})
    assert result.get("error") == "invalid arg"
    assert result.get("field") == "instrument"


def test_backfill_peg_blocks_when_baseline_active(active_baseline, mock_popen):
    result = _action_backfill_peg({})
    assert "error" in result
    assert "baseline in progress" in result["error"]


def test_backfill_peg_force_bypasses_baseline(active_baseline, mock_popen):
    result = _action_backfill_peg({"force": True})
    assert result["status"] == "started"


# ── backfill_window_drift ─────────────────────────────────────────────────

def test_backfill_window_drift_empty_args(no_baseline, mock_popen):
    result = _action_backfill_window_drift({})
    assert result["status"] == "started"
    assert result["cmd"] == "stan backfill-window-drift"


def test_backfill_window_drift_with_instrument(no_baseline, mock_popen):
    result = _action_backfill_window_drift({"instrument": "TIMS-10878", "force": True})
    assert "--instrument=TIMS-10878" in result["cmd"]
    assert "--force" in result["cmd"]


def test_backfill_window_drift_rejects_metachar_instrument(no_baseline, mock_popen):
    result = _action_backfill_window_drift({"instrument": "a$b"})
    assert result.get("error") == "invalid arg"


def test_backfill_window_drift_blocks_when_baseline_active(active_baseline, mock_popen):
    result = _action_backfill_window_drift({})
    assert "baseline in progress" in result["error"]


def test_backfill_window_drift_force_bypasses_baseline(active_baseline, mock_popen):
    result = _action_backfill_window_drift({"force": True})
    assert result["status"] == "started"


# ── backfill_tic ──────────────────────────────────────────────────────────

def test_backfill_tic_empty_args(no_baseline, mock_popen):
    result = _action_backfill_tic({})
    assert result["status"] == "started"
    assert result["cmd"] == "stan backfill-tic"


def test_backfill_tic_all_flags(no_baseline, mock_popen):
    result = _action_backfill_tic({"push": True, "force": True, "really_force": True})
    cmd = result["cmd"]
    assert "--force" in cmd
    assert "--push" in cmd
    assert "--really-force" in cmd


def test_backfill_tic_blocks_when_baseline_active(active_baseline, mock_popen):
    result = _action_backfill_tic({})
    assert "baseline in progress" in result["error"]


def test_backfill_tic_force_bypasses_baseline(active_baseline, mock_popen):
    result = _action_backfill_tic({"force": True})
    assert result["status"] == "started"


# ── backfill_features ─────────────────────────────────────────────────────

def test_backfill_features_empty_args(no_baseline, mock_popen):
    result = _action_backfill_features({})
    assert result["status"] == "started"
    assert result["cmd"] == "stan backfill-features"


def test_backfill_features_with_limit(no_baseline, mock_popen):
    result = _action_backfill_features({"limit": 50})
    assert "--limit" in result["cmd"]
    assert "50" in result["cmd"]


def test_backfill_features_with_force(no_baseline, mock_popen):
    result = _action_backfill_features({"force": True})
    assert "--force" in result["cmd"]


def test_backfill_features_invalid_limit(no_baseline, mock_popen):
    result = _action_backfill_features({"limit": "not_a_number"})
    assert result.get("error") == "invalid arg"
    assert result.get("field") == "limit"


def test_backfill_features_blocks_when_baseline_active(active_baseline, mock_popen):
    result = _action_backfill_features({})
    assert "baseline in progress" in result["error"]


def test_backfill_features_force_bypasses_baseline(active_baseline, mock_popen):
    result = _action_backfill_features({"force": True})
    assert result["status"] == "started"
