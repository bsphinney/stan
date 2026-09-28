"""Thermo PEG through the ThermoRawFileParser container (spec §4.7).

Covers the streaming mzML reader (32/64-bit, zlib and uncompressed, MS1+MS2
mixed, param-group refs), the stride sampling shared with the fisher_py
reader, the fallback order in ``peg_io.read_ms1_thermo`` (fisher_py ->
container -> PegReaderUnavailable) and its temp-dir cleanup, the apptainer
command line, and the Hive backfill driver. Nothing here runs apptainer.
"""
from __future__ import annotations

import base64
import importlib.util
import json
import struct
import subprocess
import sys
import types
import zlib
from pathlib import Path

import pytest

from stan.metrics import peg_io, peg_trfp
from stan.metrics.peg import PEG_REFERENCE, detect_peg_in_spectra
from stan.metrics.peg_io import PegReaderUnavailable
from stan.metrics.peg_trfp import (
    MzmlReadError,
    TrfpContainer,
    TrfpConversionError,
    TrfpUnavailable,
)

REPO = Path(__file__).resolve().parents[1]


# ── synthetic mzML builders ────────────────────────────────────────

_TYPE_ACC = {("f", 64): "MS:1000523", ("f", 32): "MS:1000521",
             ("i", 32): "MS:1000519", ("i", 64): "MS:1000522"}
_STRUCT = {("f", 64): "d", ("f", 32): "f", ("i", 32): "i", ("i", 64): "q"}


def _cv(acc: str, value: str = "", name: str = "x") -> str:
    return f'<cvParam cvRef="MS" accession="{acc}" name="{name}" value="{value}"/>'


def _bda(values, array_acc: str, *, bits: int = 64, kind: str = "f",
         compress: bool = True, extra_acc: str | None = None,
         group_ref: str | None = None) -> str:
    raw = struct.pack("<%d%s" % (len(values), _STRUCT[(kind, bits)]), *values)
    if compress:
        raw = zlib.compress(raw)
    b64 = base64.b64encode(raw).decode()
    if group_ref:
        params = f'<referenceableParamGroupRef ref="{group_ref}"/>'
    else:
        params = (_cv(_TYPE_ACC[(kind, bits)])
                  + _cv("MS:1000574" if compress else "MS:1000576"))
    params += _cv(array_acc)
    if extra_acc:
        params += _cv(extra_acc)
    return (f'<binaryDataArray encodedLength="{len(b64)}">{params}'
            f"<binary>{b64}</binary></binaryDataArray>")


def _spectrum(idx: int, level: int | None, mz, inten, **kw) -> str:
    level_cv = _cv("MS:1000511", str(level), "ms level") if level is not None else ""
    # A nested cvParam with the ms-level accession must NOT be read as the
    # spectrum's own level: only direct children count.
    nested = ('<scanList count="1"><scan>'
              + _cv("MS:1000016", "1.5", "scan start time")
              + _cv("MS:1000511", "7") + "</scan></scanList>")
    mz_kw = {k[3:]: v for k, v in kw.items() if k.startswith("mz_")}
    in_kw = {k[3:]: v for k, v in kw.items() if k.startswith("in_")}
    arrays = _bda(mz, "MS:1000514", **mz_kw) + _bda(inten, "MS:1000515", **in_kw)
    return (f'<spectrum index="{idx}" id="scan={idx + 1}" defaultArrayLength="{len(mz)}">'
            f"{level_cv}{nested}"
            f'<binaryDataArrayList count="2">{arrays}</binaryDataArrayList></spectrum>')


def _mzml(spectra: list[str], *, indexed: bool = False, groups: str = "") -> str:
    chrom = ('<chromatogramList count="1"><chromatogram index="0" id="TIC" defaultArrayLength="2">'
             '<binaryDataArrayList count="2">'
             + _bda([0.0, 1.0], "MS:1000595") + _bda([5.0, 6.0], "MS:1000515")
             + "</binaryDataArrayList></chromatogram></chromatogramList>")
    body = ('<mzML xmlns="http://psi.hupo.org/ms/mzml" version="1.1.0">'
            + (f'<referenceableParamGroupList count="1">{groups}</referenceableParamGroupList>'
               if groups else "")
            + f'<run id="r"><spectrumList count="{len(spectra) + 99}">'
            + "".join(spectra) + "</spectrumList>" + chrom + "</run></mzML>")
    if indexed:
        body = ('<indexedmzML xmlns="http://psi.hupo.org/ms/mzml">' + body
                + '<indexList count="0"/><fileChecksum>0</fileChecksum></indexedmzML>')
    return '<?xml version="1.0" encoding="utf-8"?>\n' + body


def _write(tmp_path: Path, text: str, name: str = "run.mzML") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _numbered_ms1_file(tmp_path: Path, n_ms1: int, ms2_every: int = 0) -> Path:
    """MS1 spectrum k carries the single peak (1000 + k, 1e6)."""
    spectra, idx = [], 0
    for k in range(n_ms1):
        spectra.append(_spectrum(idx, 1, [1000.0 + k], [1e6]))
        idx += 1
        if ms2_every and k % ms2_every == 0:
            spectra.append(_spectrum(idx, 2, [5.0, 6.0], [7.0, 8.0]))
            idx += 1
    return _write(tmp_path, _mzml(spectra))


@pytest.fixture(params=["numpy", "pure-python"])
def decoder(request, monkeypatch):
    """Run a test under both binary decoders."""
    if request.param == "pure-python":
        monkeypatch.setattr(peg_trfp, "_np", None)
    elif peg_trfp._np is None:
        pytest.skip("numpy not installed")
    return request.param


# ── mzML parsing ───────────────────────────────────────────────────

def test_decodes_64bit_zlib_exactly(tmp_path, decoder):
    mz, inten = [100.0512, 445.12003, 1500.98765], [1.5e4, 2.25e7, 3.0e9]
    p = _write(tmp_path, _mzml([_spectrum(0, 1, mz, inten)]))
    assert list(peg_trfp.iter_mzml_ms1(p, 80)) == [list(zip(mz, inten))]


def test_decodes_32bit_uncompressed(tmp_path, decoder):
    mz, inten = [200.25, 300.5], [1e4, 2e5]
    p = _write(tmp_path, _mzml([_spectrum(0, 1, mz, inten, mz_bits=32, in_bits=32,
                                          mz_compress=False, in_compress=False)]))
    (scan,) = list(peg_trfp.iter_mzml_ms1(p, 80))
    assert scan == pytest.approx(list(zip(mz, inten)))
    assert all(isinstance(v, float) for pair in scan for v in pair)


@pytest.mark.parametrize("bits,big", [(64, 34_000_000_000), (32, 2_000_000_000)])
def test_integer_intensities(tmp_path, decoder, bits, big):
    p = _write(tmp_path, _mzml([_spectrum(0, 1, [301.0, 302.0], [12, big],
                                          in_kind="i", in_bits=bits, in_compress=False)]))
    assert list(peg_trfp.iter_mzml_ms1(p, 80)) == [[(301.0, 12.0), (302.0, float(big))]]


def test_skips_ms2_and_level_less_spectra(tmp_path, decoder):
    spectra = [
        _spectrum(0, 1, [101.0], [1.0]),
        _spectrum(1, 2, [999.0], [9.0]),
        _spectrum(2, None, [888.0], [8.0]),       # no level -> not MS1
        _spectrum(3, 1, [103.0], [3.0]),
    ]
    p = _write(tmp_path, _mzml(spectra))
    assert peg_trfp.count_mzml_ms1(p) == 2
    assert list(peg_trfp.iter_mzml_ms1(p, 80)) == [[(101.0, 1.0)], [(103.0, 3.0)]]


def test_ms1_spectrum_term_without_ms_level(tmp_path):
    spec = _spectrum(0, None, [150.0], [2.0]).replace(
        "<scanList", _cv("MS:1000579", name="MS1 spectrum") + "<scanList", 1)
    p = _write(tmp_path, _mzml([spec]))
    assert list(peg_trfp.iter_mzml_ms1(p, 80)) == [[(150.0, 2.0)]]


def test_indexed_mzml_wrapper(tmp_path):
    p = _write(tmp_path, _mzml([_spectrum(0, 1, [111.0], [11.0])], indexed=True))
    assert list(peg_trfp.iter_mzml_ms1(p, 80)) == [[(111.0, 11.0)]]


def test_referenceable_param_group(tmp_path, decoder):
    group = ('<referenceableParamGroup id="g64z">' + _cv("MS:1000523") + _cv("MS:1000574")
             + "</referenceableParamGroup>")
    spec = _spectrum(0, 1, [123.0, 456.0], [7.0, 8.0],
                     mz_group_ref="g64z", in_group_ref="g64z")
    p = _write(tmp_path, _mzml([spec], groups=group))
    assert list(peg_trfp.iter_mzml_ms1(p, 80)) == [[(123.0, 7.0), (456.0, 8.0)]]


def test_empty_spectrum_is_yielded_as_empty_scan(tmp_path):
    """An empty MS1 scan keeps its slot, so scan index stays an RT proxy."""
    p = _write(tmp_path, _mzml([_spectrum(0, 1, [], []), _spectrum(1, 1, [5.0], [6.0])]))
    assert list(peg_trfp.iter_mzml_ms1(p, 80)) == [[], [(5.0, 6.0)]]


def test_noise_arrays_are_ignored(tmp_path):
    spec = _spectrum(0, 1, [10.0], [20.0]).replace(
        "</binaryDataArrayList>", _bda([1.0], "MS:1002744") + "</binaryDataArrayList>")
    p = _write(tmp_path, _mzml([spec]))
    assert list(peg_trfp.iter_mzml_ms1(p, 80)) == [[(10.0, 20.0)]]


def test_numpress_is_refused_not_misread(tmp_path):
    p = _write(tmp_path, _mzml([_spectrum(0, 1, [1.0], [2.0], mz_extra_acc="MS:1002312")]))
    with pytest.raises(MzmlReadError, match="Numpress"):
        list(peg_trfp.iter_mzml_ms1(p, 80))


def test_truncated_file_raises_mzml_read_error(tmp_path):
    text = _mzml([_spectrum(i, 1, [100.0 + i], [1.0]) for i in range(5)])
    p = _write(tmp_path, text[: len(text) // 2])
    with pytest.raises(MzmlReadError):
        list(peg_trfp.iter_mzml_ms1(p, 80))


# ── stride sampling ────────────────────────────────────────────────

def test_stride_indices_match_fisher_formula():
    total, n = 1234, 80
    step = total / n
    assert peg_trfp.stride_indices(total, n) == [int(i * step) for i in range(n)]
    assert peg_trfp.stride_indices(50, 80) == list(range(50))
    assert peg_trfp.stride_indices(0, 80) == []
    with pytest.raises(ValueError):
        peg_trfp.stride_indices(10, 0)


def test_iter_samples_the_same_scans_as_fisher_path(tmp_path, decoder):
    """200 MS1 (+MS2 interleaved) -> the 80 picks fisher_py would take."""
    p = _numbered_ms1_file(tmp_path, 200, ms2_every=3)
    got = [scan[0][0] - 1000.0 for scan in peg_trfp.iter_mzml_ms1(p, 80)]
    step = 200 / 80
    assert got == [float(int(i * step)) for i in range(80)]


def test_iter_yields_every_scan_when_few(tmp_path):
    p = _numbered_ms1_file(tmp_path, 7)
    assert [s[0][0] for s in peg_trfp.iter_mzml_ms1(p, 80)] == [1000.0 + k for k in range(7)]


def test_parsed_spectra_feed_detect_peg(tmp_path):
    """A PEG ladder written through mzML is found by detect_peg_in_spectra."""
    ions = [i for i in PEG_REFERENCE if i.adduct == "+NH4" and 6 <= i.n <= 12]
    spectra = []
    for k, ion in enumerate(ions):  # higher n elutes later: a coherent ladder
        spectra.append(_spectrum(k, 1, [500.123, ion.mz], [5e6, 2e6]))
    p = _write(tmp_path, _mzml(spectra))
    res = detect_peg_in_spectra(peg_trfp.iter_mzml_ms1(p, 80))
    assert res.n_ions_detected == len(ions)
    assert res.ladder_coherence == 1.0
    assert res.intensity_pct == pytest.approx(100 * 2e6 * len(ions) / (7e6 * len(ions)))


# ── container discovery + command line ─────────────────────────────

def test_find_container_missing_image(monkeypatch, tmp_path):
    monkeypatch.setenv("STAN_TRFP_SIF", str(tmp_path / "nope.sif"))
    with pytest.raises(TrfpUnavailable, match="image not found"):
        peg_trfp.find_trfp_container()


def test_find_container_no_apptainer(monkeypatch, tmp_path):
    sif = tmp_path / "trfp.sif"
    sif.write_bytes(b"x")
    monkeypatch.setenv("STAN_TRFP_SIF", str(sif))
    monkeypatch.delenv("STAN_APPTAINER", raising=False)
    monkeypatch.setattr(peg_trfp.shutil, "which", lambda name: None)
    with pytest.raises(TrfpUnavailable, match="apptainer"):
        peg_trfp.find_trfp_container()


def test_find_container_env_overrides(monkeypatch, tmp_path):
    sif = tmp_path / "trfp.sif"
    sif.write_bytes(b"x")
    runtime = tmp_path / "my-apptainer"
    runtime.write_text("#!/bin/sh\n")
    runtime.chmod(0o755)
    monkeypatch.setenv("STAN_TRFP_SIF", str(sif))
    monkeypatch.setenv("STAN_APPTAINER", str(runtime))
    c = peg_trfp.find_trfp_container()
    assert c == TrfpContainer(apptainer=str(runtime), sif=sif)


def test_find_container_default_sif_path(monkeypatch):
    monkeypatch.delenv("STAN_TRFP_SIF", raising=False)
    with pytest.raises(TrfpUnavailable) as ei:
        peg_trfp.find_trfp_container()  # the Hive path does not exist here
    assert "/quobyte/proteomics-grp/STAN/historical_bsa/trfp.sif" in str(ei.value)


def test_build_command(tmp_path):
    nfs, quobyte = tmp_path / "nfs", tmp_path / "quobyte"
    nfs.mkdir()
    quobyte.mkdir()
    raw = nfs / "lab" / "Ex01_HeL50.raw"
    out = tmp_path / "scratch"
    c = TrfpContainer(apptainer="/usr/bin/apptainer", sif=Path("/img/trfp.sif"))
    cmd = peg_trfp.build_trfp_command(
        raw, out, c, bind_roots=(str(nfs), str(quobyte), str(tmp_path / "missing")))
    assert cmd[:3] == ["/usr/bin/apptainer", "exec", "--cleanenv"]
    binds = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--bind"]
    # existing roots at the same path, the scratch dir explicitly, no missing root,
    # and the raw's directory is covered by /nfs so it is not bound again
    assert binds == [f"{nfs}:{nfs}", f"{quobyte}:{quobyte}", f"{out}:{out}"]
    tail = cmd[cmd.index("/img/trfp.sif"):]
    assert tail == ["/img/trfp.sif", "ThermoRawFileParser", f"-i={raw}", f"-o={out}/",
                    "-f=1", "-L=1"]
    # comparability with fisher_py: keep peak picking and reference peaks
    assert not any(a.startswith(("-p", "--noPeakPicking", "-x", "-N")) for a in tail)


def test_build_command_binds_raw_dir_outside_roots(tmp_path):
    raw = tmp_path / "elsewhere" / "a.raw"
    out = tmp_path / "scratch"
    c = TrfpContainer(apptainer="apptainer", sif=Path("/img/trfp.sif"))
    cmd = peg_trfp.build_trfp_command(raw, out, c, bind_roots=())
    binds = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--bind"]
    assert binds == [f"{out}:{out}", f"{raw.parent}:{raw.parent}"]


# ── conversion (subprocess faked) ──────────────────────────────────

@pytest.fixture
def fake_container(tmp_path):
    return TrfpContainer(apptainer="apptainer", sif=tmp_path / "trfp.sif")


def _raw(tmp_path: Path, name: str = "Ex01_HeL50.raw") -> Path:
    p = tmp_path / name
    p.write_bytes(b"not really a raw file")
    return p


def test_convert_runs_trfp_and_returns_mzml(monkeypatch, tmp_path, fake_container):
    raw, out = _raw(tmp_path), tmp_path / "out"
    out.mkdir()
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["kw"] = cmd, kw
        (out / "Ex01_HeL50.mzML").write_text(_mzml([_spectrum(0, 1, [1.0], [2.0])]))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(peg_trfp.subprocess, "run", fake_run)
    mzml = peg_trfp.convert_ms1_mzml(raw, out, container=fake_container, timeout_s=77)
    assert mzml == out.resolve() / "Ex01_HeL50.mzML"
    assert seen["kw"]["check"] is True and seen["kw"]["timeout"] == 77
    assert f"-i={raw.resolve()}" in seen["cmd"]


@pytest.mark.parametrize("exc", [
    subprocess.CalledProcessError(1, ["x"], output="", stderr="boom: bad raw"),
    subprocess.TimeoutExpired(["x"], 5),
])
def test_convert_failures_raise_conversion_error(monkeypatch, tmp_path, fake_container, exc):
    def fake_run(cmd, **kw):
        raise exc

    monkeypatch.setattr(peg_trfp.subprocess, "run", fake_run)
    with pytest.raises(TrfpConversionError):
        peg_trfp.convert_ms1_mzml(_raw(tmp_path), tmp_path, container=fake_container)


def test_convert_exit0_without_output_is_an_error(monkeypatch, tmp_path, fake_container):
    monkeypatch.setattr(peg_trfp.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(TrfpConversionError, match="wrote 0 mzML"):
        peg_trfp.convert_ms1_mzml(_raw(tmp_path), out, container=fake_container)


def test_convert_missing_raw(tmp_path, fake_container):
    with pytest.raises(TrfpConversionError, match="not found"):
        peg_trfp.convert_ms1_mzml(tmp_path / "gone.raw", tmp_path, container=fake_container)


# ── fallback order in peg_io.read_ms1_thermo ───────────────────────

class _FakeConverter:
    """Stands in for convert_ms1_mzml: writes a small mzML, records out_dir."""

    def __init__(self, n_ms1: int = 5, fail: bool = False):
        self.n_ms1, self.fail, self.out_dirs = n_ms1, fail, []

    def __call__(self, raw_path, out_dir, container=None, timeout_s=0):
        self.out_dirs.append(Path(out_dir))
        if self.fail:
            raise TrfpConversionError("ThermoRawFileParser exit 1 on x.raw: boom")
        return _numbered_ms1_file(Path(out_dir), self.n_ms1)


@pytest.fixture
def trfp_env(monkeypatch, tmp_path):
    """Container present, converter faked, TMPDIR pointed into tmp_path."""
    tmpdir = tmp_path / "node_tmp"
    tmpdir.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    monkeypatch.setattr(peg_trfp, "find_trfp_container",
                        lambda: TrfpContainer("apptainer", tmp_path / "trfp.sif"))
    conv = _FakeConverter()
    monkeypatch.setattr(peg_trfp, "convert_ms1_mzml", conv)
    return types.SimpleNamespace(tmpdir=tmpdir, conv=conv, raw=_raw(tmp_path))


def _no_fisher(monkeypatch):
    monkeypatch.setitem(sys.modules, "fisher_py", None)  # import -> ImportError


def test_fisher_missing_falls_back_to_trfp_and_cleans_up(monkeypatch, trfp_env):
    _no_fisher(monkeypatch)
    got = list(peg_io.read_ms1_thermo(trfp_env.raw, n_scans=80))
    assert [s[0][0] for s in got] == [1000.0 + k for k in range(5)]
    (out_dir,) = trfp_env.conv.out_dirs
    assert out_dir.parent == trfp_env.tmpdir          # under $TMPDIR
    assert not out_dir.exists()                       # and removed afterwards
    assert list(trfp_env.tmpdir.iterdir()) == []


def test_read_ms1_any_routes_raw_through_fallback(monkeypatch, trfp_env):
    _no_fisher(monkeypatch)
    assert len(list(peg_io.read_ms1_any(trfp_env.raw))) == 5


def test_fisher_open_failure_falls_back(monkeypatch, trfp_env):
    class BadRawFile:
        def __init__(self, path):
            raise RuntimeError("SelectInstrument(MS, 1) failed")

    monkeypatch.setitem(sys.modules, "fisher_py", types.SimpleNamespace(RawFile=BadRawFile))
    assert len(list(peg_io.read_ms1_thermo(trfp_env.raw))) == 5
    assert len(trfp_env.conv.out_dirs) == 1


def test_working_fisher_is_used_and_trfp_is_not(monkeypatch, trfp_env):
    class GoodRawFile:
        closed = False

        def __init__(self, path):
            self._ms1_scan_numbers = list(range(1, 201))

        def get_scan_from_scan_number(self, n):
            return [float(n)], [1e6], [0], "FTMS"

        def close(self):
            GoodRawFile.closed = True

    monkeypatch.setitem(sys.modules, "fisher_py", types.SimpleNamespace(RawFile=GoodRawFile))
    got = [s[0][0] for s in peg_io.read_ms1_thermo(trfp_env.raw, n_scans=80)]
    step = 200 / 80
    assert got == [float(1 + int(i * step)) for i in range(80)]
    assert trfp_env.conv.out_dirs == []
    assert GoodRawFile.closed


def test_conversion_failure_is_unavailable_and_cleans_up(monkeypatch, trfp_env):
    _no_fisher(monkeypatch)
    trfp_env.conv.fail = True
    with pytest.raises(PegReaderUnavailable, match="fallback failed") as ei:
        list(peg_io.read_ms1_thermo(trfp_env.raw))
    assert "fisher_py not installed" in str(ei.value)
    assert list(trfp_env.tmpdir.iterdir()) == []


def test_unreadable_mzml_is_unavailable(monkeypatch, trfp_env):
    _no_fisher(monkeypatch)

    def broken(raw_path, out_dir, container=None, timeout_s=0):
        return _write(Path(out_dir), "<mzML><run><spectrumList>")

    monkeypatch.setattr(peg_trfp, "convert_ms1_mzml", broken)
    with pytest.raises(PegReaderUnavailable, match="fallback failed"):
        list(peg_io.read_ms1_thermo(trfp_env.raw))
    assert list(trfp_env.tmpdir.iterdir()) == []


def test_early_close_still_removes_temp_dir(monkeypatch, trfp_env):
    _no_fisher(monkeypatch)
    gen = peg_io.read_ms1_thermo(trfp_env.raw)
    next(gen)
    (out_dir,) = trfp_env.conv.out_dirs
    assert out_dir.exists()
    gen.close()
    assert not out_dir.exists()


def test_neither_reader_raises_unavailable_with_both_reasons(monkeypatch, tmp_path):
    _no_fisher(monkeypatch)
    monkeypatch.setenv("STAN_TRFP_SIF", str(tmp_path / "absent.sif"))
    with pytest.raises(PegReaderUnavailable) as ei:
        list(peg_io.read_ms1_thermo(_raw(tmp_path)))
    msg = str(ei.value)
    assert "fisher_py not installed" in msg and "image not found" in msg


# ── Hive backfill driver ───────────────────────────────────────────

@pytest.fixture
def pbt(monkeypatch):
    """scripts/peg_backfill_thermo.py as a module, with PG writes recorded."""
    path = REPO / "scripts" / "peg_backfill_thermo.py"
    spec = importlib.util.spec_from_file_location("peg_backfill_thermo", path)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "peg_backfill_thermo", mod)
    spec.loader.exec_module(mod)
    mod.calls = []
    monkeypatch.setattr(mod, "insert_peg_ion_hits",
                        lambda **kw: mod.calls.append(("hits", kw)) or len(kw["matches"]))
    monkeypatch.setattr(mod, "update_peg_result",
                        lambda **kw: mod.calls.append(("scalars", kw)) or True)
    return mod


def _ladder_spectra():
    ions = [i for i in PEG_REFERENCE if i.adduct == "+H" and 5 <= i.n <= 9]
    return [[(400.0, 5e6), (ion.mz, 1e6)] for ion in ions]


def test_query_shards_in_sql_and_placeholders_match(pbt):
    sql, params = pbt.build_candidates_query(shard=3, nshards=8, limit=5)
    assert "peg_score IS NULL" in sql and "hidden = 0" in sql
    assert "SELECT id::text, raw_path, run_name, instrument FROM runs" in sql
    assert "md5(id::text)" in sql and sql.rstrip().endswith("LIMIT %s")
    assert "ORDER BY id" in sql
    assert sql.count("%s") == len(params)
    assert params == ("%orbitrap%", "%.raw", 8, 3, 5)
    sql1, params1 = pbt.build_candidates_query(shard=0, nshards=1, limit=0,
                                               instrument="lumos", run_ids=("a", "b"))
    assert "md5" not in sql1 and "LIMIT" not in sql1
    assert sql1.count("%s") == len(params1)
    assert params1 == ("%orbitrap%", "%.raw", "%lumos%", ["a", "b"])


def _cand(pbt, raw: Path, run_id: str = "r1"):
    return pbt.Candidate(run_id=run_id, raw_path=str(raw), run_name=raw.name,
                         instrument="Orbitrap Exploris 480")


def test_process_run_writes_hits_before_scalars(pbt, monkeypatch, tmp_path):
    seen = {}

    def fake_read(path, vendor=None, n_scans=0):
        seen.update(vendor=vendor, n_scans=n_scans)
        return iter(_ladder_spectra())

    monkeypatch.setattr(pbt, "read_ms1_any", fake_read)
    rec = pbt.process_run(_cand(pbt, _raw(tmp_path)), n_scans=80, dry_run=False)
    assert rec["event"] == "done" and rec["peg_n_ions_detected"] == 5
    assert seen == {"vendor": "thermo", "n_scans": 80}
    assert [c[0] for c in pbt.calls] == ["hits", "scalars"]
    scalars = pbt.calls[1][1]
    assert scalars["table"] == "runs" and scalars["run_id"] == "r1"
    assert scalars["peg_class"] == rec["peg_class"]
    assert scalars["peg_intensity_pct"] == pytest.approx(rec["peg_intensity_pct"])


def test_process_run_dry_run_writes_nothing(pbt, monkeypatch, tmp_path):
    monkeypatch.setattr(pbt, "read_ms1_any", lambda *a, **k: iter(_ladder_spectra()))
    rec = pbt.process_run(_cand(pbt, _raw(tmp_path)), n_scans=80, dry_run=True)
    assert rec["event"] == "dry_run" and pbt.calls == []


def test_process_run_unavailable_leaves_null(pbt, monkeypatch, tmp_path):
    def unavailable(*a, **k):
        raise PegReaderUnavailable("no reader")

    monkeypatch.setattr(pbt, "read_ms1_any", unavailable)
    rec = pbt.process_run(_cand(pbt, _raw(tmp_path)), n_scans=80, dry_run=False)
    assert rec["event"] == "unavailable" and pbt.calls == []


def test_process_run_never_stamps_unknown_on_error(pbt, monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(pbt, "read_ms1_any", boom)
    rec = pbt.process_run(_cand(pbt, _raw(tmp_path)), n_scans=80, dry_run=False)
    assert rec["event"] == "error" and pbt.calls == []


def test_process_run_no_signal_is_not_clean(pbt, monkeypatch, tmp_path):
    monkeypatch.setattr(pbt, "read_ms1_any", lambda *a, **k: iter([[], []]))
    rec = pbt.process_run(_cand(pbt, _raw(tmp_path)), n_scans=80, dry_run=False)
    assert rec["event"] == "error" and pbt.calls == []


def test_process_run_missing_raw_is_skipped(pbt, tmp_path):
    rec = pbt.process_run(_cand(pbt, tmp_path / "gone.raw"), n_scans=80, dry_run=False)
    assert rec["event"] == "skip" and pbt.calls == []


def test_process_run_reports_unmatched_row(pbt, monkeypatch, tmp_path):
    monkeypatch.setattr(pbt, "read_ms1_any", lambda *a, **k: iter(_ladder_spectra()))
    monkeypatch.setattr(pbt, "update_peg_result", lambda **kw: False)
    rec = pbt.process_run(_cand(pbt, _raw(tmp_path)), n_scans=80, dry_run=False)
    assert rec["event"] == "error" and rec["stage"] == "write"


def _read_jsonl(log_dir: Path) -> list[dict]:
    (path,) = list(log_dir.glob("peg_backfill_thermo_*_s0of1.jsonl"))
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_main_end_to_end_jsonl(pbt, monkeypatch, tmp_path):
    monkeypatch.setenv("STAN_DB_BACKEND", "pg")
    monkeypatch.setattr(pbt, "reader_status", lambda: {
        "fisher_py": False, "trfp_sif": "/x/trfp.sif", "apptainer": "a", "trfp_error": None})
    good, bad = _raw(tmp_path, "a.raw"), _raw(tmp_path, "b.raw")
    queries = []

    def fake_fetch(sql, params):
        queries.append((sql, params))
        return [_cand(pbt, good, "r1"), _cand(pbt, bad, "r2"),
                _cand(pbt, tmp_path / "gone.raw", "r3")]

    def fake_read(path, vendor=None, n_scans=0):
        if path.name == "b.raw":
            raise PegReaderUnavailable("TRFP exit 1")
        return iter(_ladder_spectra())

    monkeypatch.setattr(pbt, "fetch_candidates", fake_fetch)
    monkeypatch.setattr(pbt, "read_ms1_any", fake_read)
    log_dir = tmp_path / "logs"
    rc = pbt.main(["--log-dir", str(log_dir), "--limit", "3"])
    assert rc == 1  # one unavailable run makes the task visibly unhappy
    events = [r["event"] for r in _read_jsonl(log_dir)]
    assert events == ["start", "done", "unavailable", "skip", "end"]
    end = _read_jsonl(log_dir)[-1]
    assert (end["done"], end["unavailable"], end["skip"]) == (1, 1, 1)
    assert queries[0][1][-1] == 3
    assert [c[1]["run_id"] for c in pbt.calls] == ["r1", "r1"]


def test_main_refuses_without_pg_backend(pbt, monkeypatch, tmp_path):
    monkeypatch.setenv("STAN_DB_BACKEND", "sqlite")
    monkeypatch.setattr(pbt, "fetch_candidates", lambda *a: pytest.fail("queried PG"))
    assert pbt.main(["--log-dir", str(tmp_path)]) == 2
    assert _read_jsonl(tmp_path)[0]["event"] == "refused"


def test_main_refuses_without_any_reader(pbt, monkeypatch, tmp_path):
    monkeypatch.setenv("STAN_DB_BACKEND", "pg")
    monkeypatch.setattr(pbt, "reader_status", lambda: {
        "fisher_py": False, "trfp_sif": None, "apptainer": None, "trfp_error": "no image"})
    monkeypatch.setattr(pbt, "fetch_candidates", lambda *a: pytest.fail("queried PG"))
    assert pbt.main(["--log-dir", str(tmp_path)]) == 2


def test_main_rejects_out_of_range_shard(pbt, tmp_path):
    with pytest.raises(SystemExit):
        pbt.main(["--shard", "4", "--nshards", "4", "--log-dir", str(tmp_path)])
