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
