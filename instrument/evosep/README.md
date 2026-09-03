# Evosep One → STAN column health & clog early warning

Turns the **pressure time-series the Evosep One writes for every run** into
column-health signals and a Maintenance-tab feature for the STAN QC dashboard,
for the UC Davis Proteomics Core.

## Why this exists

Bruker's Compass database records an LC failure only as an **error string** —
`"Evosep One: pressure limit exceeded"` — i.e. *after* a run has already died,
and only when it died. Every partial blockage that degraded a sample without
aborting it is invisible there.

The Evosep One, meanwhile, logs the full pressure curve of every procedure. The
same event is visible in that curve as a **rising line, minutes earlier** — and
the near-misses are visible at all.

Everything below is measured from real mirrored logs — **568 runs, 2026-08-14 →
2026-09-01**, which at the time lived in
`…/evosep_logs/TIMS-10878_20260901_173533/S00230/`. No placeholders.

Since 2026-09-02 the copy script maintains **one stable incremental mirror**,
`<HOST>_mirror`, instead of a timestamped folder per pull, and copies the whole
2023-onward history by default. The extractor reads every log folder belonging
to the instrument — the mirror plus any legacy timestamped pulls — and
de-duplicates by run folder, so the two layouts coexist and overlapping runs are
counted once. The numbers in this section predate the backfill.

---

## Which pump is the analytical column: `Pump-HP`

Verified from the data, not assumed:

| Evidence | Pump-HP | Pumps A / B / C / D |
|---|---|---|
| Pressure range | **300 – 520 bar** | never above ~10 bar in normal running |
| Flow | **1.68 µL/min ± 0.30** (nano-flow) | loading / wash scale |
| Shape | traces the gradient; reproducible run-to-run to **0.36 bar** | flat near zero |
| `maintenance-info.txt` | product number **1001** | product number **1002** |

Only a pump driving a packed analytical bed develops hundreds of bar at
1.7 µL/min. Pumps A/B are still read, because a spike *there* is a different
fault — see the two channels below.

---

## What it found — real numbers

### The headline: the pressure trace catches every logged LC failure, early

Within the window Compass covers (to 2026-08-31 17:53):

| Compass logged | Category | Peak bar | Warning before cut-out |
|---|---|---|---|
| 2026-08-22 03:05 | LC pressure / clog | 519.2 | **20.2 min** |
| 2026-08-28 02:07 | LC pressure / clog | 519.9 | **8.5 min** |
| 2026-08-28 10:12 | LC pressure / clog | 519.9 | **12.9 min** |
| 2026-08-27 12:20 | Evotip missing | 295.8 | aborted at 0.4 min |
| 2026-08-29 18:09 | Evotip missing | 352.1 | 65 bar on pump A/B, 0.9 min abort |
| 2026-08-31 14:38 | Evotip missing | 342.7 | 68 bar on pump A/B, 0.9 min abort |

**3/3 LC clogs and 3/3 Evotip failures detected. Zero false negatives.**

The specificity is just as good: across all 568 runs, **exactly four** ever
reached the 520 bar cut-out, and three of them are precisely the three clog
failures. (The fourth, 2026-09-01 00:01, falls *after* the newest Compass
backup, so we cannot say whether Compass logged it.)

### The honest negative result

**Run-to-run baseline does not forecast these clogs days in advance.** The five
runs immediately before the 2026-08-28 02:07 clog were:

```
333.9  333.9  333.9  332.1  333.9  bar     (run-to-run sd 0.70 bar)
```

Dead flat, then the next run jumped to a 417.8 bar plateau and hit the cut-out.
These blockages are **sudden, single-run events** — consistent with particulate
from one sample, not gradual fouling. Anyone promising "days of warning" from
this data would be inventing it.

The warning is real but it lives **inside the run**. The failing run tracked its
reference curve *exactly* for 4.8 minutes, then departed and climbed
monotonically for 8.5 minutes before hitting the limit. That is enough to abort
the run, save the Evotip and the sample, and alert the operator — it is not
enough to reschedule a week.

### What the error log structurally cannot show

Between 2026-08-31 23:18 and 2026-09-01 02:08, fourteen consecutive runs ran
**20–37 % above baseline**, one of them reaching 519 bar — one bar under the
cut-out. Every one of them **completed**, so no error was ever raised. Those
samples' data was acquired on a badly restricted column and nobody knew. This is
the case the feature exists for.

### Column ageing

> **The inferred install date on this window was wrong, and the extractor now
> says so.** It reported 2026-08-19 (13.2 days, 455 injections). The real column
> change was **2026-07-31** — the operator recorded it in the run names
> (`07312026_HE50_60-spd-dia-new-zdf-column`) — which is 14 days *before* the
> first mirrored log, so no inference over this window could have found it.
> 2026-08-19 is the day a new *glass capillary* went in (`19aug26_HeL50-newGlasCap`),
> a different intervention with a similar pressure signature.
>
> `column_age()` now reports `confidence` (`logged` / `inferred` /
> `unverifiable`) and, when fewer than `INSTALL_MIN_PRIOR_DAYS` (14) of log
> precede the step, sets `installed_is_lower_bound` and a `caveat` instead of
> asserting a date. On this window it returns `unverifiable`. `days_since` and
> `injections_since` are lower bounds whenever it does.

| | |
|---|---|
| Days on the current column | **≥13.2** (lower bound — the log window opens after the real change) |
| Injections on it | **≥455** (from the instrument's own lifetime counter) |
| Baseline at install → now | **312 → 344 bar (+10.0 %)** over the observed window |

STAN's `maintenance_events` table has **no `column_change` rows at all**, so the
install date is inferred from a sustained downward step in baseline pressure.
When someone does log a column change, the extractor prefers it automatically
(`--instrument` + `stan.db.get_events`). Logging changes is worth doing: it
converts an inference into a fact.

### Instrument wear (from `maintenance-info.txt`, written per run)

Total analyses **27,822** at **35.9/day**. Pump seal displacement:

| pumpa | pumpb | pumpc | pumpd | pumphp |
|---|---|---|---|---|
| 1,840 mL | 972 mL | 2,769 mL | 1,203 mL | **337 mL** |

These are monotonic lifetime counters, so differencing them across runs gives a
real wear rate (+1.2 mL/day on the HP seal) — which is what tells you when a
seal service is due, rather than a calendar.

---

## The signals, and why each matters for column health

| Signal | What it is | Why you want it |
|---|---|---|
| **Plateau pressure** | median Pump-HP bar over 45–80 % of the run | The column-resistance number. That window is flat to <1 bar and holds solvent composition constant, so run-to-run differences are the column, not the gradient. |
| **Local (trailing) baseline** | median plateau of the previous 12 runs of the same method | "Abnormal" has to mean *relative to where this system just was*. A global median mixes a fresh column with a spent one. Trailing only — a run is never compared to its own future, so the number matches what a live watchdog would have had. |
| **Peak vs the 520 bar cut-out** | max pressure in the run | The hardware limit. Touching it *is* the abort. Empirically the sharpest single discriminator in the dataset. |
| **Envelope breach + lead time** | first sustained departure from the rolling reference curve | The early warning itself, in minutes. Measured to the moment the run hit the cut-out, not to the end of the log. |
| **Tip pressure (pumps A/B)** | max low-pressure-side bar | A *separate* fault: Evotip missing or unseated. Calibrated from the data — every run's seating test legitimately reaches ~50 bar, and the real failures reached 65–68. |
| **Sustained baseline steps** | ≥6 % level change that holds, off a stable level | Interventions: a new column, a wash, a seal service. Requiring both "holds after" and "was stable before" stops a single clogged sample's trailing edge from faking one. |
| **Per-method baselines** | grouped by gradient, never pooled | A 100 spd plateau (~325 bar) and a 60 spd (~236 bar) are different physics. Pooling them destroys both. |
| **Method classification** | analytical vs pressure-controlled vs utility | `System-and-column-wash` sits at 399.6 ± 0.3 bar across weeks and straddles two clog events — a regulated setpoint, not a measurement. Excluded, or it would flatten every baseline. |
| **Wear counters** | seal mL, total analyses | Service scheduling from actual use. |

---

## Run it

Read-only. Nothing under the log root is ever written.

```bash
# on Hive — newest mirror, whole window, to stdout
python3 extract_evosep.py

# to a file, windowed (what the cron does)
python3 extract_evosep.py --since 2026-06-01 --out evosep_column_health.json

# a specific mirror, and cross-reference STAN's logged column changes
python3 extract_evosep.py --host-dir TIMS-10878_mirror \
    --instrument timsTOF-HT --out out.json
```

| Flag | Default | Purpose |
|---|---|---|
| `--root` | `/quobyte/proteomics-grp/brett/evosep_logs` | Log mirror root |
| `--host-dir` | newest | Specific `<HOST>_<timestamp>` folder |
| `--since` | none | Only runs on/after this date |
| `--max-runs` | none | Keep only the newest N |
| `--out` | stdout | Output JSON (written atomically) |
| `--instrument` | none | STAN instrument, for `column_change` cross-reference |
| `--bruker-json` | `/quobyte/.../STAN/bruker_maintenance.json` | Scores the flags against Compass's failure log |
| `--curve-points` | 48 | Points per downsampled curve |

Idempotent: same input, same output (modulo `generated_at`). ~1 min for 568
runs; the output is ~340 KB of JSON, ~70 KB as PG `jsonb`.

> **Pending: the full history pull.** Only 2026-08-14 → 09-01 has been mirrored
> so far; a full 2023→now pull (several GB) is still to come on the instrument
> side. The extractor is built for it — signal files are streamed and discarded
> per run, only a 48-point curve is retained, and curves are dropped from the
> output for all but flagged runs and a recent tail per method. Use `--since` /
> `--max-runs` to window a routine tick; the cron already passes `--since`.

### Tests

```bash
python3 -m pytest test_extract_evosep.py -q      # 15 passed
```

They cover the parts that quietly change the headline numbers: absolute-time
alignment, breach persistence, trailing baselines, and method classification.
(The step-detection test is why the "was stable before" rule exists — it caught
a transient spike's trailing edge being counted as a column change.)

---

## How it's wired into STAN

```
Evosep One  ──mirror──▶  /quobyte/.../evosep_logs/<HOST>_<ts>/<serial>/<run>/
                                    │
                     extract_evosep.py (Hive, read-only)
                                    │
                    evosep_column_health.json  ──▶  PG Farm (evosep_column_health)
                                    │                        │
                          config/ fallback copy              │
                                    └────────┬───────────────┘
                                             ▼
                        GET /api/maintenance/evosep   (server.py)
                                             │
                                             ▼
                        <EvosepColumnPanel/>  in the Maintenance tab
```

Shipped in STAN **1.0.53**:

- `stan/dashboard/server.py` — `@app.get("/api/maintenance/evosep")`, placed
  beside the Bruker endpoint and following it exactly: PG Farm first, then
  `resolve_config_path("evosep_column_health.json")`, 404 when neither exists.
- `stan/db_pg.py` — `get_evosep_column_health_pg()`. Read-only and DDL-free; a
  missing table means "publisher hasn't run", not an error.
- `stan/dashboard/public/index.html` — `EvosepColumnPanel` (above
  `MaintenanceTab`), rendered beside `<BrukerAcquisitionPanel />`. STAN's own
  `useFetch`, `.card`, theme vars, inline SVG. **No new JS libraries.**
- `config/evosep_column_health.json` — the bundled fallback document.
- `tests/test_evosep_endpoint.py` — 7 tests for the delivery contract.

The dashboard never reads the instrument logs; it reads a small JSON, exactly
as it already reads `bruker_maintenance.json`.

### Colour and accessibility

The three method hues are dataviz categorical slots 1–3
(`#3987e5`, `#d95926`, `#199e70`), validated **all-pairs against STAN's navy
surface `#022851`** — worst CVD ΔE 9.4, worst normal-vision ΔE 20.9, all ≥3:1
contrast. Severity uses STAN's reserved status vars and always ships a **glyph +
word**, never colour alone: `--warn` and `--pass` fail CVD separation against
each other (ΔE 4.2 protan), so a colour-only badge would be unreadable. Every
method line is directly labelled; every chart with two series has a legend.

---

## Schedule it (suggested — **not** installed)

`cron_evosep_column_health.sh` follows `cron_bruker_maintenance.sh`: `flock`-
guarded, extracts to a temp file, sanity-checks that the document has runs,
publishes atomically, then upserts into PG Farm.

```
15 * * * * flock -n /tmp/stan_evosep_health.lock /quobyte/proteomics-grp/STAN/cron_evosep_column_health.sh
```

Hourly rather than nightly: the value is catching a rising column before the
next plate is burned, and the extract takes about a minute.

The PG publish is **DDL-free by design** — the table is owned by `brettsp` via
`migrations/2026-09-01_instrument_telemetry_cache.sql`; the service account has
DML only and `CREATE TABLE IF NOT EXISTS` would be refused even though the table
exists. `publish_evosep_pg.py` upserts and nothing else.

---

## Files

| File | What it is |
|---|---|
| `extract_evosep.py` | The extractor. Read-only, stdlib only, `--out`/`--since`/`--host-dir`. |
| `test_extract_evosep.py` | 15 unit tests for the analysis logic. |
| `evosep_column_health.json` | Real extracted output (568 runs). |
| `maintenance_preview_evosep.html` | **Standalone prototype** — open it; renders the real data. |
| `snippet_index_component.jsx` | The React panel, as pasted into `index.html`. |
| `build_preview.py` | Builds the prototype *from that same component*, so the preview cannot drift from what ships. |
| `publish_evosep_pg.py` | PG Farm upsert (DDL-free). |
| `cron_evosep_column_health.sh` | Suggested scheduled refresh. |

---

## Things worth knowing

- **Compare on absolute minutes, not relative time.** An aborting run is short
  *because* it failed, so a relative-time axis compresses it and shifts every
  feature earlier. Comparing bin-for-bin misaligns the steep loading transient
  and manufactures a false early breach — on the 08-28 02:07 clog that artifact
  turned a real 8.5 min warning into a false 11.1 min one.
- **A breach must persist.** A single point inside a steep transition is worth
  tens of bar for a fraction of a second of timing shift. The extractor requires
  the next few points to stay breached.
- **`Preparation` has no characteristic duration** (2.3 min to 2,451 min), so
  the "aborted" test is meaningless for it and is gated on duration stability.
- **STAN's local `stan.db` currently fails `PRAGMA integrity_check`** (index
  corruption on `dispatch_attempts`) — the recurring Quobyte-SQLite issue in
  `CLAUDE.md`. Not touched here, but it is why the extractor treats a STAN
  lookup failure as "no logged events" rather than an error.
