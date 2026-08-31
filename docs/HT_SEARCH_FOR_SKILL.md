# Searching a high-throughput submission — instructions for the pipeline skill

**Audience:** the `ucdavis-proteomics-core-pipeline` skill (or anyone running a
Core search by hand). Drop this in the skill's `references/`.

**The split, and why.** STAN knows *which files a submission is*. The skill
knows *how to search them*. Neither should learn the other's job:

- STAN alone can tell you that submission 0793 occupies **two trays**, that the
  second tray's filenames never mention 0793, which wells are blanks or HeLa
  standards, and which samples came out badly enough to want re-running.
- The skill already derives search parameters from the data type, pins the
  engine version, batches the SLURM submission, and deposits finished searches
  into FRAN's drop box. Rebuilding any of that inside STAN would mean
  maintaining two of everything and having them disagree.

So: **ask STAN for the file list, then proceed exactly as normal.**

---

## 1. Get the file list

On Hive (the STAN venv is `/quobyte/proteomics-grp/brett/stan_venv/bin`):

```bash
export STAN_DB_BACKEND=pg
export PGPASSWORD="$(cat /quobyte/proteomics-grp/brett/.pgfarm_token)"

# Customer samples only — blanks and HeLa standards excluded. This is the default.
stan ht-manifest 0793 --paths-only > files.txt

# Full detail as JSON (plates, wells, per-run classification, flags)
stan ht-manifest 0793

# Only the samples STAN flagged as needing another injection
stan ht-manifest 0793 --include rerun --paths-only
```

`--include` takes `samples` (default), `rerun`, `standards`, or `all`.

Over HTTP instead, if you are not on Hive — same data, same access rules:

```
GET /api/ht/manifest?q=0793&include=samples
```

### What comes back

```json
{
  "submission": "0793",
  "include": "samples",
  "files": ["/nfs/lssc0/flinders/proteomics/Data/raw_data/tTOF_HT/Aug26/...d", "..."],
  "n_files": 120,
  "plates": ["S5", "S6"],
  "counts": {"sample": 120, "standard": 8, "blank": 7},
  "n_needs_rerun": 6,
  "missing_paths": [],
  "entries": [{"run_name": "...", "raw_path": "...", "class": "sample",
               "plate": "S6", "well": "B3", "injection": 24026,
               "needs_rerun": false, "verdict": "pass"}]
}
```

`files` are absolute, resolved `/nfs/...` paths — feed them straight to
`--raw` / `--files`.

---

## 2. Check these before committing compute

**`missing_paths` must be empty.** It lists runs STAN knows about but has no
raw path for. They are *excluded* from `files`, so a non-empty list means the
search would silently cover a subset and then report success. Stop and ask.

**`plates` should match what the operator expects.** A submission usually
fills one or two trays. If it shows more, or fewer than you were told, check
before searching — the submission's extent is inferred from the acquisition
counter, not from a database of submissions.

**`counts` should look like a plate.** 96 wells per tray, some of them blanks
and standards. A submission reporting 3 samples has probably been mistyped.

---

## 3. Choose the FASTA

STAN does not pick the organism; it does not know it. Pre-staged FASTAs live in
`/quobyte/proteomics-grp/de-limp/fasta/` (16 as of 2026-08-31: human, mouse,
rat-adjacent, chicken, pig, cow, dog, elephant, salmon, arabidopsis, chickpea,
and others), plus `/quobyte/proteomics-grp/MRS/` for human ± contaminants.

Confirm the organism with the operator — the skill's own rule about never
fabricating parameters applies here too.

---

## 4. Search, then deposit to FRAN

Nothing changes from the skill's normal flow. Use the manifest as the file
list, resolve defaults from the data type as usual, confirm before committing
multi-hour compute, then hand off with `scripts/fran_deposit.py`.

**Do not write HT search output under `/quobyte/proteomics-grp/STAN/`.** That
subtree is on FRAN's `DEFAULT_EXCLUDES` list, deliberately — STAN writes a
DIA-NN `report.parquet` for every QC run, and FRAN's own comment is explicit
that ingesting those as customer searches "would corrupt every corpus count."
Write somewhere else and let `fran_deposit.py stage` symlink it into
`/quobyte/proteomics-grp/fran/incoming/`.

---

## 4b. STAN does not need telling

There is deliberately no "record the search" step. FRAN already holds every
search under a submission -- engine, organism, precursor and protein counts --
keyed on the same CoreOmics submission number that appears in the raw
filenames (`/api/internal/submission/{id}`). A second record inside STAN would
be a copy of that, and the copy is what goes stale.

The HT tab links straight to `https://fran.stan-proteomics.org/#/submission/<n>`
and lets FRAN answer, including deciding who may see it. Nothing to remember
after a search, and nothing to keep in step.

---

## 5. Re-runs

Re-injected samples are named `0793_rerun` with the sample name, e.g.
`20260901_0793_rerun_SI-48_S1-A1_1_24200.d`. STAN matches those to the same
submission automatically, so a later `ht-manifest 0793` includes them. Both
`0793` and `793` work as the query.

---

## Things that will bite you

**A submission can span trays that never name it.** 0793 ran tray S6 on
2026-08-27 (`20260827_793_100spd_..._1_24026.d` … `_1_24125.d`) and continued
onto tray S5 the next day as `20260828_100spd_COH-12_S5-D2_1_24157.d`. The link
is the acquisition counter: S5 begins at 24126, one after S6 ends. Searching
only the files whose names contain "793" would silently miss a third of the
submission.

**Do not grep the filenames yourself.** A substring search for `793` also
matches `..._1_23793.d` and `..._1_22793.d`, which belong to other customers.
STAN matches the submission as a delimited token with leading zeros ignored.

**Tray labels repeat.** `S6` is a tray position, reused every month since
December. It is not a submission identifier.

**`needs_rerun` is relative to the plate, not absolute.** A sample flagged
there usually has a `pass` health verdict — the file is fine, it just came out
far below its own plate-mates (the current flags are 55–80% below median
signal). It means "worth another injection", not "broken run".
