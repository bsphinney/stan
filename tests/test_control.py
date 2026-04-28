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
    _action_screencap_now,
    _action_screencap_status,
    _action_start_screencap,
    _action_stop_screencap,
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


# ── screencap_now ─────────────────────────────────────────────────────────

def test_screencap_now_in_whitelist():
    assert "screencap_now" in COMMAND_WHITELIST
    assert COMMAND_WHITELIST["screencap_now"] is _action_screencap_now


def test_screencap_now_callable_with_empty_args(tmp_path):
    """Handler runs with {} args (capture succeeds → returns path dict)."""
    from pathlib import Path as _Path
    from stan.screencap import ScreencapConfig

    fake_path = tmp_path / "20260427" / "120000.jpg"
    fake_path.parent.mkdir(parents=True, exist_ok=True)
    fake_path.write_bytes(b"fake")

    cfg = ScreencapConfig(enabled=True, local_dir=tmp_path)
    with (
        patch("stan.control._action_screencap_now.__module__", "stan.control"),
        patch("stan.screencap.load_screencap_config", return_value=cfg),
        patch("stan.screencap.capture_now", return_value=fake_path),
    ):
        result = _action_screencap_now({})

    assert "path" in result
    assert "captured_at" in result
    assert result["path"] == str(fake_path)


def test_screencap_now_returns_error_when_capture_fails(tmp_path):
    """capture_now returning None yields the error dict."""
    from stan.screencap import ScreencapConfig

    cfg = ScreencapConfig(enabled=True, local_dir=tmp_path)
    with (
        patch("stan.screencap.load_screencap_config", return_value=cfg),
        patch("stan.screencap.capture_now", return_value=None),
    ):
        result = _action_screencap_now({})

    assert "error" in result
    assert "locked" in result["error"] or "failed" in result["error"]


def test_screencap_now_rejects_metachar_run_name():
    """run_name with shell metacharacters is rejected."""
    result = _action_screencap_now({"run_name": "rm -rf /; evil"})
    assert result.get("error") == "invalid arg"
    assert result.get("field") == "run_name"


def test_screencap_now_passes_run_name(tmp_path):
    """A clean run_name is forwarded to capture_now."""
    from pathlib import Path as _Path
    from stan.screencap import ScreencapConfig

    fake_path = tmp_path / "20260427" / "120000_runend_MyRun.jpg"
    fake_path.parent.mkdir(parents=True, exist_ok=True)
    fake_path.write_bytes(b"fake")

    cfg = ScreencapConfig(enabled=True, local_dir=tmp_path)
    captured_kwargs: dict = {}

    def mock_capture(config, *, run_name=None):
        captured_kwargs["run_name"] = run_name
        return fake_path

    with (
        patch("stan.screencap.load_screencap_config", return_value=cfg),
        patch("stan.screencap.capture_now", side_effect=mock_capture),
    ):
        result = _action_screencap_now({"run_name": "MyRun"})

    assert captured_kwargs["run_name"] == "MyRun"
    assert "path" in result


# ── start_screencap ───────────────────────────────────────────────────────

def test_start_screencap_in_whitelist():
    assert "start_screencap" in COMMAND_WHITELIST
    assert COMMAND_WHITELIST["start_screencap"] is _action_start_screencap


def test_start_screencap_spawns_daemon(tmp_path, mock_popen):
    """start_screencap writes PID file and returns started status."""
    from stan.screencap import ScreencapConfig

    cfg = ScreencapConfig(enabled=True, local_dir=tmp_path)
    pid_file = tmp_path / "screencap_daemon.pid"

    with (
        patch("stan.screencap.load_screencap_config", return_value=cfg),
        patch("pathlib.Path.home", return_value=tmp_path),
    ):
        result = _action_start_screencap({})

    assert result.get("status") == "started"
    assert result.get("pid") == 12345


def test_start_screencap_refuses_when_disabled(tmp_path):
    """start_screencap returns error when config.enabled is False."""
    from stan.screencap import ScreencapConfig

    cfg = ScreencapConfig(enabled=False, local_dir=tmp_path)
    with patch("stan.screencap.load_screencap_config", return_value=cfg):
        result = _action_start_screencap({})

    assert "error" in result
    assert "disabled" in result["error"]
    assert "hint" in result


def test_start_screencap_refuses_when_already_running(tmp_path, mock_popen):
    """start_screencap refuses if PID file exists with live PID."""
    from stan.screencap import ScreencapConfig

    cfg = ScreencapConfig(enabled=True, local_dir=tmp_path)
    pid_file = tmp_path / "STAN" / "screencap_daemon.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text("99999", encoding="utf-8")

    with (
        patch("stan.screencap.load_screencap_config", return_value=cfg),
        patch("pathlib.Path.home", return_value=tmp_path),
        # Make os.kill(99999, 0) succeed — process appears alive
        patch("stan.control.os.kill", return_value=None),
    ):
        result = _action_start_screencap({})

    assert "error" in result
    assert "already running" in result["error"]


# ── stop_screencap ────────────────────────────────────────────────────────

def test_stop_screencap_in_whitelist():
    assert "stop_screencap" in COMMAND_WHITELIST
    assert COMMAND_WHITELIST["stop_screencap"] is _action_stop_screencap


def test_stop_screencap_returns_not_running_when_no_pid_file(tmp_path):
    """Returns not_running when PID file is absent."""
    with patch("pathlib.Path.home", return_value=tmp_path):
        result = _action_stop_screencap({})

    assert result.get("status") == "not_running"


def test_stop_screencap_stops_process(tmp_path):
    """Sends SIGTERM, deletes PID file, returns stopped."""
    import platform

    pid_file = tmp_path / "STAN" / "screencap_daemon.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text("77777", encoding="utf-8")

    kill_calls: list = []

    def fake_kill(pid, sig):
        kill_calls.append((pid, sig))
        if sig == 0:
            # First call (os.kill(pid, 0) in wait loop) — raise so loop exits
            raise ProcessLookupError

    with (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("stan.control.os.kill", side_effect=fake_kill),
        patch("stan.control.platform.system", return_value="Linux"),
    ):
        result = _action_stop_screencap({})

    assert result.get("status") == "stopped"
    assert result.get("pid") == 77777
    assert not pid_file.exists()


# ── screencap_status ──────────────────────────────────────────────────────

def test_screencap_status_in_whitelist():
    assert "screencap_status" in COMMAND_WHITELIST
    assert COMMAND_WHITELIST["screencap_status"] is _action_screencap_status


def test_screencap_status_callable_with_empty_args(tmp_path):
    """Returns expected keys with no daemon running."""
    from stan.screencap import ScreencapConfig

    cfg = ScreencapConfig(enabled=True, local_dir=tmp_path)

    with (
        patch("stan.screencap.load_screencap_config", return_value=cfg),
        patch("pathlib.Path.home", return_value=tmp_path),
    ):
        result = _action_screencap_status({})

    assert "daemon_running" in result
    assert "pid" in result
    assert "config" in result
    assert "recent_count" in result
    assert "disk_used_mb" in result


def test_screencap_status_reports_daemon_not_running(tmp_path):
    """daemon_running=False when no PID file exists."""
    from stan.screencap import ScreencapConfig

    cfg = ScreencapConfig(enabled=True, local_dir=tmp_path)

    with (
        patch("stan.screencap.load_screencap_config", return_value=cfg),
        patch("pathlib.Path.home", return_value=tmp_path),
    ):
        result = _action_screencap_status({})

    assert result["daemon_running"] is False
    assert result["pid"] is None


def test_screencap_status_config_keys(tmp_path):
    """Config sub-dict contains expected keys."""
    from stan.screencap import ScreencapConfig

    cfg = ScreencapConfig(
        enabled=True,
        heartbeat_min=10,
        on_acquisition_end=True,
        local_dir=tmp_path,
        mask_regions=[{"x": 0, "y": 0, "w": 10, "h": 10}],
    )

    with (
        patch("stan.screencap.load_screencap_config", return_value=cfg),
        patch("pathlib.Path.home", return_value=tmp_path),
    ):
        result = _action_screencap_status({})

    c = result["config"]
    assert c["enabled"] is True
    assert c["heartbeat_min"] == 10
    assert c["on_acquisition_end"] is True
    assert c["mask_count"] == 1
    assert "local_dir" in c
