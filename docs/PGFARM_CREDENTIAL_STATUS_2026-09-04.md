# PG Farm credential status — verified 2026-09-04 (by the FRAN session)

Empirical findings. Every claim below was tested against the live auth server / DB,
not inferred. Secrets themselves were never printed — only SHA-256 prefixes.

## 1. Service-account secret — FIXED, no PG Farm UI download needed

The live secret was already on the Mac: `~/Downloads/service-account-2.json`
(`lastRotatedAt` `2026-06-15T20:10:05.829Z`, secret sha256[:12] = `d7de2efa865f`).

Hive's `/quobyte/proteomics-grp/brett/.pgfarm_secret.json` held the superseded
**Jun 10** secret (sha256[:12] = `0f791c07b3ea`) — identical to
`~/Downloads/service-account.json`. The Jun 15 rotation invalidated it, which is
exactly the `HTTP 400 {"error":"No access_token received from auth server"}`.

Mint test against `/auth/service-account/login`:

| secret | result |
|---|---|
| Jun 10 (old Hive copy) | HTTP 400 `No access_token received from auth server` |
| Jun 15 (`service-account-2.json`) | 200, `expires_in` 604800 |

**Action taken:** copied the Jun 15 secret to Hive, `chmod 600`. Old file backed up
at `/quobyte/proteomics-grp/brett/.pgfarm_secret.json.dead-jun10.bak`.
Re-verified *from Hive*: mint OK, `expires_in` 604800.

## 2. Correction — `.pgfarm_token` was NOT expired

The claim "the token file is from 23 Jul, so anything on Hive authenticating as the
service account has been running on an expired credential" is **not right**.

`.pgfarm_token` on **both** Hive and the Mac contains sha256[:12] = `d7de2efa865f`
— the **live Jun 15 secret**, not a token. It is a raw 512-char secret, so
`_is_jwt()` is False and `db_pg._mint_jwt()` exchanges it on every use. It is
self-refreshing; the file mtime says nothing about validity. Service-account auth on
Hive has been working the whole time. Only the `.pgfarm_secret.json` copy was stale.

Minor: the Mac's `~/.pgfarm_jwt_cache` holds an **expired** service-account JWT
(`exp` 2026-08-27). Harmless if callers re-mint, but delete it if anything reads it directly.

## 3. Owner / CAS login — genuinely required, confirmed

In database `uc-davis-genome-center-proteomics-core/stan`:

- all **15** public tables are owned by `brettsp` (none by the service account)
- `has_schema_privilege('genome-proteomics-service-account','public','CREATE')` = **false**
- `sample_health` has 26 columns and **no `spd`** — the migration has not landed

So `migrations/2026-09-04_sample_health_spd.sql` cannot be applied by the service
account. The header comment in that file is accurate. Brett's CAS
`pgfarm auth login` is the only path. **This step is a real blocker.**

## 4. Do not generalize this to the delimp DB

In `uc-davis-genome-center-proteomics-core/delimp` the service account **owns all 48**
public tables. A rolled-back probe confirmed it can `ALTER TABLE` and `CREATE TABLE`
there. FRAN-side migrations do **not** need CAS. The restriction is stan-specific.

## 5. Worth doing while Brett is logged in as owner

One-time, in the stan DB, so future migrations never need a browser flow again:

```sql
GRANT CREATE ON SCHEMA public TO "genome-proteomics-service-account";
-- and/or, per table:
ALTER TABLE sample_health OWNER TO "genome-proteomics-service-account";
```

Brett's call — it widens what automation can do to the stan schema.

## 6. Ordering note stands

Guarding `insert_sample_health_pg` by narrowing its column list is correct and still
required: `spd` is confirmed absent from the live table right now, so unguarded
writes naming it would fail until the migration lands.
