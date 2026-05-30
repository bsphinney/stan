<p align="center">
  <img src="../stan/dashboard/public/icons/icon-512.png" alt="STAN" width="160" height="160">
</p>

# STAN User Guide

> *Know your instrument.*

This is the day-to-day manual for STAN — the Standardized proteomic Throughput ANalyzer. It assumes STAN is already installed and `stan` is on your PATH. If you haven't installed it yet, start with the README or the relevant install doc for your setup.

---

## Table of contents

1. [Quick orientation](#quick-orientation)
2. [Day-1 setup](#day-1-setup)
3. [Acquiring your first QC run](#acquiring-your-first-qc-run)
4. [Reading the dashboard](#reading-the-dashboard)
5. [The IPS score](#the-ips-score)
6. [The community benchmark](#the-community-benchmark)
7. [Three deployment modes](#three-deployment-modes)
8. [Remote viewing — Tailscale and phone access](#remote-viewing--tailscale-and-phone-access)
9. [STAN Godmode — multi-instrument view](#stan-godmode--multi-instrument-view)
10. [Common workflows — recipes](#common-workflows--recipes)
11. [Troubleshooting](#troubleshooting)
12. [Where to get help](#where-to-get-help)

---

## Quick orientation

STAN runs as three loosely coupled pieces. The **watcher daemon** (`stan watch`) runs in the background on the machine connected to your instrument. It monitors the directories you configure and picks up new raw files as soon as acquisition finishes. When it sees a new QC run — a HeLa standard or other file matching the QC filename pattern — it dispatches a database search (DIA-NN for DIA data, Sage for DDA data), extracts quality metrics from the results, and writes everything to a local SQLite database.

The **dashboard** (`stan dashboard`) is a local web app served at `http://localhost:8421`. Open it in any browser on the same machine. It reads the same SQLite database the watcher writes to and shows your QC runs as they accumulate — IPS scores, peptide/precursor counts, TIC traces, ion mobility clouds, chromatography trends, and more. The **community benchmark** is optional: if you opt in, STAN submits aggregate metrics (never raw files or patient metadata) to a shared HF Dataset so you can see how your instrument compares to other labs. The public dashboard is at `https://huggingface.co/spaces/brettsp/stan`.

---

## Day-1 setup

### `stan init`

Run this once after installing:

```
stan init
```

This copies the default config files (`instruments.yml`, `thresholds.yml`, `community.yml`) to `~/.stan/` without overwriting anything that already exists. It also walks you through the **fleet-sync wizard** — a short prompt that tells the godmode multi-instrument view where to find this instrument's mirrored QC data. If you skip or need to redo just that part later, run:

```
stan init --reconfigure-fleet
```

After `stan init`, open `~/.stan/instruments.yml` in any text editor. Add an entry for your instrument with at minimum:
- `name` — a short label (e.g. `timsTOF_HT`)
- `watch_dir` — the directory where raw files land (e.g. `D:\Data\HeLa_QCs`)
- `vendor` — `bruker` or `thermo`
- `family` — instrument family (e.g. `timstof`, `exploris`, `lumos`)

For a guided alternative, run `stan setup`, which walks you through instrument selection and directory configuration interactively.

### First run of `stan watch`

```
stan watch
```

You'll see a startup banner with the STAN version, and a line showing where the watcher log is written:

```
STAN v1.0.0 — watcher starting
Log: /Users/you/STAN/logs/watch_20260508_143012.log
```

The watcher polls each configured `watch_dir` for new raw files. A healthy watcher sits quietly and only prints activity when it finds something. You can leave it running in a terminal or start it as a background service. To confirm it's healthy, check the log file shown at startup — warnings and errors go there.

### First open of `stan dashboard`

In a separate terminal (while `stan watch` is running):

```
stan dashboard
```

Then open `http://localhost:8421` in Chrome, Edge, or Firefox. You'll see nine tabs across the top:

| Tab | What it shows |
|-----|---------------|
| **This Week's QCs** | Live view of QC runs from the current week |
| **QC History** | Full historical table of all QC runs |
| **Trends** | Time-series charts of metrics across weeks/months |
| **Sample Health** | Non-QC files: washes, blanks, and real samples |
| **Fleet** | Status of all instruments in your lab (if fleet sync is configured) |
| **Config** | Per-instrument configuration summary |
| **Community** | Community benchmark opt-in and submission status |
| **Arcade** | Leaderboard games built on community benchmark data |
| **Museum** | Interactive historical QC archive — 999 BSA injections 2005–2022, all instrument eras |

---

## Acquiring your first QC run

STAN uses a filename regex to decide whether a raw file is a QC standard or a real sample:

```
(?i)(he(l[a5\d]|\d)|qc|std[_\-\s]?he)
```

In plain English, files whose names contain any of the following are treated as QC runs and routed through the full search + metrics pipeline:

- `hela`, `hel5`, `hel0` ... `hel9` (case-insensitive)
- `qc` anywhere in the name
- `std_he`, `std-he`, `std he`, `stdhe` and variations

Everything else — washes, blanks, real samples — is routed to the **Sample Health** monitor pipeline, which tracks file counts and flags unusual patterns without running a full search.

**Worked example.** If you're running a timsTOF, name your HeLa standards something like:

```
HeL50_30spd_001.d
HeL50_30spd_002.d
QC_HeLa_Whisper40_20260508.d
```

All of these match. A file named `Sample_Patient_001.d` does not match and goes to Sample Health instead.

For Thermo instruments, the same rule applies to `.raw` files:

```
HeLa_50ng_30min_001.raw
QC_Exploris_20260508.raw
```

---

## Reading the dashboard

### This Week's QCs

The landing tab. Shows every QC run from the current week (Monday–Sunday) as a row in a table. Each row has the run name, acquisition date/time, IPS score (0–100), precursor/PSM count, peptide count, protein count, and a PASS / WARN / FAIL badge based on your threshold config. You can switch between three view modes at the top: **Gauges** (big metric dials), **Weekly table** (the default), or **Metric matrix** (a heatmap grid useful when you have many runs). The TIC overlay panel below the table shows all this-week traces overlaid — diverging traces are the first visual signal of an instrument problem.

### QC History

The same run data across all time. Use the date filter controls (Week / Month / 3 Months / 6 Months / Year / All) to narrow the view. Clicking a run name opens the run detail panel with per-run drift plots, TIC trace, and raw metric values. If you see a step-change in precursor counts at a particular date, check the maintenance log in Config for a column change event that might explain it.

### Trends

Time-series charts of key metrics plotted chronologically. One chart per metric (precursors, peptides, IPS, TIC AUC, etc.), with optional community median overlaid as a dashed line. Use the instrument selector at the top if you have multiple instruments configured. The time filter (Week / Month / 3 Months / etc.) lets you zoom in or out. A gradual downward slope in precursor count over weeks typically means column degradation. A sudden drop in a single run usually means a bad injection or a search failure.

### Sample Health

A separate table for non-QC files — everything that didn't match the QC filename pattern. Washes, blanks, and real samples show up here with file counts and flags for unusual patterns. This tab doesn't show search metrics (no database search is run for samples), but it helps you track whether the instrument is running the expected number of acquisitions per day and whether blanks look clean.

### Fleet

If you have fleet sync configured (via `stan init`'s wizard or `stan init --reconfigure-fleet`), this tab shows the status of all instruments in your lab — last QC time, current IPS, PASS/WARN/FAIL state. Each row is expandable. This is most useful from the godmode global view; on a single-instrument install it shows just your one machine.

### Config

A summary of your current `instruments.yml` settings: watch directories, LC column, vendor, family, community submission status. Use this to quickly confirm STAN is watching the right directory. If you need to change anything, edit `~/.stan/instruments.yml` directly — the watcher hot-reloads config changes within 30 seconds.

### Community

Shows your community benchmark opt-in status and a log of recent submissions. From here you can also manually trigger a submission or check whether any runs are queued. The "ID depth" chart compares your instrument's precursor/PSM counts against the community distribution for the same instrument family and throughput bucket (SPD).

### Arcade

Retro mini-games (Keratin Invaders, Angry Mass Specs, m/zork) playable directly in the dashboard. Scores can be submitted to a global community leaderboard so labs can compare.

#### Community Arcade Leaderboard

When `community_submit: true` is set in `~/.stan/community.yml`, arcade scores are automatically submitted to the community relay after each game. The top 5 scores per game are shown in the **Community high scores** panel at the top of the Arcade tab, pulled live from the relay.

**What gets sent** (privacy-first, same rules as QC submissions):
- Game name, numeric score, win/loss flag
- Your lab pseudonym (`display_name` from `community.yml`)
- Instrument *family* only (`timsTOF`, `Exploris`, `Lumos` — never the full model name or serial number)
- STAN version and timestamp

**Nothing else.** No raw files, no sample metadata, no patient identifiers.

**To opt in without enabling QC submissions**, add to `~/.stan/community.yml`:

```yaml
arcade_submit: true
```

**To opt out of arcade submissions only** while keeping QC submissions on:

```yaml
community_submit: true
arcade_submit: false
```

**Rate limit:** the relay accepts up to 5 score submissions per lab per game per hour to prevent spam. Duplicate submissions within 60 seconds are silently deduplicated.

The relay-side implementation is documented in `stan/community/scripts/relay_arcade.py` — this reference file describes the API contract Brett uses to deploy the endpoints on the HF Space.

### Museum

An interactive historical QC archive celebrating 999 BSA injections collected at the UC Davis Proteomics Core from 2005 to 2022 — spanning every instrument era from the LTQ ion trap through the Q-Exactive Plus. The page is a standalone HTML file (`stan/dashboard/public/museum.html`) that loads from `/static/museum.html` in the iframe.

**What the museum shows:**

- **Timeline** — one card per instrument era with peak PSM count, median, sparkline of run-to-run variability, and hover detail for best/worst run filenames.
- **Trend chart** — scatter plot (log scale) of every dated BSA injection 2005–2022 plus modern HeLa corpus points, color-coded by era. Filter by instrument type. Click a point to see the run name.
- **BSA coverage maps** — the same 607-AA bovine albumin (P02769) sequence visualized as a horizontal bar, with identified peptide spans highlighted separately for a 2007 LTQ-FT run (46.5% coverage), the 2017 Q-Exactive Plus record (56% coverage), and an Astral reference projection. The same protein, characterized progressively more completely over time.
- **Curio cabinet** — six annotated stories: oldest identification (Jan 26 2006), all-time record run (921 PSMs, May 2017), the Michrom LC era, the BSA lot transition, the LTQ-FT peak, and the 2022 coda when HeLa replaced BSA as the QC standard.
- **Then vs Now table** — direct comparison from 184 PSMs on an LTQ in 2006 to 31,672 precursors on a timsTOF HT in 2026, with a future-state Astral row from the published Orsburn et al. 2023 benchmark.
- **TIC comparison** — simulated chromatogram envelopes showing the shape difference between a 2007 LTQ-FT run and a modern Lumos HeLa acquisition.

**Deploying to the community HF Space:** see `docs/MUSEUM_DEPLOY.md` for step-by-step instructions. The page is fully self-contained — no STAN API calls — so it works as a static file on any HF Space without a running backend.

---

## The IPS score

The **Instrument Performance Score** is a single 0–100 number that answers: *how well did this run perform compared to other runs on the same instrument class at the same throughput?*

A score of 60 means you matched the median for your instrument class. A score of 90 means you matched the top 10%. A score below 30 means you underperformed the bottom 10% — something is likely wrong.

**Formula:**

- DIA: `IPS = 0.50 × s_precursors + 0.30 × s_peptides + 0.20 × s_proteins`
- DDA: `IPS = 0.50 × s_psms + 0.30 × s_peptides + 0.20 × s_proteins`

Each `s_*` term is a piecewise-linear percentile score (0–100) computed against a reference cohort bucketed by `(instrument_family, SPD)`. The cohort comparison is why SPD matters — a 100-SPD whisper run should not be scored against a 30-SPD gradient run. Full details and calibration notes are in [`docs/ips_metric.md`](ips_metric.md).

Protein count is a secondary input (20% weight) because it's context-dependent with a frozen FASTA. Precursor count (DIA) or PSM count (DDA) is the primary metric — it's the most direct measure of instrument sensitivity.

---

## The community benchmark

The community benchmark lets you see how your instrument compares to instruments at other labs running the same QC standards. It's entirely optional and privacy-first.

**What gets sent:**
- Aggregate metrics: precursor count, peptide count, PSM count, IPS score, gradient length, instrument family, search engine version
- Nothing else. Raw files are never uploaded. No patient metadata. No sample identifiers. No filenames. Serial numbers are optional, stored server-side, and never exposed in public downloads.

**Privacy note:** The community dataset publishes aggregate metrics. STAN strips raw filenames before submit. If your QC filename contained patient identifiers, only the local stan.db retains them — the public dataset never sees them.

**How to opt in:**

1. Open `~/.stan/community.yml` in a text editor
2. Set `community_submit: true`
3. Save the file — the watcher picks it up automatically

From then on, every new QC run that passes local search and metric extraction is submitted automatically. To manually push existing runs:

```
stan submit-all
```

This walks the local database and submits any runs not yet in the community benchmark.

**The public dashboard** is at `https://huggingface.co/spaces/brettsp/stan`. It shows the community leaderboard, SPD-bucketed ID depth comparisons, and cross-lab TIC overlays. Your instrument appears under its family and throughput bucket; no lab name or location is shown unless you choose to add one.

**To disable submissions temporarily:** set `community_submit: false` in `~/.stan/community.yml`. Submissions stop immediately on the next watcher config reload (within 30 seconds).

---

## Three deployment modes

### Mode A — Local (default)

The watcher runs on the instrument PC. DIA-NN or Sage searches also run on the instrument PC, using half the available CPU cores so acquisition isn't disrupted. The SQLite database lives on the instrument PC. The dashboard is served locally.

Good for: single instruments, Windows instrument workstations, labs that don't have a shared compute box.

### Mode B — WSL2 lab box

One or more instrument PCs push raw files to a shared lab workstation running WSL2. The watcher and search jobs run on that box, which has more CPU and RAM than the instrument PCs. The dashboard is served from the lab box and accessible on the local network.

Good for: multiple instruments sharing one fast compute box, labs that want to keep instrument PCs lightly loaded.

Setup details: [`docs/INSTALL_MODE_B_WSL.md`](INSTALL_MODE_B_WSL.md)

### Mode C — SLURM/HPC

Search jobs are dispatched to a SLURM cluster. The SQLite database lives on cluster storage. The dashboard can be SSH-tunneled to your local machine or served behind a proxy. Good for: multi-lab consortia, very high-throughput cores, Hive-class compute environments.

Good for: centralized search across many instruments, DIA-NN runs that benefit from many cores or GPUs.

Setup details: [`docs/INSTALL_MODE_C_HPC.md`](INSTALL_MODE_C_HPC.md)

**Choosing a mode:**

| | Mode A | Mode B | Mode C |
|---|---|---|---|
| Hardware needed | Instrument PC only | Instrument PCs + 1 lab box | Instrument PCs + HPC access |
| Setup effort | Low | Medium | High |
| Best for | 1–2 instruments | 2–6 instruments | 6+ instruments or HPC-connected labs |
| Search speed | Moderate (half cores) | Fast | Fastest |

---

## Remote viewing — Tailscale and phone access

You can view your QC dashboard from your phone, your office computer, or another lab — anywhere on your Tailscale network — without opening firewall ports or setting up a VPN.

### Why bother

The dashboard auto-refreshes as runs come in. Being able to check IPS scores and TIC traces from your phone while you're in a meeting — or from home overnight — means you catch problems before the next morning's queue of samples.

### Install Tailscale

Install Tailscale on the machine running `stan dashboard` (the instrument PC, lab box, or HPC login node):

- **macOS:** `brew install --cask tailscale` or download from [tailscale.com/download](https://tailscale.com/download)
- **Windows:** download the installer from [tailscale.com/download](https://tailscale.com/download)
- **Linux:** `curl -fsSL https://tailscale.com/install.sh | sh`

Then install Tailscale on your phone (iOS or Android — free on both app stores) and any other device you want to use for remote viewing.

### Sign in and connect

On each device, sign in to the same Tailscale account. All devices on the same account form a **tailnet** — a private network only your devices can see. No additional configuration needed.

### Find your machine's Tailscale address

You need two things: your machine's **Tailscale IP** and its **MagicDNS hostname**. Either works for connecting from another device, but the MagicDNS hostname is the better bookmark — it's stable even if the IP changes.

**Step 1.** On the machine running `stan dashboard`, open a terminal and run:

```
tailscale status
```

You'll see one row per device on your tailnet. The first row is always the local machine. Example output:

```
100.110.160.42  cbs-gc1414-mini     you@example.com  macOS  -
100.84.21.56    iphone182           you@example.com  iOS    idle
100.118.39.56   tims-10878          you@example.com  Windows offline
```

The **first column** is the Tailscale IP (e.g. `100.110.160.42`).
The **second column** is the device's hostname (e.g. `cbs-gc1414-mini`) — this is the start of your MagicDNS URL.

**Step 2.** Get the full MagicDNS hostname (which adds a `.tailXXXXX.ts.net` suffix unique to your tailnet). Easiest way:

```
tailscale status --json | grep DNSName
```

Look for the entry matching your machine. Example output:

```
"DNSName": "cbs-gc1414-mini.tail1c95dd.ts.net.",
```

Strip the trailing dot — your **MagicDNS URL** is:

```
http://cbs-gc1414-mini.tail1c95dd.ts.net:8421
```

(The `:8421` is the STAN dashboard port. The short form `http://cbs-gc1414-mini:8421` also works as long as both devices are on the same tailnet, but the full MagicDNS form is universally reliable.)

**Step 3.** Easiest of all — `stan dashboard` prints the URL on startup when Tailscale is detected (see the next section). Just look at the dashboard's launch banner.

### Access the dashboard remotely

`stan dashboard` auto-detects Tailscale at startup. If Tailscale is running and logged in, the dashboard:

- Binds to `0.0.0.0` instead of `127.0.0.1` so Tailscale traffic can reach it
- Adds your Tailscale IP and MagicDNS hostname to the CORS allowlist so godmode action POSTs work without manual config
- Prints the Tailscale URLs at startup:

```
STAN v1.0.0 — dashboard (Tailscale detected)
  Bound to:    0.0.0.0:8421
  Local:       http://localhost:8421
  Tailscale:   http://lumosrox:8421
  Tailscale:   http://lumosrox.tail-xxxx-xx.ts.net:8421
  Tailscale:   http://100.64.1.23:8421
```

On your phone or remote computer, open the MagicDNS URL (e.g. `http://lumosrox.tail-xxxx-xx.ts.net:8421`) in a browser. **Bookmark the MagicDNS hostname** — it's stable even if the Tailscale IP changes.

### macOS firewall gotcha

If you're on macOS and traffic is getting blocked even though Tailscale is connected, the macOS application firewall may be blocking incoming connections to the Python process. To fix it:

1. Open **System Settings → Network → Firewall**
2. Click **Options...**
3. Find `python3.13` (or whatever Python version runs the dashboard) in the list
4. Set it to **Allow incoming connections**

If the Python version isn't listed yet, the firewall may have blocked it silently on first launch. You can also temporarily toggle the firewall off, start `stan dashboard`, let the firewall prompt appear, allow it, then re-enable the firewall.

### Install STAN as a phone app (Add to Home Screen)

STAN's dashboard is a **Progressive Web App (PWA)** — you can pin it to your phone's home screen and it launches full-screen with a STAN icon, just like a native app. No App Store, no Play Store, no install reviewer involved.

**Prerequisites:**
- Tailscale set up on the phone and on the dashboard host (see the section above)
- The dashboard reachable from your phone at the Tailscale URL (e.g. `http://lumosrox.tail-xxxx-xx.ts.net:8421`)

**iPhone / iPad (iOS 14+):**
1. Open the dashboard URL in **Safari** (not Chrome — iOS only allows Safari to install PWAs)
2. Tap the **Share** button (the square with an up-arrow at the bottom of Safari)
3. Scroll down and tap **Add to Home Screen**
4. Confirm the name (defaults to "STAN") and tap **Add**
5. The STAN icon appears on your home screen. Tap it: full-screen dashboard, no Safari URL bar, looks and feels native.

**Android (Chrome, Edge, Firefox):**
1. Open the dashboard URL in your browser
2. Tap the browser menu (three dots, top-right)
3. Tap **Install app** or **Add to Home screen** (wording varies by browser)
4. Confirm; the STAN icon lands on your home screen
5. Some Android launchers crop the icon to a circle — STAN ships a maskable variant so the artwork stays centered.

**Desktop browsers (Chrome, Edge):**
- The address bar shows an "Install" icon (a small monitor with a down-arrow). Click it to install STAN as a standalone window. Works on macOS, Windows, and Linux.

**To uninstall:** long-press the icon on your phone home screen and choose Remove / Delete (it just removes the shortcut — no actual app is uninstalled, no data leaves the dashboard).

---

## STAN Godmode — multi-instrument view

Godmode is a single dashboard view that aggregates data from multiple instruments into one interface. Instead of opening a separate browser tab per instrument, you view a fleet-wide database.

To use it, point `stan dashboard` at a global database that aggregates runs from multiple instruments:

```
STAN_DB_PATH=/path/to/global/stan.db stan dashboard
```

The global database can be:
- A Quobyte/NFS path if all instruments write to shared storage
- A database on an HPC node (SSH-tunnel the dashboard port to your laptop)
- The fleet sync mirror set up via `stan init`'s fleet wizard

Combine godmode with Tailscale for true remote fleet ops: start the dashboard on your lab box or HPC login node, connect via Tailscale from your phone, and watch all your instruments from anywhere.

---

## Common workflows — recipes

**"I just installed; how do I get QC running?"**

```
stan init          # copies config files, runs fleet wizard
# edit ~/.stan/instruments.yml — set watch_dir, vendor, family
stan watch         # start the watcher
# acquire a HeLa run with a QC-matching filename
# open http://localhost:8421 in a browser — run will appear within minutes
```

**"I want to backfill old runs from a directory."**

```
stan backfill-from-dir /path/to/old/raw/files
```

This walks the directory, finds raw files that match the QC pattern, runs the search pipeline on each, and writes results to the database. Useful when you've just installed STAN and have months of existing HeLa runs.

**"I need to disable community submission temporarily."**

Edit `~/.stan/community.yml` and set:

```yaml
community_submit: false
```

Save the file. The watcher reloads config within 30 seconds and stops submitting.

**"I want to share my QC trends with a collaborator."**

Install Tailscale on your machine and theirs, add them to your tailnet (or use Tailscale's share feature for one-off access), and send them your MagicDNS dashboard URL.

**"How do I check what version is running?"**

```
stan version
```

**"How do I update STAN to the latest version?"**

- **Windows:** `stan.bat` self-updates from GitHub on every launch. Just run it.
- **macOS/Linux:**
  ```
  pip install --upgrade 'stan-proteomics @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip'
  ```
  Remember to always bump both `pyproject.toml` and `stan/__init__.py` if you're developing locally.

---

## Troubleshooting

**"Dashboard says 'No QC runs yet'"**

The most common cause is that the watcher is watching the wrong directory, or no files matching the QC filename pattern have been acquired yet. Check `~/.stan/instruments.yml` and confirm `watch_dir` points to where your HeLa raw files actually land. Also confirm your filenames match the QC pattern (contain `hela`, `qc`, or `std-he` / `std_he` variants — case-insensitive).

**"Dashboard is blank or shows an error in Internet Explorer"**

IE is not supported. Use Chrome, Edge (Chromium), or Firefox.

**"Watcher crashes on startup or disappears"**

Check the watcher log file printed at startup (`~/STAN/logs/watch_YYYYMMDD_HHMMSS.log`). Warnings and unhandled exceptions are written there. Common causes: Python version mismatch, instruments.yml parse error (invalid YAML), or a watch directory that doesn't exist.

**"DIA-NN search returns 0 precursors"**

Usually one of three things: (1) missing or mismatched spectral library — STAN requires a spectral library for DIA, library-free mode is not supported; (2) missing FASTA file; (3) on Linux, the .NET SDK required by ThermoRawFileParser is not installed for Thermo `.raw` files. See [`docs/external_tools.md`](external_tools.md) for library/FASTA paths and tool requirements. For SLURM/HPC deployments, see [`docs/INSTALL_MODE_C_HPC.md`](INSTALL_MODE_C_HPC.md).

**"I can't see my dashboard from my phone"**

See the [Tailscale section](#remote-viewing--tailscale-and-phone-access). The most common causes: Tailscale isn't installed on one of the devices, the devices are on different Tailscale accounts, or the macOS application firewall is blocking Python's incoming connections.

**"stan: command not found after install"**

The virtual environment isn't activated, or your terminal hasn't picked up the updated PATH. Try restarting the terminal. On Windows, confirm the STAN venv `Scripts\` directory is on your PATH. If you have both an old `.stan\venv` and a new `STAN\venv`, the updater should have migrated PATH entries — check that the old entry isn't shadowing the new one.

**"Community submission returns a 401 error"**

The submission goes through the HF Space relay — you don't need an HF token on the client side. A 401 typically means the relay's server-side token expired; this is an infrastructure issue, not something you need to fix. File an issue on GitHub and it will be resolved.

**"Files in F:\data\... aren't being picked up"**

Check `~/.stan/instruments.yml` for a `watch_dir` typo. On Windows, confirm the drive letter is correct and the path uses backslashes or forward slashes consistently. Also confirm the watcher process has read access to that path (run `stan watch` in the same user account that owns the data directory).

**"Sage returns very low PSM counts for DDA data"**

Confirm the FASTA is the community-standardized one (frozen path in `stan/search/community_params.py`). If you're running on Bruker `.d` files, Sage reads them natively — no mzML conversion needed. If you're on Thermo `.raw`, Sage converts via ThermoRawFileParser — confirm that binary is installed and on PATH. See [`docs/external_tools.md`](external_tools.md).

---

## Where to get help

- **GitHub issues:** [github.com/bsphinney/stan/issues](https://github.com/bsphinney/stan/issues) — bug reports, feature requests, questions
- **Community dashboard:** [huggingface.co/spaces/brettsp/stan](https://huggingface.co/spaces/brettsp/stan) — public benchmark and leaderboard
- **Source code:** [github.com/bsphinney/stan](https://github.com/bsphinney/stan)
