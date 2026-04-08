# IPS — Instrument Performance Score

**Module:** `stan/metrics/chromatography.py`
**Calibration set:** 359 real UC Davis HeLa QC runs (timsTOF HT, Exploris 480, Orbitrap Lumos), April 2026
**Range:** 0–100 integer, higher is better
**Version:** v2 (cohort-calibrated) — replaces the v1 composite that is retained only in git history

---

## TL;DR

IPS is a single 0–100 number that answers the question *"how well did my
instrument perform compared to other runs on the same instrument family at
the same throughput?"*

- **60** = you matched the median run on your instrument class
- **90** = you matched the top 10% of runs on your instrument class
- **100** = you significantly outperformed the top 10%
- **<30** = you underperformed the bottom 10% — something is wrong

It uses only three inputs that STAN reliably measures today —
`n_precursors`, `n_peptides`, `n_proteins` — plus two cohort keys
(`instrument_family`, `spd`). Every component is calibrated against the
real observed distribution in our 359-run reference cohort, so scores are
meaningful and directly comparable across instruments.

---

## Why this design

### What was wrong with v1

The previous IPS combined five components:
`n_precursors`, `median_fragments_per_precursor`, `median_points_across_peak`,
`pct_fragments_quantified`, and `missed_cleavage_rate`.
On paper this is reasonable. In practice, four of those five fields are not
populated by the current extractors — they come out as `0` or `None` on
almost every run, and the scoring code defaulted missing components to
constants (`0.5`, `0.9`, `0.8`, etc.).

When we recomputed v1 IPS against the 359-run seed cohort, the result was
useless:

| Cohort | v1 range | v1 median | top-10 vs bottom-10 spread |
|---|---|---|---|
| Exploris 480 | 46–65 | 58 | 2 points |
| Lumos | 50–58 | 58 | 5 points |
| timsTOF HT | 41–76 | 58 | 5 points |

50–60% of the score was literally constant. A near-empty run and an
exceptional run got nearly identical scores. Correlation with `n_precursors`
was only 0.54–0.63 *within* a cohort because the constants dominated.

### What v2 does instead

v2 uses **only metrics we actually have data for** and calibrates the
score against **real distributions** rather than aspirational reference
values:

| Metric | Nonzero in 359-run seed | Range (timsTOF HT fast, n=74) |
|---|---|---|
| `n_precursors` | 352 / 359 | 32,305 → 48,757 (p10→p90) |
| `n_peptides` | 359 / 359 | 28,864 → 43,578 |
| `n_proteins` | 359 / 359 | 4,300 → 5,104 |
| everything else | 0 / 359 | — |

After the redesign on the same 359 runs:

| Cohort | v2 range | v2 median | top-10 vs bottom-10 spread |
|---|---|---|---|
| Exploris 480 | 11 – 91 | 59 | 60 points |
| Orbitrap Lumos | 24 – 95 | 60 | 57 points |
| timsTOF HT | 12 – 91 | 59 | 71 points |

Std dev went from ~4 to 21.5 across the full cohort. **The metric actually
discriminates now.**

---

## How the score is computed

### 1. Pick the cohort reference

The score is always relative to a reference distribution defined by
`(instrument_family, spd_bucket)`. SPD buckets are coarse throughput
classes:

| bucket | samples per day | typical methods |
|---|---|---|
| `deep`    | ≤ 15 | ≥60 min gradient, deep-proteome discovery |
| `medium`  | 16–40 | standard 20–40 min gradients |
| `fast`    | 41–80 | Evosep 60 SPD territory |
| `ultra`   | > 80  | Evosep 100+ SPD, short-gradient high-throughput |

Every cohort in `IPS_REFERENCES` carries three percentile anchors
(`p10`, `p50`, `p90`) for each of the three metrics, derived directly from
our 359 real HeLa QC runs. For example, `("timsTOF", "fast")` (n=74):

```python
precursors = (32305, 42778, 48757)   # p10, p50, p90
peptides   = (28864, 38195, 43578)
proteins   = (4300,  4768,  5104)
```

**Fallback order** when a cohort has no reference entry:
1. `(family, spd_bucket)` — first choice
2. `(family, "*")` — family-wide, any SPD (used e.g. for Orbitrap `fast` which
   isn't represented in our seed)
3. `_GLOBAL_REFERENCE` — pooled across all 352 DIA runs, used only when the
   instrument family is `None` or unknown

### 2. Score each component

Each of the three metrics is mapped to a 0–100 component score by
piecewise-linear interpolation against its cohort anchors:

```
anchors:  0 → 0
         p10 → 30
         p50 → 60
         p90 → 90
       1.5·p90 → 100   (asymptotic cap)
```

In code (`_component_score`):

```python
if value <= p10:
    return 30 * (value / p10)                       # 0-30
if value <= p50:
    return 30 + 30 * (value - p10) / (p50 - p10)    # 30-60
if value <= p90:
    return 60 + 30 * (value - p50) / (p90 - p50)    # 60-90
# above p90, asymptotic to 100 at 1.5 × p90
excess = min((value - p90) / (0.5 * p90), 1.0)
return 90 + 10 * excess                             # 90-100
```

This yields intuitive anchors:
- a run **at** cohort median scores exactly **60**
- a run **at** cohort p90 scores exactly **90**
- a run at 50% above p90 saturates at **100**
- a run at zero scores exactly **0**

### 3. Combine with fixed weights

```
IPS = 0.50 · s_precursors + 0.30 · s_peptides + 0.20 · s_proteins
```

Precursors get the highest weight because they are the primary depth
metric for DIA (per STAN's metric hierarchy — see `STAN_MASTER_SPEC.md`).
Peptides are secondary depth. Proteins are weighted lowest because protein
count is confounded by FASTA choice and inference settings. Result is
clamped to `[0, 100]` and rounded to an integer.

### DDA variant

`compute_ips_dda` follows the same structure but substitutes an **absolute**
reference for PSM count because we don't yet have enough DDA seed data for
cohort calibration:

```
PSM anchors: p10=20k, p50=60k, p90=100k
Peptides + proteins: same cohort references as DIA
Weights: 50% PSMs + 30% peptides + 20% proteins
```

These PSM anchors will be replaced with real cohort references once STAN
has accumulated enough DDA community submissions.

---

## Usage

### From extractors (watcher daemon)

`stan/watcher/daemon.py` injects the cohort keys from instrument config
before computing IPS:

```python
metrics = extract_dia_metrics(str(result_path))
metrics["instrument_family"] = self._config.get("family")
metrics["spd"]               = self._config.get("spd")
metrics["ips_score"]         = compute_ips_dia(metrics)
```

If `instrument_family` or `spd` is missing the scorer gracefully falls back
through the hierarchy described above; it will never raise.

### From the HF Space relay (`app.py`)

The relay applies `compute_ips()` server-side to every community
submission so IPS is computed consistently regardless of client version.
The relay uses a standalone copy of the formula (no STAN dependency) but
must be kept in sync with this module. When IPS references change, update
**both** `stan/metrics/chromatography.py:IPS_REFERENCES` and the
corresponding block in `app.py`.

### Reading a score

| Score | Meaning |
|---|---|
| 90–100 | Excellent — top-decile performance for your instrument class |
| 70–89  | Above median — strong run |
| 55–69  | Around median — typical healthy run |
| 40–54  | Below median — nothing catastrophic but something has drifted |
| 25–39  | Poor — investigate column, LC, source, calibration |
| 0–24   | Bad — do not run samples until fixed |

A **single** low score is not a crisis; a **trend** of falling scores is
the signal to act on. The dashboard shows a 10-run rolling median alongside
the instantaneous value.

---

## Recalibration

The reference percentiles are baked into `IPS_REFERENCES` as literal Python
values so STAN works offline and scores are deterministic. They should be
refreshed when the cohort grows meaningfully (~25% more runs) or when a new
instrument family is added. The rebuild procedure:

1. Export current community submissions:
   `stan export --format parquet -o cohort.parquet`
2. Regenerate per-cohort p10/p50/p90 with the snippet in
   `scripts/rebuild_ips_references.py` (TODO — checked in alongside this
   module)
3. Paste the new `Reference(...)` entries into
   `IPS_REFERENCES` in `stan/metrics/chromatography.py`
4. Bump the version comment at the top of the file to the current date
5. Run `pytest tests/test_metrics.py` — the anchor tests will catch
   arithmetic regressions
6. Mirror the same values into the relay's `app.py`

Small cohorts (n < 10) should **not** be used as standalone references —
fold them into the family-wide (`"*"`) fallback until they grow.

---

## Known limitations

1. **Depth-only metric.** IPS v2 measures how many precursors/peptides/
   proteins you identified, not whether the chromatography was good, the
   calibration was tight, or the sampling rate was adequate. Those
   dimensions will return to IPS once STAN's extractors actually populate
   `median_fragments_per_precursor`, `median_points_across_peak`,
   `median_cv_precursor`, and `missed_cleavage_rate`.

2. **Cohort coverage is UC Davis-heavy.** The April 2026 reference set is
   entirely from one lab, which means the "median" reflects UCD's LC
   setup, column choice, and sample prep. As the community benchmark grows
   we should recalibrate against the broader population so "60" means
   "global median", not "UCD median".

3. **No temporal component.** IPS is per-run. Instrument health trends are
   shown separately on the dashboard as a rolling median.

4. **Cross-vendor comparison is still fraught.** timsTOF HT typically
   scores higher than Exploris 480 on absolute precursor count at matched
   throughput, which is why the metric is cohort-relative — you compare
   an Exploris run against other Exploris runs, not against a timsTOF.
   Direct cross-family IPS comparison is valid for *trend stability* (is
   each instrument holding its own cohort median?) but not for
   *instrument ranking*.

---

## Changelog

- **2026-04-05 — v2** Rebuilt from scratch using only measured fields.
  Cohort-calibrated against 359 UCD HeLa runs. Piecewise-linear scoring
  against p10/p50/p90 anchors. Median-run ≡ 60 by construction.
- **2026-03 — v1** Five-component absolute-reference formula. Deprecated
  after discovery that 4/5 components were always unpopulated, making the
  score ~60% constant.
