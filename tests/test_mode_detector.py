"""Tests for Thermo DIA/DDA scan-filter mode detection.

These tests exercise _parse_thermo_scan_filters directly with synthetic
metadata dicts that mirror the TRFP -f=4 -m=0 JSON structure.

Canonical scan-filter signatures (from Xcalibur Qual Browser):
  DDA: "FTMS + c NSI d Full ms2 798.8967@hcd30.00 [150.0000-1651.3812]"
       ^^^ the 'd' before 'Full' marks data-dependent acquisition
  DIA: "FTMS + c NSI Full ms2 497.7750@hcd30.00 [120.0000-1005.0000]"
       no 'd' flag; fixed isolation window tiling the m/z range

TRFP -f=4 metadata-only JSON does NOT include a per-scan 'Scans' array.
The parser therefore falls through to SampleData method-name and MsData
ratio heuristics for the common case.
"""

from __future__ import annotations

from pathlib import Path

from stan.watcher.detector import AcquisitionMode, _parse_thermo_scan_filters


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _meta_with_scan_filters(filters: list[str]) -> dict:
    """Build a synthetic metadata dict with per-scan ScanFilter entries.

    This simulates TRFP full-spectrum JSON (not the metadata-only -f=4 output).
    Each entry maps to one MS2 scan filter string.
    """
    return {
        "Scans": [
            {"ScanFilter": f, "MsLevel": 2}
            for f in filters
        ]
    }


def _meta_from_msdata(n_ms1: int, n_ms2: int) -> dict:
    """Build a synthetic TRFP metadata-only JSON dict with MsData scan counts."""
    return {
        "MsData": [
            {"name": "Number of MS1 spectra", "value": str(n_ms1)},
            {"name": "Number of MS2 spectra", "value": str(n_ms2)},
        ],
        "SampleData": [],
    }


def _meta_from_method(method_name: str, n_ms1: int = 100, n_ms2: int = 500) -> dict:
    """Build metadata dict with an acquisition method name in SampleData."""
    return {
        "MsData": [
            {"name": "Number of MS1 spectra", "value": str(n_ms1)},
            {"name": "Number of MS2 spectra", "value": str(n_ms2)},
        ],
        "SampleData": [
            {"name": "device acquisition method", "value": method_name},
        ],
    }


_DUMMY_PATH = Path("dummy.raw")


# ---------------------------------------------------------------------------
# Strategy 1: per-scan ScanFilter strings (full-spectrum JSON)
# ---------------------------------------------------------------------------

class TestScanFilterStrategy:
    """Per-scan ScanFilter strings — the most authoritative signal."""

    def test_dda_scan_filters_returns_dda(self) -> None:
        """DDA scan filters with 'd' flag → DDA_ORBITRAP."""
        filters = [
            "FTMS + c NSI d Full ms2 798.8967@hcd30.00 [150.0000-1651.3812]",
            "FTMS + c NSI d Full ms2 612.3421@hcd30.00 [100.0000-1234.0000]",
            "FTMS + c NSI d Full ms2 445.1823@hcd30.00 [100.0000-900.0000]",
        ]
        mode = _parse_thermo_scan_filters(_meta_with_scan_filters(filters), _DUMMY_PATH)
        assert mode == AcquisitionMode.DDA_ORBITRAP

    def test_dia_scan_filters_returns_dia(self) -> None:
        """DIA scan filters without 'd' flag → DIA_ORBITRAP."""
        filters = [
            "FTMS + c NSI Full ms2 497.7750@hcd30.00 [120.0000-1005.0000]",
            "FTMS + c NSI Full ms2 522.7750@hcd30.00 [120.0000-1055.0000]",
            "FTMS + c NSI Full ms2 547.7750@hcd30.00 [120.0000-1105.0000]",
            "FTMS + c NSI Full ms2 572.7750@hcd30.00 [120.0000-1155.0000]",
        ]
        mode = _parse_thermo_scan_filters(_meta_with_scan_filters(filters), _DUMMY_PATH)
        assert mode == AcquisitionMode.DIA_ORBITRAP

    def test_mixed_majority_dda_returns_dda(self) -> None:
        """Mixed filters: majority DDA (>50%) → DDA_ORBITRAP."""
        dda_filters = [
            f"FTMS + c NSI d Full ms2 {600 + i}.00@hcd30.00 [100.0000-1500.0000]"
            for i in range(8)
        ]
        dia_filters = [
            "FTMS + c NSI Full ms2 500.00@hcd30.00 [100.0000-1000.0000]",
            "FTMS + c NSI Full ms2 525.00@hcd30.00 [100.0000-1050.0000]",
        ]
        mode = _parse_thermo_scan_filters(
            _meta_with_scan_filters(dda_filters + dia_filters), _DUMMY_PATH
        )
        assert mode == AcquisitionMode.DDA_ORBITRAP

    def test_unvpep_style_dda_filter(self) -> None:
        """Exact scan filter from the UnvPep file that triggered the bug."""
        filters = [
            "FTMS + c NSI d Full ms2 798.8967@hcd30.00 [150.0000-1651.3812]",
        ]
        mode = _parse_thermo_scan_filters(_meta_with_scan_filters(filters), _DUMMY_PATH)
        assert mode == AcquisitionMode.DDA_ORBITRAP

    def test_sa_d_full_dda_filter(self) -> None:
        """Supplemental activation DDA: 'NSI sa d Full ms2' → DDA_ORBITRAP."""
        filters = [
            "FTMS + p NSI sa d Full ms2 798.8967@hcd30.00 [150.0000-1651.3812]",
        ]
        mode = _parse_thermo_scan_filters(_meta_with_scan_filters(filters), _DUMMY_PATH)
        assert mode == AcquisitionMode.DDA_ORBITRAP


# ---------------------------------------------------------------------------
# Strategy 2: SampleData acquisition method name
# (TRFP metadata-only JSON, no per-scan data)
# ---------------------------------------------------------------------------

class TestMethodNameStrategy:
    """Method-name token matching — fallback when per-scan data is absent."""

    def test_dia_in_method_name_returns_dia(self) -> None:
        """Method name containing 'dia' word-token → DIA_ORBITRAP."""
        meta = _meta_from_method("C:\\methods\\hela_dia_60min.meth")
        mode = _parse_thermo_scan_filters(meta, _DUMMY_PATH)
        assert mode == AcquisitionMode.DIA_ORBITRAP

    def test_dda_in_method_name_returns_dda(self) -> None:
        """Method name containing 'dda' word-token → DDA_ORBITRAP."""
        meta = _meta_from_method("C:\\methods\\hela_dda_60min.meth")
        mode = _parse_thermo_scan_filters(meta, _DUMMY_PATH)
        assert mode == AcquisitionMode.DDA_ORBITRAP

    def test_unvpep_method_name_not_dia_or_dda(self) -> None:
        """'ela_UnivPep_26apr60m.meth' has no dia/dda token → falls through to ratio."""
        # Ratio: 50319 MS2 / 5566 MS1 ≈ 9 → UNKNOWN (ambiguous, <15)
        meta = {
            "MsData": [
                {"name": "Number of MS1 spectra", "value": "5566"},
                {"name": "Number of MS2 spectra", "value": "50319"},
            ],
            "SampleData": [
                {
                    "name": "device acquisition method",
                    "value": "C:\\Xcalibur\\methods\\gabri\\pS_univID\\ela_UnivPep_26apr60m.meth",
                }
            ],
        }
        # Method has neither 'dia' nor 'dda' as word tokens; ratio 9 < 15 → UNKNOWN
        mode = _parse_thermo_scan_filters(meta, _DUMMY_PATH)
        assert mode == AcquisitionMode.UNKNOWN

    def test_dia_substring_in_path_component_not_matched(self) -> None:
        """'media\\experiment' should not trigger dia match (word-boundary required)."""
        # "media" contains "dia" as substring; \bdia\b must NOT match it
        meta = _meta_from_method("C:\\media\\experiment_60m.meth", n_ms1=100, n_ms2=500)
        # Ratio 500/100 = 5 → ambiguous → UNKNOWN (not DIA from "media")
        mode = _parse_thermo_scan_filters(meta, _DUMMY_PATH)
        assert mode == AcquisitionMode.UNKNOWN


# ---------------------------------------------------------------------------
# Strategy 3: MS2/MS1 ratio heuristic
# ---------------------------------------------------------------------------

class TestRatioStrategy:
    """MS2/MS1 count ratio — last resort when method name gives no signal."""

    def test_high_ratio_returns_dia(self) -> None:
        """MS2/MS1 ratio ≥ 15 → DIA_ORBITRAP."""
        meta = _meta_from_msdata(n_ms1=1000, n_ms2=20000)  # ratio = 20
        mode = _parse_thermo_scan_filters(meta, _DUMMY_PATH)
        assert mode == AcquisitionMode.DIA_ORBITRAP

    def test_ambiguous_ratio_returns_unknown(self) -> None:
        """MS2/MS1 ratio < 15 → UNKNOWN (cannot distinguish top-N DDA from few-window DIA)."""
        meta = _meta_from_msdata(n_ms1=1000, n_ms2=9000)  # ratio = 9
        mode = _parse_thermo_scan_filters(meta, _DUMMY_PATH)
        assert mode == AcquisitionMode.UNKNOWN

    def test_empty_metadata_returns_unknown(self) -> None:
        """Empty metadata dict → UNKNOWN (documented legacy fallback).

        Callers (daemon.py, _detect_mode_str) treat UNKNOWN as DIA per the
        comment at daemon.py:1196-1214: "99% of QCs are DIA". This is the
        intended sentinel for callers to apply their own fallback policy.
        """
        mode = _parse_thermo_scan_filters({}, _DUMMY_PATH)
        assert mode == AcquisitionMode.UNKNOWN

    def test_no_ms2_scans_returns_unknown(self) -> None:
        """MS1-only file (0 MS2) → UNKNOWN."""
        meta = _meta_from_msdata(n_ms1=5000, n_ms2=0)
        mode = _parse_thermo_scan_filters(meta, _DUMMY_PATH)
        assert mode == AcquisitionMode.UNKNOWN

    def test_ratio_exactly_15_returns_dia(self) -> None:
        """Boundary: ratio exactly 15 → DIA_ORBITRAP (threshold is ≥ 15)."""
        meta = _meta_from_msdata(n_ms1=1000, n_ms2=15000)
        mode = _parse_thermo_scan_filters(meta, _DUMMY_PATH)
        assert mode == AcquisitionMode.DIA_ORBITRAP
