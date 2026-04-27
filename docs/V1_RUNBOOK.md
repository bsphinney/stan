# STAN v1.0 cutover — wipe + repopulate community

The fastest path to flip the public community dataset from
"mixed v0.2.x metadata" to "consistent v1.0 metadata". Run on
each instrument PC after pulling v0.2.236+. Estimated total
clock time per instrument: 30–90 min depending on how many
runs need re-search.

---

## Decision tree per run

```
   ┌───────────────────────────────┐
   │ runs.diann_version is NULL    │  ← treat as v1.8.x (legacy)
   │   or starts with "1.8"        │
   └─────────────┬─────────────────┘
                 │
       ┌─────────┴─────────┐
       │                   │
       ▼                   ▼
   re-search            keep existing
   with 2.3.0           report.parquet
       │                   │
       └─────────┬─────────┘
                 ▼
         backfill-metrics --force
         (stamps stan_version + columns)
                 ▼
         submit-all --force
         (pushes the v1.0 row to relay)
```

The cheap path (no re-search): only DIA-NN ≥2.3 runs land in the
v1.0 community dataset. Older runs stay local. **For first cutover
this is recommended** — re-searching hundreds of runs takes hours.
Brett can decide later whether the historical 1.8 corpus is worth
re-searching.

---

## Pre-flight (per instrument)

After pulling v0.2.236+:

```bash
# 1. Confirm version
stan --version          # must be 0.2.236 or higher

# 2. Audit current state
stan list-stale --before 1.0.0     # how many rows aren't v1.0-stamped yet
stan test --extract --n 5          # do all metric fields populate?
```

Goal: green across the board on `stan test --extract`. Reds on
column_vendor / column_model / lc_system / diann_version mean the
backfill below will fix them; reds on tic / drift / cIRT mean
something in the pipeline is broken and must be fixed first.

---

## Step 1 — Re-extract everything (per instrument)

```bash
stan derive-cirt-panel --auto --force-auto    # fresh panels w/ v0.2.224 logic
stan backfill-metrics --force                 # stamps stan_version + columns
stan backfill-tic --force --push              # mean-per-bin TICs
stan backfill-cirt                            # uses fresh panels
stan backfill-window-drift --force            # Bruker only
```

Each step is idempotent. After this, `stan list-stale --before
1.0.0` should report **zero stale rows** with `diann_version >= 2.3`.

---

## Step 2 — (Optional) Re-search old DIA-NN 1.8 runs

If you want the older corpus included in v1.0 community:

```bash
# Identify what would need re-searching
sqlite3 ~/STAN/stan.db "SELECT COUNT(*) FROM runs WHERE diann_version LIKE '1.8%' OR diann_version IS NULL"
```

For each such run, the simplest path is:

```bash
# Move the old report aside, then trigger a fresh DIA-NN search
mv ~/STAN/baseline_output/<stem>/report.parquet ~/STAN/baseline_output/<stem>/report.18.parquet
stan test --extract --n 5    # auto-runs DIA-NN when report missing
```

This is slow (~10 min per Bruker run, ~3 min per Thermo). Best
for a small number of important historical runs. For bulk
re-searches, use `stan baseline` against the raw directory and
let it run overnight.

---

## Step 3 — Server-side wipe

Brett does this manually on the HF Space:

1. Go to <https://huggingface.co/datasets/brettsp/stan-benchmark/tree/main/submissions>
2. Delete every `*.parquet` (keep `.gitkeep`)
3. Delete `cohort_stats/*.parquet`
4. Wait for the relay's `/api/leaderboard` to return `count: 0`

This is destructive and irreversible — only do once metadata
is verified clean across all instruments.

---

## Step 4 — Mark all local rows as un-submitted

After the server wipe, the local `submitted_to_benchmark` flag is
out of sync. Two options:

```bash
# Option A: explicit reset (safest)
sqlite3 ~/STAN/stan.db "UPDATE runs SET submitted_to_benchmark = 0"

# Option B: --force flag on submit-all (skips the check entirely)
stan submit-all --force
```

Option B is the v0.2.236+ path — `--force` ignores the local
submitted flag, so even un-reset rows get pushed.

---

## Step 5 — Repopulate (per instrument)

```bash
stan submit-all --force        # pushes every valid QC row
```

The relay validates each submission against the v1.0 schema
(DIA-NN 2.3 required, all metadata fields populated). Rejected
rows print their reason — fix and retry, or accept the
exclusion if they can't satisfy v1.0.

Watch the JSONL log:

```bash
tail -f ~/STAN/logs/submit_all_$(date +%Y%m%d).jsonl
```

---

## Step 6 — Final audit

After all instruments have repopulated:

```bash
# Local
stan list-stale --before 1.0.0     # → 0 stale rows expected
stan test --n 5                    # all 31 fields green

# Remote (run on dev box)
curl -s https://brettsp-stan.hf.space/api/leaderboard | jq '.count'
# → expect ~total runs across all instruments
```

If everything checks out, tag the release:

```bash
git tag v1.0.0
git push --tags
```

---

## What's STILL on the v1.0 punchlist

- TIMS QC ingest blackout (search dispatch broken since 2026-04-17).
  README:625. Needs investigation BEFORE TIMS data lands in v1.0.
- runs.instrument normalization — partially fixed by v0.2.231-233's
  auto-merge; verify on TIMS after upgrade.
- Updater cascade bug (3 parallel backfill windows) — README:632,
  not blocking v1.0 but bad UX.

These can ship as v1.0.1 if not blockers; the 7 fields v1.0 needs
populated are now all working per `stan test --extract`.
