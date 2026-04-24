# WIP: stan_sc (Single-Cell Proteomics) module

**Status: paused.** An autonomous agent began implementing this on 2026-04-23
against `SPEC_stan_sc_module.md` in the STAN Brainstorming folder, but Brett
stopped the run — the agent had picked the wrong spec for that session
(he wanted `SPEC_front_page_view_selector.md` instead). The SC module is
still a real roadmap item and this doc captures exactly what was built so
the next attempt doesn't duplicate work.

## Where the work is preserved

Everything the agent produced is in `/tmp/claude/stan_sc_wip/` on Brett's
Mac (the parking lot, outside the git tree):

```
/tmp/claude/stan_sc_wip/
├── stan_sc_plan.md              # Agent's up-front implementation plan
├── stan_sc_scaffolding/         # New stan/sc/ module directory
│   ├── __init__.py
│   ├── api.py
│   ├── baseline.py
│   ├── carryover.py
│   ├── cellenone_import.py
│   ├── cli.py
│   ├── events.py
│   ├── fasta_registry.py
│   ├── gates.py
│   ├── metrics.py
│   ├── plate.py
│   ├── routing.py
│   └── schema.py
├── db.py.patch                  # Agent's edits to stan/db.py
├── cli.py.patch                 # Agent's edits to stan/cli.py
├── server.py.patch              # Agent's edits to stan/dashboard/server.py
├── test_sc_api.py
├── test_sc_baseline.py
├── test_sc_carryover.py
├── test_sc_cellenone.py
├── test_sc_fasta.py
├── test_sc_metrics.py
├── test_sc_plate.py
└── test_sc_routing.py
```

`/tmp/claude/` is volatile on macOS. **Before rebooting, move the contents
to somewhere durable** (e.g. `~/Documents/STAN_SC_WIP/` or a git branch).

## What the agent got done before being stopped

From the agent's visible output + partial file inventory:

- Wrote an up-front plan at `stan_sc_plan.md`
- Created 13 new files under `stan/sc/` (api, baseline, carryover, cellenone
  import, cli, events, fasta_registry, gates, metrics, plate, routing,
  schema, plus `__init__.py`)
- Added an 8-line migration block to `stan/db.py` (plate + well tables,
  details preserved in `db.py.patch`)
- Modified `stan/cli.py` to register a new `stan sc ...` subcommand group
- Modified `stan/dashboard/server.py` to add SCP API endpoints
- Wrote 8 test files under `tests/test_sc_*.py`
- **Did not finish:** the agent's last visible step was refactoring a
  DB-connection concurrency issue in `assess_blank` (inside
  `stan/sc/carryover.py`). It was re-opening a separate SQLite connection
  for `compute_threshold` while the outer connection was still held.
  **Treat the scaffolding as incomplete and not import-safe yet** — do
  not `from stan.sc import api` or run the tests until someone finishes
  this refactor.
- **Did not write a final report** at `/tmp/claude/stan_sc_implementation_report.md`,
  so there is no agent-authored summary of which spec items are finished
  vs outstanding. Audit against `SPEC_stan_sc_module.md` when resuming.

## Resuming the work

When we're ready to pick this up again:

1. Move `/tmp/claude/stan_sc_wip/` somewhere durable.
2. Spawn a fresh agent against `SPEC_stan_sc_module.md` with this doc in
   its prompt so it knows what's already scaffolded.
3. Have the agent restore the scaffolding into `stan/sc/` + apply the
   three `.patch` files + move the tests back.
4. First task for the resume agent: fix the `assess_blank` /
   `compute_threshold` connection-holding bug the first agent was
   halfway through.
5. Then audit against the spec and finish the remaining items.

## Why this was the wrong spec for that session

Brett intended to hand off `SPEC_front_page_view_selector.md` — a simpler
UI-level spec about the homepage view-selector — and instead got
`SPEC_stan_sc_module.md`, which is a multi-week v0.3.0 roadmap item. The
mismatch was caught after ~25 minutes of agent work.
