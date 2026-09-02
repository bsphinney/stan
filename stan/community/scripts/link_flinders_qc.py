"""Flatten Flinders QC raws into the Hive dispatcher's flat watch dirs.

The lab's instruments sync their raw acquisitions to the Flinders NFS
archive (``/nfs/lssc0/flinders/proteomics/Data/raw_data/<instrument>``),
which is **nested by month** (``tTOF_HT/June26/<run>.d``) and mixed with
patient samples, blanks, washes, and Thermo ``.sld`` sequence files.

``stan hive-dispatch`` walks a **flat** per-instrument ``watch_dir`` and is
non-recursive by design (``dispatch_hive._walk_raws``). So it never sees
the Flinders raws. This script bridges the two: it walks each Flinders
source recursively, matches the HeLa/QC filename pattern, and creates a
symlink for every QC ``.d`` / ``.raw`` into the matching ``watch_dir``.
The dispatcher then picks them up on its next walk; the SLURM search job
reads the raw through the symlink (compute nodes mount ``/nfs`` — verified
2026-06-10, job 15268298).

Design notes:
- **QC-only.** We deliberately skip non-QC raws. The full Flinders archive
  is ~24k files, almost all patient samples we must NOT search (privacy +
  cluster load). The dispatcher's ``DEFAULT_QC_PATTERN`` is the source of
  truth for "is this a QC run".
- **Idempotent.** Re-running re-points nothing: an existing symlink to the
  same target is left alone. A basename collision across month dirs (rare
  for timestamped QC names) keeps the first and logs the rest — matches the
  runs table, which dedups by basename anyway.
- **Login-node-safe.** Pure filesystem + symlink creation, no compute. Per
  CLAUDE.md, real work lands inside SLURM via the dispatcher, never here.
- **Destinations come from dispatch.yml**, so this never drifts from what
  the dispatcher actually watches. Only the Flinders *source* per family is
  hard-coded here (it's stable archive layout).

Typical use on Hive::

    # dry-run: see what would be linked, link nothing
    python -m stan.community.scripts.link_flinders_qc \\
        --config /quobyte/proteomics-grp/STAN/dispatch.yml \\
        --since-days 40 --dry-run

    # real run, then let the dispatcher submit the jobs
    python -m stan.community.scripts.link_flinders_qc --since-days 40
    stan hive-dispatch --partition low

Pair it on a cron with ``stan hive-dispatch`` so new Flinders QCs flow
automatically (this was the missing piece — no dispatch cron was installed).
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# QC filename pattern. Kept identical to the dispatcher's so "is this a QC
# run" has one definition. Imported when the installed stan is importable;
# otherwise the inline fallback keeps this script standalone on Hive.
try:  # pragma: no cover - import shim for standalone Hive runs
    from stan.community.scripts.dispatch_hive import DEFAULT_QC_PATTERN
except Exception:  # noqa: BLE001 - intentional: run before stan is updated
    DEFAULT_QC_PATTERN = r"(?i)(he(l[_\-\s]?[a5\d]|[_\-\s]?\d)|qc|std[_\-\s]?he)"

# Flinders archive source per instrument family. Stable layout; the
# destination watch_dir is read from dispatch.yml so it can't drift.
FLINDERS_SRC_BY_FAMILY: dict[str, Path] = {
    "timsTOF": Path("/nfs/lssc0/flinders/proteomics/Data/raw_data/tTOF_HT"),
    "Lumos": Path("/nfs/lssc0/flinders/proteomics/Data/raw_data/Lumos1"),
    "Exploris": Path("/nfs/lssc0/flinders/proteomics/Data/raw_data/Exploris480"),
}

# Upload/junk markers we never link.
_SKIP_SUFFIXES = (".partial", ".tmp", ".quant", ".sld", ".sld.partial")


def _load_instruments(config_path: Path) -> list[dict]:
    """Read instrument family + watch_dir pairs from the dispatcher YAML.

    Returns one dict per instrument with keys ``name``, ``family``,
    ``watch_dir``. Raises if the config is unreadable — we'd rather fail
    loud than silently link nothing.
    """
    import yaml

    with config_path.open() as fh:
        cfg = yaml.safe_load(fh)
    out: list[dict] = []
    for inst in cfg.get("instruments", []):
        out.append(
            {
                "name": inst.get("name", ""),
                "family": inst.get("family", ""),
                "watch_dir": Path(inst["watch_dir"]),
            }
        )
    return out


# Directories under the instrument's raw_data root that are not
# acquisitions. The export mixes real month directories in with method
# libraries, reports and engineer service folders, and walking those wastes
# NFS stats and — worse — feeds non-acquisition files into the dispatcher.
# `Service/` is the concrete case: its post-digitizer-replacement tune files
# were being submitted as monitor jobs on every tick and failing every time,
# because a tune file has no analysis.tdf to read.
#
# Deliberately NOT excluded: `QC` and `HeLSTDs`, which plausibly hold real
# HeLa standard acquisitions. Excluding a directory silently drops data, so
# the list stays limited to names that cannot be acquisitions.
_NON_DATA_DIRS = {
    "libraries", "msmeth", "reports", "diann", "evoseplcmeth",
    "service", "servicebrukerengineers",
}


def _recent_month_dirs(
    src: Path, window_days: float, keep_newest: int = 2
) -> set[str] | None:
    """Names of ``src``'s immediate subdirs worth walking, by creation date.

    The Flinders export is organised into one directory per month, and new
    acquisitions only ever land in the current one. Walking all of them cost
    an NFS ``stat()`` on every ``.d`` in the archive — ~15,000 stats at ~6 ms
    each, which is the ~90 s the linker was spending per tick to notice that
    nothing had changed in 2025.

    Selection is by **creation time** (``st_birthtime``), not by name and not
    by mtime:

    - Names are unusable. The export holds ``Aug26``, ``JUL26``, ``july26``,
      ``June26``, ``may26``, ``jan25AndPM``,
      ``Bruker_FAS_Promega_samples_Mar26``, ``Libraries``, ``QC``, ``Service``
      and ``desktop.ini``. Any parser over that is a future outage.
    - mtime is misleading. A directory's mtime bumps whenever anything is
      added or removed, so a bulk relink touches decade-old months: on
      2026-08-27 five stale month dirs shared yesterday's mtime, and a
      30-day mtime filter kept 20 of 32 dirs. Creation time kept 2.

    ``keep_newest`` dirs are always retained regardless of the window, so a
    month boundary can never select nothing — if September's directory has
    not been created yet, August's is still walked.

    Returns None when creation time is unavailable (not every filesystem
    reports it), which makes the caller walk everything as before. Slow is
    an acceptable failure mode here; silently skipping new data is not.
    """
    cutoff = time.time() - window_days * 86400
    try:
        children = [
            c for c in src.iterdir()
            if c.is_dir() and c.name.lower() not in _NON_DATA_DIRS
        ]
    except OSError:
        logger.warning("could not list %s for month pruning", src, exc_info=True)
        return None
    if not children:
        return None

    # Creation time has to come from `stat -c %W`, not os.stat(): CPython
    # exposes st_birthtime on macOS/BSD but not on Linux before 3.12, and
    # Hive runs 3.11 — reading it in Python there returns nothing at all and
    # would silently disable this pruning, leaving the full walk in place.
    # One subprocess covers every candidate, so the cost is a single fork.
    dated: list[tuple[float, str]] = []
    undated: set[str] = set()
    try:
        proc = subprocess.run(
            ["stat", "-c", "%W|%n", *[str(c) for c in children]],
            capture_output=True, text=True, timeout=60, check=False,
        )
        for line in proc.stdout.splitlines():
            born_s, _, path_s = line.partition("|")
            name = Path(path_s).name
            try:
                born = float(born_s)
            except ValueError:
                born = 0.0
            if born > 0:
                dated.append((born, name))
            else:
                undated.add(name)
    except (OSError, subprocess.SubprocessError):
        logger.warning("stat -c %%W failed under %s", src, exc_info=True)
        return None

    # A directory whose creation time we cannot read is always walked. One
    # odd entry should cost a little extra work, not silently switch the
    # whole archive back to the full walk — and must never cause new data
    # to be skipped.
    seen = {n for _, n in dated} | undated
    undated |= {c.name for c in children if c.name not in seen}

    if not dated:
        logger.info("no creation times under %s — walking all", src)
        return None

    if not dated:
        return None

    dated.sort(reverse=True)
    keep = {name for born, name in dated if born >= cutoff}
    keep.update(name for _, name in dated[:keep_newest])
    keep |= undated
    logger.info(
        "month pruning: walking %d of %d dirs under %s (%s)",
        len(keep), len(dated), src.name, ", ".join(sorted(keep)),
    )
    return keep


def _qc_raws(
    src: Path, pattern: str, since_days: float | None,
    month_window_days: float = 45.0,
) -> list[Path]:
    """Recursively yield QC ``.d`` dirs and ``.raw`` files under ``src``.

    Prunes into ``.d`` directories (never descends past a Bruker run).
    Filters on the QC basename pattern and, when ``since_days`` is set, on
    mtime newer than the cutoff.
    """
    if not src.exists():
        logger.warning("Flinders source missing: %s", src)
        return []

    # An empty pattern means "take everything" (--all-runs): non-QC
    # acquisitions still get linked so the Sample Health monitor can report
    # instrument health on real samples, not just HeLa standards.
    qc_re = re.compile(pattern) if pattern else None
    cutoff = time.time() - since_days * 86400 if since_days else None
    out: list[Path] = []

    # Only descend into recently-created month dirs. Skipped when the caller
    # asked for no time window at all (a full backfill), which must still
    # see the whole archive.
    recent = _recent_month_dirs(src, month_window_days) if since_days else None

    for dirpath, dirnames, filenames in os.walk(src):
        if recent is not None and Path(dirpath) == src:
            dirnames[:] = [d for d in dirnames if d in recent]
        # Identify .d run directories at this level and prune them so we
        # don't walk their internals.
        d_runs = [d for d in dirnames if d.lower().endswith(".d")]
        for d in d_runs:
            dirnames.remove(d)
            p = Path(dirpath) / d
            if _is_qc_candidate(p, qc_re, cutoff):
                out.append(p)
        for f in filenames:
            if not f.lower().endswith(".raw"):
                continue
            p = Path(dirpath) / f
            if _is_qc_candidate(p, qc_re, cutoff):
                out.append(p)
    return out


def _is_qc_candidate(p: Path, qc_re: "re.Pattern | None", cutoff: float | None) -> bool:
    """True when ``p`` is a QC raw newer than the cutoff and not a junk marker."""
    name = p.name
    low = name.lower()
    if any(low.endswith(s) for s in _SKIP_SUFFIXES):
        return False
    if qc_re is not None and not qc_re.search(name):
        return False
    if cutoff is not None:
        try:
            if p.stat().st_mtime < cutoff:
                return False
        except OSError:
            return False
    return True


def link_family(
    family: str,
    watch_dir: Path,
    pattern: str,
    since_days: float | None,
    dry_run: bool,
) -> dict:
    """Link every QC raw for one instrument family into its watch_dir.

    Returns a summary dict: ``linked``, ``existing``, ``collision``,
    ``candidates``.
    """
    src = FLINDERS_SRC_BY_FAMILY.get(family)
    summary = {"linked": 0, "existing": 0, "collision": 0, "candidates": 0}
    if src is None:
        logger.warning("no Flinders source mapped for family %r; skipping", family)
        return summary

    raws = _qc_raws(src, pattern, since_days)
    summary["candidates"] = len(raws)
    if not dry_run:
        watch_dir.mkdir(parents=True, exist_ok=True)

    for raw in raws:
        target = os.path.realpath(raw)
        link = watch_dir / raw.name

        if link.exists() or link.is_symlink():
            # Idempotent: same target -> done. Different target -> basename
            # collision across month dirs; keep the first, log the rest.
            try:
                if link.is_symlink() and os.path.realpath(link) == target:
                    summary["existing"] += 1
                    continue
            except OSError:
                pass
            summary["collision"] += 1
            logger.debug("collision, keeping existing: %s", link.name)
            continue

        if dry_run:
            summary["linked"] += 1
            continue
        try:
            os.symlink(target, link)
            summary["linked"] += 1
        except OSError as exc:
            logger.error("symlink failed for %s: %s", raw.name, exc)

    logger.info(
        "%-9s src=%s candidates=%d linked=%d existing=%d collisions=%d",
        family, src, summary["candidates"], summary["linked"],
        summary["existing"], summary["collision"],
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    """CLI entry: scan Flinders, symlink QC raws into the dispatcher watch dirs."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--config", type=Path,
        default=Path("/quobyte/proteomics-grp/STAN/dispatch.yml"),
        help="Dispatcher YAML; watch_dir destinations are read from it.",
    )
    ap.add_argument(
        "--since-days", type=float, default=40.0,
        help="Only link QC raws modified within this many days (0 = all history).",
    )
    ap.add_argument(
        "--instrument", default="",
        help="Substring filter on instrument family/name (case-insensitive).",
    )
    ap.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be linked; create no symlinks.",
    )
    ap.add_argument(
        "--all-runs", action="store_true",
        help="Link NON-QC acquisitions too, so the Sample Health monitor can "
             "report instrument health on real samples. The monitor reads "
             "rawmeat metadata only (frame counts, TIC, pressure, dropouts); "
             "it never searches these and they never enter the community "
             "benchmark.",
    )
    ap.add_argument(
        "--log-dir", type=Path,
        default=Path("/quobyte/proteomics-grp/STAN/logs"),
        help="Where to write the run log (shared FS, survives for remote debug).",
    )
    args = ap.parse_args(argv)

    since = args.since_days if args.since_days > 0 else None

    args.log_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = args.log_dir / f"link_flinders_{stamp}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler(sys.stdout)],
    )

    logger.info(
        "link_flinders_qc start | config=%s since_days=%s dry_run=%s",
        args.config, args.since_days, args.dry_run,
    )

    instruments = _load_instruments(args.config)
    flt = args.instrument.lower()
    totals = {"linked": 0, "existing": 0, "collision": 0, "candidates": 0}
    for inst in instruments:
        if flt and flt not in inst["family"].lower() and flt not in inst["name"].lower():
            continue
        s = link_family(
            family=inst["family"],
            watch_dir=inst["watch_dir"],
            pattern=("" if args.all_runs else DEFAULT_QC_PATTERN),
            since_days=since,
            dry_run=args.dry_run,
        )
        for k in totals:
            totals[k] += s[k]

    logger.info(
        "DONE | candidates=%d linked=%d existing=%d collisions=%d | log=%s",
        totals["candidates"], totals["linked"], totals["existing"],
        totals["collision"], log_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
