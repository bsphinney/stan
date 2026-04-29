"""Tests for raw-file sync to Hive SMB mirror."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from stan.sync.raw import (
    _already_synced,
    _path_mtime,
    _path_size,
    sync_raw_backlog,
    sync_raw_file_to_hive,
)


def test_path_size_file(tmp_path: Path) -> None:
    f = tmp_path / "x.raw"
    f.write_bytes(b"abcdef")
    assert _path_size(f) == 6


def test_path_size_directory(tmp_path: Path) -> None:
    d = tmp_path / "x.d"
    d.mkdir()
    (d / "a").write_bytes(b"aa")
    (d / "sub").mkdir()
    (d / "sub" / "b").write_bytes(b"bbb")
    assert _path_size(d) == 5


def test_path_mtime_directory_returns_latest(tmp_path: Path) -> None:
    d = tmp_path / "x.d"
    d.mkdir()
    (d / "a").write_bytes(b"a")
    (d / "b").write_bytes(b"b")
    # mtime should be a positive float reflecting one of the entries
    assert _path_mtime(d) > 0


def test_already_synced_matches_size_and_mtime(tmp_path: Path) -> None:
    f = tmp_path / "y.raw"
    f.write_bytes(b"123")
    manifest = {
        "y.raw": {
            "size": 3,
            "mtime": _path_mtime(f),
            "dest": "/somewhere",
        }
    }
    assert _already_synced(f, manifest) is True


def test_already_synced_rejects_size_mismatch(tmp_path: Path) -> None:
    f = tmp_path / "y.raw"
    f.write_bytes(b"123")
    manifest = {
        "y.raw": {"size": 999, "mtime": _path_mtime(f), "dest": "/x"}
    }
    assert _already_synced(f, manifest) is False


def test_sync_raw_file_returns_no_mirror_when_unset(tmp_path: Path) -> None:
    f = tmp_path / "z.raw"
    f.write_bytes(b"hi")
    with patch("stan.sync.raw.get_hive_mirror_dir", return_value=None):
        result = sync_raw_file_to_hive(f)
    assert result["status"] == "no_mirror"


def test_sync_raw_file_missing_source(tmp_path: Path) -> None:
    result = sync_raw_file_to_hive(tmp_path / "ghost.raw")
    assert result["status"] == "failed"
    assert "source does not exist" in (result.get("error") or "")


def test_sync_raw_file_copies_thermo_file(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src = src_dir / "test.raw"
    src.write_bytes(b"\x00" * 1024)

    mirror = tmp_path / "mirror"
    mirror.mkdir()
    manifest_path = tmp_path / "manifest.json"

    with patch("stan.sync.raw.get_hive_mirror_dir", return_value=mirror), \
         patch("stan.sync.raw.MANIFEST_PATH", manifest_path):
        result = sync_raw_file_to_hive(src)

    assert result["status"] == "synced"
    assert (mirror / "raw" / "test.raw").exists()
    assert (mirror / "raw" / "test.raw").read_bytes() == src.read_bytes()
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert "test.raw" in manifest


def test_sync_raw_file_skips_when_manifest_matches(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src = src_dir / "x.raw"
    src.write_bytes(b"abc")

    mirror = tmp_path / "mirror"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "x.raw": {
            "size": 3,
            "mtime": _path_mtime(src),
            "dest": "/already",
        }
    }))

    with patch("stan.sync.raw.get_hive_mirror_dir", return_value=mirror), \
         patch("stan.sync.raw.MANIFEST_PATH", manifest_path):
        result = sync_raw_file_to_hive(src)

    assert result["status"] == "skipped"
    # Mirror dir was never touched
    assert not (mirror / "raw").exists()


def test_sync_raw_file_force_overrides_manifest(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src = src_dir / "x.raw"
    src.write_bytes(b"abc")

    mirror = tmp_path / "mirror"
    mirror.mkdir()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({
        "x.raw": {"size": 3, "mtime": _path_mtime(src), "dest": "/already"}
    }))

    with patch("stan.sync.raw.get_hive_mirror_dir", return_value=mirror), \
         patch("stan.sync.raw.MANIFEST_PATH", manifest_path):
        result = sync_raw_file_to_hive(src, force=True)

    assert result["status"] == "synced"
    assert (mirror / "raw" / "x.raw").exists()


def test_sync_raw_backlog_dry_run_lists_candidates(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "a.raw").write_bytes(b"a")
    (watch / "b.raw").write_bytes(b"b")
    bruker = watch / "c.d"
    bruker.mkdir()
    (bruker / "frame").write_bytes(b"frame")

    results = sync_raw_backlog([watch], dry_run=True)
    names = sorted(Path(r["source"]).name for r in results)
    assert names == ["a.raw", "b.raw", "c.d"]
    assert all(r["status"] == "dry_run" for r in results)


def test_sync_raw_backlog_respects_limit(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    for i in range(5):
        (watch / f"{i}.raw").write_bytes(b"x")
    results = sync_raw_backlog([watch], limit=2, dry_run=True)
    assert len(results) == 2


def test_sync_raw_backlog_skips_unknown_suffix(tmp_path: Path) -> None:
    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "metadata.txt").write_bytes(b"hi")
    (watch / "valid.raw").write_bytes(b"hi")
    results = sync_raw_backlog([watch], dry_run=True)
    names = [Path(r["source"]).name for r in results]
    assert names == ["valid.raw"]
