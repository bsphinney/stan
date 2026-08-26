# PG Farm access via the service account — quickstart for a Claude session

> **Purpose.** Hand this file to a fresh Claude that just needs to *connect to
> and query* STAN's central Postgres (PG Farm) using the
> `genome-proteomics-service-account`. For the full architecture (schema,
> backend dispatch, cron sync, orphan recovery) read `docs/PG_FARM.md` — this
> is only the access cheat-sheet.

---

## What you're connecting to

```
host      pgfarm.library.ucdavis.edu
port      5432
database  uc-davis-genome-center-proteomics-core/stan
user      genome-proteomics-service-account
sslmode   require            # NOT verify-full — cert path is broken on Mac
password  = the 7-day service-account token (see below)
```

Driver is **psycopg2** (NOT psycopg v3). Reachable from both the Mac dev box
and Hive. The main table is `runs`.

---

## Auth model (mint on demand — v1.0.3+)

1. **Long-lived secret** — the 512-char service-account secret, downloaded from
   the PG Farm UI "rotate" button. It lives, raw (not JSON), in the credential
   file — the same file both STAN and FRAN read:
   - Hive: `/quobyte/proteomics-grp/brett/.pgfarm_token`
   - Mac:  `/Volumes/proteomics-grp/brett/.pgfarm_token`
   - FRAN: `/quobyte/proteomics-grp/de-limp/fran_refresh/.pgfarm_token`
2. **7-day JWT** = the actual Postgres password, minted from that secret by
   POSTing `{username, secret}` to
   `https://pgfarm.library.ucdavis.edu/auth/service-account/login`
   (response field `access_token`).

`_resolve_pgpassword()` accepts **either form** and mints on demand: a value
that looks like a JWT (`eyJ…` with two dots) is used as-is; anything else is
treated as the secret and exchanged for a fresh JWT on every connect. This is
the same pattern FRAN's `_token()` has used reliably for months.

So in practice: **read the credential file and hand it to STAN — it sorts out
the rest.** There is no refresh cron to babysit.

> **Do not "helpfully" write a minted JWT back into `.pgfarm_token`.** That
> replaces the long-lived secret with a value that dies in 7 days and re-couples
> PG access to a cron tick succeeding. That precise mistake took STAN's PG
> writes down from 2026-06-10 to 2026-08-26 (see *Postmortem* below).

### Rotation is shared — it invalidates other copies

`genome-proteomics-service-account` is used by **both** STAN and FRAN, and PG
Farm's "rotate" button invalidates the previous secret. Rotating for one
project silently breaks every stale copy. After any rotation, write the new
secret to **all** the paths above.

### Postmortem: the 2026-06→08 PG outage

Three failures stacked, and each one hid the next:

1. The Flinders dispatch cron was installed on 2026-06-10 but **never fired
   once**. `/etc/profile.d/modules.sh` dereferences `LOGNAME`, which cron does
   not set; under the script's `set -u` that killed the shell at the first
   line — before the log file was created, so the failure was totally silent.
2. Nothing refreshed the JWT, so it expired ~7 days later and every PG write
   started failing.
3. A rotation for FRAN on ~2026-06-29 invalidated the secret still sitting in
   STAN's `.pgfarm_secret.json`, so the refresh script could no longer mint
   even when run by hand (`HTTP 400: No access_token received from auth
   server`).

Net effect: `runs` took no timsTOF row between 2026-06-09 and 2026-08-26, and
135 QC acquisitions went unsearched. Fixes: `LOGNAME`/`USER` seeded and the
profile sourcing moved outside `set -u`; the refresh step deleted; auth made
mint-on-demand.

---

## How STAN code resolves the password

`stan/db_pg.py::_resolve_pgpassword()` checks, in order:

1. `$PGPASSWORD` env var (if set, wins)
2. file at `$STAN_PGFARM_TOKEN_FILE`, default
   `/quobyte/proteomics-grp/brett/.pgfarm_token`

Whichever it finds, it then mints if needed: a JWT is passed through, a secret
is exchanged for a fresh JWT (`_is_jwt()` / `_mint_jwt()`). Override the
account name with `$STAN_PGFARM_USER` if it ever changes.

To make STAN write to PG instead of SQLite, also set `STAN_DB_BACKEND=pg`.

---

## Connect — copy/paste

### Python (one-shot query, Mac-side)

```python
import psycopg2
pwd = open('/Volumes/proteomics-grp/brett/.pgfarm_token').read().strip()
with psycopg2.connect(
    host='pgfarm.library.ucdavis.edu', port=5432,
    database='uc-davis-genome-center-proteomics-core/stan',
    sslmode='require',
    user='genome-proteomics-service-account', password=pwd,
) as c:
    cur = c.cursor()
    cur.execute('SELECT host_origin, COUNT(*) FROM runs GROUP BY 1 ORDER BY 1')
    for r in cur.fetchall():
        print(r)
```

On Hive swap the token path to `/quobyte/proteomics-grp/brett/.pgfarm_token`.
Use the stan venv's Python (`/quobyte/proteomics-grp/brett/stan_venv/bin/python`
on Hive, `/opt/anaconda3/bin/python3` on Mac) — the bare system `python3` /
cron Python lacks psycopg2.

### psql / CLI

```bash
PGPASSWORD=$(cat /Volumes/proteomics-grp/brett/.pgfarm_token) \
psql "host=pgfarm.library.ucdavis.edu port=5432 \
      dbname=uc-davis-genome-center-proteomics-core/stan \
      user=genome-proteomics-service-account sslmode=require" \
  -c "SELECT COUNT(*) FROM runs;"
```

### Through STAN

```bash
PGPASSWORD=$(cat /Volumes/proteomics-grp/brett/.pgfarm_token) \
STAN_DB_BACKEND=pg \
stan <command> --backend pg
```

---

## Making the local dashboard show PG data

`stan dashboard` (http://localhost:8421) reads **SQLite only** — it has no PG
backend. Once the fleet moved to PG, `~/.stan/stan.db` went empty and the
dashboard served an empty Runs/Trends view. Repopulate it:

```bash
export PGPASSWORD="$(cat /Volumes/proteomics-grp/brett/.pgfarm_token)"
python scripts/pull_pg_to_sqlite.py          # copies runs -> ~/.stan/stan.db
stan dashboard                                # http://localhost:8421
```

Re-run the pull whenever you want fresh data; it upserts by `id`. Only `runs`
is copied because it is the only table in PG — the per-run detail tabs
(TIC traces, drift clouds, PEG) read tables that live wherever the SLURM job
wrote them, so those stay empty locally.

## Useful starter queries

```sql
SELECT COUNT(*) FROM runs;                                   -- total rows
SELECT host_origin, COUNT(*) FROM runs GROUP BY 1 ORDER BY 1;-- by host
SELECT COUNT(*) FROM runs                                    -- recently inserted
  WHERE migrated_at > NOW() - INTERVAL '30 minutes';

-- column list
SELECT column_name, data_type FROM information_schema.columns
WHERE table_name = 'runs' ORDER BY ordinal_position;
```

`runs` PK is composite `(host_origin, id)`. `host_origin` values: `lumos`,
`exploris`, `timstof`.

---

## If auth fails

1. Is the token file present and non-empty at the path above? `wc -c` it.
2. Is the secret JSON still at both `.pgfarm_secret.json` paths? The cron mints
   the token from it; if the secret is gone, re-download from the PG Farm UI.
3. Is the Hive refresh cron firing? It runs
   `scripts/pgfarm_refresh_token.py --max-age-days 5` each tick — check the
   dispatch log under `/quobyte/proteomics-grp/STAN/logs/`.
4. Force a fresh mint manually:
   `python scripts/pgfarm_refresh_token.py` (reads the secret, rewrites token).
5. **Do not** fall back to the old personal `brettsp` CAS token — retired in
   v1.0.2.

---

## Don'ts

- Don't use `sslmode=verify-full` (broken cert path on Mac).
- Don't open a new connection per row in a loop — psycopg2 SSL handshake is
  slow; reuse the connection (`stan/db_pg.py::_connect()` caches it).
- Don't run heavy queries from a Hive **login node** — submit via SLURM.
- Don't commit the secret or token to git.

See `docs/PG_FARM.md` for everything else.
