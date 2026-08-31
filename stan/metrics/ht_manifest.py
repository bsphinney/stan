"""Answer "which files are this submission?" for an external search tool.

STAN is the only thing that knows a submission's true extent: which trays it
occupies, that a tray whose filenames never mention 0793 is still part of it,
which wells are blanks, which are HeLa standards, and which samples came out
badly enough to be worth re-running.

That is the whole contribution here. The searching itself belongs to the
Core's proteomics-pipeline skill, which already derives parameters from the
data type, pins the engine version, batches the SLURM submission and deposits
finished searches into FRAN's drop box. Rebuilding any of that inside STAN
would mean maintaining two of everything and having them disagree.

So this hands over a file list and gets out of the way.
"""

from __future__ import annotations

import logging
import re

from stan.metrics.ht_outliers import (
    expand_submission_runs,
    find_outliers,
    parse_injection,
    parse_well,
)

logger = logging.getLogger(__name__)

#: Wells that are not customer material. Same vocabulary `stan submit-all`
#: uses, so "not a sample" means one thing across STAN.
_NON_SAMPLE = re.compile(r"(?i)(wash|blank|blnk|blk|DELETE)")

#: A HeLa standard dropped into the plate as an in-queue control.
_STANDARD = re.compile(r"(?i)(hela|hel50|he50)")

#: What a caller can ask for.
INCLUDE_CHOICES = ("samples", "rerun", "standards", "all")


def classify_run(run_name: str, kind: str | None = None) -> str:
    """One of: sample, blank, standard.

    `kind` comes from which table the row was read out of -- rows from `runs`
    are QC searches, i.e. standards -- and is trusted over the filename when
    present, because a lab can name a HeLa anything.
    """
    name = str(run_name or "")
    if kind == "qc":
        return "standard"
    if _NON_SAMPLE.search(name):
        return "blank"
    if _STANDARD.search(name):
        return "standard"
    return "sample"


def build_manifest(
    query: str,
    health_rows: list[dict],
    qc_rows: list[dict] | None = None,
    include: str = "samples",
) -> dict:
    """Files belonging to a submission, ready to hand to a search tool.

    Args:
        query: submission number or code, e.g. "0793".
        health_rows: sample_health rows.
        qc_rows: runs rows (the HeLa standards).
        include: "samples" (default -- customer material only), "rerun"
            (only what the outlier check flagged), "standards", or "all".

    Blanks and standards are excluded by default deliberately. They are QC,
    not the customer's samples, and FRAN's corpus counts searches as customer
    work -- ingesting a plate's washes would inflate it.
    """
    if include not in INCLUDE_CHOICES:
        raise ValueError(f"include must be one of {INCLUDE_CHOICES}")

    samples = [dict(r) for r in expand_submission_runs(query, health_rows)]
    standards = [dict(r) for r in expand_submission_runs(query, qc_rows or [])]
    for r in standards:
        r["kind"] = "qc"

    # Flag outliers per tray, exactly as the dashboard does, so "rerun" here
    # and the Needs re-run list on screen can never disagree.
    flagged: set[str] = set()
    by_plate: dict[str, list[dict]] = {}
    for r in samples:
        w = parse_well(r.get("run_name"))
        by_plate.setdefault(w["plate"] if w else "", []).append(r)
    for plate_rows in by_plate.values():
        for r in find_outliers(plate_rows)["needs_rerun"]:
            if r.get("run_name"):
                flagged.add(r["run_name"])

    entries: list[dict] = []
    for r in samples + standards:
        name = str(r.get("run_name") or "")
        cls = classify_run(name, r.get("kind"))
        w = parse_well(name)
        entries.append({
            "run_name": name,
            "raw_path": r.get("raw_path"),
            "class": cls,
            "plate": (w or {}).get("plate"),
            "well": f"{w['row']}{w['col']}" if w else None,
            "injection": parse_injection(name),
            "needs_rerun": name in flagged,
            "verdict": r.get("verdict"),
        })
    entries.sort(key=lambda e: (e["injection"] is None, e["injection"] or 0))

    if include == "samples":
        chosen = [e for e in entries if e["class"] == "sample"]
    elif include == "rerun":
        chosen = [e for e in entries if e["needs_rerun"]]
    elif include == "standards":
        chosen = [e for e in entries if e["class"] == "standard"]
    else:
        chosen = entries

    # A path STAN never resolved is worse than useless to a search tool: it
    # would search a subset and report success. Surfaced, not silently dropped.
    missing = [e["run_name"] for e in chosen if not e["raw_path"]]
    files = [e["raw_path"] for e in chosen if e["raw_path"]]

    counts: dict[str, int] = {}
    for e in entries:
        counts[e["class"]] = counts.get(e["class"], 0) + 1

    plates = sorted({e["plate"] for e in entries if e["plate"]})
    return {
        "submission": query,
        "include": include,
        "files": files,
        "n_files": len(files),
        "entries": chosen,
        "plates": plates,
        "counts": counts,
        "n_needs_rerun": sum(1 for e in entries if e["needs_rerun"]),
        "missing_paths": missing,
        "total_in_submission": len(entries),
    }
