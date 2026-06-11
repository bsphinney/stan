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
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# QC filename pattern. Kept identical to the dispatcher's so "is this a QC
# run" has one definition. Imported when the installed stan is importable;
# otherwise the inline fallback keeps this script standalone on Hive.
try:  # pragma: no cover - import shim for standalone Hive runs
    from stan.community.scripts.dispatch_hive import DEFAULT_QC_PATTERN
except Exception:  # noqa: BLE001 - intentional: run before stan is updated
    DEFAULT_QC_PATTERN = r"(?i)(he(l[a5\d]|\d)|qc|std[_\-\s]?he)"

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


def _qc_raws(src: Path, pattern: str, since_days: float | None) -> list[Path]:
    """Recursively yield QC ``.d`` dirs and ``.raw`` files under ``src``.

    Prunes into ``.d`` directories (never descends past a Bruker run).
    Filters on the QC basename pattern and, when ``since_days`` is set, on
    mtime newer than the cutoff.
    """
    if not src.exists():
        logger.warning("Flinders source missing: %s", src)
        return []

    qc_re = re.compile(pattern)
    cutoff = time.time() - since_days * 86400 if since_days else None
    out: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(src):
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


def _is_qc_candidate(p: Path, qc_re: re.Pattern, cutoff: float | None) -> bool:
    """True when ``p`` is a QC raw newer than the cutoff and not a junk marker."""
    name = p.name
    low = name.lower()
    if any(low.endswith(s) for s in _SKIP_SUFFIXES):
        return False
    if not qc_re.search(name):
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
            pattern=DEFAULT_QC_PATTERN,
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
