# Critique of the research design (2026-09-29)

**Verdict: approve-with-changes.** The statistical core holds up on the existing data and does not need a redesign. Three things do need to change before any fitting: the reference count S cannot be reproduced outside UC Davis, the "searched alone" option mixes MBR on and off, and the 80% interval ignores lab-to-lab transfer. Resolve M2, M3 and M8 before Phase 0 or the pilot, and don't set any stratum live until M4 is done.

## Checked and holds
- **Primary-source claims.** Each of these matches `sources.md` and the DIA-NN release assets:
  - there is no DIA-NN 2.4;
  - the main report defaults to 5% FDR from 2.5.0;
  - Linux reads `.raw` natively from 2.1.0;
  - 2.3.2 gives "up to 10%" more on some PASEF data;
  - the Hive Spectronaut licence refuses Linux;
  - the corrected library sizes are right: 51,099 / 52,726 / 168,557.
- **I re-ran the fit on existing pairs** (`scaling/review_check.py`, local, no compute):
  - Fitting on the ln S scale instead of the transformed scale does not change the conclusion. The power law still beats the logit when N comes from a finite library (timsTOF full library: 1.74 vs 3.13; 2.6.1 empirical: 2.73 vs 6.00).
  - "The interval compresses near the ceiling" is supported. The y-space residual spread, top third over bottom third, is 0.57–0.98. In ln S the residuals shrink as coverage rises (ρ −0.27 to −0.47). The heteroscedasticity that matters is at the low end.

## Must fix

**M1. "Alone" mixes single-file MBR on and off.**
- The DIA-NN README tells first-time users to "uncheck **MBR**" (l.83), so it is on by default in the GUI. It also says "Always keep MBR enabled … based on predicted libraries" (l.852).
- So a typical library-free visitor runs two passes on one file.
- Existing pairs (brett/affinisep_Dec25, 11 timsTOF raws per set) show the size: first pass without MBR is 0.80× STAN, versus 1.03–1.05× for two-step or MBR.
- Arms D and E produce no single-file MBR stratum.
- **Fix:** split F4 into "alone, MBR off" and "alone, MBR on". Add a per-raw `--reanalyse` second pass that reuses the first-pass `.quant` (about 13% extra).

**M2. The reference S cannot be reproduced outside UC Davis, so D0 cannot stay neutral.**
- `stan/search/local.py:425-447` automatically uses `~/STAN/instrument_library.parquet` when it exists.
- `stan/library_builder.py:91-160` builds that file by re-searching the lab's own DIA runs with `--gen-spec-lib`.
- So every install ends up searching its own empirical subset. The UC Davis cohorts use TIMS-10878's and DESKTOP-FOT3DAA's.
- A visitor's raw searched against UC Davis's subset is a counterfactual that nobody can run.
  - "For an exact placement, run STAN on the raw file" is therefore false today: full library over subset is 1.034 on timsTOF.
  - The "STAN standard search" option places such numbers uncorrected.
- It is also circular. 14 of 24 timsTOF panel raws predate the 2026-04-10 library build, which drew on all DIA raws in the runs table. I did not check the Exploris build date.
- D1 is not a "free correction" either. The 2.3.2 installs write a report that is already global-filtered, which is a count-definition bug in STAN's own metric.
- **Fix:**
  - Make the frozen community library the reference for both the cohorts and S. Re-search the cohort rows (0.2–1 core-h each, feasibility §2).
  - Pin one DIA-NN version and one count definition across Hive and single-lab installs.
  - Decide this before the Phase 2 fit, not before go-live.
  - Take panel and G8 raws from after the library build.

**M3. Arm A is not the production harness.**
- §1 says S comes from "the Hive production harness".
- §6 sends every search through the pipeline skill with `--qvalue 0.05`. Production is `--qvalue 0.01 --threads 8`, the sif and the fixed digest.
- G1 would then compare two different invocations.
- **Fix:** run arms A and B with the exact production command line (`run_one_v1`), including `--threads 8` and `--qvalue 0.01`. Only D, E, F and the probes go through the skill.

**M4. Transfer between labs is not modelled, but the band is shown as if it were calibrated.**
- Each instrument model is one physical instrument. G8 holds out later dates, not other labs.
- Settings the form does not capture already move the same raw by 1–5%, which is the whole G2 budget:
  - PC install vs Hive: Lumos 0.967, Exploris 0.954;
  - same library, different settings: 0.987 vs 1.006.
- ±5% costs 12–33 percentile points.
- Jackknife+ guarantees only 1−2α (60%) in the worst case, and only for raws exchangeable with the panel.
- The likely bias has a direction. STAN's libraries are built from UC Davis data, so other labs' raws cover them less, and the fit overestimates S.
- **Fix:**
  - Add an out-of-lab hold-out: at least 8 public HeLa raws per model from other labs (PRIDE, downloaded on the Mac and piped to Hive), searched with arm A and the visitor configurations.
  - Gate on that hold-out, and carry a transfer-variance term in the interval.
  - Until then, label every result "calibrated on one instrument at UC Davis".

**M5. The MBR batches are not realistic.**
- The batches of 8 and 4 are cut from the depth-spread panel, so each mixes p05 clogs with p97 runs.
- A visitor's QC batch is consecutive, similar injections. A run's MBR gain depends on its batch-mates.
- Reusing `.quant` files also needs fixed mass accuracy and scan window (README l.230). Visitors run with automatic settings.
- **Fix:** build batches of 2–3 and 8–10 consecutive runs from the same day or week, with batch depth as a covariate. C5 must be checked against automatic mass accuracy. If this isn't done, refuse MBR in v1.

**M6. The old-version arm on Thermo is not what visitors run.**
- DIA-NN 1.8.1 on Linux reads only `.d`, `.mzML` and `.dia` (1.8.1 README l.94). Before 2.2.0, Linux output also differs from Windows (l.507).
- 1.9 has no Linux build at all.
- The feasibility notes included controls for `.raw` vs mzML and for Windows vs Linux. The design dropped both.
- **Fix:** run arm F's Thermo raws natively on Brett's Windows workstation. Otherwise, reinstate both controls on at least 8 raws and carry the measured offset and its variance.

**M7. Form and λ selection is mostly noise, and the leave-one-out step must include it.**
- The 0.25-point tolerance is below the bootstrap SE of MdAPE, which is 0.4–0.6 at n ≈ 25. On the library-free + MBR set, λ = 0 gives 2.35 ± 0.60 and λ = 0.75 gives 1.81 ± 0.42.
- λ is stable only when the effect is large: λ = 0 was chosen in 182 of 200 draws of 24 pairs.
- On Lumos, with coverage p ≤ 0.5, λ cannot be identified.
- **Fix:**
  - Use a one-SE rule, or fix λ in advance by class (Lumos 0; library-based N 0; library-free timsTOF pooled).
  - Repeat the full selection (λ, form, Huber) inside every leave-one-out fit that is published for jackknife+. Otherwise the intervals are optimistic.

**M8. Freeze G2–G9 before any fit data are seen.**
- "Brett confirms thresholds after the pilot", but the pilot raws are part of the 72 fit raws.
- The rule "G2 failing at 8 raws per cohort" is judged on 8 points.
- The pilot may only revise cost and controls.

**M9. G9 pools 4 timsTOF and 4 Orbitrap raws, but version effects differ by vendor.**
- 2.3.2 helps some PASEF data by up to 10%; 1.8.1 "improved dia-PASEF".
- **Fix:** use at least 8 raws per vendor and gate each vendor separately.
  - Name the library mode and run context the probe covers.
  - Apply a shift of 3% or less as an offset instead of absorbing it into the merge.

**M10. The §8 mock screen contradicts the evidence and the design's own rules.**
- 61,200 → 44,100 implies N/S = 1.39. The only existing 2.7.0 library-free, no-MBR pairs give 0.913 (8 timsTOF raws).
- Under the design, the panel's N max would be about 45k. So 61,200 would trigger R7's one-sided "at least pX", not a band. Part B §B.8 has the same example.
- **Fix:** replace the example with a value consistent with the evidence, and also mock an R7 state.

**M11. The percentile line must name its reference population.**
- "p56–p85 of timsTOF HT · 60 SPD · 50 ng (577 runs)" reads as a rank among labs. It is one instrument's history, including failed runs (panel p05 = 5,540), and `amount_ng` is 50.0 on every row.
- **Fix:**
  - Headline it as "of 577 QC injections (failed runs included) from one timsTOF HT at UC Davis".
  - Drop "50 ng" until `amount_source` exists.
  - Drop the binomial widening. It is zero for a descriptive percentile and badly understated for a claim about a population from one autocorrelated series. Widen for transfer (M4) instead.

## Recommended
1. **Let visitors drop in their files instead of typing the settings.** Parse the DIA-NN `report.log.txt` and `report.stats.tsv` in the browser: the version banner and full command line fill F1–F8 and the count. Wrongly typed settings will cause larger errors than the model: 5% vs 1% FDR is +28%, MBR is ±20–30%, count source is 1–9%.
2. **Factorise the model.** Make count source and FDR a conversion layer within each search, fitted on the same searches, and for 2.3.0 on thousands of existing Hive runs. Compose it with one map per engine × version group × library × context × model, and gate the composed chain. This replaces about 180 separately gated strata; at n = 24, G3 passes a stratum with true 65% coverage about half the time.
3. **Normalised jackknife+ by default.** The proposed switch compares 12 points against 12 (coverage SD about 11.5 points), so it can never fire.
4. **Capture FASTA and mass accuracy.** Add FASTA (canonical, with isoforms, other) and mass accuracy (automatic or fixed) to F7, or measure them in D''.
5. **Tighten R9 from 60 to 40 points.** Above that, show the S range only.
6. **Warn DIA-NN 2.5+ users.** When F6 is "counted report rows" and F5 is 1%, ask: "2.5+ writes at 5% by default; did you filter Q.Value ≤ 0.01?"
7. **Rename "STAN standard search"** to "I ran STAN on this raw (STAN version …)", so a visitor who "used a standard search" does not pick it.
8. **Spectronaut: calibrate at 2–3 and 8–10 runs, and expect one version.**
   - PG/SN ratios: 0.65–0.79 multi-run vs 1.56–1.74 single-run.
   - Expect a single live Spectronaut version (licence), and include the SN20+ experiment-level q and `EG.Identified` protein cutoffs in F6.
9. **Panel sampling.**
   - The panel is chosen by depth quantiles of S with the top oversampled. Check the slope with and without p05 and p97.
   - Group CV by column install or month, not ISO week; with raws spread since 2024-09, week groups are mostly singletons.
10. **QC PDF.** In the pilot, check whether its number equals `stats.tsv`; if so, merge the two options.
11. **Pin `--threads` in every arm**, and test determinism across thread counts in C1.
12. **Gradients outside the panel (R8):** widen by the leave-one-SPD-out error instead of adding a note. G7's 8% is looser than G2's 5%, and Lumos already shows a gradient trend of about 10% (1.048 → 1.157).
13. **The UI must never:**
    - call Ŝ an identification count;
    - imply the visitor's search over-reports;
    - rank labs;
    - call the band a confidence interval;
    - store scaled values.

    Add one sentence explaining that S < N is expected on timsTOF because of the ~51k-precursor library ceiling, so a lower number is not a verdict on their run.

## Note on your request about the TIC
This review does not address it. The current mockup version (1790718407-6bca) does contain the TIC overlay: it is `details#tic`, open by default, under "Explore further" at line 669, with 3,025 traces embedded in `window.STAN_MOCK.tic`. If you can't see it, the panel is failing when the page renders or is sitting further down the page than expected; nothing is missing. It needs a separate render check in a browser.

Files are in `~/stan-handoff-2026-09-29/scaling/`:
- `review_check.py`