#!/usr/bin/env python3
"""bruker_alert.py -- email new timsTOF instrumentation failures.

Reads the failures the Bruker extractor pulls from the instrument's Compass
Server backup (missing Evotip, LC pressure/clog, MS software error, connection
lost, ...), and emails any that are NEW since the last run. Dedup is by the
acquisition filename, which is unique per run; a lookback window keeps a first
run (or a restored state file) from dredging up months of old failures.

Nightly, because the backup it reads is nightly. Read-only. Sends through the
local mail relay (the same one SLURM --mail-user uses).

    bruker_alert.py --json <maintenance.json> [--to a@b.edu] [--seed] [--dry-run]

--seed marks everything currently present as already-seen and sends nothing --
run it once at install so the backlog is not emailed.
"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

DEFAULT_TO = "bsphinney@ucdavis.edu"
DEFAULT_STATE = "/quobyte/proteomics-grp/STAN/bruker_alert_state.json"
# Categories worth an email. "Other failure" is intentionally excluded -- it is
# the catch-all and tends to be noise; add it with --all-categories if wanted.
ALERT_CATEGORIES = {
    "Evotip missing / not picked up",
    "LC pressure / clog",
    "MS / acquisition software error",
    "Connection lost",
}


def load_state(path):
    try:
        return set(json.load(open(path)).get("seen", []))
    except Exception:
        return set()


def save_state(path, seen):
    tmp = path + ".tmp"
    json.dump({"seen": sorted(seen), "updated": datetime.now().isoformat()}, open(tmp, "w"))
    os.replace(tmp, path)


def parse_dt(s):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except (ValueError, TypeError):
            pass
    return None


def send_mail(to, subject, body, sender="stan-bruker-alerts@hive.hpc.ucdavis.edu"):
    msg = (f"From: STAN Bruker alerts <{sender}>\n"
           f"To: {to}\nSubject: {subject}\n"
           f"Content-Type: text/plain; charset=utf-8\n\n{body}")
    p = subprocess.run(["/usr/sbin/sendmail", "-t"], input=msg.encode(),
                       capture_output=True)
    return p.returncode == 0, p.stderr.decode()[:200]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default="/quobyte/proteomics-grp/STAN/bruker_maintenance.json")
    ap.add_argument("--to", default=DEFAULT_TO)
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--lookback-days", type=int, default=3,
                    help="ignore failures older than this (guards first run)")
    ap.add_argument("--all-categories", action="store_true")
    ap.add_argument("--seed", action="store_true",
                    help="mark current failures seen, send nothing")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    d = json.load(open(a.json))
    inst = (d.get("instrument") or {}).get("name", "timsTOF")
    fails = d.get("failures_recent", [])
    cats = None if a.all_categories else ALERT_CATEGORIES
    cutoff = datetime.now() - timedelta(days=a.lookback_days)

    seen = load_state(a.state)

    def key(f):
        return f.get("fname") or f"{f.get('start_date')}|{f.get('well')}"

    fresh = []
    for f in fails:
        if key(f) in seen:
            continue
        if cats is not None and f.get("category") not in cats:
            continue
        dt = parse_dt(f.get("start_date"))
        if dt and dt < cutoff:
            continue
        fresh.append(f)

    if a.seed:
        for f in fails:
            seen.add(key(f))
        if not a.dry_run:
            save_state(a.state, seen)
        print(f"seeded state with {len(fails)} failures; no email sent")
        return 0

    if not fresh:
        print("no new instrumentation failures")
        return 0

    # compose
    by_cat = {}
    for f in fresh:
        by_cat.setdefault(f.get("category", "?"), []).append(f)
    lines = [f"{len(fresh)} new instrumentation failure(s) on {inst}, "
             f"from the {d.get('backup_date','?')} Bruker backup:", ""]
    for cat, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"== {cat} ({len(items)}) ==")
        for f in sorted(items, key=lambda x: x.get("start_date") or "", reverse=True):
            lines.append(f"  {f.get('start_date','?')}  {f.get('well','?'):7}  {f.get('fname','?')}")
            m = (f.get("message") or "").strip()
            if m:
                lines.append(f"      {m[:150]}")
        lines.append("")
    lines += ["-- ",
              "STAN Bruker maintenance alerter (nightly, from the Compass backup).",
              f"Full picture: https://ucd.stan-proteomics.org  (Maintenance tab)"]
    body = "\n".join(lines)
    subj = f"[STAN] {len(fresh)} timsTOF failure(s) — " + \
           ", ".join(f"{len(v)} {k.split('/')[0].strip().split()[0].lower()}" for k, v in
                     sorted(by_cat.items(), key=lambda kv: -len(kv[1])))

    if a.dry_run:
        print("SUBJECT:", subj)
        print(body)
        return 0

    ok, err = send_mail(a.to, subj, body)
    if ok:
        for f in fresh:
            seen.add(key(f))
        save_state(a.state, seen)
        print(f"emailed {len(fresh)} new failure(s) to {a.to}")
        return 0
    print("sendmail FAILED:", err, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
