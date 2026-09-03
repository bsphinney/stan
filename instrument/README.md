# Instrument-side tooling

Code that reads the instruments directly — the Evosep One's procedure logs and
the Bruker Compass Server's database — and turns them into the documents the
Maintenance tab renders.

**This lived outside version control until 2026-09-03.** It ran production
crons on Hive from a scratch directory and a copy on the Quobyte share, with no
history, no review and no way to revert. `extract_evosep.py` alone is ~156 KB
with 97 tests. That gap is what this directory closes.

## Layout

| path | what it is |
|---|---|
| `evosep/extract_evosep.py` | Reads the Evosep procedure-log mirror and produces the column-health document: per-run plateau pressure, wash flow, column lifetimes, the pressure reference and sample impact. |
| `evosep/test_extract_evosep.py` | 97 tests. Several are pinned to real log shapes (a 2023 `maintenance-info.txt`, a partial run with no plateau) — read the docstrings before "simplifying" one away. |
| `evosep/publish_evosep_pg.py` | Publishes that document to PG Farm (`evosep_column_health`), enforcing the 1 MB budget. |
| `evosep/resolve_bruker_methods.py` | Resolves a run's gradient from the `.d` method XML rather than its filename. |
| `evosep/cron_evosep.sh` | The 30-minute tick: cheap window + alerting every time, full extract and publish only when the published document is over ~20 h old. |
| `bruker/extract.sql`, `extract_bruker.sh` | Restores the nightly Compass backup into a throwaway postgres and aggregates it. |
| `scripts/*.ps1`, `*.bat` | Windows-side collectors that run **on the instrument PC** and copy logs to the share. |
| `scripts/*.py` | Submission linking, rerun exports, QC scans. |

## Two things that will bite

**The deployed copy is on the share, not here.** The crons run
`/quobyte/proteomics-grp/STAN/evosep/` and `/quobyte/proteomics-grp/STAN/bruker/`.
This directory is the versioned source; deploying still means copying. Until
that is wired to a checkout, a change made only here does not run, and a change
made only there is not recorded.

**The extract does not publish.** For Bruker in particular, running the extract
leaves a correct file on disk while PG keeps serving yesterday's document — a
state indistinguishable from a fix that did not work. It cost half a day on
2026-09-03. `extract_bruker.sh` now ends with `publish_bruker_pg.py`; keep it
that way.

## PowerShell files

Pure ASCII with a UTF-8 BOM and CRLF. PowerShell 5.1 decodes a BOM-less file as
cp1252 and mis-parses anything above ASCII. `.bat` files take CRLF but **no
BOM** — cmd.exe reports `'∩╗┐@echo' is not recognized`.
