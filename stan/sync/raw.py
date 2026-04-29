r"""Sync raw QC files (.d / .raw) from the instrument PC to the Hive SMB mirror.

The existing ``sync_to_hive_mirror`` covers stan.db / configs / logs /
screencaps. This module covers the heavyweight side: the Bruker ``.d``
directories (often 50-200GB, many small files inside) and Thermo
``.raw`` single big files (5-50GB).

Transport is the same SMB share the rest of the mirror uses
(``/Volumes/proteomics-grp/STAN/<host>/`` on Brett's dev box,
``\\proteomics-grp\STAN\<host>\`` on Windows instrument PCs). Raw
files land at ``<mirror>/raw/<filename>`` so SLURM jobs on Hive can
read them directly via the Quobyte POSIX mount at
``/quobyte/proteomics-grp/STAN/<host>/raw/``.

Speed
-----
- Bruker ``.d`` is a directory with hundreds of small files inside;
  per-file SMB latency dominates the wall-clock. On Windows we shell
  out to ``robocopy /MT:8`` (8 parallel threads, ~6× faster than naive
  copy). On Unix we use ``shutil.copytree`` (acceptable since the dev
  box has the share mounted directly via macFUSE/Quobyte).
- Thermo ``.raw`` is a single file; ``shutil.copy2`` is fine — SMB
  saturates a single large stream cleanly.

Manifest
--------
``~/.stan/hive_raw_manifest.json`` — keyed by raw filename. Each entry
records ``{size, mtime, dest, synced_at}``. We skip a re-sync if the
manifest says the source size + mtime match what we already pushed.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from stan.config import _USER_CONFIG_DIR, get_hive_mirror_dir

logger = logging.getLogger(__name__)

MANIFEST_PATH = _USER_CONFIG_DIR / "hive_raw_manifest.json"

# Bruker .d → directory copy, Thermo .raw → single-file copy.
# Anything else is treated as opaque single file.
BRUKER_SUFFIXES = (".d",)
THERMO_SUFFIXES = (".raw",)


def _path_size(path: Path) -> int:
    """Total bytes for a file, or sum-of-files for a directory."""
    if path.is_file():
        return path.stat().st_size
    if path.is_dir():
        return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
    return 0


def _path_mtime(path: Path) -> float:
    """Latest mtime across a tree (so a touched-up .d resyncs)."""
    if path.is_file():
        return path.stat().st_mtime
    if path.is_dir():
        latest = path.stat().st_mtime
        for p in path.rglob("*"):
            try:
                latest = max(latest, p.stat().st_mtime)
            except OSError:
                pass
        return latest
    return 0.0


def _load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {}
    try:
        return json.loads(MANIFEST_PATH.read_text())
    except Exception:
        logger.warning("Manifest at %s is corrupt — starting fresh", MANIFEST_PATH)
        return {}


def _save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True))
    tmp.replace(MANIFEST_PATH)


def _already_synced(raw_path: Path, manifest: dict) -> bool:
    """True if the manifest says we already pushed this exact source."""
    entry = manifest.get(raw_path.name)
    if not entry:
        return False
    return (
        entry.get("size") == _path_size(raw_path)
        and abs(entry.get("mtime", 0) - _path_mtime(raw_path)) < 1.0
    )


def _copy_dir_robocopy(src: Path, dest: Path, log_path: Path) -> bool:
    """Multi-threaded SMB copy on Windows via robocopy /MT:8.

    robocopy exit codes: <8 success, >=8 failure.
    """
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [
        "robocopy",
        str(src),
        str(dest),
        "/E",          # recursive incl. empty
        "/MT:8",       # 8 parallel copy threads
        "/R:3",        # retry 3 times on transient failures
        "/W:10",       # 10s wait between retries
        "/COPY:DAT",   # data, attributes, timestamps (skip ACLs — they don't translate)
        "/NFL", "/NDL", "/NP",  # quiet — no per-file log spam
        "/LOG+:" + str(log_path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=14400  # 4h max per .d
        )
    except subprocess.TimeoutExpired:
        logger.error("robocopy timed out for %s after 4h", src)
        return False
    return result.returncode < 8


def _copy_dir_shutil(src: Path, dest: Path) -> bool:
    """Fallback recursive copy for Unix dev boxes."""
    try:
        shutil.copytree(src, dest, dirs_exist_ok=True)
        return True
    except Exception:
        logger.exception("copytree failed for %s -> %s", src, dest)
        return False


def _copy_file(src: Path, dest: Path) -> bool:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dest))
        return True
    except Exception:
        logger.exception("copy2 failed for %s -> %s", src, dest)
        return False


def sync_raw_file_to_hive(
    raw_path: Path,
    *,
    force: bool = False,
) -> dict:
    """Sync one ``.d`` or ``.raw`` to the Hive SMB mirror.

    Args:
        raw_path: Local source file/directory on the instrument PC.
        force: Bypass the manifest skip-if-already-synced check.

    Returns:
        Result dict with keys: ``status`` (synced / skipped / failed /
        no_mirror), ``dest``, ``size_bytes``, ``elapsed_s``, ``error``.
    """
    raw_path = Path(raw_path)
    result: dict = {
        "source": str(raw_path),
        "status": "failed",
        "dest": None,
        "size_bytes": 0,
        "elapsed_s": 0.0,
        "error": None,
    }

    if not raw_path.exists():
        result["error"] = "source does not exist"
        return result

    hive_dir = get_hive_mirror_dir()
    if not hive_dir:
        result["status"] = "no_mirror"
        result["error"] = "Hive mirror not configured / unreachable"
        return result

    manifest = _load_manifest()
    if not force and _already_synced(raw_path, manifest):
        prior = manifest.get(raw_path.name, {})
        result["status"] = "skipped"
        result["dest"] = prior.get("dest")
        result["size_bytes"] = prior.get("size", 0)
        return result

    raw_dest_dir = hive_dir / "raw"
    raw_dest_dir.mkdir(parents=True, exist_ok=True)
    dest = raw_dest_dir / raw_path.name

    started = time.monotonic()
    stan_logs_dir = Path.home() / "STAN" / "logs"
    stan_logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = stan_logs_dir / f"sync_raw_{raw_path.name}.log"

    if raw_path.is_dir() and raw_path.suffix.lower() in BRUKER_SUFFIXES:
        if sys.platform == "win32":
            ok = _copy_dir_robocopy(raw_path, dest, log_path)
        else:
            ok = _copy_dir_shutil(raw_path, dest)
    else:
        # Single-file path covers .raw and any opaque blob someone passes us
        ok = _copy_file(raw_path, dest)

    elapsed = time.monotonic() - started
    size = _path_size(raw_path)

    if not ok:
        result["error"] = "copy failed — see log"
        result["elapsed_s"] = elapsed
        return result

    manifest[raw_path.name] = {
        "size": size,
        "mtime": _path_mtime(raw_path),
        "dest": str(dest),
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_s": elapsed,
    }
    _save_manifest(manifest)

    result.update(status="synced", dest=str(dest), size_bytes=size, elapsed_s=elapsed)
    logger.info(
        "Synced %s (%.1f MB in %.1fs) to %s",
        raw_path.name, size / 1e6, elapsed, dest,
    )
    return result


def sync_raw_backlog(
    watched_dirs: Iterable[Path],
    *,
    limit: int | None = None,
    suffixes: Iterable[str] = BRUKER_SUFFIXES + THERMO_SUFFIXES,
    dry_run: bool = False,
) -> list[dict]:
    """Walk the watched directories and sync every raw file not yet on Hive.

    Args:
        watched_dirs: List of acquisition directories to scan.
        limit: Stop after N files (handy for the smoke test).
        suffixes: File/dir suffixes that count as raw data.
        dry_run: Just enumerate; don't copy.

    Returns:
        List of per-file result dicts (see ``sync_raw_file_to_hive``).
    """
    candidates: list[Path] = []
    seen: set[str] = set()
    for root in watched_dirs:
        root = Path(root)
        if not root.exists():
            continue
        for child in root.rglob("*"):
            if child.name in seen:
                continue
            sfx = child.suffix.lower()
            if sfx not in suffixes:
                continue
            # Bruker .d is a directory; Thermo .raw is a file
            if sfx in BRUKER_SUFFIXES and not child.is_dir():
                continue
            if sfx in THERMO_SUFFIXES and not child.is_file():
                continue
            candidates.append(child)
            seen.add(child.name)
            if limit and len(candidates) >= limit:
                break
        if limit and len(candidates) >= limit:
            break

    logger.info("Found %d raw candidates", len(candidates))
    if dry_run:
        return [
            {
                "source": str(c),
                "status": "dry_run",
                "size_bytes": _path_size(c),
            }
            for c in candidates
        ]

    results = []
    for c in candidates:
        results.append(sync_raw_file_to_hive(c))
    return results
