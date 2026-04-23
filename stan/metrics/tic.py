"""TIC (Total Ion Current) extraction from mass spec data.

Three sources:
  - Bruker timsTOF .d: reads SummedIntensities from analysis.tdf SQLite
  - Thermo .raw: reads TIC chromatogram via fisher_py (optional) or TRFP mzML
  - DIA-NN report.parquet: "identified TIC" from Ms1.Apex.Area binned by RT
    (works for ALL vendors, no extra dependencies — ported from Vadim's QC script)

The identified TIC is often more useful for QC than raw TIC because it only
shows signal from identified precursors, filtering out noise and contaminants.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class TICTrace:
    """Raw TIC trace data from a single run."""

    rt_min: list[float]          # retention time in minutes
    intensity: list[float]       # summed intensity per MS1 frame
    run_name: str = ""


@dataclass
class TICMetrics:
    """Computed TIC shape metrics for QC evaluation."""

    total_auc: float = 0.0             # trapezoid AUC of smoothed TIC
    peak_rt_min: float = 0.0           # RT of max intensity
    peak_tic: float = 0.0              # max intensity value
    gradient_width_min: float = 0.0    # RT range where TIC > 10% of peak
    baseline_ratio: float = 0.0        # median(lowest 10%) / peak
    late_signal_ratio: float = 0.0     # signal in last 20% of gradient / total
    asymmetry: float = 0.0             # left/right width ratio at 50% height
    n_frames: int = 0                  # number of MS1 frames


def extract_tic_bruker(d_path: Path) -> TICTrace | None:
    """Extract MS1 TIC trace from a Bruker .d directory.

    Reads the Frames table from analysis.tdf, filtering to MS1 frames
    (MsMsType = 0). Falls back to ScanMode = 0 for older TDF schemas.

    Args:
        d_path: Path to the .d directory.

    Returns:
        TICTrace with RT (minutes) and intensity arrays, or None on failure.
    """
    tdf = d_path / "analysis.tdf"
    if not tdf.exists():
        logger.warning("analysis.tdf not found in %s", d_path)
        return None

    try:
        with sqlite3.connect(str(tdf)) as con:
            # Check which intensity column exists
            cols = [r[1] for r in con.execute("PRAGMA table_info(Frames)").fetchall()]

            if "SummedIntensities" in cols:
                tic_col = "SummedIntensities"
            elif "AccumulatedIntensity" in cols:
                tic_col = "AccumulatedIntensity"
            elif "MaxIntensity" in cols:
                tic_col = "MaxIntensity"
            else:
                # Fallback: find any intensity-like column
                tic_col = next((c for c in cols if "ntensit" in c.lower()), None)
                if not tic_col:
                    logger.warning("No intensity column found in Frames table")
                    return None

            # Filter to MS1 frames
            if "MsMsType" in cols:
                ms1_filter = "WHERE MsMsType = 0"
            elif "ScanMode" in cols:
                ms1_filter = "WHERE ScanMode = 0"
            else:
                ms1_filter = ""
                logger.info("No MsMsType/ScanMode column — using all frames")

            rows = con.execute(
                f"SELECT Time, {tic_col} FROM Frames {ms1_filter} ORDER BY Time"
            ).fetchall()

    except sqlite3.Error:
        logger.exception("Failed to read TIC from %s", tdf)
        return None

    if not rows:
        logger.warning("No MS1 frames found in %s", d_path.name)
        return None

    rt_min = [r[0] / 60.0 for r in rows]  # Bruker Time is in seconds
    intensity = [float(r[1]) if r[1] is not None else 0.0 for r in rows]

    return TICTrace(
        rt_min=rt_min,
        intensity=intensity,
        run_name=d_path.stem,
    )


def downsample_trace(trace: TICTrace, n_bins: int = 128) -> TICTrace:
    """Bin a TIC trace to a fixed number of RT bins.

    Bruker ``extract_tic_bruker`` returns one point per MS1 frame, which
    can be 5–20k points for a 1 h run — too much for a community
    submission payload and too noisy for cross-lab overlay plots. This
    bins the trace into ``n_bins`` equal-width RT bins and stores the
    MEAN intensity per bin.

    v0.2.147: switched from sum-per-bin to mean-per-bin. The sum
    approach produced a ~10% sawtooth artifact on Bruker timsTOF TICs:
    with ~1,250 MS1 frames and 128 bins, most bins got 10 frames and
    some got 9, so summing imposed a ~11% variation on the output that
    tracked the bin-count quantization rather than any real signal.
    Mean-per-bin is what the chart's "intensity at this RT" label
    actually means, and it matches the smooth shape Bruker Compass
    shows for the same data. Any downstream "total ion current" math
    can recover the sum by multiplying by the constant frame rate
    (frames per RT bin = n_total_frames / n_bins).

    The downsampled trace has the same shape as the output of
    ``extract_tic_from_report``, so both sources produce comparable
    community traces.
    """
    if not trace.rt_min or not trace.intensity:
        return trace
    if len(trace.rt_min) <= n_bins:
        return trace  # already small enough

    rt_lo = float(min(trace.rt_min))
    rt_hi = float(max(trace.rt_min))
    span = rt_hi - rt_lo
    if span <= 0:
        return trace

    bin_width = span / n_bins
    bin_centers = [rt_lo + (i + 0.5) * bin_width for i in range(n_bins)]
    bin_sum = [0.0] * n_bins
    bin_count = [0] * n_bins

    for rt, inten in zip(trace.rt_min, trace.intensity):
        idx = int((rt - rt_lo) / bin_width)
        if idx >= n_bins:
            idx = n_bins - 1
        if idx < 0:
            idx = 0
        bin_sum[idx] += float(inten)
        bin_count[idx] += 1

    # Mean per bin — removes the n-frames-per-bin quantization artifact.
    # Empty bins (no frames landed here, e.g. at edges) keep 0.0.
    bin_intensity = [
        bin_sum[i] / bin_count[i] if bin_count[i] else 0.0
        for i in range(n_bins)
    ]

    return TICTrace(
        rt_min=bin_centers,
        intensity=bin_intensity,
        run_name=trace.run_name,
    )


def extract_tic_thermo(raw_path: Path) -> TICTrace | None:
    """Extract MS1 TIC trace from a Thermo .raw file using fisher_py.

    Requires fisher_py (pip install fisher_py) which wraps Thermo's
    RawFileReader .NET library. Works on Windows; Linux needs .NET SDK.

    Args:
        raw_path: Path to the .raw file.

    Returns:
        TICTrace with RT (minutes) and intensity arrays, or None on failure.
    """
    try:
        from fisher_py import RawFile
    except ImportError:
        logger.debug(
            "fisher_py not installed — trying TRFP JSON metadata fallback "
            "for Thermo TIC. Install fisher_py for full-resolution extraction."
        )
        return _extract_tic_thermo_trfp(raw_path)

    try:
        raw = RawFile(str(raw_path))

        # v0.2.175: use fisher_py's pre-computed MS1 scan list and
        # retention times. The previous code called
        # `raw.first_spectrum_number` / `get_scan_filter` /
        # `retention_time_from_scan_number` / `get_scan_stats_for_scan_number`
        # — none of those exist on the public RawFile API, so every
        # Thermo TIC extraction was hitting AttributeError and silently
        # falling back to TRFP (if available) or returning None.
        # v0.2.189: `_ms1_scan_numbers` / `_ms1_retention_times` are
        # numpy arrays; `arr or []` raises "truth value of an array
        # is ambiguous". Use None + len() checks instead.
        _s = getattr(raw, "_ms1_scan_numbers", None)
        _r = getattr(raw, "_ms1_retention_times", None)
        ms1_scans = [int(x) for x in _s] if _s is not None and len(_s) > 0 else []
        ms1_rts   = [float(x) for x in _r] if _r is not None and len(_r) > 0 else []

        rt_min = []
        intensity = []

        # Prefer the fast stored-TIC path via the internal .NET wrapper
        # when available; fall back to summing intensities from the
        # full spectrum if the internal accessor isn't exposed.
        raw_access = getattr(raw, "_raw_file_access", None)
        for scan_num, rt in zip(ms1_scans, ms1_rts):
            try:
                tic_val = None
                if raw_access is not None and hasattr(raw_access, "get_scan_stats_for_scan_number"):
                    stats = raw_access.get_scan_stats_for_scan_number(int(scan_num))
                    if stats is not None and hasattr(stats, "tic"):
                        tic_val = float(stats.tic)
                if tic_val is None:
                    _mzs, ints, _c, _fs = raw.get_scan_from_scan_number(int(scan_num))
                    tic_val = float(sum(float(i) for i in ints))
                rt_min.append(float(rt))
                intensity.append(tic_val)
            except Exception:
                continue

        try:
            raw.close()
        except Exception:
            pass

        if not rt_min:
            logger.warning("No MS1 scans found in %s", raw_path.name)
            return None

        return TICTrace(
            rt_min=rt_min,
            intensity=intensity,
            run_name=raw_path.stem,
        )

    except Exception:
        logger.debug("Failed to extract TIC from %s", raw_path.name, exc_info=True)
        # Fallback: try TRFP-based extraction
        return _extract_tic_thermo_trfp(raw_path)


def _extract_tic_thermo_trfp(raw_path: Path) -> TICTrace | None:
    """Fallback TIC extraction using ThermoRawFileParser mzML output.

    Slower than fisher_py but doesn't require pythonnet.
    Converts to mzML, parses TIC chromatogram, then cleans up.
    """
    try:
        from stan.tools.trfp import ensure_installed, _build_command
        import tempfile
        import subprocess
        import xml.etree.ElementTree as ET

        exe = ensure_installed()
        cmd = _build_command(exe)

        out_dir = Path(tempfile.mkdtemp(prefix="stan_tic_"))
        cmd += [f"-i={raw_path}", f"-o={out_dir}/", "-f=1"]  # mzML output

        # Don't use check=True — we want to log stderr on failure instead of
        # swallowing it inside CalledProcessError's string representation.
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            logger.debug(
                "ThermoRawFileParser exited %s for %s\n"
                "  cmd: %s\n"
                "  stdout: %s\n"
                "  stderr: %s",
                proc.returncode, raw_path.name, " ".join(str(c) for c in cmd),
                (proc.stdout or "").strip()[:2000],
                (proc.stderr or "").strip()[:2000],
            )
            return None

        mzml_path = out_dir / f"{raw_path.stem}.mzML"
        if not mzml_path.exists():
            return None

        # Parse TIC from mzML chromatogramList
        ns = {"ms": "http://psi.hupo.org/ms/mzml"}
        tree = ET.parse(str(mzml_path))
        root = tree.getroot()

        rt_min = []
        intensity = []

        for chrom in root.iter(f"{{{ns['ms']}}}chromatogram"):
            chrom_id = chrom.get("id", "")
            if "TIC" in chrom_id:
                # Find binary data arrays
                for binary_list in chrom.iter(f"{{{ns['ms']}}}binaryDataArrayList"):
                    arrays = list(binary_list.iter(f"{{{ns['ms']}}}binaryDataArray"))
                    if len(arrays) >= 2:
                        # First array is RT, second is intensity
                        # These are base64 encoded — for simplicity, skip mzML parsing
                        # and just count scans from spectrum elements instead
                        pass
                break

        # Simpler approach: read scan-level TIC from spectra
        for spectrum in root.iter(f"{{{ns['ms']}}}spectrum"):
            ms_level = None
            scan_tic = None
            scan_rt = None

            for cv in spectrum.iter(f"{{{ns['ms']}}}cvParam"):
                accession = cv.get("accession", "")
                if accession == "MS:1000511":  # ms level
                    ms_level = int(cv.get("value", "0"))
                elif accession == "MS:1000285":  # total ion current
                    scan_tic = float(cv.get("value", "0"))
                elif accession == "MS:1000016":  # scan start time
                    scan_rt = float(cv.get("value", "0"))
                    unit = cv.get("unitName", "")
                    if "second" in unit.lower():
                        scan_rt = scan_rt / 60.0

            if ms_level == 1 and scan_tic and scan_rt:
                rt_min.append(scan_rt)
                intensity.append(scan_tic)

        # Cleanup
        import shutil
        shutil.rmtree(out_dir, ignore_errors=True)

        if not rt_min:
            return None

        return TICTrace(
            rt_min=rt_min,
            intensity=intensity,
            run_name=raw_path.stem,
        )

    except Exception:
        logger.debug("TRFP TIC fallback failed for %s", raw_path.name, exc_info=True)
        return None


def extract_tic_from_report(report_path: Path, n_bins: int = 128) -> TICTrace | None:
    """Extract "identified TIC" from DIA-NN report.parquet.

    Bins retention time and sums Ms1.Apex.Area per bin to create a TIC-like
    chromatogram from identified precursors only. Works for any vendor since
    it reads the search output, not the raw file.

    Ported from Vadim Demichev's DIA-NN QC script.

    Args:
        report_path: Path to DIA-NN report.parquet.
        n_bins: Number of RT bins (default 128).

    Returns:
        TICTrace with RT (minutes) and summed Ms1.Apex.Area, or None.
    """
    try:
        import polars as pl

        # Check what columns are available
        schema = pl.read_parquet_schema(report_path)
        available = set(schema.keys()) if hasattr(schema, 'keys') else set(schema)

        if "RT" not in available:
            logger.debug("No RT column in report — cannot extract identified TIC")
            return None

        # Pick the best signal column
        signal_col = None
        for candidate in ["Ms1.Apex.Area", "Ms1.Area", "Precursor.Quantity", "Precursor.Normalised"]:
            if candidate in available:
                signal_col = candidate
                break

        if signal_col is None:
            logger.debug("No signal column found in report for identified TIC")
            return None

        # Read only what we need
        cols = ["RT", signal_col, "Q.Value"]
        df = pl.read_parquet(report_path, columns=cols)

        # Filter to 1% FDR
        df = df.filter(pl.col("Q.Value") <= 0.01)

        if df.height < 10:
            return None

        # Bin RT and sum signal — pure Python, no numpy needed
        rt_min_val = float(df["RT"].min())
        rt_max_val = float(df["RT"].max())
        rt_range = rt_max_val - rt_min_val
        if rt_range <= 0:
            return None
        bin_width = rt_range / n_bins
        bin_centers = [rt_min_val + (i + 0.5) * bin_width for i in range(n_bins)]

        # Get arrays as Python lists (avoid numpy dependency)
        rt_array = df["RT"].to_list()
        signal_array = df[signal_col].to_list()

        binned_signal = [0.0] * n_bins
        for rt_val, sig_val in zip(rt_array, signal_array):
            if sig_val is None or sig_val <= 0:
                continue
            bin_idx = int((rt_val - rt_min_val) / bin_width)
            bin_idx = max(0, min(n_bins - 1, bin_idx))
            binned_signal[bin_idx] += float(sig_val)

        run_name = report_path.parent.name

        return TICTrace(
            rt_min=bin_centers,
            intensity=binned_signal,
            run_name=run_name,
        )

    except Exception:
        logger.debug("Failed to extract identified TIC from %s", report_path, exc_info=True)
        return None


def compute_tic_metrics(trace: TICTrace) -> TICMetrics:
    """Compute shape metrics from a TIC trace.

    Metrics follow DE-LIMP's approach: smoothed AUC, peak position,
    gradient width at 10% height, baseline ratio, tailing, and asymmetry.

    Args:
        trace: TICTrace from extract_tic_bruker().

    Returns:
        TICMetrics with all shape metrics.
    """
    n = len(trace.rt_min)
    if n < 10:
        return TICMetrics(n_frames=n)

    rt = trace.rt_min
    raw = trace.intensity

    # Smooth with moving average (2% window, minimum 3 points)
    window = max(3, n // 50)
    smoothed = _moving_average(raw, window)

    # Peak
    peak_idx = max(range(n), key=lambda i: smoothed[i])
    peak_tic = smoothed[peak_idx]
    peak_rt = rt[peak_idx]

    if peak_tic <= 0:
        return TICMetrics(n_frames=n)

    # AUC (trapezoid rule on smoothed)
    total_auc = sum(
        (rt[i + 1] - rt[i]) * (smoothed[i] + smoothed[i + 1]) / 2.0
        for i in range(n - 1)
    )

    # Gradient width: RT range where TIC > 10% of peak
    threshold_10pct = peak_tic * 0.10
    above = [i for i in range(n) if smoothed[i] >= threshold_10pct]
    if above:
        gradient_width = rt[above[-1]] - rt[above[0]]
    else:
        gradient_width = 0.0

    # Baseline ratio: median of lowest 10% vs peak
    sorted_vals = sorted(smoothed)
    n_low = max(1, n // 10)
    baseline_median = sorted_vals[n_low // 2]
    baseline_ratio = baseline_median / peak_tic if peak_tic > 0 else 0.0

    # Late signal ratio: signal in last 20% of gradient window
    if above:
        grad_start = rt[above[0]]
        grad_end = rt[above[-1]]
        grad_range = grad_end - grad_start
        if grad_range > 0:
            late_cutoff = grad_end - 0.2 * grad_range
            late_auc = sum(
                (rt[i + 1] - rt[i]) * (smoothed[i] + smoothed[i + 1]) / 2.0
                for i in range(n - 1)
                if rt[i] >= late_cutoff
            )
            late_signal_ratio = late_auc / total_auc if total_auc > 0 else 0.0
        else:
            late_signal_ratio = 0.0
    else:
        late_signal_ratio = 0.0

    # Asymmetry: left/right width at 50% height
    threshold_50pct = peak_tic * 0.50
    left_rt = peak_rt
    right_rt = peak_rt
    for i in range(peak_idx, -1, -1):
        if smoothed[i] < threshold_50pct:
            left_rt = rt[i]
            break
    for i in range(peak_idx, n):
        if smoothed[i] < threshold_50pct:
            right_rt = rt[i]
            break
    left_width = peak_rt - left_rt
    right_width = right_rt - peak_rt
    asymmetry = left_width / right_width if right_width > 0 else 0.0

    return TICMetrics(
        total_auc=round(total_auc, 1),
        peak_rt_min=round(peak_rt, 2),
        peak_tic=round(peak_tic, 0),
        gradient_width_min=round(gradient_width, 2),
        baseline_ratio=round(baseline_ratio, 4),
        late_signal_ratio=round(late_signal_ratio, 4),
        asymmetry=round(asymmetry, 3),
        n_frames=n,
    )


def _moving_average(values: list[float], window: int) -> list[float]:
    """Simple centered moving average."""
    n = len(values)
    half = window // 2
    result = []
    for i in range(n):
        lo = max(0, i - half)
        hi = min(n, i + half + 1)
        result.append(sum(values[lo:hi]) / (hi - lo))
    return result
