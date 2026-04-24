# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Project**: STAN — Standardized proteomic Throughput ANalyzer  
> **Author**: Brett Stanley Phinney, UC Davis Proteomics Core  
> **GitHub**: https://github.com/bsphinney/stan  
> **HF Space**: https://huggingface.co/spaces/brettsp/stan  
> **HF Dataset**: https://huggingface.co/datasets/brettsp/stan-benchmark  
> **Master spec**: `STAN_MASTER_SPEC.md` — read this first, it is the authoritative design doc  
> **Last updated**: April 2026

---

## Golden Rule: Document Everything As You Go

**Every change to this project must be documented in the same commit.**

This means:
- Code change → update README.md (Implementation Status / TODO) + docs/user_guide.md
- Schema change → update HF Space relay API + dashboard
- New metric → update README, user guide, dashboard figures, submission schema
- HPC discovery → update memory files + docs/HPC_PATHS.md
- Bug found → add to docs/GOTCHAS_DELIMP.md or memory
- Design decision → save to memory with rationale (why, not just what)

If you can't explain where the documentation lives for a change you just made,
the change is not done yet.

**Reference files to always check before HPC work:**
- `docs/HPC_PATHS.md` — container paths, FASTA locations, storage layout
- `docs/GOTCHAS_DELIMP.md` — 50+ hard-learned lessons (DIA-NN, SLURM, data)
- `docs/QUEUE_SWITCHING.md` — auto partition switching logic

---

## Build, Test, Lint Commands

```bash
pip install -e ".[dev]"              # install in dev mode
stan init                             # creates ~/.stan/instruments.yml
stan watch                            # start watcher daemon
stan dashboard                        # serve dashboard at http://localhost:8421
pytest tests/ -v                      # run all tests
pytest tests/test_metrics.py -v       # run a single test file
pytest tests/ -k "not integration"    # skip tests requiring Hive/SLURM
pytest tests/ -k "test_grs"           # run a single test by name
ruff check stan/                      # lint
ruff check stan/ --fix                # lint with auto-fix
```

Tests marked `@pytest.mark.integration` require Hive SLURM access and are skipped in CI.

---

## Architecture Overview

```
Raw data dir (watched by watcher daemon)
    │  file stable for stable_secs
    ▼
detector.py → reads .d/analysis.tdf or .raw metadata → DIA or DDA?
    │
    ├─ DIA → diann.py → SLURM job → report.parquet
    └─ DDA → sage.py  → SLURM job → results.sage.parquet
                                │
                        extractor.py + chromatography.py
                                │
                        evaluator.py → PASS / WARN / FAIL
                            │                │
                    SQLite (Hive)      queue.py (HOLD flag)
                            │
                    dashboard (FastAPI + React, port 8421)
                            │
                    community/submit.py → HF Dataset
```

**Data flow**: watcher detects new raw files → auto-detects DIA/DDA mode → submits SLURM search job (DIA-NN or Sage) → extracts QC metrics from search results → evaluates pass/fail thresholds → stores in SQLite + optionally submits to community benchmark.

**Key modules**: `watcher/` (file monitoring + mode detection), `search/` (SLURM job builders for DIA-NN and Sage), `metrics/` (metric extraction + GRS scoring), `gating/` (threshold evaluation + HOLD flag), `community/` (HF Dataset submission/fetch), `dashboard/` (FastAPI + React UI).

**Three external repos**: GitHub (code), HF Space (public dashboard), HF Dataset (community benchmark data).

---

## CRITICAL: Always Check Primary Sources

**This is the most important rule in this file.**

STAN depends on external tools (DIA-NN, Sage, timsrust, ThermoRawFileParser, Percolator) whose
CLIs, flags, output formats, and behavior change between versions. **Never guess, assume, or
rely on training knowledge for any flag, parameter name, output column, file format detail,
or API for these tools.** Always fetch the current documentation from the primary source before
writing any code that invokes or parses output from these tools.

### Primary sources — check these, not your memory

| Tool | Primary source | What to check there |
|------|---------------|---------------------|
| **DIA-NN** | https://github.com/vdemichev/DiaNN | CLI flags, output column names, `.parquet` schema, `report.parquet` fields, `--lib` vs `--use-quant`, version changelog |
| **DIA-NN wiki** | https://github.com/vdemichev/DiaNN/wiki | Detailed parameter documentation, recommended settings |
| **DIA-NN discussions** | https://github.com/vdemichev/DiaNN/discussions | Known issues, version-specific behavior, community workarounds |
| **Sage** | https://github.com/lazear/sage | CLI flags, JSON config schema, output file names and columns, `results.sage.parquet` schema, `lfq.parquet` schema |
| **Sage releases** | https://github.com/lazear/sage/releases | Current version, breaking changes between versions |
| **Sage wiki/docs** | https://github.com/lazear/sage (README + docs/) | Config JSON format, all valid keys, enzyme syntax |
| **timsrust** | https://github.com/MannLabs/timsrust | Bruker `.d` file reading, conversion to mzML/MGF |
| **ThermoRawFileParser** | https://github.com/compomics/ThermoRawFileParser | CLI flags for Thermo `.raw` conversion, output formats |
| **Percolator** | https://github.com/percolator/percolator | Input `.pin` format, CLI flags, output column names |
| **Bruker TDF format** | https://github.com/MannLabs/alphatims (schema reference) | `analysis.tdf` SQLite schema, `Frames` table `MsmsType` values |
| **HuggingFace Hub Python** | https://huggingface.co/docs/huggingface_hub/en/index | API methods, upload/download, dataset operations |
| **watchdog (Python)** | https://python-watchdog.readthedocs.io/ | Event types, Observer setup, cross-platform behavior |
| **Polars** | https://docs.pola.rs/ | API methods, lazy vs eager, expression syntax — changes frequently |
| **FastAPI** | https://fastapi.tiangolo.com/ | Route definitions, async patterns, Pydantic models |

### How to check primary sources in practice

Before writing any code that:
- Invokes DIA-NN or Sage as a subprocess → fetch the current README/wiki for that tool
- Parses `report.parquet` columns → verify column names against current DIA-NN docs
- Parses Sage output → verify column names against current Sage release notes
- Reads Bruker `.tdf` SQLite → verify table/column names against timsrust or alphatims docs
- Uses HF Hub API → check current `huggingface_hub` docs, the API changes often
- Uses Polars expressions → check current Polars docs, syntax changes between minor versions

**Use the `web_fetch` tool to read the raw README.md directly from GitHub:**

```
# DIA-NN README
web_fetch("https://raw.githubusercontent.com/vdemichev/DiaNN/master/README.md")

# Sage README  
web_fetch("https://raw.githubusercontent.com/lazear/sage/master/README.md")

# Sage CHANGELOG or release notes
web_fetch("https://github.com/lazear/sage/releases/latest")

# DIA-NN latest release notes
web_fetch("https://github.com/vdemichev/DiaNN/releases/latest")
```

**If a flag, column name, or behavior is not confirmed in the primary source docs, do not
implement it.** Instead, add a `# TODO: verify flag name against DIA-NN vX.X docs` comment
and note it in your response so Brett can check manually.

### DIA-NN containers on Hive — CRITICAL

There are TWO DIA-NN containers on Hive with nearly identical names. Only one works
for Thermo `.raw` files:

| Container | Path | `.raw` support |
|-----------|------|----------------|
| `diann_2.3.0.sif` (underscore) | `/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif` | **YES** — has .NET bundled, reads `.raw` natively |
| `diann2.3.0.sif` (no underscore) | `/quobyte/proteomics-grp/apptainers/diann2.3.0.sif` | **NO** — missing .NET, only works for Bruker `.d` |

**Always use the `dia-nn/` directory version with the underscore.** The `apptainers/` version
will silently skip all `.raw` files and only process the FASTA, producing a predicted library
instead of an empirical one. The error message ("please install .NET Runtime") is misleading —
the fix is using the correct container, not installing .NET on the host.

Binary path inside the container: `/diann-2.3.0/diann-linux` (NOT just `diann` on PATH).

Bind mount pattern (from DE-LIMP):
```bash
apptainer exec \
    --bind "${DATA_DIR}:/work/data,${FASTA_DIR}:/work/fasta,${OUT_DIR}:/work/out" \
    /quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif \
    /diann-2.3.0/diann-linux \
    --f /work/data/file.raw \
    --fasta /work/fasta/database.fasta \
    --out /work/out/report.parquet \
    ...
```

### Current known versions (verify these are still current before use)

- DIA-NN: 2.3.1 (December 2025, preview release channel) — stable: 2.2.0 (May 2025)
  - Key: `.predicted.speclib` library format, `report.parquet` output, `--lib` for library path
  - Linux requires .NET SDK 8.0.407+
  - Source: https://github.com/vdemichev/DiaNN/discussions/1366
- Sage: actively maintained, check https://github.com/lazear/sage/releases for latest
  - Key: accepts mzML input, JSON config, outputs `results.sage.parquet` + `lfq.parquet`
  - Built-in LDA rescoring (Percolator still optional but may be redundant)
  - Source: https://github.com/lazear/sage
- Python: 3.10+ required (pyproject.toml)
- Polars: ≥0.20 (API changed significantly at 0.19→0.20, check docs for current syntax)

---

## What STAN Is

STAN is a standalone proteomics QC tool for Bruker timsTOF and Thermo Orbitrap instruments.
It is **not** a fork or module of DE-LIMP. It is a separate Python application that:

1. Watches raw data directories for new acquisitions (`.d` for Bruker, `.raw` for Thermo)
2. Auto-detects acquisition mode (DIA/DDA) from raw file metadata
3. Submits standardized search jobs to Hive (SLURM) — DIA-NN for DIA, Sage for DDA
4. Computes QC metrics from search results (precursors, peptides, CV, GRS, iRT deviation)
5. Evaluates pass/warn/fail against per-instrument thresholds
6. Writes a HOLD flag if a run fails (gating sample queue)
7. Stores all metrics in SQLite on Hive for longitudinal tracking
8. Serves a local dashboard (FastAPI + React)
9. Optionally submits to the community HeLa benchmark (HF Dataset)

The community benchmark uses **precursor count** (DIA) and **PSM count** (DDA) as primary
metrics — not protein count, which is confounded by FASTA choice and inference settings.

---

## Repository Layout

```
stan/
├── CLAUDE.md                    ← you are here
├── STAN_MASTER_SPEC.md          ← authoritative design document, read before coding
├── pyproject.toml
├── README.md
├── config/
│   ├── instruments.yml          # instrument watch dirs (user-edited, hot-reloaded)
│   ├── thresholds.yml           # QC pass/fail thresholds per instrument model
│   └── community.yml            # HF token, display name, submission prefs
├── stan/
│   ├── cli.py                   # `stan` CLI (typer)
│   ├── watcher/                 # watchdog daemon, file stability, mode detection
│   ├── search/                  # DIA-NN + Sage SLURM job builders
│   ├── metrics/                 # metric extraction, GRS, iRT, scoring
│   ├── gating/                  # threshold evaluation, HOLD flag, queue control
│   ├── community/               # HF Dataset submit/fetch/validate + consolidate.py
│   └── dashboard/               # FastAPI backend + React frontend
├── .github/workflows/
│   ├── ci.yml
│   └── consolidate_benchmark.yml
├── docs/
└── tests/
```

---

## Key Design Decisions (do not change without discussion)

### Metric hierarchy — the whole point of STAN

```
Fragment XICs/precursor  ← purest instrument signal
Precursor count @ 1% FDR ← PRIMARY metric for DIA community benchmark
PSM count @ 1% FDR       ← PRIMARY metric for DDA community benchmark
Peptide count             ← secondary for both
Protein count             ← contextual only, never used for ranking
```

Protein count is NOT the primary metric. This is an intentional and important design
decision that differentiates STAN from ProteoScape and every other QC tool. Do not
promote proteins to primary metric status anywhere in the UI, API, or docs.

### Community benchmark tracks

- Track A = DDA, Sage search, PSM count primary
- Track B = DIA, DIA-NN search, precursor count primary
- Track C = both submitted from same instrument within 24h → unlocks radar fingerprint
- Tracks are completely separate leaderboards — never mix DDA and DIA metrics

### Search standardization

All community benchmark submissions use the **community-standardized search** with
pinned FASTA + library from the HF Dataset repo. This is what makes cross-lab
comparisons valid. The frozen parameters are defined in:
- DIA: `stan/search/community_params.py` → `COMMUNITY_DIANN_PARAMS`
- DDA: `stan/search/community_params.py` → `COMMUNITY_SAGE_PARAMS`

Do not change these without updating the version tag and migrating old submissions.

### Instrument config hot-reload

`instruments.yml` must be hot-reloaded without restarting the watcher daemon.
The watcher polls for config changes every 30 seconds using file mtime.

### File stability detection — vendor-specific

- Bruker `.d`: directory size check every 10s, trigger after `stable_secs` (default 60s)
  consecutive checks with no size change. The `.d` is a directory, not a file.
- Thermo `.raw`: single binary file, check mtime + size, trigger after `stable_secs`
  (default 30s). File handle is closed at acquisition end.

Do not swap these — they're different because the vendors handle file writing differently.

### SPD resolution chain (v0.2.188+)

`spd` (samples per day) is the cohort key for Trends / community
benchmarks. Never hardcode SPD extraction — always go through
`InstrumentWatcher._resolve_spd(raw_path)` on ingest OR
`validate_spd_from_metadata(raw_path)` in backfills. Both apply
the same layered fallback:

1. **Bruker `.d` method XML** — `_bruker_spd_from_xml()` reads
   the HyStar_LC `<name>` element from `<N>.m/submethods.xml`
   (UTF-8) or the `HyStar_LC_Method_Name` property from
   `SampleInfo.xml` (UTF-16). Parses labels like
   `"100 samples per day"` → 100. This is authoritative when
   present because it's what the operator actually loaded in
   HyStar. Works even with cryptic PAC method names like
   `DIA_Bps_11x3-k07t13Ra85.proteoscape.m`.

2. **Bruker TDF `GlobalMetadata.MethodName`** — pattern-match
   via `_spd_from_method_string()` (e.g. `100 SPD`, `Whisper40`,
   `30spd`). Weaker than XML because the method name is
   user-defined and often inconsistent, but fills gaps when
   the XML is missing.

3. **Bruker TDF `Frames.Time`** — compute gradient length from
   first/last frame timestamps, snap to nearest known SPD via
   `gradient_min_to_spd()`. Thermo `.raw` gets this path via
   `fisher_py` InstrumentMethod or `stan.tools.trfp` metadata.

4. **`instruments.yml` cohort default** — `spd:` field on the
   instrument block. Last-resort fallback when raw-file reading
   fails (e.g. network issue, corrupt .d). Do NOT rely on this
   as the primary source — it's a blanket that stamps every run
   with the same value, which bucket-mixes cohorts when an
   operator switches gradients mid-day.

5. **Filename regex** — `(\d+)[\s_-]*spd` catches inline tokens
   like `60spd`, `60-spd`, `60 SPD`, `100SPD`. Ordered LAST so
   a mistyped filename can't override real metadata.

6. **NULL** — Trends panel renders as "SPD unknown" and the
   community benchmark treats the row as unusable for cohort
   stats.

**Backfill old NULL rows** with `stan fix-spds` — walks the
`runs` table, re-reads each raw file, and updates `spd` where
the chain now produces a definitive answer. Idempotent; safe to
re-run.

**Adding a new Evosep gradient**: extend `_EVOSEP_METHOD_PATTERNS`
in `stan/metrics/scoring.py` AND `GRADIENT_TO_SPD` snapping table,
and add a regression test against a real `.d` method XML in
`tests/fixtures/`.

**Never pull SPD from cohort default alone** in new code paths.
The real-time watcher did this pre-v0.2.188 and left 58 timsTOF
runs NULL despite their filenames containing `60spd` — the XML
lookup would have caught them all.

### GRS score (0–100)

Gradient Reproducibility Score — a single composite number for LC health:
```
GRS = 40 × shape_r_scaled + 25 × auc_scaled + 20 × peak_rt_scaled + 15 × carryover_scaled
```
Stored in SQLite for every run. Shown as a badge on the dashboard. This is the number
a core facility staff member quotes when telling a PI about run quality.

### Privacy — hard rules

- Raw files are NEVER uploaded anywhere
- Patient/sample metadata is NEVER collected
- The community benchmark collects aggregate metrics only
- Serial numbers (optional) are stored server-side but never exposed in API/downloads
- CC BY 4.0 on the community dataset

---

## Infrastructure

### Three repositories

| Repo | URL | Purpose |
|------|-----|---------|
| GitHub code | https://github.com/bsphinney/stan | Application code, CI, spec |
| HF Space | https://huggingface.co/spaces/brettsp/stan | Public community dashboard |
| HF Dataset | https://huggingface.co/datasets/brettsp/stan-benchmark | Community benchmark data |

### HPC: Hive (UC Davis)

- Host: `hive.hpc.ucdavis.edu` (user `brettsp`, SSH alias `hive`)
- Scheduler: SLURM
- DIA-NN, Sage, 4DFF, etc. run as SLURM batch jobs
- SQLite database lives on Hive scratch/project storage
- Dashboard API can be SSH-tunneled to local machine
- Full context doc at `/Users/brettphinney/Documents/claude_private/HIVE_CLAUDE_GUIDE.md`
  (read at session start for partition/QOS/path details)

**Hive rules of engagement — violate at your peril:**

1. **Never run compute on the login node (`login1`)**. CPU/memory-heavy
   work gets flagged. Always use `sbatch` for real work or `srun --pty`
   for interactive. Brett caught me running `uff-cmdline2` directly on
   login1 on 2026-04-24 — stop, `pkill`, resubmit as a SLURM job.

2. **Never use `~/` or `/home/brettsp/` for large artifacts** — the home
   quota is tight and others can't see it. All shared binaries, FASTA
   files, analysis outputs, and generated `.features` live under
   `/quobyte/proteomics-grp/...`. Brett's personal scratch dir is
   `/quobyte/proteomics-grp/brett/` — writable + visible to the lab.

3. **SLURM commands need module environment loaded**. Non-interactive
   `ssh hive "sbatch ..."` won't find `sbatch` on PATH. Either:
   - `ssh hive "bash -l -c 'sbatch ...'"` (login shell), or
   - `ssh hive "source /etc/profile.d/modules.sh && source
     /etc/profile.d/hpccf.sh && sbatch ..."`

4. **Partitions + QOS** (summary; see the private guide for detail):
   | Partition | QOS | Use |
   |---|---|---|
   | `high` | `genome-center-grp-high-qos` | Priority CPU; 64-CPU per-user cap |
   | `gpu-a100` | `genome-center-grp-gpu-a100-qos` | 1 A100, use for Casanovo inference/training |
   | `low` | `publicgrp-low-qos` | Preemptible, huge capacity. Fine for fast (<30 min) jobs. `Requeue=1` recommended. |

   When `high` shows `(QOSGrpCpuLimit)` as the reason, fall back to `low`
   — different quota, usually works.

5. **Check queue state** with:
   ```bash
   squeue -u brettsp -o '%.10i %.12j %.9P %.2t %.10M %.6C %.8m %R'
   ```
   Look for the REASON column — `(None)` means just waiting for scheduler,
   `(QOSGrpCpuLimit)` / `(QOSGrpGRES)` mean quota is capped.

6. **SSH ControlMaster** speeds up repeated invocations:
   ```bash
   ssh -o ControlMaster=auto -o ControlPath=/tmp/.stan_brettsp_hive \
       -o ControlPersist=300 brettsp@hive.hpc.ucdavis.edu "<cmd>"
   ```
   macOS socket path must be ≤104 bytes — keep `ControlPath` under
   `/tmp/` not `/Users/.../...`.

**Bruker 4DFF on Hive** (v0.2.200+):
- Binary: `/quobyte/proteomics-grp/brett/bruker_ff/linux/uff-cmdline2`
- `LD_LIBRARY_PATH` must include that dir (for `libtbb.so.2`)
- STAN's `_install_dir()` in `stan/metrics/features.py` respects the
  `STAN_BRUKER_FF_DIR` env var — set it to the shared path on Hive
  so `stan install-4dff` doesn't fill up the home directory:
  `export STAN_BRUKER_FF_DIR=/quobyte/proteomics-grp/brett/bruker_ff`

---

## Autonomous troubleshooting (CRITICAL)

**When Brett reports a problem, DO NOT ask him to run diagnostic commands on the instrument PC. Read the Hive mirror first.** The instrument PCs sync their state to Hive continuously; treat the mirror as the authoritative source of truth for instrument-side state.

### Hive mirror layout

Root: `/Volumes/proteomics-grp/STAN/` (already mounted and writable on Brett's dev box).

Each instrument has its own subdirectory keyed by hostname:
- `TIMS-10878/` — timsTOF HT
- `DESKTOP-FOT3DAA/` — Exploris 480
- `lumosRox/` — Lumos

Inside each:
- `stan.db` — full SQLite mirror (copy locally with `cp` then query with `sqlite3`; direct query over the mount hits permission errors)
- `instruments.yml`, `community.yml`, `config/` — current instrument config
- `logs/` — every backfill + watch_status + submit log, sorted by timestamp:
  - `watch_status_YYYYMMDD_HHMMSS.log` — disk-vs-DB diff (columns: mtime, qc_match y/-, in_runs, in_sample_health, filename). Every file on disk is listed with its routing decision.
  - `backfill_metrics_YYYYMMDD_HHMMSS.log` — metrics backfill summary
  - `backfill_tic_YYYYMMDD_HHMMSS.log` — TIC backfill summary + skip-reason histogram (v0.2.152+)
  - `backfill_peg_YYYYMMDD_HHMMSS.jsonl` — per-file PEG results
  - `backfill_drift_YYYYMMDD_HHMMSS.jsonl` — per-file drift results and errors
  - `submit_all_YYYYMMDD.jsonl` — community submission log
- `status.json`, `failures/` — recent daemon state, per-job search failure logs
- `instrument_library.parquet` — per-instrument reference library

### Diagnosis protocol

When Brett reports an issue, in order:

1. **Check the mirror for the most recent relevant log**
   ```bash
   ls -lat /Volumes/proteomics-grp/STAN/<INSTRUMENT>/logs/ | head -20
   ```
2. **Grep the log for the file/run/error keyword Brett mentioned**
3. **For DB-state questions, copy stan.db locally first (permission quirk) then query**:
   ```bash
   cp /Volumes/proteomics-grp/STAN/<INSTRUMENT>/stan.db /tmp/claude/x.db
   sqlite3 /tmp/claude/x.db "SELECT ..."
   ```
4. **For raw-file questions on the original `.d`/`.raw`, SSH to Hive** (`ssh hive`) — the raw files live under `/quobyte/proteomics-grp/hela_qcs/<instrument>/` or `/quobyte/proteomics-grp/brett/stan_debug/` (recent troubleshooting files).
5. **Only escalate back to Brett** when the answer genuinely isn't in the mirror — e.g., "is the watcher process running right now", "what does the cmd console say". Don't ask him to relay data that's already synced.

### Ensure new code writes syncable output

Any new CLI command, backfill, or background job that could fail silently MUST:
- Write a log file to `~/STAN/logs/<command>_<timestamp>.{log,jsonl}` with per-step status and a summary at the end
- Call `sync_to_hive_mirror(include_reports=False)` after the log is written (not during — avoid syncing partial state)
- Log errors at `logger.warning` or `logger.error` minimum — DEBUG is stripped from syncs

**Past sync gaps that wasted cycles**:
- v0.2.151 `backfill-tic` printed to console only — skip-reason histogram was unreachable from Hive until v0.2.152 added the log file
- `stan watch` stderr isn't captured to a syncing file; watcher crashes are invisible to remote debugging. TODO: route watcher logs through `~/STAN/logs/watch_<ts>.log` so we can see cascade bugs or observer deaths without asking Brett to screenshot his cmd window.

### When you cannot reach the mirror

If `/Volumes/proteomics-grp/STAN/` isn't mounted (uncommon, but happens after reboot), use `ssh hive "cat /quobyte/proteomics-grp/STAN/<INSTRUMENT>/logs/<file>"` — the same files are accessible server-side.

### Config file locations

- `~/.stan/instruments.yml` — instrument watch directories
- `~/.stan/thresholds.yml` — QC thresholds (falls back to `config/thresholds.yml`)
- `~/.stan/community.yml` — HF token and submission preferences
- `~/.stan/stan.db` — SQLite database (or path configured in instruments.yml)

---

## Development Workflow

### Test fixtures (for development without real instruments)

Use the test fixtures in `tests/fixtures/`:
- `tests/fixtures/mock_d/` — minimal valid `.d` directory structure with tiny `analysis.tdf`
- `tests/fixtures/mock_raw/` — placeholder `.raw` file for path-based tests
- `tests/fixtures/report.parquet` — small real DIA-NN report for metric extraction tests
- `tests/fixtures/sage_results/` — small Sage output for DDA metric extraction tests

### Adding a new instrument model

1. Add entry to `config/thresholds.yml` with model-specific thresholds
2. Add model to `config/instruments.yml` example
3. Add model to the `instrument_model` enum in the community submission schema
4. Update the reference range table in `STAN_MASTER_SPEC.md` appendix

### Adding a new QC metric

1. Implement extraction in `stan/metrics/extractor.py`
2. Add column to SQLite schema in `stan/dashboard/server.py` (with migration)
3. Add field to the HF Dataset parquet schema in `stan/community/submit.py`
4. Add to the dashboard UI in `stan/dashboard/src/`
5. Update `STAN_MASTER_SPEC.md` metric tables

---

## Common Mistakes to Avoid

### DIA-NN

- **Do not assume column names** — DIA-NN column names have changed between versions.
  Always verify against current docs. Key columns to double-check:
  `Precursor.Id`, `Stripped.Sequence`, `Protein.Group`, `Q.Value`, `PG.Q.Value`,
  `Fragment.Info`, `Fragment.Quant.Corrected`, `Precursor.Normalised`, `File.Name`
- **`File.Name` vs `Run`** — DIA-NN 1.x used `Run`, DIA-NN 2.x uses `File.Name`.
  Check which version is being targeted before writing column references.
- **Library format** — DIA-NN 2.x uses `.predicted.speclib` (binary) for predicted
  libraries and `.parquet` for empirical libraries. Do not assume `.tsv` library format.
- **Linux requires .NET** — the Linux DIA-NN binary requires .NET SDK 8.0.407+.
  The SLURM job script must load the correct module or use a container.
- **`--lib` vs `--use-quant`** — these flags have different behaviors. Check the wiki.

### Sage

- **Input formats — what needs conversion and what doesn't:**

  | Raw format | Conversion needed? | Notes |
  |------------|-------------------|-------|
  | Bruker `.d` (ddaPASEF) | **No** | Sage reads `.d` natively — confirmed working in production at UC Davis |
  | Thermo `.raw` (DDA) | **Yes** | Sage does not read `.raw` — must convert via ThermoRawFileParser → mzML |

  The Sage release notes label `.d` support as "preliminary/unstable" but it works
  reliably in practice for ddaPASEF DDA QC runs. Do not add a timsrust/mzML
  conversion step for Bruker — pass `.d` paths directly to Sage.

  ThermoRawFileParser → mzML is only needed for Thermo DDA (Orbitrap `.raw` files).
  See the "Thermo `.raw` files — conversion to mzML" section above for confirmed flags.

- **Config is JSON** — Sage uses a JSON config file, not CLI flags for search params.
  The JSON schema has changed between versions. Always check the current README at
  https://github.com/lazear/sage before writing or modifying any config file.
- **Output files** — Sage outputs `results.sage.parquet` (PSMs) and `lfq.parquet`
  (label-free quant). Column names change between versions — always verify against
  the current release notes before writing any parsing code.
- **Built-in LDA vs Percolator** — Sage has built-in LDA rescoring that is
  comparable to Percolator for most use cases. Check current Sage docs before
  deciding whether to add a Percolator step. Do not assume Percolator is required —
  it may add complexity without meaningful benefit for QC-level FDR estimation.
- **`target_fdr` in config** — this controls Sage's internal FDR. Verify the exact
  key name in the current JSON schema docs before using.

### Bruker `.d` files

- **`.d` is a directory** — not a single file. Stability detection must check
  directory total size, not file mtime.
- **`analysis.tdf`** — SQLite database inside the `.d` directory. The `Frames` table
  has a `MsmsType` column. Known values: 0=MS1, 8=ddaPASEF, 9=diaPASEF.
  Verify these values against current Bruker TDF documentation — they could change.
- **`analysis.tdf_bin`** — binary frame data. Do not try to parse this directly.
  Use timsrust or alphatims for frame-level data access.

### Thermo `.raw` files — conversion to mzML

This is one of the most nuanced areas of the pipeline. Read this section carefully.

#### The two-track conversion situation

**DIA search (DIA-NN, Track B):**
DIA-NN 2.1+ has native `.raw` support on Linux — no conversion needed. Pass `.raw`
directly to DIA-NN with `--f file.raw`. This is confirmed in the DIA-NN 2.1.0 release
notes: "Built-in support for Thermo .raw on both Windows and Linux."

However, there is a known issue (#1468 on the DIA-NN repo) where native `.raw` reading
can fail in some Singularity/Apptainer containers. **Always implement mzML conversion
as a fallback** and make it configurable per-instrument in `instruments.yml`:

```yaml
- name: "Astral"
  vendor: "thermo"
  raw_handling: "native"       # "native" | "convert_mzml"
  # native = pass .raw directly to DIA-NN 2.1+
  # convert_mzml = run ThermoRawFileParser first, then pass mzML
```

**DDA search (Sage, Track A):**
Sage accepts mzML only — `.raw` conversion is always required, no exceptions.
Every DDA pipeline on Hive must run ThermoRawFileParser before Sage.

#### ThermoRawFileParser — confirmed CLI flags

Source: https://github.com/compomics/ThermoRawFileParser (latest: v1.4.4, May 2024)
Requires: .NET 8 runtime on Linux (`dotnet ThermoRawFileParser.dll`)
Alternative: Mono (`mono ThermoRawFileParser.exe`) — install `mono-complete`

```bash
# Convert single .raw to indexed mzML (recommended — indexed mzML is faster to seek)
dotnet ThermoRawFileParser.dll \
  -i=/path/to/file.raw \
  -o=/path/to/output_dir/ \
  -f=2 \
  -m=0

# Key flags (verified from primary source):
# -i, --input=VALUE          Input .raw file (required, use -i= format)
# -d, --input_directory=VALUE  Directory of .raw files (use instead of -i for batches)
# -o, --output=VALUE         Output directory (use -o= format)
# -b, --output_file=VALUE    Output file path (use instead of -o for single file)
# -f, --format=VALUE         Output format:
#                              0 = MGF
#                              1 = mzML
#                              2 = indexed mzML  ← use this for Sage
#                              3 = Parquet
#                              4 = no spectra output (metadata only)
# -m, --metadata=VALUE       Metadata output:
#                              0 = JSON  ← use this for acquisition mode detection
#                              1 = TXT
# -p                         Disable Thermo native peak picking (keep default ON)
# -g                         gzip compress output (adds .gz extension)

# IMPORTANT: flags use = sign format, not space:
# CORRECT:   -i=/path/to/file.raw
# INCORRECT: -i /path/to/file.raw
```

#### Using ThermoRawFileParser for acquisition mode detection

Before running any search, STAN must know if a `.raw` file is DIA or DDA.
The JSON metadata output (`-m=0 -f=4`) is the right approach — it produces
a JSON file with scan-level metadata including filter strings, without
writing the full spectral data:

```bash
# Extract metadata only (fast — no spectra written)
dotnet ThermoRawFileParser.dll \
  -i=/path/to/file.raw \
  -b=/path/to/file_metadata.json \
  -f=4 \
  -m=0
```

Parse the resulting JSON for `ScanFilter` strings:
- Contains `"DIA"` → DIA acquisition → Track B (DIA-NN)
- Contains `"dd-MS2"` or `"Full ms2"` → DDA → Track A (Sage + conversion)

**Important:** ScanFilter string formats vary across instrument models and firmware
versions. Always test on real `.raw` files from each specific instrument in the lab.
Do not hardcode string matching — use pattern matching and log any unrecognized formats.

#### mzML conversion as a SLURM step

On Hive, conversion runs as the first step of the search SLURM job, not as a
separate job. This avoids job scheduling overhead for what is typically a fast
operation (a 1h QC run converts in ~2–5 minutes).

```python
# stan/search/convert.py

def build_thermo_conversion_script(
    raw_path: Path,
    output_dir: Path,
    trfp_dll_path: Path,
) -> str:
    """
    Build bash commands to convert .raw to indexed mzML.
    trfp_dll_path: path to ThermoRawFileParser.dll on Hive
    Returns shell command string to embed in SLURM script.
    """
    mzml_path = output_dir / (raw_path.stem + ".mzML")
    return (
        f"dotnet {trfp_dll_path} "
        f"-i={raw_path} "
        f"-o={output_dir}/ "
        f"-f=2 "
        f"-m=0\n"
        f"# Converted: {raw_path.name} → {mzml_path.name}\n"
    )
```

Add `trfp_dll_path` to `instruments.yml` and `config/thresholds.yml`:

```yaml
# In instruments.yml, under each Thermo instrument:
trfp_path: "/hive/software/ThermoRawFileParser/ThermoRawFileParser.dll"
raw_handling: "convert_mzml"   # or "native" for DIA-NN 2.1+ direct .raw
```

#### Storage budget for mzML files

A typical 1h Orbitrap QC run:
- `.raw` file: ~2–4 GB
- mzML (uncompressed, indexed): ~3–6 GB (larger than raw due to XML overhead)
- mzML (gzip, `-g` flag): ~1–2 GB

For a QC-only tool, delete converted mzML files after the search completes.
Add a cleanup step at the end of the SLURM script:

```bash
# At end of SLURM job script:
echo "Cleaning up converted mzML..."
rm -f "${OUTPUT_DIR}/${RUN_NAME}.mzML"
```

Make cleanup configurable (`keep_mzml: false` in `instruments.yml`) since some
users may want to keep mzML files for downstream use.

#### Do NOT use MSConvert

MSConvert (ProteoWizard) is the other common `.raw` conversion tool but requires
a Windows license for vendor libraries when used on Linux. ThermoRawFileParser is
fully open-source, Linux-native, and produces equivalent output for proteomics use.
Do not introduce MSConvert as a dependency.

### Polars

- **API changes frequently** — Polars had major API changes at 0.19→0.20 and continues
  evolving. Always check https://docs.pola.rs/ for current expression syntax.
- **`map_elements` vs `apply`** — older versions used `.apply()`, newer versions use
  `.map_elements()`. Check current docs.
- **Lazy vs eager** — prefer lazy (`pl.scan_parquet`) for large files, eager
  (`pl.read_parquet`) for small ones. Always specify `columns=` to limit reads.

### HuggingFace Hub

- **API changes** — `huggingface_hub` Python package API changes frequently.
  Check https://huggingface.co/docs/huggingface_hub/en/index before any Hub operations.
- **Rate limits** — the HF Dataset API has rate limits. The nightly consolidation
  script should batch all reads, not iterate one file at a time in a hot loop.
- **Upload method** — use `api.upload_file()` for single files, `api.upload_folder()`
  for directories. Check current docs for correct parameter names.

---

## Dashboard: Ion Cloud View (v0.2.192+)

The drift "Ion cloud" tab has two render modes that switch automatically
depending on whether a 4DFF `.features` file exists next to the raw `.d`:

- **Plotly per-charge scatter** (`DriftCloudPlotly` in `public/index.html`)
  is the preferred view. It fetches `/api/runs/{run_id}/features-by-charge`
  which reads the `LcTimsMsFeature` table directly with a raw `sqlite3`
  connection — **never import from `stan.metrics.features` here**. One
  trace per charge state, Ziggy palette (`+2` blue, `+1` teal, `+3` green,
  `+4` orange, `+5` purple, `+6` red, unassigned yellow). DIA windows are
  overlaid as rectangles grouped by `window_group` with an 8-color palette
  cycled modulo the group count. Click the legend entries to toggle charges.
- **Legacy SVG cloud** (`DriftCloudSvg`) is the fallback when no `.features`
  exists. The friendly stub tells the user to run `stan run-4dff <path>`
  to unlock the richer view.

Plotly is loaded from `cdn.plot.ly/plotly-2.35.2.min.js` — pure client-side,
same CDN pattern as React + Babel. No build step needed. If the CDN is
unreachable the Plotly component renders a "failed to load" note and the
SVG fallback still works from its own code path.

---

## Implementation Rules

**Never guess what data is available — always read the code first.**

Before implementing any new feature or metric, read the actual extractor functions
(`stan/metrics/extractor.py`) and search engine output schemas to confirm what columns
and values are available. Do not assume a field exists because it "should" be there.
The GRS score was designed with components (TIC reference, carryover from blanks) that
could never actually be computed from the data STAN collects. This wasted time.

**Verify end-to-end before shipping:** trace the data path from raw file → search →
extraction → DB → dashboard to confirm every value is actually populated.

---

## Deployment & Versioning

- **Always bump both** `pyproject.toml` AND `stan/__init__.py` on every push — instrument PCs
  verify code freshness via `stan version`. Never update one without the other.
- **Always push to GitHub** after changes — Brett deploys to instrument PCs via `update-stan.bat`
  which downloads from `github.com/bsphinney/stan/archive/refs/heads/main.zip`.
- Baseline Builder has its own version banner (e.g. "v3") — bump it when baseline behavior changes.

---

## PowerShell 5.1 Compatibility (instrument PCs)

Instrument PCs run Windows with PowerShell 5.1. When editing `.ps1` files:
- **Always rewrite the entire file** — never patch individual lines (subtle parsing traps)
- **Never use `+` string concatenation** — use `"$var;$var"` interpolation instead
- **No inline ternary `if`** — use separate `if`/`else` blocks
- **No `Where-Object { }` pipelines** — use explicit `foreach` loops
- **Use `Join-Path`** instead of string concatenation for paths

---

## DIA-NN 2.0 Column Changes

DIA-NN 2.0 changed the report.parquet schema. The extractor must handle both 1.x and 2.0:
- `File.Name` (full path) → renamed to `Run` (basename only)
- `Fragment.Info`, `Fragment.Quant.Corrected` → removed
- `Missed.Cleavages` → may be absent
- Always check `if col in available` / `if col in filt.columns` before using any column

---

## Instrument PC Constraints

- **Half CPU cores** for DIA-NN/Sage — `max(2, cpu_count // 2)`. These are instrument
  workstations that may be acquiring data simultaneously.
- **No library-free DIA-NN** — too slow for QC, produces non-comparable community results.
  Always require a spectral library; raise ValueError if none provided.
- **Dual venv installs** — old `.stan\venv` and new `STAN\venv` may both exist on PATH.
  The updater must detect and migrate, removing old PATH entries.
- **thresholds.yml may not exist** — `load_thresholds()` must not crash; default to PASS.

---

## Community Submission Architecture

Community benchmark submissions go through the HF Space relay — **no HF token required**.
- Client (`stan/community/submit.py`) POSTs JSON to `https://brettsp-stan.hf.space/api/submit`
- Relay has `HF_TOKEN` secret, handles auth + parquet upload to `brettsp/stan-benchmark`
- Never re-introduce HF token requirements in client-side code
- `tests/test_pipeline.py` has 7 tests that catch version desync, schema mismatch, and token regressions

---

## Documentation Maintenance

**When you implement a new feature or complete a TODO item, update ALL of the following:**

1. Check `README.md` — move the item from the TODO list to the Implementation Status table.
   Remove any **(planned)** markers from the feature description.
2. Check `docs/user_guide.md` — remove **(planned)** markers and update instructions
   to reflect actual working behavior.
3. If the feature changes config format, update the YAML examples in both files.
4. If the feature adds a new CLI command, add it to the Quick Start and user guide.

The README has an [Implementation Status](#implementation-status) table and a [TODO](#todo)
checklist. These are the source of truth for what works vs what's planned. Keep them current.

5. Check the **HF Space relay API** (`app.py` on `brettsp/stan`) — if schemas, field names,
   or metrics changed, the relay must be updated and redeployed. The submission schema in
   the relay MUST match the client-side submission code in `stan/community/submit.py`.
6. Check the **HF Space dashboard HTML** — if metrics are renamed, added, or removed,
   update the figures, table columns, reference range cards, and info cards on the dashboard.

---

## Code Style

- Python 3.10+, type hints everywhere
- Ruff for linting (`ruff check stan/`)
- Docstrings on all public functions (Google style)
- No print() — use `rich.console.Console` or Python `logging`
- All file paths as `pathlib.Path`, never raw strings
- All subprocess calls via `subprocess.run(..., check=True)` with explicit timeout
- All HF API calls wrapped in try/except with meaningful error messages
- All SQLite operations use context managers (`with sqlite3.connect(...) as con:`)

---

## Testing

```bash
pytest tests/ -v                        # run all tests
pytest tests/test_metrics.py -v        # metrics only
pytest tests/ -k "not integration"     # skip tests requiring Hive connection
```

Tests that require Hive SLURM or real instrument files are marked `@pytest.mark.integration`
and skipped in CI. They can be run manually on Hive.

Mock fixtures live in `tests/fixtures/`. When adding a test that parses DIA-NN or Sage
output, add a small real output file to fixtures rather than generating synthetic data —
synthetic data won't catch format changes between tool versions.

---

## CI/CD

### GitHub Actions

- `ci.yml` — runs on every push/PR: install, ruff lint, pytest (no integration tests)
- `consolidate_benchmark.yml` — runs nightly at 4am UTC: downloads HF Dataset
  submissions, validates, recomputes percentiles, writes `benchmark_latest.parquet`

### Required secrets (GitHub repo settings → Secrets)

- `HF_TOKEN` — Hugging Face token with write access to `brettsp/stan-benchmark`

---

## Links

| Resource | URL |
|----------|-----|
| STAN GitHub | https://github.com/bsphinney/stan |
| STAN HF Space | https://huggingface.co/spaces/brettsp/stan |
| STAN HF Dataset | https://huggingface.co/datasets/brettsp/stan-benchmark |
| DIA-NN GitHub | https://github.com/vdemichev/DiaNN |
| DIA-NN wiki | https://github.com/vdemichev/DiaNN/wiki |
| DIA-NN discussions | https://github.com/vdemichev/DiaNN/discussions |
| DIA-NN releases | https://github.com/vdemichev/DiaNN/releases |
| Sage GitHub | https://github.com/lazear/sage |
| Sage releases | https://github.com/lazear/sage/releases |
| Sage paper | https://pubs.acs.org/doi/10.1021/acs.jproteome.3c00486 |
| timsrust | https://github.com/MannLabs/timsrust |
| ThermoRawFileParser | https://github.com/compomics/ThermoRawFileParser |
| Percolator | https://github.com/percolator/percolator |
| alphatims (TDF schema) | https://github.com/MannLabs/alphatims |
| HF Hub Python docs | https://huggingface.co/docs/huggingface_hub/en/index |
| Polars docs | https://docs.pola.rs/ |
| FastAPI docs | https://fastapi.tiangolo.com/ |
| watchdog docs | https://python-watchdog.readthedocs.io/ |
| DE-LIMP (sibling project) | https://github.com/bsphinney/DE-LIMP |

### Public Astral HeLa DIA Datasets (PRIDE/ProteomeXchange)

These are publicly available datasets with Orbitrap Astral HeLa DIA data, useful for
library building, validation, and benchmarking reference ranges.

| Dataset | PXD ID | Description | Search SW | Notes |
|---------|--------|-------------|-----------|-------|
| Searle et al. 2023 | PXD042704 | Astral DIA benchmark, HeLa, multiple gradients | EncyclopeDIA | On Panorama Public, not PRIDE |
| Stewart et al. 2024 ("Inflection Point") | PXD054015 | Astral HeLa DIA + biofluids/tissues, 200 ng | DIA-NN v1.8.1 lib-free | Best candidate — HeLa + Astral + DIA-NN |
| "$10 Proteome" 2025 | PXD066701 | Astral + timsTOF Ultra 2 HeLa QC, 200 pg–10 ng | DIA-NN | Has DIA-NN pg_matrix TSVs + zip archives |
| Stewart et al. 2024 (DDA) | PXD045838 | Astral DDA HeLa, 125 ng | Mascot | DDA only — useful for Track A reference |

Papers:
- Searle: https://pubs.acs.org/doi/10.1021/acs.jproteome.3c00357
- Stewart: https://pubs.acs.org/doi/10.1021/acs.jproteome.4c00384
- Nat Biotech nDIA: https://www.nature.com/articles/s41587-023-02099-7

---

## Questions? Ambiguities?

If anything in the spec is unclear or contradictory, check `STAN_MASTER_SPEC.md` first.
If the spec doesn't resolve it, ask Brett before guessing. Wrong assumptions about
search engine flags or output formats will cause silent failures that are hard to debug.

The spec is the source of truth. This CLAUDE.md is the development context.
Primary source docs are the oracle for external tool behavior.

**When in doubt: fetch, don't guess.**
