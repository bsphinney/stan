# STAN User Guide

> *Know your instrument.*

This is the day-to-day manual for STAN — the Standardized proteomic Throughput ANalyzer. It covers installation, the dashboard, every CLI command you're likely to touch, every configuration knob, and the things that go wrong and how to fix them.

If you only have five minutes, read **Who this guide is for**, **Installation**, and **First run with `stan setup`**. The rest is reference.

---

## Table of contents

1. [Who this guide is for](#who-this-guide-is-for)
2. [Installation](#installation)
3. [First run with `stan setup`](#first-run-with-stan-setup)
4. [Watching new acquisitions](#watching-new-acquisitions)
5. [The dashboard](#the-dashboard)
6. [Diagnosing problems — the badges and what to do](#diagnosing-problems)
7. [Maintenance log + column life](#maintenance-log--column-life)
8. [Email reports + Slack alerts](#email-reports--slack-alerts)
9. [Community benchmark](#community-benchmark)
10. [Fleet mode — managing multiple instruments](#fleet-mode)
11. [Backfills + repairs](#backfills--repairs)
12. [Remote control — `stan send-command`](#remote-control)
13. [Configuration reference](#configuration-reference)
14. [CLI reference](#cli-reference)
15. [Glossary](#glossary)
16. [Troubleshooting](#troubleshooting)

---

## Who this guide is for

Proteomics core-facility staff, instrument operators, and PIs running Bruker timsTOF or Thermo Orbitrap instruments. The guide assumes you are comfortable with mass-spec terms (DIA, DDA, FASTA, FDR, iRT) but not necessarily with command-line tools or Python.

You will need to open a terminal and type a command from time to time. You will not need to read or modify Python source code.

---

## Installation

### What you need

- A Windows, macOS, or Linux machine that can see the directory where your instrument writes raw files.
- Python 3.10 or newer.
- DIA-NN and Sage (auto-installed on Windows; manual on macOS/Linux).
- For Thermo DDA only: ThermoRawFileParser (auto-downloaded on first use).
- Optional: an SLURM cluster, if you want to push searches to HPC.

### Windows (recommended)

Download `install-stan.bat` from <https://raw.githubusercontent.com/bsphinney/stan/main/install-stan.bat> (right-click → Save As). Double-click it. The script:

1. Installs Python 3.10+ if it's missing.
2. Clones the STAN GitHub repo and runs `pip install`.
3. Downloads and installs DIA-NN from its GitHub releases page.
4. Downloads and installs Sage.
5. Handles UC Davis / institutional SSL proxies automatically.

To get the latest STAN later, run `update-stan.bat`. It self-updates from GitHub on every run.

> ℹ Only `install-stan.bat` and `update-stan.bat` (with hyphens) are supported. The older underscore versions have been removed.

### macOS / Linux

```bash
$ git clone https://github.com/bsphinney/stan.git
$ cd stan
$ pip install -e ".[dev]"
```

You'll need DIA-NN and Sage installed yourself:

- **DIA-NN.** <https://github.com/vdemichev/DiaNN/releases>. Add to `PATH`, or set `diann_path` in `instruments.yml`.
- **Sage.** <https://github.com/lazear/sage/releases>. Same — `PATH` or `sage_path`.

> ℹ DIA-NN is free for academic research; commercial users need a paid license from Aptila Biotech or Thermo Fisher. STAN does not redistribute either binary.

### PyPI (not yet)

```bash
$ pip install stan-proteomics    # NOT yet published. Install from GitHub for now.
```

### Verify the install

```bash
$ stan version
0.2.239
```

If `stan` isn't found, your Python scripts directory probably isn't on `PATH`. On Windows, the installer adds it automatically; if you skipped that step, search for `Edit the system environment variables` and add the directory `pip` says it installed `stan.exe` into.

A deeper check:

```bash
$ stan doctor
```

`stan doctor` walks through every dependency, finds your DIA-NN and Sage executables, checks that ThermoRawFileParser works, and confirms `~/.stan/` is set up. The result is also synced to the Hive mirror (if configured) so a remote operator can read it.

---

## First run with `stan setup`

```bash
$ stan setup
```

Six questions. The wizard reads everything else (instrument model, vendor, serial number, gradient length, DIA window scheme, LC system, acquisition date) directly from your raw files.

**Question 1 — Watch directory.** Where does your instrument write raw files? The wizard probes the directory, detects whether it contains `.d` (Bruker) or `.raw` (Thermo) files, and shows you what it found. If the directory already exists in `instruments.yml`, you're offered to update the existing entry instead of duplicating it.

**Question 2 — LC column.** The one piece of information STAN cannot read from a raw file. You'll be shown a list of common columns (Aurora, IonOpticks, EvoSep stamps, custom). Pick one or describe your own.

**Question 3 — HeLa amount in nanograms.** Default is 50 ng. This determines the amount bucket your community benchmark submissions land in (ultra-low ≤25, low 26–75, mid 76–150, standard 151–300, high 301–600, very-high >600).

**Question 4 — Community benchmark.** Default yes. If you opt in:

- The wizard generates an anonymous pseudonym ("Caffeinated Quadrupole", "Clogged PeakTail", and so on).
- You can leave it as-is, or claim a custom name. Claiming sends a 6-digit code to your email; you paste the code back into the wizard, and the community relay stores an `auth_token` in `~/.stan/community.yml`.
- Every submission and metadata patch from this point forward sends `X-STAN-Auth: <your token>`. Other STAN installs cannot spoof your name.

You can run `stan verify` at any time to confirm your token still works:

```bash
$ stan verify
Lab name:   "Caffeinated Quadrupole"
Auth token: present
Claim:      verified (claimed by you)
Submissions: 17
```

**Question 5 — Daily QC summary email.** Default yes. If you turn it on, the wizard asks for an email address and offers a Monday morning weekly digest in addition to the daily 07:00 summary. Reports are sent through the [Resend](https://resend.com) API and contain only aggregate metrics — no run names that look like patient IDs.

**Question 6 — Anonymous error telemetry.** Default yes. If STAN crashes, an aggregated error report (error type, STAN version, OS, Python version) is sent to the relay so we can fix it. No file paths, no serial numbers, no patient data. All errors are also logged locally to `~/.stan/error_log.json` (last 100) regardless of this setting.

**After question 6.** The wizard probes for DIA-NN, Sage, and ThermoRawFileParser. If anything is missing, it offers an install path. If your watch directory has existing raw files, you'll be asked whether to run `stan baseline` now, tonight at 20:00, or Saturday at 08:00.

> ℹ You can re-run `stan setup` any time to reconfigure. It dedupes `instruments.yml` automatically — pointing the wizard at a directory that's already configured updates the existing entry instead of creating a duplicate.

---

## Watching new acquisitions

```bash
$ stan watch
```

The watcher daemon runs in the foreground. Leave it running on the instrument PC.

### What it does, in plain English

1. **Watches every directory in `instruments.yml`** where `enabled: true`. It recurses into subdirectories, but it ignores events inside an in-progress Bruker `.d` directory (so you don't get a thousand spurious events from `analysis.tdf` writes).
2. **Applies the QC filter.** By default, only files whose names look like HeLa QC injections trigger a search (regex match against `qc_pattern`). Set `qc_only: false` to process everything; set `monitor_all_files: true` to additionally scan non-QC injections through the Sample Health pipeline.
3. **Waits for stability.** The acquisition is considered finished when the file/directory size hasn't changed for `stable_secs` seconds (default 60 for Bruker `.d`, 30 for Thermo `.raw`). The two are different because Bruker writes a `.d` directory continuously during acquisition while Thermo writes a single `.raw` and closes the handle at the end.
4. **Detects the acquisition mode.** Bruker: read `Frames.MsmsType` from `analysis.tdf` (8 = ddaPASEF, 9 = diaPASEF). Thermo: ThermoRawFileParser metadata extraction. Filename tokens (`-Dia-`, `-Dda-`, `-HCDIT-`, `-diaW22-`, etc.) override the sniffers when present, because Thermo Lumos files in particular have been mis-detected as DIA and burned 4 hours of search time.
5. **Runs the search locally.** DIA-NN for DIA, Sage for DDA. Bruker `.d` is fed directly to either engine. Thermo `.raw` goes directly to DIA-NN; for Sage it's converted to indexed mzML via ThermoRawFileParser first.
6. **Extracts metrics, gates against thresholds, drops a `HOLD_<run>.txt` if the run failed**, writes everything to `~/.stan/stan.db`, and (if you opted in) submits to the community benchmark.
7. **Heartbeats every ~5 minutes.** Writes a `status.json` to the fleet mirror. Polls the command queue every ~30 s for `stan send-command` requests.
8. **Hot-reloads `instruments.yml`, `thresholds.yml`, and `community.yml` every 30 seconds.** No restart needed when you change a watch directory or a threshold.
9. **On startup, sweeps every watch directory** looking for raw files acquired while STAN was offline. Anything within `startup_catchup_days` (default 30) that isn't already in the database is queued through the same handler. So if STAN was down all weekend, Monday morning catches up automatically.

### Stopping the watcher

`Ctrl-C` shuts down gracefully. In-progress local searches are terminated. SLURM jobs continue independently.

### Vendor-specific gotchas

**Bruker `.d`** is a directory, not a file. STAN watches its total size. If your instrument writes post-acquisition calibration data after the apparent end, raise `stable_secs` (90–120 s).

**Thermo `.raw`** is a single binary. mtime + size must both be unchanged for `stable_secs` (default 30). On slow network mounts you may need 45–60 s to avoid premature triggers.

**Junctions / symlinks for filenames containing `--`.** DIA-NN 2.3.x has an argv-parsing bug where any filename containing `--` is interpreted as a flag. STAN auto-creates a junction to a sanitised name and feeds DIA-NN the safe path. You don't need to do anything — but if you see junctions appearing next to your `.d` files, this is why.

---

## The dashboard

```bash
$ stan dashboard
```

Open <http://localhost:8421>.

```bash
$ stan dashboard --port 9000 --host 0.0.0.0    # different port; reachable on the LAN
```

Swagger / OpenAPI reference at <http://localhost:8421/docs>.

The dashboard is a single HTML file (`stan/dashboard/public/index.html`) that loads React and Babel from a CDN. There is no build step.

Eight tabs:

### This Week's QCs

Front page. Shows the last week of QC injections.

A three-way view selector at the top:

- **Gauges.** One gas-gauge widget per instrument with the latest run, IPS pill, and a sparkline.
- **Weekly table.** One row per QC run, grouped by day. Numeric values for Proteins / Peptides / Precursors / MS1 AUC, with a thin range bar per cell and a background tint when the value drifts ≥4% from the instrument's 30-run rolling median.
- **Metric matrix.** Transposed heatmap: rows = the four metrics, columns = QC runs across the week in chronological order. Best for spotting decoupling — for example, MS1 steady but precursors collapsing.

A shared filter bar above the views (Instrument / Since / Sample) defaults to "HeLa QC only" so load-test dilutions don't pollute the trend.

**Today TIC overlay.** Below the views. The single best at-a-glance diagnostic for "is the LC behaving today?" Every TIC trace from the last 7 days, color-coded by sample type (QC / sample / blank). Pump pressure issues and spray dropouts are immediately visible as off-shape traces. The Today TIC overlay has its own filter to switch between Evosep / custom LC and DIA / DDA / mixed modes — defaults to DIA so cycle-time differences don't corrupt the median shape.

> ℹ Saving the view as your default. The view picker is per-session by default. Click **Set as default** to write the choice to the browser's `localStorage`. To set a lab-wide default for every operator, write `~/.stan/ui_prefs.yml` (or `~/STAN/ui_prefs.yml` on Windows):
>
> ```yaml
> front_page_view: weekly_table   # gauges | weekly_table | matrix
> matrix_bar_scale: week_range
> ms1_format: sci                 # sci | short
> ```

### QC History

Sortable table of every QC run. Click a row to open the **Run Details drawer** on the right.

The drawer has:

- All extracted metrics for the run.
- Gate result (PASS / WARN / FAIL) and the reason.
- A **Hide** button. Click it to soft-delete the run from the dashboard (mistaken HeLa, test injection, knocked-over column). The run is set to `hidden=1` in the database; nothing is deleted. The QC History toggle "Show hidden" restores them with a dim style.
- Badges for **PEG**, **drift**, and **4DFF**. Each badge is clickable and opens a modal — see [Diagnosing problems](#diagnosing-problems) below.

Failed runs (near-zero IDs from a missed injection or empty spray) are hidden by default. Toggle "Show failed" to see them.

### Trends

Time-series of every run for one instrument. One chart per metric: precursors, peptides, proteins, IPS, CV, points across peak, mass accuracy, MS2 scan rate, iRT max deviation. The community-median band is overlaid where enough submissions exist for the cohort.

A **Maintenance log** form lives on this tab. Use it to log a column swap, source clean, PM, or calibration. Each event renders as a vertical marker on every trend chart for that instrument, so a sudden shift in precursor count is immediately tied to a known maintenance action. See [Maintenance log + column life](#maintenance-log--column-life).

A **cIRT panel** chart is also on this tab — one line per anchor peptide, observed RT over time, SPD selector. Lets you see RT drift without spiking Biognosys iRT into your samples.

### Sample Health

For Bruker instruments where `monitor_all_files: true` is set, STAN runs a fast rawmeat pass on every non-QC, non-excluded `.d` file and stores a verdict in the `sample_health` table:

- **pass** — looks like a real injection.
- **warn** — MS1 max-intensity is below the rolling 30-day median, or spray dropout rate is elevated, or run duration is shorter than expected for the configured gradient.
- **fail** — empty acquisition, truncated run, or no spray detected.

The Sample Health tab lists every monitored injection with its verdict and lets you filter to just `warn` or `fail` to triage. Sample Health verdicts do **not** trigger the HOLD flag and are **not** submitted to the community benchmark. The whole point is to surface bad injections that aren't QC runs but are about to waste a sample slot.

Thermo support is planned but not shipped yet.

### Fleet

Aggregates `status.json` files from every instrument PC pointing at the same fleet mirror (see [Fleet mode](#fleet-mode)). One row per host with:

- STAN version, last heartbeat (UTC).
- Last run name + gate result.
- `stan.db` row count.
- Free disk space on the user-config drive.

Per-host buttons trigger remote commands: ping, status, tail_log, export_db_snapshot, watcher_debug. Results stream back via the command queue and render in a side panel.

> ⚠ As of v0.2.239, the fleet `disk_free_gb` reports the user-config drive (usually C:), not the watch_dir's drive. If your real data drive (D:, E:, ...) is filling up, you won't see it on this tab yet.

### Config

GUI for `instruments.yml`. Each instrument has a card with name, vendor, model, watch directory, output directory, and a **Remove** button (use it for duplicate entries created by an old version of `stan setup`). The full YAML editor is planned; for now, edit `~/.stan/instruments.yml` in any text editor and the daemon will hot-reload within 30 s.

### Community

Live community benchmark view. If you've opted in, this tab shows your instrument's ranking within its cohort, percentile rank for each metric, and the cohort distribution plot. The same data is also published at <https://community.stan-proteomics.org>.

If you haven't opted in, the tab shows a button to run `stan setup` and join.

### 🎮 Arcade

A mini-game. Loads `static/arcade.html` in an iframe. Brett built it for the labs that wanted some fun on the dashboard while their searches ran. Skip if you want; nobody's checking.

---

## Diagnosing problems

This is the section to read when a run fails or a badge goes red.

### IPS — the headline number

A 0–100 cohort-calibrated score. Components:

```
DIA: IPS = 0.50 × s_precursors + 0.30 × s_peptides + 0.20 × s_proteins
DDA: IPS = 0.50 × s_psms       + 0.30 × s_peptides + 0.20 × s_proteins
```

Each `s_*` is a piecewise-linear percentile against the `(instrument family, SPD bucket)` reference cohort (p10 → 30, p50 → 60, p90 → 90). Full design rationale: [`docs/ips_metric.md`](ips_metric.md).

| Score | What it means | What to do |
|---|---|---|
| 90–100 | Excellent | Nothing. |
| 80–89 | Good | Continue monitoring. |
| 60–79 | Marginal | Look at the column, spray, buffers. Flag for next maintenance window. |
| < 60 | Investigate | Check before running real samples. |

Steady downward IPS over weeks, even if individual runs still PASS, almost always means column aging or source degradation.

### HOLD flag — `HOLD_<run_name>.txt`

Written to the instrument's `output_dir` whenever a run FAILs gates. Contents: the run name, which thresholds failed, and a plain-English diagnosis.

Most autosampler queues can poll for this file and pause the next injection. The simplest integration is "if `HOLD_*.txt` exists in directory X, do not start the next sample".

### Plain-English diagnosis cheat-sheet

| Failed metrics | Diagnosis STAN prints |
|---|---|
| Low IDs, normal IPS | Search or library issue. Check spectral library version, FASTA, DIA window scheme. |
| Low IDs, low IPS | LC or source problem. Check column condition, trap, spray stability. |
| High missed-cleavage rate | Incomplete digestion. Check trypsin activity, digest time, denaturation. |
| Elevated +1 fraction | Source contamination, buffer impurity, or spray instability. |
| High CV with normal IDs | LC reproducibility. Check injection volume, carryover, equilibration. |
| Poor mass accuracy | Recalibrate before next injection. |

### PEG contamination panel (Bruker only)

Click the **PEG** badge on a run row. A modal opens with a horizontal lollipop chart: x = m/z, y = intensity, marker color by adduct (`[M+H]⁺` blue, `[M+Na]⁺` amber, `[M+NH4]⁺` purple, `[M+K]⁺` teal). A faint polyline connects each repeat series, so the 44 Da PEG ladder is immediately visible.

Verdicts:

- **clean** — no significant PEG ions.
- **trace** — a few PEG ions, low intensity. Watch.
- **moderate** — clear PEG signal. Plan a solvent / emitter swap.
- **heavy** — PEG is dominating MS1. Stop, replace the emitter and the solvent, then run a blank before the next QC.

PEG comes mostly from solvents in plastic bottles, Evosep emitters, or polyethylene glycol contamination from labware. To populate PEG scores on existing runs, run `stan backfill-peg`. See `docs/PEG_EVOSEP_DIAGNOSTIC.md` for background.

### diaPASEF window-mobility drift (Bruker, DIA only)

Click the **drift** badge on a run row. A modal opens with a scatter: x = window index, y = observed 1/K0 mode, expected band shown as a gray strip, dot color by per-window coverage.

Verdicts:

- **ok** — every window's MS2 ion-mobility centroid is inside its expected band.
- **warn** — one or two windows have walked off; coverage is dropping in those m/z ranges.
- **drifted** — the column or the TIMS calibration has shifted enough to recommend a re-tune. Look at TIMS calibration history and column install date.

To populate drift scores on existing runs: `stan backfill-window-drift`.

### 4DFF Ion Cloud (Bruker)

The Ion Cloud tab on the run drawer renders MS1 features as m/z × 1/K0 × RT, colored by charge.

Two render modes:

- **Plotly per-charge** — when a `.features` file (from the Bruker 4D Feature Finder) exists next to the `.d`. One trace per charge state with the Ziggy palette: `+2` blue, `+1` teal, `+3` green, `+4` orange, `+5` purple, `+6` red, unassigned yellow. DIA windows are overlaid as colored rectangles grouped by `window_group`. Click `+1` in the legend to hide singly-charged contamination and watch the peptide ridge pop.
- **Legacy SVG** fallback — when no `.features` exists, you get a simple m/z × 1/K0 heatmap and a hint to run `stan run-4dff <path/to/.d>` for the richer view.

To enable the Plotly view across the fleet:

```bash
$ stan install-4dff                # downloads uff-cmdline2 to ~/.stan/tools/
$ stan run-4dff /path/to/run.d     # one .d
$ stan backfill-features           # every indexed run
```

The `.features` files live next to the `.d` directories; the dashboard finds them automatically.

> ℹ On Hive/HPC, set `STAN_BRUKER_FF_DIR` to a shared install path so each user doesn't fill their home quota.

### cIRT trends panel

Empirical retention-time anchors derived from your own runs, replacing Biognosys iRT for labs that don't spike standards. The Trends tab shows one line per anchor peptide with a SPD selector.

To populate cIRT data:

```bash
$ stan backfill-cirt           # extract anchor RTs for every existing run
$ stan derive-cirt-panel       # print/save the empirical panel for an instrument family + SPD
```

The panel is built per `(instrument family, SPD bucket)` and stored in the database. Adding a new instrument family or SPD bucket just means accumulating ~20 runs and re-deriving the panel.

---

## Maintenance log + column life

The Trends tab has a small form for logging maintenance:

| Field | What to enter |
|---|---|
| Date | When you did the work. |
| Type | column_install · column_swap · source_clean · pm · calibration · other |
| Notes | Free text. Whatever you want to remember a year from now. |

Logged events render as vertical markers on every trend chart for that instrument. So a sudden shift in precursor count is immediately tied to "I cleaned the source on Tuesday" rather than being mistaken for instrument drift.

`column_install` events feed the **column-life summary** on the same tab: column vendor, model, install date, days in service, and total runs since install. Pair this with `stan column-health` for a longitudinal view.

To log a column install from the CLI:

```bash
$ stan column-install
```

Walks you through vendor / model / serial / install date.

To update an instrument's stored column metadata:

```bash
$ stan set-column "timsTOF Ultra"
```

Maintenance events are stored in the `maintenance_events` SQLite table and are **never** submitted to the community benchmark. They stay local.

---

## Email reports + Slack alerts

### Daily email

Set up via `stan setup` question 5, or directly:

```bash
$ stan email-report                     # send a one-off report right now
$ stan email-report --weekly            # weekly digest format
```

The wizard installs a Windows Scheduled Task / cron entry that runs at 07:00 daily. If you said yes to the Monday weekly digest, a second task fires at 07:00 every Monday with a 7-day rollup.

Email config lives in `~/.stan/community.yml` under `email_reports:`:

```yaml
email_reports:
  enabled: true
  to: lab@university.edu
  daily: "07:00"          # daily send time
  weekly: "monday"        # blank to disable; "monday" sends Mon-morning digest
```

Reports are sent through Resend. The first send asks for your Resend API key and stashes it in the OS keychain.

### Slack alerts

Set up:

1. In Slack, create an Incoming Webhook for the channel you want alerts in. <https://api.slack.com/messaging/webhooks>
2. Edit `~/.stan/community.yml` and add the webhook URL plus your alert preferences:

   ```yaml
   slack_webhook_url: "https://hooks.slack.com/services/T.../B.../..."

   alerts:
     on_qc_fail: true        # alert on FAIL
     on_qc_warn: false       # alert on WARN too (default off)
   ```

3. Test it:

   ```bash
   $ stan test-alert
   ✓ Slack message sent.
   ```

You can override the global setting per instrument:

```yaml
# instruments.yml
- name: "timsTOF Ultra"
  alert_on_fail: true
  alert_on_warn: true
  alert_command: "/path/to/extra-script.sh"   # also run this on a fail
```

---

## Community benchmark

The community benchmark is a live, opt-in HeLa cross-lab comparison hosted at <https://community.stan-proteomics.org>. Hundreds of QC runs from labs around the world, every one searched with the same FASTA + library + parameters.

### How to participate

Run `stan setup` and answer **yes** to the community-benchmark question. You'll get an anonymous pseudonym; you can claim a custom name with email verification. The wizard handles everything; you do not need a HuggingFace account or a HuggingFace token.

To check your status:

```bash
$ stan verify
```

To submit accumulated runs that haven't been pushed yet (for example after enabling the feature on an existing install):

```bash
$ stan submit-all
```

### What gets sent

Aggregate metrics only. Per submission:

- Submission UUID.
- Instrument family + model + serial-number hash (server-side; never echoed back).
- Acquisition mode (DIA / DDA), gradient length, SPD bucket, injection amount.
- Precursor count, peptide count, protein count, PSM count.
- Median CV, median fragments per precursor.
- IPS score, missed cleavage rate, mass accuracy, MS2 scan rate.
- TIC trace (downsampled to 128 bins).
- Cohort key.

Never sent: raw files, sample names, patient metadata, lab member names.

### Hard validation gates

Submissions must pass minimum quality bars to enter the benchmark:

**DIA:** ≥ 1,000 precursors @ 1% FDR; median CV < 60%; +1 fraction < 50%; missed-cleavage rate < 60%.

**DDA:** ≥ 5,000 PSMs @ 1% FDR; ≥ 3,000 unique peptides; ≥ 50% of PSMs with mass error < 5 ppm; MS2 scan rate ≥ 5/min.

If your submission is rejected, the API returns the reason. Most rejections mean "this run was a failed acquisition, not a real QC". Fix the instrument and re-submit.

### Cohorts

Submissions are compared only within `(instrument family, SPD bucket, amount bucket)`. The buckets are:

| SPD bucket | Range | Evosep | Traditional |
|---|---|---|---|
| `200+spd` | ≥ 200 | 500/300/200 SPD | 2–5 min |
| `100spd` | 80–199 | 100 SPD | ~11 min |
| `60spd` | 40–79 | 60 SPD, Whisper 40 | ~21–31 min |
| `30spd` | 25–39 | 30 SPD | ~44 min |
| `15spd` | 10–24 | Extended | ~60–88 min |
| `deep` | < 10 | — | > 2 h |

| Amount bucket | Range |
|---|---|
| ultra-low | ≤ 25 ng |
| low | 26–75 ng (50 ng default) |
| mid | 76–150 ng |
| standard | 151–300 ng |
| high | 301–600 ng |
| very-high | > 600 ng |

A cohort needs at least 5 submissions before its leaderboard activates.

### Tracks

| Track | Mode | Search | Primary metric |
|---|---|---|---|
| A | DDA | Sage | PSM count @ 1% FDR |
| B | DIA | DIA-NN | Precursor count @ 1% FDR |
| C | both | both | Six-axis radar fingerprint |

Track C unlocks when a lab submits both a DDA and a DIA run from the same instrument within 24 h.

### Auth token (fork protection)

`stan setup` claims your name with an email-verified `auth_token` stored in `~/.stan/community.yml`. Every submission and every metadata update sends `X-STAN-Auth: <token>`. The relay only honors a name claim if the token matches. Forks of STAN that skip `stan setup` cannot spoof your name on the leaderboard.

To delete a submission, file a GitHub issue at <https://github.com/bsphinney/stan> with your `submission_id`. Deletions are processed within 7 days.

### Privacy

- Raw files are **never** uploaded.
- Patient or sample metadata is **never** collected.
- Serial numbers are stored server-side but never exposed in API responses or downloads.
- Anonymous submissions are supported (leave `display_name` blank — your pseudonym is still shown).
- Community dataset license: CC BY 4.0.

---

## Fleet mode

If you run STAN on more than one instrument PC and want a single dashboard that sees them all, that's **fleet mode**. Three modes are supported:

| Mode | Storage | When to use |
|---|---|---|
| `smb` | A network share that every instrument PC mounts (e.g. `\\share\STAN\` on Windows, `/Volumes/proteomics-grp/STAN/` on macOS) | The most common setup. Your lab already has a network share. |
| `hf_space` | An HTTP relay through a HuggingFace Space | Sites that can't or won't mount an SMB share — for example, instruments behind a firewall that allows outbound HTTPS only. |
| `none` | Disabled | Single-instrument labs. The default. |

### Setup

The fleet picker isn't surfaced from `stan init` yet (planned). For now, edit `~/.stan/community.yml` directly:

```yaml
fleet:
  mode: smb                                    # smb | hf_space | none
  root_path: /Volumes/proteomics-grp/STAN/     # required for smb
  space_url: https://your-fleet.hf.space       # required for hf_space
```

Restart `stan watch` to pick up the change.

### What lands in the fleet directory

For each host, a subdirectory keyed by hostname:

```
<fleet_root>/<HOSTNAME>/
├── stan.db                   ← full SQLite mirror
├── instruments.yml
├── community.yml
├── thresholds.yml
├── status.json               ← heartbeat (every ~5 min)
├── logs/                     ← every backfill / watch_status / submit log
├── failures/                 ← per-job search failure logs
├── commands/                 ← command queue
│   ├── pending/
│   ├── done/
│   └── results/
├── instrument_library.parquet
└── config/
```

A central workstation that mounts the same share runs `stan dashboard` and `stan fleet-status` — both consume the per-host directories.

### Reading the mirror

When something goes wrong on an instrument PC, **read the mirror first** before asking the operator to run diagnostics. Logs sync continuously; everything you'd want to know is already on the share.

```bash
$ ls -lat /Volumes/proteomics-grp/STAN/TIMS-10878/logs/ | head
$ cat /Volumes/proteomics-grp/STAN/TIMS-10878/logs/watch_status_20260427_153012.log
```

For database queries, copy the file locally first (network-share permissions can interfere with sqlite3):

```bash
$ cp /Volumes/proteomics-grp/STAN/TIMS-10878/stan.db /tmp/x.db
$ sqlite3 /tmp/x.db "SELECT COUNT(*) FROM runs WHERE date(run_date) >= date('now','-7 days');"
```

---

## Backfills + repairs

When you upgrade STAN, add a new metric, or fix a bug, you may need to recompute values for existing runs. These commands are idempotent — safe to re-run.

| Command | When to run it |
|---|---|
| `stan backfill-metrics` | After adding or fixing IPS / FWHM / dynamic-range / MS1-signal logic. Pure parquet-derived; near-instant. |
| `stan backfill-tic` | When TIC traces are missing (failed DIA-NN searches, pre-0.2.64 runs, older imports). Tries Bruker `analysis.tdf` → DIA-NN `report.parquet` → Thermo `fisher_py` in order. Recovered traces are downsampled to 128 bins. Add `--push` to also patch already-submitted community rows. |
| `stan backfill-cirt` | After deriving a new cIRT panel or upgrading the anchor extractor. Walks every run, extracts anchor RTs from `report.parquet`. |
| `stan backfill-peg` | Bruker only. Populates `peg_*` columns and the `peg_ion_hits` table for runs missing them. |
| `stan backfill-window-drift` | Bruker diaPASEF only. Populates `drift_*` columns and the `drift_window_centroids` table. |
| `stan backfill-features` | Bruker. Runs the 4D Feature Finder over every indexed run to generate `.features` files for the Ion Cloud view. Requires `stan install-4dff` first. |
| `stan repair-metadata` | When prior runs have wrong SPD, wrong run_date, or missing lc_system. Re-reads each raw file. `--dry-run` to preview, `--push` to patch the community relay. |
| `stan fix-spds` | Per-run SPD repair against raw-file metadata. Use this if you find your dashboard cohort-binning a bunch of runs into "SPD unknown" or the wrong bucket. |
| `stan fix-instrument-names` | When old DB rows hold a stale instrument name (model name from metadata while newer rows use `name:` from `instruments.yml`). Migrates the column to a single canonical value. |
| `stan recover-search-outputs` | When STAN crashed mid-search and orphan output directories exist next to `baseline_output/`. Moves them in and re-extracts metrics. |
| `stan list-stale` | List runs whose schema looks older than the current STAN version. Useful before a big backfill push. |
| `stan verify-community-tics` | Detects the v0.2.147 sawtooth-TIC artifact. Run once after upgrading from a pre-v0.2.152 install. |
| `stan baseline-download` | Pull baseline output back from Hive (HPC mode). |

The `stan baseline` command runs `backfill-tic` automatically at startup, so most labs don't need to invoke it manually.

A planned `stan backfill-all` wrapper will chain the right backfills in the right order. For now, the recommended sequence after a fresh install or major upgrade is:

```bash
$ stan backfill-metrics
$ stan backfill-tic
$ stan backfill-cirt
$ stan backfill-peg            # Bruker only
$ stan backfill-window-drift   # Bruker diaPASEF only
$ stan backfill-features       # Bruker, requires install-4dff first
```

---

## Remote control

If you're on a fleet (see [Fleet mode](#fleet-mode)), you can run commands on a remote instrument PC from any host that mounts the shared drive.

```bash
$ stan fleet-status                                    # snapshot of every host
$ stan send-command status --host lumosRox --wait      # wait for the answer
```

`stan send-command` writes a JSON command file into `<mirror>/<host>/commands/pending/`. The remote `stan watch` daemon picks it up within ~30 s, runs the action, writes the result to `commands/results/<id>.result.json`, and moves the request to `commands/done/`.

There is no shell passthrough. Every action is a pure Python function in `stan/control.py`. The whitelist:

| Action | What it does |
|---|---|
| `ping` | Round-trip alive check. |
| `status` | STAN version, last run, gate result, free disk, row count, heartbeat age. |
| `tail_log` | Last N lines of a named log (`name=baseline`, `name=watch`, etc.). |
| `export_db_snapshot` | Dump `stan.db` to a parquet snapshot in the mirror. |
| `watcher_debug` | Structured snapshot of every active watcher — including events the handler ignored (inside `.d`, extension mismatch, QC-filter reject) with per-category counts. |
| `qc_filter_report` | Scan the watch_dir and show match/reject examples against the live regex; pass `candidate_pattern` to also test a proposed regex. |
| `apply_config` | Hot-reload the three YAML config files via the existing `ConfigWatcher`. |
| `update_stan` | Run `update-stan.bat` on the remote host. |
| `restart_watcher` | Drop a `restart.flag` so the daemon exits gracefully and the supervisor relaunches it. |
| `cleanup_excluded` | Delete `runs` rows that match an instrument's currently-declared `exclude_pattern`. Cannot delete arbitrary rows. |
| `fix_instrument_names` | Narrowly-scoped UPDATE on `runs.instrument` and `sample_health.instrument`. No DELETE. |
| `v1_prep` | The v1.0 release prep chain — full re-extract + optional re-submit. Spawns a detached subprocess; returns PID + log path. Monitor via the synced log. |

Examples:

```bash
$ stan send-command tail_log --host lumosRox --arg name=watch --arg n=200 --wait
$ stan send-command qc_filter_report --host TIMS-10878 --arg candidate_pattern='(?i)hela|qc_check' --wait
$ stan send-command update_stan --host TIMS-10878 --wait
```

`stan poll-commands` runs one pass of the poller manually. You don't normally need it — `stan watch` polls automatically.

> ⚠ Destructive process-kill is intentionally not on the whitelist. To stop a remote watcher, send `restart_watcher` (graceful) or have the operator close the cmd window.

---

## Configuration reference

All config files live in `~/.stan/`. They are YAML and editable in any text editor. The watcher hot-reloads `instruments.yml`, `thresholds.yml`, and `community.yml` every 30 s.

### `instruments.yml`

```yaml
# Top-level: optional SLURM HPC config. Most labs do NOT need this.
hive:
  host: hive.hpc.ucdavis.edu
  user: brettsp
  key_path: ~/.ssh/id_ed25519       # optional; falls back to ssh defaults

instruments:

  - name: "timsTOF Ultra"
    vendor: bruker                   # bruker | thermo
    model: "timsTOF Ultra"           # AUTO-SET by STAN; do not edit manually
    family: "timsTOF"                # for cohort matching; usually auto-set
    vendor_family: "Bruker timsTOF"  # for community benchmark labels

    # Where the instrument writes raw files
    watch_dir: "D:/Data/raw"
    output_dir: "D:/Data/stan_out"
    extensions: [".d"]               # file extensions to watch
    stable_secs: 60                  # seconds of no size change → ready to process

    # Filtering
    qc_only: true                    # only process names matching qc_pattern
    qc_pattern: "(?i)(hela|qc)"      # regex; default matches HeLa/QC
    monitor_all_files: false         # also rawmeat-process non-QC injections (Bruker only)
    exclude_pattern: "(?i)(wash|blank)"  # regex; never process these even if qc_pattern matches

    # Acquisition
    forced_mode: ""                  # "", "dia", or "dda" — overrides mode detection (Thermo only)
    startup_catchup_days: 30         # on watcher start, look this many days back for runs to catch up

    # LC column (the one thing STAN cannot read from raw files)
    column_vendor: "IonOpticks"
    column_model: "Aurora 25cm × 75µm"

    # Search
    execution_mode: local            # local (default) | slurm
    search_mode: ""                  # "" | "library_free" — DIA-NN only; rarely used
    fasta_path: ""                   # leave blank for auto-download
    lib_path: ""                     # leave blank for auto-download
    diann_path: ""                   # leave blank for PATH lookup
    sage_path: ""                    # leave blank for PATH lookup
    trfp_path: ""                    # leave blank for ~/.stan/tools/ auto-discovery
    keep_mzml: false                 # keep converted Thermo mzML after Sage finishes

    # Cohort + community
    spd: 60                          # samples per day; primary throughput key
    hela_amount_ng: 50
    community_submit: false          # default off — set true to participate

    # Alerts (per-instrument override of global community.yml settings)
    alert_on_fail: true
    alert_on_warn: false
    alert_command: ""                # extra script to run on fail; blank = none

    enabled: true                    # set false to pause this instrument
```

#### Per-instrument key reference

| Key | Type | Default | Notes |
|---|---|---|---|
| `name` | string | (required) | Display name; used as the cohort key. |
| `vendor` | `bruker` \| `thermo` | (required) | Drives stability-detection logic. |
| `model` | string | auto-set | Read from raw-file metadata. Don't edit. |
| `family` | string | auto-set | timsTOF / Astral / Exploris / Lumos / Eclipse / etc. |
| `vendor_family` | string | auto-set | Display label on community charts. |
| `watch_dir` | path | (required) | Where raw files appear. |
| `output_dir` | path | (required) | Where STAN writes results + HOLD flags. |
| `extensions` | list | `[".d"]` or `[".raw"]` | Files to watch. |
| `stable_secs` | int | 60 (Bruker) / 30 (Thermo) | Seconds of no size change before processing. |
| `qc_only` | bool | true | Only files matching `qc_pattern` are searched. |
| `qc_pattern` | regex | `(?i)(hela|qc)` | Which filenames are QC injections. |
| `monitor_all_files` | bool | false | Bruker only. Send non-QC files through rawmeat / Sample Health. |
| `exclude_pattern` | regex | none | Never process matches, even when QC. |
| `forced_mode` | `""` \| `dia` \| `dda` | "" | Skip mode detection (recommended for Thermo). |
| `startup_catchup_days` | float | 30 | Days back to sweep on `stan watch` start. |
| `column_vendor`, `column_model` | string | "" | Set by `stan setup` or `stan column-install`. |
| `execution_mode` | `local` \| `slurm` | local | Where DIA-NN / Sage actually runs. |
| `search_mode` | string | "" | DIA-NN only. Empty = library mode (recommended). |
| `fasta_path`, `lib_path` | path | "" | Empty = auto-download community FASTA/lib. |
| `diann_path`, `sage_path`, `trfp_path` | path | "" | Empty = auto-discover. |
| `keep_mzml` | bool | false | Keep Thermo `.raw` → mzML after search. |
| `spd` | int | (required) | Samples per day. Primary cohort key. |
| `hela_amount_ng` | float | 50 | Injection amount in ng. |
| `community_submit` | bool | false | Opt in for this instrument. |
| `alert_on_fail`, `alert_on_warn` | bool | (community.yml default) | Per-instrument override. |
| `alert_command` | path | "" | Extra script to run alongside Slack alert. |
| `enabled` | bool | true | Set false to pause without removing the entry. |

#### SLURM keys (only when `execution_mode: slurm`)

| Key | Default | Notes |
|---|---|---|
| `hive_partition` | high | SLURM partition. |
| `hive_account` | "" | SLURM account / charge code. |
| `hive_mem` | from `community_params.py` | Memory request (e.g. `32G`). |

### `thresholds.yml`

Per-model dia/dda thresholds. A `default` entry applies when no model-specific entry exists.

```yaml
thresholds:

  default:
    dia:
      n_precursors_min: 5000
      median_cv_precursor_max: 20.0
      missed_cleavage_rate_max: 0.20
      pct_charge_1_max: 0.30
      ips_score_min: 50
      irt_max_deviation_max: 5.0
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

| Key | Mode | What it gates |
|---|---|---|
| `n_precursors_min` | DIA | Minimum precursors @ 1% FDR. |
| `n_psms_min` | DDA | Minimum PSMs @ 1% FDR. |
| `median_cv_precursor_max` | DIA | Max median CV (%). |
| `missed_cleavage_rate_max` | both | Max missed-cleavage fraction (0–1). |
| `pct_charge_1_max` | both | Max +1 fraction (0–1). |
| `ips_score_min` | both | Minimum IPS. |
| `irt_max_deviation_max` | DIA | Max iRT deviation (min). |
| `pct_delta_mass_lt5ppm_min` | DDA | Minimum fraction with mass error < 5 ppm. |
| `ms2_scan_rate_min` | DDA | Minimum MS2 scans per minute. |

Start with the defaults. Tune per model after collecting a few weeks of data on your specific methods.

### `community.yml`

```yaml
display_name: ""                         # "" = anonymous (pseudonym shown)
pseudonym: "Caffeinated Quadrupole"      # auto-assigned by stan setup
auth_token: "..."                        # set by stan setup email verification
community_submit: false                  # global default (per-instrument override available)

slack_webhook_url: ""                    # paste from Slack incoming webhook
alerts:
  on_qc_fail: true
  on_qc_warn: false

email_reports:
  enabled: true
  to: lab@university.edu
  daily: "07:00"
  weekly: "monday"                       # "" to disable

error_telemetry: true
```

| Key | Notes |
|---|---|
| `display_name` | Empty = your pseudonym is shown. Filled = name claim attempt (must match the relay token). |
| `pseudonym` | Auto-assigned fun name. Stable across re-runs of `stan setup`. |
| `auth_token` | Set when `stan setup` email-verifies a name claim. Sent as `X-STAN-Auth` on every submission. |
| `community_submit` | Global default. Each instrument's `community_submit:` overrides. |
| `slack_webhook_url` | Slack incoming-webhook URL. |
| `alerts.on_qc_fail`, `alerts.on_qc_warn` | Defaults for every instrument. |
| `email_reports.*` | Daily 07:00 report config. `weekly: "monday"` adds a Monday digest. |
| `error_telemetry` | Opt-in anonymous error reports. |

> ℹ Fleet-sync settings live in their own file, `~/.stan/fleet.yml` (next section), not in `community.yml`.

### `~/.stan/fleet.yml`

Created by the fleet-sync wizard inside `stan init` / `stan setup` — you only edit it directly if you need to change modes after the fact.

```yaml
fleet:
  mode: smb                              # smb | hf_space | none
  root_path: /Volumes/proteomics-grp/STAN/   # required when mode=smb
  space_url: ""                          # required when mode=hf_space
```

| Key | Notes |
|---|---|
| `fleet.mode` | `smb` (network share, current default), `hf_space` (HTTP-relayed mirror, no SMB needed), or `none` (this instrument doesn't participate in the fleet view). |
| `fleet.root_path` | The path the watcher syncs `stan.db`, `status.json`, configs, and logs into. Must be writable by the operator account running `stan watch`. |
| `fleet.space_url` | URL of the HF Space relay when `mode=hf_space`. Used by labs that can't mount the SMB share. |

To change modes later, re-run `stan init --reconfigure-fleet` (or hand-edit and restart `stan watch`).

### `~/.stan/ui_prefs.yml` (optional, lab-wide UI defaults)

```yaml
front_page_view: weekly_table   # gauges | weekly_table | matrix
matrix_bar_scale: week_range    # week_range | baseline_gates
ms1_format: sci                 # sci | short
```

Per-user `localStorage` always overrides these. Unknown keys are silently ignored. Hot-reloaded via the same `ConfigWatcher`.

---

## CLI reference

Alphabetical list of every command. Run `stan <command> --help` for the full flag list.

| Command | Purpose |
|---|---|
| `stan add-watch <DIR>` | Add a watch directory. Probes for vendor (`.d` → bruker, `.raw` → thermo) and prompts for the QC filter. Use `-y` for the default QC pattern, `--qc-pattern` for a custom regex, `--all-files` to disable filtering. |
| `stan backfill-cirt` | Extract cIRT anchor RTs from every run's `report.parquet`. |
| `stan backfill-features` | Run the Bruker 4D Feature Finder over every indexed run to generate `.features` files for the Ion Cloud view. |
| `stan backfill-metrics` | Recompute IPS / FWHM / dynamic-range / MS1 metrics for every run. Pure parquet-derived; near-instant. |
| `stan backfill-peg` | Bruker only. Populate `peg_*` columns and the `peg_ion_hits` table. |
| `stan backfill-tic` | Recover missing TIC traces (Bruker → DIA-NN → Thermo `fisher_py`). 128-bin downsample. `--push` patches submitted community rows. |
| `stan backfill-window-drift` | Bruker diaPASEF only. Populate `drift_*` columns and `drift_window_centroids`. |
| `stan baseline` | Retroactively process existing HeLa QC files. Recursive discovery, auto-detect gradient/LC, pre-flight DIA-NN/Sage tests, scheduling (now / tonight 20:00 / Saturday 08:00). |
| `stan baseline-download` | Mirror baseline output back from Hive. |
| `stan build-library` | Build an instrument-specific spectral library from local runs. |
| `stan column-health <INSTRUMENT>` | LC column condition from longitudinal TIC trends. Reports healthy / watch / degraded. |
| `stan column-install` | Log a new column install (vendor / model / serial / install date). |
| `stan dashboard` | Serve the FastAPI + React dashboard. `--port 9000 --host 0.0.0.0` to expose on the LAN. |
| `stan derive-cirt-panel` | Print or save the empirical cIRT panel for an instrument family + SPD bucket. |
| `stan doctor` | Environment + dependency check. Synced to the Hive mirror. |
| `stan email-report` | Send a one-off daily or weekly HTML report. `--weekly` for the digest format. |
| `stan export` | Archive `~/.stan/` to a single file (archive / json / parquet). |
| `stan fix-instrument-names` | Migrate stale `runs.instrument` values to a single canonical name. |
| `stan fix-spds` | Per-run SPD repair against raw-file metadata. `--dry-run` to preview. |
| `stan fleet-status` | Aggregate snapshot of every host's `status.json` on the shared mirror. |
| `stan import` | Restore an archive made by `stan export`. |
| `stan init` | Create `~/.stan/` and seed default config templates. |
| `stan install-4dff` | Download and install Bruker `uff-cmdline2` to `~/.stan/tools/` (or `STAN_BRUKER_FF_DIR`). |
| `stan install-peg-deps` | Install PEG / drift dependencies (alphatims and friends, Bruker only). |
| `stan list-stale` | List runs whose schema looks older than the current STAN version. |
| `stan list-watch` | Show every configured watch directory with its instrument name. |
| `stan log <INSTRUMENT>` | Tail recent gate / processing events for an instrument. |
| `stan poll-commands` | One-pass run of the fleet command-queue poller. Normally `stan watch` does this automatically. |
| `stan recover-search-outputs` | Move orphan search-output directories into `baseline_output/` and re-extract metrics. |
| `stan remove-watch <NAME-OR-NUMBER>` | Remove a watch directory from `instruments.yml`. |
| `stan repair-metadata` | Re-extract spd / run_date / lc_system from raw files. `--dry-run` to preview. `--push` to patch community submissions. |
| `stan run-4dff <PATH>` | Run the Bruker 4D Feature Finder on a single `.d`. |
| `stan send-command <ACTION>` | Send a whitelisted command to a remote instrument. `--host <NAME> --wait` to block until the result lands. |
| `stan set-column <INSTRUMENT>` | Update LC column metadata for an instrument. |
| `stan setup` | Interactive 6-question wizard. Always safe to re-run. |
| `stan status` | Configuration + DB summary. |
| `stan submit-all` | Manually submit every un-submitted local run to the community benchmark. `--force` to resend already-submitted runs. |
| `stan sync` | One-shot push of `stan.db` + config + logs to the fleet mirror. |
| `stan test [N]` | Audit the N most recent runs against the current schema; logs gaps. |
| `stan test-alert` | Send a test Slack message to verify `slack_webhook_url`. |
| `stan verify` | Check the community auth token and relay-side name claim. |
| `stan verify-community-tics` | Detect the v0.2.147 sawtooth-TIC artifact. Run once after a major upgrade. |
| `stan version` | Print version. |
| `stan watch` | Start the watcher daemon (foreground). |
| `stan watch-status` | Disk-vs-DB diff for one instrument: every file on disk and how it routed (or didn't). |

---

## Glossary

**1% FDR** — One percent false discovery rate. Standard threshold for accepting a peptide identification.

**4DFF** — Bruker 4D Feature Finder (`uff-cmdline2`). Generates per-run `.features` files used by the Ion Cloud view.

**Aurora / IonOpticks / Evosep** — Common LC column / system vendors. `column_vendor` / `column_model` distinguish them in `instruments.yml`.

**baseline** — Retroactive processing of existing raw files, as opposed to live monitoring of new acquisitions.

**bucket** — A discrete bin used for cohort matching. SPD buckets and amount buckets are the two used by the community benchmark.

**cIRT panel** — Empirical retention-time anchors derived from your own runs. Replaces commercial iRT spike-ins.

**cohort** — A `(instrument family, SPD bucket, amount bucket)` group within which submissions are compared. A 50 ng timsTOF Ultra at 60 SPD is in a different cohort than a 200 ng Astral at 30 SPD.

**ddaPASEF** — DDA acquisition mode on Bruker timsTOF. `Frames.MsmsType = 8`.

**diaPASEF** — DIA acquisition mode on Bruker timsTOF. `Frames.MsmsType = 9`.

**fleet mirror** — The shared directory (SMB share or HF Space) that all instrument PCs sync to in fleet mode.

**gate** — Threshold check against `thresholds.yml`. A run that fails a gate is FAIL; one that warns is WARN.

**HeLa digest** — Standard tryptic digest from HeLa cells, the proteomics community's reference QC sample.

**HOLD flag** — `HOLD_<run>.txt` written to `output_dir` when a run FAILs. Autosampler queues poll for it and pause the next injection.

**hot-reload** — STAN re-reads YAML config files every 30 seconds without restarting the watcher.

**Hive** — The UC Davis HPC cluster (`hive.hpc.ucdavis.edu`). Optional SLURM target when `execution_mode: slurm`.

**IPS** — Instrument Performance Score. 0–100 cohort-calibrated depth composite.

**LDA rescoring** — Linear discriminant analysis. Sage's built-in PSM rescoring; sufficient for QC-level FDR.

**mirror** — Synonym for fleet mirror.

**modal** — A pop-up panel on the dashboard. PEG and drift breakdowns open as modals from the Run Details drawer.

**mode detection** — The watcher's auto-detection of DIA vs DDA from raw-file metadata.

**mzML** — Open-standard mass-spec format. Sage requires Thermo `.raw` to be converted to mzML first; ThermoRawFileParser does this.

**percentile mapping** — IPS components are scored by piecewise-linear interpolation between cohort p10, p50, and p90.

**pseudonym** — Anonymous lab name assigned by `stan setup` ("Caffeinated Quadrupole" etc.).

**rawmeat** — The lightweight Bruker `.d` reader used by Sample Health to vet non-QC injections.

**relay** — The HuggingFace Space at `brettsp-stan.hf.space` that handles community submissions on behalf of clients. No HF token required client-side.

**run-and-done** — Run a QC, gate the result, and the autosampler waits for the verdict before continuing. STAN's HOLD flag implements this.

**SPD** — Samples per day. Primary throughput unit on Evosep / Vanquish Neo / equivalent. STAN cohorts by SPD bucket, not gradient minutes.

**stable_secs** — Seconds of no size change before STAN considers an acquisition complete.

**TIC** — Total ion chromatogram. Track AUC and peak RT longitudinally for column health.

**TRFP** — ThermoRawFileParser. Converts Thermo `.raw` → indexed mzML for Sage.

**watcher** — The `stan watch` daemon. Tails directories, dispatches searches, gates results.

---

## Troubleshooting

### `Config file not found`

```bash
$ stan init
```

Then check:

```bash
$ ls -la ~/.stan/
```

You should see `instruments.yml`, `thresholds.yml`, `community.yml`.

### Watcher doesn't see new files

1. Check `watch_dir` in `instruments.yml` exists and is readable.
2. Check `enabled: true`.
3. Check the file extension matches `extensions` (`.d` for Bruker, `.raw` for Thermo).
4. For network mounts, verify the mount is responsive.
5. Run with debug logging: `stan watch -v`.
6. Run `stan watch-status` to see exactly which files were detected and how they routed.

### Search engine not found

1. On Windows, re-run `install-stan.bat` — it auto-installs DIA-NN and Sage.
2. `stan baseline` and `stan setup` will prompt for a custom path if the engines aren't found.
3. STAN prefers DIA-NN 2.x over 1.x when both are installed.
4. `where diann` (Windows) / `which diann` (Mac/Linux) confirms `PATH`.

### SLURM job fails (HPC mode only)

1. Check `hive_partition` and `hive_account` in `instruments.yml`.
2. Ensure DIA-NN and Sage are available on the cluster (the Hive container at `/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif` is the recommended one).
3. For Thermo DDA, verify `trfp_path` points to a valid ThermoRawFileParser install.
4. Read the SLURM job output log in `output_dir` for the actual error.

### SSL / certificate errors during install

The Windows installer handles UC Davis institutional SSL proxies automatically. If you still see errors, re-run the installer — both `.bat` files self-update.

### Low identification counts

1. Confirm the correct acquisition mode was detected. The dashboard shows mode per run; check it matches what you actually acquired.
2. DIA: confirm the DIA window scheme matches the community library.
3. DDA: check MS2 scan rate.
4. Compare against the reference ranges below.
5. Look at IPS — if IPS is also low, the issue is LC or source, not search.

### Community submission rejected

Run failed the hard gates. Common reasons:

- Too few IDs (instrument underperforming or wrong mode detected).
- Extremely high CV.
- High +1 fraction (source contamination).

Fix the underlying problem and re-acquire.

### Dashboard won't start

1. Check port 8421 isn't already in use: `lsof -i :8421` (Mac/Linux) / `netstat -ano | findstr 8421` (Windows).
2. Different port: `stan dashboard --port 9000`.
3. Python import error? `python -c "from stan.dashboard.server import app"`.

### Database corruption

`~/.stan/stan.db` (or wherever `instruments.yml` says).

1. Stop watcher + dashboard.
2. Back up: `cp ~/.stan/stan.db ~/.stan/stan.db.bak`.
3. Delete and let STAN recreate: `rm ~/.stan/stan.db`.
4. Restart the watcher. Historical metrics are gone unless you can `stan import` an archive made by `stan export`.

If you have the fleet mirror configured, `stan.db` is mirrored continuously to the share and can be copied back from there.

### File stability never triggers

- Bruker `.d` default `stable_secs: 60`. If your instrument writes calibration data after the apparent end, raise it (90–120 s).
- Thermo `.raw` default `stable_secs: 30`. On slow network mounts, raise to 45–60 s.

### Watcher crashed silently

Since v0.2.155, `stan watch` mirrors every log line to a timestamped file at `~/STAN/logs/watch_<YYYYMMDD_HHMMSS>.log` (the path is printed when the watcher starts). The fleet sync pushes that directory to the shared mirror every 5 minutes, so you can read it from another machine without screenshotting the operator's terminal:

```bash
$ ls -lat /Volumes/proteomics-grp/STAN/<HOSTNAME>/logs/ | head
$ tail -200 /Volumes/proteomics-grp/STAN/<HOSTNAME>/logs/watch_20260427_143052.log
```

If the watcher dies, the last few lines of that log are usually enough to tell you which instrument or file triggered the crash. A separate `~/STAN/logs/watch_alert_<ts>.txt` is also written on unhandled exceptions so the failure stands out from routine INFO output.

### "PEG heavy" badge

See [Diagnosing problems → PEG contamination panel](#diagnosing-problems). Replace the emitter, swap the solvent, run a blank, then re-acquire.

### "Drift drifted" badge

See [Diagnosing problems → diaPASEF window-mobility drift](#diagnosing-problems). Re-tune TIMS calibration; check the column install date.

### "SPD unknown" on the dashboard

Some runs land in "SPD unknown" because the raw-file metadata didn't yield a definitive answer. Run:

```bash
$ stan fix-spds --dry-run
$ stan fix-spds
```

This walks every run and re-applies the resolution chain (Bruker `.d` method XML → TDF MethodName → `Frames.Time` span → `instruments.yml` cohort default → filename regex). If the answer is still NULL, the file genuinely doesn't have the metadata; either remove the run or accept the bucket-mixing.

---

## Expected HeLa reference ranges

For a standard 1-hour gradient, 200–250 ng injection. Tune your thresholds based on your specific methods.

### DIA (DIA-NN, community-standardized search)

| Instrument | Precursors | Peptides | Fragments / precursor | Median CV |
|---|---|---|---|---|
| timsTOF Ultra 2 | 18 000 – 25 000 | 12 000 – 17 000 | 7 – 10 | 4 – 8% |
| timsTOF Ultra | 16 000 – 22 000 | 11 000 – 15 000 | 7 – 10 | 4 – 9% |
| timsTOF Pro 2 | 12 000 – 17 000 | 9 000 – 12 000 | 6 – 9 | 5 – 10% |
| Astral | 20 000 – 28 000 | 14 000 – 19 000 | 8 – 12 | 3 – 7% |
| Exploris 480 | 10 000 – 15 000 | 8 000 – 11 000 | 6 – 8 | 6 – 12% |
| Exploris 240 | 8 000 – 12 000 | 6 000 – 9 000 | 5 – 8 | 7 – 14% |

### DDA (Sage, community-standardized search)

| Instrument | PSMs | Peptides | Median hyperscore | Mass acc < 5 ppm |
|---|---|---|---|---|
| timsTOF Ultra (ddaPASEF) | 50 000 – 90 000 | 14 000 – 22 000 | 28 – 35 | > 95% |
| timsTOF Pro 2 (ddaPASEF) | 35 000 – 65 000 | 11 000 – 17 000 | 25 – 32 | > 95% |
| Astral | 40 000 – 70 000 | 12 000 – 18 000 | 30 – 38 | > 98% |
| Exploris 480 | 30 000 – 55 000 | 10 000 – 16 000 | 28 – 36 | > 98% |
| Exploris 240 | 20 000 – 40 000 | 8 000 – 13 000 | 25 – 33 | > 97% |

---

*STAN — Standardized proteomic Throughput ANalyzer*
*Author: Brett Stanley Phinney, UC Davis Proteomics Core*
*Code: STAN Academic License (free for academic / non-profit; commercial requires a license — contact <bsphinney@ucdavis.edu>). Community data: CC BY 4.0.*
