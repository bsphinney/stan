"""Server-side STAN pipeline modules.

`hive_process` runs the full QC pipeline (search → extract → 4DFF → TIC →
PEG/drift → IPS → gates → DB write) inside a single Hive SLURM job, against
a raw file already on Quobyte. The instrument-PC watcher's `_store_run`
remains the canonical client-side path for now; the two share leaf modules
(extractor, features, tic, db, gating) but orchestrate independently.

Future cleanup: extract a shared `pipeline.core` once both paths have run
in production for a few weeks and we know which differences are real vs
historical drift.
"""
