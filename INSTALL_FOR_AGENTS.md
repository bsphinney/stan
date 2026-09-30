# Installing STAN: start here (instructions for AI agents)

This file is for an AI coding agent (Claude Code, Cursor, Codex, Aider, or similar) that a
lab has pointed at this repository to install STAN. A human can follow it too.

It is the entry point. It covers:

- which of the three install modes to use;
- the rules and version pins that apply to every mode;
- how to check that the install works;
- what to report back.

The step-by-step instructions for each mode are in the mode guides it links to.

Last checked against `main` at STAN 1.2.x on 2026-09-29. If `stan version` reports a
different major or minor version, confirm each command with `stan <command> --help` before
using it. If a guide and the code disagree, the code is right: tell the human and open an
issue (see [Getting help](#getting-help)).

---

## For the human: the prompt to give your agent

```text
Install STAN, the proteomics QC tool at https://github.com/bsphinney/stan, for our lab.
First read https://raw.githubusercontent.com/bsphinney/stan/main/INSTALL_FOR_AGENTS.md
and follow it. Ask me its Step 1 questions before you install anything. Tell me which
mode you chose and why. Stop and ask me before any step that needs administrator rights,
a license acceptance, my email address, or a change on an instrument PC. When you finish,
give me the report described in its "Report back" section.
```

---

## What you are installing

STAN watches the place where finished raw files arrive: Bruker `.d` directories and Thermo
`.raw` files. For every HeLa QC injection it runs a standardized search, using DIA-NN for
DIA and Sage for DDA. It then:

- extracts QC metrics (precursors, PSMs, peptides, IPS score and others);
- stores them in a SQLite database;
- shows them on a web dashboard on port 8421.

Community benchmark submission is optional, and nothing is submitted until the lab turns it
on. Error telemetry is off too, unless the lab sets `error_telemetry: true`. The only
traffic without an opt-in is a keep-alive that carries no data (see
[Privacy defaults](#24-privacy-defaults)).

**The install is finished** only when a real QC raw file has been searched, stored in the
database and shown on the dashboard. See [Success checks](#success-checks).

---

## Step 1: Choose the mode

The mode decides where the searches run. In order of recommendation:

| Mode | Searches run on | Instrument PCs run | Guide |
|---|---|---|---|
| **C: SLURM cluster** | Compute nodes of the lab's cluster | Only a file-copy task | [`docs/INSTALL_MODE_C_HPC.md`](docs/INSTALL_MODE_C_HPC.md) |
| **B: separate Linux box** (native Linux, or WSL2 on a Windows workstation) | One Linux machine that is not an acquisition PC | Only a file-copy task | [`docs/INSTALL_MODE_B_LINUX.md`](docs/INSTALL_MODE_B_LINUX.md), plus [`docs/INSTALL_MODE_B_WSL.md`](docs/INSTALL_MODE_B_WSL.md) for WSL2 |
| **A: instrument PC** (Windows), not recommended | The acquisition PC itself | All of STAN | [README, Mode A](README.md#mode-a--instrument-pc-windows) |

Mode A is last for a reason. At UC Davis, running searches on the acquisition PC froze a
timsTOF mid-run, and a stalled acquisition can cost the run or the sample. STAN holds
each DIA-NN and Sage search to half the cores, but the Bruker 4DFF feature finder, the
per-run PEG and drift scan and the backfill commands are not capped, and each watch folder
can run its own search at the same time.

### 1.1 Ask the human, and check what you can

| # | Question | How to check |
|---|---|---|
| Q1 | Does the lab have a SLURM account? | On the login node, run `sacctmgr -nP list assoc user=$USER format=account,partition,qos`. You need at least one `account\|partition\|qos` row. |
| Q2 | Is there shared storage that compute nodes can read and that instrument PCs (or a copy task) can write to? | Run `df -h <path>` on the login node and inside `srun --pty bash`. Ask whether the instrument PCs can reach that storage over SMB or NFS. |
| Q3 | Will someone keep a cluster install running? That means a login-node cron job and a git checkout that carries a small site patch. | Ask. |
| Q4 | Is there a Linux machine (x86_64), or a Windows workstation that can run WSL2, that is **not** an acquisition PC and can receive the raw files? | Ask. On that machine, run `uname -m`, `nproc`, `free -g` and `df -h`. |
| Q5 | Is the acquisition PC the only machine available? | Ask. |
| Q6 | For each instrument: vendor and model, the folder it saves raw files to, whether QC runs are DIA or DDA, the HeLa amount injected, and three or four real QC file names. | Ask. |

### 1.2 Decide

1. If Q1, Q2 and Q3 are all yes, use **Mode C**.
2. Otherwise, if Q4 is yes, use **Mode B**. This is the least-effort option that keeps
   searches off the instrument.
3. Otherwise, use **Mode A**, but only after you have explained the risk above and the human
   has explicitly agreed.

Tell the human which mode you chose and which answers decided it. Then read
[Step 2](#step-2-rules-for-every-mode) and follow the guide for that mode from its first
step.

There is also an older hybrid setup, in which a watcher on the instrument PC uploads to a
cluster (`processing_mode: hive`). It needs a full STAN install, SSH keys and a mapped drive
on the acquisition PC. The reference site replaced it with acquire-only PCs and a copy task.
Do not set it up for a new lab.

---

## Step 2: Rules for every mode

### 2.1 Ask the human before you

1. Choose Mode A.
2. Run anything that needs administrator rights or `sudo`. That includes MSI installers,
   package managers, system services, and share or firewall changes. On a cluster there is no
   `sudo` at all: ask the admins.
3. Install DIA-NN. It has its own license (free for academic and non-profit use), and the
   human must read and accept it. Never accept it on their behalf.
4. Type an email address or a verification code. Claiming a community name sends a 6-digit
   code from `noreply@stan-proteomics.org` to the human, and the human enters it.
5. Turn on anything that sends data off site (`community_submit`, `peg_share`,
   `error_telemetry`). Error telemetry is off unless `error_telemetry: true` is set; ask
   the human before setting it (see [2.4](#24-privacy-defaults)).
6. Install or change anything on an instrument PC. In Modes B and C, the only thing that
   belongs there is the file-copy task.
7. Make the dashboard reachable from other machines. It has no login, and its API can
   change the configuration.

### 2.2 Never

1. **Never copy the UC Davis setup into another lab's install.** These describe the
   maintainer's own facility: `CLAUDE.md`, `docs/HPC_PATHS.md`, `docs/PG_FARM*.md`,
   `docs/INSTALL_LUMOS_TIMSTOF_HIVE.md`, `scripts/install_stan_*.bat` and
   `scripts/cron_*.sh`. So do the paths `/quobyte/...`, `/nfs/lssc0/...` and `Y:\STAN`,
   the Hive cluster, "Flinders", PG Farm, and SLURM accounts such as `genome-center-grp`
   and `publicgrp`. Use them only as labelled examples.
2. **Never set `STAN_DB_BACKEND=pg`.** The Postgres backend is hard-wired to UC Davis's PG
   Farm (`PG_DEFAULTS` in `stan/db_pg.py`). Every other lab uses SQLite: leave the variable
   unset in shells, services, cron jobs and job scripts.
3. **Never install a DIA-NN other than 2.3.x, and never "latest".** See the pins in
   [2.3](#23-version-pins). The community benchmark rejects every other version. The
   Windows one-click installer keeps an installed 2.3.x or installs 2.3.2. Rows from any
   2.3.x are accepted and asset-verified, so its 2.3.2 is as good for the benchmark as the
   2.3.0 that Mode A installs by hand.
4. **Never use Python 3.13 or newer with the `peg` or `full` extras.**
5. **Never move, rename, modify or delete raw files, and never upload them anywhere.**
   STAN only reads raw files, and nothing in it needs them to leave the site. The one
   exception is a Mode C copy task renaming its own `.partial` copy (rule 6); the
   instrument's original is still never touched.
6. **Never deliver raw files the wrong way for the mode.** The two pickups need opposite
   things:
   - **Modes A and B (the `stan watch` watcher):** never copy under a temporary name
     that is renamed afterwards. The watcher reacts only to newly created files and does
     not see renames. Copy in place with `robocopy`, `cp` or `rsync --inplace`.
   - **Mode C (the `stan hive-dispatch` cron):** the dispatcher does not check that a copy
     has finished. Copy each run as `<run>.d.partial` or `<run>.raw.partial` (or into a
     staging folder on the same filesystem) and rename it when the copy is complete, or
     symlink runs that are already complete. It skips `.partial` and `.tmp` names.
7. **Never run compute on a cluster login node.** DIA-NN, Sage, 4DFF,
   ThermoRawFileParser, backfills and container builds belong in `sbatch` or `srun`.
8. **Never run `update-stan.bat` on a PC that acquires, or while `stan.bat` is running.**
   It kills STAN, reinstalls it, and starts a backfill chain that runs for hours. **Never
   run `stan install-4dff` on an acquisition PC.** After it, 4DFF runs after every Bruker
   QC run, and it is not thread-capped.
9. **Never answer the `stan setup` wizard on the human's behalf.** Its questions include
   community sharing, a claim email, a daily-report email and error reports, which are the
   human's decisions (2.1). To configure an instrument, write the complete block from the
   mode guide, or start it with
   `stan add-watch <folder> --vendor bruker|thermo --name "<model>" -y`. That writes a
   block the watcher can run (`vendor`, `extensions`, `stable_secs`, `enabled: true`,
   `qc_only` and `output_dir`), but not `lib_path`, `fasta_path`, `diann_path`,
   `sage_path` or `startup_catchup_days`, so add those by hand. `stan init` is optional: it
   creates minimal config files that do not exist yet, with every sharing option and error
   telemetry off, and never overwrites one. Its one question (fleet sync) defaults to
   `3`, None, and it takes that default when there is no terminal.
10. **Never use the UC Davis mirror and Hive commands.** These are `stan sync`,
    `sync-raw-now`, `sync-raw-backlog`, `backfill-from-dir`, `hive-upload`,
    `send-command`, `fleet-status`, `backup-now`, `ht-manifest` and `ht-watch`. The
    `-resolve-dashboard-backend` entry in `stan --help` is an internal helper, not a
    command.
11. **Never write config keys the code ignores:** `diann_binary`, `sage_binary` and
    `raw_handling`. The watcher reads the per-instrument keys `diann_path`, `sage_path`
    and `trfp_path`.
12. **Never deploy with `pip install -e ".[dev]"`.** `dev` is test tooling only; the
    runtime extras are `peg`, `thermo` and `full`. **Never switch off TLS certificate
    checking yourself.** The Windows one-click installer does this; tell IT if it is used.
13. **Never paste the `auth_token` from `community.yml`** into an issue, a chat or a commit.

### 2.3 Version pins

| Component | Use | Why |
|---|---|---|
| Python | 3.10, 3.11 or 3.12 | The `peg` and `full` extras pin `numpy<2`, whose last release has wheels only up to Python 3.12. |
| STAN | The `main` branch: `stan-proteomics[<extra>] @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip` (Mode C installs from a git clone) | There are no release tags after v1.0.0. To pin a version, install `archive/<commit-sha>.zip`, and record the SHA. |
| DIA-NN | **2.3.0**. Upstream publishes it only as a "Preview" build: `DIA-NN-2.3.0-Academia-Preview.msi` for Windows and `DIA-NN-2.3.0-Academia-Linux-Preview.zip` for Linux, both in release `2.0` of `vdemichev/DiaNN`. | The community benchmark accepts only DIA-NN 2.3.x (`PINNED_TOOL_VERSIONS` in `stan/search/community_params.py`), and marks runs made with any 2.3.x (2.3.0, 2.3.1 or 2.3.2) as having used the verified assets (`is_asset_hash_eligible_diann`). The mode guides install 2.3.0, the exact pin. The 2.3.2 that the Windows one-click installer puts in is equally good for the benchmark. |
| Sage | **v0.14.7** | Pinned in `PINNED_TOOL_VERSIONS`. The v0.14.7 binaries print `sage 0.14.6` for `--version`; that is expected. |
| ThermoRawFileParser | Downloaded by STAN on first use (1.4.5 on Windows; the .NET 8 build on Linux) | Detects DIA vs DDA for every `.raw`, and converts `.raw` to mzML for Sage. On Linux it needs .NET 8. |
| Community FASTA and libraries | `human_hela_202604.fasta`, plus `hela_timstof_202604.parquet` (Bruker) and/or `hela_orbitrap_202604.parquet` (Thermo), from release `v0.1.0-assets` of this repository | The watcher does not download them, and without them every DIA search fails. Each mode guide gives the commands and the MD5 checksums. |

Python extras:

| Extra | Use for | Adds |
|---|---|---|
| `peg` | Bruker | alphatims<1.0.9, numpy<2 and pandas<3, for Bruker PEG and DIA window drift |
| `thermo` | Thermo on Windows | `fisher_py`, for Thermo PEG and a faster TIC |
| `full` | Both vendors on Windows | Both of the above |

On Linux, Thermo PEG uses a ThermoRawFileParser container (`STAN_TRFP_SIF`) instead of
`fisher_py`. Prefer the extra to `stan install-peg-deps`: that command does not pin
`pandas<3`, and under pandas 3 Bruker PEG silently stays empty.

### 2.4 Privacy defaults

- **Error telemetry is off unless it is turned on.** STAN sends crash reports to
  `https://brettsp-stan.hf.space` only when `community.yml` has `error_telemetry: true`.
  When the key is missing, or there is no `community.yml`, nothing is sent. A report holds
  the error type and message, a traceback with file paths removed, the versions and the OS,
  and the raw file's name without its folder. The message is sent as it is, so it can
  contain paths: a failed search sends its full command line. A `community.yml` created by
  `stan init` says `false`; `stan setup` writes the answer to its error-report question,
  whose default is no. Set `true` only if the human agrees.
- **The watcher sends a keep-alive** (`GET /api/health`) to the same host at start-up and
  every 12 hours, whatever the other settings say. It carries no data. If the host is
  blocked, the watcher logs a warning and carries on.
- **Community submission and PEG sharing are off** until the lab sets `community_submit:
  true` or `peg_share: true` in `community.yml`. The watcher never submits by itself.
  Submissions are sent by `stan submit-all` (or the dashboard's Sync button), and PEG shares
  by `stan peg-sync`. Only aggregate metrics are sent.
- The config directory is `%USERPROFILE%\STAN\` on Windows and `~/.stan/` on Linux.

### 2.5 Network access

Give this list to IT if outbound traffic is filtered:

- `github.com`, `codeload.github.com`, `raw.githubusercontent.com`,
  `release-assets.githubusercontent.com` and `objects.githubusercontent.com`
- `pypi.org` and `files.pythonhosted.org`
- `www.python.org` (Mode A, if the Windows Python installer is used)
- `brettsp-stan.hf.space` (keep-alive; error telemetry, submissions and PEG shares if the
  lab opts in)

The mode guides list the extra hosts their optional parts need, for example the .NET
package sources and container registries.

---

## Mode summaries

Read the summary for the chosen mode, then follow its guide in order. Each guide ends every
step with a check.

### Mode C: SLURM cluster

Guide: [`docs/INSTALL_MODE_C_HPC.md`](docs/INSTALL_MODE_C_HPC.md).

- **How it works.** Instrument PCs copy each finished run to shared storage. Every 5–15
  minutes, a login-node cron job runs `stan hive-dispatch --config <dispatch.yml>`. That
  submits one SLURM job per new raw file, which runs `stan hive-process`. Results go to a
  SQLite database on shared storage. A lab machine serves the dashboard from it.
- **The pipeline is not yet portable.** In STAN 1.2.x, the job scripts still contain UC
  Davis values: container and binary paths, bind mounts, module names, the monitor-job
  SLURM account, and a forced `STAN_DB_BACKEND=pg`. Install from a git clone and apply the
  site patch in the guide's "Apply the site patch" step. Record every changed line for the
  report.
- **Budget time for the DIA-NN image.** No public STAN image exists. You need a DIA-NN
  2.3.0 container with .NET 8 inside (DIA-NN's zip ships a `Dockerfile`), or a bare binary
  plus a .NET 8 module. An image without .NET silently skips `.raw` files.
- **Delivery.** Each watch directory is read flat: subfolders are not searched. The
  dispatcher does not check that a copy has finished, so each run must appear under its
  final name only when complete: copy to `<run>.partial` and rename, or symlink finished
  runs from an existing archive (see the guide's
  [Phase 3](docs/INSTALL_MODE_C_HPC.md#phase-3--getting-raw-files-onto-the-cluster)).
  This is the opposite of Modes A and B.
- **Classification.** Every raw file whose name does not match `qc_pattern` gets a
  lightweight "monitor" job, which is never searched. Point the watch directories at the
  right folders.
- **Cron.** Harden the cron script as the guide shows: use `flock`, call it as
  `bash <script>`, wrap the module-profile `source` in `set +u`, write a dated log, and add
  a heartbeat. Run nothing heavy on the login node.

### Mode B: separate Linux box (native or WSL2)

Guide: [`docs/INSTALL_MODE_B_LINUX.md`](docs/INSTALL_MODE_B_LINUX.md). On a Windows
workstation, WSL2 is one way to get the Linux box: read the Linux guide first, then
[`docs/INSTALL_MODE_B_WSL.md`](docs/INSTALL_MODE_B_WSL.md) for what differs inside WSL2.

- **How it works.** Instrument PCs run only a copy task (for example, a scheduled
  `robocopy`; [`scripts/flinders_copy.ps1`](scripts/flinders_copy.ps1) is the reference
  for timsTOF). They copy into a share on the box. `stan watch` and `stan dashboard` run as
  systemd services on the box.
- **Hardware.** x86_64 only. Plan on at least 8 cores and 32 GB of RAM, plus more memory if
  several instruments can finish a QC at the same time. Each DIA-NN or Sage search takes
  half the CPUs the service may use, and the watcher stops any search after 20 minutes.
- **Delivery.** Copy each run in place under its final name, never under a temporary
  name that is renamed afterwards. The box should host the share, so that the watcher sees local writes. If
  instead it mounts someone else's share, native file events do not fire for writes made by
  another machine. The guide explains how to handle that.
- **Run `stan` by full path, or through a login shell.** `/home/stan/.stan/venv/bin/stan`
  always works. A bare `stan` works only where the guide's `PATH` line is read (login
  shells such as `sudo -iu stan <command>`).
- **Thermo** needs .NET 8 on the box.
- **The complete instrument block** is in the guide's "Configure STAN" section. It includes
  `enabled`, `output_dir`, `lib_path` and `fasta_path`.

### Mode A: instrument PC (Windows, not recommended)

Guide: [README, Mode A](README.md#mode-a--instrument-pc-windows), with reference details in
[`docs/INSTALL_REGRESSION_CHECKLIST.md`](docs/INSTALL_REGRESSION_CHECKLIST.md).

- **Get consent first.** Only after the human has accepted the risk.
- **Install scriptably.** Use Python 3.12 and a venv at `%USERPROFILE%\STAN\venv`. Install
  STAN from `main` with the vendor's extra, then DIA-NN 2.3.0 and Sage v0.14.7 by hand.
  Download the community library and FASTA. Write `instruments.yml`, then start
  `stan.bat` as the supervisor.
- **Limit the load.** Set `startup_catchup_days: 0` so the first start does not search 30
  days of old files while the instrument acquires. Do not install 4DFF. Never run
  `update-stan.bat`.
- **Updates.** `stan.bat` upgrades STAN from `main` every time it starts. It keeps the
  extras already installed, and it never updates DIA-NN or Sage.
- **The one-click installer.** `stan.bat` plus `install-stan.bat` is for a person at the
  keyboard. It is not a complete install: afterwards the guide's remaining steps are still
  needed.

---

## Success checks

Run the checks that apply to the mode. Mode C has no watcher, so skip checks 4 and 5 there.
In Mode C, prefix `stan status` and `stan test` with `STAN_DB_PATH=<db_path>`.

| # | Check | Command | Expected result |
|---|---|---|---|
| 1 | STAN runs | `stan version` | `STAN v1.x.y`. There is no `stan --version`. |
| 2 | Environment | `stan doctor` | No `BROKEN` line under "Critical compat checks". With `peg`: alphatims 1.0.8, numpy 1.26.x, pandas 2.x. |
| 3 | Search engines | Run the DIA-NN binary with no arguments; run `sage --version` | `DIA-NN 2.3.x` in the header; `sage 0.14.6` |
| 4 | Watch folders (A, B) | `stan list-watch` | Every instrument listed, with a tick under `Exists` and `Enabled` and an extension under `Extensions`. A red `none` or `no` means the watcher skips that block. |
| 5 | Watcher running (A, B) | Look in the newest `<config dir>/logs/watch_<timestamp>.log` (glob `watch_[0-9]*.log`; `stan watch-status` writes `watch_status_*.log` into the same folder) | `watcher: started <name> → <watch_dir>` for each instrument, then `Active watchers: <N>`. `No enabled instruments configured` means `enabled: true` is missing. |
| 6 | Dashboard | `curl -s http://127.0.0.1:8421/api/version` (PowerShell: `Invoke-RestMethod http://127.0.0.1:8421/api/version`) | `{"version":"1.x.y"}` |
| 7 | End to end | Wait for the next QC injection, or ask the human to copy one completed HeLa QC file into the watched folder. It must be a copy, never a move, and its name must match the QC filter. Then run `stan watch-status --days 1` (A, B), `stan status` and `stan test --n 1`. | The file shows as processed. `stan status` shows `(1 runs)` or more. The run appears on the dashboard. `stan test` lists `column_vendor` and `column_model` as broken until the lab sets them; that is expected. |

The default QC filter matches file names that contain `HeLa` or `QC` (plus the variants
listed in the mode guides). For example, `2026-09-29_HeLa_50ng_DIA.d` and `QC_60spd_01.raw`
match; `patient_042.raw` and `Blank_01.raw` do not.

If check 7 cannot run yet because no QC injection is scheduled, report it as pending. Do
not report the install as finished without it.

---

## Report back

When you stop, give the human a report under these headings:

1. **Mode and why.** The Step 1 answers that decided it.
2. **Machines.** OS, Python version, `stan version`, and the installed commit (the SHA of
   `main` at install time, or `git rev-parse HEAD` in a Mode C checkout).
3. **Search engines.** DIA-NN version and path, and Sage version and path. For Thermo, the
   .NET and ThermoRawFileParser status.
4. **Configuration.** The path of each config file (`instruments.yml`, `community.yml`,
   `dispatch.yml`). For each instrument: name, vendor, watched folder, output folder, QC
   filter and catch-up days.
5. **How STAN runs.** `stan.bat`, systemd services or cron: how to stop and restart it, and
   where the logs are.
6. **Dashboard.** The URL, and how the human reaches it from their own desk.
7. **Success checks.** Pass, fail or pending for each of checks 1–7, quoting the key line of
   output.
8. **Decisions for the human.** Error telemetry (off unless `error_telemetry: true`),
   community submission, PEG sharing, QC thresholds (none ship, so every run is gated PASS
   until the lab writes `thresholds.yml`), and starting STAN automatically.
9. **Deviations and problems.** Anything you did differently from the guides, and anything
   that failed. For Mode C, list every patched line: file, symbol, old value and new value.

---

## Getting help

Open an issue at <https://github.com/bsphinney/stan/issues>. Include:

- the mode, the OS and `stan version`;
- the output of `stan doctor`;
- the relevant lines from the watcher, cron or SLURM log;
- the instrument block or `dispatch.yml`;
- the exact command that failed, and its error.

Never include `auth_token`, and never attach raw files. If a statement in this file or in a
mode guide turned out to be wrong, quote it in the issue so that it can be fixed.
