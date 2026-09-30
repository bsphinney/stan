# HPC Paths — UC Davis Hive (reference deployment)

> **This file describes one cluster: UC Davis's Hive.** It is the reference deployment that [INSTALL_MODE_C_HPC.md](INSTALL_MODE_C_HPC.md) is modelled on. **Other labs: do not copy these paths, accounts or credentials.** Use this file only to see what a working install looks like, then follow the Mode C guide to choose your own.
>
> Paths below were checked with `ls` on Hive on **2026-09-29**, except where marked. Paths move. Verify with `ls` on the cluster before relying on any of them.

## Connection

| | Value |
|---|---|
| Host | `hive.hpc.ucdavis.edu` (SSH alias `hive`) |
| User | `brettsp` (the UC Davis maintainer) |
| SSH key | `~/.ssh/id_ed25519` |
| Login nodes | `ssh hive` lands on `login1`. **STAN's crontab is on `login2`** (the cron logs say `on login2`), and `crontab -l` on `login1` prints `no crontab for brettsp` |
| Modules used by STAN | `python/3.11.9`, `dotnet-core-sdk/8.0.4`, `apptainer` (`apptainer/latest` is the default) |

SLURM triples: `sacctmgr -nP list assoc user=brettsp format=account,partition,qos`, checked 2026-09-29. Each row is one valid `--account` / `--partition` / `--qos` combination. Never mix across rows.

| Account | Partition | QOS | Use |
|---|---|---|---|
| `publicgrp` | `low` | `publicgrp-low-qos` | **What STAN's dispatcher uses** (`dispatch.yml`), plus monitor jobs. Preemptible, large capacity. `MaxTime=7-00:00:00` |
| `genome-center-grp` | `high` | `genome-center-grp-high-qos` | Priority CPU; 64-CPU per-user cap (not re-checked 2026-09-29). `MaxTime=30-00:00:00` |
| `publicgrp` | `high` | `publicgrp-high-qos` | Open-access alternative on `high` |
| `genome-center-grp` | `gpu-a100` | `genome-center-grp-gpu-a100-qos` | A100 GPU (Casanovo) |

`brettsp`'s default account is `publicgrp`, so a genome-center job must pass `--account=genome-center-grp` explicitly.

## STAN deployment on Hive

| What | Path | Notes |
|---|---|---|
| Code checkout | `/quobyte/proteomics-grp/brett/stan` | Tracks `main`. The venv imports straight from it, so `git pull` here **is** the deploy |
| Python venv | `/quobyte/proteomics-grp/brett/stan_venv` | Editable install, Python 3.11.9; numpy 1.26.4, pandas 2.3.3, alphatims 1.0.8, and `psycopg2-binary` 2.9.12 (installed separately; not a declared dependency) |
| Dispatcher config | `/quobyte/proteomics-grp/STAN/dispatch.yml` | `low` / `publicgrp-low-qos` / `publicgrp`, `max_submissions_per_run: 60`, `qc_pattern` single-quoted |
| Watch dirs | `/quobyte/proteomics-grp/STAN/incoming/{TIMS-10878, lumosRox, Orbitrap Exploris 480}` | Flat directories of **symlinks** into the Flinders archive, made by the linker. `incoming/timsTOF HT` and `incoming/DESKTOP-FOT3DAA` are older and not in `dispatch.yml` |
| Linker (standalone copy) | `/quobyte/proteomics-grp/brett/link_flinders_qc.py` | Module: `stan/community/scripts/link_flinders_qc.py` |
| Raw archive | `/nfs/lssc0/flinders/proteomics/Data/raw_data/{tTOF_HT, Lumos1, Exploris480}` | Nested by month; fed by `scripts/flinders_copy.ps1` on the instrument PCs. Compute nodes mount `/nfs` |
| Search outputs | `/quobyte/proteomics-grp/STAN/processing/<run stem>/` | `out_root`. Every QC run writes a `report.parquet` here; see the FRAN warning below |
| Job logs + rendered scripts | `/quobyte/proteomics-grp/STAN/logs/sbatch/` (`scripts/`, `monitor/`) | `sbatch_log_dir` |
| Dispatcher JSONL | `/quobyte/proteomics-grp/STAN/logs/dispatch/` | `dispatch_log_dir` |
| Cron logs | `/quobyte/proteomics-grp/STAN/logs/` | Names are listed in `CRON_LOGS` in `stan/reports/instrument_watch.py` |
| Running cron and sbatch scripts | `/quobyte/proteomics-grp/STAN/*.sh`, `*.sbatch` | Canonical copies: `scripts/`, `instrument/evosep/`, `instrument/bruker/` in the repo |
| SQLite file named by `db_path` | `/quobyte/proteomics-grp/STAN/stan.db` | Not the store of record. Jobs run with `STAN_DB_BACKEND=pg` and write PG Farm |
| PG Farm credential | `/quobyte/proteomics-grp/brett/.pgfarm_token` | Holds the long-lived service-account **secret**, not a token. Its mtime says nothing about whether it still works. See [PG_FARM.md](PG_FARM.md) |
| PG document cache | `/quobyte/proteomics-grp/STAN/cache` | `STAN_PG_DOC_CACHE_DIR` for the alerts cron |
| DB backups | `/nfs/lssc0/flinders/proteomics/Data/stan-db-backups` | Nightly `pg_dump` via `stan_db_backup.sbatch` |

Tools the job body uses. These are the constants in `stan/community/scripts/run_one_v1.py` and `dispatch_hive.py` that another cluster must patch (see [Mode C Step 2.5](INSTALL_MODE_C_HPC.md#25-apply-the-site-patch)):

| Constant / variable | Hive path | Notes |
|---|---|---|
| `DIANN_SIF` / `DIANN_BIN` | `/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif` / `/diann-2.3.0/diann-linux` | .NET bundled; reads `.raw`, `.d` and `.mzML` |
| `SAGE_BIN` | `/quobyte/proteomics-grp/de-limp/cascadia/sage-v0.14.7-x86_64-unknown-linux-gnu/sage` | The v0.14.7 release binary; `--version` prints `sage 0.14.6` |
| `ASSET_CACHE` | `/quobyte/proteomics-grp/brett/stan_community_assets` | Community FASTA + spectral libraries |
| `TRFP_DLL` | `/quobyte/proteomics-grp/tools/ThermoRawFileParser/ThermoRawFileParser.dll` | Run as `dotnet <dll>` with `dotnet-core-sdk/8.0.4` loaded |
| `STAN_TRFP_SIF` default | `/quobyte/proteomics-grp/STAN/historical_bsa/trfp.sif` | ThermoRawFileParser 1.4.5 image for Thermo PEG ([PEG_WATCH.md](PEG_WATCH.md#thermo-peg-on-hive)) |
| `STAN_BRUKER_FF_DIR` | `/quobyte/proteomics-grp/brett/bruker_ff` | 4DFF binary at `linux/uff-cmdline2`; needs `LD_LIBRARY_PATH` to include `linux/` |
| TRFP for mode detection | `/home/brettsp/.stan/tools/ThermoRawFileParser/` | Auto-downloaded by `stan.tools.trfp.ensure_installed()` |

These do **not** exist on Hive, so the reference install was not built with it: `STAN/containers/diann.sif` and `STAN/sage/sage`, which are the targets of `scripts/hive_bootstrap.sh`.

### Do not move STAN's processing output without telling FRAN

FRAN (a separate UC Davis system) excludes `/quobyte/proteomics-grp/STAN/` by a hardcoded prefix (`DEFAULT_EXCLUDES` in FRAN's `ingest/find_uningested.py`). Without that exclusion, every STAN QC `report.parquet` would be ingested as a customer search. If STAN's output path ever moves, update that exclusion in the same change.

## Crontab (`brettsp` on `login2`)

Cadences are taken from each script's install comment and CLAUDE.md. The crontab itself was not readable from `login1` on 2026-09-29. Every entry is wrapped in `flock -n`. Entries marked **bash** are invoked as `bash <script>`, so a lost execute bit cannot silence them.

| Script | Cadence | Does |
|---|---|---|
| `cron_flinders_dispatch.sh` | every 5 min | Link new Flinders raws (QC + samples, rolling 30 days), then `stan hive-dispatch` (up to 60 jobs) |
| `cron_count_acquisitions.sh` | every 15 min (+ full scan 3×/day) | Per-day acquisition counts → utilisation |
| `cron_ht_watch.sh` | every 20 min | timsTOF watch/status |
| `cron_evosep.sh` | every 30 min | Evosep column-health extract |
| `cron_ioncloud.sh` | hourly at :17 | Feature-cloud backfill from existing 4DFF sidecars |
| `cron_community_sync.sh` | every 6 h at :25 | `stan submit-all --backend pg`, then `stan peg-sync --backend pg` |
| `cron_bruker_maintenance.sh` | daily 20:00 | Compass backup → maintenance document |
| `cron_stan_db_backup.sh` (**bash**) | daily 03:17 | Submits `stan_db_backup.sbatch` (`pg_dump` via `postgres16.sif`) |
| `cron_stan_alerts.sh` (**bash**) | every 20 min | `stan instrument-watch`: feed, publish and cron-heartbeat checks → Slack |

## Containers (Apptainer)

| Container | Path | Notes |
|---|---|---|
| **DIA-NN 2.3 (with Thermo .raw support)** | `/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif` | Has .NET; reads `.raw`, `.d` and `.mzML`. The one STAN uses |
| DIA-NN 2.3 (Bruker only, NO .raw) | `/quobyte/proteomics-grp/apptainers/diann2.3.0.sif` | Missing .NET; `.raw` files fail with `dotnet: not found` or are silently skipped |
| ThermoRawFileParser 1.4.5 | `/quobyte/proteomics-grp/STAN/historical_bsa/trfp.sif` | Thermo PEG. Built from `quay.io/biocontainers/thermorawfileparser:1.4.5--ha8f3691_0` (`apptainer inspect`) |
| Postgres 16 client | `/quobyte/proteomics-grp/apptainers/postgres16.sif` | `pg_dump` for the nightly backup (a client older than the server is refused) |
| msconvert (ProteoWizard) | `/quobyte/proteomics-grp/apptainers/pwiz-skyline-i-agree-to-the-vendor-licenses_latest.sif` | `wine64 msconvert file.raw --mzML`. Not used by STAN |
| alphaDIA | `/quobyte/proteomics-grp/apptainers/alphadia.sif` | Not used by STAN (not re-checked) |
| DE-LIMP | `/quobyte/proteomics-grp/de-limp/containers/de-limp.sif` | Sister project (not re-checked) |

The DIA-NN binary inside the container is `/diann-2.3.0/diann-linux`, not `diann`. Two images differ only by an underscore; see [external_tools.md → DIA-NN containers on Hive](external_tools.md#dia-nn-containers-on-hive--critical). Run command:

```bash
apptainer exec --bind /quobyte:/quobyte \
  /quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif \
  /diann-2.3.0/diann-linux [flags]
```

## FASTA files

STAN's community searches use the frozen `human_hela_202604.fasta` in `ASSET_CACHE` (above), not these files.

| Species | Path |
|---|---|
| Human (HeLa) | `/quobyte/proteomics-grp/MRS/UP000005640_9606.fasta` |
| Human + contaminants | `/quobyte/proteomics-grp/MRS/UP000005640_9606_plus_universal_contam.fasta` (not re-checked) |
| Bovine | `/quobyte/proteomics-grp/de-limp/fasta/UP000009136_bos_taurus.fasta` (not re-checked) |
| Chicken | `/quobyte/proteomics-grp/de-limp/fasta/UP000000539_gallus_gallus.fasta` (not re-checked) |
| Porcine | `/quobyte/proteomics-grp/de-limp/fasta/UP000008227_sus_scrofa.fasta` (not re-checked) |

## Other lab tools on Hive (not used by STAN; not re-checked 2026-09-29)

| What | Path |
|---|---|
| DIAMOND SwissProt | `/quobyte/proteomics-grp/bioinformatics_programs/blast_dbs/uniprot_sprot` |
| DIAMOND TrEMBL | `/quobyte/proteomics-grp/bioinformatics_programs/blast_dbs/uniprot_trembl` |
| DE-LIMP shared storage | `/quobyte/proteomics-grp/de-limp/` (per-user output `…/{username}/output/`, FASTA `…/fasta/`, downloads `…/downloads/`) |
| Cascadia training / env / model | `/quobyte/proteomics-grp/de-limp/cascadia/training/`, `/quobyte/proteomics-grp/envs/cascadia5/`, `/quobyte/proteomics-grp/de-limp/cascadia/models/cascadia.ckpt` |
| Casanovo env / model | `/quobyte/proteomics-grp/conda_envs/cassonovo_env/`, `/quobyte/proteomics-grp/bioinformatics_programs/casanovo_modles/casanovo_v4_2_0.ckpt` (both directory names are misspelled on disk) |

## SLURM notes

- Non-interactive `ssh hive "sbatch …"` does not find `sbatch`. Use `ssh hive "bash -lc '…'"`.
- DIA-NN is not a module; `module load diann` does not work. Use the container.
- `sacct` reports the `.batch` and `.extern` steps as COMPLETED even when the job failed. Use `sacct -j <id> -X`.
- Check the queue with `squeue -u brettsp -o '%.10i %.12j %.9P %.2t %.10M %.6C %.8m %R'`. The REASON column says `(None)` while a job waits for the scheduler, and `(QOSGrpCpuLimit)` / `(QOSGrpGRES)` when the group quota is full.
- Never `scancel -u brettsp` with only a state or partition filter: it also kills DE-LIMP jobs. Filter by job name (`stan-*`).
- Measured over the week to 2026-09-29: per-raw QC jobs (8 CPUs, 32 GB, `low`) took a median of 10 min and at most 26 min (24 jobs). Monitor jobs took a median of seconds (242 jobs).
