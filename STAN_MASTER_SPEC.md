# STAN — Standardized proteomic Throughput ANalyzer
## Master Specification v0.1

> **Author**: Brett Stanley Phinney, UC Davis Proteomics Core  
> **Tagline**: *Know your instrument.*  
> **GitHub**: `bsphinney/stan`  
> **HF Space**: `brettsp/stan`  
> **HF Dataset**: `brettsp/stan-benchmark`  
> **Status**: Ready for Claude Code implementation  
> **Date**: April 2026

---

## Table of Contents

1. [What is STAN?](#1-what-is-stan)
2. [Repository Setup — Claude Code Instructions](#2-repository-setup--claude-code-instructions)
3. [Project Structure](#3-project-structure)
4. [Core Architecture](#4-core-architecture)
5. [Instrument Watcher Daemon](#5-instrument-watcher-daemon)
6. [Search Pipeline](#6-search-pipeline)
7. [QC Metrics Engine](#7-qc-metrics-engine)
8. [Run & Done Gating](#8-run--done-gating)
9. [Local Dashboard](#9-local-dashboard)
10. [Community Benchmark — Track B (DIA)](#10-community-benchmark--track-b-dia)
11. [Community Benchmark — Track A (DDA)](#11-community-benchmark--track-a-dda)
12. [Community Benchmark — Track C (Dual Mode Fingerprint)](#12-community-benchmark--track-c-dual-mode-fingerprint)
13. [HF Dataset Infrastructure](#13-hf-dataset-infrastructure)
14. [Instrument Config (instruments.yml)](#14-instrument-config-instrumentsyml)
15. [Tech Stack](#15-tech-stack)
16. [Implementation Phases](#16-implementation-phases)
17. [Appendix: Expected HeLa Reference Ranges](#17-appendix-expected-hela-reference-ranges)

---

## 1. What is STAN?

STAN is a standalone, fast, instrument-agnostic proteomics QC tool built at the
UC Davis Proteomics Core. It is named for its author's middle name — Stanley — and
the acronym: **S**tandardized proteomic **T**hroughput **AN**alyzer.

STAN is not a slice of DE-LIMP. It is a separate, focused tool that does one thing
extremely well: tells you whether your mass spectrometer is performing as expected,
alerts you when it isn't, gates your sample queue automatically, and lets you compare
your instrument against the global proteomics community — all within minutes of
acquisition completing.

### What ProteoScape does that costs $50k+ and is Bruker-only:
- Real-time search results during acquisition
- "Run & Done" smart queue gating
- Integrated data viewer

### What STAN does for free, on any instrument:
- Everything above, plus:
- Multi-instrument support (Bruker timsTOF AND Thermo Orbitrap in one dashboard)
- DDA + DIA mode intelligence — separate tracks, separate metrics, right search engine
- Longitudinal instrument health database (SQLite on Hive)
- Cross-instrument comparison with cross-lab benchmarking
- Community HeLa benchmark (crowdsourced, HF Dataset, open CC BY 4.0)
- Instrument health fingerprint radar (dual-mode DDA+DIA diagnostic)
- Peptide recovery ratio (DIA vs DDA efficiency on same instrument)
- Gradient Reproducibility Score (0–100 single-number LC health)
- Column aging detection via longitudinal TIC trend analysis
- Plain-English failure diagnosis (templated, no AI call required for basic alerts)

### What STAN explicitly does NOT do:
- Full differential expression analysis (that's DE-LIMP)
- FragPipe or Spectronaut support (DIA-NN + Sage only, by design)
- Store or transmit raw data files (metrics only, ever)

---

## 2. Repository Setup — Claude Code Instructions

> **Claude Code**: Execute all steps in this section in order before implementing
> any application code. These steps create the GitHub repo, HF Space, and HF Dataset
> that the rest of the spec depends on. Run in a persistent tmux session on Hive
> or your local machine. Requires: `gh` CLI authenticated, `huggingface-cli`
> authenticated with `brettsp` / `bsphinney` accounts, `git` configured.

### Step 2.1 — Create GitHub repository

```bash
# Create public GitHub repo under bsphinney
gh repo create bsphinney/stan \
  --public \
  --description "STAN — Standardized proteomic Throughput ANalyzer. Know your instrument." \
  --clone

cd stan

# Initialize with .gitignore for Python + R
curl -sL https://raw.githubusercontent.com/github/gitignore/main/Python.gitignore > .gitignore
echo "\n# R\n.Rhistory\n.RData\n.Rproj.user/\n*.rds" >> .gitignore

# Initial commit
git add .gitignore
git commit -m "init: create STAN repository"
git push origin main
```

### Step 2.2 — Create repository structure

```bash
# Create full directory tree
mkdir -p \
  stan/watcher \
  stan/search \
  stan/metrics \
  stan/gating \
  stan/dashboard/src \
  stan/dashboard/public \
  stan/community \
  stan/community/scripts \
  config \
  .github/workflows \
  docs \
  tests

# Create placeholder files to establish structure
touch stan/__init__.py
touch stan/watcher/__init__.py
touch stan/search/__init__.py
touch stan/metrics/__init__.py
touch stan/gating/__init__.py
touch stan/community/__init__.py
touch config/instruments.yml
touch config/thresholds.yml
touch config/community.yml
```

### Step 2.3 — Write GitHub README

```bash
cat > README.md << 'EOF'
# STAN — Standardized proteomic Throughput ANalyzer

> *Know your instrument.*

STAN is an open-source proteomics QC tool for Bruker timsTOF and Thermo Orbitrap instruments.
It watches your raw data directories, runs standardized searches (DIA-NN for DIA, Sage for DDA),
computes instrument health metrics, gates your sample queue automatically, and benchmarks your
performance against the global proteomics community.

**Built at the UC Davis Proteomics Core by Brett Stanley Phinney.**

## Features

- Multi-instrument monitoring (timsTOF + Orbitrap in one dashboard)
- DIA and DDA mode intelligence — right search engine, right metrics, separate leaderboards
- Run & Done gating — pause sample queue automatically on QC failure
- Gradient Reproducibility Score (GRS) — single 0–100 LC health number
- Longitudinal instrument health database (SQLite)
- Community HeLa benchmark — compare against labs worldwide (HF Dataset, CC BY 4.0)
- Instrument health fingerprint — dual-mode DDA+DIA radar diagnostic
- Peptide/precursor-first metrics — not protein count (the right way to benchmark)

## Supported instruments

| Vendor | Instruments | Raw format | Acquisition |
|--------|------------|------------|-------------|
| Bruker | timsTOF Ultra, Ultra 2, Pro 2, SCP | `.d` | diaPASEF, ddaPASEF |
| Thermo | Astral, Exploris 480, Exploris 240 | `.raw` | DIA, DDA |

## Quick start

```bash
pip install stan-proteomics   # coming soon
stan init                      # creates ~/.stan/instruments.yml
stan watch                     # start watching configured directories
stan dashboard                 # open local dashboard
```

## Community benchmark

STAN contributes to an open HF Dataset of HeLa QC runs from labs worldwide.
Browse at: https://huggingface.co/spaces/brettsp/stan

## License

MIT License. Community benchmark dataset: CC BY 4.0.

## Citation

If STAN is useful for your work, please cite:
> Phinney BS. STAN: Standardized proteomic Throughput ANalyzer. UC Davis Proteomics Core (2026).
> https://github.com/bsphinney/stan
EOF

git add README.md
git commit -m "docs: add README"
git push origin main
```

### Step 2.4 — Create HF Space (dashboard)

```bash
# Install HF CLI if needed
pip install huggingface_hub

# Login (requires brettsp HF account token)
huggingface-cli login

# Create the Space
python3 << 'EOF'
from huggingface_hub import HfApi
api = HfApi()

api.create_repo(
    repo_id="brettsp/stan",
    repo_type="space",
    space_sdk="static",   # will upgrade to gradio or static HTML
    private=False,
)
print("Created HF Space: https://huggingface.co/spaces/brettsp/stan")
EOF

# Create HF Space README (required card metadata)
cat > /tmp/hf_space_readme.md << 'EOF'
---
title: STAN — Standardized proteomic Throughput ANalyzer
emoji: 📊
colorFrom: blue
colorTo: teal
sdk: static
pinned: true
license: mit
short_description: Know your instrument. Community HeLa benchmark for proteomics QC.
---

# STAN Community Benchmark

Browse community HeLa QC submissions and compare instrument performance globally.

**Primary metrics**: precursor count (DIA) and PSM count (DDA) at 1% FDR — not protein count.

Submit your own results from the STAN desktop tool.

Source: [github.com/bsphinney/stan](https://github.com/bsphinney/stan)
EOF

python3 << 'EOF'
from huggingface_hub import HfApi
api = HfApi()
api.upload_file(
    path_or_fileobj="/tmp/hf_space_readme.md",
    path_in_repo="README.md",
    repo_id="brettsp/stan",
    repo_type="space",
)
print("HF Space README uploaded")
EOF
```

### Step 2.5 — Create HF Dataset (community benchmark)

```bash
python3 << 'EOF'
from huggingface_hub import HfApi
import os

api = HfApi()

# Create the dataset repo under bsphinney
api.create_repo(
    repo_id="brettsp/stan-benchmark",
    repo_type="dataset",
    private=False,
)
print("Created HF Dataset: https://huggingface.co/datasets/brettsp/stan-benchmark")
EOF

# Write dataset card
cat > /tmp/hf_dataset_readme.md << 'EOF'
---
license: cc-by-4.0
task_categories:
- other
tags:
- proteomics
- mass-spectrometry
- quality-control
- benchmarking
- hela
pretty_name: STAN Community HeLa Benchmark
size_categories:
- n<1K
---

# STAN Community HeLa Benchmark

Crowdsourced instrument performance data from the STAN proteomics QC tool.

## What this dataset contains

Aggregate QC metrics from HeLa protein digest standard runs submitted by proteomics
labs worldwide. **No raw files. No patient data. Metrics only.**

Each row is one QC submission containing:
- Instrument model, vendor, acquisition mode
- LC conditions (column, gradient length, flow rate, amount injected)
- QC metrics: precursor count, peptide count, median CV, fragment depth, GRS score
- Community score (percentile within cohort)

## Primary metrics

Precursor count at 1% FDR (DIA) and PSM count at 1% FDR (DDA) are the primary
benchmark metrics — not protein count, which is heavily confounded by FASTA choice
and search settings.

## License

CC BY 4.0. Use freely with attribution:
> STAN Community Benchmark. brettsp/stan-benchmark. Hugging Face (2026).

## Contributing

Submit via the STAN tool: https://github.com/bsphinney/stan
EOF

python3 << 'EOF'
from huggingface_hub import HfApi
api = HfApi()
api.upload_file(
    path_or_fileobj="/tmp/hf_dataset_readme.md",
    path_in_repo="README.md",
    repo_id="brettsp/stan-benchmark",
    repo_type="dataset",
)
print("HF Dataset README uploaded")
EOF

# Create required directory structure in dataset repo
python3 << 'EOF'
from huggingface_hub import HfApi
import io

api = HfApi()

# Create placeholder benchmark parquet (empty, correct schema)
# Real data appended via submit_to_benchmark() at runtime
import pyarrow as pa
import pyarrow.parquet as pq

schema = pa.schema([
    pa.field("submission_id", pa.string()),
    pa.field("submitted_at", pa.timestamp("us", tz="UTC")),
    pa.field("stan_version", pa.string()),
    pa.field("display_name", pa.string()),
    pa.field("instrument_family", pa.string()),
    pa.field("instrument_model", pa.string()),
    pa.field("acquisition_mode", pa.string()),
    pa.field("gradient_length_min", pa.int32()),
    pa.field("amount_ng", pa.float32()),
    pa.field("n_precursors", pa.int32()),
    pa.field("n_peptides", pa.int32()),
    pa.field("n_proteins", pa.int32()),
    pa.field("n_psms", pa.int32()),
    pa.field("median_cv_precursor", pa.float32()),
    pa.field("median_fragments_per_precursor", pa.float32()),
    pa.field("grs_score", pa.int32()),
    pa.field("missed_cleavage_rate", pa.float32()),
    pa.field("community_score", pa.float32()),
    pa.field("cohort_id", pa.string()),
    pa.field("is_flagged", pa.bool_()),
])

buf = io.BytesIO()
pq.write_table(pa.table({f.name: pa.array([], type=f.type) for f in schema.fields}), buf)
buf.seek(0)

api.upload_file(
    path_or_fileobj=buf,
    path_in_repo="benchmark_latest.parquet",
    repo_id="brettsp/stan-benchmark",
    repo_type="dataset",
)

# Create submissions/ and community_fasta/ placeholder dirs
api.upload_file(
    path_or_fileobj=io.BytesIO(b"# STAN community FASTA files stored here\n"),
    path_in_repo="community_fasta/.gitkeep",
    repo_id="brettsp/stan-benchmark",
    repo_type="dataset",
)
api.upload_file(
    path_or_fileobj=io.BytesIO(b"# Individual submission parquets stored here\n"),
    path_in_repo="submissions/.gitkeep",
    repo_id="brettsp/stan-benchmark",
    repo_type="dataset",
)
api.upload_file(
    path_or_fileobj=io.BytesIO(b"{}"),
    path_in_repo="cohort_stats/cohort_percentiles_latest.json",
    repo_id="brettsp/stan-benchmark",
    repo_type="dataset",
)

print("HF Dataset structure initialized")
EOF
```

### Step 2.6 — Set up GitHub Actions

```bash
# Nightly consolidation workflow
cat > .github/workflows/consolidate_benchmark.yml << 'EOF'
name: Nightly benchmark consolidation
on:
  schedule:
    - cron: '0 4 * * *'
  workflow_dispatch:

jobs:
  consolidate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install polars pyarrow huggingface_hub
      - run: python stan/community/scripts/consolidate.py
        env:
          HF_TOKEN: ${{ secrets.HF_TOKEN }}
EOF

# CI test workflow
cat > .github/workflows/ci.yml << 'EOF'
name: CI
on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install -e ".[dev]"
      - run: pytest tests/ -v
EOF

# Add HF_TOKEN secret reminder
cat >> docs/setup.md << 'EOF'
## GitHub Secrets required

After creating the repos, add the following secret to bsphinney/stan on GitHub:

- `HF_TOKEN`: Hugging Face token with write access to brettsp/stan-benchmark
  Settings → Secrets and variables → Actions → New repository secret
EOF

git add .github/ docs/
git commit -m "ci: add GitHub Actions workflows"
git push origin main
```

### Step 2.7 — Create pyproject.toml

```bash
cat > pyproject.toml << 'EOF'
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.backends.legacy:build"

[project]
name = "stan-proteomics"
version = "0.1.0"
description = "STAN — Standardized proteomic Throughput ANalyzer"
readme = "README.md"
license = { text = "MIT" }
authors = [{ name = "Brett Stanley Phinney", email = "bsphinney@ucdavis.edu" }]
requires-python = ">=3.10"
dependencies = [
    "polars>=0.20",
    "pyarrow>=14",
    "watchdog>=4.0",
    "pyyaml>=6.0",
    "httpx>=0.27",
    "huggingface_hub>=0.22",
    "rich>=13.0",
    "typer>=0.12",
    "paramiko>=3.0",       # SSH to Hive for SLURM submission
]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio", "ruff", "mypy"]

[project.scripts]
stan = "stan.cli:app"

[tool.ruff]
line-length = 100
target-version = "py310"
EOF

git add pyproject.toml
git commit -m "build: add pyproject.toml"
git push origin main
```

### Step 2.8 — Verify setup

```bash
# Confirm all three exist and are accessible
echo "=== GitHub repo ==="
gh repo view bsphinney/stan --json name,url,visibility

echo "=== HF Space ==="
python3 -c "
from huggingface_hub import HfApi
api = HfApi()
info = api.space_info('brettsp/stan')
print(f'Space: {info.id}, SDK: {info.sdk}')
"

echo "=== HF Dataset ==="
python3 -c "
from huggingface_hub import HfApi
api = HfApi()
info = api.dataset_info('brettsp/stan-benchmark')
print(f'Dataset: {info.id}, License: {info.license}')
"

echo ""
echo "Setup complete. Links:"
echo "  GitHub:    https://github.com/bsphinney/stan"
echo "  HF Space:  https://huggingface.co/spaces/brettsp/stan"
echo "  HF Dataset: https://huggingface.co/datasets/brettsp/stan-benchmark"
```

---

## 3. Project Structure

```
stan/
├── pyproject.toml
├── README.md
├── config/
│   ├── instruments.yml          # instrument watch dirs + settings (user-edited)
│   ├── thresholds.yml           # QC pass/fail thresholds per instrument model
│   └── community.yml            # HF token, display name, submission prefs
│
├── stan/
│   ├── cli.py                   # `stan` CLI entry point (typer)
│   ├── watcher/
│   │   ├── daemon.py            # watchdog-based file system monitor
│   │   ├── detector.py          # acquisition mode detection from raw metadata
│   │   └── stability.py         # file stability checker (stable_secs logic)
│   ├── search/
│   │   ├── dispatcher.py        # routes to DIA-NN or Sage based on mode
│   │   ├── diann.py             # DIA-NN SLURM job builder
│   │   ├── sage.py              # Sage SLURM job builder + Percolator
│   │   └── community_params.py  # frozen community search parameters
│   ├── metrics/
│   │   ├── extractor.py         # extract_community_metrics() from report.parquet
│   │   ├── chromatography.py    # TIC, iRT, GRS score, fill time
│   │   └── scoring.py           # community score, percentile rank
│   ├── gating/
│   │   ├── evaluator.py         # apply thresholds, produce pass/warn/fail
│   │   └── queue.py             # write HOLD flag, notify
│   ├── community/
│   │   ├── submit.py            # submit_to_benchmark()
│   │   ├── fetch.py             # fetch cohort data from HF Dataset
│   │   ├── validate.py          # hard gates + soft flags
│   │   └── scripts/
│   │       └── consolidate.py   # nightly GitHub Actions consolidation
│   └── dashboard/
│       ├── server.py            # FastAPI backend
│       ├── src/                 # React frontend source
│       └── public/              # built frontend (committed)
│
├── .github/
│   └── workflows/
│       ├── ci.yml
│       └── consolidate_benchmark.yml
│
├── docs/
│   └── setup.md
└── tests/
    ├── test_detector.py
    ├── test_metrics.py
    └── test_gating.py
```

---

## 4. Core Architecture

```
Raw data landing dir (watched by daemon)
        │
        │  file stable for stable_secs
        ▼
detector.py — reads .d/analysis.tdf or .raw header
        │
        ├─ diaPASEF / DIA ──► diann.py ──► SLURM job ──► report.parquet
        └─ ddaPASEF / DDA ──► sage.py  ──► SLURM job ──► sage_results/
                                                    │
                                            extractor.py
                                            chromatography.py
                                                    │
                                            evaluator.py ──► PASS / WARN / FAIL
                                                    │              │
                                            SQLite (Hive)    queue.py (HOLD flag)
                                                    │
                                            dashboard (FastAPI + React)
                                                    │
                                            community/submit.py ──► HF Dataset
```

---

## 5. Instrument Watcher Daemon

### `stan/watcher/daemon.py`

Uses Python `watchdog` to monitor directories defined in `instruments.yml`.
One watcher thread per instrument. File stability logic is vendor-specific.

**Bruker `.d` directories**: a `.d` folder is a directory that Bruker writes to
continuously during acquisition. It contains `analysis.tdf`, `analysis.tdf_bin`,
`chromatography-data.sqlite3`, and frame binary files. The directory is "stable"
when its total size stops growing. Check every 10s; trigger after `stable_secs`
consecutive stable checks (default 60s = 6 checks).

**Thermo `.raw` files**: a single binary file. Thermo instruments close the file
handle at acquisition end. Stable when `os.path.getmtime` is older than `stable_secs`
(default 30s) and file size is unchanged for two consecutive checks.

```python
# stan/watcher/stability.py

import os, time
from pathlib import Path
from dataclasses import dataclass, field

@dataclass
class StabilityTracker:
    path: Path
    vendor: str           # "bruker" | "thermo"
    stable_secs: int
    _size_history: list = field(default_factory=list)
    _last_check: float = 0.0

    def check(self) -> bool:
        """Return True if file/dir is stable (acquisition complete)."""
        now = time.time()
        if now - self._last_check < 10:
            return False
        self._last_check = now

        if self.vendor == "bruker":
            # Sum size of all files in .d directory
            size = sum(f.stat().st_size for f in self.path.rglob("*") if f.is_file())
        else:
            size = self.path.stat().st_size

        self._size_history.append((now, size))
        # Keep only last stable_secs / 10 checks
        cutoff = now - self.stable_secs
        self._size_history = [(t, s) for t, s in self._size_history if t >= cutoff]

        if len(self._size_history) < (self.stable_secs // 10):
            return False

        sizes = [s for _, s in self._size_history]
        return len(set(sizes)) == 1 and sizes[0] > 0
```

### `stan/watcher/detector.py`

```python
# stan/watcher/detector.py

import sqlite3
from pathlib import Path
from enum import Enum

class AcquisitionMode(Enum):
    DIA_PASEF = "diaPASEF"
    DDA_PASEF = "ddaPASEF"
    DIA_ORBITRAP = "DIA"
    DDA_ORBITRAP = "DDA"
    UNKNOWN = "unknown"

def detect_bruker_mode(d_path: Path) -> AcquisitionMode:
    """Read MsmsType from analysis.tdf Frames table."""
    tdf = d_path / "analysis.tdf"
    if not tdf.exists():
        return AcquisitionMode.UNKNOWN
    con = sqlite3.connect(tdf)
    try:
        # MsmsType: 0=MS1, 8=ddaPASEF, 9=diaPASEF
        rows = con.execute(
            "SELECT DISTINCT MsmsType FROM Frames WHERE MsmsType > 0"
        ).fetchall()
        types = {r[0] for r in rows}
        if 9 in types:
            return AcquisitionMode.DIA_PASEF
        if 8 in types:
            return AcquisitionMode.DDA_PASEF
        return AcquisitionMode.UNKNOWN
    finally:
        con.close()

def detect_thermo_mode(raw_path: Path) -> AcquisitionMode:
    """
    Read scan filter strings from .raw file via ThermoRawFileParser output.
    On HPC: ThermoRawFileParser -i file.raw --metadata json
    Parse ScanFilter field for 'DIA' or 'dd-MS2'.
    """
    # Implementation uses subprocess ThermoRawFileParser
    # Returns DIA_ORBITRAP or DDA_ORBITRAP
    ...
```

---

## 6. Search Pipeline

### DIA search (Track B) — `stan/search/diann.py`

Submits a SLURM job to Hive that runs DIA-NN with community-standardized parameters.
No raw file conversion is ever needed for DIA:

- Bruker diaPASEF `.d` → DIA-NN reads natively (always)
- Thermo DIA `.raw` → DIA-NN 2.1+ reads natively on Linux (since March 2025)

Pass raw files directly to DIA-NN via `--f`. The `raw_handling` field in
`instruments.yml` controls behaviour if native `.raw` reading fails in a container:

```yaml
raw_handling: "native"       # default — pass .raw/.d directly to DIA-NN
# raw_handling: "convert_mzml"  # fallback if native fails in Apptainer on Hive
```

```python
COMMUNITY_DIANN_PARAMS = {
    "lib": "/path/to/community_fasta/human_opg_202604.predicted.speclib",
    "fasta": "/path/to/community_fasta/human_opg_202604.fasta",
    "qvalue": 0.01,
    "protein-q": 0.01,
    "min-pep-len": 7,
    "max-pep-len": 30,
    "missed-cleavages": 1,
    "min-pr-charge": 2,
    "max-pr-charge": 4,
    "cut": "K*,R*",
    "threads": 8,
    "out-dir": "{output_dir}",
}

COMMUNITY_DIANN_SLURM = {
    "partition": "{hive_partition}",
    "account": "{hive_account}",
    "mem": "32G",
    "cpus-per-task": 8,
    "time": "02:00:00",
    "job-name": "stan-diann-{run_name}",
}
```

### DDA search (Track A) — `stan/search/sage.py`

**Raw file routing — confirmed working, no conversion except Thermo DDA:**

```
Bruker .d  + diaPASEF  →  DIA-NN (native .d)               — Track B, no conversion
Bruker .d  + ddaPASEF  →  Sage   (native .d, confirmed)     — Track A, no conversion
Thermo .raw + DIA      →  DIA-NN (native .raw, v2.1+ Linux) — Track B, no conversion
Thermo .raw + DDA      →  ThermoRawFileParser → mzML → Sage — Track A, only conversion
```

ThermoRawFileParser is the only conversion step in the entire STAN pipeline.

```python
COMMUNITY_SAGE_PARAMS = {
    "database": {
        "fasta": "/path/to/community_fasta/human_opg_202604.fasta",
        "enzyme": {"missed_cleavages": 1, "min_len": 7, "max_len": 30,
                   "cleave_at": "KR", "restrict": "P"},
        "static_mods": {"C": 57.0215},
        "variable_mods": {"M": [15.9949]},
        "max_variable_mods": 2,
    },
    "precursor_tol": {"ppm": [-10, 10]},
    "fragment_tol": {"ppm": [-20, 20]},
    "min_peaks": 8,
    "max_peaks": 150,
    "min_matched_peaks": 4,
    "target_fdr": 0.01,
    "deisotope": True,
}
# Sage reads .d natively (ddaPASEF, confirmed working)
# Sage reads mzML for Thermo DDA (ThermoRawFileParser conversion required)
# Percolator optional — Sage built-in LDA is sufficient for QC-level FDR
```

---

## 7. QC Metrics Engine

### `stan/metrics/extractor.py`

All metrics extracted from `report.parquet` (DIA) or Sage `.pin` / Percolator output (DDA).
Uses Polars for speed. Never loads entire file into memory — predicate pushdown via Arrow.

#### DIA metrics (from `report.parquet`)

```python
import polars as pl

def extract_dia_metrics(report_path: str, q_cutoff: float = 0.01) -> dict:
    df = pl.read_parquet(report_path, columns=[
        "Precursor.Id", "Stripped.Sequence", "Protein.Group",
        "Q.Value", "PG.Q.Value", "Fragment.Info", "Fragment.Quant.Corrected",
        "Precursor.Charge", "Missed.Cleavages", "File.Name",
        "Precursor.Normalised",
    ])
    filt = df.filter(pl.col("Q.Value") <= q_cutoff)

    # Fragment counts
    filt = filt.with_columns([
        (pl.col("Fragment.Info").str.count_matches(";") + 1)
            .alias("n_frag_extracted"),
        pl.col("Fragment.Quant.Corrected")
            .map_elements(
                lambda s: sum(1 for x in s.split(";") if float(x or 0) > 0),
                return_dtype=pl.Int32
            ).alias("n_frag_quantified"),
    ])

    # CV across replicates
    cv_df = (
        filt.group_by(["Precursor.Id", "File.Name"])
            .agg(pl.col("Precursor.Normalised").mean().alias("intensity"))
            .group_by("Precursor.Id")
            .agg([pl.col("intensity").std().alias("sd"),
                  pl.col("intensity").mean().alias("mean")])
            .with_columns((pl.col("sd") / pl.col("mean") * 100).alias("cv"))
    )

    charge = (
        filt.group_by("Precursor.Charge").agg(pl.len().alias("n"))
            .with_columns((pl.col("n") / pl.col("n").sum()).alias("pct"))
    )

    def charge_pct(z):
        row = charge.filter(pl.col("Precursor.Charge") == z)
        return float(row["pct"][0]) if len(row) else 0.0

    return {
        "n_precursors": filt["Precursor.Id"].n_unique(),
        "n_peptides": filt["Stripped.Sequence"].n_unique(),
        "n_proteins": filt.filter(
            pl.col("PG.Q.Value") <= q_cutoff)["Protein.Group"].n_unique(),
        "median_fragments_per_precursor": float(filt["n_frag_extracted"].median()),
        "pct_fragments_quantified": float(
            filt["n_frag_quantified"].sum() / filt["n_frag_extracted"].sum()),
        "median_cv_precursor": float(cv_df["cv"].median()),
        "missed_cleavage_rate": float(
            filt.filter(pl.col("Missed.Cleavages") >= 1).height / filt.height),
        "pct_charge_1": charge_pct(1),
        "pct_charge_2": charge_pct(2),
        "pct_charge_3": charge_pct(3),
        "pct_charge_4plus": sum(charge_pct(z) for z in range(4, 10)),
    }
```

#### DDA metrics (from Sage + Percolator output)

```python
def extract_dda_metrics(percolator_psms_path: str, gradient_min: int) -> dict:
    df = pl.read_csv(percolator_psms_path, separator="\t")
    filt = df.filter(pl.col("q-value") <= 0.01)

    peptide_df = (
        filt.group_by("peptide")
            .agg(pl.col("q-value").min())
            .filter(pl.col("q-value") <= 0.01)
    )

    return {
        "n_psms": len(filt),
        "n_peptides_dda": filt["peptide"].n_unique(),
        "median_hyperscore": float(filt["score"].median()),
        "pct_hyperscore_gt30": float((filt["score"] > 30).mean()),
        "ms2_scan_rate": len(filt) / gradient_min,
        "median_delta_mass_ppm": float(filt["delta_mass"].abs().median()),
        "pct_delta_mass_lt5ppm": float((filt["delta_mass"].abs() < 5).mean()),
    }
```

### `stan/metrics/chromatography.py`

```python
def compute_grs(tic_data: dict) -> int:
    """
    Gradient Reproducibility Score — 0 to 100 composite.

    GRS = 40 × shape_r_scaled
        + 25 × auc_scaled        (1 - |z-score| / 3, clamped 0–1)
        + 20 × peak_rt_scaled
        + 15 × carryover_scaled

    Stored in SQLite for longitudinal tracking.
    A score of 90+ = excellent, 70–89 = good, 50–69 = watch, <50 = investigate.
    """
    ...

def compute_irt_deviation(report_path: str, irt_library: dict) -> dict:
    """
    Cross-reference identified precursors against known iRT peptide RTs.
    Returns max and median deviation in minutes.
    iRT library: {"LGGNEQVTR": 0.0, "GAGSSEPVTGLDAK": 26.1, ...} (normalized scale)
    """
    ...
```

---

## 8. Run & Done Gating

### `stan/gating/evaluator.py`

Reads thresholds from `config/thresholds.yml` (per instrument model) and applies them
to the computed metrics. Returns a structured result that `queue.py` acts on.

```python
from dataclasses import dataclass
from enum import Enum

class GateResult(Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"

@dataclass
class GateDecision:
    result: GateResult
    failed_gates: list[str]      # metric names that failed
    warned_gates: list[str]
    diagnosis: str               # plain-English explanation
    metrics: dict                # all computed values

def evaluate_gates(metrics: dict, instrument_model: str,
                   acquisition_mode: str) -> GateDecision:
    """
    Load thresholds from config/thresholds.yml for this instrument model.
    Apply hard gates (FAIL) and soft gates (WARN).
    Generate plain-English diagnosis from failure pattern.
    """
    ...
```

### `config/thresholds.yml` structure

```yaml
# Per-model QC thresholds — edit to match your method
# 'default' applies when no model-specific entry exists

thresholds:

  default:
    dia:
      n_precursors_min: 5000
      median_cv_precursor_max: 20.0
      missed_cleavage_rate_max: 0.20
      pct_charge_1_max: 0.30
      grs_score_min: 50
      irt_max_deviation_max: 5.0
    dda:
      n_psms_min: 10000
      pct_delta_mass_lt5ppm_min: 0.70
      ms2_scan_rate_min: 10.0

  "timsTOF Ultra":
    dia:
      n_precursors_min: 10000    # tighter — this instrument should hit 15k+
      median_cv_precursor_max: 15.0
      missed_cleavage_rate_max: 0.15
      pct_charge_1_max: 0.10
      grs_score_min: 65
      irt_max_deviation_max: 3.0
    dda:
      n_psms_min: 30000
      pct_delta_mass_lt5ppm_min: 0.90

  "Astral":
    dia:
      n_precursors_min: 12000
      median_cv_precursor_max: 10.0
      grs_score_min: 70

  "Exploris 480":
    dia:
      n_precursors_min: 6000
      median_cv_precursor_max: 18.0
    dda:
      n_psms_min: 20000
      pct_delta_mass_lt5ppm_min: 0.95  # Orbitrap mass accuracy should be excellent
```

### Plain-English failure diagnosis

Templated strings — no AI call needed for basic alerts:

```python
DIAGNOSIS_TEMPLATES = {
    ("n_precursors", "n_psms"): (
        "Low ID count with normal chromatography suggests a search/library issue. "
        "Check spectral library version, FASTA, or DIA window scheme."
    ),
    ("n_precursors", "grs_score"): (
        "Low IDs with poor GRS score — likely LC or source problem. "
        "Check column condition, trap column, spray stability."
    ),
    ("missed_cleavage_rate",): (
        "High missed cleavages suggest incomplete digestion. "
        "Check trypsin activity, digestion time/temperature, or protein denaturation."
    ),
    ("pct_charge_1",): (
        "Elevated singly-charged precursors — possible source contamination, "
        "buffer impurity, or electrospray instability."
    ),
    ("median_cv_precursor",): (
        "High CV with normal ID count — LC reproducibility issue. "
        "Check injection volume consistency, sample carryover, or column equilibration."
    ),
    ("pct_delta_mass_lt5ppm",): (
        "Poor mass accuracy — instrument may need recalibration. "
        "Run a calibration file before next sample injection."
    ),
}
```

### `stan/gating/queue.py`

Writes a `HOLD` file to the instrument output directory. Autosampler queue software
(e.g. Xcalibur sequence, Bruker timsControl) can poll this file or a webhook.

```python
def write_hold_flag(output_dir: Path, decision: GateDecision, run_name: str):
    """Write HOLD_{run_name}.txt to output_dir if decision is FAIL."""
    if decision.result == GateResult.FAIL:
        flag_path = output_dir / f"HOLD_{run_name}.txt"
        flag_path.write_text(
            f"STAN QC HOLD\n"
            f"Run: {run_name}\n"
            f"Failed gates: {', '.join(decision.failed_gates)}\n"
            f"Diagnosis: {decision.diagnosis}\n"
            f"Time: {datetime.utcnow().isoformat()}\n"
        )
```

---

## 9. Local Dashboard

FastAPI backend + React frontend. Served locally on `http://localhost:8421`.
Hot-reloads `instruments.yml` without restart.

### Key views

1. **Live runs** — per-instrument status cards, GRS badge, pass/fail gate, last run time
2. **Run history** — scrollable table, sortable by any metric, colored GRS pills
3. **Trend charts** — protein/precursor/CV over time per instrument, LOESS trendline
4. **Instrument config** — edit `instruments.yml` via form OR raw YAML editor, live preview
5. **Community benchmark** — DDA / DIA / Both tabs (see §10–12)
6. **Column health** — TIC AUC and peak RT over calendar time, drift detection

### FastAPI routes

```python
GET  /api/runs                    # recent runs from SQLite, paginated
GET  /api/runs/{run_id}           # single run detail + all metrics
GET  /api/instruments             # list from instruments.yml
POST /api/instruments             # update instruments.yml (UI save)
GET  /api/trends/{instrument}     # time-series metrics for trend plots
GET  /api/community/cohort        # fetch cohort distribution from HF Dataset
POST /api/community/submit        # submit current run to benchmark
GET  /api/thresholds              # current thresholds.yml
POST /api/thresholds              # update thresholds.yml
```

### SQLite schema (local, on Hive)

```sql
CREATE TABLE runs (
    id          TEXT PRIMARY KEY,  -- UUID
    instrument  TEXT NOT NULL,
    run_name    TEXT NOT NULL,
    run_date    TEXT NOT NULL,     -- ISO timestamp
    raw_path    TEXT,
    mode        TEXT,              -- "DIA" | "DDA"

    -- DIA metrics
    n_precursors     INTEGER,
    n_peptides       INTEGER,
    n_proteins       INTEGER,
    median_cv_precursor REAL,
    median_fragments_per_precursor REAL,
    pct_fragments_quantified REAL,

    -- DDA metrics
    n_psms           INTEGER,
    n_peptides_dda   INTEGER,
    median_hyperscore REAL,
    ms2_scan_rate    REAL,
    median_delta_mass_ppm REAL,

    -- Shared
    missed_cleavage_rate REAL,
    pct_charge_1    REAL,
    pct_charge_2    REAL,
    pct_charge_3    REAL,

    -- Chromatography
    grs_score        INTEGER,
    tic_auc          REAL,
    peak_rt_min      REAL,
    irt_max_deviation_min REAL,
    ms2_fill_time_median_ms REAL,

    -- Gate result
    gate_result      TEXT,         -- "pass" | "warn" | "fail"
    failed_gates     TEXT,         -- JSON array
    diagnosis        TEXT,

    -- Community
    submitted_to_benchmark INTEGER DEFAULT 0,
    submission_id    TEXT
);

CREATE INDEX idx_runs_instrument ON runs(instrument);
CREATE INDEX idx_runs_date ON runs(run_date);
```

---

## 10. Community Benchmark — Track B (DIA)

### Overview

Track B benchmarks DIA instrument performance using precursor count at 1% FDR as
the primary metric. All submissions use the community-standardized DIA-NN search
(fixed FASTA + spectral library) so comparisons are valid across labs.

### Why precursors, not proteins

Protein count is the worst instrument benchmark — confounded by FASTA choice,
inference algorithm, FDR setting, and search engine. Precursors at 1% FDR with
a standardized search are the cleanest instrument signal. The hierarchy:

```
Fragment XICs/precursor  ← purest instrument signal
Precursor count @ 1% FDR ← PRIMARY benchmark metric
Peptide count             ← secondary, slight FASTA sensitivity
Protein count             ← contextual only, do not use for ranking
```

### DIA community composite score

```
DIA_Score =
  40 × percentile_rank(n_precursors)                    # primary
+ 25 × percentile_rank(n_peptides)                      # depth
+ 20 × (100 - percentile_rank(median_cv_precursor))     # lower CV = better
+ 15 × percentile_rank(grs_score)                       # LC health
```

Score of 75 = better than 75% of comparable submissions in your cohort.
Computed nightly within cohort (instrument_family × gradient_bucket × amount_bucket).

### Cohort bucketing

```python
def gradient_bucket(min: int) -> str:
    if min <= 30:  return "ultra-short"
    if min <= 45:  return "short"
    if min <= 75:  return "standard-1h"   # most submissions
    if min <= 120: return "long-2h"
    return "extended"

def amount_bucket(ng: float) -> str:
    if ng <= 100:  return "low"
    if ng <= 300:  return "mid"           # covers 200 + 250 ng
    if ng <= 600:  return "high"
    return "very-high"

# Cohort minimum: 5 submissions before leaderboard appears
```

---

## 11. Community Benchmark — Track A (DDA)

### Overview

Track A benchmarks DDA instrument performance using PSM count at 1% FDR as the
primary metric. Uses Sage + Percolator with frozen community parameters.

### Why PSMs not peptides for DDA

PSMs are the raw instrument throughput signal — each PSM = one MS2 scan that
produced a confident identification. PSM count with standardized parameters reflects
duty cycle + ion flux directly. Unique peptide count is useful as secondary but
is mildly confounded by missed cleavage settings and variable mods.

### DDA community composite score

```
DDA_Score =
  35 × percentile_rank(n_psms)                          # primary throughput
+ 25 × percentile_rank(n_peptides_dda)                  # depth
+ 20 × percentile_rank(pct_delta_mass_lt5ppm)           # mass accuracy
+ 20 × percentile_rank(ms2_scan_rate)                   # duty cycle
```

### Acquisition mode auto-detection

```
timsTOF .d + MsmsType=9 in Frames → diaPASEF → Track B
timsTOF .d + MsmsType=8 in Frames → ddaPASEF → Track A
Thermo .raw + ScanFilter "DIA" → Track B
Thermo .raw + ScanFilter "dd-MS2" → Track A
```

---

## 12. Community Benchmark — Track C (Dual Mode Fingerprint)

### Overview

When a lab submits both Track A and Track B from the same instrument within 24h,
STAN links them via `session_id` and computes the instrument health fingerprint.

### The fingerprint radar

Six axes, each 0–100 within cohort:

1. Mass accuracy → `pct_delta_mass_lt5ppm` (DDA)
2. Duty cycle → `ms2_scan_rate` percentile (DDA)
3. Spectral quality → `median_hyperscore` percentile (DDA)
4. Precursor depth → `n_precursors` percentile (DIA)
5. Quantitative CV → inverted `median_cv_precursor` percentile (DIA)
6. Fragment sensitivity → `median_fragments_per_precursor × pct_fragments_quantified` (DIA)

A perfectly healthy instrument shows a regular hexagon.

### Failure pattern recognition

| Pattern | Diagnosis |
|---------|-----------|
| All axes compressed | Source fouling or spray instability |
| Fragment sensitivity + CV drop only | Column aging or degradation |
| Mass accuracy alone collapses | Calibration drift — recalibrate |
| Duty cycle low, spectral quality high | AGC/fill time misconfigured |
| Precursor depth low, fragment sensitivity normal | Search/library issue, not hardware |
| CV high, IDs normal | LC injection volume or carryover |

### Peptide recovery ratio

```
peptide_recovery_ratio = n_peptides_dia / n_peptides_dda
```

Expected values:
- timsTOF diaPASEF: 0.95–1.15 (DIA often exceeds DDA)
- Astral DIA: 1.00–1.25
- Exploris 480 DIA: 0.80–1.00
- < 0.75 on any instrument = DIA method optimization needed

---

## 13. HF Dataset Infrastructure

### Repository: `brettsp/stan-benchmark`

```
stan-community-benchmark/
├── README.md                          # dataset card
├── benchmark_latest.parquet           # consolidated, all validated submissions
├── benchmark_flagged.parquet          # flagged (transparency)
├── submissions/                       # individual submission parquets
├── community_fasta/
│   ├── human_opg_202604.fasta
│   └── human_opg_202604.fasta.md5
├── community_library/
│   └── human_opg_202604.predicted.speclib
└── cohort_stats/
    └── cohort_percentiles_latest.json
```

### Hard validation gates

```python
HARD_GATES = {
    # DIA
    "n_precursors_min": 1000,
    "median_cv_precursor_max": 60.0,
    "pct_charge_1_max": 0.50,
    "missed_cleavage_rate_max": 0.60,
    # DDA
    "n_psms_min": 5000,
    "n_peptides_dda_min": 3000,
    "pct_delta_mass_lt5ppm_min": 0.50,
    "ms2_scan_rate_min": 5.0,
}
```

### Privacy

- Raw files: **never uploaded**
- Patient/sample metadata: **never collected**
- Serial numbers: stored but never exposed in API or downloads
- Anonymous submissions: `display_name = null` → shown as "Anonymous Lab"
- Deletion: submit `submission_id` via GitHub issue, processed within 7 days
- License: CC BY 4.0

### Nightly consolidation (`stan/community/scripts/consolidate.py`)

Runs via GitHub Actions at 4am UTC. Downloads all `submissions/*.parquet`, validates,
computes cohort percentiles, writes `benchmark_latest.parquet` and
`cohort_percentiles_latest.json` back to HF Dataset.

---

## 14. Instrument Config (instruments.yml)

Located at `~/.stan/instruments.yml`. Hot-reloaded without restart.
Editable via dashboard UI or directly.

```yaml
# STAN instrument watcher configuration
# Hot-reloaded — no restart needed after edits

instruments:

  - name: "timsTOF Ultra"
    vendor: "bruker"           # "bruker" | "thermo"
    model: "timsTOF Ultra"     # used for threshold lookup
    watch_dir: "/mnt/instruments/timstof-ultra/raw"
    output_dir: "/mnt/instruments/timstof-ultra/stan_out"
    extensions: [".d"]
    stable_secs: 60            # bruker: wait for .d dir to stop growing
    enabled: true
    qc_modes: ["dia", "dda"]   # auto-detect from analysis.tdf
    hive_partition: "high"
    hive_account: "genome-center-grp"
    hive_mem: "32G"
    community_submit: true     # auto-submit to benchmark after QC

  - name: "Astral"
    vendor: "thermo"
    model: "Astral"
    watch_dir: "\\\\ASTRAL-01\\Data\\QC"   # UNC path for Windows share
    output_dir: "/mnt/instruments/astral/stan_out"
    extensions: [".raw"]
    stable_secs: 30
    enabled: true
    qc_modes: ["dia"]
    hive_partition: "high"
    hive_account: "genome-center-grp"
    community_submit: true

  - name: "Exploris 480"
    vendor: "thermo"
    model: "Exploris 480"
    watch_dir: "\\\\EXPLORIS-480\\Data\\QC"
    output_dir: "/mnt/instruments/exploris480/stan_out"
    extensions: [".raw"]
    stable_secs: 30
    enabled: false             # set true to resume watching
    qc_modes: ["dia", "dda"]
    hive_partition: "high"
    hive_account: "genome-center-grp"
    community_submit: false    # opt out this instrument from benchmark
```

### Community config: `~/.stan/community.yml`

```yaml
# Community benchmark settings
hf_token: ""              # paste HF token here (write access to brettsp/stan-benchmark)
display_name: "UC Davis Proteomics Core"   # shown on leaderboard; "" for anonymous
submit_by_default: false  # if true, auto-submit without review prompt
hela_source: "Pierce HeLa Protein Digest Standard"
institution_type: "core_facility"   # "core_facility" | "academic_lab" | "industry"
```

---

## 15. Tech Stack

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| Watcher daemon | Python `watchdog` | Cross-platform, robust, minimal deps |
| File stability | Custom `StabilityTracker` | Vendor-specific logic needed |
| Acquisition mode detection | `sqlite3` (tdf) + subprocess (raw) | Native, no extra deps |
| DIA search | DIA-NN 2.3 via SLURM | Gold standard for DIA |
| DDA search | Sage + Percolator via SLURM | Rust-native, fast, open, excellent FDR |
| Metrics extraction | Polars + PyArrow | Fast, memory-efficient parquet reads |
| Local database | SQLite | Zero-config, portable, sufficient scale |
| API backend | FastAPI | Fast, async, auto-docs |
| Frontend | React + Tailwind | Component-based, fast build |
| HPC interface | `paramiko` (SSH) + SLURM | Matches existing DE-LIMP HPC pattern |
| Community dataset | HF Datasets (Parquet) | Free, versioned, public, no server |
| Nightly consolidation | GitHub Actions | Free CI, no server needed |
| CLI | Typer | Clean, typed, auto-help |

---

## 16. Implementation Phases

### Phase 0 — Repository bootstrap (§2 above)
- [x] Create `bsphinney/stan` GitHub repo
- [x] Create `brettsp/stan` HF Space
- [x] Create `brettsp/stan-benchmark` HF Dataset
- [x] Initialize directory structure + pyproject.toml
- [x] Set up GitHub Actions (CI + nightly consolidation)

### Phase 1 — Core watcher + search (P0, ~1 week)
- [ ] `instruments.yml` config loader with hot-reload
- [ ] `watchdog` daemon for `.d` and `.raw` files
- [ ] Bruker acquisition mode detection from `analysis.tdf`
- [ ] Thermo acquisition mode detection from `.raw` metadata
- [ ] DIA-NN SLURM job builder + submission via paramiko
- [ ] Sage SLURM job builder + submission via paramiko
- [ ] Poll SLURM job completion, retrieve output

### Phase 2 — Metrics + gating (P0, ~1 week)
- [ ] `extract_dia_metrics()` from `report.parquet` (Polars)
- [ ] `extract_dda_metrics()` from Percolator output
- [ ] `compute_grs()` — Gradient Reproducibility Score
- [ ] `compute_irt_deviation()` — iRT RT stability
- [ ] `evaluate_gates()` — thresholds.yml, pass/warn/fail
- [ ] `write_hold_flag()` — queue pause mechanism
- [ ] Write all metrics to SQLite

### Phase 3 — Dashboard (P1, ~1 week)
- [ ] FastAPI backend with all routes
- [ ] React frontend: live runs, history table, trend charts
- [ ] Instrument config UI (form + YAML editor)
- [ ] Thresholds UI
- [ ] `stan watch` and `stan dashboard` CLI commands

### Phase 4 — Community benchmark (P1, ~1 week)
- [ ] `extract_community_metrics()` using community search output
- [ ] `validate_submission()` with hard gates + soft flags
- [ ] `submit_to_benchmark()` → HF Dataset API
- [ ] Community tab: DDA / DIA / Both mode selector
- [ ] Distribution plots + leaderboard per mode
- [ ] `consolidate.py` nightly script

### Phase 5 — Track C fingerprint (P2, ~3 days)
- [ ] `session_id` linking of DDA + DIA submissions
- [ ] Peptide recovery ratio computation
- [ ] Radar fingerprint chart (6-axis)
- [ ] Plain-English failure diagnosis callouts

### Phase 6 — Polish (P2, ongoing)
- [ ] `stan init` wizard (creates instruments.yml interactively)
- [ ] Column aging detection (LOESS trend on TIC AUC over time)
- [ ] Email/Slack alert integration
- [ ] `pip install stan-proteomics` packaging

---

## 17. Appendix: Expected HeLa Reference Ranges

### DIA (DIA-NN community params, 1h gradient, 200–250 ng)

| Instrument | Precursors | Peptides | Frags/precursor | Median CV |
|------------|-----------|----------|-----------------|-----------|
| timsTOF Ultra 2 | 18,000–25,000 | 12,000–17,000 | 7–10 | 4–8% |
| timsTOF Ultra | 16,000–22,000 | 11,000–15,000 | 7–10 | 4–9% |
| timsTOF Pro 2 | 12,000–17,000 | 9,000–12,000 | 6–9 | 5–10% |
| Astral | 20,000–28,000 | 14,000–19,000 | 8–12 | 3–7% |
| Exploris 480 | 10,000–15,000 | 8,000–11,000 | 6–8 | 6–12% |
| Exploris 240 | 8,000–12,000 | 6,000–9,000 | 5–8 | 7–14% |

### DDA (Sage community params, 1h gradient, 200–250 ng)

| Instrument | PSMs | Peptides | Median hyperscore | Mass acc <5ppm |
|------------|------|----------|-------------------|----------------|
| timsTOF Ultra (ddaPASEF) | 50,000–90,000 | 14,000–22,000 | 28–35 | >95% |
| timsTOF Pro 2 (ddaPASEF) | 35,000–65,000 | 11,000–17,000 | 25–32 | >95% |
| Astral | 40,000–70,000 | 12,000–18,000 | 30–38 | >98% |
| Exploris 480 | 30,000–55,000 | 10,000–16,000 | 28–36 | >98% |
| Exploris 240 | 20,000–40,000 | 8,000–13,000 | 25–33 | >97% |

### Peptide recovery ratio (DIA/DDA, same instrument)

| Instrument | Expected range | Notes |
|------------|---------------|-------|
| timsTOF Ultra diaPASEF | 0.95–1.15 | DIA often exceeds DDA depth |
| timsTOF Pro 2 diaPASEF | 0.85–1.05 | |
| Astral DIA | 1.00–1.25 | Very high DIA efficiency |
| Exploris 480 DIA | 0.80–1.00 | Window-scheme dependent |
| Exploris 240 DIA | 0.75–0.95 | |

Ratio > 1.0 is expected for modern diaPASEF instruments.
Ratio < 0.75 on any instrument = DIA method needs optimization.

---

*STAN — Standardized proteomic Throughput ANalyzer*  
*Author: Brett Stanley Phinney, UC Davis Proteomics Core*  
*Built with Claude (Anthropic)*  
*MIT License · Community data: CC BY 4.0*
