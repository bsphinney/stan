# Changelog

All notable changes to STAN (Standardized proteomic Throughput ANalyzer) are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versions correspond to tags in `pyproject.toml` / `stan/__init__.py`; both files
are always bumped together on every push.

**v1.0.0** is the first public release, cut 2026-05-30. Release process and
deferred items: [`docs/V1_PRERELEASE_CHECKLIST.md`](docs/V1_PRERELEASE_CHECKLIST.md).

---

## [1.0.23] — 2026-08-26

### Added
- **Sync button on the local dashboard.** The Community tab now carries a
  *Community sync* panel: it shows how many runs are eligible, pre-fills the
  lab name, and pushes them to the public benchmark on one click. If the
  install has no `display_name` yet, a pseudonym is generated
  (`generate_pseudonym()`, e.g. *Oxidized Cottrell*) and offered as the
  default, so nobody publishes as "anonymous" purely for having skipped setup.
  - `GET /api/community/sync-status` → `{display_name, suggested_name,
    pending, readonly}`; `POST /api/community/sync` runs the submissions and
    persists the chosen name to `community.yml`.
  - The eligible-run count reuses `_pending_community_runs()`, which mirrors
    `stan submit-all`'s own skip rules (washes/blanks, zero-ID runs), so the
    number on the button matches what the CLI would actually push.
  - On the public read-only dashboard the panel renders an explanatory note
    instead of a button — the read-only gate would 403 the POST anyway.

- **Hive cron for community sync** (`scripts/cron_community_sync.sh`, every
  6 h at :25). Newly-processed runs now reach the public site without anyone
  pushing from the Mac.
  - Login-node safe: HTTP POSTs over PG-resident rows — no raw-file IO, no
    search, no meaningful CPU.
  - Idempotent by construction: `submit-all --backend pg` selects
    `submitted_to_benchmark = 0` and sets it to 1 on success, so a tick with
    nothing new costs one query.
  - Seeds `LOGNAME`/`USER` and sources the profile scripts outside `set -u` —
    the exact omission that kept the Flinders dispatch cron silent from
    2026-06-10 to 2026-08-26.

### Fixed
- **Nightly benchmark consolidation was disabled**, which is why no QC newer
  than 2026-05-21 appeared on the community site even after runs were
  submitted: 4,091 submission parquets existed against 3,843 consolidated
  rows. Re-enabled the workflow and re-ran it.
- **`ruff check stan/` passes again.** CI's lint step had failed on every push
  today, so the gate was effectively off. All 23 findings were style-only
  (unused bindings, late imports, one-line compound statements). Unused
  bindings whose right-hand side has side effects (`subprocess.run`,
  `urlopen`, a validating `yaml.safe_load`) were renamed rather than deleted;
  only `needs_install`, a flag assigned in four branches and never read, was
  removed outright.

### Notes
- Hive had no `~/.stan/community.yml` at all, so its first sync exited early
  with "community_submit is not enabled". Created it with the same
  `display_name`/`auth_token` as the Mac so runs attribute to one lab, and
  deliberately **without** `email_reports` so Hive does not duplicate the
  daily and weekly reports the Mac already sends.

---

## [1.0.22] — 2026-08-26

### Added
- **Shared arcade leaderboard.** Game over now prompts for a name and a
  lab/affiliation — **both optional**, blank scores as `anonymous` — and posts
  to the new `POST /api/arcade/score`. `GET /api/arcade/leaderboard?game=&limit=`
  serves the board. Scores land in the central PG Farm `arcade_scores` table
  (`migrations/2026-08-26_arcade_scores.sql`) when the install has PG, so every
  STAN and the hosted dashboard share one board; installs without PG Farm get
  the same table in local SQLite and keep a working arcade.
  - Prompt + POST live in `public/arcade.html` alone. Games are unchanged: they
    still `postMessage({type:'arcade-score', game, score, won, level})`, so a
    new game gets the whole flow for free.
  - Name/affiliation are remembered in `localStorage` — a repeat player hits
    Enter.
  - The board is read local-first, with the (still undeployed) HF Space relay
    as a read-only fallback. The old client-side relay POST is gone; it 404'd.

### Security / privacy
- Name and affiliation are player-typed, world-readable on the PG board, and
  therefore untrusted: capped server-side at 40 / 60 characters, control
  characters flattened, stored verbatim and HTML-escaped at every render site.
  `submitted_by_host` is kept for moderation and never returned by the reader.
- `POST /api/arcade/score` is refused with 403 on a publicly-hosted dashboard
  (`STAN_DASHBOARD_READONLY=1`); the client says so instead of erroring. No
  exemption — a public instance cannot tell a player from a script.

---

## [1.0.2] — 2026-06-10

### Changed
- **PG Farm auth migrated to a service account.** Writes now connect as
  `genome-proteomics-service-account` (was the personal `brettsp` CAS token).
  The 7-day token is minted from a long-lived secret (`service-account.json`)
  via `scripts/pgfarm_refresh_token.py` instead of `pgfarm auth login` — no
  more manual weekly refresh. Secret lives at
  `/quobyte/proteomics-grp/brett/.pgfarm_secret.json` (chmod 600). See
  `docs/PG_FARM.md`.

### Fixed
- **Hive dispatch was writing to PG but deduping against SQLite** — a
  split-brain that, with PG-only writes, made the dispatcher re-submit the
  whole backlog every tick. `dispatch_hive._already_processed` and
  `hive_process._row_exists` now consult PG (`db_pg.raw_run_id_pg`) when
  `STAN_DB_BACKEND=pg`.
- **`_walk_raws` now resolves symlinks to their real target** so a flat farm
  of symlinks into the Flinders archive dispatches the canonical `/nfs/...`
  path — keeping the PG natural key aligned (no duplicate rows).
- Reset a **corrupted** global SQLite (`/quobyte/.../STAN/stan.db`); it now
  holds only the dispatch-audit + sample_health tables (runs live in PG).

### Added
- `stan/community/scripts/link_flinders_qc.py` — flattens Flinders QC raws
  (recursive, QC-pattern-filtered) into the dispatcher's flat watch dirs.
  Instruments sync raws to Flinders, which the flat/non-recursive dispatcher
  couldn't see.
- `scripts/cron_flinders_dispatch.sh` — 15-min Hive cron: refresh token →
  link Flinders → dispatch (the dispatch cron was never installed before).
- `scripts/pgfarm_refresh_token.py` — mint a 7-day PG token from the secret.
- `submit-all --since <ISO date>` — scope a community push to recent runs;
  `scripts/push_recent_community.sh` wraps it for the Mac (Hive has no
  egress to the relay).

## [1.0.1] — 2026-05-31

### Fixed
- CI: nightly `consolidate_benchmark` workflow — set the repo `HF_TOKEN`
  secret (was unset → "HF_TOKEN not set") and added `PYTHONPATH: .` so the
  consolidate script can import the `stan` package when run by path.

### Changed
- Docs: README marks the installable PWA as Partial (manifest + icons + iOS
  "Add to Home Screen" shipped; service worker + push-on-FAIL not yet) and adds
  a STAN Godmode (`STAN_DB_PATH` multi-instrument view) row.

_(Community HF Space perf work — gzip + lazy-TIC `/api/tic-overlay`, ~27× lighter
leaderboard — lives in the separate Space repo, not this package.)_

---

## [1.0.0] — 2026-05-30

First public release of STAN — Standardized proteomic Throughput ANalyzer.

### Highlights
- Watcher daemon with vendor-aware file-stability detection and DIA/DDA
  auto-detection; search runs locally or dispatches to SLURM/Hive (DIA-NN, Sage).
- QC metric extraction, IPS depth score (0–100), threshold gating + HOLD flag.
- Community benchmark: relay submission (no HF token required), frozen
  community-standardized search, SPD-bucketed cohorts.
- FastAPI + React dashboard (Trends, Plotly ion cloud, PEG/drift, sample health),
  with PG Farm (Postgres) and SQLite backends.

### Notes
- Folds in the v0.2.377 hardening (three F821 runtime-bug fixes, lint cleanup,
  documentation accuracy) and the v0.2.305–312 pre-release audit fixes.
- Known follow-ups deferred to 1.1 — see the "Out of scope for 1.0" section of
  [`docs/V1_PRERELEASE_CHECKLIST.md`](docs/V1_PRERELEASE_CHECKLIST.md). Operational
  follow-ups at cut time: update the timsTOF HT PC off v0.2.301 (DIA-NN `--`
  output-path fix) and the community-dataset wipe + repopulate.

---

## [0.2.377] — 2026-05-29

### Fixed
- **Three `NameError` runtime bugs** (Ruff F821), all on reachable paths:
  - `stan/cli.py` update-push path: `community_config` was never defined — now
    loaded via `load_community()` with a defensive fallback, matching the
    sibling `backfill-tic --push` block.
  - `stan/cli.py` cIRT `--auto` path: stale `cv_ladder[-1]` reference (left over
    from a removed CV-ladder design) now reports the in-scope `max_cv`.
  - `stan/sync/upload_to_hive.py`: `get_pending_uploads()` was called but its
    `def` had been lost, leaving the body as dead code after a `return` — the
    signature is restored so resume-on-restart no longer crashes when wired in.

### Changed
- Lint cleanup: 48 Ruff safe fixes across the package (F541 f-strings without
  placeholders, F401 unused imports). No behavior change; frozen community
  search params untouched.
- Documentation accuracy: README Implementation Status (CLI 57 commands, PG Farm
  + ingest-sharding rows, Thermo sample-health, Arcade→relay corrected to
  *Partial*/deferred-to-1.1), `docs/FEATURES.md` (Sage 0.14.7, cli.py line count,
  `backfill-from-dir`/`hive-upload`), and a stale "Thermo support TBD" comment in
  `stan/watcher/daemon.py`.
- Added this `CHANGELOG.md`.

---

## [0.2.376] — 2026-05-26

### Fixed
- Inline TIC is now captured correctly in the Postgres `process_raw` path; previously
  the TIC write was skipped when `STAN_DB_BACKEND=pg`.

---

## [0.2.375] — 2026-05-22

### Added
- `--shard N/M` flag on `stan ingest` — splits the raw-file list into M equal
  buckets and processes only shard N, enabling parallel ingest across multiple
  worker processes or SLURM array tasks.

---

## [0.2.374] — 2026-05-21

### Fixed
- `peak_width` was absent from the per-row dict written to Postgres, causing
  KeyError on runs that exercised that code path.
- Canonical path normalisation in PG storage (v0.2.373).
- `COALESCE` logic on PG upsert to avoid overwriting non-NULL columns with NULL
  on repeated ingests (v0.2.372).

---

## [0.2.371] — 2026-05-20

### Added
- `--force` flag on `stan ingest` — re-ingests a run even if it already exists
  in the database, overwriting the existing row.
- All v1.0 community fields (cohort_id, spd_bucket, it_params_tuned, etc.) are
  now populated in the Postgres write path, matching the SQLite path (v0.2.370).

### Fixed
- ISO 8601 serialisation of datetime columns before PG write — previously caused
  psycopg2 type errors on timezone-naive datetimes (v0.2.369).
- `diaPASEF` and `ddaPASEF` mode-detection gate now routes correctly; previously
  both could fall through to the DIA default (v0.2.368).

---

## [0.2.367] — 2026-05-19

### Added
- `stan submit-all --backend pg` — submits community benchmark rows read from
  the Postgres backend rather than local SQLite.
- PG-direct `sbatch` template for SLURM jobs that write directly to the central
  Postgres farm (v0.2.365).

### Fixed
- `insert_run` now routes to Postgres when `STAN_DB_BACKEND=pg` is set; previously
  the env-var gate was missing (v0.2.366).
- Upsert key is now the natural (instrument, run_date, raw_stem) triple rather
  than a synthetic surrogate, preventing duplicate rows on re-ingest (v0.2.364).

---

## [0.2.363] — 2026-05-18

### Added
- Mac-side path translation for `stan ingest` — Windows UNC paths in the DB are
  rewritten to local `/Volumes/` mounts automatically (v0.2.362).

### Performance
- Direct `sqlite3` read of Bruker TDF frames instead of going through timsrust,
  cutting ingest time by ~40% on large `.d` files (v0.2.363).
- PG connection is now cached for the lifetime of a CLI invocation rather than
  opened per query (v0.2.361).
- Bulk-load of PG run keys at ingest start to avoid per-row existence checks
  (v0.2.360).

---

## [0.2.359] — 2026-05-18

### Added
- Postgres-direct write path (`STAN_DB_BACKEND=pg`): STAN can now write QC metrics
  directly to the UC Davis Library central Postgres farm, enabling fleet-wide
  aggregation without manual SQLite syncing. See `docs/PG_FARM.md`.

---

## [0.2.357] — 2026-05-16

### Added
- `stan ingest-orphans` CLI — finds raw files that exist on disk but are absent
  from the database and queues them for ingest/backfill.

### Fixed
- DIA-NN version is now stamped from the search log file rather than hardcoded,
  so version strings stay accurate after DIA-NN upgrades (v0.2.356).
- Recovery preflight checks added to guard against partially-written DB state
  (v0.2.358).

---

## [0.2.355] — 2026-05-11

### Added
- Historical MS QC museum: a dashboard tab showing landmark QC runs from the
  lab's full instrument history (QE, Lumos, timsTOF SCP, Astral), with context
  annotations.
- Installation regression test and checklist (`stan test --install`) to verify
  all external tool binaries are reachable and return expected exit codes (v0.2.354).
- Arcade leaderboard wired through to the public community leaderboard on the
  HF Space dashboard (v0.2.353).

### Fixed
- Thermo DDA scan-filter detection was matching DIA windows as DDA on certain
  Exploris methods; fixed the regex (v0.2.352).

---

## [0.2.348] — 2026-05-08

### Added
- PWA manifest, icons, and `LICENSE` file; updated `docs/user_guide.md` and
  `README.md` with full rewrite for v1.0 audience.
- Auto-install of DIA-NN and Sage community search assets (Mode A + Mode C) via
  `stan install-assets` (v0.2.347).
- Cluster install guides and `stan setup` flows for Lumos and timsTOF HT on
  Hive (`lumos+timsTOF cluster install`, v0.2.346).
- WSL2 Mode B installer for labs that want to run searches locally on a Windows
  workstation without an HPC cluster (v0.2.345).
- `submit-after-upload` flag — automatically triggers `stan submit-all` after a
  successful Hive upload completes (v0.2.346).
- `STAN_DB_PATH` environment variable override — point the dashboard and CLI at
  any SQLite file, enabling the "STAN-Godmode" fleet view over Tailscale (v0.2.341).
- Monitor pipeline (`stan monitor`) with auto-classify: watches a Hive output
  directory and classifies finished SLURM jobs as pass/fail in real time (v0.2.336).
- `stan backfill-from-dir` CLI and corresponding remote action — backfill metrics
  for an entire directory of raw files without a running watcher (v0.2.331–332).

### Fixed
- Dashboard no longer auto-opens Internet Explorer on Windows (v0.2.338).
- Dashboard process is now killed before `pip install` during self-update, preventing
  file-lock errors on Windows (v0.2.337).
- Backfill defaults to `--qc-only` mode to avoid accidentally re-processing
  non-QC runs (v0.2.335).
- UTF-8 and ASCII arrow rendering in backfill output for Windows cmd.exe (v0.2.334).
- Backfill subprocess crash on Python 3.10 Windows (v0.2.333).
- 7-day staleness calculation and flip script for resetting `submitted_to_benchmark`
  flags (v0.2.329).
- `~tan-proteomics` ghost directory auto-cleaned on Windows (v0.2.330).
- Timezone-naive datetime in `last_qc` field caused comparison errors (v0.2.339).
- Dashboard restart after pip-install self-update now works reliably (v0.2.340).
- `sbatch` output directory and job classification in monitor mode (v0.2.344).

---

## [0.2.328] — 2026-05-07

### Added
- Parallel SLURM DAG per raw file: each `.d`/`.raw` now fans out to independent
  SLURM steps (mode-detect, search, extract, submit) that can run concurrently
  across the cluster.
- `stan hive-dispatch` — CLI to dispatch a single raw file to the Hive SLURM
  pipeline from the instrument PC (v0.2.318).
- `stan hive-process` — end-to-end Hive pipeline command: upload → dispatch →
  monitor → extract (v0.2.317).
- `stan hive-upload` CLI and per-instrument self-submit upload (v0.2.319–321).
- `stan time-hive-partitions` — benchmark Hive partition I/O speeds to inform
  optimal `--partition` selection (v0.2.320).

### Fixed
- 4DFF and alphatims paths on Hive corrected (v0.2.327).
- `extract_dia_metrics` now receives a `Path` object, fixing a string/Path
  mismatch on Hive (v0.2.326).
- `hive-dispatch --raw` argument now parsed correctly via Typer CLI (v0.2.325).
- Instrument vendor and family are now auto-derived from the raw file when not
  present in `instruments.yml` (v0.2.323).
- Y: drive mapping corrected to share root rather than a subdirectory (v0.2.322).

---

## [0.2.317] — 2026-05-06 / 2026-05-07

### Added
- Auto-detect Tailscale MagicDNS URL for godmode dashboard access (v0.2.315).
- `STAN_DASHBOARD_EXTRA_ORIGINS` env var — whitelist additional origins for the
  dashboard CORS policy without editing source (v0.2.314).

---

## [0.2.305–0.2.313] — 2026-05-05

Pre-release audit sprint — 8 commits resolving v1.0 blockers identified in
the v0.2.305–312 audit pass.

### Added
- `stan.bat` self-updates from GitHub on every launch; a fresh download is forced
  if the local copy is stale (v0.2.302, v0.2.297).
- `stan doctor` now probes ThermoRawFileParser (TRFP) and reports binary size and
  `--help` exit code (v0.2.296).
- Single-click `stan.bat` launcher for Windows instrument PCs (v0.2.295).
- `reinstall_trfp` remote control action via the fleet API (v0.2.298).
- TIC/BPC toggle on the Bruker dashboard tab; DIA fallback mode for TIC when
  BPC is unavailable (v0.2.301).
- BPC (base-peak chromatogram) capture alongside TIC at ingest (v0.2.300).
- Inline 4DFF ion-cloud feature extraction fires inside the watcher immediately
  after a Bruker `.d` acquisition completes (v0.2.299).

### Fixed
- Exact-match gate on community asset hashes — prevents submissions that were
  searched with a non-canonical FASTA or spectral library (v0.2.305).
- `spd_bucket` vocabulary reconciled between SQLite schema and IPS scoring code;
  mismatched keys caused NULL IPS scores (v0.2.306).
- Three audit blockers: CSRF protection on fleet API, secrets redaction in logs,
  and `it_params_tuned` flag initialisation (v0.2.307–308).
- `run_name` dropped from relay submission payload to prevent accidental filename
  leakage; relay accepts empty string (v0.2.310).
- Arcade XSS vulnerability patched; BPC backfill skip condition corrected (v0.2.309).
- `dispatch_attempts` retry table schema corrected (v0.2.312).
- `PRAGMA user_version` stamp added to DB migrations (v0.2.311).
- TRFP v1.4.5 path corrected on Windows (v0.2.303).
- `stan.bat` pip install now uses `--upgrade` supervisor flag (v0.2.313).

---

## [0.2.294] — 2026-05-04 / 2026-05-05

### Added
- `ms2_analyzer` column stamped on every run (`OT` or `IT`) for Thermo
  instruments — prerequisite for the OT vs IT cohort split on the leaderboard.
- IT MS2 detection using scan-filter regex; IT runs get ±0.5 Da fragment tolerance
  in Sage searches (v0.2.291–292).

### Fixed
- IT fragment tolerance reverted to OT default after over-correction (v0.2.293).
- DIA-NN `--` path separator in SLURM commands and relay retry logic (v0.2.290).
- DDA library-coverage-pct field dropped from community payload (DDA doesn't use
  a spectral library) (v0.2.288).
- TRFP invoked via `dotnet` on Hive for DDA `.raw` conversion (v0.2.287).
- DDA `n_peptides` correctly mapped from Sage TSV output (v0.2.281).
- Sage TSV-to-parquet bridge fixed for updated column names (v0.2.279).

---

## [0.2.282] — 2026-04-xx

### Added
- Amount detection from raw filename and metadata (50 ng, 200 ng, etc.); runs
  with a pre-2020 acquisition date are deferred to prevent contaminating cohort
  references with legacy data.
- `godmode submit_all` remote action (v0.2.286).
- DDA `n_proteins` metric and payload field (v0.2.280).

---

## [0.2.268–0.2.276] — 2026-04-xx

### Added
- Per-instrument subset spectral libraries — each instrument gets a library
  pre-filtered to peptides detected in that instrument's own HeLa history,
  reducing search time without sacrificing sensitivity (v0.2.268).
- Column metadata (column_vendor, column_model, lc_system) sourced from
  `instruments.yml` (v0.2.267).
- Cluster runs now ship TIC trace and gradient length to the community relay
  (v0.2.266).
- Dispatcher `--partition` flag with default `high`; account/QoS pairing
  validated (v0.2.261–262).
- Library saturation warning fires when >95% of library peptides are detected,
  indicating a library that is too small for the instrument's depth (v0.2.269).
- Bruker DDA via Sage: `ddaPASEF` files now route to a Sage search on Hive
  (v0.2.273).

### Fixed
- SPD extracted from DIA-NN report when metadata chain returns NULL (v0.2.274).
- Real acquisition date read from raw file metadata rather than filesystem mtime
  (v0.2.272).
- `TICTrace.rt_min` attribute name corrected (v0.2.270).
- Apptainer bind-mount flags corrected for Hive DIA-NN container (v0.2.263).
- Thermo `pts_per_peak` computed from FWHM scan count rather than total scans
  (v0.2.276).
- `huggingface-cli` replaced with `hf` (CLI was renamed in Hub ≥0.22) (v0.2.260).
- CV removed as a hard gate on community submission; it is now diagnostic-only
  (v0.2.264–265).

---

## [0.2.254–0.2.259] — 2026-04-xx

### Added
- v1.0 community schema reset: new submission parquet schema with all required
  v1.0 fields; old submissions are treated as legacy (v0.2.254).
- Raw QC file sync to Hive mirror on ingest completion (v0.2.255).
- Auto-inject asset hashes into community submissions at submit time (v0.2.256).
- Time-stratified backlog sampler — selects representative runs across the
  instrument's date range for bulk re-search (v0.2.257).
- Writable asset cache with loud failure messages when the cache directory is
  not writable (v0.2.259).

---

## [0.2.238–0.2.253] — 2026-04-xx

### Added
- `stan setup` auto-verifies instrument config after interactive setup (v0.2.230).
- `stan list-stale` audit command — lists runs that predate a given version and
  lack the full v1.0 metadata complement (v0.2.220).
- `stan_version` column on the `runs` table — every row now records the STAN
  version that ingested it (v0.2.219).
- Column flow + cIRT borrow: LC column metadata flows from `instruments.yml`
  through to community submissions; missing cIRT panels borrow from sibling
  instruments in the same family (v0.2.223).
- `--force-auto` flag on `stan derive-cirt-panel` (v0.2.226).
- Screencap remote actions — `screencap_install`, `screencap_list_windows`,
  `sync_now`, `multi-window screencap`, `capture_all` (v0.2.242–252).
- Atomic DB sync with rolling backups: the SQLite file is written atomically
  (write to `.tmp`, rename) to prevent corrupt mid-write snapshots (v0.2.243).
- Five backfill remote actions via the fleet API (v0.2.241).
- `v1_prep` remote action: runs the full backfill sequence in one command from
  the fleet API (v0.2.238).
- v1.0 runbook (`docs/V1_RUNBOOK.md`) and `stan submit-all --force` flag (v0.2.237).
- Watcher crash visibility: fatal watcher exceptions now write a `failures/`
  entry to the Hive mirror rather than silently dying (v0.2.235).
- Windows keep-awake: prevents the instrument PC from sleeping while the watcher
  is active (v0.2.253).

### Fixed
- Instrument model resolution unified to a single code path; `runs` and
  `sample_health` tables no longer write `"auto"` as the instrument name
  (v0.2.229–234).
- cIRT CV is now diagnostic only, not a filter that blocks ingest (v0.2.224).
- `backfill-metrics` reads column metadata from YAML rather than expecting it in
  the raw file (v0.2.225).
- Lumos run-resolution and watcher restart after Hive pipeline failures (v0.2.236).

---

## [0.2.217–0.2.219] — 2026-04-10 (session sweep)

### Added
- Bruker method XML parser for SPD extraction — reads `submethods.xml` (UTF-8)
  and `SampleInfo.xml` (UTF-16) to get the operator-selected HyStar method name,
  which is authoritative over TDF metadata and filename heuristics.
- DIA-NN output filename sanitiser to handle non-ASCII and path-length edge cases.
- `/api/update` endpoint on the dashboard for remote version-check and self-update.
- HF Dataset backfill for runs submitted before `stan_version` was tracked.
- `stan test --extract` command for per-field extraction smoke tests (v0.2.217).

### Fixed
- SPD resolution chain now mirrors the watcher chain; NULL SPD rows that had
  SPD tokens in their filenames were being missed (v0.2.218–275 series).

---

*Entries above v0.2.217 are reconstructed from `git log` commit messages.
Earlier history predates this changelog and is not included.*

[Unreleased]: https://github.com/bsphinney/stan/compare/v0.2.376...HEAD
[0.2.376]: https://github.com/bsphinney/stan/compare/v0.2.375...v0.2.376
[0.2.375]: https://github.com/bsphinney/stan/compare/v0.2.374...v0.2.375
[0.2.374]: https://github.com/bsphinney/stan/compare/v0.2.373...v0.2.374
[0.2.371]: https://github.com/bsphinney/stan/compare/v0.2.370...v0.2.371
[0.2.367]: https://github.com/bsphinney/stan/compare/v0.2.364...v0.2.367
[0.2.363]: https://github.com/bsphinney/stan/compare/v0.2.359...v0.2.363
[0.2.359]: https://github.com/bsphinney/stan/compare/v0.2.357...v0.2.359
[0.2.357]: https://github.com/bsphinney/stan/compare/v0.2.355...v0.2.357
[0.2.355]: https://github.com/bsphinney/stan/compare/v0.2.348...v0.2.355
[0.2.348]: https://github.com/bsphinney/stan/compare/v0.2.328...v0.2.348
[0.2.328]: https://github.com/bsphinney/stan/compare/v0.2.317...v0.2.328
[0.2.317]: https://github.com/bsphinney/stan/compare/v0.2.313...v0.2.317
[0.2.305]: https://github.com/bsphinney/stan/compare/v0.2.294...v0.2.313
[0.2.294]: https://github.com/bsphinney/stan/compare/v0.2.282...v0.2.294
[0.2.282]: https://github.com/bsphinney/stan/compare/v0.2.268...v0.2.282
[0.2.268]: https://github.com/bsphinney/stan/compare/v0.2.254...v0.2.268
[0.2.254]: https://github.com/bsphinney/stan/compare/v0.2.238...v0.2.254
[0.2.238]: https://github.com/bsphinney/stan/compare/v0.2.217...v0.2.238
