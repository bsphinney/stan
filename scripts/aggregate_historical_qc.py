"""aggregate_historical_qc.py — Build historical_bsa_summary.json from Sage search results.

Run this on a machine with access to the Quobyte mount or on Hive directly:

    python3 scripts/aggregate_historical_qc.py [--results-dir PATH] [--out PATH]

Reads:
  - <results_dir>/<run_name>/psm_summary.txt    (PSMS=N at 1% spectrum FDR)
  - <results_dir>/<run_name>/psm_ltq.txt        (same, LTQ-tuned Sage params)
  - <results_dir>/<run_name>/results.sage.tsv   (full PSM table)
  - <results_dir>/<run_name>/results.sage.ltq.tsv

Writes:
  - historical_bsa_summary.json (one JSON object per run)

Each record:
  filename       : directory name (= raw file basename)
  era            : instrument era (LTQ, LTQ-FT, QExactive, QExactive+, ...)
  date           : ISO-8601 acquisition date parsed from filename, or null
  bsa_load_fmol  : BSA load in fmol parsed from filename, or null
  psm_count      : PSM count from standard Sage search (spectrum_q ≤ 1%)
  ltq_psm_count  : PSM count from LTQ-tuned Sage search
  best_psm       : max(psm_count, ltq_psm_count) — use this for plotting
  coverage_pct   : BSA sequence coverage % (P02769, 607 AA)
  unique_peptides: unique BSA peptide sequences at 1% spectrum FDR
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# BSA reference sequence — UniProt P02769, bovine serum albumin, 607 AA.
# Extracted directly from the bovine_swissprot.fasta used for the searches.
# ---------------------------------------------------------------------------
BSA_SEQ = (
    "MKWVTFISLLLLFSSAYSRGVFRRDTHKSEIAHRFKDLGEEHFKGLVLIAFSQYLQQCPF"
    "DEHVKLVNELTEFAKTCVADESHAGCEKSLHTLFGDELCKVASLRETYGDMADCCEKQEPE"
    "RNECFLSHKDDSPDLPKLKPDPNTLCDEFKADEKKFWGKYLYEIARRHPYFYAPELLYYAN"
    "KYNGVFQECCQAEDKGACLLPKIETMREKVLASSARQRLRCASIQKFGERALKAWSVARLSQ"
    "KFPKAEFVEVTKLVTDLTKVHKECCHGDLLECADDRADLAKYICDNQDTISSKLKECCDKPL"
    "LEKSHCIAEVEKDAIPENLPPLTADFAEDKDVCKNYQEAKDAFLGSFLYEYSRRHPEYAVS"
    "VLLRLAKEYEATLEECCAKDDPHACYSTVFDKLKHLVDEPQNLIKQNCDQFEKLGEYGFQNA"
    "LIVRYTRKVPQVSTPTLVEVSRSLGKVGTRCCTKPESERMPCTEDYLSLILNRLCVLHEKTP"
    "VSEKVTKCCTESLVNRRPCFSALTPDETYVPKAFDEKLFTFHADICTLPDTEKQIKKQTALV"
    "ELLKHKPKATEEQLKTVMENFVAFVDKCCAADDKEACFAVEGPKLVVSTQTALA"
)
BSA_LEN = 607  # UniProt canonical length; verify: assert len(BSA_SEQ) == 607


# ---------------------------------------------------------------------------
# Date parser — handles the many filename conventions across 2005-2022
# ---------------------------------------------------------------------------
def parse_date(name: str) -> str | None:
    """Return ISO-8601 date from filename, or None if not parseable."""
    # YYYYMMDD anywhere in name (most reliable)
    m = re.search(r"(20\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])", name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # FT / LTQ prefix + YYYYMMDD
    m = re.search(r"[Ff][Tt](\d{4})(\d{2})(\d{2})", name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"[Ll][Tt][Qq](\d{4})(\d{2})(\d{2})", name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # MMDDYY at start of name (e.g. 022806BSA)
    m = re.match(r"^(\d{2})(\d{2})(\d{2})[A-Za-z]", name)
    if m:
        mo, dy, yr = m.group(1), m.group(2), m.group(3)
        yr_full = f"20{yr}" if int(yr) < 50 else f"19{yr}"
        try:
            if 1 <= int(mo) <= 12 and 1 <= int(dy) <= 31:
                return f"{yr_full}-{mo}-{dy}"
        except ValueError:
            pass
    # Trailing _YYMMDD job timestamp (e.g. _060724195947)
    m = re.search(r"_(\d{6})$", name)
    if m:
        s = m.group(1)
        yr, mo, dy = s[0:2], s[2:4], s[4:6]
        try:
            if 1 <= int(mo) <= 12 and 1 <= int(dy) <= 31:
                return f"20{yr}-{mo}-{dy}"
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------------------
# Era detector
# ---------------------------------------------------------------------------
def detect_era(name: str) -> str:
    """Classify filename into instrument era."""
    nl = name.lower()
    if re.search(r"^qep_|^qeplus|^qep\d", nl):
        return "QExactive+"
    if "qe2" in nl and not re.search(r"qe2\d{8}", nl):
        return "QExactive2"
    if re.search(r"^qe[0-9x]|^qex", nl):
        return "QExactive"
    if re.search(r"ltq.*ft|^ft\d|^ft[0-9]", nl):
        return "LTQ-FT"
    if re.search(r"ltq[12]|^ltq\d", nl):
        return "LTQ"
    if "decaxp" in nl or "lcq" in nl:
        return "DecaXP+"
    if "tsq" in nl:
        return "TSQ"
    if re.search(r"^\d{6}bsa|^\d{6}_", nl):
        return "LTQ"
    if re.search(r"^[a-z]{2,3}\d{8}", nl):
        return "LTQ-FT"
    return "Unknown"


# ---------------------------------------------------------------------------
# BSA load parser
# ---------------------------------------------------------------------------
def parse_bsa_load(name: str) -> int | None:
    """Extract fmol load from filename if present."""
    m = re.search(r"(\d+)\s*fmol", name, re.IGNORECASE)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Coverage computation
# ---------------------------------------------------------------------------
def compute_coverage(tsv_path: Path) -> tuple[float | None, int]:
    """Return (coverage_pct, unique_peptide_count) using spectrum_q ≤ 1% FDR.

    Filters to BSA (P02769 / ALBU_BOVIN) peptides only.
    Strips Sage modification notation before sequence matching.
    """
    covered: set[int] = set()
    peptides: set[str] = set()
    try:
        with open(tsv_path) as f:
            header = f.readline().strip().split("\t")
            if "peptide" not in header:
                return None, 0
            pi = header.index("peptide")
            # spectrum_q = PSM-level FDR; matches what psm_summary.txt counts
            si = header.index("spectrum_q") if "spectrum_q" in header else None
            prot_i = header.index("proteins") if "proteins" in header else None
            for line in f:
                cols = line.strip().split("\t")
                if len(cols) <= pi:
                    continue
                if si is not None and si < len(cols):
                    try:
                        if float(cols[si]) > 0.01:
                            continue
                    except ValueError:
                        continue
                if prot_i is not None and prot_i < len(cols):
                    prot = cols[prot_i]
                    if "P02769" not in prot and "ALBU_BOVIN" not in prot:
                        continue
                # Strip Sage mod notation: [+57.021], (-0.984), etc.
                raw_pep = cols[pi]
                pep = re.sub(r"\[.*?\]|\(.*?\)", "", raw_pep).upper()
                pep = re.sub(r"[^A-Z]", "", pep)
                if not pep or pep in peptides:
                    continue
                peptides.add(pep)
                pos = BSA_SEQ.find(pep)
                if pos >= 0:
                    for i in range(pos, pos + len(pep)):
                        covered.add(i)
    except Exception:
        return None, 0

    if not peptides:
        return None, 0
    cov_pct = round(len(covered) / BSA_LEN * 100, 1)
    return cov_pct, len(peptides)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-dir",
        default="/quobyte/proteomics-grp/STAN/historical_bsa/results",
        help="Path to directory containing one sub-dir per raw file",
    )
    parser.add_argument(
        "--out",
        default="historical_bsa_summary.json",
        help="Output JSON path",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Print progress to stderr"
    )
    args = parser.parse_args()

    results_dir = Path(args.results_dir)
    if not results_dir.is_dir():
        print(f"ERROR: results directory not found: {results_dir}", file=sys.stderr)
        sys.exit(1)

    dirs = sorted(d for d in results_dir.iterdir() if d.is_dir())
    total = len(dirs)
    rows: list[dict] = []

    for i, d in enumerate(dirs):
        if args.verbose and i % 100 == 0:
            print(f"  [{i}/{total}] {d.name}", file=sys.stderr)

        psm_count: int | None = None
        ltq_count: int | None = None

        sf = d / "psm_summary.txt"
        if sf.exists():
            for line in sf.read_text().splitlines():
                m = re.match(r"PSMS=(\d+)", line.strip())
                if m:
                    psm_count = int(m.group(1))

        lf = d / "psm_ltq.txt"
        if lf.exists():
            for line in lf.read_text().splitlines():
                m = re.match(r"PSMS=(\d+)", line.strip())
                if m:
                    ltq_count = int(m.group(1))

        best_psm = (
            psm_count
            if (psm_count is not None and psm_count > 0)
            else ltq_count
        )

        # Try standard TSV first, then LTQ TSV; keep whichever gives more coverage
        cov: float | None = None
        uniq = 0
        for tsv_name in ["results.sage.tsv", "results.sage.ltq.tsv"]:
            tsv = d / tsv_name
            if tsv.exists():
                c, u = compute_coverage(tsv)
                if c is not None and (cov is None or u > uniq):
                    cov, uniq = c, u

        rows.append(
            {
                "filename": d.name,
                "era": detect_era(d.name),
                "date": parse_date(d.name),
                "bsa_load_fmol": parse_bsa_load(d.name),
                "psm_count": psm_count,
                "ltq_psm_count": ltq_count,
                "best_psm": best_psm,
                "coverage_pct": cov,
                "unique_peptides": uniq,
            }
        )

    out = Path(args.out)
    out.write_text(json.dumps(rows, indent=2))
    n_psm = sum(1 for r in rows if r["best_psm"] and r["best_psm"] > 0)
    n_cov = sum(1 for r in rows if r["coverage_pct"] is not None)
    print(
        f"Wrote {len(rows)} records to {out}  "
        f"({n_psm} with PSMs, {n_cov} with coverage)",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
