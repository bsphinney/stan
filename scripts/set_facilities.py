"""Upload the community site's facility records, ``identity/facilities.json``.

The community page counts labs as facilities (relay 1.9.0, community
redesign P3c, spec §A.5). The relay reads this file from the dataset
``brettsp/stan-benchmark`` and never writes it; this script is how it is
written. Each record maps an opaque public id (``f1``) to every lab name one
facility submits under, and to the window in which it submitted under the
default name 'Anonymous Lab'::

    {"f1": {"names": ["Clogged PeakTail", "Clogged Peaktail", "CloggedPeakTail"],
            "anonymous_from": "2026-04-30T23:29:00Z",
            "anonymous_until": "2026-05-01T18:27:00Z"}}

``scripts/community_facilities.json`` is the copy under version control, and
the default input. Today it holds Brett's decision of 2026-10-05: UC Davis is
one facility, ``f1``, covering "Clogged PeakTail" with the two other
spellings of it claimed on the relay ("Clogged Peaktail", "CloggedPeakTail"),
and the 127 'Anonymous Lab' rows submitted from 2026-04-30T23:29Z to
2026-05-01T18:26Z by STAN 0.2.282-0.2.290. A name matches whatever its case
and however its spaces are doubled or trimmed, but a space is not optional:
each spelling is listed. The window ends at 18:27:00Z, the end of that minute:
the last of those rows was submitted at 18:26:10.097Z.
Later 'Anonymous Lab' rows stay unattributed.

The file is checked with the relay's own parser (``_parse_facilities`` in
``hf_space/app.py``, loaded from this checkout), so what passes here is
exactly what the relay will use; a file with any problem is refused. A dry
run (the default) then shows the change against the dataset's copy and what
the records attribute on the live ``benchmark_latest.parquet``: rows per
facility, per name and in the anonymous window, the STAN versions of the
window's rows, and every 'Anonymous Lab' row left unattributed.

Usage::

    python scripts/set_facilities.py                    # dry run, default file
    python scripts/set_facilities.py other.json         # dry run, another file
    python scripts/set_facilities.py --yes              # upload
    python scripts/set_facilities.py --no-live          # skip the live-table check

Authentication is the default Hugging Face token cache
(``huggingface-cli login``), as for ``scripts/deploy_hf_space.py``. The relay
re-reads the file within 5 minutes (``FACILITIES_TTL_SEC``); ``POST
/api/admin/refresh-cache`` with the ``X-STAN-Admin`` header applies it at once.
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import io
import json
import logging
import sys
import tempfile
import uuid
from collections import Counter
from pathlib import Path
from types import ModuleType

logger = logging.getLogger("set_facilities")

REPO_ROOT = Path(__file__).resolve().parents[1]
RELAY_APP = REPO_ROOT / "hf_space" / "app.py"
DEFAULT_FILE = Path(__file__).resolve().parent / "community_facilities.json"
COMMIT_MESSAGE = "Set facility records (scripts/set_facilities.py)"


def load_relay(path: Path = RELAY_APP) -> ModuleType:
    """The relay module from this checkout, for its facility parser."""
    name = f"stan_relay_for_facilities_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod   # pydantic resolves string annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


def canonical_text(raw: dict) -> str:
    """The bytes uploaded: two-space JSON, keys in the file's order, newline-terminated."""
    return json.dumps(raw, indent=2, ensure_ascii=False) + "\n"


def check(relay: ModuleType, text: str) -> tuple[dict, dict, list[str]]:
    """(raw JSON, parsed records, problems) for the file's text."""
    try:
        raw = json.loads(text)
    except ValueError as e:
        return {}, {}, [f"not valid JSON: {e}"]
    fmap, problems = relay._parse_facilities(raw)
    return raw, fmap, problems


def attribution(relay: ModuleType, fmap: dict, rows: list[dict]) -> dict:
    """What the records attribute on ``rows`` (display_name, submitted_at, stan_version)."""
    per_facility: dict[str, Counter] = {fid: Counter() for fid in fmap}
    window_versions: dict[str, Counter] = {fid: Counter() for fid in fmap}
    unattributed_anon: list = []
    names_seen: Counter = Counter()
    for r in rows:
        name = r.get("display_name")
        fid = relay._facility_of(name, r.get("submitted_at"), fmap)
        names_seen[name] += 1
        is_anon = relay._facility_key(name) == relay._ANONYMOUS_KEY
        if fid:
            per_facility[fid]["anonymous window" if is_anon else f"name {name!r}"] += 1
            if is_anon:
                window_versions[fid][str(r.get("stan_version") or "")] += 1
        elif is_anon:
            unattributed_anon.append(r.get("submitted_at"))
    unmatched = {fid: sorted(k for k in f.names
                             if not any(relay._facility_key(n) == k for n in names_seen))
                 for fid, f in fmap.items()}
    return {
        "rows": len(rows),
        "per_facility": {fid: dict(c) for fid, c in per_facility.items()},
        "window_versions": {fid: dict(sorted(c.items())) for fid, c in window_versions.items()},
        "anonymous_unattributed": len(unattributed_anon),
        "anonymous_unattributed_span": ([str(min(unattributed_anon)), str(max(unattributed_anon))]
                                        if unattributed_anon else []),
        "names_without_rows": {fid: v for fid, v in unmatched.items() if v},
        "rows_by_name": dict(names_seen),
    }


def _download(relay: ModuleType, filename: str, cache_dir: str) -> Path | None:
    """A dataset file, or None when the dataset has no such file. A Hub that
    cannot be reached raises: it is not an empty dataset (relay._hf_missing_file)."""
    from huggingface_hub import hf_hub_download

    try:
        return Path(hf_hub_download(relay.HF_DATASET_REPO, filename, repo_type="dataset",
                                    cache_dir=cache_dir, force_download=True))
    except Exception as e:
        if relay._hf_missing_file(e):
            return None
        raise


def live_rows(relay: ModuleType, cache_dir: str) -> list[dict]:
    import polars as pl

    path = _download(relay, "benchmark_latest.parquet", cache_dir)
    if path is None:
        return []
    df = pl.read_parquet(path)
    cols = [c for c in ("display_name", "submitted_at", "stan_version") if c in df.columns]
    return df.select(cols).to_dicts()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("file", nargs="?", type=Path, default=DEFAULT_FILE,
                    help=f"facility records to upload (default {DEFAULT_FILE.relative_to(REPO_ROOT)})")
    ap.add_argument("--yes", action="store_true", help="upload; without it this is a dry run")
    ap.add_argument("--no-live", action="store_true",
                    help="skip the attribution check against the live benchmark_latest.parquet")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    relay = load_relay()
    raw, fmap, problems = check(relay, args.file.read_text())
    if problems:
        for p in problems:
            logger.error("problem: %s", p)
        logger.error("%s has %d problem(s); nothing uploaded.", args.file, len(problems))
        return 1
    new_text = canonical_text(raw)
    logger.info("%s: %d facility record(s), no problems.", args.file, len(fmap))

    with tempfile.TemporaryDirectory(prefix="stan_facilities_") as cache:
        current = _download(relay, relay.FACILITIES_FILE, cache)
        old_text = current.read_text() if current is not None else ""
        if current is None:
            logger.info("The dataset has no %s yet (the site counts lab names).", relay.FACILITIES_FILE)
        diff = "".join(difflib.unified_diff(
            old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            fromfile=f"dataset:{relay.FACILITIES_FILE}", tofile=str(args.file)))
        logger.info("%s", diff or "No change against the dataset's copy.")

        if not args.no_live:
            rows = live_rows(relay, cache)
            logger.info("On the live benchmark_latest.parquet:\n%s",
                        json.dumps(attribution(relay, fmap, rows), indent=2, default=str))

        if not args.yes:
            logger.info("Dry run: nothing uploaded. Run again with --yes to upload.")
            return 0
        if old_text == new_text:
            logger.info("The dataset already holds this file; nothing to upload.")
            return 0
        from huggingface_hub import HfApi
        HfApi().upload_file(path_or_fileobj=io.BytesIO(new_text.encode()), path_in_repo=relay.FACILITIES_FILE,
                            repo_id=relay.HF_DATASET_REPO, repo_type="dataset",
                            commit_message=COMMIT_MESSAGE)
        logger.info("Uploaded %s to %s. The relay reads it within %d minutes; "
                    "POST /api/admin/refresh-cache (X-STAN-Admin) applies it now.",
                    relay.FACILITIES_FILE, relay.HF_DATASET_REPO, relay.FACILITIES_TTL_SEC // 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
