"""How many indexed .d runs have lost their spectra?

Stat-only audit (no compute): for runs with no ion cloud, check whether
the .d still has analysis.tdf_bin. A .d with analysis.tdf but no
analysis.tdf_bin has its metadata and none of its data — 4DFF, PEG,
drift and TIC extraction all fail on it, each with a different unhelpful
error.
"""
from pathlib import Path
from stan.db_pg import _connect

pg = _connect()
cur = pg.cursor()
cur.execute("""
    SELECT r.run_name, r.raw_path, r.run_date::date FROM runs r
    WHERE r.raw_path LIKE '%%.d'
      AND NOT EXISTS (SELECT 1 FROM feature_clouds f WHERE f.run_id = r.id::text)
    ORDER BY r.run_date DESC LIMIT 200
""")
rows = cur.fetchall()

missing_dir = no_tdf = no_bin = ok = 0
examples = []
for name, raw, date in rows:
    d = Path(raw)
    try:
        if not d.is_dir():
            missing_dir += 1
            continue
        if not (d / "analysis.tdf").exists():
            no_tdf += 1
            continue
        if not (d / "analysis.tdf_bin").exists():
            no_bin += 1
            if len(examples) < 8:
                examples.append(f"  {date}  {name[:52]}")
            continue
        ok += 1
    except OSError:
        missing_dir += 1

print("200 newest runs with no ion cloud:")
print(f"  .d dir not present/readable : {missing_dir}")
print(f"  no analysis.tdf             : {no_tdf}")
print(f"  SPECTRA GONE (no tdf_bin)   : {no_bin}")
print(f"  intact, just never 4DFF'd   : {ok}")
if examples:
    print("\nexamples of runs whose spectra are gone:")
    print("\n".join(examples))
