# QC gating and the Slack run summary

**Modules:** `stan/alerts.py`, `stan/gating/evaluator.py`, `stan/pipeline/hive_process.py`
**Introduced:** v1.0.83 (summary), v1.0.84 (IPS colouring)
**Related:** [`ips_metric.md`](ips_metric.md), [`PG_FARM_ACCESS.md`](PG_FARM_ACCESS.md)

---

## Read this first: `stan/gating/` does not do anything

`evaluate_gates()` looks like the authority on whether a run passed QC. **It is
not.** It has never failed a run in production, and it cannot.

`stan/gating/evaluator.py:96`:

```python
if not merged:
    logger.warning("No thresholds found for model=%s, mode=%s", ...)
    return GateDecision(result=GateResult.PASS, metrics=metrics)
```

`merged` comes from `load_thresholds()`, which reads `thresholds.yml`.
**That file does not exist and never has.** It is not in the repo, there is no
template for it, it is not gitignored, and nothing in the tree writes one. So
`load_thresholds()` returns `{}`, `merged` is empty, and every run
short-circuits to PASS before a single metric is compared. The
threshold-comparison code below that branch — which would correctly fail
`0 < min` — is unreachable.

Measured against live PG on 2026-09-04:

| query | result |
|---|---|
| `SELECT gate_result, COUNT(*) FROM runs GROUP BY gate_result` | `('pass', 4540)` — one group |
| `SELECT COUNT(*) FROM runs WHERE n_precursors = 0 OR n_precursors IS NULL` | **398** |

There is no `warn` and no `fail` anywhere in the table, and 398 runs that
identified nothing are recorded as having passed.

**Do not key any signal off `gate_result`.** It is a constant. This is what
v1.0.83 got wrong — the Slack icon was coloured from `GateDecision.result` and
was therefore green on every run, including a dead one.

Reviving the subsystem would mean populating `thresholds.yml`, and that is not
as simple as picking numbers: the same Lumos ranges from 31k precursors on a
35 min gradient to 53k on a 120 min one, so a flat per-`(model, mode)` minimum
either misses real failures on long gradients or false-alarms on short ones.
Threshold keys parse as `<metric>_min` / `<metric>_max`
(`_parse_threshold_key`), with structure `{model: {mode: {key: value}}}` plus a
`default` model key. Gradient length is not in that key today.

**The QC surface people actually read is the front-page gauges, driven by IPS.**

## IPS is the quality number

IPS is cohort-calibrated from the lab's own accumulated baseline results — see
[`ips_metric.md`](ips_metric.md) for the calibration set and component weights.
It is computed by `compute_ips_dia` / `compute_ips_dda` in
`stan/metrics/chromatography.py`, scored against `IPS_REFERENCES_DDA` /
the DIA equivalent.

Crucially, **IPS reported these failures correctly all along.** The four dead
Lumos runs on 2026-09-04 scored IPS 0. The measurement worked; only the gate
ignored it.

### Colour bands — one definition, two consumers

The bands come from `IpsBadge` in `stan/dashboard/public/index.html`:

| IPS | colour |
|---|---|
| `>= 80` | green |
| `>= 60` | yellow |
| `< 60` | red |
| `null` | neutral — unknown, not bad |

`stan/alerts.py` mirrors these as `_IPS_GREEN` / `_IPS_YELLOW`. **If you change
one, change the other**, or Slack and the front page will disagree about the
same run. Anything new that needs to judge run quality should read `ips_score`
and use these bands rather than inventing its own.

---

## The Slack run summary

One line per run: what was identified, plus anything that flagged.

```
🟢 *Orbitrap Fusion Lumos* `FL030926_HeL50_120m_2.raw` — 53,084 PSMs · 5,430 proteins · IPS 85
🟡 *timsTOF-10878* `20260904_hela_qc_02.d` — 21,044 precursors · 4,310 proteins · IPS 61  ⚠️ PEG moderate (63), mass calibration (72% <5 ppm)
🔴 *Orbitrap Fusion Lumos* `FL030926_HeL50_90m_3.raw` — IPS 0  ⚠️ no identifications
```

### Where it is called, and why there

`send_qc_summary()` is called from `process_raw()` in
`stan/pipeline/hive_process.py`, **after `_run_peg_and_drift`** — not beside
`evaluate_gates`.

This placement is load-bearing. On the Hive path PEG is computed *after*
`insert_run`, so a call at gate-evaluation time would report every run as
PEG-free. `_run_peg_and_drift` returns its figures so the caller can fold them
into `metrics`; it previously returned `None` on three of its four paths, so it
now returns a dict on every path and the caller guards regardless.

### What flags

- **PEG** from `peg_score >= 50` (moderate) only. `classify_peg_score` calls
  20–50 "trace", which is the normal background of shared plasticware and would
  fire on nearly every run. PEG is not a gate — it is folded into `metrics` by
  the caller.
- **No identifications** on an explicit zero in `n_precursors` / `n_psms`.
  Guarded on the key being *present*, not merely falsy, so monitor-pipeline runs
  that were never searched are not mislabelled dead.
- **Gate flags** from `decision.failed_gates` / `warned_gates` — read off the
  decision rather than re-compared here, so two places cannot drift about what
  is out of spec. In practice these lists are always empty; see above.

### Configuration

| item | value |
|---|---|
| Enabled by default | yes |
| Disable | `STAN_SLACK_QC_SUMMARY=0` (also `false`/`no`/`off`) |
| Webhook resolution | `$STAN_SLACK_WEBHOOK` → `community.yml:slack_webhook_url` → `~/.stan/slack_webhook` |
| Webhook in production | resolves from `/home/brettsp/.stan/community.yml` |

`STAN_SLACK_WEBHOOK` is **not** set on Hive and does not need to be — the value
comes from `community.yml` via `load_community()`. Verified resolvable both
under a bare `env -i` (the cron-equivalent environment) and inside a SLURM job
on a compute node, which matters because `send_qc_summary` fires from
`process_raw` on compute nodes rather than on the login node.

### Failure behaviour

It never raises. Formatting errors are caught and logged at DEBUG, the POST is
fire-and-forget on a daemon thread, and a missing webhook is a silent no-op
returning `False`. A QC run must not fail because Slack is unreachable — the
tradeoff is that delivery success is unobservable.

---

## Open items

- **398 runs with zero or null precursors** are recorded as `pass`. Four are
  from one Lumos batch on 2026-09-04 — both 90 min repeats and both
  `HeL50-UnvPe` 60 min runs — while `90m_1` on the same plate returned 38,883.
  Whatever killed those injections is undiagnosed. New occurrences now show red
  in Slack; the historical rows have not been revisited.
- **`thresholds.yml` / `stan/gating/`** is inert as described above. Either
  populate it (with gradient length in the key) or retire the subsystem, but do
  not leave it looking authoritative.
