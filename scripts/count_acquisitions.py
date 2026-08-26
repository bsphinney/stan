"""Count instrument acquisitions per day from the Flinders archive.

STAN deliberately ingests only HeLa/QC runs -- patient samples are never
searched or stored (privacy + cluster load). That makes the ``runs`` table
useless for answering "how busy is this instrument?", because it sees a few
QC injections a day out of a hundred real ones.

This script closes that gap **without touching sample data**. It walks the
Flinders archive and records nothing but a per-day count per instrument: no
filenames, no paths, no metadata, no file contents are read or emitted. The
output is a small aggregate JSON the dashboard reads to chart throughput and
utilisation.

Run it on Hive (never on an instrument PC), from cron::

    python scripts/count_acquisitions.py --out /quobyte/proteomics-grp/STAN/utilization.json

Acquisition date comes from the filesystem mtime of the ``.d`` directory or
``.raw`` file, which is the same signal ``link_flinders_qc.py`` uses for its
``--since-days`` window, so the two stay consistent.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("count_acq")

# Same archive roots as stan.community.scripts.link_flinders_qc.
FLINDERS_SOURCES: dict[str, Path] = {
    "timsTOF HT": Path("/nfs/lssc0/flinders/proteomics/Data/raw_data/tTOF_HT"),
    "Orbitrap Fusion Lumos": Path("/nfs/lssc0/flinders/proteomics/Data/raw_data/Lumos1"),
    "Orbitrap Exploris 480": Path("/nfs/lssc0/flinders/proteomics/Data/raw_data/Exploris480"),
}

# Nominal Evosep-style capacities to express utilisation against.
CAPACITIES = (100, 60)


def _recent_month_dirs(root: Path, since_epoch: float, slack_days: int = 40):
    """Top-level month dirs plausibly touched since ``since_epoch``.

    The archive is nested by month (``tTOF_HT/Aug26/<run>.d``). A full walk
    is ~11 minutes across 24k files, which is far too heavy to run often --
    and must never run on a login node. For an incremental refresh we only
    need the month dirs whose own mtime is recent, which is typically one or
    two directories and takes seconds. The slack window keeps a month dir
    whose mtime lags its contents.
    """
    cutoff = since_epoch - slack_days * 86400
    out = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        try:
            if d.stat().st_mtime >= cutoff:
                out.append(d)
        except OSError:
            continue
    return out


def _walk_acquisitions(root: Path, since_epoch: float):
    """Yield (date_str) for each acquisition under ``root``.

    A Bruker acquisition is a ``.d`` *directory*; a Thermo one is a ``.raw``
    file. Recurse with os.walk and prune inside any ``.d`` we match so its
    internal files aren't counted as separate acquisitions.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        keep = []
        for d in dirnames:
            if d.lower().endswith(".d"):
                p = Path(dirpath) / d
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                if mt >= since_epoch:
                    yield datetime.fromtimestamp(mt, timezone.utc).strftime("%Y-%m-%d")
                # don't descend into the .d -- it is one acquisition
            else:
                keep.append(d)
        dirnames[:] = keep

        for f in filenames:
            if not f.lower().endswith(".raw"):
                continue
            p = Path(dirpath) / f
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if mt >= since_epoch:
                yield datetime.fromtimestamp(mt, timezone.utc).strftime("%Y-%m-%d")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path,
                    default=Path("/quobyte/proteomics-grp/STAN/utilization.json"))
    ap.add_argument("--days", type=int, default=365,
                    help="Only count acquisitions newer than this many days.")
    ap.add_argument("--instrument", default="",
                    help="Limit to one instrument (substring match).")
    ap.add_argument("--merge", action="store_true",
                    help="Update only the days recomputed, keeping the rest "
                         "of the existing file. Pair with a small --days for "
                         "a cheap incremental refresh.")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    since_epoch = time.time() - args.days * 86400

    out: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_days": args.days,
        "capacities": list(CAPACITIES),
        "instruments": {},
    }

    for name, root in FLINDERS_SOURCES.items():
        if args.instrument and args.instrument.lower() not in name.lower():
            continue
        if not root.exists():
            logger.warning("%s: source missing (%s) — skipped", name, root)
            continue
        t0 = time.time()
        daily: dict[str, int] = defaultdict(int)
        # On an incremental run, only descend into recently-touched month dirs.
        roots = [root] if args.days > 120 else _recent_month_dirs(root, since_epoch)
        for sub in roots:
            for day in _walk_acquisitions(sub, since_epoch):
                daily[day] += 1
        total = sum(daily.values())
        out["instruments"][name] = {"daily": dict(sorted(daily.items())), "total": total}
        logger.info("%-24s %6d acquisitions across %4d days (%.0fs)",
                    name, total, len(daily), time.time() - t0)

    if args.merge and args.out.exists():
        try:
            prev = json.loads(args.out.read_text())
        except Exception as e:  # noqa: BLE001
            logger.warning("--merge: could not read %s (%s) — writing fresh", args.out, e)
            prev = None
        if prev:
            for name, blk in prev.get("instruments", {}).items():
                merged = dict(blk.get("daily") or {})
                merged.update(out["instruments"].get(name, {}).get("daily", {}))
                out["instruments"][name] = {
                    "daily": dict(sorted(merged.items())),
                    "total": sum(merged.values()),
                }
            out["window_days"] = max(prev.get("window_days", 0), args.days)
            logger.info("merged into existing counts from %s", prev.get("generated_at"))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=1))
    tmp.replace(args.out)          # atomic — dashboard may be reading it
    logger.info("wrote %s", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
