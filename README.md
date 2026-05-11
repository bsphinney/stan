<p align="center">
  <img src="stan/dashboard/public/icons/icon-512.png" alt="STAN" width="180" height="180">
</p>

# STAN — Standardized proteomic Throughput ANalyzer

> *Know your instrument.*

[![License: STAN Academic](https://img.shields.io/badge/License-Academic-blue.svg)](LICENSE)
[![Dataset: CC BY 4.0](https://img.shields.io/badge/Data_License-CC_BY_4.0-green.svg)](https://creativecommons.org/licenses/by/4.0/)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

**Author**: Brett Stanley Phinney, UC Davis Proteomics Core  
**License**: STAN Academic License (free for academic/non-profit use; see [LICENSE](LICENSE))

> 📖 **New to STAN?** Start with the [**User Guide**](docs/user_guide.md) for installation, daily use, dashboard tour, and troubleshooting.

STAN watches the directory where your instrument writes raw files, runs a standardized DIA-NN or Sage search on every HeLa QC injection, scores the result against your instrument's historical cohort, and drops a HOLD flag if a run fails — before your sample queue continues. A local web dashboard tracks everything. Community benchmark submission is opt-in.

---

## What it is

Proteomics core facilities need continuous, automated QC. The existing options are either vendor-locked (Bruker ProteoScape), expensive, or require bespoke scripting. STAN is an open-source alternative that runs on the instrument workstation, reads Bruker `.d` directories and Thermo `.raw` files natively, and calls DIA-NN or Sage as subprocesses — no proprietary middleware.

The approach is watcher-based: a daemon monitors your acquisition directory, waits for each file to finish writing (vendor-specific stability detection), identifies DIA vs. DDA acquisition mode from the raw file itself, dispatches the appropriate search engine, extracts a fixed set of QC metrics, and gates the result against configurable thresholds. Everything lands in a local SQLite database and is visible on a single-page dashboard. SLURM is supported for labs that want centralized HPC compute.

The community benchmark uses a frozen FASTA + spectral library so that precursor and PSM counts are comparable across labs. Submission is aggregate metrics only — no raw files, no sample metadata leave your building. A public leaderboard is hosted at [community.stan-proteomics.org](https://community.stan-proteomics.org), faceted by instrument family, gradient length (SPD), and injection amount.

---

## Quick install — pick your mode

| If you... | Use mode | Estimated time | Install doc |
|---|---|---|---|
| Have one instrument PC and want auto-QC on it | **A — Local (Windows)** | 5 min | Below |
| Have a beefy Windows box that should process raws from multiple instruments | **B — WSL2** | 20 min | [`docs/INSTALL_MODE_B_WSL.md`](docs/INSTALL_MODE_B_WSL.md) |
| Have HPC access and want centralized compute | **C — SLURM** | 1–3 hours | [`docs/INSTALL_MODE_C_HPC.md`](docs/INSTALL_MODE_C_HPC.md) |

### Mode A — instrument PC (Windows, recommended)

Download [`install-stan.bat`](https://raw.githubusercontent.com/bsphinney/stan/main/install-stan.bat), right-click → Save As, double-click. The script installs Python if needed, clones STAN from GitHub, and auto-installs DIA-NN and Sage from their official release pages.

To update: run [`update-stan.bat`](https://raw.githubusercontent.com/bsphinney/stan/main/update-stan.bat). It self-updates from GitHub and restarts the watcher.

After install, run `stan setup` (a 6-question wizard) then `stan watch` to start the daemon.

### Mode A — Mac / Linux / advanced

```bash
git clone https://github.com/bsphinney/stan.git
cd stan
pip install -e ".[dev]"
```

Install DIA-NN and Sage separately and ensure they are on your `PATH`. Then:

```bash
stan init      # creates ~/.stan/ with config templates
stan setup     # 6-question wizard
stan watch     # start the watcher daemon
stan dashboard # serve dashboard at http://localhost:8421
```

---

## The dashboard

Open `http://localhost:8421` after `stan dashboard`. Eight tabs:

- **This Week's QCs** — gauge, weekly table, or metric-matrix view of recent HeLa runs. IPS badge front and center.
- **QC History** — every run, sortable and filterable. Click a row for the full modal: metric breakdown, gate verdicts, PEG lollipop chart, diaPASEF drift cloud (Bruker), 4DFF Ion Cloud (Bruker, optional).
- **Trends** — longitudinal sparklines for IPS, precursor/PSM count, peptide count, iRT deviation, TIC area, column age. Maintenance events render as vertical markers.
- **Sample Health** — non-QC Bruker `.d` acquisitions monitored for TIC dropout and injection failures.
- **Fleet** — all instruments on the shared drive in one view; send remote commands.
- **Config** — live view of `instruments.yml` and `thresholds.yml`.
- **Community** — your benchmark standing within your cohort, submission log, TIC overlay vs. community runs.
- **Arcade** — retro mini-games with global community leaderboard (opt-in).

---

## Architecture

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

---

## Key design decisions

- **Precursor count (DIA) and PSM count (DDA) are the primary metrics** — not protein count. Protein count is confounded by FASTA choice and inference settings and is shown only as a contextual secondary. This is what makes cross-lab comparison valid.
- **Community benchmark is the cross-lab surface** — submissions are compared only within `(instrument family, SPD bucket, injection amount bucket)`. Opt-in; default off.
- **Privacy** — raw files never leave your lab. Only aggregate run-level metrics are submitted. Serial numbers are stored server-side but never exposed in the API or downloads.
- **SPD-first cohort bucketing** — cohorts are keyed on samples-per-day, not gradient minutes, because SPD directly encodes throughput intent. The layered SPD resolution chain reads Bruker method XML first, then TDF metadata, then gradient frame span, then filename tokens.
- **All three modes share the same `stan.db` schema** — a run processed locally on an instrument PC looks identical in the database to one processed via SLURM on a cluster.

---

## Supported instruments

| Vendor | Models | Raw format | Modes |
|---|---|---|---|
| Bruker | timsTOF Ultra 2, Ultra, HT, Pro 2, SCP | `.d` directory | diaPASEF, ddaPASEF |
| Thermo | Astral, Exploris 480/240, Orbitrap Fusion Lumos, Eclipse | `.raw` file | DIA, DDA |

---

## Key metrics

| Metric | Modes | What it tells you |
|---|---|---|
| **IPS** (0–100) | DIA + DDA | Cohort-calibrated composite of precursor/PSM + peptide + protein depth. The single number to check first. |
| **Precursor count @ 1% FDR** | DIA | Primary DIA metric. |
| **PSM count @ 1% FDR** | DDA | Primary DDA metric. |
| **Peptide count** | both | Secondary depth metric. |
| **Protein count** | both | Contextual. Never used for ranking. |
| **Missed cleavage rate** | both | Digestion quality. Healthy: < 0.15. |
| **Median CV (precursor)** | DIA, replicates | Quantitative reproducibility. Healthy timsTOF Ultra: 4–9%. |
| **iRT max deviation** | DIA | Retention-time drift from the empirical cIRT panel. |
| **Points across peak** | both | Median MS2 scans per elution peak. Quantitation quality. |
| **PEG contamination score** | Bruker | MS1 scan for the polyethylene-glycol ladder. |
| **diaPASEF window drift** | Bruker | Detects MS2 windows walking off their 1/K0 calibration. |

Full definitions, reference ranges, and formulas: [`docs/user_guide.md`](docs/user_guide.md) and [`docs/ips_metric.md`](docs/ips_metric.md).

---

## Community benchmark

| | |
|---|---|
| Public dashboard | [community.stan-proteomics.org](https://community.stan-proteomics.org) · [HF Space](https://huggingface.co/spaces/brettsp/stan) |
| Public dataset | [huggingface.co/datasets/brettsp/stan-benchmark](https://huggingface.co/datasets/brettsp/stan-benchmark) · CC BY 4.0 |

Runs `stan setup`, answer yes to the benchmark question, and STAN claims an anonymous pseudonym and stores an auth token. Subsequent QC runs are submitted automatically via the HF Space relay — no HF token required on your end.

Three tracks: Track A (DDA, PSM primary), Track B (DIA, precursor primary), Track C (both within 24 h from the same instrument — unlocks a six-axis radar fingerprint).

---

## Implementation Status

What ships today vs. what's still planned.

| Component | Status | Notes |
|---|---|---|
| CLI (56 commands) | Done | Full list in `docs/user_guide.md`. |
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
| Arcade → community leaderboard | Done | `stan/community/arcade_submit.py`; relay endpoints in `stan/community/scripts/relay_arcade.py`. Opt-in via `arcade_submit: true` in `community.yml`. |
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

## Documentation index

| Doc | Contents |
|---|---|
| [`STAN_MASTER_SPEC.md`](STAN_MASTER_SPEC.md) | Authoritative design doc. Read before changing core behavior. |
| [`docs/INSTALL_MODE_B_WSL.md`](docs/INSTALL_MODE_B_WSL.md) | Mode B — WSL2 install and configuration. |
| [`docs/INSTALL_MODE_C_HPC.md`](docs/INSTALL_MODE_C_HPC.md) | Mode C — SLURM/HPC install and configuration. |
| [`docs/user_guide.md`](docs/user_guide.md) | Day-to-day manual: all CLI commands, dashboard tour, config reference, troubleshooting. |
| [`docs/ips_metric.md`](docs/ips_metric.md) | IPS formula, cohort references, why protein count is excluded. |
| [`docs/external_tools.md`](docs/external_tools.md) | DIA-NN, Sage, ThermoRawFileParser: CLI flags, version pins, container paths, gotchas. |
| [`docs/HPC_PATHS.md`](docs/HPC_PATHS.md) | Hive HPC reference paths for SLURM integration. |
| [`docs/GOTCHAS_DELIMP.md`](docs/GOTCHAS_DELIMP.md) | 50+ hard-learned lessons: DIA-NN edge cases, SLURM quirks, raw-file parsing traps. |
| [`CLAUDE.md`](CLAUDE.md) | Context for AI coding agents working on this codebase. |

---

## Search engines

STAN does not bundle DIA-NN, Sage, or ThermoRawFileParser. Each is called as a subprocess and must be installed separately. The Windows installer fetches DIA-NN and Sage automatically.

| Tool | Used for | License |
|---|---|---|
| [DIA-NN](https://github.com/vdemichev/DiaNN) | All DIA searches (Bruker `.d` and Thermo `.raw` natively, no conversion) | Free for academic research; commercial use requires a paid license from Aptila Biotech or Thermo. |
| [Sage](https://github.com/lazear/sage) | All DDA searches. Bruker `.d` native. Thermo `.raw` requires mzML conversion first. | MIT |
| [ThermoRawFileParser](https://github.com/compomics/ThermoRawFileParser) | Thermo DDA only — `.raw` → indexed mzML. Auto-downloaded on first use; cached at `~/.stan/tools/`. | Apache 2.0 |

---

## Citing STAN

No paper yet. Until one lands, please cite:

> Phinney BS. STAN: Standardized proteomic Throughput ANalyzer. UC Davis Proteomics Core (2026). <https://github.com/bsphinney/stan>

Also cite the search engine(s) STAN runs on your data:

> Demichev V, et al. DIA-NN: neural networks and interference correction enable deep proteome coverage in high throughput. *Nature Methods*. 2020;17:41–44. <https://doi.org/10.1038/s41592-019-0638-x>

> Lazear MR. Sage: An Open-Source Tool for Fast Proteomics Searching and Quantification at Scale. *J. Proteome Research*. 2023;22(11):3652–3659. <https://doi.org/10.1021/acs.jproteome.3c00486>

---

## Contributing

Open an issue for design discussion first, then submit a PR. Run `ruff check stan/` and `pytest tests/ -v` before submitting. Prefer real DIA-NN or Sage output snippets in `tests/fixtures/` over synthetic data.

---

## License

**Code**: [STAN Academic License](LICENSE) — free for academic, non-profit, educational, and personal research use. Commercial use (CROs, pharma, biotech) requires a separate agreement. Contact <bsphinney@ucdavis.edu>.

**Community dataset**: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).

---

## Links

| | |
|---|---|
| GitHub | <https://github.com/bsphinney/stan> |
| Community dashboard | <https://community.stan-proteomics.org> · <https://huggingface.co/spaces/brettsp/stan> |
| Community dataset | <https://huggingface.co/datasets/brettsp/stan-benchmark> |
| DE-LIMP (sister project) | <https://github.com/bsphinney/DE-LIMP> |
