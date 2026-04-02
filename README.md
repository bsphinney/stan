# STAN — Standardized proteomic Throughput ANalyzer

> *Know your instrument.*

STAN is an open-source proteomics QC tool for Bruker timsTOF and Thermo Orbitrap instruments.
It watches your raw data directories, runs standardized searches (DIA-NN for DIA, Sage for DDA),
computes instrument health metrics, gates your sample queue automatically, and benchmarks your
performance against the global proteomics community.

**Built at the UC Davis Proteomics Core by Brett Stanley Phinney.**

## Features

- Multi-instrument monitoring (timsTOF + Orbitrap in one dashboard)
- DIA and DDA mode intelligence — right search engine, right metrics, separate leaderboards
- Run & Done gating — pause sample queue automatically on QC failure
- Gradient Reproducibility Score (GRS) — single 0–100 LC health number
- Longitudinal instrument health database (SQLite)
- Community HeLa benchmark — compare against labs worldwide (HF Dataset, CC BY 4.0)
- Instrument health fingerprint — dual-mode DDA+DIA radar diagnostic
- Peptide/precursor-first metrics — not protein count (the right way to benchmark)

## Supported instruments

| Vendor | Instruments | Raw format | Acquisition |
|--------|------------|------------|-------------|
| Bruker | timsTOF Ultra, Ultra 2, Pro 2, SCP | `.d` | diaPASEF, ddaPASEF |
| Thermo | Astral, Exploris 480, Exploris 240 | `.raw` | DIA, DDA |

## Quick start

```bash
pip install stan-proteomics   # coming soon
stan init                      # creates ~/.stan/instruments.yml
stan watch                     # start watching configured directories
stan dashboard                 # open local dashboard
```

## Community benchmark

STAN contributes to an open HF Dataset of HeLa QC runs from labs worldwide.
Browse at: https://huggingface.co/spaces/brettsp/stan

## License

MIT License. Community benchmark dataset: CC BY 4.0.

## Citation

If STAN is useful for your work, please cite:
> Phinney BS. STAN: Standardized proteomic Throughput ANalyzer. UC Davis Proteomics Core (2026).
> https://github.com/bsphinney/stan
