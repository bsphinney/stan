#!/usr/bin/env python3
"""Re-resolve the Bruker panel's gradient/method chart, in Python.

WHY THIS EXISTS. `extract.sql` parsed the throughput out of the filename with
its own regex, `_([0-9]{1,4}[sS][pP][dD])`, which demands `60spd` contiguous.
A third of this facility's filenames spell it `60-spd-dia`, so they matched
nothing and fell into `other/unspecified` — 11,164 runs, the largest bar on
the chart, bigger than 60 SPD and 100 SPD combined.

That was a second, drifted copy of a regex STAN already owns. This stage
deletes the copy: resolution goes through `stan.metrics.scoring
._spd_from_method_string`, the same function the search pipeline uses, so a
fix in one place fixes both. It is also why `1000spd` disappears — the shared
function lists only real Evosep throughputs, and `_1000spd` is a filename typo
for 100 (confirmed against the instrument's own method XML), so those runs
stop being a phantom cohort.

CONTROL RUNS ARE A SEPARATE AXIS FROM GRADIENT. A wash has no gradient in any
meaningful sense; a BLANK does — it runs a real gradient, the filename just
does not say which. So `is_control` describes what the sample was, and never
swallows the throughput. Conflating the two is what made "other/unspecified"
look like one bucket when it was three different things.

Reads and rewrites the extract JSON in place (or to --out). Safe to run on a
document with no `method_names` key: it does nothing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

#: Both archives holding Bruker `.d`. Flinders is the working tree; `to-hive`
#: is the older mass-spec archive and reaches back to 2021. 10,307 basenames
#: appear in BOTH, so the preference between them must be deterministic or the
#: same acquisition resolves differently on different runs for no visible
#: reason.
ARCHIVE_ROOTS = ("/nfs/lssc0/flinders/proteomics/Data",
                 "/quobyte/proteomics-grp/to-hive")

#: Where the basename -> path index is cached, and how long it stays fresh.
#: Walking both archives takes minutes; the XML reads are ~22 ms each.
INDEX_CACHE = "/quobyte/proteomics-grp/STAN/evosep/dpath_index.tsv"
INDEX_MAX_AGE_H = 24.0


def build_index(roots=ARCHIVE_ROOTS, cache=INDEX_CACHE,
                max_age_h=INDEX_MAX_AGE_H, rebuild=False) -> dict:
    """basename -> [paths], across both archives. Cached; rebuilt when stale.

    Every path for a basename is kept, ordered by root. The choice between
    duplicates is made later and lazily: whichever copy actually carries a
    `<N>.m/` method directory answers, because that is the one that can be
    read. Deciding at index time would need a stat per candidate across ~35k
    names for no gain.
    """
    fresh = (not rebuild and os.path.exists(cache)
             and (time.time() - os.path.getmtime(cache)) / 3600.0 < max_age_h)
    idx: dict[str, list[str]] = {}
    if fresh:
        with open(cache) as fh:
            for line in fh:
                b, _, path = line.rstrip("\n").partition("\t")
                if b and path:
                    idx.setdefault(b, []).append(path)
        return idx
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, _ in os.walk(root):
            keep = []
            for d in dirnames:
                if d.endswith(".d"):
                    idx.setdefault(d, []).append(os.path.join(dirpath, d))
                else:
                    keep.append(d)
            dirnames[:] = keep
    try:
        tmp = cache + ".tmp"
        with open(tmp, "w") as fh:
            for b, paths in idx.items():
                for path in paths:
                    fh.write(f"{b}\t{path}\n")
        os.replace(tmp, cache)
    except OSError:
        pass
    return idx


def spd_from_xml(paths: list[str]):
    """Read the Bruker method XML, trying each copy of the acquisition.

    Returns (spd, path) or (None, path_tried). A copy without a `<N>.m/`
    simply yields nothing and the next is tried, which is the whole reason
    both archives are indexed rather than one.
    """
    from stan.metrics.scoring import _bruker_spd_from_xml
    last = None
    for path in paths:
        last = path
        try:
            spd = _bruker_spd_from_xml(path)
        except Exception:
            spd = None
        if spd:
            return spd, path
    return None, last

#: Washes and system procedures — no gradient in any meaningful sense.
WASH_RE = re.compile(r"(?i)(^|[_\-])(wash|prime|clean|flush|equilibrat)")
#: Blanks and standards — a real gradient, just not named in the filename.
BLANK_RE = re.compile(r"(?i)(^|[_\-])(blank|blnk|nist|hela|hel-?\d|qc)")


def resolve(doc: dict, use_xml: bool = True, index: dict | None = None) -> dict:
    rows = doc.get("method_names")
    if not rows:
        return {"applied": False, "reason": "no method_names in the document"}

    from stan.metrics.scoring import _spd_from_method_string

    if use_xml and index is None:
        index = build_index()
    index = index or {}

    buckets: dict[str, dict] = {}
    # FOUR outcomes, not two. "We could not tell" and "the file is gone" are
    # different facts: the first is a bug to chase, the second is a permanent
    # limit. Lumping them is what made the original 11,164 bar meaningless.
    src = {"metadata": 0, "filename": 0, "file_unavailable": 0, "unresolved": 0}
    disagreements = []
    n_control = n_wash = 0
    for r in rows:
        fname = r.get("fname") or ""
        count = r.get("count", 0)
        from_name = _spd_from_method_string(fname)
        paths = index.get(fname) or []
        from_xml, tried = (spd_from_xml(paths) if (use_xml and paths) else (None, None))

        if from_xml:
            spd, how = from_xml, "metadata"
            # The XML wins, always: a filename is what a person typed, the
            # method record is what the instrument did. ~0.5 % of runs carry a
            # filename asserting a throughput that was never run, and those sit
            # in the wrong cohort in every comparison anyone has made on them.
            if from_name and from_name != from_xml:
                disagreements.append({"fname": fname, "filename_said": from_name,
                                      "xml_said": from_xml, "count": count,
                                      "path": tried})
        elif from_name:
            spd, how = from_name, "filename"
        elif not paths:
            spd, how = None, "file_unavailable"
        else:
            spd, how = None, "unresolved"
        src[how] += count

        is_wash = bool(WASH_RE.search(fname))
        if spd:
            key = f"{spd}spd"
        elif is_wash:
            key = "wash / system"
        elif how == "file_unavailable":
            key = "unknown — raw file no longer on disk"
        else:
            key = "unknown gradient"
        if is_wash:
            n_wash += count
        elif BLANK_RE.search(fname):
            n_control += count
        b = buckets.setdefault(key, {"method": key, "count": 0, "done": 0,
                                     "failed": 0, "is_control": False})
        b["count"] += count
        b["done"] += r.get("done", 0)
        b["failed"] += r.get("failed", 0)
    if "wash / system" in buckets:
        buckets["wash / system"]["is_control"] = True

    doc["methods"] = sorted(buckets.values(), key=lambda b: -b["count"])
    doc["method_resolution"] = {
        "resolved_by": src,
        "n_control_samples": n_control,
        "n_wash_procedures": n_wash,
        "n_disagreements": len(disagreements),
        "disagreements": sorted(disagreements, key=lambda d: -d["count"])[:200],
        "archives_indexed": list(ARCHIVE_ROOTS) if use_xml else [],
        "n_indexed_basenames": len(index),
        "basis": ("Bruker method XML first via stan.metrics.scoring"
                  "._bruker_spd_from_xml, filename second via "
                  "_spd_from_method_string — the same functions the search "
                  "pipeline uses, so there is one definition rather than a "
                  "second copy in SQL."),
        "note": ("`metadata` is the audit trail for this work: it should "
                 "dominate. `file_unavailable` is a permanent limit — the "
                 "acquisition is in Compass but its .d is on neither archive. "
                 "`unresolved` means the .d IS there and the XML did not "
                 "parse, which is a bug worth chasing, not a limit. Blanks "
                 "and standards DO run a gradient; only washes and system "
                 "procedures genuinely have none."),
    }
    doc.pop("method_names", None)
    return {"applied": True, "n_buckets": len(buckets), "resolved": src,
            "n_disagreements": len(disagreements),
            "n_wash": n_wash, "n_control": n_control}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("json_file")
    ap.add_argument("--out", default=None)
    ap.add_argument("--no-xml", action="store_true",
                    help="Filename only. For a quick run; the whole point of "
                         "this stage is the XML.")
    ap.add_argument("--rebuild-index", action="store_true")
    args = ap.parse_args(argv)
    with open(args.json_file) as fh:
        doc = json.load(fh)
    idx = None if args.no_xml else build_index(rebuild=args.rebuild_index)
    info = resolve(doc, use_xml=not args.no_xml, index=idx)
    out = args.out or args.json_file
    tmp = out + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh)
    import os
    os.replace(tmp, out)
    print(json.dumps(info), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
