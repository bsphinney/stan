# Cross-engine / cross-version precursor-count scaling: primary sources

Compiled 2026-09-29 for the STAN community "Where does my run sit?" box.
Every claim below was fetched from the source named next to it during this session;
nothing is taken from memory. Raw copies of what was fetched sit next to this file so
any number can be re-checked:

| File | What it is |
|---|---|
| `diann_releases.json` | GitHub API dump of all 38 DIA-NN releases (fetched 2026-09-29) |
| `diann_disc1366_body.md` | DIA-NN "2.0-series Updates" discussion #1366 (release notes for 2.0.1 to 2.7.0) |
| `diann_README.md`, `diann_README_{1.8,1.8.1,1.9,1.9.2,2.0}.md`, `readme_hist/` | DIA-NN README at HEAD and at each tag / each README commit 2025-02 to 2026-09 |
| `diann_disc1035.json` | DIA-NN discussion #1035 "FDR in proteomics & data filtering" |
| `sn/*.pdf`, `sn/*.txt` | Spectronaut 17, 18.5, 19, 20, 21 manuals and the 15, 16, 19, 20 release notes |
| `ft/*.txt`, `lou2023_fulltext.txt`, `pubmed_batch*.json` | PubMed Central full texts and PubMed metadata |
| `proteobench/` | shallow clones of the ProteoBench result repositories and code (commit hashes in `pb_commits.txt`), plus `pb_summary.tsv` and `pb_groups.tsv` derived from them |
| `stan_example_report_log.txt` | DIA-NN log and stats file from one live STAN run (read-only, 2026-09-29) |

---

## 0. What STAN's number is (the target of any scaling)

**Code.** In `stan/metrics/extractor.py` (STAN-peg checkout) the report is filtered with
`pl.col("Q.Value") <= q_cutoff` and the metric is `filt["Precursor.Id"].n_unique()`.
Nothing else is filtered: no Global.Q.Value, no Lib.Q.Value and no protein q-value.
`Precursor.Id` is defined by DIA-NN as the "modified peptide sequence with a specific
charge, channel-oblivious" (DIA-NN README, Main output reference). So the STAN count is
the number of unique **modified sequence + charge** entries with a **run-specific**
precursor q-value of at most 1%.

**Production invocation (verified on a live run, not taken from the repo).** The
`report.log.txt` of `09282026_HE50_60-spd-dia_S3-G1_1_24706` (timsTOF HT, HeLa 50 ng,
60 SPD, searched 2026-09-29 10:50) shows:

```
DIA-NN 2.3.0 Academia ... Compiled on Sep 26 2025
/diann-2.3.0/diann-linux --f .../09282026_HE50_60-spd-dia_S3-G1_1_24706.d
  --lib /quobyte/proteomics-grp/STAN/TIMS-10878/instrument_library.parquet
  --fasta .../human_hela_202604.fasta --threads 8 --qvalue 0.01 --min-pep-len 7
  --max-pep-len 30 --missed-cleavages 1 --min-pr-charge 2 --max-pr-charge 4
Spectral library loaded: ... 51487 precursors in 47847 elution groups.
IDs at 0.01 FDR: 46361
Number of IDs at 0.01 FDR: 46760
```

Two points to confirm with Brett:
1. The production search uses a **per-instrument library** (`TIMS-10878/instrument_library.parquet`,
   51,487 precursors), not the 54,000-precursor community library (`hela_timstof_202604.parquet`)
   named in `community_params.py`. Any library-coverage percentage, and any claim that a
   number is "STAN-equivalent", has to say which of the two libraries it was measured against.
2. The search is single-run (no `--reanalyse`, so no MBR). `--no-refine-q` is not passed, so the
   2.3.0 protein-informed q-value refinement is on (see section 1).

**The same run counted six ways** (tiny read-only `pyarrow` read of the 7 MB `report.parquet`;
script `q_count.py`):

| Filter on report.parquet | Unique Precursor.Id | vs STAN |
|---|---|---|
| Q.Value <= 0.01 (**the STAN metric**; equals "Number of IDs at 0.01 FDR" in the log) | 46,760 | 1.000 |
| `report.stats.tsv` Precursors.Identified (equals the first "IDs at 0.01 FDR" log line) | 46,361 | 0.991 |
| Q.Value <= 0.01 and Global.Q.Value <= 0.01 | 46,340 | 0.991 |
| Q.Value <= 0.01 and PG.Q.Value <= 0.01 | 46,581 | 0.996 |
| Q.Value <= 0.01 and Global.PG.Q.Value <= 0.01 | 46,568 | 0.996 |
| Q.Value <= 0.005 | 44,791 | 0.958 |
| Q.Value <= 0.001 | 37,491 | 0.802 |

Library ceiling: 46,760 / 51,487 = 90.8%, which confirms that the timsTOF cohort is
library-limited. n = 1. The table shows the kind of spread to expect, not a calibration.

---

## 1. DIA-NN: version history as it bears on precursor counts

Sources: GitHub release bodies (`https://github.com/vdemichev/DiaNN/releases/tag/<v>`); for
2.0.1 onward, the maintainer's release notes in discussion #1366
(`https://github.com/vdemichev/DiaNN/discussions/1366`, body updated 2026-09-16). Binaries
from 2.0.1 to 2.7.0 are attached to the `2.0` release tag. No `2.1`, `2.2` or `2.3` tag exists
(the tags API returns 404). README at HEAD: "Q: Are DIA-NN version numbers indicative of the
performance? A: No, they are just chronological."

| Version (date) | Release-note text relevant to counts (quoted) | Magnitude given? |
|---|---|---|
| 1.8 (2021-06-28) | "More peptide & protein IDs." "Stringent control of global precursor and protein FDR, validated on thousands of samples." "Transformatively better library-free analysis mode" | No number |
| 1.8.1 (2022-04-15) | "Improved dia-PASEF performance" | No number |
| 1.9 (2024-06-09) | "Better identification numbers and stricter control of false discoveries, along with extensive options to tailor the identification and quantification confidence control"; second-generation QuantUMS; peptidoform scoring module; "1.3x-2x speed gains for library-free search" | No ID number |
| 1.9.1 (2024-07-15) | Minor. Empirical DIA libraries saved as .parquet instead of .tsv. Linux build. | n/a |
| 1.9.2 (2024-10-21) | "Redesigned neural network classifier with on average better performance." "Completely redesigned and improved mass calibration, in particular on Orbitrap and Astral instruments." "The normalisation algorithm has been changed." New experimental 'Conservative' ML mode "imposes the theoretical upper bound of a factor of 2 on the possible q-value deflation due to ML overfitting"; `--nn-fold 4`. | No ID number |
| 2.0 (2025-01-29) | "Major changes in DIA-NN architecture (new search logic, decoy generation, neural network module and calibration module), resulting in higher identification numbers." New Proteoforms scoring mode (decoys by single-residue mutation). | No number |
| 2.0.1 / 2.0.2 (Feb 2025) | 2.0.2: "No functional changes, i.e. all output is identical." 2.0.1: speed; decoy-handling tweak with "negligible effect on protein q-value calculation with empirical libraries". | ~0 |
| 2.1.0 (2025-03-25) | "Identification performance improvements (minimal)." Native Thermo .raw on Linux. | "minimal" |
| 2.2.0 (2025-05-30) | "Marginal gains in proteomic depth." Custom math functions so that "most workflows produce identical output between Windows and Linux." | "marginal" |
| 2.3.0 Preview (2025-09-26), **the STAN version** | "Slightly improved identification performance and data completeness, in particular on challenging data (e.g. single cell). Part of the gain is due to an experimental module that takes into account protein information when identifying precursors (can be turned off with --no-refine-q)". InfinDIA and DDA (beta) added. Warning: "does not correctly recognise Slice/diagonal PASEF methods ... resulting in lower IDs". | "slightly" |
| 2.3.1 (2025-12-05) | "Minimal changes to algorithms." Same Slice/diagonal-PASEF bug. | ~0 |
| 2.3.2 (2026-01-21) | "More peptides used to train NNs in some scenarious, no performance change on most data, up to 10% improvement on some PASEF data." Fixes the 2.3.0/2.3.1 Slice/diagonal-PASEF detection. | **up to +10% (some PASEF)** |
| 2.5.0 (2026-04-12) | "Major increase in protein numbers: up to 70% for low sample amounts." Enterprise-only "Knowledge base" "boosts identification performance". | **up to +70% proteins (low input)** |
| 2.5.1 (2026-04-30) | "Matrices are now generted without lib q-value filters when using MBR (as these are no longer recommended)" | Changes matrix counts |
| 2.6.0 (2026-06-10) | "Output matrices now uses 'Global' filters with MBR consistent with user guide recommendations"; "Retention time alignment and mass calibration improvements" | Changes matrix counts |
| 2.6.1 (2026-06-30) | "Minor algorithm improvements"; "Fixed calibration library not being searched fully in some scenarious" | n/a |
| 2.7.0 Preview (2026-09-16), current | "Performance improvements, in particular on heterogeneous experiments, low sample amounts and in InfinDIA mode (expect several percent gain in protein numbers)" | "several percent" (proteins) |

**Changes in defaults and semantics, from the README at each tag and each README commit (`readme_hist/`):**

- **Default precursor FDR went from 1% to 5% with 2.5.0.** README 2.0 through the 2026-02-05
  commit: "Precursor FDR (%) ... Normally should be kept at 1%, increasing up to 5% may be
  helpful". From the 2.5.0 README (commit 1b37cd4, 2026-04-12) to HEAD: "FDR (%) sets the
  precursor q-value filtering to be auto-applied to the main output report. The default is 5%"
  and "The default precursor FDR setting in DIA-NN is 5%, meaning the main report is filtered
  already at Q.Value <= 0.05." A visitor on 2.5 or later with default settings who counts rows
  of report.parquet is counting at **5%** FDR. The GUI "auto-filters are seeded on first use:
  Q.Value <= 0.01 and Global.PG.Q.Value <= 0.01" (HEAD README), so the GUI table and the file
  disagree.
- **Recommended filters for MBR changed three times.** 1.8.1 and 1.9.2: with MBR use
  "Lib.Q.Value instead of Global.Q.Value" and "Lib.PG.Q.Value instead of Global.PG.Q.Value".
  2.0: "Lib.Q.Value at 0.01, and in addition Global.Q.Value unless using an empirical library
  created based on the same samples". HEAD (2.5+): "Q.Value at 0.01 - 0.05", "Global.Q.Value at
  0.01", "Global.PG.Q.Value at 0.01", and no Lib filter except Lib.Peptidoform.Q.Value.
  HEAD also says: "When benchmarking LC-MS methods, it is often convenient to only use
  run-specific and not global filters."
- **Matrices vs the main report.** From 1.8.1: pr_matrix is "filtered at 1% FDR, using global
  q-values for protein groups and both global and run-specific q-values for precursors". HEAD
  adds "Additional 5% run-specific protein-level FDR filter is applied to the protein matrices".
  The HEAD README states: "The numbers of precursors and proteins reported in different types of
  output files might appear different due to different filtering".
- **Stats report (`report.stats.tsv`).** The README documents only the protein column ("unique
  proteins ... at 1% unique protein q-value"). On 2.3.0 the live run above shows that
  Precursors.Identified (46,361) equals the first log line "IDs at 0.01 FDR" and is 0.9% below
  the final main-report count (46,760). *TODO: verify on more runs, and on 1.8.1/1.9.x, whether
  the two lines coincide in versions without refine-q.*
- **Default machine-learning mode.** 1.8.1 to 1.9.2: "Neural network classifier ... 'single-pass'
  mode is the default". 2.0+: "The default 'NNs (cross-validated)' mode is recommended".
- **Scoring auto-switch.** HEAD `--fix-scoring`: "currently, in DDA mode, during second MBR pass
  or when analysing with empirical library, DIA-NN may auto-switch to Proteoforms regardless of
  the initial Scoring setting". First seen in the 2.5.0-era README. *TODO: STAN searches with an
  empirical library, so check whether 2.3.0 does this (the log above does not print a scoring
  mode).*
- **`--no-refine-q`** (first in the 2.3.0 README): "do not use protein information when
  calculating precursor q-values, essential if the goal is validation of DIA-NN's
  precursor-level q-values using classic entrapment". On the one run above, the refined count is
  +0.86% over the unrefined log line.
- **Proteotypicity and protein inference** affect Protein.Q.Value and protein counts, not
  Precursor.Id counts (HEAD README "Proteotypicity").
- **Decoys.** Decoy rows appear in the main report only with `--report-decoys` ("Decoy 0 or 1
  ... relevant when using --report-decoys"). A visitor who uses that flag and counts rows without
  `Decoy == 0` inflates the count.
- **q-value definitions (HEAD README):** Q.Value = "run-specific precursor q-value";
  Global.Q.Value = "global precursor q-value"; Lib.Q.Value = "q-value for the respective library
  entry, 'global' if the library was created by DIA-NN. In case of MBR, this applies to the
  empirical library created based on the first MBR pass"; PG.Q.Value = run-specific protein
  group; Global.PG.Q.Value; Protein.Q.Value = run-specific unique protein (proteotypic only).
  Global: "global q-values provide confidence that a particular precursor or protein was
  correctly identified in at least one mass spectrometry run of the experiment".
- **MBR (HEAD README):** two passes. The first builds an empirical library from the
  experiment, the second re-searches with it. "Always keep MBR enabled for quantitative analyses
  ... based on predicted libraries. In contrast, MBR should be turned off when analysing using
  an empirical library generated by DIA-NN." A single-run QC count with MBR over a batch is
  therefore partly a property of the batch.
- **FDR comparability warning (HEAD README FAQ):** "the ID number can be, say, 20k peptides at
  1% FDR, and go up to 30k at 2%" and "it only makes sense to compare global protein q-value
  filtering in one software with that in another, and not to compare global ... to run-specific
  one."

---

## 2. Spectronaut: definitions, defaults and version changes

Sources: Biognosys user manuals and release notes (downloaded PDFs, text in `sn/`):
- SN21 manual: https://biognosys.com/content/uploads/2026/05/Spectronaut-21-manual.pdf
- SN20 manual: https://biognosys.com/content/uploads/2025/06/Spectronaut-20-Manual.pdf
- SN20 release notes: https://biognosys.com/content/uploads/2025/06/20250529_Spectronaut20_ReleaseNotes.pdf
- SN19 manual: https://biognosys.com/content/uploads/2024/09/Spectronaut-19-manual-v4.pdf
- SN19 release notes: https://biognosys.com/content/uploads/2024/06/SN19_releasenotes.pdf
- SN18.5 manual: https://biognosys.com/content/uploads/2022/12/spectronaut-185-manual-v2.pdf
- SN17 manual: https://biognosys.com/content/uploads/2023/03/Spectronaut-17_UserManual.pdf
- SN16 release notes: https://biognosys.com/content/uploads/2022/06/Spectronaut-16-Release-Notes.pdf
- SN15 release notes: https://biognosys.com/content/uploads/2021/06/SN15_releasenotes.pdf

**Precursor definition.** SN21 manual, EG headers: "EG.PrecursorId: Unique Id for the
precursor: [modified sequence] plus [charge]". This is the same unit as DIA-NN Precursor.Id.
"EG.Qvalue: The q-value (FDR) of the EG." "EG.Identified: True or False. The EG has to pass the
precursor and protein q-value cutoff to be considered identified." So **a Spectronaut
"identified precursor" is also conditional on protein-level cutoffs**, whereas STAN's count
is not.

**What "identified" means in the UI (SN20 manual p.57):** "only what has passed all the
identification thresholds ... These include precursor posterior error probability (PEP)
cutoff, precursor Q-value cutoff, and protein Q-value cutoff at both experiment and run level."

**Default identification settings (BGS Factory).** The manuals do not tabulate the defaults.
Baker et al. 2024 (JPR, DOI 10.1021/acs.jproteome.3c00671, PMID 38691771, Table 3, SN16/17)
list the defaults as: precursor q-value cutoff 0.01; precursor PEP cutoff 0.2; protein FDR
strategy "accurate"; protein q-value cutoff (experiment) 0.01; protein q-value cutoff (run)
0.05; protein PEP cutoff 0.75. Lou et al. 2023 used the same values in SN 16.1 (precursor PEP
0.2, precursor q 0.01, protein q 0.01 experiment and 0.05 run, "machine learning performed per
run").

**Run-wise vs experiment-wide:**
- SN15 release notes: "New Run-wise protein FDR calculation and filtering"; "New peptide
  posterior error probability (PEP) filtering".
- SN18.5 and SN19 manuals: "Machine Learning: Per Run (default): calculates the discriminant
  scores (Cscores) and q-values (Qvalues) per run. Across Experiment: makes a experiment-wise
  Cscore space. Can compromise the sensitivity." This option is absent from the SN21 manual
  text. SN21 notes "Simplified identification settings for directDIA; default behavior remains
  same".
- Wen et al. 2025 (SN 18.7): "Spectronaut only estimates precursor-level FDR for each run".
- **SN20 added experiment-wide (global) precursor FDR and made it a default:** "[New default]
  DIA Analysis → Identification → Precursor Qvalue Cutoff (Experiment) → 0.01"; also new
  defaults "Single Hit Protein Rule → Stratified Single Hit Protein FDR" and "Run level protein
  scoring → All Expected Observations". The SN21 manual now says "The precursor and protein
  Qvalue cutoffs should be specified on both: experiment and run-wise level" (SN19: only "The
  protein Qvalue cutoff should be specified on both"). ProteoBench's Spectronaut parser reads
  `EG.LibraryQvalue` for the experiment-level q.

**Cross-run behaviour that acts like MBR:**
- directDIA builds its library from all runs in the experiment (SN21 manual §3.4.1.5:
  "directDIA+ ... library free directDIA pipeline"; Wang et al. 2025: "Spectronaut generates
  spectral libraries implicitly from the DIA data per se by the directDIA workflow"). A
  single-run count therefore depends on which other runs were searched with it.
- Quantification "Precursor Filtering" (SN17 to SN21 manuals): "Identified (Qvalue) (default):
  only those precursors passing the q-value cut-offs will be reported ... By the default there
  will be no imputation". The alternatives are "Identified in All Runs (complete)" and
  "Identified in % of Runs".
- **SN15** release notes: "Default row filtering for quantification changed from Q-value to
  Q-value Sparse". **SN16** release notes: "Streamlined quantification settings by replacing
  q-value sparse with new imputation strategy 'Use Background Signal'" and "Changed default
  quantification setting back to q-value without imputation based on user feedback". A
  Spectronaut 15 report exported with defaults therefore carries rows for precursors identified
  in only one run of the experiment. Counting report rows per run over-counts.
- Baker et al. 2024 attribute Spectronaut false positives in heterogeneous samples to "the
  erroneous transfer of identifications across the different runs, a similar situation is seen
  in DDA with 'match between runs'", and tightening the run-level protein q and PEP cut 89% of
  them.

**Version changes that move counts (Biognosys's own numbers, not independently verified):**

| Version | Quoted claim | Source |
|---|---|---|
| 15 | "Up to 35% more precursor identifications for directDIA with diaPASEF"; "Up to 10% more precursor identifications for directDIA in general"; "Up to 30% more ... for short gradient DIA analysis with library"; "Up to 10% more ... with library in general" | SN15 release notes |
| 16 | "Up to 17% more precursor and 10% more protein groups identifications for directDIA"; "Up to 25% more precursor and 15% more protein groups identifications for library based DIA analysis"; DeepXIC scoring | SN16 release notes |
| 17 | directDIA+: "Up to 50% more precursor IDs for dia-PASEF compared to Spectronaut 16", "Up to 50% more ... Orbitrap-DIA", "Up to 100% more ... IonTrap-DIA", "Up to 100% more ... ZenoTOF-SWATH"; "Added FAST or DEEP option for directDIA+"; "Added Run-level Protein Group PEP filter" | SN17 manual §1.2 |
| 18 | "Improved protein identifications (5% more on average)"; Linux CLI; "New default quantification setting (via DIA analysis → Quantification → Quantification window)" | SN18.5 manual §1.2 |
| 19 | directDIA "Improved scoring: 10% more proteins groups and 13% more precursors on average based on a large and diverse set of DIA datasets"; diagonal-PASEF support; "[Change] ... only MS2 quantification for differential abundance" | SN19 release notes |
| 20 | Kuiper engine for unspecific searches; "Improved FDR processes for higher confidence in protein identifications"; "Added global precursor FDR"; new default experiment-level precursor q 0.01 (above) | SN20 release notes |
| 21 | "10% more protein groups on average"; "Simplified identification settings for directDIA; default behavior remains same" | SN21 manual §1.2 |

**directDIA modes (SN21 manual §3.4.1.5):** "directDIA+ (Deep) ... will always provide the
deepest coverage"; "directDIA+ (Fast) ... might yield less identifications for large PTM
search spaces"; classic "directDIA ... will however, yield significantly less identifications
in most cases". The mode has to be captured on the form.

**Library merging (SN21 manual):** "we do not recommend [merging libraries], since this can
lead to uncontrolled inflation of the protein FDR".

---

## 3. Published head-to-head numbers

Each entry gives the citation, identifiers, setup and the numbers as printed. "Union" means
precursors seen in at least one run; "all-k" means seen in every one of k runs.

### 3.1 DIA-NN vs Spectronaut

1. **Demichev V, Messner CB, Vernardis SI, Lilley KS, Ralser M. DIA-NN: neural networks and
   interference correction enable deep proteome coverage in high throughput. Nat Methods
   2020;17:41-44.** DOI 10.1038/s41592-019-0638-x; PMID 31768060; PMC6949130.
   Setup: DIA-NN 1.6.0 vs Spectronaut Pulsar 11.0.15038.17.27438, OpenSWATH, Skyline; HeLa on
   QE-HF, 0.5 to 4 h gradients (PXD005573); effective FDR by a two-species human+maize library
   (202,310 human + 9,781 maize precursors).
   Numbers in the text: "Out of the top 50000 precursors reported by DIA-NN at 0.5h, 49694 are
   confirmed by Spectronaut at either 1h, 2h or 4h"; K562 on TripleTOF 6600, 19 min: DIA-NN
   "consistently identified over 35000 precursors". Head-to-head counts are in figures only.
   Key caveat quoted: "even a simple change to a decoy precursor generation algorithm can halve
   or double the internal FDR estimates reported by an analysis tool."

2. **Demichev V, Szyrwiel L, Yu F, et al. dia-PASEF data analysis using FragPipe and DIA-NN
   for deep proteomics of low sample amounts. Nat Commun 2022;13:3944.** DOI
   10.1038/s41467-022-31492-0; PMID 35803928; PMC9270362.
   Setup: DIA-NN 1.8.1 with the TIMS module; HeLa dia-PASEF on timsTOF Pro (Evosep 200/60 SPD,
   200 ng) and timsTOF Pro 2 (93 min nanoflow). Comparators: Spectronaut 14.3 (two-species FDR
   benchmark) and Spectronaut 14.4 directDIA (CLL cohort).
   Numbers: Evosep 60 SPD with the Meier library, DIA-NN "identifies on average 46,497
   precursors, compared to 26,348 reported by OpenSWATH from the same data"; vs Spectronaut "a
   roughly two-fold gain in terms of precursor numbers detected using longer gradients", and at
   5.6 min "both the performance of Spectronaut and the accuracy of FDR values reported by it
   further dropped significantly"; CLL 50 samples, 100 min, DIA-NN library-free vs Spectronaut
   14.4 directDIA: "74% gain on the precursor level and 48% gain on the protein level".
   Library effect with the same engine: the FragPipe library added "between 598 and 740 extra
   protein identifications" over the Meier library. timsTOF Pro 2 library-free: 8,962 proteins
   (200 ng), 7,442 (10 ng), 3,651 (1 ng).

3. **Lou R, Cao Y, Li S, et al. Benchmarking commonly used software suites and analysis
   workflows for DIA proteomics and phosphoproteomics. Nat Commun 2023;14:94.** DOI
   10.1038/s41467-022-35740-1; PMID 36609502; PMC9822986.
   Setup: mouse brain membrane proteins in a yeast background, 7 samples x 5 replicates, QE HF
   and timsTOF Pro; DIA-NN 1.8.1 (MBR on, double-pass NN) vs Spectronaut 16.1.220730.53000
   (defaults above), MaxDIA 2.1.3.0, Skyline. Universal library 174,115 (HF) and 225,350 (TIMS)
   precursors; in silico library 1,529,467 peptides.
   Numbers (mouse proteins, union over replicates): HF universal library, DIA-NN, Skyline and
   Spectronaut 4,919 to 5,173; Spectronaut with its own DDA library 5,354 proteins and 67,310
   peptides; DIA-NN in silico 5,186 proteins and 51,313 peptides. TIMS universal library:
   DIA-NN 7,128 vs Spectronaut 7,116. Missing values across 35 runs: Spectronaut directDIA 7.2%
   and 4.5%; DIA-NN 16.6 to 18.7%. False IDs (two-species): DIA-NN and Spectronaut "<1.5% and
   0.32% false ID on protein and precursor levels". **Search-space inflation:** with the full
   decoy-species library appended, "Spectronaut ... 88.2% total proteins and 80.7% precursors
   retained", DIA-NN ">98.1% ... proteins and more than 96.4% peptide precursors" (HF). So
   library size moves the two engines by different amounts.

4. **Baker CP, Bruderer R, Abbott J, Arthur JSC, et al. Optimizing Spectronaut Search
   Parameters to Improve Data Quality with Minimal Proteome Coverage Reductions in DIA Analyses
   of Heterogeneous Samples. J Proteome Res 2024;23(6):1926-1936.** DOI
   10.1021/acs.jproteome.3c00671; PMID 38691771; PMC11165578.
   Setup: Exploris 480, 1.5 ug, 120 min, directDIA; mouse BMDM +/- Candida spike; Spectronaut
   16 vs 17, default vs stringent settings (precursor PEP 0.01, protein q run 0.01, protein PEP
   0.01).
   Numbers: the same spiked samples gave 32,083 Candida peptides / 3,190 proteins in SN16 and
   39,413 / 3,477 in SN17 ("20.5% increase", "8.6% increase"). Stringent settings reduced mouse
   peptides from 95,113 to 89,016 (-6.4%) and proteins from 6,768 to 6,414 (-5.2%). False
   Candida peptides in non-spiked samples fell from 1,514 to 164.

5. **Yu Z, Du A, Xu X, Li Y, et al. Spectronaut and DIA-NN: A Comparison of their Performance
   in the Analysis of Lung Adenocarcinoma Biopsies. ACS Omega 2026;11(5):8080-8093.** DOI
   10.1021/acsomega.5c10421; PMID 41696233; PMC12903166.
   Setup: Exploris 480, 1 h, 48 LUAD tissue runs, library-free with MBR; Spectronaut 20.1 vs
   DIA-NN 2.1.0 (the authors also ran 19.9 and 1.9.2 and report "not insignificant
   differences" between versions without numbers).
   Numbers (protein level only): Spectronaut 7,597 vs DIA-NN 7,787 proteins in total, 7,180
   shared.

6. **Wang J, Huang Y, Lu F, Xu Q, et al. Benchmarking informatics workflows for
   data-independent acquisition single-cell proteomics. Nat Commun 2025;16:10276.** DOI
   10.1038/s41467-025-65174-4; PMID 41271703; PMC12639053.
   Setup: 200 pg HYE mix, timsTOF Pro 2 diaPASEF, 6 technical replicates per sample, 30 runs,
   library-free. Software versions are not in the PMC text extract.
   Numbers per run: Spectronaut directDIA 3,066 +/- 68 proteins and 12,082 +/- 610 peptides;
   PEAKS 2,753 proteins; DIA-NN 11,348 +/- 730 peptides. "Spectronaut detected 11% more
   (3194/2879) and 23% more (3194/2607) proteins than PEAKS and DIA-NN" (>=50% completeness).
   Co-searching different organisms "increased the identified proteins from the wrong
   organisms". This is MBR / library transfer risk.

7. **Frejno M, Berger MT, Tüshaus J, et al. Unifying the analysis of bottom-up proteomics
   data with CHIMERYS. Nat Methods 2025;22(5):1017-1027.** DOI 10.1038/s41592-025-02663-w;
   PMID 40263583; PMC12074992. *Vendor paper (MSAID).*
   Numbers: "DIA-NN and Spectronaut seemed to underestimate FDR" by entrapment. Filtering at run
   eFDR "reduced the overall number of identifications for DIA-NN and Spectronaut to a level
   comparable to CHIMERYS". Data completeness fell "for Spectronaut from 86% to 61% and for
   DIA-NN from 78% to 30%".

### 3.2 DIA-NN version to version

8. **Gu K, Kenko M, Ogawa K, et al. Evaluation of the False Discovery Rate in Library-Free
   Search by DIA-NN Using In Vitro Human Proteome. J Proteome Res 2025;24(8):3874-3883.** DOI
   10.1021/acs.jproteome.5c00036; PMID 40679152. *Abstract only; the full text is paywalled
   (HTTP 403).*
   "Versions 1.9.2 and 2.10 identified more peptides than version 1.8.1 ... average FDR at the
   precursor level was 0.538% for version 1.8.1, 0.389% for version 1.9.2, and 0.385% for
   version 2.1.0; at the protein level ... 2.85%, 1.81%, and 1.81%". The size of the peptide
   gain is not in the abstract.

9. **Moschem JDC, de Barros BCSC, Serrano SMT, Chaves AFA. Decoding the Impact of Isolation
   Window Selection and QuantUMS Filtering in DIA-NN for DIA Quantification of Peptides and
   Proteins. J Proteome Res 2025;24(8):3860-3873.** DOI 10.1021/acs.jproteome.5c00009; PMID
   40629671. *Abstract only.* "we compared [six different versions of DIA-NN] and found high
   reproducibility except for version 1.9."

10. **Yue Q, Shen Y, Dai C, ... Demichev V, Perez-Riverol Y. quantmsdiann: a scalable
    SDRF-driven DIA-NN workflow ... Research Square preprint 2026.** DOI
    10.21203/rs.3.rs-10319687/v1 (Europe PMC PPR1278521). *Preprint, not peer reviewed.*
    "upgrading DIA-NN from 1.8.1 to a current release increased protein-group identifications
    by up to 17% in single-cell datasets"; reanalysis of public deposits "recovering up to 59%
    more protein groups, with the largest gains on deposits processed with older or non-DIA-NN
    engines".

11. **ProteoBench community benchmark (EuBIC-MS)**: https://proteobench.cubimed.rub.de/ and
    https://github.com/Proteobench. Result repositories were cloned 2026-09-29 (hashes in
    `proteobench/pb_commits.txt`). Datasets use the Van Puyvelde et al. 2022 HYE design (Sci
    Data 9:126, DOI 10.1038/s41597-022-01216-6, PMID 35354825).
    - Astral module: 50 ng, ~16 min, 2 Th x 300 windows, 6 runs.
    - diaPASEF module: 25 ng, 30 min, 6 runs.
    - AIF module: QE HF-X, 6 runs.
    Metric: "number of unique precursor ions quantified", with precursor ion = modified sequence
    + charge. ProteoBench applies no q-value filter of its own in the quant modules
    (`parse_settings_diann.toml` maps no Q column), so the count reflects the tool's own report
    filtering. These are **multi-run** counts on identical raw files, and each submitter chose
    their own settings and library.
    Medians at a submitted 1% FDR (`pb_groups.tsv`; n = number of submissions):

    | Dataset | Engine / version (MBR) | n | union (>=1 of 6) | all 6 runs |
    |---|---|---|---|---|
    | diaPASEF | DIA-NN 1.8 (on) | 1 | 102,260 | 78,138 |
    | diaPASEF | DIA-NN 1.9.x (on) | 4 | 108,316 | 82,887 |
    | diaPASEF | DIA-NN 2.0 (on) | 1 | 120,520 | 90,463 |
    | diaPASEF | DIA-NN 2.2.0 (on) | 1 | 122,710 | 92,834 |
    | diaPASEF | DIA-NN 2.5.0 (on) | 2 | 116,484 | 90,863 |
    | diaPASEF | DIA-NN 2.3.0 (off, user lib) | 1 | 132,229 | 67,510 |
    | diaPASEF | Spectronaut 19.5 | 2 | 129,999 | 110,137 |
    | diaPASEF | Spectronaut 20.0 | 3 | 124,534 | 112,610 |
    | diaPASEF | Spectronaut 21.0 | 2 | 147,554 | 127,351 |
    | Astral | DIA-NN 1.8.1 (on) | 1 | 112,879 | 68,634 |
    | Astral | DIA-NN 1.9.x (on) | 10 | 124,437 | 78,117 |
    | Astral | DIA-NN 2.0 (on) | 1 | 130,787 | 82,417 |
    | Astral | DIA-NN 2.2.0 (on) | 4 | 133,844 | 85,458 |
    | Astral | DIA-NN 2.3.x (on) | 5 | 133,244 | 84,692 |
    | Astral | DIA-NN 2.5.0 (on) | 5 | 119,779 | 77,701 |
    | Astral | Spectronaut 19.5/19.9 | 3 | 119,380 | 91,220 |
    | Astral | Spectronaut 20.0/20.4 | 3 | 112,922 | 97,980 |
    | Astral | Spectronaut 21.0 | 2 | 140,158 | 112,027 |
    | AIF | DIA-NN 1.8 (on) | 1 | 105,663 | 63,874 |
    | AIF | DIA-NN 1.9.x (on) | 6 | 96,164 | 51,352 |
    | AIF | Spectronaut 19 / 20 | 1 / 2 | 103,716 / 101,546 | 68,278 / 65,547 |

    What these show, stated carefully:
    - (a) With MBR on, going from DIA-NN 1.8 to 2.2/2.3 raised the all-runs count by about
      +19% (diaPASEF, 78k to 93k) and +23% (Astral, 69k to 85k). 1.9 to 2.0 accounts for most
      of the rest of the step.
    - (b) Spectronaut 20 to 21 added +13% (diaPASEF) and +14% (Astral) on all-runs counts.
    - (c) **The Spectronaut-to-DIA-NN ratio depends on how the count is defined.** diaPASEF,
      SN20 vs DIA-NN 2.2: 1.21x on all-runs but 1.01x on union. Astral, SN20 vs DIA-NN 2.3:
      1.16x on all-runs but 0.85x on union.
    - (d) DIA-NN with MBR off and run-level q only gives a large union (132k) but a small
      all-runs count (67.5k). This is the accumulation effect that the README's global q-value
      text warns about.

12. **ProteoBench DIA entrapment module (Astral)**: 3 x 50 ng HeLa, 15 min, 2 Th nDIA, with a
    1:1 pre-digested entrapment FASTA. It evaluates **global** precursor FDR following Wen et
    al. 2025. Submissions (from `Results_entrapment_ion_DIA_Astral`):

    | Engine and version (FDR set) | Global precursor IDs | Paired FDP (upper bound) |
    |---|---|---|
    | DIA-NN 1.8.1 (1%) | 73,573 | 0.74% |
    | DIA-NN 2.3.2 (1%), two runs | 87,473 / 85,980 | 0.93% / 0.94% |
    | DIA-NN 2.5.1 (1%), two runs | 87,847 / 90,421 | 1.44% / 1.47% |
    | DIA-NN 2.6.0 (1%), two runs | 89,733 / 87,172 | 1.43% / 1.46% |
    | DIA-NN 2.3.1 (5%) | 112,166 | 3.82% |
    | DIA-NN 2.3.1 (10%) | 124,441 | 7.82% |
    | AlphaDIA 2.1.2 (1%) | 60,084 | 0.18% |

    **FDR threshold alone moves the count by +28% (1% to 5%) and +42% (1% to 10%)** on the same
    data and version family. This matters because the 2.5+ default is 5%. The 1.8.1 entry used a
    DIA-NN predicted library and the 2.3.2 entries a user-defined library, so the 1.8.1 to 2.3.2
    step (+19%) mixes version and library effects.

13. **Wen B, Hsu C, Shteynberg D, et al. Carafe enables high quality in silico spectral
    library generation for data-independent acquisition proteomics. Nat Commun
    2025;16:9815.** DOI 10.1038/s41467-025-64928-4; PMID 41198693; PMC12592563. (Library-type
    effect with a fixed engine: DIA-NN 1.8.1.)
    Numbers: Carafe fine-tuned vs DDA-trained AlphaPeptDeep libraries "+5.1-38.0%" precursors;
    vs DIA-NN library-free "2.6-27.1% more precursors" on four DIA datasets; "21.45% more
    precursors compared with the library generated using the pretrained DDA models and 10.02%
    more precursors compared with the library generated using DIA-NN's built-in model". Library
    size: Prosit+GPF library 44,903 precursors vs full-proteome Carafe library 661,012.
    Restricting Carafe to the GPF-observed peptides gave "4.2% more peptides".

14. **Fröhlich K, Brombacher E, Fahrner M, et al. Benchmarking of analysis strategies for
    data-independent acquisition proteomics using a large-scale dataset comprising inter-patient
    heterogeneity. Nat Commun 2022;13:2622.** DOI 10.1038/s41467-022-30094-0; PMID 35551187;
    PMC9098472. DIA-NN with a GPF-refined in silico library (84,016 precursors): "on average
    48,698 precursors were identified per measurement". The authors add: "as the
    'match-between-runs' function had not yet been included in the DIA-NN version we used ...
    results should be revisited for newer DIA-NN versions."

### 3.3 FDR validity: the same nominal 1% is not the same across engines

15. **Wen B, Freestone J, Riffle M, MacCoss MJ, et al. Assessment of false discovery rate
    control in tandem mass spectrometry analysis using entrapment. Nat Methods
    2025;22:1454-1463.** DOI 10.1038/s41592-025-02719-x; PMID 40524023; PMC12240826.
    DIA-NN 1.8.1 and Spectronaut 18.7.240325.55695 directDIA, plus EncyclopeDIA, across 10 DIA
    datasets. "none of these search tools consistently controls the FDR at the peptide level
    ... this problem becomes much worse ... at the protein level." With the paired estimate at
    1%, DIA-NN reports "up to 6.7% [more discoveries] at the precursor level and up to 4.7% at
    the protein level"; single-cell (1cell-eclipse) "up to 48.3% at the precursor level". A
    large entrapment ratio gave a DIA-NN lower bound "of almost 7%"; Spectronaut "lower bound is
    above 4%".

16. **Lou et al. 2023** (above): DIA-NN and Spectronaut under 1.5% (protein) and 0.32%
    (precursor) false IDs by two-species library. **Gu et al. 2025** (above): DIA-NN precursor
    FDP 0.538%, 0.389%, 0.385% (1.8.1, 1.9.2, 2.1.0).

17. **DIA-NN discussion #1035** (V. Demichev, 2024-06-09,
    https://github.com/vdemichev/DiaNN/discussions/1035) sets out three definitions of a false
    identification (F1 to F3) and argues that FDR under the strictest one "is almost never well
    controlled by the modern software". Different engines implicitly target different
    definitions.

### 3.4 HeLa single-run context (not engine-to-engine)

18. **Wallmann G, Skowronek P, Brennsteiner V, et al. AlphaDIA enables DIA transfer learning
    for feature-free proteomics. Nat Biotechnol 2025.** DOI 10.1038/s41587-025-02791-w; PMID
    41120665; PMC13368584. HeLa, 21 min / 60 SPD, timsTOF Ultra: ">73,000 precursors ... ~6,800
    protein groups"; Orbitrap Astral 60 SPD ">120,000 precursors and 9,800 protein groups"
    (precursors at local 1% FDR). The benchmark versions were Spectronaut 18.6, DIA-NN 2.1.0 and
    CHIMERYS 4.2.1; head-to-head numbers are in figures only.

19. **Guzman UH, Martinez-Val A, Ye Z, Damoc E, et al. Ultra-fast label-free quantification and
    comprehensive proteome coverage with narrow-window data-independent acquisition. Nat
    Biotechnol 2024;42:1855-1866.** DOI 10.1038/s41587-023-02099-7; PMID 38302753;
    PMC11631760. HEK293, 30 min Astral nDIA (DIA-NN 1.8.1, MBR, 4,299,848-precursor predicted
    library): "approximately 170,000 peptide precursors and ~10,000 protein groups". Five-minute
    HeLa, same software across platforms, DIA-NN library-free: Astral 7,538 protein groups vs
    timsTOF HT 3,737, ZenoTOF 3,419, TripleTOF 3,330, Exploris 480 3,143. For the Spectronaut
    17/18 directDIA searches, "each analysis contained three experimental replicates", which is
    the multi-run context again.

### 3.5 Found but not usable for precursor numbers (abstract level only)

- Gotti C, et al. J Proteome Res 2021;20(10):4801-4814; DOI 10.1021/acs.jproteome.1c00490; PMID
  34472865. UPS1 in E. coli, 36 workflows, protein-level only.
- Staes A, et al. J Proteome Res 2024;23(6):2078-2089; DOI 10.1021/acs.jproteome.4c00048; PMID
  38666436. UPS2 in yeast, DIA-NN / EncyclopeDIA / Spectronaut, library vs library-free. The
  abstract says DIA-NN had "the highest sensitivity". No PMC text.
- Yang Z, et al. J Proteome Res 2026;25(3):1686-1699; DOI 10.1021/acs.jproteome.5c01028; PMID
  41738589. Astral low-input: DIA-NN, Spectronaut and FragPipe. The abstract recommends DIA-NN
  library-free with MBR. No PMC text.
- Szyrwiel L, Grossmann JL, Sinn LR, Rappsilber J, Demichev V. "Large-scale quantitative
  benchmark reveals accuracy limits of proteomics" (bioRxiv 2025, DOI
  10.64898/2025.12.03.692002). Quantitative accuracy, not ID counts.

---

## 4. Confounders the form must capture for a fair scaling

Each item is tied to a source above.

1. **Engine** (DIA-NN / Spectronaut / other). Section 3.
2. **Exact version** including patch and build.
   - DIA-NN: 2.3.0/2.3.1 mis-handle Slice/diagonal-PASEF, fixed in 2.3.2 (up to +10% on some
     PASEF); 2.5.0 up to +70% proteins at low input.
   - Spectronaut: major versions move counts by roughly 10 to 50% (release notes); ProteoBench
     SN20 to SN21 is about +13%.
   - Ask for the full build string, e.g. "19.5.241126.62635".
3. **Edition / licensed features.** DIA-NN Enterprise "Knowledge base" (2.5+) "may
   significantly boost sensitivity". Academia does not have it.
4. **FDR threshold actually applied to the number reported.** DIA-NN's default is 1% up to
   2.3.x and 5% from 2.5. ProteoBench shows +28% at 5% and +42% at 10%. STAN's own run shows
   -4% at 0.5% and -20% at 0.1%.
5. **FDR scope.**
   - Run-specific (Q.Value / EG.Qvalue) vs global / experiment (Global.Q.Value, Lib.Q.Value,
     SN20+ "Precursor Qvalue Cutoff (Experiment)").
   - Whether protein-level filters were also applied (PG.Q.Value, Global.PG.Q.Value; Spectronaut
     EG.Identified requires protein cutoffs at run 0.05 and experiment 0.01 by default, plus
     precursor PEP 0.2).
   - STAN applies none of these.
6. **Where the number was read from.**
   - DIA-NN: main report rows vs pr_matrix (global + run q, and MBR-dependent filters that
     changed in 2.5.1 and 2.6.0) vs `report.stats.tsv` Precursors.Identified (0.9% below the
     report on STAN's 2.3.0 run) vs the GUI log line.
   - Spectronaut: Run Identifications panel vs report export rows (depends on Precursor
     Filtering: SN15 defaulted to Qvalue Sparse, SN16+ to Identified (Qvalue)).
7. **Library type and size.**
   - Type: library-free / predicted (in silico from FASTA, DIA-NN or Prosit or Carafe) vs
     project DDA library vs empirical DIA library (as STAN uses) vs Spectronaut directDIA+
     (Fast / Deep / classic) vs hybrid.
   - Size: number of precursors in the library, and whether it is restricted to HeLa-observed
     peptides.
   - Library-type effects with a fixed engine run from +2.6% to +38% (Carafe) and +598 to 740
     proteins (Demichev 2022). Search-space inflation costs Spectronaut up to 19% of precursors
     vs DIA-NN about 4% (Lou 2023).
   - **Near a library ceiling the mapping saturates.** STAN timsTOF sits at ~91% of a
     51.5k-precursor library, so a visitor's library-free 70k cannot map linearly.
8. **FASTA / search space.** Canonical vs isoforms, contaminants, species; the digest rule
   (STAN: `K*,R*`, 1 missed cleavage, length 7 to 30, charge 2 to 4); variable modifications
   (M-ox, N-term acetyl) multiply precursor identities; the precursor and fragment m/z range.
9. **MBR / cross-run context.**
   - DIA-NN MBR (two-pass; the second pass uses an empirical library from the batch) and
     Spectronaut directDIA (library built from all runs in the experiment).
   - Record how many runs were searched together and what they were (blanks, other samples,
     other gradients).
   - STAN is single-run with a fixed library. ProteoBench shows union vs all-runs ratios of
     0.35 to 0.9 depending on engine and MBR.
10. **Counting unit.**
    - Precursor = modified sequence + charge (DIA-NN Precursor.Id, Spectronaut EG.PrecursorId).
      Not stripped peptides, not modified peptides, not "elution groups".
    - Unique per run vs total rows.
    - Charge range searched.
    - plexDIA / channels: Precursor.Id is "channel-oblivious", so counting per channel inflates.
11. **Decoys and contaminants.** DIA-NN writes decoys to the main report only with
    `--report-decoys`; exclude `Decoy == 1`. Exclude contaminant entries (ProteoBench removes
    `Cont_` precursors and multi-species hits).
12. **Scoring and decoy model.**
    - DIA-NN Generic vs Peptidoforms vs Proteoforms (2.0+); the auto-switch to Proteoforms with
      an empirical library is documented from 2.5.
    - DIA-NN `--no-refine-q` (2.3.0+; +0.86% on STAN's run); custom `--dg-*` decoy parameters
      (ProteoBench: 77.9k to 79.7k vs about 87k IDs).
    - Spectronaut decoy method (Mutated default) and Machine Learning Per Run vs Across
      Experiment (SN18 to 19).
13. **Machine-learning mode.** DIA-NN single-pass (1.8 to 1.9.2 default) vs NNs
    cross-validated (2.0+ default) vs "NNs (fast)" vs "Conservative" (1.9.2+).
14. **Speed mode.** DIA-NN "Ultra-fast ... ID numbers are not as high" and InfinDIA (2.3+,
    designed for MBR). Spectronaut directDIA+ Fast vs Deep.
15. **Mass-accuracy handling.** Automatic vs fixed. The STAN log warns that for timsTOF "it is
    better to manually fix both the MS1 and MS2 mass accuracies to 10-15 ppm". It is part of the
    STAN definition and should be recorded for the visitor.
16. **Instrument and acquisition scheme** (dia-PASEF vs Slice/diagonal/Synchro-PASEF, nDIA 2 Th
    vs wide windows). This interacts with version (e.g. the 2.3.0/2.3.1 PASEF bug), so it has
    to key the version correction, not only the cohort.
17. **Operating system (minor).** DIA-NN README: Linux and Windows outputs "may be slightly
    different"; 2.2.0 made "most workflows produce identical output". STAN runs Linux.
18. **Sample and load** (HeLa source, ng on column, SPD / gradient). This is already the
    cohort key in STAN; recorded here because the engine and version gains are
    input-dependent (e.g. DIA-NN 2.5.0 "up to 70% for low sample amounts"; quantmsdiann "up to
    17% ... single-cell").

---

## 5. What this means for the scaling algorithm (research conclusions, not a design)

- No single "Spectronaut to STAN" or "DIA-NN x.y to STAN" multiplier is defined unless the
  count definition (FDR level, scope, source file, single-run vs batch) is pinned on the form.
  In ProteoBench the Spectronaut / DIA-NN ratio flips from 0.85x to 1.16x on the same Astral
  data depending on union vs all-runs counting.
- Published version-to-version magnitudes are mostly vendor "up to" claims. The only
  controlled same-data comparisons found are ProteoBench (multi-run, submitter-chosen
  settings, n = 1 to 10 per cell) and Gu et al. 2025 (paywalled; counts not in the abstract).
  **STAN's own archive is the better calibration source.** Re-searching a stratified subset
  of existing HeLa raws with DIA-NN 1.8.1, 1.9.2, 2.0, 2.2, 2.3.2, 2.5 and 2.7 (library-free
  and with the STAN library, 1% and 5%, with and without `--no-refine-q`) on Hive would give
  per-instrument-family, per-SPD correction curves that sources cannot supply. Spectronaut would
  need a licence holder. (This session submitted no searches.)
- Because STAN's timsTOF counts are library-capped (~91% of 51.5k), any mapping from a
  library-free visitor count must be monotone and saturating, not multiplicative. The Orbitrap
  library (~170k) is far from its cap, so the two vendors need different treatment.

## 6. Open items / TODO verify

- `report.stats.tsv` Precursors.Identified vs the final report count: check on more STAN runs
  and on pre-2.3 versions.
- Whether DIA-NN 2.3.0 auto-switches to Proteoforms scoring with an empirical library.
- Why production uses `TIMS-10878/instrument_library.parquet` (51,487) while
  `community_params.py` names the 54k community library; which one the community cohort was
  built with.
- Full texts of Gu 2025, Moschem 2025, Staes 2024 and Yang 2026 were not available (ACS
  paywall) and should be read for per-version counts.
- Spectronaut default identification cutoffs are taken from Baker 2024 (SN16/17) and Lou 2023
  (SN16.1), not from a Biognosys defaults table; confirm for SN19 to SN21 from an
  ExperimentSetupOverview export.
