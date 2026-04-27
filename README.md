# STAN — Standardized proteomic Throughput ANalyzer

> *Know your instrument.*

[![License: STAN Academic](https://img.shields.io/badge/License-Academic-blue.svg)](LICENSE)
[![Dataset: CC BY 4.0](https://img.shields.io/badge/Data_License-CC_BY_4.0-green.svg)](https://creativecommons.org/licenses/by/4.0/)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

STAN is an open-source proteomics QC tool for Bruker timsTOF and Thermo Orbitrap mass spectrometers. It watches the directory where your instrument writes raw files, runs a standardized search on every HeLa QC injection, computes instrument health metrics, and pauses your sample queue when a run fails. A local web dashboard shows the results, and you can opt in to a community benchmark that compares your instrument against labs around the world.

It is built for proteomics core-facility staff who want continuous QC without writing scripts, and for PIs who want one number to glance at before approving a batch.

**Built at the UC Davis Proteomics Core by Brett Stanley Phinney.**

---

## Why STAN

- **Catch instrument problems before your samples do.** Every HeLa QC injection is searched, scored, and gated automatically. If a run fails, STAN drops a HOLD flag the autosampler queue can read.
- **Know what "normal" looks like for your instrument.** STAN tracks every QC run forever in a local SQLite database, so a slow drift in IPS or peptide count is visible weeks before it would crash an experiment.
- **Compare against the rest of the world.** The optional community benchmark tells you where your timsTOF Ultra at 60 SPD sits versus other timsTOF Ultras at 60 SPD — without ever uploading a raw file or sample metadata.
- **No HPC required.** DIA-NN and Sage run as subprocesses on the instrument workstation. SLURM is supported as an option for labs that already have a cluster.
- **Two questions, then it just works.** STAN auto-detects the instrument model, serial number, gradient length, DIA window scheme, LC system, and acquisition mode by reading the raw file. You only configure your column and your HeLa amount.

---

## Supported Instruments

| Vendor | Models | Raw format | Modes |
|---|---|---|---|
| Bruker | timsTOF Ultra 2, Ultra, HT, Pro 2, SCP | `.d` directory | diaPASEF, ddaPASEF |
| Thermo | Astral, Exploris 480 / 240, Orbitrap Fusion Lumos, Eclipse | `.raw` file | DIA, DDA |

Adding a new model is a config edit — see [`docs/user_guide.md`](docs/user_guide.md).

---

## Quick Start

The happy path is five commands.

### 1. Install

**Windows (recommended).** Download [`install-stan.bat`](https://raw.githubusercontent.com/bsphinney/stan/main/install-stan.bat) (right-click → Save As) and double-click it. The script installs Python if needed, downloads STAN from GitHub, and auto-installs DIA-NN and Sage from their official release pages. Re-run [`update-stan.bat`](https://raw.githubusercontent.com/bsphinney/stan/main/update-stan.bat) any time you want the latest STAN — it self-updates from GitHub.

**Mac / Linux / advanced.** STAN is on GitHub only (no PyPI release yet):

```bash
$ git clone https://github.com/bsphinney/stan.git
$ cd stan
$ pip install -e ".[dev]"
```

You will need DIA-NN and Sage installed separately and on your `PATH`. See the user guide.

### 2. Initialize

```bash
$ stan init
```

Creates `~/.stan/` and seeds three config templates: `instruments.yml`, `thresholds.yml`, `community.yml`.

### 3. Set up

```bash
$ stan setup
```

A 6-question wizard. It picks up the watch directory, asks for your LC column and HeLa amount, optionally enrolls you in the community benchmark (with an anonymous pseudonym and email verification), turns on a daily email summary, and offers to run a baseline over any existing files. Everything else (instrument model, gradient, mode) is read from your raw files.

### 4. Watch

```bash
$ stan watch
```

The watcher daemon. It tails every directory in `instruments.yml`, waits for a new acquisition to finish writing, runs DIA-NN or Sage, computes metrics, gates against thresholds, and (optionally) submits to the community benchmark. Leave it running — it auto-reloads config every 30 s.

### 5. Dashboard

```bash
$ stan dashboard
```

Serves the dashboard at <http://localhost:8421>. Eight tabs: This Week's QCs, QC History, Trends, Sample Health, Fleet, Config, Community, and a 🎮 Arcade mini-game.

> ℹ The dashboard is a single HTML file (`stan/dashboard/public/index.html`) — React + Babel from a CDN, no build step. It already ships; there is nothing to compile.

---

## What STAN does for you

- **Auto-search every QC injection.** DIA-NN for DIA, Sage for DDA. Both run locally on the instrument PC; a typical 60 SPD HeLa run finishes in 5–15 min.
- **Run-and-Done gating.** A FAIL writes `HOLD_<run_name>.txt` to the output directory. Most autosampler queues can poll for this file and pause before the next injection.
- **Plain-English failure diagnosis.** If a gate fails, STAN names the likely cause ("incomplete digestion — check trypsin", "source contamination — clean the source", and so on).
- **IPS — one number for instrument health.** A 0–100 cohort-calibrated score that combines precursor, peptide, and protein depth against your instrument family at your gradient. Score 60 means cohort median; 90 means cohort top decile.
- **Column health.** Trended TIC area and peak retention time tell you when the column is aging.
- **PEG contamination scan.** Bruker only: STAN scans MS1 for the polyethylene-glycol ladder and flags solvent or emitter contamination before it tanks your IDs.
- **diaPASEF window-mobility drift.** Bruker only: detects when MS2 windows have walked off the 1/K0 they were calibrated for.
- **4DFF Ion Cloud.** Optional Bruker 4D feature finder. Generates per-run feature files; the Ion Cloud tab renders m/z × 1/K0 × RT colored by charge with DIA windows overlaid.
- **cIRT panel.** Empirical retention-time anchors per instrument family + SPD bucket. Lets you trend RT stability without spiking Biognosys iRT into every sample.
- **Maintenance log.** Record column swaps, source cleans, PMs, and calibrations. Events render as vertical markers on every trend chart, so a sudden shift is immediately traceable.
- **Daily email + Slack alerts.** Optional. Daily 07:00 HTML summary via Resend; weekly Monday digest; Slack webhook for instant FAIL notifications.
- **Fleet view.** Run STAN on N instruments, point each one at the same network share, and a single dashboard sees them all. No cloud, no token.
- **Community benchmark.** Opt-in, fully aggregate. No raw files, no sample metadata, no patient data ever leaves your lab.

---

## How a run is processed

```
new .d / .raw appears
        │
        │  size stops changing for stable_secs
        ▼
  acquisition mode detector (DIA vs DDA)
        │
        ├─ DIA  → DIA-NN  → report.parquet
        └─ DDA  → Sage    → results.sage.parquet  (.raw → mzML via ThermoRawFileParser first)
                       │
                       ▼
            metric extraction (Polars)
                       │
                       ▼
              gating vs thresholds.yml
              ┌────────────┼────────────┐
              ▼            ▼            ▼
          PASS         WARN         FAIL → HOLD_<run>.txt
                       │
                       ▼
            SQLite (~/.stan/stan.db)
                       │
                       ▼
        dashboard at :8421  +  (optional) community submission
```

> ⚠ STAN does **not** redistribute DIA-NN, Sage, or ThermoRawFileParser. They are called as subprocesses, like a Makefile calls `gcc`. Each tool ships under its own license and must be installed separately. DIA-NN is free for academic research; commercial users need a paid license from Aptila Biotech or Thermo Fisher. Sage and ThermoRawFileParser are open source.

---

## Metrics at a glance

The headline numbers you'll see on the dashboard.

| Metric | Modes | What it tells you |
|---|---|---|
| **IPS** (0–100) | DIA + DDA | Single composite of precursor, peptide, and protein depth scored against your cohort. The first thing to glance at. |
| **Precursor count @ 1% FDR** | DIA | Primary metric for DIA. Cleaner than protein count because it doesn't depend on FASTA or inference. |
| **PSM count @ 1% FDR** | DDA | Primary metric for DDA. |
| **Peptide count** | both | Secondary depth metric. |
| **Protein count** | both | Contextual only. Heavily confounded by FASTA + inference settings — never use as a leaderboard primary. |
| **Median CV (precursor)** | DIA, replicates | Quantitative reproducibility. Healthy timsTOF Ultra: 4–9%. |
| **Missed cleavage rate** | both | Digestion quality. Should be < 0.15. |
| **Median ΔΜ < 5 ppm** | DDA | Mass calibration health. |
| **MS2 scan rate** | DDA | Duty cycle. Method/platform dependent. |
| **iRT max deviation** | DIA | Retention-time drift, from the empirical cIRT panel. |
| **Points across peak** | both | Median MS2 scans per elution peak. Quantitation quality. |

Full definitions, ranges, and formulas are in [`docs/user_guide.md`](docs/user_guide.md) and [`docs/ips_metric.md`](docs/ips_metric.md).

---

## The community benchmark

A live, opt-in HeLa benchmark hosted at **[community.stan-proteomics.org](https://community.stan-proteomics.org)**. Hundreds of QC runs from labs around the world, every one searched with the same FASTA + library + parameters so the differences you see actually reflect instrument performance.

- **Privacy.** No raw files. No sample metadata. No patient data. Aggregate metrics only. Serial numbers are stored server-side but never exposed on the API.
- **Identity.** Your lab gets an anonymous pseudonym ("Caffeinated Quadrupole", etc.). You can claim a custom name with email verification. The verification token (`auth_token` in `~/.stan/community.yml`) prevents anyone else's STAN install from spoofing your name.
- **Cohorts.** Submissions are compared only within the same `(instrument family, SPD bucket, injection amount bucket)`. A 50 ng timsTOF Ultra at 60 SPD is not compared against a 500 ng Astral at 200 SPD.
- **Tracks.** Track A = DDA. Track B = DIA. Track C unlocks when you submit both a DDA and a DIA run from the same instrument within 24 h, which produces a six-axis radar fingerprint.
- **Opt out.** `community_submit: false` (the default) and nothing leaves your lab. STAN is fully usable as a local-only QC tool.

To join, run `stan setup` and answer "yes" to the community-benchmark question. The wizard claims your name and stores the auth token automatically. Use `stan verify` to check your token any time.

---

## Documentation map

| File | What's in it |
|---|---|
| [`docs/user_guide.md`](docs/user_guide.md) | The day-to-day manual. Installation, dashboard tour, every CLI command, configuration reference, troubleshooting, full glossary. |
| [`docs/ips_metric.md`](docs/ips_metric.md) | How IPS is calculated, the cohort references, why protein count is excluded. |
| [`STAN_MASTER_SPEC.md`](STAN_MASTER_SPEC.md) | The authoritative design spec. Read this if you're changing how STAN works. |
| [`docs/V1_RUNBOOK.md`](docs/V1_RUNBOOK.md) | The v1.0 release runbook. |
| [`docs/PEG_EVOSEP_DIAGNOSTIC.md`](docs/PEG_EVOSEP_DIAGNOSTIC.md) | Background on the PEG contamination test. |
| [`docs/HPC_PATHS.md`](docs/HPC_PATHS.md) | Reference paths for the optional Hive HPC integration. |
| [`docs/QUEUE_SWITCHING.md`](docs/QUEUE_SWITCHING.md) | Auto partition switching logic for SLURM. |
| [`docs/GOTCHAS_DELIMP.md`](docs/GOTCHAS_DELIMP.md) | 50+ hard-learned lessons (DIA-NN, SLURM, raw-file quirks). |
| [`CLAUDE.md`](CLAUDE.md) | Context for Claude Code when working on this codebase. |

---

## Implementation Status

What ships today vs. what's still planned.

| Component | Status | Notes |
|---|---|---|
| CLI (45 commands) | Done | Full list in `docs/user_guide.md`. |
| Watcher daemon | Done | File-stability detection, hot-reloaded config, recursive monitoring, startup catch-up sweep. |
| Acquisition mode detection | Done | Bruker via `analysis.tdf.Frames.MsmsType`; Thermo via ThermoRawFileParser metadata + filename token fallback. |
| Local DIA-NN execution | Done | Default. Subprocess on the instrument PC, community-standard params. |
| Local Sage execution | Done | Default. Bruker `.d` native, Thermo `.raw` via ThermoRawFileParser → mzML. |
| SLURM HPC execution (optional) | Done | `execution_mode: slurm` per instrument. SSH + `sbatch` via system `ssh` (with `ControlMaster`); no `paramiko` dependency. |
| Metric extraction (DIA + DDA) | Done | Polars-based, from `report.parquet` and `results.sage.parquet`. |
| IPS scoring | Done | 3-component depth composite (precursors / peptides / proteins), 0–100, percentile-mapped against an `(instrument family, SPD bucket)` cohort. See `docs/ips_metric.md`. |
| QC gating + HOLD flag | Done | Hard gates with plain-English diagnosis. |
| Column health | Done | TIC AUC + peak RT trend analysis. |
| SQLite database | Done | All metrics, gate results, sample-health verdicts, maintenance events, PEG/drift breakdowns, 4DFF features-by-charge. |
| FastAPI dashboard backend | Done | All routes wired (runs, trends, instruments, thresholds, fleet, community, PEG, drift, 4DFF, sample-health, hide). Swagger at `/docs`. |
| Single-file React dashboard | Done | `stan/dashboard/public/index.html`, React + Babel via CDN. 8 tabs. |
| Setup wizard | Done | 6 questions, dedupes `instruments.yml`, offers baseline at the end. |
| Baseline builder | Done | Recursive discovery, auto-detect gradient/LC, pre-flight DIA-NN/Sage tests, resume on interrupt, scheduling (now / tonight / weekend). |
| Windows installer + updater | Done | `install-stan.bat`, `update-stan.bat`. Self-update from GitHub. |
| Community submission | Done | Hard gates, soft flags, asset MD5 verification, no HF token needed (relay). |
| Community auth token | Done | `stan setup` claims a pseudonym via email; relay enforces `X-STAN-Auth` on PATCH. |
| Community FASTA | Done | UniProt human + universal contaminants, MD5-verified, auto-downloaded on first need. |
| Community speclibs | Partial | Astral + timsTOF HeLa empirical/predicted libs in progress. |
| Cohort scoring + percentiles | Done | Computed nightly within `(family, SPD, amount)` cohorts. |
| HF Space community dashboard | Done | Live at `community.stan-proteomics.org`. |
| Bruker `.d` XML method-tree parser | Done | Reads `<N>.m/submethods.xml`, `hystar.method`, `SampleInfo.xml` for authoritative SPD + Evosep detection. |
| `validate_spd_from_metadata()` | Done | XML → MethodName → `Frames.Time` span fallback chain. |
| `detect_lc_system()` | Done | Evosep vs custom from `.d` method tree + TrayType; powers the LC filter on the community TIC overlay. |
| Real acquisition-date preservation | Done | Bruker `analysis.tdf.AcquisitionDateTime` / Thermo `fisher_py` CreationDate, not insertion time. |
| DIA-NN filename `--` sanitizer | Done | Junction/symlink workaround for the DIA-NN argv-parsing bug. |
| Today TIC overlay | Done | `/api/today/tic-overview` powers the at-a-glance pump-and-spray view. |
| PEG contamination panel | Done | `stan backfill-peg`, scoring, lollipop chart in the run modal. |
| diaPASEF window drift | Done | `stan backfill-window-drift`, drift cloud scatter in the run modal. |
| 4DFF Ion Cloud | Done | `stan install-4dff`, `run-4dff`, `backfill-features`. Plotly per-charge view, SVG fallback. |
| cIRT panel + trends | Done | `stan backfill-cirt`, `derive-cirt-panel`, Trends tab visualisation. |
| Maintenance log UI | Done | Trends-tab form. Events render as vertical markers on every trend chart. |
| Hide / restore a run | Done | `POST /api/runs/{id}/hide`. UI button on the QC History row. |
| Sample Health (rawmeat) | Done | Bruker `.d` non-QC files monitored; verdict (pass/warn/fail) stored in `sample_health` table. Thermo support TBD. |
| Fleet sync (SMB / HF Space / none) | Done | `~/.stan/fleet.yml`, configured by `stan/fleet_setup.py`. |
| Fleet command queue | Done | 12 whitelisted actions (`ping`, `status`, `tail_log`, `export_db_snapshot`, `watcher_debug`, `qc_filter_report`, `apply_config`, `update_stan`, `restart_watcher`, `cleanup_excluded`, `fix_instrument_names`, `v1_prep`). |
| Email reports | Done | Daily 07:00 + optional Monday weekly. Resend API. |
| Slack alerts | Done | Webhook in `community.yml`. `stan test-alert` to verify. |
| Error telemetry (opt-in) | Done | Anonymous reports to the relay; local log at `~/.stan/error_log.json`. |
| Front-page view selector | Done | Gauges / Weekly table / Metric matrix on This Week's QCs. |
| Test fixtures (real DIA-NN / Sage output) | Planned | `tests/fixtures/` is mostly empty. |
| Outlier detection (amount / SPD mismatch) | Planned | Flag submissions whose metrics don't match the declared cohort. |
| Community downtime / reliability leaderboard | Planned | MTBF / availability / recovery-time per instrument model. |
| PyPI release | Planned | `pip install stan-proteomics` not yet published. |
| Auto-start `stan watch` as a Windows service | Planned | Today the operator launches it manually after install. |
| Mobile PWA | Planned | Responsive CSS + service worker + push notifications on FAIL. |

---

## Roadmap / TODO

The shortlist of things actively being worked on or queued. (Bug fixes and shipped features have been moved out of this list — see Implementation Status above.)

**High priority**

- [ ] **Investigate QC ingest blackout on timsTOF HT since 2026-04-17.** Watcher matches the QC filter but doesn't write rows into `runs`. Likely a downstream search-dispatch bug. See `/Volumes/proteomics-grp/STAN/TIMS-10878/failures/`.
- [ ] **Watcher stderr → syncable log.** Cascade bugs and observer deaths are invisible to the Hive mirror because `stan watch` only logs to stderr. Route to `~/STAN/logs/watch_<ts>.log` with periodic re-sync.
- [ ] **`backfill-tic --push` HF error capture.** Push-side relay errors aren't logged. Add a `push_errors` section to the summary log with response codes and bodies.
- [ ] **Normalize `runs.instrument` + `sample_health.instrument`.** Some hosts split into two cards (`timsTOF HT` + `data_bruker`) because old rows hold the model name from metadata while newer rows use `name:` from `instruments.yml`. One-time migration that maps config name → model derived from the raw file.
- [ ] **PEG + drift trend lines on the Trends tab.** We already store the per-run scalars and breakdowns. Add sparklines (peg_score, drift_median_im, drift_coverage) so slow weeks-long drifts are visible.
- [ ] **Rolling 3-month IPS baselines.** Recompute `IPS_REFERENCES` quarterly from each instrument's own history per SPD bucket. Decouples short-term variance from long-term drift. New `ips_baselines` table; `stan recalibrate-ips`; auto-monthly from the watcher.
- [ ] **Auto-start `stan watch`.** New `stan install-service` CLI registers a Windows Scheduled Task with "At user logon" + "At system startup" triggers and "Restart on failure". `install-stan.bat` calls it; `update-stan.ps1` cycles it on update so post-update watch is never forgotten.
- [ ] **`stan backfill-all`.** One wrapper that chains `backfill-metrics` + `backfill-cirt` + `backfill-tic` + `backfill-peg` + `backfill-window-drift` so a post-update sweep truly fills every gap.
- [ ] **Consolidate entry-point scripts.** Operators don't know which `.bat` to click. Rename `update-stan.bat` → `stan.bat`, make the update step a fast no-op when versions match, drop `start_stan.bat` and `start_stan_loop.bat`.
- [ ] **Integration tests on Hive.** Pre-push gate (`stan dev smoke-test`) that runs the real pipeline against real `.d` / `.raw` files. Would have caught most of the v0.2.147–0.2.161 regressions.
- [ ] **Investigate jaggy Bruker TIC artifact.** STAN's "Today's TIC overlay" sometimes renders ~30 sharp evenly-spaced peaks where Compass shows a smooth chromatogram. Diagnose first (raw resolution vs downsample artifact); fix per finding.
- [ ] **Fleet `disk_free_gb`.** Today reports the user-config drive (usually C:) instead of the watch_dir's drive. Report one entry per watch_dir.

**Medium priority**

- [ ] **Sample Health TIC chart.** Under the table, render TICs for the currently-listed runs in overlaid + faceted modes. Pull from `tic_traces` joined on the visible row IDs.
- [ ] **Thermo TIC failures on Lumos.** `fisher_py` throws `ArgumentOutOfRangeException` on some firmwares; TRFP also exits non-zero. Test `SelectInstrument(Device.MS, 0)` or document a per-instrument skip flag.
- [ ] **Thermo ion-injection-time drift.** Add `median_ion_injection_time_ms` and a mid-run upward-drift flag. Catches marginal sprays that the TIC dropout test misses.
- [ ] **Remote `run_baseline` / `baseline_status`.** Kick off a baseline from the fleet dashboard or `stan send-command`; poll progress via a mirrored `baseline_progress.json`.
- [ ] **Mobile PWA.** Responsive CSS, `manifest.json`, service worker, push on FAIL.
- [ ] **Lumos / Exploris Thermo TIC backfill** via Hive-side `report.parquet` identified-TIC path.
- [ ] **Thermo `.raw` `fisher_py`-based SPD extraction** from the InstrumentMethod header.
- [ ] **Generate + upload Astral and timsTOF HeLa speclibs** to the HF Dataset.
- [ ] **Outlier detection** for community submissions: flag runs where metrics are inconsistent with the declared amount/SPD.
- [ ] **PyPI release.**
- [ ] **End-to-end watcher integration test** with real instrument data.
- [ ] **Points-across-peak metric** (DIA + DDA): median FWHM, cycle time, data points per elution peak.
- [ ] **Community dashboard figures**: SPD vs. points-across-peak, faceted by LC column model.
- [ ] **TIC filter by pseudonym** (your traces vs community vs all). Color by lab when showing all traces.
- [ ] **Migration-keyed `backfill-tic` sentinel** instead of version-keyed (so trivial bumps don't re-force the whole sweep).
- [ ] **Install wizard for shared-drive selection.** First-run prompt for the fleet root (SMB path / HF Space URL / none). Today: ships as `stan/fleet_setup.py` but isn't surfaced from `stan init` yet.
- [ ] **Community downtime / reliability leaderboard** — heartbeat-gap detection, MTBF, recovery time, availability normalized by `institution_type`.

---

## Repository Layout

```
stan/
├── pyproject.toml
├── README.md                     ← this file
├── STAN_MASTER_SPEC.md            authoritative design doc
├── CLAUDE.md                      development context for Claude Code
├── LICENSE                        STAN Academic License
├── install-stan.bat               Windows fresh install
├── update-stan.bat                Windows update
├── start_stan.bat                 launches dashboard + watcher
├── stan/
│   ├── cli.py                     Typer CLI entry point (45 commands)
│   ├── config.py                  YAML loaders + hot-reload
│   ├── db.py                      SQLite schema + queries
│   ├── setup.py                   6-question wizard
│   ├── fleet_setup.py             SMB / HF Space / none picker
│   ├── baseline.py                retroactive QC over existing files
│   ├── control.py                 fleet command-queue dispatcher
│   ├── alerts.py                  Slack webhook
│   ├── telemetry.py               opt-in error reports
│   ├── watcher/                   watchdog daemon + stability + mode detection
│   ├── search/                    DIA-NN + Sage runners (local + SLURM)
│   ├── metrics/                   extraction, IPS, iRT, TIC, PEG, drift, 4DFF
│   ├── gating/                    threshold evaluation + HOLD flag
│   ├── community/                 relay submit/fetch/validate, cohort scoring
│   │   └── scripts/consolidate.py nightly GitHub Actions consolidation
│   ├── reports/daily_email.py     Resend-based HTML reports
│   └── dashboard/                 FastAPI + single-file React UI
├── tests/
├── docs/
└── .github/workflows/
    ├── ci.yml                     lint + test
    └── consolidate_benchmark.yml  nightly community percentiles
```

---

## Development

```bash
$ pip install -e ".[dev]"
$ pytest tests/ -v
$ pytest tests/ -k "not integration"   # skip Hive-only tests
$ ruff check stan/
$ ruff check stan/ --fix
```

Tests marked `@pytest.mark.integration` require Hive SLURM and real raw files. They're skipped in CI and can be run manually on the cluster.

---

## Search engines

STAN does not bundle DIA-NN, Sage, or ThermoRawFileParser. Each is downloaded separately and called as a subprocess. The Windows installer fetches DIA-NN and Sage automatically; on Mac/Linux you install them yourself and put them on `PATH`.

| Tool | Used for | License |
|---|---|---|
| [DIA-NN](https://github.com/vdemichev/DiaNN) | All DIA searches (Bruker `.d` and Thermo `.raw` direct, no conversion) | Free for academic research; commercial requires a paid license from Aptila Biotech or Thermo. STAN recommends the latest academic release. |
| [Sage](https://github.com/lazear/sage) | All DDA searches. Bruker `.d` direct (works in production for ddaPASEF). Thermo `.raw` requires mzML conversion first. | MIT |
| [ThermoRawFileParser](https://github.com/compomics/ThermoRawFileParser) | Thermo DDA only — `.raw` → indexed mzML. Auto-downloaded by STAN on first use; cached at `~/.stan/tools/`. | Apache 2.0 |

Sage's built-in LDA rescoring is sufficient for QC-level FDR estimation. STAN does not call Percolator.

---

## Links

| | |
|---|---|
| GitHub | <https://github.com/bsphinney/stan> |
| Community dashboard | <https://community.stan-proteomics.org> · <https://huggingface.co/spaces/brettsp/stan> |
| Community dataset | <https://huggingface.co/datasets/brettsp/stan-benchmark> |
| DE-LIMP (sister project) | <https://github.com/bsphinney/DE-LIMP> |

STAN handles QC and instrument health. For differential-expression analysis and quantitative pipelines, see [DE-LIMP](https://github.com/bsphinney/DE-LIMP).

---

## Contributing

Pull requests welcome.

1. Fork and create a feature branch.
2. `ruff check stan/` and `pytest tests/ -v` before submitting.
3. Add tests for new behaviour. Prefer real DIA-NN / Sage output snippets in `tests/fixtures/` over synthetic data.
4. Open a PR with a clear description.

For design discussion, open an issue first.

---

## License

**Code.** [STAN Academic License](LICENSE) — free for academic, non-profit, educational, and personal research use. Commercial use (CROs, pharma, biotech) requires a separate license. Contact <bsphinney@ucdavis.edu>.

**Community dataset.** [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

## Citations

If STAN is useful for your work, please cite STAN and the search engines it depends on.

> Phinney BS. STAN: Standardized proteomic Throughput ANalyzer. UC Davis Proteomics Core (2026). <https://github.com/bsphinney/stan>

> Demichev V, Messner CB, Vernardis SI, Lilley KS, Ralser M. DIA-NN: neural networks and interference correction enable deep proteome coverage in high throughput. *Nature Methods*. 2020;17:41–44. <https://doi.org/10.1038/s41592-019-0638-x>

> Lazear MR. Sage: An Open-Source Tool for Fast Proteomics Searching and Quantification at Scale. *J. Proteome Research*. 2023;22(11):3652–3659. <https://doi.org/10.1021/acs.jproteome.3c00486>

> Matthews DE, Hayes JM. Systematic Errors in Gas Chromatography-Mass Spectrometry Isotope Ratio Measurements. *Anal. Chem.* 1976;48(9):1375–1382.
