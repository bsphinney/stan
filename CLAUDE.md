# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Project**: STAN — Standardized proteomic Throughput ANalyzer  
> **Author**: Brett Stanley Phinney, UC Davis Proteomics Core  
> **GitHub**: https://github.com/bsphinney/stan  
> **HF Space**: https://huggingface.co/spaces/brettsp/stan  
> **HF Dataset**: https://huggingface.co/datasets/bsphinney/stan-community-benchmark  
> **Master spec**: `STAN_MASTER_SPEC.md` — read this first, it is the authoritative design doc  
> **Last updated**: April 2026

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
| HF Dataset | https://huggingface.co/datasets/bsphinney/stan-community-benchmark | Community benchmark data |

### HPC: Hive (UC Davis)

- Scheduler: SLURM
- Connection: SSH via `paramiko`
- DIA-NN and Sage run as SLURM batch jobs on Hive
- SQLite database lives on Hive scratch/project storage
- Dashboard API can be SSH-tunneled to local machine

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

## Documentation Maintenance

**When you implement a new feature or complete a TODO item, update the docs immediately.**

1. Check `README.md` — move the item from the TODO list to the Implementation Status table.
   Remove any **(planned)** markers from the feature description.
2. Check `docs/user_guide.md` — remove **(planned)** markers and update instructions
   to reflect actual working behavior.
3. If the feature changes config format, update the YAML examples in both files.
4. If the feature adds a new CLI command, add it to the Quick Start and user guide.

The README has an [Implementation Status](#implementation-status) table and a [TODO](#todo)
checklist. These are the source of truth for what works vs what's planned. Keep them current.

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

- `HF_TOKEN` — Hugging Face token with write access to `bsphinney/stan-community-benchmark`

---

## Links

| Resource | URL |
|----------|-----|
| STAN GitHub | https://github.com/bsphinney/stan |
| STAN HF Space | https://huggingface.co/spaces/brettsp/stan |
| STAN HF Dataset | https://huggingface.co/datasets/bsphinney/stan-community-benchmark |
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
