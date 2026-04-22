"""Streaming telemetry helpers for long-running backfills (v0.2.153).

The drift backfill on Brett's timsTOF 2026-04-22 ran for 2.5 hours and
hit 220 consecutive ValueErrors (alphatims/polars compat break), with
zero feedback to the Hive mirror until the end. This module adds three
escape-hatches so future systemic bugs get caught early:

1. ``PeriodicSync`` — calls ``sync_to_hive_mirror`` every N rows or
   M seconds so a backfill's in-progress log shows up on Hive without
   waiting for the loop to finish.

2. ``AbortIfRepeating`` — tracks consecutive errors by error class.
   If the same exception type fires N times in a row, aborts the loop
   and writes an alert file. Prevents the "220 of 220 errored" scenario
   by cutting the loop at the 10th identical failure.

3. ``write_alert`` — drops a JSON alert into ``~/STAN/alerts/`` which
   syncs to Hive immediately. Intended for high-signal events (watcher
   crashed, systemic backfill abort, relay auth rejected) that
   shouldn't wait for the next backfill-end sync.

Deliberately separate from ``stan/telemetry.py`` (the opt-in anonymous
error reporter) — this module runs locally, always on, and targets the
Hive mirror for autonomous Claude troubleshooting. No user-PII leaves
the lab.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Safe defaults. Override per caller for workloads with expected high
# skip rates (e.g. an optional metric extractor that legitimately misses
# 30% of rows — don't use AbortIfRepeating for those).
DEFAULT_CONSECUTIVE_ERROR_THRESHOLD = 10
DEFAULT_SYNC_EVERY_N_ROWS = 25
DEFAULT_SYNC_EVERY_SECONDS = 120


class AbortedForRepeatingErrors(RuntimeError):
    """Raised to short-circuit a backfill loop when the same error class
    fires repeatedly — almost always a systemic break (library compat,
    config drift, network outage) rather than per-row data issues."""


class AbortIfRepeating:
    """Track consecutive errors by error_type and raise when the run
    has clearly gone off the rails.

    Usage::

        guard = AbortIfRepeating(threshold=10, run_label="backfill-drift")
        for row in rows:
            try:
                do_work(row)
                guard.record_success()
            except Exception as e:
                guard.record_error(e, context={"run_name": row.name})
    """

    def __init__(
        self,
        threshold: int = DEFAULT_CONSECUTIVE_ERROR_THRESHOLD,
        run_label: str = "backfill",
    ) -> None:
        self.threshold = threshold
        self.run_label = run_label
        self._current_error_type: str | None = None
        self._consecutive = 0

    def record_success(self) -> None:
        self._current_error_type = None
        self._consecutive = 0

    def record_error(
        self, exc: BaseException, context: dict[str, Any] | None = None
    ) -> None:
        err_type = type(exc).__name__
        if err_type == self._current_error_type:
            self._consecutive += 1
        else:
            self._current_error_type = err_type
            self._consecutive = 1

        if self._consecutive >= self.threshold:
            payload = {
                "run_label": self.run_label,
                "error_type": err_type,
                "error_message": str(exc),
                "consecutive_count": self._consecutive,
                "last_context": context or {},
            }
            write_alert(
                kind="consecutive_errors",
                summary=(
                    f"{self.run_label}: {self._consecutive} consecutive "
                    f"{err_type} errors — aborting to save remaining "
                    f"runs. Check the alert payload for the last error "
                    f"context."
                ),
                payload=payload,
            )
            raise AbortedForRepeatingErrors(
                f"{self.run_label}: {self._consecutive} consecutive "
                f"{err_type} errors. Most recent: {exc}"
            )


class PeriodicSync:
    """Call ``sync_to_hive_mirror`` on a schedule so in-progress logs
    show up on Hive without the operator waiting for the backfill to
    finish. Safe to call ``maybe_sync()`` on every iteration — internal
    counters decide when to actually fire.
    """

    def __init__(
        self,
        every_n_rows: int = DEFAULT_SYNC_EVERY_N_ROWS,
        every_seconds: float = DEFAULT_SYNC_EVERY_SECONDS,
    ) -> None:
        self.every_n_rows = every_n_rows
        self.every_seconds = every_seconds
        self._rows_since_sync = 0
        self._last_sync_time = time.monotonic()

    def maybe_sync(self) -> bool:
        """Return True if a sync was performed this call, else False."""
        self._rows_since_sync += 1
        now = time.monotonic()
        elapsed = now - self._last_sync_time
        if self._rows_since_sync >= self.every_n_rows or elapsed >= self.every_seconds:
            self._rows_since_sync = 0
            self._last_sync_time = now
            try:
                from stan.config import sync_to_hive_mirror
                sync_to_hive_mirror(include_reports=False)
                return True
            except Exception:
                logger.debug("periodic sync failed", exc_info=True)
        return False


def write_alert(
    kind: str,
    summary: str,
    payload: dict | None = None,
) -> Path | None:
    """Drop a high-signal alert into ~/STAN/alerts/ and sync immediately.

    Call this for events that shouldn't wait for the next backfill-end
    sync: watcher crashes, systemic backfill aborts, relay-auth
    rejections. Returns the alert path on success or None if writing
    failed (never raises — alerts are best-effort).
    """
    try:
        from stan.config import get_user_config_dir, sync_to_hive_mirror

        alert_dir = get_user_config_dir() / "alerts"
        alert_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_kind = "".join(c if c.isalnum() or c in "-_" else "_" for c in kind)
        path = alert_dir / f"{ts}_{safe_kind}.json"
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "summary": summary,
            "payload": payload or {},
        }
        path.write_text(json.dumps(record, indent=2))
        try:
            sync_to_hive_mirror(include_reports=False)
        except Exception:
            logger.debug("alert sync failed", exc_info=True)
        logger.warning("ALERT[%s]: %s", kind, summary)
        return path
    except Exception:
        logger.debug("write_alert failed", exc_info=True)
        return None
