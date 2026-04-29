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
ASSET_CACHE = "/hive/data/stan_community_assets"


def _resolve_spd_from_name(name: str) -> int | None:
    """Best-effort SPD extract from filename (matches stan/metrics/scoring)."""
    m = re.search(r"(\d+)\s*spd\b", name, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"_(\d+)spd_", name, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _resolve_instrument(family: str, vendor: str) -> str:
    """Map family + vendor to an instrument_model string."""
    if family == "timsTOF":
        return "timsTOF HT"
    if family == "Lumos":
        return "Orbitrap Fusion Lumos"
    if family == "Exploris":
        return "Orbitrap Exploris 480"
    return family


def run_diann(raw: Path, out_dir: Path, vendor: str) -> Path | None:
    """Run DIA-NN 2.3.0 with frozen community params via apptainer."""
    from stan.search.community_params import (
        get_community_diann_params, build_asset_download_script,
    )

    out_dir.mkdir(parents=True, exist_ok=True)

    # Download frozen FASTA + speclib if needed (writes to ASSET_CACHE)
    bash_block = build_asset_download_script(vendor, cache_dir=ASSET_CACHE)
    bash_path = out_dir / "_assets.sh"
    bash_path.write_text(bash_block)
    subprocess.run(["bash", str(bash_path)], check=True, timeout=600)

    params = get_community_diann_params(vendor, cache_dir=ASSET_CACHE)
    out_report = out_dir / "report.parquet"

    cmd = [
        "apptainer", "exec", DIANN_SIF, DIANN_BIN,
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


def extract_and_submit(
    report: Path, raw: Path, mode: str, vendor: str, family: str,
) -> dict:
    """Pull metrics from search output, build run dict, submit to community."""
    from stan.community.submit import submit_to_benchmark
    from stan.metrics.extractor import extract_dia_metrics, extract_dda_metrics

    if mode == "dia":
        metrics = extract_dia_metrics(
            report, raw_path=raw, vendor=vendor,
        )
    else:
        metrics = extract_dda_metrics(report)

    run = dict(metrics)
    run["run_name"] = raw.name
    run["mode"] = mode
    run["vendor"] = vendor
    run["instrument"] = _resolve_instrument(family, vendor)
    run["diann_version"] = "2.3.0"
    run["run_date"] = datetime.fromtimestamp(
        raw.stat().st_mtime, tz=timezone.utc
    ).isoformat()
    spd = _resolve_spd_from_name(raw.name)

    result = submit_to_benchmark(
        run,
        spd=spd,
        amount_ng=50.0,
        diann_version="2.3.0",
    )
    return {"submission_id": result.get("submission_id"), "spd": spd, "metrics": metrics}


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
            report = run_diann(args.raw, args.out_dir, args.vendor)
        else:
            logger.error("DDA via Sage not yet wired in this dispatcher")
            sys.exit(2)

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
