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
5. [Baseline Builder](#baseline-builder)
6. [Community Benchmark Submission](#community-benchmark-submission)
7. [QC Metric Reference](#qc-metric-reference)
8. [Instrument Performance Score (IPS)](#instrument-performance-score-ips)
9. [Run and Done Gating](#run-and-done-gating)
10. [Column Health Monitoring](#column-health-monitoring)
11. [Troubleshooting](#troubleshooting)

---

## Installation

### Requirements

- Python 3.10 or newer
- Windows 10/11 (recommended for instrument workstations), Linux, or macOS
- DIA-NN and Sage are auto-installed by the Windows installer; on Linux/macOS install them manually
- For Thermo DDA runs: ThermoRawFileParser (auto-downloaded by STAN on first use on Windows)
- Optional: SLURM HPC cluster access for remote search execution

### Windows Install (Recommended)

Download **`install-stan.bat`** from the [GitHub releases page](https://github.com/bsphinney/stan/releases) and double-click it. The installer:

1. Checks for Python 3.10+ and installs it if missing
2. Clones the STAN repository and runs `pip install`
3. Auto-installs **DIA-NN** from GitHub releases (`.msi` for 2.x, with admin elevation if needed)
4. Auto-installs **Sage** from GitHub releases
5. Handles SSL certificate and proxy issues automatically (common on UC Davis and other institutional networks)
6. Uses `--no-cache-dir` to ensure fresh code

Both `install-stan.bat` and `update-stan.bat` self-update by downloading their latest version from GitHub on each run.

> **Note:** The old `install_stan.bat` (underscore) was removed. Only `install-stan.bat` (hyphen) exists now.

To update an existing installation:

```
update-stan.bat
```

### Install from Source (Linux/macOS/Advanced)

```bash
git clone https://github.com/bsphinney/stan.git
cd stan
pip install -e ".[dev]"
```

The `[dev]` extra installs pytest, ruff, and mypy for development and testing.

You will also need to install DIA-NN and Sage manually and add them to your PATH:
- **DIA-NN**: Download from [github.com/vdemichev/DiaNN/releases](https://github.com/vdemichev/DiaNN/releases)
- **Sage**: Download from [github.com/lazear/sage/releases](https://github.com/lazear/sage/releases)

### Install from PyPI (coming soon)

```bash
pip install stan-proteomics          # not yet published
```

### Verify Installation

```bash
stan version
```

This should print the installed STAN version. If the `stan` command is not found, ensure your Python scripts directory is on your `PATH`.

### Verify Community Benchmark Credentials

```bash
stan verify
```

Prints your lab pseudonym, whether an auth token is present in `~/.stan/community.yml`, and whether the community relay still recognizes that name as claimed. Run this if you are unsure whether your email verification is still valid, or after migrating to a new workstation. If the token is missing or unclaimed, run `stan setup` to re-verify by email.

### ThermoRawFileParser (auto-discovered)

On Windows, the one-click installer places ThermoRawFileParser under `%USERPROFILE%\.stan\tools\trfp\`. STAN's `detect_mode()` and search dispatch call `stan.tools.trfp.ensure_installed()` to locate it automatically -- you do **not** need to set `trfp_path` in `instruments.yml` unless you are on Linux/macOS or want to override with a cluster-side install (v0.2.72+).

---

## Configuration Walkthrough

### Initialize Configuration

```bash
stan init
```

This creates `~/.stan/` and copies three default configuration files into it:

| File | Purpose |
|------|---------|
| `instruments.yml` | Instrument watch directories, vendor, model, settings |
| `thresholds.yml` | QC pass/warn/fail thresholds per instrument model |
| `community.yml` | Community benchmark preferences and error telemetry |

All three files are YAML. Edit them with any text editor, or use the dashboard Config tab to view and manage instruments (including a Remove button for deleting duplicates).

**Interactive setup (recommended):**

```bash
stan setup
```

The setup wizard asks 6 questions:

1. **Watch directory** -- where do your raw files land?
2. **LC column** -- the one thing STAN cannot read from raw files
3. **HeLa amount** -- injection amount in ng (default: 50)
4. **Community benchmark** -- opt in with anonymous pseudonym and email verification
5. **Daily QC email** -- morning report and optional weekly summary
6. **Error telemetry** -- opt-in anonymous error reporting to help improve STAN

Everything else (instrument model, vendor, serial number, LC system, gradient length, DIA/DDA mode) is auto-detected from your raw files.

The wizard deduplicates `instruments.yml` automatically -- if the same watch directory already exists, it offers to update the existing entry rather than creating a duplicate. If your watch directory has existing raw files, the wizard offers to run `stan baseline` at the end.

To change your pseudonym's contact email later, contact bsphinney@ucdavis.edu.

### instruments.yml

This is the primary configuration file. It tells STAN which instruments to watch, where their raw files land, and search settings. Most fields are auto-populated by `stan setup` or auto-detected from raw files.

```yaml
instruments:

  - name: "timsTOF Ultra"
    vendor: "bruker"              # "bruker" or "thermo"
    model: "timsTOF Ultra"        # used for threshold lookup in thresholds.yml
    watch_dir: "D:/Data/raw"
    output_dir: "D:/Data/stan_out"
    extensions: [".d"]            # file extensions to watch for
    stable_secs: 60               # seconds of no size change before processing
    enabled: true                 # set false to pause watching
    qc_modes: ["dia", "dda"]      # modes to accept (auto-detected from metadata)
    community_submit: true        # auto-submit to community benchmark
    hela_amount_ng: 50            # injection amount in ng (default: 50)
    spd: 60                       # samples per day (primary -- Evosep/Vanquish method)
    gradient_length_min: 21       # gradient length in minutes (fallback if spd not set)

# ── Optional: SLURM HPC execution ──────────────────────────────────
# Uncomment to run searches on a remote cluster instead of locally.
# Most labs do NOT need this — local execution is the default.
#
# hive:
#   host: "hive.ucdavis.edu"
#   user: "your_username"
#
# Then add to each instrument:
#   execution_mode: "slurm"
#   hive_partition: "high"
#   hive_account: "your-account-grp"
#   hive_mem: "32G"
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
      ips_score_min: 50                # minimum IPS score
      irt_max_deviation_max: 5.0       # maximum iRT deviation (minutes)
    dda:
      n_psms_min: 10000                # minimum PSMs at 1% FDR
      pct_delta_mass_lt5ppm_min: 0.70  # minimum fraction with mass error < 5 ppm
      ms2_scan_rate_min: 10.0          # minimum MS2 scans per minute

  "timsTOF Ultra":
    dia:
      n_precursors_min: 10000          # tighter -- this instrument should hit 15k+
      median_cv_precursor_max: 15.0
      ips_score_min: 65
    dda:
      n_psms_min: 30000
      pct_delta_mass_lt5ppm_min: 0.90
```

Model-specific entries override the `default` values. If a metric is not specified for a model, the `default` threshold applies.

Thresholds should be tuned to your specific methods and expected performance. Start with the defaults and adjust after collecting a few weeks of data.

### community.yml

Controls your participation in the community benchmark and error telemetry.

```yaml
display_name: "Your Lab Name"         # shown on the leaderboard; blank = anonymous
submit_by_default: false              # true = auto-submit without prompting
hela_source: "Pierce HeLa Protein Digest Standard"
institution_type: "core_facility"     # core_facility | academic_lab | industry
error_telemetry: true                 # opt-in anonymous error reports
```

No HuggingFace account or token is needed -- STAN submits through a relay API hosted on the HF Space.

Leave `display_name` blank for anonymous submissions (shown as "Anonymous Lab" on the leaderboard).

When `error_telemetry: true`, STAN sends anonymous error reports (error type, message, STAN version, OS, Python version) to the HF Space relay. No file paths, serial numbers, or patient data is ever included. All errors are also logged locally at `~/.stan/error_log.json` (last 100 entries) regardless of the telemetry setting.

---

## Watcher Daemon

### Starting the Watcher

```bash
stan watch
```

The watcher runs in the foreground. It recursively monitors all directories (and subdirectories) listed in `instruments.yml` where `enabled: true`. Events inside Bruker `.d` directories (such as `analysis.tdf` writes) are automatically filtered out so they do not trigger redundant processing. For production use, run it in a `tmux` or `screen` session, set it up as a systemd service, or use `start_stan.bat` on Windows.

### What Happens When a File is Detected

1. **Stability check** -- STAN monitors the file/directory size at regular intervals. Once the size has not changed for `stable_secs`, the acquisition is considered complete.

2. **Mode detection** -- For Bruker `.d` files, STAN reads the `MsmsType` column from the `Frames` table in `analysis.tdf` (an SQLite database inside the `.d` directory). Values: 8 = ddaPASEF (DDA), 9 = diaPASEF (DIA). For Thermo `.raw` files, ThermoRawFileParser extracts scan filter metadata.

3. **Search dispatch** -- Based on mode, a local search is launched (or a SLURM job if configured):
   - DIA: DIA-NN with community-standardized parameters
   - DDA: Sage (with ThermoRawFileParser conversion for Thermo `.raw` only)

4. **Metric extraction** -- After the search job completes, STAN extracts QC metrics from the search output using Polars.

5. **Gating** -- Metrics are compared against thresholds. Result: PASS, WARN, or FAIL.

6. **HOLD flag** -- If the result is FAIL, a `HOLD_{run_name}.txt` file is written to the output directory. This can be used to pause the autosampler queue.

7. **Database** -- All metrics and the gate result are stored in the local SQLite database.

8. **Community submission** -- If `community_submit: true`, aggregate metrics are uploaded to the HF Dataset.

### Stopping the Watcher

Press `Ctrl-C` to shut down gracefully. In-progress search jobs will be terminated. If using SLURM mode, remote cluster jobs continue running independently.

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

The dashboard serves a FastAPI backend with a basic HTML frontend. The full React frontend is **(planned)**. The current dashboard includes a Config tab with instrument cards (each with a Remove button for deleting duplicates). You can also access the API directly:

- `/api/runs` -- list recent QC runs
- `/api/runs/{run_id}` -- full metric detail for a run
- `/api/trends/{instrument}` -- time-series metrics for trend analysis
- `/api/instruments` -- current instrument config (hot-reloaded)
- `/api/thresholds` -- current QC thresholds
- `/api/ui-prefs` -- optional lab-wide UI defaults from `ui_prefs.yml` (404 when not configured)
- `/api/community/submit` -- submit a run to the community benchmark
- `/docs` -- Swagger UI for interactive API exploration

### Front Page View Selector (v0.2.106+)

The "This Week's QCs" tab has a three-way view selector at the top:

- **Gauges** -- per-instrument SVG gauges, snapshot of the latest run (the pre-v0.2.106 default)
- **Weekly table** -- one row per QC run grouped by day, numeric values for Proteins / Peptides / Precursors / MS1 AUC, background tint when a value drifts >=4% from the instrument's 30-run rolling median, and a thin range bar per cell
- **Metric matrix** -- transposed heatmap: rows = the four metrics, columns = QC runs across the week in chronological order. Surfaces decoupling (e.g. MS1 steady but precursors collapsing). Click a column header to jump to that run.

All three views share the same filter bar (Instrument / Since / Sample) and consume the same in-memory `runs` array, so switching is instant with no refetch. "HeLa QC only" is the default Sample filter to keep load-test dilutions from polluting the trend.

**Persisting your preference.** Clicking a view only changes the current session. To save your selection as your per-browser default, click **Set as default** -- the preference is written to `localStorage` and will load automatically next time.

**Lab-wide default (optional).** A PI can set a default for every operator by creating `~/.stan/ui_prefs.yml` (or `~/STAN/ui_prefs.yml` on Windows):

```yaml
# Lab-wide UI defaults. Per-user localStorage always overrides these.
front_page_view: weekly_table   # gauges | weekly_table | matrix
matrix_bar_scale: week_range    # week_range | baseline_gates
ms1_format: sci                 # sci | short
```

Unknown keys are silently ignored. The file is optional; if absent, the built-in defaults (gauges, week_range, sci) are used.

**Planned frontend views:**

**Live Runs** -- Status cards for each instrument showing the most recent run, IPS badge, gate result (PASS/WARN/FAIL), and time since last acquisition.

**Run History** -- Sortable, scrollable table of all runs. Click any run to see full metric detail.

**Trend Charts** -- Time-series plots of precursor count, peptide count, CV, and IPS over time for each instrument. Includes LOESS trendlines for detecting gradual drift.

**Column Health** -- TIC AUC and peak retention time plotted over calendar time. Highlights periods of degradation.

**Community Benchmark** -- Tabs for DDA (Track A), DIA (Track B), and Both (Track C). Shows distribution plots, your instrument's position, and the leaderboard for your cohort.

**Instrument Config** -- View instrument cards with Remove button (implemented). Full YAML editor with live preview **(planned)**.

### Remote control & fleet status (v0.2.81+)

If you run STAN on more than one instrument PC, the same Hive mirror drive
(`Y:\STAN\` on the instruments, `/Volumes/proteomics-grp/STAN/` on a Mac)
doubles as a command queue and an aggregated status board. No HuggingFace
relay, no token, no cloud — just the network share you already use for log
mirroring.

**Per-instrument heartbeat.** Every ~5 minutes, `stan watch` writes a
`status.json` file to its own subdirectory on the mirror: STAN version,
`stan.db` row count, last run name + gate result, free disk space, and a
UTC timestamp.

**Command queue.** Every ~30 seconds, `stan watch` also scans
`<mirror>/<host>/commands/pending/` for JSON command files. The action
name must be one of a hardcoded whitelist (currently read-only only —
`ping`, `status`, `tail_log`, `export_db_snapshot`); unknown or stale
actions (>10 min old) are rejected without side effects. Results are
written to `<mirror>/<host>/commands/results/<id>.result.json` and the
request is moved to `commands/done/` for auditing. There is no shell
passthrough anywhere — every action is a pure Python function.

**Central workstation CLIs.** From any machine that mounts the shared
drive:

```bash
# Fleet overview
stan fleet-status

# Ask one instrument for its current status and wait for the answer
stan send-command status --host lumosRox --wait

# Tail the last 50 lines of baseline.log on a specific instrument
stan send-command tail_log --host lumosRox --arg name=baseline --arg n=50 --wait

# Force an instrument to write a full parquet snapshot of stan.db
stan send-command export_db_snapshot --host TIMS-10878 --wait
```

`stan poll-commands` runs one pass of the poller manually; normally you
don't need it because `stan watch` polls automatically.

Destructive actions (remote `update-stan.bat`, process kill) are
intentionally NOT in the whitelist yet. They'll arrive in a later
release after the diagnostic channel has been proven out.

### Maintenance Log (v0.2.68+)

The Trends tab has a maintenance log form for recording column swaps, source cleans, PMs, and calibrations. Each event stores a date, an event type, and free-text notes. Logged events render as vertical markers on every trend chart for that instrument, so a sudden shift in precursor count or IPS is immediately traceable to a known maintenance action rather than being mistaken for instrument drift. Events are persisted in the `maintenance_events` SQLite table and are never submitted to the community benchmark.

### Error Logs

The dashboard writes two kinds of error logs:

- **Client-side** -- browser JavaScript errors are POSTed to `/api/client-error` and appended to `dashboard_errors.log` in the output directory.
- **Server-side** (v0.2.77+) -- the `GET /` handler wraps `index_path.read_text()` and writes any exception (for example the Windows UTF-8 decode crash fixed in v0.2.76) to the same log before re-raising. This makes otherwise-invisible server crashes visible in the Hive mirror for remote debugging.

---

## Baseline Builder

The baseline builder processes existing HeLa QC files retroactively, ideal for populating your database with historical data before starting the live watcher.

```bash
stan baseline
```

The baseline builder walks you through:

0. **TIC backfill sweep (v0.2.65+)** -- silently scans the local DB for runs that are missing a TIC trace (from failed DIA-NN searches, pre-0.2.64 runs, or older baseline imports) and re-extracts them in order: Bruker `analysis.tdf` → DIA-NN `report.parquet` → Thermo `fisher_py`. Recovered traces are downsampled to 128 points. Prints a one-line summary only if something was actually recovered.
1. **Directory selection** -- lists every configured watch directory from `instruments.yml` as a numbered menu (v0.2.61+), or accepts a custom path. Each choice shows the vendor and QC filter settings so you can tell which directory is which.
2. **File discovery** -- recursively finds all `.d` and `.raw` files in subdirectories
3. **Metadata extraction** -- auto-detects instrument model, gradient length, LC system, and acquisition mode. On Bruker `.d` files the XML method tree under `<N>.m/` is read first (`submethods.xml` contains `"100 samples per day"`, `hystar.method` contains `<SubDeviceName>Evosep One</SubDeviceName>`), then the TDF `MethodName`, then `Frames.Time` span. On Thermo `.raw` files, `fisher_py` is tried before `ThermoRawFileParser`.
4. **Per-file SPD validation** (v0.2.55+) -- each file's SPD is resolved from its own raw-file metadata via `validate_spd_from_metadata()` BEFORE the DB insert, so mixed-gradient directories get the correct per-run SPD instead of the cohort default.
5. **Acquisition date preservation** (v0.2.54+) -- `run_date` is pulled from `analysis.tdf.AcquisitionDateTime` (Bruker) or the `.raw` header (Thermo) instead of being stamped with insertion time. Historical files keep their real timestamps.
6. **Summary** -- shows a table of discovered files broken down by instrument before committing
7. **Pre-flight tests** -- runs a quick test search with DIA-NN and Sage to verify they work before processing your files
8. **Processing** -- searches all files with standardized parameters and stores metrics in the database. Filenames containing `--` are automatically aliased via a directory junction (v0.2.63+) to work around a DIA-NN 2.3.2 argv-parsing bug.
9. **TIC extraction** -- raw Bruker TIC from `Frames.SummedIntensities` or identified TIC from the DIA-NN report, downsampled to 128 bins before local storage and community submission.

If a search engine is not found or fails pre-flight, the builder prompts for a custom executable path. DIA-NN 2.x is preferred over 1.x when both are installed.

The community FASTA (`UP000005640_9606_plus_universal_contam.fasta`, 21,044 entries) is auto-downloaded from the HF Dataset if not cached locally.

Additional features:
- **Scheduling** -- run now, tonight (8 PM), or weekend (Saturday 8 AM)
- **Resume** -- progress is tracked in `~/.stan/baseline_progress.json`; interrupted runs resume where they left off
- **Duplicate detection** -- files already in the database are skipped
- **Community upload** -- if community submission is enabled, metrics are batch-uploaded after processing

### Adding a new watch directory (v0.2.59+)

You don't need to hand-edit `instruments.yml`. Use:

```bash
stan add-watch D:\Data                                          # interactive
stan add-watch D:\Data -y                                       # default QC pattern, no prompt
stan add-watch D:\Data --qc-pattern "(?i)(hela|mylab_qc)"       # custom regex
stan add-watch G:\qc_only --all-files                           # dedicated QC dir, no filter
```

The interactive mode scans the directory (recursively, up to 5000 entries), shows how many files match the default HeLa/QC regex, and asks whether to keep the default, provide a custom regex, or process every file. Each watch directory gets its own `qc_only` + `qc_pattern` fields in `instruments.yml`, so mixed-sample dirs and dedicated QC dirs can have different filters side by side. The vendor is auto-detected from contents (`.d` → bruker, `.raw` → thermo). The watcher daemon hot-reloads the config within 30 seconds of the change.

### Repairing historical metadata (v0.2.57+)

If you have prior baseline runs with wrong SPD, wrong `run_date`, or missing `lc_system`:

```bash
stan repair-metadata --dry-run       # preview diffs
stan repair-metadata                 # apply to local DB
stan repair-metadata --push          # also push corrections to the community
                                     # relay for runs that were already submitted
```

`stan repair-metadata` walks every row in the local `runs` table, re-reads the raw file at `raw_path`, and updates SPD (from `validate_spd_from_metadata`), acquisition date (from `get_acquisition_date`), and LC system (from `detect_lc_system`). With `--push` it forwards corrections to `POST /api/update/{submission_id}` on the community relay so the benchmark reflects reality without re-running any searches.

### Repairing missing TIC traces (v0.2.65+)

```bash
stan backfill-tic                    # local DB only
stan backfill-tic --push             # also push to community relay
```

For each run without a TIC trace in the local DB, the command tries the Bruker raw `analysis.tdf`, the DIA-NN `report.parquet` under `baseline_output/<stem>/`, and Thermo `fisher_py` in that order. Every recovered trace is downsampled to 128 bins before storage. With `--push`, already-submitted runs also get their TIC patched on the community benchmark via `POST /api/update/{submission_id}`.

Note: `stan baseline` runs this sweep automatically at startup now, so you only need the manual command if you want to force a refresh or use `--push`.

### Repairing zero-peptide submissions (v0.2.71+)

Older STAN versions stored `n_precursors` from DIA-NN but never recomputed `n_peptides` (unique `Stripped.Sequence` at 1% FDR) or `n_proteins` (unique `Protein.Group`). `stan backfill-tic` now detects rows where `n_peptides = 0` but `baseline_output/<stem>/report.parquet` still exists, recomputes both counts from the parquet, and updates the local DB. With `--push`, repaired counts are also sent to the community relay via `POST /api/update/{submission_id}`. The `stan baseline` startup sweep calls the same code path, so this repairs itself the next time baseline runs.

---

## Community Benchmark Submission

### Prerequisites

1. `community_submit: true` on the instrument in `instruments.yml`
2. A valid HeLa QC run that passes the community hard gates

No HuggingFace account or token is needed -- STAN submits through a relay API automatically.

### Auth Token and Fork Protection (v0.2.74+)

When you run `stan setup` and verify your email, the relay at `brettsp-stan.hf.space` issues an `auth_token` that is stored in `~/.stan/community.yml` alongside your `display_name`. Every submission and every `/api/update/{id}` call sends this token in an `X-STAN-Auth` header so the relay can verify that the claimed lab name still belongs to a setup-verified installation. Forks that skip `stan setup` or modify the submission code will not have a valid token and cannot spoof a claimed name. If the HF Space operator sets an `ADMIN_SECRET` env var, PATCH requests without a valid client token are rejected entirely (403).

The commands that send the token automatically:

- `stan submit` (manual submission)
- Automatic submissions after a gated run (`community_submit: true`)
- `stan backfill-tic --push`
- `stan repair-metadata --push`

Use `stan verify` to confirm your token is present and your name is still claimed server-side.

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
- IPS score, missed cleavage rate
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
| `ips_score` | Instrument Performance Score (0-100) | See IPS section below |
| `tic_auc` | Total ion chromatogram area under curve | Track longitudinally, not absolute |
| `peak_rt_min` | Retention time of TIC peak (minutes) | Should be consistent run-to-run |
| `irt_max_deviation_min` | Maximum iRT peptide RT deviation (minutes) | <3 min for healthy column |
| `ms2_fill_time_median_ms` | Median MS2 ion accumulation time (ms) | Platform-dependent |

---

## Instrument Performance Score (IPS)

IPS is a 0-100 composite computed entirely from search output — no reference run, no blank runs, no historical data needed. It works from the very first QC injection.

### Components

**DIA:**
```
IPS = 30% precursor depth + 25% spectral quality (fragments/precursor)
    + 20% sampling quality (points across peak) + 15% quant coverage
    + 10% digestion quality (1 - missed cleavage rate)
```

**DDA:**
```
IPS = 30% identification depth (PSMs) + 25% mass accuracy (<5 ppm fraction)
    + 20% sampling quality (points across peak) + 15% scoring (hyperscore)
    + 10% digestion quality (1 - missed cleavage rate)
```

### Interpretation

| Score | Status | Action |
|-------|--------|--------|
| 90-100 | Excellent | Instrument performing optimally. No action needed. |
| 80-89 | Good | Normal operating range. Continue monitoring. |
| 60-79 | Marginal | Performance may be declining. Investigate column, spray, and buffers soon. |
| Below 60 | Investigate | Likely instrument or LC problem. Check before running valuable samples. |

### Longitudinal Tracking

IPS is stored in the SQLite database for every run. A steady downward trend in IPS, even if individual runs still pass thresholds, typically indicates column aging or source degradation.

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
| Low ID count, normal IPS | Search or library issue. Check spectral library version, FASTA, or DIA window scheme. |
| Low IDs, low IPS | LC or source problem. Check column condition, trap column, spray stability. |
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

## Ion Cloud View (timsTOF, v0.2.192+)

The dashboard's **Ion cloud** tab shows a Bruker-DataAnalysis-style scatter of
m/z vs 1/K0 mobility for a single run, with DIA isolation windows overlaid.
Two rendering modes exist depending on whether 4DFF features are available:

- **Charge-labeled Plotly view** (preferred) — shown when a `.features` file
  from the 4D feature finder (`stan run-4dff`) exists next to the raw `.d`.
  Each charge state becomes its own legend entry; click `+1` once in the
  legend to hide contamination and watch the +2/+3 peptide ridge pop. DIA
  windows are rendered as colored rectangles grouped by `window_group`.
  Colors follow the Ziggy palette (`+1` teal, `+2` blue, `+3` green, `+4`
  orange, `+5` purple, `+6` red, unassigned yellow).
- **Legacy SVG cloud** (fallback) — when no `.features` file is present the
  view falls back to the heatmap of MS1 peaks. A helpful hint tells you to
  run `stan run-4dff <path/to/.d>` to enable the richer view.

To enable the new view on older runs, generate the `.features` files in
bulk with `stan run-4dff` (or the per-instrument backfill variant). The
dashboard will pick them up on the next page load — no re-search required.

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

### Search engine not found

- On Windows, the installer auto-installs DIA-NN and Sage. If they were not installed, run `install-stan.bat` again
- `stan baseline` and `stan setup` will prompt for a custom executable path if the search engine cannot be found on PATH
- DIA-NN 2.x is preferred over 1.x; if both are installed, STAN uses 2.x
- Check that the executable is on your PATH: `where diann` (Windows) or `which diann` (Linux/macOS)

### SLURM job fails (HPC mode only)

- Verify `hive_partition` and `hive_account` are correct in `instruments.yml`
- Ensure DIA-NN and Sage are available on the cluster (check module loads or paths)
- For Thermo DDA runs, verify `trfp_path` points to a valid ThermoRawFileParser installation
- Check the SLURM job output log in the `output_dir` for error details
- DIA-NN on Linux requires .NET SDK 8.0.407 or newer

### SSL/certificate errors during install

The Windows installer handles UC Davis network SSL proxy issues automatically. If you still see certificate errors:
- Run the installer again -- it includes TLS trust bypass for the download step
- Both `.bat` files self-update, so re-running always gets the latest fix

### Low identification counts

- Verify the correct acquisition mode was detected (check dashboard or logs)
- For DIA: check that the DIA window scheme matches the community library
- For DDA: check MS2 scan rate -- low scan rate may indicate a method configuration issue
- Compare against expected reference ranges for your instrument model (see appendix in STAN_MASTER_SPEC.md)
- Check IPS score -- if IPS is also low, the issue is likely LC or source, not search

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
*STAN Academic License (free for academic/non-profit; commercial requires license -- contact bsphinney@ucdavis.edu) -- Community data: CC BY 4.0*
