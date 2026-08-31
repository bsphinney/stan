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

## Build the package

```bash
STAGE=$(mktemp -d)
rsync -a --exclude '__pycache__' --exclude '*.pyc' stan/ "$STAGE/stan/"
cp pyproject.toml README.md "$STAGE/"
cp deploy/requirements-azure.txt "$STAGE/requirements.txt"   # REQUIRED
( cd "$STAGE" && zip -qr /tmp/stan_deploy.zip . -x '*.pyc' '*__pycache__*' )
unzip -l /tmp/stan_deploy.zip | grep requirements.txt        # verify before deploying
```

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

## Expected public surface after a deploy

| Route | Anonymous | Why |
|---|---|---|
| `GET /` | 200 | public by design |
| `GET /api/ht/submission` | 403 | customer submission data |
| `POST /api/arcade/score` | 200 | shared leaderboard |
| `POST /api/fleet/command` | 403 | RCE against instrument PCs |
| `POST /api/instruments/{i}/events` | 403 | needs a signed-in operator |
