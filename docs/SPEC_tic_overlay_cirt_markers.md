# Spec — Today's Runs TIC Overlay + cIRT RT Drift Markers

> **Status**: Planning. Not yet implemented.
> **Owner**: Brett (decisions), Claude (implementation)
> **Target version**: v0.2.104 (after multi-charge detector; can ship independently)
> **Estimated effort**: 4–6 days across all phases
> **Date drafted**: 2026-04-20
> **Resolves TODOs**: #1 (TIC overlay/facet toggle), #12 (Thermo TIC backfill), #18 (column tracking), #21 (replace Biognosys iRT library)
> **Partially addresses**: #11 (Thermo TIC fisher_py bug — mitigation in scope, full fix deferred), #20 (points-across-peak Thermo extension — Phase 5, gated on Spectronaut validation)
> **Depends on**: Nothing — can start immediately. #21 (cIRT library fix) must ship before Phase 4 (markers).

---

## 1. Goal

Fill the empty space in the **Today's Runs** dashboard tab with something that earns
its real estate: an overlay of every run's TIC trace from today, faceted into three
panels — **QC | Sample | Blank** — so an operator can visually assess the health of
the entire day's queue at a glance.

On top of the QC panel, overlay **cIRT peptide RT markers** — small colored triangles
at the observed elution RT of each retention time calibration peptide, colored green /
yellow / red by deviation from the expected reference. A row of green ticks = gradient
is stable. A row of red ticks = something drifted.

This replaces the dead `DEFAULT_IRT_LIBRARY` (Biognosys iRT peptides that are no
longer available to most labs) with an empirical cIRT panel derived from STAN's own
historical data, fixing `irt_max_deviation_min` which currently always returns 0.

### What an operator sees

```
┌─────────────────────────────────────────────────────────┐
│ Today's Runs  (April 20 — 12 runs)                      │
│                                                         │
│ [QC table: run name, GRS, precursors, CV, gate result]  │
│                                                         │
├─────────────────────────────────────────────────────────┤
│  QC (4 runs)     ◀ cIRT markers shown here              │
│  ████████████████████████████████████████               │
│  ▲    ▲   ▲▲  ▲ ▲▲▲  (green/yellow/red ticks)          │
│                                                         │
│  Sample (7 runs)                                        │
│  ████████████████████████████████████████               │
│                                                         │
│  Blank (1 run)                                          │
│  ████████████████████████████████████████               │
└─────────────────────────────────────────────────────────┘
```

Within each panel, traces are colored light → dark by time of day (earliest = lightest,
latest = darkest), so a color gradient through the day immediately reveals trends.

---

## 2. Why now / what's broken without this

- The Today's Runs table shows metrics that only mean something in context. A precursor
  count of 14,200 is good on an Exploris, bad on a timsTOF Ultra 2. The TIC shape
  tells you immediately whether the LC is the problem — you don't need a threshold.
- Sample runs and blanks have no QC numbers at all today. Their TICs are the only
  signal visible in real time.
- `irt_max_deviation_min` has always returned 0.0 because the Biognosys iRT peptides
  are rarely spiked, and the `DEFAULT_IRT_LIBRARY` is built around them. This metric
  is wired into GRS scoring but contributes nothing. Fixing the library makes it real.
- Labs that spike Pierce RT calibration mix, Escher-Chem, or their own cIRT panel
  get no benefit from STAN's iRT logic today.

---

## 3. Scope

### In scope

- Persistent TIC trace storage in SQLite (new `tic_traces` table)
- Backfill TIC traces for historical Bruker runs from raw MS1 extraction (best-effort)
- Thermo TIC backfill from Hive-side `report.parquet` identified-TIC path (#12)
- Thermo TIC fisher_py bug (#11): graceful fallback + UI warning when trace unavailable
- cIRT library replacement: configurable YAML panel, bootstrapped from historical data (#21)
- Observed cIRT RT extraction from DIA-NN `report.parquet` during search completion
- New FastAPI endpoints for bulk TIC + cIRT data for today's runs
- Three-panel TIC overlay in the Today's Runs tab (React component)
- Time-of-day color gradient within each panel
- cIRT RT deviation markers on QC panel only
- Hover tooltip: run name, acquisition time, gate result
- Click-to-navigate to run detail page
- `stan cirt bootstrap` CLI command to derive reference RTs from historical data
- **Column tracking** (#18): `column_installs` table; "new column" event markers on TIC panel

### Out of scope (v1)

- Community TIC comparison (TODO #2/#3 — separate feature)
- Mobile / PWA rendering (TODO #6)
- XIC (per-peptide chromatogram) view — just TIC for now
- Real-time TIC streaming during active acquisition
- Storing TIC traces for non-QC runs where no DIA-NN search is run; these will be
  extracted directly from the raw file's MS1 TIC channel

### Deferred to Phase 5 (post-v1)

- **Points-across-peak Thermo extension** (#20): algorithm exists for Bruker; Thermo
  path requires cross-validation against Spectronaut output on real Thermo data before
  shipping. Must confirm STAN values match Spectronaut's equivalent metric within
  acceptable tolerance on the same files (same validation method used for Bruker).
  Do not ship until validated.

---

## 4. Data model

### 4.1 New table: `tic_traces`

```sql
CREATE TABLE tic_traces (
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    source          TEXT NOT NULL,  -- "diann_report" | "raw_ms1" | "backfill"
    rt_seconds      TEXT NOT NULL,  -- JSON float array, e.g. [0.5, 1.0, 1.5, ...]
    intensity       TEXT NOT NULL,  -- JSON float array (same length), raw counts
    n_points        INTEGER NOT NULL,
    rt_start_sec    REAL,           -- min RT in trace (for quick range queries)
    rt_end_sec      REAL,           -- max RT in trace
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (run_id)
);

CREATE INDEX idx_tic_traces_run ON tic_traces(run_id);
```

**Storage estimate**: 1h timsTOF TIC at ~1 s resolution = ~3600 points.
As JSON float32 arrays: ~28 KB per trace. For 30 runs/day = ~840 KB. Acceptable.

**Downsampling**: Store at native instrument resolution if ≤ 5000 points; otherwise
downsample to 3600 points (Lttb algorithm — Largest-Triangle-Three-Buckets) before
storage. This keeps the visual shape correct at any zoom level.

### 4.2 New table: `cirt_observations`

Stores per-run, per-peptide observed RTs from DIA-NN for the cIRT panel:

```sql
CREATE TABLE cirt_observations (
    run_id          TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    peptide_seq     TEXT NOT NULL,  -- stripped sequence, e.g. "LGGNEQVTR"
    charge          INTEGER,        -- precursor charge state
    observed_rt_sec REAL NOT NULL,  -- DIA-NN reported RT in seconds
    expected_rt_sec REAL,           -- from cirt_library at time of run
    deviation_sec   REAL,           -- observed - expected (positive = later)
    q_value         REAL,           -- DIA-NN Q.Value for this observation
    intensity       REAL,           -- Precursor.Quantity
    PRIMARY KEY (run_id, peptide_seq)
);

CREATE INDEX idx_cirt_obs_run ON cirt_observations(run_id);
```

### 4.3 New table: `column_installs` (TODO #18)

Tracks when LC columns are installed per instrument, so the TIC panel can annotate
the "new column" events and explain TIC variance.

```sql
CREATE TABLE column_installs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id   TEXT NOT NULL,
    installed_at    TEXT NOT NULL,  -- ISO timestamp (date is enough if exact time unknown)
    vendor          TEXT,           -- "Waters", "Thermo", "Phenomenex", "AgilentRestek", etc.
    model           TEXT,           -- "HSS T3", "Acclaim PepMap", "Kinetex C18"
    length_mm       INTEGER,        -- column length in mm
    id_um           INTEGER,        -- inner diameter in µm (e.g. 75 for standard nano)
    particle_size_um REAL,          -- particle size in µm (e.g. 1.8, 2.0)
    serial          TEXT,           -- optional vendor serial / lot number
    notes           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_col_installs_instrument ON column_installs(instrument_id, installed_at);
```

**CLI**: `stan column install --instrument timsTOF-Ultra-2-0001 --vendor Waters --model "HSS T3" --length 150`

**Dashboard**: column installs table is editable from the Instruments tab (operator logs
a new install when they swap a column).

**TIC panel usage**: at render time, query `column_installs` for the instrument, sorted
by `installed_at`. Draw a thin vertical dashed line at the install timestamp on the
longitudinal TIC trend view (not on Today's panel, which is a single day). In the Today's
panel, show the current column model as a subtitle under the panel header:
`QC (4 runs) — Waters HSS T3, 150mm × 75µm, installed 2026-04-15 (5 days ago)`.

### 4.4 cIRT library config (YAML)

Lives at `~/.stan/cirt_library.yml`. Auto-generated by `stan cirt bootstrap`.
Can be hand-edited.

```yaml
# STAN cIRT retention time calibration library
# Generated: 2026-04-20 by stan cirt bootstrap (n=42 historical QC runs)
# Instrument: timsTOF-Ultra-2-0001
# Warning: expected_rt_sec is instrument + method specific.
#          Re-run bootstrap if you change gradient length, flow rate, or column.

version: 1
gradient_min: 21
instrument_model: "timsTOF Ultra 2"
n_runs_used: 42

peptides:
  - seq: "LGGNEQVTR"
    charge: 2
    expected_rt_sec: 312.4
    rt_stddev_sec: 4.1   # observed variability across bootstrap runs
  - seq: "GAGSSEPVTGLDAK"
    charge: 2
    expected_rt_sec: 487.2
    rt_stddev_sec: 3.8
  - seq: "VEATFGVDESNAK"
    charge: 2
    expected_rt_sec: 623.7
    rt_stddev_sec: 5.2
  - seq: "YILAGVENSK"
    charge: 2
    expected_rt_sec: 712.0
    rt_stddev_sec: 4.9
  - seq: "TPVISGGPYEYR"
    charge: 2
    expected_rt_sec: 814.3
    rt_stddev_sec: 3.6
  - seq: "DSTLIMQLLR"
    charge: 2
    expected_rt_sec: 958.1
    rt_stddev_sec: 6.3
  # ... additional peptides as found in historical data
```

The library is a ranked list — STAN uses whichever peptides are actually identified in
each run. Runs that identify fewer than 3 cIRT peptides get `irt_max_deviation_min = null`
(not 0) and are excluded from the iRT deviation gate.

---

## 5. cIRT library replacement (TODO #21)

### 5.1 The problem

`stan/metrics/cirt.py` contains a `DEFAULT_IRT_LIBRARY` built around Biognosys iRT
peptides (`LGGNEQVTR`, `AGGSSEPVTGLDAK`, etc.) with hardcoded absolute RT values.
These are only meaningful if the lab spikes in Biognosys iRT standards.

The `compute_irt_deviation()` function computes:
1. For each iRT peptide found in `report.parquet`, record observed RT
2. Compare to `DEFAULT_IRT_LIBRARY` expected RT
3. Return max and median deviation

Because most labs don't spike iRT standards, no matches are found, and the function
returns 0 (or null) always — making the GRS `carryover_scaled` component meaningless.

### 5.2 The fix

Replace the absolute hardcoded library with a two-tier approach:

**Tier 1 — Empirical cIRT panel (new default)**:
Bootstrap expected RTs from the lab's own historical data. Any peptides that appear
consistently across ≥ 80% of historical QC runs are candidates. The bootstrap picks
the top N by frequency and computes median observed RT + stddev as the reference.

Benefits:
- Works with whatever peptides the lab is actually seeing (HeLa tryptic peptides)
- Self-calibrating: reference RTs match the lab's actual gradient
- No need to purchase or spike any standard

**Tier 2 — Named standards (optional)**:
If `~/.stan/community.yml` specifies `irt_standard: "biognosys"` or `"pierce_rtcm"`,
STAN uses a built-in table of relative iRT values for those peptides and converts to
absolute RT via linear regression on observed/expected pairs (classic iRT calibration).

### 5.3 `stan cirt bootstrap` CLI

```bash
stan cirt bootstrap [--instrument INSTRUMENT_ID] [--n-runs N] [--min-frequency 0.8]
```

Algorithm:
1. Load last N QC runs (default 50) for the instrument from SQLite
2. For each run, load `report.parquet` and extract all precursor (seq, charge, RT, Q.Value)
   where Q.Value ≤ 0.01
3. Build a frequency table: for each (seq, charge) pair, count how many runs it appears in
4. Select peptides appearing in ≥ `min_frequency` × N runs
5. For each selected peptide, compute `median(observed_RT)` and `stddev(observed_RT)`
   across all runs
6. Write to `~/.stan/cirt_library.yml`
7. Backfill `cirt_observations` table for all historical runs in the bootstrap set

Output example:
```
stan cirt bootstrap
✓ Loaded 50 QC runs from timsTOF-Ultra-2-0001 (Jan 2026 – Apr 2026)
✓ Found 2,841 recurring precursors
✓ Selected 18 cIRT peptides (≥ 80% frequency, Q.Value ≤ 0.01)
✓ Wrote ~/.stan/cirt_library.yml
✓ Backfilled cirt_observations for 50 runs
  Typical RT variability: median ± 4.3 s, max ± 12.1 s
  Gradient: 21 min, instrument: timsTOF Ultra 2
  Run 'stan cirt bootstrap' again after gradient or column changes.
```

### 5.4 Runtime cIRT extraction (per-run)

After DIA-NN search completes (hook in `stan/watcher/daemon.py`):

```python
# In the post-search hook, after extract_dia_metrics():
from stan.metrics.cirt import extract_cirt_observations, compute_irt_deviation

cirt_obs = extract_cirt_observations(
    report_path=report_parquet_path,
    library_path=get_cirt_library_path(),   # ~/.stan/cirt_library.yml
)
store_cirt_observations(run_id, cirt_obs)

# Recompute irt_max_deviation_min from the new observations
irt_stats = compute_irt_deviation(cirt_obs)
update_run_irt_stats(run_id, irt_stats)   # overwrites the always-0 value
```

`extract_cirt_observations()` returns a list of `CirtObservation` dataclasses. If the
library doesn't exist yet (first-time user), it returns an empty list and logs a warning
suggesting `stan cirt bootstrap`.

---

## 6. TIC extraction and storage

### 6.1 Source priority

| Run type | Vendor | TIC source | Fallback |
|---|---|---|---|
| QC (DIA) | Bruker | Raw MS1 via timsdata | — |
| QC (DIA) | Thermo | Raw MS1 via fisher_py | Hive `report.parquet` identified-TIC (#12) |
| QC (DDA) | Bruker | Raw MS1 via timsdata | — |
| QC (DDA) | Thermo | Raw MS1 via fisher_py | Hive `report.parquet` (Sage output) |
| Sample | Bruker | Raw MS1 via timsdata | — |
| Sample | Thermo | Raw MS1 via fisher_py | None — show "trace unavailable" |
| Blank | Bruker | Raw MS1 via timsdata | — |
| Blank | Thermo | Raw MS1 via fisher_py | None — show "trace unavailable" |

For **Bruker `.d`**: use existing `stan/tools/timsdata/` wrapper. MS1 frames
(MsmsType=0) have per-scan intensities; sum across scans per frame → TIC point.
Reliable — no known failures.

For **Thermo `.raw` (TODO #11 risk)**: `fisher_py SelectInstrument(MS, 1)` fails
silently on some Lumos `.raw` files (known bug, awaiting next Lumos baseline for
stderr diagnostics). Mitigation:
1. Wrap Thermo extraction in a `try/except` — never crash the watcher
2. On failure, attempt the report.parquet fallback (QC runs only — see §6.3 below)
3. If both fail, store `has_tic = false` in the run record and surface a warning
   badge ("⚠ TIC unavailable") in the dashboard panel rather than an empty trace
4. Do not gate QC pass/fail on TIC availability — it's display-only

**Identified-TIC from `report.parquet`** (fallback for Thermo QC runs): reconstruct
a pseudo-TIC by binning DIA-NN `RT` values weighted by `Precursor.Quantity` into
0.1-min bins. This is an identified TIC (only peptide signal, no background), which
is actually useful for seeing the peptide elution profile — label it visually as
"Identified TIC" to distinguish it from a raw MS1 TIC. This is also the Thermo TIC
backfill path for historical runs (#12).

### 6.2 New module: `stan/metrics/tic_store.py`

```python
from pathlib import Path
import json
import numpy as np

def extract_and_store_tic(run_id: str, raw_path: Path, vendor: str, db_conn) -> bool:
    """
    Extract TIC from raw file and store in tic_traces table.
    Returns True on success, False on failure (non-fatal — just means no trace in UI).
    """
    try:
        rt_sec, intensity = _extract_raw_tic(raw_path, vendor)
        rt_sec, intensity = _downsample_lttb(rt_sec, intensity, max_points=3600)
        _store_tic(run_id, rt_sec, intensity, source="raw_ms1", db_conn=db_conn)
        return True
    except Exception as e:
        logger.warning(f"TIC extraction failed for {raw_path}: {e}")
        return False


def _extract_raw_tic(raw_path: Path, vendor: str) -> tuple[np.ndarray, np.ndarray]:
    """Return (rt_seconds, intensity) arrays from raw file."""
    if vendor == "bruker":
        return _extract_bruker_tic(raw_path)
    elif vendor == "thermo":
        return _extract_thermo_tic(raw_path)
    raise ValueError(f"Unknown vendor: {vendor}")


def _extract_bruker_tic(d_path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Sum MS1 frame intensities from Bruker .d via timsdata."""
    from stan.tools.timsdata import timsdata
    td = timsdata.TimsData(str(d_path))
    # Filter frames where MsmsType == 0 (MS1)
    ...


def _extract_thermo_tic(raw_path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Read MS1 TIC from Thermo .raw via fisher_py (existing STAN pattern)."""
    # See stan/metrics/tic.py for existing fisher_py usage pattern
    ...


def _downsample_lttb(rt: np.ndarray, intensity: np.ndarray,
                     max_points: int) -> tuple[np.ndarray, np.ndarray]:
    """
    Largest-Triangle-Three-Buckets downsampling.
    Preserves visual shape (peaks, valleys) much better than uniform subsampling.
    """
    ...


def _store_tic(run_id: str, rt_sec: np.ndarray, intensity: np.ndarray,
               source: str, db_conn) -> None:
    db_conn.execute(
        """INSERT OR REPLACE INTO tic_traces
           (run_id, source, rt_seconds, intensity, n_points, rt_start_sec, rt_end_sec)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (run_id, source,
         json.dumps(rt_sec.tolist()), json.dumps(intensity.tolist()),
         len(rt_sec), float(rt_sec[0]), float(rt_sec[-1]))
    )
```

### 6.3 Thermo TIC backfill for historical runs (TODO #12)

For historical Thermo runs already in SQLite that don't have a `tic_traces` row:

```bash
stan tic backfill --instrument Astral --since 2026-01-01
```

Algorithm:
1. Query `runs` for Thermo runs within date range with no `tic_traces` entry
2. For each run, check if `report.parquet` is still accessible on Hive
3. If yes: build identified-TIC from report.parquet (see §6.1 fallback), label source
   as `"report_identified"` in `tic_traces.source`
4. If no report.parquet: skip, log warning — don't attempt SSH fetch of raw file
   (raw files may have been deleted)
5. Surface a progress bar with counts: `Backfilled 47/89 Thermo runs (42 skipped — no report)`

The backfill runs as a local command (no SLURM). It SSH-fetches report.parquet files
from Hive on demand, same pattern as the existing metric extraction path.

### 6.4 Watcher hook

Add to `stan/watcher/daemon.py` immediately after the raw file is confirmed stable,
before SLURM submission:

```python
# TIC extraction — fire-and-forget, non-blocking
# Runs before search so even non-QC files get a trace
run_id = ingest_run_record(raw_path, instrument)
extract_and_store_tic(run_id, raw_path, vendor=instrument["vendor"], db_conn=conn)
```

This means TIC traces are available in the dashboard immediately (within ~10s of
file stability), well before the DIA-NN search completes.

---

## 7. Run classification (QC / Sample / Blank)

The three facets require classifying each run. Classification is determined in order:

1. **Config override**: `instruments.yml` can specify `run_patterns` with regex patterns
   per class. E.g. `qc_pattern: ".*QC.*|.*HeLa.*"`, `blank_pattern: ".*blank.*|.*BLANK.*"`.
2. **Filename heuristics (default)**:
   - `blank_pattern`: `(?i)(blank|bk|wash|empty|noise)` → Blank
   - `qc_pattern`: `(?i)(qc|hela|standard|std|cal)` → QC
   - Everything else → Sample
3. **Manual override**: operator can reclassify from the dashboard (stored in `runs.run_class`).

New column in `runs` table:
```sql
ALTER TABLE runs ADD COLUMN run_class TEXT DEFAULT NULL;
-- NULL = unclassified (heuristic applied at display time)
-- "qc" | "sample" | "blank" | "other"
```

---

## 8. API surface

New routes mounted in `stan/dashboard/server.py`:

### 8.1 Today's runs TIC overview

```
GET /api/today/tic-overview
```

Query params:
- `date` (optional): ISO date string, defaults to today
- `instrument_id` (optional): filter by instrument

Response:
```json
{
  "date": "2026-04-20",
  "instrument_id": "timsTOF-Ultra-2-0001",
  "runs": [
    {
      "run_id": "abc123",
      "run_name": "2026-04-20_HeLa_QC_01",
      "run_class": "qc",
      "started_at": "2026-04-20T08:14:00Z",
      "gate_result": "pass",
      "grs_score": 87,
      "n_precursors": 19420,
      "time_of_day_rank": 0,      ← 0 = earliest, used for color interpolation
      "has_tic": true,
      "has_cirt": true,
      "tic": {
        "rt_seconds": [0.0, 1.0, 2.0, ...],    ← downsampled to 3600 pts max
        "intensity": [1.2e6, 3.4e6, ...]
      },
      "cirt_markers": [           ← only present for QC runs with cirt data
        {
          "peptide_seq": "LGGNEQVTR",
          "observed_rt_sec": 315.2,
          "expected_rt_sec": 312.4,
          "deviation_sec": 2.8,
          "deviation_class": "green"   ← "green" | "yellow" | "red"
        },
        ...
      ]
    },
    ...
  ],
  "summary": {
    "n_qc": 4, "n_sample": 7, "n_blank": 1, "n_other": 0
  }
}
```

`deviation_class` thresholds (configurable in `thresholds.yml`):
```yaml
cirt_deviation:
  green_max_sec: 30      # < 0.5 min = normal gradient variation
  yellow_max_sec: 90     # 0.5–1.5 min = mild drift, watch
  # > 90 sec = red = investigate
```

### 8.2 Single run TIC (for detail page)

```
GET /api/runs/{run_id}/tic
```

Response: same `tic` + `cirt_markers` structure as above, for a single run.

### 8.3 cIRT library info

```
GET /api/cirt/library
```

Returns the current library version, peptide list, and bootstrap provenance.

---

## 9. Front-end component

### 9.1 Placement

Below the existing QC stats table in the Today's Runs tab. The three panels stack
vertically. Default height: QC panel 200px, Sample 160px, Blank 100px. Each panel
has a collapse toggle (▼/▶).

### 9.2 Color scheme

**Time-of-day gradient** (within each panel):
```javascript
// Earliest run in panel → lightest, latest → darkest
// Use a perceptually uniform color ramp per panel category
const colors = {
  qc:     d3.scaleSequential(d3.interpolateBlues).domain([0, nRuns - 1]),
  sample: d3.scaleSequential(d3.interpolatePurples).domain([0, nRuns - 1]),
  blank:  d3.scaleSequential(d3.interpolateGreys).domain([0, nRuns - 1]),
};
// run.time_of_day_rank used as the color domain input
```

Blues for QC, Purples for Sample, Greys for Blank. Darkest shade is the most recent
run — operators typically care most about "what just ran."

**cIRT marker colors**:
```javascript
const markerColor = {
  green:  "#2da44e",   // passes threshold
  yellow: "#d97706",   // mild drift
  red:    "#cf222e",   // significant drift
};
```

Markers are drawn as small downward-pointing triangles (▼) at the bottom edge of the
QC panel, positioned at `x = observed_rt_sec`. A thin vertical dotted line extends
up from each marker. On hover, a tooltip shows:
```
LGGNEQVTR (2+)
Observed: 315.2 s  (5:15.2)
Expected: 312.4 s  (5:12.4)
Drift: +2.8 s
```

### 9.3 Interactions

| Action | Result |
|---|---|
| Hover over TIC line | Tooltip: run name, time, gate result, GRS |
| Click TIC line | Navigate to run detail page |
| Hover over cIRT marker | Tooltip: peptide, observed/expected RT, drift |
| Click panel collapse toggle | Panel collapses / expands, preference saved to localStorage |
| Click run name in table | Highlights its TIC trace (bring to front, bold) |

### 9.4 Component structure

New React component: `TicOverlayPanel.jsx`

```
TicOverlayPanel
├── PanelHeader (title, run count, collapse toggle)
├── TicChart (SVG canvas via d3)
│   ├── XAxis (RT in minutes)
│   ├── YAxis (intensity, normalized 0–1 or log10)
│   ├── TicLine × n_runs (colored by time_of_day_rank)
│   └── CirtMarkerLayer (QC panel only)
│       └── CirtMarker × n_peptides × n_runs
└── Legend (color ramp: earliest → latest)
```

Y-axis display option: `raw` (absolute intensity) or `normalized` (0–1 per trace).
Normalized is the default — it lets you compare shape regardless of injection amount.
Toggle button in panel header.

---

## 10. Performance

| Target | Rationale |
|---|---|
| `/api/today/tic-overview` response < 500ms for 30 runs | Inline TIC data in response, no N+1 queries |
| TIC extraction < 10s after file stability (Bruker) | Runs before search submission; must not delay queue |
| TIC extraction < 5s after file stability (Thermo) | fisher_py is fast for TIC channel |
| LTTB downsampling < 50ms for 10k-point trace | numpy vectorized |
| Frontend render < 200ms for 30 × 3600-point traces | Canvas fallback if SVG is slow |

The TIC response payload for 30 runs × 3600 pts × 2 arrays (float32) ≈ 1.7MB JSON.
This is acceptable for a local-only dashboard but should be gzip-compressed in the
FastAPI response (`GZipMiddleware` is already available in FastAPI).

---

## 11. Implementation plan

### Phase 0 — Schema + TIC extraction (Days 1–2)

1. Add migration for `tic_traces`, `cirt_observations`, and `column_installs` tables;
   add `run_class` column to `runs`
2. Implement `stan/metrics/tic_store.py`:
   - Bruker MS1 TIC extraction via timsdata
   - Thermo MS1 TIC extraction via fisher_py (port from existing `stan/metrics/tic.py`)
   - LTTB downsampling
   - Storage
3. Wire into watcher daemon (fire-and-forget, pre-search)
4. Implement Thermo fisher_py `try/except` fallback to identified-TIC; surface
   "⚠ TIC unavailable" badge in run record when both paths fail (#11 mitigation)
5. Implement run classification heuristics + `instruments.yml` pattern config
6. Add `stan column install` CLI command + Instruments tab UI for column logging (#18)
7. Test on 3–5 real files (Bruker + Thermo), confirm traces look correct

### Phase 1 — cIRT library (TODO #21) (Days 3–4)

1. Update `stan/metrics/cirt.py`:
   - Remove `DEFAULT_IRT_LIBRARY` (or keep as legacy fallback behind a flag)
   - Add `load_cirt_library()` — reads `~/.stan/cirt_library.yml`
   - Add `extract_cirt_observations()` — queries `report.parquet` for library peptides
   - Rewrite `compute_irt_deviation()` to use observations, return `null` if < 3 matches
2. Implement `stan cirt bootstrap` CLI command
3. Wire `extract_cirt_observations()` into the post-search hook (after DIA-NN completes)
4. Run bootstrap on Brett's historical QC data; confirm the library has ≥ 10 peptides
5. Verify `irt_max_deviation_min` is now non-zero on a recent QC run

Phase 1 can be done in parallel with Phase 0.

### Phase 2 — API (Day 5)

1. Implement `GET /api/today/tic-overview` with efficient SQLite JOIN
   (`runs` + `tic_traces` + `cirt_observations`)
2. Implement `GET /api/runs/{run_id}/tic`
3. Implement `GET /api/cirt/library`
4. Add GZip middleware if not already present
5. Manual test: hit endpoint after running Phase 0/1 extraction on real data

### Phase 3 — Frontend (Days 6–7)

1. `TicOverlayPanel.jsx` with all three facets (QC/Sample/Blank)
2. Time-of-day color gradient (Blues/Purples/Greys via d3)
3. Hover tooltips on TIC lines
4. Click-to-navigate to run detail
5. Normalized vs raw intensity toggle
6. Collapse toggle per panel

### Phase 4 — cIRT markers + column events (Day 8)

1. `CirtMarkerLayer` component in `TicChart`
2. Green/yellow/red triangle markers at observed RT
3. Dotted vertical guide lines
4. Hover tooltip with peptide name, observed/expected RT, drift value
5. Column install annotations in TIC panel header ("Waters HSS T3, installed 5 days ago")
6. Column install event markers on longitudinal trend view (vertical dashed lines)
7. Manual validation on a real day's data: confirm markers fall at visually
   plausible positions on the TIC

### Phase 5 — Thermo TIC backfill + points-across-peak (post-v1)

**Thermo backfill (#12)**:
1. Implement `stan tic backfill` CLI command
2. SSH-fetch `report.parquet` from Hive for historical Thermo runs lacking traces
3. Build identified-TIC, store with `source = "report_identified"`
4. Progress display with skip count for runs without accessible reports

**Points-across-peak Thermo extension (#20)** — **gated on Spectronaut validation**:
1. Port existing Bruker points-across-peak algorithm to Thermo raw file input
   (peak picking from raw MS1 scans via fisher_py, same math as Bruker path)
2. **Validation (mandatory before shipping)**:
   - Run STAN's implementation on a set of real Thermo `.raw` QC files
   - Run the same files through Spectronaut, extract Spectronaut's equivalent
     chromatographic peak quality metric
   - Compare STAN vs Spectronaut values per precursor; confirm correlation
     within the same tolerance used for the Bruker validation
   - Do not merge until Brett signs off on the comparison plot
3. Add points-across-peak to TIC panel as a per-run badge (median value)
4. Add to community benchmark schema for Thermo Track B submissions

---

## 12. Configuration additions

In `config/thresholds.yml`:

```yaml
# cIRT RT deviation thresholds (seconds)
cirt_deviation:
  green_max_sec: 30       # < 30 s = normal
  yellow_max_sec: 90      # 30–90 s = mild drift (warn)
  # > 90 s = red (investigate; does not trigger HOLD by itself)
  min_peptides_for_gate: 3   # fewer than this = no deviation reported
```

In `config/instruments.yml`:

```yaml
instruments:
  - name: "timsTOF Ultra 2"
    ...
    run_patterns:
      qc_pattern: "(?i)(qc|hela|std|standard)"
      blank_pattern: "(?i)(blank|bk|wash|empty)"
      # everything else → sample
```

---

## 13. Open questions

| # | Question | Who decides | Default |
|---|---|---|---|
| 1 | Should the cIRT deviation gate contribute to HOLD, or just color the marker? | Brett | Color only in v1; add to GRS/HOLD in a follow-on once we've seen real data |
| 2 | Y-axis: normalized (0–1 per trace) or log10 absolute intensity as default? | Brett | Normalized default; toggle to absolute in panel header |
| 3 | What regex patterns should be defaults for QC / Blank classification? | Brett (filenames) | `(?i)(qc\|hela\|std)` for QC, `(?i)(blank\|bk\|wash)` for Blank |
| 4 | For the bootstrap, what frequency threshold? | Brett | 80% — enough to be robust but not so strict we miss good peptides |
| 5 | Should Sample + Blank runs get TIC extraction only if no search is run, or always? | Brett | Always — TIC extraction runs before search, regardless of run type |
| 6 | LTTB or uniform subsampling for downsampling? | Technical | LTTB — preserves peak shape; complexity is O(n) |
| 7 | Should cIRT markers link to a "cIRT calibration" detail page, or just tooltips? | Brett | Tooltips only in v1 |
| 8 | If the gradient changes (new column, different length), does the library auto-detect the change? | Brett | No — user must re-run `stan cirt bootstrap`. Log a warning if gradient_min in run config differs from library gradient_min by > 20% |

---

## 14. Done criteria

### Phase 0 — TIC storage + column tracking
- [ ] TIC traces stored in SQLite for all new runs (QC, Sample, Blank) within 10s of
      file stability detection
- [ ] Verified on at least 2 Bruker .d + 2 Thermo .raw files with eyeball confirmation
      that trace shape matches what DataAnalysis / Xcalibur shows
- [ ] Thermo extraction failure handled gracefully: "⚠ TIC unavailable" badge shown,
      watcher does not crash or stall (#11 mitigation)
- [ ] `stan column install` CLI works; column record visible in Instruments tab (#18)

### Phase 1 — cIRT library
- [ ] `stan cirt bootstrap` runs successfully on Brett's instrument data, producing a
      `~/.stan/cirt_library.yml` with ≥ 10 peptides
- [ ] `irt_max_deviation_min` is non-zero (and plausible) on the next QC run
- [ ] `cirt_observations` table populated for both historical (bootstrap) and live runs

### Phase 2 — API
- [ ] `GET /api/today/tic-overview` returns correct faceted data in < 500ms for 30 runs

### Phase 3 — Frontend
- [ ] Three-panel TIC overlay visible in Today's Runs tab
- [ ] Time-of-day color gradient is visually correct (earlier = lighter)
- [ ] Hover tooltip shows run name + gate result
- [ ] Click navigates to run detail

### Phase 4 — cIRT markers + column events
- [ ] Markers appear on QC panel at visually correct RT positions
- [ ] Green / yellow / red coloring matches deviation thresholds
- [ ] Hover tooltip shows peptide, observed vs expected RT, deviation
- [ ] Column model + install age shown as subtitle in QC panel header
- [ ] Brett's eyeball verdict: "I can see the gradient is drifting from this"

### Phase 5 — Thermo backfill + points-across-peak
- [ ] `stan tic backfill` populates historical Thermo runs with identified-TIC traces (#12)
- [ ] Points-across-peak Thermo algorithm implemented AND validated against Spectronaut
      on real Thermo data — comparison plot reviewed and signed off by Brett before merge (#20)
- [ ] Points-across-peak median value displayed as badge in QC panel per run

---

## 15. References

- `stan/metrics/tic.py` — existing TIC extraction (Thermo fisher_py pattern)
- `stan/metrics/chromatography.py` — GRS score, where iRT deviation currently contributes
- `stan/metrics/cirt.py` — current cIRT module with dead DEFAULT_IRT_LIBRARY
- `stan/watcher/daemon.py` — watcher hooks to extend for TIC extraction + cIRT obs
- `stan/dashboard/server.py` — FastAPI app; new routes mount here
- `stan/dashboard/src/` — React components; `TicOverlayPanel.jsx` goes here
- Biognosys iRT paper: Escher et al. 2012, *J. Proteome Res.*, DOI 10.1021/pr300542g
- LTTB algorithm: Steinarsson 2013, *Uni Iceland MSc thesis* (reference implementation
  at github.com/dgodfrey206/lttb, MIT license — translate to numpy in-house)
- Existing STAN TODO list items resolved: **#1**, **#12** (Phase 5), **#18**, **#21**
- Partially addressed: **#11** (mitigation), **#20** (Phase 5, Spectronaut validation required)

---

*Implement Phase 0 and Phase 1 in parallel (they are independent). Phase 2 requires
both. Phase 3 requires Phase 2. Phase 4 requires Phase 1 + Phase 3.*

*The cIRT bootstrap (Phase 1) is the most important standalone fix — it makes
`irt_max_deviation_min` a real metric in GRS scoring immediately, independent of
the UI work.*
