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

# Per-instrument minimum file/dir size. We use a low universal floor —
# file size is a weak filter (Orbitrap DDA is inversely correlated with
# size because MS2 is centroided; timsTOF DDA isn't). The real
# "did this run succeed" signal comes from the search, not the bytes
# on disk. The 200 MB floor only catches obviously-aborted runs (e.g.
# ``DEL-chekTipClog`` test files at 150 MB or KB-scale placeholders).
DEFAULT_SIZE_MIN_GB: dict[str, float] = {
    "Lumos": 0.2,
    "timsTOF": 0.2,
    "Exploris": 0.2,
}

# HeLa-name match (loose, case-insensitive)
HELA_PATTERNS = ("hel50", "hela", "hel_", "hel-", "hel0", "_hel")

# Mode disambiguation patterns. Path-based first (most reliable), then
# filename-based for the flinders flat layout. None of the timsTOF
# files have explicit "diaPASEF" or "ddaPASEF" tokens — the watcher
# uses TDF metadata for that — but the directory structure +
# filename-token heuristic catches >70% of the corpus.
DDA_PATH_TOKENS = ("/dda/", "/ddapasef/", "/dda-")
DIA_PATH_TOKENS = ("/dia/", "/diapasef/", "/dia-")
DDA_NAME_TOKENS = ("dda", "_dd_", "-dd-")
DIA_NAME_TOKENS = ("dia", "_di_", "-di-")


def _is_hela(name: str) -> bool:
    n = name.lower()
    return any(p in n for p in HELA_PATTERNS)


def _detect_mode(path: Path) -> str:
    """Best-effort DIA / DDA detection without reading TDF metadata.

    Path-based first (the hela_qcs/ tree has explicit /dia/ + /dda/
    subdirs), then filename token. Returns "dia", "dda", or "unknown".
    For ``unknown`` the watcher's TDF reader would resolve definitively;
    we can't easily call that from a stdlib-only sampler.
    """
    p_lower = str(path).lower()
    for tok in DDA_PATH_TOKENS:
        if tok in p_lower:
            return "dda"
    for tok in DIA_PATH_TOKENS:
        if tok in p_lower:
            return "dia"
    n_lower = path.name.lower()
    # Filename: order matters since "dda" and "dia" can both substring-match
    # weird names. Be strict: check 3-letter token AT WORD BOUNDARIES.
    import re

    if re.search(r"\bdda\b|[_-]dda[_-]|\bddapasef\b", n_lower):
        return "dda"
    if re.search(r"\bdia\b|[_-]dia[_-]|\bdiapasef\b", n_lower):
        return "dia"
    return "unknown"


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
                "mode": _detect_mode(child),
                "name": name,
            })
            seen.add(name)
    n_dia = sum(1 for c in cands if c["mode"] == "dia")
    n_dda = sum(1 for c in cands if c["mode"] == "dda")
    n_unk = sum(1 for c in cands if c["mode"] == "unknown")
    logger.info(
        "%s: %d HeLa candidates (size>=%.2f GB) — dia=%d dda=%d unknown=%d",
        family, len(cands), size_min_gb, n_dia, n_dda, n_unk,
    )
    return cands


def _month_key(mtime: float) -> str:
    return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m")


def stratified_sample(
    candidates: list[dict],
    per_dia: int,
    per_dda: int,
    seed: int,
) -> list[dict]:
    """Time-stratify by year-month per (family, mode), sample uniformly
    across buckets to reach the per-mode quota for each family.

    Unknown-mode rows are grouped with DIA (the dominant mode by ~5x);
    they'll get re-classified by the watcher / detector when actually
    searched.
    """
    rng = random.Random(seed)
    by_family_mode: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in candidates:
        bucket_mode = c["mode"] if c["mode"] in ("dia", "dda") else "dia"
        by_family_mode[(c["family"], bucket_mode)].append(c)

    selected: list[dict] = []
    quotas = {"dia": per_dia, "dda": per_dda}
    for (family, mode), rows in by_family_mode.items():
        if not rows:
            continue
        buckets: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            buckets[_month_key(r["mtime"])].append(r)

        ordered_months = sorted(buckets.keys())
        for m in ordered_months:
            rng.shuffle(buckets[m])
        picked = 0
        target = quotas[mode]
        while picked < target and any(buckets[m] for m in ordered_months):
            for m in ordered_months:
                if not buckets[m]:
                    continue
                selected.append(buckets[m].pop())
                picked += 1
                if picked >= target:
                    break

    return selected


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--per-instrument-dia", type=int, default=80,
                   help="How many DIA files to sample per instrument (default 80).")
    p.add_argument("--per-instrument-dda", type=int, default=20,
                   help="How many DDA files to sample per instrument (default 20).")
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

    selected = stratified_sample(
        all_candidates, args.per_instrument_dia, args.per_instrument_dda, args.seed
    )

    by_family_mode: dict[str, int] = defaultdict(int)
    by_month: dict[str, int] = defaultdict(int)
    total_bytes = 0
    for s in selected:
        by_family_mode[f"{s['family']}/{s['mode']}"] += 1
        by_month[_month_key(s["mtime"])] += 1
        total_bytes += s["size_bytes"]

    summary = {
        "n_total_candidates": len(all_candidates),
        "n_selected": len(selected),
        "per_family_mode": dict(sorted(by_family_mode.items())),
        "size_filters_gb": size_mins,
        "per_instrument_dia": args.per_instrument_dia,
        "per_instrument_dda": args.per_instrument_dda,
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
