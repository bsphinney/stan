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

## Auth model (two tiers)

1. **Long-lived secret** — `service-account.json` = `{username, secret}` (512-char
   secret), downloaded from the PG Farm UI "rotate" button. chmod 600 at:
   - Hive: `/quobyte/proteomics-grp/brett/.pgfarm_secret.json`
   - Mac:  `/Volumes/proteomics-grp/brett/.pgfarm_secret.json`
2. **7-day token** = the actual Postgres password. Minted from the secret by
   POSTing `{username, secret}` to
   `https://pgfarm.library.ucdavis.edu/auth/service-account/login`
   (response field `access_token`). You normally **don't mint it yourself** —
   the Hive cron refreshes it and writes the token file:
   - Hive: `/quobyte/proteomics-grp/brett/.pgfarm_token`
   - Mac:  `/Volumes/proteomics-grp/brett/.pgfarm_token`

So in practice: **read the token file, use it as the password.** Done.

---

## How STAN code resolves the password

`stan/db_pg.py::_resolve_pgpassword()` checks, in order:

1. `$PGPASSWORD` env var (if set, wins)
2. file at `$STAN_PGFARM_TOKEN_FILE`, default
   `/quobyte/proteomics-grp/brett/.pgfarm_token`

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
