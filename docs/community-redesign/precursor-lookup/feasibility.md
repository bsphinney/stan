# Calibration panel on Hive: feasibility notes

Measured 2026-09-29. Only read-only work was done: PG aggregates, sacct history, directory listings, and two small read-only scan jobs (24194373, 24194394, both on partition `low`, output in `~/stan_scaling/` on Hive). No searches were submitted.

## 0. Findings that change the design

1. **`runs.library_coverage_pct` is NULL on every PG row** (0 of 4,636 DIA rows). The "~90 %" figure is not stored anywhere. It only exists when you recompute it, and the hardcoded denominators are wrong for what was actually searched:
   - `COMMUNITY_LIBRARY_PRECURSOR_COUNT` = 54,000 (bruker) and 170,000 (thermo). These are the full community libraries.
   - Production Hive searches do not use the full library for two of the three instruments. `run_one_v1.run_diann` uses the per-host subset whenever it exists. The 3,633 DIA-NN logs under `/quobyte/proteomics-grp/STAN/processing` (all DIA-NN **2.3.0**, via `diann_2.3.0.sif`) show:

     | instrument | library actually searched | target precursors |
     |---|---|---|
     | timsTOF HT | `STAN/TIMS-10878/instrument_library.parquet` (subset) | **51,099** (DIA-NN reports 51,487 loaded) |
     | Exploris 480 | `STAN/DESKTOP-FOT3DAA/instrument_library.parquet` (subset) | **52,726** (53,248 loaded) |
     | Fusion Lumos | `stan_community_assets/hela_orbitrap_202604.parquet` (full) | **168,557** (170,284 loaded) |

     The full community libraries hold 53,039 (timsTOF) and 168,557 (Orbitrap) targets. Both subsets are strict subsets (0 precursors outside the community library). So the Exploris ceiling is about 3.2x lower than the 170k constant implies.
2. For Hive-searched runs, the PG `n_precursors` equals DIA-NN's "Number of IDs at 0.01 FDR" log line exactly (ratio 1.000 at p10, p50 and p90 across all cohorts).
3. The "memory" note says the per-instrument subsets are a speed-only optimisation with identical IDs. Nobody has measured that. FDR is estimated against a library built from this instrument's own past detections. That is exactly the kind of effect the panel must measure (arm B below).
4. Every PG row has `amount_ng = 50.0`, including files named `HeL100`. The value is a default, not a measurement. A visitor who enters any other amount is outside the data.
5. `ms2_analyzer` is `unknown` on every Lumos and Exploris row, so Lumos OT-vs-IT MS2 cannot be stratified from PG.
6. PG holds duplicate rows for renamed copies of the same acquisition (for example `Ex110225_HeL50_30m_4OK.raw` and `..._4_OK.raw`, identical size). Some rows also carry misleading names: a `..._DDA_60m.raw` sits in a DIA cohort with 27.5k precursors, and `Ex300125_HeL50_30m.raw` is filed under 19 SPD. De-duplicate and screen the panel by hand.

## 1. DIA-NN on Hive

| path | version | how it runs | Thermo .raw |
|---|---|---|---|
| `/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif` (+ `.def`) | 2.3.0 | Apptainer, Ubuntu 22.04, `dotnet-sdk-8.0` from the MS feed, binary `/diann-2.3.0/diann-linux` | **yes**. This is STAN production: 1,107 Lumos + 1,097 Exploris logs |
| `/quobyte/proteomics-grp/apptainers/diann2.3.0.sif` (no underscore) | 2.3.0 (Preview zip) | Apptainer, no .NET | **no**. Silently skips .raw (CLAUDE.md gotcha) |
| `/quobyte/proteomics-grp/dia-nn/build_251/diann-2.5.1/` | 2.5.1 (banner confirmed) | native dir, ships RawFileReader DLLs | yes, when run **inside diann_2.3.0.sif** by bind-mount. Proven by test job 15062017 (3 Exploris .raw, library-free, 25.8–31.5k IDs). The `.sif` build failed (mksquashfs `malloc(): corrupted top size`) |
| `/quobyte/proteomics-grp/dia-nn/build_260/diann-2.6.0/` | 2.6.0 | native dir | not verified |
| `/quobyte/proteomics-grp/dia-nn/build_261/diann-2.6.1/` | 2.6.1 | native dir | not verified |
| `/quobyte/proteomics-grp/dia-nn/build_270/diann-2.7.0/` | 2.7.0 | native dir; pipeline-skill pin | not verified on .raw (FRAN logs are all .d) |
| `/quobyte/proteomics-grp/fran/engines/pilot_tools/diann/2.7.0/` | 2.7.0 (banner in FRAN logs) | FRAN's copy, native | .d proven (610 first passes) |
| `apptainers/alphadia.sif`, `dia-analyst_v0.10.5.sif`, `radiant-fulcrum-2.3.3.sif` | — | other DIA engines | — |

- A native build needs .NET 8 to read .raw. The DIA-NN README says ".NET SDK 8.0-series (version 8.0.407 or later)". The pipeline skill says runtime ≥ 8.0.17. The Hive module `dotnet-core-sdk/8.0.4` is too old for this, so the proven route is: `apptainer exec --bind ... diann_2.3.0.sif /b/diann-X/diann-linux`.
- Missing and commonly used by visitors: **1.8.1, 1.9.x, 2.0.x, 2.1.0, 2.2.0**, plus all Windows builds. Hive has no internet egress, so a missing version comes in like this:
  1. Download the `DIA-NN-<v>-Academia-Linux.zip` from GitHub releases on the Mac.
  2. Pipe it with `ssh hive 'cat > ...'`. Plain `scp` closed the connection today.
  3. Unzip it to `/quobyte/proteomics-grp/dia-nn/build_<nnn>/diann-<v>/`.
  4. Run it through `diann_2.3.0.sif` (glibc 2.35).
- Per the release notes, native Linux .raw support arrived in **2.1.0**. For 1.8.1, 1.9.x and 2.0.x on Thermo you first convert to mzML with `apptainers/msconvert.sif`. That adds a mzML-vs-.raw confounder, so measure it with 2.3.0 on a few files.
- Versions before 2.0 cannot read `.parquet` libraries, so the library-mode arm for them needs a .tsv or .speclib export of the frozen library. That is another confounder.

## 2. Runtime (sacct 2026-07-01 → today and DIA-NN logs)

STAN production uses `low`, 8 CPUs and 32 GB per raw, and the job also does extraction and 4DFF. 3,578 COMPLETED, 320 FAILED, 11 OOM.

| cohort | SLURM job wall p50 | CPU-time p50 / p90 | MaxRSS p50 / max | DIA-NN wall p50 / p90 (log) |
|---|---|---|---|---|
| timsTOF 100 SPD (n=122) | 4.8 min | 0.2 / 1.2 core-h | 14.9 / 25.7 GB | 1.5 / 2.8 min |
| timsTOF 60 SPD (n=128) | 5.8 min | 0.2 / 2.4 core-h | 14.4 / 32 GB | 2.0 / 3.7 min |
| timsTOF 30 SPD (n=3; logs n=49) | 12.3 min | 1.0 / 1.3 core-h | 32 GB (at cap) | 3.2 / 5.5 min |
| Exploris 38 / 19 / 12 SPD | 4.4 / 7.2 / 9.0 min | ≤ 0.2 core-h | 2–7 GB | 0.8 / 0.9 / 1.2 min |
| Lumos 32 / 19 / 12 / 9 SPD | 4.0 / 5.9 / 8.2 / 10.3 min | 0.2–0.4 core-h | 2.4–7.4 GB | 1.3 / 1.3 / 2.1 / 2.4 min |

A few logs hit the 3 h subprocess timeout; those are I/O stalls, not search time. Frozen-library searches are about 1–4 min of DIA-NN time. Budget **≤ 1 core-h allocated per search** (8 CPUs × ~7 min).

### Library-free cost

- **Library prediction (FRAN `diann_libpred`, 2.7.0, 16 CPUs):** 12–21 min, which is 3–5 core-h, once per (version × FASTA × params). A human UP000005640 2.7.0 predicted library already exists at `/quobyte/proteomics-grp/fran/speclib_cache/ad9ed5c6.../step1.predicted.speclib`. It uses FRAN's parameters: 2 missed cleavages, M-ox, 299–1201 m/z.
- **timsTOF .d first pass (FRAN, 2.7.0, 16 threads, ~6.9M-entry library, 610 file-passes / 30 searches):** p10 27.8, p50 **35.9**, p90 48.0 min per file. That is **9.6 core-h (p50), 12.8 (p90) per .d**. A STAN-matched digest (1 missed cleavage, no M-ox) is roughly 1/2–1/3 the size, so expect about 4–7 core-h. Plan for **~10 core-h per timsTOF raw**.
- **Orbitrap .raw (2.5.1 test job 15062017, 32 threads, 3.66M-entry mouse library, 3 Exploris files):**
  - prediction 3.3 min
  - first pass about 3–4.5 min per file, including ~1.5 min to load the .raw
  - MBR second pass about 1 min per file
  - total about **1.5–2 core-h per file**
  
  Lumos and Exploris HeLa runs at 90–128 min gradients will be larger. Plan for **~3 core-h per Orbitrap raw**.
- **Speed ratio:** library-free is about 30–100x the frozen-library search per raw.

## 3. Library ceiling (PG aggregates, hidden=0, DIA, n_precursors>0)

Coverage is recomputed against the library that was actually searched: 51,099 / 52,726 / 168,557. The `hive` rows are restricted to runs with a Hive DIA-NN log.

| instrument | SPD | n | n_prec p5 / p25 / p50 / p75 / p90 / p95 / p99 / max | coverage % p50 / p90 / p99 / max | ≥80 % | ≥90 % |
|---|---|---|---|---|---|---|
| timsTOF HT | 100 | 828 | 5826 / 23523 / 34412 / 39569 / 42708 / 43778 / 45356 / 46826 | 67 / 84 / 89 / 92 | 146 (18 %) | 2 |
| timsTOF HT | 60 | 751 | 8659 / 30597 / 40526 / 44427 / 46318 / 47141 / 48240 / 49934 | 79 / 91 / 94 / 98 | 362 (48 %) | 97 (13 %) |
| timsTOF HT | 60 (hive) | 653 | … p50 40775, p90 46318 | 80 / 91 / 94 / 96 | 322 (49 %) | 87 |
| timsTOF HT | 30 | 63 | 27357 / 38807 / 44008 / 47028 / 48447 / 48726 / 51289 / 51289 | 86 / 95 / 100 / 100 | 44 (70 %) | 26 (41 %) |
| Exploris 480 | 12 | 174 | 15149 / 23683 / 30588 / 36281 / 40565 / 42076 / 43748 / 43748 | 58 / 77 / 83 / 83 | 7 (4 %) | 0 |
| Exploris 480 | 19 | 347 | … p50 23047, max 33863 | 44 / 55 / 61 / 64 | 0 | 0 |
| Exploris 480 | 38 | 735 | … p50 22616, max 36995 | 43 / 56 / 62 / 70 | 0 | 0 |
| Exploris 480 | 9 | 40 | … p50 26381, max 40095 | 50 / 69 / 76 / 76 | 0 | 0 |
| Lumos | 9 | 92 | … p50 49254, max 84857 | 29 / 35 / 50 / 50 | 0 | 0 |
| Lumos | 12 / 19 / 32 | 319 / 257 / 623 | p50 31415 / 30121 / 26207 | ≤ 32 max | 0 | 0 |

- Measured against the naive 54,000, timsTOF gives 65 / 246 / 39 runs ≥ 80 % and 0 / 4 / 4 ≥ 90 %. Against the library actually searched it is 146 / 362 / 44 and 2 / 97 / 26.
- 244 timsTOF rows have no Hive log (DIA-NN "2.3.2" or unknown, presumably searched on the instrument PC). Their max is 51,289, which is above the 51,099-target subset, so they were probably searched against a different (full) library.
- **Implication:**
  - A linear or multiplicative scale is fine on Lumos (< 50 % used) and on most of Exploris.
  - Exploris 12 SPD touches 83 %.
  - timsTOF 60 and 30 SPD are **library-saturated in their top half**. The mapping from engine count to STAN count must be monotone and saturating, for example `STAN ≈ L·(1 − exp(−(x/s)^b))` or isotonic, fit per family with SPD as a covariate.
  - Near the top the STAN cohort itself is compressed: the top ~20 % of timsTOF 60 SPD spans only 45.2k–49.9k. A visitor's percentile loses resolution there, so the box should report "≥ p80" style buckets rather than a point estimate once the STAN-equivalent exceeds ~85 % of L.
  - The ratio STAN / other-engine is expected to vary with depth, which is why a single factor is wrong. Measure this; don't assume it.

## 4. Spectronaut

- Hive has Linux CLI tarballs `/quobyte/proteomics-grp/spectronaut/Spectronaut_20.1.250624.92449.tgz` and `Spectronaut_20.3.251119.92449.tgz`. The installed copy is `binaries/Spectronaut/bin/SpectronautCMD.dll`, dated 2025-11-19, which matches 20.3.251119. Run it with `module load dotnet-core-sdk/8.0.4; dotnet SpectronautCMD.dll`.
- **The licence blocks it.**
  - Probe job 22927780 (2026-09-10): `Current license does not allow for the execution on non-Windows operating systems … Insufficient license! Aborting.`
  - Array jobs on 2025-12-02: `No valid license found!`
  - A 2025-10-20 test ran (it produced an .sne) but got 0 IDs because the FASTA and settings paths were wrong.
- The core's licence is Windows-only. **Brett has to run Spectronaut himself on a licensed Windows workstation, or get Biognosys to enable Linux (or offline activation) on the licence.**
- What he needs to run:
  - For each Spectronaut version visitors use (at least the current 20.x, ideally also 19.x and 18.x), a single-run **directDIA** search on the panel raws with BGS Factory Settings.
  - The human FASTA used for the DIA-NN arms.
  - Export: run-wise "Unique precursors (Qvalue ≤ 0.01)" from the AnalysisLog / Run Summary, plus the report with `EG.PrecursorId`, `EG.Qvalue`, `R.FileName`.
  - Optional second arm: the same raws as one experiment (experiment-wide FDR), to measure the batch effect.
  - The raws can be read straight from Flinders (~74 GB for the panel).
  - Runtime on Windows is unmeasured. A guess is 15–45 min per timsTOF run and 10–20 min per Orbitrap run, so about 1–2 machine-days per version for 72 raws.
- **Security (report it, don't act on it):** a Spectronaut licence key appears in plain text in the echoed command line of `/quobyte/proteomics-grp/spectronaut/results/2025102*_PXD032759/PXD032759_AnalysisLog.txt`. At least one of these files has mode `-rw-rw-r--` (world-readable). The `submit_*license*` scripts may also embed it. Consider `chmod o-r` and rotating the key. `~/.sn_test_key` is correctly 0600.

## 5. Proposed panel (not submitted)

**Raws (72; `panel_final.tsv` in this folder).** There are 9 cohorts: timsTOF 100/60/30, Exploris 38/19/12 and Lumos 32/19/12 SPD. Each cohort has 8 depth quantiles (p05, p20, p35, p50, p65, p80, p90, p97) of STAN `n_precursors`, taken from runs since 2024-09.

- Every path was checked with `test -e` on Hive: 24 timsTOF `.d` (36.1 GB) and 48 Thermo `.raw` (37.8 GB).
- 11 of the first-choice rows had moved on Flinders or held a Windows `D:\` raw_path. They were replaced by the nearest-quantile alternate that exists.
- The top quantiles are deliberately oversampled for the ceiling.
- Before use, screen three things:
  - `Ex300125_HeL50_30m.raw` is filed as 19 SPD.
  - `Ex270126_HeL100_30m_1.raw` is 100 ng.
  - Three names contain `--`, so use `_sanitize_path_for_diann` staging.

**Configurations** (single-run unless stated; count every one the same way, see "Counts" below):

| arm | engine / library | purpose | searches | core-h (plan) |
|---|---|---|---|---|
| A | DIA-NN 2.3.0 sif, production library (subset or full), STAN frozen params | harness check; must reproduce PG `n_precursors` exactly | 72 | ~40 |
| B | DIA-NN 2.3.0, **full** community library (53,039 / 168,557) | isolates the subset-library effect (timsTOF, Exploris) | 48 | ~25 |
| C | DIA-NN 2.5.1, 2.6.1, 2.7.0, full community library | version effect in library mode | 216 | ~110 |
| C' | + imported 1.9.2, 2.0.2, 2.1.0, 2.2.0 (library as .tsv for < 2.0; mzML for < 2.1 on Thermo) | older versions still in use | 288 | ~150 |
| D/E | **library-free** (predicted from human FASTA, STAN-matched digest), DIA-NN 2.7.0 and 2.3.0. Batched per cohort (8 files) with `--reanalyse`: `report-first-pass.parquet` gives single-run counts, `report.parquet` gives MBR counts | what most visitors actually report; MBR on/off comes almost free | 2 × 72 | 2 × (24×10 + 48×3 + 5 + ~50 MBR) ≈ **880** |
| D' | library-free, 1.9.2 / 2.0.2 / 2.2.0 | optional | 3 × 72 | ~1,300 |
| D'' | library-free with FRAN / DIA-NN-GUI-default digest (2 missed cleavages, M-ox), using the cached FRAN 2.7.0 human library | parameter sensitivity | 72 | ~440 |
| S | Spectronaut 20.x directDIA (Brett, Windows) | Spectronaut box option | 72 | Windows machine time |

- **Minimum useful set (A + B + C + D/E for 2.7.0 and 2.3.0):** about **1,050 core-h**.
- **Cheaper tier:** run library-free on only 4 depth points per cohort (p20, p50, p80, p97; 36 raws). That is about **~500 core-h**.
- On `low` with 16-CPU jobs and ~15 concurrent, each library-free version takes about 2–4 h wall.
- FRAN is currently saturating `low` with `diann_libpred`, `diann_search` and `s2_firstpass` jobs, so expect queueing. `QOSGrpCpuLimit` on `high` is 64 CPUs.

**Controls:**
1. Arm A equals PG exactly.
2. On 3 files, single-run versus the batch first pass (confirm they are equal).
3. On 4 Thermo files, 2.3.0 on .raw versus mzML.
4. Optional: 2 files on Windows DIA-NN versus Linux, same version.

**Counts to record for every output**, so the box can ask which number the visitor has:
1. STAN definition: unique `Precursor.Id` with run `Q.Value ≤ 0.01`.
2. The log line "Number of IDs at 0.01 FDR".
3. `report.stats.tsv` Precursors.Identified.
4. `Global.Q.Value ≤ 0.01`.
5. MBR final versus first pass.
6. Spectronaut: run-wise and experiment-wide "Unique precursors".

**Outputs:** put them under `/quobyte/proteomics-grp/STAN/calibration/<arm>/<version>/<raw_stem>/`. They **must stay under `/quobyte/proteomics-grp/STAN/`**, because FRAN's `DEFAULT_EXCLUDES` prefix is what stops FRAN ingesting these `report.parquet` files as customer searches. Predicted libraries go in `STAN/calibration/libs/`, at ~0.9–2 GB each. Total storage is about 30–60 GB. `$HOME` has only 3.7 GB free, so never stage outputs there. SLURM logs go to `/quobyte/proteomics-grp/STAN/logs/calibration/`.

**Fit:**
- Per family: a monotone saturating map from engine count to STAN count, with SPD as covariate and depth-dependent ratio.
- Leave-one-cohort-out validation.
- Report prediction intervals, and the percentile band rather than a point estimate in the saturated zone.
- Because the 72 raws are a stratified sample of the STAN cohorts, the fitted map can be inverted into "visitor count → STAN-equivalent → cohort percentile".

## Files

- `ceiling_table.txt`, `pg_agg.out` (PG aggregates), `pg_runs.tsv` (run_name / instrument / spd / n_precursors, for joins)
- `sacct_stan_X.txt`, `sacct_batch.txt`, `diann_logs.tsv` (3,653 production logs), `libs.json`, `fran_firstpass.tsv`
- `panel_final.tsv` (the 72-raw panel), `panel_candidates*.tsv`, `panel_exist.tsv`, `alt_exist.tsv`
- `hive/scan_logs.*`, `hive/scan_fran.*` (the two read-only jobs; copies in `~/stan_scaling/` on Hive)
