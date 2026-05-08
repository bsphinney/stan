# STAN: One-Click Hive Install for Lumos and timsTOF HT

This guide covers setting up STAN on the `lumosRox` (Orbitrap Fusion Lumos) and
`TIMS-10878` (timsTOF HT) instrument PCs so each automatically uploads raw files
to UC Davis Hive and submits SLURM search jobs — no operator input required after
the one-time install.

Both instruments run **Mode C (HPC dispatch)**: the watcher detects a stable
acquisition, copies it to Quobyte via the mapped `Y:\` drive, then SSH-submits a
SLURM job to Hive in one step.

---

## Prerequisites

Before running the installer, confirm:

| Requirement | Lumos (`lumosRox`) | timsTOF HT (`TIMS-10878`) |
|---|---|---|
| Windows 10 or 11 | required | required |
| Internet access | required (GitHub download) | required |
| `Y:\` mapped to `\\proteomics-grp` | required | required |
| SSH key at `%USERPROFILE%\.ssh\id_ed25519` | required | required |
| SSH key permissions locked (see below) | required | required |

### Mapping `Y:\` to Quobyte

If `Y:\` is not already mapped, in Windows Explorer:

1. Right-click **This PC** → **Map network drive**
2. Drive: **Y**
3. Folder: `\\proteomics-grp` (or the exact UNC path Brett provides)
4. Check **Reconnect at sign-in**

### SSH key permissions (Windows equivalent of `chmod 600`)

OpenSSH on Windows rejects keys with broad ACLs. After placing your Hive private
key at `%USERPROFILE%\.ssh\id_ed25519`, run this once in an elevated CMD:

```cmd
icacls "%USERPROFILE%\.ssh\id_ed25519" /inheritance:r /grant:r "%USERNAME%:R"
```

This removes inherited permissions and grants read-only access to your account
only — equivalent to `chmod 600` on Linux.

---

## One-Click Install

### Lumos (`lumosRox`)

From the instrument PC, navigate to the STAN repo root (or a local copy of the
`scripts\` folder) and double-click:

```
scripts\install_stan_lumosrox.bat
```

Or run from CMD:

```cmd
scripts\install_stan_lumosrox.bat
```

### timsTOF HT (`TIMS-10878`)

```
scripts\install_stan_tims10878.bat
```

---

## What the Installer Does

Each `.bat` runs three steps. No silent writes, no surprises.

### Step 1 — Base STAN install (`install-stan.bat`)

- Downloads the latest `install_stan.ps1` from GitHub
- Finds or installs Python 3.10+
- Creates `%USERPROFILE%\STAN\venv` (the virtual environment)
- Installs the `stan-proteomics` package from `github.com/bsphinney/stan`
- Installs DIA-NN (latest Windows MSI from GitHub releases)
- Installs Sage (latest Windows release zip from GitHub)
- Adds `%USERPROFILE%\STAN\venv\Scripts` to the user PATH

### Step 2 — Hive-mode config (`configure_instruments_yml.ps1`)

Writes two files under `%USERPROFILE%\.stan\`:

**`instruments.yml`** — the watcher config. The exact block written:

For **Lumos**:
```yaml
instruments:
- name: "Orbitrap Fusion Lumos"
  vendor: thermo
  watch_dir: "E:\Data"
  extensions:
  - .raw
  stable_secs: 30
  qc_only: true
  enabled: true
  hela_amount_ng: 50.0
  community_submit: true
  column_vendor: PepSep
  column_model: ""
  monitor_all_files: true
  exclude_pattern: '(?i)(wash|blank|blnk|blk)'
  aliases:
  - auto
  - unknown
  processing_mode: hive
  hive_host: hive.hpc.ucdavis.edu
  hive_user: brettsp
  hive_venv: /quobyte/proteomics-grp/brett/stan_venv
  hive_dispatch_yml: /quobyte/proteomics-grp/STAN/dispatch.yml
  hive_upload_dir: "Y:\STAN\incoming\lumosRox"
  submit_after_upload: true
```

For **timsTOF HT**:
```yaml
instruments:
- name: "timsTOF HT"
  vendor: bruker
  watch_dir: "D:\Data"
  extensions:
  - .d
  stable_secs: 60
  qc_only: true
  enabled: true
  hela_amount_ng: 50.0
  community_submit: true
  column_vendor: PepSep
  column_model: ""
  monitor_all_files: true
  exclude_pattern: '(?i)(wash|blank|blnk|blk)'
  aliases:
  - auto
  - unknown
  processing_mode: hive
  hive_host: hive.hpc.ucdavis.edu
  hive_user: brettsp
  hive_venv: /quobyte/proteomics-grp/brett/stan_venv
  hive_dispatch_yml: /quobyte/proteomics-grp/STAN/dispatch.yml
  hive_upload_dir: "Y:\STAN\incoming\TIMS-10878"
  submit_after_upload: true
```

**`community.yml`** — only written if the file does not already exist. The stub
contains placeholder values; you must fill in `auth_token` and optionally
`display_name` before community benchmark submissions will work.

### Step 3 — Post-install checklist

The `.bat` prints a checklist of what to verify: watch_dir, Y: drive, SSH key
path, and community.yml auth_token. Read it before starting `stan watch`.

---

## First-Run Verification

Open a **new** CMD window (to pick up the updated PATH) and run:

```cmd
stan watch
```

Expected output includes:

```
Active watchers: 1
  Orbitrap Fusion Lumos  |  E:\Data  |  mode=hive
```

(or `D:\Data` / `timsTOF HT` for the timsTOF).

To confirm end-to-end dispatch, copy or acquire a HeLa `.raw` or `.d` into the
watch directory. After the stability window, you should see log lines like:

```
hive_upload_done   status=done  dest=Y:\STAN\incoming\lumosRox\...
hive_dispatch_submitted  job_id=12345678
```

On the Hive side, confirm the job is queued:

```bash
ssh hive "squeue -u brettsp -o '%.10i %.12j %.2t %R'"
```

---

## Where to Look for Problems

### Local logs

```
%USERPROFILE%\STAN\logs\
```

The watcher writes per-event log files here. Look for `watch_status_*.log` and
`backfill_*.log`.

### Hive mirror (Mac-side)

The instrument's state directory syncs to the Quobyte mount. On Brett's Mac:

```
/Volumes/proteomics-grp/STAN/lumosRox/logs/
/Volumes/proteomics-grp/STAN/TIMS-10878/logs/
```

Most recent logs first: `ls -lat /Volumes/proteomics-grp/STAN/lumosRox/logs/ | head -10`

### Per-job failures

Search failures are written to:

```
/Volumes/proteomics-grp/STAN/lumosRox/failures/
/Volumes/proteomics-grp/STAN/TIMS-10878/failures/
```

Each failure file is named after the raw file and contains the SLURM stderr.

---

## Common Gotchas

### SSH key permissions rejected by OpenSSH

**Symptom**: `stan watch` logs `hive_submit_skipped: missing_key=...` or SSH exits
with `Permissions for '...\id_ed25519' are too open`.

**Fix**: run the `icacls` command shown in the Prerequisites section above.

### `Y:\` drive goes offline (F: or network drop)

The upload step will fail with a path error. The watcher logs `hive_upload_failed`
and does NOT retry automatically. When the drive comes back, run:

```cmd
stan hive-upload --raw "E:\Data\your_file.raw"
```

to manually upload + dispatch the missed file.

### NTFS mtime drift (Thermo `.raw`)

Some Xcalibur versions write a final mtime update several seconds after the last
data byte. The default `stable_secs: 30` for the Lumos accounts for this. Do not
reduce it below 30 s.

### Bruker `.d` stability (timsTOF HT)

A Bruker `.d` is a **directory**, not a single file. HyStar writes multiple
sub-files (`.tdf`, `.tdf_bin`, `analysis.baf`, etc.) simultaneously. STAN
measures the **total directory size** every 10 s and only triggers after
`stable_secs: 60` s of no change. It does not use `mtime` (which changes on
every sub-file write). Do not reduce `stable_secs` below 60 s for Bruker
instruments.

### Watcher does not pick up files in `F:\data` or another drive

Check `%USERPROFILE%\.stan\instruments.yml` and confirm `watch_dir` matches the
actual acquisition path. Edit the file and restart `stan watch` — the watcher
hot-reloads config every 30 s, but a restart is faster.

### Community submissions not appearing on the leaderboard

Confirm `auth_token` in `%USERPROFILE%\.stan\community.yml` is non-empty and
matches the token Brett assigned to the lab. Token is validated server-side; a
mismatch produces a 401 in the SLURM job log.

---

## Key File Locations

| File | Purpose |
|---|---|
| `%USERPROFILE%\.stan\instruments.yml` | Watcher config (watch_dir, hive settings) |
| `%USERPROFILE%\.stan\community.yml` | Auth token, display name, email reports |
| `%USERPROFILE%\.stan\stan.db` | Local SQLite mirror (dashboard reads this) |
| `%USERPROFILE%\STAN\venv\Scripts\stan.exe` | STAN executable |
| `%USERPROFILE%\.ssh\id_ed25519` | Hive SSH private key |
| `Y:\STAN\incoming\lumosRox\` | SMB upload staging (Lumos) |
| `Y:\STAN\incoming\TIMS-10878\` | SMB upload staging (timsTOF HT) |
