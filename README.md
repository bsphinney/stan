# STAN -- Standardized proteomic Throughput ANalyzer

> *Know your instrument.*

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
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
- **Community HeLa benchmark** **(planned)** -- compare your instrument against labs worldwide via an open HuggingFace Dataset (CC BY 4.0)
- **Instrument health fingerprint** -- dual-mode DDA+DIA radar chart for rapid visual diagnosis
- **Plain-English failure diagnosis** -- templated alerts explain what failed and what to check, no guesswork
- **Privacy by design** -- raw files are never uploaded; only aggregate QC metrics leave your lab

> **Status note**: The Python backend (watcher, search dispatch, metric extraction, gating, scoring, DB) is implemented and tested. The React dashboard frontend, community HF Dataset assets, and PyPI packaging are in progress. See [Implementation Status](#implementation-status) below.

## Supported Instruments

| Vendor | Instruments | Raw Format | Acquisition Modes |
|--------|-------------|------------|-------------------|
| Bruker | timsTOF Ultra 2, Ultra, Pro 2, SCP | `.d` directory | diaPASEF, ddaPASEF |
| Thermo | Astral, Exploris 480, Exploris 240 | `.raw` file | DIA, DDA |

---

## Quick Start

### Prerequisites

STAN runs search engines locally on your instrument workstation. Install these first:

1. **Python 3.10+**
2. **DIA-NN** -- download from [github.com/vdemichev/DiaNN/releases](https://github.com/vdemichev/DiaNN/releases) and add to PATH
3. **Sage** -- download from [github.com/lazear/sage/releases](https://github.com/lazear/sage/releases) and add to PATH
4. **ThermoRawFileParser** (only if running DDA on Thermo instruments) -- [github.com/compomics/ThermoRawFileParser](https://github.com/compomics/ThermoRawFileParser)

### Install STAN

```bash
pip install stan-proteomics          # coming soon — not yet on PyPI
```

Install from source (recommended for now):

```bash
git clone https://github.com/bsphinney/stan.git
cd stan
pip install -e ".[dev]"
```

### Initialize

```bash
stan init
```

Creates `~/.stan/` and copies default configuration templates into it.

Run `stan setup` for an interactive wizard that picks your instrument, directories, LC method, and FASTA -- no YAML editing required. Or create the config files manually:
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

Serves the FastAPI backend at [http://localhost:8421](http://localhost:8421). The API is fully functional -- browse `/docs` for Swagger UI. **(The React frontend is planned; currently a placeholder page is shown. Use the API endpoints directly or via the Swagger UI.)**

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
                 community/submit.py --> HF Dataset (planned)
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

Browse the community dashboard **(planned)**: [huggingface.co/spaces/brettsp/stan](https://huggingface.co/spaces/brettsp/stan)

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

All configuration files live in `~/.stan/`. They are YAML files that can be edited with any text editor. The watcher daemon hot-reloads `instruments.yml` every 30 seconds without requiring a restart. **(Dashboard UI editing is planned; for now edit the YAML files directly.)**

Until the default config templates are shipped, create these files manually in `~/.stan/` using the examples below.

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
```

---

## Instrument Performance Score (IPS)

IPS is a 0-100 composite computed entirely from search output. No reference run, no blank runs, no historical data needed — works from the very first QC injection.

**DIA:**
```
IPS = 30 x precursor_depth + 25 x spectral_quality (frags/precursor)
    + 20 x sampling_quality (pts/peak) + 15 x quant_coverage + 10 x digestion
```

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

### DIA: DIA-NN

[DIA-NN](https://github.com/vdemichev/DiaNN) handles all DIA searches. Both Bruker `.d` and Thermo `.raw` files are passed directly to DIA-NN without conversion (DIA-NN 2.1+ has native support for both formats on Linux and Windows).

**Install:** Download from https://github.com/vdemichev/DiaNN/releases and add to PATH, or place the executable and set `diann_path` in `instruments.yml`.

**License:** DIA-NN is free for academic and non-commercial use. Commercial use requires a license from Vadim Demichev. See the [DIA-NN license](https://github.com/vdemichev/DiaNN/blob/master/LICENSE.txt) for details.

Community benchmark submissions use a frozen HeLa-specific predicted spectral library and a pinned FASTA, both hosted in the HF Dataset repository. **(Library generation is in progress -- the HF Dataset assets are not yet uploaded.)**

### DDA: Sage

[Sage](https://github.com/lazear/sage) handles all DDA searches. Bruker `.d` files are read natively by Sage (confirmed working for ddaPASEF). Thermo `.raw` files require conversion to mzML via ThermoRawFileParser before Sage can process them -- this is the only conversion step in the entire STAN pipeline.

**Install:** Download from https://github.com/lazear/sage/releases and add to PATH, or place the executable and set `sage_path` in `instruments.yml`.

**License:** Sage is open source under the [MIT license](https://github.com/lazear/sage/blob/master/LICENSE).

Sage includes built-in LDA rescoring that is sufficient for QC-level FDR estimation.

### ThermoRawFileParser (Thermo DDA only)

If you run DDA on a Thermo instrument, STAN needs [ThermoRawFileParser](https://github.com/compomics/ThermoRawFileParser) to convert `.raw` to mzML before Sage can search it. This is only needed for Thermo DDA -- not for Thermo DIA (DIA-NN reads `.raw` natively) and not for any Bruker workflows.

**License:** ThermoRawFileParser is open source under the [Apache 2.0 license](https://github.com/compomics/ThermoRawFileParser/blob/master/LICENSE).

---

## Repository Layout

```
stan/
+-- pyproject.toml
+-- README.md
+-- STAN_MASTER_SPEC.md            # authoritative design document
+-- CLAUDE.md                      # development context for Claude Code
+-- stan/
|   +-- cli.py                     # CLI entry point (typer)
|   +-- config.py                  # config loader with hot-reload
|   +-- db.py                      # SQLite operations
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
| CLI (`stan init/watch/dashboard/status/column-health/version`) | Done | All commands wired up and working |
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
| Test fixtures (real DIA-NN/Sage output) | **Planned** | `tests/fixtures/` is empty — need small real output files |
| React dashboard frontend | **Planned** | Only a placeholder HTML page exists |
| PyPI publishing (`pip install stan-proteomics`) | **Planned** | `pyproject.toml` is ready, not yet published |
| HF Dataset assets (FASTA + speclibs) | **Planned** | Library generation in progress, MD5 hashes TODO |
| HF Space public dashboard | **Planned** | Space repo exists but not deployed |
| Community benchmark live data | **Planned** | Requires HF Dataset assets + first submissions |
| Setup wizard (`stan setup`) | Done | Interactive instrument picker, LC method, FASTA path, writes YAML |
| Outlier detection (amount mismatch) | **Planned** | Flag submissions where metrics don't match declared amount/SPD |
| Failed run rejection | **Planned** | Block near-zero results from entering benchmark (failed injection, empty spray) |

---

## TODO

- [x] Ship default config YAML templates in `config/` so `stan init` works out of the box
- [x] Setup wizard (`stan setup`) — interactive instrument config, no YAML editing
- [ ] Add small real DIA-NN and Sage output files to `tests/fixtures/`
- [ ] Generate and upload Astral HeLa predicted spectral library to HF Dataset
- [ ] Generate and upload timsTOF HeLa predicted spectral library to HF Dataset
- [ ] Upload pinned human UniProt reviewed FASTA to HF Dataset (hash-verified, shipped with STAN for community mode)
- [ ] Populate MD5 hashes in `stan/community/validate.py` — submissions with wrong FASTA hash are rejected
- [ ] Auto-download community FASTA on first community submission if not cached locally
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
- [ ] Add `spd` field to instruments.yml example configs and user guide

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

**Code**: MIT License

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
