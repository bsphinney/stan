# Community site redesign + search-aware precursor lookup

**Date:** 2026-09-29  
**Status:** Part A approved to plan and implement. Part B design approved in direction; the calibration panel needs one go/no-go.  
**Approved by:** Brett Phinney, 2026-09-29: "okay I like the changes lets plan to implement them and design an algorythm so peopel can put in their precursors and what search it was from".  
**Mockup:** https://claude.ai/artifact/SJSs2MqX18cLJteWH28Fka (version 2). It is private to Brett's account.  
**Sources in git:** `docs/community-redesign/`
- `REVIEW.md` is the site review (D1–D8, B1–B6, and 21 bugs), with app.py line numbers as of relay 1.2.1.
- `mockup/community_mockup.template.html` holds the mockup page code. It is a reference implementation of the new panels.
- `mockup/build_mockup.py` fills the template from the snapshot. It also carries the reference Python for dedupe, cohort key and TIC summaries.
- `mockup/shot_mock.py` takes headless-Chrome screenshots over CDP and reports console errors and overflow.

**Kept outside git** (~66 MB), in `~/stan-handoff-2026-09-29/`:
- the 2026-09-29 live-API snapshot (`sitereview/*.json`);
- screenshots;
- the TIC verification results (`tic_verification.json`);
- the engine-scaling research (`scaling/`).

Rebuild the mockup with `STAN_MOCK_SNAPSHOT=~/stan-handoff-2026-09-29/sitereview python3 docs/community-redesign/mockup/build_mockup.py`.

---

## 0. Rules that govern this work

These come from Brett's feedback this session and are also in memory.

1. **Keep every existing chart visible and recognisable** (`feedback_keep_existing_charts`). The redesign fixes the numbers inside the charts; it does not prune them.
   - The first mockup collapsed, redrew or left out charts: the TIC overlay, Points Across Peak, Depth by Throughput, MS1 signal, Dynamic Range, and Throughput vs. Quantitation Quality. Brett read each of those as a removal.
   - Removing, collapsing, merging or replacing any chart is Brett's decision. List it and ask. The only removals he has approved are the IPS scatter and the fingerprint radar (decisions 3 and 4).
2. **Mockup before UI changes.** Every kept chart is drawn in the mockup with real data, never a placeholder.
3. **Deploy when verified; no need to ask** (`feedback_deploy_when_verified`). **Rewriting stored data always needs Brett's explicit go**, with a before/after first. That covers the parquet history, PG rows and stored IPS scores.
4. The project CLAUDE.md applies:
   - bump the version in `pyproject.toml`, `stan/__init__.py` and both instrument `.ps1` `$ScriptVersion` values;
   - update the docs in the same commit;
   - commit subjects are 50 characters or fewer, with no AI attribution;
   - run `node scripts/check_jsx.js` before an Azure deploy;
   - the relay deploys through `scripts/deploy_hf_space.py`, which has a base-hash guard and a sync-window guard.

---

## 1. Decisions

| # | Decision | Status | Recommended / agreed |
|---|---|---|---|
| 1 | Implement the mocked changes (D1–D8, B1–B6, TIC fixes) | **Approved** 2026-09-29 | As in Part A, with the chart-keep correction in §A.2 |
| 2 | Keep all live charts listed in §A.2 as KEEP | **Brett's direction** | Keep them, visible |
| 3 | Identification Depth vs. IPS scatter | **Decided 2026-09-29: drop** (Brett: "I agree with you… drop the radar fingerprint and ips scatter") | Delete the chart and its `renderIps*` code. It plotted IPS against its own 50% input, so it showed diagonal bands only |
| 4 | Instrument Health Fingerprint radar | **Decided 2026-09-29: drop** | Delete the chart and its code. It min-max scaled medians across mixed SPD tiers, and its ppm axis ranked the analyser type |
| 5 | Filenames (D4): API and hovers | Approved with #1 | Remove from `/api/leaderboard`, `/api/cohorts` and hovers; keep them on the server only |
| 5b | Filenames (D4): scrub the published parquet history | **Gated: data rewrite** | Brett's explicit go, with a backup first (`wipe_v1.py --backup`) |
| 6 | IPS family→reference key fix, then recompute stored `ips_score` | **Gated: data rewrite** | Show Brett the before/after first. Decide on Exploris recalibration after that |
| 7 | Footer license wording | **Approved 2026-09-29** (Brett: "those two decisions are fine") | "Code: STAN Academic License (free for academic and non-profit use; commercial use by written permission)", linked to LICENSE. UC tech-transfer ownership is still open |
| 8 | One-facility disclosure wording | **Approved 2026-09-29** | The mockup's line, see D2 below |
| 9 | Engine-calibration panel compute on Hive (Part B) | **One go/no-go**, required by the pipeline skill | See §B.5 |
| 10 | Spectronaut arm of the panel | **Brett runs it** on the licensed machine | See §B.5 |
| 11 | **Which library defines STAN's reference count** (research finding M2, §B.10) | **Decided 2026-09-29: frozen community library** for cohorts and S; re-search cohort rows, before/after to Brett before any PG write | Recommended: the frozen community library for both cohorts and S; re-search the cohort rows (0.2–1 core-h each). Today timsTOF and Exploris cohorts use per-instrument subset libraries that no outside lab can reproduce |
| 12 | **nanoLC cohort key: gradient bands + LC model + flow regime** (§A.3 B2, §A.5) | **Approved 2026-09-29** (Brett: "we should make it so others using non-Evosep LCs can eventually be ranked") | Fixed gradient bands; LC model and flow regime join the key; LC detected at submit time. Ships with the P3 schema changes |
| 13 | **Header link to the Instrument Health Explorer** (§A.1) | **Approved 2026-09-29** (Brett) | Section keeps its live name; linked from the header nav |

---

# Part A: Community site redesign

The live page is one HTML/JS string inside `hf_space/app.py`, served by the relay (`SPACE_VERSION`, currently "1.2.1"). The mockup template is a *reference* for the new panels. Port its logic, not its markup wholesale. Remove its review layer, the pink annotations and the `window.__stanLookup` test hook.

## A.1 Page order

This is REVIEW §6, amended so that no chart is dropped.

1. **Header.** Purpose line: "Compare your QC HeLa against reference ranges from labs running the same frozen search." Then the one-facility disclosure (D2). Nav order: Join · Where do I stand · Instrument Health Explorer · Methods · PEG Watch · Dataset · API · GitHub · Museum · Arcade. (Brett asked for the Instrument Health Explorer link, 2026-09-29.)
2. **Stats row.** Runs · labs (counted as facilities) · instruments · latest run. A Join tile replaces the inert "Hide failed runs · 0 flagged" card. A three-line glossary (SPD, IQR, IPS) sits below.
3. **Sticky filter bar.** QC standard · DIA/DDA · amount. Every panel follows it and prints what it follows in its badge. A panel that deliberately ignores a filter says so ("all amounts").
4. **Where does my run sit?** The lookup (B1, with the Part B engine fields).
5. **Reference ranges.** Grouped by instrument model. The primary metric is large, each card shows "n runs · n labs", and sparse cohorts are folded (D5).
6. **Join the benchmark** (D6).
7. **How the numbers are made** (D7 + D3).
8. **Instrument Health Explorer** (the live section name, linked from the header). Every chart in §A.2, open by default at desktop width. On phones each is a `<details>` with a one-line takeaway. The TIC overlay, Depth by Throughput and Throughput vs. Quantitation Quality are open at every width.
9. **LC / instrument health (ID-free).** Mass accuracy, MS1 signal, dynamic range and points across peak, **visible**, drawn one line per instrument, with the detector-family note.
10. **Lab trend vs. reference** (B3).
11. **Evosep PEG Watch.** Unchanged.
12. **Submissions table.** Adds a Cohort column; no filenames (D4, B2).
13. **Footer.** Correct license, CC BY data, and a link to the exact list of public fields.

## A.2 Chart inventory: every live chart and its fate

Each fix applies the shared cohort key (B2): DIA/DDA never mixed, instrument **model** not family, and "n runs · n labs" on every chart.

| Live chart (app.py) | Fate | Fixes inside it |
|---|---|---|
| Depth by Throughput (which SPD gives me the best data?), `renderSpdDepth` | **KEEP**, live form: a box per SPD, a facet per instrument, amount select, amount shapes | Split DIA/DDA. Facet by model (HT ≠ Pro ≠ Pro 2). Within-vendor caveat (D7). Keep the title question |
| Throughput vs. Quantitation Quality (Matthews & Hayes 1976), `chart-points-peak` | **KEEP, visible** | DOI → 10.1021/ac50003a028. Check that the "<6 points → >1% error" threshold is actually in that paper; otherwise label it a STAN guideline that cites it. "Shape = LC column" must not treat "Unknown" as a column |
| Points Across Peak (time series), `chart-pts-peak` | **KEEP, visible** | One line per instrument, not per family. Same DOI fix |
| MS1 Signal (TIC proxy) | **KEEP, visible** | Per instrument. Note: "timsTOF reads ~1.5 log units lower because of its detector, not its health" |
| Dynamic Range | **KEEP, visible** | Per instrument. Delete "Populated going forward by the STAN watcher" (bug 19) |
| Mass Accuracy Drift (MS1) | **KEEP, visible** | Per instrument |
| Depth by Amount Loaded | **KEEP** | Amount fixes (D8/B4). Delete the saturation sentence. Today 99% of rows are 50 ng, so state that |
| Identification Depth by Platform (violins) | **KEEP the violins** | Split by SPD cohort and DIA/DDA, with the within-vendor caveat. Fix the axis labels that clip at 400 px |
| Column Comparison | **KEEP, visible** | `colKey()` returns '' for "Unknown". Show n labs and date span under each bar. Honest empty state: "needs a second known column in one cohort" |
| Community TIC overlay by SPD (and LC) | **KEEP**, see §A.4 | See §A.4 |
| Your Trend vs. Community Reference | **KEEP, rebuilt** as "Lab trend vs. reference" (B3) | Reference excludes the selected lab; percentile bands; own-baseline drift overlay |
| Best Configurations | **KEEP** (B6) | Amount select (default 50 ng), Labs column, sort/bestDepth fix (D1) |
| Reference range cards | **KEEP, regrouped** (D5) | |
| Identification Depth vs. IPS | **DROP** (decision 3, Brett 2026-09-29) | — |
| Instrument Health Fingerprint (radar) | **DROP** (decision 4, Brett 2026-09-29) | — |
| PEG Watch (leaderboard, week by week, Evosep vs other LC, join card, ranking card) | **KEEP, unchanged** | |
| Info cards: HeLa Standard, IPS, Why This Benchmark Works, Points Across Peak | HeLa kept; IPS rewritten (D3); "Why…" becomes "How the numbers are made" (D7); Points Across Peak card kept with the DOI fix | |

## A.3 Changes, by review item

Full problem statements and evidence are in `docs/community-redesign/REVIEW.md`; the line numbers are app.py 1.2.1.

- **D1 DIA/DDA separation.**
  - Add mode to both `broadId()` keys (~3913, ~5362) and read each cohort's own mode.
  - Best Configurations: reset the sort to `psms` under DDA, compute `bestDepth` as the maximum of the active metric, and hide the precursor column under DDA.
  - Accept when no DIA card range starts at 0 and no DDA row appears in a DIA view.
- **D2 Single-facility honesty.**
  - Add the disclosure line: "Today essentially every run here comes from one facility, the UC Davis Proteomics Core (timsTOF HT, Exploris 480, Fusion Lumos). The ranges below are that facility's longitudinal ranges until more labs join."
  - Compute the banner count from the same array as the stats row.
  - Show "n runs · n labs" everywhere, and a "single-lab reference" tag below 2 facilities.
  - Keep "best …" badges off below 2 labs.
  - Below 10 runs, list the values instead of an IQR.
  - Counting labs as facilities needs a facility id (see §A.6).
- **D3 IPS.**
  - Rewrite the card to the v2 definition.
  - Use the band table from `docs/ips_metric.md`.
  - Proteins line: "context only; not used for leaderboards; 20% of IPS".
  - **Keep IPS badges off the public page until decision 6 ships.** The family→key mismatch means that:
    - all 1,026 Exploris and 1,342 timsTOF HeLa DIA scores equal the global fallback;
    - keyed correctly, the medians move 38→50 (Exploris) and 68→54 (timsTOF).
- **D4 Filenames.**
  - Drop `run_name` and `fingerprint` from the public API responses and from the three hovers (~4086, 4390, 4579).
  - Make run_name optional at submit (`V1_REQUIRED_*`), so the `STAN_STRIP_RUN_NAME` opt-out works.
  - Scrubbing the parquet history is decision 5b.
- **D5 Reference cards.**
  - `colKey()`: "unknown" or empty returns ''.
  - Group the cards by model; show the primary metric large.
  - Fold cards under n=5.
  - Show "gradients seen: 46–60 SPD" rather than a contradictory title.
- **D6 Join card and license.** Three steps:
  1. Install STAN and inject Pierce HeLa 88328.
  2. `stan community-claim`.
  3. `community_submit: true` or `stan submit-all`.

  Add the exact list of published fields, the fields that never leave the lab, and "appears after the nightly rebuild". Footer wording is decision 7.
- **D7 How the numbers are made.**
  - DIA-NN 2.3.0 for DIA; Sage 0.14.x for DDA; `SEARCH_PARAMS_VERSION` v1.0.0.
  - The precursor count is unique `Precursor.Id` at run-level `Q.Value ≤ 0.01` (`stan/metrics/extractor.py` ~500, 637).
  - **Empirical** per-vendor HeLa libraries (timsTOF ~54k, Orbitrap ~170k precursors), with md5 links.
  - Library coverage on timsTOF cards; above 90% marks the cohort as library-limited.
  - Within-vendor caveat on cross-vendor charts.
- **D8 Data hygiene.**
  - Set `is_flagged` for implausible amounts (>5,000 ng, or a unit-parse mismatch).
  - Deduplicate at consolidation. Reference rule, from `build_mockup.py`: same instrument, track and all four ID counts, with acquisition instants within 2 s of each other. The filename is not in the key. On the snapshot that is 3,305 → 3,061 rows, 235 groups.
  - Delete the saturation sentence.
  - Changing stored rows is gated. Filtering at read time is not.
- **B1 Where does my run sit?** The lookup, plus the engine and version fields from Part B.
  - Until Part B's calibration is live, only "STAN standard search (DIA-NN 2.3.0 + STAN library)" gets a percentile.
  - Any other engine gets the refusal text from §B.4.
- **B2 One cohort key.**
  - The key is model × mode × gradient × amount bucket.
  - Evosep runs are named only by real Evosep methods; other Evosep SPDs read "SPD unverified".
  - nanoLC is named by the gradient its SPD implies (1440 ÷ (1.25 × SPD)) and then the **stored** `gradient_length_min` run length ("~30 min gradient (38 SPD) · 44 min run"). *(P2b review, 2026-09-30: this replaces "44 min run (~38 SPD)", which read as a contradiction beside "44 min run (~32 SPD)". The cohort key is unchanged.)* **Amended 2026-09-29, decision 12:** see the nanoLC key below.
  - A run with no LC recorded at an Evosep-method SPD is "LC not recorded", never inferred.
  - Reference: `lc_class()` in `build_mockup.py`.
  - The sticky filter bar drives every panel.
  - **nanoLC cohort key (decision 12, 2026-09-29).** Goal: other labs' non-Evosep runs can be ranked against each other, not only against their own history. Today a nanoLC cohort is keyed by the exact run length, so a lab at 45 min and one at 44 min never share a cohort, and "custom" lumps every non-Evosep LC together.
    - **Gradient band** replaces the exact length. The band is taken from the **active gradient behind the stored SPD**, gradient_min = 1440 / (1.25 × SPD), which is what `gradient_min_to_spd` derived it from. It is **not** taken from `gradient_length_min`, which holds the acquisition length: the Exploris 38 SPD runs store 44 min while their TIC ends at 30, and on both Orbitraps 19 SPD (61 min gradient, median ~22.8k precursors) and 12 SPD (96 min, ~31.5k) runs both store ~88 min, so banding on it merged them (mockup check, 2026-09-29). Fixed bands: ≤20 → 15 min, 21–37 → 30, 38–52 → 45, 53–75 → 60, 76–105 → 90, 106–150 → 120, >150 → 180. Fixed bands keep cohort names stable as runs arrive; a ±% rule would move boundaries. Title: "nanoLC · Vanquish Neo · nanoflow · 30 min gradient band (~38 SPD)".
    - **LC model** joins the key. STAN already reads it from raw files (`stan/tools/trfp.py` `_extract_lc_from_raw_binary`: "Thermo Vanquish Neo", "Dionex UltiMate 3000", …; Bruker via the HyStar method in `stan/metrics/scoring.py` `detect_lc_system`) but `detect_lc_system` collapses it to evosep/custom. Keep the model string; add `lc_model` to the submission.
    - **Flow regime** joins the key: nanoflow (<1 µL/min) · capillary (1–10) · microflow (>10). It is the biggest driver of depth and is not reliably in raw files, so it comes from `stan setup` / `add-watch` (per instrument, like the column) and is submitted as `lc_flow`.
    - **LC detected at submit time** for every run, so "LC not recorded" becomes rare; a run whose LC still cannot be read is matched on band only and says so.
    - Matching falls back gracefully: band + model + flow when all are recorded; band only (labelled "LC model/flow not recorded") otherwise. Today's UC Davis Orbitrap cohorts have neither recorded until backfilled from their raws (Hive `trfp.sif` can read Thermo LC metadata).
    - Follow-up: `gradient_length_min` is the acquisition length on these instruments, not the active gradient. Either rename it in the schema docs or add an explicit `active_gradient_min`, so the lookup's "active gradient" field and the stored value mean the same thing.
- **B3 Lab trend vs. reference.**
  - The reference is the same cohort **without** the selected lab, drawn as p10/p25/p75/p90 bands.
  - Add a drift overlay from the lab's own baseline: median ± 3 robust SD (1.4826 × MAD, the robust-σ control-chart convention) of the lab's first 30 runs in the cohort, provisional from 20 runs until the 30th.
  - The empty state is "No other lab in this cohort yet".
  - This rewrite fixes bug 10.
- **B4 Amount source and FAIMS** (a schema change, §A.6).
  - The client resolves the amount from metadata first, then from a unit-anchored filename parse.
  - Record `amount_source` as declared, parsed or assumed.
  - Add a FAIMS flag as a cohort attribute.
- **B5 Page weight and phone.** Replace the 3.0 MB `/api/tic-overlay` load with per-cohort summaries (§A.4). Phone-sized `<details>` per chart, and fix the clipped labels. *No chart is dropped* (§A.2 overrides the review's "trim").
- **B6 Best Configurations.** Amount select, Labs column, primary metric right after Instrument on phone.
- **Bugs 1–21** (REVIEW §4): all fixed. Bugs not covered by the items above:
  - bug 6: Matthews & Hayes DOI;
  - bug 12: ordinals;
  - bug 18: Astral in a legend;
  - bug 19: "populated going forward";
  - bug 21: favicon and `mobile-web-app-capable`.

## A.4 TIC overlay spec

Brett values this panel ("really like the TIC overlay broken out by SPD and LC. It was very useful").

Keep:
- the separate **SPD** and **LC** menus (All · Evosep only · Custom / nanoLC only);
- each run scaled to its own peak;
- the median line with IQR and 10–90% bands;
- "show all traces";
- Plotly interaction (zoom, pan, legend toggles, PNG download) and the ⛶ fullscreen button.

DIA/DDA follows the page filter bar. Changes:

1. **Say what it plots.** 2,974 of 3,025 traces are the **raw MS1 TIC from the file**:
   - `hive_process.py` ~683 and `hive_steps.py` ~674 call `extract_tic_bruker/thermo`;
   - `baseline.py` ~1556 prefers raw.
   51 older submissions (STAN 0.2.282/0.2.283/0.2.376/1.0.5) carry an **identified-ion** trace, which starts at the first identification. The live text "Identified (DIA) or raw (DDA)" is wrong. Label the trace "MS1 total-ion chromatogram from the raw file", and either exclude the 51 identified-ion traces from the median or show them as a separate series.
2. **Serve summaries, not raw traces.**
   - A new relay endpoint returns, per (sample, mode, SPD, LC): n, instrument counts, the time axis, and p10/p25/p50/p75/p90.
   - For every SPD × LC choice that is 140 KB raw (48 KB gzipped), against 8.7 MB raw (3.0 MB gzipped) today, which is 60% of the cold load.
   - Raw traces load only when "show all traces" is ticked, per cohort.
3. **Take percentiles at the same minute.** Interpolate each trace onto the cohort's median time axis. Live takes them at the same *bin index* on the first trace's axis. That disagrees for 30 runs in their own LC cohort (99% agree), mostly because identified-ion traces start late. The combined 30/46 SPD views also mix 44 min Evosep with 40 min nanoLC.
4. **Open on the largest cohort** (100 SPD Evosep, 626 deduped runs), not the lowest SPD (7 SPD, 2 runs). **Bands only from 5 or more runs.** Below that, draw each run on its full own axis, coloured by instrument, and write "each of 3 runs (too few for a median)", not "the median shape of 3 runs".
5. **Use the same LC rule as the rest of the page** (`lc_class`, not live's `inferLcSystem`). Non-method Evosep SPDs read "Evosep, 32 min run (36 SPD unverified)". Runs with no LC recorded are not put into Evosep. Build from the same held-back-filtered set as every other panel (`valid`), so trace counts never exceed benchmark runs.
6. **Menu labels.**
   - Use the stored `gradient_length_min` (one run-length source for the whole page), not the trace end.
   - Show "(no bands)" rather than a long suffix.
   - When All mixes gradients, say so ("30 SPD · Evosep 44 min + nanoLC 40 min").
7. **Say what "community" means here.** Take line: "626 runs · 1 facility", with the single-lab tag.
8. The DDA empty state disables the LC select and the checkbox, and shows its message once.

Verification details: `~/stan-handoff-2026-09-29/tic_verification.json`.

## A.5 Schema changes

These cross the client, relay and dashboard in the same change, per CLAUDE.md.

- `amount_source` (declared | parsed | assumed) and `faims` (bool) on submissions (B4).
- `lc_model` (string, from the raw file) and `lc_flow` (nano | capillary | micro, from setup) on submissions; `gradient_band` derived server-side (B2 amendment, decision 12).
- `run_name` optional at submit; never in public responses (D4).
- A **facility id**, so labs are counted as facilities. Two pseudonyms of one facility (Clogged PeakTail and Anonymous Lab are both UC Davis) must count as one. Proposal: attach it to the verified `community-claim` record. *As built (P3c):* a separate, admin-written `identity/facilities.json` instead, because `/api/verify-claim` replaces a name's claim record on every re-claim, and because a facility's 'Anonymous Lab' rows are matched by submission time, which no claim can carry.
- TIC summary endpoint (§A.4).
- `/api/scaling` (Part B).

## A.6 Phasing

Each phase ships on its own, verified and deployed without asking, except the gated items.

- **P0: mockup v3 (next).**
  - Draw every KEEP chart from §A.2 in its live form with real data.
  - Apply the §A.4 TIC fixes.
  - Add the Part B engine fields to the lookup.
  - Republish to the same artifact URL for Brett.
- **P1: correctness** (the one-day list): D1, D2, D3 (IPS badges off), D4 API and hovers, bugs 6, 10, 11, 12, 19, and the TIC wording (§A.4 item 1).
  - *Status 2026-09-29:* shipped as relay (Space) 1.2.2 / STAN 1.2.9, with optional run_name at submit pulled forward from P3; see CHANGELOG 1.2.9. Identified-ion TIC traces are kept out of the median (§A.4 item 1, second half). Until the P3 facility id, the default name 'Anonymous Lab' never counts as a second lab (`labCount`).
- **P2: layout.** Page order, the B2 cohort key and sticky bar, B1 (standard-search only), D5, D6, D7, B5 (TIC summaries, `<details>` on phone), B6, B3.
  - *Status 2026-09-30, P2a (layout and cards):* relay (Space) 1.3.0 / STAN 1.2.12, see CHANGELOG 1.2.12. Done: §A.1 page order and nav (Join · Where do I stand · Instrument Health Explorer · Methods · PEG Watch · …; "Where do I stand" anchors the reference ranges until P2c), the Join tile in place of "Hide failed runs", the glossary, D5 cards (grouped by model, `colKey()` "Unknown" → '', sparse under 5 runs folded, "gradients seen", primary large), D6 Join card, D7 + D3 Methods (states the decision-11 subset libraries plainly), §A.2 chart fixes (violins per SPD cohort and mode, Depth by Throughput per model, Column Comparison always visible with its empty state, ID-free charts one line per model with the detector note, no saturation sentence, phone layouts), B6 amount select and column order, D8 at read time (dedupe with the mockup's key, keeping a usable copy, then the larger lab's, which inherits only the LC column from a dropped copy when it records none; stored amount > 5,000 ng held back; nothing stored changes), bug 21. Methods says the row checksums are stamped, not computed, so they do not prove which library a run searched. PEG Watch, the TIC overlay and the lab trend are byte-identical to 1.2.3. Not in P2a: the sticky bar and unified B2 key and the lab-trend rebuild (P2b); the lookup and TIC summaries (P2c); the file-name part of the D8 amount check (needs `run_name`, server-side).
  - *Status 2026-09-30, P2b (filter bar and lab trend):* relay (Space) 1.4.0 / STAN 1.2.13, see CHANGELOG 1.2.13. Done: the sticky filter bar (§A.1 item 3) with QC standard · DIA / DDA / Both · instrument model · gradient · amount · column, the runs in view and a reset; at phone width a one-line summary and a Filters button; cascading menus with counts; `scroll-padding-top` = the bar's height, so the nav anchors land below it; no persistence (none asked for), no browser storage. The B2 key (model × mode × gradient × amount bucket, `lc_class`/`grad_label`/`abucket` ported) for the reference cards (titled by gradient), Best Configurations, the violins, Column Comparison, the lab trend and the table (new Cohort column, percentile within the cohort; under a column filter the cohort names the column). Every option in the bar counts the view picking it leads to, cascade included (`resolveView()`, shared with `setView()`). nanoLC is named by the gradient its SPD implies, then the run length (B2 amended above). A cohort is ranked with 5+ runs and its LC known; unranked ones are folded with the reason and left out of rankings (Best Configurations' minimum rises from 3 to 5). Every panel badges what it follows; Depth by Amount ignores the amount, Depth by Throughput and Throughput vs. Quantitation Quality the gradient, Column Comparison the column, and each says so; a change re-renders only the panels that follow it. The per-chart amount selects and the DIA / DDA / All tabs are views of the bar; the reference cards' family and mode checkboxes are gone into it. B3: the lab trend, reference = same cohort without the lab (and never 'Anonymous Lab') as p10/p25/p75/p90 bands, own baseline median ± 3 robust SD (1.4826 × MAD) of the first 30 runs (drawn from 20, provisional until 30), last-15 median, "No other lab in this cohort yet", opens on the most recent lab, peptides replace proteins. The TIC overlay (code and card, with its own menus) and PEG Watch are byte-identical to 1.3.0, pinned by hash; §A.4's "DIA/DDA follows the page filter bar" for the TIC is left to P2c with the TIC summaries. Not in P2b: the lookup and TIC summaries (P2c), schema (P3), IPS display.
  - *Status 2026-10-01, P2c (the lookup):* relay (Space) 1.5.0 / STAN 1.2.14, see CHANGELOG 1.2.14. Brett's decision (2026-10-01): **matching searches only.** "Where does my run sit?" holds `#where` under the filter bar, with the reference ranges right below at `#ranges`. A count is placed in its B2 cohort only from the community search: DIA-NN 2.3.x with the frozen community library at 1% run-level FDR, without MBR (alone, or with other runs and MBR off, with a note); DDA: Sage 0.14.x with the frozen FASTA at 1% PSM FDR. Percentile = mid-rank in the cohort, plus the median, middle half, n runs · n labs, the single-lab tag, a strip of every run, and the P2b rules (none below 5 runs, values below 10, unranked and missing cohorts explained). Every other search (other engines and versions, library-free or predicted, lab-built including `instrument_library.parquet`, the other vendor's frozen library, MBR, global 1%, any other FDR) is refused with what differs, the exact community command and the note that calibration is planned. **No scaling factor is shipped**: the mockup's "Example (preliminary)" 2.7.0 library-free path is a refusal. The search fields start unchosen; the run's fields follow the bar until set. An optional `report.log.txt` drop fills the fields and, for a one-file search, the count, parsed in the browser (FileReader, 2 MB cap, linear patterns, lines over 4,096 characters skipped) and checked against real 1.9/2.3.0/2.3.2/2.7.0 logs (`tests/fixtures/diann_logs/`). From a log the frozen library is recognised by file name and loaded size (53,580 / 170,284 precursors), not by checksum, and `--predictor` with any library is refused; typed-in details are labelled self-reported. The lookup quotes no conversion-like magnitude. Nothing typed or dropped is sent, stored or put in the address. TIC overlay, PEG Watch, the P2a read-time rules and the B2 key are byte-identical to 1.4.0. Not in P2c: the TIC summaries and "DIA/DDA follows the bar" for the TIC (§A.4 items 2–8), Part B's calibration (P5), schema (P3).
  - *Status 2026-10-05, P2 TIC (the TIC overlay, §A.4 items 1–8 and B5's page weight):* relay (Space) 1.6.0 / STAN 1.2.15, see CHANGELOG 1.2.15. The page loads `/api/tic-summary` (87 KB raw, 30 KB gzipped on the 2026-09-29 snapshot) instead of `/api/tic-overlay` (8.7 MB raw, 3.1 MB gzipped); the default load went from 5.02 MB to 1.96 MB on the wire. One summary per QC standard × mode × SPD × LC class (plus "all LC" where an SPD holds more than one): runs, identified-ion traces, labs (`labCount`), instruments, the stored run lengths, the median time axis and p10–p90 at the same minute (each trace interpolated onto the axis; a minute needs half the runs and at least 5), and each run on its own axis under 5 runs. Built once per data refresh from the `/api/leaderboard` rows through a Python port of the page's read-time rules (dedupe, hold-back, `lcClass`), checked against the page's own JS on the snapshot (3,305 → 3,061 kept → 3,059 usable, 3,023 traces; 100 SPD Evosep 626). `/api/tic-traces` serves one cohort's runs, fetched only when "show all traces" is ticked; it draws every run of the cohort, where the mockup sampled 40. `/api/tic-overlay` is kept. DIA/DDA and the QC standard follow the bar ("Both" shows DIA, "All standards" the largest standard, each said in the badge); the SPD and LC menus, per-run peak scaling, bands, show all traces, Plotly interaction and ⛶ are kept. Labelled "MS1 total-ion chromatogram from the raw file"; opens on the largest cohort; "(no bands)", "All" names its mix, nanoLC by gradient then run length; take line "626 runs · 1 lab" with the single-lab tag; the DDA empty state turns its controls off and speaks once. Review fixes before the deploy: an outage (no table) is a 503 the panel reports as such, and a failed `/api/leaderboard` settles the panel instead of "Loading"; the bar's Reset also returns the TIC to its largest cohort; leaving ⛶ restores every chart's height and width (all 12 charts, 1280 and 400 px); the port reads run dates as V8 does (`+0700`, rolled-over days, 24:00) and trims LC names as JS `trim()` does, so TIC counts never exceed the page's. PEG Watch, the lookup and the P2a/P2b rules are byte-identical to 1.5.0. Not done here: B5's phone-sized `<details>` per chart.
- **P3: schema.** B4, the facility id, optional run_name, and the nanoLC key (`lc_model`, `lc_flow`, gradient bands; decision 12). Brett's decisions of 2026-10-05 split it into P3a (capture + accept, forward only), P3b (read-time amount/FAIMS check), P3c (facility id) and P3d (nanoLC band key, page only); the Hive backfill of `lc_model` / `faims` from UC Davis raws moved to P4 (gated).
  - *Status 2026-10-05, P3a (capture and accept, new runs only):* STAN 1.2.16 / relay (Space) 1.7.0, see CHANGELOG 1.2.16. Four per-run fields are stamped at ingest (watcher, Hive `hive-process`/`step_extract`, `run_one_v1`) and sent with every submission: `lc_model` (canonical name read from the raw file: Thermo DriverIds, Bruker HyStar method; `detect_lc_model`), `lc_flow` (nano | capillary | micro from `stan setup` / `add-watch --lc-flow` / dispatch.yml), `amount_source` (declared | parsed | assumed; per-run declaration > unit-anchored file name > instrument default > 50 ng, `stan/community/amount.py`) and `faims` (a `cv=` in the Thermo scan filters; Bruker false). A declared/file-name conflict, a file name with two amounts, or an amount above 5,000 ng is never sent: `submission_readiness` holds the run as needs_metrics with the reason, and `submit_to_benchmark` refuses it. Rows stored before 1.2.16 send `amount_source` derived from the file name and the other three as not recorded. The relay stores them as optional columns (unknown enum values → '', `faims` bool/null, `lc_model` mapped onto the same canonical vocabulary as the client, a copy kept identical by a test, anything unrecognised → '', so a free-text spelling can never become its own cohort), outside the v1 gate; `/api/update` may patch them but refuses an unusable value with 422 and changes nothing. The page does not read them yet. Deploy order: edit dispatch.yml only after Hive is on ≥1.2.16; remove `lc_flow`/`amount_ng` from dispatch.yml before any rollback below 1.2.16. SQLite gains the four columns (`faims` INTEGER 1/0/NULL); PG gets them by the owner migration `migrations/2026-10-05_runs_lc_faims.sql`, which Brett applies when convenient: until then `insert_run_pg` drops keys the live table lacks (read from information_schema, logged once). `run_one_v1` now reads `lc_system` / `lc_model` from the raw file, with the synced instruments.yml as the fallback. Nothing stored was changed; `compute_cohort_id` and the broad collapse in `normalize_v1` are untouched. The mockup v3.1 sources (`docs/community-redesign/mockup/`, nanoLC bands) are on main from this change, for P3d. Known gap: Hive has no `fisher_py`, so Hive-ingested Thermo runs stamp `faims` NULL (as `ms2_analyzer` is already 'unknown' there); P3b's file-name hint covers them at read time.
  - *Status 2026-10-05, P3b (read-time amount and FAIMS check):* relay (Space) 1.8.0 / STAN 1.2.17, see CHANGELOG 1.2.17. Brett's decision 3: a run whose file name states a different amount than the stored one is held back, and FAIMS is marked from the file name; nothing stored changes. `_leaderboard_frame` (which feeds `/api/leaderboard` and the TIC summaries) works out three public fields from the private `run_name`, which never leaves the server: `amount_check` ('mismatch' when the name states an amount and the stored one, from above 0 to 5,000 ng, disagrees: another amount, two amounts, or a unit typo above 5,000 ng; the parser is a copy of `stan/community/amount.py`, one fixture list tests both copies), `faims` (stored, else true from the file-name hint, else null) and `faims_source` (stored | filename | ''). The hint is the token "Faim"/"Faims" on its own or joined CamelCase after a letter with a capital F, not followed by a lower-case letter except "pro", not negated ("no_FAIMS", "FAIMS_off"), never on a timsTOF; a CV alone is not read; it lives in the relay only. The page's `isHeldBack` also holds back `amount_check == 'mismatch'`, with the Python `_page_held_back` changed in lockstep and checked against it, so TIC counts never exceed the page's; the dedupe's choice of copy ignores `amount_check` (still not flagged, not above 5,000 ng, as in 1.7.0), so it never swaps in an older seed copy, and the acquisition is held back as a whole. The stats row counts "amount unconfirmed" apart from the >5,000 ng hold-back. FAIMS joins the B2 key (`rowKey`, '|faims' appended, so other keys are unchanged) and every title built from it adds "· FAIMS"; no FAIMS filter (decision 9), and the lookup, which has no FAIMS field, compares with runs acquired without FAIMS and says so. The submissions table marks "assumed" amounts (`amount_source`) and lists the held-back runs in view under it, each with its reason; chart hovers say "amount not recorded" (open-circle point) instead of a stand-in 50 ng. On the 2026-09-29 snapshot: 71 rows unconfirmed (69 runs after the dedupe), 15 marked FAIMS (13 Lumos, 2 Exploris 480), stats tile 3,035 → 2,966, TIC traces 3,023 → 2,954; on the live table of 2026-10-06 (3,393 rows) the same 71 / 69 / 15, stats 3,112 → 3,043, TIC traces 3,100 → 3,031; in both the TIC opens on 60 SPD instead of 100 SPD Evosep. The P2a rule and lookup pins keep their fc5cb33/ede086b values with P3b's listed edits put back before hashing; TIC and PEG are byte-identical to 1.7.0. Not in P3b: correcting stored amounts or writing `faims` (P4), the facility id (P3c), the nanoLC band key (P3d).
  - *Status 2026-10-06, P3c (facility id, and the token on submit):* relay (Space) 1.9.0 / STAN 1.2.18, see CHANGELOG 1.2.18. Brett's decision 1: UC Davis is one facility with an opaque id. `identity/facilities.json` in the dataset maps an id (`f` and a number, never a name) to the lab names one facility submits under and to the window in which it submitted as 'Anonymous Lab'; the admin writes it with `scripts/set_facilities.py` (dry run by default, the relay's own parser, refuses a file with any problem) from `scripts/community_facilities.json`: `f1` = "Clogged PeakTail", "Clogged Peaktail", "CloggedPeakTail" (matched exactly as the claim registry matches names: canonical form, case and spaces kept; a run sent under one without its token, `name_verified` false, is not stamped; a key given twice makes the file unreadable) and 'Anonymous Lab' submitted 2026-04-30T23:29:00Z–2026-05-01T18:27:00Z, both ends included (the 127 rows of STAN 0.2.282–0.2.290; the last at 18:26:10.097Z). It is kept apart from `claims.json`, and the relay only reads it (5-minute cache; no file, or one not yet readable, means no facilities and today's counting; an unreadable file keeps the last good copy). `_leaderboard_frame` adds a public `facility` (the id or ''). `labCount` and its Python port `_page_lab_count` (the TIC summaries) count facilities (an unstamped run under a name the table's stamped rows carry counts with that facility), a row without one by its lab name, and never 'Anonymous Lab' as a second lab, checked against each other alone and through the TIC summaries; the TIC cache key includes the records. `/api/claim-name` refuses a name that differs from another email's claimed name only in case or spacing. The stats tile reads "Contributing facility/facilities" with "under n lab names" once a record covers the names, else the 1.8.0 wording; a glossary line defines a lab as a facility; the Join card says the code publicly links a facility's names; the lab trend never draws the selected lab's own facility, under another name, as "other labs". On the live table of 2026-10-06 (3,393 rows) all rows are `f1` and every lab count is 1 before and after; only the tile's wording moves ("1 Contributing lab · under 2 lab names" → "1 Contributing facility · under 2 lab names"). `/api/submit` now checks `X-STAN-Auth` with `_peg_identity`, as PEG and `/api/update` do (it called an undefined helper before, a 500): a claimed name with its token is stored `name_verified`, with another token refused (403); an unclaimed name or no token is accepted unverified (grace period kept). STAN 1.2.18 sends the token by default (`STAN_SEND_AUTH=0` opts out) and, on an unhandled 500 from a relay before 1.9.0, sends the run again without it and leaves it off for an hour; checked against the real 1.8.0 relay. The P2a rule, lookup, TIC and PEG pins are unchanged: P3c edits none of their regions. Nothing stored changes.
- **P4: gated data work.** Each item needs Brett's go after a before/after:
  - the D4 parquet scrub;
  - D8 dedupe and `is_flagged` written to storage;
  - the IPS key fix plus recompute;
  - Exploris recalibration.
- **P5: Part B live**, once the calibration passes §B.6.

## A.7 Implementation notes

- Port from the mockup template:
  - the cohort key (`lc_class`, `grad_label`, `abucket`);
  - the lookup (`wsForm` / `wsResult`);
  - the TIC summary builder (`summarise` in `build_mockup.py`, with the §A.4 fixes);
  - the strip plot.
- Keep Plotly for the ported charts, so live interaction survives. The mockup draws SVG only because it is static.
- Tests:
  - extend `tests/test_relay_*` for the new endpoints and D1/D4;
  - add a render check for the page (pattern: `scripts/render_check.js`);
  - add a test that no public response carries `run_name` or `fingerprint`.
- Deploy: `python scripts/deploy_hf_space.py`. It has the base-hash and sync-window guards and ships the pinned Dockerfile in the same commit. Bump `SPACE_VERSION`. Check `curl -s https://brettsp-stan.hf.space/api/version`.

---

# Part B: Search-aware precursor lookup

## B.1 Problem

A visitor types a precursor count into "Where does my run sit?". Every STAN cohort number comes from **STAN's standardized search**:
- DIA-NN **2.3.0** (Hive: `/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif`);
- a **frozen empirical HeLa library per vendor** (`stan/search/community_params.py` ~102–130; timsTOF ~54k, Orbitrap ~170k precursors; `SEARCH_PARAMS_VERSION` v1.0.0);
- count = unique `Precursor.Id` with run-level `Q.Value ≤ 0.01`.

The visitor's number usually comes from another engine, version and library. Depending on the setup, that can be 30–50% or more off for the same raw file. Comparing it directly is wrong.

**Target quantity.** For the visitor's run, estimate *S*: the count STAN's standardized search would report on the same raw file. Show it with an honest uncertainty range. Place the whole range, not a point, in the matched cohort.

**What STAN's data says about the shape.** From the D7 measurements, library coverage (n_precursors / library size) has:
- a median of **70.1% on timsTOF**, maximum 91.4%;
- 16.9% on Lumos;
- 13.7% on Exploris.

So on timsTOF, STAN's count **saturates against the frozen library**, while a library-free count does not. A single ratio, or a straight line, is wrong near the top. On the Orbitraps, coverage is low and the relation should be close to a power law.

## B.2 Form fields

Added to the lookup, below the precursor count.

| Field | Options | Required |
|---|---|---|
| Search engine | STAN standard search · DIA-NN · Spectronaut · Other | yes |
| Version | Only calibrated versions are selectable. Others are listed as "not calibrated yet" | yes (DIA-NN, Spectronaut) |
| Library | Library-free / predicted (DIA-NN `--fasta-search --predictor`; Spectronaut directDIA) · Project / empirical library · STAN community library | yes |
| Searched with other runs? | Single run (no match-between-runs) · Batch with MBR: 2–10 · 11–50 · >50 runs | yes |
| Precursor FDR | 1%, run-specific (DIA-NN `Q.Value`, Spectronaut run-wise) · 1%, global / experiment-wide (`Global.Q.Value`) · other | yes |
| What was counted | help text: "unique modified sequence + charge (DIA-NN Precursor.Id / Spectronaut EG.PrecursorId)" | — |

"STAN standard search" means no scaling. The count is already on STAN's scale.

## B.3 Model

For each **calibrated configuration** *c* and **instrument model** *m*, fit on paired searches of the same raw file.

- *N* = the count from configuration *c*;
- *S* = the count from STAN's standard search;
- *L* = the size of the frozen library for *m*'s vendor (read the exact count from the speclib).

**Primary form: power law** (revised after the research, §B.10):

```
ln S = a_cm + b_cm · ln N + ε
```

The first draft proposed a logit-coverage model, `logit(S/L) = a + b ln N`, so that predictions could never exceed the library size *L*. On the existing paired searches, the power law beats it: median error 1.74% vs 3.13% on the timsTOF full library, and 2.73% vs 6.00% on 2.6.1 empirical. The residuals also shrink near the ceiling. Keep the logit form only as a candidate for library-free timsTOF input. Cap Ŝ at *L*.

**Candidate forms, chosen inside every leave-one-out fit** (research critique M7): power law; logit-coverage; a Box-Cox λ fixed in advance by class. Use a one-SE rule, not "the lowest error wins".

Ship the simplest form that passes acceptance.

**Fitting details:**
- Regress S on N (the prediction direction), by ordinary least squares on the transformed scale.
- Check heteroscedasticity against ln N. If present, use a variance model or conformal intervals that are local in ln N (binned).
- A stratum is instrument model × configuration. If a model has fewer than 30 pairs, fit a vendor-level model with a per-model offset and a wider interval.
- Only instrument models with a STAN cohort need a model. Without a cohort there is no percentile to give anyway.

**Uncertainty:** split-conformal (or cross-conformal / jackknife+) on held-out residuals in logit space, at nominal **80%**. That gives [Ŝ_lo, Ŝ_hi].

**Placement:** percentile range [pct(Ŝ_lo), pct(Ŝ_hi)] in the matched B2 cohort. Draw the interval as a band on the strip plot, not a single dot.

## B.4 Refusal rules

Refuse rather than mislead. Each refusal names the exact route: "Run STAN on the raw file for an exact placement."

1. **Configuration not calibrated** (engine, version, library or MBR bin): show the typed number and "not on STAN's scale". Give no percentile. List the nearest calibrated configurations.
2. **Outside the calibrated range**: N beyond the panel's observed range for that stratum, by more than 10%.
3. **FDR not 1%**, or the FDR scope is unknown.
4. **MBR batch larger than the largest calibrated bin**, or a project library built from the same batch of runs.
5. **Instrument model without a STAN cohort**, or with only a vendor-level model that fails §B.6.
6. **"Other" engine** (AlphaDIA, MaxDIA, FragPipe-DIA-NN, …) until calibrated.

## B.5 Calibration panel: paired searches of Brett's HeLa QC

Brett, 2026-09-29: "I have tons of QC files that you can task my proteomics skill to search on hive to build this algorythm".

**Engine: the `ucdavis-proteomics-core-pipeline` skill** (plugin `ucdavis-proteomics-core`, v2.6.0).
- It pins exact DIA-NN builds (`PIN_ENGINE=diann PIN_VERSION=x.y.z bash scripts/acquire_tools.sh hpc`).
- It runs >5 files as a parallel SLURM chain.
- It records provenance.

Its golden rules apply:
- **one confirmation** of the resolved defaults before compute (decision 9);
- the organism is asked and confirmed: human, because these are HeLa;
- never hand-write the engine command;
- nothing heavy on login nodes.

**Must-checks before running.** These are known risks.
- **FRAN exclusion.** The skill deposits searches to FRAN. About 1,000 QC searches must **not** land in FRAN's customer corpus.
  - Write all outputs under `/quobyte/proteomics-grp/STAN/calibration/engine_scaling/<panel_id>/`. That is inside FRAN's `DEFAULT_EXCLUDES` prefix (`~/Documents/FRAN/ingest/find_uningested.py:143`).
  - **Disable the FRAN deposit** for these runs; find the skill's switch.
- **FASTA.** Use STAN's frozen community FASTA (md5-pinned) for every configuration, so the engine configuration is the only variable. Confirm how the skill takes an explicit FASTA.
- **Missing DIA-NN builds.** Hive has no internet egress. The skill says it fetches missing Academia builds, so confirm how. Fallback: download on the Mac and copy to `/quobyte/proteomics-grp/dia-nn/build_<nnn>/`.

**Raws.** Stratified from Flinders (`/nfs/lssc0/flinders/proteomics/Data/raw_data/`), chosen from PG by cohort and depth.
- Cohorts:
  - timsTOF HT: Evosep 100, 60 and 30 SPD;
  - Exploris 480: 44 min (~38 SPD) and 88 min (~19 SPD);
  - Fusion Lumos: 44 min (~32 SPD) and 88–96 min (~12 SPD).
- In each cohort, **12 fitting + 4 held-out** raws, spread across STAN depth deciles, including bad runs (clog, PEG, spray) so the low end is covered, and across time (different columns).
- ≈ 112 raws.
- **Pilot first:** HT Evosep 60 SPD and Exploris 44 min, 16 raws each, 3 configurations. It checks cost, FRAN isolation and model shape before the full panel.

**Configurations** (final list set by what the skill can acquire; see the research in §B.10):
- DIA-NN library-free / predicted, single run, 1% FDR: **1.8.1, 1.9.2, 2.0.x, 2.1.x, 2.2.x, 2.3.0, latest (2.6/2.7)**.
- DIA-NN with the **STAN community library**, the same versions. This is cheap, and covers labs that use STAN's library with another DIA-NN.
- MBR on, in batches of **10 and 40** raws, for the two or three most-used versions.
- Extract **both** run-specific `Q.Value` and `Global.Q.Value` ≤ 0.01 from the same searches. That calibrates both FDR scopes for no extra compute. Column names differ by version; see research.
- **Spectronaut 19/20 directDIA**, factory defaults, precursor q 1%, run-wise. **Brett runs it** on the licensed machine, on the same raw list, and exports a report with `R.FileName, EG.PrecursorId, EG.Qvalue` to `/quobyte/proteomics-grp/STAN/calibration/engine_scaling/<panel_id>/spectronaut_<ver>/`.

**Counting.** One extractor for every output. Unique precursor id (modified sequence + charge) at q ≤ 0.01, per raw, handling DIA-NN 1.8 `report.tsv` and 2.x `report.parquet`.

**Compute.** Estimate from the feasibility research (§B.10). Library-free searches are much slower than STAN's frozen-library searches. Run on `low` with `Requeue=1`, per the partition policy.

## B.6 Validation and acceptance, per stratum, before a configuration goes live

- **Grouped CV:** leave one instrument-month out, so near-identical consecutive runs cannot leak between train and test. Then a final check on the 4 held-out raws per cohort.
- **Proposed acceptance** (confirm the numbers after the pilot):
  - median absolute % error of Ŝ vs S ≤ **5%**;
  - 90th-percentile absolute % error ≤ **12%**;
  - empirical coverage of the nominal 80% interval within **75–85%**;
  - median percentile-placement error |pct(Ŝ) − pct(S)| ≤ **5 points**.
- A failing stratum stays "not calibrated". Refusal rule 1 applies.

## B.7 Data and publication

**File:** `calibration/engine_scaling.json` in the HF Dataset `brettsp/stan-benchmark`.

```json
{ "schema_version": 1, "panel_id": "2026-10-xx-a", "fitted_at": "…",
  "stan_reference": { "diann": "2.3.0", "search_params": "v1.0.0",
                      "libraries": { "bruker": {"md5": "…", "n": 0}, "thermo": {"md5": "…", "n": 0} } },
  "models": [ { "engine": "diann", "version": "1.9.2", "library": "free", "mbr": "none",
                "fdr_scope": "run", "instrument_model": "timsTOF HT", "form": "logit-log",
                "a": 0, "b": 0, "resid_q": [-0.0, 0.0], "x_range": [0, 0],
                "n_pairs": 0, "metrics": {"mdape": 0, "cov80": 0}, "status": "live" } ] }
```

- **Endpoint:** the relay serves it at `GET /api/scaling`, cached.
- **Computation:** the page computes Ŝ in the browser. "Nothing you type leaves your browser" stays true.
- **Versioning:** if STAN's reference changes (DIA-NN pin, library, `SEARCH_PARAMS_VERSION`), the target has moved. The **whole table is refit**, and the old one is kept with its `panel_id`.
- **New engine version:** add it to the panel configurations, run only the new searches, fit, validate, publish.

## B.8 What the visitor sees

One screen:

> You entered **40,000** precursors (DIA-NN 2.7.0, library-free, single run, MBR off, 1% run FDR).  
> On STAN's search of the same file that would be about **43,800** (41,500–46,000).  
> Within the **577 QC injections (failed runs included) from one timsTOF HT at UC Davis**, Evosep 60 SPD, that sits between the 56th and 85th percentile.  
> Calibrated on one instrument at UC Davis. A lower STAN number is expected on timsTOF: STAN's library holds ~51k precursors.

The example is consistent with the only existing pairs (2.7.0 library-free, no MBR: N/S ≈ 0.91 on 8 timsTOF raws). The final numbers come from the fit. Name the reference population (critique M11): one instrument's history, not a ranking of labs. Refusals use the §B.4 texts. Show a one-sided state ("at least the 90th percentile") when N lies above the calibrated range.

## B.9 Implementation units

| Unit | Does | Depends on |
|---|---|---|
| `stan/community/engine_scaling.py` | Pure functions: `fit(pairs, form)`, `predict(model, n)`, `conformal_interval`, `grouped_cv`, `export_json`. Fully unit-tested | — |
| `scripts/engine_calibration/select_panel.py` | PG (read-only) → stratified raw list per cohort and depth decile, as a manifest | PG |
| `scripts/engine_calibration/collect_counts.py` | Walks the panel outputs and counts precursors per raw × configuration, for DIA-NN 1.8–2.x and Spectronaut reports | panel outputs |
| `stan engine-calibration {select,collect,fit,publish}` in `stan/cli.py` | Wraps the above. Writes `~/STAN/logs/engine_calibration_<ts>.{log,jsonl}`, per the "new jobs publish their own logs" rule | above |
| Relay: `/api/scaling`, lookup form fields, client `predict` | §B.2, §B.8 | JSON in the Dataset |
| `docs/ENGINE_SCALING.md` | Method, acceptance, how to add a version | — |
| Tests: `tests/test_engine_scaling.py` | Synthetic saturating data recovers `a` and `b`; interval coverage; refusal rules; JSON schema | — |

## B.10 Research results (2026-09-29): these override §B.3–B.5 where they conflict

The research workflow finished. Its full output is in `docs/community-redesign/precursor-lookup/`:
- `inventory.md`: existing paired data;
- `sources.md`: DIA-NN and Spectronaut primary sources;
- `feasibility.md`: Hive feasibility;
- `RESEARCH_DESIGN.md`: an independent, more detailed design (644 lines, arms A–F, gates G1–G9, refusal rules R1–R9);
- `CRITIQUE.md`: an adversarial review, verdict **approve-with-changes**.

Raw tables are in `~/stan-handoff-2026-09-29/scaling/`. **Implement from RESEARCH_DESIGN.md as amended by CRITIQUE.md's M1–M11.** This doc's §B.1–B.9 is the summary.

**Findings that change the plan:**
1. **STAN's cohorts are not searched against one frozen library.**
   - `run_one_v1.py` and `stan/search/local.py` (~425–447) prefer a per-instrument subset library (`instrument_library.parquet`, built by `stan/library_builder.py` from the lab's own runs):
     - timsTOF: TIMS-10878, ~51k precursors;
     - Exploris: DESKTOP-FOT3DAA, ~53k, **not** the 170k Orbitrap library;
     - only Lumos uses the frozen `hela_orbitrap_202604` (~170k).
   - On the same raws, the full frozen library gives **1.034×** the subset on timsTOF, so the subset is not "speed-only".
   - An outside lab cannot reproduce S, and "run STAN on the raw file for an exact placement" is false today.
   - It also contradicts the D7 page text. **This is decision 11.**
2. **Paired data already exist in quantity.** 904 non-DDA HeLa raws have STAN's search plus at least one other configuration:
   - DIA-NN 2.3.0 vs 2.3.2;
   - community library vs subset library;
   - multi-run vs single-run;
   - DE-LIMP library-free + MBR;
   - 2.6.1/2.7.0 (8–35 raws each).

   The Excel QC log has 639 Lumos/Exploris raws with **DIA-NN 1.8** counts (2.0/1.8 = 1.12 on 20 raws). The panel can be much smaller: fill only the gaps (library-free, newer versions, single-file MBR on and off, out-of-lab raws).
3. **Count definition matters by 1–9%.** STAN counts unique `Precursor.Id` at `Q.Value ≤ 0.01`, without a Global.Q filter. That is 1–4% above DIA-NN's own `stats.tsv` "Precursors.Identified", and up to 9% on weak runs. **DIA-NN 2.5.0+ writes the main report at 5% FDR by default**, so counting rows gives about +28%. The form must ask.
4. **MBR on one file** is how DIA-NN's GUI runs by default. Without it, the first pass reads 0.80× STAN; with MBR or a two-step search it reads 1.03–1.05×. Split "single run" into MBR off and on (M1).
5. **Spectronaut depends heavily on experiment size.** PG/SN is 0.65–0.79 for multi-run directDIA and 1.56–1.74 for single-run. No version is recorded in the 76 HeLa exports found (204 runs, 83 matched to PG). **The Hive Spectronaut licence refuses Linux**, so Brett's Windows machine is the only route. Calibrate batches of 2–3 and 8–10 runs.
6. **Transfer between labs is unmodelled** (M4). Settings the form doesn't capture already move the same raw 1–5%. Add an **out-of-lab hold-out**: at least 8 public HeLa raws per model from PRIDE, downloaded on the Mac and piped to Hive. Until then, every result reads "calibrated on one instrument at UC Davis".
7. **Production parity** (M3). The reference arm must use the exact production command (`run_one_v1`: `--qvalue 0.01 --threads 8`, the sif, the fixed digest), not the pipeline skill. Only the visitor-configuration arms go through the skill.
8. **Freeze the acceptance gates before any fit data are seen** (M8), and gate each vendor separately (M9).
9. **Better input** (recommended). Let the visitor drop in DIA-NN's `report.log.txt` and `report.stats.tsv`. Parse them in the browser: the version banner and the command line fill every form field. Typed settings err by more than the model does.
10. **Data bug found:** 67 DDA-named Exploris raws are stored in PG as mode=DIA. They were excluded from the pairs; fix them in PG (gated).

## B.11 Open questions

1. Are 16 raws per cohort enough for per-model fits, or is a vendor-level fit with per-model offsets needed? Decide from the pilot's residual spread.
2. Empirical/project-library configurations cannot be reproduced in general, because the library comes from the visitor's own DDA or GPF runs. Proposal: calibrate one case, a GPF library built from Brett's own HeLa, and refuse the rest.
3. MBR effects depend on batch size and composition. Are the 10- and 40-run bins representative of how labs report QC? Otherwise refuse MBR.
4. Transfer to other labs' instruments of the same model: the calibration uses one facility's instruments. Widen the intervals, or add a note, until a second facility contributes paired searches.

---

# Part C: Other work in flight when this was written

This is not part of this design; it is here so it isn't lost across the restart. The worktree is `/Users/brettphinney/Documents/STAN-peg`, on branch `feat/peg-watch`, level with `origin/main` at v1.2.5.

- **Install docs refresh** (workflow `install-docs-final`, run `wf_c7ad8f5e-173`).
  - Uncommitted edits: README.md, `INSTALL_FOR_AGENTS.md` (new), `docs/INSTALL_MODE_B_LINUX.md` (new), INSTALL_MODE_B_WSL.md, INSTALL_MODE_C_HPC.md, HPC_PATHS.md, INSTALL_REGRESSION_CHECKLIST.md, user_guide.md.
  - Verify, then commit.
- **Installer bug fixes** (`installer-bugfixes`, run `wf_4ff01a72-aa0`).
  - Uncommitted edits: install_stan.ps1, update_stan.ps1, stan.bat, stan/setup.py, stan/cli.py, stan/config.py, stan/fleet_setup.py, stan/search/local.py, stan/dashboard/server.py (`_mirror_enabled`), and new tests (`test_dashboard_mirror_gate.py`, `test_init_and_add_watch.py`, `test_search_threads.py`, `test_windows_installer.ps1`).
  - After both are verified:
    - drop the doc workarounds that the fixes make unnecessary;
    - bump to 1.2.6, including both instrument `.ps1` `$ScriptVersion` values, and add a CHANGELOG entry;
    - commit only the relevant files and push;
    - deploy Hive (`git pull`) and Azure (server.py changed; run `check_jsx` first).
- **timsTOF PEG re-score dry run** (`timstof-rescore-dryrun`, run `wf_11bf0d90-c4e`). Show Brett the before/after. **No PG writes without his go.**
- **stan-peg-2 design** (`docs/superpowers/specs/2026-09-29-peg-detector-independent-design.md`, untracked). Direction approved. Do not commit it without asking.
- **Open items to raise with Brett:**
  - rotate the Slack webhook and signing secret (they were printed in a session);
  - free Mac disk space and move repos out of iCloud;
  - 576 Orbitrap runs with stale `raw_path`;
  - duplicate rows in PG.
