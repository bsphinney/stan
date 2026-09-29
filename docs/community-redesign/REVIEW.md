# STAN community site: review and recommendations

**Site:** https://community.stan-proteomics.org (same app as https://brettsp-stan.hf.space)
**Reviewed:** 2026-09-29. The whole page was captured read-only on **relay 1.2.0** between 12:24 and 12:44 PDT. By 12:49 the relay was on **1.2.1**, and it was still 1.2.1 on both hosts when this report was written. Only the "Evosep vs other LC" card was re-captured on 1.2.1. The header, run_name and reference-card findings were re-checked live on 1.2.1 and have not changed.
**Line numbers** refer to `/Users/brettphinney/Documents/STAN-peg/hf_space/app.py` (vendored, `SPACE_VERSION = "1.2.1"`). I spot-checked every line cited below against that file.
**Evidence:** captured in a session scratch folder, now kept (not in git, ~66 MB) at `~/stan-handoff-2026-09-29/sitereview/`. Screenshot paths below are relative to it. `INDEX.txt` there maps every file.
**Method:** I captured desktop (1280 px) and phone (400 px) views and ran through every select, tab and chip. Three separate reviews followed: a new-lab visitor, a proteomics expert and a design pass. A critic pass then merged the three and removed duplicates. I sent no POSTs, deployed nothing and made no repo edits.

---

## 1. Summary

- **What works:** Evosep PEG Watch is the best-built part of the page. It states its sample size, gives three numbered join steps, lists exactly which fields are shared, and warns against comparing across detector families. The rest of the page follows the primary-metric rule throughout (precursors for DIA, PSMs for DDA, proteins as context). The engineering is solid: no JS exceptions, no overflow on phone, and every link returns 200.
- **Problem 1: the page doesn't say that all the data comes from one facility.** It says "community", "2 Contributing Labs" and "established by the community". In fact, essentially every run is from UC Davis: 3,178 of 3,305 rows are Clogged PeakTail, and the other 127 ("Anonymous Lab") are also UC Davis runs. An expert who spots this, from the "FL…" filenames in chart hovers, will stop trusting the rest of the page.
- **Problem 2: DIA and DDA are mixed, in breach of the project's own track rule.** Cohorts have no acquisition mode in their key. So DIA cards show precursor IQRs of "0 - 37,360", column bars plot PSMs against precursors, and the DDA view of Best Configurations ranks on an empty precursor column.
- **Problem 3: the IPS explanation describes the retired v1 score, and the colour bands mark whole platforms as failing.** 59% of HeLa DIA runs, and 96% of Exploris runs, get a red "Investigate" badge. Of the few things on the page a visitor can act on, this is the most misleading.
- **Also serious:** every run's raw filename is public (`run_name`, 3,305 of 3,305 rows). This contradicts the user guide ("No filenames") and the PEG card on the same page.

---

## 2. Do first (highest impact per effort)

### D1. Keep DIA and DDA separate in every panel *(small)*
- **Problem:** `broadId()` (app.py:3913, and a second copy near 5362) builds cohorts from model × SPD × amount only. Whether a cohort counts as DIA is then read from its first row (`const isDIA = subs[0]...`, app.py:3947, 5439). All 37 DDA rows (all Anonymous Lab, PepSep) therefore fall into DIA cohorts, where their precursor count is 0:
  - The "timsTOF HT · 100 SPD" PepSep card (n=29, 11 of them DDA) shows **Precursors (IQR) 0 - 37,360**. The 60 SPD card shows 0 - 40,416, and the Lumos 15 SPD card shows 0 - 39,659.
  - In Column Comparison, the HT 60 SPD PepSep bar reads avg 19,232. The DIA-only mean is 40,068. The HT 30 SPD PepSep bar (49,715) is five DDA PSM counts plotted as precursors.
  - Under the DDA tab, Best Configurations still says "sorted by precursors", every precursor cell is "—", and **"best depth" sits on 24,636 PSMs while row 3 has 52,085**. The cause is that `configSortCol` is initialised to `'precursors'` (4863) and never reset in `showTab()` (5680), and `bestDepth = rows[0]` (4953).
- **Change:** add mode to both `broadId()` keys (model|mode|spd|amount) and choose precursors or PSMs from each cohort's own mode. In `renderConfigLeaderboard`, switch the sort to `psms` on the DDA tab (and back to precursors on DIA), compute `bestDepth` as the maximum of the active metric, and hide the Precursors column under DDA. Under "All", show two tables rather than one mixed table.
- **Why:** Separate DIA and DDA tracks are a founding design rule, and "0 - x" on the first cards a visitor sees reads as broken data.
- **Evidence:** `shots/03_community_reference_ranges_p1.png`, `shots/i_bestconfig_under_tab_dda.png`, `shots/05_instrument_health_explorer_p3.png`.

### D2. Say on the first screen that this is one facility's data, and show "n runs · n labs" everywhere *(small)*
- **Problem:** The banner hard-codes "Seeded with **3,800+** longitudinal QC runs" (app.py:3190), while the stats row just below reads 3,279 (3,305 across all standards). "2 Contributing Labs" is really two UC Davis pseudonyms, and 46 run_names are listed under both. Section text says "established by the community" (3236) and "First-of-its-kind cross-lab TIC comparison" (3372). Twelve reference cards have n=1 and show IQRs such as "41,555 - 41,555". The TIC overlay draws percentile bands from 2 traces. "Best depth" (timsTOF HT) and "best accuracy" (Exploris) are cross-vendor verdicts drawn from one lab's three instruments, and a Thermo or Bruker shop will read them as advocacy.
- **Change:**
  - Put one computed line under the header: *"Today essentially every run here comes from one facility, the UC Davis Proteomics Core (timsTOF HT, Exploris 480, Fusion Lumos). The ranges below are that facility's longitudinal ranges until more labs join."*
  - Compute the banner count from the same array the stats row uses.
  - Add "n runs · n labs" to every card, Best Configurations row, TIC band and lab-trend annotation, and mark cohorts with fewer than 2 labs as "single-lab reference".
  - Drop "established by the community" and "cross-lab" until a shown cohort actually has 2 or more labs.
  - Below about 10 runs, list the values rather than an IQR.
  - Suppress the "best …" badges until a row has 2 or more labs.
  - Do not rename or merge the "Anonymous Lab" pseudonym. Remove its exact-duplicate rows instead (D8).
- **Why:** The PEG section already handles this well ("One lab shares in this cohort so far, so the band is that lab's own week-to-week spread"). A skeptical staff scientist will accept a candid single-lab reference, but not one presented as a community consensus.
- **Evidence:** `shots/00_header.png`, `shots/01_stats.png`, `shots/07_evosep_peg_watch_p1.png` (the model), `data_summary.txt`.

### D3. Replace the IPS card with the IPS actually computed, and stop painting typical runs red *(small, plus a calibration decision)*
- **Problem:** The card (app.py:3540-3556) gives the retired v1 formula: "30% precursor depth · 25% spectral quality · 20% sampling · 15% quant coverage · 10% digestion", with hyperscore for DDA. The relay computes v2, `0.50·s_precursors + 0.30·s_peptides + 0.20·s_proteins` within (family, SPD bucket), where a median run = 60 (docs/ips_metric.md). Badges (`ipsBadge`, 5715-5719) show "<60 Investigate" in red.
  - On live data, 59% of 3,242 HeLa DIA runs score below 60 and 1.1% reach 90.
  - Exploris has a median of 38, and 96% of its runs score below 60. At 38 SPD (578 runs) and 19 SPD (272 runs) the median is 37, and effectively every run is under 60.
  - So the April-2026 Exploris references no longer centre on 60. A visitor with an Exploris will conclude that the platform is failing.
  - The "Health: IPS, missed cleavages, charge distribution" line (3572) names two metrics the page never shows, and calls a depth score a health metric.
  - "Proteins — not used for ranking" contradicts proteins' 20% weight in IPS.
  - The "Identification Depth vs. IPS" scatter plots IPS against one of its own inputs, so it shows diagonal bands and nothing else. Its legend also lists Astral, although no Astral data exists.
- **Change:**
  - Rewrite the card in two sentences: *"IPS places a run's depth within its own instrument + SPD cohort: 60 = the cohort's median run, 90 = top 10%. Weighted 50% precursors (PSMs for DDA), 30% peptides, 20% proteins; calibrated on 359 UC Davis runs, April 2026."*
  - Use the band table that docs/ips_metric.md already defines (90-100 top decile, 70-89 above median, 55-69 around median, 40-54 below, 25-39 poor, 0-24 bad), or the plainer labels "top 10% / above median / below median / bottom 10%". Either way, the page and the doc should agree.
  - Reword the proteins line to "context only; not used for leaderboards; 20% of IPS".
  - Remove IPS from the "Health" line.
  - Delete the IPS-vs-precursors scatter.
  - **Your decision:** recalibrate the Exploris references (`IPS_REFERENCES` in stan/metrics/chromatography.py and the matching block in app.py). Relabelling alone won't fix this, because under the doc's own table a typical Exploris run still reads "Poor — investigate". Until the references are recalibrated, hide the coloured badges on the public page, or print the reference n next to each score.
- **Why:** IPS is the one number on the page that tells a visitor "act on this", and at the moment it gives them the wrong message.
- **Evidence:** `shots/08_understanding_the_metrics.png`, `shots/05_instrument_health_explorer_p2.png`, `shots/09_community_submissions_p1.png`.

### D4. Stop publishing raw filenames *(small for the API and hovers, medium for the parquet scrub)*
- **Problem:** `/api/leaderboard` returns a non-empty `run_name` for all 3,305 rows, and three chart hovers print it (app.py:4086, 4390, 4579). The published names include operator initials and notes ("stilcrap", "haspolymer", "testclog", "mascalbutfailsystcal", "KerryCol", "_MK_"). This contradicts docs/user_guide.md:244/246 ("No filenames. STAN strips raw filenames before submit") and the PEG card on the same page ("File and sample names … never leave your lab"). The client's `STAN_STRIP_RUN_NAME` opt-out (stan/community/submit.py:188-196) cannot work either, because the relay's required-field check (app.py:988-1011, `if not v`) rejects an empty run_name. The shared "FL…" prefix also links the two pseudonyms to each other.
- **Change:** keep run_name on the server only, for dedupe and `/api/update`.
  - Remove it from the `/api/leaderboard` and `/api/cohorts` responses and from all three hovers, which can show instrument, date and SPD instead. `run_date` is populated on every row, so nothing on the page needs the filename.
  - Make run_name optional in `V1_REQUIRED_DIA_STR`/`DDA_STR`, or accept a hash.
  - Then rewrite the historical dataset parquet without the column in one HF commit.
- **Why:** A core whose filenames contain customer or project IDs will not join once it sees this. The site also breaks a privacy promise that is written down in two places.
- **Evidence:** `data_summary.txt` ("rows exposing a non-empty run_name: 3305 of 3305"), `api_leaderboard.json`.

### D5. Clean up the reference cards *(small)*
- **Problem:** `colKey()` (app.py:3924) treats the literal column "Unknown" (3,178 rows) as a real column.
  - Every major cohort therefore appears twice, once as a gold "Unknown Unknown" card and once as a PepSep card. Lumos 30 SPD appears as n=407 "Unknown Unknown" and n=409 "All columns combined", with identical numbers.
  - Titles contradict subtitles ("60 SPD" over "46-60 SPD").
  - All 31 cards sit in one flat grid: 2,578 px tall on desktop, 8,824 px on phone.
  - The same "Unknown" key drives Column Comparison. Its caption says "only the column differs", but every PepSep bar is Anonymous Lab (2018 to Mar 2026) and every "Unknown" bar is Clogged PeakTail (to Sep 2026). What it really compares is two labs from two eras.
- **Change:**
  - Make `colKey()` return '' for "unknown" or empty, so unlabelled runs get only the family card.
  - Group the cards under instrument-model subheadings, collapsible on phone.
  - In each card, show the primary metric (precursors for DIA, PSMs for DDA) as the large number, with peptides and proteins smaller.
  - Fold cohorts under n=5 behind "Show N sparse cohorts".
  - Show the observed spread as "gradients seen: 46–60 SPD" rather than as a second title.
  - Hide Column Comparison until a cohort has at least 2 *known* columns (it already has `setVisible(false)`), and print n labs and the date span under each bar when it does show.
  - Longer term, have `stan setup` ask for the column so the field stops defaulting to "Unknown".
- **Why:** These cards are the first content on the page, and they are where a visitor decides whether the data is sound.
- **Evidence:** `shots/03_community_reference_ranges_p1.png`, `shots/03_community_reference_ranges_phone_p1.png` … `_p11.png`, `shots/05_instrument_health_explorer_p3.png`.

### D6. Add a "Join the benchmark" card and correct the license line *(small)*
- **Problem:** The only way into the benchmark is the banner link "Install STAN to contribute your own", which goes to the GitHub root (app.py:3191). The page never says what a lab has to do, what gets shared, or when its data will appear. The footer says "Code: **MIT**" and links to opensource.org (app.py:3629), but `/Users/brettphinney/Documents/STAN/LICENSE` is the **STAN Academic License**: "Any use of the Software for commercial purposes requires prior written permission". A CRO or pharma core reading the footer would be misled.
- **Change:** add a card modelled on the PEG "Put your lab on the board" card (app.py:3500-3510):
  1. Install STAN and inject Pierce HeLa (88328) as your QC standard.
  2. Run `stan community-claim` first, so nobody else can take your pseudonym.
  3. Set `community_submit: true` in `~/.stan/community.yml`, or run `stan submit-all`.

  Add the exact list of per-run fields that are published, what never leaves the lab, and "appears after the nightly rebuild". Make "Join" the first nav item, and put a link to it in the slot of the inert "Hide failed runs · 0 flagged" card. Change the footer to "Code: STAN Academic License (free for academic and non-profit use; commercial use by written permission)", linked to LICENSE. Please check that one-line summary yourself, since UC ownership is still to be confirmed with tech transfer.
- **Why:** Getting labs to join is the point of the site, and at the moment the page makes it hard.
- **Evidence:** `shots/00_header.png`, `shots/07_evosep_peg_watch_p2.png` (the pattern to copy), `shots/10_footer.png`.

### D7. State exactly how the numbers are made, including the per-vendor libraries *(small)*
- **Problem:** "Why This Benchmark Works" says every submission searches the same "…**predicted** spectral library" (app.py:3566). In fact, stan/search/community_params.py:102-130 defines two **empirical** HeLa libraries of very different size: timsTOF about 54k precursors, Orbitrap about 170k. Median `library_coverage_pct` is 70.1% on timsTOF (maximum 91.4%, against the code's 0.90 saturation threshold), 16.9% on Lumos and 13.7% on Exploris. The best of 1,342 timsTOF DIA runs is 49,341 precursors. The page also never states the FDR, the engine versions, or what counts as a precursor. `diann_version` 2.3.0 and the md5s are on every API row but appear nowhere on the page.
- **Change:** add a "How the numbers are made" card covering:
  - DIA-NN 2.3.0 (DIA) and Sage 0.14.x (DDA), plus SEARCH_PARAMS_VERSION;
  - the q-value level and column behind each count (confirm against stan/metrics/extractor.py when writing the text);
  - the charge, length and missed-cleavage limits;
  - links, with md5, to the FASTA and both libraries in the Dataset.

  Replace "predicted" with "empirical HeLa libraries, one per vendor (timsTOF ~54k, Orbitrap ~170k precursors)". Put a PEG-style caveat on the platform violins and Best Configurations: *"Counts compare within a vendor; across vendors the search spaces differ."* Show library coverage on timsTOF cards and mark cohorts above 90% as library-limited.
- **Why:** "Precursors at what FDR, searched how, against whose library?" is the first question an expert asks. The cross-vendor rankings are also not like-for-like, and the page should say so before a reader works it out for themselves.
- **Evidence:** `shots/08_understanding_the_metrics.png`, `api_leaderboard.json`.

### D8. Three cheap data-hygiene fixes *(small)*
- **Problem:**
  - Two unit-parse errors create a fake high-load cohort: `k562100ng_…` is stored as **562,100 ng** and `FL030921_HeLa100ug_…` as **100,000 ng**. Together with four other rows they make the "Lumos · 15 SPD · >600 ng (1000-100000 ng)" card.
  - `is_flagged` is false on all 3,305 rows, so "Hide failed runs · 0 flagged" does nothing.
  - There are 39 exact-duplicate row groups. For example, `FL270925_HeL50-DeepClean_120m_1.raw` and `…_1-goo.raw` both have 55,533 precursors, and the same run appears twice in the table as the 82nd and 75th-percentile rows.
  - The Depth-by-Amount caption says "Saturation typically appears between 100–250 ng on Orbitrap" (app.py:3270). 99% of rows are at 50 ng, so the data cannot show that.
- **Change:**
  - Set `is_flagged` for implausible amounts (> 5,000 ng, or a unit-parse mismatch).
  - Remove duplicates at consolidation, keyed on fingerprint plus counts (not run_name, which leaves the public API under D4).
  - Delete the saturation sentence until the data can support it.
- **Evidence:** `data_summary.txt`, `shots/09_community_submissions_p1.png`, `shots/i_stats_hidefailed_off.png`.

---

## 3. Bigger improvements

### B1. A "Where does my run sit?" lookup at the top of the page *(medium)*
A visiting core director arrives with one question: *"I get 38k precursors on a 60 SPD timsTOF HT at 50 ng. Where is that?"* At the moment the only way to answer it is to scan 31 cards, 11 ranking rows and 15 charts, each with its own buckets. That is about 13,800 px of page on desktop and 25,400 px on phone.

Add a small client-side form with these fields: instrument model, DIA/DDA, Evosep method or gradient minutes, amount, and the primary count (precursors or PSMs, optionally peptides). It should return:
- the percentile within the matched cohort (B2), with "n runs · n labs";
- a strip plot with the user's value marked;
- library coverage for timsTOF.

State "nothing leaves your browser". Add a link: "track this over time → Join (D6)". When no cohort matches: *"Your configuration isn't in the benchmark yet — join to create it."* The data is already there: `/api/cohorts` returns per-cohort value arrays and `pctile()` exists (app.py:5704). By default, collapse the 31 cards to the selected cohort.

### B2. One cohort definition, one vocabulary, one global filter bar *(medium)*
The page buckets throughput four ways and load amount three ways:
- **Throughput:** `SPD_LABELS` (3889), `_configSpdTier` (4866; "30-60" excludes 60 SPD), `BUCKET_LABEL` (4757; "medium (16–40)"), and the Fingerprint `spdTier` (5565).
- **Amount:** "standard" means 50 ng in every select but 151-300 ng on the cohort labels (3887).

So a 60 SPD timsTOF HT at 50 ng gets a different n in every panel: 606 on its card, 617 in Best Configurations and the radar, 1,341 in Depth by Throughput, and the whole family in Lab vs Community. The table percentile uses the family-level `cohort_id` (5773), which pools HT, Pro and Pro 2, and under "All" it pools DIA with DDA as well. That is why a 53,187-precursor 12 SPD row reads "100th" directly below rows at 70th.

- Define the cohort once, shared by the JS and the relay: model × mode × gradient × amount bucket. Name the gradient by Evosep method for Evosep runs ("Evosep 60 SPD") and by minutes plus derived SPD for nanoLC ("30 min (~38 SPD)"), which matches the method-versus-throughput distinction the project already draws.
- Use that definition in every panel and print it in each panel's badge. Add a "Cohort" column to the submissions table so every percentile has a visible reference.
- Move the DIA/DDA switch from the bottom of the page into a sticky filter bar at the top, next to QC Standard and amount, and make every panel follow it.
- Add a three-line glossary under the stats row:
  - **SPD:** samples/day. For Evosep this is the method; for nanoLC it is 1440 / (gradient min × 1.25).
  - **IQR:** the middle half of runs.
  - **IPS:** one line from D3.

Evidence: `shots/04_best_configurations_hela_dia_sorted_by_p.png`, `shots/05_instrument_health_explorer_p2.png`, `shots/05_instrument_health_explorer_p5.png`.

### B3. Rebuild "Your Lab vs. Community" as "Lab trend vs. reference" *(medium)*
The band is mean ± 1/2/3σ over every run of the same instrument **family** (app.py:4356-4364). That covers every SPD from 7 to 200, with no model or mode match, and **includes the selected lab's own runs**. For Clogged PeakTail on Exploris, 1,008 of the band's 1,026 runs are its own. The ±3σ band spans 3,509 to 42,749 precursors, so it cannot flag anything. Levey-Jennings rules also belong to within-lab control, not an inter-lab SD.

The panel opens on "Anonymous Lab" (18 runs, 2021–2023). Its y-axis title is the raw field name `n_precursors`. Proteins is on the menu but Peptides and PSMs are not.

- Build the reference from the B2 cohort **excluding the selected lab**, drawn as p10/p25/p75/p90 bands. ID counts are skewed, so percentiles fit better than σ.
- When there is no other lab, say *"No other lab in this cohort yet — the band appears when one joins"*.
- Add a separate drift overlay built from the lab's own baseline: median ± 3·MAD of its first N runs, or a rolling window.
- Default to the lab with the most recent runs, replace Proteins with Peptides (and PSMs for DDA), and use readable axis titles.
- The stale empty-state bug (see §4) goes away with this rewrite.

Evidence: `shots/06_your_lab_vs_community.png`, `shots/repro_stale_labtrend_after_ecoli_roundtrip.png`.

### B4. Record where the load amount came from, and whether FAIMS was used *(medium; schema change across client, relay and dashboard)*
`amount_ng` is 50 on 3,273 of 3,305 rows, and that value is stamped at submission. Of the 19 runs with "1ug" in the name, 14 are stored as 50 ng. The **top four DIA rows in the table** (84,857 / 84,461 / 82,477 / 69,682 precursors, "Lumos · 9 SPD · 50ng") are `FL271022_FaimHe1ug_…`, which are **1 µg FAIMS** runs. They set the 85k Lumos whisker in Depth by Throughput and sit inside the "Lumos Deep (>2h) · 26-75 ng" card.

- Resolve the amount in the client from metadata first, then from a filename parse anchored on the unit (so "…He1ug…" reads as 1000 ng and "k562100ng" as 100 ng).
- Record `amount_source` (declared | parsed | assumed) and badge rows as "assumed 50 ng" in amount-matched views.
- Add FAIMS on/off as a cohort attribute.

Per the project's documentation rule, update the relay schema, `stan/community/submit.py` and the dashboard in the same change.

Evidence: `shots/09_community_submissions_p1.png`, `shots/05_instrument_health_explorer_p2.png`.

### B5. Trim the Explorer, load the TIC data lazily, and make the phone view usable *(medium)*
The Explorer is 15 charts: 5,200 px on desktop and 7,063 px on phone.

- **Keep:** the platform violins (with the D7 within-vendor caveat), Depth by Throughput, and the TIC overlay.
- **Drop:** the IPS scatter (D3).
- **Move the four ID-free date series** (mass accuracy, MS1 signal, dynamic range, points/peak) into a collapsed "UC Davis instrument history" block. They are one lab's instruments plotted since 2018, coloured by family, so they cannot show any single instrument's drift. MS1 signal compares absolute TIC across detectors, and timsTOF sits about 1.5 log units lower because of its detector, not its health. Show them per instrument, and carry over the PEG section's "do not compare across detector families" note.
- **Fingerprint radar:** it min-max-normalises cohort medians across mixed SPD tiers, gives proteins an axis equal to precursors, and its inverted MS1-ppm axis mostly ranks the analyser (medians: Exploris 0.50, Lumos 0.55, timsTOF 1.27 ppm). Restrict it to one vendor, or drop it.
- **Performance:** `/api/tic-overlay` is 3.02 MB gzipped (8.74 MB raw) of the 5.03 MB cold load, for a chart about 17,000 px down the page, and it makes the TIC chart ready only at 6.3 s on phone against 1.6 s for everything else. Fetch it with an IntersectionObserver as the card nears the viewport, or serve it per SPD bucket.
- **Phone:** render each Explorer chart as a closed `<details>` with a one-line takeaway, and fix the overlapping facet titles and clipped axis and radar labels.

Evidence: `shots/05_instrument_health_explorer_p4.png`, `shots/05_instrument_health_explorer_p5.png`, `shots/05_instrument_health_explorer_phone_p3.png`, `timing.json`, `network.tsv`.

### B6. Make Best Configurations a fair within-cohort table *(small to medium)*
The DIA view ranks a ≥250 ng Lumos cohort (#3) against 50 ng cohorts, and on phone only #, Instrument, SPD and Amount are visible, with the counts off-screen.

- Add an amount select that defaults to 50 ng.
- Add a Labs column. The "best" badges stay off until a row has 2 or more labs (D2), and the table carries the cross-vendor caveat (D7).
- On phone, put Precursors or PSMs directly after Instrument.
- The sort and `bestDepth` fixes are in D1.

Evidence: `shots/04_best_configurations_hela_dia_sorted_by_p.png`, `shots/04_best_configurations_hela_dia_sorted_by_p_phone_p1.png`.

---

## 4. Bugs and wrong numbers

All of these were captured on 1.2.0. The code references are from the 1.2.1 vendored app.py, where the same code is still present.

| # | Defect | How to reproduce / evidence | Fix |
|---|---|---|---|
| 1 | DIA reference cards show DDA rows as 0 precursors: "0 - 37,360", "0 - 40,416", "0 - 39,659" | Load page → Reference Ranges → PepSep timsTOF HT 100/60 SPD and Lumos 15 SPD cards. `03_community_reference_ranges_p1.png` | D1 |
| 2 | Column Comparison bars mix PSMs and precursors (HT 30 SPD PepSep 49,715 = DDA PSMs; HT 60 SPD PepSep 19,232 against a DIA-only mean of 40,068) | `05_instrument_health_explorer_p3.png`, `page_text.txt` lines 587-600 | D1, D5 |
| 3 | DDA tab: Best Configurations stays "sorted by precursors" (all "—") and puts "best depth" on 24,636 PSMs while 52,085 is in row 3 | Click **DDA** in Community Submissions, then scroll up to Best Configurations. `i_bestconfig_under_tab_dda.png`. app.py:4863, 4953, 5680 | D1 |
| 4 | Banner "3,800+" against 3,279 in the stats row (3,305 across all standards) | `00_header.png`, `01_stats.png`; app.py:3190 hard-coded | D2 |
| 5 | IPS card shows the retired v1 formula. Bands mark 59% of HeLa DIA runs and 96% of Exploris runs "Investigate" | `08_understanding_the_metrics.png`; app.py:3540-3556, 5715-5719 against docs/ips_metric.md | D3 |
| 6 | Both Matthews & Hayes 1976 links point to the wrong papers. app.py:3358 → 10.1021/ac50005a009 (Crossref: Hancock, *Low flux multielement instrumental neutron activation analysis in archaeometry*). app.py:3580 → 10.1021/ac50012a005 (Crossref: *Editor's Column*, 1977) | Checked against Crossref on 2026-09-29. The correct DOI is **10.1021/ac50003a028**: Matthews & Hayes, *Systematic errors in gas chromatography-mass spectrometry isotope ratio measurements*, Anal. Chem. 1976, 1375-1382 | Point both links at ac50003a028. The paper is a GC-MS isotope-ratio sampling-error study, so check that the "<6 points → >1% error" and "12+ points" thresholds really come from it. Otherwise present them as STAN guidelines that cite it |
| 7 | "Predicted spectral library". The frozen libraries are empirical and differ by vendor | app.py:3566 against community_params.py:102-130 | D7 |
| 8 | Footer says "Code: MIT"; the repo is under the STAN Academic License | `10_footer.png`; app.py:3629; LICENSE | D6 |
| 9 | Raw filenames are public on every row, contradicting the user guide and the PEG card. The client strip opt-out is rejected by the relay | `data_summary.txt`; app.py:988-1011, 4086, 4390, 4579; docs/user_guide.md:244/246 | D4 |
| 10 | "Not enough community data for this instrument on this metric." stays on screen under a drawn chart | Your Lab vs. Community → set page QC Standard to **E. coli** → set it back to **HeLa**. The box persists below the plot (`repro_stale.json`: boxes 0 → 1 → 1). `repro_stale_labtrend_after_ecoli_roundtrip.png`. The box is written at app.py:4362 and never cleared | Clear the empty-state element on every render (or B3) |
| 11 | TIC overlay opens on "7 SPD (2 runs)" and draws IQR and 10–90% bands from 2 traces. In DDA mode it says "No TIC traces for this LC system", but no DDA traces exist at all (all 3,268 traces are DIA) | `05_instrument_health_explorer_p6.png`, `i_tic_mode_dda.png`; app.py:4175 sorts SPD ascending and takes the first | Default to the cohort with the most traces (100 SPD, 661). Draw bands only from ≥5 traces. Say "No DDA TIC traces have been submitted yet". Group options by the B2 buckets |
| 12 | Ordinals "93th", "82th", "72th", "62th", "61th" | `09_community_submissions_p1.png`; `pctileBadge` app.py:5709-5712 appends a fixed "th" | st/nd/rd/th, with 11–13 taking "th" |
| 13 | Duplicate rows: 39 exact-duplicate groups (47 repeated run_names) and 46 run_names listed under both pseudonyms. The same run appears twice in the table | `data_summary.txt`; `page_text.txt` lines 974-975 and 978-979 | D8 |
| 14 | Amount unit errors: 562,100 ng and 100,000 ng produce a ">600 ng (1000-100000 ng)" card. 1 µg FAIMS runs are stored as 50 ng and top the DIA table | `data_summary.txt`; `page_text.txt` lines 259-260, 965-968 | D8, B4 |
| 15 | "Hide failed runs · 0 flagged" toggle does nothing (`is_flagged` false on all 3,305 rows) | `i_stats_hidefailed_off.png` | D8 |
| 16 | Duplicate cards: Lumos 30 SPD "Unknown Unknown" n=407 and "All columns combined" n=409 show identical ranges | `03_community_reference_ranges_p1.png` | D5 |
| 17 | Percentile badges are computed against an unnamed family-level cohort, so a 53,187-precursor row reads "100th" directly below rows at 70th | `page_text.txt` line 983; app.py:5773 | B2 |
| 18 | "Identification Depth vs. IPS" legend lists ◆ Astral, but there is no Astral data | `05_instrument_health_explorer_p2.png` | D3 (delete the chart) |
| 19 | Dynamic Range says "Populated going forward by the STAN watcher" above eight years of data | `page_text.txt` line 686; app.py:3351 | Delete the sentence |
| 20 | Card titles contradict their subtitles ("60 SPD" over "46-60 SPD"; "15 SPD · >600 ng" over "1000-100000 ng") | `page_text.txt` lines 64-65, 259-260 | D5 |
| 21 | Minor: `/favicon.ico` returns 404 on every page; `/arcade` calls `/api/community/identity`, which returns 404; there is an `apple-mobile-web-app-capable` deprecation warning. No other console output and no JS exceptions | `console.txt` | Serve a favicon and add `mobile-web-app-capable` |

---

## 5. Keep as is

- **Evosep PEG Watch, as the template for the rest of the page.** It gives honest sample-size wording, three numbered join steps, an exact list of shared fields, full ranking rules, and a detector-family caveat. On 1.2.1 the "Evosep vs other LC" card shows a "no lab yet" state with a join link when one side is empty, rather than an empty chart (`shots/v121_peg_lc_2_exploris.png`). Copy this pattern into the benchmark rather than inventing a new one.
- **The primary-metric ordering.** Cards, tables and charts lead with precursors (DIA) or PSMs (DDA), and DDA cards switch to PSMs. Proteins are labelled as context. Keep that ordering through every change above.
- **Cohorts built from instrument × SPD × load amount.** This is how a core actually thinks about QC. Once the buckets are unified (B2), it is the right backbone for the lookup (B1).
- **The concrete HeLa standard:** Pierce 88328/88329 with buy links. A visitor knows exactly what to inject.
- **The TIC overlay's median line with IQR and 10–90% bands**, rather than a hairball of traces. It is a distinctive view once it opens on a populated cohort (bug 11).
- **The engineering.** The capture recorded no JS exceptions across load and every interaction at both widths. There is no horizontal overflow at 400 px and every header and footer link returns 200. The PEG board draws at 1.2 s and the main content at about 1.9 s. The open CC BY dataset and the `/docs` API support the trust story.

---

## 6. Proposed page outline

The page is currently a 13,800 px data dump (25,400 px on phone), and the only interactive answer it gives is inside the PEG section. Proposed order:

1. **Header:** one line on what the site is for ("Compare your QC HeLa against reference ranges from labs running the same frozen search"), the single-facility disclosure (D2), and nav in the order Join · Where do I stand · Methods · PEG Watch · Dataset · API · GitHub · Museum · Arcade.
2. **Stats row:** runs · labs · instruments · latest run date, with the Join link in place of the inert "0 flagged" card. Put the three-line glossary (B2) directly underneath.
3. **Sticky filter bar:** QC standard · DIA/DDA · amount. Every panel below follows it (B2).
4. **Where does my run sit?** (B1): the matched cohort card plus the user's percentile.
5. **Reference ranges:** grouped by instrument model, primary metric large, "n runs · n labs" on each card, sparse cohorts folded (D5).
6. **Join the benchmark** (D6): the three steps, the exact list of shared fields, what never leaves the lab, and when data appears.
7. **How the numbers are made** (D7 + D3): engines and versions, FDR, per-vendor empirical libraries with md5, the IPS v2 definition, and the within-vendor caveat.
8. **Explore further** (B5, B6): Depth by Throughput, platform violins, Best Configurations (with a Labs column) and the TIC overlay (loaded lazily). Collapsed `<details>` on phone.
9. **Lab trend vs. reference** (B3).
10. **Evosep PEG Watch:** unchanged.
11. **UC Davis instrument history** (collapsed): the four ID-free date series, per instrument, with the detector-family note.
12. **Submissions table**, with a Cohort column and no filenames (D4, B2).
13. **Footer:** the correct license, CC BY data, and a link to the exact public field list.

If only one day of work is available, do D1, D2, D3, D4 (API and hovers), and bugs 6, 10, 11 and 12. Together they remove every wrong number a first-time expert visitor is likely to find in the first two screens.
