"""MS1 spectrum readers for PEG detection.

Thin adapters over alphatims (Bruker) and fisher_py (Thermo) that yield
(m/z, intensity) tuples in the shape stan.metrics.peg.detect_peg_in_spectra
expects. Both readers are OPTIONAL dependencies:

    stan[peg]    — adds alphatims for Bruker .d
    stan[thermo] — adds fisher_py for Thermo .raw

A caller that doesn't install the relevant extra gets a clear error and
the pipeline falls back to "no PEG score" (not a crash).

Subsampling: a full timsTOF run has 5k–20k MS1 frames, way more than we
need. The default of 80 random MS1 scans per file hits PEG contamination
with very high confidence if it's there, and keeps per-file runtime to
~30–60 seconds. Fixed random seed for reproducibility.
"""
from __future__ import annotations

import logging
import random
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

N_SCANS_DEFAULT = 80
RANDOM_SEED = 42


class PegReaderUnavailable(RuntimeError):
    """Raised when the vendor's MS1 reader library isn't installed.

    Caller should catch and treat as 'PEG score unavailable' — not a
    pipeline failure. Install the appropriate extra:
      stan[peg]    — alphatims (Bruker)
      stan[thermo] — fisher_py (Thermo)
    """


# ── Bruker (.d via alphatims) ──────────────────────────────────────

def read_ms1_bruker(
    d_path: Path,
    n_scans: int = N_SCANS_DEFAULT,
) -> Iterator[list[tuple[float, float]]]:
    """Yield (m/z, intensity) lists for up to n_scans random MS1 frames.

    Raises PegReaderUnavailable if alphatims isn't installed.
    """
    try:
        from alphatims.bruker import TimsTOF
    except ImportError as e:
        raise PegReaderUnavailable(
            "alphatims not installed — `pip install stan-proteomics[peg]` to enable"
        ) from e

    data = TimsTOF(str(d_path), use_hdf_if_available=True)
    ms1_frame_ids = [
        int(fid) for fid, msms in zip(data.frames.Id, data.frames.MsMsType)
        if msms == 0
    ]
    rng = random.Random(RANDOM_SEED)
    if len(ms1_frame_ids) > n_scans:
        ms1_frame_ids = rng.sample(ms1_frame_ids, n_scans)

    for fid in ms1_frame_ids:
        frame_df = data[fid]
        if "mz_values" not in frame_df.columns:
            continue
        if "intensity_values" not in frame_df.columns:
            continue
        mzs = frame_df["mz_values"].to_numpy()
        ints = frame_df["intensity_values"].to_numpy()
        # Cast intensities to Python int to avoid uint32 overflow when
        # downstream code does sort(key=lambda x: -x.intensity) etc.
        yield [(float(m), int(i)) for m, i in zip(mzs, ints)]


# ── Thermo (.raw via fisher_py) ────────────────────────────────────

def read_ms1_thermo(
    raw_path: Path,
    n_scans: int = N_SCANS_DEFAULT,
) -> Iterator[list[tuple[float, float]]]:
    """Yield (m/z, intensity) lists for up to n_scans random MS1 scans.

    Raises PegReaderUnavailable if fisher_py isn't installed OR if the
    fisher_py SelectInstrument(MS, 1) bug hits this file (TODO #11 — some
    Lumos .raw files fail at RawFile.__init__ time).
    """
    try:
        from fisher_py import RawFile
    except ImportError as e:
        raise PegReaderUnavailable(
            "fisher_py not installed — `pip install stan-proteomics[thermo]` to enable"
        ) from e

    try:
        raw = RawFile(str(raw_path))
    except Exception as e:
        # SelectInstrument failure, .NET not present, etc. — treat as
        # unavailable rather than propagating; PEG is best-effort.
        raise PegReaderUnavailable(
            f"fisher_py could not open {raw_path.name}: {type(e).__name__}: {e}"
        ) from e

    try:
        first = raw.first_spectrum_number
        last = raw.last_spectrum_number
        ms1_scans: list[int] = []
        for n in range(first, last + 1):
            try:
                f = raw.get_scan_filter(n)
                if not f:
                    continue
                fs = str(f).lower()
                # Heuristic: "ms " in filter, no "ms2" / "dd-ms2"
                if "ms " in fs and "ms2" not in fs and "dd-ms" not in fs:
                    ms1_scans.append(n)
            except Exception:
                continue
        rng = random.Random(RANDOM_SEED)
        if len(ms1_scans) > n_scans:
            ms1_scans = rng.sample(ms1_scans, n_scans)
        for n in ms1_scans:
            try:
                stats = raw.get_scan_stats_for_scan_number(n)
                mzs, ints = raw.get_segmented_scan(n)
                yield [(float(m), float(i)) for m, i in zip(mzs, ints)]
            except Exception:
                continue
    finally:
        try:
            raw.close()
        except Exception:
            pass


# ── Dispatch ───────────────────────────────────────────────────────

def read_ms1_any(
    path: Path, vendor: str | None = None, n_scans: int = N_SCANS_DEFAULT,
) -> Iterator[list[tuple[float, float]]]:
    """Dispatch to the right reader based on vendor or file suffix.

    vendor: "bruker" or "thermo". When None, inferred from .d/.raw suffix.
    Raises PegReaderUnavailable when the reader library isn't installed,
    or ValueError on unrecognized vendor/extension.
    """
    if vendor is None:
        if path.is_dir() and path.suffix == ".d":
            vendor = "bruker"
        elif path.is_file() and path.suffix == ".raw":
            vendor = "thermo"
        else:
            raise ValueError(
                f"Cannot infer vendor from path: {path}. Pass vendor='bruker' or 'thermo'."
            )
    if vendor == "bruker":
        yield from read_ms1_bruker(path, n_scans=n_scans)
    elif vendor == "thermo":
        yield from read_ms1_thermo(path, n_scans=n_scans)
    else:
        raise ValueError(f"Unknown vendor: {vendor!r}")
