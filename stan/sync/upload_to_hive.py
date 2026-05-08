"""Instrument-PC → Hive upload via SMB.

In the new "hive" processing mode, the watcher detects a stable raw
file/directory and copies it to the Quobyte SMB share that's already
mounted on every instrument PC at ``Y:\\``. No SFTP, no per-PC SSH
key — Quobyte's existing auth carries the upload. The Hive-side
dispatcher (``stan hive-dispatch``) sees the new file land in
``/quobyte/proteomics-grp/STAN/incoming/<instrument>/`` and submits
a SLURM job running ``stan hive-process``.

Atomicity: copy to ``<basename>.partial`` first, then ``os.rename``
to the final name. The dispatcher's walk filter skips ``.partial``
suffixes (already implemented in dispatch_hive._walk_raws), so a
half-uploaded file never triggers a SLURM job.

Resume: every upload writes a row in the local stan.db ``uploads``
table at start. Status transitions ``pending → done`` on rename
success, ``pending → failed`` on copy/rename failure. On watcher
startup, ``resume_pending_uploads`` walks pending+failed rows and
re-attempts so a crashed/killed copy doesn't strand the file.

Reuses the existing copy primitives from ``stan.sync.raw``:
robocopy /MT:8 for Bruker .d on Windows, shutil.copytree on Unix
dev boxes, shutil.copy2 for Thermo .raw.
"""

from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from stan.sync.raw import (
    BRUKER_SUFFIXES,
    THERMO_SUFFIXES,
    _copy_dir_robocopy,
    _copy_dir_shutil,
    _copy_file,
    _path_size,
)

logger = logging.getLogger(__name__)


def _record_upload_start(
    raw_path: Path, dest_path: Path, db_path: Path,
) -> None:
    """Insert/update ``uploads`` row in 'pending' state.

    ON CONFLICT bumps attempt_count rather than appending, mirroring
    ``record_dispatch_attempt`` semantics. Best-effort: a failure to
    write the audit row should not block the actual upload. The watcher
    will still see the rename-on-success and the dispatcher will still
    process the file; we just lose the local audit trail.
    """
    now = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(str(db_path)) as con:
            con.execute(
                "INSERT INTO uploads "
                "(raw_path, dest_path, size_bytes, status, started_at, attempt_count) "
                "VALUES (?, ?, ?, 'pending', ?, 1) "
                "ON CONFLICT(raw_path) DO UPDATE SET "
                "  dest_path     = excluded.dest_path, "
                "  size_bytes    = excluded.size_bytes, "
                "  status        = 'pending', "
                "  started_at    = excluded.started_at, "
                "  attempt_count = uploads.attempt_count + 1, "
                "  error         = NULL",
                (str(raw_path), str(dest_path), _path_size(raw_path), now),
            )
    except Exception:
        logger.debug("uploads(start) write failed", exc_info=True)


def _record_upload_done(raw_path: Path, db_path: Path) -> None:
    """Mark a row as 'done' with completed_at timestamp."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(str(db_path)) as con:
            con.execute(
                "UPDATE uploads SET status='done', completed_at=?, error=NULL "
                "WHERE raw_path=?",
                (now, str(raw_path)),
            )
    except Exception:
        logger.debug("uploads(done) write failed", exc_info=True)


def _record_upload_failed(raw_path: Path, error: str, db_path: Path) -> None:
    """Mark a row as 'failed' with the error message."""
    try:
        with sqlite3.connect(str(db_path)) as con:
            con.execute(
                "UPDATE uploads SET status='failed', error=? WHERE raw_path=?",
                (error[:500], str(raw_path)),
            )
    except Exception:
        logger.debug("uploads(failed) write failed", exc_info=True)


def upload_raw_to_incoming(
    raw_path: Path,
    dest_dir: Path,
    *,
    db_path: Path | None = None,
    log_dir: Path | None = None,
) -> dict:
    """Copy a stable raw to the Hive incoming dir via SMB, atomically.

    Args:
        raw_path: Source on the instrument PC. Bruker ``.d`` directory
            or Thermo ``.raw`` file.
        dest_dir: Destination directory on the Quobyte SMB mount, e.g.
            ``Y:/proteomics-grp/STAN/incoming/timsTOF HT/``. Created if
            absent.
        db_path: stan.db path for upload tracking. Defaults to the
            global STAN DB at ``~/.stan/stan.db``.
        log_dir: Where robocopy verbose logs land. Defaults to
            ``~/STAN/logs/``.

    Returns:
        Dict with keys ``status`` ('done'|'skipped'|'failed'), ``dest``,
        ``size_bytes``, ``error``.
    """
    raw_path = Path(raw_path)
    dest_dir = Path(dest_dir)

    if db_path is None:
        from stan.db import get_db_path
        db_path = get_db_path()
    if log_dir is None:
        log_dir = Path.home() / "STAN" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {
        "source": str(raw_path),
        "status": "failed",
        "dest": None,
        "size_bytes": 0,
        "error": None,
    }

    if not raw_path.exists():
        result["error"] = "source does not exist"
        return result

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        result["error"] = f"cannot create dest_dir {dest_dir}: {e}"
        _record_upload_failed(raw_path, result["error"], db_path)
        return result

    final_dest = dest_dir / raw_path.name
    partial_dest = dest_dir / (raw_path.name + ".partial")

    # Skip if a final-named dest already exists with matching size +
    # mtime — the dispatcher will see it next cron tick. Cheaper than
    # re-copying a 50 GB .d that's already on Quobyte.
    if final_dest.exists():
        try:
            src_size = _path_size(raw_path)
            dest_size = _path_size(final_dest)
            if src_size == dest_size and src_size > 0:
                _record_upload_done(raw_path, db_path)
                result.update(
                    status="skipped", dest=str(final_dest),
                    size_bytes=src_size,
                )
                logger.info(
                    "Upload skip (already on Hive at matching size): %s",
                    raw_path.name,
                )
                return result
        except OSError:
            pass

    _record_upload_start(raw_path, final_dest, db_path)

    # Clean up any prior .partial leftover from a killed copy. Without
    # this, robocopy would happily merge new files into a stale shell.
    if partial_dest.exists():
        try:
            if partial_dest.is_dir():
                shutil.rmtree(str(partial_dest), ignore_errors=True)
            else:
                partial_dest.unlink()
        except OSError as e:
            logger.warning(
                "Could not clean stale partial %s: %s", partial_dest, e,
            )

    log_path = log_dir / f"upload_{raw_path.name}.log"
    suffix = raw_path.suffix.lower()
    is_bruker_dir = raw_path.is_dir() and suffix in BRUKER_SUFFIXES
    is_thermo_file = raw_path.is_file() and suffix in THERMO_SUFFIXES

    if is_bruker_dir:
        if sys.platform == "win32":
            ok = _copy_dir_robocopy(raw_path, partial_dest, log_path)
        else:
            ok = _copy_dir_shutil(raw_path, partial_dest)
    elif is_thermo_file:
        ok = _copy_file(raw_path, partial_dest)
    else:
        # Opaque single file (e.g. a converted mzML). Treat as Thermo.
        ok = _copy_file(raw_path, partial_dest)

    if not ok:
        err = "copy failed — see " + str(log_path)
        result["error"] = err
        _record_upload_failed(raw_path, err, db_path)
        return result

    # Atomic rename. On Windows, os.rename can fail if dest exists,
    # but we already removed the stale .partial above. final_dest may
    # have been created in a race — replace it with os.replace which
    # overwrites cross-platform.
    try:
        os.replace(str(partial_dest), str(final_dest))
    except OSError as e:
        err = f"rename {partial_dest} → {final_dest} failed: {e}"
        result["error"] = err
        _record_upload_failed(raw_path, err, db_path)
        logger.exception(err)
        return result

    size = _path_size(final_dest)
    _record_upload_done(raw_path, db_path)
    result.update(status="done", dest=str(final_dest), size_bytes=size)
    logger.info(
        "Uploaded %s (%.1f MB) → %s",
        raw_path.name, size / 1e6, final_dest,
    )
    return result


def _record_submission(
    raw_path: Path, slurm_job_id: str, db_path: Path,
) -> None:
    """Stamp the uploads row with the SLURM job id once submission lands."""
    now = datetime.now(timezone.utc).isoformat()
    try:
        with sqlite3.connect(str(db_path)) as con:
            con.execute(
                "UPDATE uploads SET slurm_job_id=?, submitted_at=? "
                "WHERE raw_path=?",
                (slurm_job_id, now, str(raw_path)),
            )
    except Exception:
        logger.debug("uploads(submission) write failed", exc_info=True)


def _smb_to_quobyte_path(smb_path: str) -> str:
    """Translate Windows SMB Quobyte mount → Linux Quobyte POSIX path.

    Brett's lab convention: ``Y:\\`` on Windows mounts to
    ``/quobyte/`` on Hive. So ``Y:\\proteomics-grp\\STAN\\incoming\\
    timsTOF HT\\foo.d`` on the instrument PC is the same file as
    ``/quobyte/proteomics-grp/STAN/incoming/timsTOF HT/foo.d`` on
    Hive. The translation is a static prefix swap + slash flip.
    """
    s = str(smb_path)
    if s[:3].upper() == "Y:\\" or s[:3].upper() == "Y:/":
        s = s[3:]
        return "/quobyte/" + s.replace("\\", "/").lstrip("/")
    # Already a Linux-style path (dev box on macOS uses
    # /Volumes/proteomics-grp/STAN/...). Map Volumes → quobyte too.
    if s.startswith("/Volumes/proteomics-grp/"):
        return "/quobyte/proteomics-grp/" + s[len("/Volumes/proteomics-grp/"):]
    return s


def submit_one_via_ssh(
    raw_source: Path,
    raw_dest_smb: Path,
    *,
    instrument: str,
    family: str,
    vendor: str,
    ssh_key: Path,
    hive_user: str,
    hive_host: str,
    hive_venv: str,
    hive_dispatch_yml: str = "/quobyte/proteomics-grp/STAN/dispatch.yml",
    column_vendor: str = "",
    column_model: str = "",
    db_path: Path | None = None,
    timeout: int = 120,
) -> dict:
    """SSH to Hive and submit a SLURM job for one already-uploaded raw.

    Called by the watcher's hive-mode dispatch IMMEDIATELY after the
    SMB upload completes — minimizes the latency from "acquisition
    done" to "row in dashboard." Translates the Windows SMB destination
    to a Linux Quobyte path, then runs:

        ssh hive "<venv>/bin/stan hive-dispatch --raw <linux_path>
                  --instrument-name '<inst>' --family <fam> --vendor <v>"

    Returns dict with ``{status, job_id, raw, error}`` from the
    dispatcher's JSON output, plus locally-recorded ``submitted_at``
    on success.
    """
    import json as _json
    import subprocess

    if db_path is None:
        from stan.db import get_db_path
        db_path = get_db_path()

    raw_hive = _smb_to_quobyte_path(raw_dest_smb)
    cmd = [
        "ssh",
        "-i", str(ssh_key),
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=15",
        f"{hive_user}@{hive_host}",
        f"{hive_venv}/bin/stan", "hive-dispatch",
        "--config", hive_dispatch_yml,
        "--raw", raw_hive,
        "--instrument-name", instrument,
        "--family", family,
        "--vendor", vendor,
    ]
    if column_vendor:
        cmd += ["--column-vendor", column_vendor]
    if column_model:
        cmd += ["--column-model", column_model]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "failed", "job_id": None, "raw": raw_hive,
            "error": f"ssh submit timed out after {timeout}s",
        }
    except FileNotFoundError:
        return {
            "status": "failed", "job_id": None, "raw": raw_hive,
            "error": "ssh not on PATH on this instrument PC",
        }

    if result.returncode != 0:
        return {
            "status": "failed", "job_id": None, "raw": raw_hive,
            "error": f"ssh exit {result.returncode}: {result.stderr.strip()[:300]}",
        }

    # Dispatcher's --raw mode prints a single JSON line on stdout.
    # Some lines may also come from logging; pick the last line that
    # parses as JSON.
    out: dict | None = None
    for line in reversed(result.stdout.strip().splitlines()):
        try:
            out = _json.loads(line)
            break
        except _json.JSONDecodeError:
            continue
    if not out:
        return {
            "status": "failed", "job_id": None, "raw": raw_hive,
            "error": f"could not parse dispatcher JSON: {result.stdout[:300]}",
        }

    if out.get("status") == "submitted" and out.get("job_id"):
        # db_path is the LOCAL instrument-PC stan.db. The uploads row
        # is keyed by the SOURCE local raw_path (where the file was
        # acquired), not the SMB destination. Stamping the SLURM job id
        # there means resume_pending_uploads can retry on restart for
        # rows where status='done' but slurm_job_id IS NULL.
        _record_submission(
            Path(str(raw_source)), out["job_id"], db_path,
        )
    return out

    """Return rows in ``pending`` or ``failed`` state for resume."""
    if db_path is None:
        from stan.db import get_db_path
        db_path = get_db_path()
    try:
        with sqlite3.connect(str(db_path)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT * FROM uploads "
                "WHERE status IN ('pending', 'failed') "
                "ORDER BY started_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.OperationalError:
        return []  # uploads table doesn't exist yet


def resume_pending_uploads(
    dest_dir_for: "callable[[str], Path] | None" = None,
    *,
    db_path: Path | None = None,
) -> int:
    """Walk pending+failed uploads and re-attempt each.

    Called from the watcher's startup so a crashed/killed copy
    doesn't strand the source. ``dest_dir_for`` maps a raw_path to
    its destination dir — the watcher provides this from
    instruments.yml since the dest is per-instrument.

    Returns count of uploads re-attempted (regardless of success).
    """
    pending = get_pending_uploads(db_path)
    if not pending:
        return 0
    if dest_dir_for is None:
        # Without a resolver we can only retry uploads whose recorded
        # dest_path parent is still reachable.
        def dest_dir_for(rp: str) -> Path | None:
            row_dest = next(
                (r.get("dest_path") for r in pending if r["raw_path"] == rp),
                None,
            )
            return Path(row_dest).parent if row_dest else None

    n = 0
    for row in pending:
        raw = Path(row["raw_path"])
        if not raw.exists():
            logger.info("Skipping resume of %s — source gone", raw.name)
            continue
        dest = dest_dir_for(str(raw)) if callable(dest_dir_for) else None
        if not dest:
            logger.info(
                "Skipping resume of %s — no dest dir resolved", raw.name,
            )
            continue
        logger.info("Resuming pending upload: %s → %s", raw.name, dest)
        upload_raw_to_incoming(raw, dest, db_path=db_path)
        n += 1
    return n
