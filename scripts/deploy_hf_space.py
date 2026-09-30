"""Deploy hf_space/app.py to the HF Space brettsp/stan without clobbering it.

The Space has no canonical copy anywhere but itself: for 135 commits it was
edited by downloading app.py, patching it and uploading it back. Since v1.2.0
(PEG Watch) the relay is vendored in this repo as ``hf_space/app.py`` so its
changes are reviewed and tested here. That only stays safe if nothing edited
in the Space directly is ever silently overwritten, so this script refuses to
upload unless the Space's live app.py is still byte-for-byte the copy the
vendored file was taken from (``RECORDED_BASE_SHA256``), or is already the
local file (nothing to do).

Usage::

    python scripts/deploy_hf_space.py                 # dry run: checks + diff stat
    python scripts/deploy_hf_space.py --yes           # upload hf_space/app.py
    python scripts/deploy_hf_space.py --yes --record-base

``app.py`` is uploaded, and ``Dockerfile`` with it when the local copy
differs (same guard: the live Dockerfile must still be the recorded base).
The Space's README and static/ are left alone. The upload names the Space
commit it was checked against as its parent, so an edit that lands between
the check and the upload makes the commit fail instead of being overwritten.

Why the Dockerfile ships too: it used to ``pip install`` every package
unpinned, so each rebuild took whatever was newest. huggingface_hub 2.0
dropped the ``huggingface_hub.hf_api.CommitOperationAdd`` re-export the relay
imported, and GitHub CI caught it (2026-09-29): the next rebuild of the live
Space, for any reason, would have crashed at import and taken the public
site down. The pinned versions are the ones tests/test_relay_peg.py passes
against.

After an upload the script polls ``/api/version`` until the rebuilt Space
reports the new ``SPACE_VERSION``, then prints ``/api/health``. A redeploy
restarts the container and drops any submissions still in the relay's
in-memory commit queue, so ``--yes`` refuses to run in the half hour around
the batch syncs (00:27, 06:27, 12:27, 18:27 Pacific, Hive's local time, so
01:27 UTC and on in summer but 02:27 UTC and on in winter) unless told
otherwise.

``--record-base`` rewrites ``RECORDED_BASE_SHA256`` below to the deployed
file once the Space is confirmed to be serving it, so the next deploy is
checked against what is actually live.

Authentication is the default Hugging Face token cache
(``huggingface-cli login``); no token is read from the environment here.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import logging
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("deploy_hf_space")

SPACE_REPO = "brettsp/stan"
SPACE_URL = "https://brettsp-stan.hf.space"
REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_APP = REPO_ROOT / "hf_space" / "app.py"
LOCAL_DOCKERFILE = REPO_ROOT / "hf_space" / "Dockerfile"
SCRIPT_PATH = Path(__file__).resolve()

# sha256 of the Space's app.py that hf_space/app.py was last synced from:
# Space commit d041ef68, SPACE_VERSION 1.1.0, vendored 2026-09-28.
# `--record-base` rewrites this line after a verified deploy.
RECORDED_BASE_SHA256 = "09ac8fd6ca0d3e7fe2ec4bcf12300de39b8b2ddf51b219a327a7e1b786c1b0f4"
# The same for the Space's Dockerfile (unpinned pip installs at d041ef68).
RECORDED_DOCKERFILE_SHA256 = "963a848c863615304108c022338bc4f85189745fe80d5bd005c6bfb564244dcc"

# Hive's crontab runs cron_community_sync.sh at "25 */6 * * *" in the
# system zone, America/Los_Angeles (no CRON_TZ), so it submits at about :27
# past these LOCAL hours, and the relay flushes its queue about a minute
# after each request. In UTC that is 01/07/13/19 under PDT but 02/08/14/20
# under PST, which is why the guard is evaluated in Hive's zone, not UTC.
SYNC_TZ = "America/Los_Angeles"
SYNC_HOURS_LOCAL = (0, 6, 12, 18)
SYNC_GUARD_MINUTES = (15, 45)
# Pacific's two UTC offsets, guarded together when there is no tz database.
_SYNC_FALLBACK_OFFSETS = (timezone(timedelta(hours=-7)), timezone(timedelta(hours=-8)))

_BASE_LINE_RE = re.compile(r'^RECORDED_BASE_SHA256 = "[0-9a-f]{64}"$', re.MULTILINE)
_DOCKERFILE_LINE_RE = re.compile(r'^RECORDED_DOCKERFILE_SHA256 = "[0-9a-f]{64}"$', re.MULTILINE)
_VERSION_RE = re.compile(r'^SPACE_VERSION = "([^"]+)"', re.MULTILINE)


def sha256_bytes(data: bytes) -> str:
    """Hex sha256 of ``data``."""
    return hashlib.sha256(data).hexdigest()


def space_version_of(source: str) -> str | None:
    """The ``SPACE_VERSION = "..."`` value declared in an app.py source."""
    m = _VERSION_RE.search(source)
    return m.group(1) if m else None


def classify(live_sha: str, local_sha: str, base_sha: str) -> str:
    """Where the live Space stands relative to the local file.

    Returns:
        "deployed" when the Space already serves the local file, "base" when
        it still serves the recorded base (safe to upload), else "drift"
        (edited in the Space since it was vendored; never overwrite).
    """
    if live_sha == local_sha:
        return "deployed"
    if live_sha == base_sha:
        return "base"
    return "drift"


def diff_stat(old: str, new: str) -> tuple[int, int, int]:
    """(lines added, lines removed, hunks) of a unified diff from old to new."""
    added = removed = hunks = 0
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if line.startswith("@@"):
            hunks += 1
        elif line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed, hunks


def in_sync_window(now: datetime) -> bool:
    """True inside the guard window around a scheduled community sync.

    ``now`` must be timezone-aware. Without a tz database (Windows lacking
    the tzdata package) both Pacific offsets are guarded, which blocks an
    extra half hour four times a day rather than missing a sync.
    """
    lo, hi = SYNC_GUARD_MINUTES
    try:
        zones = (ZoneInfo(SYNC_TZ),)
    except ZoneInfoNotFoundError:
        zones = _SYNC_FALLBACK_OFFSETS
    return any(
        t.hour in SYNC_HOURS_LOCAL and lo <= t.minute < hi
        for t in (now.astimezone(z) for z in zones)
    )


def rewrite_recorded_base(script_path: Path, new_sha: str, constant: str = "RECORDED_BASE_SHA256") -> None:
    """Point the recorded-base ``constant`` in ``script_path`` at ``new_sha``.

    Raises:
        ValueError: ``new_sha`` is not a sha256, or the constant line is not
            found exactly once (the rewrite would be ambiguous).
    """
    if not re.fullmatch(r"[0-9a-f]{64}", new_sha):
        raise ValueError(f"not a sha256: {new_sha!r}")
    pattern = {"RECORDED_BASE_SHA256": _BASE_LINE_RE, "RECORDED_DOCKERFILE_SHA256": _DOCKERFILE_LINE_RE}[constant]
    text = script_path.read_text()
    new_text, n = pattern.subn(f'{constant} = "{new_sha}"', text)
    if n != 1:
        raise ValueError(f"expected one {constant} line in {script_path}, found {n}")
    script_path.write_text(new_text)


def fetch_live(repo_id: str = SPACE_REPO) -> tuple[str, bytes]:
    """(Space commit sha, app.py bytes at that commit), bypassing any local cache."""
    from huggingface_hub import HfApi

    commit = HfApi().space_info(repo_id).sha
    return commit, fetch_live_file(repo_id, commit, "app.py")


def fetch_live_file(repo_id: str, commit: str, filename: str) -> bytes:
    """Bytes of ``filename`` in the Space at ``commit``, bypassing any local cache."""
    from huggingface_hub import hf_hub_download

    with tempfile.TemporaryDirectory(prefix="stan_space_") as tmp:
        path = hf_hub_download(
            repo_id, filename, repo_type="space", revision=commit,
            cache_dir=tmp, force_download=True,
        )
        return Path(path).read_bytes()


def upload(local_path: Path, repo_id: str, parent_commit: str, version: str,
           extra: dict[str, Path] | None = None) -> str:
    """Upload ``local_path`` as the Space's app.py, plus ``extra`` files, in one commit.

    ``extra`` maps a path in the Space to a local file (the Dockerfile). One
    commit, so the Space never rebuilds with a new app.py on old pins or the
    reverse. Returns the new commit URL.
    """
    from huggingface_hub import CommitOperationAdd, HfApi

    ops = [CommitOperationAdd(path_in_repo="app.py", path_or_fileobj=str(local_path))]
    ops += [CommitOperationAdd(path_in_repo=k, path_or_fileobj=str(v)) for k, v in (extra or {}).items()]
    names = " + ".join(["hf_space/app.py"] + [f"hf_space/{k}" for k in (extra or {})])
    info = HfApi().create_commit(
        repo_id=repo_id,
        repo_type="space",
        operations=ops,
        commit_message=f"relay {version}: deploy {names} from the STAN repo",
        parent_commit=parent_commit,
    )
    return str(getattr(info, "commit_url", info))


def get_json(url: str, timeout: float = 15.0) -> tuple[int, object]:
    """(status code, parsed JSON or response text) for a GET."""
    import httpx

    r = httpx.get(url, timeout=timeout, follow_redirects=True)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:200]


def wait_for_version(base_url: str, expected: str, timeout_sec: float, interval_sec: float = 15.0) -> bool:
    """Poll ``/api/version`` until it reports ``expected``; False on timeout."""
    deadline = time.monotonic() + timeout_sec
    while True:
        try:
            status, body = get_json(f"{base_url}/api/version")
            seen = body.get("version") if isinstance(body, dict) else None
            logger.info("  /api/version -> %s %s", status, seen)
            if status == 200 and seen == expected:
                return True
        except Exception as e:  # noqa: BLE001 - the Space is rebuilding; keep polling
            logger.info("  /api/version not reachable yet (%s)", type(e).__name__)
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_sec)


def report_health(base_url: str) -> None:
    """Log /api/health and whether the PEG leaderboard answers."""
    for path in ("/api/health", "/api/peg/leaderboard"):
        try:
            status, body = get_json(f"{base_url}{path}")
        except Exception as e:  # noqa: BLE001 - reporting only
            logger.warning("%s: %s", path, e)
            continue
        if path == "/api/peg/leaderboard" and isinstance(body, dict):
            body = {"ranked": len(body.get("ranked") or []), "cohorts": body.get("cohorts")}
        logger.info("%s -> %s %s", path, status, body)


def main(argv: list[str] | None = None) -> int:
    """Run the deploy check (and the upload with --yes). Returns a process exit code."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="Upload. Without it this is a dry run.")
    ap.add_argument("--record-base", action="store_true",
                    help="After a verified deploy, record the deployed sha256 as the new base.")
    ap.add_argument("--timeout", type=float, default=900.0,
                    help="Seconds to wait for the rebuilt Space to report the new version (default 900).")
    ap.add_argument("--ignore-sync-window", action="store_true",
                    help="Deploy even in the half hour around a scheduled community sync.")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    local_bytes = LOCAL_APP.read_bytes()
    local_sha = sha256_bytes(local_bytes)
    local_version = space_version_of(local_bytes.decode("utf-8"))
    commit, live_bytes = fetch_live(SPACE_REPO)
    live_sha = sha256_bytes(live_bytes)
    live_version = space_version_of(live_bytes.decode("utf-8"))
    state = classify(live_sha, local_sha, RECORDED_BASE_SHA256)

    added, removed, hunks = diff_stat(live_bytes.decode("utf-8"), local_bytes.decode("utf-8"))
    logger.info("Space %s @ %s: app.py sha256 %s (SPACE_VERSION %s)",
                SPACE_REPO, commit[:8], live_sha[:16], live_version)
    logger.info("local %s: sha256 %s (SPACE_VERSION %s)",
                LOCAL_APP.relative_to(REPO_ROOT), local_sha[:16], local_version)
    logger.info("recorded base: sha256 %s", RECORDED_BASE_SHA256[:16])
    logger.info("diff live -> local: +%d -%d lines in %d hunks", added, removed, hunks)

    local_docker = LOCAL_DOCKERFILE.read_bytes()
    live_docker = fetch_live_file(SPACE_REPO, commit, "Dockerfile")
    docker_state = classify(sha256_bytes(live_docker), sha256_bytes(local_docker), RECORDED_DOCKERFILE_SHA256)
    logger.info("Dockerfile: live sha256 %s, local %s -> %s",
                sha256_bytes(live_docker)[:16], sha256_bytes(local_docker)[:16], docker_state)
    if docker_state == "drift":
        logger.error(
            "REFUSING: the Space's Dockerfile matches neither the recorded base nor "
            "hf_space/Dockerfile, so it was edited in the Space. Merge that edit first."
        )
        return 3
    extra = {"Dockerfile": LOCAL_DOCKERFILE} if docker_state == "base" else {}

    if state == "drift":
        logger.error(
            "REFUSING: the Space's app.py matches neither the recorded base nor the local "
            "file, so it was edited in the Space. Download it, merge those edits into "
            "hf_space/app.py, then re-run (and --record-base once it is live)."
        )
        return 3

    if state == "deployed":
        logger.info("The Space already serves hf_space/app.py; nothing to upload.")
        if extra:
            logger.warning("hf_space/Dockerfile differs from the Space's; it ships with the next "
                           "app.py deploy (bump SPACE_VERSION to make one).")
        if args.record_base and RECORDED_BASE_SHA256 != live_sha:
            rewrite_recorded_base(SCRIPT_PATH, live_sha)
            logger.info("Recorded base updated to %s", live_sha[:16])
        report_health(SPACE_URL)
        return 0

    # state == "base": the Space is untouched since vendoring; safe to upload.
    if not local_version or local_version == live_version:
        logger.error(
            "REFUSING: SPACE_VERSION is %s live and %s locally. Bump it in hf_space/app.py "
            "-- the post-deploy check waits for the new version, so an unbumped deploy "
            "cannot be told apart from the old container still running.",
            live_version, local_version,
        )
        return 4
    if not args.yes:
        if args.record_base:
            logger.error("--record-base needs a deploy (--yes) or an already-deployed Space.")
            return 2
        logger.info("Dry run: would upload hf_space/app.py%s (%s -> %s). Re-run with --yes.",
                    " + Dockerfile" if extra else "", live_version, local_version)
        return 0
    now = datetime.now(timezone.utc)
    if in_sync_window(now) and not args.ignore_sync_window:
        logger.error(
            "REFUSING: %s UTC is inside the community-sync window (HH:%02d-HH:%02d at %s %s). "
            "A redeploy drops the relay's unflushed commit queue. Try again later or pass "
            "--ignore-sync-window.", now.strftime("%H:%M"), *SYNC_GUARD_MINUTES,
            ", ".join(f"{h:02d}" for h in SYNC_HOURS_LOCAL), SYNC_TZ,
        )
        return 5

    url = upload(LOCAL_APP, SPACE_REPO, parent_commit=commit, version=local_version, extra=extra)
    logger.info("Uploaded: %s", url)
    logger.info("Waiting up to %.0f s for the Space to report %s ...", args.timeout, local_version)
    ok = wait_for_version(SPACE_URL, local_version, args.timeout)
    report_health(SPACE_URL)
    if not ok:
        logger.error("The Space did not report %s within %.0f s; check its build logs.",
                     local_version, args.timeout)
        return 6
    logger.info("Deployed: the Space reports SPACE_VERSION %s.", local_version)
    if args.record_base:
        rewrite_recorded_base(SCRIPT_PATH, local_sha)
        logger.info("Recorded base updated to %s", local_sha[:16])
        if extra:
            rewrite_recorded_base(SCRIPT_PATH, sha256_bytes(local_docker), "RECORDED_DOCKERFILE_SHA256")
            logger.info("Recorded Dockerfile base updated to %s", sha256_bytes(local_docker)[:16])
    return 0


if __name__ == "__main__":
    sys.exit(main())
