# PEG Watch — the PEG tab and the community PEG board

> **TL;DR for any Claude session.** STAN has scored PEG on every QC run
> for a long time, but the number sat in `runs.peg_*` and nowhere else.
> v1.2.0 adds a **PEG tab** on the dashboard (the lab's own history:
> episodes, calendar, ladder, column periods, precursor cost) and a
> **community PEG board** on the HF relay, fed by `stan peg-sync` over a
> channel of its own. Three rules hold everywhere: a run counts only when
> it has a *real* measurement; **one acquisition is one run** however many
> times it was ingested; and **PEG share is never compared across
> instrument families**. The relay source is `hf_space/app.py` in this
> repo, deployed only through `scripts/deploy_hf_space.py`.
>
> Design spec, with the decisions and their reasons:
> `docs/superpowers/specs/2026-09-28-peg-watch-design.md`.

---

## Where the code lives

| Piece | Files |
|---|---|
| Detection (unchanged maths) | `stan/metrics/peg.py`, readers in `stan/metrics/peg_io.py` |
| Thermo on Hive (ThermoRawFileParser container) | `stan/metrics/peg_trfp.py`, `scripts/peg_backfill_thermo.py` + `.sbatch` |
| Trend maths (pure) | `stan/metrics/peg_trends.py` |
| DB readers | `stan/db.py` (`get_peg_*`, SQLite), `stan/db_pg.py` (`get_peg_*_pg`, PG) |
| Dashboard API | `stan/dashboard/server.py` → `GET /api/peg/overview` |
| Dashboard tab | `stan/dashboard/public/index.html` (`PegTab` and friends), `?tab=peg` |
| Share client | `stan/community/peg_submit.py`, `stan peg-sync`, `stan community-claim` (`stan/cli.py`) |
| Hive schedule | `scripts/cron_community_sync.sh` (runs `peg-sync` after `submit-all`) |
| Relay | `hf_space/app.py` (`/api/peg/*`, the "Evosep PEG Watch" section of the public page) |
| Relay deploy | `scripts/deploy_hf_space.py` |

---

## Data flow

```
Raw .d / .raw ──► peg_io reader (80 strided MS1 scans) ──► detect_peg_in_spectra
                                                             │
                     runs.peg_score / peg_intensity_pct / peg_n_ions_detected /
                     peg_class  +  peg_ion_hits (one row per oligomer x adduct)
                                                             │
                              PG Farm (store of record) ◄────┘   (Hive inline QC,
                                  │                               Thermo backfill)
            ┌─────────────────────┴───────────────────────────┐
            ▼                                                 ▼
 get_peg_runs / _instruments / _ladder_month_counts /   get_peg_share_rows
 get_column_change_events / get_peg_lab_lc_summary      (one row per acquisition)
 (one row per acquisition)                                    │
            │                                                 ▼
 peg_trends.build_overview                           stan peg-sync (Hive cron, 6-hourly)
            │                                                 │  POST /api/peg/submit
 GET /api/peg/overview (10-min cache)                         ▼
            │                              HF Space relay ──► peg/peg_latest.parquet
            ▼                                   │            peg/submissions/<ts>_<id>.parquet
 dashboard PEG tab ◄── browser fetch ───────────┤            (brettsp/stan-benchmark)
   (own history)       /api/peg/leaderboard     │
                       /api/peg/trend           ▼
                       /api/peg/lc-compare   community page, "Evosep PEG Watch" (#peg)
```

The PEG tab reads the lab's own data from its own dashboard server, and
the community parts straight from the relay in the browser (the relay's
CORS is `*`). If the relay is unreachable, those panels say so and the
rest of the tab still works.

Unlike the benchmark, the PEG board is not rebuilt nightly. An accepted
change is visible on the next read of an aggregate (the relay's cache key
includes the store version); the dataset commit follows through the
relay's batched commit worker.

---

## The metric: PEG share of MS1

`detect_peg_in_spectra` (`stan/metrics/peg.py`) reads **80 MS1 scans**
taken at an even stride in acquisition order and matches peaks within
**5 ppm** to a 60-ion panel: PEG1–20 as [M+H]⁺, [M+NH₄]⁺ and [M+Na]⁺
(the Rardin 2018 panel, `peg_method = "stan-peg-1"`). Peaks under
**10⁴** counts are ignored.

`peg_intensity_pct` — shown as "PEG share of MS1" — is

```
sum of matched PEG peak intensities
─────────────────────────────────────────────────── × 100
sum of ALL sampled MS1 peaks at or above 1e4 counts
```

**The denominator is not the TIC.** It is the above-floor intensity of
the 80 sampled scans. The same floor also applies to the numerator.

The PEG board ranks on this share, not on the 0–100 `peg_score`. The
score saturates at both ends: 29 % of UC Davis timsTOF runs score exactly
0 and 95 score exactly 100, so a median score cannot tell a bad month
from a terrible one. The share keeps rising with contamination, and
percent-of-signal is the unit the literature uses (HowDirty, mzsniffer).
The score and class stay as badges.

### Why it is never compared across instrument families

The 10⁴ floor is an absolute number, and the intensity scales it is
applied to are not the same:

- **Bruker**: alphatims yields per-detector events, not centroids, and
  stores their intensity as **uint16**. An event above 65,535 counts has
  already wrapped (70,000 becomes 4,464) and usually falls under the
  floor. The brightest PEG events are under-counted or dropped.
- **Thermo**: both readers return the FTMS centroid stream, whose
  intensities are on a different scale entirely, so the floor keeps a
  different fraction of the peaks.
- Which oligomers are reachable depends on the method's MS1 scan range,
  and 80 scans are taken whatever the gradient length. Detector gain can
  also differ between models within a family (Pro, Pro 2, HT, Ultra), which
  a cohort by family does not remove.

So a timsTOF 3 % and an Orbitrap 3 % are not the same amount of PEG. The
leaderboard ranks within `instrument_family × Evosep SPD method`, the
"Evosep vs other LC" panel compares LC groups **within one family**, and
the tab labels any cross-family comparison as such. The score and classes
are further off than the share, and since v1.2.1 the tab keeps them out
of cross-family comparisons altogether (Known limitations, first entry).

### Classes

| Class | `peg_score` | Note |
|---|---|---|
| clean | < 20 | |
| trace | 20–50 | also any score ≥ 50 with fewer than 4 ions (one bright coincidence is not a ladder) |
| moderate | 50–70 | ≥ 4 ions |
| heavy | > 70 | ≥ 4 ions. **Shared like any other class**: the board exists to show these runs |
| unknown | — | The pipeline's failure sentinel, stored with score 0.0. Never a measurement |

### What counts as a real measurement

A run counts only when `peg_score` and `peg_intensity_pct` are non-NULL
and finite and `peg_class` is one of the four real classes. `'unknown'`
is stored with score 0.0 — exactly what a spotless run scores — so a
reader that forgets the class check counts a failed read as clean.

QC only: `hidden = 0` (an integer compare; PG's `runs.hidden` is an
integer), `run_date` after 2015-01-01 (PG holds a Lumos run dated
1980-01-02), and names matching `wash|blank|blnk|blk|DELETE` excluded,
the same regex `stan submit-all` uses.

**Failed acquisitions are excluded too.** When a reader returns no MS1
signal, `detect_peg_in_spectra` answers 0 ions, 0 % and class `clean`.
The writers now leave PEG NULL in that case (watcher, Hive inline QC,
split-step `pegdrift`, the Thermo backfill, and the TRFP path, which
raises on an mzML with no MS1 peaks). Rows written before that fix are
dropped by the readers: **0 precursors and 0 PEG ions and exactly 0 %**
means unmeasured, not clean (`peg_trends.is_failed_acquisition`, and the
same clause in SQL on both backends). On live PG that was 21 of the 22
timsTOF rows with 0 precursors; the ones checked on Hive were 13–364 MB
acquisitions beside a 1.1 GB median. A missing precursor count (DDA, or
never searched) is not 0 and keeps the row.

---

## One acquisition, one run

PG holds some raw files more than once. On 2026-09-28 it had **1,674
timsTOF rows for 1,404 acquisitions: 241 duplicate groups, 270 extra
rows.** They come from double ingest — the instrument PC's own row
(STAN 0.2.222) next to Hive's re-processing (0.2.376), and pairs of the
same file under `/quobyte/...` and `/nfs/...`. The copies disagree: PEG
differs in 168 of the 241 groups and the class in 78, and 179 of the
extras fall inside the Dec–Apr episode. Counted as they stood, they
weighted those acquisitions twice or more and stretched episode 1 from
2026-04-07 to 2026-04-26.

**An acquisition** is `(instrument, basename(run_name), run_date to the
UTC second)`: exactly the fields `run_key` hashes, normalised the same way
(instrument trimmed; path reduced to its last component whatever the
separator, trailing `/` or `\` dropped, extension kept).

**The copy kept** is, in order:

1. the one whose ion hits are stored (`peg_ion_hits`) — the ladder needs
   them, and on live PG those are the Hive re-processings;
2. then the newest `stan_version`, compared as numbers (`1.0.10` beats
   `1.0.9`; `1.0.44` beats `0.2.376`; NULL is oldest);
3. then the highest `id` (bytewise).

The tab, the board and the Thermo backfill agree on this because there is
one definition in each language: `peg_trends.acquisition_key` /
`canonical_rank` / `pick_canonical` in Python (SQLite readers, and the
share client's second-line `_processing_rank`), and `_PEG_ACQUISITION_KEY`
+ `_peg_canonical_sql`'s `DISTINCT ON` in PG. Checked read-only against
live PG: `DISTINCT ON` keeps exactly the ids `pick_canonical` keeps, zero
mismatches. The backfill scores every copy whose PEG is NULL, so each
copy gets its own hits and the readers then choose among them by the same
rule.

After the rule and the failed-acquisition filter, UC Davis's timsTOF HT
has **1,385** real-PEG QC runs.

The rule does not remove all arbitrariness. In 43 groups the copies tie
on hits and version (`/quobyte` vs `/nfs` pairs processed by the same
Hive version), so the id decides; PEG differs in 17 of those and the class
in 7. That moves episode 1's median between 5.30 % (highest id, shipped)
and 4.55 % (lowest id); its dates and run count do not change. Deleting
the duplicate rows in PG is the real fix, and it is a separate data change
that needs its own go-ahead.

---

## What the PEG tab shows (`GET /api/peg/overview`)

`GET /api/peg/overview?instrument=<name>` — public, carries no run or
sample names. `instrument` defaults to the one with the most real PEG
runs; an unknown name gets an empty document without touching the store.
Cached 10 min per (instrument, UTC date); the `sharing` block is re-read
on every request. A failure reading the runs answers **503**; a failure
in a side panel (ladder, column log, LC comparison) serves the rest with
`"degraded": [...]` and is not cached.

All windows are UTC days. Definitions (`stan/metrics/peg_trends.py`):

| Section | Definition |
|---|---|
| Rolling median | Trailing 14-day median of PEG share per day; null when the window holds fewer than 5 runs. Keys `"all"` plus each SPD with ≥ 20 runs. |
| Episodes | A day is *hot* when its rolling median is ≥ 3.0 %. Hot days fewer than 21 days apart merge. Each episode's start is pulled back to the first run ≥ 3 % inside the first hot day's window (the day PEG showed up, not the day the median noticed). Episodes shorter than 14 days are dropped. `ongoing` when the last hot day is within 3 days of today. |
| Best baseline | Lowest trailing-90-day median among windows with ≥ 20 runs. Ties — and at UC Davis the median is exactly 0 in 425 of 969 windows — go to the lowest upper quartile, then the most runs, then the newest. |
| 30-day summary | Last 30 days vs the 30 before. `change_pct` is null when either window is empty or the previous median is below 0.1 % (the relay's rule). `streak_clean` counts clean runs back from the newest. |
| Ladder fingerprint | For PEG2–20, per month (January of last year onward): the largest fraction of that month's runs *with a known ladder* in which any one adduct of that oligomer was seen. A run's ladder is known when its hits were stored or it detected no PEG ion. A run with ions but no stored hits is left out of the denominator rather than read as "oligomer absent". |
| Column periods | From `maintenance_events` rows with `event_type = 'column_change'`; each change opens a period to the next one. |
| Precursor cost | Median precursors per class, per SPD, DIA runs with > 0 precursors only, SPDs with ≥ 10 clean and ≥ 10 heavy runs. |
| `lab_lc` | Every instrument with real PEG: majority LC, 90- and 365-day median, 90-day clean rate, 26 trailing 7-day weekly medians. |
| `sharing` | This host's setting, not the lab's: `source` is `config` (community.yml `peg_share: true`), `env` (`STAN_PEG_SHARE=1`), `opted_out` (set to anything else) or `off` (no setting here). |

On UC Davis's timsTOF HT (live PG, 2026-09-28, one row per acquisition)
the episodes are **2025-12-15 → 2026-04-07** (113 d, 169 runs, median
5.30 %) and **2026-05-21 → 2026-09-18** (120 d, 236 runs, median 5.31 %).

---

## The community board

### Cohorts and ranking (`GET /api/peg/leaderboard`)

- **Only Evosep runs are ranked**: `lc_system == "evosep"` at an Evosep
  method SPD (500, 300, 200, 100, 60, 40, 30, 15). On Evosep an SPD is a
  method identity; a derived 36 or 128 is not a cohort anyone ran.
- **Cohort** = `instrument_family × SPD`. Families are stored in STAN's
  fixed spelling (`timsTOF`, `Astral`, `Exploris`, `Lumos`, `Eclipse`,
  `Orbitrap`), so no client can respell or split one.
- **Window** 30, 90 or 365 UTC days ending today (default 30).
- **Ranked** needs ≥ 5 runs in the window. Order: median PEG share
  ascending, then clean % descending, then run count descending, then
  name. Labs under 5 runs are listed as `unranked`.
- **`change_pct`**: vs the previous window of the same length; null when
  that window had fewer than 5 runs or its median is below **0.1 %**
  (0.004 % → 3.2 % would otherwise read as +80,150 %).
- **Badges**: `cleanest` = rank 1 when at least 2 labs are ranked.
  `most_improved` = the most negative `change_pct` among ranked labs with
  `change_pct ≤ −15` **and** a fall of at least **0.5 percentage points**
  (so 0.02 % → 0.01 % cannot out-improve 12 % → 7 %), and, like
  `cleanest`, only when at least 2 labs are ranked.
- **Verified rows win**: once a name has any verified row, its unverified
  rows are left out of every aggregate. A lab that claims its name resends
  everything with its token and re-marks its own runs verified; what stays
  unverified under that name was sent by someone else before the claim.
- `weekly` is 12 trailing 7-day buckets; `community` is the pooled
  p25/median/p75 of the cohort's runs in the window.

The dashboard draws the community band on its timeline only when the
cohort has at least 3 labs.

### Evosep vs other LC (`GET /api/peg/lc-compare`)

Shown, never ranked. Both LC groups (`evosep`, `other`) within one
instrument family, over a 30/90/365-day window (default 90), with 26
weekly medians. Runs whose LC was never detected are not shared at all,
so they appear in neither group. UC Davis runs Evosep only on the timsTOF
and its own LC on the Orbitraps, so its own comparison is cross-family
and the tab says so; a within-family comparison needs a lab with both.

---

## Relay API reference

Base URL `https://brettsp-stan.hf.space`. Every aggregate is a pure
function of the stored rows and the current UTC date, cached 5 min.

### `POST /api/peg/submit`

Header `X-STAN-Auth: <auth_token from community.yml>` (omit when the name
is unclaimed).

```jsonc
{
  "display_name": "Clogged PeakTail",
  "stan_version": "1.2.0",
  "records": [{
    "run_key": "9f2c0c1e5b7d4a3f8e21c0aa",   // 24 hex
    "run_date": "2026-09-25T18:27:00Z",
    "instrument_family": "timsTOF",
    "instrument_model": "timsTOF HT",
    "lc_system": "evosep",                  // "evosep" | "other"
    "lc_model": null,
    "spd": 100,
    "acquisition_mode": "diapasef",
    "sample_type": "hela",
    "amount_ng": 50.0,
    "peg_intensity_pct": 0.353,
    "peg_score": 7.5,
    "peg_n_ions_detected": 2,
    "peg_class": "clean",
    "peg_method": "stan-peg-1"
  }]
}
```

Response `200`:
`{"status": "ok", "display_name", "verified", "accepted", "unchanged",
"rejected": [{"index", "reason"}]}`. A record identical to the stored one
counts as `unchanged`; a batch that changes nothing makes no commit.

| Status | When |
|---|---|
| 400 | `display_name` empty, over 60 characters after canonicalisation, or "Anonymous Lab" |
| 403 | The name is claimed and `X-STAN-Auth` does not match its token ("Run `stan community-claim`") |
| 413 | More than 2,000 records in one request |
| 422 | Body is not the expected JSON shape (FastAPI validation) |
| 429 | More than 30 requests in an hour from one client address (refused calls count), or a new unclaimed name while 200 unclaimed names already exist |
| 503 | `identity/claims.json` or `peg/peg_latest.parquet` could not be read. **Nothing is written** — a store rebuilt from one batch would overwrite every other lab's history |

Per-record rejections land in `rejected` and do not fail the batch:
invalid `run_key`; unparseable, future (> +1 day) or pre-2000 `run_date`;
`lc_system` not `evosep`/`other`; class not one of the four; share or
score outside 0–100; ions outside 0–500; `spd` outside 1–2000; missing
family or model; `amount_ng` outside 0–10⁶; a `run_key` repeated in the
batch (the later record is used); a new run past **20,000 rows per lab
name** (updates to stored runs still land); a new unclaimed-name row past
**200,000 unclaimed rows** in total.

The client address is the right-most `X-Forwarded-For` entry. Which hop
the HF proxy chain leaves there is **not verified yet** — see the deploy
runbook for the one-minute check.

### `GET` endpoints

| Endpoint | Query (default) | Returns |
|---|---|---|
| `/api/peg/leaderboard` | `family` (timsTOF), `spd` (100), `window` 30/90/365 (30) | `generated_at, as_of, window_days, family, spd, cohorts[{family, spd, n_labs, n_runs_365d}], ranked[{rank, display_name, verified, instrument_models, n_runs, median_pct, clean_pct, heavy_pct, change_pct, weekly[12], badges}], unranked[{display_name, verified, n_runs}], community{n_labs, n_runs, p25_pct, median_pct, p75_pct}` |
| `/api/peg/trend` | `family`, `spd`, `weeks` 1–260 (52) | `{weeks: [{week_start, n_labs, n_runs, p25, p50, p75}]}`, Evosep cohort only, oldest first |
| `/api/peg/lc-compare` | `family`, `window` 30/90/365 (90) | `family, window_days, as_of, groups[{lc, n_labs, n_runs, p25_pct, median_pct, p75_pct, clean_pct, heavy_pct, weekly[26]}], families[{family, evosep_runs, other_runs, evosep_labs, other_labs}]`. A group with no runs has `n_runs: 0` and nulls |

All three answer 400 for a window outside 30/90/365 and 503 when the
store cannot be loaded.

### Storage (`brettsp/stan-benchmark`, public, CC BY 4.0)

- `peg/peg_latest.parquet` — the whole table, one row per
  `(display_name, run_key)`, newest wins. Loaded once, on the first PEG
  request after the Space starts, held in memory, rewritten on every
  accepted change.
- `peg/submissions/<YYYYMMDDTHHMMSSZ>_<uuid8>.parquet` — the changed rows
  of each accepted batch, as an audit log.

Both go through the relay's batched commit worker with the benchmark's
own files. A Space restart drops what is still queued; the next 6-hourly
sync resends everything, so nothing is lost for good.

| Column | Type | |
|---|---|---|
| `display_name` | string | canonical lab name |
| `run_key` | string | 24 hex, sha256 prefix; no run name |
| `run_date` | timestamp[us, UTC] | acquisition time |
| `instrument_family` | string | STAN's fixed spelling |
| `instrument_model` | string | |
| `lc_system` | string | `evosep` \| `other` |
| `lc_model` | string | null today |
| `spd` | int32 | |
| `acquisition_mode` | string | lower case |
| `sample_type` | string | lower case |
| `amount_ng` | float64 | 50.0 when STAN does not know it (the `submit-all` default, not a measurement) |
| `peg_intensity_pct` | float64 | PEG share of MS1, percent |
| `peg_score` | float64 | 0–100 |
| `peg_n_ions_detected` | int32 | |
| `peg_class` | string | clean \| trace \| moderate \| heavy |
| `peg_method` | string | `stan-peg-1` |
| `verified` | bool | the name was claimed and the token matched |
| `submitted_at` | timestamp[us, UTC] | last time this row changed |
| `first_seen_at` | timestamp[us, UTC] | |

float64, not float32, so a value read back equals the one a client
resends and a resync does not look like a change.

---

## Identity

- **Claims** live in `identity/claims.json` in the public dataset:
  `{name: {email_hash, token_hash, claimed_at, v?}}`. `stan setup` or
  `stan community-claim` sends a 6-digit code to the email
  (`/api/claim-name`), and `/api/verify-claim` returns a token **once**.
  Only its hash is stored. The client writes it to community.yml as
  `auth_token`.
- **One token per name.** Re-claiming replaces the token, so the old one
  stops working everywhere at once. Copy the new `auth_token` line to
  every machine that shares as the lab.
- **Unclaimed names** are accepted as unverified. Whoever claims a name
  first owns it, and from then on the name's unverified rows drop off the
  board. `stan peg-sync` warns on every run that has no `auth_token`.
- **Canonical names.** Every lab name — claimed, submitted or looked up —
  goes through `_clean_text`: NFKC; control, format and
  Default_Ignorable characters dropped (zero-width spaces, bidi overrides,
  variation selectors, the combining grapheme joiner, Hangul fillers); any
  blank becomes a space; re-NFC; whitespace collapsed. Case is kept.
  Cross-script look-alikes (a Cyrillic "С") are not folded: such a name is
  a different, unverified lab, and the missing check mark tells them
  apart. Names are 1–60 characters; "Anonymous Lab" is refused. A claim
  stored before canonicalisation still binds its canonical form and is
  replaced on re-claim.
- **Claim limits** are keyed by the caller, never by the name alone, so a
  stranger cannot stop the owner re-claiming: 10 `claim-name` calls per
  caller address per hour (refused ones included), at most 3 codes per
  (name, email) per hour, and a `claim_id` that `verify-claim` must echo.
  Five wrong codes with that `claim_id` discard the code. A verify without
  a `claim_id` (STAN older than 1.2.0) never spends the code and is capped
  at 5 guesses per caller per name and 20 per name per hour.
- **`CLAIMS_PEPPER`** (Space secret). With it set, stored email hashes
  become `hmac_sha256(pepper, sha256(email)[:32])[:32]`, marked `"v": 2`;
  existing entries are migrated in place on first load (one commit to
  `claims.json`). Before this anyone with a list of candidate emails could
  link a pseudonym on a public PEG ranking to a person. **Never remove or
  rotate it once set**: v2 entries cannot be matched without it, and
  `claim-name` answers 503 for those names rather than a false "different
  email". The old unpeppered hashes stay in the dataset's git history;
  squashing that is a separate decision.

---

## Privacy

Sent per run: `run_key`, run date and time (UTC, to the second),
instrument model and family, LC group, SPD, acquisition mode, sample type,
amount, the four PEG fields and `peg_method`. Once per request: the lab
name and STAN version.

Never sent: run or file names, paths, raw data, spectra, sample names,
serial numbers, customer or project details. The client never reads raw
files, and the relay stores only the fields listed above whatever a
client adds. Blanks and washes are not shared, and `sample_health` is not
read at all.

`run_key = sha256(f"{instrument_model}|{basename(run_name)}|{run_date_utc}")[:24]`.
It is unsalted, and the instrument and the exact second are published
next to it, so a structured QC file name
(`09282026_HE50_60-spd-dia_S1-A2_1_24684.d`) can be recovered by a
dictionary search. That was accepted for QC runs, the same trade the
benchmark's fingerprint already makes. If names must be unrecoverable,
the fix is an HMAC with a per-lab secret generated once and kept in
community.yml, applied to the benchmark fingerprint too — a spec change.

There is **no retraction**. The relay only adds and updates. A run hidden
in STAN after it was shared, or re-dated so its key changes, stays on the
board under its old key.

---

## Configuration

| Variable | Where | Effect |
|---|---|---|
| `peg_share: true` | `~/.stan/community.yml` | Opt in to `stan peg-sync`. Off by default. `false` is an explicit opt-out the PEG tab reports as such |
| `STAN_PEG_SHARE=1` | env | Same opt-in. On a dashboard host that does not sync (Azure), it tells the PEG tab the lab shares from elsewhere; the server never runs `peg-sync` itself |
| `display_name`, `auth_token` | `~/.stan/community.yml` | Lab name and its claim token |
| `STAN_DISPLAY_NAME` | env | Name fallback when there is no community.yml (the hosted container) |
| `STAN_DB_BACKEND` | env | What `peg-sync` reads; `--backend pg\|sqlite` sets it for one run |
| `STAN_TRFP_SIF` | env | ThermoRawFileParser image. Default `/quobyte/proteomics-grp/STAN/historical_bsa/trfp.sif` |
| `STAN_APPTAINER` | env | Container runtime. Default `apptainer`, else `singularity`, on PATH |
| `CLAIMS_PEPPER` | HF Space secret | Peppered claim email hashes. Never remove or rotate once set |

---

## `stan peg-sync` and `stan community-claim`

```bash
stan peg-sync --dry-run              # build and count, send nothing (works with sharing off)
stan peg-sync --backend pg           # read PG Farm, send
stan peg-sync --relay http://localhost:7860   # against a local relay
stan community-claim                 # re-verify the lab name by email, store a fresh token
```

`peg-sync` is stateless and idempotent: every run resends every shareable
QC run in batches of 2,000, and the relay keeps what changed. No PG
column remembers what was sent, so no owner DDL is needed. Each row
becomes one record or one skip reason (`hidden`, `no_run_name`,
`blank_or_wash`, `peg_unknown`, `no_peg`, `peg_out_of_range`,
`lc_unknown`, `no_instrument`, `bad_date`, `no_spd`,
`duplicate_run_key`). A 429, a 5xx, a network error or a 200 without the
expected JSON (a sleeping Space) is retried once. A 403 stops the run
with the `community-claim` hint.

Exit code 1 when nothing could be sent (`failed`), the name is missing or
"Anonymous Lab", or the database read failed; 0 otherwise, including
`partial`, `sharing_off` and `nothing_to_share`.

Logs: `~/.stan/logs/peg_sync_<UTC timestamp>.jsonl` (`~/STAN/logs` on
Windows): one line per batch, with up to 50 sampled rejections, and a
summary line. On Hive that is `/home/brettsp/.stan/logs/`, which the lab
can read; the cron also appends the last six lines of each run to
`/quobyte/proteomics-grp/STAN/logs/cron_community_sync_<YYYYMMDD>.log`.

`community-claim` checks that community.yml can be written **before**
asking for a code, because the relay retires the old token the moment it
issues the new one. It writes the new token through an owner-only temp
file and keeps every other key (YAML comments are dropped, as with
`stan setup`).

---

## Thermo PEG on Hive

`read_ms1_thermo` tries `fisher_py` first. The Hive venv has no
`fisher_py` (it needs .NET), so before v1.2.0 every Orbitrap QC run kept
a NULL PEG: 0 of 2,924 Lumos + Exploris rows. Now, when `fisher_py` is
missing or cannot open the file, `stan.metrics.peg_trfp` converts the
`.raw` to an MS1-only mzML with the ThermoRawFileParser 1.4.5 image
(`-f=1 -L=1`, peak picking on) in a temporary directory under `$TMPDIR`,
samples the same 80-scan stride, and deletes the directory. Any failure —
no image, no apptainer, a failed conversion, an mzML with no MS1 peaks —
becomes `PegReaderUnavailable` and PEG stays NULL.

Measured in SLURM job 24179541 on a 1.1 GB Exploris 480 60-min DIA QC:
conversion **~35 s**, a **~50 MB** MS1 mzML in `$TMPDIR`, **1.3 GB** peak
RSS.

**Conversion runs only inside SLURM jobs, never on a login node.** Nothing
in the code enforces it. What keeps it true is where the callers run: the
inline QC path already runs inside the search job, the backfill is an
sbatch array, and the local commands that could reach it
(`stan backfill-peg`, `stan test --extract`) read only a local SQLite that
is empty on Hive. Do not call `read_ms1_any` on a `.raw` from an ad-hoc
script on `login1`/`login2`.

---

## Runbooks

Every step below needs Brett's go-ahead. They follow the order of spec §6.

### Deploy v1.2.0

1. **Merge and push** `feat/peg-watch` to `main`. That deploys nothing by
   itself.
2. **HF Space.** Authentication is the default HF token cache
   (`huggingface-cli login`).
   ```bash
   python scripts/deploy_hf_space.py                     # dry run
   python scripts/deploy_hf_space.py --yes --record-base
   ```
   The dry run prints the Space's live `app.py` sha256, the recorded base,
   and a diff stat. It **refuses** (exit 3) when the live file matches
   neither the recorded base nor the local file — someone edited the
   Space directly; download that `app.py`, merge its edits into
   `hf_space/app.py`, then deploy. It refuses (exit 4) when
   `SPACE_VERSION` was not bumped, since the post-deploy check waits for
   the new version. With `--yes` it also refuses (exit 5) in the half hour
   around a community sync — HH:15 to HH:45 at 00, 06, 12 and 18 h
   America/Los_Angeles, Hive's zone — because a restart drops the relay's
   unflushed commit queue. It uploads **only** `app.py`, naming the
   checked commit as parent so a concurrent edit fails the commit instead
   of being overwritten, then polls `/api/version` until it reports 1.2.0
   (exit 6 on timeout). `--record-base` rewrites `RECORDED_BASE_SHA256` in
   the script; commit that change.

   Then set the Space secret **`CLAIMS_PEPPER`** (Space settings →
   Variables and secrets) to a long random value, for example
   `python -c "import secrets; print(secrets.token_hex(32))"`. Keep a copy
   somewhere safe: HF does not show a secret again, and losing it locks
   every claimed name out of re-claiming. Setting it restarts the Space,
   so do it outside the sync window too.

   Check:
   ```bash
   curl -s https://brettsp-stan.hf.space/api/version          # 1.2.0
   curl -s https://brettsp-stan.hf.space/api/peg/leaderboard  # "ranked": []
   ```
   And settle the rate-limit key: send one `POST /api/peg/submit` with no
   `X-Forwarded-For` and one with `X-Forwarded-For: 203.0.113.1`, each
   with the body `{"display_name": "PEG deploy check", "records": []}`
   (accepted 0, writes nothing; the log line is written only for a
   request that gets through), then read the Space log's
   `[client <hash>, N X-Forwarded-For entries]`. A stable hash is correct.
   A hash that follows the forged value means the limit is forgeable; the
   same hash from two networks means it is global. Either way, fix
   `_peg_client_key` before relying on the 30/hour limit.
3. **Re-claim the lab name.** On the Mac, with 1.2.0 installed:
   `stan community-claim` (the code goes to Brett's email). Copy the new `auth_token` line from
   `~/.stan/community.yml` into `/home/brettsp/.stan/community.yml` on
   Hive, and set `peg_share: true` there.
4. **Hive.**
   ```bash
   ssh hive "cd /quobyte/proteomics-grp/brett/stan && git pull"
   ssh hive "cp /quobyte/proteomics-grp/brett/stan/scripts/cron_community_sync.sh /quobyte/proteomics-grp/STAN/"
   ```
   Then run one sync by hand (the login node is fine: it reads PG and
   posts, no compute):
   ```bash
   ssh hive "bash -lc 'export STAN_DB_BACKEND=pg PGPASSWORD=\$(cat /quobyte/proteomics-grp/brett/.pgfarm_token); \
     /quobyte/proteomics-grp/brett/stan_venv/bin/stan peg-sync --backend pg --dry-run; \
     /quobyte/proteomics-grp/brett/stan_venv/bin/stan peg-sync --backend pg'"
   ```
   Expect **about 1,385 accepted, verified** — one record per timsTOF
   acquisition, after failed acquisitions are dropped. The summary line
   names every skip reason; a few `lc_unknown` or `no_spd` are normal. A
   403 means the token on Hive is not the one step 3 issued. Network
   errors mean Hive's route to `*.hf.space` is down; run the same command
   from the Mac with
   `PGPASSWORD=$(cat /Volumes/proteomics-grp/brett/.pgfarm_token)`.
5. **Azure.** Zip deploy per `docs/AZURE_DEPLOY.md`, after
   `node scripts/check_jsx.js stan/dashboard/public/index.html`. Then:
   ```bash
   az webapp config appsettings set -g rg-fran -n stan-ucd-proteomics \
     --settings STAN_PEG_SHARE=1
   curl -s https://ucd.stan-proteomics.org/api/version    # 1.2.0
   ```
   The hosted dashboard never syncs; the setting only lets its PEG tab say
   that this lab shares (from Hive). Without it the card falls back to
   what the relay's board shows.
6. **Thermo backfill** (below), then `stan peg-sync --backend pg` again or
   wait for the cron.

### Thermo backfill

```bash
# after the Hive git pull (the venv must have stan/metrics/peg_trfp.py)
ssh hive "cp /quobyte/proteomics-grp/brett/stan/scripts/peg_backfill_thermo.py \
            /quobyte/proteomics-grp/brett/stan/scripts/peg_backfill_thermo.sbatch \
            /quobyte/proteomics-grp/STAN/"
# 1. a five-run test on one shard
ssh hive "bash -lc 'cd /quobyte/proteomics-grp/STAN && sbatch --array=0 peg_backfill_thermo.sbatch --limit 5'"
ssh hive "bash -lc 'sacct -j <jobid> --format=JobID,State,Elapsed,MaxRSS'"   # check at once
```

The running copies live in `/quobyte/proteomics-grp/STAN/`, **never**
`/quobyte/proteomics-grp/brett/`, where the `stan/` checkout would shadow
the installed package. Read the shard's JSONL
(`/quobyte/proteomics-grp/STAN/logs/peg_backfill_thermo_<ts>_s0of1.jsonl`,
one `done` line per run) and the SLURM output
(`peg_backfill_thermo_<job>_0.out`), then confirm the five rows in PG have
`peg_score`, `peg_class` and `peg_ion_hits`. Then:

```bash
ssh hive "bash -lc 'cd /quobyte/proteomics-grp/STAN && sbatch peg_backfill_thermo.sbatch'"   # 8 shards
```

It runs on `low` (`publicgrp` / `publicgrp-low-qos`, `--requeue`), 8
shards because each holds a PG Farm connection shared with FRAN. The
queue is "PEG still NULL": 2,924 rows (2,363 acquisitions), ~24
CPU-hours, ~3 h per shard. Ion hits are written before the scalars, so a
preempted shard only redoes the run in flight. Any failure leaves PEG
NULL for the next pass rather than stamping `'unknown'`. `--list-only`
logs a shard's queue without converting; `--dry-run` scores without
writing. Afterwards run `stan peg-sync --backend pg` (step 4's command)
to send the Orbitrap runs.

### A corrupt `peg/peg_latest.parquet`

The relay refuses to write over a store it could not read: every submit
and every aggregate answers **503** and it retries the load every 30 s.
Nothing is lost while that lasts — the clients retry next tick.

1. Find the last good revision and restore it:
   ```python
   from huggingface_hub import HfApi, hf_hub_download
   import pyarrow.parquet as pq
   repo = "brettsp/stan-benchmark"
   api = HfApi()
   for c in api.list_repo_commits(repo, repo_type="dataset")[:40]:
       print(c.commit_id[:10], c.created_at, c.title)
   p = hf_hub_download(repo, "peg/peg_latest.parquet", repo_type="dataset", revision="<sha>")
   print(pq.read_table(p).num_rows)          # it must read
   api.upload_file(path_or_fileobj=p, path_in_repo="peg/peg_latest.parquet",
                   repo_id=repo, repo_type="dataset",
                   commit_message="restore peg/peg_latest.parquet")
   ```
   Do it outside the sync window. While the store is failing to load the
   Space picks the restored file up by itself within 30 s. If it had
   loaded (a file that reads but holds the wrong rows), restart the Space
   after the upload, or its in-memory table is written back over the
   restore on the next accepted batch.
2. Runs accepted after that revision come back on each lab's next sync.
   For a lab that no longer syncs, replay `peg/submissions/*.parquet`
   newer than the revision, newest row per `(display_name, run_key)`.
3. As a last resort, delete the file: the relay starts an empty store and
   every syncing lab refills it within one 6-hourly cycle. `first_seen_at`
   restarts, and labs that no longer sync are gone.

---

## Known limitations and follow-ups

- **The measurement is not comparable across instrument families, and the
  classes least of all** (found 2026-09-29, from PG). All three UC Davis
  instruments show the same ladder (PEG8–13, mostly [M+NH₄]⁺); the
  difference is STAN's measurement, not what a detector can see. The
  Bruker reader (`peg_io.read_ms1_bruker`, alphatims) yields raw per-push
  TOF events stored as uint16: recorded timsTOF PEG ion intensities are
  p10 10,861 / median 24,365 / p90 56,771 / max 65,260 against a 65,535
  ceiling, and brighter events wrap and fall under the floor. Orbitrap
  centroids (TRFP mzML) run p10 ~20–30k / median 110–190k / p90
  1.3–2.3 M / max 4.5–6.0 × 10⁹. At the same absolute 10⁴ floor
  (`detect_peg_in_spectra`) a timsTOF run records ~3 PEG ions to an
  Orbitrap's ~27, and the 0–100 score, its classes and "clean %" are
  driven by the ion count. So the tab showed the timsTOF 15 % clean
  against 1 % on the Orbitraps, although the Orbitraps' PEG share is
  25–35× lower (90-day median: timsTOF 4.66 %, Lumos 0.14 %, Exploris
  0.19 %). Within one instrument over time the measurement is
  consistent. **What the dashboard does now (v1.2.1, UI only):** the
  "Your instruments" table leaves out the clean column whenever its rows
  span more than one family and says the score and classes are calibrated
  on timsTOF data; off a timsTOF, the Clean and Heavy tiles, the timeline
  class legend and the calendar legend are tagged "timsTOF-calibrated"
  with a one-line note to compare PEG share, and the headline does not
  quote a best-90-day clean rate. PEG share is the closer measure, not a
  comparable one: the floor and the uint16 ceiling bear on it too. The
  community LC comparison (one family) keeps its clean %. **The fix is
  pending a design** — reading Bruker intensities without the uint16
  ceiling, a floor and score calibration that hold per family, and a
  rescore of the history — and nothing in PG, the relay or the classes
  has changed.
- **PEG on blanks and `sample_health`.** The monitor PEG step is a no-op
  and a `sample_health` PEG write in PG mode goes to local SQLite. Blank
  PEG is carry-over and would be the most direct Evosep signal; it needs
  that path built first.
- **`ladder_coherence` and `lc_model` are not stored.** Both need owner
  DDL (`brettsp`, CAS login). `lc_model` is sent as null.
- **Duplicate rows in PG.** The readers collapse them; deleting them is a
  separate data fix, and it would settle the 43 groups the id decides.
- **Empty reads in local CLI paths.** `stan backfill-peg` and
  `stan test --extract` still store a no-signal read as a clean 0.0. The
  readers' failed-acquisition filter catches such a row only when the run
  also identified no precursors.
- **No retraction** on the relay, and `run_key` is unsalted (Privacy).
- **Benchmark auth holes, pre-existing and untouched:** `/api/submit`
  with `X-STAN-Auth` calls an undefined `_load_claimed_names` (a 500, so
  the benchmark client drops the header); `/api/update` accepts any
  non-empty `X-STAN-Auth`; the admin endpoints are open when
  `ADMIN_SECRET` is unset.
- **Space dependencies are pinned** in `hf_space/Dockerfile` (fixed in
  the same release): they used to be unpinned, and huggingface_hub 2.0
  dropped the `huggingface_hub.hf_api.CommitOperationAdd` re-export the
  relay imported, so any rebuild of the live Space would have crashed it
  at import. GitHub CI caught it on 2026-09-29. `deploy_hf_space.py` ships
  the Dockerfile with `app.py` in one commit, under the same recorded-base
  guard. Move a pin only together with a run of `tests/test_relay_peg.py`
  on the new version (it passes on huggingface_hub 1.8 and 2.0).
- **`fisher_py` vs ThermoRawFileParser** read the same FTMS centroid
  stream by both readers' source, but no one file has been scored both
  ways to confirm the numbers match.
- **The rate-limit key** (right-most `X-Forwarded-For`) is unverified
  until the post-deploy check in step 2.
- **The best-baseline tie-break** picks 2024-05-14 → 2024-08-11 (44 runs,
  median 0.0 %) on UC Davis data, not the 2025 window the approved mockup
  showed. It is deliberate and tested, but wants Brett's explicit
  sign-off.
- **Mixed-LC instruments.** `lab_lc` has one row per instrument, labelled
  with its majority LC. An instrument run on both Evosep and a nanoLC in
  the same window would be pooled.
