<!-- Copy of ~/Documents/HT_work/CLAUDE.md as of 2026-09-03. That file is
     loaded as project memory in the scratch directory and is not itself
     version-controlled; this is the durable copy. Re-sync when it changes. -->

# HT_work — High-Throughput Proteomics Scripts

Scratch/dev directory for scripts supporting Brett Phinney's high-throughput proteomics
work (UC Davis Proteomics Core). Compute runs on the UC Davis **Hive** SLURM cluster.

Source of truth for cluster details, in priority order:
1. **`ls` on the cluster** — always beats any doc. Paths drift; containers get rebuilt.
2. `~/Documents/claude_private/HIVE_CLAUDE_GUIDE.md` — fullest Hive bootstrap doc
3. `~/Documents/STAN/docs/HPC_PATHS.md`, `hpc_guide.md`, `GOTCHAS_DELIMP.md`
4. `~/Documents/STAN/CLAUDE.md` § "HPC: Hive (UC Davis)"

---

## Access

```bash
ssh hive                    # alias in ~/.ssh/config → brettsp@hive.hpc.ucdavis.edu
```

Key-based auth via `~/.ssh/id_ed25519`. Verified working 2026-08-28 (lands on `login2`).

**SLURM commands need a login shell.** Non-interactive `ssh hive "sbatch ..."` will not
find `sbatch` on PATH:

```bash
ssh hive "bash -l -c 'sbatch job.sbatch'"
```

**ControlMaster** for repeated calls (macOS socket path must be ≤104 bytes — keep it in `/tmp`):

```bash
ssh -o ControlMaster=auto -o ControlPath=/tmp/.ht_brettsp_hive -o ControlPersist=300 \
    brettsp@hive.hpc.ucdavis.edu "<cmd>"
```

> `~/.ssh/config` contains a **second, dead `Host hive` block** with a
> `YOUR_HIVE_USERNAME` placeholder. SSH uses first-match-wins so the real block
> (listed first) applies — but delete the stale one if it ever causes confusion.

---

## SLURM

Valid `(account, partition, qos)` triples — verified 2026-08-28. **Never mix a QOS from
one row with an account from another** (→ `Invalid qos specification`):

| Partition | Account | QOS | Use |
|---|---|---|---|
| `high` | `genome-center-grp` | `genome-center-grp-high-qos` | Default for real searches. 64-CPU per-user cap (`MaxTRESPU`). |
| `high` | `publicgrp` | `publicgrp-high-qos` | Fallback when genome-center is capped. |
| `gpu-a100` | `genome-center-grp` | `genome-center-grp-gpu-a100-qos` | 1× A100/80GB. Casanovo/Cascadia. Use `--gres=gpu:a100:1`. |
| `low` | `publicgrp` | `publicgrp-low-qos` | Preemptible, huge capacity. Set `--requeue`. Fine for <30 min jobs. |

Brett's **default account is `publicgrp`**, so genome-center jobs MUST pass
`--account=genome-center-grp` explicitly.

```bash
squeue -u brettsp -o '%.10i %.14j %.9P %.2t %.10M %.6C %R'
sacct -j <jobid> --format=JobID,JobName%25,State,ExitCode,Elapsed
sacctmgr -nP list assoc user=brettsp format=account,partition,qos
```

- REASON `(None)` = just waiting. `(QOSGrpCpuLimit)` / `(QOSGrpGRES)` = quota capped → fall back to `low`.
- **`sacct` substeps lie**: `.extern` / `.batch` report COMPLETED even when the parent job failed.
  Filter with `grep -v "\."` or read the top-level JobID only.
- **Never run compute on the login node.** `sbatch`, or `srun --pty` for interactive.

---

## Storage

| Purpose | Path |
|---|---|
| Group root | `/quobyte/proteomics-grp/` (8.4P, 55% used) |
| Brett's scratch | `/quobyte/proteomics-grp/brett/` — writable, lab-visible |
| Per-user DE-LIMP | `/quobyte/proteomics-grp/de-limp/users/brettsp/` |
| STAN shared | `/quobyte/proteomics-grp/STAN/` |
| Raw archives | `/quobyte/proteomics-grp/to-hive/`, `/nfs/lssc0/flinders/proteomics/Data` |
| HeLa QCs | `/quobyte/proteomics-grp/hela_qcs/<instrument>/` |
| Node-local temp | `$SLURM_TMPDIR` or `/tmp/<job>_${SLURM_JOB_ID}` |

**Never write large artifacts to `~/` or `/home/brettsp/`** — 20 GB quota, and compute
nodes/lab members can't see it.

*Cleanup 2026-08-28*: home went 100% full → **38% (13 GB free)**. Removed ~11.9 GB of
regenerable cache (`.cache/uv`, `.cache/pip`, `.cache/conda`, `.cache/casanovo`,
`.apptainer/cache`) plus a stray `~/quobyte/` tree — 5.4 GB of conda packages created by a
**relative-path bug** (a write to `quobyte/...` instead of `/quobyte/...`). Four items with
possible value were *moved*, not deleted, to
`/quobyte/proteomics-grp/brett/home_rescue/`: `.predicted.speclib` (1.3 GB DIA-NN predicted
library), `cascadia_env` (788 MB conda env that existed nowhere else), `results.sage.tsv`,
`report-lib.parquet`. Delete those once you've confirmed they're not needed.

If a script ever recreates `~/quobyte/`, that's the relative-path bug again — find the
caller that dropped the leading slash.

---

## Tools

Always bind `/quobyte:/quobyte` so container paths match host paths.

### DIA-NN 2.3 (DIA)

```bash
apptainer exec --bind /quobyte:/quobyte \
  /quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif \
  /diann-2.3.0/diann-linux [flags]
```

**The single most-misused fact on this cluster:** there are two DIA-NN containers.

| Path | Thermo `.raw`? |
|---|---|
| `/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif` (underscore) | ✅ has .NET — **USE THIS** |
| `/quobyte/proteomics-grp/apptainers/diann2.3.0.sif` (no underscore) | ❌ no .NET — `.raw` **silently skipped** |

The wrong one doesn't error: it skips all `.raw`, searches only the FASTA, and hands back a
*predicted* library instead of an empirical one. Only tell is `0 files will be processed`
plus `dotnet: not found` in the log.

Other DIA-NN traps:
- Binary is `/diann-2.3.0/diann-linux`, **not** `diann`. `module load diann` does not exist.
- **Symlinks don't resolve inside containers** — bind the *parent* dir, don't symlink files into a staging dir.
- `--protein-q` is not a valid 2.3.0 flag.
- `--quant-ori-names` is REQUIRED on all steps of a parallel search (filenames mismatch across binds otherwise).
- With `--use-quant`, set `--mass-acc` / `--mass-acc-ms1` / `--window` explicitly — auto-mode gives different results than the original run.

### Sage (DDA) — standalone binary, no container

```
/quobyte/proteomics-grp/de-limp/cascadia/sage-v0.14.7-x86_64-unknown-linux-gnu/sage
```

- Bruker `.d` works directly. **Thermo `.raw` must be converted to mzML first.**
- v0.14.7 has no native protein grouping — post-hoc via `sage_protein_groups.py`.
- ~2× more PSMs than DIA-NN on timsTOF DDA; comparable on Orbitrap.

### msconvert (Thermo `.raw` → mzML)

```bash
apptainer exec --bind /quobyte:/quobyte \
  /quobyte/proteomics-grp/apptainers/pwiz-skyline-i-agree-to-the-vendor-licenses_latest.sif \
  wine msconvert file.raw --mzML --64 --zlib \
  --filter "peakPicking vendor msLevel=1-2" -o /path/to/output/
```

### Other containers (`/quobyte/proteomics-grp/apptainers/`)

`alphadia.sif` · `dia-analyst_v0.10.5.sif` · `radiant-fulcrum-2.3.3.sif` ·
`diamond_mriffle_2.1.10.sif` · `postgres16.sif` / `postgres18.sif` · `msconvert.sif`

### Conda envs — activate by PATH, not `conda activate`

```bash
export PATH="/quobyte/proteomics-grp/conda_envs/casanovo5/bin:$PATH"
```

`envs/`: `cascadia5`, `casanovo-gpu`, `casanovo5`, `instanovo_017_final`, `datasci`
`conda_envs/`: `R4.5.1`, `alphadia`, `dotnet`, `mztab`, `cassonovo_env` *(typo — do not rename)*

### Modules

`apptainer/latest` · `diamond/2.1.7` · `blast-plus/2.16.0` · `python/3.11.9` · `R/4.3.3`, `R/4.4.2`

---

## Reference data

| Item | Path |
|---|---|
| Human | `/quobyte/proteomics-grp/MRS/UP000005640_9606.fasta` |
| Human + universal contaminants | `/quobyte/proteomics-grp/MRS/UP000005640_9606_plus_universal_contam.fasta` |
| Other organisms | `/quobyte/proteomics-grp/de-limp/fasta/` (bovine, chicken, porcine, …) |
| DIAMOND DBs (+ `_reversed` decoys) | `/quobyte/proteomics-grp/bioinformatics_programs/blast_dbs/uniprot_{sprot,trembl}.dmnd` |
| Casanovo models | `/quobyte/proteomics-grp/bioinformatics_programs/casanovo_modles/` *(typo — do not rename)* |
| Cascadia models | `/quobyte/proteomics-grp/de-limp/cascadia/models/cascadia.ckpt` |

---

## sbatch template

```bash
#!/bin/bash -l
#SBATCH --job-name=ht-diann
#SBATCH --partition=high
#SBATCH --account=genome-center-grp
#SBATCH --qos=genome-center-grp-high-qos
#SBATCH --cpus-per-task=32
#SBATCH --mem=128G
#SBATCH --time=08:00:00
#SBATCH --output=logs/%x_%j.out
#SBATCH --error=logs/%x_%j.err

module load apptainer
mkdir -p logs                      # SLURM fails cryptically if the log dir is missing

apptainer exec --bind /quobyte:/quobyte \
  /quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif \
  /diann-2.3.0/diann-linux \
  --f /quobyte/proteomics-grp/brett/<data>/file.raw \
  --fasta /quobyte/proteomics-grp/MRS/UP000005640_9606_plus_universal_contam.fasta \
  --out /quobyte/proteomics-grp/brett/<out>/report.parquet \
  --qvalue 0.01 --quant-ori-names \
  --threads ${SLURM_CPUS_PER_TASK}
```

---

## Related projects

- **STAN** (`~/Documents/STAN`) — QC watcher: instrument → SLURM search → QC metrics → SQLite → dashboard
- **FRAN** (`~/Documents/FRAN`) — corpus/ingest layer over archived searches; Lance store, XIC extraction

Neither should be modified from here. This directory is for standalone HT scripts.

---

## CoreOmics (submission LIMS)

- API: `https://ucdavis.coreomics.com/server/api`, header `Authorization: Token <tok>`
- Token: `~/.coreomics_token` (or `$COREOMICS_TOKEN`). Lab filter `?lab=PROTEOMICS`
- `GET submissions/?lab=PROTEOMICS&page=N&page_size=200` — list (4,477 records, back to 2014)
- `GET submissions/<id>/` — detail; **only the detail view has `submission_data.samples`**
- Two IDs per submission: `id` = 12-hex (`1ed8b74497e4`), `internal_id` = `PROT_0793`.
  Filenames carry the **number** from `internal_id`; the API is keyed on the **hex id**.
- Only ~779 of 4,477 submissions have an `internal_id` — older `*_ARCHIVE` types don't.
- FRAN also caches this into Postgres (`coreomics_submissions_cache`) if SQL is easier.

### On-disk layout

```
/nfs/lssc0/flinders/proteomics/coreomics/projects/<YYYY>/<MM>/<hex id>/
```

**CORRECTION (2026-09-01):** the LIVE CoreOmics tree is the **Flinders NFS export**
above (months current through the latest submission; `.submission/` + `share/` per
submission, plus friendly symlink views under `views/{monthly,institute,pi,submission_id}/`
keyed by `PROT_XXXX`). The path `/quobyte/proteomics-grp/coreomics` used earlier this
session is a **STALE mirror** (stopped ~2026-03 / May 21) — do not trust it. My earlier
claim that "folder creation stopped in May" was wrong: it was the wrong filesystem.
The `share/` folder is CoreOmics-managed (auto-generated README); raws are NOT linked
into it — no submission has raw links in its CoreOmics folder.

`<YYYY>/<MM>` come from the **submitted** date. Dirs are created by `amschaal`,
group `proteomics-grp`, group-writable. 635 exist; most hold only `.submission/`.
Coverage is patchy — as of 2026-08-31, `projects/2026/` has only `01`, `02`, `03`.

## Raw data → submission linking

`scripts/symlink_submission_raws.py` links instrument raws into their CoreOmics folder.

Filenames encode the submission number in field 2:
`20260827_793_100spd_SI-60_S6-D8_1_24040.d` → `793` → `PROT_0793` → hex `1ed8b74497e4`.
Matching is anchored on `^\d{8}_(\d{2,4})_` so plate wells and instrument serials later in
the name can't be misread as a submission number.

```bash
python3 symlink_submission_raws.py --submission 793 --map-cache ~/.coreomics_map.json   # dry-run
python3 symlink_submission_raws.py --submission 793 --map-cache ~/.coreomics_map.json --commit
python3 symlink_submission_raws.py --all --commit
```

Dry-run by default; idempotent (re-runs report `ok`); a link pointing elsewhere is a
`CONFLICT` and is only rewritten with `--repair`. Falls back to
`<fallback-root>/PROT_XXXX/raw/` when the CoreOmics folder is missing —
currently `/quobyte/proteomics-grp/de-limp/users/brettsp/service/`.

**Naming coverage**: of 3,306 raws under `STAN/incoming/`, 538 have a date prefix but only
the 96 from PROT_0793 carry a submission number. The rest are HeLa/blank QC runs with no
submission — expected, not a parser failure.

⚠️ **Symlinks + Apptainer**: a container cannot follow a symlink whose target is outside
its bind mount. Any DIA-NN/Sage job reading these links must `--bind /quobyte:/quobyte`.
These links are for organisation and provenance, not data staging.

---

## QC scan + rerun exports

Two scripts, split because `matplotlib` is not in the Hive `stan_venv` — the scan
runs on Hive (where the data is), the exports run locally.

```bash
# on Hive: read frame counts + TIC straight from each .d/analysis.tdf
ssh hive "cd /quobyte/proteomics-grp/brett && python3 qc_scan.py"   # -> qc_scan.json

# locally: the three deliverables
python3 scripts/export_rerun_package.py --scan qc_scan.json --submission 793 \
    --run-date 20260902 --out-dir ./exports
```

Produces `PROT_xxxx_reruns.tsv`, `PROT_xxxx_rerun_queue.xlsx`, `PROT_xxxx_platemap.pdf`.

**Rerun classes** — `no_data` (no `analysis.tdf`; acquisition produced nothing) and
`at_blank` (sample TIC under `--blank-frac`, default 25%, of its *plate* median —
per-plate because plates differ in load). Modest low signal is deliberately not
flagged: a well at 60% of median is a real measurement of a weak sample.

**Queue format** (matched from `SERVICE/off_campus/PROTIFI/Aug_2026/Protifi_plate*.xlsx`):
sheet `SampleTable`, 12 columns, Tahoma 10. `Vial` is `S1-<well>` filled
**column-major** (A1,B1…H1,A2…). `Sample ID` is `<date>_<sub>_<method>_<sample>` and
stops at the sample name — HyStar appends `_S<tray>-<well>_1_<counter>` itself.

**Filename parsing is anchored on the well token**, not the submission number —
some trays are acquired without a number, and anchoring on it made the parser fall
through and emit whole filenames where a sample name belongs. In the queue that
becomes the name the instrument writes, so it matters.

## Evosep One — logs, column history, and what they proved (2026-09-02/03)

The Evosep records **Pressure, Actual-flow, Setpoint, Displacement and
Pump-speed for five pumps** per run, plus `execution-log.txt`, `journal.txt`
and `maintenance-info.txt`. Bruker's Compass DB has **no** pressure series, so
these logs are the only place a pressure trace exists.

| item | where |
|---|---|
| Log mirror (2023-07 → now, 31,432 runs) | `/quobyte/proteomics-grp/brett/evosep_logs/TIMS-10878_mirror/S00230/` |
| Collector (run on the instrument PC) | `Y:\brett\scripts\copy_evosep_logs.bat` — full history is the DEFAULT, mirrors incrementally, skips files already present, safe to interrupt |
| Extractor + tests (versioned) | `STAN/instrument/evosep/` |
| Deployed copy the cron runs | `/quobyte/proteomics-grp/STAN/evosep/` |

**Pump-HP is the analytical-column pump** (300–520 bar); pumps A–D make the
gradient and stay under 10 bar. **520 bar is the pump's cut-out** — it shuts
off and aborts the run.

### The one measurement that makes the rest work

`System-and-column-wash` procedures are **pressure-controlled** at a 400 bar
setpoint, so pressure is pinned and **flow is the resistance measurement**. It
needs no baseline, no reference and no normalisation — every point is measured
under identical conditions. Comparing raw pressures between methods running at
different flows is the trap; convert to bar/(µL/min) first.

**Brett's rule, validated 12 times:** *a wash cannot restore flow to the
fresh-column level; only a new column can.* Every recovery landed at 97–105 %
of that column's own fresh value, while the best wash recovery reached 2.17
against a fresh 2.26. Step **size** does not discriminate — one wash recovery
was +18.7 %, larger than a real column change at +10.4 %.

### Column history recovered from the washes

Twelve changes 2023-07 → 2026-09, **ten of which nobody logged**. Median
lifetime **71 days** (excluding the first, whose life is a floor). The column
retired 2026-09-02 lasted **33 days** — second shortest of eleven — against a
336-day predecessor.

This history is complete **because Brett changes columns only on failure**, so
every change leaves a decline-then-recovery signature. A proactively replaced
column would leave none and be invisible. If that practice changes, these
become lower bounds.

Three separate columns measured **186.1 / 185.5 / 186.9 bar/(µL/min)** fresh —
within 0.7 %. The lab has run one column type (PepSep Max C18 10 cm × 150 µm,
1.5 µm, Bruker part 1893483) for years, which is why that agreement holds and
why cross-column comparison is sound. `config/columns.yml` carries it as
`default_column_id`.

### What killed the 33-day column

Nothing was injected across the step:

```
08-12 20:28  355.7 bar   last run of the evening
   overnight             no analytical runs, only washes
08-13 11:11  402.4 bar   +47 bar from nothing that ran
08-13 18:20  388 → 442   08132026__60SPD_DIA-LRS-*, 13 injections, ~4 bar each
08-22 03:26              first cut-out
```

A physical step (emitter, fitting, frit, or debris shed by the washes) plus one
batch fouling at ~4 bar/injection. That column reached 426 bar in 15 days where
its predecessor took 10 weeks to go 340 → 397 — roughly **5× faster fouling**,
which is why five months passed with no cut-out and then six came in twelve days.

## Predictive signals — what is real and what is not (2026-09-03)

**Evotip misses are 47 % of all failures** (290 of 24,126 tasks; overall 1.20 %,
but real customer samples only 0.64 % — the headline is mostly blanks at 1.96 %
and QC/wash at 3.40 %).

**Position on the plate is NOT the risk factor** — that was a confound, found by
Brett and confirmed by stratification. Plates are filled from A1 outward, so low
rows and columns *are* the early-queue wells. Adjusting for rank destroys the
effect (edge OR 3.44 → 1.54, ns), and adding position to a rank-only model makes
prediction *worse* (AUC 0.806 → 0.786).

**Rank within the plate is the real signal:**

| rank among that plate's customer samples | miss rate |
|---|---|
| 1st | 3.12 % |
| 2nd–3rd | 1.35 % |
| 4th–10th | 0.27 % |
| 11th+ | 0.13 % |

24× spread, out-of-sample AUC 0.806, p < 0.002. **Lead each plate with something
expendable.**

**`syst.event_log` in Compass is not in the restore.** `extract_bruker.sh` uses
an inline ten-table whitelist; `keep_tables.txt` lists more but nothing reads
it. The table therefore exists but is empty. It holds multi-day hardware fault
states nothing else sees — 4,120 distinct `fan detection fault` events over six
days in 2025-10, ending 13 minutes before the HP pump serial changed 1140 →
1182. **Every ICF fault is written twice** (level 3 and level 2), so dedupe on
`source='HyStar' AND level=3`. A rule of *any level-3 message repeating >50
times in a day* fires on 10 days out of ~1,150 and never otherwise — the largest
ordinary day is 14, the smallest storm day 256.

**Column pressure predicts cut-outs weakly**: 32 % of cut-out days caught at 9 %
false positives. **75 % of cut-outs are sudden.** Pressure *slope* is null
(AUC 0.517); only level and step carry signal.

**No operator attribution exists** — `owner_user_id` is NULL on 24,115 of 24,126
tasks, so "who was at the instrument" is unrecoverable.

## Gotchas learned the hard way

- **`STAN/stan.db` corrupts repeatedly** — SQLite on Quobyte with dozens of concurrent
  SLURM writers. Five occurrences: May 11, Jun 10, Aug 26 (×2), Sep 1. Symptom is
  silent: every `stan-mon-*` job exits 1 in ~1 s at `init_db()`, the cron keeps
  submitting ~60/tick, and the HT plate map simply stops updating. **7,357 jobs failed
  in one day before anyone noticed.** Check first when the map looks stale:
  ```bash
  sqlite3 /quobyte/proteomics-grp/STAN/stan.db "PRAGMA integrity_check;"
  ```
  Repair (PG Farm is canonical; local SQLite holds only dispatch audit + sample_health):
  ```bash
  cd /quobyte/proteomics-grp/STAN
  cp stan.db stan.db.corrupt.$(date +%Y%m%d_%H%M%S)
  sqlite3 stan.db ".recover" | sqlite3 stan.db.repair
  sqlite3 stan.db.repair "PRAGMA integrity_check;"          # expect: ok
  flock /tmp/stan_flinders_cron.lock cp stan.db.repair stan.db
  ```
  Worth adding an integrity guard to the dispatcher so the next one fails loudly.
- **A tray continuing a queue without the submission number is normal**, not an error.
  `expand_submission_runs` joins trays by injection-counter contiguity; 0793/S5 is its
  worked example. Renaming is for *other* tools, never to make STAN work.
- **`du` dedups hardlinks within one invocation** — `du -sh a b` credits shared inodes
  to whichever it walks first, so per-directory sizes can mislead when comparing.

## STAN HT dashboard — three bugs found 2026-09-01

**1. "Colour samples by" dropdown did nothing** (`stan/metrics/ht_outliers.py:489`).
The stats-aggregation loop used `metric` as its loop variable, shadowing the
function *parameter* of the same name. By the time `plate_map(..., metric=metric)`
ran, `metric` held whatever came last out of the stats dict — always
`n_ms2_frames`. Every selection was silently discarded. Renamed the loop variable
to `mkey`.

**2. Dropdown offered unusable option values** (`public/index.html:7324`).
`stats` keys are plate-scoped (`S5:n_ms2_frames`) because outliers are scored per
plate, but the backend colours a well with a bare `r.get(metric)` on the run.
Feeding the prefixed key into `<option value>` asked for a column no run has, and
left `<select>` with no option matching the metric in use — so it displayed the
first one regardless of state. Now strips the prefix, dedupes, and looks stats up
under either spelling.

**3. Half the HeLa standards were never searched.** The QC filename pattern
required a digit immediately after `hel`:
`(?i)(he(l[a5\d]|\d)|qc|std[_\-\s]?he)`. Submission 0793 named its S6 standards
`Hel50` (matched → QC pipeline, precursor counts) and its S5 standards `Hel-50`
(**no match** → monitor pipeline, no search). So S5 showed "0 HeLa standards" and
its standards sat in the sample list coloured by `n_ms2_frames`. Widened to allow
an optional `_`/`-`/space before the digits. **The pattern is duplicated in six
files** (`watcher/qc_filter.py`, `community/scripts/{link_flinders_qc,dispatch_hive}.py`,
`pipeline/hive_process.py`, `cli.py` ×2) plus `dispatch.yml` plus the Hive fork —
all must be changed together or the dispatcher and the job body disagree about
routing. Worth collapsing to one definition.

**Renaming raws leaves orphan rows.** After the S5 rename, 39 injections carried
both an old-name and a new-name row in PG (221 sample rows for 182 injections) —
the wells processed before the rename were re-processed after it. The plate map
hides this (it keys by well, so duplicates overwrite), but the queue-trend chart
plots all of them and per-run statistics double-count. Clean up with:
```sql
DELETE FROM sample_health
 WHERE run_name LIKE '20260828\_100spd\_%' ESCAPE '\';   -- old-name S5 rows
```
Check the count with a SELECT first. Renaming raws already in the DB always needs
this follow-up.
