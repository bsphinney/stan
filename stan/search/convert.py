"""ThermoRawFileParser conversion utilities.

Only needed for Thermo DDA → Sage (Track A). DIA-NN reads .raw natively.
"""

from __future__ import annotations

from pathlib import Path


def get_mzml_path(raw_path: Path, output_dir: Path) -> Path:
    """Return the expected mzML output path for a given .raw file."""
    return output_dir / (raw_path.stem + ".mzML")


def build_thermo_conversion_script(
    raw_path: Path,
    output_dir: Path,
    trfp_dll_path: Path,
) -> str:
    """Build bash commands to convert .raw to indexed mzML.

    Args:
        raw_path: Path to the Thermo .raw file on Hive.
        output_dir: Directory for mzML output on Hive.
        trfp_dll_path: Path to ThermoRawFileParser.dll on Hive.

    Returns:
        Shell command string to embed in SLURM script.
    """
    mzml_path = get_mzml_path(raw_path, output_dir)
    return (
        f"echo 'Converting {raw_path.name} to indexed mzML...'\n"
        f"dotnet {trfp_dll_path} "
        f"-i={raw_path} "
        f"-o={output_dir}/ "
        f"-f=2 "
        f"-m=0\n"
        f"echo 'Converted: {raw_path.name} → {mzml_path.name}'\n"
    )


def build_cleanup_script(raw_path: Path, output_dir: Path, keep_mzml: bool = False) -> str:
    """Build bash commands to clean up converted mzML files after search.

    Args:
        raw_path: Original .raw file path (used to derive mzML name).
        output_dir: Directory containing the mzML file.
        keep_mzml: If True, skip cleanup.

    Returns:
        Shell command string (empty if keep_mzml is True).
    """
    if keep_mzml:
        return ""
    mzml_path = get_mzml_path(raw_path, output_dir)
    return (
        f"echo 'Cleaning up converted mzML...'\n"
        f"rm -f {mzml_path}\n"
    )
