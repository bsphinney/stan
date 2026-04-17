# STAN -- Standardized proteomic Throughput ANalyzer

> *Know your instrument.*

[![License: STAN Academic](https://img.shields.io/badge/License-Academic-blue.svg)](LICENSE)
[![Dataset: CC BY 4.0](https://img.shields.io/badge/Data_License-CC_BY_4.0-green.svg)](https://creativecommons.org/licenses/by/4.0/)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

STAN is an open-source proteomics QC tool for Bruker timsTOF and Thermo Orbitrap mass spectrometers. It watches your raw data directories for new acquisitions, auto-detects DIA or DDA mode, runs standardized search jobs (DIA-NN for DIA, Sage for DDA) directly on your instrument workstation, computes instrument health metrics, gates your sample queue automatically on QC failure, tracks longitudinal performance in a local database, serves a dashboard, and optionally benchmarks your instrument against the global proteomics community through a crowdsourced HeLa digest dataset.

**No HPC cluster required.** STAN runs DIA-NN and Sage locally on the same machine as your instrument. For labs with SLURM cluster access, remote HPC execution is available as an option.

**Built at the UC Davis Proteomics Core by Brett Stanley Phinney.**

---

## Key Features

- **Multi-instrument monitoring** -- Bruker timsTOF and Thermo Orbitrap in a single dashboard
- **DIA and DDA mode intelligence** -- auto-detects acquisition mode and routes to the right search engine with the right metrics
- **Run and Done gating** -- automatically pauses your sample queue (HOLD flag) when a QC run fails thresholds
- **Instrument Performance Score (IPS)** -- a single 0-100 composite number for LC health, updated every run
- **Column health tracking** -- longitudinal TIC trend analysis detects column aging before it affects your data
- **Precursor-first metrics** -- benchmarks on precursor count (DIA) and PSM count (DDA), not protein count, because protein count is confounded by FASTA choice and inference settings
- **Community HeLa benchmark** -- compare your instrument against 967+ runs from labs worldwide at [community.stan-proteomics.org](https://community.stan-proteomics.org) (CC BY 4.0)
- **Zero-config raw file intelligence** -- STAN auto-extracts instrument model, serial number, LC system, gradient length, DIA window size, acquisition date, and DIA/DDA mode directly from `.raw` and `.d` files. The only thing you tell STAN is your column and HeLa amount.
- **Anonymous lab identity** -- fun pseudonyms ("Clogged PeakTail", "Caffeinated Quadrupole") with email verification so you can track your own data on the community site without revealing your lab
- **Gas-gauge dashboard** -- at-a-glance instrument health: 5 recent runs × 3 metrics, green/amber/red zones vs your instrument's own history
- **Instrument health fingerprint** -- dual-mode DDA+DIA radar chart for rapid visual diagnosis
- **Plain-English failure diagnosis** -- templated alerts explain what failed and what to check, no guesswork
- **Privacy by design** -- raw files are never uploaded; only aggregate QC metrics leave your lab

## Supported Instruments

| Vendor | Instruments | Raw Format | Acquisition Modes |
|--------|-------------|------------|-------------------|
| Bruker | timsTOF Ultra 2, Ultra, Pro 2, SCP | `.d` directory | diaPASEF, ddaPASEF |
| Thermo | Astral, Exploris 480, Exploris 240 | `.raw` file | DIA, DDA |

---

## Quick Start

### Windows (Recommended)

Download [**install-stan.bat**](https://raw.githubusercontent.com/bsphinney/stan/main/install-stan.bat) (right-click → Save As) and double-click it. This single script handles everything:

1. Installs Python 3.10+ if not present
2. Clones the STAN repository and installs it via pip
3. Auto-installs **DIA-NN** from GitHub releases (.msi for 2.x, with admin elevation if needed)
4. Auto-installs **Sage** from GitHub releases
5. Handles SSL/proxy issues automatically (common on UC Davis and other institutional networks)
6. Uses `--no-cache-dir` to ensure fresh code on every install

To update an existing install, use [**update-stan.bat**](https://raw.githubusercontent.com/bsphinney/stan/main/update-stan.bat) -- it downloads the latest code from GitHub and reinstalls. Both `.bat` files self-update by downloading their latest version from GitHub on each run.

> **Note:** The old `install_stan.bat` (underscore) was removed to avoid confusion. Only `install-stan.bat` (hyphen) and `update-stan.bat` exist now.

DIA-NN 2.x is preferred over 1.x when both are installed. If the installer cannot find or install a search engine, `stan setup` and `stan baseline` will prompt you for a custom executable path.

### Install from Source (Linux/macOS/Advanced)

```bash
git clone https://github.com/bsphinney/stan.git
cd stan
pip install -e ".[dev]"
```

You will also need DIA-NN and Sage installed and on your PATH. See the search engine sections below for download links.

### Install from PyPI (coming soon)

```bash
pip install stan-proteomics          # not yet published
```

### Initialize

```bash
stan init
```

Creates `~/.stan/` and copies default configuration templates into it.

Run `stan setup` for an interactive 6-question wizard that picks your instrument, directories, LC method, FASTA, and error telemetry preferences -- no YAML editing required. If your watch directory already has existing raw files, the wizard offers to run `stan baseline` at the end to process them retroactively. Or create the config files manually:
- `instruments.yml` -- instrument watch directories and settings
- `thresholds.yml` -- QC pass/warn/fail thresholds per instrument model
- `community.yml` -- HuggingFace token and community benchmark preferences

### Watch

```bash
stan watch
```

Starts the watcher daemon. It monitors directories configured in `instruments.yml`, detects new raw files, determines acquisition mode, and runs DIA-NN or Sage locally on your machine. **(Requires a working `instruments.yml` in `~/.stan/` -- see [Configuration](#configuration). DIA-NN and Sage must be installed and on your PATH.)**

### Dashboard

```bash
stan dashboard
```

Serves the FastAPI backend at [http://localhost:8421](http://localhost:8421). The API is fully functional -- browse `/docs` for Swagger UI. The dashboard includes a Config tab for managing instruments. **(The full React frontend is planned; the current UI is a basic HTML page with config management.)**

### Baseline Builder

```bash
stan baseline
```

Processes existing HeLa QC files retroactively -- ideal for building historical QC data from a directory of past runs. Features:

- Recursive discovery of `.d` and `.raw` files in subdirectories
- Auto-detects gradient length from raw files (Thermo via TRFP metadata, Bruker via `Frames.Time` in `analysis.tdf`)
- Auto-detects LC system from raw file metadata (U3000, Vanquish Neo, Evosep, etc.)
- Auto-downloads the community FASTA from the HF Dataset if not cached locally
- Pre-flight tests DIA-NN and Sage before processing (runs a quick test search to verify they work)
- If a search engine is not found or fails pre-flight, prompts for a custom executable path
- Prefers DIA-NN 2.x over 1.x when both are installed
- Resume support (tracks progress in `~/.stan/baseline_progress.json`)
- Duplicate detection (skips files already in the database)
- Scheduling options: run now, tonight (8 PM), or weekend (Saturday 8 AM)

### Other Commands

```bash
stan status           # show configuration and database summary
stan column-health    # assess LC column condition from longitudinal TIC trends
stan version          # print STAN version
```

---

## Architecture

```
Raw data directory (watched by watcher daemon)
        |
        |  file stable for stable_secs
        v
detector.py -- reads .d/analysis.tdf or .raw metadata
        |
        +-- DIA --> local DIA-NN --> report.parquet
        +-- DDA --> local Sage   --> results.sage.parquet
                         |
                 extractor.py + chromatography.py
                         |
                 evaluator.py --> PASS / WARN / FAIL
                         |              |
                    SQLite DB     queue.py (HOLD flag)
                         |
                 dashboard (FastAPI, port 8421)
                         |               (React frontend planned)
                 community/submit.py --> HF Dataset
```

**Data flow**: The watcher daemon detects new raw files and checks for file stability (size stops changing). Once stable, the detector reads instrument metadata to determine DIA or DDA mode. STAN runs DIA-NN (for DIA) or Sage (for DDA) **locally on your instrument workstation** as a subprocess with standardized parameters. After the search completes, STAN extracts QC metrics from the results, evaluates them against per-instrument thresholds, writes a HOLD flag if the run fails, stores everything in SQLite for longitudinal tracking, and optionally submits to the community benchmark.

**Execution modes:**
- **Local (default)** -- DIA-NN and Sage run as subprocesses on the same machine. Install them once, add to PATH, and STAN handles the rest. A typical QC HeLa run searches in 5-15 minutes on a modern workstation.
- **SLURM (optional)** -- For labs with HPC cluster access, set `execution_mode: "slurm"` in `instruments.yml`. STAN submits batch jobs via SSH/paramiko and polls for completion.

---

## QC Metric Hierarchy

STAN uses a deliberate metric hierarchy. This is a core design decision that differentiates STAN from other QC tools:

```
Fragment XICs / precursor    <-- purest instrument signal
Precursor count @ 1% FDR    <-- PRIMARY metric for DIA (Track B)
PSM count @ 1% FDR          <-- PRIMARY metric for DDA (Track A)
Peptide count                <-- secondary for both modes
Protein count                <-- contextual only, never used for ranking
```

Protein count is intentionally excluded from primary benchmarking. It is heavily confounded by FASTA database choice, protein inference algorithm, and FDR propagation settings. Precursor and PSM counts with a standardized search provide a much cleaner signal of instrument performance.

---

## Community Benchmark

STAN powers an open, crowdsourced HeLa digest benchmark hosted on HuggingFace. Labs worldwide submit aggregate QC metrics (never raw files) from their HeLa standard runs, enabling cross-lab instrument performance comparisons.

**Community submission is entirely opt-in.** By default, `community_submit` is `false` and nothing leaves your machine. STAN works fully standalone for local QC monitoring, gating, and longitudinal tracking without ever contacting an external service. Set `community_submit: true` per instrument only if you want to participate in the benchmark.

Browse the community dashboard: **[community.stan-proteomics.org](https://community.stan-proteomics.org)** (also at [huggingface.co/spaces/brettsp/stan](https://huggingface.co/spaces/brettsp/stan))

The community site is **live** with 967 runs across Fusion Lumos, timsTOF HT, and Exploris 480 instruments.

### How It Works

All community benchmark submissions use a **frozen, standardized search** with pinned FASTA, spectral libraries, and search parameters hosted in the HF Dataset repository. This is what makes cross-lab comparisons valid -- every lab searches the same library with the same settings, so differences in precursor counts reflect actual instrument performance, not search configuration.

### Benchmark Tracks

| Track | Mode | Search Engine | Primary Metric | Secondary Metrics |
|-------|------|---------------|----------------|-------------------|
| **Track A** | DDA | Sage | PSM count @ 1% FDR | Peptide count, mass accuracy, MS2 scan rate |
| **Track B** | DIA | DIA-NN | Precursor count @ 1% FDR | Peptide count, median CV, IPS |
| **Track C** | Both | Both | Instrument fingerprint | Radar chart (6 axes), peptide recovery ratio |

Track C unlocks when a lab submits both a DDA and a DIA run from the same instrument within 24 hours. The resulting six-axis radar chart provides a comprehensive instrument health fingerprint covering mass accuracy, duty cycle, spectral quality, precursor depth, quantitative reproducibility, and fragment sensitivity.

### Cohort Bucketing

Submissions are compared only within their cohort, defined by three dimensions: **instrument family**, **throughput (SPD)**, and **injection amount**. This ensures a 50 ng run on a timsTOF Ultra at 60 SPD is compared against other 50 ng timsTOF Ultra 60 SPD runs, not against a 500 ng Astral at 200 SPD.

**Throughput buckets (SPD -- samples per day):**

SPD is the primary throughput unit. Labs set their Evosep, Vanquish Neo, or equivalent method by SPD in `instruments.yml`. Gradient length in minutes is accepted as a fallback for custom LC methods.

| Bucket | SPD Range | Evosep Method | Traditional Equivalent |
|--------|-----------|---------------|----------------------|
| `200+spd` | 200 or more | 500/300/200 SPD | ~2-5 min gradient |
| `100spd` | 80-199 | 100 SPD | ~11 min gradient |
| `60spd` | 40-79 | 60 SPD (most popular), Whisper 40 | ~21-31 min gradient |
| `30spd` | 25-39 | 30 SPD | ~44 min gradient |
| `15spd` | 10-24 | Extended | ~60-88 min gradient |
| `deep` | under 10 | -- | >2h gradient |

**Amount buckets (injection amount in ng):**

| Bucket | Range | Typical Use |
|--------|-------|-------------|
| ultra-low | 25 ng or less | Single-cell, very low input |
| low | 26-75 ng | Standard QC (50 ng default) |
| mid | 76-150 ng | Moderate load |
| standard | 151-300 ng | Traditional 200-250 ng QC |
| high | 301-600 ng | High-load methods |
| very-high | over 600 ng | Specialized applications |

The default injection amount is **50 ng** and is configurable per instrument in `instruments.yml` via the `hela_amount_ng` field.

A minimum of 5 submissions per cohort is required before the leaderboard activates.

### Community Composite Scores

**DIA Score** (Track B):
```
DIA_Score = 40 x percentile_rank(n_precursors)
          + 25 x percentile_rank(n_peptides)
          + 20 x (100 - percentile_rank(median_cv_precursor))
          + 15 x percentile_rank(ips_score)
```

**DDA Score** (Track A):
```
DDA_Score = 35 x percentile_rank(n_psms)
          + 25 x percentile_rank(n_peptides_dda)
          + 20 x percentile_rank(pct_delta_mass_lt5ppm)
          + 20 x percentile_rank(ms2_scan_rate)
```

Scores are computed nightly within each cohort by a GitHub Actions workflow. A score of 75 means your instrument outperformed 75% of comparable submissions. **(Nightly consolidation is implemented but will not run until the HF Dataset has live submissions.)**

### Annual Community Awards **(planned)**

To encourage participation and make QC a point of pride (or healthy shame), STAN will recognize top and bottom performers each year:

| Award | Criteria | Prize |
|-------|----------|-------|
| Golden Spray Tip | Highest median community score across all cohorts, minimum 50 submissions | Trophy + bragging rights |
| Most Consistent | Lowest CV of community scores over the year (the lab that never has a bad day) | Trophy |
| Most Improved | Largest year-over-year score increase | Trophy |
| The Clogged Emitter | Lowest median community score, minimum 50 submissions | Trophy of Shame (opt-in -- you have to claim it) |

Awards are computed from the community benchmark dataset and announced annually. Labs must have `community_submit: true` and at least 50 submissions in the calendar year to qualify. The Clogged Emitter is opt-in -- your lab is never publicly shamed without consent. All awards are meant in good fun and to motivate better instrument maintenance across the field.

### Privacy

- Raw files are **never uploaded** -- only aggregate QC metrics
- Patient or sample metadata is **never collected**
- Serial numbers are stored server-side but never exposed in API responses or downloads
- Anonymous submissions are supported (`display_name` can be left blank)
- Submissions can be deleted by filing a GitHub issue with the `submission_id`
- Community dataset licensed under CC BY 4.0

---

## Configuration

All configuration files live in `~/.stan/`. They are YAML files that can be edited with any text editor. The watcher daemon hot-reloads `instruments.yml` every 30 seconds without requiring a restart. The dashboard Config tab provides a GUI for viewing and removing instruments (the Remove button deletes duplicate entries).

Run `stan init` to create default configuration templates, or `stan setup` for the interactive wizard. Manual editing examples below.

### instruments.yml

Defines which instruments to monitor, where their raw files land, and instrument-specific settings.

```yaml
# STAN instrument watcher configuration
# Hot-reloaded every 30 seconds -- no restart needed after edits

instruments:

  - name: "timsTOF Ultra"
    vendor: "bruker"
    model: "timsTOF Ultra"
    watch_dir: "D:/Data/raw"           # where .d directories appear
    output_dir: "D:/Data/stan_out"     # STAN writes results + HOLD flags here
    extensions: [".d"]
    stable_secs: 60              # seconds of no size change before processing
    enabled: true
    hela_amount_ng: 50           # injection amount in ng (default: 50)
    spd: 30                      # samples per day (Evosep 30 SPD)
    community_submit: false      # set true to share QC metrics with community benchmark

  - name: "Astral"
    vendor: "thermo"
    model: "Astral"
    watch_dir: "D:/Data/raw"
    output_dir: "D:/Data/stan_out"
    extensions: [".raw"]
    stable_secs: 30
    enabled: true
    hela_amount_ng: 50
    spd: 60                      # Evosep 60 SPD
    community_submit: false      # set true to opt in

# ── Optional: SLURM HPC execution ──────────────────────────────────
# Uncomment to run searches on a remote cluster instead of locally.
# Most labs do NOT need this — local execution is the default.
#
# hive:
#   host: "hive.ucdavis.edu"
#   user: "your_username"
#
# Then add to each instrument:
#   execution_mode: "slurm"      # default is "local"
#   hive_partition: "high"
#   hive_account: "your-account-grp"
```

**Vendor-specific file stability detection:**
- **Bruker `.d`**: The `.d` directory size is checked every 10 seconds. The run is considered complete after `stable_secs` consecutive seconds with no size change (default: 60 seconds).
- **Thermo `.raw`**: A single binary file. Checked via mtime and size. Stable after `stable_secs` with no change (default: 30 seconds).

### thresholds.yml

Defines QC pass/warn/fail thresholds per instrument model. A `default` entry applies when no model-specific entry exists.

```yaml
thresholds:

  default:
    dia:
      n_precursors_min: 5000
      median_cv_precursor_max: 20.0
      missed_cleavage_rate_max: 0.20
      ips_score_min: 50
    dda:
      n_psms_min: 10000
      pct_delta_mass_lt5ppm_min: 0.70
      ms2_scan_rate_min: 10.0

  "timsTOF Ultra":
    dia:
      n_precursors_min: 10000
      median_cv_precursor_max: 15.0
      ips_score_min: 65
    dda:
      n_psms_min: 30000
      pct_delta_mass_lt5ppm_min: 0.90
```

### community.yml

Controls community benchmark participation. No HuggingFace account or token is needed -- STAN submits through a relay API automatically.

```yaml
display_name: "Your Lab Name"              # shown on leaderboard; blank = anonymous
hela_source: "Pierce HeLa Protein Digest Standard"
institution_type: "core_facility"          # core_facility | academic_lab | industry
error_telemetry: true                      # opt-in anonymous error reports (set by stan setup)
```

---

## Zero-Config Raw File Intelligence

STAN reads your raw files before any search and auto-detects everything it needs. **You only configure two things: your LC column and your HeLa amount.**

| What STAN auto-detects | Thermo `.raw` | Bruker `.d` | How |
|---|---|---|---|
| Instrument model | Orbitrap Fusion Lumos, Exploris 480, Astral, ... | timsTOF HT, Ultra, Pro, ... | TRFP metadata / TDF GlobalMetadata |
| Serial number | fsn20215, ... | 1895883.10878, ... | Same |
| Acquisition date | 09/02/2025 15:18:13 | 2024-06-04T15:32:57 | FileProperties / AcquisitionDateTime |
| DIA vs DDA mode | From method name + MS2/MS1 ratio | MsmsType in Frames table (8=DDA, 9=DIA) | Automatic |
| Gradient length (min) | 35, 60, 90, 120 | From Frames.Time in analysis.tdf | TRFP metadata / TDF Frames table |
| DIA window size (Th) | 22 Da, 3 Th, 4 Th, ... | From method name | Parsed from method + computed from scan ratio |
| LC system | Dionex UltiMate 3000, Vanquish Neo, Easy-nLC | Evosep One, nanoElute | Binary string scan (`.raw`) / hystar.method XML (`.d`) |
| LC pump model | NCS-3500RS, HPG-3400RS, ... | — | DriverId in embedded method XML |
| Autosampler | WPS-3000, ... | Standard | Same |
| Fragmentation type | HCD, CID | CID (TIMS-CID) | ScanSettings / method |
| Column oven temp | 40°C | — | ScanSettings |
| Injection volume | 2 µL | — | SampleData |
| Xcalibur method path | `C:\Xcalibur\methods\gabri\Dia\ela_fDIAw22_35m.meth` | — | SampleData |

On Windows, STAN auto-downloads ThermoRawFileParser on first use (~10 MB, cached in `~/.stan/tools/`). On Linux/HPC, it uses the system `dotnet` runtime. Metadata extraction takes ~3 seconds per file.

---

## Instrument Performance Score (IPS)

IPS v2 is a 0-100 **cohort-calibrated depth score** derived from 967 real UC Davis HeLa QC runs. It uses only metrics STAN reliably measures — no reference TIC, no blank runs, works from run 1. A run at its cohort median scores 60; cohort p90 scores 90.

**DIA:**
```
IPS = 50% precursor_depth + 30% peptide_depth + 20% protein_depth
```
Each component scored by piecewise-linear interpolation against (instrument_family, SPD_bucket) reference p10/p50/p90.

**DDA:** Same structure but uses PSM counts with separate per-instrument DDA cohort references.

**DDA:**
```
IPS = 30 x identification_depth + 25 x mass_accuracy
    + 20 x sampling_quality (pts/peak) + 15 x scoring_quality + 10 x digestion
```

| Score Range | Interpretation |
|-------------|----------------|
| 90-100 | Excellent -- instrument performing optimally |
| 80-89 | Good -- normal operating range |
| 60-79 | Marginal -- investigate soon |
| Below 60 | Investigate -- likely instrument or LC issue |

IPS is stored for every run in the local SQLite database and included in community benchmark submissions.

---

## Search Engines

STAN depends on two external search engines that you install separately. Both run locally on your instrument workstation by default.

> **License note:** STAN does **not** bundle, redistribute, or include any part of DIA-NN, Sage, or ThermoRawFileParser. It calls them as external subprocesses, the same way a Makefile calls `gcc`. Users must install each tool separately under its own license. This is required for DIA-NN in particular because commercial use requires a paid license from Aptila Biotech or Thermo Fisher Scientific.

### DIA: DIA-NN

[DIA-NN](https://github.com/vdemichev/DiaNN) handles all DIA searches. Both Bruker `.d` and Thermo `.raw` files are passed directly to DIA-NN without conversion (DIA-NN 2.1+ has native support for both formats on Linux and Windows).

**Install:** Download from https://github.com/vdemichev/DiaNN/releases and add to PATH, or place the executable and set `diann_path` in `instruments.yml`.

**License:** DIA-NN is **free for academic research use**. Since STAN is designed for academic core facilities and research labs, this is the intended use case. Commercial users need to obtain a paid license separately from [Aptila Biotech](https://aptila.bio) or Thermo Fisher Scientific — STAN does not modify the licensing terms.

Historical note: DIA-NN versions up to 1.9.1 were free for all users (academic and commercial). Starting with 1.9.2, commercial use requires a paid license while academic use remains free. DIA-NN 2.x follows the same model. STAN recommends the latest academic release.

**Citation required:** If STAN is useful for your work, please cite the DIA-NN paper: Demichev V, Messner CB, Vernardis SI, Lilley KS, Ralser M. *Nature Methods* (2020).

Community benchmark submissions use a frozen HeLa-specific empirical spectral library and a pinned FASTA (`UP000005640_9606_plus_universal_contam.fasta`, 21,044 entries), both hosted in the HF Dataset repository. The FASTA is auto-downloaded on first community submission or baseline run if not cached locally. MD5 hashes are verified client-side before submission.

### DDA: Sage

[Sage](https://github.com/lazear/sage) handles all DDA searches. Bruker `.d` files are read natively by Sage (confirmed working for ddaPASEF). Thermo `.raw` files require conversion to mzML via ThermoRawFileParser before Sage can process them -- this is the only conversion step in the entire STAN pipeline.

**Install:** Download from https://github.com/lazear/sage/releases and add to PATH, or place the executable and set `sage_path` in `instruments.yml`.

**License:** Sage is open source under the [MIT license](https://github.com/lazear/sage/blob/master/LICENSE).

Sage includes built-in LDA rescoring that is sufficient for QC-level FDR estimation.

### ThermoRawFileParser (Thermo DDA only)

If you run DDA on a Thermo instrument, STAN needs [ThermoRawFileParser](https://github.com/compomics/ThermoRawFileParser) to convert `.raw` to mzML before Sage can search it. This is only needed for Thermo DDA -- not for Thermo DIA (DIA-NN reads `.raw` natively) and not for any Bruker workflows.

**License:** ThermoRawFileParser is open source under the [Apache 2.0 license](https://github.com/compomics/ThermoRawFileParser/blob/master/LICENSE).

### Running on an HPC Cluster (optional)

For labs with SLURM cluster access, STAN can submit search jobs via SSH instead of running locally. See the [HPC Guide](docs/hpc_guide.md) for setup, container paths, bind mount patterns, and common errors. This includes critical gotchas about DIA-NN containers, symlinks, and invalid flags that will save you hours of debugging.

---

## Repository Layout

```
stan/
+-- pyproject.toml
+-- README.md
+-- STAN_MASTER_SPEC.md            # authoritative design document
+-- CLAUDE.md                      # development context for Claude Code
+-- LICENSE                        # STAN Academic License
+-- install-stan.bat               # Windows fresh install (auto-installs DIA-NN + Sage)
+-- update-stan.bat                # Windows update (reinstalls from GitHub)
+-- start_stan.bat                 # launches dashboard + watcher + opens Chrome
+-- stan/
|   +-- cli.py                     # CLI entry point (typer)
|   +-- config.py                  # config loader with hot-reload
|   +-- db.py                      # SQLite operations
|   +-- setup.py                   # interactive 6-question setup wizard
|   +-- baseline.py                # retroactive QC processing from existing files
|   +-- telemetry.py               # opt-in anonymous error reporting
|   +-- watcher/                   # watchdog daemon, stability, mode detection
|   +-- search/                    # DIA-NN + Sage SLURM job builders
|   |   +-- community_params.py    # frozen community search parameters
|   +-- metrics/                   # metric extraction, IPS, iRT, scoring
|   +-- gating/                    # threshold evaluation, HOLD flag, queue control
|   +-- community/                 # HF Dataset submit/fetch/validate
|   |   +-- scripts/consolidate.py # nightly GitHub Actions consolidation
|   +-- dashboard/                 # FastAPI backend + React frontend
+-- tests/
+-- docs/
+-- .github/workflows/
    +-- ci.yml                     # lint + test on push/PR
    +-- consolidate_benchmark.yml  # nightly benchmark consolidation
```

---

## Development

```bash
# Install in development mode
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run a single test file
pytest tests/test_metrics.py -v

# Skip integration tests (require Hive/SLURM)
pytest tests/ -k "not integration"

# Lint
ruff check stan/

# Lint with auto-fix
ruff check stan/ --fix
```

Tests marked `@pytest.mark.integration` require Hive SLURM access and real instrument files. They are skipped in CI and can be run manually on the HPC cluster.

---

## Implementation Status

| Component | Status | Notes |
|-----------|--------|-------|
| CLI (`stan init/setup/watch/dashboard/baseline/status/column-health/version`) | Done | All commands wired up and working |
| Watcher daemon (file stability, hot-reload config) | Done | Bruker `.d` and Thermo `.raw` stability detection |
| Acquisition mode detection (Bruker `.d`) | Done | Reads `MsmsType` from `analysis.tdf` |
| Acquisition mode detection (Thermo `.raw`) | Done | Via ThermoRawFileParser metadata |
| Local DIA-NN execution (default) | Done | Subprocess-based, community-standardized params |
| Local Sage execution (default) | Done | JSON config, Thermo mzML conversion via TRFP |
| SLURM HPC execution (optional) | Done | SSH/paramiko job submission for labs with clusters |
| Metric extraction (DIA + DDA) | Done | Polars-based, from `report.parquet` and `results.sage.parquet` |
| IPS scoring | Done | 4-component composite, 0-100 scale |
| QC gating + HOLD flag | Done | Hard gates, plain-English diagnosis |
| Column health assessment | Done | Longitudinal TIC trend analysis |
| SQLite database + migrations | Done | Stores all metrics, gate results, amount_ng, spd |
| Community validation + submission | Done | Hard gates, soft flags, asset hash verification |
| Community scoring (DIA + DDA) | Done | Percentile-based within SPD/amount cohorts |
| Instrument fingerprint (Track C) | Done | 6-axis radar, failure pattern matching |
| Nightly consolidation script | Done | GitHub Actions, recomputes cohort percentiles |
| FastAPI dashboard backend | Done | API routes for runs, trends, instruments, thresholds, submission |
| SPD-first cohort bucketing | Done | Evosep 500-30 SPD, Vanquish Neo, traditional LC |
| Default config files (`config/`) | Done | instruments.yml, thresholds.yml, community.yml templates |
| Test fixtures (real DIA-NN/Sage output) | **Planned** | `tests/fixtures/` is empty -- need small real output files |
| React dashboard frontend | **Planned** | Only a placeholder HTML page exists |
| PyPI publishing (`pip install stan-proteomics`) | **Planned** | `pyproject.toml` is ready, not yet published |
| HF Dataset assets (speclibs) | **Partial** | FASTA uploaded + MD5 verified; spectral libraries in progress |
| HF Space public dashboard | **Planned** | Space repo exists but not deployed |
| Community benchmark live data | **Planned** | Requires spectral library uploads + first submissions |
| Setup wizard (`stan setup`) | Done | 6-question wizard, deduplicates instruments.yml, offers baseline at end |
| Baseline builder (`stan baseline`) | Done | Retroactive QC processing, auto-detect gradient/LC, pre-flight search engine tests |
| Windows installer (`install-stan.bat`) | Done | Auto-installs Python, DIA-NN, Sage, handles SSL/proxy, self-updates |
| Windows updater (`update-stan.bat`) | Done | One-click reinstall, self-updates from GitHub |
| Email reports (`stan email-report`) | Done | Daily + weekly HTML reports via Resend API, cron/schtasks install |
| Error telemetry (opt-in) | Done | Anonymous error reports to HF Space relay, local log at ~/.stan/error_log.json |
| Community FASTA | Done | Frozen UniProt human + contaminants (21,044 entries), MD5-verified, auto-downloaded |
| Recursive watcher | Done | Watches subdirectories, filters events inside Bruker .d directories |
| Dashboard Config tab | Done | Remove button on instrument cards for deleting duplicates |
| Outlier detection (amount mismatch) | **Planned** | Flag submissions where metrics don't match declared amount/SPD |
| Failed run rejection | **Planned** | Block near-zero results from entering benchmark (failed injection, empty spray) |
| Bruker `.d` XML metadata parser | Done | Reads `<N>.m/submethods.xml`, `hystar.method`, `SampleInfo.xml` for authoritative SPD + Evosep detection (v0.2.56) |
| `validate_spd_from_metadata()` | Done | XML → MethodName → Frames.Time span fallback chain; handles PAC method names (v0.2.55) |
| `detect_lc_system()` | Done | Evosep vs custom detection from .d XML tree + TrayType; powers LC filter on community TIC overlay (v0.2.56) |
| Real acquisition-date preservation | Done | `insert_run` stores `analysis.tdf` AcquisitionDateTime or fisher_py CreationDate, not insertion time (v0.2.54) |
| DIA-NN filename sanitizer | Done | Junction/symlink workaround for DIA-NN's `--` parsing bug that broke PAC-style filenames (v0.2.63) |
| Dashboard error boundary + null guards | Done | React ErrorBoundary + Array.isArray guards; `/api/runs` empty-DB graceful fallback (v0.2.62) |
| `stan repair-metadata [--push]` CLI | Done | Walks local DB, re-reads raw files, updates SPD/run_date/lc_system; optionally pushes to community relay (v0.2.57) |
| `stan fix-spds [--dry-run]` CLI | Done | Per-run SPD correction against raw-file metadata (v0.2.55) |
| `stan backfill-tic [--push]` CLI | Done | Multi-source TIC recovery (Bruker TDF → DIA-NN report → Thermo fisher_py) with 128-bin downsample + relay push (v0.2.65) |
| `stan baseline` auto-TIC backfill sweep | Done | Recovers missing TIC traces silently at startup; no manual command needed (v0.2.65) |
| `stan baseline` multi-directory picker | Done | Lists every configured watch dir as a numbered choice (v0.2.61) |
| `stan add-watch` with QC filter prompt | Done | Interactive scan/filter preview; `-y` / `--qc-pattern` / `--all-files` non-interactive flags (v0.2.59) |
| `stan add-watch` recursive vendor detect | Done | rglob with 5000-entry scan cap for nested subdirectories (v0.2.60) |
| `POST /api/update/{id}` relay endpoint | Done | Metadata-only whitelist (spd, run_date, lc_system, tic_rt_bins, tic_intensity, stats); used by `repair-metadata --push` (v0.2.57) |
| HF Space leaderboard TTL cache + snapshot_download | Done | 5 min in-memory cache; parallel submission downloads; cache-bust on `/api/submit`; `?refresh=1` override (server-side) |
| Community TIC LC filter (Evosep / Custom / All) | Done | Client-side filter on submissions by `lc_system` with inference fallback |
| Community TIC DIA/DDA separator | Done | Dropdown with DIA/DDA/Mixed options; defaults to DIA so cycle-time differences don't corrupt median shape |
| `downsample_trace(n_bins=128)` helper | Done | Bins arbitrary-length TIC to canonical 128-point format before local store + submission (v0.2.64) |
| Dashboard "Today's Runs" + MiniSparkline + IPS / FWHM-sec / signed mass-acc | Done | Cleaner Run History columns, per-instrument 30-run trend sparkline, unified Precursors/PSMs column (v0.2.58) |
| Lumos + Eclipse instrument family mapping | Done | `_instrument_family` now routes Lumos and Eclipse raw files to the right cohort (v0.2.67) |
| Maintenance log UI | Done | Trends tab form for logging column swaps, source cleans, PMs, calibrations; events render as vertical markers on trend charts (v0.2.68) |
| Setup wizard auto-assigns pseudonym | Done | `stan setup` picks an anonymous lab name automatically -- no opt-out, no prompt (v0.2.70) |
| `stan backfill-tic` zero-peptide repair | Done | Also recomputes `n_peptides` + `n_proteins` at 1% FDR from `baseline_output/<stem>/report.parquet` when the DB row has zero peptides; `--push` patches the community relay (v0.2.71) |
| Lumos DDA misdetection fix + TRFP auto-discovery | Done | `detect_mode()` now auto-finds TRFP via `stan.tools.trfp.ensure_installed()` (one-click installer path); filename tokens `HCD`/`DDA`/`CID`/`ETD` also force DDA routing so Thermo DDA files are no longer DIA-searched for 4 hours before timing out (v0.2.72) |
| Community auth token on submissions | Done | `stan setup` email-verifies the lab pseudonym and stores an `auth_token` in `community.yml`; submissions send `X-STAN-Auth` so forks that skip setup cannot spoof a claimed lab name (v0.2.74) |
| `stan verify` CLI | Done | Prints lab name, token presence, relay-side name claim status, submission count; directs to `stan setup` when the token is missing or unclaimed (v0.2.75) |
| Dashboard Windows UTF-8 read_text fix | Done | `index_path.read_text(encoding="utf-8")` — cp1252 default on Windows was failing to decode `—` and `≥` (v0.2.76) |
| Server-side dashboard error log | Done | GET `/` handler writes exceptions to `dashboard_errors.log` (mirrored to Hive) before re-raising so invisible server-side crashes are captured (v0.2.77) |
| `/api/update` auth-token enforcement | Done | Relay requires `X-STAN-Auth` (or admin `X-STAN-Admin`) for PATCHes; `stan backfill-tic --push` and `stan repair-metadata --push` now send the client token from `community.yml`. Set `ADMIN_SECRET` on the HF Space to fully lock down the endpoint (v0.2.78) |
| Explicit DDA/DIA filename tokens override sniffers | Done | Files with explicit `-Dda-`, `-HCDIT-`, `-HCDOT-` (DDA) or `-Dia-`, `-diaW<N>-` (DIA) tokens now override TRFP's scan-ratio verdict so Lumos DDA files are no longer DIA-searched for 4 hours before timing out (v0.2.79) |
| Extra Lumos DDA tokens + tighter search timeout | Done | Added `UnvPep`/`UnvID`/`Univ`/`1StpHCD`/`2StpHCD`/`Pepmx`/`HCD-IT`/`HCD-OT` to explicit-DDA override list; DIA-NN and Sage default timeout dropped from 4 h to 20 min to cap hung-search blast radius (v0.2.80) |
| Fleet control queue over the Hive mirror | Done | `stan/control.py` whitelist-dispatch poller wired into `stan watch`; instruments heartbeat `status.json` to `<mirror>/<host>/` every ~5 min; `stan send-command --host <h> --wait`, `stan fleet-status`, `stan poll-commands` CLIs. Read-only actions only for now (`ping`, `status`, `tail_log`, `export_db_snapshot`) — no remote process-kill or updater yet (v0.2.81) |
| Fleet dashboard tab | Done | New **Fleet** tab in `stan dashboard` aggregates `status.json` across every host on the shared mirror, auto-refreshes every 30 s, and exposes per-host command buttons (ping / status / tail_log / export_db_snapshot) that post to `/api/fleet/command` and poll `/api/fleet/result/{host}/{id}` (v0.2.82) |
| Watcher debug telemetry | Done | `InstrumentWatcher` now keeps a 100-entry ring buffer of every event the handler sees — including ones it ignored (inside `.d`, extension mismatch, QC-filter reject) — with per-category counts and per-tracker age. New whitelisted control action `watcher_debug` exposes a structured snapshot of all active watchers for remote diagnosis; also reachable via the Fleet tab's "Watcher debug" button (v0.2.83) |
| Acquisition-pipeline + config remote-control | Done | Ring buffer now records `stability_poll` / `stable` / `acquisition_complete_start|end|exception` so you can tell why a detected file isn't processing. Instrument config YAMLs (`instruments.yml`, `thresholds.yml`, `community.yml`) auto-sync to `<mirror>/<host>/config/` on every heartbeat. Three new write actions: `apply_config` (hot-reloads via existing `ConfigWatcher`), `update_stan` (runs `update-stan.bat`), `restart_watcher` (graceful exit via `restart.flag` — relaunches under `start_stan_loop.bat` supervisor). New diagnostic action `qc_filter_report` scans each watch_dir and shows match/reject examples against the live regex plus an optional `candidate_pattern` (v0.2.84) |
| Sample Health Monitor (rawmeat) | Done | `monitor_all_files: true` + `exclude_pattern: "(?i)(wash\|blank)"` config. Non-QC, non-excluded Bruker `.d` files get rawmeat-processed on acquisition; verdict (pass / warn / fail) stored in new `sample_health` SQLite table. Heuristics: MS1 max-intensity vs rolling 30-day median, spray dropout rate per 100 MS1 frames, run duration vs expected. New **Sample Health** dashboard tab with verdict filter; `/api/sample-health` endpoint. No HOLD flag, no community submission — purely surfaces bad injections for operator review. Also quiets TRFP's `CalledProcessError` traceback in baseline logs (catches exit-code non-fatal errors cleanly). Thermo support TBD (v0.2.89) |

---

## TODO

- [ ] Local dashboard trends tab: (1) add `n_proteins` over time as a selectable trend metric alongside precursors/peptides/PSMs; (2) overlay two horizontal dashed reference lines on every trend chart — this-instrument median (local stan.db) and community-median for the matching instrument_family/cohort (from benchmark snapshot); (3) split the trend line by `spd` so each gradient cohort has its own series (right now a 60-SPD drop-off is drowned out by the 100-SPD mean); (4) hover tooltip on each dot showing the exact metric value, run_name, and run_date (right now hovering just highlights — no numbers)
- [ ] cIRT retention-time trend plot on local dashboard: for a curated panel of ~10 endogenous HeLa peptides that elute across the gradient, chart observed RT over time with one line per peptide. CRITICAL: split by SPD — same peptide elutes at very different absolute times on 30-SPD vs 100-SPD (toggle/selector or separate panels per SPD). Purpose: detect LC column drift, gradient shape changes, and solvent problems that don't show in ID counts. **Peptide selection — tested both literature and empirical approaches against /Volumes/proteomics-grp/STAN/TIMS-10878/baseline_output at SPD=100 (25 runs, >15k precursors each)**: (1) Parker et al. 2015 CiRT set (MCP 14(10):2800-2813, doi:10.1074/mcp.O114.042267) — 14 peptides tested, only **3 usable** (≥80% presence, CV<5%): `SYELPDGQVITIGNER` (CV 4.70%), `YFPTQALNFAFK` (CV 3.58%), `DSTLIMQLLR` (CV 2.59%); 4 not detected at all, 7 present but CV 7–20% (mostly early eluters). Insufficient as a panel. (2) Empirical sweep — 2,473 endogenous peptides with RT_CV <5%, so pick the panel from the data instead. **Implementation**: for each (instrument_family, spd) cohort, run an empirical anchor-selection job: take all runs passing a quality gate (>15k precursors for DIA), find peptides present in ≥90% of runs with RT_CV <5%, bin by RT and pick ~10 spread anchors (9–18 residues, tryptic C-term K/R, prefer low CV and spread across gradient). Include the 3 working Parker peptides when they fall in the right RT bin — literature grounding on an otherwise ad-hoc panel. Persist anchor list in a new `irt_anchors` table keyed by (instrument_family, spd, peptide) so it's stable across sessions and survives re-searches. **Separate but related**: `stan/metrics/chromatography.py:269 DEFAULT_IRT_LIBRARY` is the Biognosys spike-in set — UC Davis HeLa QC has zero of those peptides at 1% FDR, so `irt_max_deviation_min` and `n_irt_found` are returning 0 on every run and the metric is effectively dead. Replace the hardcoded Biognosys list with the empirically-built panel (or mark Biognosys peptides as optional and only compute that metric when any spike-in peptide is actually seen)
- [ ] Startup catch-up scan: on `stan watch` / `stan` daemon startup, after the existing in-flight-acquisition scan (v0.2.101), sweep every watch_dir for completed QC files that are NOT in the local `runs` table, and queue each one for search. Catches runs that finished while STAN was offline (PC reboot, installer downtime, network blip, `stan watch` crash). Must respect the QC filter and the file-stability rules — only files that pass `is_qc_file()` and are older than `stable_secs` should auto-enqueue. Log each recovery so `baseline.log` shows exactly what was backfilled. This is distinct from the existing `stan baseline` command (which is interactive and manual); this is automatic on daemon start, no prompting
- [ ] Sample Health tab: under the results table, render the TIC traces for the currently-listed runs as a chart with two display modes toggled by a single control — **overlaid** (all traces on one axis, color-coded per run, so you can spot spray dropouts or gradient shape differences at a glance) and **faceted** (one small-multiple panel per run, shared y-axis, so outliers aren't drowned out by a single huge-intensity run). Default to overlaid; toggle switches to faceted. Pull TIC from the `tic_traces` table, joined on run_id of the rows currently visible in the table
- [ ] Thermo TIC extraction is failing on some Lumos `.raw` files — fisher_py's `RawFile(...)` ctor throws `System.ArgumentOutOfRangeException: Instrument index not available for requested device` (hardcoded `SelectInstrument(Device.MS, 1)` inside fisher_py), and ThermoRawFileParser also exits non-zero on the same files. TIC is non-essential (dashboard gradient traces only, doesn't block submission) but we're losing it on ~every Lumos run. Need to: (a) test whether `SelectInstrument(Device.MS, 0)` works on the Lumos (fisher_py may need a patch or a per-instrument index override), (b) read the TRFP stderr surfaced by v0.2.115 to find out why TRFP also fails, (c) if it's a specific Lumos firmware quirk, document in GOTCHAS_DELIMP.md and add an instruments.yml flag to skip TIC extraction for that model
- [x] Ship default config YAML templates in `config/` so `stan init` works out of the box
- [x] Setup wizard (`stan setup`) — 6-question interactive config with deduplication and baseline offer
- [x] Windows installer (`install-stan.bat`) — auto-installs Python, DIA-NN (.msi), Sage, handles SSL/proxy
- [x] Windows updater (`update-stan.bat`) — one-click reinstall with self-update
- [x] Baseline builder (`stan baseline`) — retroactive QC processing with auto-detect and pre-flight tests
- [x] Upload pinned human UniProt reviewed FASTA to HF Dataset (UP000005640_9606_plus_universal_contam.fasta, 21,044 entries)
- [x] Populate MD5 hashes in `stan/community/validate.py`
- [x] Auto-download community FASTA on first community submission or baseline run
- [x] Opt-in anonymous error telemetry with local log
- [x] Recursive watcher with Bruker .d event filtering
- [x] Dashboard Config tab with instrument Remove button
- [x] Bruker `.d` XML method-tree parser for authoritative SPD + LC detection
- [x] `stan repair-metadata` CLI for re-extracting SPD/run_date/lc_system from raw files
- [x] `stan fix-spds` CLI for per-run SPD correction against raw-file metadata
- [x] `stan backfill-tic --push` for multi-source TIC recovery with community relay push
- [x] `stan baseline` auto-TIC-backfill sweep on startup
- [x] `stan baseline` multi-directory picker
- [x] `stan add-watch` with interactive QC filter prompt + non-interactive flags
- [x] `stan add-watch` recursive vendor auto-detect for nested watch dirs
- [x] DIA-NN filename-with-`--` sanitizer (junction/symlink workaround)
- [x] `POST /api/update/{id}` relay endpoint with metadata whitelist
- [x] HF Space leaderboard TTL cache + snapshot_download
- [x] Community TIC overlay: LC system filter (Evosep/Custom/All)
- [x] Community TIC overlay: DIA/DDA separator (defaults to DIA)
- [x] Dashboard ErrorBoundary + null-safety for empty DB
- [x] Dashboard Today's Runs MiniSparkline + Run History column refresh
- [x] `downsample_trace()` for canonical 128-bin TIC shape
- [x] Real acquisition-date preservation from `analysis.tdf.AcquisitionDateTime`
- [x] HF Dataset historical backfill: 81/83 submissions corrected with real SPD + run_date + lc_system
- [x] Maintenance log UI — enter column swaps/source cleans/PMs with date+notes, overlay as vertical markers on Trends charts (v0.2.68)
- [x] `stan backfill-tic` zero-peptide repair — recomputes n_peptides/n_proteins from report.parquet at 1% FDR (v0.2.71)
- [x] Lumos DDA misdetection fix + TRFP auto-discovery — filename-token fallback when TRFP unavailable (v0.2.72)
- [x] Community auth token + relay `X-STAN-Auth` enforcement — fork protection for lab pseudonyms (v0.2.74, v0.2.78)
- [x] `stan verify` CLI — check auth token + relay name claim (v0.2.75)
- [x] Dashboard UTF-8 `read_text` fix for Windows (v0.2.76)
- [x] Server-side dashboard error log mirrored to Hive (v0.2.77)
- [ ] Fleet `disk_free_gb` should report the watch_dir's drive (D:, E:, etc.) instead of the STAN user-config drive (usually C:). Today `_action_status` calls `shutil.disk_usage(user_dir)`, which tells you whether `stan.db` is running out of space but not whether the acquisition drive is. On multi-instrument hosts, report one entry per watch_dir so the Fleet tab flags when the REAL data drive is filling up
- [ ] Community downtime / reliability leaderboard — track instrument uptime, downtime, and failure rate across the community benchmark so labs can see which mass spec models are reliable vs. problematic. Needs: (a) heartbeat-gap detection (if no QC submission for >N hours on a weekday = likely downtime), (b) gate-failure rate (FAIL results per 1000 injections or per calendar month), (c) MTBF (mean time between failures — hours between consecutive FAIL gate results), (d) recovery time (hours from FAIL to next PASS), (e) availability % normalized by expected uptime (core facilities ~24/7 vs academic labs ~40h/week — use `institution_type` from community.yml), (f) distinguish planned downtime (maintenance_events: PM, column change, calibration) from unplanned (crash, spray failure, no maintenance event logged). Privacy: opt-in per instrument, aggregate by instrument model not by lab unless lab consents. Community dashboard shows reliability rankings by model with error bars. Could become a major selling point — "which Orbitrap model actually stays up?"
- [ ] Thermo ion-injection-time drift detection — `fisher_py` exposes per-scan injection time; add `median_ion_injection_time_ms` plus a mid-run upward-drift flag to the Thermo Sample Health pipeline. AGC compensation for a collapsing spray forces longer inject times before the TIC shows it, so this should catch marginal spray stutters that the TIC-only dropout test misses
- [ ] Remote `run_baseline` / `baseline_status` control actions — kick off a baseline from the fleet dashboard or `stan send-command`, poll progress via a mirrored `baseline_progress.json`, so operators don't need to shell into the instrument PC
- [ ] Mobile PWA — responsive dashboard CSS, `manifest.json` + service worker, push notifications on gate failure, auth layer for off-campus access (Tailscale / Cloudflare Tunnel / HF Space relay)
- [ ] Add small real DIA-NN and Sage output files to `tests/fixtures/`
- [ ] Column tracking — log column installs (vendor, model, serial, install date) to explain TIC variance
- [ ] TIC filter by pseudonym (your traces vs community vs all)
- [ ] TIC color by lab when showing all traces
- [ ] Lumos/Exploris Thermo TIC backfill via Hive-side `report.parquet` identified-TIC path
- [ ] Thermo `.raw` fisher_py-based SPD extraction from InstrumentMethod header
- [ ] Generate and upload Astral HeLa predicted spectral library to HF Dataset
- [ ] Generate and upload timsTOF HeLa predicted spectral library to HF Dataset
- [ ] Build React frontend for dashboard (run history, trend charts, community leaderboard)
- [ ] Deploy HF Space public community dashboard
- [ ] Publish to PyPI
- [ ] Outlier detection for community submissions — flag runs where metrics are wildly inconsistent with declared amount/SPD (e.g., someone declares 50 ng but IDs suggest 500 ng injection)
- [ ] Failed run rejection — detect near-zero results (failed injection, empty file, broken spray) and block them from entering the benchmark; these should never pollute cohort percentiles
- [ ] Add Thermo `.raw` mode detection integration tests on Hive
- [ ] Points-across-peak metric (DIA + DDA): compute median FWHM, cycle time, and data points per elution peak (quantitation quality diagnostic, per Matthews & Hayes 1976)
- [ ] Community dashboard figures: SPD vs. points-across-peak (shows the quantitation cliff), faceted/colored by LC column model
- [ ] LC column as a dimension in all community dashboard figures (color, facet, or filter)
- [ ] End-to-end watcher integration test with real instrument data

---

## Links

| Resource | URL |
|----------|-----|
| STAN GitHub | [github.com/bsphinney/stan](https://github.com/bsphinney/stan) |
| STAN Community Dashboard | [huggingface.co/spaces/brettsp/stan](https://huggingface.co/spaces/brettsp/stan) |
| STAN Community Dataset | [huggingface.co/datasets/brettsp/stan-benchmark](https://huggingface.co/datasets/brettsp/stan-benchmark) |
| DE-LIMP (sibling project) | [github.com/bsphinney/DE-LIMP](https://github.com/bsphinney/DE-LIMP) |

STAN handles QC and instrument health monitoring. For differential expression analysis and full quantitative proteomics workflows, see [DE-LIMP](https://github.com/bsphinney/DE-LIMP).

---

## Contributing

Contributions are welcome. Please:

1. Fork the repository and create a feature branch
2. Run `ruff check stan/` and `pytest tests/ -v` before submitting
3. Include tests for new functionality (use fixtures in `tests/fixtures/`, prefer real output snippets over synthetic data)
4. Open a pull request with a clear description of the change

For questions about the spec or design decisions, open a discussion on GitHub before implementing.

---

## License

**Code**: [STAN Academic License](LICENSE) -- free for academic, non-profit, educational, and personal research use. Commercial use (including CROs, pharmaceutical companies, and biotech) requires a separate license. Contact bsphinney@ucdavis.edu for commercial licensing inquiries.

**Community benchmark dataset**: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)

## Citations

If STAN is useful for your work, please cite STAN and the search engines it depends on:

**STAN:**
> Phinney BS. STAN: Standardized proteomic Throughput ANalyzer. UC Davis Proteomics Core (2026). https://github.com/bsphinney/stan

**DIA-NN (DIA search engine):**
> Demichev V, Messner CB, Vernardis SI, Lilley KS, Ralser M. DIA-NN: neural networks and interference correction enable deep proteome coverage in high throughput. *Nature Methods*. 2020;17:41-44. https://doi.org/10.1038/s41592-019-0638-x

**Sage (DDA search engine):**
> Lazear MR. Sage: An Open-Source Tool for Fast Proteomics Searching and Quantification at Scale. *Journal of Proteome Research*. 2023;22(11):3652-3659. https://doi.org/10.1021/acs.jproteome.3c00486

**Points-across-peak quantitation quality metric:**
> Matthews DE, Hayes JM. Systematic Errors in Gas Chromatography-Mass Spectrometry Isotope Ratio Measurements. *Analytical Chemistry*. 1976;48(9):1375-1382.
