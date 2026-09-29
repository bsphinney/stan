# Scaling a visitor's precursor count to STAN's scale: design for approval

For: Brett Phinney · 2026-09-29 · Status: **proposal, nothing built, nothing searched**
Inputs: `inventory.md`, `sources.md`, `feasibility.md` (same folder), the Part B spec in
`STAN-peg/docs/superpowers/specs/2026-09-29-community-redesign-and-precursor-lookup-design.md`,
and a zero-compute test of the model on pairs that already exist (`demo_fit.py`, `demo_lambda.py`,
outputs `demo_fit.out`, `demo_lambda.out`).

---

## 0. Summary

1. **Target.** For a visitor's HeLa run, estimate *S*: the number STAN's Hive production search would
   report on the same raw file. That is DIA-NN 2.3.0, single run, the library STAN actually searches
   for that instrument model, and unique `Precursor.Id` at run-level `Q.Value ≤ 0.01`.
2. **Form.** Six new required fields: engine, version, library/workflow, run context (MBR),
   FDR level, and where the number was read. There is also one conditional field (search space) and
   one timsTOF-only field (acquisition scheme). Options that are not calibrated can be seen but not
   used.
3. **Model.** For each instrument model × configuration × count source × FDR level, fit a
   saturation-strength regression on paired searches of Brett's own HeLa raws:
   `ln(S/L) − λ·ln(1 − S/L) = a + b·ln N`. λ = 0 is a power law and λ = 1 is logit coverage. λ is
   chosen by cross-validation, and predictions are capped at *L*, the library size.
4. **Uncertainty.** A jackknife+ 80% interval on *S*, which becomes a percentile **band** in the
   matched cohort, widened for cohort sampling error. The page never shows a point percentile for a
   scaled number.
5. **Refusal.** The page refuses when the configuration is not calibrated, the count is out of
   range, MBR was run over more than 10 runs or mixed batches, a project library was used, the
   instrument model has no pairs, or the band is too wide to say anything.
6. **Go-live gate** for each configuration:
   - nested cross-validation median absolute % error ≤ 5% and P90 ≤ 12%;
   - interval coverage not significantly below 80%;
   - median band width ≤ 30 percentile points;
   - the same checks pass on 24 prospective raws that were never used for fitting.
7. **Work.** Phase 0 uses pairs that already exist and costs no compute. The pilot is about
   340 core-h. The full panel is about 1,650 core-h, and version probes about 340 more. These are
   planning figures; the pilot replaces them with measured costs. Spectronaut has to be run by Brett
   on a licensed Windows machine, because the Hive licence refuses Linux (probe job 22927780).

Three facts from the research change the premise the task started from. They are in §1 and §10.
- STAN's timsTOF cohort was searched against a **51,099-target per-instrument subset**, not the 54k
  library.
- Exploris was searched against a **52,726-target subset**, not the 170k library.
- `library_coverage_pct` is NULL on every PG row.

---

## 1. The target *S*, and two decisions needed before any fitting

**Definition (verified).** `stan/metrics/extractor.py` counts `Precursor.Id.n_unique()` after
`Q.Value <= 0.01`, with no global, library or protein filter. On Hive-searched runs, PG
`n_precursors` equals DIA-NN's final log line "Number of IDs at 0.01 FDR" (ratio 1.000 at p10, p50
and p90; feasibility §0.2).

**The library actually searched** (3,653 production logs, all DIA-NN 2.3.0; feasibility §0.1):

| Instrument model | Library searched | *L* (target precursors) | Max coverage in PG (Hive rows) |
|---|---|---|---|
| timsTOF HT | `STAN/TIMS-10878/instrument_library.parquet` (md5 674cde06…) | **51,099** (51,487 loaded) | 60 SPD 96%, 30 SPD 97% |
| Orbitrap Exploris 480 | `STAN/DESKTOP-FOT3DAA/instrument_library.parquet` (md5 c1e2a725…) | **52,726** (53,248 loaded) | 12 SPD 83% |
| Orbitrap Fusion Lumos | `hela_orbitrap_202604.parquet` (md5 ac84e40f…) | **168,557** (170,284 loaded) | 9 SPD 50%, others ≤ 32% |

- *L* is recomputed by the fit script from the parquet under STAN's length and charge filters, then
  stored with its md5.
- `COMMUNITY_LIBRARY_PRECURSOR_COUNT` (54,000 / 170,000) is wrong for timsTOF and Exploris; for
  Exploris it is off by 3.2×.
- The coverage figures in Part B §B.1 (Exploris median 13.7%) used those constants. Against the
  library actually searched, Exploris cohort medians are 43–58% in the three main cohorts (34% at
  30 SPD, n = 24).

**Provenance of *S*.** PG mixes search setups within one cohort:
- timsTOF has 1,411 Hive 2.3.0 rows, 245 instrument-PC 2.3.2 rows (219 of them duplicate a Hive row
  for the same raw) and 68 "unknown" rows.
- Some PG values exceed *L* (maximum 51,289 against *L* = 51,099). Those raws were searched against
  the full library on the PC.

So *S* for fitting comes from **arm A**: a re-search of each panel raw with the Hive production
harness, which must reproduce the Hive PG row. It never comes from an arbitrary PG row.

**D0. Which library defines STAN's scale? (Brett)** Production uses the per-instrument subsets;
`community_params.py` names the full community libraries. The difference is not zero:
- timsTOF: community/subset = 1.034 (p10 1.003, p90 1.062, n = 184, multi-run, file level);
- Exploris: 0.975 (2.3.2, n = 91) and 0.992 (2.3.0 multi, n = 74).

The calibration measures both (arm B), so either choice can be fitted without new library-free
searches. But the choice must be made before go-live, and changing it later retires every model
(§7).

**D1. Should STAN's count be the same across deployments? (Brett)** The same raw on the
instrument-PC install (DIA-NN 2.3.2, report already global-filtered) compares with the Hive PG
value as follows (median, other/PG):
- timsTOF 0.998 (n = 115, same subset library);
- Lumos 0.967 (n = 212, same library);
- Exploris 0.954 (n = 91; this install also used the full 170k library, so library and count
  definition are mixed).

Other labs' single-lab installs are therefore about 3–5% below UC Davis Hive on Orbitraps. That is a
cohort-consistency question for Part A. This design gives it a correction for free (§6, Phase 0),
but the policy is Brett's.

**Cohort hygiene (a Part A prerequisite).** These problems affect percentiles, not the map:
- drop the 219 duplicate timsTOF raws (keep the Hive row);
- remove the 67 DDA-named Exploris rows stored as `mode='DIA'`;
- merge renamed duplicate acquisitions (e.g. `Ex110225_HeL50_30m_4OK` and `_4_OK`);
- fix misfiled SPD (e.g. `Ex300125_HeL50_30m` under 19 SPD);
- note that `amount_ng = 50.0` on every row, including HeL100 files.

---

## 2. Form fields

These go below the existing count / instrument / LC-SPD / amount inputs. Options come from the
published model index (§7), so an uncalibrated option is shown greyed out with "not calibrated yet"
and cannot be chosen. **Bold** options are in the v1 calibration plan.

| # | Field | Required | Options (exact) | Notes |
|---|---|---|---|---|
| F1 | Search engine | yes | **STAN standard search** · **DIA-NN** · Spectronaut · Other (AlphaDIA, FragPipe/MSFragger-DIA, MaxDIA, PEAKS, CHIMERYS, Skyline/EncyclopeDIA, …) | STAN standard means no scaling. "Other" is refused in v1. Spectronaut goes live only after Brett's runs (§9) |
| F2 | Version | yes (DIA-NN, Spectronaut) | DIA-NN: 1.7.x or older · 1.8 · **1.8.1** · 1.8.2 beta · 1.9 · 1.9.1 · 1.9.2 · 2.0 / 2.0.1 / 2.0.2 · 2.1.0 · 2.2.0 · **2.3.0** · 2.3.1 · 2.3.2 · 2.5.0 · 2.5.1 · 2.6.0 · 2.6.1 · **2.7.0** · newer than 2.7.0. Spectronaut: 14 or older · 15 · 16 · 17 · 18 · 19 · 20 · 21 · newer, plus an optional free-text build (e.g. 20.3.251119) that is displayed but not used | No 2.4 appears in the DIA-NN release notes (discussion #1366). Version groups (§3.6) let a probed version share its representative's model |
| F3 | Library / workflow | yes | DIA-NN: **Library-free (predicted from FASTA by DIA-NN)** · **STAN community HeLa library** (from the STAN Dataset) · Other spectral library (DDA/project, public, GPF, Prosit/Carafe/AlphaPeptDeep-predicted) · Special mode (InfinDIA, Ultra-fast, plexDIA, Enterprise Knowledge base, DIA-NN DDA mode). Spectronaut: directDIA+ (Deep) · directDIA+ (Fast) · directDIA (classic) · Library-based (Pulsar/project/public) | "Other spectral library" and "Special mode" are refused. Which Spectronaut modes go live depends on what Brett runs |
| F4 | Searched with other runs? | yes | **Alone (only this raw in the search)** · **With other files, no MBR/second pass** · **With 2–10 HeLa QC runs of the same method, MBR or two-step on** · More than 10 runs, or the batch includes other samples or gradients | The last option is refused. "No MBR" maps to the single-run model only if control C2 passes (§6). For Spectronaut the wording is "experiment with 1 / 2–10 HeLa runs / larger or mixed" |
| F5 | Precursor FDR | yes | **1%** · **5%** · other | Hint: "DIA-NN 2.5 and later filter the main report at 5% by default; Spectronaut defaults to 1%." "Other" is refused |
| F6 | Where the number came from | yes | DIA-NN: **`report.stats.tsv` → Precursors.Identified** · **log line "Number of IDs at 0.01 FDR" (last one printed)** · **counted unique Precursor.Id in the main report, Q.Value only** · **… with Global.Q.Value ≤ 0.01 as well** · **… with a protein-group q filter as well (PG.Q.Value or Global.PG.Q.Value ≤ 0.01)** · QC PDF / GUI summary · pr_matrix rows · Not sure. Spectronaut: per-run precursor number in the Run summary / AnalysisLog (exact label for Brett to confirm) · counted unique EG.PrecursorId per run in a report export · Not sure | Every calibrated option comes from the same searches at no extra cost (§6). QC PDF is not calibrated (its definition is unknown). pr_matrix is refused (experiment-level). "Not sure" gives the union of the calibrated sources' intervals |
| F7 | Search space | yes, when F3 is library-free or directDIA | **Trypsin, ≤ 1 missed cleavage, no variable modifications, charge 2–4 (STAN's)** · 2 missed cleavages and/or M-oxidation / N-term acetylation · Not sure. Spectronaut: BGS Factory Settings · modified | The permissive option and "Not sure" stay uncalibrated until arm D'' (§6) measures the effect. DIA-NN's GUI default digest is **unverified**; the README only says 1 missed cleavage is "optimal in most cases" |
| F8 | Acquisition (Bruker models only) | yes | **Standard dia-PASEF** · Slice-, diagonal-, Synchro- or midia-PASEF | The second option is refused: the panel is dia-PASEF only, and DIA-NN 2.3.0/2.3.1 mis-handle Slice/diagonal PASEF (release notes) |

Help text on the count field: "Precursors = unique modified sequence + charge from **one** run
(DIA-NN `Precursor.Id`, Spectronaut `EG.PrecursorId`). Not peptides, not protein groups, not rows,
not a total across runs."

A soft warning fires if the number looks like a protein count, e.g. under 10,000 on a timsTOF HT at
60 SPD. It is a warning, not a refusal, because real bad runs go that low: the panel's p05 raw at
100 SPD has 5,540.

Scaling applies only to the **precursor count** (the DIA / Track B primary metric). Peptides,
proteins and DDA PSMs are out of scope for v1.

---

## 3. The transform

### 3.1 Notation

- *k* = one stratum: instrument model *m* × configuration *c* (F1–F4, F7, F8) × count source *s*
  (F6) × FDR level *f* (F5).
- For panel raw *i*: *N_i* is the count under stratum *k*, *S_i* is the arm-A count, and
  *p_i* = *S_i* / *L_m*.
- *x* = ln *N*.

### 3.2 Model family (saturation strength λ)

```
y_λ(S) = ln(S/L) − λ · ln(1 − S/L)          λ ∈ {0, 0.25, 0.5, 0.75, 1}
y_λ(S_i) = a + b·x_i  [+ a_spd(i)]  [+ c·(x_i − x̄)²]  + ε_i
Ŝ(N) = y_λ⁻¹(a + b·ln N)    (bisection on S ∈ (0, L); capped at L when λ = 0)
```

- **Elasticity.** d ln S / d ln N = b · (1 − p) / (1 − p + λp).
  - At low coverage it is *b*, which gives a power law. That is the right shape for the Lumos,
    which stays at or below 50% coverage.
  - For λ > 0 it falls to 0 as *p* → 1, so a timsTOF run at about 90% of the library cannot be
    pushed past *L* however large the visitor's library-free count is.
- λ = 1 is Part B's logit-coverage form, and λ = 0 with *b* fixed at 1 is a constant ratio. The
  family therefore **nests the naive ratio, the power law and the hard-ceiling logit**, and the data
  choose among them.

**Why not fix λ = 1, as Part B does?** On pairs that already exist (zero compute; "other" configs
confounded by multi-file searches and own libraries), leave-one-out median absolute % error for
λ = 0 / 0.25 / 0.5 / 0.75 / 1 was:

| Existing pair set (S = PG value) | n | MdAPE % for λ = 0 / .25 / .5 / .75 / 1 | best λ | max *S/L* |
|---|---|---|---|---|
| timsTOF, DIA-NN 2.3.0 library-free + MBR | 25 | 2.35 / 2.03 / 2.00 / **1.81** / 1.85 | 0.75 | 0.75 |
| timsTOF, 2.6.1 two-step empirical library (69.6k) | 29 | **2.72** / 4.19 / 4.59 / 4.93 / 4.84 | 0 | 0.82 |
| timsTOF, 2.3.0 full community library 53.6k, multi | 182 | **1.66** / 2.17 / 2.87 / 3.07 / 3.24 | 0 | 0.99 |
| Exploris, 2.3.2 full 170k library (PC install) | 91 | **1.33** / 1.78 / 1.91 / 2.28 / 2.34 | 0 | 0.83 |
| Exploris, 2.3.0 library-free, multi, MA 10 | 19 | **5.79** / 5.94 / 6.07 / 6.14 / 6.20 (P90 18.3%) | 0 | 0.56 |
| Lumos, 2.3.2 same 170k library (PC install) | 212 | 1.22 / 1.15 / **1.08** / 1.21 / 1.27 | 0.5 | 0.38 |
| Lumos, DIA-NN 1.8 QC log (stats; settings unverified) | 292 | **3.22** / 3.28 / 3.42 / 3.47 / 3.50 (P90 10.4%) | 0 | 0.33 |

What the table shows:
- When *N* is **library-free** and *S* saturates, a strong λ helps.
- When *N* **itself comes from a finite library**, both sides saturate together and the power law
  wins. A fixed logit then roughly doubles the error.
- One existing configuration (Exploris library-free, multi) would fail acceptance outright. That is
  what the refusal rules are for.

These are **not a calibration**, only evidence about the model's shape:
- the other-side counts are file-level stats from multi-file searches;
- *S* is the PG value, including provenance leaks (1 pair with *S* ≥ *L* was dropped).

### 3.3 Fitting

- **Direction.** Regress *y*(*S*) on ln *N*, the prediction direction. What we want is E[*S* | *N*],
  not an errors-in-variables slope.
- **Estimator.** Huber M-estimation on the transformed scale (tuning 1.345 × MAD scale, IRLS).
  - The panel deliberately includes clogged, PEG-contaminated and spray-failure runs at p05, where
    engines diverge. With 24 pairs, a single one of those must not set the slope.
  - OLS is reported alongside for comparison.
- **Candidate forms,** in order of preference (simplest first):
  1. M0: constant ratio (λ = 0, *b* ≡ 1).
  2. M1: power law or saturation (the λ grid).
  3. M2: M1 plus per-SPD-cohort intercepts.
  4. M3: M1 plus a quadratic in *x*. M3 must stay monotone (derivative > 0) over
     [0.9·*N*min, 1.5·*N*max].
- **Selection.** Nested grouped cross-validation:
  - the form and λ are chosen inside each outer fold, so the reported error includes the cost of
    choosing them;
  - take the simplest form whose CV median absolute % error is within 0.25 points of the best.
- **CV groups.**
  - Single-run and first-pass strata: raws acquired on the same instrument in the same ISO week
    form one group (same column, near-duplicate conditions).
  - MBR strata: the search batch is the group, because every count in a batch shares one empirical
    library.
- **Pooling.** The fit is per instrument model, pooling its SPD cohorts: 3 cohorts × 8 raws = 24
  pairs.
  - Adding SPD as a covariate (M2) is allowed only if it wins in CV.
  - Evidence that it might: the Lumos DIA-NN 1.8 QC-log ratio PG/1.8 rises from 1.048 (35 min) to
    1.157 (120 min). In the λ = 0 fit (*b* = 1.024), depth alone explains only about 1–2% of that
    rise. Adding SPD intercepts to the logit form improved the P90 error (11.4 → 9.4%) but worsened
    the median (3.5 → 3.8%). So whether SPD terms are needed is open, and the cross-validation
    decides.
- **No pooling across instrument models.** Each has its own *L*.
- **Other-lab instruments.** A model STAN has no pairs for (Astral, timsTOF Pro/Ultra, QE…) gets no
  scaling model (rule R6).

### 3.4 Prediction interval: jackknife+ at 80%

- Why jackknife+ (Barber, Candès, Ramdas & Tibshirani, *Ann. Stat.* 49:486, 2021): with n = 24,
  splitting off a calibration set wastes a third of the data.
- Why 80%: at n = 24 a 95% interval would rest on the single largest residual.

**Steps:**
1. For each *i*, refit without *i* to get (*a*₋ᵢ, *b*₋ᵢ, extra terms), and the leave-one-out
   residual *R_i* = |*y_i* − ŷ₋ᵢ(*x_i*)|.
2. For a visitor's *N*, compute μᵢ = ŷ₋ᵢ(ln *N*).
3. The lower bound in *y* is the ⌊0.2(n+1)⌋-th smallest of μᵢ − *R_i*. The upper bound is the
   ⌈0.8(n+1)⌉-th smallest of μᵢ + *R_i*. For n = 24 these are the 5th and the 20th.
4. Map both bounds back through y_λ⁻¹ and cap at *L*.

The monotone back-transform preserves coverage. Near saturation it compresses the interval
naturally, because a fixed width in *y* is fewer precursors when *p* is close to 1.

**Heteroscedasticity.** The Q-only versus global-count gap is larger for weak runs: +5.8% and +9% at
8–13k precursors, against about 1% at 42k. If CV coverage in the lower half of *N* differs from the
upper half by more than 15 points, switch to normalised jackknife+: divide *R_i* by
σ̂(*x_i*) = exp(g₀ + g₁*x_i*), fitted to the leave-one-out |residuals|.

**Browser cost.** The published model carries the n leave-one-out coefficient sets and the *R_i*
(24 × 3 numbers), so the browser computes the interval with no server round-trip.

**Existing-data check.** Nested jackknife+ on the small existing sets covered 0.80 (n = 25),
0.83 (n = 29) and 0.84 (n = 19). The median 80% widths were 8.5–10.2% of *S* on timsTOF, about
15–16% on the Lumos DIA-NN 1.8 log, and 29–31% on the failing Exploris set.

### 3.5 From interval to percentile band

- Matched cohort *C*: Part A's cohort key (instrument model × LC class × gradient × amount bucket),
  using STAN-standard rows only, after hygiene.
- pct(*s*) = 100 × mid-rank of *s* in *C*.
- Band = [pct(*S*lo) − 1.28·se_lo, pct(*S*hi) + 1.28·se_hi], where se = 100·√(p(1−p)/|C|).
  - This widening is 2.2 points at |C| = 828 and 8.1 points at |C| = 63 (timsTOF 30 SPD) when p =
    0.5.
  - Clip to 0–100 and round outward.

**Display states:**
- Band width ≤ 60 points: show "p*lo*–p*hi*".
- *S*hi hits *L* or pct(*S*hi) ≥ 99: show "≥ p*lo* (top of cohort)", with a library-cap note (§8).
- Band wider than 60 points: no percentile. Show the *S* range and "too uncertain to place" (rule
  R9).

**How much a count error costs in percentile points.** From current PG cohorts (hidden = 0, DDA
names excluded, not de-duplicated), an *S* range of ±5% spans:

| Cohort | at cohort median | at cohort p90 |
|---|---|---|
| Lumos 32 SPD | p44–p58 | p83–p95 |
| Exploris 38 SPD | p44–p57 | p82–p96 |
| timsTOF 100 SPD | p44–p56 | p81–p98 |
| timsTOF 60 SPD | p42–p62 | **p73–p99** |
| timsTOF 30 SPD (n = 63) | p30–p63 | **p57–p98** |

The top of the timsTOF cohorts is library-compressed: the top 25% of 60 SPD sits between 44.4k and
49.9k. Wide bands there are the truth about the cohort, not a modelling failure. That is why the
band-width acceptance criterion (§5, G6) is taken as a median across the calibrated range.

### 3.6 Version groups (hypotheses to test, never assumed)

The release notes suggest these groups. A version joins its group's model only after it passes the
probe in §5 G9; until then only the representative is live.

| Group | Members | Representative | Release-note basis |
|---|---|---|---|
| G1 | 1.8, 1.8.1, 1.8.2 beta | 1.8.1 | 1.8.1 "improved dia-PASEF", so test on timsTOF |
| G2 | 1.9, 1.9.1 | 1.9.1 | 1.9.1 is minor |
| G3 | 1.9.2 | 1.9.2 | Redesigned NN classifier and mass calibration |
| G4 | 2.0, 2.0.1, 2.0.2 | 2.0.2 | 2.0.2 "all output is identical" |
| G5 | 2.1.0, 2.2.0 | 2.2.0 | "minimal" and "marginal" gains |
| G6 | 2.3.0, 2.3.1, (2.3.2 on Orbitrap) | 2.3.0 | 2.3.1 "minimal changes"; 2.3.2 up to +10% on some PASEF data, so 2.3.2 on timsTOF needs its own probe |
| G7 | 2.5.0, 2.5.1 | 2.5.1 | 5% default FDR (handled by F5), Knowledge base refused |
| G8 | 2.6.0, 2.6.1 | 2.6.1 | Calibration and RT alignment changes |
| G9 | 2.7.0 | 2.7.0 | Preview, current |

---

## 4. Refusal rules

Every refusal shows the typed number, says "not on STAN's scale", and ends with "For an exact
placement, run STAN on the raw file."

| Rule | Trigger | What the visitor sees instead of a percentile |
|---|---|---|
| R1 | F1 = Other, or no stratum with status `live` for the chosen F1–F8 | "No calibration for this setup yet." Lists the live setups for the same engine |
| R2 | Version not a live representative and not a probed group member | Same as R1, naming the nearest calibrated version, without substituting it |
| R3 | F5 = other; or 5% for a stratum only calibrated at 1% | "Only 1% (and 5% where listed) precursor FDR can be scaled." |
| R4 | F4 = more than 10 runs or mixed batch; or an empirical/project library built from the same batch | "Match-between-runs over a large or mixed batch borrows identifications from the other runs, so the count describes the batch, not this run." |
| R5 | F3 = other spectral library or special mode; F7 or F6 uncalibrated option; F8 = non-standard PASEF | "This library/mode changes the search space in ways we have not measured." |
| R6 | Instrument model has a STAN cohort but no paired searches (only timsTOF HT, Exploris 480 and Fusion Lumos have pairs in v1) | "We have no paired searches on <model> yet." A STAN-standard number is still placed exactly |
| R7 | *N* outside [0.9·*N*min, 1.1·*N*max] of the stratum's panel | Between 1.1× and 1.5× *N*max: one-sided "at least p*X*", with *X* = pct(*S*lo at *N*max). Below 0.9·*N*min: "at most p*X*". Above 1.5·*N*max: refused, with "higher than any calibrated run by >50%; check this is a single-run precursor count" |
| R8 | The LC/SPD cohort is not among the panel's cohorts for that model (e.g. Exploris 9/30 SPD, Lumos 9 SPD), and either the chosen form has SPD terms or the leave-one-SPD-out check (G7) failed | "This gradient is outside the calibration panel." If G7 passed, placement is allowed, with the same sentence as a note |
| R9 | Final band width > 60 percentile points | The *S* range is shown, with "too uncertain to place in the cohort" |
| R10 | Model status is not `live`: failed acceptance, internal, or retired because STAN's reference changed | Same as R1 |
| R11 | The matched cohort has fewer runs than Part A's minimum | Part A's cohort message; no scaling is shown |

---

## 5. Validation and acceptance, per stratum, before it can go live

The thresholds are proposals. Brett confirms them after the pilot.

| Gate | Test | Threshold | Why this number |
|---|---|---|---|
| G1 harness (global, blocks everything) | Arm A re-search against the Hive PG row, all 72 fit raws | \|ΔS\| ≤ 0.5% on every raw; report how many are exact | PG equals the log line exactly today; this also measures DIA-NN run-to-run determinism, which is **unknown** |
| G2 accuracy | Nested grouped CV, median absolute % error of Ŝ vs *S* | ≤ 5%, and P90 ≤ 12% | Noise floor: Lumos 2.3.2 vs 2.3.0 on the same library, MdAPE 1.1–1.2% (n = 212); timsTOF full vs subset library, 1.7% (n = 182) (table §3.2). Existing library-free/empirical sets sit at 1.8–2.7%. The failing Exploris set sits at 5.8% (P90 18%) |
| G3 interval coverage, per stratum | Fraction of CV raws inside their 80% jackknife+ interval | Reject if covered ≤ 15 of 24 (one-sided binomial p = 0.036 at 0.8), or ≤ 9 of 16 | A 75–85% window cannot be tested at n = 24 (SD 8.2 points) |
| G4 coverage, pooled per engine | Same, across all live strata of the engine | 75–85% | At n ≥ 96 the SD is 4.1 points |
| G5 band coverage | True pct(*S*) inside the displayed band (CV) | Same thresholds as G3 | This is what the visitor reads |
| G6 usefulness | Median displayed band width over the panel raws | ≤ 30 points; otherwise status `s-range-only` (the page shows *S* range, no percentile) | ±5% already costs 12–20 points at cohort medians, 33 at timsTOF 30 SPD (§3.5) |
| G7 gradient transfer | Leave-one-SPD-cohort-out MdAPE | ≤ 8% to allow cohorts outside the panel (R8) | This tests extrapolation across gradients |
| G8 prospective | 24 raws (8 per model) acquired after the panel freeze date, searched only after the fit is frozen | MdAPE ≤ 6%; pooled coverage ≥ 16 of 24; any single error > 25% investigated before go-live | A true out-of-time hold-out: new columns, new tuning |
| G9 version-group membership | Probe version on 8 raws (4 timsTOF + 4 Orbitrap, spread in depth) against the representative's CV intervals | ≥ 5 of 8 inside, and \|median log ratio\| ≤ 3% | For n = 8 at 0.8, covering ≤ 3 has P = 0.01 |
| G10 external consistency (reported, not a gate) | Apply the fitted models to existing independent pairs with matching settings: 2.7.0 library-free timsTOF (8 raws, ratio 0.913); 2.3.0 predicted first pass (timsTOF 0.80, Lumos 0.69, 11 each); the Excel 1.8 Lumos log (292) if its settings are confirmed | Discrepancies are explained in the release note | Uses raws and searches the fit never saw |

A stratum that fails stays `failed` (R10), with its metrics published. It is not silently tuned
until it passes.

---

## 6. What must be measured first: the calibration panel

**Raws.** The fit set is `panel_final.tsv`:
- 72 raws: 9 cohorts (timsTOF HT 100/60/30, Exploris 38/19/12, Lumos 32/19/12 SPD) × 8 depth
  quantiles (p05, p20, p35, p50, p65, p80, p90, p97, top oversampled);
- every path checked with `test -e` on Hive (36.1 GB of `.d`, 37.8 GB of `.raw`);
- Brett screens the flagged oddities first (§9).

The **prospective set** is 24 new raws, 8 per model, acquired after the freeze.

**Searches.** They run through the `ucdavis-proteomics-core-pipeline` skill, as Part B §B.5 says:
one confirmation, pinned builds, FRAN deposit **off**. Settings for every search:
- `--qvalue 0.05`, so the 1% and 5% counts come from one search (control C3 checks this is safe);
- no `--report-decoys`;
- one pinned FASTA and digest (D3, §9).

Outputs go under `/quobyte/proteomics-grp/STAN/calibration/engine_scaling/<panel_id>/`, inside
FRAN's `DEFAULT_EXCLUDES` prefix. Logs go to `/quobyte/proteomics-grp/STAN/logs/calibration/`.
Never `$HOME` (3.7 GB free) or `/tmp`.

**Arms**

| Arm | What | Purpose |
|---|---|---|
| A | DIA-NN 2.3.0 sif, production library, STAN params, single file | *S*, and gate G1 |
| B | 2.3.0, **full** community library (53,039 / 168,557), timsTOF + Exploris | Resolves D0; lets the whole table be refitted to either reference |
| C (optional) | 2.5.1 / 2.6.1 / 2.7.0 with the full community library | "STAN community library" option in F3 |
| D, E, F | Library-free, predicted from the pinned FASTA: **2.7.0 (D)**, **2.3.0 (E)**, **1.8.1 (F**, imported; Thermo via mzML because Linux `.raw` support starts at 2.1.0) | Per cohort, one `--reanalyse` batch of 8 plus two batches of 4 reusing first-pass `.quant` files. That gives three strata from one first pass: `…-first-pass.parquet` → single/no-MBR; batch-8 and batch-4 → "MBR, 2–10 runs" (batch size tested as a covariate) |
| D'' (optional) | 2.7.0 library-free, permissive digest (2 missed cleavages, M-ox), reusing FRAN's cached human 2.7.0 predicted library | Needed before F7 "permissive" or "Not sure" can go live |
| Probes | 8 raws each: 1.9.2, 2.0.2, 2.2.0, 2.3.2, 2.5.1, 2.6.1 | Gate G9 |
| S (Brett) | Spectronaut directDIA+, BGS Factory, per raw as its own experiment, plus one 8-run experiment per cohort | Spectronaut options |

**Counts extracted per output.** One versioned extractor handles DIA-NN 1.8 `report.tsv`, 2.x
`report.parquet` and Spectronaut exports:
- unique `Precursor.Id` with `Q.Value` ≤ 0.01 and ≤ 0.05 (`Decoy == 0` when that column exists);
- the same with `Global.Q.Value` ≤ 0.01;
- the same with `PG.Q.Value` ≤ 0.01, and with `Global.PG.Q.Value` ≤ 0.01;
- `report.stats.tsv` Precursors.Identified;
- every "IDs at … FDR" log line (the last one and the first one);
- first pass vs final for MBR;
- Spectronaut: unique `EG.PrecursorId` per `R.FileName` at `EG.Qvalue` ≤ 0.01, with and without
  `EG.Identified`, plus the run-summary number.

Each of these is one F6 option, at no extra compute.

**Controls, run in the pilot:**

| Control | Question |
|---|---|
| C1 | Arm A determinism (G1) |
| C2 | Is the batch first pass the same as a truly single-file search (3 raws)? If not, F4 "Alone" and "no MBR" become separate strata |
| C3 | Is the set at Q ≤ 0.01 the same under `--qvalue 0.05` and `0.01`, including the MBR library (one Exploris batch re-run at 0.01)? |
| C4 | Do DIA-NN 2.6/2.7 native builds read Thermo `.raw` on Hive inside `diann_2.3.0.sif`? Only 2.5.1 has been shown to; the fallback is mzML |
| C5 | Does `.quant` reuse across batch compositions reproduce the first pass? |
| C6 | Library-free cost with the STAN-matched digest (today's 10 core-h per timsTOF raw is FRAN's larger digest) |

**Compute.** Allocated core-h on `low` (`publicgrp-low-qos`, account `publicgrp`, `Requeue=1`),
**planning figures**:
- library-mode search 0.55 core-h (feasibility arm A);
- library-free first pass 10 core-h per timsTOF raw (FRAN p50 9.6, larger digest) and 3 core-h per
  Orbitrap raw (extrapolated from a 3-file mouse test; **unmeasured on HeLa**);
- MBR second passes +26% (2 compositions × about 13%);
- library prediction 5 core-h per version.

| Phase | Content | core-h | Cumulative |
|---|---|---|---|
| 0 | Existing pairs only (below) | 0 | 0 |
| 1 pilot | 16 raws (8 timsTOF 60 SPD + 8 Exploris 38 SPD from the panel): A, B, D, E, controls C1–C6 | ~340 | 340 |
| 2a | Rest of the 72 + the 24 prospective raws: A, B, D, E | ~1,030 | ~1,370 |
| 2b | Arm F, DIA-NN 1.8.1, all 96 (plus mzML conversion, cost unmeasured) | ~620 | ~1,990 |
| 3 | Six version probes | ~340 | ~2,330 |
| optional | C ~160; D'' ~390 | | |
| S | Spectronaut: Brett's Windows machine; runtime unmeasured (feasibility guesses 1–2 machine-days per version) | — | — |

- If the pilot measures 4–7 core-h per timsTOF raw, the timsTOF share drops by 30–60%.
- Wall time: about 7 h of pure compute for Phase 2 at 15 concurrent 16-CPU jobs. FRAN currently
  saturates `low`, so the real queue wait is **unknown**.
- Storage: feasibility estimated 30–60 GB for the 72-raw plan; re-estimate after the pilot.

**Phase 0 (no searches):**
1. Build the extractor and fitter, and run them on existing pairs.
2. The **STAN single-lab install (DIA-NN 2.3.2)** strata already have 212 Lumos, 91 Exploris and
   115 + 62 timsTOF pairs. The Lumos and Exploris sets tested at MdAPE 1.1–1.3% (§3.2); the timsTOF
   sets were not tested. They can go live as their own F1 option, or feed D1,
   once their counts are re-extracted from `report.parquet` with the matching definition.
3. The **Excel DIA-NN 1.8 log** gives 292 Lumos pairs. They stay `internal` until Brett confirms the
   search settings **and** 16 of those raws re-searched with 1.8.x reproduce the logged numbers
   (MdAPE ≤ 2%).
4. Wire JSON → relay → UI end to end with these, so the pilot's output lands in a working pipeline.

**Pilot decision rules.**
- Median band width > 30 points or G2 failing at 8 raws per cohort: expand to 12 per cohort (Part B's
  size) before Phase 2.
- C2 fails: split F4.
- C4 fails: mzML route for 2.6+/2.7 on Thermo.

---

## 7. Where the calibration lives, versioning, new versions, what STAN users see

**Storage** (no PG: DDL needs `brettsp` via CAS login, and none of this is operational data):

| What | Where |
|---|---|
| Search outputs, logs | `/quobyte/proteomics-grp/STAN/calibration/engine_scaling/<panel_id>/…` and `/quobyte/proteomics-grp/STAN/logs/calibration/` |
| Pairs | `…/<panel_id>/pairs.parquet`: one row per raw × stratum × count source × FDR, with engine banner, build sha256, command-line hash, library md5, FASTA md5 and extractor version |
| Public pairs | HF Dataset `brettsp/stan-benchmark`, `calibration/engine_scaling_pairs_<panel_id>.parquet`: counts, model, SPD cohort, acquisition month and an opaque pair id. **No run names** (Part A rule) |
| Models | HF Dataset `calibration/engine_scaling.json`, served by the relay as `GET /api/scaling` (cached). The browser computes everything, so "nothing you type leaves your browser" stays true, and no demand is logged |

**Model entry** (extends Part B §B.7):

```json
{ "id": "diann|2.7.0|libfree|alone|stan_digest|dia-pasef|stats_tsv|0.01|timsTOF HT",
  "engine": "diann", "version_group": "G9", "versions_live": ["2.7.0"],
  "library": "libfree_predicted", "run_context": "alone", "search_space": "stan_digest",
  "count_source": "stats_tsv", "fdr": 0.01, "instrument_model": "timsTOF HT",
  "L": 51099, "lambda": 0.75, "form": "M1", "coef": {"a": 0.0, "b": 0.0},
  "loo": [[0.0, 0.0, 0.0]], "alpha": 0.2, "normalised": false,
  "x_range": [0, 0], "spd_cohorts": [100, 60, 30], "loco_ok": true,
  "n_pairs": 24, "metrics": {"cv_mdape": 0, "cv_p90": 0, "cov80": 0, "band_med": 0,
  "prospective_mdape": 0, "prospective_cov": 0}, "status": "live" }
```

The top level carries:
- `calibration_version` (semver), `panel_id`, `fitted_at`;
- `stan_reference`: DIA-NN 2.3.0 container path and sha256, `SEARCH_PARAMS_VERSION`, per-model
  library path, md5 and *L*, FASTA md5, the count definition text, and a `stan_reference_hash` over
  all of these.

**Versioning.**
- **MAJOR**: `stan_reference_hash` changed (new DIA-NN pin, a library switch after D0, a new count
  definition). Every model becomes `retired`. The relay compares the hash with the live STAN
  reference and applies R10 on any mismatch.
  - Arm B means a library switch can be refitted without new library-free searches.
- **MINOR**: strata added. **PATCH**: refit or extractor fix.
- Published entries are never edited in place. Retired entries stay, with a reason.

**Adding a DIA-NN or Spectronaut version:**
1. Import the build: Mac download, then `ssh hive 'cat > …'` (scp dropped), then
   `/quobyte/proteomics-grp/dia-nn/build_<nnn>/`, run inside `diann_2.3.0.sif`. Record the banner and
   sha256.
2. Run the G9 probe (about 57 core-h library-free) against the nearest group.
3. If it passes, add it to `versions_live`: MINOR bump, then Brett reviews.
4. If it fails, or the release notes mention changes to scoring, FDR, decoys or defaults (as in 2.0
   and 2.5), run the full panel for that version (about 620 core-h library-free), fit, validate and
   publish.

**What STAN's own users see:**
- STAN-searched runs are unchanged. The number on their dashboard and in the cohorts is exact, and
  the box places a "STAN standard search" number without scaling.
- Scaled visitor numbers are never written to PG, the relay parquet or any cohort.
- The "How the numbers are made" card (D7) shows:
  - the STAN reference: engine, version, library md5, *L* and the count definition, with corrected
    coverage denominators;
  - a table of live strata (n pairs, CV error, coverage);
  - the `panel_id` and a link to the public pairs.
- Other labs on the single-lab install: see D1. Their Orbitrap numbers run 3–5% below Hive's, and
  whether that is corrected or flagged in cohorts is Brett's call.

**Other facilities contributing pairs (optional, later).** STAN labs already have *S* for their
HeLa raws. If they also search the same raws with their usual engine, they could submit counts-only
pairs through the relay: no raw files, consistent with the privacy rules. Pairs from a second
facility are the only way to measure **transfer error across labs, which is unknown today**. They
would also unlock R6 for other instrument models.

---

## 8. What the visitor sees (one screen)

The cohort percentiles below are real: *S* 42,300–45,600 against the current timsTOF HT 60 SPD rows
with a Hive raw path (n = 577) gives p59–p83, widened to p56–p85 for cohort sampling. **The
scaled range itself is illustrative, not a fitted result.**

```
Where does my run sit?
 Precursors [ 61,200 ]   Instrument [timsTOF HT]   LC [Evosep 60 SPD]   Amount [50 ng]
 Engine [DIA-NN]  Version [2.7.0]  Library [Library-free]  Searched [Alone]
 FDR [1%]  Number from [report.stats.tsv]  Search space [STAN's digest]  Acquisition [dia-PASEF]

 ┌──────────────────────────────────────────────────────────────────────────────┐
 │ You entered 61,200 precursors (DIA-NN 2.7.0 · library-free · single run ·    │
 │ 1% · stats.tsv).                                                             │
 │ On STAN's standard search that is about 44,100 (42,300–45,600, 80% range).   │
 │ p56–p85 of timsTOF HT · Evosep 60 SPD · 50 ng (577 runs)                     │
 │ |····:····:····:····:····:····:····:····:····:██████████████▓▓·····:····|    │
 │ We searched 24 of our own HeLa QC files with both DIA-NN 2.7.0 and STAN's    │
 │ search; the range covers 80% of those paired files.                          │
 │ Calibration 1.0.0 · one facility · exact placement: run STAN on the raw file │
 └──────────────────────────────────────────────────────────────────────────────┘
```

Other states, each still one screen:
- **Top of cohort:** "≥ p78 (top of cohort). STAN's library for this instrument holds 51,099
  precursors, so the deepest runs are packed close together."
- **R7 above range:** "at least p90. Your count is above every calibrated run of this setup."
- **R9:** "About 38,000–47,000 on STAN's scale: too uncertain to place in the cohort."
- **Refusals (R1–R6, R8, R10):** the typed number, the rule's sentence, the live setups for this
  engine, and "For an exact placement, run STAN on the raw file."

---

## 9. Only Brett can do, or decide

1. **D0.** Choose STAN's reference library: the per-instrument subsets (current production) or the
   full community libraries (current `community_params.py`). Correct
   `COMMUNITY_LIBRARY_PRECURSOR_COUNT` and populate `library_coverage_pct` to match.
2. **D1.** Choose how to handle single-lab-install counts (DIA-NN 2.3.2, 3–5% lower on Orbitraps)
   in cohorts: correct, flag, or leave.
3. **D2.** Priority of engine versions. The default proposal is 2.7.0, 2.3.0 and 1.8.1, then
   probes. Visitor usage is **unknown**.
4. **D3.** The FASTA and digest for library-free arms. The options are STAN's
   `human_hela_202604.fasta` (whose contents, HeLa-restricted or full proteome, should be
   confirmed) or UniProt UP000005640, the FASTA FRAN's cached library uses, which visitors more
   likely use. Also decide whether to run D''.
5. **Approvals.**
   - Compute: pilot about 340 core-h, then Phase 2.
   - Importing DIA-NN 1.8.1 / 1.9.2 / 2.0.2 / 2.2.0 Linux builds via the Mac.
   - The pipeline skill's single confirmation, with FRAN deposit off.
6. **Spectronaut.**
   - Run it on the licensed Windows workstation, or ask Biognosys for Linux or offline activation.
   - Version 20.x at least, ideally 19 and 21 too. directDIA+ with BGS Factory Settings; record Fast
     or Deep and the full build string.
   - Per raw as its own experiment, plus one 8-run experiment per cohort. Same FASTA as D3.
   - Export `R.FileName`, `EG.PrecursorId`, `EG.Qvalue`, `EG.Identified` (if exportable) and the
     run summary to `…/engine_scaling/<panel_id>/spectronaut_<ver>/`.
   - Confirm the exact label of the per-run precursor number (F6).
7. **Confirm the Excel QC-log settings:** DIA-NN 1.8 library-free? Which FASTA? Single or multi-run?
   MBR?
8. **Screen the panel:**
   - `Ex300125_HeL50_30m.raw` is filed under 19 SPD;
   - `Ex270126_HeL100_30m_1.raw` is 100 ng;
   - check the DDA-named rows;
   - three names contain `--`, which need `_sanitize_path_for_diann` staging.
9. **Optional:** recover Spectronaut versions for the 83 FRAN_reports HeLa runs that match PG (from
   the `.sne` or the FRAN DB) to use as G10 cross-checks. The ratio PG/SN swings from 0.65–0.79
   (multi-run) to 1.56–1.74 (2022 single-run), so they cannot be fit data.
10. **Security, separate from this design:** a Spectronaut licence key is in plain text in
    world-readable `…/spectronaut/results/2025102*_PXD032759/PXD032759_AnalysisLog.txt`. Run
    `chmod o-r` and consider rotating the key.
11. **Sign off the G2–G9 thresholds** after the pilot.

---

## 10. Differences from the Part B spec (critique)

1. **The ceiling is per instrument model and is smaller than assumed.** *L* is 51,099 / 52,726 /
   168,557, not about 54k / 170k per vendor. Exploris is library-limited (up to 83%), not at 13.7%.
2. **Fixed logit (λ = 1) is not safe as the primary form.** On existing pairs it lost to the power
   law wherever *N* itself came from a finite library, by up to 2×. The λ-family nests both, and CV
   chooses.
3. ***S* must come from arm A,** not from any PG row. PG holds mixed-provenance rows, 219 duplicate
   raws, and values above *L*.
4. **"Where the number came from" is a required field.** STAN's Q-only count is 1.1% (timsTOF),
   3.9% (Lumos) and 2.2% (Exploris) above DIA-NN's own stats count on the same search, and up to 9%
   on weak runs.
5. **Calibrate 5% FDR instead of refusing it.** The DIA-NN default has been 5% since 2.5.0.
   `--qvalue 0.05` gives both levels from one search.
6. **Jackknife+ instead of split conformal,** because n = 24 is too small to split. Coverage is
   tested with a binomial test per stratum and a window only when pooled, since a 75–85% window is
   untestable at n = 24.
7. **Panel.** 72 verified raws over 9 cohorts, plus 24 prospective raws as the true hold-out, instead
   of 12 + 4 over 7 cohorts. Expand after the pilot only if G2 or G6 fail.
8. **Phase 0 uses existing pairs.** The single-lab 2.3.2 install pairs and the 1.8 QC log (pending
   confirmation) exercise the whole pipeline before any compute.
9. **The subset-library effect is not zero.** It was 1.034 on timsTOF (confounded, multi-run). It is
   measured by arm B and settles D0.
10. **MBR** is calibrated from batches of 4 and 8 that reuse first-pass `.quant` files, rather than
    batches of 10 and 40. More than 10 runs is refused.
11. **Spectronaut cannot run on Hive** (the licence is Windows-only). The existing 83 SN–PG pairs are
    cross-checks, not fit data.

---

## 11. Unknowns (not estimated here)

- Library-free cost with STAN's digest, and on 90–128 min Orbitrap HeLa. The pilot measures both.
- DIA-NN run-to-run determinism on Hive (G1, C1).
- Whether the batch first pass equals a single-file search (C2); whether `--qvalue 0.05` leaves the
  1% set and the MBR library unchanged (C3); whether 2.6/2.7 read `.raw` on Hive (C4).
- DIA-NN's GUI default digest, and what fraction of visitors use which version, count source or FDR.
- Transfer error to other labs' instruments of the same model. There is one facility per model
  today.
- Why the DIA-NN 2.3.2 PC report is already global-filtered while the Hive 2.3.0 report is not:
  a version difference or an invocation difference.
- Spectronaut runtime, which versions Brett's licence covers, and whether directDIA+ Deep or Fast
  is the factory default in each version.
- The settings behind the Excel DIA-NN 1.8 numbers, and the 0.41× DIA-NN 2.2.0 short-course
  searches (not investigated).
- Queue wait on `low` while FRAN's corpus re-search runs.

Evidence files (this folder):
- `inventory.md`, `sources.md`, `feasibility.md`;
- `paired_sets_vs_stan_standard_noDDA.tsv`, `excel_vs_pg.tsv`, `ceiling_table.txt`,
  `panel_final.tsv`;
- `demo_fit.py` / `demo_fit.out`, `demo_lambda.py` / `demo_lambda.out` (zero-compute model-shape
  check).
