# Spec — Multi-Charge Feature Detector for Bruker timsTOF

> **Status**: Planning. Not yet implemented.
> **Owner**: Brett (decisions), Claude (implementation)
> **Target version**: v0.2.104 (after v0.2.103 ships TIC-ratio carry-over)
> **Estimated effort**: 2–4 days
> **Date drafted**: 2026-04-15

## 1. Goal

Detect multi-charge isotope envelopes (charge ≥ 2) in Bruker `.d` files
without relying on any third-party feature finder. The output is used as
the **honest carry-over signal** in STAN's blank-monitoring pipeline:

> A 2+/3+ envelope at a given RT and 1/K₀ is essentially a peptide
> fingerprint. Counting these in a blank gives a peptide-specific
> residual measure that TIC AUC cannot, and that singly-charged
> contaminants cannot inflate.

**Specifically**: given a Bruker `.d`, return a count of features with
charge ≥ 2, plus optional per-feature detail (m/z, RT, 1/K₀, charge,
n_isotopes, summed intensity).

## 2. Why not use an existing tool

Investigation summary (see chat transcript 2026-04-15):

| Option | Verdict |
|---|---|
| Bruker DataAnalysis / PaSER `LcTimsMsFeature` table | Available only if Bruker's commercial software ran first; can't depend on it |
| AlphaPept | Bundles `uff-cmdline2.exe` (Bruker proprietary, 65 MB Windows binary, separate EULA). AlphaPept's own pure-Python detector only handles Thermo/mzML, not Bruker. Maintenance-mode (last commit Apr 2024) |
| OpenMS `FeatureFinderMultiplex` | C++, BSD-3, industrial-grade but heavy build dependency on Windows |
| Dinosaur (Käll lab) | Java, Apache-2.0, Orbitrap-only, no mobility |
| **Roll our own (this spec)** | Pure Python + numpy/numba, ~300 lines for the narrow "count z ≥ 2" use case, no external binaries, easy to ship in pip wheel |

The narrow use case (carry-over count, not quant-grade features) is what
makes a small in-house implementation reasonable. We are explicitly **not**
trying to replace AlphaPept or DIA-NN's feature finder.

## 3. Scope

### In scope (v1)

- Read MS1 frames via existing `stan/tools/timsdata/` wrapper
- Build "hills" (centroids connected across consecutive frames within
  m/z + mobility tolerance)
- Cluster hills into isotope envelopes by `Δm/z = 1.00335 / z` spacing
- Assign charge from spacing, require ≥ 3 isotopologues at consistent
  spacing for a confident assignment
- Apply minimum intensity / S/N threshold
- Return count of envelopes with charge ≥ 2, plus optional per-feature
  detail
- Cross-validation command (`stan verify-features`) against DIA-NN
  `report.parquet` precursor count on a known HeLa run

### Deferred to later

- Full quant (XIC, isotope-cosine scoring, averagine fit)
- MS2 / fragment-level processing
- Thermo support (carry-over feature is Bruker-only at first)
- Adduct / in-source fragment handling
- Hill-splitting at intensity minima (use simple "always extend" for v1)

### Explicitly out of scope

- Replacing DIA-NN, AlphaPept, or any other identification engine
- Quantitative comparison across runs (we just count)

## 4. Algorithm

Adapted from Meier et al. 2018 (DOI 10.1021/acs.jproteome.8b00523) and
2020 (DOI 10.1038/s41592-020-00998-0, the 4D-FF paper). Mobility
co-elution is the key constraint that makes PASEF feature detection
cleaner than Orbitrap.

### Steps

1. **Per-frame peak picking (already done)** — `timsdata` exposes
   centroided `(scan, mz_index, intensity)` tuples per MS1 frame.
   Convert `mz_index` → m/z via `indexToMz`, `scan` → 1/K₀ via
   `scanNumToOneOverK0`.

2. **Hill construction**:
   - For each centroid in frame N, find the nearest centroid in frame
     N+1 with `|Δm/z| / m/z < 15 ppm` AND `|Δscan| ≤ 1`.
   - Connect them into a hill. Hills end when no continuation found.
   - Drop hills shorter than 3 frames (noise).
   - Compute per-hill: apex RT, apex 1/K₀, monoisotopic m/z (weighted
     mean over apex frames), summed intensity, FWHM in mobility.

3. **Intensity / S/N filter**:
   - Per-frame baseline = median of bottom 10 % of intensities.
   - Drop hills whose apex intensity is < 5 × baseline.

4. **Isotope envelope clustering**:
   - For each hill H:
     - For each candidate charge `z ∈ {2, 3, 4, 5, 6}`:
       - Look for co-eluting hills at `m/z = H.mz + k × 1.00335 / z`
         for `k ∈ {1, 2, 3, 4, 5}` within 10 ppm.
       - "Co-eluting" = same apex frame ± 1 AND same 1/K₀ bin ± 0.005.
       - Count matches.
     - Pick the `z` that yields the highest count.
     - If best count ≥ 3, call this an envelope with charge `z`.
   - Mark all hills in the envelope as consumed (don't double-count
     M+1 as its own monoisotopic).

5. **Output**: list of features with `(monoisotopic_mz, rt_apex_sec,
   ook0_apex, charge, n_isotopes, summed_intensity)`.

6. **Carry-over count**: `n_features_z2plus = sum(1 for f in features
   if f.charge >= 2)`.

### Critical edge cases (must handle in v1)

| Case | Mitigation |
|---|---|
| M+0 below detection but M+1 / M+2 visible | Search backward for missing M-1 within ppm + mobility tolerance |
| Charge ambiguity with only 2 isotopologues | Require ≥ 3 peaks before assigning charge — log "ambiguous" otherwise |
| Mass calibration drift on uncalibrated runs | Use ppm not Da throughout; consider dynamic recalibration via lock-mass if available |
| High mass (M > 1800 Da) where M+1 > M+0 | Handle in monoisotopic-pick: if second-tallest is at expected M+1 spacing and taller than first, re-pick first as mono |
| Singly-charged contaminant noise | This is in our favour — 1+ noise is the bulk of singletons we discard. But still apply min intensity filter |
| Mobility overlap with chimeric ions | Mobility tolerance 0.005 1/K₀ (PASEF resolution ~0.01) — tight enough to separate most chimeras |

## 5. API surface

New module: `stan/metrics/feature_finder.py`

```python
from dataclasses import dataclass

@dataclass
class Feature:
    monoisotopic_mz: float
    rt_apex_sec: float
    ook0_apex: float
    charge: int           # 1 if not assigned (singleton hill)
    n_isotopes: int       # how many co-eluting peaks confirmed the envelope
    summed_intensity: float

def find_features(d_path: str | Path,
                  ms_level: int = 1,
                  min_charge_for_count: int = 2,
                  mz_ppm: float = 10.0,
                  ook0_tol: float = 0.005,
                  min_intensity_snr: float = 5.0,
                  ) -> list[Feature]:
    """Detect multi-charge isotope envelopes in a Bruker .d.

    Pure Python + numpy. Requires `stan/tools/timsdata/` to be
    installed (Bruker DLL). Returns empty list on failure.

    For carry-over use, callers typically only need:
        n = sum(1 for f in find_features(d_path) if f.charge >= 2)
    """

def count_multicharge(d_path: str | Path) -> int:
    """Convenience wrapper — returns just the integer count of
    features with charge >= 2. Used by the carry-over panel."""
```

Integration with carry-over module (separate spec, future):
- `stan/metrics/carryover.py` calls `count_multicharge()` on each blank
- Compares to a per-instrument rolling baseline of multi-charge counts
  from clean reference HeLa runs
- Stores `(blank_run_id, n_z2plus, n_z2plus_per_50ng_hela_ratio,
  verdict)` in a new `carryover` SQLite table

## 6. Performance targets

| Target | Rationale |
|---|---|
| < 90 s per 1 h timsTOF QC run (~10k MS1 frames) | Acceptable for monitor cadence (1 file/hour) |
| < 30 s with `numba @njit` on inner loops | Stretch goal for batch reprocessing |
| Peak memory < 4 GB on a 1 h run | Leaves headroom for parallel watcher activity |
| Cross-validation: count within 0.5×–2× of DIA-NN precursor count on a clean HeLa | Sanity bound; outside this range = miscalibration |

Bruker's `uff-cmdline2.exe` takes 2–10 minutes on the same files; we
should beat that with vectorized numpy (sort by m/z + binary-search
neighbours).

## 7. Validation strategy

### Unit tests

- Synthetic input: a fake `.d` directory with hand-crafted Frames table
  and known isotope envelopes. Assert correct charge assignment.
- Edge case: 2+ envelope with M+0 missing → still detected via M+1
  back-search.
- Edge case: high-mass envelope (M > 2500) → mono pick is correct.

### Integration tests

- Run on a known HeLa QC `.d` from Brett's timsTOF.
- Assert: n_features_z2plus is within 0.5×–2× of DIA-NN's precursor
  count from the same run's `report.parquet`.

### Cross-validation CLI

```bash
stan verify-features /path/to/known_hela.d
```

Output:
- Our count
- DIA-NN precursor count (from `report.parquet` if present)
- Ratio
- PASS / WARN / FAIL based on whether ratio is in [0.5, 2.0]

If any instrument starts reporting outside that range, run
`verify-features` to confirm the detector is still calibrated.

## 8. Implementation plan

### Files to create

| File | Purpose | Est. LOC |
|---|---|---|
| `stan/metrics/feature_finder.py` | The detector | ~300 |
| `tests/test_feature_finder.py` | Unit + integration tests | ~150 |
| `stan/cli.py` (additions) | New `stan verify-features` command | ~30 |
| `docs/USER_feature_finder.md` | User-facing doc explaining what the count means + when to run verify | ~50 |

### Implementation order

1. **Hill construction + plotting helpers** — get a working hill
   builder, eyeball some real data on a known HeLa to confirm hills
   look right. Do this BEFORE adding charge logic.
2. **Isotope envelope clustering** — add the spacing search, charge
   assignment.
3. **`count_multicharge` wrapper + cross-validation against DIA-NN**.
4. **`stan verify-features` CLI**.
5. **Numba speedup** if performance is below target.

Each step ends with a manual sanity check on a real file. Don't write
all 300 lines blind.

### Dependencies

Already in STAN's pyproject.toml:
- `numpy`
- `polars` (for reading DIA-NN `report.parquet` in cross-validation)

New (optional):
- `numba` (only if Phase 5 speedup needed)

No changes to `stan/tools/timsdata/` — that wrapper already exposes
`readScans`, `mzToIndex`, `indexToMz`, `scanNumToOneOverK0`. See
`stan/metrics/ion_detail.py` (peptide wizard's fork) for usage examples.

## 9. Open questions

| # | Question | Who decides | Default if unanswered |
|---|---|---|---|
| 1 | Should `count_multicharge()` cache results per (path, mtime) like `scan_cache`? | Brett | Yes — same fingerprint pattern |
| 2 | Required minimum hill length (frames) before considering for envelope? | Empirical, validate on real data | 3 frames |
| 3 | Should we surface per-RT-bin count (so the carry-over panel can show "carry-over peaked at RT 35 min")? | Brett (UI question) | Yes — return list of Features, let dashboard bin |
| 4 | Should we attempt to handle DDA frames (MsmsType=8) too, or MS1 only? | Brett | MS1 only for v1 |
| 5 | Is averagine scoring needed in v1 to filter contaminants? | Empirical | Skip in v1, add only if false positives are a problem |
| 6 | Adduct handling (e.g. Na+, NH4+ adducts) | Brett | Skip in v1 — these are usually 1+ anyway |

## 10. References

- **Meier et al. 2018** — original PASEF paper. *J. Proteome Research.*
  DOI: [10.1021/acs.jproteome.8b00523](https://doi.org/10.1021/acs.jproteome.8b00523)
- **Meier et al. 2020** — diaPASEF + 4D feature finder description.
  *Nature Methods.*
  DOI: [10.1038/s41592-020-00998-0](https://doi.org/10.1038/s41592-020-00998-0)
- **AlphaPept source** — github.com/MannLabs/alphapept — `feature_finding.py`
  for the pure-Python (Thermo/mzML) algorithm. Apache-2.0. Don't copy
  the Bruker path (it's just a wrapper around `uff-cmdline2.exe`).
- **Dinosaur** — github.com/fickludd/dinosaur — Java reference for
  Orbitrap isotope envelope detection. Apache-2.0. Algorithm
  documented in Teleman et al. 2016.
- **OpenMS** `FeatureFinderMultiplex` — github.com/OpenMS/OpenMS — C++,
  BSD-3, industrial reference. Hard to read but correct.
- **Existing STAN code to read** before starting:
  - `stan/tools/timsdata/timsdata.py` — DLL wrapper API
  - `stan/metrics/ion_detail.py` (peptide wizard's fork at
    [MKrawitzky/Nats](https://github.com/MKrawitzky/Nats/blob/main/stan/metrics/ion_detail.py))
    — example of using `readScans` + `mzToIndex` for per-frame work.
    Apache-2.0 / MIT compatible per fork license.

## 11. Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Silent miscalibration on instruments we haven't tested | Medium | High | `stan verify-features` cross-check against DIA-NN |
| Slow on high-density runs (>20k frames) | Low | Medium | `numba @njit` Phase 5 |
| False positives from chemical noise | Low | Low | Min intensity filter; require ≥3 isotopologues |
| Authors of Meier 2020 algorithm find issues with our implementation | Low | Low | We're not publishing — it's a QC count, not a quant claim |
| Bruker changes `analysis.tdf` schema | Low | High | Already a risk for ALL of STAN; pin tested DLL versions |

## 12. Done criteria

- [ ] `stan.metrics.feature_finder.find_features()` returns a non-empty
  list on at least one Brett-confirmed timsTOF HeLa file
- [ ] Cross-validation ratio (vs DIA-NN precursors) is within [0.5, 2.0]
  on three different Brett-confirmed HeLa files
- [ ] Runtime is under 90 s on a 1 h timsTOF QC run on Brett's hardware
- [ ] `stan verify-features` CLI is documented in `docs/user_guide.md`
- [ ] Carry-over panel (separate spec, v0.2.103+) wires this in via
  `count_multicharge()`

---

*Ship the v0.2.103 TIC-ratio carry-over panel first. Build this when
that's in production for at least a week and we know the dashboard +
table layout we want to wire it into.*
