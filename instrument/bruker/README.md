# Bruker → STAN maintenance pipeline

Turns a Bruker timsTOF **Compass Server** PostgreSQL backup into a compact set of
**instrument-maintenance signals** and a **Maintenance-tab feature** for the STAN
QC dashboard, for the UC Davis Proteomics Core.

A Bruker timsTOF HT runs its own PostgreSQL ("compass") holding the full
acquisition history — every run, batch, method, status and error. That history is
a goldmine for a facility manager: *is the instrument busy or idle, is throughput
trending, what is failing, which methods dominate.* This tool extracts those
signals **read-only** from the nightly backup and surfaces them beautifully.

Everything here runs against the **real** Aug-31-2026 backup
(`.../BrukerDBBackup/daily/2026-08-31_180000/compass.backup`, 2.46 GB). Every
number below and in the prototype is real — no placeholders.

---

## What it found (real numbers, station **HPZ6**)

| Signal | Value |
|---|---|
| Acquisitions on record | **24,115** over **1,188 days** (2023-05-31 → 2026-08-31) |
| Success rate | **97.4 %** (614 failed / aborted) |
| Median completed runtime | **14.2 min** |
| Days the instrument was in use | **197** (17 % of calendar days) |
| Longest idle gap | **18.1 days** (2023-06-23 → 2023-07-11) |
| **#1 failure cause** | **Evosep "no Evotip present" — 309 runs (50 % of all failures)** |
| Other top causes | LC pressure / clog (106), MS software error (46), connection lost (31) |
| Busiest gradients | 60 spd (7,558), 100 spd (5,002), 30 spd (368) |
| Duty cycle | ramped from ~10 % to **~37 %/month** through 2026 |

**The headline diagnostic:** on the latest plate (S5, 2026-08-28) two wells
produced no data file — **S5-F6 / COH-46** and **S5-H6 / COH-48**. The database
says exactly why: both **FAILED** with *"Evosep One: No Evotip was present during
the analysis"*. That is a consumable/loading miss, not an MS fault — reload the
two tips and re-queue. The dashboard surfaces this automatically.

---

## The maintenance signals surfaced — and why each matters

| Signal | Source in Compass DB | Why a facility manager wants it |
|---|---|---|
| **Throughput over time** (monthly completed vs failed) | `cst.task` (status, `start_date`) | Spot a declining instrument or a productive month at a glance. |
| **Success / failure rate** | `cst.task.status` | The single health number; a rising failure rate is an early warning. |
| **Failure taxonomy** (root-cause buckets) | `cst.task.status_text` parsed | Tells you *what to fix*: tips vs LC pressure vs MS software vs network. |
| **Latest-plate well map** | `cdr.data_set.file_system_path` + task status | Which wells have no usable data and *why* — actionable per-sample. |
| **Gradient / method usage** | run filenames (`_60spd_`, `_100spd_`) | Capacity planning and which method's failures dominate. |
| **Duty cycle & active days** | `cst.task` run durations | Is the instrument saturated or idle? Justifies scheduling / a 2nd instrument. |
| **Longest idle gap** | gaps between successful runs | Surfaces unplanned downtime and long stalls. |
| **Recent-failures log** | `cst.task` + parsed filename | The operator's triage list with the raw instrument message. |

### Key tables used (read-only)

- `cst.task` — one row per acquisition: `status` (DONE/FAILED/ABORTED/RUNNING),
  `status_text` (the human error string), `start_date`, `end_date`, `analysis_fk`.
- `cdr.data_set` — one row per data file: `file_system_path`
  (e.g. `D:\Data\Aug26\20260828_100spd_COH-46_S5-F6_1_24165.d`), linked to a task
  via `task.analysis_fk = data_set.origin_id`.
- `cst.station` — the instrument identity (name **HPZ6**, live status).

The filename is parsed for run date, gradient (`100spd`), sample (`COH-46`) and
well (`S5-F6`); dates appear in both `YYYYMMDD` and `MMDDYYYY` forms and both are
handled.

---

## How it works

`extract_bruker.sh` spins up a **throwaway** PostgreSQL inside the pinned
`postgres16.sif` apptainer, restores **only** the ~10 tables it needs (via a
filtered `pg_restore -L` table-of-contents — the giant spectrum tables are
skipped), runs one analysis query that emits a single JSON document, then deletes
the throwaway cluster. Restoring the whole 2.4 GB would take minutes; the filtered
restore + analysis is **~40 s**. Nothing is ever written to the Bruker database or
the backups.

## Run it

On Hive (the script re-execs itself inside the apptainer if postgres tools aren't
on `PATH`, so this one line is enough):

```bash
# newest daily backup -> stdout
/quobyte/proteomics-grp/apptainers/postgres16.sif  # (image it uses)
bash extract_bruker.sh --out bruker_maintenance.json

# or a specific backup
bash extract_bruker.sh --backup /quobyte/proteomics-grp/brett/BrukerDBBackup/weekly/<date>/compass.backup --out out.json
```

Flags: `--backup <path>` (default: newest `daily/*/compass.backup`), `--out <file>`
(default: stdout). The historical `weekly/ monthly/ yearly/` backups let you run
trend analysis further back.

## Put it in the dashboard

See **`INTEGRATION.md`** — paste one function into `server.py`, one component +
one line into `index.html`, and schedule the extractor. All code is matched to
STAN's existing patterns.

## Schedule it (suggested, not installed)

STAN already has `flock`-guarded `cron_*.sh` scripts under
`/quobyte/proteomics-grp/STAN/` and ships JSON caches there (`acq_date_cache.json`).
`cron_bruker_maintenance.sh` follows that exact pattern — it extracts to a temp
file, sanity-checks it, and atomically publishes `bruker_maintenance.json`:

```
*/30 * * * * flock -n /tmp/stan_bruker_maint.lock /quobyte/proteomics-grp/STAN/cron_bruker_maintenance.sh
```

---

## Files

| File | What it is |
|---|---|
| `extract_bruker.sh` | The extractor (runs on Hive; read-only; `--backup`/`--out`). |
| `extract.sql` | The analysis query — restore-agnostic; emits the JSON document. |
| `maintenance.json` | Real extracted output from the Aug-31 backup (33 KB). |
| `maintenance_preview.html` | **Standalone prototype** — open it; renders the real data as the STAN feature will look. |
| `snippet_server.py` | Drop-in FastAPI endpoint for `server.py`. |
| `snippet_index_component.jsx` | Drop-in React panel for `index.html`. |
| `snippet_maintenance_wiring.jsx` | The one-line tab wiring. |
| `cron_bruker_maintenance.sh` | Suggested scheduled refresh script. |
| `INTEGRATION.md` | Exact paste-here integration steps. |
| `test_harness.html` | React+Babel harness used to verify the component (real data). |
| `schema_full.sql` | Full Compass DDL dump, for reference when extending the query. |

## Principles honoured

- **Read-only** on all Bruker data — throwaway restores only; backups untouched.
- **Real data** — every figure comes from the Aug-31 backup; both the prototype
  and the React component were rendered/screenshotted and are console-clean.
- **Matches STAN** — `useFetch`, `className="card"`, theme CSS vars, no new libs.
- **Accessible charts** — dataviz-skill palette validated with the CVD/contrast
  script on the navy surface; status carries glyph + label, never colour alone.
