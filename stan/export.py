"""Export and import QC data for backup, migration, and external analysis.

Three export formats:
  - archive: full .tar.gz with DB + config + metadata (for moving between STAN installs)
  - json:    flat JSON with schema docs (for LLMs / external tools)
  - parquet: columnar parquet (for Python/R analysis)

Import handles deduplication via the submission fingerprint so re-importing
the same archive doesn't create duplicate rows.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stan import __version__
from stan.config import get_user_config_dir
from stan.db import get_db_path

logger = logging.getLogger(__name__)

# Metric schema — used in AI/LLM exports so tools can interpret the data
# without needing to guess what each field means.
METRIC_SCHEMA = {
    "instrument": "Instrument name as configured in instruments.yml",
    "run_name": "Raw file or directory name",
    "run_date": "ISO timestamp when STAN processed the run",
    "mode": "Acquisition mode: dia or dda",
    "amount_ng": "HeLa digest amount injected, in nanograms",
    "spd": "Samples per day (LC method throughput)",
    "gradient_length_min": "Active gradient length in minutes (fallback if SPD not set)",
    "column_vendor": "LC column manufacturer (Evosep, IonOpticks, PepSep, Thermo)",
    "column_model": "LC column model including dimensions and particle size",
    "n_precursors": "Unique precursors identified at 1% FDR (DIA)",
    "n_peptides": "Unique stripped peptide sequences at 1% FDR",
    "n_proteins": "Unique protein groups at 1% protein FDR",
    "n_psms": "Peptide-spectrum matches at 1% FDR (DDA)",
    "median_cv_precursor": "Median CV of precursor intensities across replicates (percent, DIA)",
    "median_fragments_per_precursor": "Median fragment XICs per precursor",
    "pct_fragments_quantified": "Fraction of extracted fragments with nonzero intensity",
    "median_peak_width_sec": "Median chromatographic peak width in seconds",
    "median_points_across_peak": (
        "Median MS2 scans per elution peak. Matthews and Hayes 1976 — "
        "values <6 indicate quantitation degradation"
    ),
    "missed_cleavage_rate": "Fraction of peptides with at least one missed cleavage",
    "pct_charge_1": "Fraction of singly-charged precursors (elevated = source contamination)",
    "pct_charge_2": "Fraction of doubly-charged precursors (typical optimal)",
    "pct_charge_3": "Fraction of triply-charged precursors",
    "ips_score": (
        "Instrument Performance Score (0-100). DIA: 30% precursor depth + "
        "25% spectral quality + 20% sampling + 15% quant coverage + 10% digestion. "
        "DDA: 30% PSM depth + 25% mass accuracy + 20% sampling + 15% hyperscore + 10% digestion"
    ),
    "gate_result": "QC gate result: pass, warn, or fail",
    "failed_gates": "JSON array of metric names that failed thresholds",
    "diagnosis": "Plain-English explanation of any failures",
}


# ── Export ──────────────────────────────────────────────────────────

CLAUDE_ANALYSIS_PROMPT = """# STAN QC Data Analysis Request

You are analyzing QC data from a proteomics mass spectrometer monitored by STAN
(Standardized proteomic Throughput ANalyzer). This archive contains longitudinal
HeLa standard QC runs from one or more instruments. Your job is to help the user
understand their instrument's health, spot problems, compare against the
community benchmark, and make actionable recommendations.

## What's in this archive

- `stan_runs.json` — flat JSON of the user's QC runs with a `schema` section
  documenting every metric. Read the schema first.
- `stan_runs.parquet` — the same data in parquet format for faster loading.
- `community_baseline.json` — snapshot of the STAN community benchmark at export
  time. This contains reference data from other labs' HeLa QC runs on similar
  instruments. Use this as your primary community comparison source.
  **Prefer live data if available**: if you can fetch URLs, hit
  `https://brettsp-stan.hf.space/api/leaderboard` for the current submissions —
  the community dataset grows over time and live data is more accurate than this
  snapshot. Fall back to the bundled file if network is unavailable.
- `instruments.yml`, `thresholds.yml` — the lab's instrument config and QC thresholds.
- `manifest.json` — STAN version, export date, run count.

## What to produce

1. **A written report** covering:
   - Instrument fleet summary (which instruments, how many runs each, date range)
   - Current performance state per instrument (median precursors/PSMs, IPS score)
   - **"How my instruments compare to the community"** — this is the most
     important section. For EACH of the user's instruments:
     * Identify its cohort: `instrument_family` + SPD bucket + amount bucket
       (e.g., "Astral_60spd_low" for an Astral at 60 SPD with 50 ng HeLa)
     * Find all community submissions in the same cohort (use `community_baseline.json`
       or fetch live data from https://brettsp-stan.hf.space/api/leaderboard)
     * Compute the user's percentile rank for precursors, peptides, proteins, IPS
     * State it plainly: "Your Astral at 60 SPD produces 22,500 precursors,
       which is at the 68th percentile of 45 community submissions in this cohort.
       The community median is 20,100 (range 15,200 – 26,800 IQR)."
     * If the user's instrument is BELOW the 25th percentile, flag it as
       underperforming and suggest concrete fixes.
     * If ABOVE the 75th percentile, note it as excellent performance.
     * If you can't find a matching cohort (too few community submissions for
       that exact combination), fall back to the broader instrument family and
       note the reduced confidence.
   - Any concerning trends (drops in ID counts, rising CV, declining IPS)
   - Failed runs and what they might indicate (low IDs + low IPS → LC/source;
     high missed cleavages → digestion; high +1 charge → source contamination)
   - Specific actionable recommendations (e.g., "column replacement recommended
     on Astral based on 15% drop in precursors over last 30 days; also below the
     community 25th percentile for this cohort")

2. **Figures** (generate with matplotlib or plotly, save as PNG):
   - Longitudinal trends per instrument (precursors/PSMs over time, with rolling median)
   - **Community comparison plot**: the user's submissions overlaid on the community
     distribution for their cohort — violin or KDE with the user's points highlighted
   - IPS score time series with threshold lines
   - Points-across-peak distribution (Matthews and Hayes 1976 quantitation quality)
   - Instrument comparison box plots if multiple instruments

3. **Raw numbers** the core director can paste into a PI email:
   - "Your samples were run on an Astral producing X precursors at 1% FDR, which is
     in the Nth percentile of the STAN community benchmark for comparable setups
     (instrument family × throughput × injection amount)."

## Metric hierarchy — important context

STAN uses a deliberate metric hierarchy:
- **Primary**: precursor count (DIA) / PSM count (DDA) — purest instrument signal
- **Secondary**: peptide count, protein count — valid because FASTA is standardized
- **Health**: IPS score, points across peak, missed cleavage rate, charge distribution

Do NOT rank instruments by protein count alone — that metric is confounded by
FASTA choice and protein inference, even when the search is standardized.

## Tone

Write for a proteomics core facility director or an experienced MS user. Be
specific, use proper terminology, and back claims with the numbers in the data.
Don't hedge — if something is clearly broken, say so.

## Getting started

```python
import json
with open('stan_runs.json') as f:
    data = json.load(f)

# Read the schema first
print(data['schema'])

# Then work with the runs
runs = data['runs']
print(f"Total runs: {len(runs)}")
```

Now proceed with the analysis. Start by summarizing what you see in the data,
then dive into each instrument and produce figures as you go.
"""


def export_claude(output_path: Path | None = None, limit: int | None = None) -> Path:
    """Export a .zip optimized for dropping into Claude, ChatGPT, or other LLMs.

    Bundles the data, schema, and a ready-made prompt that asks the AI to
    produce a full QC report with figures. User just drops the zip into the
    chat and gets an instant analysis.

    Args:
        output_path: Optional destination. Defaults to ~/stan_for_claude_YYYYMMDD.zip
        limit: Maximum runs to export. None = all.

    Returns:
        Path to the created zip.
    """
    import zipfile

    if output_path is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path.home() / f"stan_for_claude_{stamp}.zip"

    config_dir = get_user_config_dir()
    runs = _fetch_runs(limit=limit)

    with tempfile.TemporaryDirectory() as tmpdir:
        staging = Path(tmpdir)

        # 1. The prompt — this is what makes it one-drop
        (staging / "README_START_HERE.md").write_text(CLAUDE_ANALYSIS_PROMPT)

        # 2. Flat JSON with schema
        payload = {
            "stan_version": __version__,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "n_runs": len(runs),
            "schema": METRIC_SCHEMA,
            "metric_hierarchy": {
                "primary_dia": "n_precursors",
                "primary_dda": "n_psms",
                "secondary": ["n_peptides", "n_proteins"],
                "health": ["ips_score", "median_points_across_peak", "missed_cleavage_rate"],
            },
            "runs": runs,
        }
        (staging / "stan_runs.json").write_text(json.dumps(payload, indent=2, default=str))

        # 3. Same data as parquet for faster loading
        try:
            import polars as pl
            if runs:
                pl.DataFrame(runs, strict=False).write_parquet(staging / "stan_runs.parquet")
        except Exception:
            logger.warning("Parquet export skipped — polars not available")

        # 4. Config files for context
        for fname in ["instruments.yml", "thresholds.yml"]:
            src = config_dir / fname
            if src.exists():
                shutil.copy2(src, staging / fname)

        # 5. Community baseline — small (~80 KB zipped) and lets Claude
        #    compare the user's runs against the community without needing
        #    network access during the analysis.
        try:
            import urllib.request
            with urllib.request.urlopen(
                "https://brettsp-stan.hf.space/api/leaderboard", timeout=30
            ) as r:
                community_data = r.read()
            (staging / "community_baseline.json").write_bytes(community_data)
            logger.info("Bundled community baseline (%d bytes)", len(community_data))
        except Exception as e:
            logger.warning("Could not fetch community baseline: %s", e)
            (staging / "community_baseline.json").write_text(
                json.dumps({
                    "note": "Community baseline fetch failed at export time.",
                    "reason": str(e),
                    "fallback": "Fetch manually from https://brettsp-stan.hf.space/api/leaderboard",
                })
            )

        # 5. Manifest
        manifest = {
            "stan_version": __version__,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "n_runs": len(runs),
            "purpose": "AI-assisted QC analysis",
            "intended_reader": "Claude / ChatGPT / Gemini or any LLM",
            "instructions": "Read README_START_HERE.md first. It contains the analysis prompt.",
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2))

        # Zip it
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in staging.iterdir():
                zf.write(f, arcname=f.name)

    logger.info("Claude export ready: %s (%d runs)", output_path, len(runs))
    return output_path


def export_archive(output_path: Path | None = None) -> Path:
    """Export STAN data as a portable .tar.gz archive.

    Contents:
      - stan.db (SQLite database with all runs)
      - instruments.yml, thresholds.yml, community.yml
      - manifest.json (STAN version, export date, stats)

    Args:
        output_path: Optional destination path. Defaults to ~/stan_export_YYYYMMDD.tar.gz

    Returns:
        Path to the created archive.
    """
    db_path = get_db_path()
    config_dir = get_user_config_dir()

    if output_path is None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = Path.home() / f"stan_export_{stamp}.tar.gz"

    with tempfile.TemporaryDirectory() as tmpdir:
        staging = Path(tmpdir) / "stan_export"
        staging.mkdir()

        # Copy database (if it exists)
        if db_path.exists():
            shutil.copy2(db_path, staging / "stan.db")
            n_runs = _count_runs(db_path)
        else:
            n_runs = 0
            logger.warning("No database found at %s", db_path)

        # Copy config files
        for fname in ["instruments.yml", "thresholds.yml", "community.yml"]:
            src = config_dir / fname
            if src.exists():
                shutil.copy2(src, staging / fname)

        # Write manifest
        manifest = {
            "stan_version": __version__,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "n_runs": n_runs,
            "schema": METRIC_SCHEMA,
            "instructions": (
                "Restore with: stan import <path_to_archive>. "
                "STAN will merge runs with your existing database using "
                "fingerprint-based deduplication. Config files are NOT "
                "overwritten — review them manually if needed."
            ),
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2))

        # Create tarball
        with tarfile.open(output_path, "w:gz") as tar:
            tar.add(staging, arcname="stan_export")

    logger.info("Exported %d runs to %s", n_runs, output_path)
    return output_path


def export_json(output_path: Path | None = None, limit: int | None = None) -> Path:
    """Export runs as a flat JSON file for LLMs and external tools.

    Includes the metric schema so tools can interpret fields without
    external context.

    Args:
        output_path: Optional destination. Defaults to ~/stan_runs.json
        limit: Maximum runs to export (newest first). None = all.

    Returns:
        Path to the created file.
    """
    if output_path is None:
        output_path = Path.home() / f"stan_runs_{datetime.now().strftime('%Y%m%d')}.json"

    runs = _fetch_runs(limit=limit)

    payload = {
        "stan_version": __version__,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "n_runs": len(runs),
        "schema": METRIC_SCHEMA,
        "metric_hierarchy": {
            "primary_dia": "n_precursors",
            "primary_dda": "n_psms",
            "secondary": ["n_peptides", "n_proteins"],
            "health": ["ips_score", "median_points_across_peak", "missed_cleavage_rate"],
        },
        "runs": runs,
    }

    output_path.write_text(json.dumps(payload, indent=2, default=str))
    logger.info("Exported %d runs to %s", len(runs), output_path)
    return output_path


def export_parquet(output_path: Path | None = None, limit: int | None = None) -> Path:
    """Export runs as a parquet file for Python/R/DuckDB analysis."""
    import polars as pl

    if output_path is None:
        output_path = Path.home() / f"stan_runs_{datetime.now().strftime('%Y%m%d')}.parquet"

    runs = _fetch_runs(limit=limit)
    if not runs:
        # Write an empty dataframe with expected columns
        pl.DataFrame({"run_name": [], "instrument": []}).write_parquet(output_path)
    else:
        df = pl.DataFrame(runs, strict=False)
        df.write_parquet(output_path)

    logger.info("Exported %d runs to %s", len(runs), output_path)
    return output_path


# ── Import ──────────────────────────────────────────────────────────

def import_archive(archive_path: Path, skip_duplicates: bool = True) -> dict:
    """Import a .tar.gz archive into the local STAN database.

    Merges runs with the existing database. Duplicates are detected via
    a (instrument, run_name, run_date) tuple since not every legacy run
    has a fingerprint.

    Args:
        archive_path: Path to the .tar.gz created by export_archive.
        skip_duplicates: If True, skip rows that already exist.

    Returns:
        Dict with import stats: imported, skipped, total.
    """
    archive_path = Path(archive_path)
    if not archive_path.exists():
        raise FileNotFoundError(f"Archive not found: {archive_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(tmpdir)

        extracted = Path(tmpdir) / "stan_export"
        if not extracted.exists():
            raise ValueError("Invalid archive — missing stan_export directory")

        manifest_path = extracted / "manifest.json"
        manifest = {}
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            logger.info("Importing from STAN v%s (%d runs exported %s)",
                        manifest.get("stan_version", "?"),
                        manifest.get("n_runs", 0),
                        manifest.get("exported_at", "?"))

        src_db = extracted / "stan.db"
        if not src_db.exists():
            return {"imported": 0, "skipped": 0, "total": 0}

        return _merge_databases(src_db, skip_duplicates=skip_duplicates)


def _merge_databases(src_db: Path, skip_duplicates: bool) -> dict:
    """Merge rows from src_db into the local database."""
    from stan.db import init_db

    init_db()  # ensure target schema is current
    dst_db = get_db_path()

    with sqlite3.connect(str(src_db)) as src_con, sqlite3.connect(str(dst_db)) as dst_con:
        src_con.row_factory = sqlite3.Row
        src_rows = src_con.execute("SELECT * FROM runs").fetchall()

        if not src_rows:
            return {"imported": 0, "skipped": 0, "total": 0}

        # Build set of existing (instrument, run_name, run_date) keys
        existing = set()
        if skip_duplicates:
            for row in dst_con.execute("SELECT instrument, run_name, run_date FROM runs"):
                existing.add((row[0], row[1], row[2]))

        # Get target column list (to handle schema mismatches)
        dst_cols = {row[1] for row in dst_con.execute("PRAGMA table_info(runs)").fetchall()}

        imported = 0
        skipped = 0
        for row in src_rows:
            row_dict = dict(row)
            key = (row_dict.get("instrument"), row_dict.get("run_name"), row_dict.get("run_date"))
            if key in existing:
                skipped += 1
                continue

            # Only insert columns that exist in the target schema
            filtered = {k: v for k, v in row_dict.items() if k in dst_cols}
            cols = ", ".join(filtered.keys())
            placeholders = ", ".join(f":{k}" for k in filtered.keys())
            try:
                dst_con.execute(f"INSERT INTO runs ({cols}) VALUES ({placeholders})", filtered)
                imported += 1
            except sqlite3.IntegrityError:
                skipped += 1

        dst_con.commit()

    return {"imported": imported, "skipped": skipped, "total": len(src_rows)}


# ── Helpers ─────────────────────────────────────────────────────────

def _count_runs(db_path: Path) -> int:
    try:
        with sqlite3.connect(str(db_path)) as con:
            return con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    except Exception:
        return 0


def _fetch_runs(limit: int | None = None) -> list[dict[str, Any]]:
    """Fetch all runs (or the most recent `limit`) as list of dicts."""
    db_path = get_db_path()
    if not db_path.exists():
        return []

    query = "SELECT * FROM runs ORDER BY run_date DESC"
    if limit:
        query += f" LIMIT {int(limit)}"

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(query).fetchall()

    return [dict(row) for row in rows]
