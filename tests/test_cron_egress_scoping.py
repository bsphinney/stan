"""Two Hive crons that re-read unchanged PG rows every tick, and the fixes.

PG Farm bills every byte it serves (it runs on Google Cloud), so a scheduled
reader may only move what changed. Measured against live PG on 2026-09-22:

* `cron_community_sync.sh` runs `stan submit-all --backend pg` unscoped, and
  that is pinned here ON PURPOSE. A 30-day `--since` saved ~11 MB/day but
  would silently never push a run that becomes submittable late (run_date is
  the acquisition date). See the comment in the script.

* `cron_ioncloud.sh` is the hourly tick that submits the ion-cloud backfill.
  Its running copy on Hive carried an squeue anti-stacking guard the repo
  lacked (added there by patch_ioncloud_cron.py on 2026-08-26), so deploying
  the repo copy would have silently removed it. The guard is now in the repo
  and exercised here for real, with squeue and sbatch stubbed on PATH.
"""
from __future__ import annotations

import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
HIVE_STAN = "/quobyte/proteomics-grp/STAN"


@pytest.mark.parametrize("name", ["cron_community_sync.sh", "cron_ioncloud.sh",
                                  "feature_cloud.sbatch"])
def test_shell_parses(name):
    r = subprocess.run(["bash", "-n", str(SCRIPTS / name)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


# ── community sync ───────────────────────────────────────────────────────

def _community_text() -> str:
    return (SCRIPTS / "cron_community_sync.sh").read_text()


def test_community_sync_reads_every_pending_row():
    """No date window: a late-submittable run must still be pushed by cron."""
    calls = [ln for ln in _community_text().splitlines()
             if "submit-all" in ln and not ln.lstrip().startswith("#")]
    assert calls, "no submit-all invocation found"
    for ln in calls:
        assert "--since" not in ln, f"date-scoped submit-all: {ln.strip()}"


# ── ion-cloud anti-stacking guard ────────────────────────────────────────

def _stub(dirpath: Path, name: str, body: str) -> None:
    p = dirpath / name
    p.write_text("#!/bin/bash\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_ioncloud(tmp_path: Path, squeue_body: str | None):
    """Run the real cron script with its Hive paths pointed at tmp_path."""
    root = tmp_path / "STAN"
    (root / "logs").mkdir(parents=True)
    (root / "feature_cloud.sbatch").write_text(
        (SCRIPTS / "feature_cloud.sbatch").read_text())
    script = root / "cron_ioncloud.sh"
    script.write_text((SCRIPTS / "cron_ioncloud.sh").read_text()
                      .replace(HIVE_STAN, str(root)))
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    calls = tmp_path / "calls.log"
    _stub(bin_, "sbatch", f'echo "sbatch $*" >> "{calls}"; echo "Submitted batch job 1"')
    if squeue_body is not None:
        _stub(bin_, "squeue", f'echo "squeue $*" >> "{calls}"\n{squeue_body}')
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(tmp_path)}
    r = subprocess.run(["bash", str(script)], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=60)
    log = (root / "logs" / "cron_ioncloud.log")
    return (r.returncode,
            log.read_text() if log.exists() else "",
            calls.read_text() if calls.exists() else "")


def test_guard_skips_when_a_backfill_is_already_queued(tmp_path):
    rc, log, calls = _run_ioncloud(tmp_path, 'echo "23953459 low stan-ioncloud PD"')
    assert rc == 0
    assert "skip: stan-ioncloud already queued/running" in log
    assert "sbatch feature_cloud.sbatch" not in calls


def test_guard_submits_when_nothing_is_queued(tmp_path):
    rc, log, calls = _run_ioncloud(tmp_path, "true")
    assert rc == 0
    assert "submitting feature_cloud.sbatch (job=stan-ioncloud)" in log
    assert "sbatch feature_cloud.sbatch" in calls


def test_guard_queries_the_job_name_the_sbatch_declares(tmp_path):
    """A renamed job would make the guard look for something never queued."""
    _, _, calls = _run_ioncloud(tmp_path, "true")
    declared = re.search(r"--job-name=(\S+)",
                         (SCRIPTS / "feature_cloud.sbatch").read_text()).group(1)
    assert f"-n {declared}" in calls


def test_guard_fails_closed_when_squeue_errors(tmp_path):
    rc, log, calls = _run_ioncloud(tmp_path, "exit 1")
    assert rc == 1
    assert "ABORT: squeue failed" in log
    assert "sbatch feature_cloud.sbatch" not in calls


def test_guard_fails_closed_when_squeue_is_missing(tmp_path):
    rc, log, calls = _run_ioncloud(tmp_path, None)
    assert rc == 1
    assert "ABORT: squeue not on PATH" in log
    assert "sbatch feature_cloud.sbatch" not in calls


@pytest.mark.parametrize("squeue_body", ['echo "1 low stan-ioncloud R"', "true",
                                         "exit 1", None])
def test_every_branch_writes_the_heartbeat_log(tmp_path, squeue_body):
    """The heartbeat judges this job by cron_ioncloud.log's mtime (CRON_LOGS
    in stan.reports.instrument_watch, pinned by
    test_instrument_watch_freshness), so a quiet skip must still write."""
    _, log, _ = _run_ioncloud(tmp_path, squeue_body)
    assert log.strip(), "a branch exited without writing cron_ioncloud.log"
