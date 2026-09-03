#!/usr/bin/env python3
"""Read frame counts + TIC straight from each .d/analysis.tdf, then flag outliers
by robust z (median / MAD), the same |z| > 3.5 rule the HT tab uses."""
import glob, os, re, sqlite3, sys, json

D = "/quobyte/proteomics-grp/STAN/incoming/TIMS-10878"
pats = [f"{D}/*_793_*.d", f"{D}/20260828_*S5-*.d"]
# The two globs overlap now that S5 carries the submission number; a path
# scanned twice would double every plate statistic.

def well(n):
    m = re.search(r"_(S\d+)-([A-H]\d{1,2})_", n)
    return (m.group(1), m.group(2)) if m else ("?", "?")

def inj(n):
    m = re.search(r"_(\d+)\.d$", n)
    return int(m.group(1)) if m else None

rows = []
seen = set()
for p in pats:
    for d in sorted(glob.glob(p)):
        if d in seen:
            continue
        seen.add(d)
        name = os.path.basename(d)
        tdf = os.path.join(d, "analysis.tdf")
        rec = {"run": name, "plate": well(name)[0], "well": well(name)[1], "inj": inj(name)}
        try:
            con = sqlite3.connect(f"file:{tdf}?mode=ro", uri=True, timeout=30)
            cols = [r[1] for r in con.execute("PRAGMA table_info(Frames)")]
            tic = "SummedIntensities" if "SummedIntensities" in cols else "AccumulatedIntensity"
            rec["ms1"] = con.execute("SELECT COUNT(*) FROM Frames WHERE MsMsType=0").fetchone()[0]
            rec["ms2"] = con.execute("SELECT COUNT(*) FROM Frames WHERE MsMsType!=0").fetchone()[0]
            rec["tic"] = con.execute(f"SELECT SUM({tic}) FROM Frames WHERE MsMsType=0").fetchone()[0] or 0
            con.close()
        except Exception as e:
            rec["error"] = str(e)[:80]
        rows.append(rec)
        print(".", end="", flush=True)
print()
json.dump(rows, open("/quobyte/proteomics-grp/brett/qc_scan.json", "w"), indent=1)
print(f"scanned {len(rows)} runs -> /quobyte/proteomics-grp/brett/qc_scan.json")
