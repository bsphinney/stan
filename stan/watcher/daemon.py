"""Watchdog-based instrument watcher daemon.

Monitors raw data directories for new acquisitions, detects when files are stable,
identifies acquisition mode, and dispatches search jobs.
"""

from __future__ import annotations

import logging
import signal
import threading
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
        # Hard exclude — wash/blank/etc. are skipped at both paths
        if self._exclude_pattern and self._exclude_pattern.search(path.stem):
            self._on_event("exclude_pattern_match", path,
                           f"pattern={self._exclude_pattern.pattern!r}")
            return

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


class InstrumentWatcher:
    """Watches a single instrument's raw data directory."""

    def __init__(self, instrument_config: dict) -> None:
        import collections
        self._config = instrument_config
        self._name = instrument_config.get("name", "unknown")
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

        catchup_days = float(self._config.get("startup_catchup_days", 7))
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
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                if mt < cutoff:
                    continue
                # Hand off to the same handler path a normal on_created
                # event would take — honors qc_only, qc_pattern,
                # exclude_pattern, monitor_all_files uniformly.
                self._handler._register_tracker(p, ev_kind="startup_scan")
                n_registered += 1
        except Exception:
            logger.debug("startup-scan walk failed", exc_info=True)

        if n_registered or n_skipped_known:
            logger.info(
                "watcher: startup scan on %s → registered %d new, "
                "skipped %d already-known (lookback=%sd)",
                self._name, n_registered, n_skipped_known, catchup_days,
            )
            self._record_event(
                "startup_scan_registered", watch,
                f"count={n_registered} max_age_min={max_age_min}",
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

        # PEG + window drift on every Bruker sample_health row (Brett
        # asked for "every sample" coverage, not just QC). Best-effort,
        # same try/except pattern — if alphatims isn't installed the
        # pipeline keeps running and columns stay NULL.
        if health_id and vendor == "bruker":
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
            from stan.metrics.peg_io import read_ms1_bruker, PegReaderUnavailable
            from stan.metrics.window_drift import detect_window_drift
            from stan.db import update_peg_result, update_drift_result
        except Exception:
            logger.debug(
                "PEG/drift imports failed; skipping for %s", d_path.name,
                exc_info=True,
            )
            return

        # PEG — reuses the backfill pipeline
        try:
            spectra = list(read_ms1_bruker(d_path))
            peg = detect_peg_in_spectra(spectra)
            update_peg_result(
                run_id=row_id,
                peg_score=peg.peg_score,
                peg_n_ions_detected=peg.n_ions_detected,
                peg_intensity_pct=peg.intensity_pct,
                peg_class=peg.peg_class,
                table=table,
            )
            if peg.peg_class in ("moderate", "heavy"):
                logger.info(
                    "watcher: PEG %s on %s (score %.1f, %d ions)",
                    peg.peg_class, d_path.name, peg.peg_score,
                    peg.n_ions_detected,
                )
        except PegReaderUnavailable:
            logger.debug("alphatims missing — PEG skipped for %s", d_path.name)
            return  # drift also needs alphatims, bail out early
        except Exception:
            logger.debug("PEG detection failed for %s", d_path.name, exc_info=True)

        # DIA window drift
        try:
            drift = detect_window_drift(d_path)
            if drift.drift_class != "unknown":
                update_drift_result(
                    run_id=row_id,
                    drift_coverage=drift.global_coverage,
                    drift_median_im=drift.median_drift_im,
                    drift_p90_abs_im=drift.p90_abs_drift_im,
                    drift_class=drift.drift_class,
                    table=table,
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

        try:
            if is_dia(mode):
                metrics = extract_dia_metrics(
                    str(result_path),
                    raw_path=raw_path,
                    vendor=self._config.get("vendor"),
                )
                from stan.metrics.chromatography import compute_ips_dia
                metrics["instrument_family"] = self._config.get("family") or self._config.get("vendor_family")
                metrics["spd"] = self._config.get("spd")
                metrics["ips_score"] = compute_ips_dia(metrics)
            else:
                metrics = extract_dda_metrics(str(result_path))
                from stan.metrics.chromatography import compute_ips_dda
                metrics["instrument_family"] = self._config.get("family") or self._config.get("vendor_family")
                metrics["spd"] = self._config.get("spd")
                metrics["ips_score"] = compute_ips_dda(metrics)

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

            run_id = insert_run(
                instrument=self._config.get("name", "unknown"),
                run_name=raw_path.name,
                raw_path=str(raw_path),
                mode=mode.value,
                metrics=metrics,
                gate_result=decision.result.value,
                failed_gates=decision.failed_gates,
                diagnosis=decision.diagnosis,
                amount_ng=self._config.get("hela_amount_ng", 50.0),
                spd=self._config.get("spd"),
                gradient_length_min=self._config.get("gradient_length_min"),
                run_date=raw_mtime,
            )

            # PEG + DIA window drift on every QC run (Bruker only —
            # Thermo support follows in a later ship). Same best-effort
            # pattern as the monitor path; never propagates exceptions
            # because the QC row is already saved with full metrics
            # and failure here just means empty peg/drift columns.
            if run_id and (self._config.get("vendor") or "").lower() == "bruker":
                self._run_peg_and_drift(raw_path, run_id, table="runs")

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

            tick += 1
            self._stop_event.wait(timeout=CONFIG_POLL_INTERVAL)

        self._stop_all()

    def stop(self) -> None:
        """Signal the daemon to stop."""
        self._stop_event.set()

    def _signal_handler(self, signum: int, frame) -> None:
        logger.info("Received signal %d — shutting down", signum)
        self.stop()

    def _apply_config(self, config: dict) -> None:
        """Diff current watchers against config and add/remove as needed."""
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
