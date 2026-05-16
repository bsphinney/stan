#!/usr/bin/env python3
"""Smoke-test the PEG detector against real Bruker .d files on Hive.

Usage (on a host that has alphatims installed):
    python3 peg_smoke_test.py /path/to/file1.d /path/to/file2.d ...

Or batch:
    python3 peg_smoke_test.py /quobyte/proteomics-grp/hela_qcs/timstofHT/dia/*.d

Output: one row per input file, plus a CSV at the end if --csv given.

The script reads MS1 frames via alphatims, subsamples to N_SCANS_PER_FILE
spectra (random across the gradient — full-file is too slow for a smoke
test), runs detect_peg_in_spectra(), and prints peg_score + breakdown.

A "clean" timsTOF QC should score < 20. A "trace" customer sample
might score 20-50. PEG-contaminated samples will score 50+.
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

# Make stan/ importable when running from anywhere
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from stan.metrics.peg import detect_peg_in_spectra, PEG_REFERENCE  # noqa: E402

# Sampling — full files have 5-20k MS1 frames, way more than we need
# to detect a sustained ladder. Even 50 random scans hits PEG with
# very high confidence if it's there.
N_SCANS_PER_FILE = 100
RANDOM_SEED = 42

# Skip peaks below this intensity (electronic noise / chemical noise).
# Same default as the algorithm; exposed here in case a smoke test wants
# to be more permissive on lower-intensity contamination.
INTENSITY_THRESHOLD = 1e4


def _read_ms1_spectra_alphatims(d_path: Path, n_scans: int):
    """Yield (m/z, intensity) tuples per scan from a Bruker .d via alphatims.

    alphatims is the canonical Bruker reader (pip install alphatims).
    Yields one list[(mz, intensity)] per random MS1 scan.
    """
    try:
        from alphatims.bruker import TimsTOF
    except ImportError:
        raise RuntimeError(
            "alphatims is not installed. Run:  pip install alphatims"
        )
    data = TimsTOF(str(d_path), use_hdf_if_available=True)
    # MS1 frames have MsMsType == 0
    ms1_frame_ids = [
        int(fid) for fid, msms in zip(data.frames.Id, data.frames.MsMsType)
        if msms == 0
    ]
    rng = random.Random(RANDOM_SEED)
    if len(ms1_frame_ids) > n_scans:
        ms1_frame_ids = rng.sample(ms1_frame_ids, n_scans)
    for fid in ms1_frame_ids:
        # alphatims indexing: frame_id → DataFrame of (mz, intensity, ...)
        frame_df = data[fid]
        if "mz_values" in frame_df.columns and "intensity_values" in frame_df.columns:
            mzs = frame_df["mz_values"].to_numpy()
            ints = frame_df["intensity_values"].to_numpy()
            yield list(zip(mzs, ints))
        else:
            continue


def _read_ms1_spectra_timsrust(d_path: Path, n_scans: int):
    """Fallback reader via timsrust_pyo3 if alphatims isn't available."""
    try:
        import timsrust_pyo3
    except ImportError:
        raise RuntimeError("timsrust_pyo3 is not installed either")
    raise NotImplementedError(
        "timsrust path not implemented in this smoke test. "
        "Install alphatims for now."
    )


def smoke_test_one(d_path: Path, n_scans: int = N_SCANS_PER_FILE) -> dict:
    t0 = time.time()
    try:
        spectra = list(_read_ms1_spectra_alphatims(d_path, n_scans))
    except Exception as e:
        return {"file": d_path.name, "error": str(e)}
    n_actual = len(spectra)
    result = detect_peg_in_spectra(
        spectra, intensity_threshold=INTENSITY_THRESHOLD,
    )
    elapsed = time.time() - t0
    # Sample a few top matches for the report
    top = sorted(result.matches, key=lambda m: -m.intensity)[:5]
    return {
        "file": d_path.name,
        "scans_sampled": n_actual,
        "score": round(result.peg_score, 1),
        "class": result.peg_class,
        "n_ions": result.n_ions_detected,
        "intensity_pct": round(result.intensity_pct, 2),
        "elapsed_sec": round(elapsed, 1),
        "top_matches": [
            f"PEG{m.ion.n}{m.ion.adduct} m/z={m.observed_mz:.4f} "
            f"int={m.intensity:.0e} Δ={m.ppm_error:+.1f}ppm"
            for m in top
        ],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("paths", nargs="+", help=".d directory or directories")
    ap.add_argument("--n-scans", type=int, default=N_SCANS_PER_FILE,
                    help=f"random MS1 scans per file (default {N_SCANS_PER_FILE})")
    ap.add_argument("--csv", type=Path, help="also write a CSV summary here")
    args = ap.parse_args()

    print(f"PEG reference loaded: {len(PEG_REFERENCE)} ions in 200-1500 m/z")
    print(f"Sampling {args.n_scans} MS1 scans per file at 5 ppm, ≥1e4 intensity\n")

    rows = []
    fmt = "{file:<55} {score:>5} {class:<8} {n_ions:>4} {pct:>6}  {scans:>4}s {elapsed:>5}s"
    print(fmt.format(file="file", score="score", **{"class": "class"},
                     n_ions="n_ion", pct="int%", scans="scans", elapsed="time"))
    print("-" * 100)

    for p in args.paths:
        path = Path(p)
        if not path.is_dir() or path.suffix != ".d":
            print(f"SKIP {p} (not a .d directory)")
            continue
        r = smoke_test_one(path, args.n_scans)
        if "error" in r:
            print(f"ERROR {r['file']}: {r['error']}")
            continue
        rows.append(r)
        print(fmt.format(
            file=r["file"][:55], score=r["score"], **{"class": r["class"]},
            n_ions=r["n_ions"], pct=r["intensity_pct"],
            scans=r["scans_sampled"], elapsed=r["elapsed_sec"],
        ))
        if r["top_matches"]:
            for m in r["top_matches"][:3]:
                print(f"  └─ {m}")

    if args.csv and rows:
        import csv
        with open(args.csv, "w", newline="") as f:
            w = csv.DictWriter(
                f,
                fieldnames=["file", "score", "class", "n_ions",
                            "intensity_pct", "scans_sampled", "elapsed_sec"],
            )
            w.writeheader()
            for r in rows:
                w.writerow({k: r[k] for k in w.fieldnames})
        print(f"\nWrote {len(rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
