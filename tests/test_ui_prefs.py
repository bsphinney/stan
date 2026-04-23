"""Tests for the optional ``ui_prefs.yml`` loader used by the dashboard."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml

from stan.config import UI_PREF_KEYS, load_ui_prefs


def test_load_ui_prefs_missing_returns_empty(tmp_path: Path) -> None:
    """When no ui_prefs.yml exists anywhere, load_ui_prefs returns {}."""
    empty_user = tmp_path / ".stan_empty"
    empty_user.mkdir()
    empty_pkg = tmp_path / "config_empty"
    empty_pkg.mkdir()

    with (
        patch("stan.config._USER_CONFIG_DIR", empty_user),
        patch("stan.config._PACKAGE_CONFIG_DIR", empty_pkg),
    ):
        assert load_ui_prefs() == {}


def test_load_ui_prefs_whitelists_keys(tmp_path: Path) -> None:
    """Unknown keys are silently dropped; known keys pass through."""
    user_dir = tmp_path / ".stan"
    user_dir.mkdir()
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()

    content = {
        "front_page_view": "weekly_table",
        "matrix_bar_scale": "week_range",
        "ms1_format": "short",
        "evil_key": "should_be_ignored",
        "rogue_nested": {"a": 1},
    }
    (user_dir / "ui_prefs.yml").write_text(yaml.dump(content))

    with (
        patch("stan.config._USER_CONFIG_DIR", user_dir),
        patch("stan.config._PACKAGE_CONFIG_DIR", pkg_dir),
    ):
        result = load_ui_prefs()

    assert result == {
        "front_page_view": "weekly_table",
        "matrix_bar_scale": "week_range",
        "ms1_format": "short",
    }
    # Every returned key must be in the whitelist.
    assert set(result.keys()) <= set(UI_PREF_KEYS)


def test_load_ui_prefs_empty_yaml_ok(tmp_path: Path) -> None:
    """An empty or non-dict YAML shouldn't crash — just returns {}."""
    user_dir = tmp_path / ".stan"
    user_dir.mkdir()
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (user_dir / "ui_prefs.yml").write_text("")

    with (
        patch("stan.config._USER_CONFIG_DIR", user_dir),
        patch("stan.config._PACKAGE_CONFIG_DIR", pkg_dir),
    ):
        assert load_ui_prefs() == {}


def test_load_ui_prefs_non_dict_yaml_ok(tmp_path: Path) -> None:
    """If someone writes a list instead of a dict, return {} safely."""
    user_dir = tmp_path / ".stan"
    user_dir.mkdir()
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (user_dir / "ui_prefs.yml").write_text("- 1\n- 2\n- 3\n")

    with (
        patch("stan.config._USER_CONFIG_DIR", user_dir),
        patch("stan.config._PACKAGE_CONFIG_DIR", pkg_dir),
    ):
        assert load_ui_prefs() == {}


def test_api_ui_prefs_404_when_absent(tmp_path: Path, monkeypatch) -> None:
    """/api/ui-prefs returns 404 if no ui_prefs.yml — frontend treats
    that as 'fall back to built-in defaults'."""
    from fastapi.testclient import TestClient

    empty_user = tmp_path / ".stan_empty"
    empty_user.mkdir()
    empty_pkg = tmp_path / "pkg"
    empty_pkg.mkdir()

    import stan.config as _cfg
    import stan.dashboard.server as server

    monkeypatch.setattr(_cfg, "_USER_CONFIG_DIR", empty_user)
    monkeypatch.setattr(_cfg, "_PACKAGE_CONFIG_DIR", empty_pkg)
    # Reset the cached watcher so the test sees a fresh lookup.
    monkeypatch.setattr(server, "_ui_prefs_watcher", None)

    client = TestClient(server.app)
    resp = client.get("/api/ui-prefs")
    assert resp.status_code == 404


def test_api_ui_prefs_returns_whitelisted(tmp_path: Path, monkeypatch) -> None:
    """/api/ui-prefs returns 200 with only whitelisted keys from the yaml."""
    from fastapi.testclient import TestClient

    user_dir = tmp_path / ".stan"
    user_dir.mkdir()
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    (user_dir / "ui_prefs.yml").write_text(yaml.dump({
        "front_page_view": "matrix",
        "ms1_format": "short",
        "bogus_field": "x",
    }))

    import stan.config as _cfg
    import stan.dashboard.server as server

    monkeypatch.setattr(_cfg, "_USER_CONFIG_DIR", user_dir)
    monkeypatch.setattr(_cfg, "_PACKAGE_CONFIG_DIR", pkg_dir)
    monkeypatch.setattr(server, "_ui_prefs_watcher", None)

    client = TestClient(server.app)
    resp = client.get("/api/ui-prefs")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"front_page_view": "matrix", "ms1_format": "short"}
