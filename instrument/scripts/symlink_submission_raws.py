#!/usr/bin/env python3
"""symlink_submission_raws.py — link instrument raw files into their CoreOmics project folder.

Raw files land in a flat per-instrument incoming directory and encode the CoreOmics
submission NUMBER in the filename:

    20260827_793_100spd_SI-60_S6-D8_1_24040.d
    ^date    ^submission number (-> internal_id PROT_0793)

CoreOmics organises submissions on disk as

    <coreomics-root>/projects/<YYYY>/<MM>/<submission_id>/

where <submission_id> is the 12-hex API id and YYYY/MM come from the SUBMITTED date.
This script resolves number -> id via the CoreOmics REST API, then symlinks each raw
into that folder under a `raw/` subdirectory.

If the submission's CoreOmics folder does not exist, the links go to the fallback
service directory instead (--fallback-root), under `<fallback>/PROT_XXXX/`.

Dry-run by default. Pass --commit to actually create links. Idempotent: an existing
correct link is left alone; a link pointing somewhere else is repaired only with
--repair, never silently.

CAVEAT — symlinks and containers: Apptainer cannot follow a symlink whose target is
outside the bind mount. A DIA-NN/Sage job reading these links MUST bind the real
parent (e.g. --bind /quobyte:/quobyte) or the files will appear to not exist. These
links are for organisation and provenance; they are not a data-staging mechanism.

Usage
-----
    # what would happen, for one submission
    python3 symlink_submission_raws.py --submission 793

    # do it
    python3 symlink_submission_raws.py --submission 793 --commit

    # every submission found in the incoming tree
    python3 symlink_submission_raws.py --all --commit

    # reuse a cached number->id map (avoids a ~60s API crawl)
    python3 symlink_submission_raws.py --all --map-cache ~/.coreomics_submission_map.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

DEFAULT_API = "https://ucdavis.coreomics.com/server/api"
DEFAULT_LAB = "PROTEOMICS"
DEFAULT_SOURCE = "/quobyte/proteomics-grp/STAN/incoming"
# The LIVE CoreOmics tree is the Flinders NFS export, written by amschaal's
# pipeline (months current through the latest submission). The Quobyte copy at
# /quobyte/proteomics-grp/coreomics is a STALE mirror that stopped at 2026-03 /
# May 21 -- do not use it. Layout: projects/<year>/<month>/<hex-id>/ with a
# CoreOmics-managed .submission/ + share/, and friendly symlink views under
# views/{monthly,institute,pi,submission_id}/.
DEFAULT_COREOMICS = "/nfs/lssc0/flinders/proteomics/coreomics"
DEFAULT_FALLBACK = "/quobyte/proteomics-grp/de-limp/users/brettsp/service"
RAW_SUFFIXES = (".d", ".raw", ".wiff", ".mzML")

# 8-digit date, then the submission number. Anchored so a stray 3-digit token later in
# the name (plate wells, sample counts, instrument serials) can never be mistaken for it.
FNAME_RE = re.compile(r"^(?P<date>\d{8})_(?P<subnum>\d{2,4})_")


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# CoreOmics API
# ─────────────────────────────────────────────────────────────────────────────

def read_token(explicit: str | None) -> str:
    if explicit:
        return explicit.strip()
    if os.environ.get("COREOMICS_TOKEN"):
        return os.environ["COREOMICS_TOKEN"].strip()
    p = Path.home() / ".coreomics_token"
    if p.exists():
        return p.read_text().strip()
    sys.exit("FATAL: no CoreOmics token (--token, $COREOMICS_TOKEN, or ~/.coreomics_token)")


def api_get(base: str, path: str, token: str, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        f"{base.rstrip('/')}/{path.lstrip('/')}",
        headers={"Authorization": f"Token {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit(f"FATAL: HTTP {e.code} from CoreOmics — token expired or unauthorized.")
        raise


def build_submission_map(base: str, lab: str, token: str) -> dict[str, dict]:
    """internal_id (PROT_0793) -> {id, submitted, year, month, name, email}."""
    out: dict[str, dict] = {}
    page = 1
    while True:
        d = api_get(base, f"submissions/?lab={lab}&page={page}&page_size=200", token)
        for s in d.get("results", []):
            iid = s.get("internal_id")
            submitted = s.get("submitted") or ""
            if not iid or len(submitted) < 7:
                continue
            out[iid] = {
                "id": s["id"],
                "submitted": submitted,
                "year": submitted[:4],
                "month": submitted[5:7],
                "name": f"{s.get('first_name','')} {s.get('last_name','')}".strip(),
                "email": s.get("email", ""),
                "institute": s.get("institute", ""),
            }
        if not d.get("next"):
            break
        page += 1
    return out


def load_map(args) -> dict[str, dict]:
    cache = Path(args.map_cache).expanduser() if args.map_cache else None
    if cache and cache.exists() and not args.refresh_map:
        m = json.loads(cache.read_text())
        log(f"submission map: {len(m)} entries (cached {cache})")
        return m
    log("fetching submission map from CoreOmics API ...")
    m = build_submission_map(args.api, args.lab, read_token(args.token))
    log(f"submission map: {len(m)} entries (live)")
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(m, indent=1))
        log(f"cached -> {cache}")
    return m


# ─────────────────────────────────────────────────────────────────────────────
# Scanning
# ─────────────────────────────────────────────────────────────────────────────

def scan_raws(source: Path) -> dict[str, list[Path]]:
    """Walk the incoming tree; group raw paths by submission number token."""
    by_num: dict[str, list[Path]] = defaultdict(list)
    if not source.exists():
        sys.exit(f"FATAL: source not found: {source}")
    for inst_dir in sorted(p for p in source.iterdir() if p.is_dir()):
        for entry in sorted(inst_dir.iterdir()):
            if not entry.name.endswith(RAW_SUFFIXES):
                continue
            m = FNAME_RE.match(entry.name)
            if m:
                by_num[m.group("subnum")].append(entry)
    return by_num


def dest_for(subnum: str, meta: dict | None, args) -> tuple[Path, str]:
    """Return (destination dir, reason). Falls back when the CoreOmics folder is absent."""
    internal_id = f"PROT_{int(subnum):04d}"
    fallback = Path(args.fallback_root) / internal_id / "raw"
    if meta is None:
        return fallback, "no CoreOmics submission matches this number"
    proj = Path(args.coreomics_root) / "projects" / meta["year"] / meta["month"] / meta["id"]
    if proj.is_dir():
        return proj / "raw", f"CoreOmics {meta['year']}/{meta['month']}/{meta['id']}"
    return fallback, f"CoreOmics folder absent ({meta['year']}/{meta['month']}/{meta['id']})"


def link_one(src: Path, dest_dir: Path, args) -> str:
    """Create one symlink to the REAL file. Returns a status string.

    ``src`` in the incoming tree is itself a symlink to the raw on the Flinders
    export, so we resolve it to the actual file first: a link into the CoreOmics
    folder (also on Flinders) then points straight at the raw, one hop, and does
    not depend on the STAN incoming staging dir persisting.
    """
    target = Path(os.path.realpath(src))
    link = dest_dir / src.name
    if link.is_symlink():
        current = os.readlink(link)
        if Path(current) in (target, src):
            return "ok"
        if not args.repair:
            return f"CONFLICT (-> {current}; use --repair)"
        if args.commit:
            link.unlink()
            link.symlink_to(target)
        return "repaired"
    if link.exists():
        return "CONFLICT (real file/dir in the way)"
    if args.commit:
        dest_dir.mkdir(parents=True, exist_ok=True)
        link.symlink_to(target)
    return "linked"


# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--submission", action="append", default=[],
                    help="submission number(s) to process, e.g. 793 (repeatable)")
    ap.add_argument("--all", action="store_true", help="process every submission found")
    ap.add_argument("--source", default=DEFAULT_SOURCE, help=f"raw root (default {DEFAULT_SOURCE})")
    ap.add_argument("--coreomics-root", default=DEFAULT_COREOMICS)
    ap.add_argument("--fallback-root", default=DEFAULT_FALLBACK,
                    help="where links go when the CoreOmics folder is missing")
    ap.add_argument("--api", default=DEFAULT_API)
    ap.add_argument("--lab", default=DEFAULT_LAB)
    ap.add_argument("--token")
    ap.add_argument("--map-cache", help="path to cache the number->id map")
    ap.add_argument("--refresh-map", action="store_true", help="ignore an existing cache")
    ap.add_argument("--repair", action="store_true", help="fix symlinks pointing elsewhere")
    ap.add_argument("--commit", action="store_true", help="actually create links (default: dry-run)")
    args = ap.parse_args()

    if not args.all and not args.submission:
        ap.error("give --submission NNN (repeatable) or --all")

    by_num = scan_raws(Path(args.source))
    if not by_num:
        log(f"no raw files matching {FNAME_RE.pattern!r} under {args.source}")
        return 1

    wanted = sorted(by_num) if args.all else [str(int(s)) for s in args.submission]
    submap = load_map(args)
    by_internal = {k: v for k, v in submap.items()}

    mode = "COMMIT" if args.commit else "DRY-RUN"
    log(f"\n=== {mode} — source {args.source} ===\n")

    totals = defaultdict(int)
    for subnum in wanted:
        files = by_num.get(subnum, [])
        if not files:
            log(f"submission {subnum}: no raw files found under {args.source}")
            continue
        internal_id = f"PROT_{int(subnum):04d}"
        meta = by_internal.get(internal_id)
        dest, reason = dest_for(subnum, meta, args)

        who = f"{meta['name']} <{meta['email']}> — {meta['institute']}" if meta else "UNMATCHED"
        log(f"{internal_id}  ({len(files)} runs)  {who}")
        log(f"   -> {dest}")
        log(f"      [{reason}]")

        statuses = defaultdict(int)
        for f in files:
            st = link_one(f, dest, args)
            statuses[st] += 1
            if st.startswith("CONFLICT"):
                log(f"      ! {f.name}: {st}")
        log("      " + ", ".join(f"{v} {k}" for k, v in sorted(statuses.items())))
        log()
        for k, v in statuses.items():
            totals[k] += v

    log("=== totals: " + (", ".join(f"{v} {k}" for k, v in sorted(totals.items())) or "nothing") + " ===")
    if not args.commit:
        log("dry-run — nothing was written. Re-run with --commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
