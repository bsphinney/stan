# HT scripts

Working copies of the high-throughput helper scripts. Canonical source is
`~/Documents/HT_work/scripts/` on Brett's Mac — edit there, re-copy here.

Reachable from the timsTOF as `Y:\brett\scripts\` (Y: = proteomics-grp),
and from Hive as `/quobyte/proteomics-grp/brett/scripts/`.

## dump_bruker_db.ps1  — run ON the instrument

Read-only survey + dump of Bruker's HyStar PostgreSQL database, written to
the share. Answers whether that database holds the acquisition sample table;
if it does, queue reconciliation stops depending on a hand-exported .xlsx.

Double-click `run_bruker_dump.bat` — it wraps the .ps1 with
`-ExecutionPolicy Bypass`, because double-clicking a .ps1 opens Notepad and
the default policy blocks unsigned scripts. Copy BOTH files, they must sit
together:

```bat
copy Y:\brett\scripts\dump_bruker_db.ps1  C:\Temp\
copy Y:\brett\scripts\run_bruker_dump.bat C:\Temp\

C:\Temp\run_bruker_dump.bat          survey + schema + server logs
C:\Temp\run_bruker_dump.bat /data    + sample-table rows (run when idle)
```

⚠️ `run_bruker_dump.bat` is CRLF and must stay CRLF — cmd.exe mis-parses an
LF-only batch file. Do not round-trip it through an editor that rewrites line
endings. (The .ps1 is LF and that is fine; PowerShell does not care.)

Never writes to the database, never stops the service, never modifies anything
in `D:\BrukerDBData`. Output lands in
`Y:\brett\bruker_db\<HOST>_<timestamp>\`.

## qc_scan.py  — run ON hive

Reads frame counts + TIC straight from each `.d/analysis.tdf` for a submission.

```bash
cd /quobyte/proteomics-grp/brett && python3 scripts/qc_scan.py   # -> qc_scan.json
```

## export_rerun_package.py  — run on the Mac (needs matplotlib + openpyxl)

Turns a scan into the three deliverables: rerun TSV, a HyStar SampleTable
queue matching the original export format, and a plate-map PDF.

The STAN dashboard is the authority on which acquired wells are suspect.
Wells that produced no data file, and queued wells that produced no directory
at all, are unioned in regardless — the dashboard is structurally blind to
both, and a sample that makes no data file is an automatic rerun.

```bash
python3 export_rerun_package.py --scan qc_scan.json --submission 793 \
    --dashboard https://ucd.stan-proteomics.org --dash-token <token> \
    --queue Protifi_plate1.xlsx --queue Protifi_plate2.xlsx \
    --run-date 20260902 --out-dir ./exports
```

## symlink_submission_raws.py  — run ON hive

Links a submission's raws into its CoreOmics project folder, falling back to
the service directory when that folder does not exist. Dry-run by default.

```bash
python3 scripts/symlink_submission_raws.py --submission 793 \
    --map-cache /quobyte/proteomics-grp/brett/submission_map.json --commit
```

⚠️ Apptainer cannot follow a symlink whose target is outside its bind mount.
Any DIA-NN/Sage job reading these links must `--bind /quobyte:/quobyte`.
