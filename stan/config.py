"""Central configuration loader with hot-reload support."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
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


def _backup_sqlite(src: Path, dest: Path) -> None:
    """Atomic page-level copy of a SQLite DB via the online-backup API.

    Safe during concurrent writes — readers/writers see consistent state.
    Opens the source read-only so no write lock is acquired. Writes to a
    sibling temp file then renames atomically so readers never see a partial
    destination file.

    Args:
        src: Source SQLite database path.
        dest: Destination path (will be overwritten atomically).

    Raises:
        Exception: Propagates any sqlite3 or OS error to the caller.
    """
    import sqlite3

    src_uri = f"file:{src}?mode=ro"
    src_conn = sqlite3.connect(src_uri, uri=True)
    try:
        tmp = dest.with_suffix(dest.suffix + ".sync.tmp")
        if tmp.exists():
            tmp.unlink()
        dest_conn = sqlite3.connect(str(tmp))
        try:
            src_conn.backup(dest_conn)
        finally:
            dest_conn.close()
        # os.replace is atomic on POSIX and same-volume NTFS moves.
        # SMB shares: goes through a server-side rename — atomic enough
        # for concurrent readers (no reader sees a partial file).
        os.replace(str(tmp), str(dest))
    finally:
        src_conn.close()


def _rotate_backups(mirror_dir: Path, src_db: Path, keep: int = 24) -> None:
    """Keep the last ``keep`` snapshots of stan.db under ``<mirror_dir>/backups/``.

    Each snapshot is named ``stan.db.YYYYMMDD_HHMMSS`` (UTC). Snapshots
    beyond ``keep`` are deleted oldest-first. Timestamp-based rotation only —
    no content deduplication (sqlite3 backup shifts page checksums each call).

    Args:
        mirror_dir: The per-instrument Hive mirror directory.
        src_db: Path to the source stan.db to snapshot.
        keep: Number of snapshots to retain (default 24).
    """
    backups_dir = mirror_dir / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    snap_name = f"stan.db.{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    snap_path = backups_dir / snap_name
    try:
        _backup_sqlite(src_db, snap_path)
    except Exception as e:
        logger.warning("backup snapshot failed: %s", e)
        return
    # Prune oldest beyond keep.
    snaps = sorted(backups_dir.glob("stan.db.*"))
    for old in snaps[:-keep]:
        try:
            old.unlink()
        except OSError:
            pass


def sync_to_hive_mirror(include_reports: bool = True) -> bool:
    """Copy stan.db, configs, and baseline reports to the Hive mirror.

    Syncs:
    - stan.db (full QC database)
    - instruments.yml, community.yml (config)
    - instrument_library.parquet (if exists)
    - baseline_output/*/report.parquet (DIA-NN reports — for deep analysis)
    - baseline_output/*/report.stats.tsv (per-run stats)

    Runs after baseline completes or on demand. Silently no-ops if
    the Hive mirror isn't available.

    Returns:
        True if sync succeeded, False otherwise.
    """
    hive_dir = get_hive_mirror_dir()
    if not hive_dir:
        return False

    import shutil
    user_dir = _USER_CONFIG_DIR
    synced = []
    db_synced = False
    # stan.db uses the sqlite3 online-backup API (atomic, safe during writes).
    # shutil.copy2 is NOT used for stan.db — it can produce a corrupt
    # destination if the watcher writes while the copy is in progress.
    src_db = user_dir / "stan.db"
    if src_db.exists():
        try:
            _backup_sqlite(src_db, hive_dir / "stan.db")
            synced.append("stan.db")
            db_synced = True
        except Exception as e:
            logger.warning("Could not sync stan.db to Hive (skipping): %s", e)
    for fname in ["instruments.yml", "community.yml", "instrument_library.parquet"]:
        src = user_dir / fname
        if src.exists():
            try:
                dest = hive_dir / fname
                shutil.copy2(str(src), str(dest))
                synced.append(fname)
            except Exception as e:
                logger.debug("Could not sync %s to Hive: %s", fname, e)

    # Mirror ~/.stan/logs/ so submit-all and other CLI logs are visible
    # on the shared drive without SSHing into the instrument PC.
    logs_src = user_dir / "logs"
    if logs_src.exists() and logs_src.is_dir():
        logs_dest = hive_dir / "logs"
        logs_dest.mkdir(parents=True, exist_ok=True)
        log_count = 0
        for log_file in logs_src.iterdir():
            if not log_file.is_file():
                continue
            try:
                dest_file = logs_dest / log_file.name
                if (not dest_file.exists()
                        or log_file.stat().st_mtime > dest_file.stat().st_mtime):
                    shutil.copy2(str(log_file), str(dest_file))
                    log_count += 1
            except Exception:
                pass
        if log_count > 0:
            synced.append(f"{log_count} logs")

    # Mirror screencaps — heartbeat + run-end frames captured by the
    # screencap daemon at ~/STAN/screencaps/<YYYYMMDD>/<HHMMSS>[_runend_<run>].jpg
    # godmode reads from <mirror>/<host>/screencaps/ and renders the
    # ScreencapCard.
    screencaps_src = Path.home() / "STAN" / "screencaps"
    if screencaps_src.exists() and screencaps_src.is_dir():
        screencaps_dest = hive_dir / "screencaps"
        screencaps_dest.mkdir(parents=True, exist_ok=True)
        cap_count = 0
        for date_dir in screencaps_src.iterdir():
            if not date_dir.is_dir():
                continue
            dest_date_dir = screencaps_dest / date_dir.name
            dest_date_dir.mkdir(parents=True, exist_ok=True)
            for cap in date_dir.iterdir():
                if not cap.is_file() or not cap.name.endswith(".jpg"):
                    continue
                try:
                    dest_cap = dest_date_dir / cap.name
                    if (not dest_cap.exists()
                            or cap.stat().st_mtime > dest_cap.stat().st_mtime):
                        shutil.copy2(str(cap), str(dest_cap))
                        cap_count += 1
                except Exception:
                    pass
        if cap_count > 0:
            synced.append(f"{cap_count} screencaps")

    # Mirror baseline_output — small per-run files (not mzML etc.)
    if include_reports:
        baseline_src = user_dir / "baseline_output"
        if baseline_src.exists():
            baseline_dest = hive_dir / "baseline_output"
            baseline_dest.mkdir(parents=True, exist_ok=True)
            report_count = 0
            for run_dir in baseline_src.iterdir():
                if not run_dir.is_dir():
                    continue
                dest_run = baseline_dest / run_dir.name
                dest_run.mkdir(parents=True, exist_ok=True)
                for fname in ["report.parquet", "report.stats.tsv", "report.log.txt",
                              "sage_config.json", "results.sage.parquet", "results.json",
                              "diann.log", "sage.log"]:
                    src_file = run_dir / fname
                    if src_file.exists():
                        try:
                            dest_file = dest_run / fname
                            if not dest_file.exists() or src_file.stat().st_mtime > dest_file.stat().st_mtime:
                                shutil.copy2(str(src_file), str(dest_file))
                                if fname == "report.parquet":
                                    report_count += 1
                        except Exception:
                            pass
            if report_count > 0:
                synced.append(f"{report_count} reports")

    # Rolling backups — only after a successful live stan.db sync.
    if db_synced:
        _rotate_backups(mirror_dir=hive_dir, src_db=src_db, keep=24)

    if synced:
        logger.info("Synced to Hive mirror: %s", ", ".join(synced))
    return len(synced) > 0


def setup_hive_mirror_logging(log_name: str) -> Path | None:
    """Set up a file handler that writes logs to the Hive mirror directory.

    Should be called at the start of any STAN command that needs its
    output captured for remote debugging. Silently no-ops if the Hive
    mirror isn't available.

    Args:
        log_name: Filename for the log (e.g. "build_library.log").

    Returns:
        The log file path if set up, else None.
    """
    hive_dir = get_hive_mirror_dir()
    if not hive_dir:
        return None
    try:
        log_path = hive_dir / log_name
        file_handler = logging.FileHandler(str(log_path), mode="w", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        file_handler.flush = file_handler.stream.flush  # type: ignore[assignment]
        logging.getLogger().addHandler(file_handler)
        logging.getLogger().setLevel(logging.DEBUG)
        return log_path
    except Exception:
        return None


def get_hive_mirror_dir() -> Path | None:
    """Return the Hive mirror directory if mapped, else None.

    Checks common locations where STAN may be set up to sync logs to Hive:
    - Y:\\STAN  (Windows mapped network drive — default convention)
    - Any path in community.yml under hive_mirror_dir
    - HIVE_MIRROR_DIR environment variable

    Returns a per-instrument subdirectory (so multiple instruments can
    share the same mirror without overwriting each other).
    """
    import os
    candidates = []

    # Environment variable override
    env_dir = os.environ.get("HIVE_MIRROR_DIR")
    if env_dir:
        candidates.append(Path(env_dir))

    # Config file
    try:
        comm = load_community()
        cfg_dir = comm.get("hive_mirror_dir")
        if cfg_dir:
            candidates.append(Path(cfg_dir))
    except Exception:
        pass

    # Default: Y:\STAN on Windows
    if _plat.system() == "Windows":
        candidates.append(Path("Y:/STAN"))
    # Default: macOS Quobyte / SMB mounts (Brett's Mac, `stan fleet-status`)
    if _plat.system() == "Darwin":
        candidates.append(Path("/Volumes/proteomics-grp/STAN"))

    for base in candidates:
        try:
            if base.exists() and base.is_dir():
                # Create a per-instrument subdirectory based on hostname
                import socket
                hostname = socket.gethostname().replace(" ", "_")
                instrument_dir = base / hostname
                instrument_dir.mkdir(parents=True, exist_ok=True)
                return instrument_dir
        except (OSError, PermissionError):
            continue

    return None


def get_hive_mirror_root() -> Path | None:
    """Return the shared `Y:\\STAN` (or equivalent) root — NOT the
    per-hostname subdirectory.

    Distinct from `get_hive_mirror_dir()` because read-only clients
    (e.g. Brett's Mac running `stan fleet-status`) need to iterate
    *every* host's subdir without creating their own. Calling code
    that needs to write WON'T use this — it'll use the host-scoped
    `get_hive_mirror_dir()` so it gets a guaranteed-writable dir.
    """
    import os as _os

    candidates = []

    env_dir = _os.environ.get("HIVE_MIRROR_DIR")
    if env_dir:
        candidates.append(Path(env_dir))

    try:
        comm = load_community()
        cfg_dir = comm.get("hive_mirror_dir")
        if cfg_dir:
            candidates.append(Path(cfg_dir))
    except Exception:
        pass

    if _plat.system() == "Windows":
        candidates.append(Path("Y:/STAN"))
    if _plat.system() == "Darwin":
        candidates.append(Path("/Volumes/proteomics-grp/STAN"))

    for base in candidates:
        try:
            if base.exists() and base.is_dir():
                return base
        except (OSError, PermissionError):
            continue
    return None




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


# Allow-list of UI preference keys the dashboard consumes. Unknown keys are
# silently dropped so a typo in ui_prefs.yml can't pollute localStorage or
# flip unrelated behavior. Keep in sync with usePreferences() in
# stan/dashboard/public/index.html.
UI_PREF_KEYS: tuple[str, ...] = (
    "front_page_view",     # "gauges" | "weekly_table" | "matrix"
    "matrix_bar_scale",    # "week_range" | "baseline_gates"
    "ms1_format",          # "sci" | "short"
)


def load_ui_prefs() -> dict:
    """Load the optional lab-wide UI defaults from ``ui_prefs.yml``.

    This file is optional. If it doesn't exist, an empty dict is returned
    and the dashboard falls back to its built-in defaults. Unknown keys
    are filtered out so YAML typos can't propagate into the UI.

    Returns:
        Dict of whitelisted UI preference keys. Empty if the file is
        missing or all keys are unknown.
    """
    try:
        path = resolve_config_path("ui_prefs.yml")
    except FileNotFoundError:
        return {}
    data = load_yaml(path)
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in UI_PREF_KEYS if k in data}


def get_default_config_dir() -> Path:
    """Return the package-level config/ directory path."""
    return _PACKAGE_CONFIG_DIR


def get_user_config_dir() -> Path:
    """Return the user config directory path (~/.stan/)."""
    return _USER_CONFIG_DIR
