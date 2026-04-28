"""Tests for stan.screencap — capture engine, masking, pruning, and CLI commands.

All tests use heavy mocking; no actual screen capture happens.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# load_screencap_config
# ---------------------------------------------------------------------------


def test_load_screencap_config_defaults_when_missing(tmp_path: Path) -> None:
    """Returns all-default ScreencapConfig when the YAML file doesn't exist."""
    from stan.screencap import ScreencapConfig, load_screencap_config

    missing = tmp_path / "screencap.yml"
    cfg = load_screencap_config(path=missing)

    assert isinstance(cfg, ScreencapConfig)
    assert cfg.enabled is False
    assert cfg.heartbeat_min == 15
    assert cfg.on_acquisition_end is True
    assert cfg.window_titles == []
    assert cfg.mask_regions == []
    assert cfg.quality == 80
    assert cfg.max_dimension == 1280
    assert cfg.local_retention_days == 7
    assert cfg.mirror_retention_hours == 48


def test_load_screencap_config_parses_yaml(tmp_path: Path) -> None:
    """Parses a complete screencap.yml correctly."""
    from stan.screencap import load_screencap_config

    cfg_file = tmp_path / "screencap.yml"
    data = {
        "enabled": True,
        "heartbeat_min": 30,
        "on_acquisition_end": False,
        "window_titles": ["timsControl", "HyStar"],
        "fallback_full_screen": False,
        "mask_regions": [{"x": 0, "y": 1040, "w": 1920, "h": 40}],
        "quality": 70,
        "max_dimension": 800,
        "local_retention_days": 14,
        "mirror_retention_hours": 24,
    }
    cfg_file.write_text(yaml.dump(data), encoding="utf-8")

    cfg = load_screencap_config(path=cfg_file)

    assert cfg.enabled is True
    assert cfg.heartbeat_min == 30
    assert cfg.on_acquisition_end is False
    assert cfg.window_titles == ["timsControl", "HyStar"]
    assert cfg.fallback_full_screen is False
    assert cfg.mask_regions == [{"x": 0, "y": 1040, "w": 1920, "h": 40}]
    assert cfg.quality == 70
    assert cfg.max_dimension == 800
    assert cfg.local_retention_days == 14
    assert cfg.mirror_retention_hours == 24


def test_load_screencap_config_partial_yaml(tmp_path: Path) -> None:
    """Partial YAML fills remaining keys from defaults."""
    from stan.screencap import load_screencap_config

    cfg_file = tmp_path / "screencap.yml"
    cfg_file.write_text(yaml.dump({"enabled": True, "heartbeat_min": 5}), encoding="utf-8")

    cfg = load_screencap_config(path=cfg_file)
    assert cfg.enabled is True
    assert cfg.heartbeat_min == 5
    assert cfg.quality == 80  # default


# ---------------------------------------------------------------------------
# Golden-image mask test
# ---------------------------------------------------------------------------


def test_apply_masks_golden_image() -> None:
    """Masking a known red image with one region produces solid black in that region."""
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("Pillow not installed")

    from stan.screencap import _apply_masks

    # 100x100 solid red image.
    img = Image.new("RGB", (100, 100), color=(255, 0, 0))
    mask_regions = [{"x": 10, "y": 10, "w": 20, "h": 20}]

    result = _apply_masks(img, mask_regions)

    # The masked rectangle (10,10) to (29,29) must be solid black.
    for px in range(10, 30):
        for py in range(10, 30):
            assert result.getpixel((px, py)) == (0, 0, 0), (
                f"Pixel ({px},{py}) should be black but is {result.getpixel((px, py))}"
            )

    # Pixels outside the mask must remain red.
    assert result.getpixel((0, 0)) == (255, 0, 0)
    assert result.getpixel((99, 99)) == (255, 0, 0)
    assert result.getpixel((50, 50)) == (255, 0, 0)


def test_apply_masks_noop_when_empty() -> None:
    """Empty mask_regions list leaves image unchanged."""
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("Pillow not installed")

    from stan.screencap import _apply_masks

    img = Image.new("RGB", (50, 50), color=(0, 128, 255))
    result = _apply_masks(img, [])
    assert result.getpixel((25, 25)) == (0, 128, 255)


# ---------------------------------------------------------------------------
# Lockscreen skip
# ---------------------------------------------------------------------------


def test_capture_now_skips_locked_screen(tmp_path: Path) -> None:
    """capture_now returns None when the captured image is nearly black."""
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("Pillow not installed")

    from stan.screencap import ScreencapConfig, capture_now

    cfg = ScreencapConfig(enabled=True, local_dir=tmp_path, fallback_full_screen=True)
    black_img = Image.new("RGB", (100, 100), color=(0, 0, 0))

    # Patch _grab_fullscreen to return a fake mss shot, then _mss_to_pil to
    # return the black PIL image.
    fake_shot = MagicMock()
    with (
        patch("stan.screencap._grab_window", return_value=None),
        patch("stan.screencap._grab_fullscreen", return_value=fake_shot),
        patch("stan.screencap._mss_to_pil", return_value=black_img),
    ):
        result = capture_now(cfg)

    assert result is None, "capture_now should return None for a locked (black) screen"


# ---------------------------------------------------------------------------
# Disabled config
# ---------------------------------------------------------------------------


def test_capture_now_returns_none_when_disabled(tmp_path: Path) -> None:
    """capture_now returns None immediately when config.enabled is False."""
    from stan.screencap import ScreencapConfig, capture_now

    cfg = ScreencapConfig(enabled=False, local_dir=tmp_path)

    with (
        patch("stan.screencap._grab_fullscreen") as mock_grab,
        patch("stan.screencap._grab_window") as mock_win,
    ):
        result = capture_now(cfg)

    assert result is None
    mock_grab.assert_not_called()
    mock_win.assert_not_called()


# ---------------------------------------------------------------------------
# Filename format
# ---------------------------------------------------------------------------


def test_capture_now_heartbeat_filename_format(tmp_path: Path) -> None:
    """Heartbeat frame is saved under <YYYYMMDD>/<HHMMSS>.jpg."""
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("Pillow not installed")

    from stan.screencap import ScreencapConfig, capture_now

    cfg = ScreencapConfig(enabled=True, local_dir=tmp_path, fallback_full_screen=True)
    bright_img = Image.new("RGB", (100, 100), color=(200, 200, 200))
    fake_shot = MagicMock()

    with (
        patch("stan.screencap._grab_window", return_value=None),
        patch("stan.screencap._grab_fullscreen", return_value=fake_shot),
        patch("stan.screencap._mss_to_pil", return_value=bright_img),
    ):
        path = capture_now(cfg)

    assert path is not None
    # Parent dir must match YYYYMMDD
    date_dir = path.parent.name
    assert len(date_dir) == 8 and date_dir.isdigit(), f"Expected YYYYMMDD dir, got {date_dir!r}"
    # Filename must match HHMMSS.jpg (no _runend_ marker)
    assert path.name.endswith(".jpg")
    assert "_runend_" not in path.name, "Heartbeat frame must not have _runend_ marker"
    stem = path.stem
    assert len(stem) == 6 and stem.isdigit(), f"Expected HHMMSS stem, got {stem!r}"


def test_capture_now_runend_filename_format(tmp_path: Path) -> None:
    """Run-end frame is saved with _runend_<run_name> marker in filename."""
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("Pillow not installed")

    from stan.screencap import ScreencapConfig, capture_now

    cfg = ScreencapConfig(enabled=True, local_dir=tmp_path, fallback_full_screen=True)
    bright_img = Image.new("RGB", (100, 100), color=(200, 200, 200))
    fake_shot = MagicMock()

    with (
        patch("stan.screencap._grab_window", return_value=None),
        patch("stan.screencap._grab_fullscreen", return_value=fake_shot),
        patch("stan.screencap._mss_to_pil", return_value=bright_img),
    ):
        path = capture_now(cfg, run_name="20240101_HeLa_001")

    assert path is not None
    assert "_runend_" in path.name, f"Expected _runend_ in filename, got {path.name!r}"
    assert "20240101_HeLa_001" in path.name or "20240101_HeLa_001" in path.stem


# ---------------------------------------------------------------------------
# Cadence / daemon logic
# ---------------------------------------------------------------------------


def test_run_daemon_one_tick(tmp_path: Path) -> None:
    """run_daemon captures exactly once per heartbeat cycle (one tick test)."""
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("Pillow not installed")

    from stan.screencap import ScreencapConfig, run_daemon

    cfg = ScreencapConfig(
        enabled=True,
        local_dir=tmp_path,
        heartbeat_min=1,
        fallback_full_screen=True,
    )

    bright_img = Image.new("RGB", (100, 100), color=(200, 200, 200))
    fake_shot = MagicMock()
    stop = threading.Event()
    captured_paths: list[Path] = []

    def mock_capture(config, *, run_name=None):
        # Record and then set the stop event so daemon exits after one capture.
        from stan.screencap import _save_frame
        result = _save_frame(
            bright_img.copy(),
            tmp_path,
            run_name,
            config.quality,
            config.max_dimension,
        )
        captured_paths.append(result)
        stop.set()
        return result

    with (
        patch("stan.screencap._grab_window", return_value=None),
        patch("stan.screencap._grab_fullscreen", return_value=fake_shot),
        patch("stan.screencap._mss_to_pil", return_value=bright_img),
        patch("stan.screencap.capture_now", side_effect=mock_capture),
    ):
        run_daemon(cfg, stop_event=stop)

    assert len(captured_paths) == 1, (
        f"Expected exactly 1 capture after one tick, got {len(captured_paths)}"
    )


# ---------------------------------------------------------------------------
# prune_screencaps
# ---------------------------------------------------------------------------


def _write_fake_jpg(path: Path) -> None:
    """Write a minimal valid JPEG-ish file (just needs to exist for stat)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)  # fake JPEG header


def test_prune_screencaps_removes_old_heartbeat(tmp_path: Path) -> None:
    """Old heartbeat files are deleted from local_dir."""
    import os

    from stan.screencap import ScreencapConfig, prune_screencaps

    local_dir = tmp_path / "local"
    cfg = ScreencapConfig(local_retention_days=7, mirror_retention_hours=48, local_dir=local_dir)

    # Old heartbeat file (10 days ago)
    old_file = local_dir / "20240101" / "120000.jpg"
    _write_fake_jpg(old_file)
    old_time = (datetime.now() - timedelta(days=10)).timestamp()
    os.utime(old_file, (old_time, old_time))

    # Recent heartbeat file (1 hour ago)
    recent_file = local_dir / "20240110" / "120000.jpg"
    _write_fake_jpg(recent_file)

    result = prune_screencaps(local_dir, None, cfg)

    assert result["local_deleted"] == 1
    assert not old_file.exists(), "Old heartbeat file should be deleted"
    assert recent_file.exists(), "Recent heartbeat file should be kept"


def test_prune_screencaps_retains_runend_in_mirror(tmp_path: Path) -> None:
    """Run-end frames in mirror_dir are retained regardless of age."""
    import os

    from stan.screencap import ScreencapConfig, _RUNEND_MARKER, prune_screencaps

    local_dir = tmp_path / "local"
    mirror_dir = tmp_path / "mirror"
    cfg = ScreencapConfig(
        local_retention_days=7,
        mirror_retention_hours=48,
        local_dir=local_dir,
        mirror_dir=mirror_dir,
    )

    # Old run-end file in mirror (100 days ago)
    old_runend = mirror_dir / "20240101" / f"120000{_RUNEND_MARKER}MyRun.jpg"
    _write_fake_jpg(old_runend)
    old_time = (datetime.now() - timedelta(days=100)).timestamp()
    os.utime(old_runend, (old_time, old_time))

    # Old heartbeat file in mirror (also 100 days ago — should be deleted)
    old_hb = mirror_dir / "20240101" / "130000.jpg"
    _write_fake_jpg(old_hb)
    os.utime(old_hb, (old_time, old_time))

    result = prune_screencaps(local_dir, mirror_dir, cfg)

    assert old_runend.exists(), "Run-end frame must be retained in mirror regardless of age"
    assert not old_hb.exists(), "Old heartbeat frame must be pruned from mirror"
    assert result["mirror_retained_runend"] == 1
    assert result["mirror_deleted"] == 1


def test_prune_screencaps_no_mirror(tmp_path: Path) -> None:
    """prune_screencaps handles mirror_dir=None without error."""
    import os

    from stan.screencap import ScreencapConfig, prune_screencaps

    local_dir = tmp_path / "local"
    cfg = ScreencapConfig(local_retention_days=7, mirror_retention_hours=48, local_dir=local_dir)

    old_file = local_dir / "20240101" / "120000.jpg"
    _write_fake_jpg(old_file)
    old_time = (datetime.now() - timedelta(days=10)).timestamp()
    os.utime(old_file, (old_time, old_time))

    result = prune_screencaps(local_dir, None, cfg)

    assert result["mirror_deleted"] == 0
    assert result["mirror_retained_runend"] == 0
    assert result["local_deleted"] == 1


# ---------------------------------------------------------------------------
# CLI — enabled: false guard on screencap-now
# ---------------------------------------------------------------------------


def test_screencap_now_cli_refuses_when_disabled(tmp_path: Path) -> None:
    """screencap-now CLI exits non-zero with helpful message when disabled."""
    from typer.testing import CliRunner
    from stan.cli import app
    from stan.screencap import ScreencapConfig

    runner = CliRunner()

    disabled_cfg = ScreencapConfig(enabled=False)
    # The command imports load_screencap_config lazily from stan.screencap,
    # so patch at the source module.
    with patch("stan.screencap.load_screencap_config", return_value=disabled_cfg):
        result = runner.invoke(app, ["screencap-now"])

    assert result.exit_code != 0, (
        f"Expected non-zero exit when disabled, got {result.exit_code}. "
        f"Output: {result.output}"
    )
    assert "enabled" in result.output.lower() or "screencap.yml" in result.output, (
        f"Expected helpful message about enabling, got: {result.output!r}"
    )


def test_screencap_daemon_cli_refuses_when_disabled() -> None:
    """screencap-daemon CLI exits non-zero with helpful message when disabled."""
    from typer.testing import CliRunner
    from stan.cli import app
    from stan.screencap import ScreencapConfig

    runner = CliRunner()
    disabled_cfg = ScreencapConfig(enabled=False)

    with patch("stan.screencap.load_screencap_config", return_value=disabled_cfg):
        result = runner.invoke(app, ["screencap-daemon"])

    assert result.exit_code != 0
    assert "enabled" in result.output.lower() or "screencap.yml" in result.output


# ---------------------------------------------------------------------------
# Watcher hook — on_acquisition_end fired after search dispatch
