"""Every cron script in the repo must be executable in git's index.

WHY THIS EXISTS. The crontab on Hive invokes these paths directly:

    */30 * * * * flock -n /tmp/stan_evosep.lock /quobyte/.../cron_evosep.sh

so the file needs +x to run at all. Deploying is a copy from this repo to
/quobyte/proteomics-grp/STAN/, and `cp -p` preserves the SOURCE's mode -- so a
mode-644 file here silently strips the execute bit off a working 755 file
there. Cron then answers "Permission denied" into nothing.

That is not hypothetical. On 2026-09-09 `cron_evosep.sh` was deployed that way
and the Evosep extract stopped for eight days, until 2026-09-17. Feed alerting
lived inside that same script at the time, so the alarm died with the thing it
was meant to alarm about, and the dashboard served an eight-day-old column
document that looked exactly like a healthy one.

WHAT MAKES IT WORTH A TEST rather than care. The two checks a person naturally
runs after deploying a shell script -- `diff` against the source and `bash -n`
-- both pass on a file that cannot execute. Nothing in the content is wrong.
Only the mode is, and nothing looks at the mode unless it is asked to.

Mode is asserted from `git ls-files -s`, not from the filesystem: the index is
what a fresh clone gets, and a local `chmod` that never reached git would make
a filesystem check pass while the bug shipped.

`cron_stan_alerts.sh` is exempt from NEEDING this only in the sense that its
crontab line runs it as `bash <script>`, deliberately, so the watchdog cannot
be killed the way its subject was. It is still asserted executable here --
belt and braces cost nothing, and the crontab is not in this repo to check.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _tracked_cron_scripts() -> list[tuple[str, str]]:
    """[(mode, path)] for every tracked cron_*.sh, straight from the index."""
    out = subprocess.run(
        ["git", "ls-files", "-s", "--", "*cron_*.sh"],
        cwd=REPO, capture_output=True, text=True, check=True, timeout=30,
    ).stdout
    rows = []
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        rows.append((meta.split()[0], path))
    return rows


def test_cron_scripts_are_tracked_executable():
    scripts = _tracked_cron_scripts()
    assert scripts, "no cron_*.sh found -- has the layout moved?"
    not_exec = [p for mode, p in scripts if mode != "100755"]
    assert not not_exec, (
        "these cron scripts are mode 644 in git, so a `cp -p` deploy will "
        "strip the execute bit from the running copy and cron will fail "
        "silently:\n  " + "\n  ".join(not_exec) +
        "\nFix with: git update-index --chmod=+x <path>"
    )


def test_cron_scripts_have_a_shebang():
    """A file cron execs directly must say what to exec it with."""
    missing = []
    for _mode, path in _tracked_cron_scripts():
        first = (REPO / path).read_text(errors="replace").splitlines()[:1]
        if not first or not first[0].startswith("#!"):
            missing.append(path)
    assert not missing, "cron scripts with no shebang:\n  " + "\n  ".join(missing)


def _preamble_positions(text: str) -> dict[str, int | None]:
    """First line number of each preamble element, 1-indexed, or None."""
    pos: dict[str, int | None] = {"source": None, "logname": None, "plus_u": None}
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if pos["source"] is None and s.startswith("source /etc/profile.d"):
            pos["source"] = i
        if pos["logname"] is None and s.startswith("export LOGNAME"):
            pos["logname"] = i
        if pos["plus_u"] is None and s == "set +u":
            pos["plus_u"] = i
    return pos


def test_profile_sourcing_cannot_kill_the_tick():
    """Seed LOGNAME/USER *before* sourcing, and drop -u *across* the sourcing.

    `/etc/profile.d/modules.sh` dereferences unbound variables under a clean
    cron environment. Under `set -u` that exits the whole shell -- not the
    sourced file -- so `|| true` is never evaluated and the script dies before
    it can write its own log. The failure is completely silent: no output, no
    log line, exit 1.

    Both halves are load-bearing, and knowing only half is what shipped:
    `cron_stan_db_backup.sh` sat staged for 13 days with the exports placed
    AFTER the sources and no `set +u`. LOGNAME is merely the FIRST unbound
    variable modules.sh touches -- line 19 goes on to read MANPATH, which cron
    does not set either -- so seeding LOGNAME alone still died. Measured under
    `env -i`: exit 127, "MANPATH: unbound variable", zero bytes logged. Had it
    been installed as written, it would have failed every night at 03:17
    forever, while `bash -n` and a `diff` against this repo both passed.

    Asserted by position rather than presence, because presence is what the
    broken version had.
    """
    broken = []
    for _mode, path in _tracked_cron_scripts():
        text = (REPO / path).read_text(errors="replace")
        p = _preamble_positions(text)
        if p["source"] is None:
            continue  # does not source the system profile; nothing to guard
        if p["logname"] is None:
            broken.append(f"{path}: sources profile.d but never seeds LOGNAME")
        elif p["logname"] > p["source"]:
            broken.append(
                f"{path}: exports LOGNAME at line {p['logname']}, AFTER "
                f"sourcing at line {p['source']} -- too late to help"
            )
        if p["plus_u"] is None:
            broken.append(
                f"{path}: no `set +u` before sourcing profile.d -- a stray "
                "unbound var (MANPATH, not just LOGNAME) will kill the tick"
            )
        elif p["source"] is not None and p["plus_u"] > p["source"]:
            broken.append(
                f"{path}: `set +u` at line {p['plus_u']} comes after the "
                f"source at line {p['source']}"
            )
    assert not broken, (
        "cron scripts that will die silently under cron's environment:\n  "
        + "\n  ".join(broken)
        + "\n\nUse the proven order from cron_flinders_dispatch.sh:\n"
          '  export LOGNAME="${LOGNAME:-$(id -un)}"\n'
          '  export USER="${USER:-$LOGNAME}"\n'
          "  set +u\n"
          "  source /etc/profile.d/modules.sh 2>/dev/null || true\n"
          "  source /etc/profile.d/hpccf.sh   2>/dev/null || true\n"
          "  set -u"
    )


def test_every_installed_cron_is_watched_by_the_heartbeat():
    """A scheduled job nothing watches can stop without anyone finding out.

    db_backup is the case that matters most: PG Farm keeps no backups of its
    own, so if that tick dies the loss surfaces only when a restore is needed.
    """
    from stan.reports.instrument_watch import CRON_LOGS
    assert "db_backup" in CRON_LOGS, "the pg_dump tick is unwatched"
    assert CRON_LOGS["db_backup"][0] == "db_backup_submit.log"
    # Daily 03:17 tick: one missed run must alert, a slow one must not.
    assert CRON_LOGS["db_backup"][1] > 24
