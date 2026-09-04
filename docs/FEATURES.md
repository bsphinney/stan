# STAN feature highlights

> Quick-reference for Claude Code sessions. Read this before touching the
> code so you know what's already built and where it lives. Heavier docs:
> `STAN_MASTER_SPEC.md` (design spec), `CLAUDE.md` (project rules),
> `docs/PG_FARM.md` (central DB), `docs/HPC_PATHS.md` (Hive paths).

---

## What STAN is

A proteomics QC system that watches mass-spectrometer raw output, runs a
standardized search (DIA-NN for DIA, Sage for DDA), extracts a fixed set
of quality metrics, evaluates them against thresholds, and publishes
results to a public community benchmark — without ever uploading the raw
files. Designed to detect failing or drifting instruments and to surface
cross-lab comparability via aggregate metrics.

Three runtime modes:
1. **Instrument PC** — local watcher daemon + SQLite + offline dashboard
2. **Hive HPC** — SLURM-dispatched batch + central PG Farm Postgres
3. **Community** — HF Space relay + HF Dataset for aggregate cohort stats

---

## Acquisition + search

- **Auto-detects DIA vs DDA** at ingest time. Bruker: reads
  `analysis.tdf.Frames.MsmsType`. Thermo: TRFP via dotnet inspects scan
  filters / window count. Override via `--mode dia|dda`.
- **DIA-NN 2.3.0** containerized for the Hive path:
  `/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif`. Native `.raw` support
  on Hive, no msconvert. Half-CPU on instrument PCs (`max(2, cpu/2)`).
- **Sage 0.14.7** for DDA. Thermo `.raw` goes through TRFP→mzML first;
  Bruker `.d` is read natively.
- **Frozen community params + spectral library** pinned per
  search-engine version. Hash-verified at submission time so the relay
  rejects rows from a different FASTA/speclib.
  Live in `stan/search/community_params.py`.

---

## Metric pipeline (`stan/metrics/`)

| Metric | Source | Notes |
|---|---|---|
| `n_precursors` (DIA) | DIA-NN report.parquet, Q.Value ≤ 0.01 | **Primary** for DIA community benchmark |
| `n_psms` (DDA) | Sage results.sage.parquet | **Primary** for DDA community benchmark |
| `n_peptides`, `n_proteins` | search engine | Secondary; proteins never primary |
| `ips_score` | IPS (Instrument Performance Score, 0–100) | Cohort-calibrated depth score with (family, SPD-bucket) reference |
| `median_peak_width_sec` | RT.Stop - RT.Start | Required for v1 community submission |
| `median_points_across_peak` | Bruker: `DiaFrameMsMsWindows` coverage. Thermo: 2× FWHM scans approx | Matthews & Hayes 1976 quant-quality metric |
| `peak_capacity` | gradient ÷ peak_width | Computed |
| `dynamic_range_log10` | log10(p99/p01) precursor intensity | |
| `tic_rt_bins` + `tic_intensity` | 128-bin downsampled TIC trace | Identified-only, shipped to community for cross-lab gradient comparison |
| `peg_score`, `peg_class` | PEG iRT-anchor coverage via alphatims | Bruker-only |
| `drift_coverage`, `drift_median_im` | Bruker ion-mobility drift QC | Bruker-only — NULL on Thermo is correct |
| `ms2_analyzer` | TRFP scan-filter parsing | "OT" / "IT" / "tof" for cohort split |
| `library_coverage_pct` | n_precursors ÷ community library precursors | DIA only |
| `column_vendor`, `column_model` | From `instruments.yml` (set at `stan setup`) | NOT auto-detected |
| `spd` | 6-stage resolution chain (Bruker XML → TDF → frames → fallback) | See CLAUDE.md "SPD resolution chain" |
| `lc_system` | `detect_lc_system` from raw metadata | |

---

## SPD-first cohort design

Throughput is keyed on **samples per day (SPD)** rather than gradient
minutes — community benchmark cohorts bucket by `(instrument_family,
SPD_bucket)` so a 60-SPD run on a Lumos compares against other 60-SPD
Lumos runs, not a 100-SPD timstof. Default load 50 ng HeLa.

`stan fix-spds` re-resolves NULL `spd` rows from existing raw metadata
when the chain is improved. `stan fix-sample-spds` does the same for
non-QC acquisitions in `sample_health` (v1.0.85), which is what lets
the TIC overlay's SPD filter keep Sample and Blank traces instead of
emptying both panels.

Utilisation percentages are scored against each instrument's own two
most-used gradients, not a fixed Evosep 100/60 pair.

---

## Gating + community submission

- `stan/gating/evaluator.py` — applies per-model thresholds from
  `~/.stan/thresholds.yml`. Hard fails set `HOLD` flag in queue.
- `stan/community/submit.py` — POSTs to HF Space relay
  (`brettsp-stan.hf.space/api/submit`). **No HF token required** —
  the relay holds the secret. Submissions include all v1 fields:
  metrics, gates, TIC trace, library coverage, asset hashes.
- `stan submit-all` walks runs, validates, posts un-submitted ones.
  Idempotent. Supports `--backend pg` for PG Farm reads.

---

## Storage backends (three concurrent)

| Backend | Use | Lives at |
|---|---|---|
| SQLite (local) | Instrument PC default | `~/.stan/stan.db` |
| SQLite (Hive mirror) | Per-instrument backup | `/quobyte/proteomics-grp/STAN/<host>/stan.db` |
| **PG Farm Postgres** | Central source-of-truth for Hive bulk + dashboards | `pgfarm.library.ucdavis.edu/uc-davis-genome-center-proteomics-core/stan` |

Activate PG writes with `STAN_DB_BACKEND=pg`. Full docs:
`docs/PG_FARM.md`. The Mac launchd sync (`scripts/cron_sync_to_pgfarm.sh`)
keeps the per-instrument SQLites mirrored to PG every 30 min.

---

## HPC orchestration

- `stan hive-dispatch` (`stan/community/scripts/dispatch_hive.py`)
  walks watch dirs OR submits one raw via `--raw`. Generates per-raw
  sbatch scripts under `<sbatch_log_dir>/scripts/`. Each sets
  `STAN_DB_BACKEND=pg` + PGPASSWORD so step_extract writes straight
  to PG, bypassing the corrupt Quobyte SQLite.
- `stan hive-process` (per-job entry point) runs detect → search →
  extract → IPS → gates → DB write → TIC → 4DFF → PEG/drift.
- `--step search|features|pegdrift|extract` for parallel-DAG mode.
  Bruker .d gets a 4-job DAG (search + 4DFF + pegdrift in parallel,
  extract via afterany dependency). Cuts wall time ~30%.
- **Partitions**: `low` for batch (publicgrp-low-qos), `high` for
  live QC dispatch only (genome-center-grp-high-qos). Strict rule —
  see CLAUDE.md "SLURM partition policy".

---

## Recovery + repair

- `stan ingest-orphans` — walks `/quobyte/proteomics-grp/STAN/processing/`,
  parses sbatch sidecars to recover cohort args, re-extracts metrics
  from existing parquets and upserts to PG. Used after the 2026-05-16
  SQLite-corruption incident to recover ~2,700 orphan parquets without
  re-running DIA-NN. Idempotent.
- `scripts/repair_and_reingest.sh` — full recovery driver: stop
  dispatcher → snapshot SQLite → ingest-orphans into PG.
- Failure modes are recoverable: corrupt local SQLite doesn't lose
  search output as long as `/processing/` survives.

---

## Dashboard (`stan/dashboard/`)

- FastAPI backend + single-file React UI in `public/index.html`
- Default port `localhost:8421`. Hive-mode tunnel via SSH.
- Plotly per-charge ion-cloud view (requires Bruker `.features`
  sidecar from `stan run-4dff`). Falls back to SVG cloud when absent.
- "QC History" tab with per-instrument filter dropdown
- "Trends" tab cohort-keyed by SPD bucket
- **Museum** + **Karatemass** at `/museum.html` and `/karatemass.html` —
  historical mass-spec timeline + Karateka-parody educational game

---

## CLI surface (`stan/cli.py`, ~7,100 lines)

Major commands:
- `stan init|setup` — first-time config wizard
- `stan watch` — watcher daemon
- `stan dashboard` — local web UI
- `stan version|doctor|verify` — diagnostics
- `stan submit-all [--backend pg]` — push to community relay
- `stan hive-dispatch|hive-process|ingest-orphans` — HPC + recovery
- `stan backfill-tic|backfill-metrics|fix-spds|fix-sample-spds` — re-derive metrics
- `stan baseline` — retroactive QC over existing dirs
- `stan backfill-from-dir` — retroactively search + extract a dir of raws
- `stan hive-upload` — SMB-upload a single raw to the Hive incoming dir
- `stan run-4dff|install-4dff` — Bruker ion-cloud sidecar
- `stan time-hive-partitions` — partition-comparison timing test

Tab-complete via `stan --install-completion`.

---

## Privacy stance (hard rules — see CLAUDE.md)

- Raw files NEVER uploaded
- Patient/sample metadata NEVER collected
- Run filename optionally stripped on submission via
  `STAN_STRIP_RUN_NAME=1` (default sends; community dataset publishes
  every submission as a parquet)
- CC BY 4.0 on the community dataset
- Serial numbers stored server-side, never exposed in API/downloads

---

## Three repositories

1. `github.com/bsphinney/stan` — code
2. `huggingface.co/spaces/brettsp/stan` — community dashboard relay
3. `huggingface.co/datasets/brettsp/stan-benchmark` — aggregate dataset

Domain alias `stan-proteomics.org` → HF Space.

---

## What to read next

| Want to | Read |
|---|---|
| Understand the design | `STAN_MASTER_SPEC.md` |
| Touch the DB | `docs/PG_FARM.md` |
| Touch the Hive | `docs/HPC_PATHS.md` + CLAUDE.md "Hive rules" |
| Avoid known traps | `docs/GOTCHAS_DELIMP.md` |
| Search engine flags | `docs/external_tools.md` |
| Run the v1 release | `docs/V1_PRERELEASE_CHECKLIST.md` |
