# Hosting the STAN dashboard on Azure with UC Davis SSO

**Status: PLAN ONLY — nothing has been provisioned.** No Azure resources were created, no app
registrations made, no DNS changed, no commits pushed. Everything below is a proposal for Brett to
approve.

**Goal:** run the STAN QC dashboard (`stan dashboard` — FastAPI + a single-file React page, today on
`localhost:8421`) at a stable HTTPS URL that **only Proteomics Core lab members can reach**, using the
same campus login FRAN already uses.

**Anchor:** FRAN is already doing exactly this, and this plan deliberately mirrors it so Brett
maintains *one* hosting pattern, not two.

---

## 0. What was verified vs. assumed

Because a confident wrong answer here costs money and can open a hole, here is the honest split.

### Verified live (read-only commands, 2026-08-26)

| Fact | How it was checked |
|---|---|
| FRAN runs on **Azure App Service**, resource group `rg-fran`, West US 2 | `az resource list --subscription 7c2f921b-…` |
| Plan `plan-fran` is **Linux, B1 Basic, 1 worker, currently hosting 1 site** | `az appservice plan list` |
| App `fran-ucd-proteomics` runs the **`PYTHON\|3.11` runtime** (a *code* deploy, **not** a container) with startup command `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000` | `az webapp config show` |
| EasyAuth v2 is **enabled**, Entra ID client id `3129af59-332e-428a-abdc-9e25e143292b`, issuer `https://login.microsoftonline.com/a8046f64-66c0-4f00-9046-c8daf92ff62b/v2.0`, `unauthenticatedClientAction = **AllowAnonymous**`, `requireAuthentication = false`, token store on | `az rest GET …/config/authsettingsV2/list` |
| Access control today is an **app-setting allowlist**, not a group: `FRAN_ALLOWED_USERS = bsphinney, msalemi, ggrigorean, amschaal, lydixon` (@ucdavis.edu) | `az webapp config appsettings list` |
| Secrets come from **Key Vault references**: `DELIMP_PG_SECRET` → `@Microsoft.KeyVault(VaultName=kv-fran-protcore;SecretName=delimp-pg-secret)`, and `MICROSOFT_PROVIDER_AUTHENTICATION_SECRET` → `…;SecretName=easyauth-client-secret)` | same |
| Deploys are **zip deploys with an Oryx server-side build** (`SCM_DO_BUILD_DURING_DEPLOYMENT=true`) | same |
| **PG Farm is reachable from Azure.** `https://fran.stan-proteomics.org/api/health` returns `{"connected":true,"read_only":"on","error":null,"version":"0.25.1"}` — that app is on Azure and is connected to PG Farm right now | `curl` |
| `https://fran.stan-proteomics.org/login` → `302` → `https://fran.stan-proteomics.org/.auth/login/aad?post_login_redirect_uri=/` | `curl -w` |
| **PG Farm is not behind the campus firewall at all.** `pgfarm.library.ucdavis.edu` → `34.170.150.232`, a public Google Cloud address, resolvable from a public resolver (8.8.8.8) | `dig`, `nslookup … 8.8.8.8` |
| Brett's `az` CLI is logged in as `bsphinney@ucdavis.edu` to subscription **`prot-core`** (`7c2f921b-c35c-4c1c-aa76-bc51a89e2229`), UC Davis tenant `a8046f64-66c0-4f00-9046-c8daf92ff62b` | `az account show` |

### Assumed / unverified — read these before spending money

1. **Entra ID P1 licensing.** Assigning a *security group* to an enterprise application ("Assignment
   required = Yes") requires Entra ID P1 or above. Assigning *individual users* works on the free
   tier. UC Davis M365 A3/A5 normally includes P1, but **this was not verified.** The plan below
   works either way — see §2.
2. **Whether Brett may create a security group himself.** FRAN's own plan (`corpus_browser/docs/AZURE_HOSTING_PLAN.md`,
   Phase 0 step 3) says to file a ServiceHub → IAM request if self-service creation is unavailable.
   FRAN never actually did this — it is still on the allowlist — so the group path is **untested in
   this environment.**
3. **Whether a data/security review is needed.** FRAN filed one because it serves P3 confidential
   PI/customer names. STAN's dashboard serves instrument QC metrics and no patient or sample
   metadata (CLAUDE.md, "Privacy — hard rules"), so it is a *lower* classification than FRAN.
   Brett should confirm that a new review is not required, but this should be easier than FRAN's, not
   harder.
4. **Azure list prices** (§5) are approximate and change; confirm in the Azure pricing calculator.
5. Adding a second app to `plan-fran` is standard Azure and supported, but the **performance impact of
   two Python apps sharing one B1 core has not been measured.**

---

## 1. Recommended architecture

Mirror FRAN exactly, with one deliberate difference in the auth mode.

```
lab member ──► https://stan.stan-proteomics.org
                      │
                      ▼
        Azure App Service EasyAuth  (Entra ID, UC Davis tenant)
        unauthenticatedClientAction = RedirectToLoginPage   ◄── the delta vs FRAN
                      │  no anonymous request ever reaches the app
                      ▼
        Entra Enterprise App, "Assignment required = Yes"
        + STAN-Lab-Users group (or individual assignments)
                      │  non-assigned UC Davis accounts are refused at Entra
                      ▼
        stan-ucd-proteomics   (Web App, PYTHON|3.11, uvicorn)
        on the EXISTING plan-fran  (Linux B1)
                      │
                      ├─ SQLite cache at /tmp/stan/stan.db  (ephemeral, derived)
                      │        ▲ refreshed every 5 min by the existing
                      │        │ _pg_refresh_loop() background task
                      ▼        │
        PG Farm  pgfarm.library.ucdavis.edu:5432
        db  uc-davis-genome-center-proteomics-core/stan
        user genome-proteomics-service-account
        password = JWT minted on demand from the Key Vault secret
```

### Why this shape

**App Service + EasyAuth, not Container Apps / Static Web Apps / a VM.** FRAN proved this exact
combination works in this exact subscription against this exact identity provider. Container Apps
would need its own auth wiring; Static Web Apps can't host a long-lived background refresh loop; a VM
means Brett patches an OS forever. Reusing FRAN's pattern means one runbook, one Key Vault, one set of
habits.

**A *code* deploy (`PYTHON|3.11` + zip), not a container.** Note that FRAN's
`corpus_browser/deploy/azure_setup.sh` describes an ACR + container path — **that is not what is
actually running.** There is no ACR in the subscription; the live config is `linuxFxVersion:
PYTHON|3.11` with a uvicorn startup command. Follow what is deployed, not what the script says. This
also saves the ~$5/mo ACR charge and a build step.

**Reuse `plan-fran` rather than creating a new plan.** Marginal plan cost is $0, and the two apps are
maintained together. If they contend for the single B1 core, bump the plan to B2 once and *both* apps
get more headroom (§5).

**Reuse `kv-fran-protcore` and the *same* `delimp-pg-secret`.** This is not laziness — it is the
correct call. STAN and FRAN authenticate to PG Farm as the **same service account**
(`genome-proteomics-service-account`), and `docs/PG_FARM_ACCESS.md:53-58` warns that PG Farm's
"rotate" button invalidates the previous secret, silently breaking every stale copy. There are
already copies on Hive and on Brett's Mac. Pointing STAN's Key Vault reference at the *existing*
secret means a rotation updates **one** vault entry and both web apps pick it up, instead of creating
a fourth stale copy.

**SQLite on ephemeral local disk, deliberately.** The dashboard is a SQLite *reader*; PG Farm is the
canonical store (`stan/sync/pg_to_sqlite.py:3`). The local DB is pure derived cache. On App Service,
`$HOME` (`/home`) is a persistent Azure Files share — and SQLite over network storage is precisely
the failure mode STAN already documented (`stan/db.py:18-28`, ~37 jobs lost to `SQLITE_IOERR` on
Quobyte). So set `STAN_DB_PATH=/tmp/stan/stan.db` to keep it on local disk. Cold start is fine: the
refresh loop performs its first pull *before* it sleeps (`server.py:186-193`), so a restarted
container repopulates within seconds.

---

## 2. Auth and authorization design

### The important distinction

FRAN is **public with a login that unlocks more**. STAN is the opposite: **nothing is public.** That
makes STAN's configuration *simpler* than FRAN's and removes most of its risk, because we can require
authentication at the platform edge and no anonymous request ever reaches Python.

Set `unauthenticatedClientAction = RedirectToLoginPage` (FRAN uses `AllowAnonymous`). App Service then
performs the login itself, and only after validating it does it forward the request with a
platform-set `X-MS-CLIENT-PRINCIPAL` header — a header App Service strips from client input, so it
cannot be spoofed (see the reasoning in `~/Documents/claude/corpus_browser/app/auth.py`, "Why trust
the header?").

### Authentication ≠ authorization

Requiring login only proves the caller is *somebody at UC Davis* — roughly 40,000 accounts. Two more
layers restrict it to the lab:

**Layer 1 (primary) — Entra enterprise-app assignment.** In Entra → Enterprise applications → the STAN
app → Properties → **Assignment required = Yes**, then assign who may use it. Non-assigned accounts
are refused *by Entra*, with error `AADSTS50105`, and never reach the app at all.

- **If Entra ID P1 is available:** create a security group `STAN-Lab-Users` with Brett as owner and
  assign the group. Adding a lab member is then one click in Entra with no redeploy — the long-term
  answer.
- **If P1 is not available:** assign the five-or-so individual users directly. Free-tier compatible,
  same enforcement, slightly more clicking.

**Layer 2 (defense in depth) — an app-side allowlist.** Port FRAN's `app/auth.py` pattern into STAN: a
small module that decodes `X-MS-CLIENT-PRINCIPAL`, extracts the UPN/email, and checks it against a
`STAN_ALLOWED_USERS` app setting (and/or a `STAN_REQUIRED_GROUP` object id), **fail-closed** — if
neither is configured, deny. This matters because Layer 1 is a portal setting that a future
misconfiguration could silently flip; Layer 2 is in source control. FRAN's implementation is directly
reusable and already handles the multiple Entra group/UPN claim shapes.

### Granting a new lab member access

1. Add them to the `STAN-Lab-Users` Entra group (or assign them individually in the enterprise app).
2. Add their `@ucdavis.edu` address to the `STAN_ALLOWED_USERS` app setting.
3. No redeploy, no restart. They browse to the URL and get campus SSO + Duo.

Removing someone is the same two steps in reverse. Note that revocation is not instantaneous — the
EasyAuth session cookie/token store means an already-signed-in user may retain access until their
session expires.

### What must NOT be exposed — this is the sharpest issue in the whole plan

`stan/dashboard/server.py:31-42` carries this comment verbatim:

```
# v0.2.307: tighten CORS + add Origin gate on state-changing requests.
# Pre-fix the dashboard had `allow_origins=["*"]` and the
# /api/fleet/command endpoint had zero auth, so any drive-by URL the
# operator visited while the dashboard was open could `fetch` POST
# update_stan / apply_config / submit_all and trigger code execution
# on every instrument PC. Defense:
#   1) CORS now allows only the dashboard's own localhost origins.
#   2) A request middleware rejects POST/PUT/DELETE/PATCH whose
#      Origin header points anywhere other than our own origins.
#      Missing Origin is allowed — covers operator CLI clients
#      (curl, requests) which is fine because the listener is
#      already 127.0.0.1-bound, not externally reachable.
```

**That last sentence is the load-bearing assumption, and hosting on Azure breaks it.** The Origin
middleware (`server.py:73-96`) deliberately allows mutating requests that carry *no* `Origin` header,
justified by the listener being bound to `127.0.0.1`. On a public URL, a plain
`curl -X POST https://stan…/api/fleet/command` sends no Origin and sails straight through. There is no
authentication anywhere in `server.py`.

Requiring EasyAuth login (above) neutralises this, because unauthenticated requests are rejected
before reaching Python. But relying on a single portal toggle for something that dispatches remote
code execution is not enough. The mutating endpoints, all currently guarded only by that Origin
middleware:

| Route | Method | Risk |
|---|---|---|
| `/api/fleet/command` (`server.py:1618`) | POST | Enqueues whitelisted commands for any instrument host — the whitelist (`stan/control.py:1999-2054`) includes `update_stan`, `apply_config`, `restart_watcher`, `submit_all`, `v1_prep`. **Remote code execution on instrument PCs.** |
| `/api/instruments` (`:421`) | POST | Overwrites `instruments.yml` from the request body |
| `/api/instruments/{index}` (`:442`) | DELETE | Rewrites `instruments.yml` |
| `/api/thresholds` (`:1144`) | POST | Overwrites `thresholds.yml` from the request body |
| `/api/refresh` (`:196`) | POST | Forces an immediate PG Farm pull — unauthenticated DoS amplification against PG Farm |
| `/api/runs/{run_id}/hide` (`:293`) | POST | DB write |
| `/api/instruments/{instrument}/events` (`:1199`) | POST | DB write |
| `/api/community/submit` (`:1271`) | POST | Publishes lab QC data to the public HF relay |
| `/api/dashboard-error` (`:1314`) | POST | Appends caller-controlled text to a log file — unbounded disk growth |
| `/api/dashboard-errors` (`:1348`) | GET | Returns stack traces |
| `/docs` | GET | FastAPI Swagger UI, on by default |

There is one accidental safety net: `/api/fleet/command` calls `get_hive_mirror_root()`
(`stan/config.py:320`), which on Azure finds no Quobyte mount and no `HIVE_MIRROR_DIR` env var, so it
returns `None` and the endpoint 503s. **Do not rely on this** — it is one stray env var away from
being live, and it does not protect the other ten routes.

**Required change before deploying (small, and the only code change this plan needs):** add a
`STAN_DASHBOARD_READONLY=1` env gate that (a) returns 403 from every mutating route, (b) disables
`/docs` and `/redoc` via `FastAPI(docs_url=None, redoc_url=None)`, and (c) hides `/api/dashboard-errors`.
The Azure app sets it; local operator installs never do, so `stan dashboard` on an instrument PC is
unaffected. Also set `STAN_DASHBOARD_EXTRA_ORIGINS` to the Azure origin (`server.py:57-66`) or the
browser's own same-origin POSTs will 403.

---

## 3. How the dashboard gets its data on Azure

**This was flagged as the possible deciding constraint. It is not a constraint — it is settled.**

`pgfarm.library.ucdavis.edu` resolves to **34.170.150.232**, a public Google Cloud address, from a
public resolver. PG Farm is a UC Davis Library service hosted on GCP with a public endpoint; it is not
inside the campus network perimeter. And the decisive evidence is that **FRAN, running on Azure App
Service right now, is connected to it**: `https://fran.stan-proteomics.org/api/health` →
`{"connected":true,"read_only":"on","error":null}`. Same host, same port, same service account, same
`sslmode=require` — only the database name differs (`…/delimp` vs `…/stan`).

No tunnel, no VPN, no ExpressRoute, no campus network. **No fallback is needed.** (Noted for the
record: had it *not* been reachable, the fallback would have been a small push-based sync from Hive or
the Mac into Azure storage, since Hive already has PG access — but that contingency is moot.)

Two supporting details:

**Credentials.** `stan/db_pg.py:108-127` reads `PGPASSWORD` first, and — importantly — mints a fresh
JWT on the spot if the value is the long-lived service-account secret rather than a JWT
(`_resolve_pgpassword` → `_mint_jwt`). So the Azure app needs exactly one setting:

```
PGPASSWORD = @Microsoft.KeyVault(VaultName=kv-fran-protcore;SecretName=delimp-pg-secret)
```

Mint-on-demand means **no token file and no refresh cron on Azure** — which is the point of the
v1.0.3 design (`docs/PG_FARM_ACCESS.md`: "There is no refresh cron to babysit"). Note `docs/PG_FARM.md:39-41`
still describes the older cron model; the code follows `PG_FARM_ACCESS.md`. The container needs
outbound **443** to `pgfarm.library.ucdavis.edu` (to mint) as well as **5432** (to query); App Service
allows both by default.

**Dependencies.** `psycopg2` is **not** declared in `pyproject.toml` — it is imported lazily
(`db_pg.py:172,223`). If it is missing, `_pull_from_pg_once` swallows the ImportError
(`server.py:158-180` never raises) and the dashboard silently serves an empty DB forever. **The Azure
`requirements.txt` must list `psycopg2-binary` explicitly.** This is the single most likely way to
deploy something that looks healthy and shows no data.

Conversely, the dashboard's import path is light: `server.py` module-level imports are only stdlib +
`yaml` + `fastapi` + `pydantic` + `stan.config` + `stan.db`. It does **not** need `polars`, `pyarrow`,
`Pillow`, `mss`, or `pygetwindow` — and `mss`/`pygetwindow` are desktop screen-capture libraries that
want an X11 display. Ship a purpose-built `requirements-azure.txt` rather than installing the package's
full dependency set:

```
fastapi>=0.110
uvicorn[standard]>=0.29
pyyaml>=6.0
pydantic>=2
psycopg2-binary>=2.9
```

The trade-off, stated plainly: trimming deps turns `/api/fleet/command` and `/api/community/submit`
into 500s rather than clean errors, because they lazily import wider graphs. Since §2 disables both
anyway, that is acceptable — but the readonly gate should return 403 *before* the import is attempted.

**One config item that matters more than it looks:** set **`alwaysOn = true`** on the web app. FRAN
runs with `alwaysOn: false`, which is fine for FRAN because it queries PG per request. STAN's freshness
depends on an in-process asyncio background task (`_pg_refresh_loop`, started in the `startup` event
handler at `server.py:204-211`). If App Service unloads the idle app after ~20 minutes, **that loop
dies and the data silently goes stale** until someone loads the page. B1 supports Always On.

---

## 4. What STAN's dashboard is, in one paragraph

`stan/dashboard/server.py:29` creates `app = FastAPI(...)`, so the ASGI target is
**`stan.dashboard.server:app`** — already the exact string used by `stan/cli.py:2366`. `stan dashboard`
binds `127.0.0.1:8421` by default (`cli.py:2313-2316`), though it silently widens to `0.0.0.0` when
Tailscale is detected (`cli.py:2350-2351`). `GET /` serves `stan/dashboard/public/index.html`
(~268 KB, 5,653 lines, a single-file React page), and the same directory is mounted at `/static`
(`server.py:1692`). The database is opened via `get_db_path()` (`stan/db.py:413-424`), which honours
`STAN_DB_PATH` and otherwise defaults to `~/.stan/stan.db`. `central_mode` in `/api/capabilities`
(`server.py:216-235`) is a **UI-only** flag — it hides the Config tab once a PG pull has succeeded, but
it does **not** disable the config-writing endpoints. The repo currently contains **no Dockerfile, no
Bicep, no Terraform, no Procfile, and no requirements.txt** — this is a from-scratch deployment.

---

## 5. Rough monthly cost

| Item | Cost |
|---|---|
| App Service plan — **reuse existing `plan-fran` (B1)** | **$0 marginal** (already billed ~$13/mo for FRAN) |
| *Optional* bump B1 → B2 if the two apps contend | +~$12/mo (~$25/mo total for **both** apps) |
| Key Vault — **reuse `kv-fran-protcore`** | ~$0 (secret operations are fractions of a cent) |
| App Service managed TLS certificate | Free |
| Custom domain `stan.stan-proteomics.org` on Cloudflare | $0 (domain already owned) |
| Egress bandwidth | First 100 GB/mo free; a ~270 KB page for a handful of users is nowhere near it |
| Entra ID SSO | Included with campus M365 — **unless** the group-assignment path needs P1 (§0, open question) |
| Azure Container Registry | **$0 — not used.** FRAN's setup script mentions ACR; the live deployment is a code deploy |

**Bottom line: $0–13/mo marginal, worst case ~$25/mo total for FRAN *and* STAN together.** Billed to
the existing `prot-core` AggieCloud subscription — no new cost center, no new approvals.

---

## 6. Deploy runbook

Do not run this blind. Steps 1 and 6 need a human in the Azure Portal.

### Step 0 — code changes first (in the repo, before any Azure work)

1. Add the `STAN_DASHBOARD_READONLY=1` gate described in §2 (mutating routes → 403; `docs_url=None`,
   `redoc_url=None`; hide `/api/dashboard-errors`).
2. Add `stan/dashboard/auth.py`, ported from `~/Documents/claude/corpus_browser/app/auth.py`: decode
   `X-MS-CLIENT-PRINCIPAL`, check against `STAN_ALLOWED_USERS` / `STAN_REQUIRED_GROUP`, fail-closed.
   Wire it as middleware that 403s any request failing the check, and no-ops when neither setting is
   present *and* `STAN_DASHBOARD_READONLY` is unset (so local `stan dashboard` is untouched).
3. Add `requirements-azure.txt` (§3) and a `.deployment`/startup command.
4. Bump `pyproject.toml` **and** `stan/__init__.py` together — never one without the other.
5. Add a `/api/health` route mirroring FRAN's, returning PG connection state. It is what makes
   step 8 verifiable, and FRAN's is what proved PG reachability for this plan.
6. Test locally with a faked principal header before deploying: no header → 403; header for a
   non-allowlisted user → 403; allowlisted user → 200.

### Step 1 — Entra app registration and group (Portal; needs a human)

```
Portal → App registrations → New registration
  Name: STAN Dashboard
  Supported account types: this organizational directory only (UC Davis)
  Redirect URI (Web): https://stan-ucd-proteomics.azurewebsites.net/.auth/login/aad/callback
```

Then Entra → Groups → New security group `STAN-Lab-Users`, owner = Brett, members = the lab. If P1
is unavailable, skip the group and assign users individually in step 6.

### Step 2 — create the web app on the existing plan

```bash
S=7c2f921b-c35c-4c1c-aa76-bc51a89e2229

az webapp create \
  --subscription "$S" -g rg-fran -p plan-fran \
  -n stan-ucd-proteomics \
  --runtime "PYTHON:3.11"

az webapp config set --subscription "$S" -g rg-fran -n stan-ucd-proteomics \
  --startup-file "python -m uvicorn stan.dashboard.server:app --host 0.0.0.0 --port 8000" \
  --always-on true --min-tls-version 1.2

az webapp update --subscription "$S" -g rg-fran -n stan-ucd-proteomics --https-only true
```

### Step 3 — managed identity + Key Vault read access (reusing FRAN's vault)

```bash
az webapp identity assign --subscription "$S" -g rg-fran -n stan-ucd-proteomics
MI=$(az webapp identity show --subscription "$S" -g rg-fran -n stan-ucd-proteomics --query principalId -o tsv)
KV_ID=$(az keyvault show --subscription "$S" -n kv-fran-protcore --query id -o tsv)
az role assignment create --assignee "$MI" --role "Key Vault Secrets User" --scope "$KV_ID"
```

### Step 4 — app settings

```bash
az webapp config appsettings set --subscription "$S" -g rg-fran -n stan-ucd-proteomics --settings \
  SCM_DO_BUILD_DURING_DEPLOYMENT=true \
  STAN_DASHBOARD_READONLY=1 \
  STAN_DB_PATH=/tmp/stan/stan.db \
  STAN_PG_REFRESH_SECONDS=300 \
  STAN_DB_BACKEND=pg \
  STAN_DASHBOARD_EXTRA_ORIGINS="https://stan-ucd-proteomics.azurewebsites.net,https://stan.stan-proteomics.org" \
  STAN_ALLOWED_USERS="bsphinney@ucdavis.edu,msalemi@ucdavis.edu,ggrigorean@ucdavis.edu,amschaal@ucdavis.edu,lydixon@ucdavis.edu" \
  PGPASSWORD="@Microsoft.KeyVault(VaultName=kv-fran-protcore;SecretName=delimp-pg-secret)"
```

Do **not** set `HIVE_MIRROR_DIR` — leaving it unset keeps `get_hive_mirror_root()` returning `None`,
which is the belt to the readonly gate's braces on `/api/fleet/command`.

### Step 5 — deploy the code

Package the `stan/` source tree plus `requirements-azure.txt` renamed to `requirements.txt` (Oryx
looks for that name), then:

```bash
az webapp deploy --subscription "$S" -g rg-fran -n stan-ucd-proteomics --type zip --src-path stan-azure.zip
```

Note from FRAN's cutover log: basic auth on the SCM site is disabled in this subscription, so the CLI
authenticates via AAD — expect that, it is not an error.

### Step 6 — turn on SSO in **require-auth** mode (Portal; needs a human)

```
Web App → Authentication → Add identity provider → Microsoft
  Tenant type: Workforce (UC Davis)
  App registration: use the existing one from Step 1
  Restrict access: **Require authentication**      ◄── NOT "Allow unauthenticated"
  Unauthenticated requests: HTTP 302 Redirect to log in
```

Then Entra → Enterprise applications → STAN Dashboard → Properties → **Assignment required = Yes**,
and under Users and groups assign `STAN-Lab-Users` (or the individual users). If using the group,
also go to App registration → Token configuration → add the **groups** claim, so the group id reaches
the app for the Layer-2 check.

### Step 7 — custom domain and TLS (optional but recommended)

Mirror FRAN's DNS pattern on the Cloudflare zone `stan-proteomics.org`:

```bash
# 1. Get the verification id, add it to Cloudflare as TXT  asuid.stan
az webapp show --subscription "$S" -g rg-fran -n stan-ucd-proteomics --query customDomainVerificationId -o tsv
# 2. Cloudflare: CNAME  stan → stan-ucd-proteomics.azurewebsites.net   (grey cloud / DNS-only)
# 3. Bind + managed cert
az webapp config hostname add --subscription "$S" -g rg-fran --webapp-name stan-ucd-proteomics \
  --hostname stan.stan-proteomics.org
az webapp config ssl create --subscription "$S" -g rg-fran --name stan-ucd-proteomics \
  --hostname stan.stan-proteomics.org
```

Add `https://stan.stan-proteomics.org/.auth/login/aad/callback` to the app registration's redirect
URIs — FRAN's cutover log records this as step 1 of its own custom-domain move, and forgetting it
breaks login on the custom domain while the azurewebsites.net URL keeps working.

Grey cloud (DNS-only) matters: proxying through Cloudflare's orange cloud would interfere with the
App Service managed certificate's domain validation.

### Step 8 — verify (the acceptance test)

1. **Logged out, incognito** → `https://stan.stan-proteomics.org` redirects to
   `login.microsoftonline.com`. It must **never** render the dashboard.
2. **A UC Davis account NOT assigned to the app** → Entra refuses with `AADSTS50105`. Confirm the
   dashboard does not render.
3. **An assigned lab member** → full dashboard.
4. `GET /api/health` → `connected: true` (this is the PG Farm proof for STAN, mirroring FRAN's).
5. Run counts on the dashboard match `SELECT count(*) FROM runs` on PG Farm.
6. `curl -X POST https://stan.stan-proteomics.org/api/fleet/command -d '{}'` with **no** auth cookie
   and **no** Origin header → must be a login redirect or 403, **never** 200. This is the specific
   regression test for the incident quoted in §2.
7. Leave it idle 30 minutes, reload, confirm the newest run timestamp still advances (proves Always On
   kept the refresh loop alive).

### Step 9 — document it

Per the Golden Rule in CLAUDE.md: update `README.md` (Implementation Status), `docs/user_guide.md`,
and add the new env vars (`STAN_DASHBOARD_READONLY`, `STAN_ALLOWED_USERS`, `STAN_REQUIRED_GROUP`) to
the config documentation, in the same commit as the code change.

---

## 7. Risks and open questions

**Honest, in rough order of how much they could hurt.**

1. **The `/api/fleet/command` RCE path is the real risk of this whole project.** STAN's dashboard was
   written for `127.0.0.1` and its CSRF middleware explicitly allows Origin-less mutating requests
   *because* of that binding. Putting it on a public hostname invalidates the assumption the security
   comment is built on. EasyAuth in require-auth mode closes it, but the readonly gate (§2, Step 0)
   should ship **in the same change** so the app is safe even if the portal setting is later flipped.
   Do not deploy without both.
2. **Entra ID P1 licensing for group assignment — unverified.** If P1 is unavailable, fall back to
   individual user assignment. Functionally equivalent, just manual. Check before promising Brett the
   "one click to add a lab member" workflow.
3. **STAN's PG Farm grants include write access.** `docs/PG_FARM.md:47` lists
   `SELECT, INSERT, UPDATE, DELETE` on `runs`. The dashboard only reads (via `pull_from_pg`), but a
   public-facing app holding write credentials to the fleet's canonical store is more privilege than
   it needs. **Recommend asking the PG Farm admins for a SELECT-only role for the Azure app**, and
   note that FRAN goes further — it forces `default_transaction_read_only=on` at the session level
   (`corpus_browser/app/db.py`), which STAN's `db_pg.py` does not. Worth copying.
4. **Secret rotation is shared and fragile.** One `genome-proteomics-service-account` secret is now
   copied to Hive, Brett's Mac, and Key Vault, and PG Farm's rotate button invalidates the old one
   (`docs/PG_FARM_ACCESS.md:53-58`). Reusing `kv-fran-protcore` avoids adding a *fourth* copy, but
   rotation still means updating the vault entry plus the two file copies. Add Key Vault to whatever
   rotation checklist exists.
5. **The React page loads four scripts from public CDNs with no SRI and no CSP**
   (`public/index.html:16-22`): React 18, ReactDOM, `@babel/standalone`, and Plotly. Babel-standalone
   means JSX is transpiled in the browser on every page load. Behind a login this is a lower-severity
   supply-chain exposure than it would be publicly, but it is still a third party executing script in
   an authenticated session. **Recommend vendoring all four into `public/` (already mounted at
   `/static`) before or shortly after go-live.** Only Plotly has a documented fallback; if jsdelivr is
   blocked or down, the page is blank.
6. **`/static` also serves ~1.9 MB of PNGs and several standalone pages** (`museum.html`,
   `arcade.html`, `karatemass.html`, `keratin-invaders.html`, `mzork.html`, `angry-specs.html`).
   Harmless behind login, but Brett should know they ship.
7. **Two Python apps on one B1 core is unmeasured.** B1 is 1 vCPU / 1.75 GB. If FRAN gets slower after
   STAN goes up, bump to B2 (§5) — a one-line change that helps both.
8. **Session revocation is not immediate.** Removing someone from the group stops *new* logins; an
   existing EasyAuth session persists until it expires. For urgent removal, restart the app and rotate
   `MICROSOFT_PROVIDER_AUTHENTICATION_SECRET`.
9. **This does not replace the public HF Space.** `https://huggingface.co/spaces/brettsp/stan` remains
   the public community benchmark dashboard, and per the `reference_community_site_reads_parquet`
   memory it reads relay parquet, not PG. The Azure app is the *internal fleet view*. Two different
   audiences, two different deployments — do not conflate them.
10. **Data/security review scope — unconfirmed.** FRAN needed one for P3 confidential names. STAN
    collects no patient or sample metadata, so this should be lighter, but Brett should confirm rather
    than assume.
11. **`docs/PG_FARM.md` and `docs/PG_FARM_ACCESS.md` disagree** about token refresh (cron vs
    mint-on-demand). The code implements mint-on-demand. `PG_FARM.md` should be corrected — a future
    reader following it would build a token-refresh cron on Azure that is not needed.

---

## 8. Reference — the FRAN files this plan is built on

Quoted so Brett can verify none of this was invented:

- `~/Documents/claude/corpus_browser/docs/AZURE_DEPLOY_STATUS.md` — the live cutover log, resource
  names, tenant/subscription ids, the Cloudflare DNS records, the TLS thumbprint
- `~/Documents/claude/corpus_browser/docs/AZURE_HOSTING_PLAN.md` — FRAN's own design doc, the
  approvals path (AggieCloud, IET review, ServiceHub IAM), the CAS fallback, and the cost estimate
- `~/Documents/claude/corpus_browser/deploy/azure_setup.sh` — the provisioning commands **(note: this
  describes an ACR/container path that was NOT the one actually deployed)**
- `~/Documents/claude/corpus_browser/app/auth.py` — the `X-MS-CLIENT-PRINCIPAL` decoder and
  fail-closed group/allowlist check to port into STAN
- `~/Documents/claude/corpus_browser/app/db.py:170-250` — the `_mint_jwt` service-account flow and the
  read-only session enforcement worth copying

STAN-side:

- `stan/dashboard/server.py:29` (`app`), `:31-42` (the auth-incident comment), `:43-71` (CORS),
  `:73-96` (Origin middleware), `:145-211` (PG refresh loop + startup), `:216-235` (`/api/capabilities`),
  `:1618` (`/api/fleet/command`)
- `stan/db.py:413-424` (`get_db_path` / `STAN_DB_PATH`), `stan/db_pg.py:34-44` (PG defaults),
  `:85-151` (credential resolution and JWT minting)
- `stan/config.py:320` (`get_hive_mirror_root`), `stan/control.py:1999-2054` (the command whitelist)
- `docs/PG_FARM.md`, `docs/PG_FARM_ACCESS.md`
