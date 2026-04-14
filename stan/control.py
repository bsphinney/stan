"""Remote-control command queue over the Hive mirror network drive.

`stan watch` polls the per-instrument mirror directory for command files
(a whitelisted set of diagnostic actions), executes them, and writes
results back to the same share. Nothing here touches the HF Space; the
transport is the existing access-controlled `Y:\\STAN\\<hostname>\\` drop.

Current whitelist is READ-ONLY on purpose: `status`, `tail_log`,
`export_db_snapshot`, `ping`. Destructive actions (updater, process
kill) will arrive in a later release once this channel is proven.

Command file format — `commands/pending/<id>.json`:
    {
      "id":         "20260414-141500-abc",
      "action":     "status",
      "args":       {},
      "created_at": "2026-04-14T14:15:00Z"
    }

Result file format — `commands/results/<id>.result.json`:
    {
      "id":           "...",
      "action":       "status",
      "status":       "ok" | "error" | "rejected",
      "message":      "...",
      "data":         { ... },
      "completed_at": "2026-04-14T14:15:02Z"
    }

Safety rules:
  * The action name is looked up in a hardcoded dict; unknown names are
    rejected without any other code path.
  * No shell, no eval, no subprocess with `shell=True`.
  * Command files older than `STALE_AFTER_SEC` are rejected so a stuck
    queue cannot fire surprise actions after the fact.
  * The poller swallows all exceptions so a single bad command cannot
    crash the watcher daemon.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

STALE_AFTER_SEC = 600  # 10 minutes
MAX_LOG_LINES = 500


# ── Action implementations ──────────────────────────────────────────────

def _action_ping(args: dict) -> dict:
    """Cheapest possible action — confirms the queue is live."""
    return {
        "hostname": socket.gethostname(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _action_status(args: dict) -> dict:
    """Return a single snapshot of STAN health for this host."""
    from stan.config import get_user_config_dir

    user_dir = get_user_config_dir()
    db_path = user_dir / "stan.db"

    status: dict[str, Any] = {
        "hostname": socket.gethostname(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_config_dir": str(user_dir),
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
    }

    if db_path.exists():
        try:
            with sqlite3.connect(str(db_path)) as con:
                status["n_runs"] = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                row = con.execute(
                    "SELECT run_name, run_date, gate_result "
                    "FROM runs ORDER BY run_date DESC LIMIT 1"
                ).fetchone()
                if row:
                    status["last_run"] = {
                        "run_name": row[0],
                        "run_date": row[1],
                        "gate_result": row[2],
                    }
                status["n_maintenance_events"] = con.execute(
                    "SELECT COUNT(*) FROM maintenance_events"
                ).fetchone()[0]
        except Exception as e:
            status["db_error"] = str(e)

    try:
        usage = shutil.disk_usage(str(user_dir))
        status["disk_free_gb"] = round(usage.free / 1024 ** 3, 1)
        status["disk_total_gb"] = round(usage.total / 1024 ** 3, 1)
    except Exception:
        pass

    # STAN version
    try:
        from stan import __version__
        status["stan_version"] = __version__
    except Exception:
        pass

    return status


def _action_tail_log(args: dict) -> dict:
    """Return the last N lines of baseline.log / watcher log.

    Args: {"name": "baseline" | "watcher", "n": int}
    """
    name = args.get("name", "baseline")
    n = int(args.get("n", 100))
    if n < 1:
        n = 1
    if n > MAX_LOG_LINES:
        n = MAX_LOG_LINES

    from stan.config import get_hive_mirror_dir, get_user_config_dir

    candidates = []
    mirror = get_hive_mirror_dir()
    if mirror:
        candidates.append(mirror / f"{name}.log")
    candidates.append(get_user_config_dir() / f"{name}.log")

    for path in candidates:
        if path.exists():
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                return {
                    "log_name": name,
                    "path": str(path),
                    "total_lines": len(lines),
                    "lines": lines[-n:],
                }
            except Exception as e:
                return {"log_name": name, "path": str(path), "error": str(e)}
    return {"log_name": name, "error": "log file not found in any known location"}


def _action_export_db_snapshot(args: dict) -> dict:
    """Export the runs + maintenance_events tables to the mirror as parquet.

    Polars-dependent; no-ops gracefully if polars isn't installed.
    """
    from stan.config import get_hive_mirror_dir, get_user_config_dir

    mirror = get_hive_mirror_dir()
    if not mirror:
        return {"error": "no hive mirror directory configured"}

    db_path = get_user_config_dir() / "stan.db"
    if not db_path.exists():
        return {"error": "stan.db does not exist"}

    try:
        import polars as pl
    except ImportError:
        return {"error": "polars not installed — cannot export parquet"}

    snapshot_dir = mirror / "db_snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    exported = {}
    with sqlite3.connect(str(db_path)) as con:
        for table in ("runs", "maintenance_events", "tic_traces"):
            try:
                rows = con.execute(f"SELECT * FROM {table}").fetchall()
                cols = [c[0] for c in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
            except sqlite3.OperationalError:
                continue
            if not rows:
                exported[table] = {"n_rows": 0}
                continue
            df = pl.DataFrame(
                {c: [r[i] for r in rows] for i, c in enumerate(cols)},
                strict=False,
            )
            out = snapshot_dir / f"{table}.parquet"
            df.write_parquet(out)
            exported[table] = {"n_rows": len(rows), "path": str(out)}

    return {"snapshot_dir": str(snapshot_dir), "tables": exported}


# ── Whitelist ───────────────────────────────────────────────────────────

COMMAND_WHITELIST: dict[str, Callable[[dict], dict]] = {
    "ping":                _action_ping,
    "status":              _action_status,
    "tail_log":            _action_tail_log,
    "export_db_snapshot":  _action_export_db_snapshot,
}


# ── Poller ──────────────────────────────────────────────────────────────

def _ensure_dirs(mirror_dir: Path) -> dict[str, Path]:
    base = mirror_dir / "commands"
    dirs = {
        "pending": base / "pending",
        "done": base / "done",
        "results": base / "results",
    }
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    return dirs


def _write_result(results_dir: Path, cmd_id: str, action: str, status: str,
                  data: dict | None = None, message: str = "") -> None:
    payload = {
        "id": cmd_id,
        "action": action,
        "status": status,
        "message": message,
        "data": data or {},
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    out = results_dir / f"{cmd_id}.result.json"
    tmp = out.with_suffix(".result.json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(out)


def poll_once(mirror_dir: Path | None = None) -> int:
    """Check the mirror for pending commands and execute any that are valid.

    Returns the number of commands processed. Never raises — failures
    are captured into result files so a bad command cannot take down
    the caller.
    """
    from stan.config import get_hive_mirror_dir

    if mirror_dir is None:
        mirror_dir = get_hive_mirror_dir()
    if mirror_dir is None:
        return 0

    dirs = _ensure_dirs(mirror_dir)
    pending = sorted(dirs["pending"].glob("*.json"))
    if not pending:
        return 0

    n_processed = 0
    for cmd_file in pending:
        cmd_id = cmd_file.stem

        # Stale check — reject anything older than STALE_AFTER_SEC
        try:
            age = (datetime.now(timezone.utc).timestamp()
                   - cmd_file.stat().st_mtime)
        except OSError:
            continue
        if age > STALE_AFTER_SEC:
            logger.warning("control: rejecting stale command %s (age %.0fs)",
                           cmd_id, age)
            try:
                _write_result(dirs["results"], cmd_id, "?", "rejected",
                              message=f"command too old ({age:.0f}s)")
                cmd_file.replace(dirs["done"] / cmd_file.name)
            except OSError:
                pass
            continue

        # Parse the request
        try:
            payload = json.loads(cmd_file.read_text(encoding="utf-8"))
            action = payload.get("action", "")
            args = payload.get("args", {}) or {}
        except Exception as e:
            logger.error("control: bad command file %s: %s", cmd_id, e)
            try:
                _write_result(dirs["results"], cmd_id, "?", "error",
                              message=f"bad JSON: {e}")
                cmd_file.replace(dirs["done"] / cmd_file.name)
            except OSError:
                pass
            continue

        fn = COMMAND_WHITELIST.get(action)
        if fn is None:
            logger.warning("control: unknown action %r rejected", action)
            _write_result(dirs["results"], cmd_id, action, "rejected",
                          message=f"action {action!r} not in whitelist")
            try:
                cmd_file.replace(dirs["done"] / cmd_file.name)
            except OSError:
                pass
            n_processed += 1
            continue

        logger.info("control: running %s (%s)", action, cmd_id)
        try:
            data = fn(args)
            _write_result(dirs["results"], cmd_id, action, "ok", data=data)
        except Exception as e:
            logger.exception("control: action %s failed", action)
            _write_result(dirs["results"], cmd_id, action, "error",
                          message=f"{type(e).__name__}: {e}")

        try:
            cmd_file.replace(dirs["done"] / cmd_file.name)
        except OSError:
            pass
        n_processed += 1

    return n_processed


# ── Client-side helpers ─────────────────────────────────────────────────

def enqueue_command(action: str, args: dict | None = None,
                    mirror_dir: Path | None = None) -> Path:
    """Drop a command file into `<mirror>/commands/pending/`.

    Returns the path to the written command file. Used by the `stan
    send-command` CLI and by external tooling that mounts the same share.
    """
    from stan.config import get_hive_mirror_dir

    if mirror_dir is None:
        mirror_dir = get_hive_mirror_dir()
    if mirror_dir is None:
        raise RuntimeError("no hive mirror directory resolvable on this host")

    dirs = _ensure_dirs(mirror_dir)
    now = datetime.now(timezone.utc)
    cmd_id = now.strftime("%Y%m%dT%H%M%S") + f"-{action}-{os.getpid()}"
    payload = {
        "id": cmd_id,
        "action": action,
        "args": args or {},
        "created_at": now.isoformat(),
    }
    out = dirs["pending"] / f"{cmd_id}.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out
