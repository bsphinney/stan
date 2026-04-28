"""Tests for configuration loading and hot-reload."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import patch

import yaml

from stan.config import ConfigWatcher, _backup_sqlite, _rotate_backups, load_yaml, resolve_config_path


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


# ---------------------------------------------------------------------------
# _backup_sqlite tests
# ---------------------------------------------------------------------------

def _make_db(path: Path, rows: int = 10) -> None:
    """Create a simple SQLite DB with `rows` rows in a test table."""
    with sqlite3.connect(str(path)) as con:
        con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        con.executemany("INSERT INTO t VALUES (?, ?)", [(i, f"v{i}") for i in range(rows)])
        con.commit()


def test_backup_sqlite_atomic_during_writes(tmp_path: Path) -> None:
    """_backup_sqlite produces a valid, consistent DB while a writer is active."""
    src = tmp_path / "src.db"
    dest = tmp_path / "dest.db"
    _make_db(src, rows=50)

    stop_event = threading.Event()
    errors: list[Exception] = []

    def writer() -> None:
        """Continuously insert rows into src until stop_event is set."""
        try:
            with sqlite3.connect(str(src)) as con:
                i = 1000
                while not stop_event.is_set():
                    con.execute("INSERT OR IGNORE INTO t VALUES (?, ?)", (i, f"x{i}"))
                    con.commit()
                    i += 1
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=writer, daemon=True)
    t.start()
    # Give writer a moment to get going.
    time.sleep(0.02)

    _backup_sqlite(src, dest)

    stop_event.set()
    t.join(timeout=2)

    # Destination must be a valid SQLite file with a consistent row count.
    assert dest.exists()
    with sqlite3.connect(str(dest)) as check:
        row_count = check.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert row_count >= 50, f"Expected at least 50 rows, got {row_count}"
    assert not errors, f"Writer thread raised: {errors}"


def test_backup_sqlite_handles_locked_source(tmp_path: Path) -> None:
    """_backup_sqlite succeeds even when the source has a shared read lock.

    The sqlite3 online-backup API does not require an exclusive lock on the
    source — it snapshots page-by-page and can work around concurrent readers.
    """
    src = tmp_path / "src.db"
    dest = tmp_path / "dest.db"
    _make_db(src, rows=5)

    # Hold a read transaction open on src while we back it up.
    hold_conn = sqlite3.connect(str(src))
    hold_conn.execute("BEGIN")
    hold_conn.execute("SELECT * FROM t")

    try:
        # Should not raise even with the read transaction open.
        _backup_sqlite(src, dest)
    finally:
        hold_conn.close()

    assert dest.exists()
    with sqlite3.connect(str(dest)) as check:
        count = check.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    assert count == 5


def test_backup_sqlite_no_partial_on_failure(tmp_path: Path) -> None:
    """If dest directory is read-only, dest itself must not be left as a partial file."""
    src = tmp_path / "src.db"
    _make_db(src, rows=3)

    # Create a read-only destination directory so the write will fail.
    ro_dir = tmp_path / "readonly"
    ro_dir.mkdir()
    ro_dir.chmod(0o555)
    dest = ro_dir / "dest.db"

    try:
        import pytest
        with pytest.raises(Exception):
            _backup_sqlite(src, dest)
        # The final destination must not exist (no partial file at dest).
        assert not dest.exists()
    finally:
        # Restore permissions so tmp_path cleanup works.
        ro_dir.chmod(0o755)


# ---------------------------------------------------------------------------
# _rotate_backups tests
# ---------------------------------------------------------------------------

def test_rotate_backups_keeps_n(tmp_path: Path) -> None:
    """_rotate_backups prunes oldest snapshots, keeping exactly `keep`."""
    src = tmp_path / "stan.db"
    _make_db(src, rows=2)

    backups_dir = tmp_path / "backups"
    backups_dir.mkdir()

    # Pre-populate 30 fake snapshot files with distinct names.
    for i in range(30):
        fake = backups_dir / f"stan.db.20240101_{i:06d}"
        fake.write_bytes(b"x")

    _rotate_backups(mirror_dir=tmp_path, src_db=src, keep=10)

    remaining = sorted(backups_dir.glob("stan.db.*"))
    # The real backup added one, so we had 31; keep=10 means exactly 10 remain.
    assert len(remaining) == 10, f"Expected 10 snapshots, got {len(remaining)}"
    # Newest 10 should be kept — the real backup name sorts last (current UTC time).
    names = [p.name for p in remaining]
    assert any(n.startswith("stan.db.202") for n in names), "Real backup snapshot missing"


def test_rotate_backups_creates_dir(tmp_path: Path) -> None:
    """_rotate_backups creates the backups/ directory if it doesn't exist."""
    src = tmp_path / "stan.db"
    _make_db(src, rows=1)

    # backups/ must NOT exist yet.
    backups_dir = tmp_path / "backups"
    assert not backups_dir.exists()

    _rotate_backups(mirror_dir=tmp_path, src_db=src, keep=24)

    assert backups_dir.exists()
    snaps = list(backups_dir.glob("stan.db.*"))
    assert len(snaps) == 1, f"Expected 1 snapshot, got {snaps}"
