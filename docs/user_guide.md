# STAN User Guide

> *Know your instrument.*

This guide covers installation, configuration, daily use, and troubleshooting for STAN -- the Standardized proteomic Throughput ANalyzer.

**Audience**: Proteomics core facility staff, instrument operators, and researchers who want automated QC monitoring and community benchmarking for their mass spectrometers.

> **Status note**: The Python backend (watcher, search dispatch, metric extraction, gating, scoring, DB) is implemented and tested. Items marked **(planned)** below are not yet available. See the [Implementation Status](../README.md#implementation-status) table in the README for the full breakdown.

---

## Table of Contents

1. [Installation](#installation)
2. [Configuration Walkthrough](#configuration-walkthrough)
3. [Watcher Daemon](#watcher-daemon)
4. [Dashboard](#dashboard)
5. [Community Benchmark Submission](#community-benchmark-submission)
6. [QC Metric Reference](#qc-metric-reference)
7. [Gradient Reproducibility Score (GRS)](#gradient-reproducibility-score-grs)
8. [Run and Done Gating](#run-and-done-gating)
9. [Column Health Monitoring](#column-health-monitoring)
10. [Troubleshooting](#troubleshooting)

---

## Installation

### Requirements

- Python 3.10 or newer
- Access to a SLURM HPC cluster (e.g., UC Davis Hive) with DIA-NN and Sage installed
- SSH access to the cluster (STAN uses `paramiko` for job submission)
- For Thermo DDA runs: ThermoRawFileParser installed on the cluster (requires .NET 8 runtime)

### Install from PyPI (coming soon)

```bash
pip install stan-proteomics          # not yet published — use source install below
```

### Install from Source (Development)

```bash
git clone https://github.com/bsphinney/stan.git
cd stan
pip install -e ".[dev]"
```

The `[dev]` extra installs pytest, ruff, and mypy for development and testing.

### Verify Installation

```bash
stan version
```

This should print the installed STAN version. If the `stan` command is not found, ensure your Python scripts directory is on your `PATH`.

---

## Configuration Walkthrough

### Initialize Configuration

```bash
stan init
```

This creates `~/.stan/` and copies three default configuration files into it:

| File | Purpose |
|------|---------|
| `instruments.yml` | Instrument watch directories, vendor, model, SLURM settings |
| `thresholds.yml` | QC pass/warn/fail thresholds per instrument model |
| `community.yml` | HuggingFace token and community benchmark preferences |

All three files are YAML. Edit them with any text editor, or use the dashboard UI once it is running.

### instruments.yml

This is the primary configuration file. It tells STAN which instruments to watch, where their raw files land, and how to submit search jobs.

```yaml
hive:
  host: "hive.ucdavis.edu"       # your SLURM cluster hostname
  user: "your_username"           # SSH username

instruments:

  - name: "timsTOF Ultra"
    vendor: "bruker"              # "bruker" or "thermo"
    model: "timsTOF Ultra"        # used for threshold lookup in thresholds.yml
    watch_dir: "/mnt/instruments/timstof-ultra/raw"
    output_dir: "/mnt/instruments/timstof-ultra/stan_out"
    extensions: [".d"]            # file extensions to watch for
    stable_secs: 60               # seconds of no size change before processing
    enabled: true                 # set false to pause watching
    qc_modes: ["dia", "dda"]      # modes to accept (auto-detected from metadata)
    hive_partition: "high"        # SLURM partition
    hive_account: "your-grp"      # SLURM account
    hive_mem: "32G"               # SLURM memory request
    community_submit: true        # auto-submit to community benchmark
    hela_amount_ng: 50            # injection amount in ng (default: 50)
    spd: 60                       # samples per day (primary — Evosep/Vanquish method)
    gradient_length_min: 21       # gradient length in minutes (fallback if spd not set)
```

**Key fields explained:**

`vendor` -- Must be `"bruker"` or `"thermo"`. Determines how file stability is checked and which acquisition mode detector is used.

`watch_dir` -- The directory where the instrument writes raw files. For Bruker, this is where `.d` directories appear. For Thermo, where `.raw` files land. This can be a local path, a network mount, or a UNC path.

`output_dir` -- Where STAN writes search results, metrics, and HOLD flags.

`stable_secs` -- How long a file must remain unchanged in size before STAN considers the acquisition complete. For Bruker `.d` directories, which are written to continuously during acquisition, 60 seconds is recommended. For Thermo `.raw` files, which are closed at acquisition end, 30 seconds is sufficient.

`qc_modes` -- Which acquisition modes STAN should process. Setting `["dia"]` means STAN will only process DIA acquisitions and ignore DDA files from this instrument. Setting `["dia", "dda"]` processes both.

`hela_amount_ng` -- The amount of HeLa digest injected in nanograms. This determines which amount bucket your community benchmark submissions fall into. The default is 50 ng. Adjust this to match your standard QC protocol.

`gradient_length_min` -- The active gradient length in minutes (not total run time). This determines the gradient bucket for community benchmark cohort assignment.

`community_submit` -- When `true`, STAN automatically submits QC metrics to the community benchmark after each successful QC run. Set to `false` to disable for specific instruments.

**Thermo-specific fields:**

```yaml
  - name: "Astral"
    vendor: "thermo"
    raw_handling: "native"        # "native" or "convert_mzml"
    trfp_path: "/path/to/ThermoRawFileParser.dll"
    keep_mzml: false              # delete converted mzML after search
```

`raw_handling` -- For DIA runs, DIA-NN 2.1+ reads `.raw` files natively on Linux, so `"native"` is the default. If native reading fails in your container environment, set `"convert_mzml"` to run ThermoRawFileParser first. For DDA runs (Sage), conversion to mzML is always performed regardless of this setting.

`trfp_path` -- Path to the ThermoRawFileParser DLL on your cluster. Required for any Thermo DDA workflow and for Thermo DIA when `raw_handling` is `"convert_mzml"`.

`keep_mzml` -- Whether to keep the converted mzML file after the search completes. Default is `false` (delete to save disk space). Set `true` if you want mzML files for downstream use.

### Hot-Reload

The watcher daemon checks `instruments.yml` for changes every 30 seconds. You can add, remove, or modify instruments without restarting the daemon. Changes take effect on the next poll cycle.

### thresholds.yml

Defines QC thresholds for each instrument model. When a metric crosses a threshold, the run is marked as WARN or FAIL.

```yaml
thresholds:

  default:
    dia:
      n_precursors_min: 5000           # minimum precursors at 1% FDR
      median_cv_precursor_max: 20.0    # maximum median CV (percent)
      missed_cleavage_rate_max: 0.20   # maximum missed cleavage fraction
      pct_charge_1_max: 0.30           # maximum fraction of +1 precursors
      grs_score_min: 50                # minimum GRS score
      irt_max_deviation_max: 5.0       # maximum iRT deviation (minutes)
    dda:
      n_psms_min: 10000                # minimum PSMs at 1% FDR
      pct_delta_mass_lt5ppm_min: 0.70  # minimum fraction with mass error < 5 ppm
      ms2_scan_rate_min: 10.0          # minimum MS2 scans per minute

  "timsTOF Ultra":
    dia:
      n_precursors_min: 10000          # tighter -- this instrument should hit 15k+
      median_cv_precursor_max: 15.0
      grs_score_min: 65
    dda:
      n_psms_min: 30000
      pct_delta_mass_lt5ppm_min: 0.90
```

Model-specific entries override the `default` values. If a metric is not specified for a model, the `default` threshold applies.

Thresholds should be tuned to your specific methods and expected performance. Start with the defaults and adjust after collecting a few weeks of data.

### community.yml

Controls your participation in the community benchmark.

```yaml
hf_token: ""                          # HuggingFace token with write access
display_name: "Your Lab Name"         # shown on the leaderboard
submit_by_default: false              # true = auto-submit without prompting
hela_source: "Pierce HeLa Protein Digest Standard"
institution_type: "core_facility"     # core_facility | academic_lab | industry
```

To obtain a HuggingFace token, create an account at [huggingface.co](https://huggingface.co), go to Settings, then Access Tokens, and create a token with write access. Paste it in the `hf_token` field.

Leave `display_name` blank for anonymous submissions (shown as "Anonymous Lab" on the leaderboard).

---

## Watcher Daemon

### Starting the Watcher

```bash
stan watch
```

The watcher runs in the foreground. It monitors all directories listed in `instruments.yml` where `enabled: true`. For production use, run it in a `tmux` or `screen` session, or set it up as a systemd service.

### What Happens When a File is Detected

1. **Stability check** -- STAN monitors the file/directory size at regular intervals. Once the size has not changed for `stable_secs`, the acquisition is considered complete.

2. **Mode detection** -- For Bruker `.d` files, STAN reads the `MsmsType` column from the `Frames` table in `analysis.tdf` (an SQLite database inside the `.d` directory). Values: 8 = ddaPASEF (DDA), 9 = diaPASEF (DIA). For Thermo `.raw` files, ThermoRawFileParser extracts scan filter metadata.

3. **Search dispatch** -- Based on mode, a SLURM job is submitted:
   - DIA: DIA-NN with community-standardized parameters
   - DDA: Sage (with ThermoRawFileParser conversion for Thermo `.raw` only)

4. **Metric extraction** -- After the search job completes, STAN extracts QC metrics from the search output using Polars.

5. **Gating** -- Metrics are compared against thresholds. Result: PASS, WARN, or FAIL.

6. **HOLD flag** -- If the result is FAIL, a `HOLD_{run_name}.txt` file is written to the output directory. This can be used to pause the autosampler queue.

7. **Database** -- All metrics and the gate result are stored in the local SQLite database.

8. **Community submission** -- If `community_submit: true`, aggregate metrics are uploaded to the HF Dataset.

### Stopping the Watcher

Press `Ctrl-C` to shut down gracefully. In-progress SLURM jobs continue running on the cluster; STAN will not cancel them.

---

## Dashboard

### Starting the Dashboard

```bash
stan dashboard
```

The dashboard is served at [http://localhost:8421](http://localhost:8421) by default. Change the port or host with flags:

```bash
stan dashboard --port 9000 --host 0.0.0.0
```

API documentation is available at `http://localhost:8421/docs` (Swagger UI).

### Dashboard Views

The dashboard currently serves a FastAPI backend with JSON API endpoints. The React frontend is **(planned)**. In the meantime, you can access the API directly:

- `/api/runs` -- list recent QC runs
- `/api/runs/{run_id}` -- full metric detail for a run
- `/api/trends/{instrument}` -- time-series metrics for trend analysis
- `/api/instruments` -- current instrument config (hot-reloaded)
- `/api/thresholds` -- current QC thresholds
- `/api/community/submit` -- submit a run to the community benchmark
- `/docs` -- Swagger UI for interactive API exploration

**Planned frontend views:**

**Live Runs** -- Status cards for each instrument showing the most recent run, GRS badge, gate result (PASS/WARN/FAIL), and time since last acquisition.

**Run History** -- Sortable, scrollable table of all runs. Click any run to see full metric detail.

**Trend Charts** -- Time-series plots of precursor count, peptide count, CV, and GRS over time for each instrument. Includes LOESS trendlines for detecting gradual drift.

**Column Health** -- TIC AUC and peak retention time plotted over calendar time. Highlights periods of degradation.

**Community Benchmark** -- Tabs for DDA (Track A), DIA (Track B), and Both (Track C). Shows distribution plots, your instrument's position, and the leaderboard for your cohort.

**Instrument Config** -- Edit `instruments.yml` through a form or raw YAML editor with live preview.

---

## Community Benchmark Submission

### Prerequisites

1. A HuggingFace account with a write-access token
2. The token configured in `~/.stan/community.yml`
3. `community_submit: true` on the instrument in `instruments.yml`
4. A valid HeLa QC run that passes the community hard gates

### Hard Validation Gates

Submissions must pass minimum quality thresholds to be accepted. These prevent corrupted or obviously failed runs from entering the benchmark:

**DIA (Track B):**
- At least 1,000 precursors at 1% FDR
- Median CV below 60%
- Singly-charged precursor fraction below 50%
- Missed cleavage rate below 60%

**DDA (Track A):**
- At least 5,000 PSMs at 1% FDR
- At least 3,000 unique peptides
- At least 50% of PSMs with mass error under 5 ppm
- MS2 scan rate at least 5 scans per minute

### Automatic Submission **(planned)**

When `community_submit: true` and `submit_by_default: true`, STAN will automatically submit after every QC run that passes hard gates. No manual intervention required. (The submission code is implemented; auto-triggering from the watcher is not yet wired up.)

### Manual Submission

Via the API: `POST /api/community/submit` with your `run_id`. Once the React frontend is built **(planned)**, a "Submit to Benchmark" button will be available on each run detail page.

### What Gets Submitted

Only aggregate metrics are submitted -- never raw files, never patient data. Each submission contains:

- Submission ID (UUID)
- Instrument family and model
- Acquisition mode (DIA or DDA)
- Gradient length and injection amount
- Precursor count, peptide count, protein count
- PSM count (DDA)
- Median CV, median fragments per precursor
- GRS score, missed cleavage rate
- Cohort ID (instrument family + gradient bucket + amount bucket)

### Deletion

To delete a submission, file a GitHub issue at [github.com/bsphinney/stan](https://github.com/bsphinney/stan) with your `submission_id`. Deletions are processed within 7 days.

---

## QC Metric Reference

### Metric Hierarchy

STAN follows a deliberate hierarchy of metric importance. This reflects how cleanly each metric reports on instrument performance versus being confounded by informatics choices:

```
Fragment XICs / precursor    <-- purest instrument signal
Precursor count @ 1% FDR    <-- PRIMARY metric for DIA benchmarking
PSM count @ 1% FDR          <-- PRIMARY metric for DDA benchmarking
Peptide count                <-- secondary (slight FASTA sensitivity)
Protein count                <-- contextual only (heavily confounded)
```

**Why not protein count?** Protein count depends heavily on the FASTA database used, the protein inference algorithm, how shared peptides are handled, and FDR propagation settings. Two identical raw files can produce wildly different protein counts depending on search settings. Precursor and PSM counts with a standardized search provide a much cleaner signal of instrument performance.

### DIA Metrics (from DIA-NN report.parquet)

| Metric | Description | Healthy Range (1h, timsTOF Ultra) |
|--------|-------------|-----------------------------------|
| `n_precursors` | Unique precursors at 1% FDR | 16,000-22,000 |
| `n_peptides` | Unique stripped sequences at 1% FDR | 11,000-15,000 |
| `n_proteins` | Unique protein groups at 1% protein FDR | Contextual only |
| `median_fragments_per_precursor` | Median fragment XICs per precursor | 7-10 |
| `pct_fragments_quantified` | Fraction of extracted fragments with nonzero intensity | >0.80 |
| `median_cv_precursor` | Median CV of precursor intensities across replicates | 4-9% |
| `missed_cleavage_rate` | Fraction of precursors with at least one missed cleavage | <0.15 |
| `pct_charge_1` | Fraction of singly-charged precursors | <0.10 |
| `pct_charge_2` | Fraction of doubly-charged precursors | Informational |
| `pct_charge_3` | Fraction of triply-charged precursors | Informational |

### DDA Metrics (from Sage output)

| Metric | Description | Healthy Range (1h, timsTOF Ultra) |
|--------|-------------|-----------------------------------|
| `n_psms` | PSMs at 1% FDR | 50,000-90,000 |
| `n_peptides_dda` | Unique peptides at 1% FDR | 14,000-22,000 |
| `median_hyperscore` | Median Sage hyperscore | 28-35 |
| `ms2_scan_rate` | PSMs per minute of gradient time | Instrument-dependent |
| `median_delta_mass_ppm` | Median absolute precursor mass error (ppm) | <3 ppm |
| `pct_delta_mass_lt5ppm` | Fraction of PSMs with mass error under 5 ppm | >95% |

### Chromatography Metrics

| Metric | Description | Interpretation |
|--------|-------------|----------------|
| `grs_score` | Gradient Reproducibility Score (0-100) | See GRS section below |
| `tic_auc` | Total ion chromatogram area under curve | Track longitudinally, not absolute |
| `peak_rt_min` | Retention time of TIC peak (minutes) | Should be consistent run-to-run |
| `irt_max_deviation_min` | Maximum iRT peptide RT deviation (minutes) | <3 min for healthy column |
| `ms2_fill_time_median_ms` | Median MS2 ion accumulation time (ms) | Platform-dependent |

---

## Gradient Reproducibility Score (GRS)

The GRS condenses LC chromatography health into a single number from 0 to 100.

### Components

```
GRS = 40 x shape_r_scaled      (TIC peak shape correlation to reference)
    + 25 x auc_scaled           (TIC AUC z-score, clamped to 0-1)
    + 20 x peak_rt_scaled       (peak RT deviation from expected)
    + 15 x carryover_scaled     (inter-run carryover assessment)
```

### Interpretation

| Score | Status | Action |
|-------|--------|--------|
| 90-100 | Excellent | System is performing optimally. No action needed. |
| 70-89 | Good | Normal operating range. Continue monitoring. |
| 50-69 | Watch | Performance is declining. Inspect column condition, buffer freshness, and spray stability in the near future. |
| Below 50 | Investigate | Likely LC or source problem. Check column, trap column, spray tip, and mobile phases before running more samples. |

### Longitudinal Tracking

GRS is stored in the SQLite database for every run. The dashboard trend view plots GRS over time with a LOESS trendline. A steady downward trend in GRS, even if individual runs still pass thresholds, typically indicates column aging and should prompt column replacement.

---

## Run and Done Gating

When a QC run fails one or more thresholds, STAN writes a HOLD flag file to the instrument's output directory:

```
HOLD_{run_name}.txt
```

The file contains the run name, which gates failed, and a plain-English diagnosis of what the failure pattern suggests.

### Diagnosis Examples

| Failed Metrics | Diagnosis |
|---------------|-----------|
| Low ID count, normal GRS | Search or library issue. Check spectral library version, FASTA, or DIA window scheme. |
| Low IDs, low GRS | LC or source problem. Check column condition, trap column, spray stability. |
| High missed cleavages | Incomplete digestion. Check trypsin activity, digestion time, or protein denaturation. |
| Elevated +1 charge | Source contamination, buffer impurity, or electrospray instability. |
| High CV, normal IDs | LC reproducibility issue. Check injection volume consistency, carryover, or column equilibration. |
| Poor mass accuracy | Instrument needs recalibration. Run a calibration file before next injection. |

### Integrating with Your Queue

The HOLD flag file can be monitored by:
- Autosampler queue software that checks for flag files before starting the next injection
- A script that polls the output directory
- A webhook triggered by the STAN dashboard API

---

## Column Health Monitoring

STAN tracks TIC AUC and peak retention time over calendar time to detect gradual column degradation.

```bash
stan column-health "timsTOF Ultra"
```

This command analyzes the longitudinal trend and reports:

- **healthy** -- no significant drift detected
- **watch** -- gradual decline in TIC AUC or shift in peak RT; plan column replacement
- **degraded** -- significant performance drop; replace column before running valuable samples

A minimum of 10 runs with TIC AUC data is required for the assessment.

---

## Troubleshooting

### "Config file not found"

Run `stan init` to create the default configuration files in `~/.stan/`. If you previously initialized, check that the files exist:

```bash
ls -la ~/.stan/
```

### Watcher does not detect new files

- Verify `watch_dir` in `instruments.yml` points to the correct directory and is accessible
- Verify `enabled: true` for the instrument
- Check that the file extension matches the `extensions` list (`.d` for Bruker, `.raw` for Thermo)
- For network-mounted directories, ensure the mount is active and responsive
- Check `stan watch` output for error messages (run with `-v` for debug logging: `stan watch -v`)

### SLURM job fails

- Verify `hive_partition` and `hive_account` are correct in `instruments.yml`
- Ensure DIA-NN and Sage are available on the cluster (check module loads or paths)
- For Thermo DDA runs, verify `trfp_path` points to a valid ThermoRawFileParser installation
- Check the SLURM job output log in the `output_dir` for error details
- DIA-NN on Linux requires .NET SDK 8.0.407 or newer

### Low identification counts

- Verify the correct acquisition mode was detected (check dashboard or logs)
- For DIA: check that the DIA window scheme matches the community library
- For DDA: check MS2 scan rate -- low scan rate may indicate a method configuration issue
- Compare against expected reference ranges for your instrument model (see appendix in STAN_MASTER_SPEC.md)
- Check GRS score -- if GRS is also low, the issue is likely LC or source, not search

### Community submission rejected

Submissions must pass hard validation gates. Common rejection reasons:
- Too few identifications (instrument underperforming or wrong mode detected)
- Extremely high CV (sample preparation issue)
- High singly-charged fraction (source contamination or spray problem)

Fix the underlying instrument issue and re-run the QC acquisition.

### Dashboard does not start

- Check that port 8421 is not already in use: `lsof -i :8421`
- Try a different port: `stan dashboard --port 9000`
- Check for Python import errors: `python -c "from stan.dashboard.server import app"`

### Database issues

The SQLite database is stored at `~/.stan/stan.db` by default. If it becomes corrupted:

1. Stop the watcher and dashboard
2. Back up the database: `cp ~/.stan/stan.db ~/.stan/stan.db.bak`
3. Delete and let STAN recreate it: `rm ~/.stan/stan.db`
4. Restart the watcher -- historical data will be lost but the schema will be recreated

### File stability never triggers

- For Bruker `.d`: the default `stable_secs: 60` means the `.d` directory must not change size for 60 consecutive seconds. If your instrument writes post-acquisition calibration data, increase this value.
- For Thermo `.raw`: the default `stable_secs: 30` is usually sufficient since Thermo closes the file handle at acquisition end. If triggering too early on slow network mounts, increase to 45-60 seconds.

---

## Expected HeLa Reference Ranges

These ranges assume a standard 1-hour gradient with 200-250 ng injection. Your thresholds should be tuned based on your specific methods and expected performance.

### DIA (DIA-NN, community standardized search)

| Instrument | Precursors | Peptides | Fragments/Precursor | Median CV |
|------------|-----------|----------|---------------------|-----------|
| timsTOF Ultra 2 | 18,000-25,000 | 12,000-17,000 | 7-10 | 4-8% |
| timsTOF Ultra | 16,000-22,000 | 11,000-15,000 | 7-10 | 4-9% |
| timsTOF Pro 2 | 12,000-17,000 | 9,000-12,000 | 6-9 | 5-10% |
| Astral | 20,000-28,000 | 14,000-19,000 | 8-12 | 3-7% |
| Exploris 480 | 10,000-15,000 | 8,000-11,000 | 6-8 | 6-12% |
| Exploris 240 | 8,000-12,000 | 6,000-9,000 | 5-8 | 7-14% |

### DDA (Sage, community standardized search)

| Instrument | PSMs | Peptides | Median Hyperscore | Mass Acc <5 ppm |
|------------|------|----------|-------------------|-----------------|
| timsTOF Ultra (ddaPASEF) | 50,000-90,000 | 14,000-22,000 | 28-35 | >95% |
| timsTOF Pro 2 (ddaPASEF) | 35,000-65,000 | 11,000-17,000 | 25-32 | >95% |
| Astral | 40,000-70,000 | 12,000-18,000 | 30-38 | >98% |
| Exploris 480 | 30,000-55,000 | 10,000-16,000 | 28-36 | >98% |
| Exploris 240 | 20,000-40,000 | 8,000-13,000 | 25-33 | >97% |

---

*STAN -- Standardized proteomic Throughput ANalyzer*
*Author: Brett Stanley Phinney, UC Davis Proteomics Core*
*MIT License -- Community data: CC BY 4.0*
