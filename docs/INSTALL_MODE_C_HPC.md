# STAN Mode C — Install on a SLURM Cluster

> **Who this is for:** an AI coding agent (Claude Code, Cursor, Codex, Aider, …) that a lab has pointed at this repository to install STAN on the lab's own SLURM cluster, and the person supervising that agent.
> **Verified against:** `main` at STAN 1.2.x, 2026-09-29. Code is cited by file and symbol. Line numbers drift; symbol names do not.
> **Other modes:** [Mode A — instrument PC](../README.md#quick-install--pick-your-mode) (not recommended) · [Mode B — separate Linux box](INSTALL_MODE_B_LINUX.md) · [UC Davis reference paths](HPC_PATHS.md)

Mode C runs STAN's searches as SLURM jobs on the lab's cluster. The instrument PCs only acquire and copy raw files. A cron job on a login node finds new raw files and submits one SLURM job for each. Each job searches the file, scores it, and writes the result to STAN's database.

UC Davis runs STAN this way on its Hive cluster. That deployment is the reference: [HPC_PATHS.md](HPC_PATHS.md) lists its paths, and the `scripts/cron_*.sh` files are its running cron jobs. This guide tells you how to reproduce it on a different cluster. It does not assume any UC Davis path, account or credential.

---

## Read this first

### What runs where

```
Instrument PC (acquires only; no STAN install)
    │  copy each finished run (scheduled robocopy task, rsync, vendor transfer)
    ▼
Shared storage that compute nodes mount
    <STAN_HOME>/incoming/<instrument>/<run>.d | <run>.raw      flat, one dir per instrument
    │
Login-node cron, every 5–15 min, under flock  (walks directories + calls sbatch; no compute)
    │  stan hive-dispatch --config <STAN_HOME>/dispatch.yml
    ▼
One SLURM job per raw file:  stan hive-process
    QC-named file  → DIA-NN 2.3.0 (DIA) or Sage 0.14.7 (DDA) → metrics, IPS → runs row
                     (+ TIC, and for Bruker 4DFF feature cloud + PEG/drift when installed)
    anything else  → "monitor" job: raw-file metadata only, never searched → sample_health row
    ▼
<STAN_HOME>/db/stan.db  (SQLite on shared storage)
    ├──► dashboard on a lab machine (reads a copy of the DB)
    └──► optional login-node cron: stan submit-all / stan peg-sync → community relay
```

The dispatcher decides whether a file is QC by its filename. A file whose name matches `qc_pattern` gets a search job. Every other file gets a monitor job, which reads only the raw file's metadata and is never searched or submitted anywhere. `DEFAULT_QC_PATTERN` in `stan/community/scripts/dispatch_hive.py` matches names containing `HeLa`/`Hel5`/`He5`, `QC` or `stdHe`.

### Portability status at STAN 1.2.x

The dispatcher's config file is portable, but the scripts that run inside each SLURM job still contain UC Davis values. **You must apply a small site patch before any job will work on another cluster.** Step 2.5 lists every value.

| Component | Portable? | What you do |
|---|---|---|
| `stan hive-dispatch`: directory walk, dedup, submission cap, `dispatch.yml` | Yes, except the monitor-job SLURM triple | Write your own `dispatch.yml` (Step 2.6) |
| QC job script rendered by `_render_sbatch()` (`dispatch_hive.py`) | **No.** It hardcodes the UC Davis module names and 4DFF path, and forces `STAN_DB_BACKEND=pg` with a UC Davis token file | Patch it (Step 2.5) |
| Monitor job profile `_MONITOR_SLURM` (`dispatch_hive.py`) | **No.** Hardcoded to `low` / `publicgrp-low-qos` / `publicgrp` | Patch it (Step 2.5) |
| Job body constants in `stan/community/scripts/run_one_v1.py` | **No.** These are the DIA-NN image, Sage binary, asset cache, TRFP DLL and bind mounts | Patch them (Step 2.5) |
| `scripts/hive_bootstrap.sh` | **No.** It uses UC Davis paths and modules, pulls a container image that was never published, and installs Sage from "latest" | Do not run it. Follow Phase 2 instead |
| Postgres backend (`STAN_DB_BACKEND=pg`) | **No.** The host and database are hardcoded to UC Davis PG Farm (`PG_DEFAULTS` in `stan/db_pg.py`) | Stay on SQLite. Leave `STAN_DB_BACKEND` unset |
| `scripts/cron_*.sh` | **No.** They contain UC Davis paths | Copy the *pattern* (Phase 4), not the files |
| `stan/community/scripts/link_flinders_qc.py` | **No.** Its source archive paths are hardcoded | Write your own linker if you need one (Phase 3) |
| Cron heartbeat (`check_cron_heartbeat` in `stan/reports/instrument_watch.py`) | **No.** Its log directory and job names are hardcoded | Write a small watchdog (Step 4.3) |

Because you will carry a patch, install STAN from a git clone on a site branch (Step 2.2), the same way UC Davis does: its venv imports straight from a checkout. The maintainer's to-do list for making these values configurable is at the end of this guide ([Known gaps](#known-gaps-in-12x-for-the-maintainer)).

### Hard rules

Follow these without exception. Each one cost the reference deployment an outage or lost data.

1. **No compute on a login node.** A login node may walk directories, create symlinks, run `git`/`pip`/`curl`, call `sbatch`, and make HTTP requests (`stan submit-all`). Anything that opens a raw file, runs DIA-NN, Sage, 4DFF or ThermoRawFileParser, or builds a container goes in `sbatch` or `srun`.
2. **Never put outputs or `#SBATCH --output` under `/tmp`.** It is node-local, and the files are gone when the job ends. Everything goes on shared storage. (A `flock` lock file in the login node's `/tmp` is fine, because only that node uses it.)
3. **Use `pip install --upgrade`, never `--force-reinstall`, on a distributed filesystem.** On Quobyte, Lustre and GPFS the force-reinstall rename fails with `OSError [Errno 2] … INSTALLER<rand>.tmp`. For a clean reinstall, delete the venv and recreate it.
4. **No `sudo`.** Ask the cluster admins for OS packages.
5. **Use one complete `(account, partition, qos)` row from `sacctmgr`.** Mixing a QOS from one row with an account from another fails with `Invalid qos specification`.
6. **Use exactly DIA-NN 2.3.0 and Sage v0.14.7.** The cluster pipeline records `diann_version = "2.3.0"` on every DIA run whatever binary actually ran (`_extract_metrics` in `stan/pipeline/hive_process.py`). A different DIA-NN would be mislabelled in your own database and in any community submission.
7. **Home directories get small files only:** `~/.stan/community.yml` and `~/.stan/tools/ThermoRawFileParser/` (a ~10 MB download). Everything large goes on group storage.
8. **Leave `STAN_DB_BACKEND` unset** everywhere: in cron jobs, job scripts and your shell.
9. **Harden every cron job:** use `flock`, invoke the script as `bash <script>`, turn off `set -u` while sourcing the module profile, write a dated log, and add a heartbeat. Record which login node holds the crontab. Phase 4 explains each item.

### Is Mode C the right mode?

| Situation | Mode | Guide |
|---|---|---|
| The lab has a SLURM cluster, shared storage that compute nodes mount, and someone who can keep a crontab running | **C** | this doc |
| No cluster, but a spare Linux machine (or a Windows machine running WSL2) | B | [INSTALL_MODE_B_LINUX.md](INSTALL_MODE_B_LINUX.md) (WSL2 specifics: [INSTALL_MODE_B_WSL.md](INSTALL_MODE_B_WSL.md)) |
| Only the instrument PC | A | [README](../README.md#quick-install--pick-your-mode). Not recommended: searches on the acquisition PC froze UC Davis's timsTOF |

Most of the setup time goes into two steps: finding or building a DIA-NN 2.3.0 image with .NET 8 inside (Step 2.3), and the site patch (Step 2.5).

---

## Phase 1 — Survey the cluster

Run these on a login node and keep the output. Write every choice you make into `<STAN_HOME>/SITE.md` as you go: the account triple, paths, module names, and the login node that holds the crontab. Whoever maintains the install next needs that record.

```bash
hostname                                                   # the node your crontab will live on
sacctmgr -nP list assoc user=$(id -un) format=account,partition,qos
sinfo -s
scontrol show partition <partition> | grep -oE 'MaxTime=[^ ]+|DefaultTime=[^ ]+|MaxCPUsPerNode=[^ ]+'
squeue --me --noheader >/dev/null; echo "squeue --me exit=$?"  # must be 0; the dispatcher needs --me
module avail python 2>&1 | tr ' ' '\n' | grep -i '^python'  # or: module spider python
module avail apptainer singularity dotnet 2>&1 | tr ' ' '\n' | grep -iE 'apptainer|singularity|dotnet'
command -v apptainer singularity; apptainer --version 2>/dev/null || singularity --version
df -h /home /scratch /project /work /lustre /gpfs /beegfs /nfs 2>/dev/null
cat /etc/os-release | grep PRETTY_NAME
for u in https://github.com https://huggingface.co https://brettsp-stan.hf.space; do
  printf '%s ' "$u"; curl -sS -o /dev/null -w '%{http_code}\n' --max-time 15 "$u"; done
```

Then repeat the OS, storage and egress checks from a compute node:

```bash
srun --account=<acct> --partition=<part> --qos=<qos> --time=00:05:00 --cpus-per-task=1 --mem=1G \
  bash -c 'hostname; grep PRETTY_NAME /etc/os-release; ls -ld ~ <candidate STAN_HOME parent>;
           for u in https://github.com https://huggingface.co; do printf "%s " $u;
           curl -sS -o /dev/null -w "%{http_code}\n" --max-time 15 $u; done'
```

| Record | Used for |
|---|---|
| One `sacctmgr` row for search jobs, and one cheap or preemptible row for monitor jobs | `dispatch.yml` `slurm:` block and the patched `_MONITOR_SLURM` |
| `MaxTime` of that partition | Must exceed `time:` in `dispatch.yml` (default `06:00:00`) |
| A Python module between 3.10 and 3.12 | The venv. `[peg]` pins `numpy<2`, which has no wheels for Python 3.13+ |
| Apptainer/Singularity module name and version | Running DIA-NN; Thermo PEG |
| A .NET 8 module name, if there is one | Thermo `.raw` (ThermoRawFileParser; DIA-NN outside a container) |
| A group-writable filesystem mounted on login **and** compute nodes | `STAN_HOME` |
| Whether `~` is visible from compute nodes | `~/.stan/tools/ThermoRawFileParser` must be readable inside jobs |
| Egress from login and compute nodes | Login: GitHub, PyPI, Hugging Face, and the relay if you submit to the community. Compute: none needed if you pre-stage assets (Step 2.4) |
| `hostname` of the login node for the crontab | Clusters with several login nodes keep a separate crontab on each. At UC Davis `ssh hive` lands on `login1`, while STAN's crontab is on `login2`; `crontab -l` on `login1` says `no crontab` |

**Verify:** you have one complete `sacctmgr` row, a Python 3.10–3.12 module, a container runtime, and a shared path that a compute node can list. If any of these is missing, stop and ask the person you are working for. Do not guess.

---

## Phase 2 — Install

Set these once. Every later step uses them.

```bash
export STAN_HOME=/shared/<lab>/stan          # shared, group-readable, mounted on compute nodes
export PY_MODULE=python/3.11.9               # your module, 3.10–3.12
```

### 2.1 Directory layout

```bash
mkdir -p "$STAN_HOME"/{src,db,processing,logs/sbatch,logs/dispatch,incoming,assets,tools,backups}
```

| Directory | Holds | `dispatch.yml` key |
|---|---|---|
| `src/` | git clone of STAN (site branch) | — |
| `venv/` | Python venv, created in 2.2 | `stan_venv` |
| `db/stan.db` | SQLite database | `db_path` |
| `processing/<run>/` | DIA-NN/Sage output per raw file | `out_root` |
| `logs/sbatch/` | job stdout, rendered job scripts (`scripts/`), monitor logs (`monitor/`) | `sbatch_log_dir` |
| `logs/dispatch/` | one JSONL summary line per dispatcher run | `dispatch_log_dir` |
| `logs/` | cron logs (`cron_*_YYYYMMDD.log`) | — |
| `incoming/<instrument>/` | flat watch directory per instrument | `instruments[].watch_dir` |
| `assets/` | community FASTA and spectral libraries | patched `ASSET_CACHE` |
| `tools/` | Sage, DIA-NN image, 4DFF, TRFP image | patched constants |

**Verify from a compute node:**

```bash
srun --account=<acct> --partition=<part> --qos=<qos> --time=00:02:00 --cpus-per-task=1 --mem=1G \
  bash -c "touch $STAN_HOME/processing/.w && rm $STAN_HOME/processing/.w && echo WRITE-OK"
```

### 2.2 STAN from a git clone, editable

```bash
module load "$PY_MODULE"
git clone https://github.com/bsphinney/stan.git "$STAN_HOME/src"
git -C "$STAN_HOME/src" switch -c site/<cluster-name>
python3 -m venv "$STAN_HOME/venv"
"$STAN_HOME/venv/bin/pip" install --upgrade pip
"$STAN_HOME/venv/bin/pip" install -e "$STAN_HOME/src[peg]"
```

- `[peg]` installs `alphatims>=1.0,<1.0.9`, `numpy<2` and `pandas<3`, which Bruker PEG and DIA window drift need. Under pandas 3, alphatims frame windows shift and PEG stays NULL (`stan/metrics/alphatims_guard.py`). Do not use `stan install-peg-deps` here: it does not pin pandas.
- Do not add `[thermo]` (`fisher_py`). It needs .NET inside the venv. Thermo PEG on a cluster uses the ThermoRawFileParser container instead (Step 2.3).
- If `python3 -m venv` fails with an `ensurepip` error, the system Python lacks it. Load the Python module first; that is why the module comes first above.

**Verify:**

```bash
"$STAN_HOME/venv/bin/stan" version                      # "STAN v1.2.x"   (there is no --version flag)
"$STAN_HOME/venv/bin/python" -c "import stan; print(stan.__file__)"   # must be under $STAN_HOME/src
"$STAN_HOME/venv/bin/stan" doctor                       # numpy 1.x, pandas 2.x, alphatims 1.0.8
```

**Updating later:** `git -C "$STAN_HOME/src" fetch origin && git -C "$STAN_HOME/src" rebase origin/main`. Rerun the `pip install -e` line only when `pyproject.toml` dependencies changed. The checkout *is* the deployment: a job that starts after the rebase runs the new code. Re-check the site patch after every rebase (Step 2.5, "Verify").

### 2.3 Search engines and tools

| Tool | Version | Needed for | Put it in | How the job finds it |
|---|---|---|---|---|
| DIA-NN | **2.3.0 exactly**, in an Apptainer image with the .NET 8 SDK (built from DIA-NN's own Dockerfile) | DIA QC; Thermo `.raw` inside DIA-NN | `$STAN_HOME/tools/diann_2.3.0.sif` | patched `DIANN_SIF` + `DIANN_BIN` |
| Sage | **v0.14.7** static binary | DDA QC | `$STAN_HOME/tools/sage-v0.14.7-x86_64-unknown-linux-gnu/sage` | patched `SAGE_BIN` |
| ThermoRawFileParser (TRFP) net8 DLL + a .NET 8 runtime | the build STAN auto-downloads (`v.2.0.0-dev`) | Thermo DIA/DDA detection; `.raw`→mzML before Sage | `~/.stan/tools/ThermoRawFileParser/` | detection: automatic. Sage: patched `TRFP_DLL` |
| 4DFF (`uff-cmdline2`) | pinned by `stan install-4dff` | Bruker feature cloud (optional) | `$STAN_HOME/tools/bruker_ff/linux/` | `STAN_BRUKER_FF_DIR` in the patched job script |
| TRFP Apptainer image | `quay.io/biocontainers/thermorawfileparser:1.4.5--ha8f3691_0` | Thermo PEG (optional) | `$STAN_HOME/tools/trfp.sif` | `STAN_TRFP_SIF` in the patched job script |

**DIA-NN.** STAN does not ship a DIA-NN image. `hive_bootstrap.sh`'s `pull_diann_sif()` points at `docker://registry.hf.space/brettsp-stan-proteomics:latest`. The script itself says that image was never published, and a registry probe on 2026-09-29 returned 404. Build your own from DIA-NN's release zip. The zip ships a `Dockerfile` and `make-docker.sh`. That Dockerfile starts from `debian:12`, installs `dotnet-sdk-8.0` from Microsoft's repository, and copies the build to `/diann-2.3.0/`, so `DIANN_BIN` stays `/diann-2.3.0/diann-linux`:

```bash
# on any machine with Docker (not the cluster):
curl -fLO https://github.com/vdemichev/DiaNN/releases/download/2.0/DIA-NN-2.3.0-Academia-Linux-Preview.zip
unzip DIA-NN-2.3.0-Academia-Linux-Preview.zip -d diann-build && cd diann-build
bash make-docker.sh                                   # → image "diann_docker"
docker save diann_docker -o diann_docker.tar          # copy this tar to $STAN_HOME/tools/
# on the cluster, inside SLURM (converting to SIF is CPU-heavy):
srun --account=<acct> --partition=<part> --qos=<qos> --time=01:00:00 --cpus-per-task=4 --mem=16G \
  apptainer build "$STAN_HOME/tools/diann_2.3.0.sif" "docker-archive://$STAN_HOME/tools/diann_docker.tar"
```

The 2.3.0 Linux build is published only as `-Preview.zip`, and DIA-NN publishes its 2.x builds as assets of the `2.0` release tag. DIA-NN is academic-licensed, so the human must read and accept the license. If your cluster forbids containers, see [Appendix B](#appendix-b--net-8-for-thermo-raw-on-a-cluster-no-sudo) and plan a larger patch to `run_diann()` (it calls `apptainer exec`).

At UC Davis, two DIA-NN images with near-identical names differ: one silently skips `.raw` files because it has no .NET. See [external_tools.md → DIA-NN containers on Hive](external_tools.md#dia-nn-containers-on-hive--critical). Test yours with a real Thermo `.raw` before relying on it (Step 2.7).

**Sage:**

```bash
cd "$STAN_HOME/tools"
curl -fLO https://github.com/lazear/sage/releases/download/v0.14.7/sage-v0.14.7-x86_64-unknown-linux-gnu.tar.gz
tar -xzf sage-v0.14.7-x86_64-unknown-linux-gnu.tar.gz      # → sage-v0.14.7-x86_64-unknown-linux-gnu/sage
./sage-v0.14.7-x86_64-unknown-linux-gnu/sage --version     # prints "sage 0.14.6"
```

The v0.14.7 release binary reports `sage 0.14.6` (checked 2026-09-29 on the Linux and macOS builds of that release). That is the right file.

**ThermoRawFileParser** (Thermo labs). This is a download only, so it is fine on a login node:

```bash
"$STAN_HOME/venv/bin/python" -c "from stan.tools.trfp import ensure_installed; print(ensure_installed())"
# → /home/<you>/.stan/tools/ThermoRawFileParser/ThermoRawFileParser.dll
```

Jobs need `dotnet` (a .NET 8 runtime) on `PATH`. If `~/.stan` is not visible on compute nodes, choose Bruker-only or ask the admins to mount home. There is no override for the `~/.stan` location.

**4DFF** (Bruker, optional):

```bash
STAN_BRUKER_FF_DIR="$STAN_HOME/tools/bruker_ff" "$STAN_HOME/venv/bin/stan" install-4dff --platform linux
ls -l "$STAN_HOME/tools/bruker_ff/linux/uff-cmdline2"
```

**Thermo PEG image** (optional). UC Davis's image was built from the BioContainers ThermoRawFileParser 1.4.5 image (`apptainer inspect`, 2026-09-29). Pull the same one inside SLURM:

```bash
srun --account=<acct> --partition=<part> --qos=<qos> --time=00:30:00 --cpus-per-task=2 --mem=8G \
  apptainer pull "$STAN_HOME/tools/trfp.sif" docker://quay.io/biocontainers/thermorawfileparser:1.4.5--ha8f3691_0
```

STAN runs `apptainer exec --cleanenv <sif> ThermoRawFileParser -i=… -o=… -f=1 -L=1`. Background and costs are in [PEG_WATCH.md → Thermo PEG on Hive](PEG_WATCH.md#thermo-peg-on-hive).

### 2.4 Pre-stage the community search assets

Every search uses the frozen community FASTA and a vendor spectral library. A job downloads any file missing from `ASSET_CACHE` with `hf download`, and a compute node usually cannot. Stage them now from the login node:

```bash
cd "$STAN_HOME/assets"
for f in human_hela_202604.fasta hela_timstof_202604.parquet hela_orbitrap_202604.parquet; do
  curl -fL -o "$f" "https://github.com/bsphinney/stan/releases/download/v0.1.0-assets/$f"
done
md5sum human_hela_202604.fasta hela_timstof_202604.parquet hela_orbitrap_202604.parquet
```

**Verify** that the hashes equal `EXPECTED_ASSET_HASHES` in `stan/community/validate.py`:

| File | MD5 | Size |
|---|---|---|
| `human_hela_202604.fasta` | `8de1d9bd0a052b175f88f66f82500d92` | 13.9 MB |
| `hela_timstof_202604.parquet` | `ad72bfb2730644c69147ba8f34bfe982` | 12.4 MB |
| `hela_orbitrap_202604.parquet` | `ac84e40f5b2f23e1286f28a7baeccec2` | 38.4 MB |

The files sit directly in `assets/`, not in subdirectories. That is where `get_community_diann_params()` looks.

### 2.5 Apply the site patch

Edit these in `$STAN_HOME/src` on your site branch and commit them. Change nothing else.

| File | Symbol | UC Davis value | Set to |
|---|---|---|---|
| `stan/community/scripts/run_one_v1.py` | `DIANN_SIF` | `/quobyte/…/dia-nn/diann_2.3.0.sif` | your DIA-NN 2.3.0 image |
| 〃 | `DIANN_BIN` | `/diann-2.3.0/diann-linux` | the binary's path inside your image |
| 〃 | `SAGE_BIN` | `/quobyte/…/sage-v0.14.7-…/sage` | your Sage v0.14.7 binary |
| 〃 | `ASSET_CACHE` | `/quobyte/…/stan_community_assets` | `$STAN_HOME/assets` (absolute path) |
| 〃 | `TRFP_DLL` | `/quobyte/…/ThermoRawFileParser.dll` | `~/.stan/tools/ThermoRawFileParser/ThermoRawFileParser.dll`, written as an absolute path |
| 〃 | `--bind` list in `run_diann()` | `/quobyte`, `/nfs`, `/tmp` | your storage roots plus `/tmp`. Apptainer fails outright if a bind source does not exist |
| `stan/community/scripts/dispatch_hive.py` | `_MONITOR_SLURM` | `low` / `publicgrp-low-qos` / `publicgrp` | a valid triple of yours, ideally cheap or preemptible (the job keeps `--requeue`) |
| 〃 | `_render_sbatch()`: `module load apptainer`, `module load dotnet-core-sdk/8.0.4` | UC Davis module names | your module names. Both lines end in `\|\| true`, so a wrong name fails silently |
| 〃 | `_render_sbatch()`: `bruker_ff_dir` | `/quobyte/proteomics-grp/brett/bruker_ff` | `$STAN_HOME/tools/bruker_ff` (or leave it; 4DFF is then skipped) |
| 〃 | `_render_sbatch()`: `export STAN_DB_BACKEND=pg` and the `PGPASSWORD` block | UC Davis PG Farm | **delete these lines**. Otherwise every QC job fails at the DB write with `no PG Farm password` |
| 〃 | `_render_sbatch()` | — | optional: add `export STAN_TRFP_SIF=$STAN_HOME/tools/trfp.sif` for Thermo PEG |
| 〃 | `_render_monitor_sbatch()`: `module load dotnet-core-sdk/8.0.4` | UC Davis module name | your .NET module |

Leave these alone. They are harmless or unused off-site:

- `MIRROR_BASE` / `FAMILY_TO_HOST` in `run_one_v1.py`: optional per-instrument library lookups that are skipped when the path is absent.
- `DEFAULT_CONFIG_PATH` and the `_load_config()` defaults: you always pass `--config` and set every key.
- The `--partition` map in `dispatch_one_raw()`: do not use `--partition`; edit `dispatch.yml` instead.

The rendered job script sources `/etc/profile.d/modules.sh` after `set -euo pipefail`. That works at UC Davis because the job inherits the cron's environment (`sbatch` exports it by default). If you touch those lines, wrap the sourcing in `set +u` … `set -u` the way the cron scripts do (Phase 4).

**Verify.** Render both job scripts from your config after writing `dispatch.yml` (Step 2.6), and check the job-body constants:

```bash
cd "$STAN_HOME" && venv/bin/python - <<'EOF'
from pathlib import Path
from stan.community.scripts.dispatch_hive import _load_config, _render_sbatch, _render_monitor_sbatch
from stan.community.scripts import run_one_v1 as r
cfg = _load_config(Path("dispatch.yml")); inst = cfg["instruments"][0]
raw = Path(inst["watch_dir"]) / "HeLa_example.d"
text = _render_sbatch(raw, inst, cfg) + _render_monitor_sbatch(raw, inst, cfg)
bad = [s for s in ("quobyte", "/nfs", "publicgrp", "genome-center", "pgfarm", "STAN_DB_BACKEND=pg")
       if s in text]
print("UC Davis strings left in job scripts:", bad or "none")
for name in ("DIANN_SIF", "SAGE_BIN", "ASSET_CACHE", "TRFP_DLL"):
    p = getattr(r, name); print(f"{name:12} {p}  {'OK' if Path(p).exists() else 'MISSING'}")
EOF
git -C "$STAN_HOME/src" diff origin/main --stat     # only run_one_v1.py and dispatch_hive.py
```

Expect `none`, and `OK` on every constant you use. (`TRFP_DLL` may read `MISSING` in a Bruker-only lab.)

### 2.6 Write `dispatch.yml`

Write it by hand from this template. **Do not use `stan hive-dispatch --print-default-config` as-is:** its `qc_pattern` line is double-quoted, and YAML rejects the backslashes (`ScannerError while scanning a double-quoted scalar`). Single quotes work.

```yaml
# <STAN_HOME>/dispatch.yml — read on every dispatcher run; no restart needed.
db_path: /shared/<lab>/stan/db/stan.db
out_root: /shared/<lab>/stan/processing
sbatch_log_dir: /shared/<lab>/stan/logs/sbatch       # never /tmp
dispatch_log_dir: /shared/<lab>/stan/logs/dispatch
stan_venv: /shared/<lab>/stan/venv

slurm:                     # ONE complete sacctmgr row, plus limits
  partition: <partition>
  qos: <qos>
  account: <account>
  time: "06:00:00"         # must be below the partition MaxTime
  cpus: 8
  mem: "32G"

max_submissions_per_run: 5 # new jobs per dispatcher run; keep low on SQLite (Step 4.4)
max_attempts: 3            # stop retrying a raw after 3 failed attempts
qc_pattern: '(?i)(he(l[_\-\s]?[a5\d]|[_\-\s]?\d)|qc|std[_\-\s]?he)'   # SINGLE quotes

instruments:
  - name: timsTOF HT                 # stored as runs.instrument
    family: timsTOF                  # IPS cohort key
    vendor: bruker                   # bruker | thermo
    watch_dir: /shared/<lab>/stan/incoming/timsTOF-HT
    column_vendor: ""                # optional
    column_model: ""                 # optional
    # amount_ng: 50                  # optional usual HeLa load; a unit-anchored amount in the file name wins
    # lc_flow: nano                  # non-Evosep LC: nano | capillary | micro (sent to the community benchmark)
  - name: Orbitrap Exploris 480
    family: Exploris
    vendor: thermo
    watch_dir: /shared/<lab>/stan/incoming/Exploris480
```

- `family` has built-in mappings for `timsTOF`, `Lumos` and `Exploris` (`_resolve_instrument` in `run_one_v1.py`). Any other value is stored verbatim as the instrument family.
- Per-instrument `name`, `family`, `vendor` and `watch_dir` are required; the dispatcher raises `KeyError` without them. See [Appendix A](#appendix-a--dispatchyml-keys) for every key.

**Verify** with a dry run. It walks and classifies files and submits nothing:

```bash
"$STAN_HOME/venv/bin/stan" hive-dispatch --config "$STAN_HOME/dispatch.yml" --dry-run
```

Expected output, from a scratch test with one timsTOF QC, one Exploris QC, one blank and one `.partial`:

```
[dry-run] would submit HeLa_50ng_60spd_01.d (timsTOF HT) [qc]
[dry-run] would submit Blank_03.raw (Orbitrap Exploris 480) [monitor]
[dry-run] would submit QC_HeLa_200ng_02.raw (Orbitrap Exploris 480) [qc]
Dry-run: scanned=3 submitted=3 skipped(processed=0, pattern=0, in_flight=0, max_attempts=0) failed=0 capped=0
```

`.partial` and `.tmp` entries are skipped. `pattern=` is always 0 at 1.2.x, because every file is either QC or monitor. Exit code 0 means the config loaded.

### 2.7 Smoke test on a compute node

Save this as `$STAN_HOME/smoke.sbatch` and fill in the placeholders. It checks every dependency a real job needs. It is not a search.

```bash
#!/bin/bash
#SBATCH --job-name=stan-smoke
#SBATCH --partition=<partition>
#SBATCH --qos=<qos>
#SBATCH --account=<account>
#SBATCH --time=00:20:00
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --output=<STAN_HOME>/logs/sbatch/stan-smoke_%j.out
set -uo pipefail
set +u
source /etc/profile.d/modules.sh 2>/dev/null || true      # your site's module init
module load <apptainer-module> 2>/dev/null || true
module load <dotnet-module> 2>/dev/null || true
set -u
source <STAN_HOME>/venv/bin/activate
echo "node: $(hostname)"; stan version
command -v apptainer || echo "MISSING apptainer"
command -v dotnet    || echo "MISSING dotnet (Thermo only)"
command -v hf        || echo "MISSING hf (only needed if assets are not pre-staged)"
python - <<'EOF'
import numpy, pandas, alphatims
print("numpy", numpy.__version__, "pandas", pandas.__version__, "alphatims", alphatims.__version__)
from pathlib import Path
from stan.community.scripts import run_one_v1 as r
from stan.watcher.detector import detect_mode
print("DIA-NN image", r.DIANN_SIF, Path(r.DIANN_SIF).exists())
for raw, vendor in [("<one Bruker QC .d>", "bruker"), ("<one Thermo QC .raw>", "thermo")]:
    if Path(raw).exists():
        print(vendor, "mode:", detect_mode(Path(raw), vendor))   # UNKNOWN = TRFP/dotnet broken
EOF
eval "$(python -c 'from stan.community.scripts import run_one_v1 as r; print(f"SIF={r.DIANN_SIF!r}; BIN={r.DIANN_BIN!r}; SAGE={r.SAGE_BIN!r}")')"
apptainer exec "$SIF" "$BIN" 2>&1 | grep -m1 'DIA-NN'     # expect: DIA-NN 2.3.0 Academia …
"$SAGE" --version                                         # expect: sage 0.14.6 (see 2.3)
```

```bash
sbatch "$STAN_HOME/smoke.sbatch"          # "Submitted batch job <id>"
sacct -j <id> -X -o JobID,State,Elapsed   # -X hides the .batch/.extern steps
cat "$STAN_HOME/logs/sbatch/stan-smoke_<id>.out"
```

**Pass when** the log contains no `MISSING` lines, shows numpy 1.x, pandas 2.x and alphatims 1.0.8, prints `DIA-NN 2.3.0`, and reports a mode for each vendor you run: `DIA_PASEF`/`DDA_PASEF` for Bruker, `DIA_ORBITRAP`/`DDA_ORBITRAP` for Thermo. An `UNKNOWN` Thermo mode means TRFP or `dotnet` is missing. The job would then search that file as DIA, because `_detect_mode_str` falls back to DIA.

### 2.8 First real job

Submit one QC file directly, skipping the walk:

```bash
"$STAN_HOME/venv/bin/stan" hive-dispatch --config "$STAN_HOME/dispatch.yml" \
  --raw "<absolute path to one QC .d or .raw>" \
  --instrument-name "timsTOF HT" --family timsTOF --vendor bruker
# → {"status": "submitted", "job_id": "…", "classification": "qc", …}
```

The job is named `stan-<run stem>` (monitor jobs are `stan-mon-<run stem>`). Its log is `<sbatch_log_dir>/<run stem>_<jobid>.out`, and the script it ran is kept at `<sbatch_log_dir>/scripts/<run stem>.sbatch`. At UC Davis, 24 per-raw QC jobs in the week to 2026-09-29 (8 CPUs, 32 GB) took a median of 10 min and at most 26 min. DIA-NN itself has a 3 h timeout.

**Verify:**

```bash
sacct -j <jobid> -X -o JobID,JobName%40,State,Elapsed
ls -l "$STAN_HOME/processing/<run stem>/report.parquet"
"$STAN_HOME/venv/bin/python" -c "
import sqlite3; con = sqlite3.connect('$STAN_HOME/db/stan.db')
print(con.execute('select instrument, run_name, mode, n_precursors, n_psms, ips_score, diann_version from runs order by rowid desc limit 3').fetchall())
print(con.execute('select raw_path, status, attempt_count, error from dispatch_attempts order by attempted_at desc limit 3').fetchall())"
```

A new `runs` row with a non-zero `n_precursors` (DIA) or `n_psms` (DDA) means the whole path works. Run the same check with one non-QC file and look for a `sample_health` row. Then walk the whole directory with `stan hive-dispatch --config …` (no `--dry-run`) and move on to Phase 4.

---

## Phase 3 — Getting raw files onto the cluster

The dispatcher's walk (`_walk_raws`) has three rules you must design around:

- **It is flat and non-recursive.** It lists `watch_dir` itself and takes `<name>.d` directories and `<name>.raw` files. Month subfolders are never seen.
- **It does not check that a copy has finished.** Deliver each run atomically: copy to `<name>.d.partial` or `<name>.raw.partial` (or to a staging directory on the same filesystem) and rename when the copy is complete. `.partial` and `.tmp` entries are skipped. A half-copied run that gets searched either fails (it is retried up to `max_attempts`) or, worse, succeeds and stores bad metrics that are never recomputed.
- **Symlinks are followed.** The dispatcher resolves each entry to its real path, so a directory of symlinks into an archive works, provided compute nodes can read the target.

| Route | Instrument PC needs | Notes |
|---|---|---|
| Scheduled copy task → staging name → rename, straight into `incoming/<instrument>/` | Nothing beyond Windows (robocopy) | Simplest. `scripts/flinders_copy.ps1` is the reference copier: pure PowerShell, a scheduled task every 5 min, copies only runs whose size stopped changing, `/IPG:20` at BelowNormal priority, never touches the source. It is timsTOF-only with UC Davis constants (`$SourceDir`, `$InstrumentDir`), and it copies in place into an archive. Adapt it, and add the rename |
| The lab already archives raw files (often nested by month) | Nothing | Write a small linker that symlinks new QC runs from the archive into the flat watch dir, and run it in the dispatch cron before `hive-dispatch`. Link a run only once its newest file is older than your copy window (for example 15 min). `link_flinders_qc.py` is the UC Davis version: QC-only by default, `--all-runs` for monitoring, idempotent, archive paths hardcoded |
| STAN watcher on the instrument PC, `processing_mode: hive` | A full Mode A install | Uploads as `.partial` then renames, which is correct. Set `submit_after_upload: false`: the SSH submit path maps `Y:\` to UC Davis's `/quobyte` (`_smb_to_quobyte_path` in `stan/sync/upload_to_hive.py`). Not recommended, because it puts STAN back on the acquisition PC |

Point each `watch_dir` at QC runs only if you do not want sample monitoring. Otherwise every non-QC file there gets a cheap monitor job (metadata only, never searched, never submitted).

**Verify:** acquire or copy one QC run and time it. It should appear in `incoming/<instrument>/` under its final name only after it is complete, and the next dispatcher tick should list it as `[qc]`.

---

## Phase 4 — Run it unattended

### 4.1 Dispatch cron script

Save this as `$STAN_HOME/cron_stan_dispatch.sh`. It follows the pattern of `scripts/cron_flinders_dispatch.sh`, the UC Davis version.

```bash
#!/bin/bash
# STAN dispatch tick: optionally link new raws, then submit up to
# max_submissions_per_run SLURM jobs. Login-node-safe: walks, links, sbatch only.
set -uo pipefail

# cron sets neither LOGNAME nor USER, and module init scripts read unbound
# variables (LOGNAME, then MANPATH). Under `set -u` that exits the SHELL
# before anything is logged; `|| true` never runs. Seed first, then drop -u
# across the sourcing. This order is asserted in tests/test_cron_scripts_executable.py.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"
set +u
source /etc/profile.d/modules.sh 2>/dev/null || true     # your site's module init
set -u

STAN_HOME=/shared/<lab>/stan
STAN="$STAN_HOME/venv/bin/stan"
CONFIG="$STAN_HOME/dispatch.yml"
LOG="$STAN_HOME/logs/cron_dispatch_$(date +%Y%m%d).log"
MAX_IN_FLIGHT=8          # SQLite: cap concurrent writers (Step 4.4)

{
  echo "===== tick $(date '+%F %T') on $(hostname) ====="
  # your linker here, if Phase 3 needs one
  if ! queued=$(squeue --me --noheader --format=%j 2>&1); then
    echo "squeue failed, skipping this tick: $queued"     # fail CLOSED
  else
    n=$(printf '%s\n' "$queued" | grep -c '^stan-' || true)
    if [ "$n" -ge "$MAX_IN_FLIGHT" ]; then
      echo "in flight: $n >= $MAX_IN_FLIGHT, not dispatching"
    else
      "$STAN" hive-dispatch --config "$CONFIG" 2>&1 | tail -3
      echo "dispatch exit=${PIPESTATUS[0]}"
    fi
  fi
  echo
} >> "$LOG" 2>&1
```

Use `squeue --me` or `id -un`, never `$USER`, in any guard. Cron does not reliably set `$USER`, and a guard that errors out must skip the tick, not wave everything through.

### 4.2 Install the crontab

On the login node you recorded in Phase 1:

```cron
*/5 * * * *  flock -n /tmp/stan_dispatch.lock  bash /shared/<lab>/stan/cron_stan_dispatch.sh
```

- **`bash <script>`, not `<script>`.** At UC Davis a cron script lost its execute bit, and cron answered `Permission denied` into nothing for eight days. With `bash` in the crontab line, the execute bit does not matter.
- **`flock -n`** skips a tick while the previous one is still running, rather than stacking them.
- Save the crontab in `SITE.md` too (`crontab -l > $STAN_HOME/crontab.txt`), with the host name.

**Verify** after 10 minutes:

```bash
tail -20 "$STAN_HOME/logs/cron_dispatch_$(date +%Y%m%d).log"    # "===== tick" lines + "Dispatch: scanned=…"
tail -1  "$STAN_HOME/logs/dispatch/dispatch_$(date +%Y%m%d).jsonl"
```

Cron has a minimal environment. If `hive-dispatch` logs `sbatch not on PATH`, the module init did not load SLURM. Source whichever profile script puts `sbatch` on `PATH` at your site.

### 4.3 Heartbeat: a watchdog outside what it watches

A cron job that stops writing looks exactly like "nothing is wrong". Run a separate script from its own crontab line that alarms when a log goes quiet. It measures silence, not success, so one check catches a lost execute bit, a syntax error, an unmounted share, a dead venv and a deleted crontab line alike. The reference implementation is `check_cron_heartbeat()` in `stan/reports/instrument_watch.py`; its log directory and job names are UC Davis values. A minimal generic version:

```bash
#!/bin/bash
# cron_stan_heartbeat.sh — alarm when a STAN cron log stops being written.
set -uo pipefail
LOGDIR=/shared/<lab>/stan/logs
now=$(date +%s); msg=""
check() {   # $1 = log glob, $2 = max silence in hours
  newest=$(ls -t $LOGDIR/$1 2>/dev/null | head -1)
  if [ -z "$newest" ]; then msg+=$'\n'"$1: no log at all"; return; fi
  age=$(( (now - $(stat -c %Y "$newest")) / 3600 ))
  if [ "$age" -gt "$2" ]; then msg+=$'\n'"$1: silent for ${age} h"; fi
}
check 'cron_dispatch_20*.log'        1
check 'cron_community_sync_20*.log' 14   # only if you run Step 5.2
check 'db_backup_20*.log'           30
echo "$(date '+%F %T') heartbeat ran${msg:- — all fresh}" >> "$LOGDIR/heartbeat_$(date +%Y%m%d).log"
if [ -n "$msg" ]; then
  printf 'STAN cron silence on %s:%s\n' "$(hostname)" "$msg" | mail -s "STAN heartbeat" <you@lab>
fi
```

```cron
*/20 * * * *  flock -n /tmp/stan_heartbeat.lock  bash /shared/<lab>/stan/cron_stan_heartbeat.sh
```

Use `mail`, a Slack webhook, or whatever your site allows. The heartbeat writes its own log, so another check can watch it.

**Verify:** temporarily add `check 'does_not_exist_20*.log' 1` and confirm that the alarm arrives.

### 4.4 SQLite on shared storage: concurrency and backups

Every SLURM job writes its own row into `stan.db`. UC Davis ran SQLite on Quobyte until about 100 concurrent jobs surfaced as `SQLITE_IOERR` and index corruption (May 11 and 16, 2026). A drain on 2026-08-26 lost about 37 monitor jobs in 11 minutes. That is why UC Davis moved to Postgres, but at 1.2.x the Postgres backend is wired to UC Davis's server, so other labs stay on SQLite and keep concurrency low:

- Set `max_submissions_per_run` low (`5`), and keep `MAX_IN_FLIGHT` in the dispatch cron (Step 4.1) at a level your filesystem tolerates. Start at 8.
- Back up daily with SQLite's online-backup API, which is safe while jobs write. Save it as `$STAN_HOME/cron_stan_db_backup.sh`:

```bash
#!/bin/bash
set -uo pipefail
STAN_HOME=/shared/<lab>/stan
LOG="$STAN_HOME/logs/db_backup_$(date +%Y%m%d).log"
{
  echo "===== backup $(date '+%F %T') on $(hostname) ====="
  "$STAN_HOME/venv/bin/python" - "$STAN_HOME" <<'EOF'
import sqlite3, sys, datetime, pathlib
home = pathlib.Path(sys.argv[1])
dest = home / "backups" / f"stan_{datetime.date.today():%Y%m%d}.db"
src, dst = sqlite3.connect(home / "db" / "stan.db"), sqlite3.connect(dest)
src.backup(dst)
print(dest, dst.execute("PRAGMA integrity_check").fetchone()[0], dest.stat().st_size, "bytes")
EOF
  echo "exit=$?"
  ls -1t "$STAN_HOME"/backups/stan_*.db | tail -n +31 | xargs -r rm -f     # keep 30
} >> "$LOG" 2>&1
```

```cron
17 3 * * *  flock -n /tmp/stan_db_backup.lock  bash /shared/<lab>/stan/cron_stan_db_backup.sh
```

**Verify:** the log line ends in `ok <bytes> bytes`. Report bytes with `stat` or Python, not `du`: on some NFS exports `du` reports block usage that looks like an empty file.

### 4.5 Recovering runs whose search finished but whose DB write failed

`stan ingest-orphans` finds `processing/<run>/report.parquet` files that have no `runs` row and re-extracts them. It recovers the instrument and family from the saved job script. Run it inside SLURM, dry run first:

```bash
srun --account=<acct> --partition=<part> --qos=<qos> --time=01:00:00 --cpus-per-task=2 --mem=8G \
  "$STAN_HOME/venv/bin/stan" ingest-orphans --backend sqlite \
    --db "$STAN_HOME/db/stan.db" \
    --processing-dir "$STAN_HOME/processing" \
    --sbatch-log-dir "$STAN_HOME/logs/sbatch" --dry-run
```

`--backend` defaults to `pg` (UC Davis), so always pass `--backend sqlite` and all three paths.

---

## Phase 5 — Dashboard and community benchmark (optional)

### 5.1 Dashboard

`stan dashboard` is a long-running web server, which most clusters do not allow on a login node. Run it on a lab machine that can read the cluster's shared storage. Point it at a **copy** of the database (the daily backup, or a more frequent copy made with the same backup API), not at the file that jobs are writing:

```bash
STAN_DB_PATH=/local/disk/stan_copy.db STAN_PG_REFRESH_SECONDS=0 stan dashboard --backend sqlite     # http://127.0.0.1:8421
```

`STAN_PG_REFRESH_SECONDS=0` keeps off a background task that copies UC Davis's Postgres into the local database every 5 minutes. That task starts only when `STAN_DB_BACKEND=pg` is set or the host has a PG Farm credential (a `PGPASSWORD` or UC Davis's token file), so on a lab machine without either the setting changes nothing. Keep it anyway: it also covers a shell that exports `PGPASSWORD` for another tool. The default bind is `127.0.0.1:8421`. `--host 0.0.0.0` exposes it to the network; do that only behind the lab's firewall. **Verify:** the page loads, and the runs from Step 2.8 appear.

### 5.2 Community benchmark and PEG sharing

These send aggregate metrics only: never raw files, never sample metadata. PEG sharing sends each run under an anonymous hash. Everything is opt-in and runs from a login-node cron (HTTP only). The login node needs egress to `https://brettsp-stan.hf.space`. No Hugging Face token is needed.

1. In the cron user's `~/.stan/community.yml`, set `display_name: <lab pseudonym>` and `community_submit: true`. Add `peg_share: true` to share PEG. Error telemetry to the relay is off unless you also set `error_telemetry: true`; a missing key means off.
2. Claim the name interactively on a login node with `stan community-claim`. It emails a 6-digit code from `noreply@stan-proteomics.org`; check spam. It writes `auth_token` into `community.yml`.
3. Preview what would be sent:

   ```bash
   STAN_DB_PATH="$STAN_HOME/db/stan.db" "$STAN_HOME/venv/bin/stan" submit-all --dry-run
   "$STAN_HOME/venv/bin/stan" peg-sync --backend sqlite --dry-run
   ```

   `peg-sync --backend sqlite` reads `~/.stan/stan.db` unless `STAN_DB_PATH` is set. Set it for both commands.

4. Schedule both commands every 6 h with the same hardened wrapper as Step 4.1 (dated `cron_community_sync_*.log`, `bash`, `flock`). `scripts/cron_community_sync.sh` is the UC Davis version: `submit-all` first, then `peg-sync`, each exit code logged separately so that a PEG failure cannot hold back a benchmark push.

Only DIA-NN 2.3.x results are accepted. Results appear on the public site after the nightly consolidation. For PEG details see [PEG_WATCH.md](PEG_WATCH.md).

**Verify:** the cron log shows `submit-all exit=0` and `peg-sync exit=0`. After the next consolidation, the lab's pseudonym appears at https://community.stan-proteomics.org.

---

## Master Prompt (paste into your coding agent)

Paste this block, followed by your Phase 1 output, into the agent that will do the install.

```
===== BEGIN STAN MODE C PROMPT =====
You are installing STAN (https://github.com/bsphinney/stan) on my SLURM cluster.
STAN is a proteomics QC tool: a login-node cron walks watch directories and submits
one SLURM job per raw file; each job runs DIA-NN or Sage and writes QC metrics to a
SQLite database.

Before doing anything, read in the repo:
  1. docs/INSTALL_MODE_C_HPC.md  — the procedure you must follow, in order
  2. stan/community/scripts/dispatch_hive.py  — _load_config, _walk_raws,
     _render_sbatch, _render_monitor_sbatch, _MONITOR_SLURM
  3. stan/community/scripts/run_one_v1.py  — DIANN_SIF, DIANN_BIN, SAGE_BIN,
     ASSET_CACHE, TRFP_DLL, run_diann (the apptainer --bind list), run_sage
  4. scripts/cron_flinders_dispatch.sh and scripts/cron_stan_db_backup.sh — the
     hardened cron pattern (UC Davis paths; copy the pattern, not the paths)
  5. docs/HPC_PATHS.md — the UC Davis reference deployment, for comparison only

Deliver, under STAN_HOME on shared storage:
  - a git clone on branch site/<cluster> carrying ONLY the site patch of Step 2.5
  - venv/ with `pip install -e src[peg]` on Python 3.10–3.12
  - dispatch.yml (Step 2.6; qc_pattern in SINGLE quotes)
  - smoke.sbatch (Step 2.7), and its passing output
  - cron_stan_dispatch.sh, cron_stan_heartbeat.sh, cron_stan_db_backup.sh, and the
    crontab lines, installed on ONE named login node
  - SITE.md recording every choice: sacctmgr rows used, module names, paths,
    login node holding the crontab, DIA-NN image provenance, open questions

Rules — do not break these:
  - Never invent partition, QOS, account, module or path names. Use only what
    my Phase 1 output shows. If a value is missing, ask me one specific question.
  - Never run compute on a login node; never write outputs or SLURM logs to /tmp;
    never use sudo; never `pip install --force-reinstall`.
  - DIA-NN must be exactly 2.3.0 and Sage exactly v0.14.7.
  - Leave STAN_DB_BACKEND unset; delete the PG lines from _render_sbatch.
  - Do not run scripts/hive_bootstrap.sh or copy UC Davis paths from any file.
  - After each Phase 2 step, run its "Verify" check and show me the output. Do
    not move on while a check fails.

Stop and ask me before: building or pulling a container image, installing a
crontab, submitting more than one real search job, or enabling community
submission.

My cluster (Phase 1 output):
<paste here>
===== END STAN MODE C PROMPT =====
```

---

## Review checklist (for the person supervising)

- [ ] `dispatch.yml` `slurm:` values appear together in one row of `sacctmgr`. So do the values in the patched `_MONITOR_SLURM`.
- [ ] `time:` is below the partition's `MaxTime`.
- [ ] Every path in `dispatch.yml` is on shared storage. None is under `/tmp`, and none is in a home directory unless home is mounted on compute nodes.
- [ ] `git -C $STAN_HOME/src diff origin/main --stat` touches only `run_one_v1.py` and `dispatch_hive.py`, and the Step 2.5 check prints `UC Davis strings left in job scripts: none`.
- [ ] The smoke-test log shows `DIA-NN 2.3.0`, `sage 0.14.6` (the v0.14.7 binary), pandas 2.x, and a detected mode for each vendor you run.
- [ ] A real QC run produced a `runs` row with non-zero IDs, and a non-QC run produced a `sample_health` row.
- [ ] The crontab lines use `bash <script>` and `flock -n`. `SITE.md` names the login node that holds them.
- [ ] The heartbeat alarm has been seen to fire at least once, in a test.
- [ ] A backup exists and its log reads `ok`.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ScannerError while scanning a double-quoted scalar` from `hive-dispatch` | `qc_pattern` is double-quoted (the `--print-default-config` template does this) | Use single quotes (Step 2.6) |
| `sbatch: error: Batch job submission failed: Invalid qos specification` | QOS and account come from different `sacctmgr` rows. For monitor jobs, `_MONITOR_SLURM` still holds UC Davis's triple | Use one complete row; patch `_MONITOR_SLURM` |
| Job pending with reason `(QOSGrpCpuLimit)` / `(QOSGrpGRES)` | Group quota exhausted | Wait, or switch `dispatch.yml` `slurm:` to another complete row you are allowed to use. `stan hive-dispatch --partition` only knows UC Davis triples |
| QC jobs fail at the end with `no PG Farm password` | `export STAN_DB_BACKEND=pg` still in `_render_sbatch` | Delete it and the `PGPASSWORD` block (Step 2.5) |
| DIA-NN job fails at once with an apptainer bind/mount error naming `/quobyte` or `/nfs` | The `--bind` list in `run_diann()` names a path your nodes do not have | Patch it to your storage roots |
| `apptainer: command not found` / `dotnet: command not found` in a job log | Module names in `_render_sbatch` are UC Davis's, and `\|\| true` hid the failure | Patch the `module load` lines; rerun the smoke test |
| Thermo DDA run searched as DIA, or Thermo mode `UNKNOWN` | TRFP missing from `~/.stan/tools/ThermoRawFileParser/`, or no `dotnet` in the job | Step 2.3 TRFP pre-install; load the .NET module in the job |
| DIA-NN returns 0 precursors on Thermo `.raw` | Image without .NET 8 | Use an image with the .NET 8 SDK; see [Appendix B](#appendix-b--net-8-for-thermo-raw-on-a-cluster-no-sudo) |
| Job fails in `_assets.sh` on `hf download` | Compute node has no egress, and the asset is missing from `ASSET_CACHE` | Pre-stage assets (Step 2.4); check that `ASSET_CACHE` points there |
| `sacct` shows COMPLETED but nothing was written | `.batch`/`.extern` steps always read COMPLETED | `sacct -j <id> -X`, then read `<sbatch_log_dir>/<stem>_<id>.out` |
| `report.parquet` exists but there is no `runs` row | DB write failed after the search | `stan ingest-orphans --backend sqlite …` (Step 4.5) |
| `module: command not found` over non-interactive `ssh` | Non-interactive shells skip the profile | `ssh <host> "bash -lc '…'"`, or source the module init explicitly |
| Cron seems dead; `crontab -l` says `no crontab` | You are on a different login node | Check the host recorded in `SITE.md` |
| Cron log never created | Script died before logging (`set -u` + module init), or lost its execute bit | Use the Step 4.1 preamble order; invoke with `bash` |
| `pip install` fails with `OSError [Errno 2] … INSTALLER<rand>.tmp` | `--force-reinstall` on a distributed filesystem | Use `--upgrade`; to start clean, delete and recreate the venv |
| `stan version` is older than expected | The clone was not rebased | `git -C $STAN_HOME/src fetch && git -C $STAN_HOME/src rebase origin/main` |

---

## Reference deployment: UC Davis Hive

UC Davis runs everything in this guide at scale. Its specifics are **not** instructions for other labs:

| | UC Davis (Hive) | Generic equivalent |
|---|---|---|
| Install | `/quobyte/proteomics-grp/brett/stan` checkout, editable-installed into `…/brett/stan_venv` (Python 3.11.9) | Step 2.2 |
| Raw file delivery | `flinders_copy.ps1` robocopy task → Flinders NFS archive (nested by month) → `link_flinders_qc.py --all-runs` symlinks into flat watch dirs | Phase 3 |
| Dispatch | `cron_flinders_dispatch.sh` every 5 min on `login2`; `max_submissions_per_run: 60`; jobs on `low` / `publicgrp-low-qos` / `publicgrp` | Phase 4 |
| Database | PG Farm (UC Davis Library Postgres) via `STAN_DB_BACKEND=pg` with a service-account secret; nightly `pg_dump` in SLURM ([PG_FARM.md](PG_FARM.md)) | SQLite + Step 4.4 |
| Watchdog | `cron_stan_alerts.sh` → `stan instrument-watch` (feed, publish and cron-heartbeat checks → Slack) | Step 4.3 |
| Community | `cron_community_sync.sh` every 6 h: `submit-all --backend pg`, then `peg-sync --backend pg` | Step 5.2 |
| Dashboard | Hosted on Azure (`ucd.stan-proteomics.org`), reading PG | Step 5.1 |

Paths, containers, account triples and the cron table: [HPC_PATHS.md](HPC_PATHS.md). Canonical copies of every cron and sbatch script live in `scripts/`. Their header comments record why each guard exists.

---

## Appendix A — `dispatch.yml` keys

From `_load_config()` and `_render_sbatch()` in `stan/community/scripts/dispatch_hive.py`.

| Key | Required | Default if absent | Notes |
|---|---|---|---|
| `db_path` | yes | — | SQLite file every job writes to |
| `out_root` | yes | — | `<out_root>/<run stem>/report.parquet` |
| `sbatch_log_dir` | yes | — | Job logs, `scripts/` (rendered job files, which `ingest-orphans` parses), `monitor/`, `monitor_workdir/` |
| `stan_venv` | yes | — | Each job runs `<stan_venv>/bin/stan hive-process` |
| `instruments` | yes, non-empty | — | See below |
| `dispatch_log_dir` | no | `sbatch_log_dir` | `dispatch_YYYYMMDD.jsonl`, one summary line per non-dry run |
| `slurm.partition` / `qos` / `account` | set all three | UC Davis values | One `sacctmgr` row |
| `slurm.time` / `cpus` / `mem` | no | `06:00:00` / `8` / `32G` | QC jobs only; monitor jobs use `_MONITOR_SLURM` (30 min, 4 CPU, 8 GB) |
| `max_submissions_per_run` | no | `50` | Cap on new jobs per dispatcher run |
| `max_attempts` | no | `3` | A raw file that failed this many times is skipped |
| `qc_pattern` | no | `DEFAULT_QC_PATTERN` | Match → search job; no match → monitor job. `''` makes every file QC |
| `instruments[].name` | yes | — | Stored as `runs.instrument`; must not be `auto` or `unknown` |
| `instruments[].family` | yes | — | IPS cohort key; `timsTOF`, `Lumos` and `Exploris` have built-in mappings |
| `instruments[].vendor` | yes | — | `bruker` or `thermo` |
| `instruments[].watch_dir` | yes | — | Flat directory; see Phase 3 |
| `instruments[].column_vendor` / `column_model` | no | — | Stamped on each run |
| `instruments[].amount_ng` | no | 50 | The instrument's usual HeLa load, passed as `--default-amount-ng`: a unit-anchored amount in the file name (`50ng`, `1ug`) wins, and the run records `amount_source` `parsed` or `assumed` (1.2.16; before, it was passed as `--amount-ng`) |
| `instruments[].lc_flow` | no | — | `nano`, `capillary` or `micro`; passed as `--lc-flow`, stamped on each run and sent to the community benchmark. Leave unset for an Evosep |
| `instruments[].spd` | no | — | Fallback only, used when raw-file metadata cannot resolve the samples-per-day value |

`sage_binary`, which `hive_bootstrap.sh` writes, is read by nothing.

---

## Appendix B — .NET 8 for Thermo `.raw` on a cluster (no sudo)

Two different things need .NET:

| Consumer | Needs | Where |
|---|---|---|
| DIA-NN 2.3.0 reading Thermo `.raw` | .NET 8 **SDK** (8.0.407+ per [external_tools.md](external_tools.md#current-known-versions-re-verify-before-use)) | Inside the DIA-NN image (preferred) or on compute nodes |
| ThermoRawFileParser DLL (mode detection, `.raw`→mzML for Sage) | a .NET 8 **runtime** | On compute nodes, found as `dotnet` on `PATH` |

Try these in order:

1. **An image with .NET inside.** This makes the DIA-NN half a non-issue on the host.
2. **A cluster module** (`module avail dotnet`). UC Davis uses `dotnet-core-sdk/8.0.4`.
3. **A user install on shared storage**, which needs no sudo:

   ```bash
   curl -fsSL https://dot.net/v1/dotnet-install.sh -o "$STAN_HOME/tools/dotnet-install.sh"
   bash "$STAN_HOME/tools/dotnet-install.sh" --channel 8.0 --install-dir "$STAN_HOME/tools/dotnet"
   # in the patched job script:
   export DOTNET_ROOT=$STAN_HOME/tools/dotnet; export PATH=$DOTNET_ROOT:$PATH
   ```

   `dotnet-install.sh` does not install system libraries. .NET also needs `libicu`, `libssl` and `libstdc++` from the OS. If they are missing, ask the admins; you cannot install them without root.

**Verify inside `srun`:** `dotnet --list-sdks` shows `8.x` (needed for DIA-NN outside a container), `dotnet --list-runtimes` shows `Microsoft.NETCore.App 8.x`, and the Step 2.7 smoke test reports a Thermo mode other than `UNKNOWN`. A populated `--list-runtimes` with an empty `--list-sdks` means runtime only. That is enough for TRFP but not for bare-binary DIA-NN.

The same 4-tier install for Ubuntu with root is `install_dotnet8_sdk()` in `stan_wsl_setup.sh` (Mode B).

---

## Appendix C — Other SLURM code paths (do not use for a new install)

- **`execution_mode: slurm`** plus a top-level `hive:` block in `instruments.yml` (`stan/search/dispatcher.py`, `stan/search/slurm.py`, needs the `[hpc]` extra). The watcher itself SFTPs a job script over SSH and polls until the job finishes. The job runs bare `diann` from `PATH` (no container), passes no QOS, and expects `output_dir` to be the same path on both machines. It predates the dispatcher, is not what the reference deployment runs, and is what the older [hpc_guide.md](hpc_guide.md) describes.
- **`processing_mode: hive`** (watcher on the instrument PC uploads, then SSH-submits): see the Phase 3 table. Use it only with `submit_after_upload: false`.

---

## Known gaps in 1.2.x (for the maintainer)

Every item below was found by reading the code on 2026-09-29. Each one is why a step above is written the way it is.

1. **Site constants are hardcoded**: `run_one_v1.py` (`DIANN_SIF`, `DIANN_BIN`, `SAGE_BIN`, `ASSET_CACHE`, `TRFP_DLL`, `--bind` list) and `dispatch_hive.py` (`_MONITOR_SLURM`, module names and `bruker_ff_dir` in `_render_sbatch`, the `--partition` map). These should come from `dispatch.yml` or environment variables so that other labs need no patch.
2. **`_render_sbatch` forces `STAN_DB_BACKEND=pg`** and reads a UC Davis token path. It should follow the dispatcher's own backend.
3. **The Postgres backend cannot be pointed elsewhere**: `PG_DEFAULTS` in `stan/db_pg.py` has no host or database override, and `psycopg2` is not a declared dependency (Hive's venv has `psycopg2-binary` installed separately).
4. **`--print-default-config` emits YAML that does not parse**: `qc_pattern` is double-quoted.
5. **No copy-completion check** in `_walk_raws` or `link_flinders_qc.py`. A run that is still being copied can be dispatched.
6. **Monitor jobs are not seen as in flight**: `_job_already_queued` looks for `stan-<stem>`, but monitor jobs are named `stan-mon-<stem>`, so a pending monitor job can be submitted again on the next tick. The Hive job history for the week to 2026-09-29 is consistent with this: 42 of 214 monitor raws were submitted 2–3 times.
7. **`diann_version` is stamped `2.3.0`** for every DIA run (`_extract_metrics`), whatever binary actually ran.
8. **DDA rows carry no `diann_version`**, so `submit_to_benchmark` falls back to running `diann` on the submitting host. On a login node without it, that gives `Invalid version format: unknown`, which rejects the row.
9. **`hive_bootstrap.sh` is stale**: it pulls an unpublished image, installs Sage from "latest", writes an unused `sage_binary` key, installs `alphatims` and `numpy<2` without `pandas<3`, and hardcodes UC Davis paths. UC Davis's own install was not built with it (`STAN/containers/diann.sif` and `STAN/sage/sage` do not exist on Hive).
10. **`stan install-peg-deps` does not pin `pandas<3`**. The `[peg]` extra does.
