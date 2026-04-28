# External Tool Reference

Authoritative docs for tools STAN invokes or whose output it parses.
**Always check the primary source before writing code that depends on a flag, column name, or output format** — these tools change behavior between minor versions.

This file consolidates content previously in `CLAUDE.md`. The CLAUDE.md "Check primary sources" rule still applies; this is the lookup table.

---

## Primary sources — fetch these, don't guess

| Tool | Primary source | What to check |
|------|----------------|---------------|
| **DIA-NN** | https://github.com/vdemichev/DiaNN | CLI flags, `report.parquet` schema, `--lib` vs `--use-quant`, changelog |
| **DIA-NN wiki** | https://github.com/vdemichev/DiaNN/wiki | Detailed parameter docs |
| **DIA-NN discussions** | https://github.com/vdemichev/DiaNN/discussions | Known issues, version-specific behavior |
| **Sage** | https://github.com/lazear/sage | CLI flags, JSON config schema, `results.sage.parquet` + `lfq.parquet` schemas |
| **Sage releases** | https://github.com/lazear/sage/releases | Current version, breaking changes |
| **timsrust** | https://github.com/MannLabs/timsrust | Bruker `.d` reading, mzML/MGF conversion |
| **ThermoRawFileParser** | https://github.com/compomics/ThermoRawFileParser | Thermo `.raw` conversion CLI |
| **Percolator** | https://github.com/percolator/percolator | `.pin` format, output columns |
| **Bruker TDF** | https://github.com/MannLabs/alphatims | `analysis.tdf` SQLite schema, `Frames.MsmsType` values |
| **HuggingFace Hub** | https://huggingface.co/docs/huggingface_hub/en/index | API methods, upload/download |
| **watchdog** | https://python-watchdog.readthedocs.io/ | Event types, Observer setup |
| **Polars** | https://docs.pola.rs/ | Expression syntax — changes frequently |
| **FastAPI** | https://fastapi.tiangolo.com/ | Routes, async, Pydantic models |

**Use `web_fetch`** to read raw README.md from GitHub before writing code:

```
web_fetch("https://raw.githubusercontent.com/vdemichev/DiaNN/master/README.md")
web_fetch("https://raw.githubusercontent.com/lazear/sage/master/README.md")
web_fetch("https://github.com/lazear/sage/releases/latest")
web_fetch("https://github.com/vdemichev/DiaNN/releases/latest")
```

If a flag/column isn't confirmed in the primary source, **don't implement it** — add a `# TODO: verify against vX.X` comment and tell Brett.

---

## Current known versions (re-verify before use)

- **DIA-NN**: 2.3.1 (Dec 2025, preview); stable 2.2.0 (May 2025). `.predicted.speclib`, `report.parquet`, `--lib`. Linux requires .NET SDK 8.0.407+. https://github.com/vdemichev/DiaNN/discussions/1366
- **Sage**: actively maintained, check https://github.com/lazear/sage/releases. mzML input, JSON config, outputs `results.sage.parquet` + `lfq.parquet`. Built-in LDA rescoring (Percolator usually redundant).
- **Python**: 3.10+ (pyproject.toml)
- **Polars**: ≥0.20 (API broke at 0.19→0.20)

---

## DIA-NN gotchas

- **Column names change between versions.** Always check `if col in df.columns` before access. Key columns: `Precursor.Id`, `Stripped.Sequence`, `Protein.Group`, `Q.Value`, `PG.Q.Value`, `Fragment.Info`, `Fragment.Quant.Corrected`, `Precursor.Normalised`.
- **`File.Name` vs `Run`**: DIA-NN 1.x used `Run`; 2.x uses `File.Name` (full path). 2.0 also renamed some columns and removed `Fragment.Info` / `Fragment.Quant.Corrected`. `Missed.Cleavages` may be absent.
- **Library format**: 2.x uses `.predicted.speclib` (binary) for predicted, `.parquet` for empirical. Don't assume `.tsv`.
- **Linux requires .NET 8.0.407+** — SLURM job must load module or use container.
- **`--lib` vs `--use-quant`** — different behaviors, check the wiki.

### DIA-NN containers on Hive — CRITICAL

| Container | Path | `.raw` support |
|-----------|------|----------------|
| `diann_2.3.0.sif` (underscore) | `/quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif` | **YES** — .NET bundled |
| `diann2.3.0.sif` (no underscore) | `/quobyte/proteomics-grp/apptainers/diann2.3.0.sif` | **NO** — missing .NET |

Always use the `dia-nn/` underscore version. The `apptainers/` version silently skips `.raw` files and produces a predicted library. The "install .NET Runtime" error is misleading — fix is the right container, not host .NET.

Binary inside container: `/diann-2.3.0/diann-linux` (NOT `diann` on PATH).

```bash
apptainer exec \
    --bind "${DATA_DIR}:/work/data,${FASTA_DIR}:/work/fasta,${OUT_DIR}:/work/out" \
    /quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif \
    /diann-2.3.0/diann-linux \
    --f /work/data/file.raw \
    --fasta /work/fasta/database.fasta \
    --out /work/out/report.parquet \
    ...
```

---

## Sage gotchas

| Raw format | Conversion needed? | Notes |
|------------|-------------------|-------|
| Bruker `.d` (ddaPASEF) | **No** | Sage reads `.d` natively — confirmed in production at UC Davis |
| Thermo `.raw` (DDA) | **Yes** | Convert via ThermoRawFileParser → mzML |

Sage release notes label `.d` support "preliminary/unstable" but it works reliably for ddaPASEF. Don't add timsrust/mzML conversion for Bruker.

- **Config is JSON** — not CLI flags. Schema changes between versions; check current README.
- **Outputs**: `results.sage.parquet` (PSMs), `lfq.parquet` (LFQ). Column names change — verify against current release notes.
- **Built-in LDA** is comparable to Percolator for QC FDR. Don't add Percolator without measuring benefit.
- **`target_fdr`** in config controls internal FDR. Verify exact key in current schema.

---

## Bruker `.d` files

- `.d` is a **directory**, not a file. Stability detection checks total directory size.
- `analysis.tdf` is the SQLite database inside. `Frames.MsmsType`: 0=MS1, 8=ddaPASEF, 9=diaPASEF (verify against current docs — could change).
- `analysis.tdf_bin` is binary frame data — use timsrust/alphatims, don't parse directly.

---

## Thermo `.raw` → mzML

### When conversion is needed

| Search | Tool | Native `.raw` support? | Action |
|--------|------|------------------------|--------|
| DIA | DIA-NN 2.1+ | Yes (Linux + Windows) | Pass `.raw` directly with `--f` |
| DIA | DIA-NN in some Apptainer/Singularity | Sometimes broken (#1468) | Implement mzML fallback, configurable per instrument |
| DDA | Sage | No | Always convert via ThermoRawFileParser |

`instruments.yml` per-Thermo-instrument toggle:
```yaml
- name: "Astral"
  vendor: "thermo"
  raw_handling: "native"       # "native" | "convert_mzml"
```

### ThermoRawFileParser CLI (verified)

Source: https://github.com/compomics/ThermoRawFileParser (v1.4.4, May 2024). Linux requires .NET 8 (`dotnet ThermoRawFileParser.dll`) or Mono (`mono ThermoRawFileParser.exe`).

```bash
# .raw → indexed mzML for Sage
dotnet ThermoRawFileParser.dll \
  -i=/path/to/file.raw \
  -o=/path/to/output_dir/ \
  -f=2 \
  -m=0
```

| Flag | Purpose |
|------|---------|
| `-i=PATH` | Input `.raw` (use `=` not space) |
| `-d=PATH` | Input directory (batch mode) |
| `-o=PATH` | Output directory (use `=`) |
| `-b=PATH` | Output single file (alternative to `-o`) |
| `-f=N` | Format: 0=MGF, 1=mzML, 2=indexed mzML, 3=Parquet, 4=metadata-only |
| `-m=N` | Metadata: 0=JSON, 1=TXT |
| `-p` | Disable Thermo native peak picking (default ON) |
| `-g` | gzip compress |

**Flag syntax uses `=`, not space**: `-i=/path/file.raw` ✓, `-i /path/file.raw` ✗.

### Acquisition mode detection from metadata

Run with `-f=4 -m=0` to extract metadata-only JSON:

```bash
dotnet ThermoRawFileParser.dll -i=/path/file.raw -b=/path/file_metadata.json -f=4 -m=0
```

Parse `ScanFilter` strings:
- Contains `"DIA"` → DIA → DIA-NN
- Contains `"dd-MS2"` or `"Full ms2"` → DDA → Sage

**Don't hardcode string matching** — formats vary by instrument and firmware. Use patterns and log unrecognized strings.

### mzML conversion as a SLURM step

Conversion runs as the first step of the search SLURM job, not a separate job (1h QC run converts in 2–5 min — not worth scheduling overhead).

```python
# stan/search/convert.py
def build_thermo_conversion_script(raw_path, output_dir, trfp_dll_path):
    return (
        f"dotnet {trfp_dll_path} -i={raw_path} -o={output_dir}/ -f=2 -m=0\n"
    )
```

Add `trfp_path` to `instruments.yml`:
```yaml
trfp_path: "/hive/software/ThermoRawFileParser/ThermoRawFileParser.dll"
raw_handling: "convert_mzml"
```

### mzML storage budget

| File | Size (1h Orbitrap QC) |
|------|------------------------|
| `.raw` | 2–4 GB |
| mzML uncompressed indexed | 3–6 GB |
| mzML gzipped (`-g`) | 1–2 GB |

Delete converted mzML after search completes. Make `keep_mzml: false` configurable.

### Don't use MSConvert

ProteoWizard MSConvert needs Windows licensing for vendor libraries on Linux. ThermoRawFileParser is fully open-source, Linux-native, equivalent for proteomics. Don't introduce MSConvert.

---

## Polars gotchas

- **API changes frequently** — major break at 0.19→0.20.
- `map_elements` (new) vs `apply` (old) — check current docs.
- Prefer **lazy** (`pl.scan_parquet`) for large files, **eager** (`pl.read_parquet`) for small. Always pass `columns=` to limit reads.

---

## HuggingFace Hub gotchas

- `huggingface_hub` API changes often — check docs first.
- HF Dataset API has rate limits — nightly consolidation must batch reads, not iterate one-at-a-time.
- `api.upload_file()` for single files; `api.upload_folder()` for directories.

---

## Cross-reference links

| Resource | URL |
|----------|-----|
| STAN GitHub | https://github.com/bsphinney/stan |
| STAN HF Space | https://huggingface.co/spaces/brettsp/stan |
| STAN HF Dataset | https://huggingface.co/datasets/brettsp/stan-benchmark |
| DIA-NN GitHub | https://github.com/vdemichev/DiaNN |
| DIA-NN wiki | https://github.com/vdemichev/DiaNN/wiki |
| DIA-NN discussions | https://github.com/vdemichev/DiaNN/discussions |
| DIA-NN releases | https://github.com/vdemichev/DiaNN/releases |
| Sage GitHub | https://github.com/lazear/sage |
| Sage releases | https://github.com/lazear/sage/releases |
| Sage paper | https://pubs.acs.org/doi/10.1021/acs.jproteome.3c00486 |
| timsrust | https://github.com/MannLabs/timsrust |
| ThermoRawFileParser | https://github.com/compomics/ThermoRawFileParser |
| Percolator | https://github.com/percolator/percolator |
| alphatims | https://github.com/MannLabs/alphatims |
| HF Hub Python | https://huggingface.co/docs/huggingface_hub/en/index |
| Polars | https://docs.pola.rs/ |
| FastAPI | https://fastapi.tiangolo.com/ |
| watchdog | https://python-watchdog.readthedocs.io/ |
| DE-LIMP (sibling) | https://github.com/bsphinney/DE-LIMP |

---

## Public Astral HeLa DIA datasets (PRIDE / ProteomeXchange)

For library building, validation, reference range benchmarking.

| Dataset | PXD ID | Description | Search SW | Notes |
|---------|--------|-------------|-----------|-------|
| Searle et al. 2023 | PXD042704 | Astral DIA HeLa, multiple gradients | EncyclopeDIA | Panorama Public, not PRIDE |
| Stewart et al. 2024 ("Inflection Point") | PXD054015 | Astral HeLa DIA + biofluids/tissues, 200 ng | DIA-NN v1.8.1 lib-free | Best candidate |
| "$10 Proteome" 2025 | PXD066701 | Astral + timsTOF Ultra 2, 200 pg–10 ng | DIA-NN | Has DIA-NN pg_matrix TSVs |
| Stewart et al. 2024 (DDA) | PXD045838 | Astral DDA HeLa, 125 ng | Mascot | DDA only — Track A reference |

Papers:
- Searle: https://pubs.acs.org/doi/10.1021/acs.jproteome.3c00357
- Stewart: https://pubs.acs.org/doi/10.1021/acs.jproteome.4c00384
- Nat Biotech nDIA: https://www.nature.com/articles/s41587-023-02099-7
