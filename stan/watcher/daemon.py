"""Watchdog-based instrument watcher daemon.

Monitors raw data directories for new acquisitions, detects when files are stable,
identifies acquisition mode, and dispatches search jobs.
"""

from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path

from watchdog.events import (
    DirCreatedEvent,
    FileCreatedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer
from watchdog.observers.polling import PollingObserver

from stan.config import CONFIG_POLL_INTERVAL, ConfigWatcher, resolve_config_path
from stan.watcher.detector import AcquisitionMode, detect_mode, is_dia
from stan.watcher.qc_filter import compile_qc_pattern, is_qc_file
from stan.watcher.stability import StabilityTracker

logger = logging.getLogger(__name__)


# Module-level singleton populated by WatcherDaemon.run(). Read by
# stan.control._action_watcher_debug to expose runtime state.
_ACTIVE_DAEMON: "WatcherDaemon | None" = None


def _is_network_path(path: str) -> bool:
    """Detect UNC or network paths where native OS events may not work."""
    return path.startswith("\\\\") or path.startswith("//")


class _AcquisitionHandler(FileSystemEventHandler):
    """Watchdog event handler that creates StabilityTrackers for new raw files."""

    def __init__(
        self,
        instrument_config: dict,
        trackers: dict[str, StabilityTracker],
        tracker_modes: dict[str, str],
        lock: threading.Lock,
        on_event=None,
    ) -> None:
        super().__init__()
        self._config = instrument_config
        self._trackers = trackers
        # Parallel map: tracker path → "qc" | "monitor". Lets
        # _on_acquisition_complete route QC files to search and
        # monitor-only files to rawmeat without plumbing a flag
        # through StabilityTracker.
        self._tracker_modes = tracker_modes
        self._lock = lock
        self._extensions = set(instrument_config.get("extensions", []))
        self._vendor = instrument_config.get("vendor", "")
        self._stable_secs = instrument_config.get("stable_secs", 60)
        self._qc_only = instrument_config.get("qc_only", True)
        self._qc_pattern = compile_qc_pattern(instrument_config.get("qc_pattern"))
        self._monitor_all_files = bool(instrument_config.get("monitor_all_files", False))
        # Exclude pattern — files matching this are skipped entirely at
        # both the QC path and the monitor path. Typical value:
        # "(?i)(wash|blank)". None = disabled.
        exc = instrument_config.get("exclude_pattern")
        self._exclude_pattern = None
        if exc:
            import re as _re
            try:
                self._exclude_pattern = _re.compile(exc)
            except _re.error:
                logger.warning(
                    "watcher: invalid exclude_pattern %r — ignoring", exc,
                )
        # Callback used by InstrumentWatcher to record a ring-buffer entry
        # for each event. Takes (category, path, detail).
        self._on_event = on_event or (lambda *a, **kw: None)

    def _is_inside_dot_d(self, path: Path) -> bool:
        """Check if path is inside a Bruker .d directory (not the .d itself)."""
        for parent in path.parents:
            if parent.suffix == ".d":
                return True
        return False

    def on_created(self, event) -> None:
        path = Path(event.src_path)
        ev_kind = "dir" if isinstance(event, DirCreatedEvent) else "file"

        # Ignore anything inside a .d directory — those are Bruker internals
        # (analysis.tdf, analysis.tdf_bin, etc.), not new acquisitions
        if self._is_inside_dot_d(path):
            self._on_event("ignore_inside_dot_d", path, ev_kind)
            return

        # Bruker .d: directory creation event
        if isinstance(event, DirCreatedEvent) and path.suffix == ".d":
            if ".d" in self._extensions:
                self._register_tracker(path, ev_kind)
            else:
                self._on_event("ignore_ext_mismatch", path,
                               f"{ev_kind}; extensions={sorted(self._extensions)}")

        # Thermo .raw: file creation event
        elif isinstance(event, FileCreatedEvent) and path.suffix in self._extensions:
            if path.suffix != ".d":
                self._register_tracker(path, ev_kind)

        else:
            self._on_event("ignore_other", path,
                           f"{ev_kind}; suffix={path.suffix!r}; "
                           f"extensions={sorted(self._extensions)}")

    def _register_tracker(self, path: Path, ev_kind: str = "") -> None:
        # Hard exclude - wash/blank/etc. are skipped at both paths
        if self._exclude_pattern and self._exclude_pattern.search(path.stem):
            self._on_event("exclude_pattern_match", path,
                           f"pattern={self._exclude_pattern.pattern!r}")
            return

        # v0.2.159: catchup-registered trackers (ev_kind="startup_scan")
        # are for acquisitions that already completed before the watcher
        # started, so we bypass the Bruker "saw growth" guard in
        # StabilityTracker. Without this, catchup trackers never
        # stabilize - Brett 2026-04-22 had 434 stuck trackers because
        # of this.
        assume_complete = ev_kind == "startup_scan"

        is_qc = is_qc_file(path, self._qc_pattern)
        if is_qc:
            mode = "qc"
        elif self._monitor_all_files:
            mode = "monitor"
        elif self._qc_only:
            # QC-only mode (default): non-QC files are ignored
            pat = getattr(self._qc_pattern, "pattern", None) if self._qc_pattern else None
            logger.info("watcher: QC filter rejected %s (pattern=%r)", path.name, pat)
            self._on_event("qc_filter_reject", path, f"pattern={pat!r}")
            return
        else:
            mode = "qc"  # qc_only=false treats everything as QC (legacy behavior)

        key = str(path)
        with self._lock:
            if key not in self._trackers:
                self._trackers[key] = StabilityTracker(
                    path=path,
                    vendor=self._vendor,
                    stable_secs=self._stable_secs,
                    assume_complete=assume_complete,
                )
                self._tracker_modes[key] = mode
                logger.info(
                    "watcher: tracking new %s acquisition: %s (stable_secs=%d)",
                    "QC" if mode == "qc" else "monitor-only",
                    path.name, self._stable_secs,
                )
                self._on_event(
                    "tracked_qc" if mode == "qc" else "tracked_monitor",
                    path, f"stable_secs={self._stable_secs}",
                )
            else:
                self._on_event("already_tracked", path, "")


def _model_from_raw(raw_path: Path) -> str | None:
    """Return the instrument model string from a single raw file.

    Bruker .d → analysis.tdf GlobalMetadata.InstrumentName
    (e.g. "timsTOF HT"). Thermo .raw → trfp.extract_metadata()
    instrument_model (e.g. "Orbitrap Fusion Lumos"). Returns
    None if the file can't be read or the field isn't present.
    """
    try:
        if raw_path.is_dir() and raw_path.suffix == ".d":
            tdf = raw_path / "analysis.tdf"
            if tdf.exists():
                import sqlite3 as _sq
                with _sq.connect(str(tdf)) as con:
                    row = con.execute(
                        "SELECT Value FROM GlobalMetadata WHERE Key='InstrumentName'"
                    ).fetchone()
                    if row and row[0]:
                        return str(row[0]).strip()
        elif raw_path.is_file() and raw_path.suffix == ".raw":
            from stan.tools.trfp import extract_metadata
            meta = extract_metadata(raw_path)
            model = getattr(meta, "instrument_model", None) or \
                    (isinstance(meta, dict) and meta.get("instrument_model"))
            if model:
                return str(model).strip()
    except Exception:
        logger.debug("Could not read model from %s", raw_path, exc_info=True)
    return None


def _resolve_instrument_name(config: dict) -> str:
    """Return a human-readable instrument name from a watcher config.

    v0.2.190: handles the `name: auto` placeholder by reading vendor
    metadata from the first raw file in the configured watch_dir.
    Previously the watcher stamped rows with the literal string "auto"
    when operators left the default in place, which then showed up in
    the dashboard + mirror as a meaningless identifier.

    v0.2.229: when name resolves to "auto" because the watch_dir was
    empty/unreadable at watcher startup, _store_run will re-resolve
    from the actual raw file at ingest time (which we definitionally
    have in hand). So this startup-time resolver is now a fast-path
    only; "auto" is no longer the final answer.

    Resolution order:
      1. Explicit name in yaml (anything other than "auto"/empty/
         "unknown") — use verbatim.
      2. First `.d` under watch_dir → TDF GlobalMetadata.InstrumentName
         (e.g. "timsTOF HT").
      3. First `.raw` under watch_dir → fisher_py or TRFP metadata's
         instrument_model field (e.g. "Orbitrap Fusion Lumos").
      4. Fall back to the literal "auto" — _store_run re-resolves at
         ingest time before writing to the runs row.
    """
    configured = (config.get("name") or "").strip()
    if configured and configured.lower() not in ("auto", "unknown", ""):
        return configured

    watch = Path(config.get("watch_dir") or "")
    if not watch.exists():
        return "auto"

    try:
        # Bruker .d — read the first one we find.
        for d in watch.glob("*.d"):
            tdf = d / "analysis.tdf"
            if not tdf.exists():
                continue
            try:
                import sqlite3 as _sq
                with _sq.connect(str(tdf)) as con:
                    row = con.execute(
                        "SELECT Value FROM GlobalMetadata WHERE Key='InstrumentName'"
                    ).fetchone()
                    if row and row[0]:
                        return str(row[0]).strip()
            except Exception:
                continue
            break  # Only try the first .d
    except Exception:
        logger.debug("auto-name Bruker path failed", exc_info=True)

    try:
        # Thermo .raw — use the existing trfp helper.
        for r in watch.glob("*.raw"):
            try:
                from stan.tools.trfp import extract_metadata
                meta = extract_metadata(r)
                model = getattr(meta, "instrument_model", None) or \
                        (isinstance(meta, dict) and meta.get("instrument_model"))
                if model:
                    return str(model).strip()
            except Exception:
                continue
            break  # Only try the first .raw
    except Exception:
        logger.debug("auto-name Thermo path failed", exc_info=True)

    return "auto"


class InstrumentWatcher:
    """Watches a single instrument's raw data directory."""

    def _resolve_spd(self, raw_path) -> int | None:
        """Best-effort SPD resolution for a new acquisition.

        v0.2.188: previously the watcher only read `spd:` from
        instruments.yml, which meant instruments without that
        key got NULL for every run even though the gradient was
        easily derivable from the raw file's own metadata.

        Order of precedence:
          1. validate_spd_from_metadata(raw_path) — reads the
             Bruker .d XML HyStar_LC method name ("60 samples
             per day"), falls back to Frames.Time gradient
             length, or Thermo InstrumentMethod.
          2. instruments.yml `spd:` field (cohort default).
          3. Filename regex as a last resort (matches tokens
             like "60spd" / "60-spd" / "100 SPD").
          4. None — Trends panel shows "SPD unknown".
        """
        # 1. Try the authoritative per-file resolver.
        try:
            from stan.metrics.scoring import validate_spd_from_metadata
            spd = validate_spd_from_metadata(raw_path)
            if spd:
                return int(spd)
        except Exception:
            logger.debug("validate_spd_from_metadata failed for %s",
                         raw_path, exc_info=True)

        # 2. Cohort default from the yaml.
        cfg_spd = self._config.get("spd")
        if cfg_spd:
            try:
                return int(cfg_spd)
            except (TypeError, ValueError):
                pass

        # 3. Filename regex fallback — catches the common Evosep
        #    label written directly into the run name. Underscore
        #    and hyphen separators both accepted.
        try:
            import re
            stem = getattr(raw_path, "stem", str(raw_path))
            m = re.search(r"(\d+)[\s_-]*spd", str(stem), re.IGNORECASE)
            if m:
                return int(m.group(1))
        except Exception:
            pass

        return None

    def _merge_placeholder_runs(self) -> None:
        """Resolve 'auto'/'unknown'/'' rows in runs + sample_health
        from each row's own raw_path and rewrite the instrument
        column to the actual vendor model.

        Runs once at watcher startup. Idempotent — no-op when no
        placeholder rows remain.
        """
        try:
            import sqlite3
            from stan.db import get_db_path, init_db
            init_db()
            db = get_db_path()
            if not db.exists():
                return
            placeholders = ("auto", "unknown", "")
            merged_total = 0
            with sqlite3.connect(str(db)) as con:
                con.row_factory = sqlite3.Row
                for table in ("runs", "sample_health"):
                    try:
                        rows = con.execute(
                            f"SELECT id, raw_path, instrument FROM {table} "
                            f"WHERE instrument IN ('auto', 'unknown', '') "
                            f"OR instrument IS NULL"
                        ).fetchall()
                    except sqlite3.OperationalError:
                        continue
                    for r in rows:
                        rp = r["raw_path"]
                        if not rp:
                            continue
                        try:
                            model = _model_from_raw(Path(rp))
                        except Exception:
                            model = None
                        # Fallback: if we can't read the file (e.g.
                        # raw was archived off-disk), use this
                        # watcher's resolved name as the next-best
                        # guess for rows that came from this watch_dir.
                        if not model and self._name and self._name.lower() not in placeholders:
                            try:
                                if self._watch_dir and rp.lower().startswith(
                                    str(self._watch_dir).lower()
                                ):
                                    model = self._name
                            except Exception:
                                pass
                        if model and model.lower() not in placeholders:
                            con.execute(
                                f"UPDATE {table} SET instrument = ? WHERE id = ?",
                                (model, r["id"]),
                            )
                            merged_total += 1
                con.commit()
            if merged_total:
                logger.info(
                    "Resolved %d placeholder instrument rows from raw_path metadata",
                    merged_total,
                )
        except Exception:
            logger.debug("_merge_placeholder_runs failed", exc_info=True)

    def _persist_resolved_name(
        self, original_name, resolved_name,
    ) -> None:
        """Write the resolved instrument name back to instruments.yml
        on disk if the config currently has a placeholder.
        """
        if not isinstance(original_name, str):
            return
        if original_name.strip().lower() not in ("auto", "unknown", ""):
            return
        if not resolved_name or resolved_name.lower() in ("auto", "unknown"):
            return
        try:
            import yaml as _y
            from stan.config import resolve_config_path
            yml_path = resolve_config_path("instruments.yml")
            if not yml_path or not yml_path.exists():
                return
            doc = _y.safe_load(yml_path.read_text(encoding="utf-8")) or {}
            blocks = doc.get("instruments") or []
            changed = False
            for blk in blocks:
                if not isinstance(blk, dict):
                    continue
                if str(blk.get("watch_dir") or "") != str(self._watch_dir or ""):
                    continue
                if (blk.get("name") or "").strip().lower() in ("auto", "unknown", ""):
                    blk["name"] = resolved_name
                    changed = True
                    aliases = list(blk.get("aliases") or [])
                    for ph in ("auto", "unknown"):
                        if ph not in aliases:
                            aliases.append(ph)
                    blk["aliases"] = aliases
            if changed:
                yml_path.write_text(_y.safe_dump(doc, sort_keys=False), encoding="utf-8")
                logger.info(
                    "Persisted resolved name '%s' back to %s",
                    resolved_name, yml_path,
                )
        except Exception:
            logger.debug(
                "Could not persist resolved name to instruments.yml",
                exc_info=True,
            )

    def __init__(self, instrument_config: dict) -> None:
        import collections
        self._config = instrument_config
        # v0.2.190: resolve "auto" to a real vendor metadata name so
        # rows don't get stamped with a placeholder.
        original_name = instrument_config.get("name")
        resolved_name = _resolve_instrument_name(instrument_config)
        if resolved_name != original_name:
            logger.info(
                "Auto-resolved instrument name '%s' → '%s' from raw metadata",
                original_name, resolved_name,
            )
            instrument_config["name"] = resolved_name
        self._name = resolved_name

        # v0.2.231: scan the runs table for any rows tagged
        # "auto"/"unknown"/"" and resolve each from its own raw_path
        # directly. v0.2.229's merge guard only fired when
        # resolved_name != original_name, which missed the case
        # where instruments.yml says "auto" and resolution at startup
        # also returns "auto" (e.g. watch_dir empty on first boot)
        # while older rows had landed with the proper model name.
        # By reading the raw file referenced by each placeholder row,
        # we get the right model regardless of config drift. Also
        # persist the resolved name back to instruments.yml on disk
        # so the dashboard's "duplicate cards" banner suggests the
        # right merge target instead of the placeholder.
        self._merge_placeholder_runs()
        self._persist_resolved_name(original_name, resolved_name)
        self._watch_dir = instrument_config.get("watch_dir", "")
        self._trackers: dict[str, StabilityTracker] = {}
        # Parallel map of tracker path → "qc" | "monitor". Written by
        # the handler, consumed by _on_acquisition_complete to decide
        # whether to run a full search or just rawmeat.
        self._tracker_modes: dict[str, str] = {}
        self._lock = threading.Lock()

        # Ring buffer of recent events for remote debugging via the
        # `watcher_debug` control action. Every event the handler sees —
        # including ones it chose to ignore — lands here with a category
        # so we can tell "we got a create but skipped it" apart from
        # "we never got any events at all".
        self._recent_events: "collections.deque[dict]" = collections.deque(maxlen=100)
        self._started_at: float | None = None
        self._event_counts: dict[str, int] = {}

        self._handler = _AcquisitionHandler(
            instrument_config, self._trackers, self._tracker_modes,
            self._lock, on_event=self._record_event,
        )

        # Use polling observer for network paths
        self._observer_type = "PollingObserver" if _is_network_path(self._watch_dir) else "Observer"
        if _is_network_path(self._watch_dir):
            self._observer = PollingObserver(timeout=10)
        else:
            self._observer = Observer()

        self._stability_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def name(self) -> str:
        return self._name

    def _record_event(self, category: str, path: Path, detail: str) -> None:
        """Append one row to the recent-events ring buffer."""
        import time
        self._recent_events.append({
            "ts": time.time(),
            "category": category,
            "path": str(path),
            "detail": detail,
        })
        self._event_counts[category] = self._event_counts.get(category, 0) + 1

    def debug_snapshot(self) -> dict:
        """Expose internal state for the `watcher_debug` control action.

        Returns everything a remote diagnostician would need to tell
        whether events are arriving at all, being filtered, or stuck in
        the stability tracker."""
        import time
        pat = getattr(self._handler._qc_pattern, "pattern", None) if self._handler._qc_pattern else None
        now = time.time()
        with self._lock:
            trackers = [
                {
                    "path": key,
                    "age_sec": round(now - tr.first_seen, 1)
                        if hasattr(tr, "first_seen") else None,
                    "last_size": getattr(tr, "last_size", None),
                }
                for key, tr in self._trackers.items()
            ]
        exc_pat = getattr(self._handler._exclude_pattern, "pattern", None) \
            if self._handler._exclude_pattern else None
        return {
            "name": self._name,
            "watch_dir": self._watch_dir,
            "watch_dir_exists": Path(self._watch_dir).exists() if self._watch_dir else False,
            "vendor": self._config.get("vendor"),
            "extensions": sorted(self._config.get("extensions", [])),
            "stable_secs": self._config.get("stable_secs", 60),
            "qc_only": self._config.get("qc_only", True),
            "qc_pattern": pat,
            "monitor_all_files": self._handler._monitor_all_files,
            "exclude_pattern": exc_pat,
            "observer_type": self._observer_type,
            "observer_alive": self._observer.is_alive() if hasattr(self._observer, "is_alive") else None,
            "uptime_sec": round(now - self._started_at, 1) if self._started_at else None,
            "event_counts": dict(self._event_counts),
            "n_trackers_active": len(trackers),
            "trackers_active": trackers,
            "recent_events": list(self._recent_events)[-25:],
        }

    def start(self) -> None:
        """Start watching the directory and the stability check loop."""
        watch_path = Path(self._watch_dir)
        if not watch_path.exists():
            logger.warning(
                "Watch directory does not exist for %s: %s", self._name, self._watch_dir
            )
            self._record_event("watch_dir_missing", watch_path, self._watch_dir)
            return

        self._observer.schedule(self._handler, str(watch_path), recursive=True)
        self._observer.start()

        import time
        self._started_at = time.time()

        self._stop_event.clear()
        self._stability_thread = threading.Thread(
            target=self._stability_loop,
            name=f"stability-{self._name}",
            daemon=True,
        )
        self._stability_thread.start()
        logger.info(
            "watcher: started %s → %s (observer=%s, extensions=%s, "
            "qc_only=%s, qc_pattern=%r, stable_secs=%d)",
            self._name, self._watch_dir, self._observer_type,
            sorted(self._config.get("extensions", [])),
            self._config.get("qc_only", True),
            getattr(self._handler._qc_pattern, "pattern", None)
                if self._handler._qc_pattern else None,
            self._config.get("stable_secs", 60),
        )

        # Catch acquisitions that were already in flight when the
        # daemon started up AND any completed files that arrived while
        # the daemon was down (power cycle, installer restart, crash).
        # watchdog's on_created only fires for newly-appearing paths,
        # so without this scan a .d file saved 2 days ago while STAN
        # was offline would stay invisible forever. The v0.2.101 scan
        # only looked back 30 minutes — this version (v0.2.119) looks
        # back `startup_catchup_days` (default 7) days and checks both
        # the `runs` and `sample_health` tables so already-processed
        # files don't get re-queued.
        try:
            self._scan_for_in_flight()
        except Exception:
            logger.debug("in-flight scan failed", exc_info=True)

    def _scan_for_in_flight(self) -> None:
        """Register StabilityTrackers for raw files the watcher hasn't
        processed yet.

        Two reasons a file might need catching up:
        1. **In-flight**: an acquisition was running when the daemon
           started (restart mid-run). StabilityTracker's v0.2.100
           size/growth checks decide when it's actually finished.
        2. **Offline gap**: the file was written while the daemon was
           down. `startup_catchup_days` (instruments.yml, default 7)
           controls how far back to look. Setting 0 disables it.

        Skips files already in `runs` (search-pipeline completed) or
        `sample_health` (monitor-only pipeline completed) for this
        instrument, so re-running the scan is idempotent."""
        import time
        from stan.db import get_db_path

        watch = Path(self._watch_dir)
        exts = set(self._config.get("extensions", []))
        if not watch.exists() or not exts:
            return

        # v0.2.156: bumped default from 7 to 30. Brett notes on-disk
        # files already acquired don't incur search-time cost on the
        # walk — the stability trackers register and fire fast when
        # the file is already stable. 30 days covers most "I was away"
        # gaps without needing manual config.
        catchup_days = float(self._config.get("startup_catchup_days", 30))
        if catchup_days <= 0:
            # User disabled catch-up. Nothing to do.
            return
        cutoff = time.time() - (catchup_days * 86400)

        # Pull existing entries from BOTH tables so already-processed
        # files don't get re-queued. The runs table covers the full
        # search pipeline; sample_health covers monitor_all_files.
        existing: set[str] = set()
        try:
            import sqlite3
            db = get_db_path()
            if db.exists():
                with sqlite3.connect(str(db)) as con:
                    for row in con.execute(
                        "SELECT run_name FROM runs WHERE instrument = ?",
                        (self._name,),
                    ):
                        existing.add(row[0])
                    try:
                        for row in con.execute(
                            "SELECT run_name FROM sample_health "
                            "WHERE instrument = ?",
                            (self._name,),
                        ):
                            existing.add(row[0])
                    except sqlite3.OperationalError:
                        # sample_health table may not exist on older DBs
                        pass
        except Exception:
            logger.debug("startup-scan DB lookup failed", exc_info=True)

        n_registered = 0
        n_skipped_known = 0
        n_skipped_old = 0
        # v0.2.163: collect candidate paths first, then process
        # newest-first so the operator sees recent acquisitions
        # in the dashboard before the catchup chews through
        # month-old files. Brett 2026-04-22: 434 trackers at start
        # meant recent QCs were buried at the bottom of the queue.
        candidates: list[tuple[float, Path, bool]] = []  # (mtime, path, is_d_dir)
        try:
            for p in watch.rglob("*"):
                # Skip anything inside a .d directory — its nested
                # files (analysis.tdf etc.) aren't raw files to track.
                if any(parent.suffix == ".d" for parent in p.parents):
                    continue
                # Bruker .d is a directory; Thermo .raw is a file.
                is_d_dir = p.is_dir() and p.suffix == ".d"
                is_raw = p.is_file() and p.suffix in exts and p.suffix != ".d"
                if not (is_d_dir or is_raw):
                    continue
                if p.suffix not in exts:
                    continue
                if p.name in existing:
                    n_skipped_known += 1
                    continue
                # v0.2.158: for .d directories, use analysis.tdf mtime
                # instead of directory mtime. Directory mtime updates
                # any time something inside the .d gets touched
                # (Bruker bookkeeping, STAN metadata reads, Hive sync),
                # which made year-old acquisitions appear "fresh" and
                # get queued past the 30-day cutoff. 477 bogus trackers
                # registered on Brett's timsTOF 2026-04-22. analysis.tdf
                # is written once at acquisition end and not modified
                # afterwards, so its mtime is the real acquisition time.
                try:
                    if is_d_dir:
                        tdf = p / "analysis.tdf"
                        if tdf.exists():
                            mt = tdf.stat().st_mtime
                        else:
                            # No analysis.tdf: incomplete or corrupt .d
                            # — skip rather than guess.
                            continue
                    else:
                        mt = p.stat().st_mtime
                except OSError:
                    continue
                if mt < cutoff:
                    n_skipped_old += 1
                    continue
                candidates.append((mt, p, bool(is_d_dir)))
        except Exception:
            logger.debug("startup-scan walk failed", exc_info=True)

        # Register newest first so the dashboard fills in with recent
        # data before the catchup grinds through last month.
        candidates.sort(key=lambda t: t[0], reverse=True)
        for _mt, p, _is_d in candidates:
            # Hand off to the same handler path a normal on_created
            # event would take - honors qc_only, qc_pattern,
            # exclude_pattern, monitor_all_files uniformly.
            self._handler._register_tracker(p, ev_kind="startup_scan")
            n_registered += 1

        if n_registered or n_skipped_known or n_skipped_old:
            logger.info(
                "watcher: startup scan on %s - registered %d new, "
                "skipped %d already-known, skipped %d too-old "
                "(lookback=%sd)",
                self._name, n_registered, n_skipped_known,
                n_skipped_old, catchup_days,
            )
            # v0.2.157: fix NameError on max_age_min (leftover from the
            # v0.2.101 minutes-based lookback). That NameError was
            # silently eaten by the outer try/except in __init__, so
            # catchup APPEARED to do nothing because the summary event
            # never got recorded — operator had no visibility.
            self._record_event(
                "startup_scan_registered", watch,
                f"count={n_registered} lookback_days={catchup_days}",
            )

    def stop(self) -> None:
        """Stop the observer and stability loop."""
        self._stop_event.set()
        self._observer.stop()
        self._observer.join(timeout=5)
        if self._stability_thread and self._stability_thread.is_alive():
            self._stability_thread.join(timeout=5)
        logger.info("Stopped watching: %s", self._name)

    def _stability_loop(self) -> None:
        """Periodically check all trackers and trigger on stable acquisitions."""
        while not self._stop_event.is_set():
            stable_paths: list[str] = []

            with self._lock:
                for key, tracker in self._trackers.items():
                    # Every pass, record the tracker's current state so
                    # watcher_debug can tell the difference between "no
                    # events" and "events arriving but never stable".
                    try:
                        size = getattr(tracker, "last_size", None)
                    except Exception:
                        size = None
                    self._record_event(
                        "stability_poll", tracker.path,
                        f"last_size={size}",
                    )
                    if tracker.check():
                        stable_paths.append(key)
                        self._record_event("stable", tracker.path, "")
                        # v0.2.158: log stability fires at INFO so the
                        # syncable log shows them. Previously these only
                        # went to the internal event deque (debug_snapshot)
                        # which isn't part of the synced watch log -
                        # Brett's 2026-04-22 log showed 477 trackers
                        # registered and zero stability events, making
                        # it look like the pipeline was stuck when in
                        # fact the events just weren't logged.
                        logger.info(
                            "watcher: file stable, dispatching: %s (mode=%s)",
                            tracker.path.name,
                            self._tracker_modes.get(key, "qc"),
                        )

            for key in stable_paths:
                with self._lock:
                    tracker = self._trackers.pop(key, None)
                    mode = self._tracker_modes.pop(key, "qc")
                if tracker is not None:
                    self._record_event(
                        "acquisition_complete_start", tracker.path,
                        f"mode={mode}",
                    )
                    try:
                        if mode == "monitor":
                            self._on_monitor_complete(tracker.path)
                        else:
                            self._on_acquisition_complete(tracker.path)
                        self._record_event(
                            "acquisition_complete_end", tracker.path, "ok",
                        )
                        logger.info(
                            "watcher: acquisition_complete OK: %s",
                            tracker.path.name,
                        )
                    except Exception as e:
                        # _on_acquisition_complete catches most things,
                        # but belt-and-braces so a raise here doesn't
                        # kill the stability thread.
                        logger.exception(
                            "Unhandled error in _on_acquisition_complete "
                            "for %s", tracker.path.name,
                        )
                        self._record_event(
                            "acquisition_complete_exception",
                            tracker.path, f"{type(e).__name__}: {e}",
                        )

            self._stop_event.wait(timeout=10)

    def _on_monitor_complete(self, path: Path) -> None:
        """Sample Health Monitor path — runs only for non-QC, non-excluded
        files when `monitor_all_files: true`. Extracts rawmeat metrics,
        classifies verdict, and stores in `sample_health`. Never runs a
        search, never writes a HOLD flag, never submits anywhere. The
        point is just to surface bad injections in the dashboard for
        the operator to review.

        Thermo support is TBD — `rawmeat.py` is currently Bruker-only.
        For Thermo files we skip with a recorded event so the operator
        can see the monitor fired but had no data source."""
        vendor = self._config.get("vendor", "").lower()
        if vendor not in ("bruker", "thermo"):
            self._record_event(
                "monitor_skip_unknown_vendor", path, f"vendor={vendor!r}",
            )
            return

        from stan.db import (
            init_db, insert_sample_health,
            rolling_median_ms1_max_intensity,
        )
        from stan.metrics.rawmeat import (
            evaluate_sample_health,
            extract_rawmeat_metrics,
            extract_rawmeat_thermo,
        )

        init_db()
        if vendor == "bruker":
            rawmeat = extract_rawmeat_metrics(path)
        else:  # thermo
            rawmeat = extract_rawmeat_thermo(path)
        if not rawmeat:
            self._record_event(
                "monitor_rawmeat_empty", path,
                "analysis.tdf unreadable or empty Frames table",
            )
            return

        rolling_median = rolling_median_ms1_max_intensity(self._name)
        verdict = evaluate_sample_health(
            rawmeat,
            rolling_median_max_intensity=rolling_median,
        )

        run_date = rawmeat.get("metadata", {}).get("acquisition_date") or ""
        if not run_date:
            run_date = datetime.now(timezone.utc).isoformat()

        health_id = insert_sample_health(
            instrument=self._name,
            run_name=path.name,
            run_date=run_date,
            raw_path=str(path),
            verdict=verdict["verdict"],
            reasons=verdict["reasons"],
            rawmeat_summary=rawmeat.get("summary", {}),
        )

        # Best-effort TIC extraction so the dashboard's Sample / Blank
        # facets in Today's TIC overlay can render the trace alongside
        # the QC TICs. Wrapped in try/except — TIC is dashboard-only,
        # any failure must NOT propagate and break the monitor pipeline.
        try:
            from stan.db import insert_health_tic_trace
            from stan.metrics.tic import (
                downsample_trace, extract_tic_bruker, extract_tic_thermo,
            )
            tic = (extract_tic_bruker(path) if vendor == "bruker"
                   else extract_tic_thermo(path))
            if tic is not None and health_id:
                tic = downsample_trace(tic, n_bins=128)
                insert_health_tic_trace(health_id, tic.rt_min, tic.intensity)
        except Exception:
            logger.debug("monitor TIC extraction failed for %s", path.name, exc_info=True)

        # v0.2.163: PEG now covers both vendors (via read_ms1_any).
        # Drift is still Bruker-only and skipped internally for .raw.
        # Brett asked for "every sample" coverage, not just QC.
        if health_id:
            self._run_peg_and_drift(path, health_id, table="sample_health")

        logger.info(
            "monitor: %s → %s (%s)",
            path.name, verdict["verdict"],
            "; ".join(verdict["reasons"]) or "no issues",
        )
        self._record_event(
            f"monitor_{verdict['verdict']}", path,
            "; ".join(verdict["reasons"]) or "no issues",
        )

    def _on_acquisition_complete(self, path: Path) -> None:
        """Handle a completed acquisition: validate, detect mode, dispatch search.

        Mode resolution order:
        1. forced_mode in config (recommended for Thermo — use separate watch dirs)
        2. Auto-detect from raw file metadata (reliable for Bruker .d)
        """
        logger.info("Acquisition complete: %s", path.name)

        # Validate the raw file BEFORE attempting search — incomplete or
        # corrupt files cause DIA-NN/Sage to crash with cryptic errors.
        from stan.watcher.validate_raw import RawFileValidationError, validate_raw_file
        try:
            validate_raw_file(path, vendor=self._config.get("vendor"))
        except RawFileValidationError as e:
            logger.error("Invalid raw file, skipping: %s", e)
            # Write a HOLD flag with the validation failure reason
            try:
                from stan.gating.queue import write_hold_flag
                from stan.gating.evaluator import GateDecision, GateResult
                decision = GateDecision(
                    result=GateResult.FAIL,
                    failed_gates=["raw_file_invalid"],
                    diagnosis=str(e),
                )
                write_hold_flag(
                    output_dir=Path(self._config.get("output_dir", "")) / path.stem,
                    decision=decision,
                    run_name=path.name,
                )
            except Exception:
                logger.exception("Failed to write HOLD flag for invalid file")
            return

        forced = self._config.get("forced_mode", "").lower()
        if forced:
            mode = _resolve_forced_mode(forced, self._config.get("vendor", ""))
            logger.info("Using forced mode: %s for %s", mode.value, path.name)
        else:
            mode = detect_mode(
                path,
                vendor=self._config.get("vendor", ""),
                trfp_path=self._config.get("trfp_path"),
                output_dir=self._config.get("output_dir"),
            )

        if mode == AcquisitionMode.UNKNOWN:
            logger.warning(
                "Could not detect acquisition mode for %s — skipping. "
                "For Thermo instruments, set 'forced_mode: dia' or 'forced_mode: dda' "
                "in instruments.yml instead of relying on auto-detection.",
                path.name,
            )
            return

        logger.info("Detected mode: %s for %s", mode.value, path.name)

        # Import here to avoid circular imports at module level
        from stan.search.dispatcher import dispatch_search

        try:
            result_path = dispatch_search(
                raw_path=path,
                mode=mode,
                instrument_config=self._config,
            )
            if result_path is not None:
                self._store_run(path, mode, result_path)
        except Exception as e:
            logger.exception("Search dispatch failed for %s", path.name)
            from stan.telemetry import report_error
            report_error(e, {
                "vendor": self._config.get("vendor"),
                "raw_file_name": path.stem,
                "acquisition_mode": mode.value if mode != AcquisitionMode.UNKNOWN else None,
            })

    def _run_peg_and_drift(self, d_path: Path, row_id: str, table: str) -> None:
        """Compute PEG score + DIA window drift for a Bruker .d and
        write both to the given row's table.

        Shared between the QC (runs) and sample_health (monitor) paths
        so every acquisition gets contamination + drift coverage,
        regardless of whether DIA-NN ran on it. Takes ~60–120s on a
        typical timsTOF .d (two passes over MS1 frames via alphatims);
        alphatims HDF-caches the parse between calls so the second
        analysis is cheaper.

        Fully best-effort. Any failure at any stage is logged at DEBUG
        and the pipeline continues — columns stay NULL, operator sees
        a badge-less row rather than a crashed watcher.
        """
        try:
            from stan.metrics.peg import detect_peg_in_spectra
            from stan.metrics.peg_io import read_ms1_any, PegReaderUnavailable
            from stan.metrics.window_drift import detect_window_drift
            from stan.db import (
                update_peg_result, update_drift_result,
                insert_peg_ion_hits, insert_drift_window_centroids,
                insert_drift_peak_cloud,
            )
        except Exception:
            logger.debug(
                "PEG/drift imports failed; skipping for %s", d_path.name,
                exc_info=True,
            )
            return

        # v0.2.163: PEG works for both Bruker and Thermo via read_ms1_any
        # (routes to read_ms1_bruker for .d, read_ms1_thermo for .raw).
        # Drift is still Bruker-only (diaPASEF isolation windows don't
        # exist on Orbitrap).
        is_bruker = d_path.is_dir() and d_path.suffix == ".d"

        # PEG - reuses the backfill pipeline
        peg_reader_available = True
        try:
            spectra = list(read_ms1_any(d_path))
            peg = detect_peg_in_spectra(spectra)
            update_peg_result(
                run_id=row_id,
                peg_score=peg.peg_score,
                peg_n_ions_detected=peg.n_ions_detected,
                peg_intensity_pct=peg.intensity_pct,
                peg_class=peg.peg_class,
                table=table,
            )
            # v0.2.147: also persist the per-ion breakdown so the
            # dashboard can render a lollipop chart. Dedup'd to one
            # row per (repeat_n, adduct, charge) by insert_peg_ion_hits.
            try:
                insert_peg_ion_hits(run_id=row_id, matches=peg.matches, table=table)
            except Exception:
                logger.debug(
                    "PEG breakdown write failed for %s", d_path.name,
                    exc_info=True,
                )
            if peg.peg_class in ("moderate", "heavy"):
                logger.info(
                    "watcher: PEG %s on %s (score %.1f, %d ions)",
                    peg.peg_class, d_path.name, peg.peg_score,
                    peg.n_ions_detected,
                )
        except PegReaderUnavailable:
            logger.warning("alphatims missing - PEG + drift skipped for %s", d_path.name)
            peg_reader_available = False
            # v0.2.160: still mark peg_class="unknown" so the dashboard
            # surfaces the degraded state instead of a NULL cell.
            try:
                update_peg_result(
                    run_id=row_id, peg_score=0.0,
                    peg_n_ions_detected=0, peg_intensity_pct=0.0,
                    peg_class="unknown", table=table,
                )
            except Exception:
                pass
        except Exception as _e:
            # Other exceptions (alphatims init ValueError from 1.0.9,
            # corrupt .d, etc.) - write unknown so operator sees
            # the degraded state.
            logger.warning(
                "PEG detection failed for %s (%s: %s)",
                d_path.name, type(_e).__name__, _e,
            )
            try:
                update_peg_result(
                    run_id=row_id, peg_score=0.0,
                    peg_n_ions_detected=0, peg_intensity_pct=0.0,
                    peg_class="unknown", table=table,
                )
            except Exception:
                pass

        # v0.2.160: only bail out of drift if the reader is genuinely
        # unavailable (alphatims not installed at all). A per-file
        # exception in PEG shouldn't prevent drift from trying on
        # other files - and drift has its own try/except around init.
        if not peg_reader_available:
            return

        # v0.2.163: drift is Bruker-only (diaPASEF isolation windows
        # don't exist on Orbitrap). Skip the drift pass entirely for
        # Thermo .raw files.
        if not is_bruker:
            return

        # DIA window drift
        try:
            drift = detect_window_drift(d_path)
            # v0.2.160: always write the drift result, even when
            # drift_class="unknown" (alphatims init failed, no DIA
            # windows found, etc.). Previously we skipped the write
            # on unknown, leaving drift_class NULL in the DB - which
            # looks identical to "drift never ran" in the dashboard
            # and hides the degraded-extraction signal. Writing
            # "unknown" surfaces the real state.
            update_drift_result(
                run_id=row_id,
                drift_coverage=drift.global_coverage,
                drift_median_im=drift.median_drift_im,
                drift_p90_abs_im=drift.p90_abs_drift_im,
                drift_class=drift.drift_class,
                table=table,
            )
            if drift.drift_class != "unknown":
                # v0.2.147: per-window breakdown for the drift scatter
                # chart. Only meaningful when we actually computed
                # windows (i.e. drift_class != "unknown").
                try:
                    insert_drift_window_centroids(
                        run_id=row_id, per_window=drift.per_window, table=table,
                    )
                except Exception:
                    logger.debug(
                        "drift breakdown write failed for %s", d_path.name,
                        exc_info=True,
                    )
                # v0.2.173: persist downsampled cloud for the Bruker
                # DataAnalysis-style m/z x 1/K0 visualization.
                try:
                    if drift.cloud_mz:
                        insert_drift_peak_cloud(
                            run_id=row_id,
                            mz=drift.cloud_mz, im=drift.cloud_im,
                            log_intensity=drift.cloud_log_intensity,
                            table=table,
                        )
                except Exception:
                    logger.debug(
                        "drift cloud write failed for %s", d_path.name,
                        exc_info=True,
                    )
                if drift.drift_class in ("warn", "drifted"):
                    logger.info(
                        "watcher: window drift %s on %s "
                        "(coverage %.1f%%, median drift %+.3f /K0)",
                        drift.drift_class, d_path.name,
                        100 * drift.global_coverage, drift.median_drift_im,
                    )
        except Exception:
            logger.debug("drift detection failed for %s", d_path.name, exc_info=True)

    def _store_run(
        self, raw_path: Path, mode: AcquisitionMode, result_path: Path
    ) -> None:
        """Extract metrics and store in the local database."""
        from stan.db import insert_run
        from stan.gating.evaluator import evaluate_gates
        from stan.metrics.extractor import extract_dda_metrics, extract_dia_metrics

        # v0.2.213: resolve SPD + gradient up-front so the extractor
        # can compute peak_capacity. Pre-fix the extractor was called
        # without gradient_min and TIMS rows always had peak_capacity
        # NULL because instruments.yml only set the Evosep SPD, not a
        # per-run gradient length.
        resolved_spd = self._resolve_spd(raw_path)
        gradient_min = self._config.get("gradient_length_min")
        if not gradient_min and resolved_spd:
            # Snap SPD → typical Evosep gradient length so peak_capacity
            # can be computed for any TIMS / Whisper / Vanquish-Neo run
            # whose config didn't pin a gradient.
            _SPD_TO_GRAD = {200: 6, 100: 11, 60: 21, 40: 30, 30: 44, 15: 88}
            for s, g in _SPD_TO_GRAD.items():
                if resolved_spd >= s:
                    gradient_min = g
                    break

        try:
            if is_dia(mode):
                metrics = extract_dia_metrics(
                    str(result_path),
                    raw_path=raw_path,
                    vendor=self._config.get("vendor"),
                    gradient_min=gradient_min,
                )
                from stan.metrics.chromatography import compute_ips_dia
                metrics["instrument_family"] = self._config.get("family") or self._config.get("vendor_family")
                metrics["spd"] = resolved_spd
                metrics["gradient_length_min"] = gradient_min
                metrics["ips_score"] = compute_ips_dia(metrics)
            else:
                metrics = extract_dda_metrics(
                    str(result_path),
                    gradient_min=gradient_min or 60,
                )
                from stan.metrics.chromatography import compute_ips_dda
                metrics["instrument_family"] = self._config.get("family") or self._config.get("vendor_family")
                metrics["spd"] = resolved_spd
                metrics["gradient_length_min"] = gradient_min
                metrics["ips_score"] = compute_ips_dda(metrics)

            # v0.2.212: populate lc_system from raw metadata so the live
            # path matches baseline. Pre-fix this column was always blank
            # because daemon._store_run never called detect_lc_system,
            # only baseline.py did.
            try:
                from stan.metrics.scoring import detect_lc_system
                lc_sys = detect_lc_system(raw_path)
                if lc_sys:
                    metrics["lc_system"] = lc_sys
            except Exception:
                logger.debug("LC system detect failed for %s", raw_path.name, exc_info=True)

            # v0.2.223: copy operator-set column metadata from
            # instruments.yml onto every QC row. The setup wizard
            # already collects column_vendor + column_model on first
            # install (see stan/setup.py::_pick_column), but pre-fix
            # the watcher never read them back into the metrics dict
            # so every runs row landed with NULL columns. Brett's
            # `stan set-column` keeps these fresh between installs.
            cv = self._config.get("column_vendor")
            cm = self._config.get("column_model")
            if cv:
                metrics["column_vendor"] = cv
            if cm:
                metrics["column_model"] = cm

            # Resolve acquisition mode string for threshold lookup
            acq_mode = "dia" if is_dia(mode) else "dda"

            decision = evaluate_gates(
                metrics=metrics,
                instrument_model=self._config.get("model", ""),
                acquisition_mode=acq_mode,
            )

            # Acquisition date from raw file metadata (Bruker analysis.tdf
            # GlobalMetadata.AcquisitionDateTime or Thermo .raw header via
            # fisher_py). Falls back to file mtime only if both fail —
            # mtime can be wrong after copies/archive moves.
            from stan.watcher.acquisition_date import get_acquisition_date
            raw_mtime = get_acquisition_date(raw_path)
            if not raw_mtime:
                from datetime import datetime, timezone
                try:
                    raw_mtime = datetime.fromtimestamp(
                        raw_path.stat().st_mtime, tz=timezone.utc
                    ).isoformat()
                except Exception:
                    raw_mtime = None

            # v0.2.229: never let "auto"/"unknown"/"" leak into the
            # runs row — re-resolve from the raw file in hand if the
            # config name didn't yield a real model. This eliminates
            # the "two cards on the dashboard" issue Brett saw on
            # the Lumos: 14 recent runs landed as "auto" while older
            # runs (resolved successfully at startup) had the proper
            # model name.
            inst_name = (self._config.get("name") or "").strip()
            if not inst_name or inst_name.lower() in ("auto", "unknown"):
                resolved = _model_from_raw(raw_path)
                if resolved:
                    inst_name = resolved

            run_id = insert_run(
                instrument=inst_name or "unknown",
                run_name=raw_path.name,
                raw_path=str(raw_path),
                mode=mode.value,
                metrics=metrics,
                gate_result=decision.result.value,
                failed_gates=decision.failed_gates,
                diagnosis=decision.diagnosis,
                amount_ng=self._config.get("hela_amount_ng", 50.0),
                spd=metrics.get("spd"),
                gradient_length_min=gradient_min,
                run_date=raw_mtime,
            )

            # v0.2.163: PEG now covers both vendors via read_ms1_any.
            # Drift is still Bruker-only and skipped internally for .raw.
            # Best-effort - never propagates exceptions because the QC
            # row is already saved with full metrics and failure here
            # just means empty peg/drift columns.
            if run_id:
                self._run_peg_and_drift(raw_path, run_id, table="runs")

                # v0.2.212: extract + persist the TIC trace as part of
                # the live ingest path. Pre-fix the live watcher never
                # stored the TIC and we relied on `stan backfill-tic`
                # running periodically to fill in the gaps. That created
                # a window where the latest QC row had no tic_traces
                # child until backfill ran, and on TIMS the latest row
                # ended up empty because the chain had finished.
                try:
                    from stan.metrics.tic import (
                        extract_tic_bruker, extract_tic_thermo,
                        downsample_trace,
                    )
                    from stan.db import insert_tic_trace
                    trace = None
                    if raw_path.is_dir() and raw_path.suffix.lower() == ".d":
                        trace = extract_tic_bruker(raw_path)
                    elif raw_path.is_file() and raw_path.suffix.lower() == ".raw":
                        trace = extract_tic_thermo(raw_path)
                    if trace and trace.rt_min and trace.intensity:
                        trace = downsample_trace(trace, n_bins=128)
                        insert_tic_trace(run_id, trace.rt_min, trace.intensity)
                except Exception:
                    logger.debug(
                        "TIC extraction failed for %s",
                        raw_path.name, exc_info=True,
                    )

            if decision.result.value == "fail":
                from stan.gating.queue import write_hold_flag
                write_hold_flag(
                    output_dir=Path(self._config.get("output_dir", "")) / raw_path.stem,
                    decision=decision,
                    run_name=raw_path.name,
                )

        except Exception:
            logger.exception("Failed to store run for %s", raw_path.name)


class WatcherDaemon:
    """Manages multiple InstrumentWatchers with config hot-reload."""

    def __init__(self) -> None:
        self._watchers: dict[str, InstrumentWatcher] = {}
        self._config_watcher: ConfigWatcher | None = None
        self._stop_event = threading.Event()

    def run(self) -> None:
        """Blocking main loop. Start all watchers and poll for config changes."""
        # Register in the module-level singleton so stan.control can
        # reach the running daemon's state for the `watcher_debug`
        # diagnostic action.
        global _ACTIVE_DAEMON
        _ACTIVE_DAEMON = self

        # Register signal handlers for clean shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        config_path = resolve_config_path("instruments.yml")
        self._config_watcher = ConfigWatcher(config_path)

        self._apply_config(self._config_watcher.data)

        if not self._watchers:
            logger.info("No enabled instruments configured. Waiting for config changes...")

        tick = 0
        # v0.2.198: keep-alive ping to the HF relay. Spaces sleep after
        # 48h idle; when they sleep the first request returns a 200 HTML
        # loading page which the backfill-tic --push code counted as
        # successful commits for MONTHS (root cause of the 590 sawtooth
        # rows stuck on HF). Every 12 hours we hit /api/health — tiny
        # cost, bulletproof prevention.
        import time as _hf_time
        _hf_last_ping = 0.0
        _HF_PING_INTERVAL_S = 12 * 3600
        while not self._stop_event.is_set():
            # Remote restart request (from stan.control.restart_watcher).
            # The presence of ~/.stan/restart.flag means someone on the
            # mirror asked this daemon to exit cleanly — consume the
            # flag and stop. If start_stan_loop.bat (or equivalent) is
            # supervising, a fresh process picks up the current code.
            try:
                from stan.config import get_user_config_dir
                flag = get_user_config_dir() / "restart.flag"
                if flag.exists():
                    logger.info("Remote restart requested — exiting cleanly")
                    try:
                        flag.unlink()
                    except OSError:
                        pass
                    self._stop_event.set()
                    break
            except Exception:
                logger.debug("restart-flag check failed", exc_info=True)

            # Check for config changes
            if self._config_watcher.is_stale():
                logger.info("instruments.yml changed — reloading")
                self._config_watcher.reload()
                self._apply_config(self._config_watcher.data)

            # Poll the Hive mirror for remote-control commands (diagnostic
            # whitelist only — see stan.control). Swallows all errors so a
            # broken share cannot take the watcher down.
            try:
                from stan.control import poll_once
                poll_once()
            except Exception:
                logger.debug("control: poll_once failed", exc_info=True)

            # Heartbeat: write status.json to the mirror every ~5 minutes
            # so `stan fleet-status` on any workstation can see whether
            # this instrument is alive and what state it's in.
            if tick % 10 == 0:
                try:
                    _write_heartbeat()
                except Exception:
                    logger.debug("control: heartbeat failed", exc_info=True)

            # v0.2.198: HF Space keep-alive ping. See the comment above
            # the _hf_last_ping init for rationale.
            if _hf_time.time() - _hf_last_ping > _HF_PING_INTERVAL_S:
                try:
                    import urllib.request
                    from stan.community.submit import RELAY_URL
                    with urllib.request.urlopen(
                        f"{RELAY_URL}/api/health", timeout=10
                    ) as _r:
                        _r.read(256)   # drain + discard
                    logger.info("HF relay keep-alive ping: ok")
                except Exception as _e:
                    logger.warning("HF relay keep-alive ping failed: %s", _e)
                _hf_last_ping = _hf_time.time()

            tick += 1
            self._stop_event.wait(timeout=CONFIG_POLL_INTERVAL)

        self._stop_all()

    def stop(self) -> None:
        """Signal the daemon to stop."""
        self._stop_event.set()

    def _signal_handler(self, signum: int, frame) -> None:
        logger.info("Received signal %d — shutting down", signum)
        self.stop()

    def _auto_merge_aliases(self, config: dict) -> None:
        """For each instrument with an ``aliases:`` list, rewrite any
        DB rows whose ``instrument`` column matches an alias to the
        canonical ``name``. Fixes the "two cards for one physical
        instrument" problem that otherwise recurs whenever a rename
        happens out of sync with new acquisitions coming in (e.g. a
        17-minute race between apply_config and fix_instrument_names
        bit us 2026-04-23 on TIMS — 3 runs + 13 sample_health rows
        landed under the stale name during the gap).

        Runs on daemon startup and on every config hot-reload
        (v0.2.183). Idempotent — a no-op when nothing matches.
        """
        import sqlite3
        from stan.db import get_db_path, init_db

        try:
            init_db()
            db = get_db_path()
            if not db.exists():
                return
        except Exception:
            logger.debug("auto-merge init_db failed", exc_info=True)
            return

        for inst in config.get("instruments", []) or []:
            if not isinstance(inst, dict):
                continue
            name = inst.get("name")
            aliases = inst.get("aliases") or []
            if not (isinstance(name, str) and name and isinstance(aliases, list)):
                continue
            aliases = [a for a in aliases
                       if isinstance(a, str) and a and a != name]
            if not aliases:
                continue
            total_merged = 0
            try:
                with sqlite3.connect(str(db)) as con:
                    for alias in aliases:
                        for table in ("runs", "sample_health"):
                            try:
                                r = con.execute(
                                    f"UPDATE {table} SET instrument = ? "
                                    f"WHERE instrument = ?",
                                    (name, alias),
                                )
                                total_merged += r.rowcount
                            except sqlite3.OperationalError:
                                pass
                    con.commit()
            except Exception:
                logger.warning("auto-merge alias sweep failed for %s",
                               name, exc_info=True)
                continue
            if total_merged:
                logger.info(
                    "Auto-merged %d rows from aliases %s into %s",
                    total_merged, aliases, name,
                )

    def _apply_config(self, config: dict) -> None:
        """Diff current watchers against config and add/remove as needed."""
        # v0.2.183: auto-merge any legacy-named rows into the canonical
        # name BEFORE spinning up new watchers, so the dashboard never
        # sees two cards for a renamed instrument.
        self._auto_merge_aliases(config)

        instruments = config.get("instruments", [])
        enabled = {
            inst["name"]: inst
            for inst in instruments
            if inst.get("enabled", False)
        }

        # Stop watchers for removed or disabled instruments
        to_remove = [name for name in self._watchers if name not in enabled]
        for name in to_remove:
            self._watchers[name].stop()
            del self._watchers[name]
            logger.info("Removed watcher: %s", name)

        # Start watchers for new instruments
        for name, inst_config in enabled.items():
            if name not in self._watchers:
                watcher = InstrumentWatcher(inst_config)
                watcher.start()
                self._watchers[name] = watcher

        active = len(self._watchers)
        logger.info("Active watchers: %d", active)

    def _stop_all(self) -> None:
        """Stop all instrument watchers."""
        for watcher in self._watchers.values():
            watcher.stop()
        self._watchers.clear()
        logger.info("All watchers stopped")


def get_active_daemon() -> "WatcherDaemon | None":
    """Return the running WatcherDaemon singleton, if any. Used by
    stan.control to surface watcher state in diagnostic actions."""
    return _ACTIVE_DAEMON


def _write_heartbeat() -> None:
    """Write status.json to the Hive mirror so `stan fleet-status` can
    see whether this instrument is alive and current, and mirror the
    current instrument config YAMLs so a remote operator can read them.

    Atomic write: temp file + os.replace so a reader never sees a
    half-written JSON blob.
    """
    import json
    import os

    from stan.config import get_hive_mirror_dir
    from stan.control import _action_status, upload_configs

    mirror = get_hive_mirror_dir()
    if mirror is None:
        return

    payload = _action_status({})
    out = mirror / "status.json"
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    os.replace(tmp, out)

    # Also sync config YAMLs so a remote operator can see what this
    # instrument is actually running (and edit them via apply_config).
    try:
        upload_configs(mirror)
    except Exception:
        logger.debug("heartbeat: config upload failed", exc_info=True)


def _resolve_forced_mode(forced: str, vendor: str) -> AcquisitionMode:
    """Convert a forced_mode string to an AcquisitionMode enum.

    Args:
        forced: "dia" or "dda" (case-insensitive).
        vendor: "bruker" or "thermo" — determines which enum variant to use.
    """
    forced = forced.strip().lower()
    if forced == "dia":
        return (
            AcquisitionMode.DIA_PASEF if vendor == "bruker"
            else AcquisitionMode.DIA_ORBITRAP
        )
    if forced == "dda":
        return (
            AcquisitionMode.DDA_PASEF if vendor == "bruker"
            else AcquisitionMode.DDA_ORBITRAP
        )
    return AcquisitionMode.UNKNOWN
