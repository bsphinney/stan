"""Thermo MS1 spectra for PEG detection through a ThermoRawFileParser container.

Why this exists: ``stan.metrics.peg_io.read_ms1_thermo`` reads ``.raw``
in-process through ``fisher_py``, which needs .NET and is not installed in
the Hive venv. Every Orbitrap QC run on Hive therefore raised
``PegReaderUnavailable`` and kept a NULL PEG: 0 of 2,924 Lumos + Exploris
``runs`` rows had a score when this was written (PG, 2026-09-28). Hive
already reads ``.raw`` through the ThermoRawFileParser apptainer image, so
this module converts the ``.raw`` to an MS1-only mzML in a scratch
directory and streams the spectra back out of it. ``peg_io`` uses it as the
fallback when fisher_py is missing or cannot open the file.

Container, verified in SLURM job 24179193 on 2026-09-28 (never assume the
flags — they differ between TRFP releases):

  * image: ``/quobyte/proteomics-grp/STAN/historical_bsa/trfp.sif``, built
    from ``quay.io/biocontainers/thermorawfileparser:1.4.5--ha8f3691_0``;
    ``ThermoRawFileParser --version`` prints ``1.4.5`` (the mono build).
    ``ThermoRawFileParser`` and ``ThermoRawFileParser.sh`` are both on the
    image's PATH and both run ``mono ThermoRawFileParser.exe``.
  * apptainer 1.5.4 is ``/usr/bin/apptainer`` on the compute nodes, before
    and after ``module load apptainer``.
  * flags used, from that version's ``--help``: ``-i=`` input raw,
    ``-o=`` output directory, ``-f=1`` plain (non-indexed) mzML,
    ``-L=1`` "Select MS levels ... included in the output" — so only MS1
    spectra are written. Peak picking is left at its default (on), and
    ``-x`` (exclude reference/exception peaks) is deliberately NOT passed.

Why those defaults: with peak picking on, TRFP 1.4.5's mzML writer takes an
FTMS scan's *centroid stream* (``MzMlSpectrumWriter.cs``,
``scan.HasCentroidStream`` -> ``scan.CentroidScan.Masses/Intensities``),
which is also what fisher_py 2.0.2's ``get_scan_from_scan_number`` returns
for an FTMS scan (``_get_scan_`` -> ``get_centroid_stream(n, False)``), and
Orbitrap MS1 is always FTMS. Both select MS1 by the scan event's MS order.
So both paths hand ``detect_peg_in_spectra`` the same peaks on the same
intensity scale, and a run scored by either reader lands in the same cohort.
``-x`` would drop lock-mass/exception peaks that fisher_py keeps.

TRFP writes m/z and intensity as zlib-compressed 64-bit floats; the parser
below also reads 32-bit floats, 32/64-bit integers and uncompressed arrays,
so an mzML from another converter works too. Numpress is refused with a
clear error rather than decoded wrongly. No new dependency: numpy is used
when importable, ``array`` otherwise.

Sampling matches ``peg_io.read_ms1_bruker``/``read_ms1_thermo`` exactly: MS1
spectra are counted in document (= acquisition) order and the same
``int(i * step)`` picks are taken, so ``detect_peg_in_spectra``'s
ladder-coherence check, which uses scan index as its RT proxy, sees the
same ordering it does on the fisher_py path. The count is a real first pass
over the file: ``spectrumList/@count`` in TRFP 1.4.5 is the raw file's
total scan count (``GetTotalScanNumber()``), not the number of spectra that
survived ``-L``.

Measured end to end in SLURM job 24179541 (2026-09-28, 2 CPUs) on an
Exploris 480 60-min DIA HeLa QC (1.1 GB .raw): conversion 34 s to a 52 MB
MS1-only mzML holding 3,107 MS1 spectra; count + sample passes under 1 s;
peak RSS 1.3 GB. It scored n_ions=27, intensity_pct=0.158, peg_score=35.0,
class "trace" (ladder coherence 0.79 over 24 pairs), and the pure-Python
decoder returned exactly what numpy did.

Conversion runs only inside SLURM jobs — never on a Hive login node.
"""
from __future__ import annotations

import array
import base64
import binascii
import logging
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

try:  # numpy is in every STAN install today, but the parser must not need it
    import numpy as _np
except ImportError:  # pragma: no cover - exercised by monkeypatching _np
    _np = None

logger = logging.getLogger(__name__)

#: Default image. Override with STAN_TRFP_SIF.
DEFAULT_TRFP_SIF = Path("/quobyte/proteomics-grp/STAN/historical_bsa/trfp.sif")
#: Apptainer does not mount /nfs (the Flinders raw archive) or /quobyte by
#: default. Only roots that exist on this host are bound: binding a missing
#: source path makes ``apptainer exec`` fail outright.
DEFAULT_BIND_ROOTS: tuple[str, ...] = ("/nfs", "/quobyte")
#: Per-file ceiling for the conversion. Generous on purpose: the job is
#: preemptible and a timeout just leaves PEG NULL for the next pass.
TRFP_TIMEOUT_S = 1800

# PSI-MS controlled-vocabulary accessions used below.
_MS_LEVEL = "MS:1000511"
_MS1_SPECTRUM = "MS:1000579"
_MSN_SPECTRUM = "MS:1000580"
_MZ_ARRAY = "MS:1000514"
_INTENSITY_ARRAY = "MS:1000515"
_ZLIB = "MS:1000574"
_NO_COMPRESSION = "MS:1000576"
_NUMPRESS = frozenset({
    "MS:1002312", "MS:1002313", "MS:1002314",   # linear / pic / slof
    "MS:1002746", "MS:1002747", "MS:1002748",   # the same, followed by zlib
})
# accession -> (numpy dtype, array typecode candidates, item size in bytes)
_DTYPES: dict[str, tuple[str, str, int]] = {
    "MS:1000521": ("<f4", "f", 4),    # 32-bit float
    "MS:1000523": ("<f8", "d", 8),    # 64-bit float
    "MS:1000519": ("<i4", "il", 4),   # 32-bit integer
    "MS:1000522": ("<i8", "q", 8),    # 64-bit integer
}


class TrfpError(RuntimeError):
    """Base class: the container path could not produce MS1 spectra."""


class TrfpUnavailable(TrfpError):
    """The ThermoRawFileParser image or an apptainer executable is missing."""


class TrfpConversionError(TrfpError):
    """ThermoRawFileParser ran (or was about to) and produced no usable mzML."""


class MzmlReadError(TrfpError):
    """The mzML exists but could not be parsed or decoded."""


@dataclass(frozen=True)
class TrfpContainer:
    """A runnable ThermoRawFileParser image."""

    apptainer: str
    sif: Path


# ── Container discovery and conversion ─────────────────────────────

def find_trfp_container() -> TrfpContainer:
    """Locate the ThermoRawFileParser image and an apptainer to run it.

    The image is ``$STAN_TRFP_SIF`` or :data:`DEFAULT_TRFP_SIF`. The runtime
    is ``$STAN_APPTAINER`` when set, else ``apptainer`` (or ``singularity``)
    on PATH. On Hive both answers are found without ``module load``.

    Returns:
        The container to run.

    Raises:
        TrfpUnavailable: the image or the runtime is missing, with the reason.
    """
    sif = Path(os.environ.get("STAN_TRFP_SIF") or DEFAULT_TRFP_SIF)
    if not sif.is_file():
        raise TrfpUnavailable(
            f"ThermoRawFileParser image not found at {sif} (set STAN_TRFP_SIF)"
        )
    runtime = os.environ.get("STAN_APPTAINER", "").strip()
    if runtime:
        resolved = shutil.which(runtime) or (runtime if Path(runtime).is_file() else None)
        if not resolved:
            raise TrfpUnavailable(f"STAN_APPTAINER={runtime!r} is not executable")
    else:
        resolved = shutil.which("apptainer") or shutil.which("singularity")
        if not resolved:
            raise TrfpUnavailable(
                "no apptainer/singularity on PATH (on Hive: `module load "
                "apptainer`, or set STAN_APPTAINER)"
            )
    return TrfpContainer(apptainer=resolved, sif=sif)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def build_trfp_command(
    raw_path: Path,
    out_dir: Path,
    container: TrfpContainer,
    bind_roots: tuple[str, ...] = DEFAULT_BIND_ROOTS,
) -> list[str]:
    """Build the argv that converts ``raw_path`` to an MS1-only mzML.

    Args:
        raw_path: the ``.raw`` file (resolved; symlinks are followed).
        out_dir: directory TRFP writes ``<stem>.mzML`` into.
        container: from :func:`find_trfp_container`.
        bind_roots: host trees to bind at the same path when they exist.

    Returns:
        The argument list for ``subprocess.run`` (no shell).
    """
    raw = Path(raw_path)
    out = Path(out_dir)
    binds: list[Path] = [Path(r) for r in bind_roots if Path(r).is_dir()]
    # The scratch dir is normally node-local $TMPDIR (/tmp on Hive), which
    # apptainer mounts by default -- but "by default" is site config, so bind
    # it explicitly. A raw outside every bound root gets its directory bound.
    for extra in (out, raw.parent):
        if not any(_is_under(extra, b) for b in binds):
            binds.append(extra)
    cmd = [container.apptainer, "exec",
           # Keep the host's module environment (LD_LIBRARY_PATH in
           # particular) out of the image's mono runtime.
           "--cleanenv"]
    for b in binds:
        cmd += ["--bind", f"{b}:{b}"]
    cmd += [
        str(container.sif),
        "ThermoRawFileParser",
        f"-i={raw}",
        f"-o={out}/",
        "-f=1",   # mzML, not indexed: the parser streams, it never seeks
        "-L=1",   # MS1 only; MS2 is most of a DIA file and PEG never reads it
    ]
    return cmd


def convert_ms1_mzml(
    raw_path: Path,
    out_dir: Path,
    container: TrfpContainer | None = None,
    timeout_s: int = TRFP_TIMEOUT_S,
) -> Path:
    """Convert a Thermo ``.raw`` into an MS1-only mzML inside ``out_dir``.

    Args:
        raw_path: the ``.raw`` file.
        out_dir: an existing, writable scratch directory (the caller owns
            its lifetime).
        container: image to use; found with :func:`find_trfp_container`
            when None.
        timeout_s: wall-clock limit for ThermoRawFileParser.

    Returns:
        Path of the written mzML.

    Raises:
        TrfpUnavailable: no image or runtime.
        TrfpConversionError: the raw is missing, TRFP failed or timed out,
            or it exited 0 without writing a non-empty mzML.
    """
    raw = Path(raw_path)
    if not raw.exists():
        raise TrfpConversionError(f"raw file not found: {raw}")
    raw = raw.resolve()
    out = Path(out_dir).resolve()
    if container is None:
        container = find_trfp_container()
    cmd = build_trfp_command(raw, out, container)
    logger.debug("TRFP: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, check=True, timeout=timeout_s, capture_output=True, text=True,
        )
    except FileNotFoundError as e:
        raise TrfpUnavailable(f"cannot execute {container.apptainer}: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise TrfpConversionError(
            f"ThermoRawFileParser timed out after {timeout_s}s on {raw.name}"
        ) from e
    except subprocess.CalledProcessError as e:
        tail = ((e.stderr or "") + (e.stdout or "")).strip()[-600:]
        raise TrfpConversionError(
            f"ThermoRawFileParser exit {e.returncode} on {raw.name}: {tail}"
        ) from e

    mzml = out / f"{raw.stem}.mzML"
    if not mzml.is_file():
        found = sorted(out.glob("*.mzML"))
        if len(found) != 1:
            raise TrfpConversionError(
                f"ThermoRawFileParser exited 0 but wrote {len(found)} mzML "
                f"files for {raw.name}: {(proc.stdout or '').strip()[-300:]}"
            )
        mzml = found[0]
    if mzml.stat().st_size == 0:
        raise TrfpConversionError(f"ThermoRawFileParser wrote an empty {mzml.name}")
    return mzml


# ── Streaming mzML reader ──────────────────────────────────────────

def _local(tag: str) -> str:
    """Strip the ``{namespace}`` prefix ElementTree puts on every tag."""
    return tag.rsplit("}", 1)[-1]


def _cv_params(elem: ET.Element, groups: dict[str, dict[str, str]]) -> dict[str, str]:
    """accession -> value for ``elem``'s own cvParams and param-group refs.

    Direct children only: a spectrum's scan and precursor sub-elements carry
    cvParams of their own that must not be mistaken for the spectrum's.
    """
    out: dict[str, str] = {}
    for child in elem:
        tag = _local(child.tag)
        if tag == "cvParam":
            out[child.get("accession", "")] = child.get("value", "")
        elif tag == "referenceableParamGroupRef":
            out.update(groups.get(child.get("ref", ""), {}))
    return out


def _ms_level(params: dict[str, str]) -> int | None:
    if _MS_LEVEL in params:
        try:
            return int(params[_MS_LEVEL])
        except ValueError:
            return None
    if _MS1_SPECTRUM in params:
        return 1
    if _MSN_SPECTRUM in params:
        return 2
    return None


def _iter_spectrum_elements(
    path: Path,
) -> Iterator[tuple[int | None, ET.Element, dict[str, dict[str, str]]]]:
    """Yield ``(ms_level, <spectrum>, param_groups)`` in document order.

    Each spectrum is cleared once the consumer moves on, so memory holds one
    spectrum's text at a time rather than the file. The file is opened here
    (not by iterparse) so an early ``break`` in the consumer closes it.
    """
    groups: dict[str, dict[str, str]] = {}
    with open(path, "rb") as fh:
        for _event, elem in ET.iterparse(fh, events=("end",)):
            tag = _local(elem.tag)
            if tag == "spectrum":
                yield _ms_level(_cv_params(elem, groups)), elem, groups
                elem.clear()
            elif tag == "referenceableParamGroup":
                groups[elem.get("id", "")] = _cv_params(elem, {})
                elem.clear()
            elif tag == "chromatogram":
                elem.clear()


def _decode_array(text: str, params: dict[str, str]) -> list:
    """Decode one ``<binary>`` payload into a list of numbers."""
    text = (text or "").strip()
    if not text:
        return []
    numpress = _NUMPRESS.intersection(params)
    if numpress:
        raise ValueError(f"MS-Numpress arrays ({sorted(numpress)[0]}) are not supported")
    dtype = next((_DTYPES[a] for a in params if a in _DTYPES), None)
    if dtype is None:
        raise ValueError("binaryDataArray has no recognised data type cvParam")
    np_code, typecodes, itemsize = dtype
    data = base64.b64decode(text)
    if _ZLIB in params:
        data = zlib.decompress(data)
    if len(data) % itemsize:
        raise ValueError(f"{len(data)} bytes is not a whole number of {itemsize}-byte values")
    if _np is not None:
        return _np.frombuffer(data, dtype=_np.dtype(np_code)).tolist()
    code = next(c for c in typecodes if array.array(c).itemsize == itemsize)
    arr = array.array(code)
    arr.frombytes(data)
    if sys.byteorder == "big":  # mzML binary is little-endian
        arr.byteswap()
    return arr.tolist()


def _decode_spectrum(
    elem: ET.Element, groups: dict[str, dict[str, str]],
) -> list[tuple[float, float]]:
    """(m/z, intensity) pairs of one ``<spectrum>`` element."""
    mz: list | None = None
    inten: list | None = None
    for bda in elem.iter():
        if _local(bda.tag) != "binaryDataArray":
            continue
        params = _cv_params(bda, groups)
        if _MZ_ARRAY not in params and _INTENSITY_ARRAY not in params:
            continue  # e.g. TRFP's optional noise arrays (-N)
        binary = next((c for c in bda if _local(c.tag) == "binary"), None)
        values = _decode_array(binary.text if binary is not None else "", params)
        if _MZ_ARRAY in params:
            mz = values
        else:
            inten = values
    if not mz or not inten:
        return []
    if len(mz) != len(inten):
        logger.debug("spectrum %s: %d m/z vs %d intensities; truncating",
                     elem.get("id"), len(mz), len(inten))
    return [(float(m), float(i)) for m, i in zip(mz, inten)]


def stride_indices(total: int, n_scans: int) -> list[int]:
    """Positions ``peg_io`` samples out of ``total`` MS1 scans.

    Every scan when there are at most ``n_scans``; otherwise ``int(i * step)``
    for ``step = total / n_scans`` -- the formula ``read_ms1_bruker`` and the
    fisher_py path use, kept identical so the readers are comparable.
    """
    if n_scans < 1:
        raise ValueError(f"n_scans must be >= 1, got {n_scans}")
    if total <= n_scans:
        return list(range(total))
    step = total / n_scans
    return [int(i * step) for i in range(n_scans)]


def count_mzml_ms1(path: Path) -> int:
    """Number of MS1 spectra in an mzML (a full streaming pass).

    Raises:
        MzmlReadError: the file is not parseable mzML.
    """
    n = 0
    try:
        for level, _elem, _groups in _iter_spectrum_elements(Path(path)):
            if level == 1:
                n += 1
    except (ET.ParseError, OSError) as e:
        raise MzmlReadError(f"cannot read {Path(path).name}: {e}") from e
    return n


def iter_mzml_ms1(path: Path, n_scans: int) -> Iterator[list[tuple[float, float]]]:
    """Yield (m/z, intensity) lists for up to ``n_scans`` MS1 spectra.

    Spectra are evenly strided through the file's MS1 spectra in document
    order (see :func:`stride_indices`); MS2+ spectra are skipped, so a mixed
    mzML works as well as the MS1-only one :func:`convert_ms1_mzml` writes.

    Raises:
        MzmlReadError: unparseable XML or an undecodable binary array.
    """
    path = Path(path)
    picks = stride_indices(count_mzml_ms1(path), n_scans)
    if not picks:
        return
    wanted = set(picks)
    last = picks[-1]
    idx = -1
    try:
        for level, elem, groups in _iter_spectrum_elements(path):
            if level != 1:
                continue
            idx += 1
            if idx in wanted:
                yield _decode_spectrum(elem, groups)
            if idx >= last:
                break  # the rest of the file holds nothing we will read
    except (ET.ParseError, OSError, ValueError, zlib.error, binascii.Error) as e:
        raise MzmlReadError(
            f"cannot decode {path.name} at MS1 spectrum {idx}: {type(e).__name__}: {e}"
        ) from e
