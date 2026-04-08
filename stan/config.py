"""Central configuration loader with hot-reload support."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

CONFIG_POLL_INTERVAL = 30  # seconds between mtime checks

# Package-level config/ directory (fallback)
_PACKAGE_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

# User config directory — visible location on Windows, hidden on Unix
import platform as _plat
if _plat.system() == "Windows":
    _USER_CONFIG_DIR = Path.home() / "STAN"
else:
    _USER_CONFIG_DIR = Path.home() / ".stan"




def resolve_config_path(filename: str) -> Path:
    """Resolve config file path: ~/STAN/ (or ~/.stan/) first, then package config/ fallback."""
    user_path = _USER_CONFIG_DIR / filename
    if user_path.exists():
        return user_path
    # Fallback: check old .stan directory on Windows
    if _plat.system() == "Windows":
        old_path = Path.home() / ".stan" / filename
        if old_path.exists():
            return old_path
    package_path = _PACKAGE_CONFIG_DIR / filename
    if package_path.exists():
        return package_path
    raise FileNotFoundError(
        f"Config file '{filename}' not found in {_USER_CONFIG_DIR} or {_PACKAGE_CONFIG_DIR}"
    )


def load_yaml(path: Path) -> dict:
    """Load a YAML file and return its contents as a dict."""
    with open(path) as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    return data


class ConfigWatcher:
    """Watches a config file for changes via mtime polling."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._last_mtime: float = 0.0
        self._data: dict = {}
        self.reload()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def data(self) -> dict:
        return self._data

    def is_stale(self) -> bool:
        """Check if the file has been modified since last reload."""
        try:
            current_mtime = self._path.stat().st_mtime
            return current_mtime != self._last_mtime
        except OSError:
            return False

    def reload(self) -> dict:
        """Reload the config file and update internal state."""
        try:
            self._data = load_yaml(self._path)
            self._last_mtime = self._path.stat().st_mtime
            logger.info("Loaded config: %s", self._path)
        except Exception:
            logger.exception("Failed to reload config: %s", self._path)
        return self._data


def load_instruments() -> tuple[dict, list[dict]]:
    """Load instruments.yml. Returns (hive_config, instruments_list)."""
    path = resolve_config_path("instruments.yml")
    data = load_yaml(path)
    hive = data.get("hive", {})
    instruments = data.get("instruments", [])
    return hive, instruments


def load_thresholds() -> dict:
    """Load thresholds.yml. Returns the thresholds dict keyed by model name.

    Returns empty dict if thresholds.yml doesn't exist — gating will
    default to PASS for all runs until thresholds are configured.
    """
    try:
        path = resolve_config_path("thresholds.yml")
        data = load_yaml(path)
        return data.get("thresholds", {})
    except FileNotFoundError:
        logger.debug("thresholds.yml not found — all runs will pass gating")
        return {}


def load_community() -> dict:
    """Load community.yml."""
    path = resolve_config_path("community.yml")
    return load_yaml(path)


def get_default_config_dir() -> Path:
    """Return the package-level config/ directory path."""
    return _PACKAGE_CONFIG_DIR


def get_user_config_dir() -> Path:
    """Return the user config directory path (~/.stan/)."""
    return _USER_CONFIG_DIR
