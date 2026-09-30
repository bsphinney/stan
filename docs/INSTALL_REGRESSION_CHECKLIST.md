# STAN Mode A: install reference and checklist (Windows instrument PC)

> **Audience**: an AI agent, or a person, who is installing or checking STAN
> on a Windows acquisition PC. The install steps themselves are in the
> README, under [Mode A — instrument PC (Windows)](../README.md#mode-a--instrument-pc-windows).
> This page holds what those steps link to: what the Windows scripts really
> do, every config key, sharing, load, verification and troubleshooting.
>
> Mode A is the fallback. Running searches on an acquisition PC has frozen
> UC Davis's timsTOF, so use [Mode B](INSTALL_MODE_B_LINUX.md) (a separate
> Linux box) or [Mode C](INSTALL_MODE_C_HPC.md) (a SLURM cluster) when you
> can. In both, the instrument PC runs only a file-copy task. The rules
> and version pins shared by every mode are in
> [`INSTALL_FOR_AGENTS.md`](../INSTALL_FOR_AGENTS.md).
>
> The code still has an older hybrid in which a watcher on the instrument PC
> uploads each raw file to a cluster (per-instrument `processing_mode: hive`).
> **Do not set it up for a new lab.** It keeps a full STAN install, SSH keys
> and a mapped drive on the acquisition PC, and the reference site replaced
> it with acquire-only PCs and a copy task. See
> [Mode C, Appendix C](INSTALL_MODE_C_HPC.md#appendix-c--other-slurm-code-paths-do-not-use-for-a-new-install).
>
> Everything here was checked against the code on `main` on 2026-09-29.
> Where a script and this page disagree, the script wins; please open an
> issue.

---

## 1. What the Windows scripts do

| Script | What it does | Use it? |
|---|---|---|
| `stan.bat` | Daily launcher and supervisor (details below). | Yes. This is how STAN should run. |
| `install-stan.bat` → `install_stan.ps1` | One-click installer for a person at the keyboard (details below). | Only for a person. An agent should follow the README steps. |
| `update-stan.bat` → `update_stan.ps1` | Kills every `stan.exe`, force-reinstalls `main`, installs `fisher_py`, and installs `alphatims<1.0.9` (with no numpy or pandas pin) when the `instruments.yml` STAN reads (`%USERPROFILE%\STAN\instruments.yml`, else the legacy `%USERPROFILE%\.stan` one) has `vendor: bruker`; it also drops a byte-order mark an older installer wrote to that file. Checks DIA-NN and Sage the same way the installer does (steps 7 and 8 below): it keeps an installed DIA-NN 2.3.x or installs 2.3.2, and installs Sage v0.14.7 unless that binary is already present. Then it launches `start_stan_loop.bat` (or `stan watch`) and `stan dashboard`, and opens a "STAN overnight backfill" console. That console runs `install-4dff`, `fix-spds`, `backfill-metrics`, `derive-cirt-panel --auto`, `backfill-cirt`, `backfill-tic --force --push`, `backfill-peg`, `backfill-features` and `backfill-window-drift --force`. | **No** on a PC that acquires. Never run it next to `stan.bat`, because both launch the watcher and the dashboard. |

**`stan.bat`**

- It looks for `%USERPROFILE%\STAN\venv\Scripts\stan.exe`. If that is
  missing, it tries `%USERPROFILE%\.stan\venv\Scripts\stan.exe`.
- **If `stan.exe` is missing**, it pauses and then calls `install-stan.bat`.
  It looks for the installer in its own folder first, then in
  `%USERPROFILE%\Downloads`. If the installer is in neither place, it
  downloads it from `main` on GitHub into its own folder. If the download
  fails, or what comes back is not the installer (a proxy or sign-in page),
  it deletes the file, prints `ERROR: could not download install-stan.bat`
  and exits. After the install it re-reads `PATH` from the registry, so the
  watcher it starts finds the search engines the installer just added.
- **Every launch**:
  1. It downloads `main`'s `stan.bat`. If that copy differs from itself, it
     replaces itself and relaunches.
  2. It writes `%USERPROFILE%\STAN\update_pending.flag`.
  3. It starts `stan dashboard` in a window titled "STAN Dashboard". It
     opens no browser.
  4. It enters a loop. While the flag exists, it kills that window and every
     `stan.exe`, and runs
     `pip install --upgrade "stan-proteomics @ …/main.zip"`. That adds no
     extras, but extras you already installed stay. Then it relaunches the
     dashboard, runs `stan watch`, waits 5 s after the watcher exits, and
     loops.
- Apart from running the installer on a PC with no STAN, it never installs
  or updates DIA-NN, Sage, `fisher_py` or `alphatims`.
- It works from the folder it was started in. With no `output_dir`, search
  output lands in that folder.

**`install-stan.bat` → `install_stan.ps1`**

1. It deletes any `install_stan.ps1` next to it, downloads a fresh one from
   `main` with TLS certificate checks turned off, and runs it.
2. It asks the operator to type `yes` to accept the DIA-NN license. Any
   other answer exits.
3. It turns off TLS certificate validation for the rest of the run, and
   passes `--trusted-host` to pip for `pypi.org`, `files.pythonhosted.org`,
   `github.com` and `objects.githubusercontent.com`.
4. **[1/8] Python.** It uses the first `python`, `python3` or `py` (on
   `PATH` or in a known folder) that reports 3.10 or newer, and that includes
   3.13 and 3.14. If there is none, it installs 3.12.4 per-user with
   `PrependPath=1`.
5. **[2/8]** It creates `%USERPROFILE%\STAN\venv`, or keeps the one that is
   already there.
6. **[3/8]** It runs `pip install --force-reinstall` on the `main.zip`
   archive, with **no extras**. No git and no checkout are involved.
7. **[4/8] DIA-NN.** It lists every `DiaNN.exe` on `PATH` and under the
   usual install folders, with its version (from the folder name, or from
   the header DIA-NN prints). It keeps an installed 2.3.0 if there is one,
   otherwise the newest one whose major.minor is 2.3, the only line the
   benchmark accepts. If there is none, it downloads
   `DIA-NN-2.3.2-Academia.msi` from release `2.0`, refuses it unless its
   sha256 matches the pinned value, and installs it alongside anything
   already there. It installs 2.3.2, not 2.3.0, because upstream ships 2.3.0
   for Windows only as a Preview MSI. The benchmark accepts 2.3.2 rows and
   marks them asset-verified, exactly as it does 2.3.0 rows. It then puts the
   chosen DIA-NN's folder first on the user `PATH`. If a copy on the system
   `PATH` would still win, it says so and prints a `diann_path` line to add
   to each block. Finally it asks the installed STAN which `DiaNN.exe`
   `stan baseline` would search with, and prints a warning if that is a
   different file. Since v1.2.6 `stan baseline` picks by the same rule
   (2.3.0 first, then the newest 2.3.x), so the two normally agree.
8. **[5/8] Sage.** It uses a `sage.exe` whose sha256 matches the v0.14.7
   release binary, first on `PATH`, then under
   `%USERPROFILE%\STAN\tools\sage`. Otherwise it downloads
   `sage-v0.14.7-x86_64-pc-windows-msvc.zip`, checks the zip and the
   `sage.exe` inside it against pinned sha256 values, and unpacks it to
   `%USERPROFILE%\STAN\tools\sage\sage-v0.14.7-x86_64-pc-windows-msvc\`.
   That folder goes first on the user `PATH`, with the same system-`PATH`
   warning (`sage_path`).
9. **[6/8]** It makes sure an `instruments.yml` exists. It keeps one that
   STAN already reads (removing a byte-order mark an older installer wrote),
   or creates `%USERPROFILE%\.stan\instruments.yml` containing
   `instruments: []`. It no longer writes `diann_binary` or `sage_binary`.
   The first `stan setup` or `stan add-watch` copies that file's content
   into `%USERPROFILE%\STAN\instruments.yml` and writes there from then on.
10. **[7/8]** It skips `stan init`, because step 9 just created the file it
    checks for.
11. **[8/8]** It adds `venv\Scripts` to the user `PATH` and removes the old
    `.stan\venv\Scripts` entry.
12. It downloads `install-stan.bat`, `update-stan.bat` and `start_stan.bat`
    next to itself, then asks "Run 'stan setup' now? (Y/n)".

**`stan setup`** is a wizard with six numbered questions:

1. the watch folder. It then asks for the vendor only when the raw files
   already in the folder cannot tell it (`.d` folders mean Bruker, `.raw`
   files Thermo), for the instrument name (the default is the model read
   from a raw file there, otherwise `auto`, which means "read it from the
   first raw file"), and for the QC filename filter (the default pattern, a
   custom regex, or every file);
2. the LC column;
3. the HeLa amount in ng;
4. whether to join the community benchmark. The default is Yes, which
   generates a pseudonym and emails a 6-digit claim code;
5. a daily or weekly QC email. The default is Yes;
6. error reports. The default is No. The question says that a report holds
   the error message (which can include file paths), the raw file's name,
   and the STAN, Python and OS versions, and no patient data.

It writes a block the watcher can run: `name`, `vendor`, `watch_dir`,
`extensions`, `stable_secs`, `enabled: true`, `qc_only` (plus `qc_pattern`
for a custom regex), `output_dir` (unless the block already has one:
`<config>\qc_output\<name>`, using the watch folder's name when the name
is `auto`), `hela_amount_ng` and the column fields. Run again on the same
folder, it updates that folder's block instead of adding a second one. It does **not** write `lib_path`, `fasta_path`, `diann_path`,
`sage_path` or `startup_catchup_days`. It writes `community_submit`,
`error_telemetry` and, when the lab joins, `display_name` and `auth_token`
to `community.yml`. Missing config files are created first, as `stan init`
does. It then offers `stan baseline` and asks whether to start STAN.
Because it asks for email addresses and sharing decisions, it is for the
human to run, not an agent.

**`stan init`** creates a minimal `instruments.yml` (no instruments), an
empty `thresholds.yml` (no gates) and a `community.yml` with every sharing
option and `error_telemetry` set to `false`, but only for a file that does
not exist yet. It never overwrites one, including a legacy
`%USERPROFILE%\.stan\` copy. It then runs a fleet-sync wizard whose default
is `3`, None; Enter, or no input at all, takes it. The answer goes to
`fleet.yml`, which nothing reads yet. Neither install path needs
`stan init`.

---

## 2. Where things live

| | Windows | Linux / macOS |
|---|---|---|
| Config folder | `%USERPROFILE%\STAN\` | `~/.stan/` |
| Legacy fallback | `%USERPROFILE%\.stan\`. STAN reads a file here only when that file is missing from `STAN\`. | none |
| venv (README steps) | `%USERPROFILE%\STAN\venv` | your choice |
| Database | `<config>\stan.db` (SQLite). `STAN_DB_PATH` overrides it. | same |
| Logs | `<config>\logs\`: `watch_<ts>.log`, `doctor_<ts>.log`, `submit_all_<date>.jsonl`, `peg_sync_<ts>.jsonl`, `backfill_peg_<ts>.jsonl` | same |
| Tools | `<config>\tools\ThermoRawFileParser\`, `<config>\tools\sage\` (README steps), `<config>\bruker_ff\` (4DFF) | same |
| Search assets | `<config>\community_assets\` (README step 4, and `stan baseline`) | same |

A single-lab install uses SQLite. Leave `STAN_DB_BACKEND` unset: `pg` is
only for a central Postgres like UC Davis's PG Farm, and
`stan dashboard --backend auto` (the default) falls back to SQLite when it
finds no Postgres credentials.

---

## 3. `instruments.yml` keys

The file has a top-level `instruments:` list with one block per watch
folder. It can also hold a `hive:` block, which only the legacy cluster
paths in the last two rows of the table below use. Leave it out.
Adding, removing, enabling or disabling a block takes effect within about
30 s. **Changing a key in an existing block needs a watcher restart**: close
and reopen `stan.bat`. Save the file as ASCII or UTF-8 without a BOM, and put
single quotes around Windows paths.

| Key | Default | Meaning |
|---|---|---|
| `name` | required | A stable label. Runs are stored under it, so renaming it later splits the history. |
| `vendor` | none, so set it | `bruker` or `thermo`. It selects mode detection, stability rules and search parameters. |
| `watch_dir` | required | The folder the instrument writes to. It is watched recursively. |
| `extensions` | `[]` | `['.d']` or `['.raw']`. If this is empty, every file is ignored. |
| `enabled` | `false` | The watcher starts only blocks with `true`. |
| `stable_secs` | `60` | Seconds of unchanged size before a file counts as finished. Use 60 for Bruker and 30 for Thermo. |
| `qc_only` | `true` | Search only file names that match `qc_pattern`. |
| `qc_pattern` | HeLa/QC regex (below) | A regex matched against the file name. |
| `exclude_pattern` | none | A regex. Matching files are skipped entirely, for example washes and blanks. |
| `monitor_all_files` | `false` | Also scans every non-QC acquisition into Sample Health: TIC, plus PEG and drift on Bruker. This adds load. |
| `output_dir` | none | Each run's search output goes to `<output_dir>\<run name>\`. **Always set it**, or output lands in whatever folder the watcher started in. |
| `lib_path` | `<config>\instrument_library.parquet` if it exists | The DIA-NN spectral library. See [section 5](#5-search-engines-library-and-thermo-readers). |
| `fasta_path` | the FASTA bundled in the venv | The FASTA for DIA-NN and Sage. |
| `diann_path` | `diann` on `PATH` | The full path to `DiaNN.exe`. |
| `sage_path` | `sage` on `PATH` | The full path to `sage.exe`. |
| `trfp_path` | downloaded automatically | ThermoRawFileParser. Thermo only. |
| `forced_mode` | auto-detect | `dia` or `dda`. Skips DIA/DDA detection for the whole folder. |
| `search_mode` | `local` | `community` uses the frozen community parameters and expects the library and FASTA in `<output_dir>\_community_assets\`. |
| `keep_mzml` | `false` | Keeps the mzML that is converted from `.raw` before a Sage search. |
| `startup_catchup_days` | `30` | At start-up, searches QC files from the last N days that are not in the database yet. `0` turns this off. |
| `hela_amount_ng` | `50` | The injected amount. It is part of the community cohort. |
| `column_vendor`, `column_model` | none | The LC column. STAN cannot read it from raw files. |
| `spd` | none | Last-resort samples-per-day. STAN reads SPD from the raw file, so leave this unset unless that fails. |
| `execution_mode` | `local` | Leave it unset. `slurm` is a legacy path in which the watcher submits each search to a cluster over SSH. New cluster installs use Mode C's dispatcher instead. |
| `processing_mode` | `local` | Leave it unset. `hive` is the legacy upload-to-cluster hybrid described at the top of this page. Do not use it for a new lab. |

The default `qc_pattern` matches names that contain `HeLa`, `Hela5`, `He1`,
`QC` or `std_he`, ignoring case:

```
(?i)(he(l[_\-\s]?[a5\d]|[_\-\s]?\d)|qc|std[_\-\s]?he)
```

Example `exclude_pattern`: `'(?i)(wash|blank)'`.

`stan add-watch <dir> --vendor bruker|thermo --name <label> -y` writes
`name`, `vendor`, `watch_dir`, `extensions`, `stable_secs`, `enabled: true`,
`qc_only` (plus `qc_pattern` if you pass `--qc-pattern`) and `output_dir`
(`<config>\qc_output\<label>`, spaces turned into `_`). Without `--name` the
name is `<folder>_<vendor>`. It writes to the `instruments.yml` STAN reads,
so if only the one-click installer's `%USERPROFILE%\.stan\instruments.yml`
exists, it edits that one: create `%USERPROFILE%\STAN\instruments.yml` with
`instruments: []` first. On a folder that already has a block it changes
nothing that is set, but adds `vendor`, `extensions`, `stable_secs`,
`enabled` and `output_dir` where an older version left them out.
`stan list-watch` shows *Enabled* and *Extensions* columns and marks a block
that is missing either in red.

**`community.yml` keys** (in the same folder):

| Key | Meaning |
|---|---|
| `display_name` | The lab's pseudonym on the community boards. |
| `auth_token` | Proof that this install owns the name. `stan setup` or `stan community-claim` writes it. The relay keeps one token per name. |
| `community_submit` | When `true`, `stan submit-all` may send runs. `stan setup` writes it from question 4, and the dashboard Community **Sync** button sets it to `true`. Older versions of `stan setup` put it in the `instruments.yml` block instead, where submissions do not read it. STAN never turns that old answer into consent: submissions stay off, and a one-time warning says to set `community_submit: true` in `community.yml`. |
| `peg_share` | When `true`, `stan peg-sync` may send PEG results. |
| `error_telemetry` | Error reports. Sent only when `true`; **off when missing** (since v1.2.6). A `community.yml` created by `stan init` says `false`; `stan setup` writes its question 6 answer (default no). |
| `email_reports` | Daily or weekly QC email settings, written by `stan setup`. |

---

## 4. Choosing the Python and the extras

| Extra | Installs | Needed for |
|---|---|---|
| none | the core | Searches, metrics and the dashboard |
| `peg` | `alphatims>=1.0,<1.0.9`, `numpy<2`, `pandas<3` | Bruker PEG and DIA-window drift |
| `thermo` | `fisher_py>=2.0` (through pythonnet and .NET) | Thermo PEG, and a fast Thermo TIC |
| `full` | `peg` + `thermo` | Both vendors |
| `dev` | pytest, pytest-asyncio, ruff, mypy | Development only; installs no runtime extras |

- Use **Python 3.10, 3.11 or 3.12** with `peg` or `full`. `numpy<2` has no
  wheels for 3.13 or newer, so pip tries to build numpy from source and
  fails. The one-click installer accepts any 3.10+ it finds, including
  3.13. If the venv came out as 3.13+, delete it and recreate it with 3.12.
- Each pin guards a real break. alphatims 1.0.9 fails with polars 1.35+.
  alphatims 1.0.8 fails with numpy 2. pandas 3 shifts every alphatims frame
  window, and STAN then refuses the data, so PEG stays empty.
  `stan install-peg-deps` pins alphatims and numpy but **not** pandas, so
  prefer the extra.
- Install an extra into an existing venv with
  `<venv>\Scripts\python.exe -m pip install "stan-proteomics[peg] @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip"`.
  Later `stan.bat` upgrades keep it.

---

## 5. Search engines, library and Thermo readers

| Tool | Pinned | Windows download | Notes |
|---|---|---|---|
| DIA-NN | 2.3.0 | [`DIA-NN-2.3.0-Academia-Preview.msi`](https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.3.0-Academia-Preview.msi) | The benchmark rejects anything other than 2.3.x. 2.3.1 and 2.3.2 are accepted too, and their rows are asset-verified like 2.3.0 rows, so the installers' 2.3.2 is equally good. Free for academic use; commercial use needs a license. |
| Sage | 0.14.7 | [`sage-v0.14.7-x86_64-pc-windows-msvc.zip`](https://github.com/lazear/sage/releases/download/v0.14.7/sage-v0.14.7-x86_64-pc-windows-msvc.zip) | `sage.exe --version` prints `sage 0.14.6`: the release binary carries the old version string. |
| ThermoRawFileParser | 1.4.5 | Downloaded on first use to `<config>\tools\ThermoRawFileParser\` | A .NET Framework build. Linux uses 2.0.0-dev (net8, needs `dotnet` 8). It does not run on macOS. |

- **Which DIA-NN counts.** For DIA runs, STAN records the DIA-NN version
  from the search output. For DDA runs, `stan submit-all` asks whichever
  `diann` is first on `PATH`. Keep 2.3.x first on `PATH` even on a
  DDA-only instrument. If no DIA-NN is found, the version is "unknown" and
  the submission is rejected.
- **Library and FASTA, in order of use** (for the default
  `search_mode: local`):
  1. `lib_path` from the block;
  2. `<config>\instrument_library.parquet`, which `stan baseline` followed by
     `stan build-library` produces. This file is used for **every**
     instrument on the PC, whatever its vendor, so on a PC with both vendors
     set `lib_path` per block;
  3. otherwise the search falls back to community mode, which expects files
     in `<output_dir>\_community_assets\`. Nothing downloads them, so the
     search fails.

  The FASTA defaults to the copy bundled in the venv
  (`<venv>\community_fasta\human_hela_202604.fasta`).
- **Community assets.** Download them from
  `https://github.com/bsphinney/stan/releases/download/v0.1.0-assets/<file>`,
  or from the Hugging Face dataset `brettsp/stan-benchmark`
  (`community_library/` and `community_fasta/`).

  | File | MD5 |
  |---|---|
  | `human_hela_202604.fasta` | `8de1d9bd0a052b175f88f66f82500d92` |
  | `hela_timstof_202604.parquet` | `ad72bfb2730644c69147ba8f34bfe982` |
  | `hela_orbitrap_202604.parquet` | `ac84e40f5b2f23e1286f28a7baeccec2` |
- **Thermo `.raw` on Windows** needs two readers:
  - **ThermoRawFileParser** tells DIA from DDA on every `.raw`, reads the
    metadata, and converts to mzML before a Sage search. If it is
    unavailable, detection returns "unknown" and the run is searched as DIA.
    Set `forced_mode: dda` on a DDA-only folder.
  - **`fisher_py`** (the `thermo` extra) reads MS1 spectra in-process
    through pythonnet and .NET, for PEG and the fast TIC. Without it, Thermo
    PEG stays empty and the TIC falls back to ThermoRawFileParser. Test it
    against a real file:
    `<venv>\Scripts\python.exe -c "from fisher_py import RawFile; RawFile(r'D:\Data\<a QC>.raw'); print('ok')"`.
  - The container route (`STAN_TRFP_SIF`, `STAN_APPTAINER`) needs apptainer,
    so it is Linux-only.

---

## 6. Load on an acquiring PC

| Work | Capped? | What you can do |
|---|---|---|
| DIA-NN search | Yes: `max(2, cores/2)` threads | Nothing more to do |
| Sage (DDA) search | Yes: the same `max(2, cores/2)`, passed to Sage as `RAYON_NUM_THREADS` (a `RAYON_NUM_THREADS` you set yourself wins) | Nothing more to do |
| PEG + drift scan after each QC run | **No**: about 1–2 min per Bruker `.d` | Runs only when the `peg` or `thermo` extra is installed |
| Bruker 4DFF after each QC run | **No**: 1–5 min per `.d` | Do not run `stan install-4dff` on this PC |
| Catch-up at start-up | Searches every unprocessed QC file from the last 30 days | Set `startup_catchup_days: 0` |
| Several watch folders | One search at a time per folder, but folders run in parallel | Use fewer blocks |
| `monitor_all_files: true` | TIC, PEG and drift on every non-QC file | Leave it `false` |
| `update-stan.bat` | Starts a backfill chain that runs for hours | Do not run it |
| `stan baseline`, `backfill-*` | Search or scan whole folders | Run them only when the instrument is idle |
| Keep-awake | `stan watch` stops Windows from sleeping | `stan watch --no-keep-awake` (`stan.bat` does not pass it) |

---

## 7. Sharing data off-site

**Do nothing in this section without the human's explicit yes.** Every item
sends data to the STAN relay at `https://brettsp-stan.hf.space`. The public
boards are at <https://community.stan-proteomics.org>.

### 7.1 Community benchmark

**What is sent:** aggregate QC metrics and run metadata for each QC run.
That includes the instrument, the SPD, the amount, the column, the DIA-NN
version, a 128-bin TIC trace, the **raw file's name** and the acquisition
date. No raw data and no sample metadata are sent. The client sends the file
name because the relay's completeness check currently rejects rows without
one (see the comment in `stan/community/submit.py`). Setting
`STAN_STRIP_RUN_NAME=1` blanks it. Tell the human, and rename QC files if
their names carry anything sensitive.

1. **Claim a name.** Put `display_name: <the name the human chose>` in
   `community.yml`, then have the **human** run `stan community-claim`. It
   asks for their email and sends a 6-digit code from
   `noreply@stan-proteomics.org`; tell them to check spam. The code is typed
   by the human, not by you. It writes `auth_token`. (`stan setup` question
   4 does the same thing and generates a pseudonym.) Re-claiming a name
   invalidates the token on every other machine that uses it.
   **Check:** `stan verify` shows the name and a valid token.
2. **Enable submission.** Add `community_submit: true` to `community.yml`.
3. **Preview, then send.** Run `stan submit-all --dry-run`, then
   `stan submit-all`. **Check:** `<config>\logs\submit_all_<date>.jsonl`
   has a line for each run. Rows appear on the public board after the next
   nightly consolidation.
4. **Schedule it.** The watcher never submits on its own, so schedule
   submission with the task below. The dashboard's Community **Sync** button
   also submits, and it sets `community_submit: true` (minting an unclaimed
   pseudonym if there is no name), so treat pressing it as consent.

### 7.2 PEG Watch board

**What is sent:** each run's PEG measurements under an anonymous hash. Run
names never leave the lab. See [`PEG_WATCH.md`](PEG_WATCH.md).

1. PEG needs the `peg` extra (Bruker) or the `thermo` extra (Thermo). New QC
   runs get PEG automatically. Run `stan backfill-peg` for older runs.
   **Check:** `stan backfill-peg --force --limit 1 --verbose` recomputes one
   run. A yellow line that names a missing reader (for example
   `fisher_py not installed`) means the extra is missing.
2. Claim a name, as in 7.1 step 1. Without a token the sync still sends,
   but it warns, and the relay answers HTTP 403 if someone else holds the
   name.
3. Preview with `stan peg-sync --dry-run`, which works while sharing is off.
4. Add `peg_share: true` to `community.yml` (or set `STAN_PEG_SHARE=1`), then
   run `stan peg-sync`. **Check:** it exits 0 and writes
   `<config>\logs\peg_sync_<ts>.jsonl`.

### 7.3 Scheduling both

UC Davis's reference cron runs `stan submit-all` and then `stan peg-sync`
every 6 hours. On Windows:

```powershell
$S = "$env:USERPROFILE\STAN"
Set-Content "$S\community_sync.bat" -Encoding ASCII -Value '@echo off', '"%USERPROFILE%\STAN\venv\Scripts\stan.exe" submit-all', '"%USERPROFILE%\STAN\venv\Scripts\stan.exe" peg-sync'
schtasks --% /Create /TN "STAN community sync" /SC HOURLY /MO 6 /TR "\"%USERPROFILE%\STAN\community_sync.bat\""
```

**Check:** `schtasks /Query /TN "STAN community sync"` shows a next run
time. `schtasks /Run /TN "STAN community sync"` writes new `submit_all_*` and
`peg_sync_*` logs. Leave out the line for whichever sharing the human
declined.

### 7.4 Traffic that needs no opt-in, and error reports

- **Error reports are off unless `community.yml` has `error_telemetry: true`.**
  Set it only with the human's yes. When it is on and a search fails, STAN
  sends the error type and message (which can contain a file path), a
  traceback with file paths stripped, the STAN, Python and OS versions, and
  the raw file's name without its folder.
- **Health ping.** At start-up and every 12 hours the watcher calls the
  relay's `/api/health`, whatever the sharing and telemetry settings are.
  It sends no data. If the host is blocked, the watcher logs a warning and
  carries on.
- **Email reports** (`stan setup` question 5) send QC summaries to the
  address given, through Resend (`api.resend.com`). Since 1.2.7 they need the
  lab's own Resend API key: `resend_api_key:` in `community.yml` or the
  `RESEND_API_KEY` environment variable. Without one, sending fails with
  "No Resend API key configured"; STAN no longer ships a built-in key.
- **The `Y:\STAN` mirror.** If a `Y:\STAN` folder exists (the UC Davis
  share convention), STAN copies `stan.db`, its config files (secrets
  redacted), logs and baseline reports into `Y:\STAN\<hostname>\`.
  `HIVE_MIRROR_DIR`, or `hive_mirror_dir` in `community.yml`, points it
  somewhere else. Check `Test-Path Y:\STAN`, and tell the human if it is
  True.
- **Dashboard.** It listens on `127.0.0.1:8421`. If Tailscale is logged in,
  it binds `0.0.0.0` so that the tailnet can reach it; the Windows firewall
  still decides. The page loads scripts from `cdn.jsdelivr.net` and
  `cdn.plot.ly`.

---

## 8. QC gating (`thresholds.yml`)

No `thresholds.yml` ships with STAN, so **every run is gated PASS and the
HOLD flag never fires**. The dashboard colours runs by IPS instead. If the
lab wants gating, write `<config>\thresholds.yml` using the schema in
[`STAN_MASTER_SPEC.md`](../STAN_MASTER_SPEC.md): search for
"thresholds.yml structure". It has a top-level `thresholds:` key, then
`default` or a model name, then `dia` or `dda`, then keys such as
`n_precursors_min` and `ips_score_min`. Background:
[`qc_gating_and_slack_summary.md`](qc_gating_and_slack_summary.md).

---

## 9. Post-install verification (10 checks)

Run these in a **new** PowerShell window, so that the updated `PATH` loads.
`$S` is `%USERPROFILE%\STAN`.

- [ ] **1. STAN runs.** `stan version` prints `STAN v<version>`, the same
  version as in `pyproject.toml` on `main`. There is no `stan --version`.
- [ ] **2. Environment.** `stan doctor` shows Python 3.10–3.12, the extras
  you chose (Bruker: alphatims 1.0.8, numpy 1.26.x, pandas 2.x; Thermo:
  fisher_py), no "BROKEN" line, and `instruments.yml` present under the
  config folder.
- [ ] **3. Right config file.** `Test-Path "$S\instruments.yml"` is True.
  If `%USERPROFILE%\.stan\instruments.yml` also exists, STAN ignores it; do
  not edit it.
- [ ] **4. Complete blocks.** The `load_instruments` one-liner from README
  step 5 prints `True`, a vendor, an extension list and an `output_dir` for
  every block.
- [ ] **5. DIA-NN 2.3.** Run the `diann_path` executable with no arguments:
  its header shows `DIA-NN 2.3.x` (any 2.3.x gives asset-verified rows). The
  first line of `where.exe diann` is the same file.
- [ ] **6. Sage.** Running the `sage_path` executable with `--version`
  prints `sage 0.14.6` (the v0.14.7 binary).
- [ ] **7. Library.** `Test-Path` is True for `lib_path` and `fasta_path`,
  and `Get-FileHash -Algorithm MD5` matches [section 5](#5-search-engines-library-and-thermo-readers).
- [ ] **8. Watcher.** The newest `$S\logs\watch_[0-9]*.log` contains
  `Active watchers: N`, where N is your number of blocks.
- [ ] **9. Dashboard.** `Invoke-RestMethod http://127.0.0.1:8421/api/version`
  returns the version from check 1, and the page opens in Chrome or Edge.
- [ ] **10. First QC run.** `stan watch-status --days 1` shows the file as
  matched and in `runs`. `stan test --n 1` lists the filled fields.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ERROR: could not download install-stan.bat` | `stan.bat` could not fetch the installer from `raw.githubusercontent.com`, or a proxy answered instead | Save `install-stan.bat` next to `stan.bat` by hand, or ask IT to allow the host. |
| `stan.exe still not found … Check %TEMP%\stan_install.log` | Nothing writes that log | Run `install-stan.bat` from a `cmd` window and read its output; then run `stan doctor`. |
| `Missing closing }` at install start | A non-ASCII character in `install_stan.ps1` (regression of the 2026-05-08 bug 1) | Do not retry. Open an issue with the full output. |
| `Active watchers: 0` / "No enabled instruments configured" | A block is missing `enabled: true` (`stan list-watch` shows `no` under *Enabled*) | Add it, or run `stan add-watch <its folder> --vendor bruker\|thermo -y`, which adds only the missing keys. |
| The watcher runs, but new files are ignored | The block has no `vendor` or `extensions` (for example, one written by an older `stan setup`), or the name does not match `qc_pattern` | Add the missing keys with `stan add-watch <its folder> --vendor bruker\|thermo -y`, or fix `qc_pattern`, then restart; check with `stan watch-status --days 1`. |
| A Bruker `.d` is never processed | It was already complete when it appeared (a fast copy or a move), so no growth was seen | Restart `stan.bat` with `startup_catchup_days` > 0, or wait for a real acquisition. |
| A DIA search fails, and the log mentions `_community_assets` | There is no library | Do README step 4 and set `lib_path`. |
| Output in an odd folder (next to `stan.bat`) | No `output_dir` | Set it. |
| YAML error such as `expected '<document start>'` | The file was saved with a BOM, or as UTF-16 | Re-save it as ASCII (`Set-Content -Encoding ASCII`) or UTF-8 without a BOM. |
| Edits to an existing block have no effect | Hot reload only adds, removes, enables and disables blocks | Restart `stan.bat`. |
| `community_submit is not enabled in community.yml` | Nobody has opted in: `stan init` writes `false`, and `stan setup` writes `true` only when question 4 is answered yes | Set it in `community.yml` (with consent). |
| `Submission rejected: DIA-NN version mismatch` | DIA-NN is not 2.3.x. DDA rows read the first `diann` on `PATH` | Install 2.3.0 and put it first on `PATH`. |
| HTTP 403 from `stan peg-sync` | The name is claimed and this install has no current token | Run `stan community-claim`. |
| Bruker PEG or drift is empty | alphatims is missing, or numpy 2 or pandas 3 is installed | Reinstall with the `peg` extra on Python 3.10–3.12. |
| Thermo PEG is empty | `fisher_py` is missing or cannot load .NET | Install the `thermo` extra; test it with the one-liner in section 5. |
| `pip install` of `peg` tries to build numpy and fails | The venv is Python 3.13+ | Recreate the venv with 3.12. |
| A Thermo DDA run is searched as DIA | ThermoRawFileParser is unavailable, so detection returned "unknown" | Check it with the `ensure_installed` one-liner (README step 3), or set `forced_mode: dda`. |
| `ModuleNotFoundError: No module named 'stan'` after an update | pip ran while a `stan.exe` was still running | Close every STAN window, then rerun the pip install from README step 2. |
| The dashboard page is blank | It was opened in Internet Explorer, or `cdn.jsdelivr.net` or `cdn.plot.ly` is blocked | Use Chrome or Edge, and allow those hosts. |
| `stan --help` lists `-resolve-dashboard-backend` | An internal helper registered as a command | Ignore it. |

---

## 11. Updating, pinning, uninstalling

- **Updating.** Relaunching `stan.bat` is the update: it moves STAN to the
  head of `main`. It keeps your extras but never adds new ones, and it never
  touches DIA-NN or Sage. Change the search engines only on purpose, to the
  pinned versions, and update `diann_path` and `sage_path` when you do.
- **Pinning.** `main` has no release tags after `v1.0.0`, and `stan.bat`
  always upgrades to `main`. To hold a version, install from a commit
  archive
  (`"stan-proteomics[peg] @ https://github.com/bsphinney/stan/archive/<commit>.zip"`)
  and start `stan dashboard` and `stan watch` yourself instead of through
  `stan.bat`. You then lose its restart-on-crash.
- **Uninstalling.**
  1. Close the `stan.bat` and "STAN Dashboard" windows. Remove the scheduled
     task with `schtasks /Delete /TN "STAN community sync" /F`.
  2. Remove `%USERPROFILE%\STAN\venv\Scripts`, and any DIA-NN or Sage folder
     you added, from the user `PATH`.
  3. Delete `%USERPROFILE%\STAN\venv` and `%USERPROFILE%\STAN\tools`. Keep
     `stan.db`, the YAML files and `logs` if the QC history matters, or
     delete all of `%USERPROFILE%\STAN`. `%USERPROFILE%\.stan\` is left over
     from the one-click installer and can go too.
  4. Uninstall DIA-NN from Settings → Apps if nothing else uses it.
  5. Rows already submitted, and a claimed name, stay on the relay. Ask the
     maintainer (see the README) to remove them.

  **Check:** in a new window, `where.exe stan` finds nothing.

---

## 12. Network access

| Host | When | Why |
|---|---|---|
| `raw.githubusercontent.com` | Install; every `stan.bat` launch | Installer scripts; `stan.bat` self-update |
| `github.com`, `codeload.github.com` | Install; every `stan.bat` launch | The STAN package archive (`main.zip`) |
| `github.com`, `release-assets.githubusercontent.com` | Install; first Thermo file | DIA-NN, Sage, ThermoRawFileParser, the community library and FASTA |
| `www.python.org` | Install | The Python installer |
| `pypi.org`, `files.pythonhosted.org` | Install; updates | Python packages |
| `brettsp-stan.hf.space` | Runtime | The 12-hour health ping; also error reports and sharing, if the human opted in |
| `cdn.jsdelivr.net`, `cdn.plot.ly` | Viewing the dashboard | Page scripts, loaded by the browser |
| `huggingface.co` | Only if you fetch assets from the HF dataset | Community library and FASTA |
| `api.resend.com` | Only if email reports are on | QC emails |

**TLS.** The one-click installer and `update-stan.bat` turn off TLS
certificate validation for their downloads and pass `--trusted-host` to
pip. They do this so that they work behind proxies that intercept TLS. The
README steps keep validation on. If those downloads fail with certificate
errors, ask IT to allow the hosts above rather than turning validation off.

---

## 13. Known defects in the Windows scripts

These are the reasons the README steps look the way they do. Each one is
reported for a code fix.

1. Neither `stan setup` nor `stan add-watch` writes `lib_path`, and nothing
   downloads the community library for the watcher, so every DIA search
   fails until README step 4 is done and `lib_path` is set (or `stan
   build-library` has written `<config>\instrument_library.parquet`).
2. The one-click installer and `stan.bat` install no extras.
   `update_stan.ps1` and `stan install-peg-deps` do not pin `pandas<3`.
3. `scripts/test_fresh_install.bat` calls `stan --version`, which does not
   exist, and its check passes on the error text. It also passes DIA-NN 2.7.
4. `stan verify --help` says it offers to re-verify by email. It does not;
   use `stan community-claim`.

---

## 14. History: the 2026-05-08 UC Davis install

The reference install below is UC Davis's `lumosRox` instrument, using
`scripts/install_stan_lumosrox.bat`. That wrapper is a **UC Davis hybrid**,
not a template for other labs. It configures `processing_mode: hive`, maps
`Y:` to the Quobyte share, and installs an SSH key from
`Y:\STAN\temp_keys\`. The install was meant to be one click, and it hit six
bugs:

| # | Bug | Symptom | Fixed in | Status on 2026-09-29 |
|---|-----|---------|----------|----------------------|
| 1 | Em-dashes in `install_stan.ps1` that PowerShell 5.1 misread | `Missing closing }` near line 406; the install aborts | 4da0f4a | Fixed. It would show up again as the same error. |
| 2 | A cached `install-stan.bat` kept running an old PS1 | The install ran outdated code | 9a18279 | Fixed. `install-stan.bat` deletes `install_stan.ps1` before each download. |
| 3 | `stan init` piped to `Out-Null` hid its prompt | Step 7 hung silently | 96236b3 | Moot. The installer no longer runs `stan init` (section 1). |
| 4 | The DIA-NN picker sorted alphabetically (1.8.1 over 2.3.2) | DIA-NN 1.x was used; the benchmark rejected rows | 96236b3 | Fixed. The installer now keeps an installed 2.3.x, or installs the sha256-pinned 2.3.2 (section 1). |
| 5 | `configure_instruments_yml.ps1` wrote to `.stan\` while the watcher read `STAN\` | Hive mode never activated | c5bf09a | Fixed in that script. The generic `install_stan.ps1` still writes to `.stan\` (section 13). |
| 6 | `install_stan_lumosrox.bat` reinstalled STAN when it was already present | 3+ extra minutes; risk of clobbering the venv | 96236b3 | Fixed. It skips the install when `stan.exe` exists. |
