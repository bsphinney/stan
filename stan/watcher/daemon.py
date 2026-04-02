"""Watchdog-based instrument watcher daemon.

Monitors raw data directories for new acquisitions, detects when files are stable,
identifies acquisition mode, and dispatches search jobs.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
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
from stan.watcher.stability import StabilityTracker

logger = logging.getLogger(__name__)


def _is_network_path(path: str) -> bool:
    """Detect UNC or network paths where native OS events may not work."""
    return path.startswith("\\\\") or path.startswith("//")


class _AcquisitionHandler(FileSystemEventHandler):
    """Watchdog event handler that creates StabilityTrackers for new raw files."""

    def __init__(
        self,
        instrument_config: dict,
        trackers: dict[str, StabilityTracker],
        lock: threading.Lock,
    ) -> None:
        super().__init__()
        self._config = instrument_config
        self._trackers = trackers
        self._lock = lock
        self._extensions = set(instrument_config.get("extensions", []))
        self._vendor = instrument_config.get("vendor", "")
        self._stable_secs = instrument_config.get("stable_secs", 60)

    def on_created(self, event) -> None:
        path = Path(event.src_path)

        # Bruker .d: directory creation event
        if isinstance(event, DirCreatedEvent) and path.suffix == ".d":
            if ".d" in self._extensions:
                self._register_tracker(path)

        # Thermo .raw: file creation event
        elif isinstance(event, FileCreatedEvent) and path.suffix in self._extensions:
            if path.suffix != ".d":
                self._register_tracker(path)

    def _register_tracker(self, path: Path) -> None:
        key = str(path)
        with self._lock:
            if key not in self._trackers:
                self._trackers[key] = StabilityTracker(
                    path=path,
                    vendor=self._vendor,
                    stable_secs=self._stable_secs,
                )
                logger.info("Tracking new acquisition: %s", path.name)


class InstrumentWatcher:
    """Watches a single instrument's raw data directory."""

    def __init__(self, instrument_config: dict) -> None:
        self._config = instrument_config
        self._name = instrument_config.get("name", "unknown")
        self._watch_dir = instrument_config.get("watch_dir", "")
        self._trackers: dict[str, StabilityTracker] = {}
        self._lock = threading.Lock()
        self._handler = _AcquisitionHandler(instrument_config, self._trackers, self._lock)

        # Use polling observer for network paths
        if _is_network_path(self._watch_dir):
            self._observer = PollingObserver(timeout=10)
        else:
            self._observer = Observer()

        self._stability_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def name(self) -> str:
        return self._name

    def start(self) -> None:
        """Start watching the directory and the stability check loop."""
        watch_path = Path(self._watch_dir)
        if not watch_path.exists():
            logger.warning(
                "Watch directory does not exist for %s: %s", self._name, self._watch_dir
            )
            return

        self._observer.schedule(self._handler, str(watch_path), recursive=False)
        self._observer.start()

        self._stop_event.clear()
        self._stability_thread = threading.Thread(
            target=self._stability_loop,
            name=f"stability-{self._name}",
            daemon=True,
        )
        self._stability_thread.start()
        logger.info("Started watching: %s → %s", self._name, self._watch_dir)

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
                    if tracker.check():
                        stable_paths.append(key)

            for key in stable_paths:
                with self._lock:
                    tracker = self._trackers.pop(key, None)
                if tracker is not None:
                    self._on_acquisition_complete(tracker.path)

            self._stop_event.wait(timeout=10)

    def _on_acquisition_complete(self, path: Path) -> None:
        """Handle a completed acquisition: detect mode and dispatch search.

        Mode resolution order:
        1. forced_mode in config (recommended for Thermo — use separate watch dirs)
        2. Auto-detect from raw file metadata (reliable for Bruker .d)
        """
        logger.info("Acquisition complete: %s", path.name)

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
        except Exception:
            logger.exception("Search dispatch failed for %s", path.name)

    def _store_run(
        self, raw_path: Path, mode: AcquisitionMode, result_path: Path
    ) -> None:
        """Extract metrics and store in the local database."""
        from stan.db import insert_run
        from stan.gating.evaluator import evaluate_gates
        from stan.metrics.extractor import extract_dda_metrics, extract_dia_metrics

        try:
            if is_dia(mode):
                metrics = extract_dia_metrics(str(result_path))
            else:
                metrics = extract_dda_metrics(str(result_path))

            gate_result, failed, diagnosis = evaluate_gates(
                metrics, mode.value, self._config.get("model", ""),
            )

            insert_run(
                instrument=self._config.get("name", "unknown"),
                run_name=raw_path.name,
                raw_path=str(raw_path),
                mode=mode.value,
                metrics=metrics,
                gate_result=gate_result,
                failed_gates=failed,
                diagnosis=diagnosis,
                amount_ng=self._config.get("hela_amount_ng", 50.0),
                gradient_length_min=self._config.get("gradient_length_min"),
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
        # Register signal handlers for clean shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        config_path = resolve_config_path("instruments.yml")
        self._config_watcher = ConfigWatcher(config_path)

        self._apply_config(self._config_watcher.data)

        if not self._watchers:
            logger.info("No enabled instruments configured. Waiting for config changes...")

        while not self._stop_event.is_set():
            # Check for config changes
            if self._config_watcher.is_stale():
                logger.info("instruments.yml changed — reloading")
                self._config_watcher.reload()
                self._apply_config(self._config_watcher.data)

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
