# Evosep PEG Contamination Diagnostic

One-day matrix experiment that isolates the likely PEG sources on an
Evosep timsTOF system: Evotips, solvents, sample-prep plasticware, and
system carryover. Each variable is changed one at a time against a
HeLa QC baseline; STAN scores every run automatically and the PEG
deltas tell you which variable matters.

Runtime: **8 injections on 60 SPD ≈ 8 hours, or 4 injections on 30 SPD
≈ 6 hours.** Run overnight; review STAN scores in the morning.

---

## Prerequisites

- [ ] STAN v0.2.168+ running on the timsTOF (`stan doctor` should show
      alphatims 1.0.8 + numpy 1.x)
- [ ] Fresh HeLa 50 ng aliquots (enough for 5 injections)
- [ ] **Two lots of Evotips** — your currently-installed lot and one
      other (older box, newer box, or from Evosep directly as a "lot
      comparison kit" — email `support@evosep.com` if needed)
- [ ] **Glass-bottled LC-MS grade solvents** from a different vendor
      than what's currently in the Evosep (Fisher Optima or Honeywell
      Burdick-and-Jackson are standard references). Buffer A: 0.1%
      formic acid in water. Buffer B: 0.1% formic acid in acetonitrile.
- [ ] Clean glass digest vials (e.g., Thermo autosampler vials,
      Waters LCMS vials) for one of the controls
- [ ] Log book / spreadsheet for lot numbers, timestamps, and STAN scores

---

## Sample prep

Prepare your HeLa 50 ng stock once, split into aliquots. Every
injection uses the same stock — the only thing changing is **what
plastic/solvent the stock passes through** between the vial and the
column.

- `HeLa-A` — 4 aliquots, normal prep in your usual plastic tubes
- `HeLa-G` — 1 aliquot, transferred via glass pipette to glass autosampler vial

---

## Injection matrix

| # | Injection | Evotip lot | Solvents | Sample vial | Purpose |
|---|-----------|------------|----------|-------------|---------|
| 1 | **Water blank** | Current lot | Current Evosep | — | Baseline: what PEG does the Evosep+current-lot tip add to nothing? |
| 2 | **HeLa-A** | Current lot | Current Evosep | Plastic (normal) | Baseline: your standard QC. PEG score here = your usual contamination level. |
| 3 | **Water blank** | **Other lot** | Current Evosep | — | If this is lower than #1: tip lot is contributing PEG. |
| 4 | **HeLa-A** | **Other lot** | Current Evosep | Plastic (normal) | Compare with #2. Lower = tip lot matters. |
| 5 | **Water blank** | Current lot | **Glass-bottled** | — | If lower than #1: solvents/bottles are contributing. |
| 6 | **HeLa-A** | Current lot | **Glass-bottled** | Plastic (normal) | Compare with #2. Lower = solvents/bottles matter. |
| 7 | **HeLa-A** (pre-washed tip) | Current lot, **2 extra MeOH washes** | Current Evosep | Plastic (normal) | Compare with #2. Lower = tip needs conditioning. |
| 8 | **HeLa-G** | Current lot | Current Evosep | **Glass vial** | Compare with #2. Lower = sample-side plastic is contributing. |

**For each injection, log:**
- Timestamp
- Evotip lot number (look on the box label)
- Current solvent bottle lot number (if present)
- Any deviations from the standard protocol

---

## Reading the results in STAN

Once all 8 injections finish and STAN processes them, open the
dashboard and check **PEG score** (shown as a numeric 0-100 badge since
v0.2.168) for each run. Then decode:

| Comparison | Low # vs High # | Interpretation |
|---|---|---|
| **1 vs 3** (blank on two tip lots) | #3 < #1 by >15 | Tip lot is a significant PEG source |
| **1 vs 5** (blank on two solvent batches) | #5 < #1 by >15 | Solvents are a significant PEG source |
| **2 vs 4** (HeLa on two tip lots) | Confirms #1 vs #3 | Tip contribution is real in sample path too |
| **2 vs 6** (HeLa on two solvent batches) | Confirms #1 vs #5 | Solvent contribution is real in sample path |
| **2 vs 7** (standard vs extra-washed tip) | #7 < #2 by >15 | Tip conditioning strips leachable PEG — add extra MeOH wash to SOP |
| **2 vs 8** (HeLa via plastic vs glass vial) | #8 < #2 by >15 | Pipette tips / tubes in sample prep are contributing |
| **All the blanks high** | #1, #3, #5 all >40 | Column + in-line plastics (pump, tubing, fittings) are the source — needs Evosep service call |

**If nothing moves significantly,** PEG is likely coming from upstream:
- Mobile phase solvent impurities
- Column itself (ask Evosep if others have seen the same lot)
- Sample handling before Evotip loading

**If everything moves together,** your reference HeLa stock is
contaminated — new HeLa prep from scratch.

---

## Escalation

If the matrix points at **tip lot**: email Evosep support with the two
lot numbers + STAN PEG scores. They track lot-specific contamination
complaints.

If the matrix points at **solvents**: switch your standard buffers to
the glass-bottled vendor permanently. ~$40/bottle is cheap insurance.

If the matrix points at **sample prep plastic**: audit your tip/tube
brand. LC-MS grade (Eppendorf LoBind, Rainin BioClean) is usually OK;
commodity polypropylene is not.

If everything points at **system carryover / column**: schedule an
Evosep service visit and ask for a full plumbing inspection + column
replacement. Document your STAN scores to support the request.

---

## Optional extension: time-series after intervention

Once you've identified the culprit and fixed it, run your normal daily
HeLa QC for a week and watch the PEG score on the Trends tab. Should
settle to <20 within 3-5 injections if the source is truly gone. If
not, there's a second contributor you missed — repeat the matrix.

---

## Protocol version

- **v1.0** (2026-04-22) — initial writeup
- Based on: Rardin 2018 Skyline panel, HowDirty 2024 coherence check,
  Allumiqs PEG contamination guide
