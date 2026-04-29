"""Tests for stan.keep_awake — Windows keep-awake helper."""
from __future__ import annotations

import logging
import platform
from unittest.mock import MagicMock, patch

from stan.keep_awake import (
    ES_CONTINUOUS,
    ES_DISPLAY_REQUIRED,
    ES_SYSTEM_REQUIRED,
    keep_awake,
    release_awake,
)


# ---------------------------------------------------------------------------
# Non-Windows (current dev machine — macOS/Linux)
# ---------------------------------------------------------------------------

def test_keep_awake_returns_false_on_non_windows():
    """keep_awake() must be a no-op on non-Windows platforms."""
    assert platform.system() != "Windows", "This test only runs on non-Windows"
    assert keep_awake() is False


def test_release_awake_is_noop_on_non_windows():
    """release_awake() must not raise on non-Windows platforms."""
    assert platform.system() != "Windows", "This test only runs on non-Windows"
    release_awake()  # should not raise


# ---------------------------------------------------------------------------
# Windows (mocked)
# ---------------------------------------------------------------------------

def _make_ctypes_mock(return_value: int) -> MagicMock:
    """Build a minimal ctypes mock with windll.kernel32.SetThreadExecutionState."""
    mock_ctypes = MagicMock()
    mock_ctypes.windll.kernel32.SetThreadExecutionState.return_value = return_value
    return mock_ctypes


def test_keep_awake_returns_true_on_windows_success(monkeypatch):
    """keep_awake() returns True when SetThreadExecutionState succeeds (non-zero)."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    mock_ctypes = _make_ctypes_mock(return_value=1)
    with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
        result = keep_awake()

    assert result is True
    expected_flags = ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED
    mock_ctypes.windll.kernel32.SetThreadExecutionState.assert_called_once_with(expected_flags)


def test_keep_awake_calls_correct_flags(monkeypatch):
    """keep_awake() passes ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    mock_ctypes = _make_ctypes_mock(return_value=0xFFFFFFFF)
    with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
        keep_awake()

    call_args = mock_ctypes.windll.kernel32.SetThreadExecutionState.call_args[0][0]
    assert call_args & ES_CONTINUOUS
    assert call_args & ES_DISPLAY_REQUIRED
    assert call_args & ES_SYSTEM_REQUIRED


def test_keep_awake_returns_false_when_api_returns_zero(monkeypatch, caplog):
    """keep_awake() returns False and logs a warning when SetThreadExecutionState returns 0."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    mock_ctypes = _make_ctypes_mock(return_value=0)
    with caplog.at_level(logging.WARNING, logger="stan.keep_awake"):
        with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
            result = keep_awake()

    assert result is False
    assert any("SetThreadExecutionState returned 0" in r.message for r in caplog.records)


def test_release_awake_calls_es_continuous_on_windows(monkeypatch):
    """release_awake() calls SetThreadExecutionState(ES_CONTINUOUS) to restore defaults."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    mock_ctypes = _make_ctypes_mock(return_value=1)
    with patch.dict("sys.modules", {"ctypes": mock_ctypes}):
        release_awake()

    mock_ctypes.windll.kernel32.SetThreadExecutionState.assert_called_once_with(ES_CONTINUOUS)


def test_keep_awake_returns_false_on_ctypes_exception(monkeypatch, caplog):
    """keep_awake() returns False and logs a warning if ctypes raises."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    bad_ctypes = MagicMock()
    bad_ctypes.windll.kernel32.SetThreadExecutionState.side_effect = OSError("boom")
    with caplog.at_level(logging.WARNING, logger="stan.keep_awake"):
        with patch.dict("sys.modules", {"ctypes": bad_ctypes}):
            result = keep_awake()

    assert result is False
    assert any("keep-awake unavailable" in r.message for r in caplog.records)


def test_release_awake_swallows_exception_on_windows(monkeypatch):
    """release_awake() must not propagate exceptions even if ctypes raises."""
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    bad_ctypes = MagicMock()
    bad_ctypes.windll.kernel32.SetThreadExecutionState.side_effect = OSError("boom")
    with patch.dict("sys.modules", {"ctypes": bad_ctypes}):
        release_awake()  # must not raise
