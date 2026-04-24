"""Bruker 4D feature finder (4DFF / `uff-cmdline2`) management.

Wraps the Bruker universal feature finder shipped by AlphaPept at
`ext/bruker/FF/`. Provides auto-download (pinned to a specific
alphapept commit SHA so upstream changes cannot silently corrupt
our binaries), SHA256 verification, Linux + Windows invocation, and
a polars reader for the resulting `.features` SQLite database.

Why 4DFF rather than our built-in MS1-histogram drift detector:
the histogram approach picks the densest 1/K0 mode inside each m/z
slice, which is dominated by +1 solvent/autolysis contamination at
~1/K0 1.20-1.30 whenever a window's peptide-zone coverage is low.
Brett's canonical false-positive (file 21149, m/z 299-325, +1 at
1.125 drove the mode even though the ridge was fine) motivated the
switch to a real charge-aware feature finder. 4DFF returns charge
state per feature, so we can restrict drift analysis to z=2 peptide
features only — +1 contamination is filtered by construction.

Binaries are downloaded from raw.githubusercontent.com at a pinned
SHA. License text (THIRD-PARTY-LICENSE-README.txt from alphapept) is
copied next to the binaries on install — redistribution requires it.
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform as _platform
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Pinned alphapept commit — never use `master` (mutable ref risks
# silent breakage when upstream changes the vendored binaries).
# Bump this SHA deliberately and re-verify all hashes below when we
# want to adopt a newer uff-cmdline2 build.
# ─────────────────────────────────────────────────────────────────
_ALPHAPEPT_PINNED_SHA = "c81f6f7072cad940aa95328f49d6b6cb6b252669"

_BASE_URL = (
    f"https://raw.githubusercontent.com/MannLabs/alphapept/"
    f"{_ALPHAPEPT_PINNED_SHA}/alphapept/ext/bruker/FF"
)

# Files shipped per platform. SHA256s computed at pin time against
# the pinned commit; verified after each download. Mismatch aborts
# install so a tampered CDN cannot hand us a malicious binary.
#
# Shared (both platforms): the default 4DFF config + license text.
# redistribution of the Bruker libraries requires shipping the
# license text alongside — do not drop it from the install set.
_SHARED_FILES: dict[str, tuple[str, int]] = {
    "proteomics_4d.config": (
        "07affa3bfcbd5f706cc3dce5394a3ebdbc1378dbd93fbedc24d4fd575fc0cf83",
        14122,
    ),
    "THIRD-PARTY-LICENSE-README.txt": (
        "1323f66b719b428d678a2066073575a1c5d9ffd2d9a3a1ea6433133e37cbd9fd",
        41030,
    ),
}

_LINUX_FILES: dict[str, tuple[str, int]] = {
    "linux64/uff-cmdline2": (
        "c3f7ed20b95eb0ba6430050315d77d884aa5ab7e8f1d6e5637290075a25663b7",
        102_532_736,
    ),
    "linux64/libtbb.so.2": (
        "230658d6d883c348a312b74f0d1002a46c43f94679d87d12202dc653552b7112",
        2_264_552,
    ),
}

_WINDOWS_FILES: dict[str, tuple[str, int]] = {
    "win64/uff-cmdline2.exe": (
        "b301a6a7f1ce477372dbab2ff24fdf8ef1deb1921534e7cb1cf57eb03e3c50a5",
        64_634_600,
    ),
    "win64/tbb.dll": (
        "692380cecd03181d7fd536e4402783e7f38ea0765b35bb52a3236256959b40cd",
        401_280,
    ),
}


def _detect_platform(override: str | None = None) -> str:
    """Return `"linux"` or `"windows"` (lowercase), honouring override."""
    if override:
        p = override.lower()
        if p not in ("linux", "windows"):
            raise ValueError(
                f"platform override must be 'linux' or 'windows', got {override!r}"
            )
        return p
    system = _platform.system()
    if system == "Linux":
        return "linux"
    if system == "Windows":
        return "windows"
    raise RuntimeError(
        f"4DFF is not available on {system}. "
        "Bruker universal feature finder ships for Linux x86_64 and Windows x64 only."
    )


def _install_dir(platform: str) -> Path:
    """Where the Bruker FF binary + libs live for ``platform``.

    Default: ``get_user_config_dir() / "bruker_ff" / <platform>``.

    Override via ``STAN_BRUKER_FF_DIR`` env var — useful on HPC (UC
    Davis Hive) where home directories have tight quotas and can fill
    up. Brett 2026-04-24: ``~/.stan/bruker_ff/linux/`` on Hive holds a
    102 MB binary + 2 MB .so — set the env to a shared-group path like
    ``/quobyte/proteomics-grp/brett/bruker_ff`` so the install is both
    durable and visible to the whole lab.
    """
    import os
    from stan.config import get_user_config_dir

    override = os.environ.get("STAN_BRUKER_FF_DIR")
    if override:
        d = Path(override) / platform
    else:
        d = get_user_config_dir() / "bruker_ff" / platform
    d.mkdir(parents=True, exist_ok=True)
    return d


def _binary_name(platform: str) -> str:
    """Filename of the uff-cmdline2 executable on <platform>."""
    return "uff-cmdline2.exe" if platform == "windows" else "uff-cmdline2"


def _binary_path(platform: str) -> Path:
    """Where the uff-cmdline2 executable should live after install."""
    return _install_dir(platform) / _binary_name(platform)


def _config_path(platform: str) -> Path:
    """Default proteomics_4d.config path inside the install dir."""
    return _install_dir(platform) / "proteomics_4d.config"


def _files_for(platform: str) -> dict[str, tuple[str, int]]:
    """All files that must be present for this platform's install."""
    files: dict[str, tuple[str, int]] = dict(_SHARED_FILES)
    if platform == "linux":
        files.update(_LINUX_FILES)
    else:
        files.update(_WINDOWS_FILES)
    return files


def _sha256_file(path: Path) -> str:
    """Compute SHA256 of a file on disk."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_4dff_installed(platform: str | None = None) -> bool:
    """True iff uff-cmdline2 + config + libs are present and hash-valid.

    Passing ``platform`` lets a mac driving a remote Linux install
    query without raising — pass ``"linux"`` explicitly for that.
    """
    try:
        plat = _detect_platform(platform)
    except RuntimeError:
        return False

    expected = _files_for(plat)
    install_dir = _install_dir(plat)
    for rel, (expected_sha, _size) in expected.items():
        # Flatten: linux64/uff-cmdline2 → install_dir/uff-cmdline2
        flat = install_dir / Path(rel).name
        if not flat.exists():
            return False
        # Hash check is cheap for small files; for the 100 MB binary
        # it adds ~0.3 s but guards against partial downloads and
        # disk corruption. Skip only if we're hot-pathing inside run.
        try:
            if _sha256_file(flat) != expected_sha:
                return False
        except OSError:
            return False
    return True


def install_4dff(platform: str | None = None, force: bool = False) -> Path:
    """Download + install 4DFF binaries for the requested platform.

    Returns the path to ``uff-cmdline2`` (or ``uff-cmdline2.exe``).
    Idempotent: skips files already present with a matching SHA256
    unless ``force=True``. Aborts on any SHA mismatch after download.
    """
    import httpx

    plat = _detect_platform(platform)
    install_dir = _install_dir(plat)
    expected = _files_for(plat)

    for rel, (expected_sha, expected_size) in expected.items():
        flat = install_dir / Path(rel).name
        if flat.exists() and not force:
            try:
                if _sha256_file(flat) == expected_sha:
                    logger.debug("4DFF file OK (cached): %s", flat.name)
                    continue
            except OSError:
                pass
            logger.warning("Cached %s has bad SHA, re-downloading", flat.name)

        url = f"{_BASE_URL}/{rel}"
        tmp = flat.with_suffix(flat.suffix + ".part")
        logger.info("Downloading 4DFF file: %s (%d bytes)", rel, expected_size)
        try:
            with httpx.stream("GET", url, follow_redirects=True, timeout=600) as r:
                r.raise_for_status()
                with tmp.open("wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
        except Exception as e:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise RuntimeError(
                f"Failed to download {url}: {e}. "
                "Check network; GitHub raw.githubusercontent.com must be reachable."
            ) from e

        actual_sha = _sha256_file(tmp)
        if actual_sha != expected_sha:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(
                f"SHA256 mismatch for {rel}: expected {expected_sha}, got {actual_sha}. "
                f"alphapept pin SHA {_ALPHAPEPT_PINNED_SHA} may need refresh, "
                "or the download was corrupted."
            )
        tmp.replace(flat)
        logger.info("Installed 4DFF file: %s", flat)

    # Linux binary needs +x (zip metadata is lost in raw.githubusercontent
    # downloads). Also chmod libtbb for symmetry — harmless on files that
    # already have it.
    if plat == "linux":
        binary = install_dir / "uff-cmdline2"
        try:
            mode = binary.stat().st_mode
            binary.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError as e:
            logger.warning("Could not chmod +x %s: %s", binary, e)

    return _binary_path(plat)


def find_features_file(d_path: str | Path) -> Path | None:
    """Locate the ``.features`` SQLite file 4DFF wrote for a ``.d`` run.

    v0.2.200 fix (2026-04-24): 4DFF's actual output name is
    ``<d_full_basename>.features`` — the ``.d`` suffix is PRESERVED
    before ``.features`` (e.g. ``foo.d/foo.d.features``). An earlier
    draft used ``d_path.stem`` which strips the ``.d`` and resulted
    in a guaranteed miss. Both name forms are tried, plus the parent
    directory (older 4DFF builds + Ziggy placed the file alongside
    the ``.d`` rather than inside it).

    Returns ``None`` if none of the candidates exist.
    """
    d_path = Path(d_path)
    full = d_path.name              # "foo.d"
    stem = d_path.stem              # "foo"  (legacy alphapept builds)
    candidates = [
        d_path / f"{full}.features",          # 4DFF current: foo.d/foo.d.features
        d_path / f"{stem}.features",          # legacy:        foo.d/foo.features
        d_path.parent / f"{full}.features",   # sibling variant
        d_path.parent / f"{stem}.features",   # Ziggy-style sibling
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


@dataclass
class FeatureFinderResult:
    """Result of a single run_4dff invocation."""
    features_path: Path
    wall_clock_sec: float
    returncode: int
    stdout_tail: str  # last ~4 KB of stdout
    stderr_tail: str


def run_4dff(
    d_path: str | Path,
    timeout_min: int = 30,
    config: str | Path | None = None,
    platform: str | None = None,
) -> FeatureFinderResult:
    """Run Bruker 4D feature finder on a ``.d`` directory.

    Blocks until completion or ``timeout_min`` expires. Raises
    ``FileNotFoundError`` if the binary isn't installed or the ``.d``
    directory is missing. Raises ``subprocess.TimeoutExpired`` on
    timeout. Raises ``RuntimeError`` if the binary exits non-zero
    AND no ``.features`` file was produced.

    Note ``uff-cmdline2`` often exits with non-zero status codes while
    still writing a valid ``.features`` file (it prints spurious
    warnings). We treat "file produced" as the authoritative success
    signal and only raise if BOTH the exit code is non-zero AND no
    features file landed on disk.
    """
    import time as _time

    d_path = Path(d_path).resolve()
    if not d_path.exists() or not d_path.is_dir():
        raise FileNotFoundError(f".d directory not found: {d_path}")

    plat = _detect_platform(platform)
    if not is_4dff_installed(plat):
        raise FileNotFoundError(
            f"4DFF not installed for {plat}. Run `stan install-4dff` first."
        )

    binary = _binary_path(plat)
    cfg = Path(config) if config else _config_path(plat)
    if not cfg.exists():
        raise FileNotFoundError(f"4DFF config not found: {cfg}")

    # Mirror alphapept.feature_finding.extract_bruker() — the only
    # confirmed working invocation. `--ff 4d` selects the 4D mode;
    # `--readconfig` + `--analysisDirectory` are the flag names the
    # binary accepts (verified against alphapept commit pin).
    cmd = [
        str(binary),
        "--ff", "4d",
        "--readconfig", str(cfg),
        "--analysisDirectory", str(d_path),
    ]

    # Linux binary needs libtbb.so.2 on its lib search path. We install
    # libtbb.so.2 next to the binary; add that dir to LD_LIBRARY_PATH.
    env = os.environ.copy()
    if plat == "linux":
        ld = str(_install_dir(plat))
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{ld}:{existing}" if existing else ld

    logger.info("Running 4DFF on %s (timeout %d min)", d_path.name, timeout_min)
    t0 = _time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            env=env,
            timeout=timeout_min * 60,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.error("4DFF timed out after %d min on %s", timeout_min, d_path.name)
        raise
    wall = _time.monotonic() - t0
    logger.info(
        "4DFF finished in %.1fs (rc=%d) for %s",
        wall, proc.returncode, d_path.name,
    )

    features = find_features_file(d_path)
    if features is None:
        raise RuntimeError(
            f"4DFF produced no .features file for {d_path.name} "
            f"(rc={proc.returncode}). "
            f"stderr tail: {proc.stderr[-800:] if proc.stderr else '(empty)'}"
        )

    return FeatureFinderResult(
        features_path=features,
        wall_clock_sec=round(wall, 2),
        returncode=proc.returncode,
        stdout_tail=(proc.stdout or "")[-4000:],
        stderr_tail=(proc.stderr or "")[-4000:],
    )


def read_features(features_path: str | Path, columns: list[str] | None = None):
    """Read the LcTimsMsFeature table from a ``.features`` SQLite DB.

    Returns a polars.DataFrame. Only loads ``columns`` if provided.
    Columns that exist in the schema but not in a particular 4DFF
    version are silently dropped from the selection.
    """
    import sqlite3

    import polars as pl

    features_path = Path(features_path)
    if not features_path.exists():
        raise FileNotFoundError(f"Features file not found: {features_path}")

    with sqlite3.connect(str(features_path)) as con:
        have = {
            r[1] for r in
            con.execute("PRAGMA table_info(LcTimsMsFeature)").fetchall()
        }
        if not have:
            raise RuntimeError(
                f"LcTimsMsFeature table missing from {features_path}. "
                "4DFF may have aborted mid-run."
            )
        if columns:
            keep = [c for c in columns if c in have]
            if not keep:
                raise ValueError(
                    f"None of the requested columns {columns} exist in schema. "
                    f"Available: {sorted(have)}"
                )
            sql = f"SELECT {', '.join(keep)} FROM LcTimsMsFeature"
        else:
            sql = "SELECT * FROM LcTimsMsFeature"
        rows = con.execute(sql).fetchall()
        col_names = [d[0] for d in con.execute(sql).description]

    if not rows:
        return pl.DataFrame({c: [] for c in col_names})
    return pl.DataFrame(
        {c: [r[i] for r in rows] for i, c in enumerate(col_names)}
    )


def uninstall_4dff(platform: str | None = None) -> None:
    """Delete the installed 4DFF binaries (for clean-room testing)."""
    plat = _detect_platform(platform)
    d = _install_dir(plat)
    if d.exists():
        shutil.rmtree(d)
        logger.info("Uninstalled 4DFF from %s", d)
