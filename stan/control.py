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


def _action_qc_filter_report(args: dict) -> dict:
    """Audit the QC-filter function against real files in each watch dir.

    For every enabled instrument, walks `watch_dir` up to `max_files`
    entries (default 200, ordered newest-first by mtime), runs
    `is_qc_file()` with the instrument's configured regex, and returns
    a breakdown: counts + example matches + example rejects.

    Args (all optional):
        max_files        : int, default 200 (files to scan per instrument)
        max_age_days     : int, default 14  (only consider files newer than this)
        candidate_pattern: str, a trial regex to also test against every file
                           — lets you see "would THIS regex work better?"
                           without restarting the daemon
    """
    from stan.config import resolve_config_path
    from stan.watcher.qc_filter import (
        DEFAULT_QC_PATTERN, compile_qc_pattern, is_qc_file,
    )
    import time
    import yaml

    max_files = int(args.get("max_files", 200))
    max_age_days = int(args.get("max_age_days", 14))
    candidate_pattern = args.get("candidate_pattern")

    try:
        cfg_path = resolve_config_path("instruments.yml")
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return {"error": f"cannot load instruments.yml: {e}"}

    now = time.time()
    cutoff = now - (max_age_days * 86400)
    out: dict = {
        "default_pattern": DEFAULT_QC_PATTERN,
        "candidate_pattern": candidate_pattern,
        "max_files": max_files,
        "max_age_days": max_age_days,
        "instruments": [],
    }

    cand_pat = None
    if candidate_pattern:
        try:
            import re
            cand_pat = re.compile(candidate_pattern)
        except Exception as e:
            out["candidate_pattern_error"] = str(e)

    for inst in cfg.get("instruments", []):
        if not inst.get("enabled", False):
            continue
        name = inst.get("name", "?")
        watch_dir = inst.get("watch_dir", "")
        exts = set(inst.get("extensions", []))
        qc_pattern_str = inst.get("qc_pattern")
        qc_only = inst.get("qc_only", True)
        pat = compile_qc_pattern(qc_pattern_str)

        wd = Path(watch_dir) if watch_dir else None
        entry: dict = {
            "name": name,
            "watch_dir": watch_dir,
            "watch_dir_exists": wd.exists() if wd else False,
            "extensions": sorted(exts),
            "qc_only": qc_only,
            "qc_pattern": getattr(pat, "pattern", None),
            "n_scanned": 0,
            "n_match": 0,
            "n_reject": 0,
            "examples_match": [],
            "examples_reject": [],
        }
        if cand_pat is not None:
            entry["candidate_n_match"] = 0
            entry["candidate_examples_match"] = []

        if wd is None or not wd.exists():
            out["instruments"].append(entry)
            continue

        # Collect candidate files — anything with a watched extension,
        # newer than cutoff, sorted newest-first, capped at max_files.
        candidates = []
        try:
            for p in wd.rglob("*"):
                if p.suffix not in exts:
                    continue
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                if mt < cutoff:
                    continue
                candidates.append((mt, p))
                if len(candidates) > max_files * 4:
                    # keep scan bounded even on huge dirs
                    break
        except Exception as e:
            entry["scan_error"] = str(e)
            out["instruments"].append(entry)
            continue

        candidates.sort(key=lambda x: x[0], reverse=True)
        candidates = candidates[:max_files]

        for mt, p in candidates:
            entry["n_scanned"] += 1
            matched = is_qc_file(p, pat)
            if matched:
                entry["n_match"] += 1
                if len(entry["examples_match"]) < 5:
                    entry["examples_match"].append(p.name)
            else:
                entry["n_reject"] += 1
                if len(entry["examples_reject"]) < 10:
                    entry["examples_reject"].append(p.name)
            if cand_pat is not None and cand_pat.search(p.stem):
                entry["candidate_n_match"] += 1
                if len(entry["candidate_examples_match"]) < 5:
                    entry["candidate_examples_match"].append(p.name)

        out["instruments"].append(entry)

    return out


def _action_watcher_debug(args: dict) -> dict:
    """Dump the running `stan watch` daemon's internal state so a
    remote diagnostician can tell whether events are arriving, being
    filtered, or stuck in the stability tracker.

    Returns a list of per-watcher snapshots (watch dir, observer type,
    event counts, active trackers with age, and the last 25 events the
    handler saw — including the ones it intentionally ignored)."""
    try:
        from stan.watcher.daemon import get_active_daemon
    except Exception as e:
        return {"error": f"cannot import watcher daemon: {e}"}

    daemon = get_active_daemon()
    if daemon is None:
        return {
            "error": "no active watcher daemon in this process",
            "hint": "run `stan watch` on the host you're querying; the "
                    "control poller shares its process with the daemon",
        }

    watchers = []
    for name, w in daemon._watchers.items():
        try:
            watchers.append(w.debug_snapshot())
        except Exception as e:
            watchers.append({"name": name, "snapshot_error": str(e)})
    return {"n_watchers": len(watchers), "watchers": watchers}


# ── Config sync (upload + apply) ────────────────────────────────────────

# Only these filenames can be written via apply_config. Hardcoded so an
# attacker with mirror-write access cannot overwrite arbitrary files in
# the user's STAN directory.
_EDITABLE_CONFIGS = frozenset({"instruments.yml", "thresholds.yml", "community.yml"})

# Cap per file — these are human-edited YAMLs, anything bigger is
# almost certainly a malformed command, not a real config.
_MAX_CONFIG_BYTES = 100_000


def upload_configs(mirror_dir: Path | None = None) -> int:
    """Copy the current user config YAMLs into `<mirror>/config/` so a
    remote operator can read them without shelling into the instrument.

    Called from the watcher heartbeat so the mirrored copies stay fresh.
    Returns the number of files copied. Silent no-op if no mirror."""
    from stan.config import get_hive_mirror_dir, get_user_config_dir

    if mirror_dir is None:
        mirror_dir = get_hive_mirror_dir()
    if mirror_dir is None:
        return 0

    user_dir = get_user_config_dir()
    out_dir = mirror_dir / "config"
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for fname in _EDITABLE_CONFIGS:
        src = user_dir / fname
        if not src.exists():
            continue
        try:
            content = src.read_text(encoding="utf-8")
            dst = out_dir / fname
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(dst)
            n += 1
        except Exception:
            logger.exception("control: config upload failed for %s", fname)
    return n


def _action_update_stan(args: dict) -> dict:
    """Schedule a STAN update for the next supervisor restart.

    Architectural note: we CAN'T just pip-install here. The `stan watch`
    process that's running this action holds `stan.exe` open, and
    Windows blocks pip from overwriting locked executables (WinError 32).
    Past attempts resulted in half-installed venvs and ModuleNotFoundError
    on relaunch.

    Instead we write `update_pending.flag`. The v0.2.90+ `start_stan_loop.bat`
    supervisor checks for this flag AFTER `stan watch` exits and BEFORE
    relaunching — by which point nothing has stan.exe open, so pip can
    overwrite cleanly. Pair with `restart_watcher` to actually trigger
    the exit.

    Safety: refuses to set the flag if a baseline is in progress
    (baseline_progress.json has been touched within the last 10 minutes)
    so a schema change doesn't land under a running baseline process.
    Override with `{force: true}`.
    """
    import os
    import platform
    import time

    if platform.system() != "Windows":
        # Linux/macOS: no supervisor convention yet; pip directly.
        # These platforms typically don't have the stan.exe file-lock
        # issue because ELF binaries can be unlinked while in use.
        import subprocess
        timeout_sec = int(args.get("timeout_sec", 300))
        cmd = [
            "pip", "install", "--upgrade", "--no-cache-dir",
            "https://github.com/bsphinney/stan/archive/refs/heads/main.zip",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout_sec, check=False,
            )
        except subprocess.TimeoutExpired as e:
            return {"error": f"updater timed out after {timeout_sec}s",
                    "stdout_tail": (e.stdout or "")[-2000:],
                    "stderr_tail": (e.stderr or "")[-2000:]}
        return {
            "returncode": proc.returncode, "cmd": cmd,
            "stdout_tail": (proc.stdout or "")[-2000:],
            "stderr_tail": (proc.stderr or "")[-2000:],
            "hint": "run `restart_watcher` next to load the new code",
        }

    # Windows path — write the flag, don't pip.
    from stan.config import get_user_config_dir

    force = bool(args.get("force", False))
    cfg_dir = get_user_config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)

    # Baseline-in-progress safety gate
    progress_file = cfg_dir / "baseline_progress.json"
    if not force and progress_file.exists():
        age_sec = time.time() - progress_file.stat().st_mtime
        if age_sec < 600:
            return {
                "error": "baseline in progress — refusing to schedule update",
                "baseline_progress_age_sec": round(age_sec, 1),
                "hint": "wait for baseline to finish, or retry with "
                        "args={'force': true} if you're sure",
            }

    flag = cfg_dir / "update_pending.flag"
    flag.write_text(
        f"update scheduled at {datetime.now(timezone.utc).isoformat()}\n"
        "start_stan_loop.bat will run update-stan.bat on next restart\n",
        encoding="utf-8",
    )

    # Verify the supervisor will find the updater
    userprofile = Path(os.environ.get("USERPROFILE", ""))
    updater_candidates = [
        userprofile / "Downloads" / "update-stan.bat",
        userprofile / "STAN" / "update-stan.bat",
    ]
    updater = next((p for p in updater_candidates if p.exists()), None)
    if updater is None:
        # Try to fetch it so start_stan_loop.bat can find it later
        import urllib.request
        try:
            dest = userprofile / "STAN" / "update-stan.bat"
            dest.parent.mkdir(parents=True, exist_ok=True)
            url = "https://raw.githubusercontent.com/bsphinney/stan/main/update-stan.bat"
            with urllib.request.urlopen(url, timeout=30) as resp:
                dest.write_bytes(resp.read())
            updater = dest
        except Exception as e:
            return {
                "flag_path": str(flag),
                "warning": f"flag written but update-stan.bat not found and "
                           f"GitHub fetch failed: {type(e).__name__}: {e}. "
                           f"Supervisor will skip the update step.",
                "searched": [str(p) for p in updater_candidates],
            }

    return {
        "flag_path": str(flag),
        "updater": str(updater),
        "note": (
            "Update scheduled. Queue `restart_watcher` next — the "
            "supervisor will run the updater when stan watch exits, "
            "then relaunch on the new version."
        ),
    }


def _action_restart_watcher(args: dict) -> dict:
    """Ask the running `stan watch` daemon to exit cleanly.

    Writes `<user_config_dir>/restart.flag`. The daemon main loop checks
    for this flag on each tick (~30 s), deletes it, and exits via
    self.stop(). A supervisor wrapper (start_stan_loop.bat or systemd)
    relaunches `stan watch` and thus loads whatever code is currently
    installed — typically v0.N.N+1 after `update_stan`.

    Without a supervisor the daemon just stops; the operator has to
    manually relaunch it. Flagged prominently in the return value.
    """
    from stan.config import get_user_config_dir
    from stan.watcher.daemon import get_active_daemon

    cfg_dir = get_user_config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)
    flag = cfg_dir / "restart.flag"
    flag.write_text("restart requested via control queue\n", encoding="utf-8")

    return {
        "flag_path": str(flag),
        "daemon_active": get_active_daemon() is not None,
        "note": (
            "Watcher will exit within ~30 s. For auto-relaunch, the "
            "host must be running start_stan_loop.bat (or equivalent "
            "supervisor). Without one, the watcher just stops."
        ),
    }


def _action_cleanup_excluded(args: dict) -> dict:
    """Retroactively enforce each instrument's `exclude_pattern` on the
    runs table.

    Born from the 2026-04-14 baseline-resume bug: 16 blank/wash files
    on the timsTOF made it past baseline (resume path skipped the
    QC filter pre-v0.2.96). Now they sit in `runs` polluting Run
    History even though they should never have been processed.

    Action:
      * For every instrument in instruments.yml that has an
        exclude_pattern, find every row in `runs` whose run_name
        matches that pattern.
      * Delete those rows from `runs`, plus matching rows from
        `tic_traces` and `sample_health` (cascade).
      * Delete the corresponding `baseline_output/<stem>/` directories
        to reclaim disk.
      * Refuse to delete rows that have already been submitted to
        the community benchmark (`submitted_to_benchmark = 1`) —
        those need a relay-side delete via `/api/update` instead.

    Args:
      dry_run: bool, default True. Returns the list of rows that
        WOULD be deleted without actually touching anything. Set
        False to actually delete.

    Safety: pattern source is the live instruments.yml — only
    things the operator has already declared as 'exclude'. There
    is no user-supplied pattern argument; you can't ask this
    action to delete rows matching arbitrary regexes.
    """
    import re as _re
    import shutil
    import yaml

    from stan.config import (
        get_user_config_dir, resolve_config_path,
    )
    from stan.db import get_db_path

    dry_run = bool(args.get("dry_run", True))

    try:
        cfg_path = resolve_config_path("instruments.yml")
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return {"error": f"cannot load instruments.yml: {e}"}

    db_path = get_db_path()
    if not db_path.exists():
        return {"error": "stan.db does not exist"}

    output_base = get_user_config_dir() / "baseline_output"
    instruments = cfg.get("instruments", []) or []

    summary: list[dict] = []
    for inst in instruments:
        name = inst.get("name", "?")
        pat_str = inst.get("exclude_pattern")
        if not pat_str:
            continue
        try:
            pat = _re.compile(pat_str)
        except _re.error as e:
            summary.append({"instrument": name, "error": f"bad regex: {e}"})
            continue

        with sqlite3.connect(str(db_path)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT id, run_name, submitted_to_benchmark "
                "FROM runs WHERE instrument = ?",
                (name,),
            ).fetchall()

        matched = [dict(r) for r in rows if pat.search(r["run_name"])]
        deletable = [r for r in matched if not r["submitted_to_benchmark"]]
        skipped_submitted = [r for r in matched if r["submitted_to_benchmark"]]

        actions = {
            "instrument": name,
            "exclude_pattern": pat_str,
            "n_matched": len(matched),
            "n_deletable": len(deletable),
            "n_skipped_already_submitted": len(skipped_submitted),
            "matched_examples": [r["run_name"] for r in matched[:8]],
            "skipped_already_submitted": [r["run_name"] for r in skipped_submitted[:5]],
            "deleted": [],
            "directories_removed": 0,
        }

        if not dry_run and deletable:
            ids = [r["id"] for r in deletable]
            placeholders = ",".join("?" * len(ids))
            with sqlite3.connect(str(db_path)) as con:
                con.execute(f"DELETE FROM tic_traces WHERE run_id IN ({placeholders})", ids)
                con.execute(f"DELETE FROM sample_health WHERE id IN ({placeholders})", ids)
                con.execute(f"DELETE FROM runs WHERE id IN ({placeholders})", ids)
            actions["deleted"] = [r["run_name"] for r in deletable]

            # Reclaim disk in baseline_output
            removed_dirs = 0
            for r in deletable:
                stem = r["run_name"].rsplit(".", 1)[0]
                d = output_base / stem
                if d.exists():
                    try:
                        shutil.rmtree(d)
                        removed_dirs += 1
                    except OSError:
                        pass
            actions["directories_removed"] = removed_dirs

        summary.append(actions)

    return {
        "dry_run": dry_run,
        "summary": summary,
        "hint": ("Re-run with args={'dry_run': false} to actually delete."
                 if dry_run else
                 "Already-submitted rows must be cleared from the community "
                 "relay separately via /api/update."),
    }


def _action_fix_instrument_names(args: dict) -> dict:
    """Merge two instrument-name values in runs + sample_health.

    Remote mirror of `stan fix-instrument-names` — rewrites rows where
    instrument == from_name to use to_name instead. Fixes the "two
    cards for one physical instrument" problem when a historical name
    (e.g. 'data_bruker') and the canonical model name (e.g. 'timsTOF
    HT') both accumulated rows.

    Args (all required except dry_run):
        from_name: existing instrument value to replace
        to_name:   canonical value to rewrite it to
        dry_run:   preview only, default False (operator intent is
                   signalled by queuing the command)

    Narrowly scoped: only UPDATE on the `instrument` column in the
    `runs` and `sample_health` tables. No DELETE, no other columns
    touched, no ability to rewrite arbitrary data. Refuses to run if
    from_name or to_name is empty or not a string.
    """
    import sqlite3
    from stan.db import get_db_path, init_db

    from_name = args.get("from_name")
    to_name = args.get("to_name")
    dry_run = bool(args.get("dry_run", False))

    if not isinstance(from_name, str) or not from_name:
        return {"error": "from_name (non-empty string) is required"}
    if not isinstance(to_name, str) or not to_name:
        return {"error": "to_name (non-empty string) is required"}
    if from_name == to_name:
        return {"error": "from_name and to_name are identical; nothing to do"}

    init_db()
    db = get_db_path()
    if not db.exists():
        return {"error": f"stan.db not found at {db}"}

    with sqlite3.connect(str(db)) as con:
        n_runs = con.execute(
            "SELECT COUNT(*) FROM runs WHERE instrument = ?", (from_name,)
        ).fetchone()[0]
        n_sh = 0
        try:
            n_sh = con.execute(
                "SELECT COUNT(*) FROM sample_health WHERE instrument = ?",
                (from_name,),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            # sample_health may not exist on very old DBs
            pass
        conflict_runs = con.execute(
            "SELECT COUNT(*) FROM runs WHERE instrument = ?", (to_name,)
        ).fetchone()[0]

    preview = {
        "from_name": from_name,
        "to_name": to_name,
        "runs_to_rewrite": int(n_runs),
        "sample_health_to_rewrite": int(n_sh),
        "runs_already_on_target": int(conflict_runs),
        "dry_run": dry_run,
    }

    if n_runs == 0 and n_sh == 0:
        preview["result"] = "noop: no rows matched from_name"
        return preview

    if dry_run:
        preview["result"] = "dry_run: no DB writes"
        return preview

    with sqlite3.connect(str(db)) as con:
        r1 = con.execute(
            "UPDATE runs SET instrument = ? WHERE instrument = ?",
            (to_name, from_name),
        )
        rewrote_runs = r1.rowcount
        rewrote_sh = 0
        try:
            r2 = con.execute(
                "UPDATE sample_health SET instrument = ? WHERE instrument = ?",
                (to_name, from_name),
            )
            rewrote_sh = r2.rowcount
        except sqlite3.OperationalError:
            pass
        con.commit()

    preview["result"] = "ok"
    preview["rewrote_runs"] = int(rewrote_runs)
    preview["rewrote_sample_health"] = int(rewrote_sh)
    return preview


def _action_apply_config(args: dict) -> dict:
    """Write a new config YAML on this instrument.

    The existing `ConfigWatcher` polls `instruments.yml` mtime every 30 s,
    so a successful write triggers an automatic hot-reload — no daemon
    restart needed.

    Args:
        filename: one of instruments.yml / thresholds.yml / community.yml
        content:  full YAML text to write (must parse cleanly)
        backup:   bool, default True — keep a .bak of the previous file

    Safety:
        * Filename is validated against a hardcoded allowlist; no path
          traversal is possible because only the basename is used.
        * Content is YAML-parsed before any write — a malformed edit
          cannot silently corrupt a live config.
        * Size is capped to prevent pathological payloads from filling
          the command queue.
        * Atomic write (tmp file + os.replace) so a reader never sees a
          half-written config.
    """
    import os
    import yaml

    from stan.config import get_user_config_dir

    filename = args.get("filename", "")
    content = args.get("content", "")
    backup = bool(args.get("backup", True))

    if filename not in _EDITABLE_CONFIGS:
        return {"error": f"filename not editable: {filename!r}. "
                         f"Allowed: {sorted(_EDITABLE_CONFIGS)}"}
    if not isinstance(content, str) or not content.strip():
        return {"error": "content must be a non-empty string"}
    if len(content.encode("utf-8")) > _MAX_CONFIG_BYTES:
        return {"error": f"content exceeds {_MAX_CONFIG_BYTES} bytes"}

    # Validate YAML before touching disk
    try:
        parsed = yaml.safe_load(content)
    except yaml.YAMLError as e:
        return {"error": f"YAML parse failed: {e}"}
    if parsed is None:
        return {"error": "parsed YAML is empty"}

    target = get_user_config_dir() / filename
    target.parent.mkdir(parents=True, exist_ok=True)

    prev_size = None
    if target.exists():
        prev_size = target.stat().st_size
        if backup:
            try:
                bak = target.with_suffix(target.suffix + ".bak")
                bak.write_text(target.read_text(encoding="utf-8"),
                               encoding="utf-8")
            except Exception:
                logger.exception("apply_config: backup failed for %s", target)

    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)

    # Re-upload so the mirror reflects the new state immediately (don't
    # wait 5 min for the next heartbeat)
    try:
        upload_configs()
    except Exception:
        pass

    return {
        "filename": filename,
        "path": str(target),
        "bytes_written": len(content.encode("utf-8")),
        "prev_bytes": prev_size,
        "backup_made": backup and prev_size is not None,
        "hot_reload": "ConfigWatcher picks up mtime changes within 30 s",
    }


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
    "watcher_debug":       _action_watcher_debug,
    "qc_filter_report":    _action_qc_filter_report,
    # First write actions — each is narrowly scoped:
    #   apply_config       → only 3 YAML files in user_config_dir
    #   update_stan        → only invokes update-stan.bat
    #   restart_watcher    → only writes a restart.flag the daemon consumes
    "apply_config":        _action_apply_config,
    "update_stan":         _action_update_stan,
    "restart_watcher":     _action_restart_watcher,
    # Retroactive cleanup — only deletes rows matching an instrument's
    # *already-declared* exclude_pattern. Cannot delete arbitrary rows.
    "cleanup_excluded":    _action_cleanup_excluded,
    # Narrowly-scoped instrument-name merge: only UPDATEs the
    # `instrument` column in runs + sample_health. No DELETE.
    "fix_instrument_names": _action_fix_instrument_names,
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
    # Skip macOS AppleDouble sidecars (._filename) and other hidden
    # files — macOS creates these automatically on SMB shares and they
    # trip the JSON parser on Windows readers.
    pending = sorted(
        p for p in dirs["pending"].glob("*.json")
        if not p.name.startswith(".")
    )
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
    # Include microseconds so two calls in the same second sort in call
    # order (matters when callers queue update_stan + restart_watcher
    # back-to-back and need them processed in that order).
    cmd_id = now.strftime("%Y%m%dT%H%M%S%f") + f"-{action}-{os.getpid()}"
    payload = {
        "id": cmd_id,
        "action": action,
        "args": args or {},
        "created_at": now.isoformat(),
    }
    out = dirs["pending"] / f"{cmd_id}.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out
