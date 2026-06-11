# PG Farm — STAN's central Postgres at UC Davis Library

> **TL;DR for any Claude session.** STAN now writes its central `runs`
> table to UC Davis Library's PG Farm Postgres, not SQLite, when
> `STAN_DB_BACKEND=pg` is set in the environment. The Quobyte-resident
> SQLite is fragile (corrupted twice in May 2026) — anything new on
> Hive should target PG. Instrument-PC watchers still use local SQLite
> and a 30-min cron sync (running on Brett's Mac via launchd) replicates
> them up to PG.

---

## Connection details

```
host      pgfarm.library.ucdavis.edu
port      5432
database  uc-davis-genome-center-proteomics-core/stan
user      genome-proteomics-service-account    # service account (since v1.0.2)
sslmode   require                              # 'verify-full' breaks on the
                                               # Mac cert path — don't bother
```

### Service-account auth (v1.0.2+)

As of 2026-06-10 STAN authenticates as the **service account**
`genome-proteomics-service-account`, not the personal `brettsp` CAS token.
The model is two-tier:

1. **Long-lived secret** — `service-account.json` (`{username, secret}`),
   downloaded from the PG Farm UI "rotate" button. Keep it safe; even the
   PG Farm admins can't read it. Stored chmod 600 at:
   - Hive: `/quobyte/proteomics-grp/brett/.pgfarm_secret.json`
   - Mac:  `/Volumes/proteomics-grp/brett/.pgfarm_secret.json`
2. **7-day token** minted from the secret by POSTing `{username, secret}` to
   `https://pgfarm.library.ucdavis.edu/auth/service-account/login`
   (returns `access_token`). This is the Postgres password.

`scripts/pgfarm_refresh_token.py` does the mint and writes the token file —
no more `pgfarm auth login`. The Hive cron (`scripts/cron_flinders_dispatch.sh`)
runs it with `--max-age-days 5` each tick, so the token self-refreshes.

Token file (shared volume so Hive jobs read it too):
- Hive: `/quobyte/proteomics-grp/brett/.pgfarm_token`
- Mac:  `/Volumes/proteomics-grp/brett/.pgfarm_token`

**Grants:** the service account needs `SELECT,INSERT,UPDATE,DELETE` on the
`runs` table (run once as the owner `brettsp`; see git history of this file /
the v1.0.2 session). New tables are auto-granted via `ALTER DEFAULT
PRIVILEGES`. If you rotate the secret, just re-download `service-account.json`
to both paths above — grants and code are unaffected.

---

## Schema

64 columns total — 62 mirror the SQLite `runs` table column-for-column, plus
two PG-only additions:

| Column         | Type          | Purpose                                     |
|----------------|---------------|---------------------------------------------|
| `host_origin`  | TEXT NOT NULL | Which host's data this row came from        |
| `migrated_at`  | TIMESTAMPTZ   | Default `NOW()`, set on insert              |

**Primary key:** composite `(host_origin, id)`. The instrument PCs and Hive
each use UUID4 for `id`, and `host_origin` keeps them in separate keyspaces
so re-ingesting won't collide.

### `host_origin` values

| `host_origin` | When written              | Comes from                                  |
|---------------|---------------------------|---------------------------------------------|
| `lumos`       | timsTOF? no — Lumos PC    | Cron sync from `lumosRox/stan.db`           |
| `exploris`    | Exploris 480 PC           | Cron sync from `DESKTOP-FOT3DAA/stan.db`    |
| `timstof`     | timsTOF HT PC + Hive bulk | Cron sync + Hive PG-direct inserts          |

The map from `--family` (canonical instrument family) → `host_origin` lives
in `stan/db_pg.py::FAMILY_TO_HOST_ORIGIN`. Add new families there AND in
the dashboard's host filter.

Indexes (set up by the migration script):
- `idx_runs_instrument` on `instrument`
- `idx_runs_run_date`   on `run_date`
- `idx_runs_host`       on `host_origin`

---

## Code entry points

All PG-related code lives in `stan/db_pg.py`. Read that file when in doubt
— it's the only place inside `stan/` that imports psycopg2.

| Helper                            | Purpose                                              |
|-----------------------------------|------------------------------------------------------|
| `insert_run_pg(...)`              | Upsert one row. Same kwargs as `stan.db.insert_run`. |
| `row_exists_pg(...)`              | Existence check by `(host_origin, instrument, raw_path)`. |
| `host_origin_from_family(family)` | `'Lumos' → 'lumos'`, etc.                            |
| `use_pg()`                        | Returns True iff `STAN_DB_BACKEND=pg` is set.        |
| `_connect()`                      | Module-level cached connection — reuses across calls.|

**Persistent connection.** `_connect()` caches the psycopg2 connection at
module level. Each call to `_connect()` pings with `SELECT 1` and returns the
cached connection on success, opening a fresh one only if the server has
dropped it. This matters: pre-cache, each insert was ~3.5 s of pure SSL
handshake — bulk recovery would have taken 2.5 h. Don't accidentally break
this when refactoring (e.g. by switching to `with psycopg2.connect()` blocks).

---

## Backend dispatch

`stan.pipeline.hive_steps.step_extract` checks `use_pg()` and branches:

- **PG mode** (`STAN_DB_BACKEND=pg`): builds the row dict, calls
  `insert_run_pg()`, and skips the SQLite-only child writes
  (`_apply_pegdrift_jsons`, `_persist_tic`, `record_dispatch_attempt`).
  The child writes can be backfilled later — they're not blocking the
  dashboard or community submission.
- **SQLite mode** (default, instrument PCs + local dev): existing path,
  writes to `--db`.

The row-dict construction is factored into `stan.db._build_runs_row` so the
two backends share the column mapping. Don't fork that — both paths need to
stay in sync.

---

## Common queries

### Row count + breakdown

```sql
SELECT COUNT(*) FROM runs;                       -- total
SELECT host_origin, COUNT(*) FROM runs           -- by host
  GROUP BY host_origin ORDER BY 1;
SELECT COUNT(*) FROM runs                        -- recent inserts
  WHERE migrated_at > NOW() - INTERVAL '10 minutes';
```

### Find a specific run

```sql
SELECT id, run_date, mode, n_precursors, n_peptides, gate_result
FROM runs
WHERE run_name LIKE '%hela%50ng%' AND host_origin = 'timstof'
ORDER BY run_date DESC LIMIT 10;
```

### Schema introspection (column list + PK)

```sql
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name = 'runs' ORDER BY ordinal_position;

SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
WHERE conrelid = 'runs'::regclass;
```

### One-shot Python query (Mac-side, uses the token file)

```python
import psycopg2
pwd = open('/Volumes/proteomics-grp/brett/.pgfarm_token').read().strip()
with psycopg2.connect(
    host='pgfarm.library.ucdavis.edu', port=5432,
    database='uc-davis-genome-center-proteomics-core/stan',
    sslmode='require', user='brettsp', password=pwd,
) as c:
    cur = c.cursor()
    cur.execute('SELECT host_origin, COUNT(*) FROM runs GROUP BY 1')
    for r in cur.fetchall():
        print(r)
```

---

## Path translation (Hive ↔ Mac)

The sbatch sidecar scripts under
`/quobyte/proteomics-grp/STAN/logs/sbatch/scripts/` store paths as the Hive
sees them. The same files on the Mac live under `/Volumes/`. Add new entries
to `PATH_TRANSLATIONS` in `stan/cli.py::ingest_orphans_cmd` when introducing
new mount points.

| Hive                                | Mac                              |
|-------------------------------------|----------------------------------|
| `/quobyte/proteomics-grp/...`       | `/Volumes/proteomics-grp/...`    |
| `/nfs/lssc0/flinders/proteomics/...`| `/Volumes/proteomics/...`        |

The `_translate_path` helper is conservative: it only swaps prefixes when
the original path doesn't exist AND the translated path does. So no
behavior change on Hive, where the original paths always resolve.

---

## Cron sync (SQLite mirrors → PG)

Runs every 30 min from Brett's Mac via launchd:
`~/Library/LaunchAgents/com.brettphinney.stan-pgfarm-sync.plist`.

Wrapper: `scripts/cron_sync_to_pgfarm.sh` (path-autodetect — same script
works on Hive in a pinch). Migration logic:
`scripts/migrate_sqlite_to_pgfarm.py`.

**Important history:** the cron lived on Hive's `login2` from 2026-05-15 to
2026-05-18 and silently failed every fire with `ModuleNotFoundError: No
module named 'psycopg2'` because cron's system Python lacked psycopg2.
Lessons learned:

1. Never put cron on Hive login nodes (violates CLAUDE.md and gets crowded).
2. Always check the cron log on day 1 to confirm it didn't fail silently.
3. The script uses the venv's Python explicitly when on Hive
   (`/quobyte/proteomics-grp/brett/stan_venv/bin/python`), not the system
   Python that cron's PATH finds.

Per-day log:
- Mac: `/Volumes/proteomics-grp/STAN/logs/pg_sync_YYYYMMDD.log`
- Hive: same path under `/quobyte/...` (autodetect)

launchd stdout/err:
`/Users/brettphinney/Library/Logs/stan-pgfarm-sync.{out,err}`

---

## Orphan recovery (`stan ingest-orphans`)

Recover `report.parquet` files from `/quobyte/proteomics-grp/STAN/processing/`
that have no row in `runs` — e.g. after a SQLite corruption episode like
the May 16 incident. Reads each sbatch sidecar to recover cohort args
(instrument, family, vendor, columns, amount_ng, spd) and replays the
DB-write half of the pipeline via `step_extract`.

```bash
# Mac
PGPASSWORD=$(cat /Volumes/proteomics-grp/brett/.pgfarm_token) \
STAN_DB_BACKEND=pg \
stan ingest-orphans \
    --processing-dir /Volumes/proteomics-grp/STAN/processing \
    --sbatch-log-dir /Volumes/proteomics-grp/STAN/logs/sbatch \
    --backend pg

# Hive (SLURM — never on login node)
sbatch /quobyte/proteomics-grp/STAN/scripts/repair_and_reingest.sh
```

`--backend pg` is the default. `--backend sqlite` is the legacy path.

Idempotent: re-runs short-circuit rows already present via a bulk pre-load
of `(host_origin, instrument, raw_path)` keys at startup. Run as many times
as needed.

---

## Gotchas

**psycopg2 not in the system Python.** Always invoke through the stan venv
(`/quobyte/proteomics-grp/brett/stan_venv/bin/python` on Hive,
`/opt/anaconda3/bin/python3` on the Mac) — never the bare `python3` that
might land in cron's PATH.

**`sslmode=verify-full` cert path broken on Mac.** Use `sslmode=require`.
The CA bundle PG Farm publishes doesn't resolve cleanly through macOS's
trust store; fixing it is on the backlog.

**Cached connection lifetime.** psycopg2 connections survive `with`-block
exits (the context manager commits/rollbacks but doesn't close). The cache
in `_connect()` relies on this. A bad refactor could regress to per-row
connects → 2.5 h recovery instead of 5 min.

**Don't put compute on Hive's login node.** Already burned by this twice
(cron + orphan recovery). For anything that runs more than ~5 s on Hive,
submit via SLURM (`low` partition for batch, `high` for live QC).

**`--backend sqlite` still writes pegdrift/TIC/dispatch_attempt; PG mode
does not.** Those child tables are a known follow-up. If you need a fully
populated row in PG, run the recovery then enqueue a per-instrument
backfill that calls `_apply_pegdrift_jsons`, `_persist_tic`, etc., reading
from the same `/processing/` artifacts.

**Token expiry.** Every 7 days the CAS bearer dies and everything silently
breaks. Wednesday-9am-Pacific email reminder is the safety net; if you see
auth failures, that's the first thing to check.

---

## Recurring chores

| When               | What                                          |
|--------------------|-----------------------------------------------|
| Every Wednesday    | `pgfarm auth login`, write to token file      |
| When schema drifts | Update `PG_COLUMN_TYPES` in migrate script    |
|                    | + `_build_runs_row` in `stan.db`              |
| New instrument     | Add to `FAMILY_TO_HOST_ORIGIN` in `db_pg.py`  |
|                    | + new entry in cron's host loop               |

---

## See also

- `stan/db_pg.py` — implementation
- `scripts/migrate_sqlite_to_pgfarm.py` — schema DDL + sync logic
- `scripts/cron_sync_to_pgfarm.sh` — wrapper
- `scripts/repair_and_reingest.sh` — Hive recovery driver
- `~/Library/LaunchAgents/com.brettphinney.stan-pgfarm-sync.plist` — Mac cron
- CLAUDE.md → "Cluster-only search policy" and "scancel must filter" (related Hive rules)
