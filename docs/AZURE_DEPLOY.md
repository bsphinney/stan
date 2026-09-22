# Deploying STAN to Azure

The hosted dashboard (`ucd.stan-proteomics.org`, app `stan-ucd-proteomics` in
`rg-fran`) is a **code deploy** on `PYTHON|3.11`, started with

```
python -m uvicorn stan.dashboard.server:app --host 0.0.0.0 --port 8000
```

## The package MUST contain requirements.txt

`SCM_DO_BUILD_DURING_DEPLOYMENT=true`, so Oryx builds the virtualenv
(`antenv`) from `requirements.txt` in the zip root. **If that file is absent,
the deploy replaces `wwwroot`, builds nothing, and the app loses every
dependency it had** — the container then fails to start with:

```
WARNING: Could not find virtual environment directory /home/site/wwwroot/antenv
/opt/python/3.11.15/bin/python: No module named uvicorn
```

That took the public site down on 2026-08-31. The staging directory had been
assembled under `/tmp`, and `requirements.txt` was swept with it; the missing
file produced a *successful-looking* build ("Errors (0)") followed by
"Deployment Failed". The canonical copy now lives in the repo at
`deploy/requirements-azure.txt` precisely so this cannot recur.

## Check the JSX first

`index.html` carries ~9,000 lines of in-browser-transpiled JSX, and a syntax
error there does not degrade gracefully: Babel fails, React never mounts, and
the whole dashboard is a blank page. Counting brackets is not a syntax check.

```bash
npm install --no-save @babel/parser        # once
node scripts/check_jsx.js stan/dashboard/public/index.html
```

Exit 0 means every block parses. A failure reports the line number **in
index.html**, not in the extracted block.

## Build the package

```bash
STAGE=$(mktemp -d)
rsync -a --exclude '__pycache__' --exclude '*.pyc' stan/ "$STAGE/stan/"
cp pyproject.toml README.md "$STAGE/"
cp deploy/requirements-azure.txt "$STAGE/requirements.txt"   # REQUIRED
mkdir -p "$STAGE/config" && cp config/*.yml "$STAGE/config/"  # REQUIRED
( cd "$STAGE" && zip -qr /tmp/stan_deploy.zip . -x '*.pyc' '*__pycache__*' )
unzip -l /tmp/stan_deploy.zip | grep -E 'requirements.txt|config/columns.yml'   # both, before deploying
```

### config/ is part of the package

`resolve_config_path()` looks in `~/STAN/` and then in `<package>/config/`,
which on the App Service is `/home/site/wwwroot/config` -- inside the zip. A
package built without it answers `/api/columns` with `{"columns": []}`, and
the Maintenance panel then shows **"Column not recorded"** instead of the
lab's standard column, because the frontend has no catalogue to look the
default up in. That is what happened on 2026-09-04.

Only the YAML ships. `config/` also holds `bruker_maintenance.json` and
`evosep_column_health.json`, which are local extractor output and are the
FILE FALLBACK those endpoints use when PG Farm is unreachable -- shipping a
snapshot would serve months-old maintenance data as if it were current, with
nothing on screen to say so. Absent, the panel hides itself instead.

## Deploy

Use `config-zip`, not `az webapp deploy`. The OneDeploy path
(`az webapp deploy`) failed repeatedly on 2026-08-31 with a 504 at the
gateway while the deployment itself recorded `status=3` (failed):

```bash
az webapp deployment source config-zip --subscription "$SUB" \
  -g rg-fran -n stan-ucd-proteomics --src /tmp/stan_deploy.zip
```

## Verify, and do not trust the exit code

`az` can print a 504 while the deployment fails for an unrelated reason, and a
shell `exit=$?` after an `echo` reports the echo. Check the app itself:

```bash
curl -s https://ucd.stan-proteomics.org/api/capabilities   # version should have moved
curl -s -o /dev/null -w '%{http_code}\n' https://ucd.stan-proteomics.org/   # 200
```

Deployment records tell the truth when a deploy looks stuck — `status` 4 is
success, 3 is failure:

```bash
az rest --method get --uri "https://management.azure.com/subscriptions/$SUB/resourceGroups/rg-fran/providers/Microsoft.Web/sites/stan-ucd-proteomics/deployments?api-version=2022-03-01"
az webapp log deployment show -g rg-fran -n stan-ucd-proteomics   # why it failed
```

## App settings that cost money

The app is AlwaysOn and runs the PG→SQLite mirror loop
(`stan/sync/pg_to_sqlite.py`) every `STAN_PG_REFRESH_SECONDS`, into
`STAN_DB_PATH=/tmp/stan-mirror.db`. PG Farm bills egress, so check what that
loop costs:

- **v1.1.4 and older** copied every mirrored table in full on every tick:
  73 MB per tick, ~19 GB/day.
- **v1.1.8 and newer** fingerprint each table. A quiet tick is ~3 KB
  (measured against live PG, 2026-09-22). A tick in which something changed
  ships that table's key list: ~360 KB for `runs`, and ~1 MB when a new QC
  run touches several tables at once.
- `STAN_PG_CLOUD_FULL_REFRESH` must stay **unset**. Before v1.1.8 it
  re-downloaded 50 clouds every tick, about 2 GB/day. It is ignored now,
  with a warning.

On 2026-09-22 the setting was raised from 300 to 3600 to stop the bleeding
before the fix was deployed. With v1.1.8 or newer running, 300 is fine again:

```bash
az webapp config appsettings set -g rg-fran -n stan-ucd-proteomics \
  --settings STAN_PG_REFRESH_SECONDS=300
```

`/tmp` is wiped on every restart, and each restart re-pulls the whole mirror
once. That is ~73 MB, plus ~88 MB of ion clouds drained 50 per tick. A deploy
therefore costs a few hundred MB of egress, which is fine. A restart loop
would not be.

## Expected public surface after a deploy

| Route | Anonymous | Why |
|---|---|---|
| `GET /` | 200 | public by design |
| `GET /api/ht/submission` | 403 | customer submission data |
| `POST /api/arcade/score` | 200 | shared leaderboard |
| `POST /api/fleet/command` | 403 | RCE against instrument PCs |
| `POST /api/instruments/{i}/events` | 403 | needs a signed-in operator |
