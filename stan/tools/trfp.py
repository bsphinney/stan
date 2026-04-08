"""ThermoRawFileParser management — auto-download, cache, and run.

TRFP extracts metadata and converts Thermo .raw files. STAN uses it for:
  - Acquisition date extraction (mode detection, timestamping)
  - DIA/DDA mode auto-detection from scan filter strings
  - mzML conversion for Sage DDA searches (Thermo only)

On first use, STAN downloads the correct TRFP build for the OS to
~/.stan/tools/ThermoRawFileParser/ and caches it. Subsequent calls
use the cached binary.

License: ThermoRawFileParser is Apache 2.0 (CompOmics / Ghent University).
"""

from __future__ import annotations

import io
import json
import logging
import platform
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# GitHub release for the version we pin to
TRFP_VERSION = "v.2.0.0-dev"
TRFP_BASE_URL = (
    f"https://github.com/CompOmics/ThermoRawFileParser/releases/download/{TRFP_VERSION}"
)

# OS → zip filename + how to run
_VARIANTS = {
    "Windows": {
        "zip": f"ThermoRawFileParser-{TRFP_VERSION}-win.zip",
        "exe": "ThermoRawFileParser.exe",
        "needs_dotnet": False,
    },
    "Linux": {
        "zip": f"ThermoRawFileParser-{TRFP_VERSION}-net8.zip",
        "exe": "ThermoRawFileParser.dll",
        "needs_dotnet": True,
    },
}


def _tools_dir() -> Path:
    """~/.stan/tools/ThermoRawFileParser/"""
    from stan.config import get_user_config_dir

    d = get_user_config_dir() / "tools" / "ThermoRawFileParser"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _variant() -> dict:
    system = platform.system()
    if system not in _VARIANTS:
        raise RuntimeError(
            f"ThermoRawFileParser is not available on {system}. "
            "Thermo .raw files can only be processed on Windows or Linux."
        )
    return _VARIANTS[system]


def is_installed() -> bool:
    """Check if TRFP is already cached locally."""
    v = _variant()
    return (_tools_dir() / v["exe"]).exists()


def ensure_installed() -> Path:
    """Download TRFP if not already cached. Returns path to the executable/DLL."""
    v = _variant()
    exe_path = _tools_dir() / v["exe"]

    if exe_path.exists():
        return exe_path

    url = f"{TRFP_BASE_URL}/{v['zip']}"
    logger.info("Downloading ThermoRawFileParser from %s", url)

    import urllib.request

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "STAN"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
    except Exception as e:
        raise RuntimeError(
            f"Failed to download ThermoRawFileParser: {e}\n"
            f"URL: {url}\n"
            "Check your internet connection, or download manually and place in:\n"
            f"  {_tools_dir()}"
        ) from e

    # Extract zip
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(_tools_dir())

    if not exe_path.exists():
        raise RuntimeError(
            f"Downloaded and extracted TRFP but {v['exe']} not found in {_tools_dir()}"
        )

    # Make executable on Linux
    if platform.system() != "Windows":
        exe_path.chmod(0o755)

    logger.info("ThermoRawFileParser installed at %s", exe_path)
    return exe_path


def _find_dotnet() -> str:
    """Find the dotnet binary (Linux only)."""
    # Check PATH first
    dotnet = shutil.which("dotnet")
    if dotnet:
        return dotnet

    # Common HPC module locations
    common = [
        "/usr/bin/dotnet",
        "/usr/local/bin/dotnet",
        "/opt/dotnet/dotnet",
    ]
    for p in common:
        if Path(p).exists():
            return p

    raise RuntimeError(
        "dotnet not found. On HPC, try: module load dotnet-core-sdk\n"
        "On Ubuntu/Debian: sudo apt install dotnet-sdk-8.0"
    )


def _build_command(exe_path: Path) -> list[str]:
    """Build the base command (dotnet DLL on Linux, exe on Windows)."""
    v = _variant()
    if v["needs_dotnet"]:
        dotnet = _find_dotnet()
        return [dotnet, str(exe_path)]
    else:
        return [str(exe_path)]


def run_trfp(
    raw_path: Path,
    output_dir: Path | None = None,
    metadata_only: bool = False,
    convert_mzml: bool = False,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    """Run ThermoRawFileParser on a .raw file.

    Args:
        raw_path: Path to the .raw file.
        output_dir: Directory for output files. Defaults to a temp dir.
        metadata_only: If True, extract metadata JSON only (no spectra, fast).
        convert_mzml: If True, convert to indexed mzML.
        timeout: Subprocess timeout in seconds.

    Returns:
        CompletedProcess with stdout/stderr.
    """
    exe = ensure_installed()
    cmd = _build_command(exe)

    if output_dir is None:
        import tempfile

        output_dir = Path(tempfile.mkdtemp(prefix="stan_trfp_"))
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd += [f"-i={raw_path}"]

    if metadata_only:
        cmd += ["-m=0", "-f=4", f"-o={output_dir}/"]
    elif convert_mzml:
        cmd += ["-f=2", "-m=0", f"-o={output_dir}/"]
    else:
        cmd += [f"-o={output_dir}/"]

    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def extract_metadata(raw_path: Path) -> dict:
    """Extract metadata from a .raw file. Returns a dict with parsed fields.

    Keys:
        creation_date: ISO 8601 datetime string
        instrument_model: str
        instrument_serial: str
        software_version: str
    """
    import tempfile

    out_dir = Path(tempfile.mkdtemp(prefix="stan_meta_"))
    result = run_trfp(raw_path, output_dir=out_dir, metadata_only=True)

    meta_file = out_dir / f"{raw_path.stem}-metadata.json"
    if not meta_file.exists():
        logger.warning("TRFP metadata not produced for %s: %s", raw_path, result.stderr)
        return {}

    try:
        raw_meta = json.loads(meta_file.read_text())
    except json.JSONDecodeError:
        return {}
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)

    parsed: dict = {}

    # Extract from FileProperties
    for prop in raw_meta.get("FileProperties", []):
        name = prop.get("name", "")
        value = prop.get("value", "")
        if "Creation Date" in name and value:
            try:
                dt = datetime.strptime(value, "%m/%d/%Y %H:%M:%S")
                parsed["creation_date"] = dt.isoformat(timespec="seconds")
            except ValueError:
                parsed["creation_date"] = value

    # Extract from InstrumentProperties
    for prop in raw_meta.get("InstrumentProperties", []):
        name = prop.get("name", "")
        value = prop.get("value", "")
        if "instrument model" in name.lower():
            parsed["instrument_model"] = value
        elif "serial number" in name.lower():
            parsed["instrument_serial"] = value
        elif "software version" in name.lower():
            parsed["software_version"] = value

    # Extract MS scan counts and compute DIA window size
    ms1_count = ms2_count = 0
    ms_min_mz = ms_max_mz = 0.0
    ms_data = raw_meta.get("MsData", [])
    if isinstance(ms_data, list):
        for item in ms_data:
            name = item.get("name", "")
            value = item.get("value", "")
            if "Number of MS1" in name:
                ms1_count = int(value) if value else 0
            elif "Number of MS2" in name:
                ms2_count = int(value) if value else 0
            elif "MS min MZ" in name:
                ms_min_mz = float(value) if value else 0
            elif "MS max MZ" in name:
                ms_max_mz = float(value) if value else 0

    parsed["n_ms1_scans"] = ms1_count
    parsed["n_ms2_scans"] = ms2_count

    # Compute DIA window size from scan counts + m/z range
    # DIA: each cycle has 1 MS1 + N MS2 windows. N = ms2/ms1.
    # Window width ≈ (ms2_max_mz - ms2_min_mz) / N
    if ms1_count > 0 and ms2_count > 0 and ms_max_mz > ms_min_mz:
        windows_per_cycle = ms2_count / ms1_count
        mz_range = ms_max_mz - ms_min_mz
        if windows_per_cycle > 1:
            # This is the center-to-center SPACING, not the isolation width.
            # For non-overlapping windows (Lumos w22) these are ~equal.
            # For overlapping windows (Astral 3Th) the spacing is larger
            # than the isolation width. Use dia_isolation_width_th for the
            # real width; this is just a derived stat.
            computed_spacing = round(mz_range / windows_per_cycle, 1)
            parsed["dia_windows_per_cycle"] = round(windows_per_cycle)
            parsed["dia_window_spacing_da"] = computed_spacing
            parsed["dia_mz_range"] = f"{ms_min_mz:.0f}-{ms_max_mz:.0f}"

    # Parse the REAL isolation width from the method name and/or filename.
    # This is more reliable than computing from scan counts because:
    #  - Astral uses overlapping windows (3 Th isolation, 9 Da spacing)
    #  - The method name encodes the actual quadrupole isolation width
    #  - The filename often does too (e.g. _3Th, _4Th, _w22)
    import re

    for prop in raw_meta.get("SampleData", []):
        name = prop.get("name", "")
        value = prop.get("value", "")
        if "method" in name.lower() and value:
            parsed["acquisition_method"] = value
            # Look for NTh (isolation width in Th/Da — Astral convention)
            m = re.search(r"[_\-](\d+)[Tt]h", value)
            if m:
                parsed["dia_isolation_width_th"] = int(m.group(1))
            # Look for wNN (window width in Da — Lumos/Exploris convention)
            m = re.search(r"[wW](\d+)", value)
            if m:
                parsed["dia_isolation_width_th"] = parsed.get("dia_isolation_width_th") or int(m.group(1))
            # Look for DIA/DDA
            if "dia" in value.lower():
                parsed["acquisition_mode"] = "dia"
            elif "dda" in value.lower():
                parsed["acquisition_mode"] = "dda"

    # Also check the raw filename for window info (backup if method name is absent)
    if "dia_isolation_width_th" not in parsed:
        raw_name = str(raw_path.name)
        m = re.search(r"[_\-](\d+)[Tt]h", raw_name)
        if m:
            parsed["dia_isolation_width_th"] = int(m.group(1))
        else:
            m = re.search(r"[wW](\d+)", raw_name)
            if m:
                parsed["dia_isolation_width_th"] = int(m.group(1))

    # Expected runtime (gradient length in minutes)
    for prop in raw_meta.get("ScanSettings", []):
        name = prop.get("name", "")
        value = prop.get("value", "")
        if "expected runtime" in name.lower() and value:
            try:
                parsed["gradient_length_min"] = int(float(value))
            except ValueError:
                pass

    # Extract LC system from embedded binary strings in the .raw file.
    # Thermo .raw files embed the full Chromeleon/Xcalibur instrument method
    # as XML inside the binary. The DriverId values identify the LC components.
    # This is faster than TRFP and works without .NET — just scan the binary.
    lc_info = _extract_lc_from_raw_binary(raw_path)
    parsed.update(lc_info)

    return parsed


# Known LC driver ID → human-readable name mappings
_LC_DRIVERS = {
    "Dionex.PumpNCS3500RS": "Dionex NCS-3500RS (nano/cap pump)",
    "Dionex.PumpHPG3400RS": "Dionex HPG-3400RS (high-pressure gradient)",
    "Dionex.PumpLPG3400RS": "Dionex LPG-3400RS (low-pressure gradient)",
    "Dionex.PumpDGP3600RS": "Dionex DGP-3600RS (dual gradient)",
    "Dionex.ChromatographySystem": "Dionex UltiMate 3000",
    "Thermo.Vanquish.Neo": "Thermo Vanquish Neo",
    "Thermo.Vanquish.Horizon": "Thermo Vanquish Horizon",
    "Thermo.Vanquish.Flex": "Thermo Vanquish Flex",
    "Thermo.EasyNLC": "Thermo Easy-nLC",
    "Thermo.EasyNLC1200": "Thermo Easy-nLC 1200",
    "WPS-3000": "Dionex WPS-3000 (Well Plate Sampler)",
    "Dionex.WPS3000": "Dionex WPS-3000 (Well Plate Sampler)",
    "Dionex.VWD3400RS": "Dionex VWD-3400RS (UV detector)",
    "Dionex.DAD3000RS": "Dionex DAD-3000RS (diode array)",
    "Evosep": "Evosep One",
}


def _extract_lc_from_raw_binary(raw_path: Path) -> dict:
    """Scan the .raw binary for embedded LC device identifiers.

    The Thermo .raw format embeds the full instrument method XML which
    contains DriverId values for every connected LC component. This is
    much faster than parsing through TRFP and works without .NET.

    Returns dict with:
        lc_system: str — the main LC system name (e.g. "Dionex UltiMate 3000")
        lc_pump: str — the pump module
        lc_autosampler: str — the autosampler
        lc_drivers: list[str] — all unique DriverId values found
    """
    result: dict = {}
    try:
        import shutil as _sh
        import subprocess
        # 'strings' is a Unix utility; not available on Windows
        if not _sh.which("strings"):
            return result
        proc = subprocess.run(
            ["strings", str(raw_path)],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return result

        # Extract all DriverId values
        import re
        drivers = set(re.findall(r'DriverId value="([^"]+)"', proc.stdout))
        # Also catch DriverId="..." variant
        drivers.update(re.findall(r'DriverId="([^"]+)"', proc.stdout))

        if not drivers:
            return result

        result["lc_drivers"] = sorted(drivers)

        # Classify components
        for d in drivers:
            dl = d.lower()
            # LC system
            if "chromatographysystem" in dl and "dionex" in dl:
                result["lc_system"] = "Dionex UltiMate 3000"
            elif "vanquish" in dl:
                result["lc_system"] = _LC_DRIVERS.get(d, d)
            elif "easynlc" in dl:
                result["lc_system"] = _LC_DRIVERS.get(d, d)
            elif "evosep" in dl:
                result["lc_system"] = "Evosep One"

            # Pump
            if "pump" in dl or "ncs" in dl or "hpg" in dl or "lpg" in dl or "dgp" in dl:
                result["lc_pump"] = _LC_DRIVERS.get(d, d)

            # Autosampler
            if "wps" in dl or "sampler" in dl:
                result["lc_autosampler"] = _LC_DRIVERS.get(d, d)

            # UV detector
            if "vwd" in dl or "dad" in dl:
                result["lc_detector"] = _LC_DRIVERS.get(d, d)

    except Exception:
        logger.debug("Failed to extract LC info from %s", raw_path, exc_info=True)

    return result


def get_acquisition_date(raw_path: Path) -> str | None:
    """Get just the acquisition datetime from a .raw file.

    Returns ISO 8601 string or None.
    """
    meta = extract_metadata(raw_path)
    return meta.get("creation_date")
