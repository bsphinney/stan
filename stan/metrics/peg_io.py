"""MS1 spectrum readers for PEG detection.

Thin adapters over alphatims (Bruker) and fisher_py (Thermo) that yield
(m/z, intensity) tuples in the shape stan.metrics.peg.detect_peg_in_spectra
expects. Both readers are OPTIONAL dependencies:

    stan[peg]    — adds alphatims for Bruker .d
    stan[thermo] — adds fisher_py for Thermo .raw

Thermo has a second route: when fisher_py is missing or cannot open the
file, the .raw is converted to an MS1-only mzML by a ThermoRawFileParser
apptainer image (stan.metrics.peg_trfp). That is how Hive, whose venv has
no fisher_py, scores Orbitrap QC runs.

A caller with no working reader gets PegReaderUnavailable with the reason,
and the pipeline falls back to "no PEG score" (not a crash).

Subsampling: a full timsTOF run has 5k–20k MS1 frames, way more than we
need. The default of 80 MS1 scans per file, taken at an even stride in
acquisition order (every k-th scan, not a random draw — the ladder-coherence
check in detect_peg_in_spectra uses scan index as its RT proxy), hits PEG
contamination with very high confidence if it's there, and keeps per-file
runtime to ~30–60 seconds. The stride is deterministic, so a re-run
samples the same scans.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

N_SCANS_DEFAULT = 80


class PegReaderUnavailable(RuntimeError):
    """Raised when no MS1 reader can read the file.

    Caller should catch and treat as 'PEG score unavailable' — not a
    pipeline failure. Install the appropriate extra:
      stan[peg]    — alphatims (Bruker)
      stan[thermo] — fisher_py (Thermo), or provide the ThermoRawFileParser
                     image + apptainer (stan.metrics.peg_trfp)
    """


# ── Bruker (.d via alphatims) ──────────────────────────────────────

def read_ms1_bruker(
    d_path: Path,
    n_scans: int = N_SCANS_DEFAULT,
) -> Iterator[list[tuple[float, float]]]:
    """Yield (m/z, intensity) lists for up to n_scans MS1 frames.

    Frames are evenly strided in acquisition order (see module docstring).
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
    # v0.2.168: RT-stratified downsampling (every kth frame in acquisition
    # order) instead of random.sample. detect_peg_in_spectra uses scan
    # index as an RT proxy for the ladder-coherence check, which only
    # works when scans are yielded in RT order. Sampling every-kth
    # preserves order AND gives uniform coverage across the gradient.
    if len(ms1_frame_ids) > n_scans:
        ms1_frame_ids.sort()  # ensure acquisition order
        step = len(ms1_frame_ids) / n_scans
        ms1_frame_ids = [
            ms1_frame_ids[int(i * step)] for i in range(n_scans)
        ]
    else:
        ms1_frame_ids.sort()

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


# ── Thermo (.raw via fisher_py, else a ThermoRawFileParser container) ──

def read_ms1_thermo(
    raw_path: Path,
    n_scans: int = N_SCANS_DEFAULT,
) -> Iterator[list[tuple[float, float]]]:
    """Yield (m/z, intensity) lists for up to n_scans MS1 scans.

    Scans are evenly strided in acquisition order. Two readers, in order:

      1. fisher_py, in-process (stan[thermo], needs .NET). Behaviour is
         unchanged whenever it imports and opens the file.
      2. ThermoRawFileParser in an apptainer image (stan.metrics.peg_trfp),
         when fisher_py is not installed OR cannot open the file (the
         SelectInstrument(MS, 1) bug, TODO #11, hits some Lumos .raw files
         at RawFile.__init__ time). The .raw becomes an MS1-only mzML in a
         temporary directory under $TMPDIR that is removed afterwards, and
         the same stride is sampled from it. Both readers return the FTMS
         centroid stream, so the scores are comparable.

    Raises PegReaderUnavailable when neither reader works, with both
    reasons in the message.
    """
    try:
        raw = _open_fisher_raw(raw_path)
    except PegReaderUnavailable as e:
        fisher_reason = str(e)
    else:
        yield from _iter_fisher_ms1(raw, n_scans)
        return
    # Outside the except block, so the fallback's own errors are not
    # reported as "during handling of" the fisher_py one.
    yield from _read_ms1_thermo_trfp(raw_path, n_scans, fisher_reason)


def _open_fisher_raw(raw_path: Path) -> Any:
    """Open ``raw_path`` with fisher_py, or raise PegReaderUnavailable."""
    try:
        from fisher_py import RawFile
    except ImportError as e:
        raise PegReaderUnavailable(
            "fisher_py not installed — `pip install stan-proteomics[thermo]` to enable"
        ) from e

    try:
        return RawFile(str(raw_path))
    except Exception as e:
        # SelectInstrument failure, .NET not present, etc. — treat as
        # unavailable rather than propagating; PEG is best-effort.
        raise PegReaderUnavailable(
            f"fisher_py could not open {raw_path.name}: {type(e).__name__}: {e}"
        ) from e


def _iter_fisher_ms1(raw: Any, n_scans: int) -> Iterator[list[tuple[float, float]]]:
    """Stride-sample MS1 scans from an open fisher_py RawFile, then close it."""
    try:
        # v0.2.175: fisher_py RawFile pre-computes MS1 scan numbers at
        # __init__ via _get_ms_scan_numbers_and_retention_times_. Use
        # them directly instead of iterating every scan and checking
        # the filter string — the previous code called
        # `raw.first_spectrum_number` / `raw.get_scan_filter(n)` which
        # don't exist on current fisher_py (those look like they came
        # from a different/older wrapper). Every Lumos+480 backfill
        # hit AttributeError under the old code; v0.2.174 diagnosed.
        # v0.2.189: `_ms1_scan_numbers` is a numpy array; `arr or []`
        # raises "truth value of an array is ambiguous". Use an explicit
        # None check + len() so empty arrays fall back cleanly.
        _raw_scans = getattr(raw, "_ms1_scan_numbers", None)
        ms1_scans: list[int] = (
            [int(s) for s in _raw_scans] if _raw_scans is not None and len(_raw_scans) > 0
            else []
        )
        # RT-stratified sampling: ms1_scans is already in acquisition order.
        if len(ms1_scans) > n_scans:
            step = len(ms1_scans) / n_scans
            ms1_scans = [ms1_scans[int(i * step)] for i in range(n_scans)]
        for n in ms1_scans:
            try:
                # RawFile.get_scan_from_scan_number returns
                # (positions/mz, intensities, charges, filter_str).
                mzs, ints, _charges, _fs = raw.get_scan_from_scan_number(int(n))
                yield [(float(m), float(i)) for m, i in zip(mzs, ints)]
            except Exception:
                continue
    finally:
        try:
            raw.close()
        except Exception:
            pass


def _read_ms1_thermo_trfp(
    raw_path: Path, n_scans: int, fisher_reason: str,
) -> Iterator[list[tuple[float, float]]]:
    """The container fallback of read_ms1_thermo.

    Every failure — no image, no apptainer, a failed conversion, an
    unreadable mzML, an mzML with no MS1 peaks, a missing scratch directory —
    becomes PegReaderUnavailable, so the pipeline leaves PEG NULL
    (unmeasured) rather than stamping the 'unknown' sentinel its generic
    error path writes, or scoring an empty read as a clean 0.0.
    """
    from stan.metrics import peg_trfp

    try:
        container = peg_trfp.find_trfp_container()
    except peg_trfp.TrfpUnavailable as e:
        raise PegReaderUnavailable(
            f"{fisher_reason}; ThermoRawFileParser fallback unavailable: {e}"
        ) from e
    logger.info(
        "PEG: %s — reading %s through ThermoRawFileParser (%s)",
        fisher_reason, raw_path.name, container.sif,
    )
    # $TMPDIR is node-local on Hive compute nodes (/tmp). The MS1 mzML is
    # a few hundred MB; it must never outlive this call, even when the
    # consumer stops early or conversion fails. A failed delete (EBUSY from
    # files a timed-out mono still holds) is ignored: by then the spectra
    # are read, and a leftover in node-local /tmp is not worth losing them.
    try:
        scratch = tempfile.TemporaryDirectory(
            prefix="stan_peg_trfp_", dir=os.environ.get("TMPDIR") or None,
            ignore_cleanup_errors=True,
        )
    except OSError as e:
        raise PegReaderUnavailable(
            f"{fisher_reason}; ThermoRawFileParser fallback has no scratch directory: {e}"
        ) from e
    with scratch as tmp:
        try:
            mzml = peg_trfp.convert_ms1_mzml(raw_path, Path(tmp), container=container)
            n_spectra = n_peaks = 0
            for spectrum in peg_trfp.iter_mzml_ms1(mzml, n_scans):
                n_spectra += 1
                n_peaks += len(spectrum)
                yield spectrum
            if not n_peaks:
                # An MS2-only method, an aborted acquisition, or a TRFP that
                # changed what -L=1 writes. Scored, this is a clean 0.0.
                raise peg_trfp.TrfpConversionError(
                    f"{mzml.name} has no MS1 peaks ({n_spectra} MS1 spectra sampled)"
                )
        except (peg_trfp.TrfpError, OSError) as e:
            raise PegReaderUnavailable(
                f"{fisher_reason}; ThermoRawFileParser fallback failed: {e}"
            ) from e


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
