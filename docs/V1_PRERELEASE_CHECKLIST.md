# STAN v1.0 Pre-Release Checklist

**Codebase state at checklist creation:** v0.2.312, 350/350 tests passing,
13 audit blockers from the v0.2.305-312 sprint resolved.

Work the items in order — they're sequenced so an earlier step can't
invalidate a later one. Each section ends with a yes/no gate that has
to clear before moving on.

---

## 1. Code state — verify what's already landed

- [ ] `git log --oneline v0.2.305..v0.2.312` shows 8 audit-fix commits
- [ ] `pytest tests/ -k "not integration" -x` exits clean (350/350)
- [ ] `git status` is clean on `main` (no uncommitted local changes)
- [ ] No open PRs against the v1.0 release branch
- [ ] CHANGELOG entry drafted for v1.0 covering the audit fixes

**Gate:** all 5 above pass before touching any instrument PC.

---

## 2. Per-instrument verification (do at each PC)

Touch each of the three lab PCs in turn (Lumos, timsTOF HT, Exploris 480).

### 2.1 Force-update to v1.0 candidate

- [ ] Refresh `stan.bat` on the desktop (one-time bootstrap; v0.2.302's self-update isn't on disk yet for the older PCs)
- [ ] Double-click `stan.bat`. Confirm it self-updates (banner says "stan.bat refreshed — relaunching") and pip-installs to v0.2.312
- [ ] `stan version` reports v0.2.312 in a fresh terminal
- [ ] `stan doctor` runs clean, including the **TRFP** section showing `--help exit code: 0` and binary size in the 7-15 MB range

### 2.2 Dispatch + sync sanity

- [ ] Acquire one HeLa50 QC (or trigger one from `~/STAN/test_files/` if the operator has staged samples)
- [ ] Within 5 min of `acquisition_complete`, the run lands in `runs` table (not just `sample_health`) — confirm via `sqlite3 stan.db "SELECT run_name, run_date FROM runs ORDER BY run_date DESC LIMIT 1"`
- [ ] `dispatch_attempts` table has a fresh `status='ok'` row for the file
- [ ] `~/STAN/logs/watch_*.log` shows no `TRFP mode-detect returned exit 2147516556` lines
- [ ] Hive mirror's `<host>/status.json` shows `stan_version: 0.2.312` and a fresh `last_run_any` timestamp matching the QC

### 2.3 Bruker-only checks (timsTOF HT)

- [ ] `.features` sidecar appears next to the new `.d` within 5 min of acquisition (proves inline 4DFF in the watcher is firing)
- [ ] Dashboard's ion cloud renders the **Plotly per-charge view** (not the SVG fallback) for the new run
- [ ] `tic_traces.bp_intensity` is non-NULL on the new row — confirms BPC capture works at ingest

### 2.4 Thermo-only checks (Lumos + Exploris)

- [ ] Mode-detect classifies the new file as DIA / DDA / orbitrap correctly (no `defaulting to DIA` warning unless TRFP genuinely failed)
- [ ] `ms2_analyzer` column on the new row is `OT` or `IT` (not empty)
- [ ] No `"Could not detect acquisition mode"` warnings in the watch log on Thermo runs

**Gate:** every PC passes 2.1–2.4 before touching the relay or the dataset.

---

## 3. Relay schema verification

The v0.2.310 change drops `run_name` (sends empty string). The relay
must accept that without 422.

- [ ] One-row test: pick a known-good QC, manually trigger `stan submit-all --force --limit 1` from one PC against staging or production relay
- [ ] Confirm relay returns 200 with a `submission_id`
- [ ] Pull the resulting parquet from `huggingface.co/datasets/brettsp/stan-benchmark/submissions/<id>.parquet` and verify `run_name == ""`
- [ ] One-row test for a **patch-bumped DIA-NN** version (2.3.1 instead of 2.3.0): manually edit one row's `diann_version` column in stan.db, re-submit, confirm relay accepts with `assets_verified=False` (and not 422)

**Gate:** if either relay test 422s, fix relay-side schema before continuing.

---

## 4. Data wipe + repopulate

Per Brett 2026-05-05: wipe before announcing 1.0.

- [ ] Snapshot existing dataset state for postmortem: `git clone https://huggingface.co/datasets/brettsp/stan-benchmark /tmp/pre-v1-snapshot` (already in the dataset's `pre-v1-snapshot/` directory per the file listing)
- [ ] Run `python -m stan.community.scripts.wipe_v1 --backup` from a Hive node with HF write access
- [ ] Run `--wipe`, then `--init-empty`. Confirm `submissions/` directory is empty on the dataset
- [ ] From each instrument PC: `stan submit-all --force` to repopulate
- [ ] Confirm post-repopulate row count matches the snapshot's row count (or is intentionally smaller — runs filtered out by the new exact-match DIA-NN gate or other v1.0 validators)
- [ ] Spot-check 5 random submission parquets on HF for: `run_name == ""`, `assets_verified == True`, `cohort_id` populated, `it_params_tuned` field present

**Gate:** repopulated dataset count > 0 AND `run_name=""` on every row.

---

## 5. Documentation

- [ ] `README.md`: bump version banner to v1.0, update Quick Start to point at `stan.bat` (not `start_stan.bat`), drop or update the "Implementation Status" table
- [ ] `STAN_MASTER_SPEC.md`: stamp "v1.0 frozen" date at the top, remove stale GRS / paramiko / Percolator references the audit flagged
- [ ] `docs/user_guide.md`: any "(planned)" markers removed for features now shipping
- [ ] `docs/V1_RUNBOOK.md`: cross-link to this checklist
- [ ] **Add a privacy paragraph** to `user_guide.md` and the relay submit page: *"The community dataset publishes aggregate metrics. STAN strips raw filenames before submit. If your QC filename contained patient identifiers, only the local stan.db retains them — the public dataset never sees them."*
- [ ] Cut a `CHANGELOG.md` for v1.0 referencing the v0.2.305-312 audit fixes

---

## 6. Final QA (after wipe + repopulate)

- [ ] HF Space dashboard at https://brettsp-stan.hf.space loads without errors
- [ ] Leaderboard cards render for the three lab instruments (timsTOF HT, Lumos, Exploris 480)
- [ ] Local dashboard at http://localhost:8421 loads on every instrument PC
- [ ] Local dashboard shows the **TIC | BPC** toggle on the timsTOF (proves `bp_intensity` repopulated post-wipe)
- [ ] Local dashboard's "This Week's TIC overlay" shows current QCs (not stuck on April rows)
- [ ] CSRF test: open https://example.com in a tab, paste `fetch('http://localhost:8421/api/fleet/command',{method:'POST',body:'{}',headers:{'Content-Type':'application/json'}})` in DevTools console — confirm 403 (audit fix #4 working)
- [ ] CLI test: `curl -X POST http://localhost:8421/api/instruments -d '{}' -H 'Content-Type: application/json'` from a venv terminal — confirm not 403 (missing-Origin allowed for CLI clients)

**Gate:** every QA item green.

---

## 7. The cut

- [ ] Final pre-tag commit bumps version to `1.0.0` in both `pyproject.toml` and `stan/__init__.py`
- [ ] `git tag -a v1.0.0 -m "STAN 1.0.0 — first public release"`
- [ ] `git push origin main && git push origin v1.0.0`
- [ ] GitHub Release page created from the tag, body = the v1.0 CHANGELOG entry
- [ ] Pin the release commit on the HF Space (or sync the Space's `app.py` if relay-side changes are part of v1.0)
- [ ] Trigger the `consolidate_benchmark.yml` GitHub Action manually so `benchmark_latest.parquet` rebuilds against the post-wipe submissions

---

## 8. Announce

- [ ] Slack `#proteomics` (or wherever the lab hangs)
- [ ] Email the half-dozen external labs that have submitted (the relay's `display_name` table is your contact list)
- [ ] Update the lab's website / proteomics core page with the v1.0 link
- [ ] Tweet from the lab account (Twitter/Bluesky) with screenshots of the leaderboard
- [ ] (Optional) Submit a "tools update" note to the proteomics community channels — Cell Press' MCP, Proteomics, ProteomeXchange announcements

---

## Out of scope for 1.0 (deferred to 1.1)

The audit flagged these but agents and Brett agreed they don't block 1.0:

- `mode_uncertain` UI flag for ddaPASEF runs that fall through the DIA fallback (TODO marker landed in v0.2.308; full impl needs DB migration + dashboard badge)
- Supply-chain SHA pinning on `stan.bat` self-update (security agent recommended deferring; campus-MITM exposure is non-zero but real-world risk is low for a research tool)
- `stan retry-failed` CLI to walk `dispatch_attempts` and re-dispatch (table is now populated; manual SQL works for v1.0 ops)
- Lumos OT-OT vs OT-IT cohort split on the leaderboard (`ms2_analyzer` and `it_params_tuned` are stamped on every submission from v0.2.294+; just needs a relay schema deploy + dashboard cards)
- Lumos DDA cohort references — empty seed; needs new data, not code
- Arcade high-scores → community site (memory note `project_arcade_to_community.md` captures the design)
- Rebuilding IPS references against the canonical scoring vocabulary (v0.2.306 mapped old buckets to new keys but didn't recompute reference values from the seed data)

---

## Rollback plan

If something goes sideways during the cut:

- Worst case: revert the v1.0 tag, force-push `main` to the last green commit, re-pin instrument PCs to v0.2.304 (the pre-spd-bucket-rekey version) via a queued `update_stan` command. Total recovery time: ~30 min if all three instruments are healthy and reachable.
- The post-wipe dataset can be restored from `pre-v1-snapshot/` in the HF dataset repo.
