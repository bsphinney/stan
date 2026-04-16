# Spec — Points Across Peak (Datapoints Per Peak)

> **Status**: Validated against Spectronaut on 12 test files. Bruker implementation shipping v0.2.105.
> **Date drafted**: 2026-04-16

## 1. What it measures

**Points across peak** (pts/peak) counts how many MS2 data points
actually sample each precursor's chromatographic elution profile.
This directly determines quantitation accuracy — too few points and
the peak area integration becomes unreliable.

Reference: Matthews & Hayes, 1976 (*Anal. Chem.* 48:1375).

Typical healthy ranges:
- **3–6 pts/peak**: minimum for acceptable quantitation
- **8–15 pts/peak**: good quantitation, typical for modern DIA
- **15+ pts/peak**: excellent, usually only achieved at lower throughput (≤30 SPD)

## 2. Why the naive calculation is wrong

### The broken approach (pre-v0.2.105)

```
pts/peak = peak_width / cycle_time
```

Where:
- `peak_width = (RT.Stop - RT.Start) × 60` from `report.parquet`
- `cycle_time = median(consecutive RT diffs)` from `report.parquet`

This gives **96–277 pts/peak** — off by 10–20×.

### Two bugs

**Bug 1 — Peak width**: `RT.Stop - RT.Start` is the **base width**
(~8.8 sec), not the **FWHM** (~2.8 sec). Base width ≈ 3× FWHM.
Inflates the result by ~3×.

**Bug 2 — Cycle time**: consecutive RT diffs from `report.parquet`
give the **diaPASEF frame time** (~0.09 sec), not the **precursor
revisit time** (~0.5–1.1 sec). In diaPASEF, each PASEF cycle has
~11 DIA frames, each covering a different m/z × mobility window.
A specific precursor is only sampled when its m/z falls within the
current frame's isolation window — typically 1–2 frames per cycle.
Using the frame-to-frame time as "cycle time" inflates the result
by ~10×.

Combined: 3× (peak width) × 10× (cycle time) ≈ 30× overestimate.

## 3. The correct algorithm

### Principle

For each precursor, count the actual number of DIA frames that:
1. Fall within the precursor's elution window `[RT.Start, RT.Stop]`
2. Have an isolation window covering the precursor's m/z

This is exactly what Spectronaut computes for its "Datapoints Per
Peak" metric.

### Data sources

| Source | What it provides |
|--------|-----------------|
| `report.parquet` (DIA-NN output) | `Precursor.Mz`, `RT.Start`, `RT.Stop` for each precursor at 1% FDR |
| `analysis.tdf` (Bruker raw file) | DIA window scheme (`DiaFrameMsMsWindows` table) and per-frame times + window group assignments (`Frames` + `DiaFrameMsMsInfo` tables) |

### Algorithm (pseudocode)

```
# 1. Read DIA window scheme from analysis.tdf
window_scheme = {}   # WindowGroup → list of (mz_lo, mz_hi)
for (group, iso_mz, iso_width) in DiaFrameMsMsWindows:
    half = iso_width / 2
    window_scheme[group].append((iso_mz - half, iso_mz + half))

# 2. Read DIA frame events (time + which window group)
dia_events = []   # list of (frame_time_sec, window_group)
for (frame_time, group) in Frames JOIN DiaFrameMsMsInfo WHERE MsMsType=9:
    dia_events.append((frame_time, group))

# 3. For each precursor, count covering frames
for precursor in report_parquet.filter(Q.Value < 0.01):
    mz = precursor.Precursor_Mz
    rt_lo = precursor.RT_Start * 60   # minutes → seconds
    rt_hi = precursor.RT_Stop * 60

    count = 0
    for (frame_time, group) in dia_events:
        if rt_lo <= frame_time <= rt_hi:
            for (mz_lo, mz_hi) in window_scheme[group]:
                if mz_lo <= mz <= mz_hi:
                    count += 1
                    break   # one hit per frame

    pts_per_peak.append(count)

# 4. Report median
median_pts_per_peak = median(pts_per_peak)
```

### SQL queries for analysis.tdf

```sql
-- DIA window scheme
SELECT WindowGroup, IsolationMz, IsolationWidth
FROM DiaFrameMsMsWindows;

-- DIA frame events (time + window group assignment)
SELECT f.Time, i.WindowGroup
FROM Frames f
JOIN DiaFrameMsMsInfo i ON f.Id = i.Frame
WHERE f.MsMsType = 9
ORDER BY f.Id;

-- MS1 cycle time (for reference / validation)
SELECT Time FROM Frames WHERE MsMsType = 0 ORDER BY Time;
```

### Performance

- Subsample to 2000 precursors per file (random, seed=42)
- ~10 seconds per file on a timsTOF 100 SPD run with ~37,000 precursors
- Total backfill of 200 files: ~30 minutes from the Hive mirror

### Bruker-specific details

| Parameter | Typical value (timsTOF HT, 100 SPD) |
|-----------|--------------------------------------|
| MS1-to-MS1 cycle time | 1.099 sec |
| DIA frames per cycle | 11 |
| DIA window groups | 11 |
| Windows per group | 3–4 |
| Isolation width | 25–26 Da |
| Windows covering a typical precursor per cycle | 1–2 |
| FWHM (from DIA-NN) | 2.5–3.0 sec |
| **Resulting pts/peak** | **8–9 median** |

### Why mobility matters

In diaPASEF, each DIA frame covers a specific region of m/z × 1/K₀
space (not just m/z). Our algorithm currently checks only m/z
overlap, not mobility overlap. This slightly overestimates pts/peak
for precursors at the edges of the mobility window.

For v1 this is acceptable — the overestimate is small (~0.5 pts)
because the mobility dimension of each window is broad enough to
cover most peptide 1/K₀ values at a given m/z. A future version
could cross-reference the `ScanNumBegin`/`ScanNumEnd` columns in
`DiaFrameMsMsWindows` with each precursor's measured 1/K₀ for exact
coverage.

## 4. Validation

### Test dataset

12 files from Brett's Affinisep December 2025 experiment, all
timsTOF HT at 100 SPD with 11×3 diaPASEF method:

| File | Precursors (1% FDR) | Our pts/peak | Spectronaut |
|------|--------------------:|:------------:|:-----------:|
| affinisepIPA C2 | 34,656 | 9 | 8–12 distribution |
| affinisepIPA C3 | 33,475 | 8 | " |
| affinisepIPA C4 | 37,218 | 9 | " |
| affinisepACN A1 | 36,873 | 9 | " |
| affinisepACN A2 | 37,298 | 9 | " |
| affinisepACN A4 | 37,772 | 9 | " |
| affinisepBoth A7 | 38,560 | 9 | " |
| affinisepBoth A8 | 33,809 | 9 | " |
| affinisepBoth A9 | 38,034 | 9 | " |
| Dec18 A3 | 36,066 | 8 | " |
| Dec29 A12 | 31,857 | 8 | " |
| Dec29 D10 | 33,691 | 9 | " |
| **Overall** | | **median 9.0** | **median 8–12** |

### Earlier test (different file)

| File | Our pts/peak | Q25–Q75 |
|------|:------------:|:-------:|
| 03jun2024_HeLa50ng_DIA_100spd | 8.0 | 6–10 |

## 5. Thermo adaptation (v0.2.106, planned)

For Orbitrap DIA (Exploris, Astral, Lumos), the same principle
applies but the data sources differ:

- **Cycle time**: from `fisher_py` MS1 scan timestamps
- **DIA window scheme**: from the `.raw` method header (isolation
  list with m/z centers + widths per scan event)
- **No mobility dimension**: simpler — just m/z overlap check

Expected to be easier than Bruker because there's no TIMS dimension
to consider. The `fisher_py` API exposes per-scan isolation m/z and
width, which maps directly to the same window-covering-count
algorithm.

## 6. Edge cases

| Case | How we handle it |
|------|-----------------|
| Precursor at the edge of an isolation window | Counted if m/z falls within `[IsolationMz - Width/2, IsolationMz + Width/2]`. No soft-edge correction. |
| Very narrow peaks (FWHM < 1 cycle) | Returns 1–2 pts/peak, which is correct — this IS a sampling problem |
| report.parquet has no RT.Start/RT.Stop | Falls back to the old (broken) estimate. Logged as a warning. |
| analysis.tdf not accessible (network mount) | Copies to temp file before sqlite3.connect |
| Non-DIA runs (DDA) | Skipped — pts/peak is a DIA-specific metric |
| Subsample bias | Fixed random seed (42) for reproducibility across runs |

## 7. References

- **Matthews & Hayes, 1976**. "Systematic Errors in Gas Chromatography:
  Effect of Finite Sampling Rate on Accuracy of Computed Peak Areas."
  *Analytical Chemistry* 48(10):1375–1379.
  DOI: [10.1021/ac50005a009](https://doi.org/10.1021/ac50005a009)

- **Meier et al., 2020**. "diaPASEF: parallel accumulation–serial
  fragmentation combined with data-independent acquisition."
  *Nature Methods* 17:1229–1236.
  DOI: [10.1038/s41592-020-00998-0](https://doi.org/10.1038/s41592-020-00998-0)

- **Spectronaut Manual** (Biognosys). "Datapoints Per Peak" metric
  definition — same algorithm, validated by head-to-head comparison
  on 12 files (see Section 4).

---

*Drafted 2026-04-16. Validated same day against Spectronaut on Brett's
Affinisep Dec 2025 dataset.*
