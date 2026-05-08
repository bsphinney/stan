# STAN Mode C — SLURM Cluster (HPC) Deployment Guide

> **Version**: v0.2.347  
> **Related docs**: [Mode A — Instrument PC](../README.md) · [Mode B — WSL2 Lab Box](INSTALL_MODE_B_WSL.md) · [HPC Paths Reference](HPC_PATHS.md) · [CLAUDE.md Hive section](../CLAUDE.md)

---

## TL;DR

Mode C deploys STAN on a SLURM-managed HPC cluster. The instrument PC (Mode A) copies finished raw files to a shared storage path visible from compute nodes; a cron-driven dispatcher (`stan hive-dispatch`) scans that path, submits one SLURM job per raw file, and each job runs the full search + QC pipeline. **Because every HPC cluster is bespoke** — different partition names, QOS+account triples, container runtimes, filesystem types — this guide does not give you a fixed install script. Instead, it walks you through three phases: (1) collect your cluster's specifics in about 10 minutes, (2) paste a self-contained tailoring prompt into your AI coding agent (Claude Code, Cursor, Aider, or similar) which generates a `dispatch.yml`, bootstrap script, and smoke test matched to your cluster, and (3) eyeball the output against a short review checklist before committing. Expect 1–3 hours for a first setup on an unfamiliar cluster.

---

## When to Use Mode C

```
Do you have an HPC account with SLURM access?
  ├─ No  ──→ Do you have a beefy lab workstation (≥32 cores)?
  │              ├─ Yes ──→ Mode B (WSL2) — docs/INSTALL_MODE_B_WSL.md
  │              └─ No  ──→ Mode A (Instrument PC) — README.md
  └─ Yes ──→ Mode C (this doc)
                  └─ Multiple instruments or high daily sample counts?
                       ├─ Yes ──→ Mode C is ideal
                       └─ No  ──→ Mode A or B may be simpler
```

Mode C is the right choice when:
- You already have an HPC allocation and want to offload DIA-NN/Sage compute from instrument PCs.
- You run many instruments in parallel and want one shared SQLite database and one dashboard.
- You need DDA and DIA searches to run simultaneously without tying up local hardware.

---

## Prerequisites

Before starting Phase 1, confirm all of these:

- **SSH access to the cluster login node**, plus a working `~/.ssh/config` alias (e.g. `Host hive`) so `ssh hive` works without typing a password. Cluster-managed SSH keys are fine; password auth works but is inconvenient for cron.
- **Shared storage path visible from compute nodes** — a path that exists on both the login node and inside SLURM job environments. Common filesystems: Lustre (`/lustre/...`), GPFS/Spectrum Scale (`/gpfs/...`), BeeGFS (`/beegfs/...`), NFS (`/nfs/...`), Quobyte (`/quobyte/...`). Home directories (`~/`) are often NOT visible from compute nodes on large clusters — verify before assuming.
- **Container runtime available on compute nodes**: Apptainer ≥1.0 or Singularity ≥3.x (most academic HPC). Docker is available on some cloud-burst clusters. Bare-metal install (static DIA-NN binary + Sage binary) works on clusters that forbid containers, but requires more manual setup — flag this in Phase 2.
- **Python 3.10+ accessible on the login node** — either system Python or via `module load python/3.x`. STAN's venv is created once on shared storage and sourced by every SLURM job.
- **At least one valid SLURM partition + QOS + account triple** for your user. Verify with:
  ```bash
  sacctmgr -nP list assoc user=$USER format=account,partition,qos
  ```
  If you have no output, contact your cluster admin — you cannot submit jobs without an authorized triple.

---

## Phase 1 — Run These Commands on Your Cluster

Run each block on the cluster login node. Save all output — you will paste it into the Master Prompt in Phase 2. The AI needs real output, not your guesses.

### 1.1 — Partition / QOS / Account triples

```bash
sacctmgr -nP list assoc user=$USER format=account,partition,qos
```

**What this tells the AI:** the exact set of `(account, partition, qos)` triples you are authorized to use. The AI must pick one for routine STAN community searches and one fallback for when the primary quota is exhausted.

Example output (UC Davis Hive):
```
genome-center-grp|high|genome-center-grp-high-qos
genome-center-grp|gpu-a100|genome-center-grp-gpu-a100-qos
publicgrp|high|publicgrp-high-qos
publicgrp|low|publicgrp-low-qos
```

Each `|`-delimited row is one valid triple. The AI should **never mix a QOS from one row with an account from another** — that produces `sbatch: error: Invalid qos specification`.

---

### 1.2 — Partition summary

```bash
sinfo -s
```

**What this tells the AI:** which partitions exist, their state, node counts, and whether they appear healthy. Useful for confirming the partition name from 1.1 is correct and for spotting drained/down partitions to avoid.

Example output:
```
PARTITION AVAIL  TIMELIMIT   NODES(A/I/O/T) NODELIST
high         up 7-00:00:00       12/4/0/16  cn[001-016]
low          up 7-00:00:00       60/20/0/80  cn[017-096]
gpu-a100     up 2-00:00:00        1/1/0/2   gpu[001-002]
```

---

### 1.3 — Full partition details (time limits + CPU caps)

```bash
scontrol show partition
```

**What this tells the AI:** the `MaxTime`, `MaxNodes`, `MaxCPUsPerUser`, and `DefaultTime` for each partition. STAN's DIA-NN jobs need 6–8 hours and 8–32 CPUs. If your `high` partition has a 4-hour wall-time limit, the AI must use a different partition or split the resource profile.

Look for lines like:
```
PartitionName=high MaxTime=7-00:00:00 MaxCPUsPerUser=64 ...
```

---

### 1.4 — Available Python modules

```bash
module avail python 2>&1 | tr ' ' '\n' | grep -i '^python'
```

**What this tells the AI:** which Python versions are available via the module system. STAN requires Python 3.10+. The bootstrap script will need `module load python/X.Y.Z` before creating the venv. If nothing appears, run `module spider python` as an alternative.

Example output:
```
python/3.10.4
python/3.11.9
python/3.12.2
```

Pick the highest 3.11.x or 3.12.x available — STAN's CI targets 3.10+ and newer is fine.

---

### 1.5 — Container runtime

```bash
module avail apptainer singularity 2>&1
which apptainer 2>/dev/null || which singularity 2>/dev/null || echo "no container runtime on PATH"
apptainer --version 2>/dev/null || singularity --version 2>/dev/null || true
```

**What this tells the AI:** whether Apptainer or Singularity is available, and its version. Apptainer ≥1.0 and Singularity ≥3.8 both work with `.sif` container images. If neither is present, the AI must flag this and use bare-binary install instead (DIA-NN's static Linux binary + Sage's Rust binary).

Example output:
```
apptainer/1.2.5
apptainer version 1.2.5
```

---

### 1.6 — Shared storage probe

```bash
df -h /quobyte /scratch /home /lustre /gpfs /beegfs /project /work 2>/dev/null | head -30
```

**What this tells the AI:** which shared filesystems exist, their sizes, and their mount points. The AI needs to pick one for: (a) the STAN venv, (b) the SQLite database, (c) DIA-NN/Sage search outputs, (d) SLURM stdout/stderr logs, and (e) the watch directory where instrument PCs drop raw files.

Also run:
```bash
ls -ld /scratch/$USER /project/$USER /work/$USER 2>/dev/null
```

to check if per-user subdirectories already exist.

Example output:
```
Filesystem      Size  Used Avail Use% Mounted on
quobyte         200T   80T  120T  40% /quobyte
/dev/sda1        50G   30G   20G  60% /home
```

---

### 1.7 — Scheduler version

```bash
which sbatch && sbatch --version
```

**What this tells the AI:** the SLURM version. Some `#SBATCH` directives differ between 20.x and 23.x. Most are stable; this is a sanity check.

---

### 1.8 — Base OS on compute nodes

```bash
cat /etc/os-release | grep PRETTY_NAME
# Also check a compute node if you can:
srun --partition=<your-partition> --account=<your-account> \
     --time=00:02:00 --cpus-per-task=1 --mem=1G \
     cat /etc/os-release 2>/dev/null | grep PRETTY_NAME || true
```

**What this tells the AI:** whether compute nodes run RHEL/Rocky/AlmaLinux (yum/dnf) or Ubuntu/Debian (apt). This matters for system package installation in the bootstrap script and for DIA-NN's `.NET 8 SDK` dependency path (see Appendix B).

---

## Phase 2 — The Master Prompt

Copy everything between the `BEGIN MASTER PROMPT` and `END MASTER PROMPT` markers below (including the cluster output you collected in Phase 1) and paste it into Claude Code, Cursor, Aider, or your preferred AI coding agent. The agent will generate a tailored `dispatch.yml`, bootstrap script, and smoke test for your cluster.

---

```
===== BEGIN MASTER PROMPT =====

You are helping me configure STAN (Standardized proteomic Throughput ANalyzer) for
my SLURM HPC cluster. STAN is a proteomics QC tool that dispatches DIA-NN and Sage
mass-spec search jobs via SLURM and stores results in a SQLite database.

## Your goal

Produce four artifacts tailored to my cluster:
1. `<cluster>_dispatch.yml`         — STAN's Hive-side dispatcher config
2. `bootstrap_<cluster>.sh`         — Idempotent install script (run once on the login node)
3. `instruments.yml snippet`        — The instrument block(s) to add to ~/.stan/instruments.yml
                                       on each instrument PC
4. `submit_smoke_test.sh`           — A minimal sbatch smoke test (1 CPU, 1 GB, 5 min,
                                       runs `/bin/echo "hello stan" && stan version`)

## What success looks like

- `bootstrap_<cluster>.sh` runs to completion with no errors, creates the STAN venv on
  shared storage, and prints the installed `stan version`.
- `submit_smoke_test.sh` submits a SLURM job that completes successfully and prints
  "hello stan" plus the STAN version in its output log.
- A real DIA-NN or Sage search (dispatched via `stan hive-dispatch`) produces a populated
  row in the `runs` table of `stan.db`.

## Files to read BEFORE generating anything

Read these files from the STAN repo (https://github.com/bsphinney/stan) before writing
any config or scripts. Do not invent structure — derive it from the actual code:

1. `stan/community/scripts/dispatch_hive.py`
   Pay special attention to `DEFAULT_CONFIG_TEMPLATE` (the canonical dispatch.yml
   template), `DEFAULT_CONFIG_PATH`, and the `_MONITOR_SLURM` dict (monitor job profile).

2. `scripts/hive_bootstrap.sh`
   This is the working Hive (UC Davis) bootstrap. Use it as your structural template,
   adapting paths, module names, and partition triples for my cluster.

3. `stan/pipeline/hive_process.py` and `stan/pipeline/hive_steps.py`
   These run inside SLURM jobs. Understanding the expected environment (venv on PATH,
   shared storage for outputs, apptainer available) shapes your bootstrap.

4. `CLAUDE.md` — the "HPC: Hive (UC Davis)" section
   Contains the authoritative Hive partition triples, SSH ControlMaster recipe, and
   the rules of engagement. Use the Hive setup as your reference template, then adapt.

5. `docs/HPC_PATHS.md`
   Container paths, FASTA locations, storage layout for Hive. Shows the pattern to
   replicate for my cluster.

## Anti-invention rules (CRITICAL — read before writing anything)

- **Do NOT invent partition names, QOS values, or account names.** Use only the triples
  I provide from `sacctmgr` output. If uncertain which triple to use as default, ask me.
- **Do NOT invent container paths.** Ask me where my cluster's Apptainer `.sif` files
  live, or whether I need to pull the DIA-NN container from a registry.
- **Do NOT assume the shared storage path.** Use only the mount points I show from `df`.
- **Do NOT assume the Python module name.** Use only the module name I show from
  `module avail python`.
- **Do NOT assume the package manager** (apt vs dnf/yum vs neither). Use only the OS
  I provide from `cat /etc/os-release`.
- If any required value is missing from my cluster output, **ask me a specific question**
  rather than guessing.

## Artifact specifications

### 1. `<cluster>_dispatch.yml`

Model this on the `DEFAULT_CONFIG_TEMPLATE` constant in `dispatch_hive.py`. Required keys:
- `db_path`           — absolute path on shared storage for stan.db
- `out_root`          — absolute path for DIA-NN/Sage search outputs
- `sbatch_log_dir`    — absolute path for SLURM stdout/stderr (NEVER /tmp — job-node-local)
- `dispatch_log_dir`  — absolute path for dispatcher JSONL logs
- `stan_venv`         — absolute path to the Python venv created by the bootstrap
- `slurm:`            — resource block with partition/qos/account/time/cpus/mem
- `max_submissions_per_run` — 50 is a safe default
- `qc_pattern`        — regex for QC filename matching (default: `(?i)(he(l[a5\d]|\d)|qc|std[_\-\s]?he)`)
- `max_attempts`      — 3
- `instruments:`      — list of instrument blocks (one per mass spec being watched)

The `slurm:` block MUST use a valid (partition, qos, account) triple from my `sacctmgr`
output. QOS is bound to account — mixing them causes `Invalid qos specification`.

### 2. `bootstrap_<cluster>.sh`

Model this on `scripts/hive_bootstrap.sh`. Required sections:
- `set -euo pipefail`
- Source the cluster's module system (`/etc/profile.d/modules.sh` or equivalent)
- `module load python/<version>` (use the version I provide)
- Create the venv on shared storage if it does not already exist
- `pip install --upgrade` STAN from GitHub:
  `"stan-proteomics @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip"`
  Use `--upgrade` NOT `--force-reinstall` (the force-reinstall flag triggers rename
  failures on distributed filesystems like Quobyte and Lustre).
- `pip install 'alphatims>=1.0,<1.0.9' 'numpy<2'` for PEG + drift (idempotent)
- Print `stan version` to confirm install
- `mkdir -p` all required directories (db dir, out_root, sbatch_log_dir, dispatch_log_dir,
  incoming dirs per instrument)
- Write `dispatch.yml` via `stan hive-dispatch --print-default-config` only if the file
  does not already exist (idempotent)
- **DIA-NN**: call `pull_diann_sif()` — pulls the pre-built STAN container image
  (`docker://registry.hf.space/brettsp-stan-proteomics:latest`) via `apptainer pull`
  on a compute node (CPU-intensive; mirrors the DE-LIMP `hpc_setup.sh` pattern).
  The pull is non-fatal if the image is not yet published. On Hive, the existing
  container at `/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif` is already present
  and referenced in `dispatch.yml` — the pull step adds the STAN-packaged image as a
  future upgrade path. **You do not need to install DIA-NN manually.**
- **Sage**: call `download_sage_linux()` — downloads the latest Sage release tarball
  from GitHub, extracts the static binary to `<STAN_BASE>/sage/sage`, and writes
  `sage_binary` into `dispatch.yml`. **You do not need to install Sage manually.**

Do NOT use `sudo` — the script runs as the user on the login node.
Do NOT write outputs to `/tmp` — node-local, invisible after the job ends.
Do NOT `pip install --force-reinstall` — distributed-filesystem rename bug.

### 3. `instruments.yml snippet`

This goes in `~/.stan/instruments.yml` on each instrument PC (the Windows boxes attached
to the mass specs). The relevant keys for Mode C:
```yaml
instruments:
  - name: "<instrument name>"
    watch_dir: "C:/Data/<instrument>"
    extensions: [".d"]           # or [".raw"] for Thermo
    vendor: bruker               # or thermo
    processing_mode: hive
    hive_host: "<ssh alias for my cluster login node>"
    hive_upload_dir: "<shared storage incoming path for this instrument>"
    submit_after_upload: true    # trigger dispatch after upload
```
Generate one block per instrument I mention.

### 4. `submit_smoke_test.sh`

A minimal SLURM script that verifies the venv + module load + scheduler triple all work:
```bash
#!/bin/bash
#SBATCH --job-name=stan-smoke
#SBATCH --partition=<partition>
#SBATCH --qos=<qos>
#SBATCH --account=<account>
#SBATCH --time=00:05:00
#SBATCH --cpus-per-task=1
#SBATCH --mem=1G
#SBATCH --output=<sbatch_log_dir>/stan-smoke-%j.out

source /etc/profile.d/modules.sh  # adapt if different on my cluster
module load python/<version>
source <stan_venv>/bin/activate

/bin/echo "hello stan"
stan version
```
Use 1 CPU, 1 GB, 5 minutes — the lightest possible job that still exercises the
environment stack. Fill in the SBATCH directives from my sacctmgr output.

## My cluster specifics

[PASTE YOUR PHASE 1 OUTPUT HERE — one section per command]

### sacctmgr output (partition/QOS/account triples)
```
<paste output of: sacctmgr -nP list assoc user=$USER format=account,partition,qos>
```

### sinfo -s output
```
<paste output of: sinfo -s>
```

### scontrol show partition (relevant partitions only)
```
<paste key lines including MaxTime, MaxCPUsPerUser>
```

### Python modules available
```
<paste output of: module avail python 2>&1 | tr ' ' '\n' | grep -i '^python'>
```

### Container runtime
```
<paste output of: module avail apptainer singularity 2>&1>
<paste: which apptainer OR which singularity>
<paste: apptainer --version OR singularity --version>
```

### Shared storage
```
<paste output of: df -h /quobyte /scratch /home /lustre /gpfs /beegfs /project /work 2>/dev/null>
<paste output of: ls -ld /scratch/$USER /project/$USER /work/$USER 2>/dev/null>
```

### Base OS
```
<paste output of: cat /etc/os-release | grep PRETTY_NAME>
<paste srun OS probe output if you ran it>
```

### Additional context (fill in what you know)
- Cluster name / hostname: ___
- Number of instruments sending raw files here: ___
- Instrument types (Bruker timsTOF, Thermo Exploris, Thermo Lumos, etc.): ___
- DIA-NN .sif container path on the cluster (if already present, skip the bootstrap pull): ___
  (Leave blank — `hive_bootstrap.sh` pulls it automatically via `pull_diann_sif()`)
- Sage binary path on the cluster (if already present, skip the bootstrap download): ___
  (Leave blank — `hive_bootstrap.sh` downloads it automatically via `download_sage_linux()`)
- Do you want community benchmark submissions enabled? (yes/no): ___

===== END MASTER PROMPT =====
```

---

## Phase 3 — Human Review Checklist

Before committing the AI's output, verify each item manually. Do not skip this — the AI may produce a plausible-looking config that quietly mismatches your cluster.

**Scheduler triples**
- [ ] Open `<cluster>_dispatch.yml`. Find `slurm: partition / qos / account`.
- [ ] Confirm all three values appear together in the same row of your `sacctmgr` output.
- [ ] Do the same for any `_MONITOR_SLURM`-style block if the AI added a separate monitor job profile.

**Storage paths**
- [ ] Every path in `dispatch.yml` (`db_path`, `out_root`, `sbatch_log_dir`, `dispatch_log_dir`, `stan_venv`) lives on shared storage — NOT on `/tmp`, NOT on `~/` unless your cluster explicitly mounts home on compute nodes.
- [ ] Verify those paths are writable from a compute node:
  ```bash
  srun --partition=<partition> --account=<account> --time=00:02:00 \
       --cpus-per-task=1 --mem=1G \
       bash -c "ls -ld <path> && touch <path>/.write_test && rm <path>/.write_test"
  ```

**Wall-time headroom**
- [ ] The `time:` in `dispatch.yml` is comfortably below the partition's `MaxTime`.
  DIA-NN on a timsTOF HeLa QC raw typically needs 2–4 hours at 8 CPUs.
  Sage on a DDA raw typically needs 30–90 minutes at 8 CPUs.
  Use the `scontrol show partition <name>` output from Phase 1 to confirm.

**Bootstrap script assumptions**
- [ ] The `module load python/X.Y.Z` line matches the version you found in Phase 1.
- [ ] The package manager in any OS-level install steps (`apt-get` vs `dnf` vs `yum`) matches your `cat /etc/os-release` output.
- [ ] The script does NOT contain `sudo` — it runs as your user on the login node.
- [ ] The script does NOT write to `/tmp` — node-local and invisible post-job.

**DIA-NN container / binary**
- [ ] `hive_bootstrap.sh` calls `pull_diann_sif()` automatically — check the bootstrap log
  for `"DIA-NN .sif installed"` or the non-fatal warning if the image is not yet published.
  On Hive, the existing container at `/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif`
  is already present and used by `dispatch.yml` — the bootstrap pull is additive.
- [ ] If the `.sif` was pulled, verify it exists:
  ```bash
  ssh hive "ls -lh /quobyte/proteomics-grp/STAN/containers/diann.sif"
  ```
- [ ] If using Apptainer, confirm the `.sif` file (not `.simg`) — Apptainer ≥1.0 requires `.sif`.
- [ ] If you use Thermo `.raw` files, confirm the DIA-NN container has `.NET 8 SDK` bundled.
  The Hive container at `/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif` does;
  the lookalike at `apptainers/diann2.3.0.sif` (no underscore) does NOT. See Appendix B.

**Sage binary**
- [ ] `hive_bootstrap.sh` calls `download_sage_linux()` automatically — check the bootstrap
  log for `"Sage installed: /quobyte/proteomics-grp/STAN/sage/sage"`.
- [ ] Verify the binary is present and executable:
  ```bash
  ssh hive "/quobyte/proteomics-grp/STAN/sage/sage --help 2>&1 | head -3"
  ```
- [ ] Confirm `dispatch.yml` has `sage_binary` set to the correct path:
  ```bash
  ssh hive "grep sage_binary /quobyte/proteomics-grp/STAN/dispatch.yml"
  ```

**DIA-NN download URL (if bare-binary install)**
- [ ] If the bootstrap script downloads DIA-NN directly (no container), verify the URL
  still resolves — DIA-NN releases roll forward and old URLs 404. Check:
  `https://github.com/vdemichev/DiaNN/releases/latest`
  Pin to a specific `2.x` release tag in the URL.

**Smoke test**
- [ ] Run `submit_smoke_test.sh`:
  ```bash
  bash submit_smoke_test.sh
  ```
- [ ] Check it submits (`Submitted batch job <N>`).
- [ ] Verify it completes:
  ```bash
  squeue -u $USER   # wait for it to leave the queue
  cat <sbatch_log_dir>/stan-smoke-<N>.out
  ```
  The output must contain "hello stan" and a STAN version number.

---

## Reference: Hive's Working Config (UC Davis)

The following is the verbatim "HPC: Hive (UC Davis)" section from `CLAUDE.md`. This is the gold-standard reference that the AI's Master Prompt is modelled against. When reviewing the AI's output, ask: "does this look structurally like the Hive setup below, adapted for my cluster?"

---

### HPC: Hive (UC Davis)

- Host: `hive.hpc.ucdavis.edu` (user `brettsp`, SSH alias `hive`)
- Scheduler: SLURM
- DIA-NN, Sage, 4DFF, etc. run as SLURM batch jobs
- SQLite database lives on Hive scratch/project storage
- Dashboard API can be SSH-tunneled to local machine

**Hive rules of engagement — violate at your peril:**

1. **Never run compute on the login node (`login1`)**. CPU/memory-heavy work gets flagged.
   Always use `sbatch` for real work or `srun --pty` for interactive. The dispatcher
   (`stan hive-dispatch`) only walks the filesystem and calls `sbatch` — it is login-node-safe.
   Actual DIA-NN/Sage search runs exclusively inside SLURM jobs.

2. **Never use `~/` or `/home/brettsp/` for large artifacts** — the home quota is tight
   and others can't see it. All shared binaries, FASTA files, analysis outputs, and
   generated `.features` live under `/quobyte/proteomics-grp/...`. Brett's personal
   scratch dir is `/quobyte/proteomics-grp/brett/` — writable + visible to the lab.

3. **SLURM commands need module environment loaded**. Non-interactive
   `ssh hive "sbatch ..."` won't find `sbatch` on PATH. Either:
   - `ssh hive "bash -l -c 'sbatch ...'"` (login shell), or
   - `ssh hive "source /etc/profile.d/modules.sh && source /etc/profile.d/hpccf.sh && sbatch ..."`

4. **Partitions + QOS + account** (each row is a valid `sbatch` triple):

   | Partition | QOS | Account | Use |
   |---|---|---|---|
   | `high` | `genome-center-grp-high-qos` | `genome-center-grp` | **Default for STAN community searches.** Priority CPU; 64-CPU per-user cap. |
   | `high` | `publicgrp-high-qos` | `publicgrp` | Open-access alternative when genome-center is capped. |
   | `gpu-a100` | `genome-center-grp-gpu-a100-qos` | `genome-center-grp` | 1 A100, use for Casanovo inference/training. |
   | `low` | `publicgrp-low-qos` | `publicgrp` | Preemptible, huge capacity. Fine for fast (<30 min) jobs. `Requeue=1` recommended. |

   QOS is bound to an account — passing `--qos=genome-center-grp-high-qos`
   without `--account=genome-center-grp` returns
   `sbatch: error: Batch job submission failed: Invalid qos specification`.
   Brett's default account is `publicgrp`, so genome-center jobs MUST set
   `--account=genome-center-grp` explicitly.

   When `high` shows `(QOSGrpCpuLimit)` as the reason, fall back to `low`
   — different quota, usually works. List allowed combinations with:
   ```bash
   sacctmgr -nP list assoc user=brettsp format=account,partition,qos
   ```

5. **Check queue state** with:
   ```bash
   squeue -u brettsp -o '%.10i %.12j %.9P %.2t %.10M %.6C %.8m %R'
   ```
   Look for the REASON column — `(None)` means just waiting for scheduler,
   `(QOSGrpCpuLimit)` / `(QOSGrpGRES)` mean quota is capped.

6. **SSH ControlMaster** speeds up repeated invocations (instrument PC → cluster):
   ```bash
   ssh -o ControlMaster=auto -o ControlPath=/tmp/.stan_brettsp_hive \
       -o ControlPersist=300 brettsp@hive.hpc.ucdavis.edu "<cmd>"
   ```
   macOS/Windows socket path must be short — keep `ControlPath` under `/tmp/` not
   under a long user home path (macOS socket paths max at 104 bytes).

**Hive container paths** (from `docs/HPC_PATHS.md`):

| Container | Path | Notes |
|-----------|------|-------|
| DIA-NN 2.3 (with Thermo .raw) | `/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif` | Has .NET runtime, reads .raw + .d + .mzML |
| DIA-NN 2.3 (Bruker only, NO .raw) | `/quobyte/proteomics-grp/apptainers/diann2.3.0.sif` | Missing dotnet — `.raw` silently skipped |
| msconvert (ProteoWizard) | `/quobyte/proteomics-grp/apptainers/pwiz-skyline-i-agree-to-the-vendor-licenses_latest.sif` | |

**DIA-NN binary inside any container**: `/diann-2.3.0/diann-linux` (NOT just `diann`).

Run command:
```bash
apptainer exec --bind /quobyte:/quobyte \
  /quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif \
  /diann-2.3.0/diann-linux [flags]
```

**Sage binary** (bare, no container needed):
`/quobyte/proteomics-grp/de-limp/cascadia/sage-v0.14.7-x86_64-unknown-linux-gnu/sage`

**FASTA** (human HeLa, used for community benchmark):
`/quobyte/proteomics-grp/MRS/UP000005640_9606.fasta`

---

## Common Cluster Types — Gotchas Glossary

### SLURM with Lustre filesystem

- Lustre enforces **file lock limits** — hundreds of simultaneous open handles to the same directory can cause `OSError: [Errno 11] Resource temporarily unavailable`. Avoid running dispatcher cron more frequently than every 15 minutes on a busy mount.
- **Small-file performance is poor** on Lustre. STAN's SQLite writes are fine (single file, sequential). The `pip install --upgrade` step during bootstrap creates many small files — it can be slow (5–15 minutes) the first time. Do not be alarmed.
- Use `--upgrade` not `--force-reinstall` in pip: force-reinstall's internal rename pattern can fail on distributed filesystems with `OSError [Errno 2] No such file or directory: ...INSTALLER<rand>.tmp`.

### SLURM with Apptainer-only (no Docker)

- Require `.sif` image format — Apptainer ≥1.0 dropped support for the old `.simg` format. If someone gives you a `.simg`, convert it: `apptainer build new.sif old.simg`.
- Pull once on the login node, store on shared storage, reference the absolute path in jobs. Do not pull inside SLURM jobs — it hammers the container registry and may hit rate limits.
- Bind mounts use `--bind <host_path>:<container_path>`. The Hive pattern `--bind /quobyte:/quobyte` maps the entire Quobyte mount; adapt to your filesystem.

### SLURM on AWS (ephemeral compute)

- Compute nodes are torn down after the job ends — `/tmp` on the compute node is **gone** when you look for it. All outputs must land on shared storage (EFS, FSx for Lustre, S3-backed filesystem). STAN enforces this in `dispatch.yml` but double-check your `out_root` and `sbatch_log_dir` are not on a local path.
- The login node may also be ephemeral on ParallelCluster setups — do not put the venv on the login node's local disk. Use the shared EFS mount.

### PBS/Torque masquerading as SLURM

- Some clusters (particularly older academic HPC) run PBS/Torque with a SLURM-compatible shim (`sbatch` exists but `scontrol`, `sacctmgr` may not). Run `scontrol ping` — if it returns `Slurmctld(primary) Version: 23.x...` you have real SLURM. If it hangs or errors, you may have a shim.
- PBS job directives use `#PBS -q <queue>` not `#SBATCH --partition`. If you have PBS, Mode C as documented does not apply — contact your cluster admin about SLURM availability or consider Mode B instead.

### Container-forbidden clusters

- Some clusters (particularly those handling sensitive data, e.g. medical/patient data) forbid Apptainer/Singularity/Docker entirely for security reasons.
- In this case, install DIA-NN's static Linux binary directly:
  1. Download from `https://github.com/vdemichev/DiaNN/releases/latest` — pin a specific `2.x` release.
  2. Place on shared storage (e.g. `/shared/apps/diann/diann-2.3.0/diann-linux`).
  3. Install the `.NET 8 SDK` on compute nodes (or confirm it is a cluster module) — required for Thermo `.raw` reading even with the bare binary. See Appendix B.
  4. In `dispatch.yml`, point `diann_binary` at the bare path instead of an Apptainer exec command.
- Sage is a single Rust binary with no container dependency — download from `https://github.com/lazear/sage/releases/latest` and place on shared storage.
- Flag this situation in the Master Prompt so the AI skips the Apptainer invocation pattern.

---

## Troubleshooting

### `sbatch: error: Batch job submission failed: Invalid qos specification`

**Cause:** QOS value does not match the account in the same `sbatch` call. QOS+account is a bound pair — you cannot mix them across rows in `sacctmgr` output.

**Fix:** Re-run `sacctmgr -nP list assoc user=$USER format=account,partition,qos` and use one complete row as-is. In `dispatch.yml`, all three of `partition`, `qos`, and `account` must come from the same row.

```bash
# Wrong: qos from one row, account from another
#SBATCH --qos=genome-center-grp-high-qos
#SBATCH --account=publicgrp              ← MISMATCH

# Right: same row
#SBATCH --qos=genome-center-grp-high-qos
#SBATCH --account=genome-center-grp      ← correct pair
```

---

### Job stuck in Pending with reason `(QOSGrpCpuLimit)`

**Cause:** Your user or group has hit the per-partition CPU quota. The scheduler is queuing the job until quota frees up.

**Fix:** Switch to the fallback partition/QOS triple. On Hive, the fallback is `low` + `publicgrp-low-qos` + `publicgrp` — preemptible but large capacity. Update `slurm:` in `dispatch.yml` temporarily, or run `stan hive-dispatch --partition low --qos publicgrp-low-qos --account publicgrp` to override for one invocation.

Check current queue:
```bash
squeue -u $USER -o '%.10i %.12j %.9P %.2t %.10M %.6C %.8m %R'
```

---

### `module: command not found` from non-interactive SSH

**Cause:** Non-interactive SSH sessions do not source `/etc/profile.d/modules.sh`, so `module` is not on PATH and `sbatch` may not be either.

**Fix:** Use a login shell or source the module init explicitly:
```bash
# Option A — login shell (simplest)
ssh hive "bash -l -c 'sbatch my_job.sh'"

# Option B — explicit source (needed for complex pipelines)
ssh hive "source /etc/profile.d/modules.sh && source /etc/profile.d/hpccf.sh && sbatch my_job.sh"
```

The bootstrap script already does this. Instrument PCs using `hive_upload_dir` + `submit_after_upload: true` must also use one of these patterns when SSHing to submit.

---

### `stan version` reports an older value than the git log shows

**Cause:** The STAN venv was installed from a prior GitHub zip and has not been upgraded.

**Fix:** Reinstall from the current main branch. Log into the cluster and run:
```bash
source /etc/profile.d/modules.sh
module load python/<version>
<stan_venv>/bin/pip install --upgrade \
    "stan-proteomics @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip"
<stan_venv>/bin/stan version
```

Do NOT use `--force-reinstall` — see the Lustre/distributed-filesystem note above.

---

### DIA-NN search returns 0 precursors on Thermo `.raw` files

**Cause:** The compute node cannot read Thermo `.raw` files because the `.NET 8 SDK` is missing or the wrong container is mounted.

**Diagnosis — run interactively on a compute node:**
```bash
srun --partition=<partition> --account=<account> \
     --time=00:05:00 --cpus-per-task=1 --mem=4G --pty bash

# Inside the job:
module load python/<version>
source <stan_venv>/bin/activate

# Check dotnet
dotnet --list-sdks      # must show 8.x — empty means runtime-only, not SDK
dotnet --list-runtimes  # shows both runtime and SDK installs

# Check DIA-NN sees .raw
apptainer exec --bind <shared_storage>:<shared_storage> <container.sif> \
  /diann-2.3.0/diann-linux --help 2>&1 | grep -i "raw\|dotnet\|net"
```

**Fix options:**
- If using a container: switch to the container that bundles `.NET 8 SDK`. On Hive, this is `/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif` (underscore in filename), NOT the lookalike in `apptainers/`.
- If using bare binary: install the `.NET 8 SDK` on compute nodes or via a module. See Appendix B for the 4-tier install procedure. Key rule: install the SDK (not just the runtime), and install `libicu` explicitly (not pulled by `dotnet-install.sh`).

Also check: `dotnet --list-sdks` returns empty but `dotnet --list-runtimes` is populated → you have runtime, not SDK. The Thermo reader requires SDK presence.

---

### `pip install` fails with `OSError [Errno 2] No such file or directory: ...INSTALLER<rand>.tmp`

**Cause:** `pip install --force-reinstall` uses a rename pattern that fails on distributed filesystems (Quobyte, Lustre, GPFS) due to distributed-rename semantics.

**Fix:** Remove `--force-reinstall` from all pip calls. Use `--upgrade` only. If you genuinely need a forced clean reinstall, delete the venv directory and recreate it:
```bash
rm -rf <stan_venv>
python3 -m venv <stan_venv>
<stan_venv>/bin/pip install --upgrade \
    "stan-proteomics @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip"
```

---

### SLURM job completes but no row written to `stan.db`

**Cause options:** (a) the output path in `dispatch.yml` is on node-local storage (e.g. `/tmp`) and vanished after the job ended; (b) the job hit a Python exception before writing to the DB; (c) the SQLite file is on a path not visible from compute nodes.

**Diagnosis:**
```bash
# Check the SLURM stdout log for the job
cat <sbatch_log_dir>/stan-hive-process-<jobid>.out

# Look for Python tracebacks
grep -i "error\|traceback\|exception" <sbatch_log_dir>/stan-hive-process-<jobid>.out

# Verify stan.db path is reachable from a compute node
srun --partition=<partition> --account=<account> --time=00:02:00 \
     --cpus-per-task=1 --mem=1G \
     bash -c "ls -lh <db_path>"
```

---

### `sacct` shows job COMPLETED but output looks empty / wrong

**Cause:** `sacct` reports the `.batch` and `.extern` sub-steps as COMPLETED even when the main job step failed. The top-level job ID (without a `.`) is the one to check.

```bash
# Wrong — includes .batch and .extern substeps which always show COMPLETED
sacct -j <jobid>

# Right — filter out substeps
sacct -j <jobid> | grep -v '\.'
```

---

## Appendix A — `dispatch.yml` Structure Reference

This is an annotated summary of the keys in `DEFAULT_CONFIG_TEMPLATE` (from
`stan/community/scripts/dispatch_hive.py`). When the AI generates your `dispatch.yml`,
every key here should be present.

```yaml
# Absolute path on shared storage. All SLURM jobs open this file.
db_path: /shared/<your-path>/stan.db

# DIA-NN/Sage search outputs land here. Per-raw subdirs created automatically.
# Must be on shared storage — never /tmp.
out_root: /shared/<your-path>/processing

# SLURM job stdout/stderr. Must be on shared storage — never /tmp.
sbatch_log_dir: /shared/<your-path>/logs/sbatch

# Dispatcher's own JSONL audit log.
dispatch_log_dir: /shared/<your-path>/logs/dispatch

# Absolute path to the Python venv. Sourced inside SLURM jobs.
stan_venv: /shared/<your-path>/stan_venv

# SLURM resource block. ALL THREE values must be from the same sacctmgr row.
slurm:
  partition: <partition>
  qos: <qos>
  account: <account>
  time: "06:00:00"   # 6h is ample for DIA-NN on a HeLa QC raw
  cpus: 8
  mem: "32G"

# Limit new submissions per dispatcher invocation (cron-safety valve).
max_submissions_per_run: 50

# Regex for QC filename matching. Empty string dispatches everything.
qc_pattern: "(?i)(he(l[a5\\d]|\\d)|qc|std[_\\-\\s]?he)"

# Stop retrying after this many failures for a given raw file.
max_attempts: 3

# One block per instrument.
instruments:
  - name: "<display name>"
    family: "<timsTOF|Orbitrap|...>"    # IPS cohort key
    vendor: "<bruker|thermo>"
    watch_dir: /shared/<your-path>/incoming/<instrument>
    column_vendor: ""
    column_model: ""
```

---

## Appendix B — Thermo `.raw` on Linux: .NET 8 SDK Requirement

Applies to any cluster where you process Thermo `.raw` files without a container that pre-bundles the .NET runtime (e.g. bare-binary DIA-NN install or a minimal container image).

**The root cause of "0 precursors from .raw" is almost always a .NET install problem**, not a DIA-NN flag problem. Specifically:

- DIA-NN 2.x requires the **.NET 8 SDK** — NOT just the runtime.
- `dotnet --list-runtimes` shows entries for both runtime-only and SDK installs.
- `dotnet --list-sdks` shows entries only if the SDK is installed.
- If `--list-runtimes` is populated but `--list-sdks` is empty → runtime only → Thermo reader will fail.

**Install order** (try each tier in order until one succeeds):

1. Check for existing 8.x SDK: `dotnet --list-sdks 2>/dev/null | grep -qE '^8\.'`
2. Try apt: `sudo apt-get install -y dotnet-sdk-8.0` (Ubuntu/Debian only)
3. Add Microsoft apt repo and retry: `packages-microsoft-prod.deb` from `packages.microsoft.com/config/ubuntu/<release>/`
4. Fall back to `dotnet-install.sh --channel 8.0` (no `--runtime` flag) + manual `apt-get install -y libicu<N> libssl3 libstdc++6 libunwind8`

The libicu package name varies by Ubuntu release:
- Ubuntu 26.04 → `libicu76`
- Ubuntu 24.04 → `libicu74`
- Ubuntu 22.04 → `libicu70`

`dotnet-install.sh` does NOT install system dependencies — you must install libicu separately or the .NET binary will fail silently.

For the canonical 4-tier implementation, see
`/Users/brettphinney/Documents/claude/docs/THERMO_RAW_WSL2_NOTES.md` (section 5).
This doc was written from 27 hotfix versions of DE-LIMP tracking down the same root cause.

**Verification** (run after install, before submitting real jobs):
```bash
dotnet --list-sdks      # must show 8.x
dotnet --list-runtimes  # must show Microsoft.NETCore.App 8.x
ldd <diann-linux-binary> | grep 'not found'  # must be empty
LD_LIBRARY_PATH=<diann-dir>:$LD_LIBRARY_PATH \
  DOTNET_ROOT=/usr/share/dotnet \
  <diann-dir>/diann-linux --help >/dev/null 2>&1 && echo "OK"
```

If running DIA-NN via Apptainer (the preferred approach on most clusters), this is a
non-issue only if the `.sif` image bundles .NET. Always verify with a test `.raw` file
before assuming the container is correctly built.
