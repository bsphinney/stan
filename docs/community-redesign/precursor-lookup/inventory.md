# Paired-search inventory for the "Where does my run sit?" scaling (raw notes)

Written 2026-09-29 by the inventory subagent. Everything here is read-only research: no searches were
submitted, nothing in the repo or PG was modified. Every number cites the query, command or path that produced it.

Scratch files (all under this `scaling/` dir):
- `q1.py`..`q6.py`: PG queries, piped to the Hive stan_venv python with PGPASSWORD from `/quobyte/proteomics-grp/brett/.pgfarm_token`.
- `q2.out`, `q3.out`, `q4.out`, `q5.out`: their outputs. `pg_runs_slim.tsv` is q6: bn, instrument, counts, spd and version for all 4,666 runs (600 KB).
- `scan/`: output of SLURM jobs 24194340-43 (`scan_searches.py`, partition low). It parsed every DIA-NN `*.log.txt`, `diann.log` and `diann.stdout.log`,
  every sibling `*.stats.tsv`, and Spectronaut-looking files under bounded roots (listed in scan_searches.py).
- `sn_counts.py` / `sn_run_counts.tsv` / `sn_meta.jsonl`: SLURM job 24194456. It computed per-run Spectronaut precursor counts from `FRAN_reports`.
- `count_defs.py`: SLURM job 24194491 (count-definition check). md5 of the libraries came from SLURM job 24194507, output in `$HOME/scaling_inventory/md5_24194507.out` on Hive.
- `hive_copies/`: QC-LUDiaHeLVar.xlsx, reference.json, comparison.csv and comparison_astral_vs_lumos.tsv, copied from Hive.
- `paired_raws_vs_stan_standard.tsv` and `paired_sets_vs_stan_standard_noDDA.tsv`: `final_pairs.py`. `excel_vs_pg.tsv` and `sn_vs_pg.tsv`: the joins.

## 0. Things the calibration design must know first

1. **PG does not record the library or search params.** `information_schema.columns` for `runs` (q1.py) has 67 columns.
   The provenance columns are only `search_engine`, `diann_version` and `stan_version`, plus `library_coverage_pct`.
   **`library_coverage_pct` is NULL for all 4,666 rows** (q2.py, `sum(case when library_coverage_pct is not null ...)` = 0 in every group).
   The "~90 % coverage" premise cannot be read from PG today.
2. **The STAN cohort is not one library.** `stan/community/scripts/run_one_v1.py` ~L240-258 prefers
   `<MIRROR_BASE>/<HOST>/instrument_library.parquet` when it exists. The report.log.txt command lines confirm what was used (scan/ logs):
   - timsTOF HT → `/quobyte/proteomics-grp/STAN/TIMS-10878/instrument_library.parquet`: **51,487 precursors**, md5 674cde06…, 2026-04-10.
     This covers 1,426 processing dirs and 164 v1_smoke dirs.
   - Exploris 480 → `/quobyte/proteomics-grp/STAN/DESKTOP-FOT3DAA/instrument_library.parquet`: **53,248 precursors**, md5 c1e2a725…
     This covers 1,083 processing and 75 v1_smoke dirs. **It is not the 170,284 Orbitrap library.** Only 28 Exploris processing dirs used hela_orbitrap_202604.
   - Fusion Lumos → no instrument library at `STAN/lumosRox/` (MISSING in the md5 job), so it uses the frozen
     `hela_orbitrap_202604.parquet` (170,284 precursors, md5 ac84e40f…, matching `stan/community/validate.py`). That covers 1,078 processing and 157 v1_smoke dirs.
   - The frozen `hela_timstof_202604.parquet` (53,580 precursors, md5 ad72bfb2…) is **not** what produces PG timsTOF numbers.
3. **Libraries that look different but are byte-identical** (md5 job 24194507):
   `de-limp/downloads/PXD054015/astral_hela_lib/report-lib.parquet` == `hela_qcs/lib/hela_orbitrap_202604.parquet` == community Orbitrap lib (ac84e40f…).
   `hela_qcs/timstof_hela_lib/report-lib.parquet` == community timsTOF lib (ad72bfb2…).
4. **Count definition differs from what a DIA-NN user reads.** STAN counts unique Precursor.Id with Q.Value ≤ 0.01 in report.parquet (extractor.py ~L500/637).
   The DIA-NN 2.3.0 single-run report.parquet on Hive is **not Global.Q-filtered**. DIA-NN's own "Number of IDs at 0.01 FDR" and `report.stats.tsv`
   `Precursors.Identified` ≈ Q ≤ 0.01 AND Global.Q ≤ 0.01. Source: count_defs.py, job 24194491:
   - processing/02032026_HE50_100-spd-dia_S1-A2_1_19747: Q-only 42,697 (= PG value); Q∧GlobalQ 42,227; stats 42,261.
   - Same raw on the instrument PC (DIA-NN 2.3.2, `TIMS-10878/baseline_output/`): Q-only 43,013 = Q∧GlobalQ 43,013 = log "Number of IDs" 43,013.
     So the 2.3.2 report is already global-filtered.
   - Ex270723_HeL50-fDia_30m_1: 7,965 vs 7,530 (+5.8 %). FL041022_…_lo2: 13,101 vs 12,001 (+9 %). **The gap is larger for weak runs.**
   - Ratio of the PG value to the same raw's stats count, over all runs (paired by basename; `final_pairs.py` precursor step): timsTOF 1.011 (n = 1,346),
     Lumos 1.039 (n = 1,318), Exploris 1.022 (n = 1,370).
   → **A visitor's DIA-NN "IDs at 1 % FDR" is ~1-4 % below the STAN number from the same search before any engine or version effect.**
     The same Q-only count definition therefore has to be applied to anything paired with STAN.
5. **PG contamination.** 67 Exploris rows whose raw name contains "dda" sit in PG as `mode='DIA'` with n_precursors>0.
   Examples: ex040524_hel50-dda_30m …; query in the last `final_pairs` check. These are DDA acquisitions searched as DIA. They were excluded from pairs.

## 1. PG runs table (q2-q5)

| instrument | mode | engine | diann_version | rows | run_date range |
|---|---|---|---|---|---|
| Exploris 480 | DIA | diann | 2.3.0 | 1,321 | 2021-03-01 → 2026-09-15 |
| Exploris 480 | DIA | diann | unknown | 28 | |
| Fusion Lumos | DIA | diann | 2.3.0 | 1,293 | 2019-07 → 2026-09-25 |
| Fusion Lumos | DIA | diann | unknown | 271 | |
| Fusion Lumos | DDA | sage | NULL / "2.3.0" | 10 / 1 | |
| timsTOF HT | diaPASEF | diann | 2.3.0 | 1,411 | 2023-07 → 2026-09-29 |
| timsTOF HT | diaPASEF | diann | **2.3.2** | **245** | 2023-07 → 2026-05-05 |
| timsTOF HT | diaPASEF | diann | unknown | 68 | (all n_precursors = 0) |
| timsTOF HT | ddaPASEF | sage | NULL | 18 | |

- All 2.3.2 rows are timsTOF instrument-PC searches: stan_version 0.2.222/0.2.234/0.2.238/0.2.255/0.2.295, raw_path `D:\Data\…` (q5 "windows-path count" = 245/245).
- The 4,666 rows cover 3,676 distinct raw basenames (q3). Duplicate basenames with identical counts are the same search stored twice (q5), not pairs.
- **Only within-PG pairing:** 219 timsTOF raws have both a 2.3.0 row (Hive, stan 0.2.376, /nfs path) and a 2.3.2 row (instrument PC) (q4).
  Ratio 2.3.2/2.3.0: median 1.007, p10 0.985, p90 1.046, corr 0.997. By SPD: 30 → 1.031 (11), 60 → 1.005 (97), 100 → 1.009 (111).
  Five outlier pairs fall outside 0.9-1.1, all with low counts or failed runs.
  Caveat: the pair confounds version (2.3.0 vs 2.3.2), mass accuracy (auto vs fixed 15 ppm) **and** the count definition (point 0.4). The library is the same (instlib 51,487).
- Four Exploris basenames (ex140526_hel50_*) exist in both May and Jun folders with different counts. The same name was likely re-acquired, so they are not pairs.

## 2. Hive search outputs (scan jobs 24194340-43)

14,145 DIA-NN logs were parsed; 9,527 carried a command line. Versions seen on Hive/Flinders: 1.7.10, 1.7.11, 1.8, 1.8.1, 1.8.2, 1.9, 1.9.1, 2.0, 2.2.0, 2.3.0, 2.3.2, 2.5.1, 2.6.0, 2.6.1 and 2.7.0.
3,449 HeLa-named raws have per-run stats. 1,010 of them were searched under at least 2 distinct configs and 451 under at least 3 (`final_pairs.py`, canonical configs, identical-md5 libraries merged).
904 non-DDA HeLa raws have the **STAN-standard search plus at least one other config**, and 930 have a PG value plus at least one other config
(timsTOF 365, Lumos 426, Exploris 142).

STAN-standard config per instrument (what PG holds): DIA-NN 2.3.0, single-file, auto mass accuracy, no MBR, with the library in point 0.2.

Ratios are other config ÷ STAN-standard, taken from the same raw's `Precursors.Identified` (file level, same definition).
`med_pg` is other ÷ the PG value. Source: `paired_sets_vs_stan_standard_noDDA.tsv`.

| instrument | other config | n raws w/ STAN file / PG | median (p10-p90) file | median vs PG | where |
|---|---|---|---|---|---|
| timsTOF | 2.3.0 **community lib 53,580**, multi-file, auto MA | 184 / 183 | **1.034** (1.003-1.062) | 1.007 | hela_qcs/search_results/timstof_ownlib (249 .d) |
| timsTOF | 2.3.2 instlib 51,487, MA 15, single | 115 / 115 | 1.012 (0.994-1.051) | 0.998 | STAN/TIMS-10878/baseline_output |
| timsTOF | 2.3.2 community lib 53,580, MA 15, single | 62 / 62 | 1.034 (1.011-1.063) | 0.996 | same |
| timsTOF | 2.3.0 library-free + MBR, MA 14, multi | 25 / 25 | 1.048 (1.014-1.105) | 1.035 | brett/affinisep_Dec25/out/no_norm, de-limp/phospho/out |
| timsTOF | 2.6.1 2-step library-free → empirical lib 69,559 | 29 / 29 | **1.103** (1.060-1.156) | 1.090 | brett/PROT_0793/search_hela |
| timsTOF | 2.3.0 2-step library-free → empirical 45,470 (DE-LIMP) | 11 / 11 | 1.029 (0.968-1.067) | 1.014 | brett/affinisep_Dec25/delimp/Affinisep_* |
| timsTOF | 2.3.0 predicted lib 4.34 M + MBR | 11 / 11 | 1.045 | 1.030 | brett/affinisep_Dec25/subset/out |
| timsTOF | 2.3.0 first-pass predicted 10 M, no MBR | 11 / 11 | **0.802** | 0.786 | brett/affinisep_Dec25/delimp/Affinisep_search_Norm_off_* |
| timsTOF | **2.7.0 predicted library-free, no MBR, 4-file** | 8 / 8 | **0.913** (0.808-0.952) | 0.902 | brett/method_comparison_2026-09-23/spd60, spd100 |
| timsTOF | 2.2.0 sc_years lib / predicted lib | 5 / 5 | 0.42 / 0.41 (suspect) | | brett/short_course_data/dia-nn_results/4 and 2 |
| timsTOF | 2.7.0 FRAN corpus two-job library-free + MBR | 0 / 0 (8 raws, 2022-24) | | | FRAN_diann/corpus_2026 (raws not in STAN) |
| Lumos | 2.3.0 same community lib, MA 20, 422-file `--use-quant` | 254 / 256 | 1.006 (0.969-1.038) | 0.967 | hela_qcs/search_results/lumos_reextract |
| Lumos | 2.3.0 same lib (/work/lib/report-lib), multi, auto | 257 / 259 | 0.987 (0.879-1.022) | 0.940 | hela_qcs/search_results/lumos + stan_watcher reports |
| Lumos | 2.3.2 same lib, MA 20, single (instrument PC) | 212 / 212 | 1.007 (0.988-1.032) | 0.967 | STAN/lumosRox/baseline_output (214 Lumos dirs) |
| Lumos | 2.6.1 empirical 54,216, MA 20, multi | 14 / 14 | 1.141 (1.111-1.261) | 1.098 | fran/incoming/search__9ff203cf |
| Lumos | 2.7.0 FRAN two-job library-free + MBR (single file) | 3 / 3 (12 raws total) | 1.100 | 1.088 | FRAN_diann/corpus_2026/SpNdirD_FL2*_HeL50* |
| Lumos | 2.3.0 library-free (predicted), multi | 11 / 11 | 0.694 | 0.668 | hela_qcs/stan_watcher/lib_comparison/build_lumos_lib |
| Lumos | 2.3.0 empirical Lumos lib 32,967 | 11 / 11 | 0.777 | 0.754 | hela_qcs/stan_watcher/lib_comparison/lumos_lib |
| Exploris | 2.3.2 **community lib 170,284**, MA 20 (PC) | 91 / 91 | 0.975 (0.929-1.019) | 0.954 | STAN/DESKTOP-FOT3DAA/baseline_output |
| Exploris | 2.3.0 community lib 170,284, multi | 74 / 74 | 0.992 (0.941-1.095) | 0.970 | hela_qcs/search_results/480_dia + stan_watcher |
| Exploris | 1.9 library-free + MBR | 2 / 2 | 0.815 | 0.800 | Flinders Data/lab/Lauren/HeLa/13Sept2024_Ex |
| Exploris | 2.3.0 library-free **--dda** (19 raws) | not usable | | | hela_qcs/search_results/dda_480 (DDA search mode) |

Notes on the table:
- The Lumos library-free vs community-library comparison (lib_comparison, 11 raws) was built on 2025 Lumos + Exploris files (`library_comparison.sbatch`).
- A predicted library-free first pass on its own gives about 0.69-0.80× STAN. A 2-step library-free run (empirical lib rebuilt from the data) or MBR gives 1.03-1.14×.
  **The visitor's workflow class, not just the engine version, moves the number by up to ±30 %.**
- The PG ratio column is lower than the file-level column by the count-definition factor (0.4), as expected.

## 3. Historical QC log (Excel), DIA-NN 1.8/2.0 vs STAN

`/quobyte/proteomics-grp/hela_qcs/lumos/2025HeL/QC-LUDiaHeLVar.xlsx` (184 KB, 2026-03-20). The sheets are He50ng-DiaW22_35m, 60m, 90m, 120m, LUvsEx and "Hel10 5 1".
Its columns are DIA-NN stats (`File.Name`, `Precursors.Identified`, …).

- 682 rows cover 639 unique raws. All are Lumos except 5 Exploris rows in LUvsEx. Values are **DIA-NN 1.8** by default.
  Brett's in-sheet notes say "anlyd w/ diaNN20 … so scor larger" / "from here on, bak2 diaNN18" (`hive_copies/excel_rows.json`).
- **20 raws were searched with both DIA-NN 1.8 and 2.0**, all FL0707–FL3007 2025 at 35/60/90/120 min. Ratio 2.0/1.8: median **1.120**, range 1.056-1.192.
- 297 raws match PG. PG / Excel-1.8 on Lumos: median 1.069 (p10 0.989, p90 1.143, n = 292).
  The ratio depends on gradient: 35 m 1.048 (158), 60 m 1.072 (41), 90 m 1.093 (60), 120 m 1.157 (24). By year: 2023 0.994, 2024 1.091, 2025 1.070, 2026 1.068.
- File-level STAN-standard / Excel-1.8 (same stats definition): median 1.026 (n = 301).
- The library mode of the Excel searches is **unverified**. The only statement is a code comment in
  `/quobyte/proteomics-grp/hela_qcs/stan_watcher/search_dia.sbatch` saying they were library-free ("Brett's standard QC pipeline").
- `/quobyte/proteomics-grp/hela_qcs/stan_watcher/comparison.csv` has 382 rows comparing the STAN-watcher search (2.3.0, Astral = community lib, multi) with the Excel values.
  `reference.json` (578 keys) keeps only the last value per raw and lost the 1.8/2.0 split.

## 4. Spectronaut

- Spectronaut is installed on Hive: `/quobyte/proteomics-grp/spectronaut/`, tarballs 20.1.250624 and 20.3.251119.
  Its only results there are PXD032759 (not HeLa; the Normal report headers hold 0 data rows).
- `brett/sn21`, `sn21cmp` and `sn21win` hold Spectronaut 21 dog/yeast entrapment data (not HeLa).
- **Main source:** `/nfs/lssc0/flinders/proteomics/Data/FRAN_reports/`, with 2,006 FRAN exports of .sne searches (manageSNE → `*_Report_FRAN (Normal).parquet`).
  **76 are HeLa-named searches, 321 run rows, 204 HeLa-named runs.**
  Per-run counts come from job 24194456 (`sn_counts.py`): unique EG.ModifiedSequence+FG.Charge with EG.Qvalue ≤ 0.01, decoys excluded.
  The exports are already q-filtered: the q ≤ 0.01 and all-rows counts are equal for 94 % of rows.
- Modes seen in the AnalysisLog: Pulsar/directDIA/directDIA+. There are also library searches, e.g. "Opentron … timstof_qc_hela_lib". Analysis dates run 2021-2025 plus 2026 exports.
  **The Spectronaut version is not written in the exported AnalysisLog/params.** A regex for the NN.N.YYMMDD.NNNNN build pattern found none. The date is the only proxy.
- 83 of those raws match PG, from 30 searches. PG / Spectronaut: timsTOF 0.789 (n = 52, p10 0.65, p90 0.96), Lumos 0.767 (15), Exploris 0.651 (16).
  Split by experiment size: multi-run experiments give 0.65-0.79. Single-run 2022 directDIA searches give 1.56-1.74 on Orbitraps, i.e. the old single-file SN gave fewer IDs.
  **Spectronaut per-run counts depend strongly on experiment size (directDIA+ library pooling) and version.**
- FRAN_diann `corpus_2026/SpNdirD_*`, `20220909_*Hela*`, `20231012_*`, `20240509_*` and others hold 24 HeLa corpus searches.
  They are the same raws re-searched with DIA-NN 2.7.0, two-job library-free + MBR.
  So for about 20 raws there are Spectronaut, DIA-NN 2.7.0 and (for some) STAN values: 2.7.0/SN ≈ 1.64 on the 12 Lumos raws.
- The FRAN corpus DB (per its CLAUDE.md, 437 M precursor rows of Spectronaut ingests) probably holds more HeLa Spectronaut runs. It was not queried: a separate DB, out of scope.
- Bounded Mac search (`find ~/Documents ~/Downloads ~/Desktop -maxdepth 5`, Spectronaut/SNE/report patterns) found no HeLa Spectronaut output.

## 5. Not HeLa, but engine-paired

`/quobyte/proteomics-grp/brett/engine_comparison/` (RECOVERY.md, `engine_comparison_per_run.tsv`) has 12 **mouse** timsTOF runs (course samples) in three arms.
The arms are DIA-NN 2.6.0 empirical lib, DIA-NN 2.6.0 library-free and FragPipe 24 (diaTracer/MSFragger/EasyPQP); no MBR.
Library-free vs library: 0.92-0.95 (bead/urea/s-trap) and about 0.75 (UE prep). FragPipe vs DIA-NN library: 0.85-1.12, depending on prep.
Useful as an engine-effect prior only.

## 6. Commands and paths worth reusing
- PG: `ssh hive "timeout -s KILL 90 bash -c 'export STAN_DB_BACKEND=pg; export PGPASSWORD=$(cat /quobyte/proteomics-grp/brett/.pgfarm_token); /quobyte/proteomics-grp/brett/stan_venv/bin/python -'" < q*.py`
- sbatch without a login shell: `/cvmfs/hpc.ucdavis.edu/sw/spack/environments/core/view/generic/slurm/bin/sbatch` (partition low / publicgrp-low-qos / publicgrp). Output goes to `$HOME/scaling_inventory/`.
- Repeated plain `ssh hive` calls hit "kex_exchange_identification: Operation timed out" around the 15th connection in a few minutes.
  A ControlMaster (`-o ControlPath=/tmp/.scal_hive -o ControlPersist=3600`) fixed it.
