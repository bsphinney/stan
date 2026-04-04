# STAN HPC Guide (SLURM on UC Davis Hive)

This guide covers running STAN's search engines on a SLURM HPC cluster. Most users will run locally on their instrument workstation — this is only needed if you want to offload searches to a cluster.

## Connection

```
Host: hive.hpc.ucdavis.edu
User: your_username
SSH key: ~/.ssh/id_ed25519
```

## SLURM Settings

```yaml
# In instruments.yml
execution_mode: "slurm"
hive_partition: "high"
hive_account: "genome-center-grp"       # your SLURM account
```

| Setting | Value |
|---------|-------|
| Partition | `high` (recommended — `low` gets preempted) |
| QOS | auto-assigned from account + partition |
| Per-user CPU limit | 64 CPUs on `high` |
| GPU partition | `gpu-a100` (for future deep learning features) |

## DIA-NN on HPC

### Containers — READ THIS CAREFULLY

There may be multiple DIA-NN containers on your cluster with similar names. Not all of them support Thermo `.raw` files.

At UC Davis Hive, there are two:

| Container | Path | `.raw` support |
|-----------|------|----------------|
| **CORRECT** | `/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif` | YES — has .NET bundled |
| WRONG | `/quobyte/proteomics-grp/apptainers/diann2.3.0.sif` | NO — `.raw` files silently skipped |

The wrong container will not error visibly — it will skip all `.raw` files, process only the FASTA, and produce a predicted library instead of an empirical one. The only clue is `0 files will be processed` in the log.

### Binary Path

The DIA-NN binary inside the container is at `/diann-2.3.0/diann-linux`, NOT on the container's PATH.

```bash
# WRONG — "executable not found" error
apptainer exec container.sif diann --f file.raw

# CORRECT
apptainer exec container.sif /diann-2.3.0/diann-linux --f file.raw
```

### Bind Mounts

Bind your data, FASTA, and output directories into the container. Use container-relative paths in all DIA-NN flags.

```bash
apptainer exec \
    --bind /path/to/data:/work/data,/path/to/fasta:/work/fasta,/path/to/output:/work/out \
    /path/to/diann_2.3.0.sif \
    /diann-2.3.0/diann-linux \
    --f /work/data/file.raw \
    --fasta /work/fasta/human.fasta \
    --out /work/out/report.parquet \
    --threads 32
```

### Symlinks Do Not Work Inside Containers

If your data directory contains symlinks pointing to files in other directories, the container cannot follow them — the symlink target path is not mounted.

```bash
# WRONG — symlinks break inside the container
ln -sf /data/8min/file.raw /selected/file.raw
apptainer exec --bind /selected:/work/data container.sif ...
# ERROR: file /work/data/file.raw does not exist

# CORRECT — bind the parent directory so all subdirs are accessible
apptainer exec --bind /data:/work container.sif \
    /diann-2.3.0/diann-linux --f /work/8min/file.raw
```

### Invalid Flags in DIA-NN 2.3.0

| Flag | Status |
|------|--------|
| `--protein-q` | NOT VALID — produces "unrecognised option" warning |
| `--fasta-search --predictor` | Step 1 only — do NOT include in Steps 2-5 of parallel workflow |
| `--quant-ori-names` | REQUIRED on all parallel steps — preserves filenames across bind mounts |

### Example SLURM Script

```bash
#!/bin/bash -l
#SBATCH --job-name=stan-diann
#SBATCH --partition=high
#SBATCH --account=genome-center-grp
#SBATCH --cpus-per-task=32
#SBATCH --mem=128G
#SBATCH --time=08:00:00
#SBATCH --mail-type=END,FAIL
#SBATCH --mail-user=you@university.edu

module load apptainer

apptainer exec \
    --bind /path/to/data:/work \
    /path/to/diann_2.3.0.sif \
    /diann-2.3.0/diann-linux \
    --f /work/hela_qc.raw \
    --fasta /work/human_uniprot.fasta \
    --fasta-search \
    --predictor \
    --gen-spec-lib \
    --out /work/output/report.parquet \
    --out-lib /work/output/report-lib.parquet \
    --qvalue 0.01 \
    --threads ${SLURM_CPUS_PER_TASK}
```

## Sage on HPC

Sage is a standalone binary, no container needed.

```bash
# Location on Hive
/quobyte/proteomics-grp/de-limp/cascadia/sage-v0.14.7-x86_64-unknown-linux-gnu/sage

# Run directly
/path/to/sage config.json
```

For Thermo DDA on HPC, you must convert `.raw` to mzML first (Sage does not read `.raw`). Use the msconvert container:

```bash
apptainer exec --bind /quobyte:/quobyte \
    /quobyte/proteomics-grp/apptainers/pwiz-skyline-i-agree-to-the-vendor-licenses_latest.sif \
    wine msconvert file.raw --mzML --64 --zlib \
    --filter "peakPicking vendor msLevel=1-2" \
    -o /path/to/output/
```

Bruker `.d` files work directly with Sage — no conversion needed.

## FASTA Files

| Species | Path (Hive) |
|---------|-------------|
| Human (for HeLa QC) | `/quobyte/proteomics-grp/MRS/UP000005640_9606.fasta` |
| Human + contaminants | `/quobyte/proteomics-grp/MRS/UP000005640_9606_plus_universal_contam.fasta` |

For other organisms, check `/quobyte/proteomics-grp/de-limp/fasta/`.

## Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `FATAL: "diann": executable file not found` | Wrong binary path | Use `/diann-2.3.0/diann-linux` |
| `dotnet: not found` + `0 files will be processed` | Wrong container (missing .NET) | Use `diann_2.3.0.sif` from `dia-nn/` dir |
| `file does not exist` inside container | Symlinks or wrong bind mount | Bind parent dir, don't use symlinks |
| `unrecognised option [--protein-q]` | Invalid flag | Remove `--protein-q` |
| `QOSMaxCpuPerUserLimit` | Hit 64 CPU limit | Wait for other jobs to finish, or use `low` partition |
| `squashfuse_ll failed to mount` | Transient node issue | Resubmit — will get a different node |
| `incorrect settings, the in silico-predicted library must be generated in a separate pipeline step` | `--fasta-search` used with existing library | This is a warning, not fatal. For library-free mode it's expected. |
