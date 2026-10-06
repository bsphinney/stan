<p align="center">
  <img src="stan/dashboard/public/icons/icon-512.png" alt="STAN" width="180" height="180">
</p>

# STAN — Standardized proteomic Throughput ANalyzer

> *Know your instrument.*

[![License: STAN Academic](https://img.shields.io/badge/License-Academic-blue.svg)](LICENSE)
[![Dataset: CC BY 4.0](https://img.shields.io/badge/Data_License-CC_BY_4.0-green.svg)](https://creativecommons.org/licenses/by/4.0/)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)

**Version**: see [CHANGELOG.md](CHANGELOG.md) for the current release (first public release: 1.0.0, 2026-05-30)  
**Author**: Brett Stanley Phinney, UC Davis Proteomics Core  
**License**: STAN Academic License (free for academic/non-profit use; see [LICENSE](LICENSE))

> 📖 **Installing STAN?** Start with [**`INSTALL_FOR_AGENTS.md`**](INSTALL_FOR_AGENTS.md). It is written for an AI coding agent, and a person can follow it too. For daily use, the dashboard tour and troubleshooting, see the [**User Guide**](docs/user_guide.md).

STAN watches the place where your instruments' finished raw files land, runs a standardized DIA-NN or Sage search on every HeLa QC injection, and scores the result against your instrument's historical cohort. It can also gate each run against pass/fail thresholds you write (none ship, so every run passes until you do). A web dashboard tracks everything. Community benchmark submission is opt-in.

---

## What it is

Proteomics core facilities need continuous, automated QC. The existing options are either vendor-locked (Bruker ProteoScape), expensive, or require bespoke scripting. STAN is an open-source alternative that runs on the lab's SLURM cluster or on a separate Linux box (running it on the instrument workstation itself is possible but not recommended; see the modes below). It reads Bruker `.d` directories and Thermo `.raw` files natively, and calls DIA-NN or Sage as subprocesses — no proprietary middleware.

The approach is watcher-based: a daemon monitors your acquisition directory, waits for each file to finish writing (vendor-specific stability detection), identifies DIA vs. DDA acquisition mode from the raw file itself, dispatches the appropriate search engine, extracts a fixed set of QC metrics, and gates the result against configurable thresholds. Everything lands in a local SQLite database and is visible on a single-page dashboard. On a SLURM cluster (Mode C), a login-node cron job does the watching instead and submits one SLURM job per raw file.

The community benchmark uses a frozen FASTA + spectral library so that precursor and PSM counts are comparable across labs. Submission is aggregate metrics only — no raw files, no sample metadata leave your building. A public leaderboard is hosted at [community.stan-proteomics.org](https://community.stan-proteomics.org), faceted by instrument family, gradient length (SPD), and injection amount.

---

## Quick install — pick your mode

> **Installing with an AI coding agent** (Claude Code, Cursor, Codex, Aider, or similar)?
> Point it at [`INSTALL_FOR_AGENTS.md`](INSTALL_FOR_AGENTS.md). That file:
>
> - helps the agent and you choose a mode;
> - lists the rules and version pins every install needs;
> - defines how to check that the install works;
> - links the step-by-step guide for each mode.
>
> If you are installing by hand, start there too.

| Mode | Where searches run | Use it when | Install guide |
|---|---|---|---|
| **C — SLURM cluster** (recommended) | On the lab's cluster. Instrument PCs only copy raw files. | You have a SLURM account, shared storage the instruments can reach, and someone to keep a cron job running. | [`docs/INSTALL_MODE_C_HPC.md`](docs/INSTALL_MODE_C_HPC.md) |
| **B — separate Linux box (native or WSL2)** | On one Linux machine that is not an acquisition PC. Instrument PCs only copy raw files. | You have no cluster, but you do have a spare Linux machine, or a Windows workstation that can run WSL2. | [`docs/INSTALL_MODE_B_LINUX.md`](docs/INSTALL_MODE_B_LINUX.md), plus [`docs/INSTALL_MODE_B_WSL.md`](docs/INSTALL_MODE_B_WSL.md) for WSL2 |
| **A — instrument PC (Windows)** ⚠️ not recommended | On the acquisition PC itself. | The instrument PC is the only machine you have. Read the warning below first. | [Mode A](#mode-a--instrument-pc-windows), below |

### Mode A — instrument PC (Windows)

> [!WARNING]
> **Not recommended.** Running searches on the acquisition PC has frozen
> UC Davis's timsTOF mid-run, and a stalled acquisition can cost you the run
> or the sample. STAN holds each DIA-NN and Sage search to
> `max(2, cores/2)` threads. The optional Bruker 4DFF feature finder, the
> per-run PEG/drift scan and the backfill commands are not capped, and each
> watch folder can run its own search at the same time. If you have a separate Linux box
> (**Mode B**) or a SLURM cluster (**Mode C**), use that instead. UC Davis
> runs Mode C, and its instrument PCs only acquire.

These steps are written for an AI agent working in Windows PowerShell 5.1
on the instrument PC. Run them as the instrument's normal Windows user, not
as Administrator, because STAN installs into that user's profile. Do the
steps in order, and run each **Check** before you move on. The reference
behind every step (config keys, sharing, load, network, troubleshooting) is
[`docs/INSTALL_REGRESSION_CHECKLIST.md`](docs/INSTALL_REGRESSION_CHECKLIST.md).

**Ask the human first.** Do not guess these answers:

| Ask | Why it matters |
|---|---|
| Which instruments (Bruker `.d` or Thermo `.raw`), and which folder does each one write to? | You need one `instruments.yml` block per folder. |
| Will this PC acquire while STAN runs? | If yes, follow the [load controls](docs/INSTALL_REGRESSION_CHECKLIST.md#6-load-on-an-acquiring-pc). |
| Do you accept the DIA-NN license? It is free for academic and non-profit use; commercial use needs a paid license. | DIA-NN runs every DIA search. |
| Should STAN share with the community benchmark or the PEG board, and may it send error reports? | Each option sends data off-site. Error reports are off unless `community.yml` sets `error_telemetry: true`. See [sharing](docs/INSTALL_REGRESSION_CHECKLIST.md#7-sharing-data-off-site). |

**0. Prepare the PowerShell session.** Run this in every new window. It
turns on TLS 1.2 so GitHub downloads work, and turns off the progress bar,
which makes large downloads very slow on PowerShell 5.1.

```powershell
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'
$S = "$env:USERPROFILE\STAN"    # holds config, database, logs, venv and tools
```

**1. Install Python 3.12.** Python 3.10, 3.11 and 3.12 all work. Do not use
3.13 or newer: the Bruker PEG extra needs `numpy<2`, which has no wheels for
3.13+. If `py -3.12 --version` already works, run
`$Py = py -3.12 -c "import sys; print(sys.executable)"` and skip the download.

```powershell
Invoke-WebRequest https://www.python.org/ftp/python/3.12.4/python-3.12.4-amd64.exe -OutFile "$env:TEMP\python-3.12.4-amd64.exe" -UseBasicParsing
Start-Process "$env:TEMP\python-3.12.4-amd64.exe" -ArgumentList '/passive', 'InstallAllUsers=0', 'PrependPath=0', 'Include_test=0' -Wait
$Py = "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
```

**Check:** `& $Py --version` prints `Python 3.12.x`.

**2. Install STAN into its own venv** with the extra that matches your
vendor:

| Extra | Use for | Adds |
|---|---|---|
| `peg` | Bruker | PEG and DIA-window drift (`alphatims<1.0.9`, `numpy<2`, `pandas<3`) |
| `thermo` | Thermo | PEG and a fast TIC (`fisher_py`, which needs .NET through pythonnet) |
| `full` | Both vendors | Both of the above |

```powershell
& $Py -m venv "$S\venv"
& "$S\venv\Scripts\python.exe" -m pip install --upgrade pip
& "$S\venv\Scripts\python.exe" -m pip install "stan-proteomics[peg] @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip"
$Stan = "$S\venv\Scripts\stan.exe"
$p = [Environment]::GetEnvironmentVariable('Path', 'User')
if (($p -split ';') -notcontains "$S\venv\Scripts") { [Environment]::SetEnvironmentVariable('Path', "$p;$S\venv\Scripts", 'User') }
```

This installs the current `main` branch. For pinning, see
[updating](docs/INSTALL_REGRESSION_CHECKLIST.md#11-updating-pinning-uninstalling).
The last two lines put `stan` on the user `PATH` for new windows.

**Check:** `& $Stan version` prints `STAN v<version>`, and the version matches
`version` in [`pyproject.toml`](pyproject.toml) on `main`. `& $Stan doctor`
should list alphatims 1.0.8, numpy 1.26.x and pandas 2.x (`peg`), or
fisher_py (`thermo`). There is no `stan --version`.

**3. Install the search engines at the pinned versions.** The community
benchmark accepts only DIA-NN 2.3.x, and runs made with any 2.3.x (2.3.0,
2.3.1 or 2.3.2) are asset-verified. The lines below install 2.3.0, the exact
pin; upstream labels its 2.3.0 Windows build "Preview". The 2.3.2 that the
one-click installer puts in is equally good for the benchmark: if it is
already here, you can skip the first two lines (the DIA-NN download and
install) and use its `DiaNN.exe` below. Sage is pinned at 0.14.7.

Install DIA-NN only after the human has accepted its license. The MSI may
show a UAC prompt, which a human must approve.

```powershell
Invoke-WebRequest https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.3.0-Academia-Preview.msi -OutFile "$env:TEMP\DIA-NN-2.3.0.msi" -UseBasicParsing
Start-Process msiexec.exe -ArgumentList '/i', "`"$env:TEMP\DIA-NN-2.3.0.msi`"", '/passive', '/norestart' -Wait
Get-ChildItem 'C:\DIA-NN', 'C:\Program Files\DIA-NN' -Recurse -Filter DiaNN.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName

Invoke-WebRequest https://github.com/lazear/sage/releases/download/v0.14.7/sage-v0.14.7-x86_64-pc-windows-msvc.zip -OutFile "$env:TEMP\sage-v0.14.7.zip" -UseBasicParsing
Expand-Archive "$env:TEMP\sage-v0.14.7.zip" -DestinationPath "$S\tools\sage" -Force
```

Put that DIA-NN 2.3.x folder first on the user `PATH`, ahead of any other DIA-NN
(the one-click installer, if it ran, put its own DIA-NN first). For DDA runs, community
submission reads the version of the first `diann` on `PATH`, not
`diann_path`.

```powershell
$Diann = '<the DiaNN.exe path printed above>'
$d = Split-Path $Diann
$p = [Environment]::GetEnvironmentVariable('Path', 'User')
$rest = @(($p -split ';') | Where-Object { $_ -and ($_ -ne $d) })
[Environment]::SetEnvironmentVariable('Path', ((@($d) + $rest) -join ';'), 'User')
```

**Check:**

- `& $Diann` (no arguments) prints a header containing `DIA-NN 2.3.0`
  (`DIA-NN 2.3.2` if you kept the one-click installer's).
- In a new PowerShell window, the first line of `where.exe diann` is that
  same `DiaNN.exe`.
- `& "$S\tools\sage\sage-v0.14.7-x86_64-pc-windows-msvc\sage.exe" --version`
  prints `sage 0.14.6`. The v0.14.7 release binary still carries the old
  version string.
- **Thermo only.** ThermoRawFileParser 1.4.5 downloads itself on the first
  `.raw`, and STAN uses it to tell DIA from DDA on every `.raw`. Fetch it now
  so that a download failure shows up here:
  `& "$S\venv\Scripts\python.exe" -c "from stan.tools.trfp import ensure_installed; print(ensure_installed())"`
  prints a path that ends in `ThermoRawFileParser.exe`.

**4. Download the spectral library and FASTA.** Nothing downloads a library
for the watcher, so without these files every DIA search fails. Put them
where `stan baseline` also looks:

```powershell
New-Item -ItemType Directory -Force "$S\community_assets" | Out-Null
foreach ($f in 'human_hela_202604.fasta', 'hela_timstof_202604.parquet') {   # Thermo: hela_orbitrap_202604.parquet
    Invoke-WebRequest "https://github.com/bsphinney/stan/releases/download/v0.1.0-assets/$f" -OutFile "$S\community_assets\$f" -UseBasicParsing
}
Get-FileHash "$S\community_assets\*" -Algorithm MD5
```

**Check:** the MD5 of each file matches. `Get-FileHash` prints them in upper
case.

| File | MD5 |
|---|---|
| FASTA | `8de1d9bd0a052b175f88f66f82500d92` |
| timsTOF library | `ad72bfb2730644c69147ba8f34bfe982` |
| Orbitrap library | `ac84e40f5b2f23e1286f28a7baeccec2` |

**5. Write the config.** On Windows the live config is
`%USERPROFILE%\STAN\instruments.yml`. Write one block per watch folder.
Save the file as ASCII or as UTF-8 without a BOM: Python on Windows reads it
with the system code page, so a BOM or UTF-16 file (the PowerShell 5.1
`Out-File` default) will not parse. From PowerShell, use
`Set-Content -Encoding ASCII`. Put single quotes around Windows paths.
Do not run the `stan setup` wizard for the human: it asks about community
sharing, email and error reports, which are theirs to answer. `stan init` is
not needed. It only creates config files that are missing, and asks one
fleet-sync question whose default, `3` (None), is right for every lab
outside UC Davis.

```yaml
instruments:
- name: timsTOF HT             # stable label; runs are stored under it
  vendor: bruker               # bruker | thermo
  watch_dir: 'D:\Data'         # watched recursively
  extensions: ['.d']           # ['.raw'] for Thermo
  stable_secs: 60              # 30 for Thermo
  enabled: true                # without this the watcher skips the block
  qc_only: true                # search only names that match the HeLa/QC pattern
  output_dir: 'C:\Users\<user>\STAN\qc_output\timsTOF_HT'   # one folder per instrument
  lib_path: 'C:\Users\<user>\STAN\community_assets\hela_timstof_202604.parquet'
  fasta_path: 'C:\Users\<user>\STAN\community_assets\human_hela_202604.fasta'
  diann_path: '<the DiaNN.exe path step 3 printed>'
  sage_path: 'C:\Users\<user>\STAN\tools\sage\sage-v0.14.7-x86_64-pc-windows-msvc\sage.exe'
  startup_catchup_days: 0      # default 30: the first start searches 30 days of old QC files
  hela_amount_ng: 50           # your usual amount; a file name that states one (50ng, 1ug) wins
  # lc_flow: nano              # non-Evosep LC only: nano | capillary | micro (stan add-watch --lc-flow)
```

`stan add-watch <dir> --vendor bruker --name "<label>" -y` writes the keys
from `name` to `output_dir`, plus `lc_flow` with `--lc-flow nano|capillary|micro`
(its `output_dir` is
`%USERPROFILE%\STAN\qc_output\<label>`, with spaces turned into `_`). Add
every key from `lib_path` down by hand. If the one-click installer ran, its
`%USERPROFILE%\.stan\instruments.yml` is the only one; the first `add-watch`
copies it into `$S\instruments.yml` and writes there from then on.
Error reports are off unless `community.yml` sets `error_telemetry: true`.
Only if the human agreed to them, also create `$S\community.yml` containing
that line. Every key is
explained in [instruments.yml keys](docs/INSTALL_REGRESSION_CHECKLIST.md#3-instrumentsyml-keys).

**Check:** `& $Stan list-watch` shows each folder with a tick under
*Exists* and *Enabled*, and its extension under *Extensions*. Then run:

```powershell
& "$S\venv\Scripts\python.exe" -c "from stan.config import load_instruments; [print(i.get('name'), i.get('enabled'), i.get('vendor'), i.get('extensions'), i.get('output_dir')) for i in load_instruments()[1]]"
```

It prints `True`, a vendor, an extension list and an output folder for
every block.

**6. Start STAN.** `stan.bat` is the supervisor. It starts the dashboard,
runs `stan watch`, and restarts the watcher 5 s after it exits. On every
launch it also upgrades STAN from `main`: extras you installed stay
installed, and DIA-NN and Sage are never touched.

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/bsphinney/stan/main/stan.bat -OutFile "$S\stan.bat" -UseBasicParsing
Start-Process "$S\stan.bat" -WorkingDirectory $S
```

**Check:** wait 1–2 minutes for the launch-time upgrade to finish. Then:

- `Invoke-RestMethod http://127.0.0.1:8421/api/version` returns the version.
- `Get-ChildItem "$S\logs\watch_[0-9]*.log" | Sort-Object LastWriteTime | Select-Object -Last 1 | Select-String 'Active watchers'`
  shows `Active watchers: N`, where N is your number of blocks. `0` means a
  block is missing `enabled: true`.

Tell the human to open <http://localhost:8421> in Chrome or Edge, not
Internet Explorer. To start STAN at logon, put a shortcut to `stan.bat` in
`shell:startup`.

**7. Confirm the first QC run.** Wait for the next QC injection, or copy a
recent QC `.raw` into the watch folder. STAN searches only file names that
match the QC pattern (for example `HeLa` or `QC`; change it with
`qc_pattern`). On Bruker, wait for a real acquisition. A `.d` that is
already complete when it appears (a fast copy or a move) can wait forever on
the live watcher.

**Check:** `& $Stan watch-status --days 1` shows the file as matched and in
`runs`, and the run appears on the dashboard. `& $Stan test --n 1` lists
which fields were filled. The DIA-NN log is
`<output_dir>\<run name>\diann.log`.

**8. Optional steps. Do these only after the human says yes:**

- [community benchmark and PEG-board sharing](docs/INSTALL_REGRESSION_CHECKLIST.md#7-sharing-data-off-site)
- [QC gating thresholds](docs/INSTALL_REGRESSION_CHECKLIST.md#8-qc-gating-thresholdsyml). None ship, so every run is gated PASS.
- The Bruker 4DFF ion cloud (`stan install-4dff`). Do not install it on a PC
  that acquires: see [load](docs/INSTALL_REGRESSION_CHECKLIST.md#6-load-on-an-acquiring-pc).

#### One-click install (for a person at the keyboard)

Save [`stan.bat`](https://raw.githubusercontent.com/bsphinney/stan/main/stan.bat)
and double-click it. On a PC with no STAN it looks for
[`install-stan.bat`](https://raw.githubusercontent.com/bsphinney/stan/main/install-stan.bat)
next to itself and then in `Downloads`, and if neither has it, downloads it
from GitHub next to itself. The installer then:

1. asks the operator to type `yes` to accept the DIA-NN license;
2. uses any Python 3.10 or newer it finds (including 3.13), or installs 3.12.4;
3. pip-installs `main` into `%USERPROFILE%\STAN\venv` with **no extras**;
4. keeps a DIA-NN 2.3.x already installed (2.3.0 if there is one, else the
   newest), or else installs DIA-NN 2.3.2, refusing the download unless its
   sha256 matches a pinned value. The benchmark accepts any 2.3.x and marks
   2.3.2 rows asset-verified just like 2.3.0 rows. It puts that DIA-NN's
   folder first on the user `PATH`;
5. installs Sage v0.14.7 the same way (sha256-checked) into
   `%USERPROFILE%\STAN\tools\sage\`, unless that exact binary is already on
   `PATH` or there, and puts it first on the user `PATH`;
6. offers `stan setup`.

A one-click install is not finished. Afterwards, do steps 2 (add the extra),
3 (its checks; installing DIA-NN 2.3.0 is optional, because the installer's
2.3.x is equally good for the benchmark and its Sage is already the pinned
one), 4 and 5 above. The full list of what the scripts do, and
their known defects, is in
[what the Windows scripts do](docs/INSTALL_REGRESSION_CHECKLIST.md#1-what-the-windows-scripts-do).
Never run `update-stan.bat` on a PC that acquires, or while `stan.bat` is
running. It kills every `stan.exe`, relaunches the watcher and dashboard, and
starts a multi-hour backfill chain.

### Mode A — not on Windows

Instrument PCs run Windows, so Mode A is Windows-only. A Linux machine that
processes raws is [Mode B](docs/INSTALL_MODE_B_LINUX.md). A Mac cannot process
raws: DIA-NN has no macOS build, and ThermoRawFileParser refuses to run on
macOS. A Mac can still run `stan dashboard`. To develop STAN, use
`pip install -e ".[dev,full]"` in a clone, on Python 3.10–3.12. `dev`
installs only the test tooling (pytest, ruff, mypy), not the runtime extras.

---

## The dashboard

Open `http://localhost:8421` after `stan dashboard`. The main tabs:

- **This Week's QCs** — gauge, weekly table, or metric-matrix view of recent HeLa runs. IPS badge front and center.
- **QC History** — every run, sortable and filterable. Click a row for the full modal: metric breakdown, gate verdicts, PEG lollipop chart, diaPASEF drift cloud (Bruker), 4DFF Ion Cloud (Bruker, optional).
- **Trends** — longitudinal sparklines for IPS, precursor/PSM count, peptide count, iRT deviation, TIC area, column age. Maintenance events render as vertical markers.
- **PEG** (PEG Watch) — each instrument's PEG history: timeline with auto-detected contamination episodes, a daily calendar, the oligomer-ladder fingerprint, PEG per LC-column period, what PEG costs in precursors, the community PEG leaderboard, and an Evosep vs other LC comparison. Deep link `?tab=peg`. See [`docs/PEG_WATCH.md`](docs/PEG_WATCH.md).
- **Sample Health** — non-QC Bruker `.d` acquisitions monitored for TIC dropout and injection failures.
- **Fleet** — all instruments on the shared drive in one view; send remote commands.
- **Config** — live view of `instruments.yml` and `thresholds.yml`.
- **Community** — your benchmark standing within your cohort, submission log, TIC overlay vs. community runs, and a **Sync** button that pushes eligible runs to the public benchmark (pseudonym generated on first use; the count shown is what would actually be sent).
- **Arcade** — retro mini-games with global community leaderboard (opt-in).
- **Museum** — interactive historical QC archive: 999 BSA injections from 2005–2022 across every instrument era the UC Davis Proteomics Core has operated, searched with Sage v0.14.7. Timeline, trend chart, coverage maps, and "Then vs Now" comparison panel. See `docs/MUSEUM_DEPLOY.md` to deploy the standalone page to the community HF Space.

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
| **PEG share of MS1** + PEG class | Bruker + Thermo | MS1 scan for the polyethylene-glycol ladder. The share is compared only within one instrument family. Thermo on Hive reads through the ThermoRawFileParser container. |
| **diaPASEF window drift** | Bruker | Detects MS2 windows walking off their 1/K0 calibration. |

Full definitions, reference ranges, and formulas: [`docs/user_guide.md`](docs/user_guide.md) and [`docs/ips_metric.md`](docs/ips_metric.md).

---

## Community benchmark

| | |
|---|---|
| Public dashboard | [community.stan-proteomics.org](https://community.stan-proteomics.org) · [HF Space](https://huggingface.co/spaces/brettsp/stan) |
| Public dataset | [huggingface.co/datasets/brettsp/stan-benchmark](https://huggingface.co/datasets/brettsp/stan-benchmark) · CC BY 4.0 |

To take part: put a pseudonym in `display_name` in `community.yml` and run `stan community-claim` (it emails a 6-digit code and stores an auth token), set `community_submit: true`, then run `stan submit-all` (or press **Sync** on the dashboard's Community tab). The watcher never submits by itself, so schedule `stan submit-all` if you want regular submissions. Submissions go through the HF Space relay — no HF token required on your end. Each install guide has the exact steps.

Three tracks: Track A (DDA, PSM primary), Track B (DIA, precursor primary), Track C (both within 24 h from the same instrument — unlocks a six-axis radar fingerprint).

**Evosep PEG Watch** is a separate, opt-in channel: `peg_share: true` in `community.yml`, then `stan peg-sync` sends per-run PEG (never file names) to the relay's PEG board, ranked within instrument family × Evosep method. It needs no community search. See [`docs/PEG_WATCH.md`](docs/PEG_WATCH.md).

---

## Implementation Status

What ships today vs. what's still planned.

| Component | Status | Notes |
|---|---|---|
| CLI (57 commands) | Done | Full list in `docs/user_guide.md`. |
| Watcher daemon | Done | File-stability detection, hot-reloaded config, recursive monitoring, startup catch-up sweep. Each start writes a log to `<config dir>/logs/watch_<ts>.log`. |
| Acquisition mode detection | Done | Bruker via `analysis.tdf.Frames.MsmsType`; Thermo via ThermoRawFileParser metadata + filename token fallback. |
| Local DIA-NN execution | Done | Default. Subprocess on the machine that runs `stan watch` (Mode B box, or the instrument PC in Mode A), community-standard params. |
| Local Sage execution | Done | Default. Bruker `.d` native, Thermo `.raw` via ThermoRawFileParser → mzML. |
| SLURM HPC execution (optional) | Done | Mode C: a login-node cron runs `stan hive-dispatch`, which submits one `stan hive-process` job per raw file. See `docs/INSTALL_MODE_C_HPC.md`. The older per-instrument `execution_mode: slurm` path (the watcher submits over SSH with `paramiko`, the `[hpc]` extra) still exists but is not for new installs. |
| Metric extraction (DIA + DDA) | Done | Polars-based, from `report.parquet` and `results.sage.parquet`. |
| IPS scoring | Done | 3-component depth composite (precursors / peptides / proteins), 0–100, percentile-mapped against an `(instrument family, SPD bucket)` cohort. See `docs/ips_metric.md`. |
| QC gating + HOLD flag | Done | Hard gates with plain-English diagnosis. |
| Column health | Done | TIC AUC + peak RT trend analysis. |
| SQLite database | Done | All metrics, gate results, sample-health verdicts, maintenance events, PEG/drift breakdowns, 4DFF features-by-charge (`feature_clouds`). |
| PostgreSQL / PG Farm backend | Done | Optional central source-of-truth for Hive bulk + fleet dashboards. `STAN_DB_BACKEND=pg`; single-lab installs stay on SQLite. See `docs/PG_FARM.md`. |
| Egress-aware PG readers (v1.1.8) | Done | PG Farm bills every byte read out of it. The dashboard mirror is xmin-fingerprinted, so a quiet refresh costs ~3 KB instead of 73 MB. The Hive crons ask PG only about what is new. Together they cut ~20 GB/day to well under 1 GB/day. See `docs/PG_FARM.md` → "Egress is billed". |
| Parallel ingest sharding | Done | `stan ingest-orphans --shard N/M` for SLURM-array recovery of orphaned parquets. |
| FastAPI dashboard backend | Done | All routes wired (runs, trends, instruments, thresholds, fleet, community, PEG, drift, 4DFF, sample-health, hide). Swagger at `/docs`. |
| Single-file React dashboard | Done | `stan/dashboard/public/index.html`, React + Babel via CDN. Tabs listed under "The dashboard" above. |
| Historical QC Museum | Done | `stan/dashboard/public/museum.html` — 999 BSA injections 2005–2022, Sage-searched; timeline, trend chart (log-scale), BSA coverage maps, Then vs Now table. Deploy guide: `docs/MUSEUM_DEPLOY.md`. |
| Setup wizard | Done | 6 questions, dedupes `instruments.yml`, offers baseline at the end. |
| Baseline builder | Done | Recursive discovery, auto-detect gradient/LC, pre-flight DIA-NN/Sage tests, resume on interrupt, scheduling (now / tonight / weekend). |
| Windows installer + updater | Done | `stan.bat` — single entry point: installs on first run, self-updates on every run, supervises the watcher. `install-stan.bat` handles the one-time install step internally. |
| timsTOF → Flinders raw copier | Done | `scripts/install_flinders_copy.bat` — scheduled task on the instrument PC, every 5 min. Pure PowerShell + `robocopy`; no Python, no STAN install, nothing resident between passes. Mirrors the local layout: timsControl acquires into `D:\Data\Aug26\`, so the run lands in `tTOF_HT\Aug26\`, reusing an existing folder that means that month if it is spelled differently (`july26` → `JUL26`). A run is finished when its file-count + byte total is unchanged across two passes — a `.d` is a directory, so its mtime does not move when a file inside it grows. Copy-only; the source is never touched. |
| Community submission | Done | Hard gates, soft flags, asset MD5 verification, no HF token needed (relay). |
| Community sync button | Done | Dashboard Community tab; mints a pseudonym if the install has none. Refused on the public read-only host. |
| Community sync cron | Done | Hive, every 6 h (`scripts/cron_community_sync.sh`). Idempotent via `submitted_to_benchmark`. |
| Community auth token | Done | `stan community-claim` (or `stan setup`) claims a pseudonym via an emailed code; relay enforces `X-STAN-Auth` on PATCH, and from relay 1.9.0 / STAN 1.2.18 checks it on every submission (`name_verified`; a claimed name with the wrong token is refused, no token still accepted unverified); a name that differs from another email's claimed name only in case or spacing cannot be claimed. |
| Community FASTA | Done | UniProt human + universal contaminants, MD5-verified, auto-downloaded on first need. |
| Community speclibs | Done | HeLa empirical libraries for timsTOF (`hela_timstof_202604.parquet`) and Orbitrap/Astral (`hela_orbitrap_202604.parquet`) on HF Dataset; MD5-verified at submission time. |
| Cohort scoring + percentiles | Done | Computed nightly within `(family, SPD, amount)` cohorts. |
| HF Space community dashboard | Done | Live at `community.stan-proteomics.org`. |
| Arcade → shared leaderboard | Done | Game over prompts for an optional name + affiliation and posts to `POST /api/arcade/score`; scores live in the PG Farm `arcade_scores` table (shared by every install) or local SQLite when there is no PG. The public dashboard reads the board and refuses writes (`STAN_DASHBOARD_READONLY`). The HF Space relay endpoints are still undeployed and are only a read fallback. |
| Bruker `.d` XML method-tree parser | Done | Reads `<N>.m/submethods.xml`, `hystar.method`, `SampleInfo.xml` for authoritative SPD + Evosep detection. |
| `validate_spd_from_metadata()` | Done | XML → MethodName → `Frames.Time` span fallback chain. |
| SPD on non-QC acquisitions | Done | `sample_health.spd`, resolved per-file at ingest (metadata, then filename token — never the cohort default). Backfill with `stan fix-sample-spds`. Lets the dashboard's TIC overlay filter Sample and Blank traces by gradient instead of dropping them. |
| Per-instrument utilisation capacity | Done | Utilisation is scored against each instrument's two most-used gradients (`spd_usage_by_instrument()`), not a fixed Evosep 100/60 pair. |
| `detect_lc_system()` | Done | Evosep vs custom from `.d` method tree + TrayType; powers the LC filter on the community TIC overlay. |
| Cohort attributes per run (v1.2.16, P3a) | Done | `lc_model` (`detect_lc_model`: Thermo DriverIds, Bruker HyStar method), `lc_flow` (`stan setup` / `add-watch --lc-flow` / dispatch.yml), `amount_source` (declared / parsed / assumed, `stan/community/amount.py`) and `faims` (`cv=` in Thermo scan filters) stamped at ingest and sent to the relay (1.7.0). Amount conflicts and amounts above 5,000 ng are held back from submission. PG gets the columns by `migrations/2026-10-05_runs_lc_faims.sql` (owner, pending); until then the PG writer skips them. Hive has no `fisher_py`, so Hive Thermo runs record FAIMS as unknown. |
| Labs counted as facilities (v1.2.18, relay 1.9.0, P3c) | Done | Admin-written `identity/facilities.json` (`scripts/set_facilities.py`, `scripts/community_facilities.json`) maps an opaque id (`f1` = UC Davis) to a facility's lab names and its 'Anonymous Lab' window; `/api/leaderboard` publishes `facility`, and every lab count on the community page (and the TIC summaries) counts facilities. |
| Read-time amount and FAIMS check (v1.2.17, relay 1.8.0, P3b) | Done | The relay works out `amount_check`, `faims` and `faims_source` from the private file name (never served). Runs whose file name contradicts the stored amount are held back from every range and ranking and listed under the submissions table; FAIMS runs form their own cohorts ("· FAIMS"), with no filter. Stored data unchanged (P4 corrects it). |
| Real acquisition-date preservation | Done | Bruker `analysis.tdf.AcquisitionDateTime` / Thermo `fisher_py` CreationDate, not insertion time. |
| DIA-NN filename `--` sanitizer | Done | Junction/symlink workaround for the DIA-NN argv-parsing bug. |
| Today TIC overlay | Done | `/api/today/tic-overview` powers the at-a-glance pump-and-spray view. |
| PEG contamination panel | Done | `stan backfill-peg`, scoring, lollipop chart in the run modal. |
| PEG Watch tab (v1.2.0) | Done | `GET /api/peg/overview`: timeline + 14-day median, contamination episodes, best 90-day baseline, daily calendar, ladder fingerprint, PEG by column period, precursor cost, Evosep vs other LC. One row per acquisition (duplicate ingests collapsed); failed acquisitions and the `unknown` sentinel never count as clean. See `docs/PEG_WATCH.md`. |
| Community PEG board (v1.2.0) | Done | `stan peg-sync` (opt-in `peg_share`) → relay `POST /api/peg/submit` → `peg/peg_latest.parquet`; `GET /api/peg/leaderboard`, `/trend`, `/lc-compare`. Evosep-only ranking within family × SPD; claimed names need their token (`stan community-claim`). Runs on the Hive community-sync cron. |
| Thermo PEG on Hive (v1.2.0) | Done | No `fisher_py` in the Hive venv, so `.raw` goes through the ThermoRawFileParser container (`stan/metrics/peg_trfp.py`), inside SLURM only. `scripts/peg_backfill_thermo.sbatch` backfills the historical Orbitrap runs. |
| Relay source in the repo (v1.2.0) | Done | `hf_space/app.py` is the canonical HF Space source; deploy only with `scripts/deploy_hf_space.py`, which refuses to overwrite edits made in the Space. |
| diaPASEF window drift | Done | `stan backfill-window-drift`, drift cloud scatter in the run modal. |
| 4DFF Ion Cloud | Done | `stan install-4dff`, `run-4dff`, `backfill-features`, `backfill-feature-cloud`. Plotly per-charge view, SVG fallback. Clouds are stored in `feature_clouds` and served from the DB, so the view no longer needs the raw `.d` mounted on the dashboard host. |
| cIRT panel + trends | Done | `stan backfill-cirt`, `derive-cirt-panel`, Trends tab visualisation. |
| Maintenance log UI | Done | Trends-tab form. Events render as vertical markers on every trend chart. |
| Hide / restore a run | Done | `POST /api/runs/{id}/hide`. UI button on the QC History row. |
| Sample Health (rawmeat) | Done | Bruker `.d` and Thermo `.raw` non-QC files monitored; verdict (pass/warn/fail) stored in `sample_health` table. |
| Fleet sync (SMB / HF Space / none) | Done | `~/.stan/fleet.yml`, configured by `stan/fleet_setup.py`. |
| STAN Godmode (multi-instrument view) | Done | `STAN_DB_PATH=<global stan.db> stan dashboard` serves a fleet-wide view across instruments (honored in `stan/db.py` + `stan/dashboard/server.py`); pairs with Tailscale for remote phone access. See `docs/user_guide.md` → "STAN Godmode". |
| Fleet command queue | Done | 12 whitelisted actions (`ping`, `status`, `tail_log`, `export_db_snapshot`, `watcher_debug`, `qc_filter_report`, `apply_config`, `update_stan`, `restart_watcher`, `cleanup_excluded`, `fix_instrument_names`, `v1_prep`). |
| Email reports | Done | Daily 07:00 + optional Monday weekly, via Resend with the lab's own key (`resend_api_key` in community.yml or `RESEND_API_KEY`; since 1.2.7). |
| Slack alerts | Done | Webhook in `community.yml`. `stan test-alert` to verify. |
| Error telemetry (opt-in) | Done | Off unless `error_telemetry: true` is set in `community.yml` (off by default since v1.2.6). A report carries the error message, which can include file paths, a traceback stripped to file names, the raw file's name and the STAN, Python and OS versions. Local log at `~/.stan/error_log.json` either way. |
| Front-page view selector | Done | Gauges / Weekly table / Metric matrix on This Week's QCs. |
| Test fixtures (real DIA-NN / Sage output) | Planned | `tests/fixtures/` is mostly empty. |
| Outlier detection (amount / SPD mismatch) | Planned | Flag submissions whose metrics don't match the declared cohort. |
| Maintenance & downtime log | Done | Calendar view; downtime recorded as a span. Hosted writes need UC Davis sign-in and record who logged it and when. |
| Community downtime / reliability leaderboard | Groundwork | Schema carries downtime spans + a per-entry `share_community` opt-in. MTBF / availability / recovery-time and the relay side are still to build. |
| PyPI release | Planned | `pip install stan-proteomics` not yet published. |
| Auto-start `stan watch` as a Windows service | Planned | `stan.bat` supervises the watcher when running, but still requires the operator to double-click it. A Windows Scheduled Task (`stan install-service`) that starts at login/boot without any manual step is not yet shipped. |
| Mobile PWA (install) | Partial | Installable PWA shipped — `manifest.json` + icons + iOS "Add to Home Screen" meta (full-screen, app icon). Service worker (offline) + push-on-FAIL not yet shipped. Setup in `docs/user_guide.md`. |

---

## Roadmap / TODO

The shortlist of things actively being worked on or queued. (Bug fixes and shipped features have been moved out of this list — see Implementation Status above.)

**High priority**

- [ ] **(UC Davis) Investigate QC ingest blackout on timsTOF HT since 2026-04-17.** Watcher matches the QC filter but doesn't write rows into `runs`. Likely a downstream search-dispatch bug. Evidence is on UC Davis's share, `/Volumes/proteomics-grp/STAN/TIMS-10878/failures/`.
- [ ] **(UC Davis) Decouple community submission from the maintainer's Mac.** Hive is firewalled from outbound internet to the HF Space (`*.hf.space` → HTTP 000), so `stan submit-all --backend pg` can only push from an internet-connected box — currently the maintainer's Mac, a single point of failure. PG Farm itself is reachable from both Hive and the Space. Preferred fix: have the HF Space's nightly consolidation job **pull from PG Farm directly** (the Space has internet; `pgfarm.library.ucdavis.edu` is reachable) instead of being pushed to. Alternative: ask HPCCF to allowlist `*.hf.space` egress on Hive so submit-all runs there (the UC Davis token is already on Hive's shared storage). High priority for 1.1 — the Mac shouldn't be load-bearing.
- [ ] **`backfill-tic --push` HF error capture.** Push-side relay errors aren't logged. Add a `push_errors` section to the summary log with response codes and bodies.
- [ ] **Normalize `runs.instrument` + `sample_health.instrument`.** Some hosts split into two cards (`timsTOF HT` + `data_bruker`) because old rows hold the model name from metadata while newer rows use `name:` from `instruments.yml`. One-time migration that maps config name → model derived from the raw file.
- [ ] **Drift trend lines on the Trends tab.** We already store the per-run scalars and breakdowns. Add sparklines (drift_median_im, drift_coverage) so slow weeks-long drifts are visible. (PEG trends shipped as the PEG tab in v1.2.0.)
- [ ] **PEG Watch follow-ups.** PEG on blanks / `sample_health`; persist `ladder_coherence` and `lc_model` (owner DDL); delete the duplicate `runs` rows in PG; guard `stan backfill-peg` against empty reads. List in `docs/PEG_WATCH.md` → "Known limitations".
- [ ] **Rolling 3-month IPS baselines.** Recompute `IPS_REFERENCES` quarterly from each instrument's own history per SPD bucket. Decouples short-term variance from long-term drift. New `ips_baselines` table; `stan recalibrate-ips`; auto-monthly from the watcher.
- [ ] **Auto-start `stan watch`.** New `stan install-service` CLI registers a Windows Scheduled Task with "At user logon" + "At system startup" triggers and "Restart on failure". `install-stan.bat` calls it; `update_stan.ps1` cycles it on update so post-update watch is never forgotten.
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
- [ ] **PWA service worker + push.** The installable PWA (manifest + icons + Add to Home Screen) shipped; still needed: a service worker for offline caching and push notifications on FAIL.
- [ ] **Lumos / Exploris Thermo TIC backfill** via Hive-side `report.parquet` identified-TIC path.
- [ ] **Thermo `.raw` `fisher_py`-based SPD extraction** from the InstrumentMethod header.
- [ ] **Generate + upload Astral and timsTOF HeLa speclibs** to the HF Dataset.
- [ ] **Outlier detection** for community submissions: flag runs where metrics are inconsistent with the declared amount/SPD.
- [ ] **PyPI release.**
- [ ] **End-to-end watcher integration test** with real instrument data.
- [ ] **Community dashboard figures**: SPD vs. points-across-peak, faceted by LC column model.
- [ ] **TIC filter by pseudonym** (your traces vs community vs all). Color by lab when showing all traces.
- [ ] **Migration-keyed `backfill-tic` sentinel** instead of version-keyed (so trivial bumps don't re-force the whole sweep).
- [ ] **Install wizard for shared-drive selection.** First-run prompt for the fleet root (SMB path / HF Space URL / none). Today: `stan init` runs `stan/fleet_setup.py` (default: none) and saves `fleet.yml`, but nothing reads that file yet; the mirror path still comes from `HIVE_MIRROR_DIR` or `hive_mirror_dir` in `community.yml`.
- [ ] **Community downtime / reliability leaderboard** — heartbeat-gap detection, MTBF, recovery time, availability normalized by `institution_type`.
  - Groundwork landed v1.0.34: `maintenance_events` records downtime as a
    span (`event_date`..`end_date`, `event_type='downtime'`), plus
    `created_by`/`created_at` for attribution and a per-entry
    `share_community` flag. Sharing is **opt-in per entry and off by
    default** because maintenance notes can name people and customers.
  - Still to build: the relay endpoint that accepts shared entries, the
    MTBF/availability/recovery-time maths, and automatic heartbeat-gap
    detection to complement manually-marked downtime.

---

## Documentation index

| Doc | Contents |
|---|---|
| [`STAN_MASTER_SPEC.md`](STAN_MASTER_SPEC.md) | Authoritative design doc. Read before changing core behavior. |
| [`INSTALL_FOR_AGENTS.md`](INSTALL_FOR_AGENTS.md) | **Start here to install.** Written for an AI agent: choosing a mode, the rules and version pins every install needs, success checks, what to report. |
| [`docs/INSTALL_MODE_C_HPC.md`](docs/INSTALL_MODE_C_HPC.md) | Mode C — SLURM cluster install (recommended), including the site patch and hardened cron jobs. |
| [`docs/INSTALL_MODE_B_LINUX.md`](docs/INSTALL_MODE_B_LINUX.md) | Mode B — separate Linux box install: search engines, raw-file delivery, config, systemd services, end-to-end test. |
| [`docs/INSTALL_MODE_B_WSL.md`](docs/INSTALL_MODE_B_WSL.md) | Mode B on a Windows workstation through WSL2: only what differs from the Linux guide. |
| [`docs/user_guide.md`](docs/user_guide.md) | Day-to-day manual: all CLI commands, dashboard tour, config reference, troubleshooting. |
| [`docs/ips_metric.md`](docs/ips_metric.md) | IPS formula, cohort references, why protein count is excluded. |
| [`docs/qc_gating_and_slack_summary.md`](docs/qc_gating_and_slack_summary.md) | The per-run Slack QC summary, the IPS colour bands, and why `stan/gating/` is inert. |
| [`docs/external_tools.md`](docs/external_tools.md) | DIA-NN, Sage, ThermoRawFileParser: CLI flags, version pins, container paths, gotchas. |
| [`docs/HPC_PATHS.md`](docs/HPC_PATHS.md) | UC Davis Hive, the Mode C reference deployment: paths, accounts, containers, crontab. For comparison only; never copy into another lab's config. |
| [`docs/PG_FARM.md`](docs/PG_FARM.md) | UC Davis only. PG Farm Postgres backend: connection, schema, `STAN_DB_BACKEND=pg`, sync, token rotation. Other labs stay on SQLite. |
| [`docs/PEG_WATCH.md`](docs/PEG_WATCH.md) | PEG tab + community PEG board: the metric, ranking rules, duplicate rule, relay API, identity, privacy, deploy and backfill runbooks. |
| [`docs/GOTCHAS_DELIMP.md`](docs/GOTCHAS_DELIMP.md) | 50+ hard-learned lessons: DIA-NN edge cases, SLURM quirks, raw-file parsing traps. |
| [`docs/INSTALL_REGRESSION_CHECKLIST.md`](docs/INSTALL_REGRESSION_CHECKLIST.md) | Mode A (instrument PC) reference: what the Windows scripts really do, every config key, sharing, load, 10 post-install checks, troubleshooting, known defects. |
| [`CLAUDE.md`](CLAUDE.md) | Development context for AI agents working on this codebase. It describes UC Davis's own deployment, so it is not install guidance for other labs: use `INSTALL_FOR_AGENTS.md`. |

---

## Search engines

STAN does not bundle DIA-NN or Sage. Each is called as a subprocess and must be installed separately, at the pinned versions: DIA-NN 2.3.0 and Sage v0.14.7 (see [`INSTALL_FOR_AGENTS.md`](INSTALL_FOR_AGENTS.md#23-version-pins)). The community benchmark rejects any DIA-NN other than 2.3.x. The Windows one-click installer and `update-stan.bat` keep an installed DIA-NN 2.3.x or install DIA-NN 2.3.2, and install Sage v0.14.7, each checked against a pinned sha256. Upstream ships 2.3.0 for Windows only as a "Preview" MSI. Rows from any 2.3.x are asset-verified, so the installers' 2.3.2 is as good for the benchmark as the 2.3.0 that the install guides install by hand. ThermoRawFileParser is downloaded by STAN on first use.

| Tool | Used for | License |
|---|---|---|
| [DIA-NN](https://github.com/vdemichev/DiaNN) | All DIA searches (Bruker `.d` and Thermo `.raw` natively, no conversion) | Free for academic research; commercial use requires a paid license from Aptila Biotech or Thermo. |
| [Sage](https://github.com/lazear/sage) | All DDA searches. Bruker `.d` native. Thermo `.raw` requires mzML conversion first. | MIT |
| [ThermoRawFileParser](https://github.com/compomics/ThermoRawFileParser) | Every Thermo `.raw`: DIA vs DDA detection and metadata. Thermo DDA also: `.raw` → indexed mzML for Sage. Auto-downloaded on first use (1.4.5 on Windows, the .NET 8 build on Linux, which needs `dotnet` 8); cached in the config dir's `tools/` folder (`~/.stan/tools/` on Linux, `%USERPROFILE%\STAN\tools\` on Windows). | Apache 2.0 |

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
