# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Project**: STAN — Standardized proteomic Throughput ANalyzer  
> **Author**: Brett Stanley Phinney, UC Davis Proteomics Core  
> **GitHub**: https://github.com/bsphinney/stan  
> **HF Space**: https://huggingface.co/spaces/brettsp/stan  
> **HF Dataset**: https://huggingface.co/datasets/brettsp/stan-benchmark  
> **Master spec**: `STAN_MASTER_SPEC.md` — read this first, it is the authoritative design doc  
> **Last updated**: May 2026

---

## Golden Rule: Document Everything As You Go

**Every change to this project must be documented in the same commit.**

This means:
- Code change → update README.md (Implementation Status / TODO) + docs/user_guide.md
- Schema change → update HF Space relay API + dashboard
- New metric → update README, user guide, dashboard figures, submission schema
- HPC discovery → update memory files + docs/HPC_PATHS.md
- Bug found → add to docs/GOTCHAS_DELIMP.md or memory
- Design decision → save to memory with rationale (why, not just what)

If you can't explain where the documentation lives for a change you just made,
the change is not done yet.

**Reference files to always check before HPC work:**
- `docs/FEATURES.md` — STAN feature highlights, quick orientation
  (start here in new Claude Code sessions)
- `docs/HPC_PATHS.md` — container paths, FASTA locations, storage layout
- `docs/GOTCHAS_DELIMP.md` — 50+ hard-learned lessons (DIA-NN, SLURM, data)
- `docs/QUEUE_SWITCHING.md` — auto partition switching logic
- `docs/PG_FARM.md` — central Postgres at UC Davis Library: connection,
  schema, `STAN_DB_BACKEND=pg`, cron sync, orphan recovery, weekly
  token rotation. Read before any change to the DB write path.

---

## Build, Test, Lint Commands

```bash
pip install -e ".[dev]"              # install in dev mode
stan init                             # creates ~/.stan/instruments.yml
stan watch                            # start watcher daemon
stan dashboard                        # serve dashboard at http://localhost:8421
pytest tests/ -v                      # run all tests
pytest tests/test_metrics.py -v       # run a single test file
pytest tests/ -k "not integration"    # skip tests requiring Hive/SLURM
pytest tests/ -k "test_ips"           # run a single test by name
ruff check stan/                      # lint
ruff check stan/ --fix                # lint with auto-fix
```

Tests marked `@pytest.mark.integration` require Hive SLURM access and are skipped in CI.

---

## Architecture Overview

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

**Data flow**: watcher detects new raw files → auto-detects DIA/DDA mode → submits SLURM search job (DIA-NN or Sage) → extracts QC metrics from search results → evaluates pass/fail thresholds → stores in SQLite + optionally submits to community benchmark.

**Key modules**: `watcher/` (file monitoring + mode detection), `search/` (DIA-NN/Sage local subprocess + SLURM dispatcher), `metrics/` (metric extraction + IPS scoring), `gating/` (threshold evaluation + HOLD flag), `community/` (HF Dataset submission/fetch), `dashboard/` (FastAPI + single-file React UI in public/index.html).

**Three external repos**: GitHub (code), HF Space (public dashboard), HF Dataset (community benchmark data).

---

## Current deployment — UC Davis Proteomics Core

**As of September 2026 this facility runs 100 % Hive + PG Farm.** The
instrument PCs acquire and nothing else: no watcher, no local `stan.db`,
no `stan dashboard`, no `update-stan.bat`. Anything in this file that
describes software running on an instrument PC is describing a
deployment *other labs* may use, not this one — those sections are
marked.

```
Instrument PC (acquire only)
    │  robocopy / vendor transfer
    ▼
Flinders archive        /nfs/lssc0/flinders/proteomics/Data/raw_data/<instrument>
    │                   (tTOF_HT | Lumos1 | Exploris480)
    ▼
Hive cron  ── every 5 min ─────────────────────────────────────────────┐
    │  symlink into the dispatcher watch dirs, sbatch the search       │
    │  DIA-NN / Sage on partition `low`, metrics, gating               │
    ▼                                                                  │
PG Farm  uc-davis-genome-center-proteomics-core/stan   ← store of record│
    │                                                                  │
    ├──► ucd.stan-proteomics.org   hosted dashboard (Azure)            │
    └──► HF Space relay            public community benchmark          │
```

### The three places code actually runs

| Where | What | Version check |
|---|---|---|
| Hive | `/quobyte/proteomics-grp/brett/stan` checkout, editable-installed into `/quobyte/proteomics-grp/brett/stan_venv`. **The venv imports straight from the checkout, so `git pull` there IS the deploy.** | `ssh hive "/quobyte/proteomics-grp/brett/stan_venv/bin/python -c 'import stan;print(stan.__version__)'"` |
| Azure | `ucd.stan-proteomics.org`, app `stan-ucd-proteomics` in `rg-fran`. Separate zip deploy — **does not** follow a `git push`. Runbook: `docs/AZURE_DEPLOY.md`. | `curl -s https://ucd.stan-proteomics.org/api/version` |
| HF Space | `brettsp/stan` relay + public community dashboard. Its own version line. | `curl -s https://brettsp-stan.hf.space/api/version` |

**These drift independently and routinely do.** Measured 2026-09-04:
Hive on 1.0.84, Azure on 1.0.82, main on 1.0.85. So "is the fix live?"
is three questions, not one, and pushing to GitHub answers none of them
by itself. Check the surface the person is actually looking at.

### Hive cron — what runs unattended

All in `/quobyte/proteomics-grp/STAN/`, from brettsp's crontab, each
wrapped in `flock` so a slow tick is skipped rather than stacked.
Canonical copies live in `scripts/`.

| Script | Cadence | Does |
|---|---|---|
| `cron_flinders_dispatch.sh` | */5 min | Walk the Flinders export, symlink new raws, sbatch up to 50 searches |
| `cron_count_acquisitions.sh` | */15 min (+ full 3×/day) | Per-day acquisition counts → utilisation snapshot |
| `cron_ht_watch.sh` | */20 min | timsTOF watch/status |
| `cron_evosep.sh` | */30 min | Evosep column health extractor |
| `cron_ioncloud.sh` | hourly | Feature-cloud backfill from existing 4DFF sidecars |
| `cron_community_sync.sh` | */6 h | Push to the community benchmark |
| `cron_bruker_maintenance.sh` | daily 20:00 | Compass BACKUP → maintenance document |
| `cron_stan_db_backup.sh` | daily 03:17 (**staged, not installed**) | pg_dump PG Farm → Flinders |

`export STAN_DB_BACKEND=pg` is set inside these scripts — that is what
`use_pg()` keys off. A script that forgets it silently writes SQLite.

### Database backups

PG Farm is the store of record and **is not known to keep its own
backups**. STAN had none at all until 2026-09-04 — `de-limp-db-backups`
sat next to an empty space where STAN's should have been. The dump is
now `scripts/stan_db_backup.sbatch` (running copy at
`/quobyte/proteomics-grp/STAN/`), writing to
`/nfs/lssc0/flinders/proteomics/Data/stan-db-backups`. 42.7 MB, ~15 s,
30 daily generations.

Modelled on FRAN's `fran_db_backup.sbatch`, which paid for each guard
once. The ones that matter:

- **No `pg_dump` on Hive** — use `/quobyte/proteomics-grp/apptainers/postgres16.sif`
  (16.15 client vs 16.14 server; a client OLDER than the server is
  refused outright). Dumps written by an 18.4 client are archive 1.16 and
  `pg_restore` 16.15 cannot even list them, so pin the client.
- **`--bind /nfs/lssc0/flinders/proteomics` is required** — apptainer
  mounts `$HOME` and defaults but NOT `/nfs`, so pg_dump fails with "No
  such file or directory" even though the job can stat the destination.
- **`APPTAINERENV_PGPASSWORD`, never `--env PGPASSWORD=`** — an `--env`
  flag lands in apptainer's own argv and argv is world-readable; /proc on
  the compute nodes has no hidepid.
- **`--no-owner --no-privileges`** — the 15 tables are owned by
  `brettsp` while the dump runs as the service account.
- **`.part` → verify → rename** — and verification checks for TABLE DATA
  sections, not just that `pg_restore --list` parses. A schema-only dump
  parses perfectly and restores an empty database.
- **Report bytes with `stat`, not `du -h`** — on this NFS export `du`
  reports block usage that reads as "512" for a 44 MB file, which looks
  exactly like an empty dump in the log.

The service account can SELECT all 15 public tables, so the dump is
complete and runs unattended off the self-refreshing secret — no CAS
dependency and no 7-day expiry to babysit.

### PG and SQLite do not share column types

SQLite stores everything as TEXT, so **nothing you can run locally
reproduces a PG type error**. Confirmed against the live DB 2026-09-04:

| Column | PG | SQLite |
|---|---|---|
| `runs.run_date` | `timestamp with time zone` | TEXT |
| `sample_health.run_date` | `text` | TEXT |
| `runs.hidden` | `integer` (not boolean) | INTEGER |

So `substr(run_date, 1, 10)` works on `sample_health` and raises
*"function substr(timestamp with time zone, integer, integer) does not
exist"* on `runs`, and a UNION of the two without casts fails outright.
Cast both arms (`run_date::text AS run_date`) and compare `hidden` to
`0`, never `false`.

This shipped as a bug in v1.0.85: `spd_usage_by_instrument_pg` caught
the error and returned `{}`, so per-instrument utilisation capacities
silently fell back to the global Evosep 100/60 pair and the feature
looked unimplemented rather than broken. **A `try/except` around a
query turns a type error into a wrong answer** — when you add one, make
the fallback path prove itself against PG, not just against SQLite.

Login-node rules still apply: these only walk the filesystem, make
symlinks and call `sbatch`. All real compute lands inside SLURM.

### PG Farm auth — two credentials, and only one of them rots

`/quobyte/proteomics-grp/brett/.pgfarm_token` holds the **long-lived
512-char service-account secret**, not a token, despite the name.
`_resolve_pgpassword()` checks `_is_jwt()` and, finding a secret,
exchanges it for a fresh JWT *on every use* — so that path is
self-healing and **its mtime tells you nothing about validity**. Do not
"fix" it because it looks old; it was last touched 23 Jul 2026 and works.
This on-demand design replaced a cron-refresh one precisely because the
cron dying on 2026-06-10 expired the JWT a week later.

What does rot is `/quobyte/proteomics-grp/brett/.pgfarm_secret.json`,
read only by `scripts/pgfarm_refresh_token.py`. The service account is
shared with FRAN, so **a rotation there silently kills every other copy
of the secret**. A stale copy mints nothing:

```
HTTP 400: {"error":"No access_token received from auth server"}
```

That is the signature of a superseded secret, not of a network or
account problem. (Happened 2026-09-04: the file held the Jun 10 secret
after a Jun 15 rotation. Fixed in place; dead copy kept at
`.pgfarm_secret.json.dead-jun10.bak`.)

DDL needs the table **owner**. Verified against the live DB 2026-09-04:
all 15 public tables in the `stan` database are owned by `brettsp`, and
`has_schema_privilege('genome-proteomics-service-account','public','CREATE')`
is **false**. So a migration needs `pgfarm auth login` — a UCD CAS
browser flow that cannot be automated. Note that `pgfarm auth whoami`
will happily print `brettsp` from cache while the cached token is
already rejected by the server, so it is not a login check.
**This is stan-specific**: in the `delimp` database the service account
owns its tables and can migrate itself. See `docs/PG_FARM.md`.

---

## CRITICAL: Always Check Primary Sources

STAN depends on external tools (DIA-NN, Sage, timsrust, ThermoRawFileParser, Percolator)
whose CLIs, flags, and output formats change between versions. **Never guess, assume, or
rely on training knowledge** for these tools — fetch primary docs first.

**Full reference (tables, gotchas, container paths, CLI flags, version pins):**
[`docs/external_tools.md`](docs/external_tools.md)

Quick `web_fetch` examples:
```
web_fetch("https://raw.githubusercontent.com/vdemichev/DiaNN/master/README.md")
web_fetch("https://raw.githubusercontent.com/lazear/sage/master/README.md")
web_fetch("https://github.com/vdemichev/DiaNN/releases/latest")
web_fetch("https://github.com/lazear/sage/releases/latest")
```

If a flag/column isn't in the primary source, add `# TODO: verify against vX.X` and tell Brett.

**The single most-misused fact:** on Hive, DIA-NN container with `.raw` support is
`/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif` (underscore). The lookalike
`/quobyte/proteomics-grp/apptainers/diann2.3.0.sif` (no underscore) silently skips `.raw`
files. Binary inside is `/diann-2.3.0/diann-linux`, not `diann`. Details in `docs/external_tools.md`.

---

## Repository Layout

```
stan/
├── CLAUDE.md                    ← you are here
├── STAN_MASTER_SPEC.md          ← authoritative design document, read before coding
├── pyproject.toml
├── README.md
├── stan/
│   ├── cli.py                   # `stan` CLI (typer)
│   ├── watcher/                 # watchdog daemon, file stability, mode detection
│   ├── search/                  # DIA-NN + Sage job builders (local subprocess + SLURM dispatcher)
│   ├── metrics/                 # metric extraction, IPS, iRT, drift, PEG, features
│   ├── gating/                  # threshold evaluation, HOLD flag, queue control
│   ├── community/               # HF Dataset submit/fetch/validate
│   └── dashboard/               # FastAPI backend + single-file React UI in public/index.html

# Runtime YAMLs live under ~/.stan/ (instruments.yml, thresholds.yml,
# community.yml, fleet.yml). They are not checked into the repo.
├── .github/workflows/
│   ├── ci.yml
│   └── consolidate_benchmark.yml
├── docs/
└── tests/
```

---

## Key Design Decisions (do not change without discussion)

### Metric hierarchy — the whole point of STAN

```
Fragment XICs/precursor  ← purest instrument signal
Precursor count @ 1% FDR ← PRIMARY metric for DIA community benchmark
PSM count @ 1% FDR       ← PRIMARY metric for DDA community benchmark
Peptide count             ← secondary for both
Protein count             ← contextual only, never used for ranking
```

Protein count is NOT the primary metric. This is an intentional and important design
decision that differentiates STAN from ProteoScape and every other QC tool. Do not
promote proteins to primary metric status anywhere in the UI, API, or docs.

### Community benchmark tracks

- Track A = DDA, Sage search, PSM count primary
- Track B = DIA, DIA-NN search, precursor count primary
- Track C = both submitted from same instrument within 24h → unlocks radar fingerprint
- Tracks are completely separate leaderboards — never mix DDA and DIA metrics

### Search standardization

All community benchmark submissions use the **community-standardized search** with
pinned FASTA + library from the HF Dataset repo. This is what makes cross-lab
comparisons valid. The frozen parameters are defined in:
- DIA: `stan/search/community_params.py` → `COMMUNITY_DIANN_PARAMS_FROZEN`
- DDA: `stan/search/community_params.py` → `COMMUNITY_SAGE_PARAMS`

Do not change these without updating the version tag and migrating old submissions.

### Instrument config hot-reload

`instruments.yml` must be hot-reloaded without restarting the watcher daemon.
The watcher polls for config changes every 30 seconds using file mtime.

### File stability detection — vendor-specific

- Bruker `.d`: directory size check every 10s, trigger after `stable_secs` (default 60s)
  consecutive checks with no size change. The `.d` is a directory, not a file.
- Thermo `.raw`: single binary file, check mtime + size, trigger after `stable_secs`
  (default 30s). File handle is closed at acquisition end.

Do not swap these — they're different because the vendors handle file writing differently.

### SPD resolution chain (v0.2.188+)

`spd` (samples per day) is the cohort key for Trends / community
benchmarks. Never hardcode SPD extraction — always go through
`InstrumentWatcher._resolve_spd(raw_path)` on ingest OR
`validate_spd_from_metadata(raw_path)` in backfills. Both apply
the same layered fallback:

1. **Bruker `.d` method XML** — `_bruker_spd_from_xml()` reads
   the HyStar_LC `<name>` element from `<N>.m/submethods.xml`
   (UTF-8) or the `HyStar_LC_Method_Name` property from
   `SampleInfo.xml` (UTF-16). Parses labels like
   `"100 samples per day"` → 100. This is authoritative when
   present because it's what the operator actually loaded in
   HyStar. Works even with cryptic PAC method names like
   `DIA_Bps_11x3-k07t13Ra85.proteoscape.m`.

2. **Bruker TDF `GlobalMetadata.MethodName`** — pattern-match
   via `_spd_from_method_string()` (e.g. `100 SPD`, `Whisper40`,
   `30spd`). Weaker than XML because the method name is
   user-defined and often inconsistent, but fills gaps when
   the XML is missing.

3. **Bruker TDF `Frames.Time`** — compute gradient length from
   first/last frame timestamps, snap to nearest known SPD via
   `gradient_min_to_spd()`. Thermo `.raw` gets this path via
   `fisher_py` InstrumentMethod or `stan.tools.trfp` metadata.

4. **`instruments.yml` cohort default** — `spd:` field on the
   instrument block. Last-resort fallback when raw-file reading
   fails (e.g. network issue, corrupt .d). Do NOT rely on this
   as the primary source — it's a blanket that stamps every run
   with the same value, which bucket-mixes cohorts when an
   operator switches gradients mid-day.

5. **Filename regex** — `(\d+)[\s_-]*spd` catches inline tokens
   like `60spd`, `60-spd`, `60 SPD`, `100SPD`. Ordered LAST so
   a mistyped filename can't override real metadata.

6. **NULL** — Trends panel renders as "SPD unknown" and the
   community benchmark treats the row as unusable for cohort
   stats.

**Backfill old NULL rows** with `stan fix-spds` — walks the
`runs` table, re-reads each raw file, and updates `spd` where
the chain now produces a definitive answer. Idempotent; safe to
re-run.

**Sample runs carry an SPD too (v1.0.85).** `sample_health.spd` is
resolved in `stan.db._resolve_sample_spd` — raw metadata first, then
`spd_from_filename()`, and *deliberately not* the instruments.yml
cohort default: a QC injection is always the same method, while a
core facility switches gradients between users through the day, so
the blanket would bucket-mix. Backfill with `stan fix-sample-spds`
(backend-aware — writes PG when `STAN_DB_BACKEND=pg`; run it where
the raw files are).

Without that column the dashboard's TIC overlay reported
`Sample (0) · Blank (0)` on a week with 185 sample acquisitions,
because the API stubbed `spd: None` and the UI filter compares
`String(r.spd) === String(spdFilter)`. Any new panel that filters by
SPD must therefore decide what an *unresolved* gradient means — the
overlay offers an explicit "SPD unknown" bucket and says how many
traces it is holding back, rather than showing a silent zero.

**Utilisation capacity is per-instrument**, from the gradients that
instrument actually runs (`spd_usage_by_instrument()` over `runs` +
`sample_health`, top two by count). The Hive counter's global
`CAPACITIES = (100, 60)` is Evosep's timsTOF ladder and is only the
fallback — scoring an Exploris against 100 SPD reports a percentage
of a method that lab has never run.

### Derived SPD is honest; conjured SPD is not

`gradient_min_to_spd()` snaps three Evosep windows (10-13 -> 100,
19-23 -> 60, 40-46 -> 30) and otherwise **derives** throughput as
`1440 / (minutes x 1.25)`. Measured against the live DB 2026-09-04,
60.8 % of `runs.spd` (2,751 of 4,524) holds a derived value rather than
an Evosep number, and that is correct, not a bug:

| runs.spd | gradient | instrument |
|---|---|---|
| 38 (728 rows) | 30 min | Exploris 480 |
| 19 (701) | 61 min | Exploris 480 |
| 32 (614) | 36 min | Lumos |
| 12 (541) | 96 min | Lumos |

These labs do not run Evosep. A 30 min gradient really is ~38
samples/day, the derivation is deterministic so equal gradients share a
cohort, and it is what the utilisation panel scores those instruments
against. **Do not "fix" this by returning None outside the snap
windows** — that blanks 61 % of the column and sends both Orbitraps back
to being measured against an Evosep ladder they never touch.

What is *not* defensible is inventing a number with no gradient behind
it. `minutes <= 0` returned a hardcoded 30 until v1.0.87 — a value
indistinguishable from a real 30 SPD method once stored. It answers
None now. `throughput_bucket()` still has the same shape in its final
`return spd_bucket(30)`, left in place with a TODO because fixing it
means adding an "unknown" cohort bucket, which changes community
benchmark cohort ids.

Two known-suspect populations, both awaiting a decision rather than a
patch: 265 `sample_health` rows at **128 SPD** (a 9 min gradient, just
below the 100 SPD window — probably real 100 SPD blanks) and 4 rows at
576/192/230 (2/6/5 min runs).

**Adding a new Evosep gradient**: extend `_EVOSEP_METHOD_PATTERNS`
in `stan/metrics/scoring.py` AND `GRADIENT_TO_SPD` snapping table,
and add a regression test against a real `.d` method XML in
`tests/fixtures/`.

**Never pull SPD from cohort default alone** in new code paths.
The real-time watcher did this pre-v0.2.188 and left 58 timsTOF
runs NULL despite their filenames containing `60spd` — the XML
lookup would have caught them all.

### IPS score (0–100)

Instrument Performance Score — a cohort-calibrated depth score:
```
DIA: IPS = 0.50 × s_precursors + 0.30 × s_peptides + 0.20 × s_proteins
DDA: IPS = 0.50 × s_psms       + 0.30 × s_peptides + 0.20 × s_proteins
```
Each `s_*` is a piecewise-linear percentile against an
`(instrument_family, SPD_bucket)` reference cohort. Stored as
`runs.ips_score` in SQLite, included in every community submission,
and shown as the IPS badge on the dashboard. Implementation:
`stan/metrics/chromatography.py`. Full rationale: `docs/ips_metric.md`.

The previous GRS (Gradient Reproducibility Score) was retired — it
required components (TIC reference, blank carryover) that STAN does
not collect, so it was replaced by IPS.

### Privacy — hard rules

- Raw files are NEVER uploaded anywhere
- Patient/sample metadata is NEVER collected
- The community benchmark collects aggregate metrics only
- Serial numbers (optional) are stored server-side but never exposed in API/downloads
- CC BY 4.0 on the community dataset

---

## Infrastructure

### Three repositories

| Repo | URL | Purpose |
|------|-----|---------|
| GitHub code | https://github.com/bsphinney/stan | Application code, CI, spec |
| HF Space | https://huggingface.co/spaces/brettsp/stan | Public community dashboard |
| HF Dataset | https://huggingface.co/datasets/brettsp/stan-benchmark | Community benchmark data |

### HPC: Hive (UC Davis)

- Host: `hive.hpc.ucdavis.edu` (user `brettsp`, SSH alias `hive`)
- Scheduler: SLURM
- DIA-NN, Sage, 4DFF, etc. run as SLURM batch jobs
- **PG Farm is the database** (`STAN_DB_BACKEND=pg`). The SQLite file on
  Quobyte is a leftover: ~100 concurrent SLURM writers on a network FS
  surfaced as `SQLITE_IOERR`, not a clean `SQLITE_BUSY`, and a
  2026-08-26 drain lost ~37 monitor jobs in 11 minutes. That is what
  moved the monitor pipeline to PG. Do not add a concurrent SQLite
  writer on Quobyte.
- Read the dashboard at `ucd.stan-proteomics.org`, not an SSH tunnel
- Full context doc at `/Users/brettphinney/Documents/claude_private/HIVE_CLAUDE_GUIDE.md`
  (read at session start for partition/QOS/path details)

**Hive rules of engagement — violate at your peril:**

1. **Never run compute on the login node (`login1`)**. CPU/memory-heavy
   work gets flagged. Always use `sbatch` for real work or `srun --pty`
   for interactive. Brett caught me running `uff-cmdline2` directly on
   login1 on 2026-04-24 — stop, `pkill`, resubmit as a SLURM job.

2. **Never use `~/` or `/home/brettsp/` for large artifacts** — the home
   quota is tight and others can't see it. All shared binaries, FASTA
   files, analysis outputs, and generated `.features` live under
   `/quobyte/proteomics-grp/...`. Brett's personal scratch dir is
   `/quobyte/proteomics-grp/brett/` — writable + visible to the lab.

3. **SLURM commands need module environment loaded**. Non-interactive
   `ssh hive "sbatch ..."` won't find `sbatch` on PATH. Either:
   - `ssh hive "bash -l -c 'sbatch ...'"` (login shell), or
   - `ssh hive "source /etc/profile.d/modules.sh && source
     /etc/profile.d/hpccf.sh && sbatch ..."`

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

6. **SSH ControlMaster** speeds up repeated invocations:
   ```bash
   ssh -o ControlMaster=auto -o ControlPath=/tmp/.stan_brettsp_hive \
       -o ControlPersist=300 brettsp@hive.hpc.ucdavis.edu "<cmd>"
   ```
   macOS socket path must be ≤104 bytes — keep `ControlPath` under
   `/tmp/` not `/Users/.../...`.

**Bruker 4DFF on Hive** (v0.2.200+):
- Binary: `/quobyte/proteomics-grp/brett/bruker_ff/linux/uff-cmdline2`
- `LD_LIBRARY_PATH` must include that dir (for `libtbb.so.2`)
- STAN's `_install_dir()` in `stan/metrics/features.py` respects the
  `STAN_BRUKER_FF_DIR` env var — set it to the shared path on Hive
  so `stan install-4dff` doesn't fill up the home directory:
  `export STAN_BRUKER_FF_DIR=/quobyte/proteomics-grp/brett/bruker_ff`

---

## Autonomous troubleshooting (CRITICAL)

**Answer from PG first. Do not ask Brett to run anything on an
instrument PC — nothing runs there** (see Current deployment above).

### The Hive mirror is dead — do not reason from it

`/quobyte/proteomics-grp/STAN/<HOSTNAME>/` was a per-instrument mirror
of each PC's local state. The PCs stopped writing it and then stopped
running STAN altogether. Measured 2026-09-04:

| | newest `status.json` |
|---|---|
| `TIMS-10878/` | 11 Aug 2026 |
| `DESKTOP-FOT3DAA/` | 11 May 2026 |
| `lumosRox/` | 14 May 2026 |

**A stale mirror is worse than no mirror**, because it answers
confidently with data from before the problem you are looking at. These
files are historical only. If you open one anyway, `ls -lat` it first
and say the date out loud when you quote it.

The same applies to `~/.stan/stan.db` on Brett's Mac: it is a local
read-cache that a `stan dashboard` fills from PG, not a source of truth,
and it holds whatever it last synced. Check its mtime before quoting it.

### Diagnosis protocol

1. **Ask PG.** It is the store of record for runs, sample_health,
   metrics, and dispatch state.
   ```bash
   ssh hive "bash -lc 'export PGPASSWORD=\$(cat /quobyte/proteomics-grp/brett/.pgfarm_token); \
     /quobyte/proteomics-grp/brett/stan_venv/bin/python -'" < query.py
   ```
   Pipe the script over stdin rather than nesting quotes in `ssh "..."` —
   nested `"` inside a remote `bash -lc` is the single most reliable way
   to waste a round trip here. If the connection reports an expired JWT,
   re-mint the token before believing any other conclusion.
2. **Check which version serves the surface in question** (see the table
   above). A "bug" is very often a fix that is live on main and not yet
   on Azure.
3. **Check the Hive cron logs** for the tick that should have done the
   work — `/quobyte/proteomics-grp/STAN/logs/`, and `squeue -u brettsp`
   for jobs actually queued.
4. **For raw-file questions**, the files are on the Flinders export
   (`/nfs/lssc0/flinders/proteomics/Data/raw_data/<instrument>`),
   readable from Hive.
5. **Only escalate to Brett** for things genuinely outside the cluster —
   what is physically on the instrument, or which hosted surface he has
   open.

### New jobs must publish their own logs

Any new CLI command, backfill, or background job that could fail
silently MUST write `~/STAN/logs/<command>_<timestamp>.{log,jsonl}` with
per-step status and a summary, and log errors at `logger.warning` or
above. On Hive that lands under the shared `/quobyte/proteomics-grp/STAN/`
tree where it is readable over ssh.

`sync_to_hive_mirror()` is retained for other labs' installs and is a
harmless no-op here — do not rely on it to make anything visible, and
never let a dead share break the job reporting through it.

### Config file locations

Hive + hosted read config from the package and PG. `~/.stan/*.yml` is
the single-lab install path (other labs), not this facility.

- `~/.stan/instruments.yml` — watch directories (single-lab installs)
- `~/.stan/thresholds.yml` — QC thresholds; falls back to `config/thresholds.yml`.
  **No deployment currently ships one**, so `evaluate_gates` returns PASS
  before comparing a single metric — which is why gate results are inert
  and the dashboard colours off `ips_score` instead.
- `config/` ships inside the Azure zip; `resolve_config_path()` looks in
  `~/STAN/` then `<package>/config/`.

---

## Development Workflow

### Test fixtures

`tests/fixtures/` is intentionally empty. Mock `.d` / `.raw` paths and
search-output samples are constructed by `tests/conftest.py` per-test —
add new fixture builders there rather than committing binary artifacts.

### Adding a new instrument model

1. Add entry to `~/.stan/thresholds.yml` (or the in-tree fallback in
   `stan/config/thresholds.yml` if you ship a default) with
   model-specific thresholds
2. Add model to `~/.stan/instruments.yml` example
3. Add model to the `instrument_model` enum in the community submission schema
4. Update the reference range table in `STAN_MASTER_SPEC.md` appendix

### Adding a new QC metric

1. Implement extraction in `stan/metrics/extractor.py`
2. Add column to SQLite schema in `stan/dashboard/server.py` (with migration)
3. Add field to the HF Dataset parquet schema in `stan/community/submit.py`
4. Add to the dashboard UI in `stan/dashboard/public/index.html`
5. Update `STAN_MASTER_SPEC.md` metric tables

---

## Common Mistakes to Avoid

DIA-NN, Sage, ThermoRawFileParser, Polars, HF Hub gotchas + Thermo `.raw` → mzML
conversion details live in [`docs/external_tools.md`](docs/external_tools.md).

**STAN-specific reminders that aren't in those docs:**

- **DIA-NN 2.0 column changes** — extractor must handle both 1.x and 2.0:
  `File.Name` (full path) replaces `Run` (basename); `Fragment.Info`, `Fragment.Quant.Corrected`,
  `Missed.Cleavages` may be absent. Always check `if col in df.columns` before access.
- **Bruker `.d` is a directory**, not a file — stability detection must check total
  directory size, not file mtime.
- **`analysis.tdf.Frames.MsmsType`**: 0=MS1, 8=ddaPASEF, 9=diaPASEF (verify against
  current Bruker docs — could change).
- **Thermo conversion routing**: DIA-NN 2.1+ reads `.raw` natively (no conversion);
  Sage always needs ThermoRawFileParser → mzML for `.raw`. `instruments.yml` has
  `raw_handling: "native" | "convert_mzml"` per Thermo instrument as a fallback toggle.

---

## Dashboard: Ion Cloud View (v0.2.192+, DB-backed since v1.0.16)

The drift "Ion cloud" tab has two render modes that switch automatically
depending on whether charge-labeled 4DFF data is available for the run:

- **Plotly per-charge scatter** (`DriftCloudPlotly` in `public/index.html`)
  is the preferred view. It fetches `/api/runs/{run_id}/features-by-charge`,
  which resolves in two steps: **the `feature_clouds` table first**, then
  the `LcTimsMsFeature` table in the on-disk `.features` sidecar via a raw
  `sqlite3` connection — **never import from `stan.metrics.features`
  here**. One trace per charge state, Ziggy palette (`+2` blue, `+1` teal, `+3` green,
  `+4` orange, `+5` purple, `+6` red, unassigned yellow). DIA windows are
  overlaid as rectangles grouped by `window_group` with an 8-color palette
  cycled modulo the group count. Click the legend entries to toggle charges.
- **Legacy SVG cloud** (`DriftCloudSvg`) is the fallback when neither a
  stored cloud nor a reachable `.features` exists. The friendly stub names
  both halves that are missing.

### Never make this view depend on the raw file being local

The sidecar-only lookup was the bug that made this tab useless in the
fleet, and it is easy to reintroduce. `stan dashboard` is a SQLite reader
that syncs from PG; it runs on Brett's Mac or an instrument PC, while the
`.d` lives on Hive / the Flinders NFS export. So `raw_path` almost never
resolves on the host serving the API, and every run showed *"no .features
file found next to raw data"* even though 4DFF had written a perfectly
good sidecar hours earlier. The `.features` files existed for 38 of the 40
newest runs the whole time.

The fix is a store-and-serve path, populated where the files are:

```
Hive:  .d/<name>.d.features        (4DFF, already written by the pipeline)
         │  stan backfill-feature-cloud       ← run on Hive
         ▼
       feature_clouds (PG)          run_id, source, mz, mobility, rt,
         │                          charge, intensity, n_points, n_total
         │  stan.sync.pg_to_sqlite  ← bounded, newest-first, missing keys only
         ▼
Mac:   feature_clouds (SQLite) → /api/runs/{id}/features-by-charge
```

- `stan/metrics/feature_cloud.py` does the extraction: a deterministic
  `rowid % step` stride down to `DEFAULT_MAX_POINTS` (5,000 — the same
  budget `drift_peak_clouds` uses), which
  preserves relative density so the cloud still *looks* like the run.
- Storage is a **separate table from `drift_peak_clouds`** on purpose.
  That one holds raw MS1 peaks from `detect_window_drift`; this one holds
  4DFF features with an exact charge per point. Different writers — a
  shared `(run_id, source)` key means whichever backfill finished last
  silently clobbers the other.
- `feature_clouds` rows are ~330 KB each, two orders of magnitude fatter
  than the other detail tables, so `_pull_feature_clouds` pulls only keys
  the local DB is missing, newest first, capped per refresh
  (`STAN_PG_CLOUD_MAX_PULL`, default 50). Do not fold it into the blanket
  `_DETAIL_TABLES` copy — that would drag ~170 MB every refresh tick.
- PG DDL needs the table **owner** (`brettsp`, CAS login) —
  `genome-proteomics-service-account` has DML but no CREATE on schema
  public. When the table is missing, the Hive driver falls back to a JSON
  cache at `/quobyte/proteomics-grp/STAN/feature_clouds/<run_id>.json`
  (visible on the Mac as `/Volumes/...`), loadable with
  `stan backfill-feature-cloud --from-cache <dir>`.

**Hive-side scripts** (canonical copies in `scripts/`, running copies in
`/quobyte/proteomics-grp/STAN/`). They wrap the CLI with the sharding,
SLURM plumbing and JSON-cache fallback the bare command does not do.
(The Hive checkout was once described here as an unpulled patched fork;
that is no longer true — `stan_venv` imports straight from it and it
tracks main, so `git pull` there is the deploy.)

| Script | Does |
|---|---|
| `feature_cloud_backfill.py` + `.sbatch` | Extract + publish clouds from sidecars that already exist. Cheap (~80 s for a full 1,600-run scan), idempotent, 4 shards on `low`. |
| `feature_cloud_4dff.py` + `.sbatch` | Generate the *missing* sidecars with 4DFF first, then publish. Expensive (minutes + ~100 MB per run). |
| `cron_ioncloud.sh` | Hourly tick that submits the cheap one. **Staged, not installed** — add the crontab line at the top of the file to enable. |

Anything new placed on Hive must go under `/quobyte/proteomics-grp/STAN/`,
**not** `/quobyte/proteomics-grp/brett/` — Python puts the script's own
directory first on `sys.path` and the `stan/` checkout there shadows the
installed package, so `import stan` picks up a bare source tree.

Plotly is loaded from `cdn.plot.ly/plotly-2.35.2.min.js` — pure client-side,
same CDN pattern as React + Babel. No build step needed. If the CDN is
unreachable the Plotly component renders a "failed to load" note and the
SVG fallback still works from its own code path.

---

## Implementation Rules

**Never guess what data is available — always read the code first.**

Before implementing any new feature or metric, read the actual extractor functions
(`stan/metrics/extractor.py`) and search engine output schemas to confirm what columns
and values are available. Do not assume a field exists because it "should" be there.
The original GRS score (now retired in favor of IPS) was designed with components
(TIC reference, blank carryover) that STAN could never actually measure. Don't repeat
that mistake — confirm every input field exists in the extractor before specifying
a composite metric.

**Verify end-to-end before shipping:** trace the data path from raw file → search →
extraction → DB → dashboard to confirm every value is actually populated.

---

## Deployment & Versioning

- **Always bump both** `pyproject.toml` AND `stan/__init__.py` on every push. The
  version is how you tell which of the three surfaces a fix has reached.
- **A push to GitHub deploys nothing by itself.** Each surface updates separately:
  | Surface | How it updates |
  |---|---|
  | Hive | `ssh hive "cd /quobyte/proteomics-grp/brett/stan && git pull"` — `stan_venv` is editable, so that is the whole deploy |
  | Azure (`ucd.stan-proteomics.org`) | zip deploy, `docs/AZURE_DEPLOY.md`. Run `node scripts/check_jsx.js stan/dashboard/public/index.html` first — a JSX syntax error blanks the page rather than degrading |
  | HF Space | see `reference_hf_space_deploy` / the Space repo |
- **Say which surface you mean** when reporting a fix as shipped. "Pushed" is not "live".
- `update-stan.bat` targets instrument PCs and is **not used at UC Davis** — those
  boxes acquire only. It remains for single-lab installs elsewhere.
- Baseline Builder has its own version banner (e.g. "v3") — bump it when baseline behavior changes.

---

## PowerShell 5.1 Compatibility (instrument PCs)

> **Not used at UC Davis** — this facility's instrument PCs acquire only.
> Kept because `scripts/*.ps1` still ships for single-lab installs elsewhere.

Instrument PCs run Windows with PowerShell 5.1. When editing `.ps1` files:
- **Always rewrite the entire file** — never patch individual lines (subtle parsing traps)
- **Never use `+` string concatenation** — use `"$var;$var"` interpolation instead
- **No inline ternary `if`** — use separate `if`/`else` blocks
- **No `Where-Object { }` pipelines** — use explicit `foreach` loops
- **Use `Join-Path`** instead of string concatenation for paths
- **`return ,$collection` when returning a set/list from a function** —
  PowerShell unrolls collections on return, so a plain `return $set`
  hands back a bare `String` when it holds one item and `$null` when it
  is empty. The caller's `.Add()` then throws on a fixed-size result.
  Only needed when the caller does *not* wrap the call in `@()`; adding
  the comma to a plain array whose caller *does* wrap turns an empty
  result into a 1-element array, which is its own bug.
- **Never pass a PS array where .NET wants `string[]`** — PowerShell
  prefers the scalar overload and stringifies the array to
  `"System.Object[]"`. `[datetime]::TryParseExact($s, @("MMMyy","MMMMyy"),
  ...)` silently matches nothing. Loop the formats one at a time.

Both of the above shipped as bugs in `scripts/flinders_copy.ps1` and
were caught only because `tests/test_flinders_copy.ps1` exercises the
real functions. **There is no PowerShell on the dev Mac**, so verify `.ps1`
work with a portable `pwsh` (`PowerShell/PowerShell` release tarball,
extract and run — no install, no sudo): parse with
`[System.Management.Automation.Language.Parser]::ParseFile`, then load the
functions out of the shipped file through the AST and test them. A parse
check plus tests is stronger evidence than a blind full-file rewrite.

---

## Instrument PC Constraints

> **Not used at UC Davis** — all search runs on Hive under SLURM (see
> the cluster-only search policy). These constraints govern the
> single-lab install path that other labs run.

- **Half CPU cores** for DIA-NN/Sage — `max(2, cpu_count // 2)`. These are instrument
  workstations that may be acquiring data simultaneously.
- **No library-free DIA-NN** — too slow for QC, produces non-comparable community results.
  Always require a spectral library; raise ValueError if none provided.
- **Dual venv installs** — old `.stan\venv` and new `STAN\venv` may both exist on PATH.
  The updater must detect and migrate, removing old PATH entries.
- **thresholds.yml may not exist** — `load_thresholds()` must not crash; default to PASS.

---

## Community Submission Architecture

Community benchmark submissions go through the HF Space relay — **no HF token required**.
- Client (`stan/community/submit.py`) POSTs JSON to `https://brettsp-stan.hf.space/api/submit`
- Relay has `HF_TOKEN` secret, handles auth + parquet upload to `brettsp/stan-benchmark`
- Never re-introduce HF token requirements in client-side code
- `tests/test_pipeline.py` has 7 tests that catch version desync, schema mismatch, and token regressions

---

## Documentation Maintenance

**When you implement a new feature or complete a TODO item, update ALL of the following:**

1. Check `README.md` — move the item from the TODO list to the Implementation Status table.
   Remove any **(planned)** markers from the feature description.
2. Check `docs/user_guide.md` — remove **(planned)** markers and update instructions
   to reflect actual working behavior.
3. If the feature changes config format, update the YAML examples in both files.
4. If the feature adds a new CLI command, add it to the Quick Start and user guide.

The README has an [Implementation Status](#implementation-status) table and a [TODO](#todo)
checklist. These are the source of truth for what works vs what's planned. Keep them current.

5. Check the **HF Space relay API** (`app.py` on `brettsp/stan`) — if schemas, field names,
   or metrics changed, the relay must be updated and redeployed. The submission schema in
   the relay MUST match the client-side submission code in `stan/community/submit.py`.
6. Check the **HF Space dashboard HTML** — if metrics are renamed, added, or removed,
   update the figures, table columns, reference range cards, and info cards on the dashboard.

---

## Code Style

- Python 3.10+, type hints everywhere
- Ruff for linting (`ruff check stan/`)
- Docstrings on all public functions (Google style)
- No print() — use `rich.console.Console` or Python `logging`
- All file paths as `pathlib.Path`, never raw strings
- All subprocess calls via `subprocess.run(..., check=True)` with explicit timeout
- All HF API calls wrapped in try/except with meaningful error messages
- All SQLite operations use context managers (`with sqlite3.connect(...) as con:`)

---

## Testing

```bash
pytest tests/ -v                        # run all tests
pytest tests/test_metrics.py -v        # metrics only
pytest tests/ -k "not integration"     # skip tests requiring Hive connection
```

Tests that require Hive SLURM or real instrument files are marked `@pytest.mark.integration`
and skipped in CI. They can be run manually on Hive.

When adding a test that parses DIA-NN or Sage output, add a small real
output file under `tests/fixtures/` (currently empty — commit artifacts
as you need them) rather than generating synthetic data. Synthetic data
won't catch format changes between tool versions.

---

## CI/CD

### GitHub Actions

- `ci.yml` — runs on every push/PR: install, ruff lint, pytest (no integration tests)
- `consolidate_benchmark.yml` — runs nightly at 4am UTC: downloads HF Dataset
  submissions, validates, recomputes percentiles, writes `benchmark_latest.parquet`

### Required secrets (GitHub repo settings → Secrets)

- `HF_TOKEN` — Hugging Face token with write access to `brettsp/stan-benchmark`

---

## Links

External-tool URLs and public Astral HeLa benchmark datasets:
[`docs/external_tools.md`](docs/external_tools.md). STAN's own repos:

- GitHub: https://github.com/bsphinney/stan
- HF Space: https://huggingface.co/spaces/brettsp/stan
- HF Dataset: https://huggingface.co/datasets/brettsp/stan-benchmark

---

## Questions? Ambiguities?

If anything in the spec is unclear or contradictory, check `STAN_MASTER_SPEC.md` first.
If the spec doesn't resolve it, ask Brett before guessing. Wrong assumptions about
search engine flags or output formats will cause silent failures that are hard to debug.

The spec is the source of truth. This CLAUDE.md is the development context.
Primary source docs are the oracle for external tool behavior.

**When in doubt: fetch, don't guess.**
