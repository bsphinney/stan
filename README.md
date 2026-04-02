# STAN -- Standardized proteomic Throughput ANalyzer

> *Know your instrument.*

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Dataset: CC BY 4.0](https://img.shields.io/badge/Data_License-CC_BY_4.0-green.svg)](https://creativecommons.org/licenses/by/4.0/)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

STAN is an open-source proteomics QC tool for Bruker timsTOF and Thermo Orbitrap mass spectrometers. It watches your raw data directories for new acquisitions, auto-detects DIA or DDA mode, submits standardized search jobs (DIA-NN for DIA, Sage for DDA) to your HPC cluster, computes instrument health metrics, gates your sample queue automatically on QC failure, tracks longitudinal performance in a local database, serves a real-time dashboard, and optionally benchmarks your instrument against the global proteomics community through a crowdsourced HeLa digest dataset.

**Built at the UC Davis Proteomics Core by Brett Stanley Phinney.**

---

## Key Features

- **Multi-instrument monitoring** -- Bruker timsTOF and Thermo Orbitrap in a single dashboard
- **DIA and DDA mode intelligence** -- auto-detects acquisition mode and routes to the right search engine with the right metrics
- **Run and Done gating** -- automatically pauses your sample queue (HOLD flag) when a QC run fails thresholds
- **Gradient Reproducibility Score (GRS)** -- a single 0-100 composite number for LC health, updated every run
- **Column health tracking** -- longitudinal TIC trend analysis detects column aging before it affects your data
- **Precursor-first metrics** -- benchmarks on precursor count (DIA) and PSM count (DDA), not protein count, because protein count is confounded by FASTA choice and inference settings
- **Community HeLa benchmark** -- compare your instrument against labs worldwide via an open HuggingFace Dataset (CC BY 4.0)
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

### Install

```bash
pip install stan-proteomics
```

Or install from source for development:

```bash
git clone https://github.com/bsphinney/stan.git
cd stan
pip install -e ".[dev]"
```

### Initialize

```bash
stan init
```

This creates `~/.stan/` with default configuration files:
- `instruments.yml` -- instrument watch directories and settings
- `thresholds.yml` -- QC pass/warn/fail thresholds per instrument model
- `community.yml` -- HuggingFace token and community benchmark preferences

Edit `~/.stan/instruments.yml` to configure your instruments (see [Configuration](#configuration) below).

### Watch

```bash
stan watch
```

Starts the watcher daemon. It monitors directories configured in `instruments.yml`, detects new raw files, determines acquisition mode, and dispatches search jobs to your HPC cluster via SLURM.

### Dashboard

```bash
stan dashboard
```

Serves the local QC dashboard at [http://localhost:8421](http://localhost:8421). Views include live instrument status, run history, trend charts, column health, and the community benchmark leaderboard.

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
        +-- DIA --> diann.py --> SLURM job --> report.parquet
        +-- DDA --> sage.py  --> SLURM job --> results.sage.parquet
                                        |
                                extractor.py + chromatography.py
                                        |
                                evaluator.py --> PASS / WARN / FAIL
                                        |              |
                                SQLite (Hive)    queue.py (HOLD flag)
                                        |
                                dashboard (FastAPI + React, port 8421)
                                        |
                                community/submit.py --> HF Dataset
```

**Data flow**: The watcher daemon detects new raw files and checks for file stability (size stops changing). Once stable, the detector reads instrument metadata to determine DIA or DDA mode. A SLURM job is submitted to the HPC cluster running DIA-NN (for DIA) or Sage (for DDA) with standardized parameters. After the search completes, STAN extracts QC metrics from the results, evaluates them against per-instrument thresholds, writes a HOLD flag if the run fails, stores everything in SQLite for longitudinal tracking, and optionally submits to the community benchmark.

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

Browse the community dashboard: [huggingface.co/spaces/brettsp/stan](https://huggingface.co/spaces/brettsp/stan)

### How It Works

All community benchmark submissions use a **frozen, standardized search** with pinned FASTA, spectral libraries, and search parameters hosted in the HF Dataset repository. This is what makes cross-lab comparisons valid -- every lab searches the same library with the same settings, so differences in precursor counts reflect actual instrument performance, not search configuration.

### Benchmark Tracks

| Track | Mode | Search Engine | Primary Metric | Secondary Metrics |
|-------|------|---------------|----------------|-------------------|
| **Track A** | DDA | Sage | PSM count @ 1% FDR | Peptide count, mass accuracy, MS2 scan rate |
| **Track B** | DIA | DIA-NN | Precursor count @ 1% FDR | Peptide count, median CV, GRS |
| **Track C** | Both | Both | Instrument fingerprint | Radar chart (6 axes), peptide recovery ratio |

Track C unlocks when a lab submits both a DDA and a DIA run from the same instrument within 24 hours. The resulting six-axis radar chart provides a comprehensive instrument health fingerprint covering mass accuracy, duty cycle, spectral quality, precursor depth, quantitative reproducibility, and fragment sensitivity.

### Cohort Bucketing

Submissions are compared only within their cohort, defined by three dimensions: **instrument family**, **gradient length**, and **injection amount**. This ensures a 50 ng run on a timsTOF Ultra is compared against other 50 ng timsTOF Ultra runs, not against a 500 ng Astral run.

**Gradient buckets:**

| Bucket | Range |
|--------|-------|
| ultra-short | 30 min or less |
| short | 31-45 min |
| standard-1h | 46-75 min |
| long-2h | 76-120 min |
| extended | over 120 min |

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
          + 15 x percentile_rank(grs_score)
```

**DDA Score** (Track A):
```
DDA_Score = 35 x percentile_rank(n_psms)
          + 25 x percentile_rank(n_peptides_dda)
          + 20 x percentile_rank(pct_delta_mass_lt5ppm)
          + 20 x percentile_rank(ms2_scan_rate)
```

Scores are computed nightly within each cohort. A score of 75 means your instrument outperformed 75% of comparable submissions.

### Privacy

- Raw files are **never uploaded** -- only aggregate QC metrics
- Patient or sample metadata is **never collected**
- Serial numbers are stored server-side but never exposed in API responses or downloads
- Anonymous submissions are supported (`display_name` can be left blank)
- Submissions can be deleted by filing a GitHub issue with the `submission_id`
- Community dataset licensed under CC BY 4.0

---

## Configuration

All configuration files live in `~/.stan/` (created by `stan init`). They are YAML files that can be edited with any text editor or through the dashboard UI. The watcher daemon hot-reloads `instruments.yml` every 30 seconds without requiring a restart.

### instruments.yml

Defines which instruments to monitor, where their raw files land, and instrument-specific settings.

```yaml
# STAN instrument watcher configuration
# Hot-reloaded every 30 seconds -- no restart needed after edits

hive:
  host: "hive.ucdavis.edu"
  user: "your_username"

instruments:

  - name: "timsTOF Ultra"
    vendor: "bruker"
    model: "timsTOF Ultra"
    watch_dir: "/mnt/instruments/timstof-ultra/raw"
    output_dir: "/mnt/instruments/timstof-ultra/stan_out"
    extensions: [".d"]
    stable_secs: 60              # seconds of no size change before processing
    enabled: true
    qc_modes: ["dia", "dda"]     # auto-detected from analysis.tdf
    hive_partition: "high"
    hive_account: "your-account-grp"
    community_submit: true       # auto-submit QC metrics to community benchmark
    hela_amount_ng: 50           # injection amount in ng (default: 50)
    gradient_length_min: 60      # gradient length in minutes

  - name: "Astral"
    vendor: "thermo"
    model: "Astral"
    watch_dir: "/mnt/instruments/astral/raw"
    output_dir: "/mnt/instruments/astral/stan_out"
    extensions: [".raw"]
    stable_secs: 30
    enabled: true
    qc_modes: ["dia"]
    raw_handling: "native"       # "native" (DIA-NN 2.1+ reads .raw) or "convert_mzml"
    trfp_path: "/path/to/ThermoRawFileParser.dll"   # needed if raw_handling is convert_mzml
    hive_partition: "high"
    hive_account: "your-account-grp"
    community_submit: true
    hela_amount_ng: 50
    gradient_length_min: 60
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
      grs_score_min: 50
    dda:
      n_psms_min: 10000
      pct_delta_mass_lt5ppm_min: 0.70
      ms2_scan_rate_min: 10.0

  "timsTOF Ultra":
    dia:
      n_precursors_min: 10000
      median_cv_precursor_max: 15.0
      grs_score_min: 65
    dda:
      n_psms_min: 30000
      pct_delta_mass_lt5ppm_min: 0.90
```

### community.yml

Controls community benchmark participation.

```yaml
hf_token: ""                     # HuggingFace token with write access
display_name: "Your Lab Name"    # shown on leaderboard; leave blank for anonymous
submit_by_default: false         # auto-submit without review prompt
hela_source: "Pierce HeLa Protein Digest Standard"
institution_type: "core_facility"  # core_facility | academic_lab | industry
```

---

## Gradient Reproducibility Score (GRS)

The GRS is a single 0-100 composite number summarizing LC chromatography health:

```
GRS = 40 x shape_r_scaled
    + 25 x auc_scaled
    + 20 x peak_rt_scaled
    + 15 x carryover_scaled
```

| Score Range | Interpretation |
|-------------|----------------|
| 90-100 | Excellent -- system performing optimally |
| 70-89 | Good -- normal operating range |
| 50-69 | Watch -- performance declining, investigate soon |
| Below 50 | Investigate -- likely LC or source issue |

GRS is stored for every run in the local SQLite database and displayed as a badge on the dashboard. It is included in community benchmark submissions and contributes to the DIA composite score.

---

## Search Engines

### DIA: DIA-NN

STAN uses DIA-NN for all DIA searches. Both Bruker `.d` and Thermo `.raw` files are passed directly to DIA-NN without conversion (DIA-NN 2.1+ has native support for both formats on Linux).

Community benchmark submissions use a frozen HeLa-specific predicted spectral library (one for timsTOF TIMS-CID fragmentation, one for Orbitrap HCD fragmentation) and a pinned FASTA, both hosted in the HF Dataset repository.

### DDA: Sage

STAN uses Sage for all DDA searches. Bruker `.d` files are read natively by Sage (confirmed working for ddaPASEF). Thermo `.raw` files require conversion to mzML via ThermoRawFileParser before Sage can process them -- this is the only conversion step in the entire STAN pipeline.

Sage includes built-in LDA rescoring that is sufficient for QC-level FDR estimation.

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
|   +-- metrics/                   # metric extraction, GRS, iRT, scoring
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

## Links

| Resource | URL |
|----------|-----|
| STAN GitHub | [github.com/bsphinney/stan](https://github.com/bsphinney/stan) |
| STAN Community Dashboard | [huggingface.co/spaces/brettsp/stan](https://huggingface.co/spaces/brettsp/stan) |
| STAN Community Dataset | [huggingface.co/datasets/bsphinney/stan-community-benchmark](https://huggingface.co/datasets/bsphinney/stan-community-benchmark) |
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

## Citation

If STAN is useful for your work, please cite:

> Phinney BS. STAN: Standardized proteomic Throughput ANalyzer. UC Davis Proteomics Core (2026). https://github.com/bsphinney/stan
