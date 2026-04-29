"""Build a time-stratified, size-filtered random sample of HeLa QC files
for the v1.0 community re-search.

Designed to run ON HIVE (where the raw files actually live) — but works
anywhere with read access to the relevant paths. Walks the four known
QC roots, applies per-vendor minimum-size filters to drop obviously-
aborted acquisitions, then bucket-stratifies by month + per-instrument
and pulls a uniform random sample so the chosen rows span the full
time range, not just the most recent runs.

Output is a JSON manifest at ``--out`` that the SLURM dispatcher
(``stan submit-community-backlog``) consumes.

USAGE
    python -m stan.community.scripts.sample_backlog \
        --per-instrument 100 \
        --out /tmp/v1_sample.json
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Where HeLa QC raw files live on Hive. Add new paths here.
QC_ROOTS: dict[str, list[Path]] = {
    "Lumos": [
        Path("/quobyte/proteomics-grp/hela_qcs/lumos"),
        Path("/quobyte/proteomics-grp/to-hive/mass-spec-archive/lumos"),
        Path("/nfs/lssc0/flinders/proteomics/Data/raw_data/Lumos1"),
    ],
    "timsTOF": [
        Path("/quobyte/proteomics-grp/hela_qcs/timstofHT"),
        Path("/nfs/lssc0/flinders/proteomics/Data/raw_data/tTOF_HT"),
    ],
    "Exploris": [
        Path("/quobyte/proteomics-grp/hela_qcs/480"),
        Path("/nfs/lssc0/flinders/proteomics/Data/raw_data/Exploris480"),
    ],
}

# Per-instrument minimum file/dir size. Anything smaller is either an
# aborted acquisition or a placeholder.
DEFAULT_SIZE_MIN_GB: dict[str, float] = {
    "Lumos": 0.5,      # Thermo .raw, typical 1-5 GB
    "timsTOF": 3.0,    # Bruker .d directory, typical 10-30 GB
    "Exploris": 0.3,   # Thermo .raw, typical 0.5-3 GB
}

# HeLa-name match (loose, case-insensitive)
HELA_PATTERNS = ("hel50", "hela", "hel_", "hel-", "hel0", "_hel")


def _is_hela(name: str) -> bool:
    n = name.lower()
    return any(p in n for p in HELA_PATTERNS)


def _path_size(p: Path) -> int:
    """Total bytes — file size or sum-of-files for a directory."""
    try:
        if p.is_file():
            return p.stat().st_size
        if p.is_dir():
            return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0
    return 0


def enumerate_candidates(
    family: str,
    roots: list[Path],
    size_min_gb: float,
) -> list[dict]:
    """Walk roots, return [{path, size_bytes, mtime, family}] for HeLa QC."""
    cands: list[dict] = []
    seen: set[str] = set()
    size_min_bytes = int(size_min_gb * 1024**3)

    for root in roots:
        if not root.exists():
            logger.warning("Root not found: %s", root)
            continue
        # depth-limited walk
        for child in root.rglob("*"):
            name = child.name
            if name in seen:
                continue
            if not _is_hela(name):
                continue
            sfx = child.suffix.lower()
            if family == "timsTOF" and not (sfx == ".d" and child.is_dir()):
                continue
            if family in ("Lumos", "Exploris") and not (sfx == ".raw" and child.is_file()):
                continue
            size = _path_size(child)
            if size < size_min_bytes:
                continue
            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue
            cands.append({
                "path": str(child),
                "size_bytes": size,
                "mtime": mtime,
                "family": family,
                "name": name,
            })
            seen.add(name)
    logger.info(
        "%s: %d HeLa candidates passing size>=%.1f GB",
        family, len(cands), size_min_gb,
    )
    return cands


def _month_key(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m")


def stratified_sample(
    candidates: list[dict],
    per_instrument: int,
    seed: int,
) -> list[dict]:
    """Time-stratify by year-month within each family, then sample
    uniformly across buckets to reach `per_instrument` total per family.
    """
    rng = random.Random(seed)
    by_family: dict[str, list[dict]] = defaultdict(list)
    for c in candidates:
        by_family[c["family"]].append(c)

    selected: list[dict] = []
    for family, rows in by_family.items():
        if not rows:
            continue
        buckets: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            buckets[_month_key(r["mtime"])].append(r)

        # Round-robin pull from buckets (sorted by month) until we hit the cap
        ordered_months = sorted(buckets.keys())
        for m in ordered_months:
            rng.shuffle(buckets[m])
        picked = 0
        while picked < per_instrument and any(buckets[m] for m in ordered_months):
            for m in ordered_months:
                if not buckets[m]:
                    continue
                selected.append(buckets[m].pop())
                picked += 1
                if picked >= per_instrument:
                    break

    return selected


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--per-instrument", type=int, default=100,
                   help="How many files to sample per instrument (default 100).")
    p.add_argument("--seed", type=int, default=42,
                   help="Random seed for reproducibility.")
    p.add_argument("--out", default="/tmp/v1_sample.json",
                   help="Path for the JSON manifest.")
    p.add_argument("--family", action="append",
                   choices=list(QC_ROOTS.keys()),
                   help="Restrict to one or more families (default: all).")
    p.add_argument("--size-min-lumos-gb", type=float,
                   default=DEFAULT_SIZE_MIN_GB["Lumos"])
    p.add_argument("--size-min-timstof-gb", type=float,
                   default=DEFAULT_SIZE_MIN_GB["timsTOF"])
    p.add_argument("--size-min-exploris-gb", type=float,
                   default=DEFAULT_SIZE_MIN_GB["Exploris"])
    args = p.parse_args()

    families = args.family or list(QC_ROOTS.keys())
    size_mins = {
        "Lumos": args.size_min_lumos_gb,
        "timsTOF": args.size_min_timstof_gb,
        "Exploris": args.size_min_exploris_gb,
    }

    all_candidates: list[dict] = []
    for fam in families:
        cands = enumerate_candidates(fam, QC_ROOTS[fam], size_mins[fam])
        all_candidates.extend(cands)

    if not all_candidates:
        logger.error("No candidates found. Check paths + size filters.")
        sys.exit(1)

    selected = stratified_sample(all_candidates, args.per_instrument, args.seed)

    by_family = defaultdict(int)
    by_month = defaultdict(int)
    total_bytes = 0
    for s in selected:
        by_family[s["family"]] += 1
        by_month[_month_key(s["mtime"])] += 1
        total_bytes += s["size_bytes"]

    summary = {
        "n_total_candidates": len(all_candidates),
        "n_selected": len(selected),
        "per_family": dict(by_family),
        "size_filters_gb": size_mins,
        "total_size_gb": round(total_bytes / 1024**3, 1),
        "month_distribution": dict(sorted(by_month.items())),
        "seed": args.seed,
    }

    out = Path(args.out)
    out.write_text(json.dumps({"summary": summary, "files": selected}, indent=2))
    logger.info("Wrote %d selected files to %s", len(selected), out)
    logger.info("Summary: %s", json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
