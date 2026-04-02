"""Tests for configuration loading and hot-reload."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import yaml

from stan.config import ConfigWatcher, load_yaml, resolve_config_path


def test_load_yaml(tmp_path: Path) -> None:
    """load_yaml should parse a YAML file into a dict."""
    config = {"key": "value", "nested": {"a": 1}}
    path = tmp_path / "test.yml"
    path.write_text(yaml.dump(config))

    result = load_yaml(path)
    assert result == config


def test_load_yaml_empty(tmp_path: Path) -> None:
    """load_yaml should return empty dict for empty YAML file."""
    path = tmp_path / "empty.yml"
    path.write_text("")

    result = load_yaml(path)
    assert result == {}


def test_resolve_config_path_user_dir(tmp_path: Path) -> None:
    """User config dir (~/.stan/) should take priority over package config/."""
    user_dir = tmp_path / ".stan"
    user_dir.mkdir()
    user_file = user_dir / "instruments.yml"
    user_file.write_text("instruments: []")

    with patch("stan.config._USER_CONFIG_DIR", user_dir):
        path = resolve_config_path("instruments.yml")
        assert path == user_file


def test_resolve_config_path_fallback(tmp_path: Path) -> None:
    """Should fall back to package config/ when user dir doesn't have the file."""
    empty_user_dir = tmp_path / ".stan_empty"
    empty_user_dir.mkdir()

    package_dir = tmp_path / "config"
    package_dir.mkdir()
    package_file = package_dir / "instruments.yml"
    package_file.write_text("instruments: []")

    with (
        patch("stan.config._USER_CONFIG_DIR", empty_user_dir),
        patch("stan.config._PACKAGE_CONFIG_DIR", package_dir),
    ):
        path = resolve_config_path("instruments.yml")
        assert path == package_file


def test_resolve_config_path_not_found(tmp_path: Path) -> None:
    """Should raise FileNotFoundError when config file not found anywhere."""
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()

    import pytest

    with (
        patch("stan.config._USER_CONFIG_DIR", empty_dir),
        patch("stan.config._PACKAGE_CONFIG_DIR", empty_dir),
    ):
        with pytest.raises(FileNotFoundError):
            resolve_config_path("nonexistent.yml")


def test_config_watcher_detects_change(tmp_path: Path) -> None:
    """ConfigWatcher should detect file mtime changes."""
    path = tmp_path / "config.yml"
    path.write_text(yaml.dump({"version": 1}))

    watcher = ConfigWatcher(path)
    assert watcher.data == {"version": 1}
    assert watcher.is_stale() is False

    # Simulate file modification (touch with new mtime)
    time.sleep(0.05)
    path.write_text(yaml.dump({"version": 2}))

    assert watcher.is_stale() is True

    # Reload should update data and clear staleness
    watcher.reload()
    assert watcher.data == {"version": 2}
    assert watcher.is_stale() is False


def test_config_watcher_initial_load(tmp_path: Path) -> None:
    """ConfigWatcher should load data on construction."""
    path = tmp_path / "config.yml"
    path.write_text(yaml.dump({"instruments": [{"name": "test"}]}))

    watcher = ConfigWatcher(path)
    assert "instruments" in watcher.data
    assert watcher.data["instruments"][0]["name"] == "test"
