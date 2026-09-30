<p align="center">
  <img src="../stan/dashboard/public/icons/icon-512.png" alt="STAN" width="160" height="160">
</p>

# STAN User Guide

> *Know your instrument.*

This is the day-to-day manual for STAN — the Standardized proteomic Throughput ANalyzer. It assumes STAN is already installed and `stan` is on your PATH. If you haven't installed it yet, start with [`INSTALL_FOR_AGENTS.md`](../INSTALL_FOR_AGENTS.md). It is written for an AI coding agent, and a person can follow it too. It helps you choose one of the [three deployment modes](#three-deployment-modes) and links the step-by-step guide for that mode. Where this guide and an install guide differ on an install step, follow the install guide.

---

## Table of contents

1. [Quick orientation](#quick-orientation)
2. [Day-1 setup](#day-1-setup)
3. [Acquiring your first QC run](#acquiring-your-first-qc-run)
4. [Reading the dashboard](#reading-the-dashboard)
5. [The IPS score](#the-ips-score)
6. [The community benchmark](#the-community-benchmark)
7. [PEG Watch — PEG history and the community PEG board](#peg-watch--peg-history-and-the-community-peg-board)
8. [Three deployment modes](#three-deployment-modes)
9. [Remote viewing — Tailscale and phone access](#remote-viewing--tailscale-and-phone-access)
10. [STAN Godmode — multi-instrument view](#stan-godmode--multi-instrument-view)
11. [Common workflows — recipes](#common-workflows--recipes)
12. [Troubleshooting](#troubleshooting)
13. [Where to get help](#where-to-get-help)

---

## Quick orientation

STAN runs as three loosely coupled pieces. The **watcher daemon** (`stan watch`) runs in the background on the machine where finished raw files arrive. That is a separate Linux box in Mode B, or the instrument PC itself in Mode A (see [Three deployment modes](#three-deployment-modes)). It watches the directories you configure and picks up each new raw file once the file has stopped growing. When it sees a new QC run — a HeLa standard or other file matching the QC filename pattern — it dispatches a database search (DIA-NN for DIA data, Sage for DDA data), extracts quality metrics from the results, and writes everything to a local SQLite database. On a SLURM cluster (Mode C) there is no watcher: a login-node cron job finds new raw files and submits one SLURM job per file, and the jobs write to a SQLite database on shared storage.

The **dashboard** (`stan dashboard`) is a local web app served at `http://localhost:8421`. It normally listens only on the machine that runs it, so open it in a browser there, or reach it from your desk as your mode guide describes. It reads the same SQLite database the watcher writes to and shows your QC runs as they accumulate — IPS scores, peptide/precursor counts, TIC traces, ion mobility clouds, chromatography trends, and more.

The **community benchmark** is optional and off until you turn it on. When you opt in and send runs (with `stan submit-all` or the dashboard's Sync button), STAN submits aggregate metrics and the name of each QC file (never raw data, spectra or patient metadata) so you can see how your instrument compares to other labs; see [The community benchmark](#the-community-benchmark). The public dashboard is at [community.stan-proteomics.org](https://community.stan-proteomics.org) (also [on Hugging Face](https://huggingface.co/spaces/brettsp/stan)). **Error telemetry**, which sends crash reports to the STAN relay, is off as well unless you set `error_telemetry: true` (see [`community.yml` and error telemetry](#communityyml-and-error-telemetry)).

---

## Day-1 setup

Your mode's install guide does all of this in order, with a check after each step. This section explains what those steps are for, so you can change the setup later.

### Where the config lives

The config directory is `~/.stan/` on Linux and macOS and `%USERPROFILE%\STAN\` on Windows. It holds:

- `instruments.yml`: what to watch and how to search it;
- `community.yml`: pseudonym, sharing switches and error telemetry;
- `thresholds.yml`: optional pass/fail gates;
- `stan.db`: the SQLite database;
- `logs/`: one log per watcher start and per command.

Mode C does not use `instruments.yml`. Its cluster dispatcher reads its own `dispatch.yml` ([Mode C guide, step 2.6](INSTALL_MODE_C_HPC.md#26-write-dispatchyml)).

### Add an instrument to `instruments.yml` (Modes A and B)

Each watch folder needs one block. The complete block, with every key the watcher needs, is in your mode guide: [Mode B, section 8.2](INSTALL_MODE_B_LINUX.md#82-instrumentsyml), or [README, Mode A step 5](../README.md#mode-a--instrument-pc-windows). To start a block from the command line:

```
stan add-watch /srv/stan/incoming/timsTOF_HT --vendor bruker --name "timsTOF HT" -y
```

The folder must already exist. `add-watch` writes a block the watcher can run: `name`, `vendor`, `watch_dir`, `extensions`, `stable_secs`, `enabled: true`, `qc_only` and `output_dir`. The `output_dir` is `qc_output/<name>` in the config directory, with spaces turned into `_`, so each instrument's results get their own folder. `--vendor` can be left out when the folder already holds `.d` folders or `.raw` files. `-y` accepts the default HeLa/QC filename filter; use `--qc-pattern REGEX` for your own pattern, or `--all-files` for a folder that holds only QC runs. `add-watch` does **not** write these keys, so add them to the block by hand:

- `diann_path` and `sage_path`: the DIA-NN 2.3.x and Sage v0.14.7 executables. They default to `diann` and `sage` on the `PATH`.
- `lib_path` and `fasta_path`: the frozen community spectral library and FASTA. The watcher does not download them, and without a library every DIA search fails. Your mode guide gives the download commands and MD5 checksums.

Give each instrument a `name` that contains its model, for example `timsTOF HT` or `Exploris 480`. The name is stored on every run, and the community benchmark takes the instrument family from it. Check the result with `stan list-watch`: every folder should have a tick under *Exists* and *Enabled*, and its extension under *Extensions*. A key shown in red means the watcher skips that block. Running `stan add-watch <folder> --vendor bruker|thermo -y` on a folder that already has a block adds only the keys it is missing, which repairs a block written by an older version.

The watcher re-reads `instruments.yml` every 30 seconds. That picks up instruments you add, remove, enable or disable. An edit to an instrument that is already being watched (its `watch_dir`, `output_dir`, `lib_path` and so on) takes effect only when the watcher restarts.

### `stan init` and `stan setup`

Neither is needed if you follow your mode guide, which writes the config files by hand.

- **`stan init`** creates the config files that do not exist yet, and never overwrites one: an `instruments.yml` with no instruments, an empty `thresholds.yml` (no gates, so every run passes) and a `community.yml` with every sharing option and error telemetry off. Then it runs the **fleet-sync wizard**, whose answer is saved to `fleet.yml`. Nothing reads that file yet. Its default answer is `3` (None), which is right for every lab outside UC Davis. Pressing Enter, or giving it no input at all (`stan init </dev/null`), takes that default. To redo only the wizard later, run `stan init --reconfigure-fleet`.
- **`stan setup`** is an interactive wizard for a person at the keyboard. It asks for the watch folder, the vendor (when the files in the folder cannot tell it), the instrument name and the QC filename filter, then the LC column, the HeLa amount, community participation, a daily email and error reports. It writes a block the watcher can run, the same keys as `stan add-watch` plus `hela_amount_ng` and the column, and updates that folder's block if there already is one. It does not write `lib_path`, `fasta_path`, `diann_path` or `sage_path`. It writes your community answer to `community_submit` in `community.yml`. Its community question defaults to yes and its error-report question to no, so read them before pressing Enter.

### `community.yml` and error telemetry

Write `community.yml` in the config directory even if the lab will not take part in the community benchmark, so that it states every sharing choice. It is also where error telemetry is switched on:

```yaml
display_name: ""          # public pseudonym; needed only for community sharing
community_submit: false   # true lets `stan submit-all` and the Sync button send benchmark rows
peg_share: false          # true lets `stan peg-sync` share PEG results
error_telemetry: false    # true sends crash reports to the STAN relay; off when this key is absent
```

**Error telemetry is off unless you set `error_telemetry: true`.** When the key is missing, or there is no `community.yml`, nothing is sent; a `community.yml` that `stan init` created says `false`. With `true`, STAN sends a report to `https://brettsp-stan.hf.space` whenever a search or a run's processing fails. A report holds the error type and message, a traceback with the folders stripped from its file paths, and the STAN, Python and OS versions. The message itself is not stripped: for a failed search it is the command line, with the full paths of the raw file and output folder. It can also carry the search engine, vendor, acquisition mode and instrument model, and the name (without its folder) of the raw file involved. Set `error_telemetry: false`, or remove the key, to stop sending. Either way, the last 100 errors are kept locally in `~/.stan/error_log.json` (`%USERPROFILE%\.stan\error_log.json` on Windows).

Separately, the watcher sends a keep-alive (`GET /api/health`) to the same host when it starts and every 12 hours, whatever these settings say. It carries no data. If the host is blocked, the watcher logs a warning and carries on.

### First run of `stan watch`

In Mode B the watcher runs as a systemd service, and in Mode A `stan.bat` starts and supervises it; your mode guide sets that up. To run it by hand in a terminal:

```
stan watch
```

You'll see a startup banner with the STAN version, and a line showing where the watcher log is written:

```
STAN v1.2.5 — watcher starting
Log: /home/stan/.stan/logs/watch_20260929_143012.log
```

Every watcher start writes a new `logs/watch_<date>_<time>.log` in the config directory (`%USERPROFILE%\STAN\logs\` on Windows). The watcher reacts to each new raw file as it appears in a configured `watch_dir`. Between files it sits quietly. To confirm it's healthy, open the newest `watch_<date>_<time>.log` (`stan watch-status` writes `watch_status_*.log` files into the same folder, so don't confuse the two). It should contain `watcher: started <name> → <watch_dir>` for each instrument, then `Active watchers: <N>`. `No enabled instruments configured` means a block is missing `enabled: true`. Warnings and errors go to the same log.

### First open of `stan dashboard`

In Modes A and B the dashboard is already running once the install is done. To start it by hand, in a separate terminal (while `stan watch` is running):

```
stan dashboard
```

It listens on `127.0.0.1:8421`, so only the machine it runs on can reach it (unless Tailscale is logged in on that machine: then it listens on every interface, as [Remote viewing](#remote-viewing--tailscale-and-phone-access) explains). Your mode guide says how to reach it from another machine ([Mode B, section 9.2](INSTALL_MODE_B_LINUX.md#92-stan-dashboardservice); [Mode C, step 5.1](INSTALL_MODE_C_HPC.md#51-dashboard)). The dashboard has no login, and anyone who can reach it can change the configuration, so do not open it to a network you do not trust.

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
(?i)(he(l[_\-\s]?[a5\d]|[_\-\s]?\d)|qc|std[_\-\s]?he)
```

In plain English, files whose names contain any of the following are treated as QC runs and routed through the full search + metrics pipeline:

- `hela`, `hel5`, `hel0` ... `hel9`, also with a `_`, `-` or space after `hel` (case-insensitive)
- `he` followed by a digit, for example `he5` or `he_5`
- `qc` anywhere in the name
- `std_he`, `std-he`, `std he`, `stdhe` and variations

Set `qc_pattern` on an instrument's block (or pass `--qc-pattern` to `stan add-watch`) to use your own pattern, and `exclude_pattern` to skip names such as washes and blanks outright. In Mode C the dispatcher's `qc_pattern` lives in `dispatch.yml`.

What happens to everything else — washes, blanks, real samples — depends on the mode:

- **Modes A and B:** the watcher ignores it and logs `QC filter rejected`. With `monitor_all_files: true` on the block, it records such files in the **Sample Health** table instead, without searching them.
- **Mode C:** every non-QC file gets a lightweight monitor job that writes **Sample Health**, which tracks file counts and flags unusual patterns without running a full search.

**Worked example.** If you're running a timsTOF, name your HeLa standards something like:

```
HeL50_30spd_001.d
HeL50_30spd_002.d
QC_HeLa_Whisper40_20260508.d
```

All of these match. A file named `Sample_Patient_001.d` does not match, so it is never searched (see above for where it goes).

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

The same run data across all time. Use the date filter controls (Week / Month / 3 Months / 6 Months / Year / All) to narrow the view. Clicking a run name opens the run detail panel with per-run drift plots, TIC trace, and raw metric values. On Bruker runs the drift panel's **Ion cloud** tab plots every MS1 feature coloured by charge state — see the Ion cloud recipe below if it reports no cloud available. If you see a step-change in precursor counts at a particular date, check the maintenance log in Config for a column change event that might explain it.

### Trends

Time-series charts of key metrics plotted chronologically. One chart per metric (precursors, peptides, IPS, TIC AUC, etc.), with optional community median overlaid as a dashed line. Use the instrument selector at the top if you have multiple instruments configured. The time filter (Week / Month / 3 Months / etc.) lets you zoom in or out. A gradual downward slope in precursor count over weeks typically means column degradation. A sudden drop in a single run usually means a bad injection or a search failure.

### PEG

Polyethylene-glycol contamination on one instrument over time, plus the community PEG board. See [PEG Watch](#peg-watch--peg-history-and-the-community-peg-board) for what each panel means and how sharing works. Link straight to it with `?tab=peg`.

### Sample Health

A separate table for non-QC files — everything that didn't match the QC filename pattern. It fills in Mode C, and in Modes A and B only for instruments with `monitor_all_files: true` (see [Acquiring your first QC run](#acquiring-your-first-qc-run)). Washes, blanks, and real samples show up here with file counts and flags for unusual patterns. This tab doesn't show search metrics (no database search is run for samples), but it helps you track whether the instrument is running the expected number of acquisitions per day and whether blanks look clean.

### Fleet

This tab shows every instrument in the database the dashboard reads, with how recently each one's QC runs arrived, and compares their depth at matched load. It needs no setup; `stan init`'s fleet-sync answer is not read by this tab or anything else yet. Below that it notes that no mirror is mounted, unless a UC Davis-style mirror folder exists (`HIVE_MIRROR_DIR`, `hive_mirror_dir` in `community.yml`, or `Y:\STAN` on Windows), in which case it lists each host in it. It is most useful from the godmode global view, or on a Mode B box that watches several instruments; on a single-instrument install it shows just your one machine.

### Config

A summary of your current `instruments.yml` settings: watch directories, LC column, vendor, family, community submission status. Use this to quickly confirm STAN is watching the right directory. If you need to change anything, edit `instruments.yml` in the config directory directly. The watcher picks up added, removed, enabled or disabled instruments within 30 seconds; any other edit takes effect when the watcher restarts.

### Community

Shows your community benchmark opt-in status and a log of recent submissions. The **Community sync** panel at the top pushes eligible runs to the public benchmark on one click and shows how many are waiting; see [The community benchmark](#the-community-benchmark) for what gets sent. The "ID depth" chart compares your instrument's precursor/PSM counts against the community distribution for the same instrument family and throughput bucket (SPD).

### Arcade

Retro mini-games (Keratin Invaders, m/zork, KarateMass, Mass Match, Core Defense) playable directly in the dashboard. Scores can be put on a shared leaderboard so labs can compare.

#### Shared Arcade Leaderboard

When a game ends, STAN asks for a **name** and a **lab / affiliation**. Both
are optional — leave them blank and the score goes on the board as
`anonymous`. Whatever you type is remembered in this browser, so the next
game is just Enter. Press Escape (or **Skip**) to keep the score off the
board entirely.

The score is posted to this dashboard's own `POST /api/arcade/score`, and
where it lands depends on the install:

| Install | Board |
|---------|-------|
| PG Farm configured (`STAN_DB_BACKEND=pg`) | Central `arcade_scores` table — one board shared by every STAN and the hosted dashboard. |
| No PG Farm | Local SQLite `arcade_scores` table — a board for this install only. Everything still works. |
| Public / hosted dashboard | **Read-only.** The board is visible, but score posts are refused (403) because a public instance can't tell a player from a script. Play on a lab install. |

The top 5 per game appear in the **Community high scores** panel at the top
of the Arcade tab.

**What gets stored:** game name, score, level, win/loss flag, the name and
affiliation you typed, and the hostname that posted it (kept for moderation,
never displayed). **Nothing else** — no email, no institutional ID, no raw
files, no sample metadata, no instrument serial numbers.

Because the PG board is readable by every lab running STAN, treat the name
field as public. Names are capped at 40 characters and affiliations at 60,
and both are HTML-escaped everywhere they are rendered.

The older per-lab pseudonym flow (`stan claim-name`, `arcade_submit` in
`~/.stan/community.yml`, `stan/community/arcade_submit.py`) targeted HF Space
relay endpoints that were never deployed; `stan claim-name` no longer exists,
and a lab name is claimed with `stan community-claim`. The pseudonym is now
only used to prefill the name box; the relay is a read-only fallback for the
board.

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

The community benchmark lets you see how your instrument compares to instruments at other labs running the same QC standards. It is optional, and nothing is sent until you turn it on and send runs. The watcher never submits by itself.

**What gets sent, per QC run:**
- your lab's pseudonym;
- instrument model and family, acquisition mode, SPD, gradient length and injected amount;
- precursor, peptide, protein and PSM counts and the IPS score, plus chromatography and mass-accuracy statistics and a binned TIC trace;
- the LC column and LC system, the DIA-NN version and the run date;
- **the raw file's name**, without its folder.

**Never sent:** raw files or spectra, the folder a raw file sits in, instrument serial numbers, or anything about your real samples. Only QC runs are sent; washes, blanks and runs with zero identifications are skipped.

**Privacy note: file names reach the public dataset.** From relay (Space) 1.2.2 the community page and the relay's API (`/api/leaderboard`, `/api/cohorts`) no longer show or return a row's file name (for example `HeLa_50ng_30min_001.raw`); chart tooltips show the instrument, date and SPD instead. The relay still stores the name with the row, where it catches duplicate submissions, and that row is written to the public Hugging Face dataset, whose history also holds every name sent so far (removing it there is a pending decision). Before you opt in, make sure your QC file names carry no patient, customer or project identifiers. Setting the environment variable `STAN_STRIP_RUN_NAME=1` blanks the name; relay 1.2.2 accepts such rows (1.2.1 and earlier refused them).

**How to take part:**

1. **Choose a pseudonym.** Put it in `display_name` in `community.yml` in the config directory (see [`community.yml` and error telemetry](#communityyml-and-error-telemetry)). Your lab appears under this name, never under its real one.
2. **Claim the name**, in an interactive terminal:
   ```
   stan community-claim
   ```
   STAN asks for an email address, and the relay sends a 6-digit code from `noreply@stan-proteomics.org` (check the spam folder). Type the code, and STAN writes an `auth_token` into `community.yml`. The address only proves that you own the name; the relay keeps a one-way hash of it, not the address. The relay keeps one token per name, so claiming again replaces the token everywhere: copy the `auth_token` line to every other machine that shares as your lab. Never paste it into an issue or a chat.
3. **Opt in.** Set `community_submit: true` in `community.yml`.
4. **Send.** Preview first, then send:
   ```
   stan submit-all --dry-run
   stan submit-all
   ```
   The first `submit-all` sends every eligible run already in the database; after that it sends only runs not yet submitted. Each run's outcome, including why a run was rejected, is written to `logs/submit_all_<date>.jsonl` in the config directory.
5. **Keep it going.** Because the watcher never submits, schedule `stan submit-all` (UC Davis runs it every 6 hours), or press **Sync** on the dashboard now and then. Your mode guide has the schedule: [Mode B, section 11](INSTALL_MODE_B_LINUX.md#11-community-benchmark-and-peg-sharing-optional) (a systemd timer), [Mode C, step 5.2](INSTALL_MODE_C_HPC.md#52-community-benchmark-and-peg-sharing) (a cron job), or [a scheduled task](INSTALL_REGRESSION_CHECKLIST.md#73-scheduling-both) for Mode A.

**Or use the Sync button.** The dashboard's Community tab has a *Community
sync* panel that does steps 3 and 4 without the terminal. It shows how many
runs are eligible, lets you set the lab name, and pushes them on one click.
The count on the button applies the same rules as `stan submit-all`
(`submission_readiness` in `stan/community/submit.py`), so it is what would
actually be pushed. Washes, blanks, runs with zero identifications, runs below
the community hard gates (too few IDs) and DIA runs with an unknown or
incompatible DIA-NN version are excluded. QC runs still missing a metric the
community site requires (the TIC trace, peak capacity, peak width, …) are
listed under the button as *waiting for metrics* and go out once a backfill
fills them in. A run the site already holds is recorded as submitted instead
of failing on every sync. Pressing the button sets `community_submit: true` in
`community.yml`.

If you have not set a pseudonym, the panel generates one for you (e.g.
*Oxidized Cottrell*) and pre-fills it; edit it to whatever you like before
syncing. Whatever you confirm is saved to `community.yml` and reused from then
on. The panel does not claim the name, so run `stan community-claim` (step 2)
if you want to make sure nobody else can claim it.

**Which searches count.** The benchmark accepts only runs searched with
DIA-NN 2.3.x, and marks runs from any 2.3.x (2.3.0, 2.3.1 or 2.3.2) as having
used the verified community library and FASTA. The 2.3.2 that the Windows
installers put in counts the same as 2.3.0. For a DDA run, submission reads
the DIA-NN version from the first `diann` on the `PATH`, so that must be 2.3.x
as well. Sage is pinned at v0.14.7
(its binary prints `sage 0.14.6`). Use the frozen community FASTA and
libraries from your mode guide, so that counts are comparable across labs.
The version pins are listed in [`INSTALL_FOR_AGENTS.md`](../INSTALL_FOR_AGENTS.md#23-version-pins).

Newly submitted runs do not appear on the public site instantly — the public
dashboard reads a consolidated file that is rebuilt nightly, so allow a day.

**The public dashboard** is at [community.stan-proteomics.org](https://community.stan-proteomics.org) (also [on Hugging Face](https://huggingface.co/spaces/brettsp/stan)). It shows the community leaderboard, SPD-bucketed ID depth comparisons, and cross-lab TIC overlays. Your runs appear under your pseudonym, grouped by instrument family and throughput bucket.

**To stop submitting:** set `community_submit: false` in `community.yml`. `stan submit-all` then refuses to send, so a scheduled sync stops at its next run. Pressing **Sync** on the dashboard turns submission back on. Runs already sent are not withdrawn.

---

## PEG Watch — PEG history and the community PEG board

STAN scores PEG (polyethylene glycol, the ladder of peaks 44.026 Da apart
that plastics, detergents and some Evosep consumables leave behind) on
every QC run it can read: Bruker `.d` through alphatims (the `peg`
install extra), Thermo `.raw` through `fisher_py` (the `thermo` extra), or,
on Linux without `fisher_py`, through the ThermoRawFileParser container
(`STAN_TRFP_SIF`; see [Mode B, section 7.3](INSTALL_MODE_B_LINUX.md#73-thermo-peg)).
The **PEG** tab turns those numbers into a history. Reference:
[`docs/PEG_WATCH.md`](PEG_WATCH.md).

### The number: PEG share of MS1

STAN reads 80 MS1 scans spread across the gradient and matches peaks
within 5 ppm to PEG1–20 as [M+H]⁺, [M+NH₄]⁺ and [M+Na]⁺. The **PEG share
of MS1** is the matched intensity divided by the intensity of all sampled
MS1 peaks above 10⁴ counts. It is not a fraction of the TIC. It keeps
rising as contamination gets worse, where the 0–100 PEG score stops at
100, so the tab and the board use the share and show the class (clean,
trace, moderate, heavy) as a badge.

**Compare it only within one instrument family.** The 10⁴ floor is the
same number on every instrument, but a timsTOF and an Orbitrap report
intensity on different scales, so 3 % on one is not 3 % on the other.
The classes are further off still: the PEG score behind them is
calibrated on timsTOF data, so on any other instrument the tab tags the
class tiles and legends **timsTOF-calibrated** and leaves the clean rate
out of any table that spans families — compare PEG share there instead.

Runs whose PEG could not be read are left out. They never count as clean,
and neither does a run that read no MS1 signal at all.

### The PEG tab

Pick the instrument at the top (the one with the most PEG runs is the
default). From top to bottom:

- **Headline and tiles** — the 30-day median PEG share against the 30
  days before, the clean and heavy QC counts, the latest episode, and
  your community rank (Evosep instruments only).
- **PEG over time** — every QC run, a 14-day median line, column changes
  from the maintenance log, and the best 90-day stretch as a baseline.
  Shaded bands are **episodes**: stretches where the 14-day median stayed
  at or above 3 % for at least two weeks (hot spells less than three
  weeks apart are one episode). Toggle the SPD and the time range.
- **Every QC day** — a calendar since the start of last year, each day
  coloured by its median QC's class, so one bad injection does not paint
  the whole day.
- **Community PEG leaderboard** and **Evosep vs other LC** — see below.
- **What PEG costs you** — median precursors per PEG class for DIA runs,
  for methods with enough clean and heavy runs to compare.
- **PEG ladder fingerprint** — which oligomers (PEG2–20) were seen each
  month, and the adduct mix. A shift in the ladder or the adducts often
  points at a new source.
- **PEG by column period** — one row per LC column from the maintenance
  log.
- **Found PEG? Isolate the source in one night** — the Evosep diagnostic
  protocol ([`docs/PEG_EVOSEP_DIAGNOSTIC.md`](PEG_EVOSEP_DIAGNOSTIC.md)).
- **What your lab shares** — whether this lab shares, and exactly what.

Each raw file counts once, even when STAN ingested it twice (say from the
instrument PC and again on the cluster).

### Sharing PEG with the community

Sharing is opt-in and separate from the benchmark: it needs no community
search, and any lab that scores PEG can take part. Only Evosep runs are
ranked; runs on other LCs feed the LC comparison.

1. **Claim your lab name** so nobody else can take it. Set `display_name`
   in `community.yml` first, then run:
   ```
   stan community-claim
   ```
   STAN asks for the email address (the one the name was claimed with, if
   it was claimed before), the relay emails it a 6-digit code, and STAN
   stores a fresh `auth_token` in `community.yml`. The relay keeps one token
   per name, so copy that `auth_token` line to every other machine that
   shares as your lab. If you already claimed the name for the
   [community benchmark](#the-community-benchmark), skip this step:
   claiming again replaces the token, and the old one stops working on
   every machine.
2. **Opt in**: add `peg_share: true` to `community.yml` (or set
   `STAN_PEG_SHARE=1`).
3. **Sync**:
   ```
   stan peg-sync --dry-run    # see what would be sent, send nothing
   stan peg-sync              # send
   ```
   Every sync resends all your shareable QC runs and the relay keeps only
   what changed, so running it on a schedule is safe. The mode guides
   schedule it together with `stan submit-all`. It reads the local SQLite
   database; on a Mode C cluster, point `STAN_DB_PATH` at the cluster
   database and pass `--backend sqlite`
   ([Mode C, step 5.2](INSTALL_MODE_C_HPC.md#52-community-benchmark-and-peg-sharing)).
   `--backend pg` is only for UC Davis's central Postgres.

**What is shared, per QC run:** date and time, instrument model and
family, LC (Evosep or other), SPD, acquisition mode, sample type, load,
the PEG share, score, ion count and class, and an anonymous `run_key`.
**Never shared:** file or sample names, raw data or spectra, serial
numbers, customer or project details. Blanks and washes are not shared.

**Logs:** each sync writes `~/.stan/logs/peg_sync_<UTC time>.jsonl`
(`~\STAN\logs` on Windows), one line per batch plus a summary, and the
command's last lines say how many runs were accepted, unchanged, rejected
and skipped, and why.

**To stop sharing:** set `peg_share: false`. The runs already sent stay
on the board for the rest of its window; the relay has no delete.

### The community board

The public page (`https://brettsp-stan.hf.space/#peg`) and the PEG tab
both show it. Labs are grouped into **cohorts** of the same instrument
family and Evosep method (for example timsTOF × 100 SPD), so a timsTOF lab
is never ranked against an Orbitrap lab. Within a cohort, over the last
30, 90 or 365 days:

- A lab needs **5 QC runs** in the window to be ranked. Fewer, and it is
  listed as unranked.
- Lower is better: labs are ordered by median PEG share, then by clean
  rate, then by run count.
- **Cleanest** goes to rank 1 when at least two labs are ranked. **Most
  improved** goes to the lab whose median fell furthest against the
  previous window: at least 15 % down *and* at least 0.5 percentage
  points. A change from a previous median below 0.1 % is not shown at
  all — at the detection floor it is noise.
- A check mark means the lab name is claimed and the runs came with its
  token. Unclaimed names are shown as unverified, and once a name is
  claimed, rows sent under it without the token are left out.

On an instrument that is not on an Evosep, the tab shows the family's
Evosep board for context.

### Evosep vs other LC

"Does PEG follow the LC?" The panel has two halves:

- **Your instruments, last 90 days** — every instrument with PEG, its LC,
  90-day median, clean rate (only when every row is one instrument
  family) and a 26-week sparkline. Instruments with no
  LC recorded are marked Unknown.
- **Community, same instrument family** — Evosep and other-LC runs from
  every sharing lab, compared only within one family.

**The caveat:** if your Evosep and non-Evosep runs are on different
instrument families — at UC Davis the Evosep is on the timsTOF and the
Orbitraps have their own LC — the panel says so, because the difference
then mixes the LC with the detector. A like-for-like LC comparison needs
the same instrument family on both sides, which is what the community
half shows once labs with both have shared.

---

## Three deployment modes

The mode decides where the searches run. Choose it with [`INSTALL_FOR_AGENTS.md`](../INSTALL_FOR_AGENTS.md#step-1-choose-the-mode), which asks the questions that decide it, and then follow that mode's guide from its first step. In order of recommendation:

| Mode | Searches run on | Instrument PCs run | You need | Install guide |
|---|---|---|---|---|
| **C: SLURM cluster** (recommended) | Compute nodes of the lab's cluster | Only a file-copy task | A SLURM account, shared storage that the instruments (or a copy task) can reach, and someone to keep a cron job running | [`INSTALL_MODE_C_HPC.md`](INSTALL_MODE_C_HPC.md) |
| **B: separate Linux box** (native Linux, or WSL2 on a Windows workstation) | One x86_64 Linux machine that is not an acquisition PC | Only a file-copy task | A spare Linux machine, or a Windows workstation that can run WSL2 | [`INSTALL_MODE_B_LINUX.md`](INSTALL_MODE_B_LINUX.md), plus [`INSTALL_MODE_B_WSL.md`](INSTALL_MODE_B_WSL.md) for WSL2 |
| **A: instrument PC** (Windows), not recommended | The acquisition PC itself | All of STAN | Only the instrument PC | [README, Mode A](../README.md#mode-a--instrument-pc-windows) |

UC Davis, where STAN is developed, runs Mode C, and its instrument PCs only acquire.

### Mode C — SLURM cluster

Instrument PCs copy each finished run to shared storage that the cluster can read. Every 5–15 minutes, a cron job on the login node runs `stan hive-dispatch --config <dispatch.yml>`, which submits one SLURM job per new raw file. Each job runs `stan hive-process`: the search, the metrics and the database write. There is no `stan watch` in this mode. Results go to a SQLite database on shared storage, and a lab machine serves the dashboard from a copy of it.

Things to know before you choose it:

- **The pipeline is not yet portable.** In STAN 1.2.x the job scripts still contain UC Davis values (container and binary paths, bind mounts, module names, one SLURM account). You install from a git clone and apply a small site patch that the guide lists line by line.
- **You need a DIA-NN 2.3.0 container with .NET 8 inside**, or a bare binary plus a .NET 8 module. No public STAN image exists.
- **Delivery is the opposite of Modes A and B.** The dispatcher does not check that a copy has finished, so each run must appear under its final name only when it is complete: copy it as `<run>.d.partial` or `<run>.raw.partial` and rename it when the copy is complete, or symlink finished runs from an existing archive. Each watch directory is read flat; subfolders are not searched.

Install guide: [`docs/INSTALL_MODE_C_HPC.md`](INSTALL_MODE_C_HPC.md).

### Mode B — separate Linux box (native or WSL2)

Instrument PCs run only a copy task, for example a scheduled `robocopy`, into a share on one Linux machine that is not an acquisition PC. `stan watch` and `stan dashboard` run on that box as systemd services, and the dashboard is reached from other machines as the guide describes. The box can be native Linux or WSL2 on a Windows workstation.

- **Hardware.** x86_64 only. Plan on at least 8 cores and 32 GB of RAM, plus more memory if several instruments can finish a QC at the same time. Each DIA-NN or Sage search uses half the cores, and the watcher stops any search after 20 minutes.
- **Delivery.** Copy each run in place under its final name. The watcher reacts only to newly created files and does not see a file that is renamed into place.
- **Thermo** needs .NET 8 on the box.

Install guide: [`docs/INSTALL_MODE_B_LINUX.md`](INSTALL_MODE_B_LINUX.md). On a Windows workstation, read the Linux guide first and then [`docs/INSTALL_MODE_B_WSL.md`](INSTALL_MODE_B_WSL.md) for what differs inside WSL2.

### Mode A — instrument PC (Windows, not recommended)

The watcher, the searches, the database and the dashboard all run on the acquisition PC. Use this only when the instrument PC is the only machine you have, and only after reading the warning in the README. At UC Davis, running searches on the acquisition PC froze a timsTOF mid-run, and a stalled acquisition can cost the run or the sample. STAN holds each DIA-NN and Sage search to half the cores, but not every heavy step is capped (the README's Mode A warning lists which are not), and each watch folder can run its own search at the same time.

If you use it anyway, set `startup_catchup_days: 0` so the first start does not search 30 days of old files while the instrument acquires, do not install 4DFF, and never run `update-stan.bat` on a PC that acquires.

Install guide: [README, Mode A](../README.md#mode-a--instrument-pc-windows), with reference details in [`docs/INSTALL_REGRESSION_CHECKLIST.md`](INSTALL_REGRESSION_CHECKLIST.md).

---

## Remote viewing — Tailscale and phone access

You can view your QC dashboard from your phone, your office computer, or another lab — anywhere on your Tailscale network — without opening firewall ports or setting up a VPN.

### Why bother

The dashboard auto-refreshes as runs come in. Being able to check IPS scores and TIC traces from your phone while you're in a meeting — or from home overnight — means you catch problems before the next morning's queue of samples.

### Install Tailscale

Install Tailscale on the machine running `stan dashboard`: the Mode B Linux box, the lab machine that serves a Mode C database (not a cluster login node), or the instrument PC in Mode A:

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

`0.0.0.0` means every network the machine is on, not only your tailnet. The dashboard has no login, and anyone who can reach it can rewrite `instruments.yml`, which names the programs the watcher runs, so treat access to it like shell access. On a machine that is also on an untrusted network, bind to the Tailscale address alone with `stan dashboard --host <tailscale IP>`, or keep it behind the lab's firewall. Only a literal `127.0.0.1` (the default) is widened: the Mode B service passes `--host localhost`, which stays on loopback until you change it ([Mode B, section 9.2](INSTALL_MODE_B_LINUX.md#92-stan-dashboardservice)).

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
- The database of a Mode B box. It already holds every instrument the box watches, so that box's own dashboard is the fleet view and needs no extra setup.
- A copy of a Mode C cluster database. Point the dashboard at a copy, not at the file the SLURM jobs are writing, and run it on a lab machine rather than a cluster login node: `STAN_DB_PATH=/local/disk/stan_copy.db STAN_PG_REFRESH_SECONDS=0 stan dashboard --backend sqlite` ([Mode C, step 5.1](INSTALL_MODE_C_HPC.md#51-dashboard)).
- A UC Davis-style mirror on a network drive, which STAN fills when `HIVE_MIRROR_DIR` or `hive_mirror_dir` in `community.yml` names it, or when a `Y:\STAN` folder exists on Windows. Option 1 of `stan init`'s fleet wizard does not set this up by itself. A single-lab install does not need it.

Combine godmode with Tailscale for true remote fleet ops: start the dashboard on your Mode B box or on the lab machine that serves the Mode C copy, connect via Tailscale from your phone, and watch all your instruments from anywhere.

---

## Common workflows — recipes

**"I just installed; how do I get QC running?"**

Follow your mode's install guide to the end (start at [`INSTALL_FOR_AGENTS.md`](../INSTALL_FOR_AGENTS.md)); its last step searches one real QC file. After that, acquire a HeLa run with a QC-matching filename, or copy (never move) a finished one into a watched folder, and check on the machine that runs STAN:

```
stan version                  # STAN v1.2.x  (there is no "stan --version")
stan list-watch               # Modes A and B: every folder has a tick under Exists and Enabled
stan watch-status --days 1    # Modes A and B: the file was matched and is in runs
stan status                   # the database shows (1 runs) or more
# then open http://localhost:8421 on the machine that runs the dashboard
```

In Mode C there is no watcher, so skip the two watch commands and prefix `stan status` with `STAN_DB_PATH=<db_path>`. The full list of checks is in [`INSTALL_FOR_AGENTS.md`, Success checks](../INSTALL_FOR_AGENTS.md#success-checks).

**"I want to search old QC runs (backfill)."**

- **Modes A and B, runs already in a watched folder:** raise `startup_catchup_days` on that instrument's block to cover them, then restart the watcher. On every start the watcher searches the QC files in its folder from that many days back (by file modification time) that are not in the database yet. Set it back afterwards.
- **Modes A and B, runs in another folder:** run `stan baseline`. It is interactive: it asks for the folder, the instrument and the search engines, then searches every matching file on this machine. It does not use `diann_path`: it looks in the usual install folders and on the `PATH`, and picks DIA-NN 2.3.0 if it finds one, otherwise the newest 2.3.x, even when a newer DIA-NN is installed beside it. It labels each run with the version of the binary that searched it. If it finds no 2.3.x it uses the newest DIA-NN it has and logs a warning, and the benchmark will reject those runs, so check the DIA-NN path it prints. It also asks whether to submit the results to the community benchmark, with yes as the default (and it does not ask at all when `auto_submit: true` is set in `community.yml`), so answer no unless the lab has opted in.
- **Mode C:** symlink the old runs into a watch directory, or copy them in under a `.partial` name and rename each one when its copy is complete. The dispatcher submits them over its next ticks, up to `max_submissions_per_run` per tick.

In Mode A, run neither while the instrument acquires: both search on the acquisition PC. Do not use `stan backfill-from-dir` in any mode: it uploads to UC Davis's cluster over UC Davis's own share and SSH setup.

**"The run modal's Ion cloud tab says no cloud is available."** (Bruker only)

The charge-labeled ion cloud comes from Bruker's 4DFF feature finder,
which writes a `<name>.d.features` file next to the raw `.d`. Two things
have to be true: the sidecar has to exist, and its contents have to be
published to the database — the dashboard usually runs on a different
machine from the raw data, so it cannot open the sidecar itself.

On the machine that can see the raw files: the Mode B box, or in Mode C
the cluster, inside a SLURM job rather than on the login node. Do not
install 4DFF on an acquisition PC (Mode A): once it is installed, it runs
after every Bruker QC run, and it is not thread-capped.

```
stan install-4dff              # once — downloads the Bruker binaries
stan run-4dff /path/to/run.d   # one run
stan backfill-features         # or: every indexed .d that lacks a sidecar
stan backfill-feature-cloud    # publish the clouds to the database
```

`backfill-feature-cloud` walks your runs newest-first, reads each
sidecar, and stores a downsampled charge-labeled cloud (5,000 points by
default) in the `feature_clouds` table. Once it's there, every dashboard
shows the Plotly view — one trace per charge state, click the legend to
hide `+1` contamination. Runs without a cloud fall back to the older
grayscale SVG cloud.

If the extraction host can't reach the database, point the two halves at
a shared directory instead:

```
# on the host with the raw data
stan backfill-feature-cloud --cache-dir /shared/feature_clouds
# on the host with the database
stan backfill-feature-cloud --from-cache /shared/feature_clouds
```

**"I need to disable community submission temporarily."**

Edit `community.yml` in the config directory and set:

```yaml
community_submit: false
```

Save the file. The watcher never submitted anything, so there is nothing to restart: `stan submit-all` refuses to send while this is `false`, so a scheduled sync stops at its next run. Pressing **Sync** on the dashboard's Community tab turns it back on. PEG sharing has its own switch, `peg_share`.

**"I want to share my QC trends with a collaborator."**

Install Tailscale on your machine and theirs, add them to your tailnet (or use Tailscale's share feature for one-off access), and send them your MagicDNS dashboard URL.

**"How do I check what version is running?"**

```
stan version
```

**"How do I update STAN to the latest version?"**

Updating STAN through `stan.bat`, git or pip never changes DIA-NN or Sage; keep them at the pinned versions.

- **Mode A (Windows, `stan.bat`):** `stan.bat` upgrades STAN from `main` every time it starts, and keeps the extras you installed. Never run `update-stan.bat` on a PC that acquires, or while `stan.bat` is running: it kills STAN and starts a backfill chain that runs for hours.
- **Mode B:** fetch and check out the new commit in the git clone, reinstall with the same extra, and restart the services. The commands are in [Mode B, section 12](INSTALL_MODE_B_LINUX.md#12-operating-the-box).
- **Mode C:** rebase the site branch of the git clone onto `origin/main`, and re-check the site patch. The next job to start runs the new code ([Mode C, step 2.2](INSTALL_MODE_C_HPC.md#22-stan-from-a-git-clone-editable)).
- **Any other pip install:** `pip install --upgrade "stan-proteomics[peg] @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip"`, with the extra you installed originally. STAN is not on PyPI, so a bare `pip install stan-proteomics` will not find it.

---

## Troubleshooting

**"Dashboard says 'No QC runs yet'"**

The most common cause is that the watcher is watching the wrong directory, or no files matching the QC filename pattern have been acquired yet. Check `instruments.yml` in the config directory: confirm `watch_dir` points to where your HeLa raw files actually land, and that the block has `enabled: true`. Also confirm your filenames match the QC pattern (contain `hela`, `qc`, or `std-he` / `std_he` variants — case-insensitive). `stan watch-status --days 1` lists each recent file in the watched folders and says whether it matched the filter and reached the database. In Mode C, look in the dispatcher's cron log and `squeue` instead.

**"Dashboard is blank or shows an error in Internet Explorer"**

IE is not supported. Use Chrome, Edge (Chromium), or Firefox.

**"Watcher crashes on startup or disappears"**

Check the watcher log file printed at startup: the newest `logs/watch_YYYYMMDD_HHMMSS.log` in the config directory (`~/.stan/logs/` on Linux and macOS, `%USERPROFILE%\STAN\logs\` on Windows). Warnings and unhandled exceptions are written there. Common causes: Python version mismatch, instruments.yml parse error (invalid YAML), or a watch directory that doesn't exist (`Watch directory does not exist` in the log). Under systemd (Mode B), `journalctl -u stan-watch` also shows anything printed before the log file opened.

**"DIA-NN search returns 0 precursors"**

Read `<output_dir>/<run name>/diann.log` first. Usually it is one of these:

1. A missing or wrong spectral library. STAN requires a spectral library for DIA; library-free mode is not supported. The watcher does not download the community library, so set `lib_path` to the file from your mode guide.
2. A missing FASTA file (`fasta_path`).
3. On Linux, Thermo `.raw` files and no .NET 8. DIA-NN needs it to read `.raw` (the log says `invalid raw MS data format`), and so does ThermoRawFileParser.
4. A DIA-NN other than 2.3.x. The community benchmark will refuse the run in any case.

The mode guides' troubleshooting tables cover the rest: [Mode B](INSTALL_MODE_B_LINUX.md#13-troubleshooting), [Mode C](INSTALL_MODE_C_HPC.md#troubleshooting). Tool details are in [`docs/external_tools.md`](external_tools.md).

**"I can't see my dashboard from my phone"**

See the [Tailscale section](#remote-viewing--tailscale-and-phone-access). The most common causes: Tailscale isn't installed on one of the devices, the devices are on different Tailscale accounts, or the macOS application firewall is blocking Python's incoming connections.

**"stan: command not found after install"**

The virtual environment isn't activated, or your terminal hasn't picked up the updated PATH. Calling `stan` by its full path always works: `~/.stan/venv/bin/stan` in Mode B, `$STAN_HOME/venv/bin/stan` in Mode C, `%USERPROFILE%\STAN\venv\Scripts\stan.exe` on Windows.

- **Linux:** the Mode B guide puts the `PATH` line in both `~/.profile` and `~/.bashrc`. Ubuntu's `~/.bashrc` stops early in a non-interactive shell, so a line only there never reaches scripts, `sudo -iu` or an AI agent's shell. A plain `bash -c` reads neither file, so use the full path there.
- **Windows:** open a new window after the install, and confirm the STAN venv `Scripts\` directory is on your user PATH. If you have both an old `.stan\venv` and a new `STAN\venv`, the updater should have migrated PATH entries — check that the old entry isn't shadowing the new one.

**"Community submission is refused or fails"**

Each run's outcome is in `logs/submit_all_<date>.jsonl` in the config directory.

- `community_submit is not enabled`: set `community_submit: true` in `community.yml`. The command suggests `stan setup`, which sets it only if you answer yes to its community question. A `community_submit` key inside an `instruments.yml` block, where older versions of `stan setup` put it, is not what submissions read.
- `DIA-NN version mismatch` or `DIA-NN version could not be detected`: the run was not searched with DIA-NN 2.3.x. For a DDA run, submission reads the version from the first `diann` on the `PATH`, so that must be 2.3.x as well.
- `Submission rejected:` followed by a metric: the run failed a quality gate or lacks a metric the benchmark requires.

The submission goes through the HF Space relay — you don't need an HF token on the client side. A 401 or a 5xx error from the relay is a server-side problem, not something you need to fix: open an issue on GitHub (never paste your `auth_token` into it).

**"`stan peg-sync` fails with HTTP 403"**

Your lab name is claimed and the relay did not accept this machine's `auth_token` — it is missing, or it was replaced when the name was re-claimed somewhere else. Run `stan community-claim` (or copy the current `auth_token` line from the machine that last claimed it). The relay keeps one token per name.

**"The PEG tab says 'No PEG measurements yet'"**

STAN cannot read that instrument's raw MS1 yet.

- **timsTOF:** install STAN with the `peg` extra on Python 3.10–3.12, and check that `stan doctor` shows alphatims 1.0.8, numpy 1.26.x and pandas 2.x. `stan install-peg-deps` also installs alphatims, but it does not pin `pandas<3`, and under pandas 3 Bruker PEG stays empty.
- **Orbitrap on Windows:** install the `thermo` extra (`fisher_py`), the same way you installed STAN, for example `pip install "stan-proteomics[thermo] @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip"`. STAN is not on PyPI.
- **Orbitrap on Linux:** use the ThermoRawFileParser container and set `STAN_TRFP_SIF` ([Mode B, section 7.3](INSTALL_MODE_B_LINUX.md#73-thermo-peg)).

Then `stan backfill-peg` scores the runs you already have. Runs whose read failed are left out rather than shown as clean.

**"Files in F:\data\... aren't being picked up"**

Check `instruments.yml` for a `watch_dir` typo, and confirm the block has `enabled: true` and the right `extensions` (`[".d"]` for Bruker, `[".raw"]` for Thermo). On Windows, confirm the drive letter is correct and the path uses backslashes or forward slashes consistently. Also confirm the watcher process has read access to that path (run `stan watch` in the same user account that owns the data directory).

Two delivery problems look the same (Modes A and B):

- The watcher reacts only to newly created files, so a file that was copied under a temporary name and then renamed into place is never seen. Copy files in place under their final names.
- A Bruker `.d` that is already complete when it appears (a fast copy or a move) can wait forever on the live watcher. Restart the watcher: its start-up catch-up scan picks the file up.

`stan watch-status --days 1` shows which of these you have. In Mode C the opposite rule applies; see [Three deployment modes](#mode-c--slurm-cluster).

**"Sage returns very low PSM counts for DDA data"**

Confirm the FASTA is the community-standardized one (`human_hela_202604.fasta`, from your mode guide) and that Sage is v0.14.7 (it prints `sage 0.14.6`). If you're running on Bruker `.d` files, Sage reads them natively — no mzML conversion needed. If you're on Thermo `.raw`, STAN first converts the file to mzML with ThermoRawFileParser, which it downloads on first use into `tools/ThermoRawFileParser/` in the config directory. On Linux that build needs .NET 8 (`dotnet` on the `PATH`). See [`docs/external_tools.md`](external_tools.md).

---

## Where to get help

- **GitHub issues:** [github.com/bsphinney/stan/issues](https://github.com/bsphinney/stan/issues) — bug reports, feature requests, questions. Include the mode, the OS, `stan version`, the output of `stan doctor`, the relevant log lines and the exact command that failed. Never include your `auth_token`, and never attach raw files.
- **Installing:** [`INSTALL_FOR_AGENTS.md`](../INSTALL_FOR_AGENTS.md) and the mode guide it links to
- **Community dashboard:** [community.stan-proteomics.org](https://community.stan-proteomics.org) (also [huggingface.co/spaces/brettsp/stan](https://huggingface.co/spaces/brettsp/stan)) — public benchmark and leaderboard
- **Source code:** [github.com/bsphinney/stan](https://github.com/bsphinney/stan)
