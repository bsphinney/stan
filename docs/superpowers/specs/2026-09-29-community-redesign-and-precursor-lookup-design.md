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
| 7 | Footer license wording | Recommended text, Brett to confirm | "Code: STAN Academic License (free for academic and non-profit use; commercial use by written permission)", linked to LICENSE. UC tech-transfer ownership is still open |
| 8 | One-facility disclosure wording | Recommended text, Brett to confirm | The mockup's line, see D2 below |
| 9 | Engine-calibration panel compute on Hive (Part B) | **One go/no-go**, required by the pipeline skill | See §B.5 |
| 10 | Spectronaut arm of the panel | **Brett runs it** on the licensed machine | See §B.5 |

---

# Part A: Community site redesign

The live page is one HTML/JS string inside `hf_space/app.py`, served by the relay (`SPACE_VERSION`, currently "1.2.1"). The mockup template is a *reference* for the new panels. Port its logic, not its markup wholesale. Remove its review layer, the pink annotations and the `window.__stanLookup` test hook.

## A.1 Page order

This is REVIEW §6, amended so that no chart is dropped.

1. **Header.** Purpose line: "Compare your QC HeLa against reference ranges from labs running the same frozen search." Then the one-facility disclosure (D2). Nav order: Join · Where do I stand · Methods · PEG Watch · Dataset · API · GitHub · Museum · Arcade.
2. **Stats row.** Runs · labs (counted as facilities) · instruments · latest run. A Join tile replaces the inert "Hide failed runs · 0 flagged" card. A three-line glossary (SPD, IQR, IPS) sits below.
3. **Sticky filter bar.** QC standard · DIA/DDA · amount. Every panel follows it and prints what it follows in its badge. A panel that deliberately ignores a filter says so ("all amounts").
4. **Where does my run sit?** The lookup (B1, with the Part B engine fields).
5. **Reference ranges.** Grouped by instrument model. The primary metric is large, each card shows "n runs · n labs", and sparse cohorts are folded (D5).
6. **Join the benchmark** (D6).
7. **How the numbers are made** (D7 + D3).
8. **Explore.** Every chart in §A.2, open by default at desktop width. On phones each is a `<details>` with a one-line takeaway. The TIC overlay, Depth by Throughput and Throughput vs. Quantitation Quality are open at every width.
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
  - nanoLC is named by the **stored** `gradient_length_min` plus the derived SPD ("44 min run (~38 SPD)").
  - A run with no LC recorded at an Evosep-method SPD is "LC not recorded", never inferred.
  - Reference: `lc_class()` in `build_mockup.py`.
  - The sticky filter bar drives every panel.
- **B3 Lab trend vs. reference.**
  - The reference is the same cohort **without** the selected lab, drawn as p10/p25/p75/p90 bands.
  - Add a drift overlay from the lab's own baseline (median ± 3·MAD).
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
- `run_name` optional at submit; never in public responses (D4).
- A **facility id**, so labs are counted as facilities. Two pseudonyms of one facility (Clogged PeakTail and Anonymous Lab are both UC Davis) must count as one. Proposal: attach it to the verified `community-claim` record.
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
- **P2: layout.** Page order, the B2 cohort key and sticky bar, B1 (standard-search only), D5, D6, D7, B5 (TIC summaries, `<details>` on phone), B6, B3.
- **P3: schema.** B4, the facility id, optional run_name.
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

**Primary form (logit-coverage):**

```
logit(S / L) = a_cm + b_cm · ln N + ε
Ŝ = L · expit(a_cm + b_cm · ln N)
```

The logit link turns the library ceiling into a straight line, so predictions can never exceed *L*. At low coverage (the Orbitraps), logit(s) ≈ ln(s), and the model reduces to the power law S ∝ N^b.

**Competing forms, chosen by the grouped CV in §B.6:**
1. Log–log linear: `ln S = a + b ln N`.
2. Logit-coverage (above).
3. A monotone spline in ln N with the logit link.

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

> You entered **61,200** precursors (DIA-NN 1.9.2, library-free, single run, 1% run FDR).  
> On STAN's standard search that is about **43,900** (41,800–45,700, 80% range).  
> **48th–63rd percentile** of timsTOF HT · Evosep 60 SPD · 50 ng (606 runs · 1 facility).  
> Scaled from 16 paired searches of UC Davis HeLa QC per cohort. For an exact placement, run STAN on the raw file.

The numbers above are illustrative. Refusals use the §B.4 texts.

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

## B.10 Research evidence

A research workflow (`engine-scaling-design`, run `wf_52331c88-823`) was gathering this when the doc was written. Its outputs land in `~/stan-handoff-2026-09-29/scaling/`:
- `inventory.md`: paired searches that already exist in PG, Hive, DE-LIMP or Spectronaut;
- `sources.md`: DIA-NN and Spectronaut version changes and benchmarks, with DOIs;
- `feasibility.md`: containers on Hive, runtimes, the measured library ceiling, and a panel proposal;
- `DESIGN.md`: an independent design, plus a critique.

**Fold these into §B.3–B.5 before the pilot.** In particular:
- the exact DIA-NN versions available;
- the q-value column per version;
- measured runtimes;
- any existing paired data that can seed or check the fit.

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
