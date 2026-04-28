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
pytest tests/ -k "test_ips"           # run a single test by name
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

**Key modules**: `watcher/` (file monitoring + mode detection), `search/` (DIA-NN/Sage local subprocess + SLURM dispatcher), `metrics/` (metric extraction + IPS scoring), `gating/` (threshold evaluation + HOLD flag), `community/` (HF Dataset submission/fetch), `dashboard/` (FastAPI + single-file React UI in public/index.html).

**Three external repos**: GitHub (code), HF Space (public dashboard), HF Dataset (community benchmark data).

---

## CRITICAL: Always Check Primary Sources

STAN depends on external tools (DIA-NN, Sage, timsrust, ThermoRawFileParser, Percolator)
whose CLIs, flags, and output formats change between versions. **Never guess, assume, or
rely on training knowledge** for these tools — fetch primary docs first.

**Full reference (tables, gotchas, container paths, CLI flags, version pins):**
[`docs/external_tools.md`](docs/external_tools.md)

Quick `web_fetch` examples:
```
web_fetch("https://raw.githubusercontent.com/vdemichev/DiaNN/master/README.md")
web_fetch("https://raw.githubusercontent.com/lazear/sage/master/README.md")
web_fetch("https://github.com/vdemichev/DiaNN/releases/latest")
web_fetch("https://github.com/lazear/sage/releases/latest")
```

If a flag/column isn't in the primary source, add `# TODO: verify against vX.X` and tell Brett.

**The single most-misused fact:** on Hive, DIA-NN container with `.raw` support is
`/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif` (underscore). The lookalike
`/quobyte/proteomics-grp/apptainers/diann2.3.0.sif` (no underscore) silently skips `.raw`
files. Binary inside is `/diann-2.3.0/diann-linux`, not `diann`. Details in `docs/external_tools.md`.

---

## Repository Layout

```
stan/
├── CLAUDE.md                    ← you are here
├── STAN_MASTER_SPEC.md          ← authoritative design document, read before coding
├── pyproject.toml
├── README.md
├── stan/
│   ├── cli.py                   # `stan` CLI (typer)
│   ├── watcher/                 # watchdog daemon, file stability, mode detection
│   ├── search/                  # DIA-NN + Sage job builders (local subprocess + SLURM dispatcher)
│   ├── metrics/                 # metric extraction, IPS, iRT, drift, PEG, features
│   ├── gating/                  # threshold evaluation, HOLD flag, queue control
│   ├── community/               # HF Dataset submit/fetch/validate
│   └── dashboard/               # FastAPI backend + single-file React UI in public/index.html

# Runtime YAMLs live under ~/.stan/ (instruments.yml, thresholds.yml,
# community.yml, fleet.yml). They are not checked into the repo.
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
- DIA: `stan/search/community_params.py` → `COMMUNITY_DIANN_PARAMS_FROZEN`
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

### IPS score (0–100)

Instrument Performance Score — a cohort-calibrated depth score:
```
DIA: IPS = 0.50 × s_precursors + 0.30 × s_peptides + 0.20 × s_proteins
DDA: IPS = 0.50 × s_psms       + 0.30 × s_peptides + 0.20 × s_proteins
```
Each `s_*` is a piecewise-linear percentile against an
`(instrument_family, SPD_bucket)` reference cohort. Stored as
`runs.ips_score` in SQLite, included in every community submission,
and shown as the IPS badge on the dashboard. Implementation:
`stan/metrics/chromatography.py`. Full rationale: `docs/ips_metric.md`.

The previous GRS (Gradient Reproducibility Score) was retired — it
required components (TIC reference, blank carryover) that STAN does
not collect, so it was replaced by IPS.

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

### Test fixtures

`tests/fixtures/` is intentionally empty. Mock `.d` / `.raw` paths and
search-output samples are constructed by `tests/conftest.py` per-test —
add new fixture builders there rather than committing binary artifacts.

### Adding a new instrument model

1. Add entry to `~/.stan/thresholds.yml` (or the in-tree fallback in
   `stan/config/thresholds.yml` if you ship a default) with
   model-specific thresholds
2. Add model to `~/.stan/instruments.yml` example
3. Add model to the `instrument_model` enum in the community submission schema
4. Update the reference range table in `STAN_MASTER_SPEC.md` appendix

### Adding a new QC metric

1. Implement extraction in `stan/metrics/extractor.py`
2. Add column to SQLite schema in `stan/dashboard/server.py` (with migration)
3. Add field to the HF Dataset parquet schema in `stan/community/submit.py`
4. Add to the dashboard UI in `stan/dashboard/public/index.html`
5. Update `STAN_MASTER_SPEC.md` metric tables

---

## Common Mistakes to Avoid

DIA-NN, Sage, ThermoRawFileParser, Polars, HF Hub gotchas + Thermo `.raw` → mzML
conversion details live in [`docs/external_tools.md`](docs/external_tools.md).

**STAN-specific reminders that aren't in those docs:**

- **DIA-NN 2.0 column changes** — extractor must handle both 1.x and 2.0:
  `File.Name` (full path) replaces `Run` (basename); `Fragment.Info`, `Fragment.Quant.Corrected`,
  `Missed.Cleavages` may be absent. Always check `if col in df.columns` before access.
- **Bruker `.d` is a directory**, not a file — stability detection must check total
  directory size, not file mtime.
- **`analysis.tdf.Frames.MsmsType`**: 0=MS1, 8=ddaPASEF, 9=diaPASEF (verify against
  current Bruker docs — could change).
- **Thermo conversion routing**: DIA-NN 2.1+ reads `.raw` natively (no conversion);
  Sage always needs ThermoRawFileParser → mzML for `.raw`. `instruments.yml` has
  `raw_handling: "native" | "convert_mzml"` per Thermo instrument as a fallback toggle.

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
The original GRS score (now retired in favor of IPS) was designed with components
(TIC reference, blank carryover) that STAN could never actually measure. Don't repeat
that mistake — confirm every input field exists in the extractor before specifying
a composite metric.

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

When adding a test that parses DIA-NN or Sage output, add a small real
output file under `tests/fixtures/` (currently empty — commit artifacts
as you need them) rather than generating synthetic data. Synthetic data
won't catch format changes between tool versions.

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

External-tool URLs and public Astral HeLa benchmark datasets:
[`docs/external_tools.md`](docs/external_tools.md). STAN's own repos:

- GitHub: https://github.com/bsphinney/stan
- HF Space: https://huggingface.co/spaces/brettsp/stan
- HF Dataset: https://huggingface.co/datasets/brettsp/stan-benchmark

---

## Questions? Ambiguities?

If anything in the spec is unclear or contradictory, check `STAN_MASTER_SPEC.md` first.
If the spec doesn't resolve it, ask Brett before guessing. Wrong assumptions about
search engine flags or output formats will cause silent failures that are hard to debug.

The spec is the source of truth. This CLAUDE.md is the development context.
Primary source docs are the oracle for external tool behavior.

**When in doubt: fetch, don't guess.**
