"""Search ONE raw file with frozen v1.0 community params, extract
metrics, submit to community. Runs INSIDE a single SLURM job.

USAGE
    python -m stan.community.scripts.run_one_v1 \
        --raw /quobyte/proteomics-grp/hela_qcs/timstofHT/dia/<file>.d \
        --mode dia \
        --vendor bruker \
        --out-dir /quobyte/proteomics-grp/brett/v1_smoke/<run_name>

Output
- Search results in --out-dir
- One v1.0-compliant row pushed to brettsp/stan-benchmark
- JSONL line appended to ~/STAN/logs/v1_smoke_<date>.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DIANN_SIF = "/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif"
DIANN_BIN = "/diann-2.3.0/diann-linux"
# Native Sage binary (DE-LIMP uses this for 7K+ DDA searches).
SAGE_BIN = (
    "/quobyte/proteomics-grp/de-limp/cascadia/"
    "sage-v0.14.7-x86_64-unknown-linux-gnu/sage"
)
# Brett's writable location on Hive (/hive/data/ is read-only).
ASSET_CACHE = "/quobyte/proteomics-grp/brett/stan_community_assets"
# Per-instrument config (instruments.yml) is synced to the mirror by
# the watcher and contains column_vendor / column_model / lc_system —
# all needed by the dashboard's Column Comparison panel.
MIRROR_BASE = "/quobyte/proteomics-grp/STAN"
FAMILY_TO_HOST = {
    "timsTOF": "TIMS-10878",
    "Lumos": "lumosRox",
    "Exploris": "DESKTOP-FOT3DAA",
}


def _column_metadata_for_family(family: str) -> dict:
    """Read column + lc_system metadata from the synced instruments.yml.

    Returns ``{column_vendor, column_model, lc_system}``. Empty dict
    when the host directory or YAML isn't reachable.
    """
    host = FAMILY_TO_HOST.get(family, "")
    if not host:
        return {}
    yml_path = Path(MIRROR_BASE) / host / "instruments.yml"
    if not yml_path.exists():
        return {}
    try:
        import yaml

        cfg = yaml.safe_load(yml_path.read_text())
        instruments = cfg.get("instruments") or []
        if not instruments:
            return {}
        first = instruments[0]
        out = {}
        for k in ("column_vendor", "column_model", "lc_system"):
            if first.get(k):
                out[k] = first[k]
        return out
    except Exception:
        logger.exception("Failed to read %s", yml_path)
        return {}


def _resolve_spd_from_name(name: str) -> int | None:
    """Best-effort SPD extract from filename (matches stan/metrics/scoring)."""
    m = re.search(r"(\d+)\s*spd\b", name, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"_(\d+)spd_", name, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _resolve_spd_from_report(report: Path) -> int | None:
    """Derive SPD from the actual gradient length in DIA-NN's report.

    Many older Thermo runs (Lumos, Exploris) were named by gradient
    minutes (e.g. ``qeP_20191112_HeLa_110m.raw``) instead of SPD, so
    the filename regex fails. Read RT.max - RT.min from the search
    output and bucket via ``gradient_min_to_spd``.
    """
    try:
        import polars as pl

        from stan.metrics.scoring import gradient_min_to_spd

        df = pl.read_parquet(report, columns=["RT"])
        if df.height == 0:
            return None
        rt_min = float(df["RT"].min())
        rt_max = float(df["RT"].max())
        gradient = rt_max - rt_min
        if gradient <= 0:
            return None
        return gradient_min_to_spd(gradient)
    except Exception:
        logger.exception("SPD-from-report failed (non-fatal)")
        return None


def _gradient_min_for_spd(spd: int | None) -> float | None:
    """Approx gradient length for an Evosep SPD setting.

    SPD = samples per day, so the per-sample window is roughly
    1440 / SPD minutes. Real Evosep methods bias slightly shorter
    (overhead etc.) but this is good enough for peak_capacity
    computation in extract_dia_metrics. None when SPD unknown.
    """
    if not spd or spd <= 0:
        return None
    # Evosep duty-cycle approximation: 90% of the wall-clock window
    # is the actual gradient (the rest is wash/equil).
    return round(1440.0 / spd * 0.9, 1)


def _resolve_instrument(family: str, vendor: str) -> str:
    """Map family + vendor to an instrument_model string."""
    if family == "timsTOF":
        return "timsTOF HT"
    if family == "Lumos":
        return "Orbitrap Fusion Lumos"
    if family == "Exploris":
        return "Orbitrap Exploris 480"
    return family


def run_diann(raw: Path, out_dir: Path, vendor: str, family: str = "") -> Path | None:
    """Run DIA-NN 2.3.0 with frozen community params via apptainer."""
    from stan.search.community_params import (
        get_community_diann_params, build_asset_download_script,
    )

    out_dir.mkdir(parents=True, exist_ok=True)

    # Download frozen FASTA + speclib if needed (writes to ASSET_CACHE).
    # Prefix with `set -euo pipefail` so a download failure aborts the
    # script loudly instead of silently leaving an empty cache.
    bash_block = "set -euo pipefail\n" + build_asset_download_script(
        vendor, cache_dir=ASSET_CACHE,
    )
    bash_path = out_dir / "_assets.sh"
    bash_path.write_text(bash_block)
    subprocess.run(["bash", str(bash_path)], check=True, timeout=600)

    params = get_community_diann_params(vendor, cache_dir=ASSET_CACHE)

    # Prefer the per-instrument library subset when available — built
    # from this instrument's baseline runs against the same community
    # parent, ~3-9x smaller, so DIA-NN searches finish proportionally
    # faster. Per-instrument libraries are rebuilt by the watcher as
    # new high-quality data comes in, so the subset stays current.
    inst_lib = (
        Path(MIRROR_BASE) / FAMILY_TO_HOST.get(family, "") / "instrument_library.parquet"
        if family in FAMILY_TO_HOST else None
    )
    if inst_lib and inst_lib.is_file():
        logger.info(
            "Using per-instrument library %s (%.1f MB)",
            inst_lib, inst_lib.stat().st_size / 1e6,
        )
        params["lib"] = str(inst_lib)
    else:
        logger.info(
            "No per-instrument library for %s (host=%s); using community library",
            family, FAMILY_TO_HOST.get(family, "?"),
        )

    out_report = out_dir / "report.parquet"

    # Bind every storage tree the job touches into the container.
    # /quobyte = community assets + most raw files, /nfs = flinders QC,
    # /tmp = scratch.
    cmd = [
        "apptainer", "exec",
        "--bind", "/quobyte:/quobyte",
        "--bind", "/nfs:/nfs",
        "--bind", "/tmp:/tmp",
        DIANN_SIF, DIANN_BIN,
        "--f", str(raw),
        "--lib", params["lib"],
        "--fasta", params["fasta"],
        "--out", str(out_report),
        "--threads", str(params.get("threads", 8)),
        "--qvalue", str(params["qvalue"]),
        "--min-pep-len", str(params["min-pep-len"]),
        "--max-pep-len", str(params["max-pep-len"]),
        "--missed-cleavages", str(params["missed-cleavages"]),
        "--min-pr-charge", str(params["min-pr-charge"]),
        "--max-pr-charge", str(params["max-pr-charge"]),
    ]
    logger.info("Running DIA-NN: %s", " ".join(cmd))
    started = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10800)  # 3h
    elapsed = time.monotonic() - started
    (out_dir / "diann.stdout.log").write_text(result.stdout)
    (out_dir / "diann.stderr.log").write_text(result.stderr)
    logger.info("DIA-NN exit=%d in %.1fs", result.returncode, elapsed)

    if result.returncode != 0 or not out_report.exists():
        logger.error("DIA-NN failed — see %s", out_dir / "diann.stderr.log")
        return None
    return out_report


def run_sage(raw: Path, out_dir: Path, vendor: str) -> Path | None:
    """Run Sage with frozen community DDA params on a raw file.

    Native Sage binary (no apptainer) — DE-LIMP uses the same one for
    its 7K+ DDA searches. Bruker .d input is read directly. Thermo
    .raw is rejected upstream because Sage prefers centroided mzML
    and we don't have the msconvert pipeline wired in yet.
    """
    from stan.search.community_params import build_asset_download_script
    from stan.search.sage import build_sage_config_json

    out_dir.mkdir(parents=True, exist_ok=True)

    # Frozen community FASTA download (Sage doesn't need a speclib).
    bash_block = "set -euo pipefail\n" + build_asset_download_script(
        vendor, cache_dir=ASSET_CACHE,
    )
    bash_path = out_dir / "_assets.sh"
    bash_path.write_text(bash_block)
    subprocess.run(["bash", str(bash_path)], check=True, timeout=600)

    config_text = build_sage_config_json(raw, out_dir, vendor)
    config_path = out_dir / "sage_config.json"
    config_path.write_text(config_text)

    cmd = [SAGE_BIN, "--write-pin", str(config_path), str(raw)]
    logger.info("Running Sage: %s", " ".join(cmd))
    started = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10800)
    elapsed = time.monotonic() - started
    (out_dir / "sage.stdout.log").write_text(result.stdout)
    (out_dir / "sage.stderr.log").write_text(result.stderr)
    logger.info("Sage exit=%d in %.1fs", result.returncode, elapsed)

    # Sage 0.14.x default output is TSV, not parquet. Read the TSV
    # and re-emit as parquet so downstream extract_dda_metrics
    # (which expects parquet) finds what it needs.
    tsv_report = out_dir / "results.sage.tsv"
    out_report = out_dir / "results.sage.parquet"
    if result.returncode != 0 or not tsv_report.exists():
        logger.error("Sage failed — see %s", out_dir / "sage.stderr.log")
        return None
    if not out_report.exists():
        try:
            import polars as pl

            pl.read_csv(tsv_report, separator="\t").write_parquet(out_report)
        except Exception:
            logger.exception("Failed to convert Sage TSV to parquet")
            return None
    return out_report


def extract_and_submit(
    report: Path, raw: Path, mode: str, vendor: str, family: str,
) -> dict:
    """Pull metrics from search output, build run dict, submit to community."""
    from stan.community.submit import submit_to_benchmark
    from stan.metrics.extractor import extract_dia_metrics, extract_dda_metrics

    # Mirror the local watcher's _resolve_spd chain (daemon.py):
    #   1. validate_spd_from_metadata — Bruker XML / TDF / Thermo trfp
    #   2. instruments.yml `spd:` cohort default for this host
    #   3. Filename regex (60spd / 100-spd / etc.)
    #   4. RT-span fallback unique to the cluster path
    #   5. None
    from stan.metrics.scoring import validate_spd_from_metadata

    spd = None
    try:
        spd = validate_spd_from_metadata(raw)
    except Exception:
        logger.debug("validate_spd_from_metadata failed", exc_info=True)
    if spd is None:
        # Step 2 — instruments.yml on the mirror has a cohort default.
        col_meta = _column_metadata_for_family(family)
        host = FAMILY_TO_HOST.get(family, "")
        if host:
            yml = Path(MIRROR_BASE) / host / "instruments.yml"
            if yml.exists():
                try:
                    import yaml

                    cfg = yaml.safe_load(yml.read_text()) or {}
                    instruments = cfg.get("instruments") or []
                    if instruments and instruments[0].get("spd"):
                        spd = int(instruments[0]["spd"])
                except Exception:
                    logger.debug("instruments.yml spd lookup failed",
                                 exc_info=True)
        _ = col_meta  # already merged into run elsewhere
    if spd is None:
        spd = _resolve_spd_from_name(raw.name)
    if spd is None:
        spd = _resolve_spd_from_report(report)
    gradient_min = _gradient_min_for_spd(spd)

    if mode == "dia":
        metrics = extract_dia_metrics(
            report,
            raw_path=raw,
            vendor=vendor,
            gradient_min=gradient_min,
        )
    else:
        metrics = extract_dda_metrics(report)

    # Compute the binned identified-TIC trace from the search output.
    # extract_tic_from_report walks report.parquet and bins
    # Precursor.Quantity into 128 RT bins. Best-effort — failure is
    # logged, not fatal.
    try:
        from stan.metrics.tic import extract_tic_from_report

        tic = extract_tic_from_report(report, n_bins=128)
        if tic is not None:
            # TICTrace uses ``rt_min`` for the bin centers (in minutes),
            # not ``rt_bins``.
            metrics["tic_rt_bins"] = list(tic.rt_min)
            metrics["tic_intensity"] = list(tic.intensity)
    except Exception:
        logger.exception("TIC extraction failed (non-fatal)")

    run = dict(metrics)
    run["run_name"] = raw.name
    run["mode"] = mode
    run["vendor"] = vendor
    run["instrument"] = _resolve_instrument(family, vendor)
    run["diann_version"] = "2.3.0"
    # Real acquisition date from raw-file metadata. Bruker .d reads
    # GlobalMetadata.AcquisitionDateTime from analysis.tdf; Thermo .raw
    # reads the file header. Falls back to filesystem mtime only when
    # both fail (filesystem mtime is wrong for archived/copied files
    # where the copy date masks the original acquisition date).
    from stan.watcher.acquisition_date import get_acquisition_date

    acq_date = get_acquisition_date(raw)
    run["run_date"] = acq_date or datetime.fromtimestamp(
        raw.stat().st_mtime, tz=timezone.utc
    ).isoformat()
    if gradient_min is not None:
        run["gradient_length_min"] = int(round(gradient_min))
    # Column + LC metadata from the watcher's synced instruments.yml.
    # Populates the dashboard Column Comparison + LC system panels.
    run.update(_column_metadata_for_family(family))

    result = submit_to_benchmark(
        run,
        spd=spd,
        gradient_length_min=int(round(gradient_min)) if gradient_min else None,
        amount_ng=50.0,
        diann_version="2.3.0",
    )
    return {
        "submission_id": result.get("submission_id"),
        "spd": spd,
        "gradient_min": gradient_min,
        "metrics": metrics,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--raw", required=True, type=Path)
    p.add_argument("--mode", required=True, choices=["dia", "dda"])
    p.add_argument("--vendor", required=True, choices=["bruker", "thermo"])
    p.add_argument("--family", required=True,
                   choices=["timsTOF", "Lumos", "Exploris"])
    p.add_argument("--out-dir", required=True, type=Path)
    args = p.parse_args()

    log_dir = Path.home() / "STAN" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"v1_smoke_{datetime.now().strftime('%Y%m%d')}.jsonl"

    record: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "raw": str(args.raw),
        "mode": args.mode,
        "vendor": args.vendor,
        "family": args.family,
    }

    try:
        if args.mode == "dia":
            report = run_diann(args.raw, args.out_dir, args.vendor, args.family)
        elif args.mode == "dda":
            if args.vendor != "bruker":
                logger.error(
                    "DDA on %s not yet wired — Thermo DDA needs msconvert pre-step",
                    args.vendor,
                )
                sys.exit(2)
            report = run_sage(args.raw, args.out_dir, args.vendor)

        if report is None:
            record.update(status="search_failed")
        else:
            sub = extract_and_submit(report, args.raw, args.mode, args.vendor, args.family)
            record.update(status="submitted", **sub)
    except Exception as e:
        logger.exception("Fatal error")
        record.update(status="error", error=f"{type(e).__name__}: {e}")

    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")

    logger.info("Done: %s", record.get("status"))
    if record.get("status") not in ("submitted",):
        sys.exit(1)


if __name__ == "__main__":
    main()
