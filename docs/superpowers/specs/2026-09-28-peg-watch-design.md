# PEG Watch: Evosep PEG tab + community PEG leaderboard

Status: approved design (mockup approved by Brett 2026-09-28); built as
v1.2.0. Numbers and rules changed in review are corrected in place and
listed under "Changes during implementation" at the end. Operating
reference: `docs/PEG_WATCH.md`.
Mockup: https://claude.ai/artifact/1BFYbYQQPYhEUfgcufzP57 (private)
Mockup source (design reference for the UI): see "Design reference" below.
Branch: `feat/peg-watch` (worktree `/Users/brettphinney/Documents/STAN-peg`, from `origin/main` @ v1.1.12)
Target version: **1.2.0**

## 1. Goal

Labs running an Evosep One report PEG contamination. STAN already scores
PEG on every Bruker QC run (`stan/metrics/peg.py`), but the number never
leaves the lab and the dashboard only shows it as a badge and one
sparkline. This feature:

1. Adds a **PEG tab** to the STAN dashboard that shows a lab's own PEG
   history: timeline with auto-detected episodes, daily calendar, ladder
   fingerprint, PEG by column period, precursor cost, and the one-night
   isolation protocol.
2. Adds a **community PEG leaderboard** so Evosep labs can compare PEG over
   time. Labs share per-run PEG through a dedicated relay channel, and
   the relay serves a small pre-aggregated leaderboard.
3. Compares **Evosep vs non-Evosep LC** PEG, for the lab's own instruments
   and across the community (added by Brett 2026-09-28, after the mockup).
4. Computes PEG for Thermo `.raw` QC runs on Hive (through the `trfp.sif`
   container, since `fisher_py` is not in the Hive venv), so UC Davis's
   Orbitraps (custom LC) give the non-Evosep side of the comparison.
5. Seeds the board with UC Davis's ~1,385 timsTOF HT + Evosep QC
   acquisitions (Jul 2023 – present; 1,674 rows before duplicate ingests
   and failed acquisitions were removed) and, after the Thermo backfill,
   ~2,360 Orbitrap acquisitions (2,924 rows).

## 2. Decisions (approved)

| # | Decision | Why |
|---|---|---|
| D1 | **Dedicated PEG channel** on the HF relay (`/api/peg/*`, store under `peg/` in the dataset), not new columns on the benchmark submission. | PEG is read from raw MS1 and needs no search, so any Evosep lab can join without the frozen DIA-NN 2.3 community search. A whole lab's history backfills in one commit (the benchmark path would need one `/api/update` commit per row against HF's 256 commits/h). The frozen benchmark schema is untouched. |
| D2 | **Rank on `peg_intensity_pct`** ("PEG share of MS1"), median per lab per window, within a cohort = `instrument_family × Evosep SPD method`. The 0–100 `peg_score` and `peg_class` stay as badges. | The score saturates (29 % of UC Davis runs are exactly 0, 95 are exactly 100). Percent-of-signal is the published unit (HowDirty 2023, mzsniffer). The absolute 1e4 intensity floor makes cross-vendor numbers incomparable, hence cohorts. |
| D3 | **PEG endpoints verify lab identity**: a claimed pseudonym must present its token. Unclaimed names are accepted but marked unverified. Names are HTML-escaped everywhere they render. The public claims file stops publishing bare email hashes (HMAC with a server-side pepper). | A public ranking by lab name invites spoofing; today anyone can submit as any name. |

| D4 | **Evosep vs other LC is one extra panel; the approved design is otherwise unchanged** (Brett 2026-09-28). The share channel accepts every LC (`lc_system` ∈ {`evosep`, `other`}, optional `lc_model`) so the panel has data, but the **leaderboard stays Evosep-only**. The panel compares LC groups *within an instrument family*; cross-family views carry an explicit "different instruments" caveat. | PEG share depends on the detector (absolute 1e4 floor), so timsTOF-vs-Orbitrap is not a fair LC comparison; timsTOF+Evosep vs timsTOF+nanoElute is. UC Davis has Evosep only on the timsTOF, so its own comparison is cross-family and is labelled so. |

Out of scope (documented as follow-ups, not built): PEG on blanks /
`sample_health` (monitor path is a no-op), persisting `ladder_coherence`
and `lc_model` in the DB (needs owner DDL), the benchmark's own
`/api/submit` and `/api/update` auth holes.

## 3. Components and file ownership

| Component | Files | Owner (build step) |
|---|---|---|
| PEG trend maths (pure) | `stan/metrics/peg_trends.py` (new) | backend |
| DB readers | `stan/db.py`, `stan/db_pg.py` | backend |
| Dashboard API | `stan/dashboard/server.py` | backend |
| PEG share client | `stan/community/peg_submit.py` (new), `stan/cli.py`, `scripts/cron_community_sync.sh` | client |
| Relay API | `hf_space/app.py` (vendored, new dir), `hf_space/Dockerfile`, `hf_space/README.md`, `scripts/deploy_hf_space.py` (new) | relay |
| Relay public page | `hf_space/app.py` (INDEX_HTML section) | relay-ui (after relay) |
| Dashboard UI | `stan/dashboard/public/index.html`, `scripts/render_check.js` | ui |
| Thermo PEG on Hive | `stan/metrics/peg_io.py`, `stan/metrics/peg_trfp.py` (new), `scripts/peg_backfill_thermo.py` + `.sbatch` (new) | thermo |
| Docs + versions | README, docs/*, STAN_MASTER_SPEC, CHANGELOG, `stan/metrics/peg*.py` docstrings, version files | docs (last) |

## 4. Contracts

### 4.1 Real-PEG filter (everywhere)

A run has a real PEG measurement iff `peg_score IS NOT NULL AND
peg_intensity_pct IS NOT NULL AND COALESCE(peg_class,'') IN
('clean','trace','moderate','heavy')`. `'unknown'` is the failure
sentinel (`peg_score=0.0`) and **must never be treated as clean** or be
shared. Unmeasured runs are dropped, never sent as 0.

QC-only: `hidden = 0` (integer compare; PG `runs.hidden` is integer),
`run_date > '2015-01-01'` (there is a bogus 1980 row), and run names
matching blank/wash/blk are excluded (reuse the filter `submit-all` uses).

Failed acquisitions (added in review): a row with `n_precursors = 0` AND
`peg_n_ions_detected = 0` AND `peg_intensity_pct = 0` is an empty read that
`detect_peg_in_spectra` scored as a clean 0.0, not a measurement, and is
excluded (SQL on both backends plus `peg_trends.is_failed_acquisition`).
The writers now leave PEG NULL when no MS1 signal was read.

One acquisition, one row (added in review): an acquisition is
`(instrument, basename(run_name), run_date to the UTC second)` — the
fields `run_key` hashes. Of duplicate copies, keep the one with stored
ion hits, then the newest `stan_version` compared numerically, then the
highest `id`. Every reader applies it (PG `DISTINCT ON`, SQLite
`peg_trends.pick_canonical`), and the share client ranks copies by the
same keys, so the tab and the board count the same copy.

PG gotchas: `runs.run_date` is `timestamptz` (format with
`to_char(run_date AT TIME ZONE 'UTC', ...)`), `peg_score` is `real`.
SQLite stores TEXT with mixed offsets; normalise to UTC in Python.

### 4.2 Dashboard API: `GET /api/peg/overview`

Query: `instrument` (optional; default = the instrument with the most real
PEG runs). Public GET (no run or sample names in the payload). Server-side
TTL cache 10 min per instrument (egress: PG Farm bills reads; this runs
only when the tab is open and pulls ~1.7k rows × 7 scalars).

```jsonc
{
  "as_of": "2026-09-28",                      // UTC date of the request
  "instrument": "timsTOF HT",
  "instruments": [{"instrument": "timsTOF HT", "n_runs": 1385, "evosep": true}],
  "lc_system": "evosep",                      // most common lc_system for this instrument's PEG runs
  "instrument_family": "timsTOF",             // via stan.community.submit._instrument_family
  "runs_cols": ["t","spd","pct","score","ions","cls","prec"],
  "runs": [["2023-07-15T10:51", 100, 0.011, 8.1, 3, 0, 1530], ...],   // t = UTC "YYYY-MM-DDTHH:MM", cls 0..3 = clean..heavy, sorted by t
  "rolling_start": "2023-07-13",              // first day of the daily series
  "rolling": {"all": [null, 0.12, ...], "100": [...], "60": [...], "30": [...]},  // trailing 14-day median pct per day through as_of; null when <5 runs in window; keys: "all" + each SPD with ≥20 runs
  "episodes": [{"start":"2025-12-15","end":"2026-04-07","days":113,"n":169,"median_pct":5.3,"heavy_pct":41,"ongoing":false}],
  "baseline": {"median_pct": 0.0, "start": "2024-05-14", "end": "2024-08-11", "n": 44},   // lowest trailing-90-day median with ≥20 runs; ties -> lowest upper quartile, then most runs, then newest; null if <20 runs total
  "summary": {"n_30d": 71, "median_30d": 2.065, "median_prev_30d": 7.741, "change_pct": -73, "clean_30d": 19, "heavy_30d": 13, "clean_rate_30d": 27, "streak_clean": 0},   // change_pct null when the previous median < 0.1 %
  "ladder": {"months": ["2025-01", ...], "n": [2, ..., 20], "share": [[...per month...] per n], "nruns": [...], "adducts": {"+H": 2413, "+NH4": 4736, "+Na": 887}},
  "column_periods": [{"installed": "2025-12-23", "retired": "2026-03-12", "column_model": null, "n_qc": 106, "median_pct": 5.65, "heavy_pct": 45, "clean_pct": 13}],   // from maintenance_events event_type='column_change' for this instrument, newest last; [] if none
  "impact": {"60": {"clean": [360, 41582], "trace": [...], "moderate": [...], "heavy": [112, 34730]}, "100": {...}},  // [n, median n_precursors] per class for DIA runs with >0 precursors, SPDs with ≥10 clean and ≥10 heavy runs
  "lab_lc": [{"instrument": "timsTOF HT", "family": "timsTOF", "lc_system": "evosep", "n_90d": 212, "median_90d": 3.1, "clean_rate_90d": 24, "n_365d": 802, "median_365d": 2.2, "weekly": [/*26*/]},
             {"instrument": "Orbitrap Exploris 480", "family": "Orbitrap", "lc_system": "custom", ...}],   // every instrument with real PEG, from get_peg_lab_lc_summary
  "sharing": {"enabled": true, "source": "config", "display_name": "Clogged PeakTail", "relay_url": "https://brettsp-stan.hf.space"}
  // THIS host's setting, not the lab's. source: "config" (peg_share true in community.yml) | "env" (STAN_PEG_SHARE=1)
  // | "opted_out" (peg_share or STAN_PEG_SHARE set to anything else: a decision) | "off" (no setting here, e.g. the hosted
  // dashboard). With "off" the tab reports the lab's name on a relay board as a past fact ("runs from the last N days are
  // on the board"); with "opted_out" the board never overrides it -- the relay keeps pre-opt-out runs for the whole window.
}
```

Maths (pure, unit-tested, in `stan/metrics/peg_trends.py`):
- `rolling_median_daily(times, pcts, start, end, window_days=14, min_runs=5)`.
- `detect_episodes(daily, start, runs, threshold_pct=3.0, gap_days=21, min_days=14)`:
  a day is "hot" when its trailing-14-day median ≥ threshold; hot days
  closer than `gap_days` merge; each episode's start is trimmed back to
  the first run ≥ threshold inside the first window; episodes shorter
  than `min_days` are dropped; `ongoing` when the last hot day is within
  3 days of `as_of`. On UC Davis data, one row per acquisition, this
  yields Dec 15 2025 → Apr 7 2026 (113 d, 169 runs, median 5.30 %) and
  May 21 → Sep 18 2026 (120 d, 236 runs, median 5.31 %).
- `best_baseline(runs, window_days=90, min_runs=20)`. Ties on the median
  (UC Davis: 425 of 969 windows have a median of exactly 0) go to the
  lowest upper quartile, then the most runs, then the newest window.
- `summary_30d`: `change_pct` is null when the previous median is below
  0.1 % (the relay's `PEG_CHANGE_FLOOR_PCT`; a test pins the two equal).
- `ladder_by_month(hits_rows, runs_per_month)`: share = max over adducts of
  (#runs with that oligomer+adduct) / (#real-PEG runs that month whose
  ladder is known: hits stored, or no PEG ion detected).
- `column_periods(events, runs)`, `impact_by_class(runs)`.
- Month bucketing for the ladder uses UTC.

### 4.3 DB readers (backend-aware, `use_pg()` branch)

- `get_peg_runs(instrument: str | None) -> list[dict]` with keys
  `run_date_utc, spd, peg_score, peg_intensity_pct, peg_n_ions_detected,
  peg_class, n_precursors, mode, lc_system, instrument` (real-PEG + QC filter
  of §4.1, no run names returned).
- `get_peg_instruments() -> list[dict]` (instrument, n_runs, evosep).
- `get_peg_ladder_month_counts(instrument) -> list[(month, repeat_n, adduct, n_runs)]`
  (`peg_ion_hits` join `runs`, `source='runs'`, aggregated in SQL).
- `get_column_change_events(instrument) -> list[(event_date, column_model)]`.
- `get_peg_lab_lc_summary(as_of) -> list[dict]`: one row per instrument with
  real PEG: `instrument, lc_system, n_90d, median_90d, clean_rate_90d,
  n_365d, median_365d` and 26 weekly medians (oldest first). Aggregated in
  SQL (`percentile_cont` on PG) so the payload stays tiny.
- `get_peg_share_rows() -> list[dict]` for the share client: QC runs of
  **every LC** with real PEG, including `lc_system`, `run_name` (used **only** to derive `run_key`,
  never sent), `instrument, run_date, spd, mode, amount_ng, sample_type`
  (if column exists) and the four PEG fields.

All PG queries are targeted SELECTs with explicit column lists; no
`SELECT *`, no array columns.

### 4.4 Share record (client → relay)

```jsonc
POST /api/peg/submit      header X-STAN-Auth: <token from community.yml auth_token>
{
  "display_name": "Clogged PeakTail",
  "stan_version": "1.2.0",
  "records": [{
    "run_key": "9f2c...",            // sha256(f"{instrument_model}|{basename(run_name)}|{run_date_utc_iso}")[:24] — no name leaves the lab
    "run_date": "2026-09-25T18:27:00Z",
    "instrument_family": "timsTOF",
    "instrument_model": "timsTOF HT",
    "lc_system": "evosep",           // "evosep" | "other" (runs.lc_system 'custom' → "other"); runs with NULL/'' lc_system are not shared
    "lc_model": null,                // optional free text, e.g. "Vanquish Neo", when known (not stored in the DB today)
    "spd": 100,
    "acquisition_mode": "diapasef",
    "sample_type": "hela",
    "amount_ng": 50.0,
    "peg_intensity_pct": 0.353,
    "peg_score": 7.5,
    "peg_n_ions_detected": 2,
    "peg_class": "clean",
    "peg_method": "stan-peg-1"       // 60-ion PEG1-20 × H/NH4/Na panel, 5 ppm, 1e4 floor, 80 MS1 scans
  }]
}
```
Batches of ≤ 2000 records. Response:
`{"status":"ok","display_name":..., "verified": bool, "accepted": n, "unchanged": n, "rejected": [{"index": i, "reason": "..."}]}`.

The client is **stateless and idempotent**: every sync resends all shareable
runs; the relay skips records identical to what it already holds and makes
no commit when nothing changed. No PG DDL is required.

All LCs are shared (evosep and other). Opt-in: `peg_share: true` in `~/.stan/community.yml`, or env
`STAN_PEG_SHARE=1`. Off by default. `display_name` from community.yml /
`STAN_DISPLAY_NAME`; refuses "Anonymous Lab" / empty.

CLI: `stan peg-sync [--backend pg|sqlite] [--dry-run] [--relay URL]`
writes `<user config dir>/logs/peg_sync_<UTC timestamp>.jsonl` —
`~/.stan/logs` on Linux and macOS (on Hive `/home/brettsp/.stan/logs`,
lab-readable, as every other CLI log there), `~/STAN/logs` on Windows —
(one line per batch +
summary; errors at `logger.warning`+). Exit 0 when sharing is off
(logs why) or on success; non-zero if every batch failed.
`stan community-claim` re-verifies the lab's pseudonym by email code
(reusing the `stan setup` claim flow) and writes the new `auth_token`.
`scripts/cron_community_sync.sh` runs `stan peg-sync --backend pg` after
`submit-all`.

### 4.5 Relay (HF Space `brettsp/stan`)

The Space's `app.py` is vendored into `hf_space/` (base: Space commit
`d041ef68`, downloaded 2026-09-28). `scripts/deploy_hf_space.py` refuses to
upload unless the Space's live `app.py` still equals the recorded base
(sha256), so edits made directly in the Space are never clobbered.

Identity (`/api/peg/submit` only):
- `display_name` required, 1–60 chars after stripping control characters;
  "Anonymous Lab" → 400.
- If the name is in `identity/claims.json`: `X-STAN-Auth` must hash
  (`_hash(token)`) to its `token_hash` → `verified=true`; otherwise 403
  ("This lab name is claimed. Run `stan community-claim` to get a token.").
- Unclaimed name → accepted, `verified=false`.
- Per-IP rate limit (30 requests / hour). Max 2000 records / request.

Validation per record: `lc_system` ∈ {"evosep", "other"}; `peg_class` in the four
classes; `0 ≤ peg_intensity_pct ≤ 100`; `0 ≤ peg_score ≤ 100`;
`0 ≤ peg_n_ions_detected ≤ 500`; `spd` 1–2000; `run_date` parses and is not
in the future (+1 day slack); `run_key` 24 hex chars. Bad records are
listed in `rejected`, good ones still accepted.

Storage:
- `peg/peg_latest.parquet`: the whole table, one row per
  `(display_name, run_key)` (newest wins), columns = record fields +
  `display_name, verified, submitted_at, first_seen_at`. Loaded at startup
  (and lazily), held in memory, rewritten on every accepted change.
- `peg/submissions/<YYYYMMDDTHHMMSSZ>_<uuid8>.parquet`: the changed records
  of each accepted batch (audit log).
- Both go through the existing batched commit worker (generalised to take
  a path per queued file). If the Space restarts before a flush, the next
  6-hourly client sync resends everything, so nothing is lost for good.

`GET /api/peg/leaderboard?family=timsTOF&spd=100&window=30` (window ∈
{30, 90, 365}; defaults timsTOF/100/30). Anchored at the relay's current
UTC date. 5-minute cache.
```jsonc
{
  "generated_at": "...Z", "as_of": "2026-09-28", "window_days": 30,
  "family": "timsTOF", "spd": 100,
  "cohorts": [{"family": "timsTOF", "spd": 100, "n_labs": 1, "n_runs_365d": 847}],
  "ranked": [{"rank": 1, "display_name": "Clogged PeakTail", "verified": true,
              "instrument_models": ["timsTOF HT"], "n_runs": 38,
              "median_pct": 2.1, "clean_pct": 28, "heavy_pct": 18,
              "change_pct": -71,            // vs previous window; null if <5 runs there or its median < 0.1 %
              "weekly": [0.4, null, ...],   // 12 weekly medians, oldest first
              "badges": ["cleanest", "most_improved"]}],
  "unranked": [{"display_name": "...", "verified": false, "n_runs": 3}],
  "community": {"n_labs": 1, "n_runs": 38, "p25_pct": 0.3, "median_pct": 2.1, "p75_pct": 7.4}
}
```
Rules: ranked needs ≥ 5 runs in window; order by median_pct asc, then
clean_pct desc, then n_runs desc. `cleanest` = rank 1 when ≥ 2 ranked labs.
`most_improved` = most negative `change_pct ≤ -15` among ranked labs that
also fell by at least 0.5 percentage points (previous median − median),
and only when ≥ 2 labs are ranked.
`change_pct` is null when the previous window had < 5 runs or a median
below 0.1 % (relative change from next to nothing is noise). Once a name
has any verified row, its unverified rows are left out of every aggregate.

The leaderboard only ever ranks `lc_system == "evosep"` records, at an
Evosep method SPD (500/300/200/100/60/40/30/15). Relay size caps (added in
review): 20,000 rows per lab name (updates still land), 200 unclaimed
names, 200,000 unclaimed rows in total.

`GET /api/peg/lc-compare?family=timsTOF&window=90` →
```jsonc
{"family": "timsTOF", "window_days": 90, "as_of": "...",
 "groups": [{"lc": "evosep", "n_labs": 1, "n_runs": 212, "p25_pct": .., "median_pct": .., "p75_pct": ..,
             "clean_pct": .., "heavy_pct": .., "weekly": [26 weekly medians]},
            {"lc": "other", ...}],
 "families": [{"family": "timsTOF", "evosep_runs": 847, "other_runs": 0, "evosep_labs": 1, "other_labs": 0}]}
```
A group with 0 runs is returned with `n_runs: 0` and nulls.

`GET /api/peg/trend?family=&spd=&weeks=52` →
`{"weeks": [{"week_start": "2026-06-01", "n_labs": 1, "n_runs": 12, "p25": .., "p50": .., "p75": ..}]}`.

Claims privacy: with Space secret `CLAIMS_PEPPER` set, stored
`email_hash` values become `hmac_sha256(pepper, sha256(email)[:32])[:32]`
(existing entries migrated in place on first load, marked `"v": 2`); the
claim flow compares in the same form. Without the secret, behaviour is
unchanged. (Old values remain in the dataset's git history; a history
squash is a separate decision for Brett.)

Claim limits are keyed by the caller, never by the lab name alone. A
budget that anyone can spend on a name lets a stranger stop the owner
from re-claiming it, and re-claiming is how a lab rotates its token.
`/api/claim-name` allows 10 calls per caller address per hour, refused
calls included, which throttles the "different email" 409. It sends at
most 3 codes per (name, email) per hour. Its answer carries a
`claim_id`, which `stan setup` / `stan community-claim` echo to
`/api/verify-claim`. After 5 wrong codes sent with that claim_id, the
code is discarded. A verify without a claim_id (STAN before this change)
still works, but it never spends the code. Such guesses are capped at 5
per caller per name and 20 per name per hour. That per-name ceiling
can block only claim_id-less verification, never a caller that holds
the claim_id.

Public page: a new "Evosep PEG Watch" section, plus one "Evosep vs other
LC" panel (per family: percentile bars and weekly medians per LC group,
same data as `/api/peg/lc-compare`), (header link + anchor
`#peg`) with cohort and window toggles, the ranked table (escaped names,
verified check, unverified tag, badges, 12-week sparkline), a Plotly
weekly community band, an empty state that says how to join
(`peg_share: true` + `stan peg-sync`), and a link to the diagnostic
protocol. `SPACE_VERSION` → `1.2.0`.

### 4.6 Dashboard PEG tab (`stan/dashboard/public/index.html`)

New `['peg', 'PEG']` tab after Trends, `?tab=peg` deep link. React port of
the mockup, fed by `/api/peg/overview` (own data) and the relay's
`/api/peg/leaderboard` + `/api/peg/trend` (fetched from the browser; CORS
is `*`; the section degrades to a "community board unavailable" note if the
relay is unreachable). Sections: hero headline + lede generated from
`summary`/`baseline`, "PEG ladder this month" stick spectrum, 5 tiles,
timeline (SPD + range toggles, episodes, 14-day median, column-change
markers, best-90-day baseline, hover tooltip), daily calendar since the
start of the previous year (scrolled to newest), community leaderboard
(cohort + window toggles, your row highlighted), precursor-cost dot plot,
ladder fingerprint heatmap + adduct mix, PEG by column period, the
one-night isolation steps (link to `docs/PEG_EVOSEP_DIAGNOSTIC.md` on
GitHub), what-is-shared card with the current sharing status (read-only:
tells the user how to switch it), and "how the ranking works". **One extra panel**, "Evosep vs other LC",
placed directly after the leaderboard (the only addition to the approved
mockup): your instruments from `lab_lc`, each with an LC chip, 90-day
median, clean rate and 26-week sparkline, with a "different instrument
families, not a like-for-like LC comparison" note when the Evosep and
non-Evosep instruments differ in family; plus the community within-family
comparison from `/api/peg/lc-compare` (family toggle), with an empty state
until a family has both LC groups. Community
weekly band is drawn on the timeline only when the cohort has ≥ 3 labs.
Empty state when the instrument has no real PEG runs (explains
`stan install-peg-deps` / `fisher_py`). All hooks above early returns.
Class colours: clean `--pass`, trace `--warn`, moderate orange, heavy
`--fail`. Names from the relay are rendered as React text (auto-escaped),
never `dangerouslySetInnerHTML`.

Bug fixes shipped with the tab:
- `PegBadge`: `peg_class === 'unknown'` renders grey "n/a", not green 0.0.
- `Sparkline` for `peg_score`: exact zeros are real clean runs; include
  them in range/median.
- QC History: PEG column sorts by `peg_score`, not by class string.
- Samples tab: `PegBadge` gets `source="sample_health"`.

### 4.7 Thermo PEG on Hive (trfp fallback)

`read_ms1_thermo` keeps `fisher_py` as the first choice. When it is not
importable (the Hive venv) and a ThermoRawFileParser container is
available (`STAN_TRFP_SIF`, default
`/quobyte/proteomics-grp/STAN/historical_bsa/trfp.sif`, plus `apptainer`
on PATH or loadable), it converts the `.raw` to an MS1-only mzML in a
temporary directory (`$TMPDIR`, removed afterwards), stride-samples the
same `N_SCANS_DEFAULT` MS1 spectra in acquisition order, and yields
`(mz, intensity)` lists, so `detect_peg_in_spectra` is unchanged. The
mzML parser lives in `stan/metrics/peg_trfp.py` (streaming `iterparse`,
base64 + zlib, 32/64-bit floats; no new dependency). If neither path
works it raises `PegReaderUnavailable` as today (PEG stays NULL).
Container flags and TRFP options must be checked against the TRFP
version inside the `.sif` (`ThermoRawFileParser --help` in a SLURM job),
never assumed. Conversion runs only inside SLURM jobs (never the login
node): the inline Hive QC path already runs in the search job.

Backfill: `scripts/peg_backfill_thermo.py` selects Thermo QC `runs` with
NULL PEG (targeted PG query: id, raw_path), processes a shard
(`--shard i --nshards n`), writes through `stan.db.update_peg_result` (+
ion hits), logs to `/quobyte/proteomics-grp/STAN/logs/peg_backfill_thermo_<ts>.jsonl`.
`scripts/peg_backfill_thermo.sbatch` is a SLURM array on partition `low`
(`publicgrp` / `publicgrp-low-qos`, `--requeue`), `--output` on Quobyte.
Running it is a deploy step that needs Brett's go-ahead.

## 5. Tests

- `tests/test_peg_trends.py`: rolling median, episode detection (incl. a
  synthetic reproduction of the two UC Davis episodes), baseline, ladder
  shares, column periods, impact, unknown/NULL exclusion.
- `tests/test_peg_overview_endpoint.py`: `/api/peg/overview` on a temp
  SQLite DB; PG branch via monkeypatched `use_pg` + fake readers; no run
  names in the payload.
- `tests/test_peg_submit.py`: record building (drops unknown/NULL/non-evosep/
  blank runs, stable `run_key`, never sends `run_name`), batching, opt-in
  off → no POST, auth header sent, log file written.
- `tests/test_relay_peg.py`: vendored relay via `TestClient` with HF
  downloads/commits mocked: claimed/unclaimed/wrong-token auth, validation
  rejects, dedup/unchanged, no commit when unchanged, leaderboard ranking,
  badges, min-runs, window change, trend bands, pepper migration.
- `tests/test_peg_trfp.py`: mzML parser on a tiny synthetic mzML (32/64-bit,
  zlib and uncompressed), stride sampling, fallback order (fisher_py →
  trfp → unavailable), temp dir cleanup, command construction.
- `node scripts/check_jsx.js stan/dashboard/public/index.html` and
  `scripts/render_check.js` include `PegTab`.
- Existing suite stays green; `ruff check stan/`.

## 6. Deploy (each step needs Brett's go-ahead)

Commands for every step: `docs/PEG_WATCH.md` → "Runbooks".

1. Merge `feat/peg-watch` to `main` and push.
2. HF Space: `python scripts/deploy_hf_space.py` (dry run: live vs
   recorded-base sha256 and a diff stat; refuses if the Space was edited
   directly), then `python scripts/deploy_hf_space.py --yes --record-base`
   (uploads `app.py` only; refuses in the half hour around the 00/06/12/18
   Pacific syncs; waits for `/api/version` = 1.2.0; rewrites the recorded
   base — commit that). Then set Space secret `CLAIMS_PEPPER` and keep a
   copy: never remove or rotate it once set. Check `/api/peg/leaderboard`
   returns an empty board, and check the rate-limit key in the Space log.
3. Re-claim "Clogged PeakTail" (`stan community-claim`, email code to
   Brett), copy the new `auth_token` into `~/.stan/community.yml` on the Mac
   and on Hive; set `peg_share: true` on Hive.
4. Hive: `git pull` in `/quobyte/proteomics-grp/brett/stan`; copy
   `scripts/cron_community_sync.sh` to `/quobyte/proteomics-grp/STAN/`; run
   `stan peg-sync --backend pg` once and confirm about 1,385 accepted,
   verified (one record per acquisition, failed acquisitions dropped).
5. Azure: zip deploy per `docs/AZURE_DEPLOY.md` (after `check_jsx`), then
   `az webapp config appsettings set -g rg-fran -n stan-ucd-proteomics
   --settings STAN_PEG_SHARE=1`, so the hosted PEG tab says the lab shares
   (it shares from Hive; the hosted server never syncs).
6. Thermo backfill: one test job on 5 runs, check PEG lands in PG, then the
   full `peg_backfill_thermo.sbatch` array; then `stan peg-sync --backend pg`.

## Changes during implementation

Recorded 2026-09-28, after review round 1. The sections above are
corrected in place; this is the list of what moved and why.

- **One acquisition, one row** (§4.1, §4.3). Live PG held 1,674 timsTOF
  rows for 1,404 acquisitions (241 duplicate groups, 270 extra rows) from
  instrument-PC plus Hive double ingest and `/quobyte` vs `/nfs` pairs,
  and the copies disagreed on PEG in 168 groups. Rule: keep stored hits,
  then the newest `stan_version` (numeric), then the highest `id`, in
  every reader and in the share client. In 43 groups only the id decides;
  deleting the duplicates in PG is a separate data fix.
- **Failed acquisitions excluded** (§4.1): 0 precursors + 0 ions + 0 %
  (19 acquisitions); writers store NULL for an empty read.
- **Counts**: ~1,385 timsTOF acquisitions, not 1,682; ~2,360 Orbitrap
  acquisitions (2,924 rows), not ~2,900; deploy step 4 expects ~1,385.
- **Episode 1** ends 2026-04-07 (113 d, median 5.30 %), not 2026-04-26
  (132 d, 4.25 %): the extra copies had stretched it.
- **Ladder denominator** counts only runs whose ladder is known.
- **`change_pct`** is null below a 0.1 % previous median, on the relay and
  the tab alike; **`most_improved`** also needs a 0.5-point fall.
- **Best baseline tie-break** (lowest upper quartile, most runs, newest)
  picks 2024-05-14 → 2024-08-11 on UC Davis data, not the mockup's 2025
  window. Deliberate and tested; wants Brett's sign-off.
- **`sharing.source`** (§4.2) tells the tab whether this host opted in by
  config, by env, opted out, or has no setting.
- **Azure `STAN_PEG_SHARE=1`** (§6 step 5): the hosted dashboard never
  syncs, so without it the PEG tab could not say the lab shares.
- **Relay hardening**: canonical lab names (`_clean_text` on claims and
  submits), claim limits keyed by caller with a `claim_id`, size caps on
  the PEG store, `force_download` for `claims.json`, and the deploy
  script's sync-window guard evaluated in America/Los_Angeles.
- **Thermo**: an empty TRFP read, or a non-TRFP error on that path,
  becomes `PegReaderUnavailable` (PEG NULL), never a clean 0.0 or the
  `'unknown'` sentinel.
- **peg-sync logs** land in `~/.stan/logs` (the convention every CLI log
  follows), not `~/STAN/logs`, except on Windows.

## Design reference

Mockup HTML (vanilla JS, same visual language as STAN):
`/private/tmp/claude-501/-Users-brettphinney-Documents-STAN/39187c0f-28e0-4ce0-892b-286c8db04795/scratchpad/mockup/peg_watch.template.html`
(data at `.../scratchpad/mockup/peg_data.js`, built from a read-only PG
extract `.../scratchpad/peg_extract.json`).
