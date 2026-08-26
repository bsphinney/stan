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
import re
import sqlite3
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
    is ~11 minutes across 24k files, far too heavy to run often -- and it
    must never run on a login node. For an incremental refresh we only need
    the month dirs whose own mtime is recent, which is one or two
    directories. The slack window keeps a month dir whose mtime lags its
    contents.
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


# Thermo filenames follow the lab's DDMMYY convention (Ex260826_… =
# 26 Aug 2026, FL150526_… = 15 May 2026). Verified against PG run_date.
_THERMO_DATE = re.compile(r"^[A-Za-z]{1,3}[-_]?(\d{2})(\d{2})(\d{2})[_-]")


def _acq_date_bruker(d: Path) -> str | None:
    """Acquisition date from a Bruker .d's analysis.tdf, or None.

    ``GlobalMetadata.AcquisitionDateTime`` is what the instrument wrote at
    acquisition, so it survives being copied into the archive. Filesystem
    mtime does NOT: bulk syncs restamp thousands of files to the copy date,
    which produced impossible days (796 acquisitions, 1327% utilisation)
    when mtime was used as the acquisition date.
    """
    tdf = d / "analysis.tdf"
    if not tdf.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{tdf}?mode=ro", uri=True, timeout=5)
        row = con.execute(
            "SELECT Value FROM GlobalMetadata WHERE Key = ?",
            ("AcquisitionDateTime",),
        ).fetchone()
        con.close()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    return str(row[0])[:10]


def _acq_ts_bruker(d: Path) -> str | None:
    """Full acquisition timestamp (``YYYY-MM-DDTHH``) from a Bruker .d."""
    tdf = d / "analysis.tdf"
    if not tdf.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{tdf}?mode=ro", uri=True, timeout=5)
        row = con.execute(
            "SELECT Value FROM GlobalMetadata WHERE Key = ?",
            ("AcquisitionDateTime",),
        ).fetchone()
        con.close()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    return str(row[0])[:13]          # YYYY-MM-DDTHH


def _acq_date_thermo(f: Path) -> str | None:
    """Acquisition date parsed from a Thermo .raw filename, or None.

    There is no cheap .raw reader on Hive (no fisher_py in the venv, no
    dotnet on PATH), so the filename convention is the best available
    signal -- still far better than a copy-restamped mtime.
    """
    m = _THERMO_DATE.match(f.name)
    if not m:
        return None
    dd, mm, yy = (int(x) for x in m.groups())
    if not (1 <= dd <= 31 and 1 <= mm <= 12):
        return None
    return f"20{yy:02d}-{mm:02d}-{dd:02d}"


def _walk_acquisitions(root: Path, since_epoch: float, cache: dict, stats: dict):
    """Yield an acquisition date string for each acquisition under ``root``.

    A Bruker acquisition is a ``.d`` *directory*; a Thermo one is a ``.raw``
    file. Dates are cached by path because they never change once written,
    so only newly-archived files pay the metadata read.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        keep = []
        for d in dirnames:
            if d.lower().endswith(".d"):
                p = Path(dirpath) / d
                yield from _resolve(p, _acq_date_bruker, cache, stats, since_epoch)
            else:
                keep.append(d)
        dirnames[:] = keep

        for f in filenames:
            if f.lower().endswith(".raw"):
                yield from _resolve(Path(dirpath) / f, _acq_date_thermo,
                                    cache, stats, since_epoch)


def _resolve(p: Path, reader, cache: dict, stats: dict, since_epoch: float):
    """Yield the acquisition date for one path, consulting/filling the cache."""
    key = str(p)
    day = cache.get(key)
    if day is None:
        day = reader(p)
        if day:
            cache[key] = day
            stats["parsed"] = stats.get("parsed", 0) + 1
        else:
            # Fall back to mtime so the file is still counted, but record
            # how often we are guessing -- a large number here means the
            # counts are being driven by copy dates and can't be trusted.
            try:
                mt = p.stat().st_mtime
            except OSError:
                return
            if mt < since_epoch:
                return
            stats["mtime_fallback"] = stats.get("mtime_fallback", 0) + 1
            yield datetime.fromtimestamp(mt, timezone.utc).strftime("%Y-%m-%d")
            return
    else:
        stats["cached"] = stats.get("cached", 0) + 1
    yield day


def _hourly_counts(root: Path, since_epoch: float, cache: dict) -> dict:
    """Per-hour acquisition counts for a recent window.

    The day-level cache stores ``YYYY-MM-DD`` only, so hours need their own
    pass. It is scoped to a short window (a week or two), which is a few
    hundred files, so re-reading the Bruker metadata for them is cheap.
    Thermo falls back to mtime here: its filename convention carries a date
    but no time.
    """
    counts: dict[str, int] = defaultdict(int)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        keep = []
        for d in dirnames:
            if not d.lower().endswith(".d"):
                keep.append(d)
                continue
            p = Path(dirpath) / d
            try:
                if p.stat().st_mtime < since_epoch - 40 * 86400:
                    continue
            except OSError:
                continue
            key = "H:" + str(p)
            ts = cache.get(key) or _acq_ts_bruker(p)
            if ts:
                cache[key] = ts
                counts[ts] += 1
        dirnames[:] = keep
        for f in filenames:
            if not f.lower().endswith(".raw"):
                continue
            p = Path(dirpath) / f
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if mt < since_epoch:
                continue
            counts[datetime.fromtimestamp(mt, timezone.utc).strftime("%Y-%m-%dT%H")] += 1
    return dict(sorted(counts.items()))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path,
                    default=Path("/quobyte/proteomics-grp/STAN/utilization.json"))
    ap.add_argument("--days", type=int, default=365,
                    help="Only count acquisitions newer than this many days.")
    ap.add_argument("--instrument", default="",
                    help="Limit to one instrument (substring match).")
    ap.add_argument("--hourly-days", type=int, default=14,
                    help="Also emit per-hour counts for this many recent days "
                         "(0 disables). Drives the dashboard's hour heatmap.")
    ap.add_argument("--no-pg", action="store_true",
                    help="Skip publishing the snapshot to PG (file only).")
    ap.add_argument("--merge", action="store_true",
                    help="Update only the days recomputed, keeping the rest "
                         "of the existing file. Pair with a small --days for "
                         "a cheap incremental refresh.")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    since_epoch = time.time() - args.days * 86400

    cache_path = args.out.with_name("acq_date_cache.json")
    cache: dict = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
            logger.info("acquisition-date cache: %d entries", len(cache))
        except Exception as e:  # noqa: BLE001
            logger.warning("cache unreadable (%s) — starting empty", e)

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
        stats: dict = {}
        # On an incremental run, only descend into recently-touched month dirs.
        roots = [root] if args.days > 120 else _recent_month_dirs(root, since_epoch)
        for sub in roots:
            for day in _walk_acquisitions(sub, since_epoch, cache, stats):
                daily[day] += 1
        # Drop days outside the requested window (cached dates bypass the
        # mtime filter, so trim here rather than in the walker).
        cutoff_day = datetime.fromtimestamp(since_epoch, timezone.utc).strftime("%Y-%m-%d")
        daily = defaultdict(int, {d: n for d, n in daily.items() if d >= cutoff_day})
        total = sum(daily.values())
        hourly = {}
        if args.hourly_days > 0:
            h_since = time.time() - args.hourly_days * 86400
            for sub in _recent_month_dirs(root, h_since):
                for k, v in _hourly_counts(sub, h_since, cache).items():
                    hourly[k] = hourly.get(k, 0) + v
        out["instruments"][name] = {
            "daily": dict(sorted(daily.items())), "total": total,
            "hourly": dict(sorted(hourly.items())),
        }
        logger.info("%-24s %6d acquisitions across %4d days (%.0fs) "
                    "[parsed=%d cached=%d mtime_fallback=%d]",
                    name, total, len(daily), time.time() - t0,
                    stats.get("parsed", 0), stats.get("cached", 0),
                    stats.get("mtime_fallback", 0))

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
                merged_h = dict(blk.get("hourly") or {})
                merged_h.update(out["instruments"].get(name, {}).get("hourly", {}))
                out["instruments"][name] = {
                    "daily": dict(sorted(merged.items())),
                    "total": sum(merged.values()),
                    "hourly": dict(sorted(merged_h.items())),
                }
            out["window_days"] = max(prev.get("window_days", 0), args.days)
            logger.info("merged into existing counts from %s", prev.get("generated_at"))

    try:
        tmpc = cache_path.with_suffix(".json.tmp")
        tmpc.write_text(json.dumps(cache))
        tmpc.replace(cache_path)
        logger.info("acquisition-date cache now %d entries", len(cache))
    except OSError as e:
        logger.warning("could not persist cache: %s", e)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(out, indent=1))
    tmp.replace(args.out)          # atomic — dashboard may be reading it
    logger.info("wrote %s", args.out)

    # Also publish to PG. The mirror file only reaches hosts that mount
    # Quobyte; a hosted dashboard has no such mount and would show
    # "not found on the Hive mirror" forever.
    if not args.no_pg:
        try:
            from stan.db_pg import put_utilization_snapshot
            put_utilization_snapshot(out.get("generated_at") or "", json.dumps(out))
            logger.info("published utilization snapshot to PG")
        except Exception as e:  # noqa: BLE001 - the file write already succeeded
            logger.warning("could not publish to PG (%s); mirror file still written", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
