#!/usr/bin/env python3
"""Flip an instrument PC's `processing_mode` via the Hive command queue.

Run from Brett's Mac. Edits the target instrument's `instruments.yml`
on its Hive mirror, sets `processing_mode: hive` (or `local` with
--disable), and drops a command file into the mirror's commands/pending/
queue. The instrument PC's running watcher polls that queue every
~5 min, applies the config via the existing `apply_config` action, and
hot-reloads instruments.yml via the watcher's mtime polling — no
restart needed, no SSH, no clicking on the instrument PC.

Usage:
    python3 scripts/flip_to_hive_mode.py <HOSTNAME>
    python3 scripts/flip_to_hive_mode.py <HOSTNAME> --disable
    python3 scripts/flip_to_hive_mode.py <HOSTNAME> --instrument timsTOF

<HOSTNAME> is the directory under /Volumes/proteomics-grp/STAN/ that
the instrument PC syncs to (e.g. DESKTOP-FOT3DAA, lumosRox, TIMS-10878).

After the queued command is processed (visible in
/Volumes/proteomics-grp/STAN/<HOSTNAME>/commands/done/), every NEW
acquisition the watcher detects will SMB-upload to Hive and SSH-submit
a SLURM job instead of running DIA-NN/Sage locally. Existing watcher
behavior for in-flight files is unchanged — only files that go stable
AFTER the hot-reload take the new path.

Rollback: re-run with --disable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


MIRROR_BASE = Path("/Volumes/proteomics-grp/STAN")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("host", help="Instrument PC hostname (mirror dir name).")
    p.add_argument("--disable", action="store_true",
                   help="Set processing_mode back to 'local' (rollback).")
    p.add_argument("--instrument", default="",
                   help="Substring filter on instrument name. Empty = all.")
    p.add_argument("--mirror-base", type=Path, default=MIRROR_BASE,
                   help=f"Mirror root. Default: {MIRROR_BASE}")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the YAML diff but don't queue the command.")
    args = p.parse_args()

    target_mode = "local" if args.disable else "hive"
    host_dir = args.mirror_base / args.host
    yml_path = host_dir / "instruments.yml"

    if not yml_path.exists():
        print(f"ERROR: {yml_path} not found. Is the mirror mounted "
              f"and is '{args.host}' a real instrument-PC dir?",
              file=sys.stderr)
        return 1

    raw = yml_path.read_text(encoding="utf-8")
    cfg = yaml.safe_load(raw) or {}
    insts = cfg.get("instruments") or []
    if not insts:
        print(f"ERROR: no instruments in {yml_path}", file=sys.stderr)
        return 1

    matched = []
    for inst in insts:
        name = inst.get("name") or ""
        if args.instrument and args.instrument.lower() not in name.lower():
            continue
        old = inst.get("processing_mode") or "local"
        inst["processing_mode"] = target_mode
        matched.append((name, old, target_mode))

    if not matched:
        print(f"ERROR: no instruments matched filter {args.instrument!r}",
              file=sys.stderr)
        return 1

    new_yaml = yaml.safe_dump(cfg, sort_keys=False, default_flow_style=False)

    print(f"Target host:     {args.host}")
    print(f"Instruments.yml: {yml_path}")
    for name, old, new in matched:
        flag = " (no change)" if old == new else ""
        print(f"  {name}: processing_mode {old} -> {new}{flag}")

    if args.dry_run:
        print()
        print("--- new instruments.yml (NOT written) ---")
        print(new_yaml)
        return 0

    # Drop the apply_config command. The watcher polls
    # commands/pending/ and runs the existing _action_apply_config which:
    #   1. Validates the YAML
    #   2. Backs up the prior file as .bak
    #   3. Atomically replaces instruments.yml
    #   4. Re-uploads the new config back to the mirror
    # ConfigWatcher then hot-reloads on mtime change within ~30s.
    pending_dir = host_dir / "commands" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    cmd_id = (
        now.strftime("%Y%m%dT%H%M%S%f")
        + f"-apply_config-{os.getpid()}"
    )
    payload = {
        "id": cmd_id,
        "action": "apply_config",
        "args": {
            "filename": "instruments.yml",
            "content": new_yaml,
            "backup": True,
        },
        "created_at": now.isoformat(),
    }
    cmd_path = pending_dir / f"{cmd_id}.json"
    cmd_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print()
    print(f"Queued: {cmd_path}")
    print()
    print("The instrument PC's watcher will pick this up on its next")
    print("poll (~5 min). Once applied, watch:")
    print(f"  {host_dir}/commands/done/{cmd_id}.json")
    print("  to confirm. Then the next stable acquisition uploads to")
    print(f"  Y:\\STAN\\incoming\\<instrument>\\ instead of running search")
    print("  locally.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
